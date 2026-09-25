#!/usr/bin/env python3
"""AI-only curation of inbox/ candidates — no human review step.

Deterministic engine of the full-AI curation protocol (see
knowledge/decisions/curation-full-ai.md):

  1. `--report` (default): audit every inbox candidate — structural checks,
     secret scan, source locator — and score semantic overlap against the
     existing knowledge/ index. Verdicts:
        promote  : safe, no strong overlap -> mechanical promotion possible
        merge    : an active note covers the same theme -> an AI session must
                   write ONE consolidated note (supersedes both), never auto
        reject   : failed a hard check (reason given)
  2. `--apply` : execute the mechanical part only — move `promote` candidates
     into knowledge/<type>/ with status: active + curated_by provenance.
     `merge` and `reject` are NEVER auto-applied: merging rewrites knowledge
     and stays the job of the AI session running the protocol.

After any apply: re-run brain_index.py, then brain_validate.py must pass.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import unicodedata
from datetime import date
from pathlib import Path

from brain_candidate_policy import scan_candidate, valid_source

TYPE_DIRS = {
    "lesson": "lessons",
    "decision": "decisions",
    "domain": "domain",
    "preference": "preferences",
}
REQUIRED = ("type", "scope", "author_agent", "model", "created", "status", "source")
MERGE_THRESHOLD = 0.62  # dense cosine vs an active knowledge/ note


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    match = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n(.*)\Z", text, re.DOTALL)
    if not match:
        return {}, text
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep and re.fullmatch(r"[A-Za-z_]+", key.strip()):
            fields[key.strip()] = value.strip().strip('"')
    return fields, match.group(2)


def _slug(title: str) -> str:
    text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return text[:80] or "note"


def _audit(path: Path, meta: dict[str, str], body: str) -> str | None:
    for field in REQUIRED:
        if not meta.get(field, "").strip():
            return f"missing field: {field}"
    if meta.get("status") != "candidate":
        return f"status is {meta.get('status')!r}, expected candidate"
    if meta.get("type") not in TYPE_DIRS:
        return f"unsupported type: {meta.get('type')!r}"
    if not valid_source(meta["source"]):
        return "source locator is not verifiable"
    if not body.strip() or not body.lstrip().startswith("#"):
        return "body is empty or has no title heading"
    metadata_text = "\n".join(value for value in meta.values() if isinstance(value, str))
    # Le nom de fichier porte un suffixe hexadecimal genere : ce n'est pas du contenu,
    # et il declenchait le motif IBAN. Le titre reel vit dans le body, deja scanne.
    heading = re.search(r"(?m)^#\s+(.+)$", body or "")
    finding = scan_candidate(
        heading.group(1).strip() if heading else "",
        body,
        meta.get("source", ""),
        metadata_text,
    )
    if finding:
        return finding
    return None


def _overlap(retriever, title: str, body: str, k: int = 5) -> list[dict]:
    query = f"{title}\n{body[:800]}"
    hits = retriever.query(query, k).get("hits", [])
    return [
        {"path": h["path"], "cos": h.get("dense_cos"), "status": h.get("status")}
        for h in hits
        if h["path"].startswith("knowledge/") and h.get("status") != "superseded"
    ]


def curate(root: Path, index: Path, apply: bool, threshold: float, reviewer: str | None = None) -> dict:
    if apply and not reviewer:
        raise ValueError("reviewer is required when applying promotions")
    retriever = None
    results = []
    for path in sorted((root / "inbox").glob("*.md")):
        meta, body = _frontmatter(path.read_text(encoding="utf-8"))
        if meta.get("status") != "candidate":
            continue  # not a candidate note (README, already-reviewed material)
        title_match = re.search(r"(?m)^#\s+(.+)$", body or "")
        title = title_match.group(1).strip() if title_match else path.stem
        entry: dict = {"candidate": path.name, "title": title}
        reason = _audit(path, meta, body)
        if reason:
            entry.update(verdict="reject", reason=reason)
            results.append(entry)
            continue
        if retriever is None:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from brain_retrieval import BrainRetriever
            retriever = BrainRetriever(index)
        hits = _overlap(retriever, title, body)
        entry["nearest"] = hits[:3]
        top = max((h["cos"] or 0.0) for h in hits) if hits else 0.0
        if top >= threshold:
            best = max(hits, key=lambda h: h["cos"] or 0.0)
            entry.update(
                verdict="merge",
                merge_with=best["path"],
                note="AI session must write ONE consolidated note superseding "
                     "both the target and this candidate (append+supersede).",
            )
        else:
            entry["verdict"] = "promote"
            if apply:
                entry["promoted_to"] = _promote(root, path, meta, body, title, reviewer=reviewer)
        results.append(entry)
    return {"threshold": threshold, "applied": apply, "candidates": results}


def _list_field(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid inline list: {raw!r}") from exc
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"invalid inline list: {raw!r}")
    return value


def _default_theme(scope: str) -> str:
    folded = scope.casefold()
    if "autowin" in folded:
        return "theme/autowin-os"
    if folded == "rig" or folded.startswith("rig-") or folded.startswith("rig/"):
        return "theme/rig"
    return "theme/gouvernance"


def _default_mocs(scope: str) -> list[str]:
    folded = scope.casefold()
    if "autowin" in folded:
        return ["knowledge/_maps/autowin-os"]
    if folded == "rig" or folded.startswith("rig-") or folded.startswith("rig/"):
        return ["knowledge/_maps/rig"]
    return ["knowledge/_maps/brain"]



MOC_SECTION = "## Notes curees rattachees"


def attach_to_mocs(root: Path, mocs: list[str], note_relative: str) -> list[str]:
    """Accroche la fiche promue a ses cartes (MOC).

    Pourquoi (mesure du 2026-09-02) : `_promote` ecrivait la fiche et s arretait la. Le lien
    inverse — la carte qui pointe vers la fiche — restait a faire a la main, or l etape 5 du
    protocole (`inbox/README.md`) l exige et `brain_validate.py` la controle : toute note active
    non ATTEIGNABLE depuis knowledge/_maps/brain.md est comptee orpheline. Resultat mesure :
    103 candidats promus mecaniquement = 102 orphelins et un validateur `invalid`. La promotion
    n etait donc jamais complete, et la dette n apparaissait qu au validateur.
    """
    linked = []
    for moc in mocs:
        target = root / (moc if moc.endswith(".md") else moc + ".md")
        if not target.exists():
            continue
        text = io.open(target, encoding="utf-8").read()
        stem = note_relative[:-3] if note_relative.endswith(".md") else note_relative
        link = "- [[" + stem + "]]"
        if link in text:
            continue
        marker = _moc_section_marker(text)
        if marker:
            head, sep, tail = text.partition(marker)
            text = head + sep + tail.rstrip("\n") + "\n" + link + "\n"
        else:
            text = text.rstrip("\n") + "\n\n" + MOC_SECTION + "\n\n" + link + "\n"
        io.open(target, "w", encoding="utf-8", newline="\n").write(text)
        linked.append(moc)
    return linked


def _moc_section_marker(text: str) -> str | None:
    for line in text.split("\n"):
        if line.startswith("## Notes cur") and "rattach" in line:
            return line
    return None


def _promote(
    root: Path, path: Path, meta: dict[str, str], body: str, title: str, *, reviewer: str | None,
) -> str:
    if not reviewer or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", reviewer):
        raise ValueError("a valid reviewer identifier is required")
    author = meta["author_agent"].strip().strip('"\'')
    if reviewer.split(":", 1)[0].casefold() == author.split(":", 1)[0].casefold():
        raise ValueError("reviewer must belong to a distinct agent family")
    target_dir = root / "knowledge" / TYPE_DIRS[meta["type"]]
    target = target_dir / f"{_slug(title)}.md"
    if target.exists():
        raise RuntimeError(f"refusing to overwrite existing note: {target}")
    today = date.today().isoformat()
    kind = meta.get("kind") or (meta["type"] if meta["type"] != "domain" else "concept")
    scope = meta["scope"].strip().strip('"\'')
    tags = _list_field(meta.get("tags"))
    if not any(tag.startswith("theme/") for tag in tags):
        tags.append(_default_theme(scope))
    mocs = _list_field(meta.get("mocs")) or _default_mocs(scope)
    sources = [meta["source"].strip().strip('"\'')]
    confidence = {"low": "hypothesis", "medium": "derived", "high": "derived"}.get(
        meta.get("confidence", "medium").strip().strip('"\''), "hypothesis"
    )
    uid = f"{_slug(scope)}/{_slug(kind)}/{_slug(title)}"
    lines = [
        "---",
        "schema: amitel-brain/v1",
        f"uid: {uid}",
        f"type: {meta['type']}",
        f"kind: {kind}",
        f"scope: {json.dumps(scope, ensure_ascii=False)}",
        f"author_agent: {json.dumps(author, ensure_ascii=False)}",
        f"model: {meta['model']}",
        f"created: {meta['created']}",
        f"updated: {today}",
        "status: active",
        f"confidence: {confidence}",
        f"sources: {json.dumps(sources, ensure_ascii=False)}",
        f"supersedes: {meta.get('supersedes', '[]')}",
        f"reviewed_by: {json.dumps([reviewer], ensure_ascii=False)}",
        f"reviewed_at: {today}",
        f"mocs: {json.dumps(mocs, ensure_ascii=False)}",
        f"tags: {json.dumps(tags, ensure_ascii=False)}",
    ]
    lines.append("---")
    target_dir.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n\n" + body.lstrip(), encoding="utf-8")
    path.unlink()
    relative = target.relative_to(root).as_posix()
    # Promotion INCOMPLETE sans ce lien : une fiche non citee par sa carte est orpheline pour
    # brain_validate.py, donc invisible dans la navigation Obsidian.
    attach_to_mocs(root, mocs, relative)
    return relative


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    here = Path(__file__).resolve().parent
    parser.add_argument("--brain", type=Path, default=here.parent)
    parser.add_argument("--index", type=Path, default=here / "index")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--merge-threshold", type=float, default=MERGE_THRESHOLD)
    parser.add_argument("--reviewer", help="Distinct agent identifier; required with --apply")
    args = parser.parse_args()
    report = curate(args.brain.resolve(), args.index, args.apply, args.merge_threshold, reviewer=args.reviewer)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
