#!/usr/bin/env python3
"""Validate the governed Amitel Brain architecture without mutating the vault."""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from rig_coverage import REGISTRY_PATH, check_registry
from obsidian_graph import graph_errors
from brain_verify import validate_contract_uri

SCHEMA = "amitel-brain/v1"
CANDIDATE_SCHEMA = "amitel-brain/candidate-v1"
CANONICAL_SOURCE = Path("knowledge/domain/rigapplication-documentation")
REQUIRED_SOURCE_FILES = tuple(Path(path) for path in (
    "_IMPORT.md",
    "RECONSTRUCT.md",
    "RIG-Dispatcher.md",
    "SITEMAP.md",
))
KNOWLEDGE_TEMPLATE = Path("knowledge/_TEMPLATE.md")
REQUIRED_V1_NOTES = tuple(Path(path) for path in (
    "knowledge/_maps/brain.md",
    "knowledge/_maps/rig.md",
    "knowledge/_maps/contribution.md",
    "knowledge/runbooks/_index.md",
    "knowledge/standards/_index.md",
))
REQUIRED_ARCHITECTURE_FILES = tuple(Path(path) for path in (
    "HOME.md",
    "index.md",
    "governance/ARCHITECTURE.md",
    "governance/CONTRIBUTING.md",
    "governance/LIFECYCLE.md",
    "governance/NOTE-SCHEMA-v1.md",
    "governance/templates/candidate.md",
    "governance/templates/domain.md",
    "governance/templates/decision.md",
    "governance/templates/lesson.md",
    "governance/templates/runbook.md",
    "governance/templates/standard.md",
    "knowledge/_TEMPLATE.md",
)) + REQUIRED_V1_NOTES
REQUIRED_FIELDS = {
    "schema", "uid", "type", "kind", "scope", "author_agent", "model",
    "created", "updated", "status", "confidence", "sources", "supersedes", "tags",
    "reviewed_by", "reviewed_at", "mocs",
}
ALLOWED_TYPES = {"lesson", "decision", "preference", "domain"}
ALLOWED_KINDS = {
    "concept", "system", "workflow", "fact", "map", "runbook", "standard",
    "decision", "lesson", "preference", "source-index",
}
ALLOWED_ACTIVE_STATUSES = {"active", "superseded", "archived"}
ALLOWED_CONFIDENCE = {"confirmed", "derived", "hypothesis"}
ALLOWED_THEMES = {
    "theme/rig", "theme/architecture", "theme/donnees", "theme/integrations",
    "theme/operations", "theme/ia", "theme/gouvernance", "theme/autowin-os",
}
UID_RE = re.compile(r"^[a-z0-9][a-z0-9:/._-]{2,127}$")
AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?::[A-Za-z0-9][A-Za-z0-9._-]*)*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
WIKILINK_RE = re.compile(r"\[\[([^\[\]\n]+)\]\]")
COMPANY_MOC = Path("knowledge/_maps/brain.md")
RIG_MOC = Path("knowledge/_maps/rig.md")


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=lambda: {
        "architecture_files": 0,
        "v1_notes": 0,
        "active_notes": 0,
        "orphan_notes": 0,
        "legacy_notes": 0,
        "candidate_notes": 0,
        "canonical_source_files": 0,
        "coverage_entries": 0,
        "errors": 0,
        "warnings": 0,
    })

    def finish(self) -> "ValidationReport":
        self.counts["errors"] = len(self.errors)
        self.counts["warnings"] = len(self.warnings)
        return self

    def as_dict(self) -> dict[str, object]:
        return {
            "status": "valid" if not self.errors else "invalid",
            "schema": SCHEMA,
            "counts": self.counts,
            "errors": self.errors,
            "warnings": self.warnings,
        }


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _frontmatter(text: str) -> dict[str, str] | None:
    text = text.removeprefix("\ufeff")
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end < 0:
        return None
    result: dict[str, str] = {}
    for raw_line in text[4:end].splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key in result:
            raise ValueError(f"duplicate frontmatter key: {key}")
        result[key] = value.strip()
    return result


def _list_value(raw: str) -> list[str] | None:
    value = raw.strip()
    if not (value.startswith("[") and value.endswith("]")):
        return None
    inside = value[1:-1].strip()
    if not inside:
        return []
    return [part.strip().strip('"\'').strip() for part in inside.split(",") if part.strip()]


def _date_value(raw: str) -> date | None:
    value = raw.strip().strip('"\'')
    if not DATE_RE.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _agent_family(value: str) -> str:
    return value.strip().strip('"\'').split(":", 1)[0].strip().casefold()


def _strip_fenced_blocks(text: str) -> str:
    visible: list[str] = []
    fence_char = ""
    fence_length = 0
    for line in text.splitlines(keepends=True):
        if fence_char:
            closing = re.match(
                rf"^[ \t]{{0,3}}{re.escape(fence_char)}{{{fence_length},}}[ \t]*$",
                line.rstrip("\r\n"),
            )
            if closing:
                fence_char = ""
                fence_length = 0
            continue
        opening = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})", line)
        if opening:
            fence_char = opening.group(1)[0]
            fence_length = len(opening.group(1))
            continue
        visible.append(line)
    return "".join(visible)


def _wikilinks(text: str) -> tuple[list[str], bool]:
    navigable = text.removeprefix("\ufeff")
    if navigable.startswith("---\n"):
        end = navigable.find("\n---\n", 4)
        if end >= 0:
            navigable = navigable[end + 5:]
    navigable = _strip_fenced_blocks(navigable)
    links = WIKILINK_RE.findall(navigable)
    remainder = WIKILINK_RE.sub("", navigable)
    return links, "[[" not in remainder and "]]" not in remainder


def _link_path(root: Path, raw_link: str, current: Path | None = None) -> Path | None:
    target = raw_link.split("|", 1)[0].split("#", 1)[0].strip().replace("\\", "/")
    path = current if not target and current is not None else root / target
    path = path if path.suffix else path.with_suffix(".md")
    root_resolved = root.resolve()
    path_resolved = path.resolve()
    try:
        path_resolved.relative_to(root_resolved)
    except ValueError:
        return None
    return path_resolved


def _link_anchor(raw_link: str) -> str:
    target = raw_link.split("|", 1)[0]
    return target.split("#", 1)[1].strip() if "#" in target else ""


def _anchor_exists(path: Path, anchor: str) -> bool:
    if not anchor:
        return True
    text = path.read_text(encoding="utf-8")
    if anchor.startswith("^"):
        return re.search(rf"(?m)(?:^|\s){re.escape(anchor)}(?:\s|$)", text) is not None
    wanted = " ".join(anchor.casefold().split())
    headings = re.findall(r"(?m)^#{1,6}\s+(.+?)\s*#*\s*$", text)
    return any(" ".join(heading.casefold().split()) == wanted for heading in headings)


def _reachable_active_notes(
    start: Path,
    root: Path,
    active_notes: dict[Path, tuple[str, dict[str, str], list[str]]],
) -> set[Path]:
    pending = [(root / start).resolve()]
    visited: set[Path] = set()
    while pending:
        current = pending.pop()
        if current in visited or current not in active_notes:
            continue
        visited.add(current)
        for raw_link in active_notes[current][2]:
            target = _link_path(root, raw_link, current)
            if (
                target is not None
                and target in active_notes
                and _anchor_exists(target, _link_anchor(raw_link))
                and target not in visited
            ):
                pending.append(target)
    return visited


def _validate_navigation_graph(
    root: Path,
    active_notes: dict[Path, tuple[str, dict[str, str], list[str]]],
    report: ValidationReport,
) -> None:
    company_reachable = _reachable_active_notes(COMPANY_MOC, root, active_notes)
    rig_reachable = _reachable_active_notes(RIG_MOC, root, active_notes)
    orphans: set[Path] = set()
    for path, (relative, metadata, _) in sorted(active_notes.items(), key=lambda item: item[1][0]):
        if path not in company_reachable:
            report.errors.append(
                f"{relative}: active note is orphaned from company MOC {COMPANY_MOC.as_posix()}"
            )
            orphans.add(path)
        scope = metadata.get("scope", "").strip().strip('"\'')
        if scope == "rig" and path not in rig_reachable:
            report.errors.append(
                f"{relative}: active RIG note is orphaned from RIG MOC {RIG_MOC.as_posix()}"
            )
            orphans.add(path)
    report.counts["active_notes"] = len(active_notes)
    report.counts["orphan_notes"] = len(orphans)


def _validate_v1_note(
    path: Path,
    root: Path,
    text: str,
    metadata: dict[str, str],
    report: ValidationReport,
    seen_uids: dict[str, tuple[str, dict[str, str]]],
) -> None:
    relative = _relative(path, root)
    missing = sorted(REQUIRED_FIELDS - metadata.keys())
    if missing:
        report.errors.append(f"{relative}: missing required fields: {', '.join(missing)}")
        return

    uid = metadata["uid"].strip().strip('"\'')
    if not UID_RE.fullmatch(uid):
        report.errors.append(f"{relative}: invalid uid: {uid!r}")
    elif uid in seen_uids:
        report.errors.append(f"{relative}: duplicate uid {uid!r}; first seen in {seen_uids[uid][0]}")
    else:
        seen_uids[uid] = (relative, metadata)

    note_type = metadata["type"].strip().strip('"\'')
    kind = metadata["kind"].strip().strip('"\'')
    status = metadata["status"].strip().strip('"\'')
    confidence = metadata["confidence"].strip().strip('"\'')
    if note_type not in ALLOWED_TYPES:
        report.errors.append(f"{relative}: unsupported type {note_type!r}")
    if kind not in ALLOWED_KINDS:
        report.errors.append(f"{relative}: unsupported kind {kind!r}")
    if status not in ALLOWED_ACTIVE_STATUSES:
        report.errors.append(f"{relative}: status {status!r} is not allowed inside knowledge/")
    if confidence not in ALLOWED_CONFIDENCE:
        report.errors.append(f"{relative}: unsupported confidence {confidence!r}")

    created = _date_value(metadata["created"])
    updated = _date_value(metadata["updated"])
    if created is None or updated is None:
        report.errors.append(f"{relative}: created and updated must be ISO dates")
    elif updated < created:
        report.errors.append(f"{relative}: updated precedes created")

    for key in ("sources", "supersedes", "tags", "reviewed_by", "mocs"):
        if _list_value(metadata[key]) is None:
            report.errors.append(f"{relative}: {key} must be an inline list")
    if "verification_contracts" in metadata:
        contracts = _list_value(metadata["verification_contracts"])
        if contracts is None:
            report.errors.append(f"{relative}: verification_contracts must be an inline list")
        else:
            for contract in contracts:
                try:
                    validate_contract_uri(contract)
                except ValueError as exc:
                    report.errors.append(f"{relative}: invalid verification contract: {exc}")
    sources = _list_value(metadata["sources"])
    if kind not in {"map", "preference"} and sources == []:
        report.errors.append(f"{relative}: kind {kind!r} requires at least one source")

    tags = _list_value(metadata["tags"])
    themes = [] if tags is None else [tag for tag in tags if tag.startswith("theme/")]
    unsupported_themes = sorted(set(themes) - ALLOWED_THEMES)
    if unsupported_themes:
        report.errors.append(f"{relative}: unsupported themes: {', '.join(unsupported_themes)}")
    if status == "active" and not 1 <= len(themes) <= 2:
        report.errors.append(f"{relative}: active note must have 1 or 2 controlled theme/* tags")

    reviewed_by = _list_value(metadata["reviewed_by"])
    author = metadata["author_agent"].strip().strip('"\'')
    if not AGENT_ID_RE.fullmatch(author):
        report.errors.append(f"{relative}: invalid author_agent identifier")
    if reviewed_by is None or any(not reviewer.strip() for reviewer in reviewed_by):
        report.errors.append(f"{relative}: reviewer identifiers must be non-empty")
    elif any(not AGENT_ID_RE.fullmatch(reviewer) for reviewer in reviewed_by):
        report.errors.append(f"{relative}: invalid reviewer identifier")
    elif any(reviewer.casefold().startswith(("pending", "todo", "tbd")) for reviewer in reviewed_by):
        report.errors.append(f"{relative}: reviewer placeholder is not a completed review")
    elif not reviewed_by or not any(_agent_family(reviewer) != _agent_family(author) for reviewer in reviewed_by):
        report.errors.append(f"{relative}: managed note requires at least one distinct reviewer agent family")
    if _date_value(metadata["reviewed_at"]) is None:
        report.errors.append(f"{relative}: reviewed_at must be an ISO date")

    links, links_complete = _wikilinks(text)
    if not links_complete:
        report.errors.append(f"{relative}: malformed wikilink")
    if kind == "map" and not links:
        report.errors.append(f"{relative}: map must contain at least one complete wikilink")
    for link in links:
        link_path = _link_path(root, link, path)
        if link_path is None:
            report.errors.append(f"{relative}: wikilink escapes outside brain root: {link!r}")
        elif not link_path.is_file():
            report.errors.append(f"{relative}: unresolved wikilink {link!r}")
        elif not _anchor_exists(link_path, _link_anchor(link)):
            report.errors.append(f"{relative}: unresolved wikilink anchor {link!r}")

    mocs = _list_value(metadata["mocs"])
    if kind != "map" and not mocs:
        report.errors.append(f"{relative}: non-map note must reference at least one MOC")
    for moc in mocs or []:
        moc_path = _link_path(root, moc)
        maps_root = (root / "knowledge/_maps").resolve()
        if moc_path is None:
            report.errors.append(f"{relative}: invalid MOC reference {moc!r}")
            continue
        try:
            moc_path.relative_to(maps_root)
        except ValueError:
            report.errors.append(f"{relative}: invalid MOC reference {moc!r}")
            continue
        if not moc_path.is_file():
            report.errors.append(f"{relative}: unresolved MOC reference {moc!r}")

    report.counts["v1_notes"] += 1


def validate_brain(root: str | Path) -> ValidationReport:
    root_path = Path(root)
    report = ValidationReport()
    try:
        return _validate_brain_checked(root_path, report)
    except OSError as exc:
        report.errors.append(f"cannot access brain root {root_path}: {exc}")
        return report.finish()


def _validate_brain_checked(root_path: Path, report: ValidationReport) -> ValidationReport:
    if not root_path.is_dir():
        report.errors.append(f"brain root is not a directory: {root_path}")
        return report.finish()

    for relative in REQUIRED_ARCHITECTURE_FILES:
        if (root_path / relative).is_file():
            report.counts["architecture_files"] += 1
        else:
            report.errors.append(f"missing architecture file: {relative.as_posix()}")

    source_root = root_path / CANONICAL_SOURCE
    if source_root.is_dir():
        report.counts["canonical_source_files"] = sum(1 for path in source_root.rglob("*") if path.is_file())
    missing_source_files = [relative.as_posix() for relative in REQUIRED_SOURCE_FILES if not (source_root / relative).is_file()]
    if missing_source_files:
        report.errors.append(
            f"canonical source is missing required files: {', '.join(missing_source_files)}"
        )

    coverage_errors = check_registry(root_path)
    report.errors.extend(coverage_errors)
    report.errors.extend(graph_errors(root_path))
    registry_path = root_path / REGISTRY_PATH
    if not coverage_errors and registry_path.is_file():
        try:
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            entries = registry.get("entries", []) if isinstance(registry, dict) else []
            report.counts["coverage_entries"] = len(entries) if isinstance(entries, list) else 0
        except (OSError, json.JSONDecodeError):
            pass

    seen_uids: dict[str, tuple[str, dict[str, str]]] = {}
    active_notes: dict[Path, tuple[str, dict[str, str], list[str]]] = {}
    knowledge = root_path / "knowledge"
    if not knowledge.is_dir():
        report.errors.append("missing knowledge/ directory")
    else:
        for path in sorted(knowledge.rglob("*.md")):
            if path.relative_to(root_path) == KNOWLEDGE_TEMPLATE:
                continue
            try:
                path.relative_to(source_root)
                continue
            except ValueError:
                pass
            text = path.read_text(encoding="utf-8")
            try:
                metadata = _frontmatter(text)
            except ValueError as exc:
                report.errors.append(f"{_relative(path, root_path)}: {exc}")
                continue
            schema = "" if metadata is None else metadata.get("schema", "").strip().strip('"\'')
            status = "" if metadata is None else metadata.get("status", "").strip().strip('"\'')
            if schema == CANDIDATE_SCHEMA or status == "candidate":
                report.errors.append(f"{_relative(path, root_path)}: candidate note is not allowed inside knowledge/")
                continue
            managed_prefix = text.removeprefix("\ufeff")
            managed_schema = re.search(
                r'(?m)^\s*schema\s*:\s*["\']?amitel-brain/(?:v1|candidate-v1)["\']?\s*$',
                managed_prefix,
            )
            if metadata is None and managed_schema:
                report.errors.append(f"{_relative(path, root_path)}: unparseable managed frontmatter")
                continue
            if metadata is None and managed_prefix.startswith("---\n"):
                report.errors.append(f"{_relative(path, root_path)}: unclosed frontmatter")
                continue
            if metadata is None or schema != SCHEMA:
                report.counts["legacy_notes"] += 1
                if path.relative_to(root_path) in REQUIRED_V1_NOTES:
                    report.errors.append(f"{_relative(path, root_path)}: architecture notes must use schema {SCHEMA}")
                if metadata is not None and status == "active":
                    active_notes[path.resolve()] = (
                        _relative(path, root_path),
                        metadata,
                        _wikilinks(text)[0],
                    )
                continue
            _validate_v1_note(path, root_path, text, metadata, report, seen_uids)
            if status == "active" and REQUIRED_FIELDS <= metadata.keys():
                active_notes[path.resolve()] = (
                    _relative(path, root_path),
                    metadata,
                    _wikilinks(text)[0],
                )

    _validate_navigation_graph(root_path, active_notes, report)

    covered_themes = {
        theme
        for _relative_path, metadata in seen_uids.values()
        if metadata.get("status", "").strip().strip('"\'') == "active"
        for theme in (_list_value(metadata.get("tags", "")) or [])
        if theme in ALLOWED_THEMES
    }
    missing_themes = sorted(ALLOWED_THEMES - covered_themes)
    if missing_themes:
        report.errors.append(f"controlled themes without active v1 notes: {', '.join(missing_themes)}")

    referenced_superseded: set[str] = set()
    for uid, (relative, metadata) in seen_uids.items():
        replacement_status = metadata.get("status", "").strip().strip('"\'')
        for old_uid in _list_value(metadata.get("supersedes", "")) or []:
            if old_uid == uid:
                report.errors.append(f"{relative}: note cannot supersede itself")
            elif old_uid not in seen_uids:
                report.errors.append(f"{relative}: unknown superseded uid {old_uid!r}")
            else:
                old_status = seen_uids[old_uid][1].get("status", "").strip().strip('"\'')
                if old_status != "superseded":
                    report.errors.append(
                        f"{relative}: superseded uid {old_uid!r} must have status superseded, got {old_status!r}"
                    )
                if replacement_status == "active":
                    referenced_superseded.add(old_uid)
    for uid, (relative, metadata) in seen_uids.items():
        status = metadata.get("status", "").strip().strip('"\'')
        if status == "superseded" and uid not in referenced_superseded:
            report.errors.append(f"{relative}: superseded note {uid!r} is not referenced by an active replacement")

    inbox = root_path / "inbox"
    if inbox.is_dir():
        for path in sorted(inbox.glob("*.md")):
            metadata = _frontmatter(path.read_text(encoding="utf-8"))
            if metadata is None:
                continue
            status = metadata.get("status", "").strip().strip('"\'')
            if status != "candidate":
                report.errors.append(f"{_relative(path, root_path)}: inbox note must have status candidate")
            else:
                report.counts["candidate_notes"] += 1

    if report.counts["legacy_notes"]:
        report.warnings.append(
            f"{report.counts['legacy_notes']} legacy curated notes remain valid during gradual migration"
        )
    return report.finish()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", action="store_true", help="pretty-print JSON")
    args = parser.parse_args()
    report = validate_brain(args.root)
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2 if args.json else None))
    raise SystemExit(0 if not report.errors else 2)


if __name__ == "__main__":
    main()
