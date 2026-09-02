#!/usr/bin/env python3
"""Index a knowledge/ dir into a flat vector store (dense emb.npy + meta/bodies jsonl).
Multilingual CPU/ONNX embeddings (fastembed). ZERO token, ZERO OAuth.
Notes with frontmatter `status: superseded` are NEVER indexed (contract).

Run PYTHONPATH-cleared (Hermes leaks its venv onto PYTHONPATH -> shadows deps):
  env -u PYTHONPATH python brain_index.py --knowledge <dir> --out <dir>
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

import numpy as np

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"  # 384d, FR-capable, ONNX
MODEL_MAX_TOKENS = 256
CHUNK_MAX_TOKENS = 240
CHUNK_OVERLAP_TOKENS = 32
EMBEDDING_THREADS = 8
EMBEDDING_BATCH_SIZE = 16

# Canonical metadata columns of an index row. Kept in lockstep with build_note_rows()
# by test_index_format.py; changing one without the other fails that test.
META_FIELDS = (
    "path", "type", "kind", "scope", "uid", "tags", "title", "author_agent",
    "model", "created", "status", "preview",
    "chunk_index", "chunk_count", "chunk_byte_start", "chunk_byte_end",
)
INDEX_FORMAT_VERSION = 2


def canonical_source_roots(value, brain_root=None) -> list[str]:
    """Return confined portable roots; ``[]`` keeps the legacy ``knowledge`` default.

    Syntax is checked with both path dialects because manifests are portable: on Windows,
    ``PurePosixPath('C:/outside')`` alone would misclassify a drive path as relative.  When the
    Brain root is known, resolving the candidate also catches junctions/symlinks that escape it.
    """
    if value is None or value == []:
        return ["knowledge"]
    if not isinstance(value, list):
        raise ValueError("source_roots must be a list")
    roots = []
    for item in value:
        if not isinstance(item, str) or not item or "\\" in item:
            raise ValueError("source_roots contains an invalid path")
        pure = PurePosixPath(item)
        windows = PureWindowsPath(item)
        if (
            item == "." or pure.is_absolute() or windows.is_absolute() or windows.drive
            or pure.as_posix() != item
            or any(part in {".", ".."} for part in pure.parts)
        ):
            raise ValueError("source_roots contains an invalid path")
        if brain_root is not None:
            confined_root = Path(brain_root).resolve()
            try:
                (confined_root / item).resolve().relative_to(confined_root)
            except (OSError, ValueError) as exc:
                raise ValueError("source_roots path escapes brain root") from exc
        if item not in roots:
            roots.append(item)
    return roots


def current_embedding_signature():
    return {"model": MODEL, "fastembed": importlib.metadata.version("fastembed")}


def current_index_format_signature():
    """The row shape THIS code produces. Compared against what an index actually holds."""
    return {
        "version": INDEX_FORMAT_VERSION,
        "meta_fields": sorted(META_FIELDS),
        "paths_relative": True,
        "chunk_max_tokens": CHUNK_MAX_TOKENS,
        "chunk_overlap_tokens": CHUNK_OVERLAP_TOKENS,
    }


def observed_index_format_signature(meta):
    """The row shape an index ACTUALLY holds, derived from its rows — never asserted.

    Recording the observation rather than the intent is the point: on 2026-08-04 the live
    index carried absolute UNC paths while the code already intended relative ones, and
    nothing could see the gap because no artefact described the rows as they were.
    """
    rows = [item for item in meta if isinstance(item, dict)]
    fields = sorted({key for item in rows for key in item})
    paths = [item.get("path") for item in rows if isinstance(item.get("path"), str)]
    return {
        "version": INDEX_FORMAT_VERSION,
        "meta_fields": fields,
        "paths_relative": all(not _looks_absolute(path) for path in paths),
        "chunk_max_tokens": CHUNK_MAX_TOKENS,
        "chunk_overlap_tokens": CHUNK_OVERLAP_TOKENS,
    }


def _looks_absolute(path):
    """True for UNC (//host/...), drive-letter (C:/...) and POSIX-absolute paths."""
    text = str(path).replace("\\", "/")
    return text.startswith("/") or re.match(r"^[A-Za-z]:", text) is not None


GRAPH_NAV_GLOB = "projects/*/obsidian"


def discover_graph_roots(brain_root):
    """The generated graph-navigation layers, found rather than declared.

    Deliberately automatic: the documented reindex command is
    `brain_index.py --knowledge ../knowledge --out index`, and it appears in four places
    (README, tooling/README, AGENTS.md, curation-full-ai). Requiring an extra flag would mean
    every rebuild that follows the docs silently drops the graph notes back out of the index —
    a guard nobody can forget beats a note in a README that nobody re-reads.
    """
    return [path for path in sorted(Path(brain_root).glob(GRAPH_NAV_GLOB)) if path.is_dir()]


def collect_note_paths(roots):
    """Every indexable note under `roots`, plus the count skipped as superseded.

    THE single walk: brain_index and brain_eval both call it, so an index and the freshness
    check that judges it can never disagree on which notes are in scope. A root that does not
    exist contributes nothing rather than raising — a project may legitimately have no
    navigation layer generated yet.
    """
    seen = set()
    paths = []
    skipped = 0
    for root in roots:
        base = Path(root)
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.md")):
            resolved = path.resolve()
            if resolved in seen:
                continue
            if path.name == "_TEMPLATE.md":
                continue
            frontmatter, body = parse_note(path)
            if frontmatter.get("status") == "superseded":
                skipped += 1
                continue
            if not body.strip():
                continue
            seen.add(resolved)
            paths.append(path)
    return paths, skipped


def knowledge_fingerprint(paths, relative_to):
    root = Path(relative_to).resolve()
    digest = hashlib.sha256()
    for path in sorted((Path(item).resolve() for item in paths), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _parse_note_source(path: Path):
    txt = path.read_bytes().decode("utf-8")
    fm, body = {}, txt
    body_start = 0
    opening = re.match(r"\A---[ \t]*\r?\n", txt)
    if opening:
        rest = txt[opening.end():]
        closing = re.search(r"(?m)^---[ \t]*\r?$", rest)
        if closing:
            head = rest[:closing.start()]
            tail = rest[closing.end():]
            for ln in head.splitlines():
                s = ln.strip()
                if s and not s.startswith("#") and ":" in s:
                    k, _, v = s.partition(":")
                    fm[k.strip()] = v.strip().strip('"')
            body = tail.strip()
            body_start = closing.end() + opening.end() + len(tail) - len(tail.lstrip())
    return fm, body, body_start, txt


def parse_note(path: Path):
    fm, body, _, _ = _parse_note_source(path)
    return fm, body


def chunk_body(
    body,
    tokenizer,
    max_tokens=CHUNK_MAX_TOKENS,
    overlap_tokens=CHUNK_OVERLAP_TOKENS,
):
    """Split a note body using tokenizer offsets while preserving stable overlap."""
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")
    if overlap_tokens < 0 or overlap_tokens >= max_tokens:
        raise ValueError("overlap_tokens must be between 0 and max_tokens - 1")

    offsets = [(start, end) for start, end in tokenizer.encode(body).offsets if end > start]
    if not offsets:
        return []

    chunks = []
    token_start = 0
    while token_start < len(offsets):
        token_end = min(token_start + max_tokens, len(offsets))
        window = offsets[token_start:token_end]
        char_start = 0 if token_start == 0 else window[0][0]
        char_end = len(body) if token_end == len(offsets) else max(end for _, end in window)
        chunks.append({
            "text": body[char_start:char_end],
            "char_start": char_start,
            "char_end": char_end,
        })
        if token_end == len(offsets):
            break
        token_start = token_end - overlap_tokens
    return chunks


def _utf8_offsets(text, positions):
    offsets = {}
    cursor = 0
    byte_cursor = 0
    for position in sorted(set(positions)):
        byte_cursor += len(text[cursor:position].encode("utf-8"))
        offsets[position] = byte_cursor
        cursor = position
    return offsets


def build_note_rows(
    path,
    tokenizer,
    max_tokens=CHUNK_MAX_TOKENS,
    overlap_tokens=CHUNK_OVERLAP_TOKENS,
    relative_to=None,
):
    fm, body, body_start, source = _parse_note_source(path)
    if fm.get("status") == "superseded" or not body.strip():
        return [], []

    chunks = chunk_body(body, tokenizer, max_tokens=max_tokens, overlap_tokens=overlap_tokens)
    absolute_positions = [
        body_start + position
        for chunk in chunks
        for position in (chunk["char_start"], chunk["char_end"])
    ]
    byte_offsets = _utf8_offsets(source, absolute_positions)
    chunk_count = len(chunks)
    title_match = re.search(r"(?m)^#\s+(.+?)\s*$", body)
    title = title_match.group(1).strip() if title_match else ""
    metadata = []
    bodies = []
    for chunk_index, chunk in enumerate(chunks):
        absolute_start = body_start + chunk["char_start"]
        absolute_end = body_start + chunk["char_end"]
        text = chunk["text"]
        indexed_path = Path(path)
        if relative_to is not None:
            indexed_path = indexed_path.resolve().relative_to(Path(relative_to).resolve())
        metadata.append({
            "path": indexed_path.as_posix(),
            "type": fm.get("type", ""),
            "kind": fm.get("kind", ""),
            "scope": fm.get("scope", ""),
            "uid": fm.get("uid", ""),
            "tags": fm.get("tags", ""),
            "title": title,
            "author_agent": fm.get("author_agent", ""),
            "model": fm.get("model", ""),
            "created": fm.get("created", ""),
            "status": fm.get("status", "active"),
            "preview": text[:200],
            "chunk_index": chunk_index,
            "chunk_count": chunk_count,
            "chunk_byte_start": byte_offsets[absolute_start],
            "chunk_byte_end": byte_offsets[absolute_end],
        })
        bodies.append(text)
    return metadata, bodies


def collect_index_rows(
    paths,
    tokenizer,
    max_tokens=CHUNK_MAX_TOKENS,
    overlap_tokens=CHUNK_OVERLAP_TOKENS,
    relative_to=None,
):
    metadata = []
    bodies = []
    indexed = 0
    for path in paths:
        note_metadata, note_bodies = build_note_rows(
            path,
            tokenizer,
            max_tokens=max_tokens,
            overlap_tokens=overlap_tokens,
            relative_to=relative_to,
        )
        if note_bodies:
            indexed += 1
            metadata.extend(note_metadata)
            bodies.extend(note_bodies)
    return metadata, bodies, indexed


def _write_bytes(path: Path, data: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_reusable_embeddings(index_dir, embedding_signature):
    """Load exact-body vectors from the last coherent compatible generation."""
    index = Path(index_dir)
    pointer = index / "CURRENT"
    try:
        generation = pointer.read_text(encoding="ascii").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", generation):
            return {}
        generations = (index / "generations").resolve()
        snapshot = (generations / generation).resolve()
        snapshot.relative_to(generations)
        manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("generation") != generation:
            return {}
        if manifest.get("embedding_signature") != embedding_signature:
            return {}
        expected_hashes = manifest.get("sha256", {})
        files = (snapshot / "bodies.jsonl", snapshot / "emb.npy")
        if any(_sha256(path) != expected_hashes.get(path.name) for path in files):
            return {}
        cached_bodies = [
            json.loads(line)
            for line in files[0].read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        cached_vecs = np.load(files[1], allow_pickle=False)
        if len(cached_bodies) != len(cached_vecs):
            return {}
        return {
            body: np.asarray(vector, dtype=np.float32)
            for body, vector in zip(cached_bodies, cached_vecs)
            if isinstance(body, str)
        }
    except (OSError, ValueError, json.JSONDecodeError, EOFError):
        return {}


def _publish_pointer(out: Path, generation: str) -> None:
    """Publie out/CURRENT en visant l'atomicite, sans en dependre.

    Sur un partage SMB, os.replace vers une cible qu'un autre process tient ouverte
    echoue en PermissionError WinError 5 : le rename atomique n'y est pas garanti.
    La generation est deja publiee a ce stade, donc laisser le pointeur en arriere
    rendrait la reindexation silencieusement inoperante.

    Voie 1 (preferee) : fichier temporaire + os.replace, atomique la ou c'est possible.
    Voie 2 (repli) : reecriture en place. Le pointeur tient en un seul write de moins
    de 64 octets : un lecteur voit l'ancienne ou la nouvelle valeur, jamais un melange.
    """
    payload = (generation + chr(10)).encode("ascii")
    pointer = out / "CURRENT"
    pointer_tmp = out / f".CURRENT-{secrets.token_hex(8)}"
    try:
        _write_bytes(pointer_tmp, payload)
        try:
            os.replace(pointer_tmp, pointer)
            return
        except PermissionError:
            pass
        with pointer.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        pointer_tmp.unlink(missing_ok=True)

def write_index_snapshot(
    out, meta, bodies, vecs, generation_id=None, embedding_signature=None,
    knowledge_fingerprint=None, index_format_signature=None, source_roots=None,
) -> Path:
    out = Path(out)
    if not (len(meta) == len(bodies) == len(vecs)):
        raise ValueError("index snapshot row counts differ")
    out.mkdir(parents=True, exist_ok=True)
    generations = out / "generations"
    generations.mkdir(parents=True, exist_ok=True)
    generation = generation_id or f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}-{secrets.token_hex(8)}"
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", generation):
        raise ValueError("invalid index generation id")
    final_dir = generations / generation
    temp_dir = generations / f".tmp-{generation}-{secrets.token_hex(8)}"
    temp_dir.mkdir(exist_ok=False)
    try:
        meta_bytes = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in meta).encode("utf-8")
        body_bytes = "".join(json.dumps(body, ensure_ascii=False) + "\n" for body in bodies).encode("utf-8")
        _write_bytes(temp_dir / "meta.jsonl", meta_bytes)
        _write_bytes(temp_dir / "bodies.jsonl", body_bytes)
        with (temp_dir / "emb.npy").open("xb") as handle:
            np.save(handle, np.asarray(vecs, dtype=np.float32), allow_pickle=False)
            handle.flush()
            os.fsync(handle.fileno())
        files = ("meta.jsonl", "bodies.jsonl", "emb.npy")
        manifest = {
            "generation": generation,
            "rows": len(meta),
            "dim": int(vecs.shape[1]) if getattr(vecs, "ndim", 0) == 2 and len(vecs) else 0,
            "sha256": {name: _sha256(temp_dir / name) for name in files},
        }
        if embedding_signature is not None:
            manifest["embedding_signature"] = embedding_signature
        if knowledge_fingerprint is not None:
            manifest["knowledge_fingerprint"] = knowledge_fingerprint
        # WHICH sources were indexed is configuration, not code version: it belongs to the
        # content axis of freshness, never to index_format_signature. Keeping the two apart is
        # what lets each answer exactly one question.
        manifest["source_roots"] = canonical_source_roots(source_roots)
        manifest["index_format_signature"] = (
            index_format_signature
            if index_format_signature is not None
            else observed_index_format_signature(meta)
        )
        _write_bytes(
            temp_dir / "manifest.json",
            (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"),
        )
        os.replace(temp_dir, final_dir)
        _publish_pointer(out, generation)
        return final_dir
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--knowledge", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--also", action="append", default=[], metavar="DIR",
        help="extra note root, repeatable. Must sit under the same Brain root as "
             "--knowledge so paths stay relative.",
    )
    ap.add_argument(
        "--only-knowledge", action="store_true",
        help=f"do NOT auto-discover the graph navigation layers ({GRAPH_NAV_GLOB})",
    )
    a = ap.parse_args()
    kdir, out = Path(a.knowledge).resolve(), Path(a.out)
    brain_root = kdir.parent
    discovered = [] if a.only_knowledge else discover_graph_roots(brain_root)
    roots = [kdir] + discovered + [Path(item).resolve() for item in a.also]
    seen_roots = set()
    roots = [root for root in roots if not (root in seen_roots or seen_roots.add(root))]
    for root in roots:
        # STRICT subpath: accepting brain_root itself would write "." into source_roots, and a
        # consumer confining to the declared roots would then serve the whole Brain — inbox/
        # drafts and .git/ included. Found by security audit 2026-08-04.
        if root == brain_root:
            raise SystemExit(
                f"note root must be a subfolder of the Brain, not the Brain root itself: {root}"
            )
        try:
            canonical = root.relative_to(brain_root)
        except ValueError:
            raise SystemExit(f"note root escapes the Brain root {brain_root}: {root}")
        # Defence in depth: refuse never-servable areas HERE too, on canonical casefolded
        # components. Until now nothing stopped `--also <brain>/inbox`; the leak was avoided
        # only because resolve() happened to canonicalise the case for the consumer to reject.
        # A guard that works by accident is not a guard.
        from brain_context import NEVER_SERVED

        forbidden = [
            part for part in canonical.parts if str(part).casefold() in NEVER_SERVED
        ]
        if forbidden:
            raise SystemExit(
                f"note root is in a never-served area ({', '.join(forbidden)}): {root}"
            )
    out.mkdir(parents=True, exist_ok=True)

    note_paths, skipped = collect_note_paths(roots)

    if note_paths:
        from fastembed import TextEmbedding

        emb = TextEmbedding(model_name=MODEL, threads=EMBEDDING_THREADS)
        emb.model.tokenizer.enable_truncation(max_length=MODEL_MAX_TOKENS)
        offset_tokenizer = type(emb.model.tokenizer).from_str(emb.model.tokenizer.to_str())
        offset_tokenizer.no_truncation()
        meta, bodies, indexed = collect_index_rows(
            note_paths,
            offset_tokenizer,
            relative_to=brain_root,
        )
        signature = current_embedding_signature()
        reusable = load_reusable_embeddings(out, signature)
        missing_bodies = [body for body in bodies if body not in reusable]
        fresh = iter(emb.embed(
            missing_bodies,
            batch_size=EMBEDDING_BATCH_SIZE,
            parallel=None,
        ))
        vecs = np.array([
            reusable[body] if body in reusable else next(fresh)
            for body in bodies
        ], dtype=np.float32)
        vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-12
    else:
        meta, bodies, indexed = [], [], 0
        vecs = np.empty((0, 0), dtype=np.float32)
    snapshot = write_index_snapshot(
        out,
        meta,
        bodies,
        vecs,
        embedding_signature=current_embedding_signature(),
        knowledge_fingerprint=knowledge_fingerprint(note_paths, relative_to=brain_root),
        source_roots=sorted(root.relative_to(brain_root).as_posix() for root in roots),
    )
    payload = {
        "indexed": indexed, "chunks": len(bodies), "skipped_superseded": skipped,
        "generation": snapshot.name,
    }
    if len(bodies):
        payload["dim"] = int(vecs.shape[1])
    print(json.dumps(payload))


if __name__ == "__main__":
    main()
