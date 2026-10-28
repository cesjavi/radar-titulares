from __future__ import annotations

import json
import math
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from radar.analysis.attribution import find_attributions
from radar.analysis.manual import set_article_hidden
from radar.analysis.relations import TYPE_LABELS
from radar.config import get_settings
from radar.interactions import record as record_interaction
from radar.models import (
    Article,
    ArticleRelation,
    ArticleSighting,
    ManualReview,
    Media,
    StoryGroup,
    StoryGroupMember,
    Topic,
    User,
)
from radar.queries import matching_topics, text_condition, topic_condition, visible_articles
from radar.sources_status import STATE_LABELS, media_health
from radar.timeutil import utcnow
from radar.web.deps import (
    demo_visible,
    get_db,
    render,
    require_analyst,
    safe_return,
    verify_csrf,
    viewer,
)

router = APIRouter()

PER_PAGE = 30


@router.get("/noticias")
def article_list(
    request: Request,
    page: int = Query(1, ge=1, le=100000),
    media: str = Query("", max_length=64),
    topic: str = Query("", max_length=64),
    q: str = Query("", max_length=200),
    seccion: str = Query("", max_length=64),
    periodo: str = Query("", max_length=8),
    autor: str = Query("", max_length=16),
    relacionadas: bool = Query(False),
    min_clicks: int = Query(0, ge=0),
    orden: str = Query("recientes", max_length=16),
    ocultas: bool = Query(False),
    db: Session = Depends(get_db),
    user: User = Depends(viewer),
):
    demo = demo_visible(request)
    # Las ocultas a mano solo se ven pidiéndolas (para volver a mostrarlas).
    stmt = visible_articles(demo, hidden=ocultas)
    if seccion:
        stmt = stmt.where(Article.section == seccion)
    if autor == "con_autor":
        stmt = stmt.where(Article.author.is_not(None), Article.author != "")
    elif autor == "sin_autor":
        stmt = stmt.where(or_(Article.author.is_(None), Article.author == ""))
    hours = {"24h": 24, "72h": 72, "7d": 168, "30d": 720}.get(periodo)
    if hours:
        stmt = stmt.where(Article.first_seen_at >= utcnow() - timedelta(hours=hours))
    if relacionadas:
        related = select(ArticleRelation.article_a_id).union(select(ArticleRelation.article_b_id))
        stmt = stmt.where(Article.id.in_(related))
    if min_clicks > 0:
        stmt = stmt.where(Article.clicks >= min_clicks)
    if media:
        stmt = stmt.join(Media).where(Media.slug == media)
    selected_topic = None
    if topic:
        selected_topic = db.scalar(select(Topic).where(Topic.slug == topic))
        cond = topic_condition(selected_topic) if selected_topic else None
        if cond is not None:
            stmt = stmt.where(cond)
    if q.strip():
        cond = text_condition(q)
        if cond is not None:
            stmt = stmt.where(cond)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    pages = max(1, math.ceil(total / PER_PAGE))
    page = min(page, pages)
    
    order_clause = (
        [Article.clicks.desc(), Article.first_seen_at.desc(), Article.id.desc()]
        if orden == "clicks"
        else [Article.first_seen_at.desc(), Article.id.desc()]
    )

    rows = db.scalars(
        stmt.options(selectinload(Article.media))
        .order_by(*order_clause)
        .offset((page - 1) * PER_PAGE)
        .limit(PER_PAGE)
    ).all()
    record_interaction(db, [article.id for article in rows], getattr(user, "id", None),
                       "impresion", f"noticias:{orden}",
                       list(range((page - 1) * PER_PAGE + 1, (page - 1) * PER_PAGE + 1 + len(rows))))

    media_q = select(Media).order_by(Media.is_demo, Media.name)
    if not demo:
        media_q = media_q.where(Media.is_demo.is_(False))
    media_warning = None
    if media:
        selected = next((m for m in db.scalars(media_q) if m.slug == media), None)
        if selected is not None:
            state = media_health(db, demo).get(selected.id)
            if state in ("caida", "bloqueada", "con_fallas"):
                media_warning = (f"{selected.name}: fuentes en estado «{STATE_LABELS[state]}». "
                                 "Los resultados pueden estar incompletos; ver Estado de fuentes.")
    ctx = {
        "media_warning": media_warning,
        "articles": rows, "total": total, "page": page, "pages": pages,
        "filters": {"media": media, "topic": topic, "q": q, "seccion": seccion,
                    "periodo": periodo, "autor": autor, "relacionadas": relacionadas,
                    "min_clicks": min_clicks, "orden": orden, "ocultas": ocultas},
        "hidden_total": db.scalar(select(func.count()).select_from(
            visible_articles(demo, hidden=True).subquery())) or 0,
        "section_options": [s for s in db.scalars(
            select(Article.section).where(Article.section.is_not(None))
            .group_by(Article.section).order_by(func.count().desc()).limit(40))],
        "media_options": db.scalars(media_q).all(),
        "topic_options": db.scalars(select(Topic).order_by(Topic.name)).all(),
        "nav": "noticias",
    }
    template = "partials/article_table.html" if request.headers.get("hx-request") else "articles.html"
    return render(request, template, ctx, user=user)


@router.post("/noticias/{article_id}/click", dependencies=[Depends(verify_csrf)])
def register_article_click(article_id: int, db: Session = Depends(get_db),
                           user: User = Depends(viewer),
                           context: str = Query("original", max_length=64),
                           rank: int | None = Query(None, ge=1, le=10000)):
    """Se abrió la nota original desde el panel (lo envía panel.js)."""
    db.execute(
        update(Article).where(Article.id == article_id).values(clicks=Article.clicks + 1)
    )
    record_interaction(db, [article_id], getattr(user, "id", None), "original", context,
                       [rank] if rank is not None else None)
    return {"ok": True}


@router.post("/noticias/{article_id}/ocultar", dependencies=[Depends(verify_csrf)])
def article_hide(article_id: int, ocultar: bool = Form(True),
                 volver: str = Form("", max_length=500),
                 db: Session = Depends(get_db), user: User = Depends(require_analyst)):
    article = db.get(Article, article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="La noticia no existe.")
    set_article_hidden(db, article, ocultar, user)
    if ocultar:
        record_interaction(db, [article.id], getattr(user, "id", None), "ocultar", "manual")
    return RedirectResponse(safe_return(volver, f"/noticias/{article_id}"), status_code=303)


@router.get("/noticias/{article_id}")
def article_detail(
    request: Request, article_id: int,
    db: Session = Depends(get_db), user: User = Depends(viewer),
):
    article = db.scalar(
        select(Article)
        .options(selectinload(Article.media), selectinload(Article.subsource),
                 selectinload(Article.versions), selectinload(Article.references),
                 selectinload(Article.sightings).selectinload(ArticleSighting.subsource))
        .where(Article.id == article_id)
    )
    if article is None or (article.is_demo and not demo_visible(request)):
        raise HTTPException(status_code=404, detail="La noticia no existe.")

    # Incrementar contador de clicks al ver la noticia
    db.execute(
        update(Article).where(Article.id == article.id).values(clicks=Article.clicks + 1)
    )
    record_interaction(db, [article.id], getattr(user, "id", None), "detalle", "ficha")
    db.commit()

    relations = db.scalars(
        select(ArticleRelation).where(
            or_(ArticleRelation.article_a_id == article.id,
                ArticleRelation.article_b_id == article.id)
        )
    ).all()
    related_ids = {r.article_b_id if r.article_a_id == article.id else r.article_a_id
                   for r in relations}
    related = {a.id: a for a in db.scalars(
        select(Article).options(selectinload(Article.media)).where(Article.id.in_(related_ids))
    )} if related_ids else {}
    relation_rows = []
    for r in relations:
        other = related.get(r.article_b_id if r.article_a_id == article.id else r.article_a_id)
        try:
            evidence = json.loads(r.evidence) if r.evidence else None
        except ValueError:
            evidence = None
        relation_rows.append((r, other, evidence))

    reviews = db.scalars(
        select(ManualReview)
        .where(ManualReview.target_type == "article", ManualReview.target_id == article.id)
        .order_by(ManualReview.created_at.desc())
    ).all()
    topics = db.scalars(select(Topic).where(Topic.enabled.is_(True))).all()

    groups = db.scalars(select(StoryGroup).join(StoryGroupMember).where(
        StoryGroupMember.article_id == article.id, StoryGroupMember.status == "activo",
        StoryGroup.status == "open")).all()
    return render(request, "article_detail.html", {
        "a": article, "relations": relation_rows, "reviews": reviews,
        "groups": groups, "rel_labels": TYPE_LABELS,
        "topic_hits": matching_topics(article, topics), "nav": "noticias",
        # Las referencias a otros medios ya tienen su propia sección.
        "attributions": find_attributions(article),
    }, user=user)
