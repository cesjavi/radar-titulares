from __future__ import annotations

import json
import re
from datetime import timedelta

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from radar.ai.service import current_group_analysis
from radar.analysis import relevance, trends
from radar.config import get_settings
from radar.interactions import record as record_interaction
from radar.models import (
    AiGroupAnalysis,
    Alert,
    Article,
    ArticleRelation,
    CollectionRun,
    Media,
    StoryGroup,
    StoryGroupMember,
    Subsource,
    Topic,
    User,
)
from radar.web.routes.alerts_routes import PRIORITY_ORDER, REVIEW_QUEUE_TYPES, _alert_scope
from radar.queries import topic_condition, visible_articles
from radar.seed import get_setting
from radar.text import excluded_terms_pattern, normalize_for_search
from radar.sources_status import STATE_LABELS, media_health
from radar.timeutil import utcnow
from radar.web.deps import demo_visible, get_db, is_public, render, viewer

router = APIRouter()

DASHBOARD_GROUP_ORDERS = {
    "recientes": "Fecha (más reciente)",
    "medios": "Más medios independientes",
    "notas": "Más notas",
    "actualizados": "Última actualización",
    "antiguos": "Fecha (más antigua)",
    "interes": "Por interés (clics y ocultos)",
}
INTEREST_CANDIDATES = 300  # grupos que se puntúan para el orden por interés
# Sin una recolección exitosa en este lapso, el resumen avisa que los datos pueden estar atrasados.
STALE_AFTER = timedelta(hours=6)
ACTIVITY_HOURS = 48  # barras de notas detectadas por hora

# Geometría de la línea de tiempo de cada tarjeta (viewBox de 300 de ancho).
TL_PLOT_X0, TL_PLOT_X1 = 96, 292  # zona de los puntos; a la izquierda van los nombres
TL_LANE_H, TL_TOP = 16, 10


def hourly_counts(times, now, hours: int = ACTIVITY_HOURS) -> list[int]:
    """Notas por hora, de la más vieja (índice 0) a la última hora (índice hours-1)."""
    counts = [0] * hours
    for t in times:
        idx = hours - 1 - int((now - t).total_seconds() // 3600)
        if 0 <= idx < hours:
            counts[idx] += 1
    return counts


def group_timeline(group: StoryGroup) -> dict | None:
    """Puntos por hora de publicación declarada, una fila por medio (ordenadas por nombre, no por
    quién publicó antes). Las notas sin hora no se ubican en la línea: se listan aparte. Hace falta
    que al menos dos notas tengan hora."""
    timed, untimed = [], []
    for m in group.members:
        a = m.article
        if m.status != "activo" or a is None or a.media is None:
            continue
        if a.published_precision == "datetime" and a.published_at is not None:
            timed.append(a)
        else:
            untimed.append(a)
    if len(timed) < 2:
        return None
    start = min(a.published_at for a in timed)
    end = max(a.published_at for a in timed)
    span = (end - start).total_seconds()
    names = sorted({a.media.name for a in timed}, key=str.casefold)
    lane = {n: i for i, n in enumerate(names)}
    width = TL_PLOT_X1 - TL_PLOT_X0
    points = [{"x": round(TL_PLOT_X0 + (width * (a.published_at - start).total_seconds() / span
                                         if span else width / 2), 1),
               "y": TL_TOP + lane[a.media.name] * TL_LANE_H, "article": a}
              for a in sorted(timed, key=lambda a: a.published_at)]
    return {"lanes": [{"name": n, "y": TL_TOP + i * TL_LANE_H} for i, n in enumerate(names)],
            "points": points, "start": start, "end": end, "span_min": round(span / 60),
            "axis_y": TL_TOP + len(names) * TL_LANE_H + 2,
            "height": TL_TOP + len(names) * TL_LANE_H + 14,
            "untimed": sorted({a.media.name for a in untimed}, key=str.casefold)}


def _excluded(group: StoryGroup, pattern: re.Pattern) -> bool:
    titles = [group.title] + [m.article.title for m in group.members
                              if m.status == "activo" and m.article is not None]
    return any(pattern.search(normalize_for_search(t or "")) for t in titles)


def _json(value: str | None, default):
    try:
        return json.loads(value) if value else default
    except ValueError:
        return default


@router.get("/")
def summary(
    request: Request,
    orden: str = Query("medios", max_length=20),
    min_medios: int = Query(0, ge=0, le=20),
    limite: int = Query(6, ge=1, le=48),
    db: Session = Depends(get_db),
    user: User = Depends(viewer),
):
    demo = demo_visible(request)
    public = is_public(request)
    since = utcnow() - timedelta(hours=24)
    if public:
        # Los visitantes ven una portada fija: sin filtros ni el orden por interés (que sale de los
        # clics y ocultos del equipo).
        orden, min_medios, limite = "medios", 0, 6

    # Grupos destacados: notas de distintos medios con coincidencias léxicas
    group_stmt = (
        select(StoryGroup)
        .options(
            selectinload(StoryGroup.members)
            .selectinload(StoryGroupMember.article)
            .selectinload(Article.media)
        )
        .where(StoryGroup.status == "open", StoryGroup.article_count >= 1,
               StoryGroup.hidden_at.is_(None))
    )
    if not demo:
        demo_groups = (
            select(StoryGroupMember.group_id).join(Article).where(Article.is_demo.is_(True))
        )
        group_stmt = group_stmt.where(StoryGroup.id.not_in(demo_groups))
    # Historias nuevas del día en dos o más medios independientes (las réplicas no suman).
    multi_24h = db.scalar(select(func.count()).select_from(
        group_stmt.where(StoryGroup.independent_media_count >= 2, StoryGroup.first_seen_at >= since)
        .subquery())) or 0

    if min_medios > 0:
        group_stmt = group_stmt.where(StoryGroup.independent_media_count >= min_medios)

    if orden not in DASHBOARD_GROUP_ORDERS:
        orden = "medios"
    interest_model, interest_note = None, None
    if orden == "interes":
        interest_model = relevance.load_model(db)
        if relevance.usable(interest_model):
            interest_note = (f"Ordenado por parecido con lo que se abrió ({interest_model['positives']} "
                             f"notas) y con lo que se ocultó o excluyó ({interest_model['negatives']}). "
                             "Solo cambia el orden: no se oculta nada.")
        else:
            got = interest_model or {}
            interest_note = (f"Todavía no hay datos para ordenar por interés: hacen falta al menos "
                             f"{relevance.MIN_EXAMPLES} notas abiertas y {relevance.MIN_EXAMPLES} "
                             f"ocultas o excluidas (hay {got.get('positives', 0)} y "
                             f"{got.get('negatives', 0)}; se actualiza en cada análisis). "
                             "Se muestra el orden por medios.")
            interest_model = None

    order_by = {
        "recientes": [StoryGroup.first_seen_at.desc().nulls_last(), StoryGroup.id.desc()],
        "medios": [StoryGroup.independent_media_count.desc(), StoryGroup.first_seen_at.desc(), StoryGroup.id.desc()],
        "notas": [StoryGroup.article_count.desc(), StoryGroup.first_seen_at.desc(), StoryGroup.id.desc()],
        "actualizados": [StoryGroup.updated_at.desc().nulls_last(), StoryGroup.id.desc()],
        "antiguos": [StoryGroup.first_seen_at.asc().nulls_last(), StoryGroup.id.desc()],
    }.get(orden, [StoryGroup.independent_media_count.desc(), StoryGroup.first_seen_at.desc(),
                  StoryGroup.id.desc()])

    if limite not in (6, 12, 18, 24):
        limite = 6

    # Los términos excluidos se aplican en Python (sin tildes, por palabra completa), así que se
    # leen tandas hasta completar el límite.
    pattern = excluded_terms_pattern(get_setting(db, "summary_excluded_terms"))
    featured_raw, excluded_count, offset, batch = [], 0, 0, limite if pattern is None else 60
    if interest_model is not None:
        # Se puntúan los grupos más recientes y se reordenan en Python; el empate lo decide la
        # cantidad de medios independientes.
        candidates = db.scalars(group_stmt.order_by(StoryGroup.first_seen_at.desc().nulls_last(),
                                                    StoryGroup.id.desc())
                                .limit(INTEREST_CANDIDATES)).all()
        candidates.sort(key=lambda g: (-relevance.group_score(interest_model, g),
                                       -(g.independent_media_count or 0), -g.id))
        for g in candidates:
            if pattern is not None and _excluded(g, pattern):
                excluded_count += 1
            elif len(featured_raw) < limite:
                featured_raw.append(g)
        offset = 2000  # no entrar al bucle por tandas
    while len(featured_raw) < limite and offset < 2000:
        chunk = db.scalars(group_stmt.order_by(*order_by).offset(offset).limit(batch)).all()
        for g in chunk:
            if pattern is not None and _excluded(g, pattern):
                excluded_count += 1
            elif len(featured_raw) < limite:
                featured_raw.append(g)
        if len(chunk) < batch or pattern is None:
            break
        offset += batch
    hidden_count = db.scalar(select(func.count(StoryGroup.id)).where(
        StoryGroup.status == "open", StoryGroup.hidden_at.is_not(None))) or 0

    visible_article_ids, visible_article_ranks = [], []
    for rank, group in enumerate(featured_raw, 1):
        article_ids = [member.article_id for member in group.members
                       if member.status == "activo" and member.article is not None]
        visible_article_ids.extend(article_ids)
        visible_article_ranks.extend([rank] * len(article_ids))
    record_interaction(db, visible_article_ids, getattr(user, "id", None),
                       "impresion", f"resumen:{orden}", visible_article_ranks)

    gids = [g.id for g in featured_raw]
    ai_analyses = {}
    if gids:
        ai_analyses = {a.group_id: a for a in db.scalars(
            select(AiGroupAnalysis).where(AiGroupAnalysis.group_id.in_(gids),
                                          current_group_analysis(public)))}

    featured_groups = []
    for g in featured_raw:
        common = _json(g.common_terms, {})
        media_list = []
        seen_slugs = set()
        for m in g.members:
            if m.status == "activo" and m.article and m.article.media:
                if m.article.media.slug not in seen_slugs:
                    media_list.append(m.article.media)
                    seen_slugs.add(m.article.media.slug)

        ai_obj = ai_analyses.get(g.id)
        sentiment_info = None
        if ai_obj:
            res = _json(ai_obj.result, {})
            enfoque = res.get("enfoque")
            tones = res.get("tono_por_nota", [])
            has_fav = any(t.get("valoracion") == "favorable" for t in tones)
            has_crit = any(t.get("valoracion") == "critico" for t in tones)
            if enfoque == "opuesto" or (has_fav and has_crit):
                sentiment_info = {"tipo": "opuesto", "label": "Enfoques opuestos"}
            elif enfoque == "diferente":
                sentiment_info = {"tipo": "diferente", "label": "Enfoques distintos"}
            elif tones:
                main_val = tones[0].get("valoracion", "")
                if main_val in ("favorable", "critico", "neutral"):
                    sentiment_info = {"tipo": main_val, "label": f"Tono {main_val}"}

        featured_groups.append((g, common, media_list, sentiment_info, group_timeline(g)))

    total_groups = db.scalar(
        select(func.count(StoryGroup.id)).where(StoryGroup.status == "open",
                                                StoryGroup.hidden_at.is_(None))
    ) or 0
    total_ai_analyses = db.scalar(
        select(func.count(AiGroupAnalysis.id)).where(current_group_analysis(public))
    ) or 0

    media_filter = [] if demo else [Media.is_demo.is_(False)]
    per_media = db.execute(
        select(Media, func.count(Article.id))
        .outerjoin(Article, (Article.media_id == Media.id) & (Article.first_seen_at >= since))
        .where(Media.enabled.is_(True), *media_filter)
        .group_by(Media.id)
        .order_by(Media.is_demo, Media.name)
    ).all()

    base = visible_articles(demo).where(Article.first_seen_at >= since)
    per_topic = []
    for topic in db.scalars(select(Topic).where(Topic.enabled.is_(True)).order_by(Topic.name)):
        cond = topic_condition(topic)
        n = 0
        if cond is not None:
            n = db.scalar(select(func.count()).select_from(base.where(cond).subquery())) or 0
        per_topic.append((topic, n))

    latest = db.scalars(
        visible_articles(demo)
        .options(selectinload(Article.media))
        .order_by(Article.first_seen_at.desc())
        .limit(10)
    ).all()

    runs = db.scalars(
        select(CollectionRun)
        .options(selectinload(CollectionRun.subsource).selectinload(Subsource.media))
        .order_by(CollectionRun.started_at.desc())
        .limit(6)
    ).all()

    failing = db.scalar(
        select(func.count(Subsource.id)).where(
            Subsource.enabled.is_(True),
            (Subsource.consecutive_failures > 0) | (Subsource.last_status == "blocked"),
        )
    ) or 0

    def scope(stmt):
        stmt = _alert_scope(stmt, request)
        return stmt if is_public(request) else stmt.where(Alert.status == "pendiente")

    new_alerts = db.scalar(select(func.count()).select_from(scope(select(Alert)).subquery())) or 0
    alert_prio = dict(db.execute(scope(select(Alert.priority, func.count()))
                                 .group_by(Alert.priority)).all())
    top_alerts = sorted(db.scalars(scope(select(Alert)).order_by(Alert.created_at.desc())
                                   .limit(30)).all(),
                        key=lambda a: PRIORITY_ORDER.get(a.priority, 9))[:5]
    total = db.scalar(select(func.count()).select_from(
        visible_articles(demo, hidden=None).subquery())) or 0
    last_ok = db.scalar(select(func.max(Subsource.last_success_at)))
    stale = last_ok is None or utcnow() - last_ok > STALE_AFTER
    notes_24h = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    now = utcnow()
    activity = hourly_counts(db.scalars(
        visible_articles(demo).with_only_columns(Article.first_seen_at)
        .where(Article.first_seen_at >= now - timedelta(hours=ACTIVITY_HOURS))).all(), now)
    trend_data = trends.load(db)
    rising = (trend_data or {}).get("rising") or []
    trend_max = max([max(r["count"], r.get("usual") or 0) for r in rising] or [0])

    # Pendientes del equipo (no se calculan para visitantes).
    todo = None
    if not public:
        todo = {"alerts": new_alerts, "failing": failing,
                "relations": db.scalar(select(func.count(ArticleRelation.id)).where(
                    ArticleRelation.review_status == "pendiente",
                    ArticleRelation.relation_type.in_(REVIEW_QUEUE_TYPES))) or 0,
                "ai": 0}
        if user.is_admin:
            todo["ai"] = db.scalar(
                select(func.count(AiGroupAnalysis.id))
                .join(StoryGroup, StoryGroup.id == AiGroupAnalysis.group_id)
                .where(current_group_analysis(False, "pendiente"),
                       StoryGroup.status == "open", StoryGroup.hidden_at.is_(None))) or 0

    return render(request, "summary.html", {
        "featured_groups": featured_groups,
        "group_order": orden,
        "group_orders": DASHBOARD_GROUP_ORDERS,
        "interest_note": interest_note,
        "trends": trend_data, "trend_max": trend_max,
        "notes_24h": notes_24h, "multi_24h": multi_24h, "todo": todo,
        "activity": activity, "activity_max": max(activity), "activity_hours": ACTIVITY_HOURS,
        "media_max": max([n for _, n in per_media] or [0]),
        "min_medios": min_medios,
        "limite": limite,
        "hidden_count": hidden_count,
        "excluded_count": excluded_count,
        "total_groups": total_groups,
        "total_ai_analyses": total_ai_analyses,
        "per_media": per_media, "per_topic": per_topic, "latest": latest, "runs": runs,
        "failing": failing, "new_alerts": new_alerts, "total": total, "nav": "resumen",
        "health": media_health(db, demo), "labels": STATE_LABELS,
        "alert_prio": alert_prio, "top_alerts": top_alerts,
        "last_ok": last_ok, "stale": stale, "stale_hours": int(STALE_AFTER.total_seconds() // 3600),
        "loaded_at": utcnow(),
    }, user=user)
