#!/usr/bin/env python
"""Trust exactly the installed Hermes Brain Codex hook via Codex app-server."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
from typing import Any


def _resolve_codex(value: str) -> str:
    resolved = shutil.which(value) or value
    path = Path(resolved)
    if os.name == "nt" and path.suffix.lower() != ".exe":
        package_root = path.parent / "node_modules" / "@openai" / "codex" / "node_modules"
        matches = list(package_root.glob("@openai/codex-win32-*/vendor/*/bin/codex.exe"))
        if len(matches) == 1:
            return str(matches[0])
    return str(path)


def _rpc(codex: str, codex_home: Path, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    executable = _resolve_codex(codex)
    try:
        process = subprocess.Popen(
            [executable, "app-server", "--stdio"],
            text=True,
            encoding="utf-8",
            errors="strict",
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            creationflags=creationflags,
        )
    except OSError as exc:
        raise RuntimeError(f"Codex app-server unavailable: {exc}") from exc
    assert process.stdin is not None and process.stdout is not None
    received: queue.Queue[dict[str, Any] | None] = queue.Queue()

    def read_stdout() -> None:
        for line in process.stdout:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                received.put(value)
        received.put(None)

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()
    responses: list[dict[str, Any]] = []
    pending: dict[int, dict[str, Any]] = {}

    def wait_for(request_id: int) -> None:
        if request_id in pending:
            responses.append(pending.pop(request_id))
            return
        while True:
            try:
                item = received.get(timeout=20)
            except queue.Empty as exc:
                raise RuntimeError(f"Timed out waiting for Codex RPC {request_id}") from exc
            if item is None:
                raise RuntimeError(f"Codex app-server exited before RPC {request_id}")
            item_id = item.get("id")
            if item_id == request_id:
                responses.append(item)
                return
            if isinstance(item_id, int):
                pending[item_id] = item

    try:
        for message in messages:
            process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            process.stdin.flush()
            request_id = message.get("id")
            if isinstance(request_id, int):
                wait_for(request_id)
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
    return responses


def _response(responses: list[dict[str, Any]], request_id: int) -> dict[str, Any]:
    for item in responses:
        if item.get("id") == request_id:
            if item.get("error") is not None:
                raise RuntimeError(f"Codex RPC error: {item['error']}")
            result = item.get("result")
            if isinstance(result, dict):
                return result
            raise RuntimeError(f"Codex RPC {request_id} returned no object result")
    raise RuntimeError(f"Codex RPC response {request_id} not found")


def _handshake() -> list[dict[str, Any]]:
    return [
        {
            "method": "initialize",
            "id": 0,
            "params": {
                "clientInfo": {
                    "name": "hermes_brain_installer",
                    "title": "Hermes Brain Installer",
                    "version": "0.1.0",
                }
            },
        },
        {"method": "initialized", "params": {}},
    ]


def _expected_commands(wrapper: Path) -> tuple[str, str]:
    prefix = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "
    value = str(wrapper)
    return prefix + f'"{value}"', prefix + value


def trust_hook(codex: str, codex_home: Path, cwd: Path, wrapper: Path) -> dict[str, str]:
    hooks_path = (codex_home / "hooks.json").resolve()
    expected_commands = _expected_commands(wrapper)
    list_messages = _handshake() + [
        {"method": "hooks/list", "id": 1, "params": {"cwds": [str(cwd.resolve())]}}
    ]
    listed = _response(_rpc(codex, codex_home, list_messages), 1)
    candidates: list[dict[str, Any]] = []
    for group in listed.get("data", []):
        if not isinstance(group, dict):
            continue
        for hook in group.get("hooks", []):
            if not isinstance(hook, dict):
                continue
            source = hook.get("sourcePath")
            command = hook.get("command")
            if not isinstance(source, str) or not isinstance(command, str):
                continue
            try:
                source_matches = Path(source).resolve() == hooks_path
            except OSError:
                source_matches = False
            if source_matches and command in expected_commands:
                candidates.append(hook)
    if len(candidates) != 1:
        raise RuntimeError(f"Expected exactly one Hermes Brain Codex hook, found {len(candidates)}")
    hook = candidates[0]
    key = hook.get("key")
    current_hash = hook.get("currentHash")
    if not isinstance(key, str) or not isinstance(current_hash, str) or not current_hash.startswith("sha256:"):
        raise RuntimeError("Codex did not return a valid hook key/hash")

    trust_messages = _handshake() + [
        {
            "method": "config/batchWrite",
            "id": 2,
            "params": {
                "edits": [
                    {
                        "keyPath": "hooks.state",
                        "value": {key: {"enabled": True, "trusted_hash": current_hash}},
                        "mergeStrategy": "upsert",
                    }
                ],
                "reloadUserConfig": True,
            },
        }
    ]
    _response(_rpc(codex, codex_home, trust_messages), 2)

    verify = _response(_rpc(codex, codex_home, list_messages), 1)
    matches = [
        item
        for group in verify.get("data", [])
        if isinstance(group, dict)
        for item in group.get("hooks", [])
        if isinstance(item, dict) and item.get("key") == key
    ]
    if len(matches) != 1 or matches[0].get("trustStatus") != "trusted" or not matches[0].get("enabled"):
        raise RuntimeError("Codex hook trust verification failed")
    return {"key": key, "trusted_hash": current_hash, "trust_status": "trusted"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--codex-home", type=Path, required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--wrapper", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = trust_hook(args.codex, args.codex_home, args.cwd, args.wrapper)
    except Exception as exc:
        print(json.dumps({"trusted": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"trusted": True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
