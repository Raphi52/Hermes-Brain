#!/usr/bin/env python3
"""Retrieve and render bounded Amitel Brain context for prompt injection."""
import argparse
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath


GENERATION_NAME = re.compile(r"[A-Za-z0-9._-]{1,128}")

# Areas that must NEVER be served into an agent prompt, whatever a manifest declares:
# uncurated drafts, repo internals, tooling, governance and local Obsidian state.
# Compared CASEFOLDED, on EVERY path component, and on the CANONICAL name after resolution.
NEVER_SERVED = frozenset({".git", ".obsidian", ".trash", "inbox", "tooling", "governance"})


def _forbidden_component(parts) -> bool:
    return any(str(part).casefold() in NEVER_SERVED for part in parts)


def _acceptable_root(relative: str, brain: Path) -> Path | None:
    """A declared root is served only if it is a STRICT, normalised, non-forbidden subpath.

    Two audit rounds on 2026-08-04, and the lesson of the second is the one that matters:
    validating the STRING is NOT enough, because the filesystem renames the target under you.
      Round 1 — `relative_to()` after `resolve()` accepts any traversal landing back inside, so
      `["."]` served the whole Brain (`inbox/` drafts, `.git/`) and `"knowledge/../inbox"`
      normalised into `inbox/`.
      Round 2 — string checks alone still fell to Windows semantics: `"Inbox"` (the filesystem
      is case-insensitive), `"inbox "` / `"inbox."` (a trailing space or dot is stripped by the
      OS), and an NTFS junction named `projects/p/obsidian` pointing at `inbox/` — a declared
      name in exactly the shape the legitimate case uses.
    Hence the decisive check is the LAST one: after `resolve()`, re-read the CANONICAL components
    and refuse any forbidden name there. That sees the real folder whatever the manifest called
    it, and it follows a junction to its target.
    """
    if not isinstance(relative, str) or not relative or "\\" in relative:
        return None
    pure = PurePosixPath(relative)
    if pure.is_absolute() or relative != pure.as_posix() or relative in {".", "./"}:
        return None
    if not pure.parts or any(part in {"..", "."} for part in pure.parts):
        return None
    # Windows strips a trailing space or dot, so "inbox " and "inbox." both reach `inbox`.
    if any(str(part).rstrip(" .") != str(part) for part in pure.parts):
        return None
    if _forbidden_component(pure.parts):
        return None
    candidate = (brain / pure).resolve()
    if candidate == brain:
        return None
    try:
        canonical = candidate.relative_to(brain)
    except ValueError:
        return None
    # THE load-bearing check: canonical names, so case folding, trailing-character stripping and
    # junction targets are all seen for what they really are.
    if _forbidden_component(canonical.parts):
        return None
    return candidate


def declared_note_roots(declared, brain_root) -> list[Path]:
    """Resolve an index manifest's roots through the serving confinement policy."""
    brain = Path(brain_root).resolve()
    fallback = [brain / "knowledge"]
    if not isinstance(declared, list) or not declared:
        return fallback
    roots = [
        candidate for candidate in (_acceptable_root(item, brain) for item in declared)
        if candidate is not None
    ]
    return roots or fallback


def indexed_note_roots(index_dir, brain_root) -> list[Path]:
    """The note roots an index DECLARES it covers, read from its own manifest.

    A consumer must confine to what was indexed, not to a hardcoded folder. Measured
    2026-08-04: with `knowledge/` hardcoded as the sole allowed root, every
    `projects/*/obsidian/...` hit failed confinement and was dropped in silence — the graph
    notes were indexed, ranked first, and served as ZERO characters of context. Reading
    `source_roots` here keeps the served set and the indexed set the same by construction.

    Falls back to `knowledge/` alone when the manifest cannot be read: a consumer that cannot
    tell what was indexed must not widen what it exposes.
    """
    brain = Path(brain_root).resolve()
    fallback = [brain / "knowledge"]
    index = Path(index_dir)
    try:
        generation = (index / "CURRENT").read_text(encoding="ascii").strip()
        # `..` matches the character class: it would read a manifest from index/ itself, i.e. a
        # path component chosen by a file's CONTENT. Same anti-pattern as the root hole above.
        if generation in {".", ".."} or not GENERATION_NAME.fullmatch(generation):
            return fallback
        manifest = json.loads(
            (index / "generations" / generation / "manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return fallback
    declared = manifest.get("source_roots")
    if not isinstance(declared, list) or not declared:
        return fallback
    return declared_note_roots(declared, brain)


def _confined(note: Path, roots) -> bool:
    """True when the note sits under at least ONE allowed root."""
    if not roots:
        return True
    try:
        resolved = note.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _body_without_frontmatter(text: str) -> str:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            return parts[2].strip()
    return text.strip()


def _portable(path: str | Path) -> str:
    return Path(path).as_posix()


def _opened_path(fd: int) -> Path | None:
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        get_final_path = ctypes.windll.kernel32.GetFinalPathNameByHandleW
        get_final_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        get_final_path.restype = wintypes.DWORD
        handle = msvcrt.get_osfhandle(fd)
        size = get_final_path(handle, None, 0, 0)
        if not size:
            return None
        buffer = ctypes.create_unicode_buffer(size + 1)
        if not get_final_path(handle, buffer, len(buffer), 0):
            return None
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return Path(value)

    for link in (f"/proc/self/fd/{fd}", f"/dev/fd/{fd}"):
        try:
            return Path(os.readlink(link))
        except OSError:
            continue
    return None


def _read_confined_prefix(
    path: Path,
    root,  # a sequence of allowed roots (falsy = no confinement)
    max_chars: int,
    byte_start: int = 0,
    byte_end: int | None = None,
) -> str | None:
    if not isinstance(byte_start, int) or isinstance(byte_start, bool):
        return None
    if byte_end is not None and (not isinstance(byte_end, int) or isinstance(byte_end, bool)):
        return None
    if byte_start < 0 or (byte_end is not None and byte_end < byte_start):
        return None
    max_bytes = min(262_144, max(1_024, max_chars * 4))
    if byte_end is not None:
        max_bytes = min(max_bytes, byte_end - byte_start)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        opened_stat = os.fstat(fd)
        if not stat.S_ISREG(opened_stat.st_mode):
            return None
        if byte_start > opened_stat.st_size or (
            byte_end is not None and byte_end > opened_stat.st_size
        ):
            return None
        opened = _opened_path(fd)
        if opened is None:
            return None
        if root and not _confined(opened, root):
            return None
        os.lseek(fd, byte_start, os.SEEK_SET)
        return os.read(fd, max_bytes).decode("utf-8", errors="replace")
    finally:
        os.close(fd)


def render_hits(hits: list[dict], max_chars: int, allowed_root=None, *, base=None) -> str:
    """Render confined notes without reading beyond the output-derived byte cap.

    `allowed_root` accepts ONE root or a sequence of them (see `indexed_note_roots`). `base`
    is what relative index paths resolve against — the Brain root, since that is what
    `brain_index` makes them relative to. The two are DISTINCT: conflating them is what
    silently dropped every graph note (resolution wanted the Brain root, confinement wanted
    `knowledge/`, and one variable served both).
    """
    budget = max(0, int(max_chars))
    if not budget:
        return ""
    rendered = ""
    if allowed_root is None:
        roots = ()
    elif isinstance(allowed_root, (str, Path)):
        roots = (Path(allowed_root).resolve(),)
    else:
        roots = tuple(Path(item).resolve() for item in allowed_root)
    if base is not None:
        resolution_base = Path(base).resolve()
    elif len(roots) == 1:
        resolution_base = roots[0].parent  # back-compat: a lone knowledge/ root
    elif roots:
        resolution_base = Path(os.path.commonpath([str(item) for item in roots]))
    else:
        resolution_base = None
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        path_value = hit.get("path")
        byte_start = hit.get("chunk_byte_start", 0)
        byte_end = hit.get("chunk_byte_end")
        if not isinstance(path_value, str) or not path_value:
            continue
        if not isinstance(byte_start, int) or isinstance(byte_start, bool):
            continue
        if byte_end is not None and (not isinstance(byte_end, int) or isinstance(byte_end, bool)):
            continue
        note = Path(path_value)
        if resolution_base is not None and not note.is_absolute():
            note = resolution_base / note
        if roots and not _confined(note, roots):
            continue
        if not note.is_file():
            continue
        provenance = " | ".join(str(hit.get(key, "")) for key in ("type", "scope", "author_agent", "created"))
        separator = "\n\n---\n\n" if rendered else ""
        header = (
            f"### Source {hit.get('rank', 1)} — {_portable(note)}\n"
            f"Provenance: {provenance}\n\n"
        )
        remaining = budget - len(rendered) - len(separator) - len(header)
        if remaining <= 0:
            rendered += (separator + header)[:budget - len(rendered)]
            break
        raw = _read_confined_prefix(
            note,
            roots,
            remaining,
            byte_start=byte_start,
            byte_end=byte_end,
        )
        if raw is None:
            continue
        body = _body_without_frontmatter(raw)
        rendered += separator + header + body[:remaining]
        if len(rendered) >= budget:
            break
    return rendered[:budget]


def retrieve_context(
    query: str,
    index_dir: str | Path,
    query_script: str | Path,
    python_exe: str = sys.executable,
    max_chars: int = 6000,
    k: int = 3,
    min_dense: float = 0.0,
    allowed_root=None,
    timeout_seconds: float = 30.0,
    brain_root=None,
) -> str:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    try:
        result = subprocess.run(
            [python_exe, _portable(query_script), "--index", _portable(index_dir), "--q", query, "--k", str(k)],
            capture_output=True,
            text=True,
            # WITHOUT this the reader thread decodes the child's stdout with the Windows locale
            # codec (cp1252), dies on the first UTF-8 byte it cannot map, and leaves
            # `result.stdout` as None — the caller then fails with an AttributeError that says
            # nothing about encoding. The vault is French and the graph notes carry « · » and
            # « — », so this fires on real content. Pre-existing, found 2026-08-04.
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"brain_query timed out after {timeout_seconds}s") from exc
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"brain_query failed ({result.returncode})")
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    payload = json.loads(lines[-1]) if lines else {"hits": []}
    hits = payload.get("hits", [])
    if min_dense > 0:
        hits = [hit for hit in hits if float(hit.get("dense_cos", -1.0)) >= min_dense]
    return render_hits(
        hits, max_chars=max_chars, allowed_root=allowed_root, base=brain_root,
    )


def _configure_stdout_utf8() -> None:
    """The vault is French and full of « → » : a cp1252 stdout crashes on the first arrow.

    Pre-existing defect found 2026-08-04 while wiring the graph roots: the documented agent
    path (`brain_context.py --q`, AGENTS.md) raised UnicodeEncodeError on any note containing a
    character outside cp1252 — so it failed on real content, not on an edge case.
    brain_query.py already does this; brain_context.py never did.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def main() -> None:
    _configure_stdout_utf8()
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--q", required=True)
    ap.add_argument("--index", default=here / "index")
    ap.add_argument("--query-script", default=here / "brain_query.py")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--max-chars", type=int, default=6000)
    ap.add_argument("--min-dense", type=float, default=0.0)
    ap.add_argument(
        "--allowed-root", default=None,
        help="override the confinement; by default the roots the index declares are used",
    )
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args()
    brain_root = here.parent
    allowed = (
        [Path(args.allowed_root)] if args.allowed_root
        else indexed_note_roots(args.index, brain_root)
    )
    print(retrieve_context(
        args.q,
        args.index,
        args.query_script,
        args.python,
        args.max_chars,
        args.k,
        args.min_dense,
        allowed,
        args.timeout,
        brain_root=brain_root,
    ))


if __name__ == "__main__":
    main()
