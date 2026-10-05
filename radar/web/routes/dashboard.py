from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from radar.config import get_settings
from radar.models import Alert, Article, CollectionRun, Media, Subsource, Topic, User
from radar.web.routes.alerts_routes import PRIORITY_ORDER, _alert_scope
from radar.queries import topic_condition, visible_articles
from radar.sources_status import STATE_LABELS, media_health
from radar.timeutil import utcnow
from radar.web.deps import demo_visible, get_db, is_public, render, viewer

router = APIRouter()


@router.get("/")
def summary(request: Request, db: Session = Depends(get_db), user: User = Depends(viewer)):
    demo = demo_visible(request)
    since = utcnow() - timedelta(hours=24)

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
        .limit(12)
    ).all()

    runs = db.scalars(
        select(CollectionRun)
        .options(selectinload(CollectionRun.subsource).selectinload(Subsource.media))
        .order_by(CollectionRun.started_at.desc())
        .limit(8)
    ).all()

    failing = db.scalar(
        select(func.count(Subsource.id)).where(
            Subsource.enabled.is_(True),
            (Subsource.consecutive_failures > 0) | (Subsource.last_status == "blocked"),
        )
    ) or 0
    # Logueados: alertas pendientes. Visitantes: solo las publicables (revisadas por defecto).
    def scope(stmt):
        stmt = _alert_scope(stmt, request)
        return stmt if is_public(request) else stmt.where(Alert.status == "pendiente")

    new_alerts = db.scalar(select(func.count()).select_from(scope(select(Alert)).subquery())) or 0
    alert_prio = dict(db.execute(scope(select(Alert.priority, func.count()))
                                 .group_by(Alert.priority)).all())
    top_alerts = sorted(db.scalars(scope(select(Alert)).order_by(Alert.created_at.desc())
                                   .limit(30)).all(),
                        key=lambda a: PRIORITY_ORDER.get(a.priority, 9))[:6]
    total = db.scalar(select(func.count()).select_from(visible_articles(demo).subquery())) or 0

    return render(request, "summary.html", {
        "per_media": per_media, "per_topic": per_topic, "latest": latest, "runs": runs,
        "failing": failing, "new_alerts": new_alerts, "total": total, "nav": "resumen",
        "health": media_health(db, demo), "labels": STATE_LABELS,
        "alert_prio": alert_prio, "top_alerts": top_alerts,
    }, user=user)
