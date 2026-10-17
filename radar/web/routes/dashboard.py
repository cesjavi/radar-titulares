from __future__ import annotations

import json
import re
from datetime import timedelta

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from radar.analysis import relevance, trends
from radar.config import get_settings
from radar.models import (
    AiGroupAnalysis,
    Alert,
    Article,
    CollectionRun,
    Media,
    StoryGroup,
    StoryGroupMember,
    Subsource,
    Topic,
    User,
)
from radar.web.routes.alerts_routes import PRIORITY_ORDER, _alert_scope
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
    since = utcnow() - timedelta(hours=24)

    # Grupos coordinados destacados (núcleo de la app)
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

    gids = [g.id for g in featured_raw]
    ai_analyses = {}
    if gids:
        ai_rows = db.scalars(
            select(AiGroupAnalysis)
            .where(AiGroupAnalysis.group_id.in_(gids), AiGroupAnalysis.status == "ok")
            .order_by(AiGroupAnalysis.created_at.desc())
        ).all()
        for ai_obj in ai_rows:
            if ai_obj.group_id not in ai_analyses:
                ai_analyses[ai_obj.group_id] = ai_obj

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
                sentiment_info = {"tipo": "opuesto", "label": "Enfoques opuestos ⚠️"}
            elif enfoque == "diferente":
                sentiment_info = {"tipo": "diferente", "label": "Enfoques distintos"}
            elif tones:
                main_val = tones[0].get("valoracion", "")
                if main_val in ("favorable", "critico", "neutral"):
                    sentiment_info = {"tipo": main_val, "label": f"Tono {main_val}"}

        featured_groups.append((g, common, media_list, sentiment_info))

    total_groups = db.scalar(
        select(func.count(StoryGroup.id)).where(StoryGroup.status == "open",
                                                StoryGroup.hidden_at.is_(None))
    ) or 0
    total_ai_analyses = db.scalar(
        select(func.count(AiGroupAnalysis.id)).where(AiGroupAnalysis.status == "ok")
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

    return render(request, "summary.html", {
        "featured_groups": featured_groups,
        "group_order": orden,
        "group_orders": DASHBOARD_GROUP_ORDERS,
        "interest_note": interest_note,
        "trends": trends.load(db),
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
        "last_ok": last_ok, "loaded_at": utcnow(),
    }, user=user)
