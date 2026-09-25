#!/usr/bin/env python3
"""Fail-open UserPromptSubmit adapter for Claude Code and Codex CLI."""
import json
import os
import re
import uuid
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from brain_auth import seal_request, service_token, verified_context
from brain_singleton import ProcessMutex

DEFAULT_PORT = 8765


def _endpoint():
    port = int(os.environ.get("AMITEL_BRAIN_PORT", DEFAULT_PORT))
    return f"http://127.0.0.1:{port}/query"


def _port():
    return int(os.environ.get("AMITEL_BRAIN_PORT", DEFAULT_PORT))


def _validate_response(payload, token):
    return verified_context(payload, token)


def _authenticate_service(token, timeout):
    """Prove the listener owns the local secret before disclosing a prompt or bearer token."""
    endpoint = _endpoint().removesuffix("/query")
    request = Request(f"{endpoint}/challenge", method="GET")
    with urlopen(request, timeout=timeout) as response:
        declared = int(response.headers.get("Content-Length", "0") or 0)
        if declared < 0 or declared > 16_384:
            raise ValueError("invalid Amitel Brain challenge")
        raw = response.read(16_385)
    if len(raw) > 16_384:
        raise ValueError("invalid Amitel Brain challenge")
    payload = json.loads(raw)
    challenge = _validate_response(payload, token)
    match = re.fullmatch(r"challenge:([0-9a-f]{24})", challenge)
    if match is None:
        raise ValueError("Amitel Brain server authentication failed")
    return match.group(1)


def _request_context(prompt, timeout=1.0, harness=None):
    token = service_token()
    nonce = _authenticate_service(token, timeout)
    request_payload = {
        "query": prompt[:8000],
        "max_chars": 2000,
        "harness": harness or os.environ.get("AMITEL_BRAIN_HARNESS", "unknown"),
        "trace_id": uuid.uuid4().hex,
    }
    body = json.dumps(seal_request(request_payload, token, nonce)).encode("utf-8")
    request = Request(
        _endpoint().replace("/query", "/query-secure"), data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read())
    return _validate_response(payload, token)


def _server_python():
    configured = os.environ.get("AMITEL_BRAIN_PYTHON")
    if configured:
        return configured
    local_venv = Path.home() / ".brain" / "tooling" / ".venv"
    candidates = [local_venv / "Scripts" / "python.exe", local_venv / "bin" / "python"]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def _spawn_server():
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    command = [_server_python(), str(Path(__file__).with_name("brain_server.py"))]
    kwargs = {
        "cwd": str(Path(__file__).resolve().parent),
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(command, **kwargs)


def query_service(prompt, startup_timeout=8.0, harness=None):
    try:
        return _request_context(prompt, harness=harness)
    except HTTPError as exc:
        if exc.code == 503:
            return _wait_for_service(prompt, startup_timeout, harness)
        return ""  # Occupied port or unauthenticated response: never inject and never retry.
    except (ValueError, json.JSONDecodeError):
        return ""  # Occupied port or unauthenticated response: never inject and never retry.
    except (URLError, TimeoutError, OSError):
        pass
    startup_mutex = ProcessMutex.try_acquire(f"startup-{_port()}")
    if startup_mutex is not None:
        try:
            # Another hook may have completed startup between the first probe and this lock.
            try:
                return _request_context(prompt, harness=harness)
            except HTTPError as exc:
                if exc.code == 503:
                    return _wait_for_service(prompt, startup_timeout, harness)
                return ""
            except (ValueError, json.JSONDecodeError):
                return ""
            except (URLError, TimeoutError, OSError):
                pass
            try:
                _spawn_server()
            except OSError:
                return ""
            return _wait_for_service(prompt, startup_timeout, harness)
        finally:
            startup_mutex.close()
    return _wait_for_service(prompt, startup_timeout, harness)


def _wait_for_service(prompt, startup_timeout, harness):
    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        try:
            return _request_context(prompt, harness=harness)
        except HTTPError as exc:
            if exc.code != 503:
                return ""
            time.sleep(0.2)
        except (ValueError, json.JSONDecodeError):
            return ""
        except (URLError, TimeoutError, OSError):
            time.sleep(0.2)
    return ""


def hook_output(payload, query_fn=query_service):
    prompt = str(payload.get("prompt") or payload.get("user_message") or "").strip()
    if not prompt:
        return None
    context = query_fn(prompt)
    if not context:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }


def main():
    try:
        # Stdin arrive en UTF-8, parfois BOMé par le wrapper PowerShell (.NET StreamWriter) ;
        # le décodage locale (cp1252) mangerait BOM et accents → lire binaire + utf-8-sig.
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
        output = hook_output(payload)
        if output:
            # ensure_ascii (défaut) : un stdout cp1252 lèverait UnicodeEncodeError sur
            # les caractères hors cp1252 des notes ; l'ASCII échappé passe partout.
            print(json.dumps(output))
    except Exception:
        pass  # Retrieval must never block the user's prompt.


if __name__ == "__main__":
    main()
