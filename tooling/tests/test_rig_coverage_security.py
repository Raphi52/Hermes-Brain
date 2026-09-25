import json
import sys
import tempfile
import unittest
from pathlib import Path

TOOLING = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLING))

from rig_coverage import COVERAGE_SCHEMA, build_registry, validate_registry
import test_rig_coverage as coverage_tests


class RigCoverageSecurityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = coverage_tests.RigCoverageTests()

    def test_manifest_path_cannot_escape_vault(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "vault"
            self.fixture.make_vault(root)
            manifest_path = root / "knowledge/domain/rigapplication-documentation/_SOURCE_MANIFEST.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"][0]["path"] = "../../../../outside.md"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            (Path(td) / "outside.md").write_text("outside", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "escapes vault"):
                build_registry(root)

    def test_registry_route_cannot_escape_vault(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "vault"
            self.fixture.make_vault(root)
            registry = build_registry(root)
            outside = Path(td) / "outside.md"
            outside.write_text("outside", encoding="utf-8")
            registry["entries"][0]["route"] = "../outside.md"

            errors = validate_registry(root, registry)

            self.assertTrue(any("route escapes vault" in error for error in errors), errors)

    def test_manifest_file_count_must_match_entries(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.fixture.make_vault(root)
            manifest_path = root / "knowledge/domain/rigapplication-documentation/_SOURCE_MANIFEST.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["file_count"] = 999
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "file_count"):
                build_registry(root)

    def test_registry_schema_is_enforced(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.fixture.make_vault(root)
            registry = build_registry(root)
            registry["schema"] = "invented"

            errors = validate_registry(root, registry)

            self.assertTrue(any("unsupported registry schema" in error for error in errors), errors)

    def test_non_scalar_level_is_rejected_without_crashing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.fixture.make_vault(root)
            registry = build_registry(root)
            registry["entries"][0]["level"] = ["curated"]

            errors = validate_registry(root, registry)

            self.assertTrue(any("level must be a string" in error for error in errors), errors)

    def test_non_scalar_override_is_rejected_as_value_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.fixture.make_vault(root)
            override = root / "governance/rig-coverage-overrides.json"
            override.parent.mkdir(parents=True)
            override.write_text(
                json.dumps({
                    "schema": COVERAGE_SCHEMA,
                    "overrides": [{
                        "id": "doc:reference/20-host-plugins/host.md",
                        "level": ["curated"],
                    }],
                }),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "override level must be a string"):
                build_registry(root)

    def test_strong_levels_require_explicit_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.fixture.make_vault(root)
            registry = build_registry(root)
            registry["entries"][0]["level"] = "curated"

            errors = validate_registry(root, registry)

            self.assertTrue(any("requires evidence" in error for error in errors), errors)


if __name__ == "__main__":
    unittest.main()
