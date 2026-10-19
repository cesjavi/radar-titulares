"""Informe de un período: junta en un solo JSON lo que el panel muestra por separado.

Secciones: volumen por medio y por día, grupos principales, agenda compartida, cambios de
titular, temas, revisiones y alertas, y tono según la IA. Todo se cuenta por la hora de
detección del radar (`first_seen_at`), la única que tienen todas las notas.

El resultado es JSON plano (fechas como texto ISO en UTC) para poder guardarlo como foto en
`saved_reports` y mostrarlo después igual que el día en que se generó.

Es descriptivo: coincidir en palabras no es transmitir el mismo mensaje, cubrir lo mismo no
es coordinación y el orden temporal no es causalidad. El tono es una lectura automática de la
IA, sin verificar.
"""

from __future__ import annotations

import csv
import io
import json
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from radar.analysis import ALGORITHM_VERSION, headline_changes
from radar.metrics import agenda_metrics
from radar.models import (
    AiGroupAnalysis,
    Alert,
    Article,
    ArticleRelation,
    ManualReview,
    Media,
    RelationLabel,
    StoryGroup,
    StoryGroupMember,
    Topic,
)
from radar.queries import topic_condition
from radar.timeutil import LOCAL_TZ, to_local, to_utc_naive, utcnow

REPORT_FORMAT = 1
MAX_DAYS = 180
TOP_GROUPS = 20
TOP_TOPICS = 15
TONES = ("favorable", "critico", "neutral", "incierto")


def period_from_dates(desde: date, hasta: date) -> tuple[datetime, datetime]:
    """Días de Buenos Aires (ambos inclusive) → [inicio, fin) en UTC naive."""
    start = datetime.combine(desde, time(), tzinfo=LOCAL_TZ)
    end = datetime.combine(hasta + timedelta(days=1), time(), tzinfo=LOCAL_TZ)
    return to_utc_naive(start), to_utc_naive(end)


def default_title(since: datetime, until: datetime, media_name: str | None = None) -> str:
    first = to_local(since).date()
    last = to_local(until - timedelta(microseconds=1)).date()
    title = f"Informe del {first:%d/%m/%Y} al {last:%d/%m/%Y}"
    return f"{title} · {media_name}" if media_name else title


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def _json(value, default):
    try:
        return json.loads(value) if value else default
    except ValueError:
        return default


def build(db: Session, since: datetime, until: datetime, media: Media | None = None,
          include_demo: bool = False, now: datetime | None = None) -> dict:
    names = dict(db.execute(select(Media.id, Media.name)).all())
    groups, group_ids = _groups(db, since, until, media, include_demo)
    return {
        "format": REPORT_FORMAT,
        "since": _iso(since),
        "until": _iso(until),
        "media": {"slug": media.slug, "name": media.name} if media else None,
        "generated_at": _iso(now or utcnow()),
        "algorithm_version": ALGORITHM_VERSION,
        "volume": _volume(db, since, until, media, include_demo, names),
        "groups": groups,
        "agenda": _agenda(db, since, until, media, include_demo),
        "headlines": _headlines(db, since, until, media, include_demo),
        "topics": _topics(db, since, until, media, include_demo, names),
        "reviews": _reviews(db, since, until),
        "tone": _tone(db, group_ids, media, include_demo, names),
    }


def _article_filter(since, until, media, include_demo) -> list:
    cond = [Article.first_seen_at >= since, Article.first_seen_at < until]
    if media is not None:
        cond.append(Article.media_id == media.id)
    if not include_demo:
        cond.append(Article.is_demo.is_(False))
    return cond


def _volume(db, since, until, media, include_demo, names) -> dict:
    rows = db.execute(select(Article.media_id, Article.first_seen_at, Article.is_syndicated)
                      .where(*_article_filter(since, until, media, include_demo))).all()
    own, syndicated = Counter(), Counter()
    daily: dict[date, Counter] = defaultdict(Counter)
    for mid, seen, synd in rows:
        (syndicated if synd else own)[mid] += 1
        daily[to_local(seen).date()][mid] += 1
    media_ids = sorted(set(own) | set(syndicated), key=lambda m: names.get(m, ""))
    first, last = to_local(since).date(), to_local(until - timedelta(microseconds=1)).date()
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    return {
        "total": len(rows),
        "media": [{"name": names.get(m, str(m)), "own": own[m], "syndicated": syndicated[m]}
                  for m in media_ids],
        "days": [{"day": d.isoformat(), "counts": [daily[d][m] for m in media_ids],
                  "total": sum(daily[d].values())} for d in days],
    }


def _groups(db, since, until, media, include_demo) -> tuple[dict, list[int]]:
    groups = db.scalars(
        select(StoryGroup)
        .options(selectinload(StoryGroup.members).selectinload(StoryGroupMember.article)
                 .selectinload(Article.media))
        .where(StoryGroup.status == "open", StoryGroup.hidden_at.is_(None),
               StoryGroup.first_seen_at >= since, StoryGroup.first_seen_at < until)).all()
    kept = []
    for g in groups:
        arts = [m.article for m in g.members if m.status == "activo" and m.article]
        if not arts or (not include_demo and any(a.is_demo for a in arts)):
            continue
        if media is not None and all(a.media_id != media.id for a in arts):
            continue
        kept.append((g, arts))
    kept.sort(key=lambda x: (-x[0].independent_media_count, -x[0].article_count,
                             x[0].first_seen_at))
    top = []
    for g, arts in kept[:TOP_GROUPS]:
        # Las réplicas se listan aparte: no cuentan como medio independiente.
        own = sorted({a.media.name for a in arts if not a.is_syndicated})
        synd = sorted({a.media.name for a in arts if a.is_syndicated} - set(own))
        top.append({"id": g.id, "title": g.title, "independent": g.independent_media_count,
                    "articles": len(arts), "media": own, "syndicated_media": synd,
                    "first_seen": _iso(g.first_seen_at)})
    return ({"total": len(kept), "multi": sum(1 for g, _ in kept if g.independent_media_count >= 2),
             "top": top}, [g.id for g, _ in kept])


def _agenda(db, since, until, media, include_demo) -> dict:
    data = agenda_metrics(db, include_demo=include_demo, since=since, until=until)
    pairs = [p for p in data["pairs"] if media is None or media.name in (p.a, p.b)]
    exclusive = [e for e in data["exclusive"] if media is None or e["name"] == media.name]
    return {
        "weeks": [w.date().isoformat() for w in data["weeks"]],
        "pairs": [{"a": p.a, "b": p.b, "shared": p.shared, "union": p.union,
                   "jaccard": p.jaccard, "weekly": [list(w) if w else None for w in p.weekly]}
                  for p in pairs],
        "exclusive": exclusive,
    }


def _headlines(db, since, until, media, include_demo) -> dict:
    data = headline_changes.change_stats(db, include_demo=include_demo, since=since, until=until)
    rows = [s for s in data["media"] if media is None or s.name == media.name]
    totals = Counter()
    for s in rows:
        totals.update(s.by_category)
    return {
        "categories": dict(headline_changes.CATEGORIES),
        "totals": dict(totals),
        "media": [{"name": s.name, "articles": s.articles, "changed": s.changed,
                   "changes": s.changes, "changed_pct": s.changed_pct,
                   "median_first_change_min": s.median_first_change_min,
                   "by_category": dict(s.by_category)} for s in rows],
    }


def _topics(db, since, until, media, include_demo, names) -> dict:
    """Notas de cada tema en el período y en el período anterior de igual largo."""
    prev_since = since - (until - since)
    out = []
    for topic in db.scalars(select(Topic).where(Topic.enabled.is_(True)).order_by(Topic.name)):
        cond = topic_condition(topic)
        if cond is None:
            continue
        rows = db.execute(select(Article.media_id, Article.is_syndicated, Article.first_seen_at)
                          .where(cond, *_article_filter(prev_since, until, media, include_demo))
                          ).all()
        current = [(m, s) for m, s, seen in rows if seen >= since]
        previous = len(rows) - len(current)
        if not current and not previous:
            continue
        by_media = Counter(names.get(m, str(m)) for m, _ in current)
        out.append({"name": topic.name, "count": len(current), "previous": previous,
                    "independent_media": len({m for m, s in current if not s}),
                    "by_media": dict(sorted(by_media.items()))})
    out.sort(key=lambda t: (-t["count"], t["name"]))
    return {"items": out[:TOP_TOPICS], "omitted": max(0, len(out) - TOP_TOPICS)}


def _reviews(db, since, until) -> dict:
    def counts(col, model_date, *where):
        return dict(db.execute(select(col, func.count()).where(model_date >= since,
                                                               model_date < until, *where)
                               .group_by(col)).all())

    return {
        "relations": counts(ArticleRelation.review_status, ArticleRelation.reviewed_at,
                            ArticleRelation.review_status != "pendiente"),
        "manual": [{"target": t, "decision": d, "count": n} for t, d, n in db.execute(
            select(ManualReview.target_type, ManualReview.decision, func.count())
            .where(ManualReview.created_at >= since, ManualReview.created_at < until)
            .group_by(ManualReview.target_type, ManualReview.decision)
            .order_by(ManualReview.target_type, ManualReview.decision))],
        "labels": db.scalar(select(func.count()).select_from(RelationLabel).where(
            RelationLabel.created_at >= since, RelationLabel.created_at < until)) or 0,
        "alerts_by_priority": counts(Alert.priority, Alert.created_at),
        "alerts_by_status": counts(Alert.status, Alert.created_at),
    }


def _tone(db, group_ids, media, include_demo, names) -> dict:
    """Último análisis de IA válido de cada grupo del período, contado por medio y valoración."""
    latest: dict[int, AiGroupAnalysis] = {}
    for i in range(0, len(group_ids), 500):
        for ga in db.scalars(select(AiGroupAnalysis)
                             .where(AiGroupAnalysis.status == "ok",
                                    AiGroupAnalysis.group_id.in_(group_ids[i:i + 500]))
                             .order_by(AiGroupAnalysis.created_at.desc(), AiGroupAnalysis.id.desc())):
            latest.setdefault(ga.group_id, ga)
    parsed = {gid: (_json(ga.article_ids, []), _json(ga.result, {})) for gid, ga in latest.items()}
    all_ids = {aid for aids, _ in parsed.values() for aid in aids if isinstance(aid, int)}
    arts = {}
    ids = sorted(all_ids)
    for i in range(0, len(ids), 500):
        arts.update({aid: (mid, demo) for aid, mid, demo in db.execute(
            select(Article.id, Article.media_id, Article.is_demo)
            .where(Article.id.in_(ids[i:i + 500])))})
    titles = dict(db.execute(select(StoryGroup.id, StoryGroup.title)
                             .where(StoryGroup.id.in_(list(parsed)))).all()) if parsed else {}
    by_media: dict[str, Counter] = defaultdict(Counter)
    opposed = []
    for gid, (aids, res) in parsed.items():
        tones = [t for t in res.get("tono_por_nota", []) if isinstance(t, dict)]
        vals = {t.get("valoracion") for t in tones}
        # Mismo criterio que la pantalla de Sentimiento.
        if res.get("enfoque") == "opuesto" or {"favorable", "critico"} <= vals:
            opposed.append({"id": gid, "title": titles.get(gid, "")})
        for t in tones:
            letter = t.get("articulo") or ""
            idx = ord(letter) - 65 if len(letter) == 1 and "A" <= letter <= "Z" else -1
            if not 0 <= idx < len(aids) or aids[idx] not in arts:
                continue
            mid, demo = arts[aids[idx]]
            if (demo and not include_demo) or (media is not None and mid != media.id):
                continue
            val = t.get("valoracion")
            by_media[names.get(mid, str(mid))][val if val in TONES else "incierto"] += 1
    return {
        "groups_total": len(group_ids),
        "groups_analyzed": len(parsed),
        "by_media": [{"name": n, **{v: c[v] for v in TONES}} for n, c in sorted(by_media.items())],
        "opposed": sorted(opposed, key=lambda g: g["id"]),
    }


# --- Exportación a CSV ------------------------------------------------------------------

SECTIONS = {
    "volumen": "Volumen por medio",
    "volumen-diario": "Volumen por día",
    "grupos": "Grupos principales",
    "agenda": "Agenda compartida",
    "titulares": "Cambios de titular",
    "temas": "Temas",
    "tono": "Tono según la IA",
}


def _local_text(iso: str | None) -> str:
    return to_local(datetime.fromisoformat(iso)).strftime("%d/%m/%Y %H:%M") if iso else ""


def _pct(value: float | None) -> str:
    return "" if value is None else f"{value:.1f}".replace(".", ",")


def table(data: dict, section: str) -> tuple[list[str], list[list]]:
    """Encabezados y filas de una sección, para CSV."""
    if section == "volumen":
        return (["Medio", "Notas propias", "Réplicas de agencia"],
                [[m["name"], m["own"], m["syndicated"]] for m in data["volume"]["media"]])
    if section == "volumen-diario":
        media = [m["name"] for m in data["volume"]["media"]]
        return (["Día", *media, "Total"],
                [[date.fromisoformat(d["day"]).strftime("%d/%m/%Y"), *d["counts"], d["total"]]
                 for d in data["volume"]["days"]])
    if section == "grupos":
        return (["Grupo", "Título", "Medios independientes", "Notas", "Medios",
                 "Réplicas de", "Primera detección"],
                [[g["id"], g["title"], g["independent"], g["articles"], ", ".join(g["media"]),
                  ", ".join(g["syndicated_media"]), _local_text(g["first_seen"])]
                 for g in data["groups"]["top"]])
    if section == "agenda":
        return (["Medio A", "Medio B", "Historias en común", "Historias de alguno",
                 "Agenda compartida (%)"],
                [[p["a"], p["b"], p["shared"], p["union"],
                  _pct(None if p["jaccard"] is None else 100 * p["jaccard"])]
                 for p in data["agenda"]["pairs"]])
    if section == "titulares":
        cats = data["headlines"]["categories"]
        return (["Medio", "Notas", "Con cambios", "Con cambios (%)", "Cambios",
                 "Mediana hasta el primer cambio (min)", *cats.values()],
                [[m["name"], m["articles"], m["changed"], _pct(m["changed_pct"]), m["changes"],
                  m["median_first_change_min"] if m["median_first_change_min"] is not None else "",
                  *[m["by_category"].get(c, 0) for c in cats]]
                 for m in data["headlines"]["media"]])
    if section == "temas":
        return (["Tema", "Notas", "Período anterior", "Medios independientes", "Por medio"],
                [[t["name"], t["count"], t["previous"], t["independent_media"],
                  ", ".join(f"{k}: {v}" for k, v in t["by_media"].items())]
                 for t in data["topics"]["items"]])
    if section == "tono":
        return (["Medio", "Favorable", "Crítico", "Neutral", "Incierto"],
                [[m["name"], *[m[v] for v in TONES]] for m in data["tone"]["by_media"]])
    raise KeyError(section)


def _cell(value) -> str:
    text = "" if value is None else str(value)
    # Un titular que empieza con =, +, - o @ se ejecutaría como fórmula en una planilla.
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def to_csv(data: dict, section: str) -> str:
    headers, rows = table(data, section)
    buf = io.StringIO()
    # Punto y coma y BOM: lo que espera Excel con configuración regional en español.
    writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    writer.writerow(headers)
    writer.writerows([[_cell(v) for v in row] for row in rows])
    return "﻿" + buf.getvalue()
