#!/usr/bin/env python3
"""Fail-open UserPromptSubmit adapter for Claude Code and Codex CLI."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from brain_auth import service_token, verified_context

DEFAULT_PORT = 8765


def _endpoint(path="/query"):
    port = int(os.environ.get("AMITEL_BRAIN_PORT", DEFAULT_PORT))
    return f"http://127.0.0.1:{port}{path}"


def _configured_root():
    default = Path(__file__).resolve().parents[1]
    return str(Path(os.environ.get("AMITEL_BRAIN_ROOT", default)).resolve())


def _validate_response(payload, token):
    return verified_context(payload, token)


def _request_context(prompt, timeout=1.0):
    token = service_token()
    body = json.dumps({"query": prompt[:8000], "max_chars": 2000}).encode("utf-8")
    request = Request(
        _endpoint(), data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read())
    return _validate_response(payload, token)


def _request_health(timeout=1.0):
    token = service_token()
    request = Request(
        _endpoint("/health"),
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read())
    return _validate_response(payload, token)


def _shutdown_server():
    token = service_token()
    request = Request(
        _endpoint("/shutdown"), data=b"{}",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urlopen(request, timeout=1.0) as response:
        payload = json.loads(response.read())
    _validate_response(payload, token)


def _wait_for_shutdown(timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            _request_health(timeout=0.2)
        except (URLError, TimeoutError, OSError):
            return
        time.sleep(0.05)
    raise TimeoutError("brain service did not stop")


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


def query_service(prompt, startup_timeout=8.0):
    expected_root = _configured_root()
    try:
        active_root = _request_health()
    except (HTTPError, ValueError, json.JSONDecodeError):
        return ""  # Occupied port or unauthenticated response: never inject and never retry.
    except (URLError, TimeoutError, OSError):
        active_root = None

    if active_root is not None:
        if active_root != expected_root:
            try:
                _shutdown_server()
                _wait_for_shutdown()
            except (HTTPError, ValueError, json.JSONDecodeError, URLError, TimeoutError, OSError):
                return ""
        else:
            try:
                return _request_context(prompt)
            except (HTTPError, ValueError, json.JSONDecodeError, URLError, TimeoutError, OSError):
                return ""

    try:
        _spawn_server()
    except OSError:
        return ""
    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        try:
            if _request_health() != expected_root:
                return ""
            return _request_context(prompt)
        except (HTTPError, ValueError, json.JSONDecodeError):
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
        payload = json.loads(sys.stdin.read())
        output = hook_output(payload)
        if output:
            print(json.dumps(output, ensure_ascii=False))
    except Exception:
        pass  # Retrieval must never block the user's prompt.


if __name__ == "__main__":
    main()
