"""TF-IDF disperso en Python puro (sin numpy): vocabulario acotado y vectores normalizados."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable

Vector = dict[str, float]


class TfIdf:
    def __init__(self, max_features: int = 20000, min_df: int = 1, max_df_ratio: float = 1.0):
        self.max_features = max_features
        self.min_df = min_df
        self.max_df_ratio = max_df_ratio
        self.idf: dict[str, float] = {}
        self.df: Counter[str] = Counter()
        self.n_docs = 0

    def fit(self, docs: Iterable[list[str]]) -> TfIdf:
        df: Counter[str] = Counter()
        n = 0
        for feats in docs:
            n += 1
            df.update(set(feats))
        max_df = max(1, int(self.max_df_ratio * n)) if self.max_df_ratio < 1 else n
        kept = [(t, c) for t, c in df.items() if self.min_df <= c <= max_df]
        # Vocabulario acotado: se conservan los términos más frecuentes.
        kept.sort(key=lambda tc: (-tc[1], tc[0]))
        kept = kept[: self.max_features]
        self.n_docs = n
        self.df = Counter(dict(kept))
        self.idf = {t: math.log((1 + n) / (1 + c)) + 1.0 for t, c in kept}
        return self

    def transform(self, feats: list[str]) -> Vector:
        tf = Counter(f for f in feats if f in self.idf)
        vec = {t: (1 + math.log(c)) * self.idf[t] for t, c in tf.items()}
        norm = math.sqrt(sum(v * v for v in vec.values()))
        return {t: v / norm for t, v in vec.items()} if norm else {}


def cosine(a: Vector, b: Vector) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(t, 0.0) for t, v in a.items())


def top_shared(a: Vector, b: Vector, n: int = 8) -> list[str]:
    """Términos que más aportan a la similitud entre dos vectores."""
    contrib = [(t, a[t] * b[t]) for t in a.keys() & b.keys()]
    contrib.sort(key=lambda tv: -tv[1])
    return [t for t, _ in contrib[:n]]
