#!/usr/bin/env python3
"""Build and validate a deterministic RIG knowledge coverage registry."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

COVERAGE_SCHEMA = "amitel-brain/rig-coverage-v1"
SOURCE_MIRROR = Path("knowledge/domain/rigapplication-documentation")
SOURCE_MANIFEST = SOURCE_MIRROR / "_SOURCE_MANIFEST.json"
REGISTRY_PATH = Path("knowledge/_generated/rig-coverage.json")
OVERRIDES_PATH = Path("governance/rig-coverage-overrides.json")
ALLOWED_LEVELS = {"source-only", "curated", "code-traced", "runtime-verified", "blocked"}


def _load_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def _resolve_inside(base: Path, raw: str, label: str) -> tuple[str, Path]:
    relative = Path(raw.replace("\\", "/"))
    if relative.is_absolute():
        raise ValueError(f"{label} escapes vault: {raw}")
    base_resolved = base.resolve()
    resolved = (base / relative).resolve()
    try:
        resolved.relative_to(base_resolved)
    except ValueError as exc:
        raise ValueError(f"{label} escapes vault: {raw}") from exc
    return relative.as_posix(), resolved


def build_registry(root: str | Path) -> dict[str, object]:
    root_path = Path(root)
    manifest = _load_json(root_path / SOURCE_MANIFEST)
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list):
        raise ValueError(f"{SOURCE_MANIFEST.as_posix()}: files must be a list")
    declared_count = manifest.get("file_count")
    if isinstance(declared_count, bool) or not isinstance(declared_count, int) or declared_count != len(raw_files):
        raise ValueError(
            f"{SOURCE_MANIFEST.as_posix()}: file_count {declared_count!r} does not match {len(raw_files)} files"
        )

    mirror_root = root_path / SOURCE_MIRROR
    entries: list[dict[str, str]] = []
    for raw_file in raw_files:
        if not isinstance(raw_file, dict) or not isinstance(raw_file.get("path"), str):
            raise ValueError(f"{SOURCE_MANIFEST.as_posix()}: every file requires a string path")
        source_path, _ = _resolve_inside(mirror_root, raw_file["path"], "manifest path")
        entries.append({
            "id": f"doc:{source_path}",
            "kind": "document",
            "path": (SOURCE_MIRROR / source_path).as_posix(),
            "level": "source-only",
            "route": "knowledge/_maps/rig-couverture.md",
        })

    for graph_path in sorted((root_path / "projects").glob("*/graphify-out/graph.json")):
        project = graph_path.parents[1].name
        graph_relative = graph_path.relative_to(root_path).as_posix()
        entries.append({
            "id": f"graph:{project}",
            "kind": "graph",
            "path": graph_relative,
            "level": "code-traced",
            "route": "knowledge/domain/rig-ast-graphes-vers-phases-reconstruction.md",
            "evidence": graph_relative,
        })

    entries.sort(key=lambda entry: entry["id"])
    override_path = root_path / OVERRIDES_PATH
    if override_path.is_file():
        override_document = _load_json(override_path)
        if override_document.get("schema") != COVERAGE_SCHEMA:
            raise ValueError(f"{OVERRIDES_PATH.as_posix()}: unsupported schema")
        raw_overrides = override_document.get("overrides")
        if not isinstance(raw_overrides, list):
            raise ValueError(f"{OVERRIDES_PATH.as_posix()}: overrides must be a list")
        by_id = {entry["id"]: entry for entry in entries}
        seen_overrides: set[str] = set()
        for override in raw_overrides:
            if not isinstance(override, dict) or not isinstance(override.get("id"), str):
                raise ValueError(f"{OVERRIDES_PATH.as_posix()}: every override requires a string id")
            override_id = override["id"]
            if override_id in seen_overrides:
                raise ValueError(f"duplicate coverage override id {override_id}")
            if override_id not in by_id:
                raise ValueError(f"unknown coverage id {override_id}")
            seen_overrides.add(override_id)
            for key in ("level", "route", "justification", "evidence"):
                if key in override:
                    if not isinstance(override[key], str):
                        raise ValueError(f"{override_id}: override {key} must be a string")
                    by_id[override_id][key] = override[key]

    document_count = sum(entry["kind"] == "document" for entry in entries)
    graph_count = sum(entry["kind"] == "graph" for entry in entries)
    return {
        "schema": COVERAGE_SCHEMA,
        "source_repo_head": manifest.get("source_repo_head", ""),
        "counts": {
            "documents": document_count,
            "graphs": graph_count,
            "total": len(entries),
        },
        "entries": entries,
    }


def validate_registry(root: str | Path, registry: dict[str, object]) -> list[str]:
    root_path = Path(root)
    errors: list[str] = []
    if registry.get("schema") != COVERAGE_SCHEMA:
        errors.append(f"unsupported registry schema {registry.get('schema')!r}")
    entries = registry.get("entries", [])
    if not isinstance(entries, list):
        return errors + ["registry entries must be a list"]
    seen_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            errors.append("registry entry must be an object")
            continue
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id:
            errors.append("registry entry requires a non-empty id")
        elif entry_id in seen_ids:
            errors.append(f"duplicate id {entry_id}")
        else:
            seen_ids.add(entry_id)

        level = entry.get("level")
        if not isinstance(level, str):
            errors.append(f"{entry_id or '<unknown>'}: level must be a string")
        elif level not in ALLOWED_LEVELS:
            errors.append(f"{entry_id or '<unknown>'}: unsupported level {level!r}")
        elif level in {"curated", "code-traced", "runtime-verified"}:
            evidence = entry.get("evidence")
            if not isinstance(evidence, str) or not evidence.strip():
                errors.append(f"{entry_id or '<unknown>'}: level {level} requires evidence")

        relative = entry.get("path")
        if not isinstance(relative, str):
            errors.append(f"{entry_id or '<unknown>'}: path must be a string")
        else:
            try:
                _, resolved = _resolve_inside(root_path, relative, "path")
                if not resolved.is_file():
                    errors.append(f"{entry_id or '<unknown>'}: missing path {relative}")
            except ValueError:
                errors.append(f"{entry_id or '<unknown>'}: path escapes vault: {relative}")

        route = entry.get("route")
        if not isinstance(route, str):
            errors.append(f"{entry_id or '<unknown>'}: missing route {route!r}")
        else:
            try:
                _, resolved_route = _resolve_inside(root_path, route, "route")
                if not resolved_route.is_file():
                    errors.append(f"{entry_id or '<unknown>'}: missing route {route!r}")
            except ValueError:
                errors.append(f"{entry_id or '<unknown>'}: route escapes vault: {route}")

        if level == "blocked" and not str(entry.get("justification", "")).strip():
            errors.append(f"{entry_id or '<unknown>'}: blocked level requires justification")
    return errors


def write_registry(root: str | Path) -> Path:
    root_path = Path(root)
    registry = build_registry(root_path)
    errors = validate_registry(root_path, registry)
    if errors:
        raise ValueError("cannot write invalid coverage registry: " + "; ".join(errors))
    output = root_path / REGISTRY_PATH
    output.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(registry, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=output.parent,
        prefix=f".{output.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(serialized)
        temporary = Path(handle.name)
    os.replace(temporary, output)
    return output


def check_registry(root: str | Path) -> list[str]:
    root_path = Path(root)
    output = root_path / REGISTRY_PATH
    if not output.is_file():
        return [f"missing coverage registry: {REGISTRY_PATH.as_posix()}"]
    try:
        actual = _load_json(output)
        expected = build_registry(root_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [f"cannot read coverage registry: {exc}"]
    errors = validate_registry(root_path, actual)
    if actual != expected:
        errors.append(f"coverage registry is out of date: {REGISTRY_PATH.as_posix()}")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", action="store_true", help="atomically regenerate the registry")
    action.add_argument("--check", action="store_true", help="validate the current registry")
    args = parser.parse_args()

    try:
        if args.write:
            output = write_registry(args.root)
            payload = {"status": "written", "path": output.relative_to(args.root).as_posix(), "errors": []}
            code = 0
        else:
            errors = check_registry(args.root)
            payload = {"status": "valid" if not errors else "invalid", "errors": errors}
            code = 0 if not errors else 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        payload = {"status": "invalid", "errors": [str(exc)]}
        code = 2
    print(json.dumps(payload, ensure_ascii=False))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
