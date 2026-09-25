#!/usr/bin/env python3
"""Graphe d'entites INTERROGEABLE : « qui depend de quoi ».

Avant ce module, les liens etaient extraits (liens entre notes par `obsidian_graph.analyze`,
relations de code par `graphify-out/graph.json`) mais seulement rendus en notes Markdown que la
recherche texte retrouvait mal (`calls.md` au rang 8). Ici on les charge en memoire, dans les DEUX
sens, et on repond a une question de voisinage : dependances (sortantes) ou dependants (entrantes),
sur une profondeur bornee. Aucun second analyseur : on reutilise ceux qui existent.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

from obsidian_graph import analyze

MAX_DEPTH = 3
MAX_RESULTS = 200
NOTE_RELATION = "links_to"


@dataclass
class EntityGraph:
    # noeud -> [(voisin, relation)]
    outgoing: dict[str, list[tuple[str, str]]] = field(default_factory=lambda: defaultdict(list))
    incoming: dict[str, list[tuple[str, str]]] = field(default_factory=lambda: defaultdict(list))
    labels: dict[str, str] = field(default_factory=dict)

    def add(self, source: str, target: str, relation: str) -> None:
        self.outgoing[source].append((target, relation))
        self.incoming[target].append((source, relation))

    def nodes(self) -> set[str]:
        return set(self.outgoing) | set(self.incoming) | set(self.labels)

    def resolve(self, entity: str) -> list[str]:
        """Identifiant exact, sinon libelle ou nom de fichier exact (insensible a la casse)."""
        wanted = entity.strip().replace("\\", "/")
        nodes = self.nodes()
        if wanted in nodes:
            return [wanted]
        folded = wanted.casefold()
        matches = sorted(
            node for node in nodes
            if node.casefold() == folded
            or self.labels.get(node, "").casefold() == folded
            or Path(node).stem.casefold() == folded.removesuffix(".md")
        )
        return matches

    def query(self, entity: str, direction: str = "dependents", depth: int = 1,
              relation: str | None = None) -> dict[str, object]:
        if direction not in {"dependents", "dependencies"}:
            raise ValueError("direction must be 'dependents' or 'dependencies'")
        depth = min(max(int(depth), 1), MAX_DEPTH)
        starts = self.resolve(entity)
        if not starts:
            return {"entity": entity, "found": False, "matches": [], "results": []}
        if len(starts) > 1:
            return {"entity": entity, "found": False, "ambiguous": True,
                    "matches": starts[:20], "results": []}
        start = starts[0]
        adjacency = self.incoming if direction == "dependents" else self.outgoing
        seen = {start}
        queue = deque([(start, 0)])
        results: list[dict[str, object]] = []
        truncated = False
        while queue:
            current, level = queue.popleft()
            if level >= depth:
                continue
            for neighbor, rel in sorted(adjacency.get(current, ())):
                if relation and rel != relation:
                    continue
                if neighbor in seen:
                    continue
                seen.add(neighbor)
                if len(results) >= MAX_RESULTS:
                    truncated = True
                    break
                results.append({"entity": neighbor, "label": self.labels.get(neighbor, neighbor),
                                "relation": rel, "via": current, "depth": level + 1})
                queue.append((neighbor, level + 1))
        return {"entity": start, "found": True, "direction": direction, "depth": depth,
                "results": results, "truncated": truncated}


def _load_code_graph(graph: EntityGraph, graph_path: Path) -> None:
    """Meme format que `rig_graph_navigation._project_map` : nodes[id,label], links[source,target,relation]."""
    data = json.loads(graph_path.read_text(encoding="utf-8"))
    # Graphify nomme le FICHIER `tooling_brain_server` (libelle `brain_server.py`) mais fait pointer
    # chaque import vers l'identifiant nu `brain_server`, qui n'est meme pas un noeud : sans ce
    # rattachement, « qui depend de brain_server.py » repondait 0 (constate le 2026-09-22).
    file_of_module: dict[str, str | None] = {}
    for node in data.get("nodes", []):
        node_id = str(node.get("id") or "")
        if node_id:
            label = str(node.get("label") or node.get("name") or node_id)
            graph.labels[node_id] = label
            if label.endswith(".py"):
                stem = label[:-3]
                # Deux fichiers de meme nom : on ne devine pas lequel est importe.
                file_of_module[stem] = None if stem in file_of_module else node_id
    known = set(graph.labels)

    def canonical(node_id: str) -> str:
        if node_id in known:
            return node_id
        return file_of_module.get(node_id) or node_id

    # Graphify ecrit `links` (format networkx) dans ses anciens graphes et `edges` depuis la v0.9
    # (constate le 2026-09-22 sur brain/graphify-out/graph.json : 1 379 `edges`, aucun `links`).
    for link in data.get("links") or data.get("edges") or []:
        source = canonical(str(link.get("source") or ""))
        target = canonical(str(link.get("target") or ""))
        if source and target:
            graph.add(source, target, str(link.get("relation") or link.get("type") or "non typée"))


def build_graph(brain_root: Path, *, include_code: bool = True) -> EntityGraph:
    brain_root = Path(brain_root).resolve()
    graph = EntityGraph()
    knowledge = brain_root / "knowledge"
    if knowledge.is_dir():
        report = analyze(knowledge)
        for node in report.nodes:
            graph.labels.setdefault(f"knowledge/{node}", f"knowledge/{node}")
        for source, target in report.links:
            graph.add(f"knowledge/{source}", f"knowledge/{target}", NOTE_RELATION)
    if include_code:
        for graph_path in sorted(brain_root.glob("projects/*/graphify-out/graph.json")):
            try:
                _load_code_graph(graph, graph_path)
            except (OSError, ValueError, AttributeError):
                continue  # un graphe de projet illisible ne doit pas priver les autres
    return graph


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("entity")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--direction", default="dependents", choices=["dependents", "dependencies"])
    parser.add_argument("--depth", type=int, default=1)
    parser.add_argument("--relation")
    args = parser.parse_args()
    graph = build_graph(Path(args.root))
    print(json.dumps(graph.query(args.entity, args.direction, args.depth, args.relation),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
# fix-ok: editions successives = ajout du module puis bornage profondeur/resultats (MAX_DEPTH, MAX_RESULTS) mesure par tests/test_brain_graph.py
