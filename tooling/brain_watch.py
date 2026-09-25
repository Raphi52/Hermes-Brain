"""Native Windows directory change notifications for Brain source roots."""
from __future__ import annotations

import ctypes
import os
import threading
import time
from pathlib import Path
from typing import NamedTuple


class WatchKey(NamedTuple):
    path: str
    notify_filter: int


FILE_NOTIFY_CHANGE_FILE_NAME = 0x00000001
FILE_NOTIFY_CHANGE_DIR_NAME = 0x00000002
FILE_NOTIFY_CHANGE_SIZE = 0x00000008
FILE_NOTIFY_CHANGE_LAST_WRITE = 0x00000010
CONTENT_NOTIFY_FILTER = (
    FILE_NOTIFY_CHANGE_FILE_NAME | FILE_NOTIFY_CHANGE_DIR_NAME
    | FILE_NOTIFY_CHANGE_SIZE | FILE_NOTIFY_CHANGE_LAST_WRITE
)
FILE_LIST_DIRECTORY = 0x0001
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000


class WindowsCorpusWatcher:
    """Watch local/UNC roots recursively; any lost coverage invalidates freshness.

    A ``WatchKey`` is the canonical case-normalized path plus its Win32 filter. ``_content_keys``
    is reconciled exactly to each serving manifest; ``_discovery_keys`` is independent and watches
    future project directory names. ``_watched`` is their desired union, ``_active`` contains keys
    currently blocked in ReadDirectoryChangesW, and ``_handles`` allows a removed key to be cancelled.
    Therefore ``healthy`` means every desired key has one active native read. Cancelled/obsolete
    threads observe that their key is no longer desired and exit without publishing callbacks.
    """

    def __init__(self, brain_root, source_roots, on_change):
        self.brain_root = Path(brain_root)
        self.on_change = on_change
        self._watched = set()
        self._active = set()
        self._content_keys = set()
        self._discovery_keys = set()
        self._handles = {}
        self._threads = []
        self._closed = False
        self._closed_event = threading.Event()
        self._lock = threading.Lock()
        self.available = os.name == "nt"
        if self.available:
            self.reconcile_roots(source_roots)

    def add_roots(self, source_roots):
        """Compatibility shim; content roots are an exact, not additive, set."""
        self.reconcile_roots(source_roots)

    def reconcile_roots(self, source_roots):
        if not self.available:
            return
        notify_filter = CONTENT_NOTIFY_FILTER
        desired = {
            self._key(relative, notify_filter)
            for relative in source_roots
        }
        with self._lock:
            removed = self._content_keys - desired
            self._content_keys = desired
            self._watched = self._content_keys | self._discovery_keys
            handles = [self._handles.get(key) for key in removed]
            self._active.difference_update(removed)
        self._cancel(handles)
        for key in desired:
            self._add_key(key)

    def add_discovery_roots(self, source_roots):
        if not self.available:
            return
        for relative in source_roots:
            key = self._key(relative, FILE_NOTIFY_CHANGE_DIR_NAME)
            if Path(key.path).is_dir():
                with self._lock:
                    self._discovery_keys.add(key)
                    self._watched = self._content_keys | self._discovery_keys
                self._add_key(key)

    def _key(self, relative, notify_filter):
        path = (self.brain_root / relative).resolve()
        return WatchKey(os.path.normcase(str(path)), notify_filter)

    def _add_key(self, key):
        path = Path(key.path)
        notify_filter = key.notify_filter
        with self._lock:
            if key in self._active or key in self._handles or key not in self._watched:
                return
        ready = threading.Event()
        thread = threading.Thread(
            target=self._watch,
            args=(path, notify_filter, ready),
            daemon=True,
            name=f"amitel-brain-watch-{len(self._watched)}",
        )
        with self._lock:
            self._threads.append(thread)
        thread.start()
        if not ready.wait(2):
            self.on_change(f"watch startup timed out for {path}")

    @staticmethod
    def _cancel(handles):
        if not handles:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.CancelIoEx.restype = ctypes.c_int
        for handle in handles:
            if handle:
                kernel32.CancelIoEx(handle, None)

    @property
    def healthy(self):
        with self._lock:
            return bool(self._watched) and self._active == self._watched

    def close(self):
        """Cancel native reads and make every watcher thread exit without callbacks."""
        with self._lock:
            self._closed = True
            self._closed_event.set()
            self._watched.clear()
            self._content_keys.clear()
            self._discovery_keys.clear()
            handles = list(self._handles.values())
            self._active.clear()
        self._cancel(handles)
        for thread in list(self._threads):
            if thread is not threading.current_thread():
                thread.join(timeout=2)

    @staticmethod
    def classify_read(ok, returned_bytes, error_code=0):
        if not ok:
            return f"directory watch failed: {error_code}"
        if returned_bytes == 0:
            return "directory watch overflow: change details were lost"
        return None

    def _watch(self, path, notify_filter, ready):
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
            ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
        ]
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.ReadDirectoryChangesW.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
            ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p, ctypes.c_void_p,
        ]
        kernel32.ReadDirectoryChangesW.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        invalid = ctypes.c_void_p(-1).value
        buffer = ctypes.create_string_buffer(32 * 1024)
        returned = ctypes.c_ulong()
        first_attempt = True
        while True:
            key = WatchKey(os.path.normcase(str(path)), notify_filter)
            with self._lock:
                if self._closed or key not in self._watched:
                    return
            try:
                open_path = path.resolve()
                open_path.relative_to(self.brain_root.resolve())
            except (OSError, ValueError):
                self.on_change(f"watch path escapes brain root: {path}")
                if self._closed_event.wait(5):
                    return
                continue
            handle = kernel32.CreateFileW(
                str(open_path), FILE_LIST_DIRECTORY,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                None, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None,
            )
            if first_attempt:
                ready.set()
                first_attempt = False
            if handle == invalid:
                with self._lock:
                    wanted = not self._closed and key in self._watched
                if wanted:
                    self.on_change(f"watch unavailable for {path}: {ctypes.get_last_error()}")
                if self._closed_event.wait(5):
                    return
                continue
            with self._lock:
                if self._closed or key not in self._watched:
                    kernel32.CloseHandle(handle)
                    return
                self._handles[key] = handle
                self._active.add(key)
            try:
                ok = kernel32.ReadDirectoryChangesW(
                    handle, buffer, len(buffer), True, notify_filter,
                    ctypes.byref(returned), None, None,
                )
                error = self.classify_read(ok, returned.value, ctypes.get_last_error())
                with self._lock:
                    wanted = not self._closed and key in self._watched
                if not wanted:
                    return
                if error:
                    self.on_change(f"{error} ({path})")
                else:
                    self.on_change(None)
                    continue
            finally:
                with self._lock:
                    self._active.discard(key)
                    self._handles.pop(key, None)
                kernel32.CloseHandle(handle)
            if self._closed_event.wait(5):
                return
