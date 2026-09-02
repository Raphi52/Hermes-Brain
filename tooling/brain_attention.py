"""Offline attention feedback; production ranking remains unchanged unless explicitly enabled."""
from __future__ import annotations

import json
import os
import re
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from brain_trace import local_log_path


FEEDBACK_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_FEEDBACK_LOCK = threading.Lock()


class AttentionModel:
    def __init__(self, max_adjustment=0.1):
        if not 0.0 <= float(max_adjustment) <= 0.25:
            raise ValueError("max_adjustment must be between 0 and 0.25")
        self.max_adjustment = float(max_adjustment)
        self._counts = defaultdict(lambda: [0, 0])

    def observe(self, *, used_ids=(), rejected_ids=()):
        for document_id in set(used_ids):
            if document_id:
                self._counts[str(document_id)][0] += 1
        for document_id in set(rejected_ids):
            if document_id:
                self._counts[str(document_id)][1] += 1

    def weights(self):
        result = {}
        for document_id, (used, rejected) in self._counts.items():
            total = used + rejected
            result[document_id] = round(
                self.max_adjustment * (used - rejected) / total if total else 0.0, 6
            )
        return result


def shadow_rank(hits, weights, *, enabled=False):
    if not enabled:
        return hits
    return sorted(
        list(hits),
        key=lambda hit: float(hit.get("dense_cos", 0.0))
        + float(weights.get(hit.get("uid") or hit.get("path"), 0.0)),
        reverse=True,
    )


def compare_rankings(cases, weights):
    baseline_total = 0.0
    shadow_total = 0.0
    details = []
    for case in cases:
        hits = list(case.get("hits", []))
        original_ids = [hit.get("uid") or hit.get("path") for hit in hits]
        expected = set(map(str, case.get("expected_ids", [])))
        shadow = shadow_rank(hits, weights, enabled=True)
        shadow_ids = [hit.get("uid") or hit.get("path") for hit in shadow]
        baseline_rank = next((index + 1 for index, item in enumerate(original_ids) if item in expected), None)
        shadow_rank_value = next((index + 1 for index, item in enumerate(shadow_ids) if item in expected), None)
        baseline_total += 0.0 if baseline_rank is None else 1.0 / baseline_rank
        shadow_total += 0.0 if shadow_rank_value is None else 1.0 / shadow_rank_value
        details.append({
            "id": str(case.get("id", "")),
            "baseline_rank": baseline_rank,
            "shadow_rank": shadow_rank_value,
            "changed": original_ids != shadow_ids,
        })
    count = len(details)
    return {
        "cases": count,
        "production_changed": False,
        "baseline_mrr": round(baseline_total / count, 4) if count else 1.0,
        "shadow_mrr": round(shadow_total / count, 4) if count else 1.0,
        "details": details,
    }


def _feedback_identifier(value):
    text = str(value or "").strip()
    return text if FEEDBACK_IDENTIFIER.fullmatch(text) else "unknown"


def record_feedback(
    path, *, trace_id, used_ids=(), rejected_ids=(), enabled=True,
    max_bytes=5_000_000,
):
    if not enabled or os.environ.get("AMITEL_BRAIN_FEEDBACK", "1") == "0":
        return False
    try:
        target = local_log_path(path)
    except ValueError:
        return False
    if max_bytes < 128:
        raise ValueError("max_bytes must be at least 128")
    event = {
        "schema": "amitel-brain/attention-feedback-v1",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "trace_id": _feedback_identifier(trace_id),
        "used_ids": sorted({_feedback_identifier(item) for item in used_ids}),
        "rejected_ids": sorted({_feedback_identifier(item) for item in rejected_ids}),
    }
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    encoded_size = len(line.encode("utf-8"))
    if encoded_size > max_bytes:
        raise ValueError("feedback event exceeds max_bytes")
    with _FEEDBACK_LOCK:
        target.parent.mkdir(parents=True, exist_ok=True)
        current_size = target.stat().st_size if target.exists() else 0
        if current_size and current_size + encoded_size > max_bytes:
            os.replace(target, target.with_suffix(target.suffix + ".1"))
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
    return True
