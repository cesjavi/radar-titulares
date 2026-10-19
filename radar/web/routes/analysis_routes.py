"""Grupos, comparación de relaciones y correcciones manuales."""

from __future__ import annotations

import json
import math
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from radar.ai.config import load_config as load_ai_config
from radar.ai.providers import make_provider
from radar.ai.service import (
    analyze_for_admin,
    analyze_group_for_admin,
    blocked_reason,
    group_articles,
    REVIEW_STATES,
    current_group_analysis,
    latest_for_group,
    pair_filter,
)
from radar.analysis import ALGORITHM_VERSION, evaluation
from radar.analysis.manual import (
    ManualError,
    merge_groups,
    remove_member,
    review_group_ai,
    review_relation,
    set_group_hidden,
    split_group,
)
from radar.analysis.relations import RULES, TYPE_LABELS
from radar.analysis.textproc import highlight
from radar.config import get_settings
from radar.graph import group_graph
from radar.queries import title_condition
from radar.models import (
    AiAnalysis,
    AiGroupAnalysis,
    Article,
    ArticleRelation,
    ManualReview,
    Media,
    StoryGroup,
    StoryGroupMember,
    User,
)
from radar.web.deps import (
    demo_visible,
    get_db,
    is_public,
    render,
    require_admin,
    require_analyst,
    safe_return,
    verify_csrf,
    viewer,
)

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
               ocultos: bool = Query(False),
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
    # Los ocultos a mano solo se ven pidiéndolos (para volver a mostrarlos).
    stmt = stmt.where(StoryGroup.hidden_at.is_not(None) if ocultos else StoryGroup.hidden_at.is_(None))
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
    groups = db.scalars(
        stmt.options(
            selectinload(StoryGroup.members)
            .selectinload(StoryGroupMember.article)
            .selectinload(Article.media)
        )
        .order_by(*order, StoryGroup.id.desc())
        .offset((min(page, pages) - 1) * PER_PAGE)
        .limit(PER_PAGE)
    ).all()
    gids = [g.id for g in groups]
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

    rows = []
    for g in groups:
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

        rows.append((g, common, media_list, sentiment_info))

    return render(request, "groups.html", {"rows": rows, "page": min(page, pages), "pages": pages,
                                           "total": total, "nav": "grupos",
                                           "q": q, "min_medios": min_medios, "corregidos": corregidos,
                                           "ocultos": ocultos, "orden": orden, "orders": GROUP_ORDERS,
                                           "qs": urlencode({k: v for k, v in {
                                               "q": q, "min_medios": min_medios,
                                               "corregidos": "true" if corregidos else "",
                                               "ocultos": "true" if ocultos else "",
                                               "orden": orden}.items() if v})}, user=user)


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
    ai = latest_for_group(db, group.id, public=is_public(request))
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


@router.get("/grupos/{group_id}/grafo")
def group_graph_data(request: Request, group_id: int, db: Session = Depends(get_db),
                     user: User = Depends(viewer)):
    """Nodos y aristas del grafo de la historia (lo dibuja static/js/graph.js)."""
    group = _load_group(db, group_id)
    articles = [m.article for m in group.members
                if m.status == "activo" and m.article is not None and _visible(m.article, request)]
    return JSONResponse(group_graph(db, group, articles))


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
                     user: User = Depends(require_analyst)):
    """Analiza con IA todas las notas activas del grupo en una sola llamada."""
    group = _load_group(db, group_id)
    message = analyze_group_for_admin(db, group)
    return RedirectResponse(f"/grupos/{group_id}?ia={quote(message)}", status_code=303)


@router.post("/grupos/{group_id}/ocultar", dependencies=[Depends(verify_csrf)])
def group_hide(group_id: int, request: Request, ocultar: bool = Form(True),
               volver: str = Form("", max_length=500),
               db: Session = Depends(get_db), user: User = Depends(require_analyst)):
    group = _load_group(db, group_id)
    set_group_hidden(db, group, ocultar, user)
    return RedirectResponse(safe_return(volver, f"/grupos/{group_id}"), status_code=303)


@router.post("/grupos/{group_id}/ia/{analysis_id}/revisar", dependencies=[Depends(verify_csrf)])
def group_ai_review(group_id: int, analysis_id: int, decision: str = Form(...),
                    note: str = Form("", max_length=2000), db: Session = Depends(get_db),
                    user: User = Depends(require_admin)):
    analysis = db.get(AiGroupAnalysis, analysis_id)
    if analysis is None or analysis.group_id != group_id:
        raise HTTPException(status_code=404, detail="El análisis no existe.")
    try:
        review_group_ai(db, analysis, decision, user, note)
    except ManualError as exc:
        return RedirectResponse(f"/grupos/{group_id}?ia={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/grupos/{group_id}#ia", status_code=303)


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


@router.get("/evaluacion")
def evaluation_page(request: Request, db: Session = Depends(get_db),
                    user: User = Depends(require_analyst)):
    """Muestra estratificada para medir la precisión del motor, de a una relación."""
    rel = evaluation.next_for_labeling(db, user, include_demo=demo_visible(request))
    evidence = _json(rel.evidence, {}) if rel else {}
    return render(request, "evaluation.html", {
        "rel": rel, "frases": evidence.get("frases", []),
        "question": evaluation.QUESTIONS.get(rel.relation_type, "¿Es correcta esta relación?")
        if rel else None,
        "label": TYPE_LABELS.get(rel.relation_type, rel.relation_type) if rel else None,
        "report": evaluation.precision_report(db), "labels": TYPE_LABELS,
        "choices": evaluation.LABELS, "target": evaluation.TARGET_PER_TYPE,
        "version": ALGORITHM_VERSION, "nav": "evaluacion",
    }, user=user)


@router.post("/evaluacion/{rel_id}", dependencies=[Depends(verify_csrf)])
def evaluation_label(rel_id: int, etiqueta: str = Form(..., max_length=16),
                     db: Session = Depends(get_db), user: User = Depends(require_analyst)):
    rel = db.get(ArticleRelation, rel_id)
    if rel is None:
        raise HTTPException(status_code=404, detail="La relación no existe.")
    try:
        evaluation.save_label(db, rel, user, etiqueta)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse("/evaluacion", status_code=303)


@router.get("/sentimiento")
def sentiment_dashboard(
    request: Request,
    valoracion: str = Query("", max_length=16),
    media: str = Query("", max_length=64),
    revision: str = Query("", max_length=16),
    db: Session = Depends(get_db),
    user: User = Depends(viewer),
):
    demo = demo_visible(request)
    public = is_public(request)
    # El análisis vigente de cada grupo (uno viejo no suma dos veces el tono de las mismas notas).
    # Los visitantes solo ven los confirmados y no pueden filtrar por revisión.
    revision = "" if public or revision not in REVIEW_STATES else revision
    group_analyses = db.scalars(
        select(AiGroupAnalysis)
        .join(StoryGroup, StoryGroup.id == AiGroupAnalysis.group_id)
        .where(current_group_analysis(public, revision or None), StoryGroup.hidden_at.is_(None))
        .order_by(AiGroupAnalysis.created_at.desc())
        .limit(100)
    ).all()

    all_article_ids = set()
    for ga in group_analyses:
        aids = _json(ga.article_ids, [])
        all_article_ids.update(aids)

    articles_map = {}
    if all_article_ids:
        arts = db.scalars(
            select(Article)
            .options(selectinload(Article.media))
            .where(Article.id.in_(all_article_ids))
        ).all()
        articles_map = {a.id: a for a in arts}

    group_ids = {ga.group_id for ga in group_analyses}
    groups_map = {}
    if group_ids:
        grps = db.scalars(
            select(StoryGroup)
            .options(
                selectinload(StoryGroup.members)
                .selectinload(StoryGroupMember.article)
                .selectinload(Article.media)
            )
            .where(StoryGroup.id.in_(group_ids))
        ).all()
        groups_map = {g.id: g for g in grps}

    # Lista por grupo para revisar las lecturas (solo usuarios con sesión).
    review_list = [] if public else [
        {"group_id": ga.group_id, "title": groups_map[ga.group_id].title,
         "review_status": ga.review_status, "created_at": ga.created_at}
        for ga in group_analyses if ga.group_id in groups_map and (demo or not any(
            m.article is not None and m.article.is_demo for m in groups_map[ga.group_id].members))]

    counts = {"favorable": 0, "critico": 0, "neutral": 0, "opuesto": 0}
    items = []
    opposed_groups = []

    for ga in group_analyses:
        grp = groups_map.get(ga.group_id)
        if grp and not demo:
            if any(m.article.is_demo for m in grp.members if m.article):
                continue

        res = _json(ga.result, {})
        flags = _json(ga.flags, [])
        enfoque = res.get("enfoque")
        tones = res.get("tono_por_nota", [])
        aids = _json(ga.article_ids, [])

        is_opposed = (enfoque == "opuesto") or (
            any(t.get("valoracion") == "favorable" for t in tones)
            and any(t.get("valoracion") == "critico" for t in tones)
        )
        if is_opposed:
            counts["opuesto"] += 1
            if grp:
                media_list = []
                seen_slugs = set()
                for m in grp.members:
                    if m.status == "activo" and m.article and m.article.media:
                        if m.article.media.slug not in seen_slugs:
                            media_list.append(m.article.media)
                            seen_slugs.add(m.article.media.slug)
                opposed_groups.append({
                    "id": grp.id,
                    "title": grp.title,
                    "media_list": media_list,
                    "flags": flags,
                    "review_status": ga.review_status,
                })

        for t in tones:
            val = t.get("valoracion", "incierto")
            if val in counts:
                counts[val] += 1

            letter = t.get("articulo")
            art = None
            if letter and len(letter) == 1 and "A" <= letter <= "Z":
                idx = ord(letter) - 65
                if 0 <= idx < len(aids):
                    art = articles_map.get(aids[idx])

            if art and not demo and art.is_demo:
                continue

            media_obj = art.media if art else None
            m_slug = media_obj.slug if media_obj else ""
            m_name = media_obj.name if media_obj else "Desconocido"

            item = {
                "group_id": ga.group_id,
                "article_id": art.id if art else None,
                "title": art.title if art else (grp.title if grp else "Nota"),
                "media_slug": m_slug,
                "media_name": m_name,
                "sujeto": t.get("sujeto", ""),
                "valoracion": val,
                "fragmento": t.get("fragmento", ""),
                "is_opposed": is_opposed,
                "review_status": ga.review_status,
            }

            if valoracion:
                if valoracion == "opuesto":
                    if not is_opposed:
                        continue
                elif val != valoracion:
                    continue

            if media and m_slug != media:
                continue

            items.append(item)

    media_q = select(Media).where(Media.enabled.is_(True)).order_by(Media.is_demo, Media.name)
    if not demo:
        media_q = media_q.where(Media.is_demo.is_(False))
    media_options = db.scalars(media_q).all()

    return render(
        request,
        "sentiment.html",
        {
            "counts": counts,
            "items": items,
            "opposed_groups": opposed_groups,
            "media_options": media_options,
            "sel_valoracion": valoracion,
            "sel_media": media,
            "sel_revision": revision,
            "review_list": review_list,
            "review_labels": {"pendiente": "Sin revisar", "confirmada": "Confirmadas",
                              "rechazada": "Rechazadas"},
            "nav": "sentimiento",
        },
        user=user,
    )
