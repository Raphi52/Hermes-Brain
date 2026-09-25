import json
import sys
import tempfile
import unittest
from pathlib import Path

TOOLING = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLING))

from brain_eval import evaluate_cases, index_freshness
from brain_index import knowledge_fingerprint, write_index_snapshot


class FakeRetriever:
    def __init__(self, results):
        self.results = results

    def query(self, text, k):
        return {"hits": self.results[text][:k], "axes": 2}


class BrainEvaluationTests(unittest.TestCase):
    def test_flags_duplicate_paths_in_a_top_k(self):
        cases = [{"id": "duplicate", "query": "x", "expected_paths": ["knowledge/a.md"]}]
        retriever = FakeRetriever({
            "x": [
                {"path": "knowledge/a.md", "dense_cos": 0.8},
                {"path": "knowledge/a.md", "dense_cos": 0.7},
            ]
        })
        self.assertEqual(evaluate_cases(retriever, cases, k=5)["duplicate_path_violations"], 1)

    def test_measures_recall_mrr_and_negative_leakage(self):
        cases = [
            {"id": "hit", "query": "architecture", "expected_paths": ["knowledge/right.md"]},
            {"id": "miss", "query": "greffe", "expected_paths": ["knowledge/missing.md"]},
            {"id": "negative", "query": "crêpes", "max_dense": 0.30},
        ]
        retriever = FakeRetriever({
            "architecture": [
                {"path": "knowledge/noise.md", "dense_cos": 0.4},
                {"path": "knowledge/right.md", "dense_cos": 0.7},
            ],
            "greffe": [{"path": "knowledge/noise.md", "dense_cos": 0.2}],
            "crêpes": [{"path": "knowledge/noise.md", "dense_cos": 0.12}],
        })

        report = evaluate_cases(retriever, cases, k=5)

        self.assertEqual(report["positive_cases"], 2)
        self.assertEqual(report["recall_at_k"], 0.5)
        self.assertEqual(report["mrr"], 0.25)
        self.assertEqual(report["negative_pass_rate"], 1.0)

    def test_detects_stale_knowledge_fingerprint(self):
        import numpy as np

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            knowledge = root / "knowledge"
            knowledge.mkdir()
            note = knowledge / "note.md"
            note.write_text("# Version 1\n", encoding="utf-8")
            fingerprint = knowledge_fingerprint([note], relative_to=root)
            snapshot = write_index_snapshot(
                root / "index",
                [{"path": "knowledge/note.md"}],
                ["# Version 1"],
                np.array([[1.0]], dtype=np.float32),
                generation_id="gen-one",
                knowledge_fingerprint=fingerprint,
            )
            manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(index_freshness(manifest, knowledge)["fresh"])

            note.write_text("# Version 2\n", encoding="utf-8")
            self.assertFalse(index_freshness(manifest, knowledge)["fresh"])


if __name__ == "__main__":
    unittest.main()
