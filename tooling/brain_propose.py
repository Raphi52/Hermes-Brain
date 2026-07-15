#!/usr/bin/env python3
"""Create immutable, provenance-carrying Amitel Brain candidates in inbox/."""
import argparse
import json
import os
import re
import secrets
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from brain_context import _opened_path

ALLOWED_TYPES = {"lesson", "decision", "preference", "domain"}
ALLOWED_SOURCE_SCHEMES = {"session", "file", "url", "git", "email", "ticket", "meeting"}
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.I),
    re.compile(r"\b(?:api[_-]?key|token|password|passwd|secret)\b\s*[:=]\s*[\"']?[A-Za-z0-9_./+=-]{8,}", re.I),
    re.compile(r"\b(?:sk|xox[baprs])-[A-Za-z0-9_-]{12,}\b", re.I),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b", re.I),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b", re.I),
]
PII_PATTERNS = [
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
    re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}\b", re.I),
    re.compile(r"\b(?:\+33|0)[1-9](?:[ .-]?\d{2}){4}\b"),
]


def _contains_likely_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def _contains_likely_pii(text: str) -> bool:
    return any(pattern.search(text) for pattern in PII_PATTERNS)


def _valid_source(source: str) -> bool:
    scheme, separator, locator = source.strip().partition(":")
    scheme, locator = scheme.casefold(), locator.strip()
    if separator != ":" or scheme not in ALLOWED_SOURCE_SCHEMES or not locator:
        return False
    if scheme == "session":
        return re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{5,127}", locator) is not None
    if scheme == "file":
        return Path(locator).expanduser().is_file()
    if scheme == "url":
        parsed = urlparse(locator)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    if scheme == "git":
        return re.fullmatch(r".+@[0-9a-fA-F]{7,64}", locator) is not None
    if scheme == "email":
        return re.fullmatch(r"<?[^<>\s@]+@[^<>\s@]+>?", locator) is not None
    if scheme == "ticket":
        return re.fullmatch(r"[A-Z][A-Z0-9]{1,15}-\d{1,12}", locator) is not None
    if scheme == "meeting":
        return re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T /][A-Za-z0-9._:+ -]{1,100})?", locator) is not None
    return False


def _slug(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-") or "candidate"


def _yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def propose_note(
    inbox_dir: str | Path,
    *,
    title: str,
    body: str,
    note_type: str,
    scope: str,
    author_agent: str,
    model: str,
    source: str,
    tags: list[str] | None = None,
    confidence: str = "medium",
    brain_root: str | Path | None = None,
) -> Path:
    required = {
        "title": title, "scope": scope, "author_agent": author_agent,
        "model": model, "source": source,
    }
    for field, value in required.items():
        if not value.strip():
            raise ValueError(f"{field} is empty")
    if not _valid_source(source):
        allowed = ", ".join(sorted(ALLOWED_SOURCE_SCHEMES))
        raise ValueError(f"source locator is not verifiable ({allowed})")
    if note_type not in ALLOWED_TYPES:
        raise ValueError(f"unsupported type: {note_type}")
    if not body.strip():
        raise ValueError("body is empty")
    if _contains_likely_secret("\n".join((title, body, source))):
        raise ValueError("likely secret detected; candidate rejected")
    if _contains_likely_pii("\n".join((title, body))):
        raise ValueError("likely personal data detected; candidate rejected")
    inbox = Path(inbox_dir).resolve()
    if inbox.name.casefold() != "inbox" or any(parent.name.casefold() == "knowledge" for parent in inbox.parents):
        raise ValueError("inbox target must be an inbox/ directory outside knowledge/")
    if brain_root is not None and inbox != (Path(brain_root).resolve() / "inbox"):
        raise ValueError("inbox target is not the canonical repository inbox/")
    inbox.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    content = (
        "---\n"
        f"type: {note_type}\n"
        f"scope: {_yaml_string(scope.strip())}\n"
        f"author_agent: {_yaml_string(author_agent.strip())}\n"
        f"model: {_yaml_string(model.strip())}\n"
        f"created: {now:%Y-%m-%d}\n"
        "status: candidate\n"
        "supersedes: []\n"
        f"tags: {json.dumps(tags or [], ensure_ascii=False)}\n"
        f"source: {_yaml_string(source.strip())}\n"
        f"confidence: {_yaml_string(confidence)}\n"
        "---\n\n"
        f"# {title.strip()}\n\n{body.strip()}\n"
    )
    encoded = content.encode("utf-8")
    for _ in range(5):
        path = inbox / f"{now:%Y%m%d-%H%M%S}-{_slug(title)}-{secrets.token_hex(8)}.md"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            continue
        try:
            opened = _opened_path(fd)
            if opened is None or opened.resolve().parent != inbox or opened.name != path.name:
                raise ValueError("created candidate escaped the canonical inbox/")
            view = memoryview(encoded)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("candidate write made no progress")
                view = view[written:]
            os.fsync(fd)
            return path
        finally:
            os.close(fd)
    raise RuntimeError("could not allocate a unique candidate filename")


def main() -> None:
    brain_root = Path(__file__).resolve().parents[1]
    ap = argparse.ArgumentParser()
    ap.add_argument("--inbox", default=brain_root / "inbox")
    ap.add_argument("--title", required=True)
    ap.add_argument("--type", required=True, choices=sorted(ALLOWED_TYPES))
    ap.add_argument("--scope", required=True)
    ap.add_argument("--author-agent", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--source", required=True)
    ap.add_argument("--tag", action="append", default=[])
    ap.add_argument("--confidence", choices=["low", "medium", "high"], default="medium")
    ap.add_argument("--body", help="Candidate body; defaults to stdin")
    args = ap.parse_args()
    body = args.body if args.body is not None else sys.stdin.read()
    print(propose_note(
        args.inbox, title=args.title, body=body, note_type=args.type, scope=args.scope,
        author_agent=args.author_agent, model=args.model, source=args.source,
        tags=args.tag, confidence=args.confidence, brain_root=brain_root,
    ).as_posix())


if __name__ == "__main__":
    main()
