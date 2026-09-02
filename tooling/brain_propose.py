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
from brain_context import _opened_path
from brain_candidate_policy import (
    ALLOWED_SOURCE_SCHEMES,
    contains_likely_pii as _contains_likely_pii,
    contains_likely_secret as _contains_likely_secret,
    scan_candidate,
    valid_source as _valid_source,
)

ALLOWED_TYPES = {"lesson", "decision", "preference", "domain"}
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
    policy_finding = scan_candidate(title, body, source, "\n".join((scope, author_agent, model, " ".join(tags or []))))
    if policy_finding:
        raise ValueError(f"{policy_finding}; candidate rejected")
    inbox = Path(inbox_dir).resolve()
    if inbox.name.casefold() != "inbox" or any(parent.name.casefold() == "knowledge" for parent in inbox.parents):
        raise ValueError("inbox target must be an inbox/ directory outside knowledge/")
    if brain_root is not None and inbox != (Path(brain_root).resolve() / "inbox"):
        raise ValueError("inbox target is not the canonical repository inbox/")
    inbox.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    content = (
        "---\n"
        "schema: amitel-brain/candidate-v1\n"
        f"type: {note_type}\n"
        f"kind: {note_type if note_type != 'domain' else 'concept'}\n"
        f"scope: {_yaml_string(scope.strip())}\n"
        f"author_agent: {_yaml_string(author_agent.strip())}\n"
        f"model: {_yaml_string(model.strip())}\n"
        f"created: {now:%Y-%m-%d}\n"
        "status: candidate\n"
        "supersedes: []\n"
        f"tags: {json.dumps(tags or [], ensure_ascii=False)}\n"
        "mocs: []\n"
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
