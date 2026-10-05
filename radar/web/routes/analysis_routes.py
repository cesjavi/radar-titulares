"""Grupos, comparación de relaciones y correcciones manuales."""

from __future__ import annotations

import json
import math
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from radar.ai.config import load_config as load_ai_config
from radar.ai.providers import make_provider
from radar.ai.service import (
    analyze_for_admin,
    analyze_group_for_admin,
    blocked_reason,
    group_articles,
    latest_for_group,
    pair_filter,
)
from radar.analysis.manual import ManualError, merge_groups, remove_member, review_relation, split_group
from radar.analysis.relations import RULES, TYPE_LABELS
from radar.analysis.textproc import highlight
from radar.config import get_settings
from radar.queries import title_condition
from radar.models import (
    AiAnalysis,
    Article,
    ArticleRelation,
    ManualReview,
    StoryGroup,
    StoryGroupMember,
    User,
)
from radar.web.deps import demo_visible, get_db, render, require_admin, verify_csrf, viewer

router = APIRouter()
PER_PAGE = 30
GROUP_ORDERS = {
    "recientes": "Primera aparición (más reciente)",
    "antiguos": "Primera aparición (más antigua)",
    "notas": "Más notas",
    "medios": "Más medios independientes",
    "actualizados": "Última actualización",
}


def _json(value: str | None, default):
    try:
        return json.loads(value) if value else default
    except ValueError:
        return default


def _load_group(db: Session, group_id: int) -> StoryGroup:
    group = db.scalar(
        select(StoryGroup)
        .options(selectinload(StoryGroup.members).selectinload(StoryGroupMember.article)
                 .selectinload(Article.media))
        .where(StoryGroup.id == group_id))
    if group is None:
        raise HTTPException(status_code=404, detail="El grupo no existe.")
    return group


def _visible(article: Article, request: Request) -> bool:
    return not article.is_demo or demo_visible(request)


@router.get("/grupos")
def group_list(request: Request, q: str = Query("", max_length=100),
               min_medios: int = Query(0, ge=0, le=20), corregidos: bool = Query(False),
               orden: str = Query("recientes", max_length=16),
               page: int = Query(1, ge=1, le=10000),
               db: Session = Depends(get_db), user: User = Depends(viewer)):
    stmt = select(StoryGroup).where(StoryGroup.status == "open", StoryGroup.article_count >= 1)
    q = q.strip()
    cond = title_condition(StoryGroup.title, q) if q else None
    if cond is not None:
        stmt = stmt.where(cond)
    if min_medios:
        stmt = stmt.where(StoryGroup.independent_media_count >= min_medios)
    if corregidos:
        stmt = stmt.where(StoryGroup.locked.is_(True))
    if orden not in GROUP_ORDERS:
        orden = "recientes"
    order = {"recientes": [StoryGroup.first_seen_at.desc().nulls_last()],
             "antiguos": [StoryGroup.first_seen_at.asc().nulls_last()],
             "notas": [StoryGroup.article_count.desc()],
             "medios": [StoryGroup.independent_media_count.desc(), StoryGroup.article_count.desc()],
             "actualizados": [StoryGroup.updated_at.desc()]}[orden]
    if not demo_visible(request):
        demo_groups = (select(StoryGroupMember.group_id).join(Article)
                       .where(Article.is_demo.is_(True)))
        stmt = stmt.where(StoryGroup.id.not_in(demo_groups))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    pages = max(1, math.ceil(total / PER_PAGE))
    groups = db.scalars(stmt.order_by(*order, StoryGroup.id.desc())
                        .offset((min(page, pages) - 1) * PER_PAGE).limit(PER_PAGE)).all()
    rows = [(g, _json(g.common_terms, {})) for g in groups]
    return render(request, "groups.html", {"rows": rows, "page": min(page, pages), "pages": pages,
                                           "total": total, "nav": "grupos",
                                           "q": q, "min_medios": min_medios, "corregidos": corregidos,
                                           "orden": orden, "orders": GROUP_ORDERS,
                                           "qs": urlencode({"q": q, "min_medios": min_medios or "",
                                                            "corregidos": "true" if corregidos else "",
                                                            "orden": orden})}, user=user)


@router.get("/grupos/{group_id}")
def group_detail(request: Request, group_id: int, db: Session = Depends(get_db),
                 user: User = Depends(viewer)):
    group = _load_group(db, group_id)
    if group.status == "merged" and group.merged_into_id:
        return RedirectResponse(f"/grupos/{group.merged_into_id}", status_code=303)
    active = [m for m in group.members if m.status == "activo" and _visible(m.article, request)]
    excluded = [m for m in group.members if m.status == "excluido" and _visible(m.article, request)]
    ids = [m.article_id for m in active]

    # Cronología: solo se ordenan las notas con hora de publicación. El resto va aparte.
    timed = sorted((m for m in active if m.article.published_precision == "datetime"),
                   key=lambda m: m.article.published_at)
    untimed = sorted((m for m in active if m.article.published_precision != "datetime"),
                     key=lambda m: m.article.first_seen_at)
    rels = db.scalars(
        select(ArticleRelation).where(ArticleRelation.article_a_id.in_(ids),
                                      ArticleRelation.article_b_id.in_(ids))
        .order_by(ArticleRelation.score.desc())).all() if ids else []
    antecedents = db.scalars(
        select(ArticleRelation)
        .options(selectinload(ArticleRelation.article_a).selectinload(Article.media),
                 selectinload(ArticleRelation.article_b).selectinload(Article.media))
        .where(ArticleRelation.relation_type == "reaparicion",
               or_(ArticleRelation.article_a_id.in_(ids), ArticleRelation.article_b_id.in_(ids)))
        .limit(20)).all() if ids else []
    titles = {m.article_id: m.article for m in group.members}
    reviews = db.scalars(select(ManualReview).where(ManualReview.target_type == "group",
                                                    ManualReview.target_id == group.id)
                         .order_by(ManualReview.created_at.desc())).all()
    media_names = sorted({m.article.media.name for m in active})
    independent = sorted({m.article.media.name for m in active if not m.article.is_syndicated})
    other_groups = db.scalars(select(StoryGroup).where(StoryGroup.status == "open",
                                                       StoryGroup.id != group.id)
                              .order_by(StoryGroup.id.desc()).limit(50)).all()
    ai_cfg = load_ai_config(db)
    ai = latest_for_group(db, group.id)
    ai_notes = []
    if ai:
        by_id = {m.article_id: m.article for m in group.members}
        ai_notes = [(chr(65 + i), by_id.get(aid))
                    for i, aid in enumerate(_json(ai.article_ids, []))]
    return render(request, "group_detail.html", {
        "g": group, "active": active, "excluded": excluded, "timed": timed, "untimed": untimed,
        "rels": rels, "titles": titles, "antecedents": antecedents, "reviews": reviews,
        "explanation": _json(group.explanation, {}), "common": _json(group.common_terms, {}),
        "media_names": media_names, "independent": independent, "labels": TYPE_LABELS,
        "other_groups": other_groups, "nav": "grupos", "error": request.query_params.get("error"),
        "ai": ai, "ai_data": _json(ai.result, {}) if ai else {},
        "ai_flags": _json(ai.flags, []) if ai else [], "ai_notes": ai_notes,
        "ai_enabled": ai_cfg.enabled, "ai_blocked": blocked_reason(db, ai_cfg),
        "ai_message": request.query_params.get("ia"),
        "ai_note_count": len(group_articles(group)),
    }, user=user)


@router.get("/relaciones/{rel_id}")
def relation_compare(request: Request, rel_id: int, db: Session = Depends(get_db),
                     user: User = Depends(viewer)):
    rel = db.scalar(select(ArticleRelation)
                    .options(selectinload(ArticleRelation.article_a).selectinload(Article.media),
                             selectinload(ArticleRelation.article_b).selectinload(Article.media))
                    .where(ArticleRelation.id == rel_id))
    if rel is None or not (_visible(rel.article_a, request) and _visible(rel.article_b, request)):
        raise HTTPException(status_code=404, detail="La relación no existe.")
    evidence = _json(rel.evidence, {})
    phrases = evidence.get("frases", [])
    terms = set(evidence.get("terminos", [])) | {t for e in evidence.get("entidades", [])
                                                  for t in e.split()}

    def hl(text):
        return highlight(text, phrases, terms) if text else []

    sides = []
    for art in (rel.article_a, rel.article_b):
        sides.append({"a": art, "title": hl(art.title), "subtitle": hl(art.subtitle)})
    groups = db.scalars(select(StoryGroup).join(StoryGroupMember).where(
        StoryGroupMember.article_id.in_([rel.article_a_id, rel.article_b_id]),
        StoryGroupMember.status == "activo", StoryGroup.status == "open").distinct()).all()
    reviews = db.scalars(select(ManualReview).where(ManualReview.target_type == "relation",
                                                    ManualReview.target_id == rel.id)
                         .order_by(ManualReview.created_at.desc())).all()
    ai_rows = db.scalars(select(AiAnalysis).where(
        pair_filter(AiAnalysis.article_a_id, AiAnalysis.article_b_id,
                    rel.article_a_id, rel.article_b_id))
        .order_by(AiAnalysis.created_at.desc()).limit(5)).all()
    ai_ok = next((x for x in ai_rows if x.status == "ok"), None)
    ai_cfg = load_ai_config(db)
    return render(request, "relation_compare.html", {
        "ai": ai_ok, "ai_data": _json(ai_ok.result, {}) if ai_ok else {},
        "ai_flags": _json(ai_ok.flags, []) if ai_ok else [], "ai_attempts": ai_rows,
        "ai_enabled": ai_cfg.enabled, "ai_blocked": blocked_reason(db, ai_cfg),
        "ai_message": request.query_params.get("ia"),
        "rel": rel, "sides": sides, "evidence": evidence, "scores": _json(rel.scores, {}),
        "rules": _json(rel.rules, []), "label": TYPE_LABELS.get(rel.relation_type, rel.relation_type),
        "groups": groups, "reviews": reviews, "nav": "grupos", "all_rules": RULES,
    }, user=user)


@router.post("/relaciones/{rel_id}/analizar-ia", dependencies=[Depends(verify_csrf)])
def relation_ai(rel_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    """Analiza con IA una relación ya detectada por las reglas (nunca texto arbitrario)."""
    rel = db.scalar(select(ArticleRelation)
                    .options(selectinload(ArticleRelation.article_a).selectinload(Article.media),
                             selectinload(ArticleRelation.article_b).selectinload(Article.media))
                    .where(ArticleRelation.id == rel_id))
    if rel is None:
        raise HTTPException(status_code=404, detail="La relación no existe.")
    message = analyze_for_admin(db, rel)
    return RedirectResponse(f"/relaciones/{rel_id}?ia={quote(message)}", status_code=303)


@router.post("/relaciones/{rel_id}/revisar", dependencies=[Depends(verify_csrf)])
def relation_review(rel_id: int, decision: str = Form(...), note: str = Form("", max_length=2000),
                    db: Session = Depends(get_db), user: User = Depends(require_admin)):
    rel = db.get(ArticleRelation, rel_id)
    if rel is None:
        raise HTTPException(status_code=404, detail="La relación no existe.")
    try:
        review_relation(db, rel, decision, user, note)
    except ManualError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Al confirmar un par, si la IA está activada se lo analiza (una vez: usa la caché).
    ai_cfg = load_ai_config(db)
    if decision == "confirmada" and ai_cfg.enabled and ai_cfg.analyze_on_confirm:
        db.commit()  # la revisión queda guardada pase lo que pase con el proveedor
        message = analyze_for_admin(db, rel)
        return RedirectResponse(f"/relaciones/{rel_id}?ia={quote(message)}", status_code=303)
    return RedirectResponse(f"/relaciones/{rel_id}", status_code=303)


def _group_action(group_id: int, db: Session, fn):
    group = _load_group(db, group_id)
    try:
        result = fn(group)
    except ManualError as exc:
        return RedirectResponse(f"/grupos/{group_id}?error={quote(str(exc))}", status_code=303)
    target = result.id if isinstance(result, StoryGroup) else group_id
    return RedirectResponse(f"/grupos/{target}", status_code=303)


@router.post("/grupos/{group_id}/analizar-ia", dependencies=[Depends(verify_csrf)])
def group_ai_analyze(group_id: int, db: Session = Depends(get_db),
                     user: User = Depends(require_admin)):
    """Analiza con IA todas las notas activas del grupo en una sola llamada."""
    group = _load_group(db, group_id)
    message = analyze_group_for_admin(db, group)
    return RedirectResponse(f"/grupos/{group_id}?ia={quote(message)}", status_code=303)


@router.post("/grupos/{group_id}/quitar", dependencies=[Depends(verify_csrf)])
def group_remove(group_id: int, article_id: int = Form(...), note: str = Form("", max_length=2000),
                 db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return _group_action(group_id, db, lambda g: remove_member(db, g, article_id, user, note))


@router.post("/grupos/{group_id}/separar", dependencies=[Depends(verify_csrf)])
async def group_split(group_id: int, request: Request, db: Session = Depends(get_db),
                      user: User = Depends(require_admin)):
    form = await request.form()
    try:
        ids = [int(x) for x in form.getlist("article_ids")]
    except ValueError:
        ids = []
    note = str(form.get("note", ""))[:2000]
    return _group_action(group_id, db, lambda g: split_group(db, g, ids, user, note))


@router.post("/grupos/{group_id}/unir", dependencies=[Depends(verify_csrf)])
def group_merge(group_id: int, other_id: int = Form(...), note: str = Form("", max_length=2000),
                db: Session = Depends(get_db), user: User = Depends(require_admin)):
    other = db.get(StoryGroup, other_id)
    if other is None:
        return RedirectResponse(f"/grupos/{group_id}?error={quote('El otro grupo no existe.')}",
                                status_code=303)
    return _group_action(group_id, db, lambda g: merge_groups(db, g, other, user, note))
