#!/usr/bin/env python3
"""Warm hybrid retriever shared by the CLI and local hook service."""
import hashlib
import importlib.metadata
import json
import re
import threading
import time
from pathlib import Path

import numpy as np

from brain_index import INDEX_FORMAT_VERSION, canonical_source_roots

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def current_embedding_signature():
    return {"model": MODEL, "fastembed": importlib.metadata.version("fastembed")}


def bm25_metadata_document(metadata):
    path_words = re.sub(r"[^0-9A-Za-zÀ-ÿ]+", " ", str(metadata.get("path", ""))).strip().lower()
    labels = " ".join(
        str(metadata.get(key, ""))
        for key in ("title", "uid", "tags", "type", "kind", "scope")
    )
    return f"{path_words} {labels}"


def bm25_document(metadata, body):
    return f"{bm25_metadata_document(metadata)}\n{body}"


def bm25_candidate_count(k, total):
    """Keep enough lexical candidates for path-level deduplication and fusion."""
    return min(max(k * 10, 50), total)


# RRF's classic k0=60 (Cormack 2009) was tuned to fuse MANY independent TREC systems. query()
# builds FIVE rank lists from three sources — dense ×1, BM25 body ×2, BM25 metadata ×2 (the
# duplication is a deliberate weight) — so k0=60 flattens them: being 1st vs 4th on a list is
# 1/61 vs 1/64, a 5% gap that the dense list overturns on noise.
# Measured 2026-08-04 over the 29 positive golden cases (IN-SAMPLE: the same 29 cases chose the
# value and reported the gain — no held-out set), MRR by k0:
#   60 -> 0.7529 (previous) | 30 -> 0.7672 | 15 -> 0.7787 | 10 -> 0.7759
#    8 -> 0.7787 |  6 -> 0.7845 |  4 -> 0.7845 | 20 -> 0.7845
#    3 -> 0.7914 but k0=2 and k0=4 both fall back: a knife-edge on n=29, REJECTED.
# The 4..30 band spans 0.7672..0.7845 — spread 0.017, i.e. about ONE case (a single rank 1->2
# moves MRR by ~0.017 at n=29), and it is not monotone inside. So any k0 in that band is
# equivalent within measurement noise; 20 is chosen at the top of it and far from the
# instability below 4, NOT because 0.7845 is meaningfully above 0.7787.
# COVERAGE, stated exactly: recall@5 stays 0.8276 by 1-for-1 COMPENSATION, not by invariance —
# at k=5, graph-tv-calls ENTERS the top 5 (was out) and graph-etapercs-endettement LEAVES it
# (was rank 1). Full per-case delta at k=5: 4 better (rig-navigation 2->1, rig-data-map 3->1,
# graph-ult-inherits 2->1, graph-tv-calls out->4), 1 worse, 24 unchanged. Losing a rank-1 answer
# entirely is the accepted cost. Note the ranks depend on k, since bm25_candidate_count(k, total)
# sizes the lexical pool — always state the k with the rank.
RRF_K0 = 20


def rrf(rank_lists, k0=RRF_K0):
    scores = {}
    for ranked in rank_lists:
        for rank, idx in enumerate(ranked):
            scores[idx] = scores.get(idx, 0.0) + 1.0 / (k0 + rank + 1)
    return sorted(scores, key=scores.get, reverse=True)


class BrainRetriever:
    """Coherent hot-index reader with a fail-closed freshness barrier.

    State contract (all mutations below happen under ``_lock``):
    - ``generation``/``manifest``/``_stamp`` identify the last fully loaded snapshot.
    - ``freshness is None`` means no query may be served while ``allow_unavailable`` is enabled.
    - a corpus event increments ``_corpus_epoch`` and invalidates freshness immediately; an older
      worker result is discarded when its generation, manifest identity or epoch no longer matches.
    - ``_watcher_error`` outranks a successful hash and remains blocking until every desired root is
      watched again; recovery clears it and requires a new hash before serving.
    - ``last_reload_error`` exposes the current blocker. A new coherent generation is loaded before
      freshness is evaluated, so a manifest may remove a now-obsolete watched root.

    ``allow_unavailable`` is server-mode compatibility: construction may succeed without a usable
    index so ``/health`` can explain the failure. It does not allow queries during an unproven or
    failed freshness state; those remain deliberately fail-closed.
    """
    def __init__(
        self, index_dir, embedder=None, enable_bm25=True, expected_embedding_signature=None,
        allow_unavailable=False, brain_root=None, freshness_interval=300.0,
        watcher_factory=None,
    ):
        self.index = Path(index_dir)
        if embedder is None:
            from fastembed import TextEmbedding
            embedder = TextEmbedding(model_name=MODEL)
            expected_embedding_signature = current_embedding_signature()
        self.embedder = embedder
        self.expected_embedding_signature = expected_embedding_signature
        self.enable_bm25 = enable_bm25
        self.allow_unavailable = allow_unavailable
        self.brain_root = Path(brain_root).resolve() if brain_root is not None else None
        self.freshness_interval = max(float(freshness_interval), 0.0)
        self._freshness_checked_at = 0.0
        self._freshness_checking = False
        self._freshness_thread = None
        self._corpus_epoch = 0
        self._watcher = None
        self._index_watcher = None
        self._watcher_error = None
        self._watcher_factory = watcher_factory
        self._index_event = threading.Event()
        self._closed = threading.Event()
        self._index_monitor_thread = None
        self._reload_checking = False
        self._reload_thread = None
        self._observed_generation = None
        self.freshness = None
        self._lock = threading.Lock()
        self._stamp = None
        self.meta, self.bodies = [], []
        self.vecs = np.empty((0, 0), dtype=np.float32)
        self.bm25, self.bm25_meta = None, None
        self.generation, self.source_roots, self.manifest = None, ["knowledge"], None
        self.last_reload_error = self._watcher_error
        try:
            self._load_index()
            self._observed_generation = self.generation
        except ValueError as exc:
            if not allow_unavailable:
                raise
            self.last_reload_error = str(exc)
        if self.allow_unavailable and self.brain_root is not None:
            with self._lock:
                if self.manifest is not None:
                    self._ensure_watcher()
                    if self.freshness_interval > 0:
                        self._start_freshness_check()
                self._start_index_monitor()

    def _snapshot(self):
        pointer = self.index / "CURRENT"
        if not pointer.exists():
            return self.index, None
        generation = pointer.read_text(encoding="ascii").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", generation):
            raise ValueError("invalid index generation pointer")
        generations = (self.index / "generations").resolve()
        snapshot = (generations / generation).resolve()
        try:
            snapshot.relative_to(generations)
        except ValueError as exc:
            raise ValueError("index generation escapes generations/") from exc
        return snapshot, generation

    @staticmethod
    def _sha256(path):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _current_stamp(self):
        snapshot, generation = self._snapshot()
        names = ["meta.jsonl", "emb.npy", "bodies.jsonl"]
        if generation is not None:
            names.append("manifest.json")
        file_stamps = tuple(
            (snapshot / name).stat().st_mtime_ns if (snapshot / name).exists() else 0
            for name in names
        )
        pointer = self.index / "CURRENT"
        pointer_stamp = pointer.stat().st_mtime_ns if pointer.exists() else 0
        return generation, pointer_stamp, file_stamps

    def _load_index(self):
        snapshot, generation = self._snapshot()
        meta_path = snapshot / "meta.jsonl"
        body_path = snapshot / "bodies.jsonl"
        vec_path = snapshot / "emb.npy"
        paths = (meta_path, vec_path, body_path)
        if not all(path.exists() for path in paths):
            if self._stamp is None and generation is None and not any(path.exists() for path in paths):
                self.meta, self.bodies = [], []
                self.vecs = np.empty((0, 0), dtype=np.float32)
                self.bm25, self.bm25_meta = None, None
                self.generation, self.source_roots, self.manifest = None, ["knowledge"], None
                self._stamp = self._current_stamp()
                return
            raise ValueError("index snapshot is incomplete")

        stamp_before = self._current_stamp()
        manifest = None
        if generation is not None:
            manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
            if manifest.get("generation") != generation:
                raise ValueError("index manifest generation mismatch")
            if (
                self.expected_embedding_signature is not None
                and manifest.get("embedding_signature") != self.expected_embedding_signature
            ):
                raise ValueError("index embedding signature mismatch; rebuild the index")
            stored_format = manifest.get("index_format_signature")
            if not isinstance(stored_format, dict) or stored_format.get("version") != INDEX_FORMAT_VERSION:
                raise ValueError("index format signature mismatch; rebuild the index")
            if self.brain_root is not None and (
                not self.allow_unavailable or self.freshness_interval == 0
            ):
                self._check_freshness(manifest)
            expected_hashes = manifest.get("sha256")
            if not isinstance(expected_hashes, dict):
                raise ValueError("index manifest hashes are missing")
            for path in paths:
                expected = expected_hashes.get(path.name)
                if not isinstance(expected, str) or self._sha256(path) != expected:
                    raise ValueError(f"index hash mismatch: {path.name}")

        meta = [json.loads(line) for line in meta_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        bodies = [json.loads(line) for line in body_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        vecs = np.load(vec_path, allow_pickle=False)
        stamp_after = self._current_stamp()
        if stamp_before != stamp_after:
            raise ValueError("index changed while loading")
        if not (len(meta) == len(bodies) == len(vecs)):
            raise ValueError("index files have inconsistent row counts")
        if manifest is not None and manifest.get("rows") != len(meta):
            raise ValueError("index manifest row count mismatch")

        valid_rows = [
            idx
            for idx, (item, body) in enumerate(zip(meta, bodies))
            if isinstance(item, dict) and isinstance(body, str)
        ]
        meta = [meta[idx] for idx in valid_rows]
        bodies = [bodies[idx] for idx in valid_rows]
        vecs = vecs[np.asarray(valid_rows, dtype=np.intp)]
        declared_roots = manifest.get("source_roots") if manifest is not None else None
        source_roots = canonical_source_roots(declared_roots, brain_root=self.brain_root)

        bm25 = None
        bm25_meta = None
        if self.enable_bm25 and bodies:
            try:
                import bm25s
                bm25 = bm25s.BM25()
                bm25.index(bm25s.tokenize([
                    bm25_document(item, body) for item, body in zip(meta, bodies)
                ]))
                bm25_meta = bm25s.BM25()
                bm25_meta.index(bm25s.tokenize([
                    bm25_metadata_document(item) for item in meta
                ]))
            except Exception:
                bm25, bm25_meta = None, None
        self.meta, self.bodies, self.vecs = meta, bodies, vecs
        self.bm25, self.bm25_meta = bm25, bm25_meta
        self.generation = generation
        self.source_roots = source_roots
        self.manifest = manifest
        self._stamp = stamp_after
        self.last_reload_error = self._watcher_error
        if self.brain_root is not None and self.allow_unavailable and self.freshness_interval > 0:
            self.freshness = None
            self._ensure_watcher()

    def _evaluate_freshness(self, manifest):
        # Imported lazily to avoid a module cycle: brain_eval uses BrainRetriever for scoring.
        from brain_eval import index_freshness
        return index_freshness(manifest, self.brain_root / "knowledge")

    @staticmethod
    def _freshness_error(freshness):
        if freshness.get("fresh"):
            return None
        reasons = "; ".join(freshness.get("reasons") or ["unknown freshness failure"])
        failed_axes = ", ".join(
            f"{axis}=false"
            for axis in ("content_fresh", "format_fresh", "roots_fresh")
            if freshness.get(axis) is False
        )
        return f"index freshness mismatch ({failed_axes}): {reasons}"

    def _check_freshness(self, manifest=None):
        if self.brain_root is None:
            return None
        manifest = self.manifest if manifest is None else manifest
        if manifest is None:
            raise ValueError("index freshness mismatch: manifest missing")
        freshness = self._evaluate_freshness(manifest)
        self._freshness_checked_at = time.monotonic()
        self.freshness = freshness
        error = self._freshness_error(freshness)
        if error:
            raise ValueError(error)
        self.last_reload_error = self._watcher_error
        return freshness

    def _on_corpus_change(self, error=None):
        with self._lock:
            self._corpus_epoch += 1
            self.freshness = None
            self._freshness_checked_at = 0.0
            if error:
                self._watcher_error = f"index freshness watcher error: {error}"
                self.last_reload_error = self._watcher_error

    def _check_watcher_health(self):
        if self._watcher is None or not hasattr(self._watcher, "healthy"):
            return
        if not self._watcher.healthy:
            self._watcher_error = "index freshness watcher is not active for every source root"
            self.last_reload_error = self._watcher_error
            self.freshness = None
        elif self._watcher_error:
            self._watcher_error = None
            self.last_reload_error = None
            self.freshness = None
            self._freshness_checked_at = 0.0

    def _ensure_watcher(self):
        if self.brain_root is None or self.manifest is None:
            return
        if self._watcher is None:
            factory = self._watcher_factory
            if factory is None:
                from brain_watch import WindowsCorpusWatcher
                factory = WindowsCorpusWatcher
            self._watcher = factory(self.brain_root, self.source_roots, self._on_corpus_change)
        elif hasattr(self._watcher, "reconcile_roots"):
            self._watcher.reconcile_roots(self.source_roots)
        elif hasattr(self._watcher, "add_roots"):
            self._watcher.add_roots(self.source_roots)
        if hasattr(self._watcher, "add_discovery_roots"):
            self._watcher.add_discovery_roots(["projects"])

    def _on_index_change(self, _error=None):
        self._index_event.set()

    def _start_index_monitor(self):
        """Watch/poll CURRENT outside request threads; snapshot construction stays off `_lock`."""
        if self._index_monitor_thread is not None or self.brain_root is None:
            return
        if self._watcher_factory is None:
            try:
                relative_index = self.index.resolve().relative_to(self.brain_root).as_posix()
                from brain_watch import WindowsCorpusWatcher
                self._index_watcher = WindowsCorpusWatcher(
                    self.brain_root, [relative_index], self._on_index_change,
                )
            except (OSError, ValueError):
                self._index_watcher = None  # Polling below remains the fallback.
        self._index_monitor_thread = threading.Thread(
            target=self._index_monitor_loop,
            daemon=True,
            name="amitel-brain-index-monitor",
        )
        self._index_monitor_thread.start()

    def _index_monitor_loop(self):
        while not self._closed.is_set():
            self._index_event.wait(0.25)
            self._index_event.clear()
            if self._closed.is_set():
                return
            try:
                current = self._current_generation()
            except (OSError, ValueError):
                continue
            with self._lock:
                if self._closed.is_set():
                    return
                self._observed_generation = current
                if current != self.generation and not self._reload_checking:
                    self._reload_checking = True
                    self.freshness = None
                    self.last_reload_error = "index reload in progress"
                    self._reload_thread = threading.Thread(
                        target=self._reload_worker,
                        args=(current,),
                        daemon=True,
                        name="amitel-brain-index-reload",
                    )
                    self._reload_thread.start()

    def _reload_worker(self, expected_generation):
        try:
            replacement = BrainRetriever(
                self.index,
                embedder=self.embedder,
                enable_bm25=self.enable_bm25,
                expected_embedding_signature=self.expected_embedding_signature,
            )
            # Loading validates files off-lock; confinement is checked without hashing the SMB
            # corpus.  After adoption the armed corpus watcher and one freshness worker provide
            # the single authoritative content proof for this generation.
            replacement.source_roots = canonical_source_roots(
                replacement.source_roots, brain_root=self.brain_root,
            )
            latest = self._current_generation()
            if (
                self._closed.is_set() or latest != expected_generation
                or replacement.generation != expected_generation
            ):
                self._index_event.set()
                return
            with self._lock:
                if self._closed.is_set():
                    return
                self.meta, self.bodies, self.vecs = (
                    replacement.meta, replacement.bodies, replacement.vecs,
                )
                self.bm25, self.bm25_meta = replacement.bm25, replacement.bm25_meta
                self.generation = replacement.generation
                self.source_roots = list(replacement.source_roots)
                self.manifest = replacement.manifest
                self._stamp = replacement._stamp
                self.freshness = None
                self._freshness_checked_at = 0.0
                self.last_reload_error = self._watcher_error
                self._ensure_watcher()
                self._start_freshness_check()
        except (OSError, EOFError, ValueError) as exc:
            with self._lock:
                self.last_reload_error = str(exc)
                self.freshness = None
        finally:
            with self._lock:
                self._reload_checking = False

    def close(self):
        """Stop every worker and release native handles before returning."""
        self._closed.set()
        self._index_event.set()
        for watcher in (self._watcher, self._index_watcher):
            close = getattr(watcher, "close", None)
            if close is not None:
                close()
        if self._index_monitor_thread is not None:
            self._index_monitor_thread.join()
        for thread in (self._reload_thread, self._freshness_thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join()

    def _freshness_worker(self, manifest, generation, epoch):
        try:
            freshness = self._evaluate_freshness(manifest)
            error = self._freshness_error(freshness)
        except Exception as exc:
            freshness, error = None, str(exc)
        with self._lock:
            if self._closed.is_set():
                self._freshness_checking = False
                return
            if (
                self.generation == generation and self.manifest is manifest
                and self._corpus_epoch == epoch
            ):
                self._freshness_checked_at = time.monotonic()
                if freshness is not None:
                    self.freshness = freshness
                self.last_reload_error = self._watcher_error or error
            self._freshness_checking = False

    def _start_freshness_check(self, *, blocking=True):
        if (
            self._closed.is_set() or self._freshness_checking
            or self.brain_root is None or self.manifest is None
        ):
            return
        self._freshness_checking = True
        if blocking:
            self.freshness = None
        self._freshness_thread = threading.Thread(
            target=self._freshness_worker,
            args=(self.manifest, self.generation, self._corpus_epoch),
            daemon=True,
            name="amitel-brain-freshness",
        )
        self._freshness_thread.start()

    def _current_generation(self):
        return self._snapshot()[1]

    def _generation_observation(self):
        try:
            return self._current_generation(), None
        except (OSError, ValueError) as exc:
            return None, str(exc)

    def status(self) -> dict:
        observed_generation, observation_error = (
            self._generation_observation() if self.allow_unavailable else (self.generation, None)
        )
        with self._lock:
            self._observed_generation = observed_generation
            if self.allow_unavailable:
                try:
                    if observation_error:
                        self.freshness = None
                        self.last_reload_error = observation_error
                    self._check_watcher_health()
                    if (
                        not observation_error and observed_generation != self.generation
                        and not (self.generation is None and self.last_reload_error)
                    ):
                        self.freshness = None
                        self.last_reload_error = "index reload in progress"
                        self._index_event.set()
                    generation_is_current = (
                        not observation_error and observed_generation == self.generation
                        and not self._reload_checking
                    )
                    if self.brain_root is not None and generation_is_current:
                        if self.freshness_interval == 0:
                            self._check_freshness()
                        elif self.freshness is None:
                            self._start_freshness_check(blocking=True)
                        elif time.monotonic() - self._freshness_checked_at >= self.freshness_interval:
                            # Native watcher events already invalidate immediately.  The periodic
                            # full hash is a fallback audit and must not create an 11s 503 window.
                            self._start_freshness_check(blocking=False)
                except (OSError, EOFError, ValueError) as exc:
                    self.last_reload_error = str(exc)
            state = "healthy"
            if self.freshness is None and self.brain_root is not None:
                state = "unavailable"
            elif self.last_reload_error:
                state = "degraded" if self.generation is not None else "unavailable"
            return {
                "state": state,
                "current_generation": self._observed_generation,
                "serving_generation": self.generation,
                "source_roots": list(self.source_roots),
                "freshness": self.freshness,
                "freshness_checking": self._freshness_checking,
                "reasons": [self.last_reload_error] if self.last_reload_error else [],
            }

    def query(self, text: str, k: int = 5) -> dict:
        if k <= 0:
            raise ValueError("k must be a positive integer")
        observed_generation, observation_error = (
            self._generation_observation() if self.allow_unavailable else (self.generation, None)
        )
        with self._lock:
            try:
                self._observed_generation = observed_generation
                if observation_error:
                    self.freshness = None
                    self.last_reload_error = observation_error
                if (
                    self.allow_unavailable and not observation_error
                    and observed_generation != self.generation
                    and not (self.generation is None and self.last_reload_error)
                ):
                    self.freshness = None
                    self.last_reload_error = "index reload in progress"
                    self._index_event.set()
                self._check_watcher_health()
                if not self.allow_unavailable and self._current_stamp() != self._stamp:
                    self._load_index()
                generation_is_current = (
                    not observation_error and observed_generation == self.generation
                    and not self._reload_checking
                )
                if self.brain_root is not None and generation_is_current:
                    if self.freshness_interval == 0:
                        self._check_freshness()
                    elif self.freshness is None:
                        self._start_freshness_check(blocking=True)
                    elif time.monotonic() - self._freshness_checked_at >= self.freshness_interval:
                        self._start_freshness_check(blocking=False)
            except (OSError, EOFError):
                pass  # Keep the last coherent snapshot during an SMB rewrite.
            except ValueError as exc:
                error = str(exc)
                if "signature mismatch" in error or "freshness mismatch" in error:
                    self.last_reload_error = error
                    if self.allow_unavailable:
                        raise RuntimeError(error) from exc
                    raise
                # A partial/corrupt publish may be transient: retry next query.
            if self.last_reload_error and self.allow_unavailable:
                raise RuntimeError(self.last_reload_error)
            if self.freshness is None and self.brain_root is not None and self.allow_unavailable:
                raise RuntimeError("index freshness check in progress")
            if not self.meta:
                return {
                    "query": text, "hits": [], "axes": 0,
                    "generation": self.generation, "source_roots": list(self.source_roots),
                }
            # Immutable-by-replacement snapshot: reload never mutates these objects after publish.
            meta, bodies, vecs = self.meta, self.bodies, self.vecs
            bm25, bm25_meta = self.bm25, self.bm25_meta
            generation, source_roots = self.generation, list(self.source_roots)

        # Embedding and ranking are CPU-heavy and safe on captured read-only objects; keeping them
        # outside `_lock` lets independent harness requests overlap.
        qv = np.asarray(list(self.embedder.embed([text]))[0], dtype=np.float32)
        qv /= np.linalg.norm(qv) + 1e-12
        dense_scores = vecs @ qv
        dense_rank = [int(i) for i in np.argsort(-dense_scores)]
        ranks = [dense_rank]
        if bm25 is not None:
            try:
                import bm25s
                candidate_count = bm25_candidate_count(k, len(bodies))
                rows, _ = bm25.retrieve(bm25s.tokenize([text]), k=candidate_count)
                bm25_rank = [int(i) for i in rows[0]]
                ranks.extend([bm25_rank, bm25_rank])
            except Exception:
                pass
        if bm25_meta is not None:
            try:
                import bm25s
                rows, _ = bm25_meta.retrieve(
                    bm25s.tokenize([text]), k=bm25_candidate_count(k, len(bodies)),
                )
                metadata_rank = [int(i) for i in rows[0]]
                ranks.extend([metadata_rank, metadata_rank])
            except Exception:
                pass
        fused = rrf(ranks)
        selected = []
        seen_paths = set()
        for idx in fused:
            path = meta[idx].get("path")
            path_key = path if isinstance(path, str) else ("row", idx)
            if path_key in seen_paths:
                continue
            seen_paths.add(path_key)
            selected.append(idx)
            if len(selected) == k:
                break
        hits = [
            {"rank": rank + 1, **meta[idx], "dense_cos": round(float(dense_scores[idx]), 4)}
            for rank, idx in enumerate(selected)
        ]
        return {
            "query": text, "hits": hits, "axes": len(ranks),
            "generation": generation, "source_roots": source_roots,
        }
