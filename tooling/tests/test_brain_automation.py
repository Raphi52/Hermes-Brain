import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

TOOLING = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLING))

from brain_context import render_hits, retrieve_context
from brain_propose import propose_note
from brain_retrieval import BrainRetriever
from brain_server import Handler, build_context
from brain_hook import hook_output, _server_python, _validate_response
import brain_auth
import brain_hook
import brain_index
import brain_server
import codex_trust_hook
from brain_auth import signed_context_payload
from brain_index import write_index_snapshot


class BrainIndexCliTests(unittest.TestCase):
    def test_relative_knowledge_argument_stores_absolute_note_paths(self):
        import contextlib
        import io
        import json
        import types

        class FakeEmbedding:
            def __init__(self, model_name):
                self.model_name = model_name

            def embed(self, bodies):
                return [[1.0, 0.0] for _ in bodies]

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            note = root / "knowledge" / "note.md"
            note.parent.mkdir()
            note.write_text("---\ntype: decision\n---\nContenu durable.\n", encoding="utf-8")
            previous = Path.cwd()
            try:
                os.chdir(root)
                with (
                    patch.object(sys, "argv", ["brain_index.py", "--knowledge", "knowledge", "--out", "index"]),
                    patch.dict(sys.modules, {"fastembed": types.SimpleNamespace(TextEmbedding=FakeEmbedding)}),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    brain_index.main()
            finally:
                os.chdir(previous)

            generation = (root / "index" / "CURRENT").read_text(encoding="ascii").strip()
            meta_line = (root / "index" / "generations" / generation / "meta.jsonl").read_text(encoding="utf-8")
            stored = Path(json.loads(meta_line)["path"])
            self.assertTrue(stored.is_absolute())
            self.assertEqual(stored.resolve(), note.resolve())


class RenderHitsTests(unittest.TestCase):
    def test_renders_ranked_note_with_provenance_within_budget(self):
        with tempfile.TemporaryDirectory() as td:
            note = Path(td) / "decision.md"
            note.write_text("# Décision\n\nUtiliser le protocole interne Alpha.\n", encoding="utf-8")
            hits = [{
                "rank": 1,
                "path": str(note),
                "type": "decision",
                "scope": "global",
                "author_agent": "human",
                "model": "",
                "created": "2026-07-15",
                "dense_cos": 0.72,
            }]

            rendered = render_hits(hits, max_chars=400)

            self.assertLessEqual(len(rendered), 400)
            self.assertIn("Utiliser le protocole interne Alpha", rendered)
            self.assertIn("decision | global | human | 2026-07-15", rendered)
            self.assertIn(str(note).replace("\\", "/"), rendered)

    def test_skips_indexed_paths_outside_allowed_knowledge_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            knowledge = root / "knowledge"
            knowledge.mkdir()
            safe = knowledge / "safe.md"
            safe.write_text("# Safe\n\nContexte autorisé", encoding="utf-8")
            outside = root / "outside.txt"
            outside.write_text("NE_DOIT_JAMAIS_ETRE_INJECTE", encoding="utf-8")
            hits = [
                {"rank": 1, "path": str(outside)},
                {"rank": 2, "path": str(safe)},
            ]

            rendered = render_hits(hits, max_chars=500, allowed_root=knowledge)

            self.assertIn("Contexte autorisé", rendered)
            self.assertNotIn("NE_DOIT_JAMAIS_ETRE_INJECTE", rendered)

    def test_reads_only_a_bounded_prefix_of_each_note(self):
        with tempfile.TemporaryDirectory() as td:
            note = Path(td) / "large.md"
            note.write_text("# Début\n\n" + "x" * 100_000, encoding="utf-8")
            hits = [{"rank": 1, "path": str(note)}]

            with patch.object(Path, "read_text", side_effect=AssertionError("unbounded read_text used")):
                rendered = render_hits(hits, max_chars=200)

            self.assertLessEqual(len(rendered), 200)
            self.assertIn("# Début", rendered)

    def test_confines_the_descriptor_actually_opened(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            knowledge = root / "knowledge"
            knowledge.mkdir()
            indexed = knowledge / "safe.md"
            indexed.write_text("CONTENU_AUTORISE", encoding="utf-8")
            outside = root / "outside.md"
            outside.write_text("SECRET_EXTERIEUR", encoding="utf-8")
            real_open = os.open

            def swapped_open(_path, flags, *args, **kwargs):
                return real_open(outside, flags, *args, **kwargs)

            with patch("brain_context.os.open", side_effect=swapped_open):
                rendered = render_hits(
                    [{"rank": 1, "path": str(indexed)}],
                    max_chars=300,
                    allowed_root=knowledge,
                )

            self.assertNotIn("CONTENU_AUTORISE", rendered)
            self.assertNotIn("SECRET_EXTERIEUR", rendered)


class RetrieveContextTests(unittest.TestCase):
    def test_runs_query_script_and_renders_returned_note(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            note = root / "lesson.md"
            note.write_text("# Leçon\n\nToujours valider le schéma avant import.\n", encoding="utf-8")
            fake_query = root / "fake_query.py"
            fake_query.write_text(
                "import json, sys\n"
                f"print(json.dumps({{'hits': [{{'rank': 1, 'path': {str(note)!r}, 'type': 'lesson', 'scope': 'global', 'author_agent': 'human', 'created': '2026-07-15'}}], 'axes': 2}}))\n",
                encoding="utf-8",
            )

            rendered = retrieve_context(
                "comment importer ?",
                index_dir=root / "index",
                query_script=fake_query,
                python_exe=sys.executable,
                max_chars=500,
            )

            self.assertIn("Toujours valider le schéma", rendered)
            self.assertIn("lesson | global | human | 2026-07-15", rendered)

    def test_filters_hits_below_dense_relevance_threshold(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            relevant = root / "relevant.md"
            irrelevant = root / "irrelevant.md"
            relevant.write_text("# Pertinent\n\nDéploiement SMB Amitel", encoding="utf-8")
            irrelevant.write_text("# Bruit\n\nPingouins", encoding="utf-8")
            fake_query = root / "fake_query.py"
            hits = [
                {"rank": 1, "path": str(relevant), "dense_cos": 0.57},
                {"rank": 2, "path": str(irrelevant), "dense_cos": 0.07},
            ]
            fake_query.write_text(
                "import json\nprint(json.dumps(" + repr({"hits": hits, "axes": 2}) + "))\n",
                encoding="utf-8",
            )

            rendered = retrieve_context(
                "déployer Amitel",
                root / "index",
                fake_query,
                sys.executable,
                500,
                min_dense=0.25,
            )

            self.assertIn("Déploiement SMB Amitel", rendered)
            self.assertNotIn("Pingouins", rendered)


class BrainRetrieverTests(unittest.TestCase):
    def test_reuses_embedder_and_ranks_by_dense_similarity(self):
        import json
        import numpy as np

        class FakeEmbedder:
            def __init__(self):
                self.calls = 0

            def embed(self, texts):
                self.calls += 1
                return iter([np.array([1.0, 0.0], dtype=np.float32)])

        with tempfile.TemporaryDirectory() as td:
            index = Path(td)
            meta = [
                {"path": "knowledge/alpha.md", "type": "decision"},
                {"path": "knowledge/beta.md", "type": "lesson"},
            ]
            (index / "meta.jsonl").write_text("\n".join(json.dumps(item) for item in meta), encoding="utf-8")
            (index / "bodies.jsonl").write_text('"alpha"\n"beta"\n', encoding="utf-8")
            np.save(index / "emb.npy", np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32))
            fake = FakeEmbedder()
            retriever = BrainRetriever(index, embedder=fake, enable_bm25=False)

            first = retriever.query("alpha", k=2)
            second = retriever.query("alpha encore", k=1)

            self.assertEqual(first["hits"][0]["path"], "knowledge/alpha.md")
            self.assertEqual(first["hits"][0]["dense_cos"], 1.0)
            self.assertEqual(second["hits"][0]["path"], "knowledge/alpha.md")
            self.assertEqual(fake.calls, 2)
            with self.assertRaisesRegex(ValueError, "k"):
                retriever.query("alpha", k=-1)

    def test_keeps_last_good_snapshot_during_partial_reindex(self):
        import json
        import numpy as np

        class FakeEmbedder:
            def embed(self, _texts):
                return iter([np.array([1.0, 0.0], dtype=np.float32)])

        with tempfile.TemporaryDirectory() as td:
            index = Path(td)
            original_meta = [
                {"path": "knowledge/alpha.md"},
                {"path": "knowledge/beta.md"},
            ]
            (index / "meta.jsonl").write_text("\n".join(json.dumps(item) for item in original_meta), encoding="utf-8")
            (index / "bodies.jsonl").write_text('"alpha"\n"beta"\n', encoding="utf-8")
            np.save(index / "emb.npy", np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32))
            retriever = BrainRetriever(index, embedder=FakeEmbedder(), enable_bm25=False)

            partial_meta = original_meta + [{"path": "knowledge/partial.md"}]
            (index / "meta.jsonl").write_text("\n".join(json.dumps(item) for item in partial_meta), encoding="utf-8")

            result = retriever.query("alpha", k=1)

            self.assertEqual(result["hits"][0]["path"], "knowledge/alpha.md")
            self.assertEqual(len(retriever.meta), 2)

    def test_uses_only_complete_manifested_generations(self):
        import numpy as np

        class FakeEmbedder:
            def embed(self, _texts):
                return iter([np.array([1.0, 0.0], dtype=np.float32)])

        with tempfile.TemporaryDirectory() as td:
            index = Path(td)
            published = write_index_snapshot(
                index, [{"path": "knowledge/old.md"}], ["old body"],
                np.array([[1.0, 0.0]], dtype=np.float32), generation_id="gen-old",
            )
            retriever = BrainRetriever(index, embedder=FakeEmbedder(), enable_bm25=False)
            self.assertEqual(retriever.query("x", k=1)["hits"][0]["path"], "knowledge/old.md")

            (published / "meta.jsonl").write_text('{"path":"knowledge/forged.md"}\n', encoding="utf-8")
            self.assertEqual(retriever.query("x", k=1)["hits"][0]["path"], "knowledge/old.md")

            write_index_snapshot(
                index, [{"path": "knowledge/new.md"}], ["new body"],
                np.array([[1.0, 0.0]], dtype=np.float32), generation_id="gen-new",
            )
            self.assertEqual(retriever.query("x", k=1)["hits"][0]["path"], "knowledge/new.md")
            self.assertEqual((index / "CURRENT").read_text(encoding="ascii").strip(), "gen-new")


class BrainServerTests(unittest.TestCase):
    def test_loopback_server_refuses_a_second_owner_of_the_same_port(self):
        first = brain_server.LocalThreadingHTTPServer(("127.0.0.1", 0), Handler)
        try:
            with self.assertRaises(OSError):
                second = brain_server.LocalThreadingHTTPServer(first.server_address, Handler)
                second.server_close()
        finally:
            first.server_close()

    def test_reserves_loopback_port_before_loading_embedding_model(self):
        events = []

        class FakeServer:
            def __init__(self, address, handler):
                events.append(("bind", address, handler))

            def serve_forever(self):
                events.append(("serve",))

            def server_close(self):
                events.append(("close",))

        def fake_retriever(index):
            events.append(("retriever", Path(index)))
            return object()

        def fake_token():
            events.append(("token",))
            return "token"

        with tempfile.TemporaryDirectory() as td:
            with (
                patch.dict(os.environ, {"AMITEL_BRAIN_ROOT": td, "AMITEL_BRAIN_PORT": "18765"}),
                patch.object(brain_server, "LocalThreadingHTTPServer", FakeServer),
                patch.object(brain_server, "BrainRetriever", side_effect=fake_retriever),
                patch.object(brain_server, "service_token", side_effect=fake_token),
            ):
                brain_server.main()

        self.assertEqual(
            [event[0] for event in events],
            ["bind", "token", "retriever", "serve", "close"],
        )

    def test_health_identifies_root_and_shutdown_requires_local_token(self):
        import json
        import threading
        from http.server import ThreadingHTTPServer
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen

        token = "service-test-token"
        Handler.token = token
        Handler.root_id = "C:/brain/current"
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        headers = {"Authorization": f"Bearer {token}"}
        try:
            with urlopen(Request(base + "/health", headers=headers), timeout=2) as response:
                health = json.loads(response.read())
            self.assertEqual(_validate_response(health, token), Handler.root_id)

            unauthenticated = Request(base + "/shutdown", data=b"{}", method="POST")
            with self.assertRaises(HTTPError) as denied:
                urlopen(unauthenticated, timeout=2)
            self.assertEqual(denied.exception.code, 403)
            self.assertTrue(thread.is_alive())

            shutdown = Request(base + "/shutdown", data=b"{}", headers=headers, method="POST")
            with urlopen(shutdown, timeout=2) as response:
                stopped = json.loads(response.read())
            self.assertEqual(_validate_response(stopped, token), Handler.root_id)
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        finally:
            server.shutdown()
            server.server_close()

    def test_build_context_filters_confines_and_marks_reference_data(self):
        class FakeRetriever:
            def query(self, text, k):
                return {"hits": hits, "axes": 2}

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            knowledge = root / "knowledge"
            knowledge.mkdir()
            relevant = knowledge / "relevant.md"
            noisy = knowledge / "noisy.md"
            outside = root / "outside.md"
            relevant.write_text("# Pertinent\n\nDécision Amitel", encoding="utf-8")
            noisy.write_text("# Bruit\n\nPingouins", encoding="utf-8")
            outside.write_text("INSTRUCTION_MALVEILLANTE", encoding="utf-8")
            hits = [
                {"rank": 1, "path": str(relevant), "dense_cos": 0.57},
                {"rank": 2, "path": str(noisy), "dense_cos": 0.07},
                {"rank": 3, "path": str(outside), "dense_cos": 0.99},
            ]

            context = build_context(FakeRetriever(), "Amitel", knowledge, max_chars=600, min_dense=0.25)

            self.assertLessEqual(len(context), 600)
            self.assertIn("REFERENCE DATA", context)
            self.assertIn("Décision Amitel", context)
            self.assertNotIn("Pingouins", context)
            self.assertNotIn("INSTRUCTION_MALVEILLANTE", context)


class BrainAuthTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows ACL contract")
    def test_uses_fixed_localappdata_token_and_restricts_existing_file(self):
        previous_token = os.environ.pop("AMITEL_BRAIN_TOKEN", None)
        try:
            with tempfile.TemporaryDirectory() as td, patch.dict(
                os.environ,
                {"LOCALAPPDATA": td, "AMITEL_BRAIN_TOKEN_FILE": str(Path(td) / "attacker-token")},
                clear=False,
            ):
                brain_auth._TOKEN_CACHE = None
                brain_auth._TOKEN_CACHE_PATH = None
                first = brain_auth.service_token()
                expected = Path(td) / "AmitelBrain" / "service-token"
                self.assertTrue(expected.is_file())
                self.assertFalse((Path(td) / "attacker-token").exists())

                brain_auth._TOKEN_CACHE = None
                brain_auth._TOKEN_CACHE_PATH = None
                second = brain_auth.service_token()
                self.assertEqual(first, second)
        finally:
            brain_auth._TOKEN_CACHE = None
            brain_auth._TOKEN_CACHE_PATH = None
            if previous_token is not None:
                os.environ["AMITEL_BRAIN_TOKEN"] = previous_token


class BrainHookTests(unittest.TestCase):
    def test_restarts_service_when_configured_root_changes(self):
        configured = str((Path.cwd() / "new-brain").resolve())
        with (
            patch.dict(os.environ, {"AMITEL_BRAIN_ROOT": configured}),
            patch.object(brain_hook, "_request_health", side_effect=["C:/old-brain", configured]),
            patch.object(brain_hook, "_shutdown_server") as shutdown,
            patch.object(brain_hook, "_wait_for_shutdown") as wait_for_shutdown,
            patch.object(brain_hook, "_spawn_server") as spawn,
            patch.object(brain_hook, "_request_context", return_value="REFERENCE") as request_context,
        ):
            self.assertEqual(brain_hook.query_service("question"), "REFERENCE")
        shutdown.assert_called_once_with()
        wait_for_shutdown.assert_called_once_with()
        spawn.assert_called_once_with()
        request_context.assert_called_once_with("question")

    def test_emits_official_user_prompt_submit_context_shape(self):
        output = hook_output(
            {"hook_event_name": "UserPromptSubmit", "prompt": "déployer Amitel"},
            query_fn=lambda prompt: "CONTEXTE:" + prompt,
        )

        self.assertEqual(output, {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "CONTEXTE:déployer Amitel",
            }
        })

    def test_emits_nothing_when_retrieval_has_no_relevant_context(self):
        self.assertIsNone(hook_output({"prompt": "blague"}, query_fn=lambda _prompt: ""))

    def test_server_python_prefers_explicit_runtime(self):
        configured = "C:/team/amitel-python.exe"
        with patch.dict(os.environ, {"AMITEL_BRAIN_PYTHON": configured}):
            self.assertEqual(_server_python(), configured)

    def test_accepts_only_authenticated_service_responses(self):
        token = "local-test-token"
        signed = signed_context_payload("REFERENCE CONTEXT", token)
        self.assertEqual(_validate_response(signed, token), "REFERENCE CONTEXT")

        forged = {"service": "amitel-brain", "protocol": 1, "context": "EVIL", "signature": "0" * 64}
        with self.assertRaisesRegex(ValueError, "auth"):
            _validate_response(forged, token)


class HermesPluginTests(unittest.TestCase):
    @staticmethod
    def _load_plugin():
        path = TOOLING.parent / "integrations" / "hermes-amitel-brain" / "__init__.py"
        spec = importlib.util.spec_from_file_location("amitel_brain_test_plugin", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_pre_llm_call_returns_ephemeral_context_dict(self):
        plugin = self._load_plugin()
        with patch.object(plugin, "_query_context", return_value="REFERENCE CONTEXT"):
            result = plugin._pre_llm_call(user_message="déployer Amitel")
        self.assertEqual(result, {"context": "REFERENCE CONTEXT"})

    def test_pre_llm_call_fails_open(self):
        plugin = self._load_plugin()
        with patch.object(plugin, "_query_context", side_effect=RuntimeError("offline")):
            self.assertIsNone(plugin._pre_llm_call(user_message="question"))

    def test_loads_local_hook_when_shared_brain_contains_hostile_code(self):
        plugin_path = TOOLING.parent / "integrations" / "hermes-amitel-brain" / "__init__.py"
        with tempfile.TemporaryDirectory() as td:
            shared = Path(td) / "shared"
            (shared / "tooling").mkdir(parents=True)
            (shared / "tooling" / "brain_hook.py").write_text(
                "raise RuntimeError('SHARED_CODE_EXECUTED')\n", encoding="utf-8"
            )
            code = (
                "import importlib.util\n"
                f"p = {str(plugin_path)!r}\n"
                "s = importlib.util.spec_from_file_location('isolated_amitel_plugin', p)\n"
                "m = importlib.util.module_from_spec(s)\n"
                "s.loader.exec_module(m)\n"
                "print(m._load_hook_module().__file__)\n"
            )
            env = os.environ.copy()
            env.pop("PYTHONPATH", None)
            env["AMITEL_BRAIN_ROOT"] = str(shared)
            env["AMITEL_BRAIN_CODE_ROOT"] = str(TOOLING)
            result = subprocess.run(
                [sys.executable, "-c", code], cwd=td, env=env,
                text=True, capture_output=True, timeout=10,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()).resolve(), (TOOLING / "brain_hook.py").resolve())


class ProposeNoteTests(unittest.TestCase):
    def test_creates_unique_candidate_with_provenance(self):
        with tempfile.TemporaryDirectory() as td:
            inbox = Path(td) / "inbox"

            created = propose_note(
                inbox,
                title="Validation du schéma import",
                body="Toujours valider le schéma avant l'import.",
                note_type="lesson",
                scope="amitel-import",
                author_agent="hermes",
                model="test-model",
                source="session:test-session-123",
                tags=["import", "schema"],
            )

            text = created.read_text(encoding="utf-8")
            self.assertEqual(created.parent.resolve(), inbox.resolve())
            self.assertIn("status: candidate", text)
            self.assertIn('author_agent: "hermes"', text)
            self.assertIn('source: "session:test-session-123"', text)
            self.assertIn("Toujours valider le schéma", text)

    def test_rejects_candidate_containing_likely_secret(self):
        with tempfile.TemporaryDirectory() as td:
            inbox = Path(td) / "inbox"

            with self.assertRaisesRegex(ValueError, "secret"):
                propose_note(
                    inbox,
                    title="Configuration",
                    body="API_KEY=sk-live-super-secret-value",
                    note_type="lesson",
                    scope="global",
                    author_agent="hermes",
                    model="test-model",
                    source="session:test-session-123",
                )

            self.assertFalse(inbox.exists())

    def test_rejects_raw_github_tokens(self):
        with tempfile.TemporaryDirectory() as td:
            inbox = Path(td) / "inbox"
            github_token = "ghp_" + "A" * 36

            with self.assertRaisesRegex(ValueError, "secret"):
                propose_note(
                    inbox,
                    title="Configuration GitHub",
                    body="Token observé: " + github_token,
                    note_type="lesson",
                    scope="global",
                    author_agent="hermes",
                    model="test-model",
                    source="session:test-session-123",
                )

            self.assertFalse(inbox.exists())

    def test_rejects_inbox_nested_under_knowledge(self):
        with tempfile.TemporaryDirectory() as td:
            forbidden = Path(td) / "knowledge" / "inbox"

            with self.assertRaisesRegex(ValueError, "inbox"):
                propose_note(
                    forbidden,
                    title="Décision",
                    body="Contenu durable",
                    note_type="decision",
                    scope="global",
                    author_agent="hermes",
                    model="test-model",
                    source="session:test-session-123",
                )

            self.assertFalse(forbidden.exists())

    def test_rejects_obvious_personal_data(self):
        samples = (
            "SSN 123-45-6789",
            "Contact privé: alice@example.org",
            "IBAN FR76 3000 6000 0112 3456 7890 189",
        )
        for body in samples:
            with self.subTest(body=body), tempfile.TemporaryDirectory() as td, self.assertRaises(ValueError):
                propose_note(
                    Path(td) / "inbox", title="PII", body=body,
                    note_type="lesson", scope="global", author_agent="hermes",
                    model="test-model", source="session:test-session-123",
                )

    def test_validates_source_locator_by_scheme(self):
        invalid_sources = (
            "session:x", "file:not-a-real-path", "url:not-a-url",
            "git:repo@not-a-commit", "email:not-a-message-id",
            "ticket:none", "meeting:yesterday",
        )
        for source in invalid_sources:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as td, self.assertRaises(ValueError):
                propose_note(
                    Path(td) / "inbox", title="Source", body="Body",
                    note_type="lesson", scope="global", author_agent="hermes",
                    model="test-model", source=source,
                )

    def test_checks_created_descriptor_before_writing_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inbox = root / "inbox"
            inbox.mkdir()
            outside = root / "outside.md"
            real_open = os.open

            def swapped_open(_path, flags, mode=0o777):
                return real_open(outside, flags, mode)

            with patch("brain_propose.os.open", side_effect=swapped_open), self.assertRaises(ValueError):
                propose_note(
                    inbox, title="No leak", body="CONFIDENTIAL_BODY",
                    note_type="lesson", scope="global", author_agent="hermes",
                    model="test-model", source="session:test-session-123", brain_root=root,
                )

            self.assertTrue(outside.exists())
            self.assertEqual(outside.read_bytes(), b"")

    def test_rejects_empty_or_untraceable_provenance(self):
        base = {
            "title": "Décision",
            "body": "Contenu durable",
            "note_type": "decision",
            "scope": "global",
            "author_agent": "hermes",
            "model": "test-model",
            "source": "session:test-session-123",
        }
        invalid = [
            ("title", ""), ("scope", "  "), ("author_agent", ""), ("model", ""),
            ("source", ""), ("source", "sans-schema"), ("source", "invented:anything"),
        ]
        with tempfile.TemporaryDirectory() as td:
            for field, value in invalid:
                with self.subTest(field=field, value=value):
                    args = dict(base)
                    args[field] = value
                    with self.assertRaises(ValueError):
                        propose_note(Path(td) / "inbox", **args)

    def test_retries_when_candidate_filename_collides(self):
        with tempfile.TemporaryDirectory() as td:
            inbox = Path(td) / "inbox"
            inbox.mkdir()
            fixed = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
            first_nonce = "a" * 16
            second_nonce = "b" * 16
            collision = inbox / f"20260715-120000-decision-{first_nonce}.md"
            collision.write_text("existing", encoding="utf-8")

            with patch("brain_propose.datetime") as clock, patch(
                "brain_propose.secrets.token_hex", side_effect=[first_nonce, second_nonce]
            ):
                clock.now.return_value = fixed
                created = propose_note(
                    inbox,
                    title="Décision",
                    body="Contenu durable",
                    note_type="decision",
                    scope="global",
                    author_agent="hermes",
                    model="test-model",
                    source="session:test-session-123",
                )

            self.assertEqual(created.name, f"20260715-120000-decision-{second_nonce}.md")
            self.assertEqual(collision.read_text(encoding="utf-8"), "existing")


class CodexHookTrustTests(unittest.TestCase):
    def test_trusts_only_the_exact_matching_hook_hash_and_verifies_it(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            codex_home = root / ".codex"
            codex_home.mkdir()
            hooks_path = (codex_home / "hooks.json").resolve()
            wrapper = Path(r"C:\State Root\amitel-brain-hook.ps1")
            hook = {
                "key": f"{hooks_path}:user_prompt_submit:0:0",
                "sourcePath": str(hooks_path),
                "command": (
                    'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File '
                    '"C:\\State Root\\amitel-brain-hook.ps1"'
                ),
                "currentHash": "sha256:abc123",
                "trustStatus": "untrusted",
                "enabled": True,
            }
            foreign = {
                **hook,
                "key": f"{hooks_path}:user_prompt_submit:1:0",
                "command": hook["command"] + " --foreign",
            }
            moved_quotes = {
                **hook,
                "key": f"{hooks_path}:user_prompt_submit:2:0",
                "command": (
                    'powershell.exe "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File '
                    'C:\\State Root\\amitel-brain-hook.ps1"'
                ),
            }
            trusted = {**hook, "trustStatus": "trusted"}
            rpc_results = [
                [{"id": 1, "result": {"data": [{"hooks": [foreign, moved_quotes, hook]}]}}],
                [{"id": 2, "result": {}}],
                [{"id": 1, "result": {"data": [{"hooks": [foreign, moved_quotes, trusted]}]}}],
            ]

            with patch.object(codex_trust_hook, "_rpc", side_effect=rpc_results) as rpc:
                result = codex_trust_hook.trust_hook("codex", codex_home, root, wrapper)

            self.assertEqual(result["trust_status"], "trusted")
            trust_messages = rpc.call_args_list[1].args[2]
            edit = trust_messages[-1]["params"]["edits"][0]
            self.assertEqual(edit["keyPath"], "hooks.state")
            self.assertEqual(
                edit["value"],
                {hook["key"]: {"enabled": True, "trusted_hash": "sha256:abc123"}},
            )

    def test_refuses_when_matching_hook_is_not_unique(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            codex_home = root / ".codex"
            codex_home.mkdir()
            hooks_path = (codex_home / "hooks.json").resolve()
            wrapper = Path("amitel-brain-hook.ps1")
            hook = {
                "key": "key",
                "sourcePath": str(hooks_path),
                "command": (
                    "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "
                    + str(wrapper)
                ),
                "currentHash": "sha256:abc123",
            }
            listed = [{"id": 1, "result": {"data": [{"hooks": [hook, hook]}]}}]

            with patch.object(codex_trust_hook, "_rpc", return_value=listed):
                with self.assertRaisesRegex(RuntimeError, "exactly one"):
                    codex_trust_hook.trust_hook(
                        "codex", codex_home, root, wrapper
                    )


@unittest.skipUnless(os.name == "nt", "PowerShell integration test")
class WindowsInstallerTests(unittest.TestCase):
    @staticmethod
    def _isolated_env(root: Path) -> dict[str, str]:
        env = os.environ.copy()
        env["USERPROFILE"] = str(root / "user")
        env["LOCALAPPDATA"] = str(root / "local")
        windows = os.environ.get("SystemRoot", r"C:\Windows")
        env["PATH"] = os.pathsep.join((windows + r"\System32", windows))
        return env

    def test_file_invocation_defaults_brain_root_to_installer_directory(self):
        import json

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / "state"
            result = subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-File", str(TOOLING.parent / "install.ps1"),
                    "-HermesHome", str(root / "hermes"), "-StateRoot", str(state),
                    "-RuntimePython", sys.executable,
                    "-SkipDependencies", "-SkipIndex", "-SkipUserEnvironment",
                ],
                env=self._isolated_env(root), text=True, errors="replace",
                capture_output=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            config = json.loads((state / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(Path(config["brain_root"]).resolve(), TOOLING.parent.resolve())

    def test_uses_local_runtime_and_preserves_collision_named_foreign_hooks(self):
        import json

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            user = root / "user"
            state = root / "state"
            brain = root / "shared-brain"
            hermes = root / "hermes"
            (brain / "knowledge").mkdir(parents=True)
            (brain / "tooling").mkdir()
            (brain / "tooling" / "brain_hook.py").write_text(
                "raise RuntimeError('SHARED_CODE_EXECUTED')\n", encoding="utf-8"
            )
            (brain / "tooling" / "brain_index.py").write_text("raise SystemExit(99)\n", encoding="utf-8")
            foreign_command = "powershell.exe -File C:/safe/foreign-amitel-brain-hook.ps1"
            managed_unquoted = (
                "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "
                + str(state / "amitel-brain-hook.ps1")
            )
            fixture = {
                "hooks": {
                    "UserPromptSubmit": [
                        {
                            "matcher": "",
                            "hooks": [
                                {"type": "command", "command": foreign_command},
                                {"type": "command", "command": managed_unquoted},
                            ],
                        }
                    ]
                }
            }
            for relative in (Path(".claude/settings.json"), Path(".codex/hooks.json")):
                target = user / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(fixture), encoding="utf-8")

            env = self._isolated_env(root)
            install = subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-File", str(TOOLING.parent / "install.ps1"),
                    "-BrainRoot", str(brain), "-HermesHome", str(hermes),
                    "-StateRoot", str(state), "-RuntimePython", sys.executable,
                    "-SkipDependencies", "-SkipIndex", "-SkipUserEnvironment",
                ],
                env=env, text=True, errors="replace", capture_output=True, timeout=30,
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            config = json.loads((state / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(Path(config["code_root"]).resolve(), (state / "tooling").resolve())
            self.assertEqual(
                (state / "tooling" / "brain_hook.py").read_bytes(),
                (TOOLING / "brain_hook.py").read_bytes(),
            )
            self.assertNotEqual(
                (state / "tooling" / "brain_hook.py").read_bytes(),
                (brain / "tooling" / "brain_hook.py").read_bytes(),
            )
            expected_command = (
                'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "'
                + str(state / "amitel-brain-hook.ps1") + '"'
            )
            for relative in (Path(".claude/settings.json"), Path(".codex/hooks.json")):
                settings = json.loads((user / relative).read_text(encoding="utf-8"))
                commands = [
                    hook["command"]
                    for group in settings["hooks"]["UserPromptSubmit"]
                    for hook in group["hooks"]
                ]
                self.assertIn(foreign_command, commands)
                self.assertIn(managed_unquoted, commands)
                self.assertNotIn(expected_command, commands)

            uninstall = subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-File", str(TOOLING.parent / "uninstall.ps1"),
                    "-HermesHome", str(hermes), "-StateRoot", str(state),
                ],
                env=env, text=True, errors="replace", capture_output=True, timeout=30,
            )
            self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
            for relative in (Path(".claude/settings.json"), Path(".codex/hooks.json")):
                settings = json.loads((user / relative).read_text(encoding="utf-8"))
                commands = [
                    hook["command"]
                    for group in settings["hooks"]["UserPromptSubmit"]
                    for hook in group["hooks"]
                ]
                self.assertEqual(commands, [foreign_command])


if __name__ == "__main__":
    unittest.main()
