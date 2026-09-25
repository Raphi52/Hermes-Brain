import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TOOLING = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLING))

from brain_validate import REQUIRED_ARCHITECTURE_FILES, REQUIRED_SOURCE_FILES, REQUIRED_V1_NOTES, validate_brain
from obsidian_graph import refresh_indexes
from rig_coverage import REGISTRY_PATH, write_registry


def write_note(
    path: Path,
    *,
    uid: str = "rig/concept/greffe",
    status: str = "active",
    include_uid: bool = True,
    schema: str = "amitel-brain/v1",
    reviewed_by: str = '["reviewer"]',
    supersedes: str = "[]",
    created: str = "2026-07-15",
    body: str = "[[knowledge/_maps/rig]]",
    kind: str = "concept",
    scope: str = "rig",
    mocs: str = '["knowledge/_maps/rig"]',
    tags: str = "[rig, theme/rig]",
    extra_frontmatter: str = "",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    uid_line = f"uid: {uid}\n" if include_uid else ""
    path.write_text(
        "---\n"
        f"schema: {schema}\n"
        f"{uid_line}"
        "type: domain\n"
        f"kind: {kind}\n"
        f"scope: {scope}\n"
        "author_agent: human\n"
        'model: ""\n'
        f"created: {created}\n"
        "updated: 2026-07-15\n"
        f"status: {status}\n"
        "confidence: confirmed\n"
        'sources: ["file:C:/Code RIG/RigApplication/README.md"]\n'
        f"supersedes: {supersedes}\n"
        f"reviewed_by: {reviewed_by}\n"
        "reviewed_at: 2026-07-15\n"
        f"mocs: {mocs}\n"
        f"tags: {tags}\n"
        f"{extra_frontmatter}"
        f"---\n\n# Greffe\n\n{body}\n",
        encoding="utf-8",
    )


def make_architecture(root: Path) -> None:
    map_bodies = {
        Path("knowledge/_maps/brain.md"): (
            "global",
            "[[HOME]] [[knowledge/_maps/rig]] [[knowledge/_maps/contribution]] "
            "[[knowledge/runbooks/_index]] [[knowledge/standards/_index]] "
            "[[knowledge/_maps/vault-inventory]]",
        ),
        Path("knowledge/_maps/rig.md"): (
            "rig",
            "[[knowledge/_maps/rig-couverture]] [[knowledge/_maps/rig-source-mirror]]",
        ),
        Path("knowledge/_maps/contribution.md"): ("global", "[[HOME]]"),
        Path("knowledge/runbooks/_index.md"): ("global", "[[HOME]]"),
        Path("knowledge/standards/_index.md"): ("global", "[[HOME]]"),
    }
    map_themes = {
        Path("knowledge/_maps/brain.md"): "[theme/ia, theme/autowin-os]",
        Path("knowledge/_maps/rig.md"): "[theme/rig]",
        Path("knowledge/_maps/contribution.md"): "[theme/gouvernance]",
        Path("knowledge/runbooks/_index.md"): "[theme/operations, theme/integrations]",
        Path("knowledge/standards/_index.md"): "[theme/architecture, theme/donnees]",
    }
    for relative in REQUIRED_ARCHITECTURE_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative in REQUIRED_V1_NOTES:
            scope, body = map_bodies[relative]
            write_note(
                path,
                uid=f"{scope}/map/{relative.parent.name}-{path.stem}",
                body=body,
                kind="map",
                scope=scope,
                mocs="[]",
                tags=map_themes[relative],
            )
        else:
            path.write_text(f"# {path.stem}\n", encoding="utf-8")
    for relative in REQUIRED_SOURCE_FILES:
        source = root / "knowledge/domain/rigapplication-documentation" / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        content = "# Source miroir sans frontmatter\n"
        if relative == Path("_IMPORT.md"):
            content = (
                "---\ntype: domain\nscope: rig\nstatus: active\n"
                "# Source miroir active\n\n[[missing-source-target]]\n"
            )
        source.write_text(content, encoding="utf-8")
    mirror = root / "knowledge/domain/rigapplication-documentation"
    files = [{"path": relative.as_posix()} for relative in REQUIRED_SOURCE_FILES]
    (mirror / "_SOURCE_MANIFEST.json").write_text(
        json.dumps({"schema": 1, "source_repo_head": "fixture", "file_count": len(files), "files": files}),
        encoding="utf-8",
    )
    coverage_map = root / "knowledge/_maps/rig-couverture.md"
    write_note(coverage_map, uid="rig/map/coverage", body="[[HOME]]", kind="map")
    ast_map = root / "knowledge/domain/rig-ast-graphes-vers-phases-reconstruction.md"
    ast_map.write_text("# AST\n", encoding="utf-8")
    graph_config = root / ".obsidian/graph.json"
    graph_config.parent.mkdir(parents=True, exist_ok=True)
    graph_config.write_text(
        json.dumps(
            {
                "search": "",
                "showAttachments": False,
                "hideUnresolved": True,
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
    refresh_indexes(root, reviewer="fixture:external-reviewer")
    write_registry(root)


class BrainArchitectureValidationTests(unittest.TestCase):
    def test_rejects_missing_rig_coverage_registry(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_architecture(root)
            (root / REGISTRY_PATH).unlink()

            report = validate_brain(root)

            self.assertTrue(any("coverage registry" in error for error in report.errors))

    def test_accepts_valid_architecture_and_ignores_canonical_source_frontmatter(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_architecture(root)
            write_note(root / "knowledge/domain/greffe.md")
            rig_map = root / "knowledge/_maps/rig.md"
            rig_map.write_text(
                rig_map.read_text(encoding="utf-8") + "\n[[knowledge/domain/greffe]]\n",
                encoding="utf-8",
            )
            refresh_indexes(root, reviewer="fixture:external-reviewer")

            report = validate_brain(root)

            self.assertEqual(report.errors, [])
            self.assertGreaterEqual(report.counts["v1_notes"], 4)
            self.assertEqual(report.counts["active_notes"], 9)
            self.assertEqual(report.counts["orphan_notes"], 0)
            self.assertEqual(report.counts["canonical_source_files"], len(REQUIRED_SOURCE_FILES) + 1)

    def test_rejects_active_v1_note_without_controlled_theme(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_architecture(root)
            write_note(
                root / "knowledge/domain/no-theme.md",
                uid="rig/concept/no-theme",
                tags="[rig, architecture]",
            )

            report = validate_brain(root)

            self.assertTrue(any("must have 1 or 2 controlled theme/* tags" in error for error in report.errors))

    def test_rejects_unknown_or_excess_themes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_architecture(root)
            write_note(
                root / "knowledge/domain/too-many-themes.md",
                uid="rig/concept/too-many-themes",
                tags="[theme/rig, theme/architecture, theme/inconnu]",
            )

            report = validate_brain(root)

            self.assertTrue(any("unsupported themes" in error for error in report.errors))
            self.assertTrue(any("must have 1 or 2 controlled theme/* tags" in error for error in report.errors))

    def test_rejects_controlled_theme_without_any_active_v1_note(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_architecture(root)
            brain = root / "knowledge/_maps/brain.md"
            brain.write_text(
                brain.read_text(encoding="utf-8").replace(
                    "tags: [theme/ia, theme/autowin-os]",
                    "tags: [theme/rig, theme/autowin-os]",
                ),
                encoding="utf-8",
            )

            report = validate_brain(root)

            self.assertTrue(any("controlled themes without active v1 notes: theme/ia" in error for error in report.errors))

    def test_rejects_reviewer_from_same_agent_family(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_architecture(root)
            note = root / "knowledge/domain/self-reviewed.md"
            write_note(
                note,
                uid="rig/concept/self-reviewed",
                reviewed_by='["hermes:reviewer"]',
            )
            note.write_text(
                note.read_text(encoding="utf-8").replace("author_agent: human", "author_agent: hermes :variant"),
                encoding="utf-8",
            )

            report = validate_brain(root)

            self.assertTrue(any("distinct reviewer agent" in error for error in report.errors))

    def test_rejects_malformed_quoted_agent_family(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_architecture(root)
            note = root / "knowledge/domain/malformed-reviewer.md"
            write_note(
                note,
                uid="rig/concept/malformed-reviewer",
                reviewed_by='["hermes:reviewer"]',
            )
            note.write_text(
                note.read_text(encoding="utf-8").replace("author_agent: human", "author_agent: 'hermes' :variant"),
                encoding="utf-8",
            )

            report = validate_brain(root)

            self.assertTrue(any("invalid author_agent identifier" in error for error in report.errors))

    def test_rejects_active_notes_disconnected_from_company_and_rig_roots(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_architecture(root)
            write_note(
                root / "knowledge/domain/orphan-a.md",
                uid="rig/concept/orphan-a",
                body="[[knowledge/domain/orphan-b]]",
            )
            write_note(
                root / "knowledge/domain/orphan-b.md",
                uid="rig/concept/orphan-b",
                body="[[knowledge/domain/orphan-a]]",
            )

            report = validate_brain(root)

            orphan_errors = [error for error in report.errors if "orphan" in error]
            self.assertEqual(len(orphan_errors), 4)
            self.assertTrue(any("company MOC" in error for error in orphan_errors))
            self.assertTrue(any("RIG MOC" in error for error in orphan_errors))
            self.assertEqual(report.counts["orphan_notes"], 2)

    def test_rejects_active_legacy_note_disconnected_from_company_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_architecture(root)
            legacy = root / "knowledge/lessons/orphan-legacy.md"
            legacy.parent.mkdir(parents=True, exist_ok=True)
            legacy.write_text(
                "---\ntype: lesson\nscope: global\nstatus: active\n---\n\n# Orpheline\n",
                encoding="utf-8",
            )

            report = validate_brain(root)

            self.assertTrue(
                any("orphan-legacy.md" in error and "company MOC" in error for error in report.errors)
            )
            self.assertEqual(report.counts["orphan_notes"], 1)

    def test_rejects_unclosed_active_legacy_frontmatter(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_architecture(root)
            broken = root / "knowledge/lessons/truncated.md"
            broken.parent.mkdir(parents=True, exist_ok=True)
            broken.write_text("---\nstatus: active\nscope: rig\n", encoding="utf-8")

            report = validate_brain(root)

            self.assertTrue(any("truncated.md: unclosed frontmatter" in error for error in report.errors))

    def test_code_fenced_wikilink_does_not_attach_an_orphan(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_architecture(root)
            write_note(root / "knowledge/domain/code-only.md", uid="rig/concept/code-only")
            rig_map = root / "knowledge/_maps/rig.md"
            rig_map.write_text(
                rig_map.read_text(encoding="utf-8")
                + "\n```md\n[[knowledge/domain/code-only]]\n````\n",
                encoding="utf-8",
            )

            report = validate_brain(root)

            self.assertTrue(any("code-only.md" in error and "RIG MOC" in error for error in report.errors))

    def test_requires_existing_anchor_for_navigation_and_accepts_alias(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_architecture(root)
            write_note(root / "knowledge/domain/anchored.md", uid="rig/concept/anchored")
            rig_map = root / "knowledge/_maps/rig.md"
            original = rig_map.read_text(encoding="utf-8")
            rig_map.write_text(
                original + "\n[[knowledge/domain/anchored#Missing|Alias]]\n",
                encoding="utf-8",
            )

            invalid = validate_brain(root)

            self.assertTrue(any("unresolved wikilink anchor" in error for error in invalid.errors))
            self.assertTrue(any("anchored.md" in error and "RIG MOC" in error for error in invalid.errors))

            rig_map.write_text(
                original + "\n[[knowledge/domain/anchored#Greffe|Alias]]\n",
                encoding="utf-8",
            )
            refresh_indexes(root, reviewer="fixture:external-reviewer")
            valid = validate_brain(root)

            self.assertEqual(valid.errors, [])

    def test_rejects_v1_note_without_uid(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_architecture(root)
            write_note(root / "knowledge/domain/broken.md", include_uid=False)

            report = validate_brain(root)

            self.assertTrue(any("uid" in error and "broken.md" in error for error in report.errors))

    def test_rejects_duplicate_uid(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_architecture(root)
            write_note(root / "knowledge/domain/one.md", uid="rig/concept/duplicate")
            write_note(root / "knowledge/domain/two.md", uid="rig/concept/duplicate")

            report = validate_brain(root)

            self.assertTrue(any("duplicate uid" in error for error in report.errors))

    def test_rejects_candidate_inside_active_knowledge(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_architecture(root)
            write_note(root / "knowledge/domain/candidate.md", status="candidate")

            report = validate_brain(root)

            self.assertTrue(any("candidate" in error and "knowledge" in error for error in report.errors))

    def test_rejects_candidate_schema_inside_knowledge_even_as_legacy(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_architecture(root)
            write_note(root / "knowledge/domain/candidate.md", schema="amitel-brain/candidate-v1", status="candidate")

            report = validate_brain(root)

            self.assertTrue(any("candidate" in error and "knowledge" in error for error in report.errors))

    def test_requires_index_and_nonempty_canonical_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_architecture(root)
            (root / "index.md").unlink()
            for relative in REQUIRED_SOURCE_FILES:
                (root / "knowledge/domain/rigapplication-documentation" / relative).unlink()

            report = validate_brain(root)

            self.assertTrue(any("index.md" in error for error in report.errors))
            self.assertTrue(any("canonical source" in error for error in report.errors))

    def test_rejects_duplicate_frontmatter_keys(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_architecture(root)
            write_note(root / "knowledge/domain/duplicate-key.md", extra_frontmatter="uid: rig/concept/second\n")

            report = validate_brain(root)

            self.assertTrue(any("duplicate frontmatter key" in error for error in report.errors))

    def test_requires_distinct_reviewer_for_active_note(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_architecture(root)
            write_note(root / "knowledge/domain/unreviewed.md", reviewed_by='["human"]')

            report = validate_brain(root)

            self.assertTrue(any("distinct reviewer" in error for error in report.errors))

    def test_rejects_pending_reviewer_placeholder(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_architecture(root)
            write_note(
                root / "knowledge/domain/pending-review.md",
                reviewed_by='["pending:external-judge"]',
            )

            report = validate_brain(root)

            self.assertTrue(any("reviewer placeholder" in error for error in report.errors), report.errors)

    def test_rejects_broken_or_unresolved_wikilinks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_architecture(root)
            write_note(root / "knowledge/domain/broken-link.md", body="[[missing-target")

            report = validate_brain(root)

            self.assertTrue(any("wikilink" in error for error in report.errors))

    def test_rejects_compact_date(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_architecture(root)
            write_note(root / "knowledge/domain/compact-date.md", created="20260715")

            report = validate_brain(root)

            self.assertTrue(any("ISO dates" in error for error in report.errors))

    def test_requires_superseded_target_to_exist_and_have_superseded_status(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_architecture(root)
            write_note(root / "knowledge/domain/old.md", uid="rig/concept/old", status="active")
            write_note(
                root / "knowledge/domain/new.md",
                uid="rig/concept/new",
                supersedes='["rig/concept/old", "rig/concept/missing"]',
            )

            report = validate_brain(root)

            self.assertTrue(any("must have status superseded" in error for error in report.errors))
            self.assertTrue(any("unknown superseded uid" in error for error in report.errors))

    def test_rejects_empty_reviewer_for_any_managed_status(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_architecture(root)
            write_note(root / "knowledge/domain/empty-reviewer.md", status="archived", reviewed_by='[""]')

            report = validate_brain(root)

            self.assertTrue(any("reviewer" in error for error in report.errors))

    def test_normalizes_reviewer_identity_before_distinctness_check(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_architecture(root)
            write_note(root / "knowledge/domain/padded-reviewer.md", reviewed_by='[" human "]')

            report = validate_brain(root)

            self.assertTrue(any("distinct reviewer" in error for error in report.errors))

    def test_parses_bom_before_rejecting_candidate_schema(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_architecture(root)
            path = root / "knowledge/domain/bom-candidate.md"
            write_note(path, schema="amitel-brain/candidate-v1", status="candidate")
            path.write_text("\ufeff" + path.read_text(encoding="utf-8"), encoding="utf-8")

            report = validate_brain(root)

            self.assertTrue(any("candidate" in error and "bom-candidate" in error for error in report.errors))

    def test_rejects_quoted_managed_schema_in_truncated_frontmatter(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_architecture(root)
            path = root / "knowledge/domain/truncated.md"
            path.write_text('---\nschema: "amitel-brain/v1"\nuid: rig/concept/truncated\n', encoding="utf-8")

            report = validate_brain(root)

            self.assertTrue(any("unparseable managed frontmatter" in error for error in report.errors))

    def test_rejects_distant_candidate_schema_in_truncated_frontmatter(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_architecture(root)
            path = root / "knowledge/domain/distant-candidate.md"
            path.write_text(
                "---\n" + ("# padding\n" * 150) + ' schema : "amitel-brain/candidate-v1" \n',
                encoding="utf-8",
            )

            report = validate_brain(root)

            self.assertTrue(any("unparseable managed frontmatter" in error for error in report.errors))

    def test_confines_wikilinks_and_mocs_to_vault(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "vault"
            make_architecture(root)
            (Path(td) / "external.md").write_text("outside", encoding="utf-8")
            write_note(
                root / "knowledge/domain/escape.md",
                body="[[../external]]",
                extra_frontmatter="",
            )
            path = root / "knowledge/domain/escape.md"
            text = path.read_text(encoding="utf-8").replace(
                'mocs: ["knowledge/_maps/rig"]',
                'mocs: ["knowledge/_maps/../../../external"]',
            )
            path.write_text(text, encoding="utf-8")

            report = validate_brain(root)

            self.assertTrue(any("outside brain root" in error for error in report.errors))
            self.assertTrue(any("invalid MOC" in error for error in report.errors))

    def test_archived_replacement_does_not_satisfy_active_supersession(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_architecture(root)
            write_note(root / "knowledge/domain/old.md", uid="rig/concept/old", status="superseded")
            write_note(
                root / "knowledge/domain/replacement.md",
                uid="rig/concept/replacement",
                status="archived",
                supersedes='["rig/concept/old"]',
            )

            report = validate_brain(root)

            self.assertTrue(any("active replacement" in error for error in report.errors))

    def test_reports_inaccessible_root_without_raising(self):
        with patch.object(Path, "is_dir", side_effect=PermissionError("denied")):
            report = validate_brain("//server/denied")

        self.assertTrue(any("cannot access brain root" in error for error in report.errors))

    def test_cli_returns_nonzero_and_json_for_invalid_vault(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_architecture(root)
            write_note(root / "knowledge/domain/broken.md", include_uid=False)

            result = subprocess.run(
                [sys.executable, str(TOOLING / "brain_validate.py"), "--root", str(root), "--json"],
                text=True,
                capture_output=True,
                timeout=10,
            )

            self.assertEqual(result.returncode, 2)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "invalid")
            self.assertGreater(payload["counts"]["errors"], 0)


if __name__ == "__main__":
    unittest.main()
