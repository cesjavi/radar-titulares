"""Alertas, revisión manual, historial, ejecuciones, cronología y métricas."""

from __future__ import annotations

import json
import math
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from radar.alerts.config import PRIORITIES
from radar.alerts.engine import STATUSES, set_status
from radar.analysis.relations import TYPE_LABELS
from radar.config import get_settings
from radar.metrics import sequence_metrics
from radar.models import (
    Alert,
    AlertEvent,
    Article,
    ArticleRelation,
    CollectionRun,
    HeadlineVersion,
    ManualReview,
    StoryGroup,
    StoryGroupMember,
    Subsource,
    User,
)
from radar.timeutil import utcnow
from radar.web.deps import (
    demo_visible,
    get_db,
    is_public,
    render,
    require_admin,
    require_user,
    verify_csrf,
    viewer,
)

router = APIRouter()
PER_PAGE = 30
PRIORITY_ORDER = {"alta": 0, "media": 1, "baja": 2}


def _json(value, default):
    try:
        return json.loads(value) if value else default
    except ValueError:
        return default


def _demo_filter(stmt, request: Request):
    if demo_visible(request):
        return stmt
    demo_groups = select(StoryGroupMember.group_id).join(Article).where(Article.is_demo.is_(True))
    return stmt.where(Alert.group_id.not_in(demo_groups))


def public_alert_statuses() -> tuple[str, ...]:
    """Estados de alerta visibles para visitantes (RADAR_PUBLIC_ALERTS)."""
    return {"revisadas": ("revisada",), "todas": ("pendiente", "revisada"),
            "ninguna": ()}[get_settings().public_alerts]


def _alert_scope(stmt, request: Request):
    """Filtro de datos DEMO y, para visitantes, solo las alertas publicables."""
    stmt = _demo_filter(stmt, request)
    if is_public(request):
        stmt = stmt.where(Alert.status.in_(public_alert_statuses() or ("__ninguna__",)))
    return stmt


def _pages(total: int, page: int) -> tuple[int, int]:
    pages = max(1, math.ceil(total / PER_PAGE))
    return min(page, pages), pages


@router.get("/alertas")
def alert_list(request: Request, estado: str = Query("pendiente", max_length=16),
               prioridad: str = Query("", max_length=8), page: int = Query(1, ge=1, le=10000),
               db: Session = Depends(get_db), user: User = Depends(viewer)):
    if is_public(request):
        estado = "todas"
    stmt = _alert_scope(select(Alert), request)
    if estado in STATUSES:
        stmt = stmt.where(Alert.status == estado)
    if prioridad in PRIORITIES:
        stmt = stmt.where(Alert.priority == prioridad)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    page, pages = _pages(total, page)
    order = [func.coalesce(Alert.last_material_change_at, Alert.created_at).desc()]
    alerts = db.scalars(stmt.order_by(*order).offset((page - 1) * PER_PAGE).limit(PER_PAGE)).all()
    alerts = sorted(alerts, key=lambda a: PRIORITY_ORDER.get(a.priority, 9)) if estado == "pendiente" else alerts
    counts = dict(db.execute(_alert_scope(select(Alert.status, func.count()), request)
                             .group_by(Alert.status)).all())
    return render(request, "alerts.html", {
        "alerts": [(a, _json(a.evidence, {})) for a in alerts], "estado": estado,
        "prioridad": prioridad, "page": page, "pages": pages, "total": total, "counts": counts,
        "statuses": STATUSES, "priorities": PRIORITIES, "nav": "alertas",
    }, user=user)


@router.get("/alertas/{alert_id}")
def alert_detail(request: Request, alert_id: int, db: Session = Depends(get_db),
                 user: User = Depends(viewer)):
    alert = db.scalar(select(Alert).options(selectinload(Alert.events)).where(Alert.id == alert_id))
    if alert is not None and is_public(request) and alert.status not in public_alert_statuses():
        alert = None
    if alert is None:
        raise HTTPException(status_code=404, detail="La alerta no existe.")
    evidence = _json(alert.evidence, {})
    ids = [a["id"] for a in evidence.get("articulos", [])]
    arts = {a.id: a for a in db.scalars(select(Article).options(selectinload(Article.media))
                                        .where(Article.id.in_(ids)))} if ids else {}
    if any(a.is_demo for a in arts.values()) and not demo_visible(request):
        raise HTTPException(status_code=404, detail="La alerta no existe.")
    events = [(e, _json(e.detail, {})) for e in reversed(alert.events)]
    return render(request, "alert_detail.html", {
        "alert": alert, "evidence": evidence, "arts": arts,
        "sources": _json(alert.common_source, []), "limitations": _json(alert.limitations, []),
        "events": events, "statuses": STATUSES, "labels": TYPE_LABELS, "nav": "alertas",
    }, user=user)


@router.post("/alertas/{alert_id}/estado", dependencies=[Depends(verify_csrf)])
def alert_status(alert_id: int, status: str = Form(...), note: str = Form("", max_length=2000),
                 db: Session = Depends(get_db), user: User = Depends(require_admin)):
    alert = db.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="La alerta no existe.")
    try:
        set_status(db, alert, status, user, note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(f"/alertas/{alert_id}", status_code=303)


@router.get("/revision")
def review_queue(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    alerts = db.scalars(_demo_filter(select(Alert).where(Alert.status == "pendiente"), request)
                        .order_by(Alert.created_at.desc()).limit(100)).all()
    alerts = sorted(alerts, key=lambda a: PRIORITY_ORDER.get(a.priority, 9))
    rel_q = (select(ArticleRelation)
             .options(selectinload(ArticleRelation.article_a).selectinload(Article.media),
                      selectinload(ArticleRelation.article_b).selectinload(Article.media))
             .where(ArticleRelation.review_status == "pendiente",
                    ArticleRelation.relation_type.in_(("titular_identico", "titular_casi_identico",
                                                       "expresion_compartida", "mismo_hecho",
                                                       "afirmaciones_distintas", "referencia_explicita")))
             .order_by(ArticleRelation.score.desc()).limit(60))
    rels = [r for r in db.scalars(rel_q)
            if demo_visible(request) or not (r.article_a.is_demo or r.article_b.is_demo)]
    return render(request, "review.html", {"alerts": alerts, "rels": rels, "labels": TYPE_LABELS,
                                           "nav": "revision"}, user=user)


@router.get("/historial")
def history(request: Request, tipo: str = Query("titulares", max_length=16),
            page: int = Query(1, ge=1, le=10000), db: Session = Depends(get_db),
            user: User = Depends(require_user)):
    rows, total = [], 0
    if tipo == "titulares":
        # Artículos con más de una versión de origen "feed" (cambios hechos por el medio).
        changed = (select(HeadlineVersion.article_id).where(HeadlineVersion.origin == "feed")
                   .group_by(HeadlineVersion.article_id).having(func.count() > 1))
        stmt = select(Article).where(Article.id.in_(changed))
        if not demo_visible(request):
            stmt = stmt.where(Article.is_demo.is_(False))
        total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        page, pages = _pages(total, page)
        rows = db.scalars(stmt.options(selectinload(Article.versions), selectinload(Article.media))
                          .order_by(Article.last_seen_at.desc())
                          .offset((page - 1) * PER_PAGE).limit(PER_PAGE)).all()
    elif tipo == "alertas":
        stmt = select(AlertEvent)
        total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        page, pages = _pages(total, page)
        rows = [(e, _json(e.detail, {})) for e in db.scalars(
            stmt.options(selectinload(AlertEvent.alert)).order_by(AlertEvent.created_at.desc())
            .offset((page - 1) * PER_PAGE).limit(PER_PAGE))]
    else:
        tipo = "revisiones"
        stmt = select(ManualReview)
        total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        page, pages = _pages(total, page)
        rows = db.scalars(stmt.order_by(ManualReview.created_at.desc())
                          .offset((page - 1) * PER_PAGE).limit(PER_PAGE)).all()
    users = dict(db.execute(select(User.id, User.username)).all())
    return render(request, "history.html", {"tipo": tipo, "rows": rows, "page": page, "pages": pages,
                                            "total": total, "users": users, "nav": "historial"},
                  user=user)


@router.get("/ejecuciones")
def runs(request: Request, tipo: str = Query("", max_length=16), solo_errores: bool = Query(False),
         page: int = Query(1, ge=1, le=10000), db: Session = Depends(get_db),
         user: User = Depends(require_user)):
    stmt = select(CollectionRun)
    if tipo in ("feed", "pagina", "analisis"):
        stmt = stmt.where(CollectionRun.run_type == tipo)
    if solo_errores:
        stmt = stmt.where(CollectionRun.status.in_(("error", "blocked", "empty")))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    page, pages = _pages(total, page)
    items = db.scalars(stmt.options(selectinload(CollectionRun.subsource).selectinload(Subsource.media),
                                    selectinload(CollectionRun.media))
                       .order_by(CollectionRun.started_at.desc())
                       .offset((page - 1) * PER_PAGE).limit(PER_PAGE)).all()
    since = utcnow() - timedelta(hours=24)
    stats = dict(db.execute(select(CollectionRun.status, func.count())
                            .where(CollectionRun.started_at >= since)
                            .group_by(CollectionRun.status)).all())
    return render(request, "runs.html", {"runs": items, "tipo": tipo, "solo_errores": solo_errores,
                                         "page": page, "pages": pages, "total": total,
                                         "stats": stats, "nav": "ejecuciones"}, user=user)


@router.get("/cronologia")
def chronology(request: Request, dias: int = Query(3, ge=1, le=30), db: Session = Depends(get_db),
               user: User = Depends(viewer)):
    since = utcnow() - timedelta(days=dias)
    groups = db.scalars(
        select(StoryGroup)
        .options(selectinload(StoryGroup.members).selectinload(StoryGroupMember.article)
                 .selectinload(Article.media))
        .where(StoryGroup.status == "open", StoryGroup.first_seen_at >= since)
        .order_by(StoryGroup.first_seen_at.desc())).all()
    items = []
    for g in groups:
        arts = [m.article for m in g.members if m.status == "activo"]
        if not demo_visible(request) and any(a.is_demo for a in arts):
            continue
        timed = [a for a in arts if a.published_precision == "datetime" and a.published_at]
        items.append({
            "g": g,
            "first_detected": min(arts, key=lambda a: a.first_seen_at) if arts else None,
            "first_published": min(timed, key=lambda a: a.published_at) if timed else None,
            "timed": len(timed), "total": len(arts),
        })
    return render(request, "chronology.html", {"items": items, "dias": dias, "nav": "cronologia"},
                  user=user)


@router.get("/metricas")
def metrics(request: Request, db: Session = Depends(get_db), user: User = Depends(viewer)):
    data = sequence_metrics(db, include_demo=demo_visible(request))
    return render(request, "metrics.html", {"m": data, "nav": "metricas"}, user=user)
