"""Privacy-preserving local retrieval traces; never records queries or note text."""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
DRIVE_REMOTE = 4


def local_log_path(path: str | Path) -> Path:
    """Resolve an explicit absolute destination and reject network/reparse-backed storage."""
    target = Path(str(path).strip())
    if not target.is_absolute() or str(target).startswith(("\\\\", "//")):
        raise ValueError("log destination must be an absolute local path")
    resolved = target.resolve(strict=False)
    if str(resolved).startswith(("\\\\", "//")):
        raise ValueError("network log destinations are disabled")
    if os.name == "nt":
        import ctypes
        drive_type = ctypes.windll.kernel32.GetDriveTypeW(str(Path(resolved.anchor)))
        if drive_type == DRIVE_REMOTE:
            raise ValueError("network log destinations are disabled")
        current = resolved if resolved.exists() else resolved.parent
        while current != current.parent:
            try:
                attributes = current.stat().st_file_attributes
            except (OSError, AttributeError):
                break
            if attributes & FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError("reparse-point log destinations are disabled")
            current = current.parent
    return resolved


class RetrievalTrace:
    def __init__(self, path: str | Path, max_bytes: int = 5_000_000):
        if max_bytes < 128:
            raise ValueError("max_bytes must be at least 128")
        self.path = local_log_path(path)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    @staticmethod
    def _identifier(value, fallback="unknown"):
        text = str(value or "").strip()
        return text if IDENTIFIER.fullmatch(text) else fallback

    def record(self, *, harness, trace_id, generation, axes, duration_ms, hits):
        documents = []
        for hit in hits:
            path = hit.get("path") if isinstance(hit.get("path"), str) else ""
            uid = hit.get("uid") if isinstance(hit.get("uid"), str) else ""
            documents.append({
                "id": uid or path,
                "path": path,
                "score": round(float(hit.get("dense_cos", 0.0)), 6),
            })
        event = {
            "schema": "amitel-brain/retrieval-trace-v1",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trace_id": self._identifier(trace_id),
            "harness": self._identifier(harness),
            "generation": self._identifier(generation, fallback="none"),
            "axes": max(int(axes), 0),
            "duration_ms": round(max(float(duration_ms), 0.0), 3),
            "documents": documents,
        }
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        encoded_size = len(line.encode("utf-8"))
        if encoded_size > self.max_bytes:
            raise ValueError("trace event exceeds max_bytes")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            current_size = self.path.stat().st_size if self.path.exists() else 0
            if current_size and current_size + encoded_size > self.max_bytes:
                rotated = self.path.with_suffix(self.path.suffix + ".1")
                os.replace(self.path, rotated)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)


def configured_trace():
    path = os.environ.get("AMITEL_BRAIN_TRACE_PATH", "").strip()
    if not path:
        return None
    try:
        return RetrievalTrace(path)
    except ValueError:
        return None
