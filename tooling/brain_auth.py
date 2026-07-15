#!/usr/bin/env python3
"""Local authentication shared by the Amitel Brain service and its clients."""
import hashlib
import hmac
import os
import secrets
import subprocess
import time
from pathlib import Path

SERVICE_NAME = "amitel-brain"
PROTOCOL_VERSION = 1
_TOKEN_CACHE = None
_TOKEN_CACHE_PATH = None


def _token_path() -> Path:
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            raise RuntimeError("LOCALAPPDATA is unavailable")
        return Path(local) / "AmitelBrain" / "service-token"
    return Path.home() / ".amitel-brain" / "service-token"


def _restrict_token_acl(path: Path, *, directory=False) -> None:
    if os.name != "nt":
        path.chmod(0o700 if directory else 0o600)
        return
    identity = subprocess.run(
        ["whoami"], check=True, capture_output=True,
    ).stdout
    principal = identity.decode("oem").strip()
    if not principal:
        raise RuntimeError("cannot determine current Windows identity")
    quiet = {"check": True, "stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE}
    subprocess.run(["icacls", str(path), "/reset"], **quiet)
    subprocess.run(["icacls", str(path), "/inheritance:r"], **quiet)
    permission = f"{principal}:{'(OI)(CI)F' if directory else 'F'}"
    subprocess.run(["icacls", str(path), "/grant:r", permission], **quiet)


def _read_token(path: Path) -> str:
    token = path.read_text(encoding="ascii").strip()
    if len(token) < 32:
        raise ValueError("Amitel Brain service token is invalid")
    return token


def service_token() -> str:
    global _TOKEN_CACHE, _TOKEN_CACHE_PATH
    configured = os.environ.get("AMITEL_BRAIN_TOKEN")
    if configured:
        if len(configured) < 32:
            raise ValueError("AMITEL_BRAIN_TOKEN must contain at least 32 characters")
        return configured

    path = _token_path().resolve()
    if _TOKEN_CACHE is not None and _TOKEN_CACHE_PATH == path:
        return _TOKEN_CACHE
    path.parent.mkdir(parents=True, exist_ok=True)
    _restrict_token_acl(path.parent, directory=True)
    try:
        with path.open("x", encoding="ascii", newline="\n") as handle:
            token = secrets.token_urlsafe(32)
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        _restrict_token_acl(path)
    except FileExistsError:
        _restrict_token_acl(path)
        token = ""
        for _ in range(10):
            try:
                token = _read_token(path)
                break
            except (OSError, ValueError):
                time.sleep(0.02)
        if not token:
            raise RuntimeError("Amitel Brain service token is unavailable")
    _TOKEN_CACHE = token
    _TOKEN_CACHE_PATH = path
    return token


def _signature(context: str, token: str) -> str:
    message = f"{SERVICE_NAME}\n{PROTOCOL_VERSION}\n{context}".encode("utf-8")
    return hmac.new(token.encode("utf-8"), message, hashlib.sha256).hexdigest()


def signed_context_payload(context: str, token: str) -> dict:
    return {
        "service": SERVICE_NAME,
        "protocol": PROTOCOL_VERSION,
        "context": context,
        "signature": _signature(context, token),
    }


def verified_context(payload: dict, token: str) -> str:
    if payload.get("service") != SERVICE_NAME or payload.get("protocol") != PROTOCOL_VERSION:
        raise ValueError("unexpected Amitel Brain service identity")
    context = payload.get("context")
    signature = payload.get("signature")
    if not isinstance(context, str) or not isinstance(signature, str):
        raise ValueError("invalid authenticated Amitel Brain response")
    if not hmac.compare_digest(signature, _signature(context, token)):
        raise ValueError("Amitel Brain response authentication failed")
    return context
