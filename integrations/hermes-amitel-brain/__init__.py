"""Ephemeral Amitel Brain recall for Hermes prompts."""
import importlib.util
import json
import os
import sys
import threading
from pathlib import Path

_MODULE_LOCK = threading.Lock()
_HOOK_MODULE = None
_HOOK_PATH = None


def _configured_paths() -> tuple[Path, Path]:
    root = os.environ.get("AMITEL_BRAIN_ROOT")
    code_root = os.environ.get("AMITEL_BRAIN_CODE_ROOT")
    python = os.environ.get("AMITEL_BRAIN_PYTHON")
    local = os.environ.get("LOCALAPPDATA")
    config_path = Path(local) / "AmitelBrain" / "config.json" if local else None
    if config_path and config_path.is_file():
        config = json.loads(config_path.read_text(encoding="utf-8"))
        root = root or config.get("brain_root")
        code_root = code_root or config.get("code_root")
        python = python or config.get("python")
    if not isinstance(root, str) or not root.strip():
        raise RuntimeError("Amitel Brain is not configured")
    if not isinstance(code_root, str) or not code_root.strip():
        raise RuntimeError("Amitel Brain local runtime is not configured")
    os.environ["AMITEL_BRAIN_ROOT"] = root
    os.environ["AMITEL_BRAIN_CODE_ROOT"] = code_root
    if isinstance(python, str) and python.strip():
        os.environ["AMITEL_BRAIN_PYTHON"] = python
    return Path(root), Path(code_root)


def _load_hook_module():
    global _HOOK_MODULE, _HOOK_PATH
    _, code_root = _configured_paths()
    hook_path = code_root / "brain_hook.py"
    with _MODULE_LOCK:
        if _HOOK_MODULE is not None and _HOOK_PATH == hook_path:
            return _HOOK_MODULE
        auth_path = hook_path.with_name("brain_auth.py")
        auth_spec = importlib.util.spec_from_file_location("brain_auth", auth_path)
        spec = importlib.util.spec_from_file_location("_amitel_brain_local_hook", hook_path)
        if auth_spec is None or auth_spec.loader is None or spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load Amitel Brain hook dependencies from: {hook_path.parent}")
        auth_module = importlib.util.module_from_spec(auth_spec)
        module = importlib.util.module_from_spec(spec)
        previous_auth = sys.modules.get("brain_auth")
        try:
            sys.modules["brain_auth"] = auth_module
            auth_spec.loader.exec_module(auth_module)
            spec.loader.exec_module(module)
        finally:
            if previous_auth is None:
                sys.modules.pop("brain_auth", None)
            else:
                sys.modules["brain_auth"] = previous_auth
        _HOOK_MODULE = module
        _HOOK_PATH = hook_path
        return module


def _query_context(user_message: str) -> str:
    return _load_hook_module().query_service(user_message)


def _pre_llm_call(**kwargs):
    user_message = kwargs.get("user_message")
    if not isinstance(user_message, str) or not user_message.strip():
        return None
    try:
        context = _query_context(user_message)
    except Exception:
        return None
    return {"context": context} if context else None


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", _pre_llm_call)
