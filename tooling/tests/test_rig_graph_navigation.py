from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from datetime import date as real_date
from pathlib import Path
from unittest.mock import patch

TOOLING = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLING))

from rig_graph_navigation import build_navigation


class RigGraphNavigationTests(unittest.TestCase):
    def make_graph(self, root: Path) -> None:
        graph_dir = root / "projects/rig-demo/graphify-out"
        graph_dir.mkdir(parents=True)
        graph = {
            "built_at_commit": "abc123",
            "nodes": [
                {"id": "a-file", "label": "A.cs", "file_type": "code", "source_file": "AreaA/A.cs", "source_location": "L1", "_origin": "ast"},
                {"id": "a-class", "label": "A", "file_type": "code", "source_file": "AreaA/A.cs", "source_location": "L3", "_origin": "ast"},
                {"id": "b-file", "label": "B.cs", "file_type": "code", "source_file": "AreaB/B.cs", "source_location": "L1", "_origin": "ast"},
                {"id": "c-file", "label": "C.cs", "file_type": "code", "source_file": "AreaC/C.cs", "source_location": "L1", "_origin": "ast"},
            ],
            "links": [
                {"source": "a-file", "target": "a-class", "relation": "contains"},
                {"source": "a-class", "target": "b-file", "relation": "calls"},
            ],
        }
        (graph_dir / "graph.json").write_text(json.dumps(graph), encoding="utf-8")

    def test_builds_bounded_project_and_area_maps(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.make_graph(root)

            report = build_navigation(root, reviewer="test-reviewer", max_areas=2, top_symbols=3)

            generated = root / "projects/rig-demo/obsidian"
            self.assertEqual(report["projects"], 1)
            self.assertEqual(report["area_maps"], 2)
            central = root / "knowledge/_maps/rig-code-graphes.md"
            self.assertTrue(central.is_file())
            central_text = central.read_text(encoding="utf-8")
            self.assertIn('reviewed_by: ["test-reviewer"]', central_text)
            self.assertIn('mocs: []', central_text)
            self.assertIn("Cette carte rend 1 snapshots Graphify", central_text)
            self.assertIn("relations structurelles", central_text)
            self.assertTrue((generated / "rig-demo.md").is_file())
            self.assertEqual(
                sorted(path.name for path in (generated / "areas").glob("*.md")),
                ["areaa.md", "areab.md"],
            )
            project = (generated / "rig-demo.md").read_text(encoding="utf-8")
            self.assertIn("4 nœuds", project)
            self.assertIn("2 arêtes", project)
            self.assertIn("Commit déclaré : abc123", project)
            self.assertIn("calls=1, contains=1", project)
            self.assertIn("[[projects/rig-demo/obsidian/areas/areaa|AreaA]]", project)
            self.assertNotIn("AreaC]]", project)

    def test_builds_traced_relation_maps_and_cross_links_selected_areas(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.make_graph(root)
            graph_path = root / "projects/rig-demo/graphify-out/graph.json"
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
            graph["links"].append({"source": "a-class", "target": "external", "relation": "calls"})
            graph["links"].append({"source": "a-class", "target": "b-file", "relation": "calls"})
            graph_path.write_text(json.dumps(graph), encoding="utf-8")

            report = build_navigation(root, reviewer="test-reviewer", max_areas=2, top_symbols=3)

            generated = root / "projects/rig-demo/obsidian"
            relation = generated / "relations/calls.md"
            self.assertEqual(report["relation_maps"], 1)
            self.assertTrue(relation.is_file())
            relation_text = relation.read_text(encoding="utf-8")
            self.assertIn("Relation Graphify explicite : `calls`", relation_text)
            self.assertIn("1 arête résolue unique sur 3 arêtes brutes", relation_text)
            self.assertIn("| A | AreaA/A.cs | B.cs | AreaB/B.cs |", relation_text)
            self.assertIn("[[projects/rig-demo/obsidian/areas/areaa|AreaA]]", relation_text)
            self.assertIn("[[projects/rig-demo/obsidian/areas/areab|AreaB]]", relation_text)
            self.assertIn(
                "[[projects/rig-demo/obsidian/relations/calls|calls]]",
                (generated / "rig-demo.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "[[projects/rig-demo/obsidian/relations/calls|calls]]",
                (generated / "areas/areaa.md").read_text(encoding="utf-8"),
            )
            self.assertFalse((generated / "relations/contains.md").exists())

    def test_graph_text_cannot_inject_markdown_structure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.make_graph(root)
            graph_path = root / "projects/rig-demo/graphify-out/graph.json"
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
            graph["nodes"][1]["label"] = "A` |\n## inject [[x]]"
            graph["nodes"][1]["source_location"] = "L3|\n# location"
            graph["built_at_commit"] = "abc`\n## commit"
            graph["links"].append({"source": "a-class", "target": "b-file", "relation": "bad|\n## relation [[z]]"})
            graph_path.write_text(json.dumps(graph), encoding="utf-8")

            build_navigation(root, reviewer="test-reviewer", max_areas=2, top_symbols=3)

            relation = (root / "projects/rig-demo/obsidian/relations/calls.md").read_text(encoding="utf-8")
            project = (root / "projects/rig-demo/obsidian/rig-demo.md").read_text(encoding="utf-8")
            self.assertNotIn("\n## inject", relation)
            self.assertNotIn("[[x]]", relation)
            self.assertIn(r"A\` \| ## inject \[\[x\]\]", relation)
            self.assertNotIn("\n## commit", project)
            self.assertIn(r"abc\` ## commit", project)
            self.assertNotIn("\n## relation", project)
            self.assertNotIn("[[z]]", project)
            self.assertIn(r"bad\| ## relation \[\[z\]\]=1", project)

    def test_all_unresolved_relation_is_still_reported_without_invented_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.make_graph(root)
            graph_path = root / "projects/rig-demo/graphify-out/graph.json"
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
            graph["links"] = [
                {"source": "a-file", "target": "a-class", "relation": "contains"},
                {"source": "a-class", "target": "external", "relation": "calls"},
            ]
            graph_path.write_text(json.dumps(graph), encoding="utf-8")

            report = build_navigation(root, reviewer="test-reviewer", max_areas=2, top_symbols=3)

            relation = root / "projects/rig-demo/obsidian/relations/calls.md"
            self.assertEqual(report["relation_maps"], 1)
            self.assertTrue(relation.is_file())
            text = relation.read_text(encoding="utf-8")
            self.assertIn("0 arêtes résolues uniques sur 1 arête brute", text)
            self.assertIn("Aucune arête à deux extrémités résolues", text)

    def test_relation_maps_do_not_claim_ast_origin_for_arbitrary_graph_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.make_graph(root)
            graph_path = root / "projects/rig-demo/graphify-out/graph.json"
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
            graph["nodes"][1]["_origin"] = "manual"
            graph_path.write_text(json.dumps(graph), encoding="utf-8")

            build_navigation(root, reviewer="test-reviewer", max_areas=2, top_symbols=3)

            relation = (root / "projects/rig-demo/obsidian/relations/calls.md").read_text(encoding="utf-8")
            self.assertNotIn("arêtes AST", relation)
            self.assertIn("arêtes Graphify tracées", relation)

    def test_disambiguates_colliding_area_slugs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            graph_dir = root / "projects/rig-demo/graphify-out"
            graph_dir.mkdir(parents=True)
            graph = {
                "nodes": [
                    {"id": "plus", "label": "Plus", "source_file": "A+B/Plus.cs"},
                    {"id": "space", "label": "Space", "source_file": "A B/Space.cs"},
                ],
                "links": [],
            }
            (graph_dir / "graph.json").write_text(json.dumps(graph), encoding="utf-8")

            report = build_navigation(root, reviewer="test-reviewer", max_areas=2, top_symbols=2)

            areas = sorted((root / "projects/rig-demo/obsidian/areas").glob("*.md"))
            self.assertEqual(report["area_maps"], 2)
            self.assertEqual(len(areas), 2)
            self.assertEqual(len({path.name for path in areas}), 2)

    def test_output_date_is_stable_across_regeneration_days(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.make_graph(root)
            with patch("rig_graph_navigation.date") as mocked_date:
                mocked_date.today.return_value = real_date(2026, 1, 1)
                build_navigation(root, reviewer="test-reviewer", max_areas=2, top_symbols=3)
                first = {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*.md")
                }
                mocked_date.today.return_value = real_date(2026, 1, 2)
                build_navigation(root, reviewer="test-reviewer", max_areas=2, top_symbols=3)
                second = {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*.md")
                }

            self.assertEqual(first, second)
            self.assertIn(b"created: 2026-01-01", first["knowledge/_maps/rig-code-graphes.md"])

    def test_output_is_stable_when_equal_key_nodes_are_reordered(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            graph_dir = root / "projects/rig-demo/graphify-out"
            graph_dir.mkdir(parents=True)
            graph = {
                "nodes": [
                    {"id": "upper", "label": "Same", "source_file": "A B/Same.cs"},
                    {"id": "lower", "label": "same", "source_file": "a b/same.cs"},
                ],
                "links": [
                    {"source": "upper", "target": "lower", "relation": "contains"},
                    {"source": "lower", "target": "upper", "relation": "calls"},
                ],
            }
            graph_path = graph_dir / "graph.json"
            graph_path.write_text(json.dumps(graph), encoding="utf-8")
            build_navigation(root, reviewer="test-reviewer", max_areas=2, top_symbols=2)
            first = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*.md")
            }

            graph["nodes"].reverse()
            graph["links"].reverse()
            graph_path.write_text(json.dumps(graph), encoding="utf-8")
            build_navigation(root, reviewer="test-reviewer", max_areas=2, top_symbols=2)
            second = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*.md")
            }

            self.assertEqual(first, second)

    def test_rejects_links_with_missing_endpoint_ids(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.make_graph(root)
            graph_path = root / "projects/rig-demo/graphify-out/graph.json"
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
            graph["links"].append({"source": "a-class", "relation": "calls"})
            graph_path.write_text(json.dumps(graph), encoding="utf-8")

            with self.assertRaises(ValueError):
                build_navigation(root, reviewer="test-reviewer", max_areas=2, top_symbols=3)

    def test_stale_owned_relation_map_is_deleted_when_relation_disappears(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.make_graph(root)
            build_navigation(root, reviewer="test-reviewer", max_areas=2, top_symbols=3)
            relation = root / "projects/rig-demo/obsidian/relations/calls.md"
            self.assertTrue(relation.is_file())
            graph_path = root / "projects/rig-demo/graphify-out/graph.json"
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
            graph["links"] = [link for link in graph["links"] if link.get("relation") != "calls"]
            graph_path.write_text(json.dumps(graph), encoding="utf-8")

            build_navigation(root, reviewer="test-reviewer", max_areas=2, top_symbols=3)

            self.assertFalse(relation.exists())

    def test_rejects_duplicate_or_empty_node_ids(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.make_graph(root)
            graph_path = root / "projects/rig-demo/graphify-out/graph.json"
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
            graph["nodes"].append({"id": "a-class", "label": "duplicate"})
            graph_path.write_text(json.dumps(graph), encoding="utf-8")

            with self.assertRaises(ValueError):
                build_navigation(root, reviewer="test-reviewer", max_areas=2, top_symbols=3)

    def test_forged_v2_marker_with_wrong_uid_cannot_authorize_relation_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.make_graph(root)
            relation = root / "projects/rig-demo/obsidian/relations/calls.md"
            relation.parent.mkdir(parents=True)
            relation.write_text(
                '---\nschema: amitel-brain/v1\nuid: "rig/manual"\nauthor_agent: hermes\ngenerated_by: rig-graph-navigation/v2\n---\n# humaine\n',
                encoding="utf-8",
            )
            manifest = root / "knowledge/_generated/rig-graph-navigation-manifest.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps({"files": {relation.relative_to(root).as_posix(): hashlib.sha256(relation.read_bytes()).hexdigest()}}),
                encoding="utf-8",
            )

            build_navigation(root, reviewer="test-reviewer", max_areas=2, top_symbols=3)

            self.assertIn("# humaine", relation.read_text(encoding="utf-8"))

    def test_rejects_any_symlink_component_even_inside_vault(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.make_graph(root)
            original_is_symlink = Path.is_symlink

            def fake_is_symlink(path: Path) -> bool:
                return path.name == "obsidian" or original_is_symlink(path)

            with patch.object(Path, "is_symlink", fake_is_symlink):
                with self.assertRaises(RuntimeError):
                    build_navigation(root, reviewer="test-reviewer", max_areas=2, top_symbols=3)

    def test_rejects_non_positive_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.make_graph(root)
            with self.assertRaises(ValueError):
                build_navigation(root, reviewer="test-reviewer", max_areas=0, top_symbols=3)
            with self.assertRaises(ValueError):
                build_navigation(root, reviewer="test-reviewer", max_areas=2, top_symbols=0)
            with self.assertRaises(ValueError):
                build_navigation(root, reviewer='bad\nreviewer', max_areas=2, top_symbols=3)

    def test_tampered_manifest_cannot_delete_manual_note(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.make_graph(root)
            build_navigation(root, reviewer="test-reviewer", max_areas=2, top_symbols=3)
            manual = root / "knowledge/manual.md"
            manual.parent.mkdir(parents=True, exist_ok=True)
            manual.write_text("# humaine\n", encoding="utf-8")
            manifest = root / "knowledge/_generated/rig-graph-navigation-manifest.json"
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["files"]["knowledge/manual.md"] = "forged"
            manifest.write_text(json.dumps(data), encoding="utf-8")

            build_navigation(root, reviewer="test-reviewer", max_areas=2, top_symbols=3)

            self.assertEqual(manual.read_text(encoding="utf-8"), "# humaine\n")

    def test_forged_allowed_manifest_entry_cannot_delete_manual_note(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.make_graph(root)
            build_navigation(root, reviewer="test-reviewer", max_areas=2, top_symbols=3)
            manual = root / "projects/rig-demo/obsidian/areas/manual.md"
            manual.write_text("# humaine\n", encoding="utf-8")
            manifest = root / "knowledge/_generated/rig-graph-navigation-manifest.json"
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["files"]["projects/rig-demo/obsidian/areas/manual.md"] = hashlib.sha256(
                manual.read_bytes()
            ).hexdigest()
            manifest.write_text(json.dumps(data), encoding="utf-8")

            build_navigation(root, reviewer="test-reviewer", max_areas=2, top_symbols=3)

            self.assertEqual(manual.read_text(encoding="utf-8"), "# humaine\n")

    def test_modified_active_relation_map_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.make_graph(root)
            build_navigation(root, reviewer="test-reviewer", max_areas=2, top_symbols=3)
            relation = root / "projects/rig-demo/obsidian/relations/calls.md"
            relation.write_text(relation.read_text(encoding="utf-8") + "\n# reprise humaine\n", encoding="utf-8")

            build_navigation(root, reviewer="test-reviewer", max_areas=2, top_symbols=3)

            self.assertIn("# reprise humaine", relation.read_text(encoding="utf-8"))

    def test_modified_generated_note_is_not_deleted_as_stale(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.make_graph(root)
            build_navigation(root, reviewer="test-reviewer", max_areas=3, top_symbols=3)
            stale = root / "projects/rig-demo/obsidian/areas/areac.md"
            stale.write_text("# reprise humaine\n", encoding="utf-8")

            build_navigation(root, reviewer="test-reviewer", max_areas=2, top_symbols=3)

            self.assertEqual(stale.read_text(encoding="utf-8"), "# reprise humaine\n")

    def test_refresh_removes_only_previously_generated_maps(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.make_graph(root)
            build_navigation(root, reviewer="test-reviewer", max_areas=3, top_symbols=3)
            generated = root / "projects/rig-demo/obsidian"
            manual = generated / "manual.md"
            manual.write_bytes(b"# note humaine\r\n")
            stale = generated / "areas/areac.md"
            self.assertTrue(stale.is_file())

            build_navigation(root, reviewer="test-reviewer", max_areas=2, top_symbols=3)

            self.assertFalse(stale.exists())
            self.assertEqual(manual.read_bytes(), b"# note humaine\r\n")


    def test_legacy_manifest_without_hashes_is_rejected_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.make_graph(root)
            build_navigation(root, reviewer="test-reviewer", max_areas=3, top_symbols=3)
            manifest = root / "knowledge/_generated/rig-graph-navigation-manifest.json"
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["files"] = list(data["files"])
            legacy_text = json.dumps(data)
            manifest.write_text(legacy_text, encoding="utf-8")
            stale = root / "projects/rig-demo/obsidian/areas/areac.md"

            with self.assertRaises(RuntimeError):
                build_navigation(root, reviewer="test-reviewer", max_areas=2, top_symbols=3)

            self.assertTrue(stale.is_file())
            self.assertEqual(manifest.read_text(encoding="utf-8"), legacy_text)


if __name__ == "__main__":
    unittest.main()
