import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLING = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLING))

from rig_coverage import (
    COVERAGE_SCHEMA,
    build_registry,
    check_registry,
    validate_registry,
    write_registry,
)


class RigCoverageTests(unittest.TestCase):
    def make_vault(self, root: Path) -> None:
        mirror = root / "knowledge/domain/rigapplication-documentation"
        mirror.mkdir(parents=True)
        files = [
            {"path": "reference/20-host-plugins/host.md", "bytes": 5, "sha256": "a" * 64},
            {"path": "reference/proc/proc_test.md", "bytes": 4, "sha256": "b" * 64},
        ]
        (mirror / "reference/20-host-plugins").mkdir(parents=True)
        (mirror / "reference/proc").mkdir(parents=True)
        (mirror / "reference/20-host-plugins/host.md").write_text("host\n", encoding="utf-8")
        (mirror / "reference/proc/proc_test.md").write_text("proc", encoding="utf-8")
        (mirror / "_SOURCE_MANIFEST.json").write_text(
            json.dumps({
                "schema": 1,
                "source_repo_head": "abc123",
                "file_count": len(files),
                "files": files,
            }),
            encoding="utf-8",
        )
        graph = root / "projects/rig-processus/graphify-out/graph.json"
        graph.parent.mkdir(parents=True)
        graph.write_text(json.dumps({"nodes": [{"id": "A"}], "links": []}), encoding="utf-8")
        coverage_map = root / "knowledge/_maps/rig-couverture.md"
        coverage_map.parent.mkdir(parents=True)
        coverage_map.write_text("# Couverture\n", encoding="utf-8")
        ast_map = root / "knowledge/domain/rig-ast-graphes-vers-phases-reconstruction.md"
        ast_map.parent.mkdir(parents=True, exist_ok=True)
        ast_map.write_text("# AST\n", encoding="utf-8")

    def test_build_registry_inventories_manifest_and_graphs_deterministically(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.make_vault(root)

            first = build_registry(root)
            second = build_registry(root)

            self.assertEqual(first, second)
            self.assertEqual(first["schema"], COVERAGE_SCHEMA)
            self.assertEqual(first["source_repo_head"], "abc123")
            self.assertEqual(first["counts"], {"documents": 2, "graphs": 1, "total": 3})
            self.assertEqual(
                [entry["id"] for entry in first["entries"]],
                [
                    "doc:reference/20-host-plugins/host.md",
                    "doc:reference/proc/proc_test.md",
                    "graph:rig-processus",
                ],
            )
            self.assertTrue(all(entry["level"] in {"source-only", "code-traced"} for entry in first["entries"]))

    def test_validate_registry_rejects_missing_source_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.make_vault(root)
            registry = build_registry(root)
            (root / "knowledge/domain/rigapplication-documentation/reference/proc/proc_test.md").unlink()

            errors = validate_registry(root, registry)

            self.assertTrue(any("missing path" in error and "proc_test.md" in error for error in errors))

    def test_validate_registry_rejects_invalid_identity_level_route_and_blocker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.make_vault(root)
            registry = build_registry(root)

            cases = []
            duplicate = json.loads(json.dumps(registry))
            duplicate["entries"].append(dict(duplicate["entries"][0]))
            cases.append((duplicate, "duplicate id"))

            invalid_level = json.loads(json.dumps(registry))
            invalid_level["entries"][0]["level"] = "invented"
            cases.append((invalid_level, "unsupported level"))

            missing_route = json.loads(json.dumps(registry))
            missing_route["entries"][0]["level"] = "curated"
            missing_route["entries"][0]["route"] = "knowledge/domain/missing.md"
            cases.append((missing_route, "missing route"))

            unjustified = json.loads(json.dumps(registry))
            unjustified["entries"][0]["level"] = "blocked"
            cases.append((unjustified, "requires justification"))

            for broken_registry, expected in cases:
                with self.subTest(expected=expected):
                    errors = validate_registry(root, broken_registry)
                    self.assertTrue(any(expected in error for error in errors), errors)

    def test_written_registry_is_reproducible_and_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.make_vault(root)

            output = write_registry(root)

            self.assertEqual(check_registry(root), [])
            payload = json.loads(output.read_text(encoding="utf-8"))
            payload["entries"].pop()
            output.write_text(json.dumps(payload), encoding="utf-8")
            self.assertTrue(any("out of date" in error for error in check_registry(root)))

    def test_cli_writes_and_checks_registry_as_json(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.make_vault(root)

            written = subprocess.run(
                [sys.executable, str(TOOLING / "rig_coverage.py"), "--root", str(root), "--write"],
                text=True,
                capture_output=True,
                timeout=10,
            )
            checked = subprocess.run(
                [sys.executable, str(TOOLING / "rig_coverage.py"), "--root", str(root), "--check"],
                text=True,
                capture_output=True,
                timeout=10,
            )

            self.assertEqual(written.returncode, 0, written.stderr)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertEqual(json.loads(checked.stdout)["status"], "valid")

    def test_build_registry_applies_known_overrides_and_rejects_unknown_ids(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.make_vault(root)
            override_path = root / "governance/rig-coverage-overrides.json"
            override_path.parent.mkdir(parents=True)
            override_path.write_text(
                json.dumps({
                    "schema": COVERAGE_SCHEMA,
                    "overrides": [{
                        "id": "doc:reference/20-host-plugins/host.md",
                        "level": "curated",
                        "route": "knowledge/_maps/rig-couverture.md",
                    }],
                }),
                encoding="utf-8",
            )

            registry = build_registry(root)

            self.assertEqual(registry["entries"][0]["level"], "curated")
            override_path.write_text(
                json.dumps({"schema": COVERAGE_SCHEMA, "overrides": [{"id": "doc:missing.md", "level": "curated"}]}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "unknown coverage id"):
                build_registry(root)


if __name__ == "__main__":
    unittest.main()
