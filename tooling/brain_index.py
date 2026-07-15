#!/usr/bin/env python3
"""Index a knowledge/ dir into a flat vector store (dense emb.npy + meta/bodies jsonl).
Multilingual CPU/ONNX embeddings (fastembed). ZERO token, ZERO OAuth.
Notes with frontmatter `status: superseded` are NEVER indexed (contract).

Run PYTHONPATH-cleared (Hermes leaks its venv onto PYTHONPATH -> shadows deps):
  env -u PYTHONPATH python brain_index.py --knowledge <dir> --out <dir>
"""
import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"  # 384d, FR-capable, ONNX


def parse_note(path: Path):
    txt = path.read_text(encoding="utf-8")
    fm, body = {}, txt
    if txt.startswith("---"):
        _, _, rest = txt.partition("---")
        head, sep, tail = rest.partition("---")
        if sep:
            for ln in head.splitlines():
                s = ln.strip()
                if s and not s.startswith("#") and ":" in s:
                    k, _, v = s.partition(":")
                    fm[k.strip()] = v.strip().strip('"')
            body = tail.strip()
    return fm, body


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


def write_index_snapshot(out, meta, bodies, vecs, generation_id=None) -> Path:
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
        _write_bytes(
            temp_dir / "manifest.json",
            (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"),
        )
        os.replace(temp_dir, final_dir)
        pointer_tmp = out / f".CURRENT-{secrets.token_hex(8)}"
        try:
            _write_bytes(pointer_tmp, (generation + "\n").encode("ascii"))
            os.replace(pointer_tmp, out / "CURRENT")
        finally:
            pointer_tmp.unlink(missing_ok=True)
        return final_dir
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--knowledge", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    kdir, out = Path(a.knowledge).resolve(), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    meta, bodies, skipped = [], [], 0
    for p in sorted(kdir.rglob("*.md")):
        if p.name == "_TEMPLATE.md":
            continue
        fm, body = parse_note(p)
        if fm.get("status") == "superseded":
            skipped += 1
            continue
        if not body.strip():
            continue
        meta.append({"path": str(p).replace("\\", "/"), "type": fm.get("type", ""),
                     "scope": fm.get("scope", ""), "author_agent": fm.get("author_agent", ""),
                     "model": fm.get("model", ""), "created": fm.get("created", ""),
                     "status": fm.get("status", "active"), "preview": body[:200]})
        bodies.append(body)

    if bodies:
        from fastembed import TextEmbedding

        emb = TextEmbedding(model_name=MODEL)
        vecs = np.array(list(emb.embed(bodies)), dtype=np.float32)
        vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-12
    else:
        vecs = np.empty((0, 0), dtype=np.float32)
    snapshot = write_index_snapshot(out, meta, bodies, vecs)
    payload = {
        "indexed": len(bodies), "skipped_superseded": skipped,
        "generation": snapshot.name,
    }
    if len(bodies):
        payload["dim"] = int(vecs.shape[1])
    print(json.dumps(payload))


if __name__ == "__main__":
    main()
