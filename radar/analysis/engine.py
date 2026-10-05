"""Orquestación del análisis: ventana reciente (72 h), antecedentes acotados (90 días),
relaciones y grupos. Respeta las correcciones manuales al reprocesar."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import delete, false, or_, select
from sqlalchemy.orm import Session

from radar.analysis import ALGORITHM_VERSION
from radar.analysis import textproc as tp
from radar.analysis.grouping import cluster, describe, edges_from_relations
from radar.analysis.relations import RULES, Corpus, Doc, RelationResult, find_relations
from radar.models import (
    Article,
    ArticleRelation,
    CollectionRun,
    Media,
    MediaReference,
    StoryGroup,
    StoryGroupMember,
    Topic,
)
from radar.queries import matching_topics
from radar.text import canonicalize_url
from radar.timeutil import utcnow

log = logging.getLogger("radar.analysis")

RECENT_HOURS = 72
HISTORY_DAYS = 90
MAX_RECENT_DOCS = 4000
MAX_HISTORY_DOCS = 1500
HISTORY_PER_TERM = 15
HISTORY_TERMS = 150
METHOD = f"{ALGORITHM_VERSION}: tfidf palabras+caracteres, coseno, reglas"


@dataclass
class AnalysisSummary:
    docs_recent: int = 0
    docs_history: int = 0
    candidate_pairs: int = 0
    relations_new: int = 0
    relations_updated: int = 0
    relations_removed: int = 0
    groups_new: int = 0
    groups_updated: int = 0
    groups_dissolved: int = 0
    duration_ms: int = 0
    by_type: dict = field(default_factory=dict)
    alerts: object = None
    ai: object = None
    notifications: dict = field(default_factory=dict)


def _to_doc(a: Article, media_names: dict[int, str], recent: bool) -> Doc:
    return Doc(
        id=a.id, media_id=a.media_id, title=a.title, subtitle=a.subtitle,
        canonical_url=a.canonical_url, is_demo=a.is_demo, is_syndicated=a.is_syndicated,
        origin_label=a.origin_label, published_at=a.published_at,
        published_precision=a.published_precision, first_seen_at=a.first_seen_at,
        recent=recent, media_name=media_names.get(a.media_id, ""),
    )


def load_docs(db: Session, now=None) -> list[Doc]:
    now = now or utcnow()
    since = now - timedelta(hours=RECENT_HOURS)
    media_names = dict(db.execute(select(Media.id, Media.name)).all())
    recent = db.scalars(
        select(Article)
        .where(or_(Article.first_seen_at >= since, Article.published_at >= since))
        .order_by(Article.first_seen_at.desc()).limit(MAX_RECENT_DOCS)
    ).all()
    docs = {a.id: _to_doc(a, media_names, True) for a in recent}

    # Antecedentes: selección acotada por las entidades/términos más distintivos recientes.
    hist_since = now - timedelta(days=HISTORY_DAYS)
    ent_count: dict[str, int] = {}
    for d in docs.values():
        for e in tp.entities(d.title):
            if len(e) >= 4:
                ent_count[e] = ent_count.get(e, 0) + 1
    # Las más repetidas no distinguen ("milei"); se toman las de frecuencia intermedia.
    terms = sorted((e for e, c in ent_count.items() if 2 <= c <= max(3, len(docs) // 20)),
                   key=lambda e: -ent_count[e])[:HISTORY_TERMS]
    history: dict[int, Article] = {}
    for term in terms:
        if len(history) >= MAX_HISTORY_DOCS:
            break
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = db.scalars(
            select(Article)
            .where(Article.first_seen_at < since, Article.first_seen_at >= hist_since,
                   Article.search_text.like(f"%{escaped}%", escape="\\"))
            .order_by(Article.first_seen_at.desc()).limit(HISTORY_PER_TERM)
        ).all()
        for a in rows:
            history.setdefault(a.id, a)
    for a in history.values():
        docs.setdefault(a.id, _to_doc(a, media_names, False))

    # Temas de seguimiento y referencias (para R5 y R7).
    topics = db.scalars(select(Topic).where(Topic.enabled.is_(True))).all()
    arts = {a.id: a for a in list(recent) + list(history.values())}
    for d in docs.values():
        d.topic_ids = {t.id for t, _ in matching_topics(arts[d.id], topics)}
    ids = list(docs)
    for start in range(0, len(ids), 500):
        for ref in db.scalars(select(MediaReference).where(
                MediaReference.article_id.in_(ids[start:start + 500]))):
            d = docs[ref.article_id]
            if ref.kind == "enlace":
                d.linked_urls.add(canonicalize_url(ref.evidence))
            d.mentioned_media.add(ref.referenced_media)
    return list(docs.values())


def _pair_key(a: int, b: int, t: str) -> tuple[int, int, str]:
    return (a, b, t) if a < b else (b, a, t)


def save_relations(db: Session, corpus: Corpus, results: list[RelationResult],
                   summary: AnalysisSummary) -> None:
    now = utcnow()
    generated = {_pair_key(r.a, r.b, r.relation_type): r for r in results}
    loaded = set(corpus.docs)
    recent = {d.id for d in corpus.docs.values() if d.recent}
    existing = {}
    ids = sorted(recent)
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        for rel in db.scalars(select(ArticleRelation).where(or_(
                ArticleRelation.article_a_id.in_(chunk), ArticleRelation.article_b_id.in_(chunk)))):
            existing[_pair_key(rel.article_a_id, rel.article_b_id, rel.relation_type)] = rel

    for key, r in generated.items():
        rel = existing.get(key)
        payload = dict(
            score=r.score, method=METHOD, scores=json.dumps(r.scores, ensure_ascii=False),
            evidence=json.dumps(r.evidence, ensure_ascii=False),
            rules=json.dumps([{"id": x, "regla": RULES[x]} for x in r.rules], ensure_ascii=False),
            algorithm_version=ALGORITHM_VERSION, time_delta_seconds=r.time_delta_seconds,
            time_note=r.time_note,
        )
        summary.by_type[r.relation_type] = summary.by_type.get(r.relation_type, 0) + 1
        if rel is None:
            db.add(ArticleRelation(article_a_id=key[0], article_b_id=key[1],
                                   relation_type=key[2], created_at=now, updated_at=now, **payload))
            summary.relations_new += 1
        elif rel.review_status == "pendiente":
            for k, v in payload.items():
                setattr(rel, k, v)
            rel.updated_at = now
            summary.relations_updated += 1
        # Las revisadas (confirmadas/rechazadas) se conservan tal cual.

    # Relaciones automáticas que ya no surgen (texto cambiado o reglas nuevas).
    stale = [rel.id for key, rel in existing.items()
             if key not in generated and rel.review_status == "pendiente"
             and rel.article_a_id in loaded and rel.article_b_id in loaded]
    for start in range(0, len(stale), 500):
        db.execute(delete(ArticleRelation).where(ArticleRelation.id.in_(stale[start:start + 500])))
    summary.relations_removed = len(stale)
    db.flush()


def _apply_group_stats(group: StoryGroup, info: dict, edges, now) -> None:
    group.title = info["title"][:500]
    group.media_count = len(info["media_ids"])
    group.independent_media_count = len(info["independent_media_ids"])
    group.common_terms = json.dumps({"terminos": info["common_terms"],
                                     "frases": info["shared_phrases"]}, ensure_ascii=False)
    group.explanation = json.dumps({
        "version": ALGORITHM_VERSION,
        "criterio": ("Se agrupan notas de distintos medios unidas por relaciones léxicas "
                     "(titular idéntico o casi idéntico, expresión compartida, posible mismo hecho), "
                     "solo si todos los pares tienen una similitud mínima y la media supera el umbral."),
        "coherencia": info["coherence"],
        "nota_central": info["central_id"],
        "aristas": [{"a": e.a, "b": e.b, "tipo": e.relation_type, "puntaje": round(e.score, 3)}
                    for e in edges][:60],
        "advertencias": [
            "La similitud es léxica: no verifica que el enfoque o el mensaje sean iguales.",
            "El orden de aparición observado no indica qué medio originó la información "
            "ni que uno reproduzca a otro.",
        ],
    }, ensure_ascii=False)
    if info["topic_ids"]:
        group.topic_id = info["topic_ids"][0][0]
    group.algorithm_version = ALGORITHM_VERSION
    group.updated_at = now


def save_groups(db: Session, corpus: Corpus, summary: AnalysisSummary) -> None:
    now = utcnow()
    recent_ids = {d.id for d in corpus.docs.values() if d.recent}
    # Relaciones de la ventana (incluidas las revisadas a mano).
    ids = sorted(recent_ids)
    rels = []
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        rels += [r for r in db.scalars(select(ArticleRelation).where(
            ArticleRelation.article_a_id.in_(chunk))) if r.article_b_id in recent_ids]
    recent_docs = {i: corpus.docs[i] for i in recent_ids}
    edges, forbidden = edges_from_relations(rels, recent_docs)

    # Grupos bloqueados (con correcciones manuales): sus artículos quedan fuera del motor.
    locked_ids = set(db.scalars(
        select(StoryGroupMember.article_id).join(StoryGroup)
        .where(StoryGroup.locked.is_(True), StoryGroup.status == "open",
               StoryGroupMember.status == "activo")).all())
    clusters = cluster(corpus, edges, forbidden, locked_ids)

    # Grupos automáticos abiertos que tocan la ventana: se actualizan o disuelven.
    auto_groups = db.scalars(
        select(StoryGroup).join(StoryGroupMember)
        .where(StoryGroup.locked.is_(False), StoryGroup.status == "open",
               StoryGroupMember.article_id.in_(recent_ids) if recent_ids else false())
        .distinct()).all()
    current = {g.id: {m.article_id for m in g.members if m.status == "activo"} for g in auto_groups}
    used: set[int] = set()
    for c in sorted(clusters, key=lambda c: -len(c.ids)):
        best, best_j = None, 0.0
        for gid, members in current.items():
            if gid in used:
                continue
            j = len(members & c.ids) / len(members | c.ids)
            if j > best_j:
                best, best_j = gid, j
        info = describe(corpus, c.ids)
        if best is not None and best_j >= 0.34:
            group = db.get(StoryGroup, best)
            used.add(best)
            summary.groups_updated += 1
        else:
            group = StoryGroup(title=info["title"][:500], status="open", created_at=now)
            db.add(group)
            db.flush()
            summary.groups_new += 1
        existing = {m.article_id: m for m in group.members}
        for aid in c.ids:
            m = existing.get(aid)
            if m is None:
                group.members.append(StoryGroupMember(article_id=aid, added_by="auto",
                                                      status="activo", added_at=now,
                                                      centrality=round(info["centrality"][aid], 3)))
            elif m.status == "activo":
                m.centrality = round(info["centrality"][aid], 3)
        for aid, m in existing.items():
            if aid not in c.ids and m.added_by == "auto" and aid in recent_ids:
                group.members.remove(m)
        _apply_group_stats(group, info, c.edges, now)
        _refresh_counts(group, corpus)

    # Grupos automáticos que ya no tienen cluster: se disuelven (sin perder los manuales).
    for gid in set(current) - used:
        group = db.get(StoryGroup, gid)
        if all(aid in recent_ids for aid in current[gid]):
            for m in list(group.members):
                if m.added_by == "auto":
                    group.members.remove(m)
            if not any(m.status == "activo" for m in group.members):
                group.status = "empty"
                summary.groups_dissolved += 1
            _refresh_counts(group, corpus)
    db.flush()


def _refresh_counts(group: StoryGroup, corpus: Corpus | None = None) -> None:
    active = [m for m in group.members if m.status == "activo"]
    group.article_count = len(active)
    firsts = [m.article.first_seen_at for m in active if m.article is not None] if corpus is None \
        else [corpus.docs[m.article_id].first_seen_at if m.article_id in corpus.docs
              else (m.article.first_seen_at if m.article else None) for m in active]
    firsts = [f for f in firsts if f]
    group.first_seen_at = min(firsts) if firsts else None


def refresh_group_stats(db: Session, group: StoryGroup) -> None:
    """Recalcula conteos tras una corrección manual (sin vectores)."""
    active = [m for m in group.members if m.status == "activo"]
    arts = [m.article for m in active]
    group.article_count = len(arts)
    group.media_count = len({a.media_id for a in arts})
    group.independent_media_count = len({a.media_id for a in arts if not a.is_syndicated})
    firsts = [a.first_seen_at for a in arts if a.first_seen_at]
    group.first_seen_at = min(firsts) if firsts else None
    if not arts:
        group.status = "empty"
    group.updated_at = utcnow()


def run_analysis(db: Session, now=None) -> AnalysisSummary:
    t0 = time.monotonic()
    summary = AnalysisSummary()
    run = CollectionRun(run_type="analisis", status="running")
    db.add(run)
    db.flush()
    docs = load_docs(db, now)
    summary.docs_recent = sum(1 for d in docs if d.recent)
    summary.docs_history = len(docs) - summary.docs_recent
    if len(docs) >= 2:
        corpus = Corpus(docs)
        summary.candidate_pairs = len(corpus.candidate_pairs())
        results = find_relations(corpus)
        save_relations(db, corpus, results, summary)
        save_groups(db, corpus, summary)
    summary.duration_ms = int((time.monotonic() - t0) * 1000)
    run.status = "ok"
    run.items_found = len(docs)
    run.items_new = summary.relations_new
    run.items_updated = summary.relations_updated + summary.groups_updated + summary.groups_new
    run.finished_at = utcnow()
    run.duration_ms = summary.duration_ms
    run.error = None
    log.info("Análisis: %s", summary)
    return summary
