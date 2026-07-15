#!/usr/bin/env python3
"""Retrieve and render bounded Amitel Brain context for prompt injection."""
import argparse
import json
import os
import stat
import subprocess
import sys
from pathlib import Path


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


def _read_confined_prefix(path: Path, root: Path | None, max_chars: int) -> str | None:
    max_bytes = min(262_144, max(1_024, max_chars * 4))
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        opened = _opened_path(fd)
        if opened is None:
            return None
        if root is not None:
            try:
                opened.resolve().relative_to(root)
            except (OSError, ValueError):
                return None
        return os.read(fd, max_bytes).decode("utf-8", errors="replace")
    finally:
        os.close(fd)


def render_hits(hits: list[dict], max_chars: int, allowed_root=None) -> str:
    """Render confined notes without reading beyond the output-derived byte cap."""
    budget = max(0, int(max_chars))
    if not budget:
        return ""
    rendered = ""
    root = Path(allowed_root).resolve() if allowed_root is not None else None
    for hit in hits:
        note = Path(hit["path"])
        if root is not None:
            try:
                note.resolve().relative_to(root)
            except (OSError, ValueError):
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
        raw = _read_confined_prefix(note, root, remaining)
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
) -> str:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    try:
        result = subprocess.run(
            [python_exe, _portable(query_script), "--index", _portable(index_dir), "--q", query, "--k", str(k)],
            capture_output=True,
            text=True,
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
    return render_hits(hits, max_chars=max_chars, allowed_root=allowed_root)


def main() -> None:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--q", required=True)
    ap.add_argument("--index", default=here / "index")
    ap.add_argument("--query-script", default=here / "brain_query.py")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--max-chars", type=int, default=6000)
    ap.add_argument("--min-dense", type=float, default=0.0)
    ap.add_argument("--allowed-root", default=here.parent / "knowledge")
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args()
    print(retrieve_context(
        args.q,
        args.index,
        args.query_script,
        args.python,
        args.max_chars,
        args.k,
        args.min_dense,
        args.allowed_root,
        args.timeout,
    ))


if __name__ == "__main__":
    main()
