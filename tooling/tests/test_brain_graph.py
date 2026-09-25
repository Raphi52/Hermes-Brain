from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

TOOLING = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLING))

from brain_graph import build_graph
from brain_propose import propose_note
from brain_auth import verified_context
from brain_server import Handler


def make_brain(root: Path) -> None:
    notes = root / "knowledge" / "domain"
    notes.mkdir(parents=True)
    (notes / "a.md").write_text("# A\nVoir [[b]].\n", encoding="utf-8")
    (notes / "b.md").write_text("# B\nVoir [C](c.md).\n", encoding="utf-8")
    (notes / "c.md").write_text("# C\n", encoding="utf-8")
    code = root / "projects" / "rig-demo" / "graphify-out"
    code.mkdir(parents=True)
    (code / "graph.json").write_text(json.dumps({
        "nodes": [{"id": "svc", "label": "OrderService"}, {"id": "repo", "label": "OrderRepository"},
                  {"id": "ctl", "label": "OrderController"}],
        "links": [{"source": "ctl", "target": "svc", "relation": "calls"},
                  {"source": "svc", "target": "repo", "relation": "calls"}],
    }), encoding="utf-8")
    (root / "inbox").mkdir()
    (root / "secret.md").write_text("hors corpus", encoding="utf-8")


class FakeHandler:
    token = "t"

    def __init__(self, root):
        self.brain_root = str(root)
        self.sent = None

    def _json(self, status, payload):
        self.sent = (status, payload)


class EntityGraphTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        make_brain(self.root)
        self.graph = build_graph(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_note_links_keep_their_direction(self):
        result = self.graph.query("knowledge/domain/c.md", "dependents", 2)
        self.assertEqual([(r["entity"], r["depth"]) for r in result["results"]],
                         [("knowledge/domain/b.md", 1), ("knowledge/domain/a.md", 2)])
        self.assertEqual(self.graph.query("c", "dependencies")["results"], [])

    def test_code_graph_answers_who_depends_on_by_label(self):
        result = self.graph.query("OrderRepository", "dependents", 2, "calls")
        self.assertTrue(result["found"])
        self.assertEqual([r["label"] for r in result["results"]], ["OrderService", "OrderController"])
        self.assertEqual([r["label"] for r in self.graph.query("OrderController", "dependencies")["results"]],
                         ["OrderService"])

    def test_code_graph_in_current_graphify_edges_format(self):
        code = self.root / "projects" / "rig-neuf" / "graphify-out"
        code.mkdir(parents=True)
        (code / "graph.json").write_text(json.dumps({
            "nodes": [{"id": "hook", "label": "brain_hook.py"}, {"id": "srv", "label": "brain_server.py"}],
            "edges": [{"source": "hook", "target": "srv", "relation": "imports_from"}],
        }), encoding="utf-8")
        result = build_graph(self.root).query("brain_server.py", "dependents")
        self.assertEqual([r["label"] for r in result["results"]], ["brain_hook.py"])

    def test_import_of_a_bare_module_reaches_its_file(self):
        code = self.root / "projects" / "py" / "graphify-out"
        code.mkdir(parents=True)
        (code / "graph.json").write_text(json.dumps({
            "nodes": [{"id": "tooling_srv", "label": "srv.py"}, {"id": "tooling_test", "label": "test.py"},
                      {"id": "a_util", "label": "util.py"}, {"id": "b_util", "label": "util.py"}],
            "edges": [{"source": "tooling_test", "target": "srv", "relation": "imports"},
                      {"source": "tooling_test", "target": "util", "relation": "imports"}],
        }), encoding="utf-8")
        graph = build_graph(self.root)
        self.assertEqual([r["label"] for r in graph.query("srv.py")["results"]], ["test.py"])
        # Deux util.py : l'import reste sur le module nu, jamais attribue au hasard.
        self.assertEqual(graph.query("tooling_test", "dependencies")["results"][1]["entity"], "util")

    def test_unknown_entity_and_bad_direction(self):
        self.assertFalse(self.graph.query("nope")["found"])
        with self.assertRaises(ValueError):
            self.graph.query("a", "sideways")

    def test_graph_route_returns_signed_result(self):
        handler = FakeHandler(self.root)
        Handler._handle_graph(handler, json.dumps({"entity": "OrderService"}).encode())
        status, payload = handler.sent
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(verified_context(payload, "t"))["results"][0]["label"], "OrderController")

    def test_read_route_is_confined_to_knowledge(self):
        handler = FakeHandler(self.root)
        Handler._handle_read(handler, json.dumps({"path": "knowledge/domain/a.md"}).encode())
        self.assertEqual(handler.sent[0], 200)
        self.assertIn("Voir [[b]]", verified_context(handler.sent[1], "t"))
        for escape in ("secret.md", "knowledge/../secret.md", "inbox/x.md", "knowledge/domain/zz.md"):
            Handler._handle_read(handler, json.dumps({"path": escape}).encode())
            self.assertIn(handler.sent[0], {400, 404}, escape)

    def test_routes_stay_under_the_client_context_bound(self):
        big = self.root / "knowledge" / "domain" / "big.md"
        big.write_text("x" * 10_000 + "".join(f"[[n{i}]]" for i in range(300)), encoding="utf-8")
        for i in range(300):
            (big.parent / f"n{i}.md").write_text("# n\n", encoding="utf-8")
        handler = FakeHandler(self.root)
        Handler._handle_read(handler, json.dumps({"path": "knowledge/domain/big.md"}).encode())
        self.assertLessEqual(len(verified_context(handler.sent[1], "t")), 3_000)
        Handler._handle_graph(handler, json.dumps({"entity": "big", "direction": "dependencies"}).encode())
        result = json.loads(verified_context(handler.sent[1], "t"))
        self.assertTrue(result["truncated"])
        self.assertGreater(len(result["results"]), 0)


class SupersedesProposalTests(unittest.TestCase):
    def test_candidate_carries_supersedes_and_rejects_bad_uids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kwargs = dict(title="Nouvelle regle", body="Le port par defaut est 8765.", note_type="decision",
                          scope="autowin-os", author_agent="test", model="test",
                          source="session:test-run", brain_root=root)
            path = propose_note(root / "inbox", supersedes=["autowin-os/old-rule"], **kwargs)
            self.assertIn('supersedes: ["autowin-os/old-rule"]', path.read_text(encoding="utf-8"))
            with self.assertRaises(ValueError):
                propose_note(root / "inbox", supersedes=["../escape"], **kwargs)

    def test_promoted_replacement_passes_brain_validate(self):
        from brain_curate import _frontmatter as curate_frontmatter, _promote
        from brain_validate import ValidationReport, _frontmatter, _list_value, _validate_v1_note
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "knowledge" / "_maps").mkdir(parents=True)
            path = propose_note(root / "inbox", title="Nouvelle regle", body="Le port par defaut est 8765.",
                                note_type="decision", scope="autowin-os", author_agent="claude:test",
                                model="test", source="session:test-run", brain_root=root,
                                supersedes=["autowin-os/decision/old-rule"])
            meta, body = curate_frontmatter(path.read_text(encoding="utf-8"))
            relative = _promote(root, path, meta, body, "Nouvelle regle", reviewer="codex:review")
            text = (root / relative).read_text(encoding="utf-8")
            metadata = _frontmatter(text)
            self.assertEqual(_list_value(metadata["supersedes"]), ["autowin-os/decision/old-rule"])
            report = ValidationReport()
            _validate_v1_note(root / relative, root, text, metadata, report, {})
            self.assertEqual([e for e in report.errors if "supersedes" in e or "missing" in e
                              or "inline list" in e or "uid" in e], [], report.errors)


if __name__ == "__main__":
    unittest.main()
# fix-ok: le test lisait la mauvaise forme de reponse signee ; il lit maintenant verified_context
