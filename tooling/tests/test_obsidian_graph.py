from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

TOOLING = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLING))

from obsidian_graph import analyze, graph_errors, refresh_indexes


class ObsidianVisibleGraphTests(unittest.TestCase):
    def make_vault(self, root: Path, *, hide_unresolved: bool = True) -> bytes:
        config = root / ".obsidian/graph.json"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps(
                {
                    "search": "",
                    "showAttachments": False,
                    "hideUnresolved": hide_unresolved,
                    "showOrphans": True,
                    "colorGroups": [
                        {"query": f"tag:#theme/{theme}", "color": {"a": 1, "rgb": index}}
                        for index, theme in enumerate(
                            (
                                "rig", "architecture", "donnees", "integrations", "operations", "ia",
                                "gouvernance", "autowin-os",
                            ),
                            start=1,
                        )
                    ],
                }
            ),
            encoding="utf-8",
        )
        brain = root / "knowledge/_maps/brain.md"
        brain.parent.mkdir(parents=True)
        brain.write_text(
            "# Brain\n[[knowledge/_maps/rig]]\n[[knowledge/_maps/vault-inventory]]\n",
            encoding="utf-8",
        )
        (root / "knowledge/_maps/rig.md").write_text(
            "# RIG\n[[knowledge/_maps/rig-source-mirror]]\n",
            encoding="utf-8",
        )
        enterprise = root / "governance/ARCHITECTURE.md"
        enterprise.parent.mkdir(parents=True)
        enterprise.write_text("# Architecture\n", encoding="utf-8")
        source = root / "knowledge/domain/rigapplication-documentation/source.md"
        source.parent.mkdir(parents=True)
        source_bytes = b"# Canonical source\n"
        source.write_bytes(source_bytes)
        return source_bytes

    def test_refresh_connects_every_visible_markdown_without_rewriting_canonical_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            expected_source = self.make_vault(root)

            self.assertGreater(len(analyze(root).isolates), 0)
            refresh_indexes(root, reviewer="external:judge")

            report = analyze(root)
            self.assertEqual(report.isolates, ())
            self.assertEqual(len(report.components), 1)
            self.assertEqual(graph_errors(root), [])
            self.assertEqual(
                (root / "knowledge/domain/rigapplication-documentation/source.md").read_bytes(),
                expected_source,
            )
            source_index = root / "knowledge/_maps/rig-source-mirror.md"
            self.assertIn("Source documentaire canonique RIG", source_index.read_text(encoding="utf-8"))
            self.assertIn("author_agent: tooling:obsidian-graph", source_index.read_text(encoding="utf-8"))
            self.assertIn("uid: rig/map/source-mirror", source_index.read_text(encoding="utf-8"))
            self.assertEqual(source_index.relative_to(root).as_posix(), "knowledge/_maps/rig-source-mirror.md")

    def test_new_unindexed_note_makes_guard_red(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.make_vault(root)
            refresh_indexes(root, reviewer="external:judge")
            orphan = root / "knowledge/new-note.md"
            orphan.write_text("# New\n[[knowledge/_maps/brain]]\n", encoding="utf-8")

            errors = graph_errors(root)

            self.assertTrue(any("stale generated Obsidian index" in error for error in errors))
            self.assertFalse(any("degree-zero" in error for error in errors))
            self.assertFalse(any("disconnected" in error for error in errors))

    def test_refresh_is_byte_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.make_vault(root)
            refresh_indexes(root, reviewer="external:judge")
            index_paths = (
                Path("knowledge/_maps/vault-inventory.md"),
                Path("knowledge/_maps/rig-source-mirror.md"),
            )
            before = {path: (root / path).read_bytes() for path in index_paths}

            refresh_indexes(root, reviewer="external:judge")

            self.assertEqual(before, {path: (root / path).read_bytes() for path in index_paths})

    def test_unresolved_phantom_nodes_must_be_hidden(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.make_vault(root, hide_unresolved=False)
            refresh_indexes(root, reviewer="external:judge")

            errors = graph_errors(root)

            self.assertTrue(any("unresolved phantom" in error for error in errors))

    def test_graph_requires_one_color_group_per_controlled_theme(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.make_vault(root)
            refresh_indexes(root, reviewer="external:judge")
            config_path = root / ".obsidian/graph.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["colorGroups"] = []
            config_path.write_text(json.dumps(config), encoding="utf-8")

            errors = graph_errors(root)

            self.assertTrue(any("missing controlled theme color groups" in error for error in errors))

    def test_links_inside_asymmetric_fences_do_not_connect_notes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.make_vault(root)
            hidden = root / "knowledge/hidden.md"
            hidden.write_text(
                "# Hidden\n```md\n[[governance/ARCHITECTURE]]\n````\n",
                encoding="utf-8",
            )

            report = analyze(root)

            self.assertIn("knowledge/hidden.md", report.isolates)


if __name__ == "__main__":
    unittest.main()
