#!/usr/bin/env python3
"""Local authentication shared by the Amitel Brain service and its clients."""
import hashlib
import hmac
import base64
import json
import os
import re
import secrets
import subprocess
import time
from pathlib import Path

SERVICE_NAME = "amitel-brain"
PROTOCOL_VERSION = 2
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


def _signature(message_body: str, token: str, protocol=PROTOCOL_VERSION) -> str:
    message = f"{SERVICE_NAME}\n{protocol}\n{message_body}".encode("utf-8")
    return hmac.new(token.encode("utf-8"), message, hashlib.sha256).hexdigest()


def signed_context_payload(
    context: str, token: str, *, corpus=None, structured_context=None, navigation=None,
    request=None,
) -> dict:
    """Return a v2 envelope whose HMAC covers every field consumed by clients.

    `request` echoes the query/trace_id the server actually answered, INSIDE the signed body:
    without it a client cannot tell a genuine answer from a replayed one, and Autowin's client
    rejects the whole response as `invalid` (measured 2026-09-02: every read failed this way).
    """
    body = {"context": context}
    if request is not None:
        body["request"] = request
    if navigation is not None:
        body["navigation"] = navigation
    if corpus is not None:
        body["corpus"] = corpus
    if structured_context is not None:
        body["structuredContext"] = structured_context
    authenticated = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    return {
        "service": SERVICE_NAME,
        "protocol": PROTOCOL_VERSION,
        "authenticated": authenticated,
        "signature": _signature(authenticated, token),
    }


def verified_context(payload: dict, token: str) -> str:
    if payload.get("service") != SERVICE_NAME:
        raise ValueError("unexpected Amitel Brain service identity")
    protocol = payload.get("protocol")
    signature = payload.get("signature")
    if not isinstance(signature, str):
        raise ValueError("invalid authenticated Amitel Brain response")
    if protocol == 1:
        context = payload.get("context")
        signed = context
    elif protocol == 2:
        signed = payload.get("authenticated")
        try:
            body = json.loads(signed)
            context = body.get("context")
        except (TypeError, json.JSONDecodeError, AttributeError) as exc:
            raise ValueError("invalid authenticated Amitel Brain response") from exc
    else:
        raise ValueError("unexpected Amitel Brain service identity")
    if not isinstance(context, str) or not isinstance(signed, str):
        raise ValueError("invalid authenticated Amitel Brain response")
    if not hmac.compare_digest(signature, _signature(signed, token, protocol=protocol)):
        raise ValueError("Amitel Brain response authentication failed")
    return context


REQUEST_AAD = b"amitel-brain/request-v1"


def seal_request(payload: dict, token: str, nonce: str) -> dict:
    """Encrypt a request so a listener that wins a loopback TOCTOU learns no prompt/token."""
    if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{24}", nonce):
        raise ValueError("invalid Amitel Brain request nonce")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    key = hashlib.sha256(token.encode("utf-8")).digest()
    encrypted = AESGCM(key).encrypt(bytes.fromhex(nonce), plaintext, REQUEST_AAD)
    return {"nonce": nonce, "ciphertext": base64.b64encode(encrypted).decode("ascii")}


def open_request(envelope: dict, token: str) -> dict:
    if not isinstance(envelope, dict):
        raise ValueError("invalid encrypted Amitel Brain request")
    nonce = envelope.get("nonce")
    ciphertext = envelope.get("ciphertext")
    if (
        not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{24}", nonce)
        or not isinstance(ciphertext, str) or len(ciphertext) > 32_768
    ):
        raise ValueError("invalid encrypted Amitel Brain request")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        encrypted = base64.b64decode(ciphertext, validate=True)
        key = hashlib.sha256(token.encode("utf-8")).digest()
        plaintext = AESGCM(key).decrypt(bytes.fromhex(nonce), encrypted, REQUEST_AAD)
        payload = json.loads(plaintext)
    except Exception as exc:
        raise ValueError("invalid encrypted Amitel Brain request") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid encrypted Amitel Brain request")
    return payload
