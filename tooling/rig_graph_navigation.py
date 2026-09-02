#!/usr/bin/env python3
"""Build a bounded Obsidian navigation layer over the RIG Graphify snapshots."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path


NAV_RELATIONS = ("calls", "references", "imports", "inherits", "implements")

# The five relation notes of a project share ~95% of their text (same header, same zone list),
# so a query matching that boilerplate saturates the lexical axis for ALL of them at once and
# the final order falls to the dense axis, which cannot separate near-duplicates. Measured
# 2026-08-04: asking « quelles fonctions … en appellent d'autres » put calls.md at rank 8,
# BEHIND its four siblings, all five within 3% of each other. Each note therefore carries the
# French vocabulary of its OWN relation — in the title, so it also reaches the metadata axis.
RELATION_GLOSS = {
    "calls": ("appels de méthodes", "quelle fonction en appelle une autre"),
    "references": ("références", "quel symbole en référence un autre"),
    "imports": ("imports", "quel fichier importe quel module"),
    "inherits": ("héritage", "quelle classe hérite de quelle autre"),
    "implements": ("implémentations", "quelle classe implémente quelle interface"),
}


def _md_text(value: object) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace("\\", "\\\\")
    for character in "`|[]":
        text = text.replace(character, f"\\{character}")
    return text


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-") or "root"


def _unique_slugs(names: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    used: set[str] = set()
    for name in names:
        base = _slug(name)
        candidate = base
        if candidate in used:
            suffix = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
            candidate = f"{base}-{suffix}"
        counter = 2
        while candidate in used:
            candidate = f"{base}-{counter}"
            counter += 1
        used.add(candidate)
        result[name] = candidate
    return result


def _confined_path(root: Path, path: Path) -> Path:
    root = root.resolve()
    lexical = root / path
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"Chemin hors vault: {path}") from exc
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise RuntimeError(f"Symlink interdit dans un chemin généré: {path}")
    candidate = lexical.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"Chemin hors vault: {path}") from exc
    return candidate


def _allowed_generated_path(relative: str) -> bool:
    if relative == "knowledge/_maps/rig-code-graphes.md":
        return True
    parts = Path(relative).as_posix().split("/")
    if len(parts) == 4:
        return (
            parts[0] == "projects"
            and re.fullmatch(r"rig-[A-Za-z0-9_.-]+", parts[1]) is not None
            and parts[2] == "obsidian"
            and parts[3] == f"{parts[1]}.md"
        )
    if len(parts) == 5:
        if (
            parts[0] == "projects"
            and re.fullmatch(r"rig-[A-Za-z0-9_.-]+", parts[1]) is not None
            and parts[2:4] == ["obsidian", "relations"]
            and parts[4] in {f"{relation}.md" for relation in NAV_RELATIONS}
        ):
            return True
        return (
            parts[0] == "projects"
            and re.fullmatch(r"rig-[A-Za-z0-9_.-]+", parts[1]) is not None
            and parts[2:4] == ["obsidian", "areas"]
            and re.fullmatch(r"[a-z0-9-]+\.md", parts[4]) is not None
        )
    return False


def _expected_uid(relative: str) -> str | None:
    if relative == "knowledge/_maps/rig-code-graphes.md":
        return "rig/map/code-graphes"
    parts = Path(relative).as_posix().split("/")
    if len(parts) == 4 and parts[:1] == ["projects"]:
        return f"rig/graph/{parts[1]}"
    if len(parts) == 5 and parts[:1] == ["projects"]:
        kind = "relation" if parts[3] == "relations" else "area" if parts[3] == "areas" else None
        if kind:
            return f"rig/graph/{parts[1]}/{kind}/{Path(parts[4]).stem}"
    return None


def _looks_generated(path: Path, relative: str) -> bool:
    expected_uid = _expected_uid(relative)
    if not path.is_file() or expected_uid is None:
        return False
    prefix = path.read_text(encoding="utf-8")[:1000]
    return (
        "schema: amitel-brain/v1" in prefix
        and "author_agent: hermes" in prefix
        and "generated_by: rig-graph-navigation/v2" in prefix
        and f"uid: {json.dumps(expected_uid)}" in prefix
    )


def _frontmatter(
    uid: str,
    source: str,
    tags: list[str],
    reviewer: str,
    generated_on: str,
    moc: str | None = "knowledge/_maps/rig-code-graphes",
) -> str:
    today = generated_on
    return "\n".join(
        [
            "---",
            "schema: amitel-brain/v1",
            f"uid: {json.dumps(uid, ensure_ascii=False)}",
            "type: domain",
            "kind: map",
            "scope: rig",
            "author_agent: hermes",
            "generated_by: rig-graph-navigation/v2",
            f"model: {json.dumps('gpt-5.6-sol')}",
            f"created: {today}",
            f"updated: {today}",
            "status: active",
            "confidence: confirmed",
            f"sources: [{json.dumps(f'file:{source}', ensure_ascii=False)}]",
            "supersedes: []",
            f"reviewed_by: [{json.dumps(reviewer, ensure_ascii=False)}]",
            f"reviewed_at: {today}",
            f"mocs: [{json.dumps(f'[[{moc}]]', ensure_ascii=False)}]" if moc else "mocs: []",
            f"tags: [{', '.join(json.dumps(tag, ensure_ascii=False) for tag in tags)}]",
            "---",
        ]
    )


def _write_generated(
    root: Path,
    path: Path,
    content: str,
    previous: dict[str, str],
    preserved: set[str],
) -> None:
    relative = path.relative_to(root).as_posix()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and relative in previous:
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != previous[relative] or not _looks_generated(path, relative):
            preserved.add(relative)
            return
    elif path.exists():
        raise RuntimeError(f"refusing to overwrite unowned output: {relative}")
    text = content.rstrip() + "\n"
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")


def _area(source_file: str) -> str:
    path = source_file.replace("\\", "/").strip("/")
    return path.split("/", 1)[0] if "/" in path else "(racine)"


def _project_map(
    root: Path,
    graph_path: Path,
    reviewer: str,
    max_areas: int,
    top_symbols: int,
    generated_on: str,
    previous: dict[str, str],
    preserved: set[str],
) -> tuple[str, list[Path], list[Path]]:
    graph_path = _confined_path(root, graph_path)
    project = graph_path.parents[1].name
    if re.fullmatch(r"rig-[A-Za-z0-9_.-]+", project) is None:
        raise ValueError(f"invalid project identifier: {project}")
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    nodes = graph.get("nodes", [])
    links = graph.get("links", [])
    degree: Counter[str] = Counter()
    edge_types: Counter[str] = Counter()
    node_ids = [str(node.get("id") or "") for node in nodes]
    if any(not node_id for node_id in node_ids) or len(set(node_ids)) != len(node_ids):
        raise ValueError(f"{project}: node ids must be non-empty and unique")
    node_by_id = dict(zip(node_ids, nodes, strict=True))
    relation_edge_ids: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for link in links:
        source_id = str(link.get("source") or "")
        target_id = str(link.get("target") or "")
        if not source_id or not target_id:
            raise ValueError(f"{project}: link endpoints must be non-empty")
        relation = str(link.get("relation") or link.get("type") or "non typée")
        degree[source_id] += 1
        degree[target_id] += 1
        edge_types[relation] += 1
        if relation in NAV_RELATIONS and source_id in node_by_id and target_id in node_by_id:
            relation_edge_ids[relation].add((source_id, target_id))
    relation_edges = {
        relation: [(node_by_id[source_id], node_by_id[target_id]) for source_id, target_id in sorted(edge_ids)]
        for relation, edge_ids in relation_edge_ids.items()
    }

    area_nodes: dict[str, list[dict[str, object]]] = defaultdict(list)
    for node in nodes:
        source_file = str(node.get("source_file") or "")
        if source_file:
            area_nodes[_area(source_file)].append(node)
    selected = sorted(area_nodes, key=lambda name: (-len(area_nodes[name]), name.casefold(), name))[:max_areas]
    area_slugs = _unique_slugs(selected)
    area_relations: dict[str, set[str]] = defaultdict(set)
    for relation, edges in relation_edges.items():
        for source_node, target_node in edges:
            for node in (source_node, target_node):
                area = _area(str(node.get("source_file") or ""))
                if area in area_slugs:
                    area_relations[area].add(relation)

    output = _confined_path(root, Path(f"projects/{project}/obsidian"))
    area_paths: list[Path] = []
    project_link = f"projects/{project}/obsidian/{project}"
    for area in selected:
        area_path = _confined_path(root, output / "areas" / f"{area_slugs[area]}.md")
        area_paths.append(area_path)
        candidates = [
            node
            for node in area_nodes[area]
            if ".Designer.cs" not in str(node.get("source_file") or "")
        ]
        candidates.sort(
            key=lambda node: (
                -degree[str(node.get("id"))],
                str(node.get("label") or "").casefold(),
                str(node.get("label") or ""),
                str(node.get("source_file") or "").casefold(),
                str(node.get("source_file") or ""),
                str(node.get("id") or ""),
            )
        )
        rows = []
        for node in candidates[:top_symbols]:
            label = _md_text(node.get("label") or node.get("id") or "?")
            source_file = _md_text(node.get("source_file") or "")
            location = _md_text(node.get("source_location") or "-")
            rows.append(f"| {label} | {source_file} | {location} | {degree[str(node.get('id'))]} |")
        source = graph_path.relative_to(root).as_posix()
        body = [
            _frontmatter(
                f"rig/graph/{project}/area/{area_slugs[area]}",
                source,
                ["rig", "graphify", "code-map", "area"],
                reviewer,
                generated_on,
            ),
            "",
            f"# {project} — {_md_text(area)}",
            "",
            f"Retour : [[{project_link}|{project}]] · [[knowledge/_maps/rig-code-graphes|Graphes code RIG]]",
            "",
            f"Zone sélectionnée déterministement : **{len(area_nodes[area])} nœuds sourcés**.",
            "Les symboles ci-dessous sont classés par degré ; les fichiers `.Designer.cs` sont exclus de ce palmarès.",
            "",
            "| Symbole | Source | Ligne | Degré |",
            "|---|---|---:|---:|",
            *rows,
        ]
        if area_relations[area]:
            body.extend(
                [
                    "",
                    "## Relations structurelles tracées",
                    "",
                    *[
                        f"- [[projects/{project}/obsidian/relations/{_slug(relation)}|{relation}]]"
                        for relation in NAV_RELATIONS
                        if relation in area_relations[area]
                    ],
                ]
            )
        _write_generated(root, area_path, "\n".join(body), previous, preserved)

    relation_paths: list[Path] = []
    relation_links: list[str] = []
    source = graph_path.relative_to(root).as_posix()
    for relation in NAV_RELATIONS:
        edges = relation_edges.get(relation, [])
        if edge_types[relation] == 0:
            continue
        relation_path = _confined_path(root, output / "relations" / f"{_slug(relation)}.md")
        relation_paths.append(relation_path)
        relation_links.append(
            f"- [[projects/{project}/obsidian/relations/{_slug(relation)}|{relation}]] — "
            f"{len(edges)} arêtes résolues uniques sur {edge_types[relation]} brutes"
        )
        involved_areas = sorted(
            {
                area
                for edge in edges
                for node in edge
                if (area := _area(str(node.get("source_file") or ""))) in area_slugs
            },
            key=lambda area: (area.casefold(), area),
        )
        ranked_edges = sorted(
            edges,
            key=lambda edge: (
                -(degree[str(edge[0].get("id"))] + degree[str(edge[1].get("id"))]),
                str(edge[0].get("label") or edge[0].get("id") or "").casefold(),
                str(edge[0].get("label") or edge[0].get("id") or ""),
                str(edge[1].get("label") or edge[1].get("id") or "").casefold(),
                str(edge[1].get("label") or edge[1].get("id") or ""),
                str(edge[0].get("id") or ""),
                str(edge[1].get("id") or ""),
            ),
        )
        edge_rows = []
        for source_node, target_node in ranked_edges[:top_symbols]:
            source_label = _md_text(source_node.get("label") or source_node.get("id") or "?")
            target_label = _md_text(target_node.get("label") or target_node.get("id") or "?")
            source_file = _md_text(source_node.get("source_file") or "-")
            target_file = _md_text(target_node.get("source_file") or "-")
            edge_rows.append(f"| {source_label} | {source_file} | {target_label} | {target_file} |")
        zone_lines = [
            f"- [[projects/{project}/obsidian/areas/{area_slugs[area]}|{_md_text(area)}]]"
            for area in involved_areas
        ] or ["- Aucune zone sélectionnée ne possède deux extrémités résolues pour cette relation."]
        edge_section = (
            ["| Source | Fichier source | Cible | Fichier cible |", "|---|---|---|---|", *edge_rows]
            if edge_rows
            else ["Aucune arête à deux extrémités résolues ; le compteur brut reste conservé."]
        )
        relation_body = [
            _frontmatter(
                f"rig/graph/{project}/relation/{_slug(relation)}",
                source,
                ["rig", "graphify", "code-map", "relation", _slug(RELATION_GLOSS[relation][0])],
                reviewer,
                generated_on,
            ),
            "",
            f"# {project} — relation {relation} ({RELATION_GLOSS[relation][0]})",
            "",
            f"Retour : [[{project_link}|{project}]] · [[knowledge/_maps/rig-code-graphes|Graphes code RIG]]",
            "",
            f"Cette carte répond à : **{RELATION_GLOSS[relation][1]}** "
            f"({RELATION_GLOSS[relation][0]}, relation Graphify `{relation}`).",
            "",
            f"Relation Graphify explicite : `{relation}` — **{len(edges)} arête"
            f"{'s' if len(edges) != 1 else ''} résolue{'s' if len(edges) != 1 else ''} unique"
            f"{'s' if len(edges) != 1 else ''} sur {edge_types[relation]} arête"
            f"{'s' if edge_types[relation] != 1 else ''} brute{'s' if edge_types[relation] != 1 else ''}**.",
            "Une arête résolue a ses deux extrémités présentes dans `nodes`; les autres restent comptées mais ne sont pas inventées dans le tableau.",
            "Cette carte décrit uniquement des arêtes Graphify tracées ; elle ne leur attribue aucune origine AST implicite ni portée métier ou runtime.",
            "",
            "## Zones sélectionnées touchées",
            "",
            *zone_lines,
            "",
            f"## Arêtes principales — sélection bornée à {top_symbols}",
            "",
            *edge_section,
        ]
        _write_generated(root, relation_path, "\n".join(relation_body), previous, preserved)

    commit = graph.get("built_at_commit") or "historique — SHA absent du snapshot"
    area_links = [
        f"- [[projects/{project}/obsidian/areas/{area_slugs[area]}|{_md_text(area)}]] — {len(area_nodes[area])} nœuds sourcés"
        for area in selected
    ]
    ordered_edges = sorted(
        edge_types.items(), key=lambda item: (-item[1], item[0].casefold(), item[0])
    )
    edge_summary = ", ".join(f"{_md_text(kind)}={count}" for kind, count in ordered_edges)
    project_body = [
        _frontmatter(
            f"rig/graph/{project}",
            source,
            ["rig", "graphify", "code-map", "project"],
            reviewer,
            generated_on,
        ),
        "",
        f"# Graphe code — {project}",
        "",
        "[[knowledge/_maps/rig-code-graphes|← Carte des graphes code RIG]]",
        "",
        "## Provenance et métriques",
        "",
        f"- Snapshot : {_md_text(source)}",
        f"- Commit déclaré : {_md_text(commit)}",
        f"- Graphe : **{len(nodes)} nœuds**, **{len(links)} arêtes**",
        f"- Relations : {edge_summary or 'aucune'}",
        f"- Navigation bornée : {len(selected)} zones sur {len(area_nodes)}",
        "",
        "## Zones principales",
        "",
        *area_links,
        "",
        "## Relations structurelles tracées",
        "",
        *relation_links,
        "",
        "> Cette carte expose une sélection navigable. Le JSON reste l’autorité exhaustive ; aucune couverture métier ou runtime n’est déduite du seul AST.",
    ]
    _write_generated(
        root,
        _confined_path(root, output / f"{project}.md"),
        "\n".join(project_body),
        previous,
        preserved,
    )
    return project, area_paths, relation_paths


def build_navigation(
    root: Path, reviewer: str, max_areas: int = 12, top_symbols: int = 15
) -> dict[str, int]:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:_.-]{0,63}", reviewer) is None:
        raise ValueError("reviewer must be a stable identifier")
    if max_areas <= 0 or top_symbols <= 0:
        raise ValueError("max_areas and top_symbols must be positive")
    root = root.resolve()
    manifest_path = _confined_path(
        root, Path("knowledge/_generated/rig-graph-navigation-manifest.json")
    )
    previous: dict[str, str] = {}
    manifest_data: dict[str, object] = {}
    if manifest_path.exists():
        loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise RuntimeError("manifest root must be an object")
        manifest_data = loaded
        manifest_files = manifest_data.get("files", {})
        if not isinstance(manifest_files, dict):
            raise RuntimeError("manifest files must be a hash mapping")
        previous = {str(path): str(digest) for path, digest in manifest_files.items()}
    generated_on_value = manifest_data.get("generated_on")
    if generated_on_value is not None:
        generated_on = str(generated_on_value)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", generated_on) is None:
            raise RuntimeError("manifest generated_on must be YYYY-MM-DD")
    else:
        central_relative = "knowledge/_maps/rig-code-graphes.md"
        central_existing = _confined_path(root, Path(central_relative))
        match = None
        if _looks_generated(central_existing, central_relative):
            match = re.search(r"^created: (\d{4}-\d{2}-\d{2})$", central_existing.read_text(encoding="utf-8"), re.MULTILINE)
        generated_on = match.group(1) if match else date.today().isoformat()
    graph_paths = sorted(root.glob("projects/rig-*/graphify-out/graph.json"))
    projects: list[tuple[str, int]] = []
    generated: set[str] = set()
    total_areas = 0
    total_relations = 0
    preserved: set[str] = set()
    for graph_path in graph_paths:
        project, areas, relations = _project_map(
            root, graph_path, reviewer, max_areas, top_symbols, generated_on, previous, preserved
        )
        projects.append((project, len(areas)))
        generated.add(f"projects/{project}/obsidian/{project}.md")
        generated.update(path.relative_to(root).as_posix() for path in areas)
        generated.update(path.relative_to(root).as_posix() for path in relations)
        total_areas += len(areas)
        total_relations += len(relations)

    links = [
        f"- [[projects/{project}/obsidian/{project}|{project}]] — {areas} zones sélectionnées"
        for project, areas in projects
    ]
    central = [
        _frontmatter(
            "rig/map/code-graphes",
            "projects/*/graphify-out/graph.json",
            ["rig", "graphify", "code-map", "map"],
            reviewer,
            generated_on,
            moc=None,
        ),
        "",
        "# Graphes code RIG",
        "",
        "[[knowledge/_maps/rig|← Carte RIG]] · [[knowledge/domain/rig-brain-carte-navigation|Guide de navigation RIG]]",
        "",
        f"Cette carte rend {len(projects)} snapshots Graphify visibles dans Obsidian sans transformer les dizaines de milliers de symboles en autant de notes.",
        "La sélection est déterministe : zones de premier niveau les plus peuplées, puis symboles non-Designer les plus connectés.",
        f"Elle ajoute {total_relations} cartes de relations structurelles explicites (`calls`, `references`, `imports`, `inherits`, `implements`) sans leur prêter une sémantique métier.",
        "",
        "## Modules",
        "",
        *links,
        "",
        "## Limites",
        "",
        "- Les snapshots historiques sans SHA restent signalés comme tels.",
        "- Le VB6 n’est pas couvert sémantiquement par ces graphes C#.",
        "- Les JSON Graphify restent les artefacts exhaustifs ; ces notes sont une couche de navigation bornée.",
    ]
    central_path = _confined_path(root, Path("knowledge/_maps/rig-code-graphes.md"))
    _write_generated(root, central_path, "\n".join(central), previous, preserved)
    generated.add(central_path.relative_to(root).as_posix())

    for relative in sorted(set(previous) - generated):
        if not _allowed_generated_path(relative):
            continue
        stale = _confined_path(root, Path(relative))
        if not _looks_generated(stale, relative):
            continue
        digest = hashlib.sha256(stale.read_bytes()).hexdigest()
        if digest == previous[relative]:
            stale.unlink()

    current = {}
    for relative in sorted(generated):
        if relative in preserved:
            current[relative] = previous[relative]
        else:
            current[relative] = hashlib.sha256(
                _confined_path(root, Path(relative)).read_bytes()
            ).hexdigest()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps({"generated_on": generated_on, "files": current}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "projects": len(projects),
        "area_maps": total_areas,
        "relation_maps": total_relations,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--max-areas", type=int, default=12)
    parser.add_argument("--top-symbols", type=int, default=15)
    args = parser.parse_args()
    print(json.dumps(build_navigation(args.root, args.reviewer, args.max_areas, args.top_symbols), ensure_ascii=False))


if __name__ == "__main__":
    main()
