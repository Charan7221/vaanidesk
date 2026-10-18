"""Keyword ranking (BM25), vector similarity and hybrid fusion. Pure Python + NumPy."""
from __future__ import annotations

import math
import re
from collections import Counter

import numpy as np

_STOP = set("""a an and are as at be by can do does for from has have how i if in is it its me my of on or our
please the their there this to was we what when where which who will with you your am any about""".split())


def tokenize(text: str) -> list[str]:
    words = re.findall(r"[^\W_]+", text.lower())
    return [w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words if w not in _STOP]


class BM25:
    """Okapi BM25 over a small in-memory corpus (a clinic has hundreds of chunks, not millions)."""

    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tokens = [tokenize(d) for d in docs]
        self.tf = [Counter(t) for t in self.tokens]
        self.avgdl = (sum(len(t) for t in self.tokens) / len(self.tokens)) if self.tokens else 0.0
        df = Counter(w for t in self.tokens for w in set(t))
        n = len(self.tokens)
        self.idf = {w: math.log(1 + (n - f + 0.5) / (f + 0.5)) for w, f in df.items()}

    def scores(self, query: str) -> list[float]:
        q = tokenize(query)
        out = []
        for tf, toks in zip(self.tf, self.tokens, strict=True):
            norm = self.k1 * (1 - self.b + self.b * len(toks) / (self.avgdl or 1))
            out.append(sum(self.idf.get(w, 0.0) * tf[w] * (self.k1 + 1) / (tf[w] + norm) for w in q if w in tf))
        return out


def cosine(matrix: np.ndarray, query: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1) * (np.linalg.norm(query) or 1.0)
    return (matrix @ query) / np.where(norms == 0, 1.0, norms)


def rrf(rankings: list[list[int]], k: int = 60) -> dict[int, float]:
    """Reciprocal Rank Fusion: combine ranked lists without having to calibrate their scores."""
    fused: dict[int, float] = {}
    for ranking in rankings:
        for rank, idx in enumerate(ranking):
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (k + rank + 1)
    return fused
