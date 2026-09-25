"""Small cross-process locks used before starting the heavy Brain runtime."""
from __future__ import annotations

import ctypes
import hashlib
import os
import tempfile
from pathlib import Path


class ProcessMutex:
    """Non-blocking per-user mutex; closing it releases ownership."""

    def __init__(self, *, handle=None, file_handle=None):
        self._handle = handle
        self._file_handle = file_handle

    @classmethod
    def try_acquire(cls, purpose: str):
        digest = hashlib.sha256(purpose.encode("utf-8")).hexdigest()[:32]
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel32.CreateMutexW(None, True, f"Local\\AmitelBrain-{digest}")
            if not handle:
                raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
            if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
                kernel32.CloseHandle(handle)
                return None
            return cls(handle=handle)

        import fcntl
        path = Path(tempfile.gettempdir()) / f"amitel-brain-{digest}.lock"
        file_handle = path.open("a+b")
        try:
            fcntl.flock(file_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            file_handle.close()
            return None
        return cls(file_handle=file_handle)

    def close(self):
        if self._handle is not None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.ReleaseMutex(self._handle)
            kernel32.CloseHandle(self._handle)
            self._handle = None
        if self._file_handle is not None:
            self._file_handle.close()
            self._file_handle = None

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.close()

