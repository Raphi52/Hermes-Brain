#!/usr/bin/env python3
"""Warm hybrid retriever shared by the CLI and local hook service."""
import hashlib
import json
import re
import threading
from pathlib import Path

import numpy as np

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def rrf(rank_lists, k0=60):
    scores = {}
    for ranked in rank_lists:
        for rank, idx in enumerate(ranked):
            scores[idx] = scores.get(idx, 0.0) + 1.0 / (k0 + rank + 1)
    return sorted(scores, key=scores.get, reverse=True)


class BrainRetriever:
    def __init__(self, index_dir, embedder=None, enable_bm25=True):
        self.index = Path(index_dir)
        if embedder is None:
            from fastembed import TextEmbedding
            embedder = TextEmbedding(model_name=MODEL)
        self.embedder = embedder
        self.enable_bm25 = enable_bm25
        self._lock = threading.Lock()
        self._stamp = None
        self._load_index()

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
                self.bm25 = None
                self._stamp = self._current_stamp()
                return
            raise ValueError("index snapshot is incomplete")

        stamp_before = self._current_stamp()
        manifest = None
        if generation is not None:
            manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
            if manifest.get("generation") != generation:
                raise ValueError("index manifest generation mismatch")
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

        bm25 = None
        if self.enable_bm25 and bodies:
            try:
                import bm25s
                bm25 = bm25s.BM25()
                bm25.index(bm25s.tokenize(bodies))
            except Exception:
                bm25 = None
        self.meta, self.bodies, self.vecs, self.bm25 = meta, bodies, vecs, bm25
        self._stamp = stamp_after

    def query(self, text: str, k: int = 5) -> dict:
        if k <= 0:
            raise ValueError("k must be a positive integer")
        with self._lock:
            if self._current_stamp() != self._stamp:
                try:
                    self._load_index()
                except (OSError, ValueError, EOFError):
                    pass  # Keep the last coherent snapshot during an SMB rewrite.
            if not self.meta:
                return {"query": text, "hits": [], "axes": 0}
            qv = np.asarray(list(self.embedder.embed([text]))[0], dtype=np.float32)
            qv /= np.linalg.norm(qv) + 1e-12
            dense_scores = self.vecs @ qv
            dense_rank = [int(i) for i in np.argsort(-dense_scores)]
            ranks = [dense_rank]
            if self.bm25 is not None:
                try:
                    import bm25s
                    rows, _ = self.bm25.retrieve(bm25s.tokenize([text]), k=min(k * 3, len(self.bodies)))
                    ranks.append([int(i) for i in rows[0]])
                except Exception:
                    pass
            fused = rrf(ranks)[:k]
            hits = [
                {"rank": rank + 1, **self.meta[idx], "dense_cos": round(float(dense_scores[idx]), 4)}
                for rank, idx in enumerate(fused)
            ]
            return {"query": text, "hits": hits, "axes": len(ranks)}
