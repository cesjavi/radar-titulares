"""Agrupamiento con validación de coherencia (evita fusiones por encadenamiento).

Dos grupos se unen solo si TODOS los pares cruzados tienen una similitud mínima y la
similitud media cruzada supera un umbral: que A se parezca a B y B a C no alcanza para
juntar A con C. Las relaciones rechazadas a mano impiden unir esos artículos.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from radar.analysis.relations import GROUPING_TYPES, Corpus, Doc
from radar.analysis.vectorize import cosine

MIN_PAIR_SIM = 0.12
MIN_AVG_SIM = 0.22
MAX_GROUP_SIZE = 30


@dataclass
class Edge:
    a: int
    b: int
    score: float
    relation_type: str


@dataclass
class Cluster:
    ids: set[int]
    edges: list[Edge] = field(default_factory=list)


def _sim(corpus: Corpus, i: int, j: int, cache: dict) -> float:
    key = (i, j) if i < j else (j, i)
    if key not in cache:
        cache[key] = cosine(corpus.docs[i].word_vec, corpus.docs[j].word_vec)
    return cache[key]


def coherent_merge(corpus: Corpus, x: set[int], y: set[int], forbidden: set[tuple[int, int]],
                   cache: dict) -> tuple[bool, float, float]:
    if len(x) + len(y) > MAX_GROUP_SIZE:
        return False, 0.0, 0.0
    sims = []
    for i in x:
        for j in y:
            if (min(i, j), max(i, j)) in forbidden:
                return False, 0.0, 0.0
            sims.append(_sim(corpus, i, j, cache))
    lo, avg = min(sims), sum(sims) / len(sims)
    return lo >= MIN_PAIR_SIM and avg >= MIN_AVG_SIM, lo, avg


def cluster(corpus: Corpus, edges: list[Edge], forbidden: set[tuple[int, int]],
            excluded_ids: set[int]) -> list[Cluster]:
    """Une por aristas de mayor a menor puntaje, validando la coherencia en cada unión."""
    owner: dict[int, int] = {}
    clusters: dict[int, Cluster] = {}
    cache: dict = {}
    next_id = 0
    for e in sorted(edges, key=lambda e: -e.score):
        if e.a in excluded_ids or e.b in excluded_ids:
            continue
        ca, cb = owner.get(e.a), owner.get(e.b)
        if ca is not None and ca == cb:
            clusters[ca].edges.append(e)
            continue
        xa = clusters[ca].ids if ca is not None else {e.a}
        xb = clusters[cb].ids if cb is not None else {e.b}
        ok, _, _ = coherent_merge(corpus, xa, xb, forbidden, cache)
        if not ok:
            continue
        if ca is None and cb is None:
            cid = next_id
            next_id += 1
            clusters[cid] = Cluster({e.a, e.b}, [e])
            owner[e.a] = owner[e.b] = cid
        elif ca is not None and cb is None:
            clusters[ca].ids.add(e.b)
            clusters[ca].edges.append(e)
            owner[e.b] = ca
        elif cb is not None and ca is None:
            clusters[cb].ids.add(e.a)
            clusters[cb].edges.append(e)
            owner[e.a] = cb
        else:
            keep, drop = (ca, cb) if len(clusters[ca].ids) >= len(clusters[cb].ids) else (cb, ca)
            clusters[keep].ids |= clusters[drop].ids
            clusters[keep].edges += clusters[drop].edges + [e]
            for i in clusters[drop].ids:
                owner[i] = keep
            del clusters[drop]
    out = []
    for c in clusters.values():
        media = {corpus.docs[i].media_id for i in c.ids}
        if len(c.ids) >= 2 and len(media) >= 2:
            out.append(c)
    return out


def describe(corpus: Corpus, ids: set[int]) -> dict:
    """Nombre, términos comunes, centralidad y conteos de un conjunto de artículos."""
    docs = [corpus.docs[i] for i in ids]
    cache: dict = {}
    centrality = {}
    for d in docs:
        others = [o for o in docs if o.id != d.id]
        centrality[d.id] = (sum(_sim(corpus, d.id, o.id, cache) for o in others) / len(others)
                            if others else 0.0)
    # El nombre sale de la nota más central, no de la primera encontrada.
    central = max(docs, key=lambda d: (centrality[d.id], -d.id))
    term_counts = Counter(t for d in docs for t in {t for t in d.word_vec if "_" not in t})
    half = max(2, (len(docs) + 1) // 2)
    common = [t for t, c in term_counts.most_common(30)
              if c >= half and corpus.term_df.get(t, 0) <= corpus.index_term_max * 3][:8]
    phrase_media: dict[str, set[int]] = {}
    for d in docs:
        for p in d.phrases:
            phrase_media.setdefault(p, set()).add(d.media_id)
    from radar.analysis.textproc import maximal_phrases

    shared_phrases = maximal_phrases({p for p, m in phrase_media.items()
                                      if len(m) >= 2 and corpus.phrase_df[p] <= corpus.rare_phrase_max})[:6]
    pairs = [(a.id, b.id) for a in docs for b in docs if a.id < b.id]
    sims = [_sim(corpus, a, b, cache) for a, b in pairs] or [0.0]
    return {
        "title": central.title,
        "central_id": central.id,
        "common_terms": common,
        "shared_phrases": shared_phrases,
        "centrality": centrality,
        "coherence": {"min": round(min(sims), 3), "media": round(sum(sims) / len(sims), 3)},
        "media_ids": sorted({d.media_id for d in docs}),
        "independent_media_ids": sorted({d.media_id for d in docs if not d.is_syndicated}),
        "topic_ids": Counter(t for d in docs for t in d.topic_ids).most_common(1),
    }


def edges_from_relations(rows, docs: dict[int, Doc]) -> tuple[list[Edge], set[tuple[int, int]]]:
    """Aristas para agrupar y pares prohibidos a partir de relaciones (incluidas las revisadas)."""
    edges, forbidden = [], set()
    for r in rows:
        if r.article_a_id not in docs or r.article_b_id not in docs:
            continue
        key = (min(r.article_a_id, r.article_b_id), max(r.article_a_id, r.article_b_id))
        if r.review_status == "rechazada":
            forbidden.add(key)
            continue
        if r.relation_type in GROUPING_TYPES or (
                r.review_status == "confirmada"
                and r.relation_type not in ("afirmaciones_distintas", "reaparicion")):
            edges.append(Edge(key[0], key[1], r.score or 0.0, r.relation_type))
    return edges, forbidden
