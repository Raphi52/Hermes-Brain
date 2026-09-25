#!/usr/bin/env python3
"""Evaluate live Amitel retrieval against a versioned golden set."""
import argparse
import json
import math
import sys
from pathlib import Path

from brain_index import (
    canonical_source_roots,
    collect_note_paths,
    current_index_format_signature,
    discover_graph_roots,
    knowledge_fingerprint,
)
from brain_query import configure_stdout_utf8
from brain_retrieval import BrainRetriever


def active_note_paths(knowledge_root, source_roots=None):
    """The notes an index is expected to cover, per the manifest that describes it.

    Reuses brain_index.collect_note_paths so the walk cannot drift from the one that built
    the index — the previous duplicate implementation was one edit away from disagreeing.
    """
    root = Path(knowledge_root).resolve()
    if not source_roots:
        return collect_note_paths([root])[0]
    brain_root = root.parent
    return collect_note_paths([brain_root / item for item in source_roots])[0]


def index_freshness(manifest, knowledge_root):
    """Two independent ways an index goes stale: the notes moved, or the CODE moved.

    The content fingerprint alone is a false green — on 2026-08-04 it reported `fresh`
    for an index built before the relative-path wiring existed, and the whole quality
    gate sat at recall 0.0 without anyone being able to see why.
    """
    knowledge = Path(knowledge_root).resolve()
    source_roots = canonical_source_roots(
        manifest.get("source_roots"), brain_root=knowledge.parent,
    )
    actual = knowledge_fingerprint(
        active_note_paths(knowledge, source_roots), relative_to=knowledge.parent,
    )
    expected = manifest.get("knowledge_fingerprint")
    content_fresh = isinstance(expected, str) and expected == actual

    expected_format = current_index_format_signature()
    stored_format = manifest.get("index_format_signature")
    format_fresh = stored_format == expected_format

    # THIRD axis: the PERIMETER. The content fingerprint only walks the roots the manifest
    # declares, so a graph navigation layer generated for a project the index never covered
    # stays invisible — `fresh: true` while its notes are absent from the index. That is the
    # 2026-08-04 blind spot moved from the CODE axis to the PERIMETER axis.
    # Roots present in the manifest but absent from discovery are fine (an explicit --also).
    brain_root = knowledge.parent
    discovered = {
        path.relative_to(brain_root).as_posix() for path in discover_graph_roots(brain_root)
    }
    declared = set(source_roots)
    appeared = sorted(discovered - declared)
    vanished = sorted(
        item for item in declared if not (brain_root / item).is_dir()
    )
    roots_fresh = not appeared and not vanished

    reasons = []
    if not content_fresh:
        reasons.append("knowledge notes changed since the index was built")
    if appeared:
        reasons.append(f"note roots exist on disk but are not indexed: {', '.join(appeared)}")
    if vanished:
        reasons.append(f"indexed note roots no longer exist: {', '.join(vanished)}")
    if not format_fresh:
        reasons.append(
            "index built by another version of the code"
            if isinstance(stored_format, dict)
            else "index predates the format signature — rebuild to make it verifiable"
        )
    return {
        "generation": manifest.get("generation"),
        "fresh": content_fresh and format_fresh and roots_fresh,
        "content_fresh": content_fresh,
        "format_fresh": format_fresh,
        "roots_fresh": roots_fresh,
        "source_roots": list(source_roots),
        "expected": expected,
        "actual": actual,
        "expected_format": expected_format,
        "stored_format": stored_format,
        "reasons": reasons,
    }


def index_duplicate_rows(retriever):
    """Rows sharing the SAME (path, chunk_index) — a real corruption check on the index.

    `duplicate_path_violations` below cannot bite against the real retriever: `query()`
    already dedupes by path before returning hits, so the count is 0 by construction and
    proves nothing about the index. It is kept because it does guard the contract for any
    OTHER retriever implementation (the unit tests exercise it with a fake), but the index
    itself needs a check that can actually fail. Found by external audit 2026-08-04.
    """
    meta = getattr(retriever, "meta", None)
    if not meta:
        return 0
    seen = set()
    duplicates = 0
    for item in meta:
        if not isinstance(item, dict):
            continue
        key = (item.get("path"), item.get("chunk_index"))
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
    return duplicates


def evaluate_cases(retriever, cases, k=5):
    positives = []
    ndcgs = []
    negatives = []
    domain_records = {}
    details = []
    duplicate_path_violations = 0
    for case in cases:
        hits = retriever.query(case["query"], k)["hits"]
        paths = [hit.get("path") for hit in hits if isinstance(hit.get("path"), str)]
        duplicate_path_violations += len(paths) - len(set(paths))
        expected = set(case.get("expected_paths", []))
        domain = str(case.get("domain", "unspecified"))
        bucket = domain_records.setdefault(domain, {"positives": [], "ndcgs": [], "negatives": []})
        if expected:
            rank = next(
                (index + 1 for index, hit in enumerate(hits) if hit.get("path") in expected),
                None,
            )
            positives.append(rank)
            relevance = case.get("relevance")
            grades = (
                {str(path): float(grade) for path, grade in relevance.items()}
                if isinstance(relevance, dict)
                else {path: 1.0 for path in expected}
            )
            dcg = sum(
                grades.get(path, 0.0) / math.log2(index + 2)
                for index, path in enumerate(paths[:k])
            )
            ideal = sum(
                grade / math.log2(index + 2)
                for index, grade in enumerate(sorted(grades.values(), reverse=True)[:k])
            )
            ndcg = dcg / ideal if ideal else 1.0
            ndcgs.append(ndcg)
            bucket["positives"].append(rank)
            bucket["ndcgs"].append(ndcg)
            details.append({
                "id": case["id"], "domain": domain, "rank": rank,
                "ndcg_at_k": round(ndcg, 4),
                "top_path": hits[0].get("path") if hits else None,
            })
        else:
            ceiling = float(case["max_dense"])
            observed = max((float(hit.get("dense_cos", -1.0)) for hit in hits), default=-1.0)
            passed = observed <= ceiling
            negatives.append(passed)
            bucket["negatives"].append(passed)
            details.append({
                "id": case["id"], "domain": domain,
                "passed": passed, "max_dense": round(observed, 4),
            })
    positive_count = len(positives)
    domains = {}
    for domain, bucket in sorted(domain_records.items()):
        domain_positive_count = len(bucket["positives"])
        domains[domain] = {
            "positive_cases": domain_positive_count,
            "negative_cases": len(bucket["negatives"]),
            "recall_at_k": round(
                sum(rank is not None for rank in bucket["positives"]) / domain_positive_count, 4
            ) if domain_positive_count else 1.0,
            "mrr": round(
                sum(0.0 if rank is None else 1.0 / rank for rank in bucket["positives"])
                / domain_positive_count, 4
            ) if domain_positive_count else 1.0,
            "ndcg_at_k": round(sum(bucket["ndcgs"]) / domain_positive_count, 4)
            if domain_positive_count else 1.0,
            "negative_pass_rate": round(
                sum(bucket["negatives"]) / len(bucket["negatives"]), 4
            ) if bucket["negatives"] else 1.0,
        }
    return {
        "positive_cases": positive_count,
        "negative_cases": len(negatives),
        "recall_at_k": round(sum(rank is not None for rank in positives) / positive_count, 4) if positive_count else 1.0,
        "mrr": round(sum(0.0 if rank is None else 1.0 / rank for rank in positives) / positive_count, 4) if positive_count else 1.0,
        "ndcg_at_k": round(sum(ndcgs) / positive_count, 4) if positive_count else 1.0,
        "negative_pass_rate": round(sum(negatives) / len(negatives), 4) if negatives else 1.0,
        "duplicate_path_violations": duplicate_path_violations,
        "index_duplicate_rows": index_duplicate_rows(retriever),
        "domains": domains,
        "details": details,
    }


def load_manifest(index_dir):
    index = Path(index_dir)
    generation = (index / "CURRENT").read_text(encoding="ascii").strip()
    return json.loads((index / "generations" / generation / "manifest.json").read_text(encoding="utf-8"))


def main():
    configure_stdout_utf8()
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", required=True)
    parser.add_argument("--knowledge", required=True)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--min-recall", type=float, default=0.80)
    parser.add_argument("--min-mrr", type=float, default=0.50)
    parser.add_argument("--min-negative-pass", type=float, default=1.0)
    args = parser.parse_args()
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))["cases"]
    manifest = load_manifest(args.index)
    freshness = index_freshness(manifest, args.knowledge)
    metrics = evaluate_cases(BrainRetriever(args.index), cases, args.k)
    report = {"status": "valid", "freshness": freshness, **metrics}
    if (
        not freshness["fresh"]
        or metrics["recall_at_k"] < args.min_recall
        or metrics["mrr"] < args.min_mrr
        or metrics["negative_pass_rate"] < args.min_negative_pass
        or metrics["duplicate_path_violations"] != 0
        or metrics["index_duplicate_rows"] != 0
    ):
        report["status"] = "invalid"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["status"] == "valid" else 1)


if __name__ == "__main__":
    main()
