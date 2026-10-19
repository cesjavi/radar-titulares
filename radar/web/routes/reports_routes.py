"""Informes por período: vista, exportación a CSV y fotos guardadas."""

from __future__ import annotations

import json
from datetime import date, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from radar import reports
from radar.actor_report import actor_graph, actor_report
from radar.ai.config import load_config as load_ai_config
from radar.ai.service import blocked_reason
from radar.ai.summary import SUMMARY_REVIEW, generate_summary
from radar.analysis.headline_changes import CATEGORIES, change_list
from radar.models import AiSummary, Media, SavedReport, User
from radar.timeutil import to_local, utcnow
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
DEFAULT_DAYS = 7


class PeriodError(ValueError):
    pass


def _parse_period(desde: str, hasta: str) -> tuple[date, date]:
    today = to_local(utcnow()).date()
    try:
        last = date.fromisoformat(hasta) if hasta else today
        first = date.fromisoformat(desde) if desde else last - timedelta(days=DEFAULT_DAYS - 1)
    except ValueError as exc:
        raise PeriodError("Fecha inválida: usá el formato AAAA-MM-DD.") from exc
    if first > last:
        raise PeriodError("La fecha «desde» es posterior a «hasta».")
    if (last - first).days + 1 > reports.MAX_DAYS:
        raise PeriodError(f"El período no puede superar los {reports.MAX_DAYS} días.")
    return first, last


def _media_options(db: Session, request: Request) -> list[Media]:
    stmt = select(Media).order_by(Media.name)
    if not demo_visible(request):
        stmt = stmt.where(Media.is_demo.is_(False))
    return list(db.scalars(stmt))


def _find_media(options: list[Media], slug: str) -> Media | None:
    if not slug:
        return None
    for m in options:
        if m.slug == slug:
            return m
    raise PeriodError("Ese medio no existe.")


def _build(request: Request, db: Session, desde: str, hasta: str, medio: str):
    first, last = _parse_period(desde, hasta)
    media = _find_media(_media_options(db, request), medio)
    since, until = reports.period_from_dates(first, last)
    data = reports.build(db, since, until, media, include_demo=demo_visible(request),
                         public=is_public(request))
    return first, last, media, data


def _csv_response(data: dict, seccion: str, filename: str) -> Response:
    if seccion not in reports.SECTIONS:
        raise HTTPException(status_code=404, detail="Esa sección no existe.")
    return Response(reports.to_csv(data, seccion), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/informes")
def report_view(request: Request, desde: str = Query("", max_length=10),
                hasta: str = Query("", max_length=10), medio: str = Query("", max_length=64),
                error: str = Query("", max_length=200), db: Session = Depends(get_db),
                user: User = Depends(viewer)):
    options = _media_options(db, request)
    today = to_local(utcnow()).date()
    try:
        first, last, media, data = _build(request, db, desde, hasta, medio)
    except PeriodError as exc:
        error = str(exc)
        first, last, media, data = _build(request, db, "", "", "")
    query = urlencode({"desde": first.isoformat(), "hasta": last.isoformat(),
                       "medio": media.slug if media else ""})
    saved = []
    if not getattr(user, "is_anonymous", False):
        saved = db.scalars(select(SavedReport).order_by(SavedReport.created_at.desc())
                           .limit(50)).all()
    return render(request, "reports.html", {
        "data": data, "desde": first, "hasta": last, "medio": media.slug if media else "",
        "media_options": options, "query": query, "csv_base": f"/informes/csv?{query}&",
        "saved": saved, "report": None, "error": error, "sections": reports.SECTIONS,
        "suggested_title": reports.default_title(*reports.period_from_dates(first, last),
                                                 media.name if media else None),
        "today": today, "nav": "informes",
        "shortcuts": [(n, (today - timedelta(days=n - 1)).isoformat()) for n in (7, 30, 90)]},
        user=user)


@router.get("/informes/csv")
def report_csv(request: Request, seccion: str = Query(..., max_length=32),
               desde: str = Query("", max_length=10), hasta: str = Query("", max_length=10),
               medio: str = Query("", max_length=64), db: Session = Depends(get_db),
               user: User = Depends(viewer)):
    try:
        first, last, _media, data = _build(request, db, desde, hasta, medio)
    except PeriodError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _csv_response(data, seccion, f"informe-{first}-{last}-{seccion}.csv")


@router.post("/informes/guardar", dependencies=[Depends(verify_csrf)])
def report_save(request: Request, desde: str = Form(""), hasta: str = Form(""),
                medio: str = Form(""), titulo: str = Form(""), db: Session = Depends(get_db),
                user: User = Depends(require_admin)):
    try:
        first, last, media, data = _build(request, db, desde, hasta, medio)
    except PeriodError as exc:
        back = urlencode({"desde": desde, "hasta": hasta, "medio": medio, "error": str(exc)})
        return RedirectResponse(f"/informes?{back}", status_code=303)
    since, until = reports.period_from_dates(first, last)
    title = " ".join(titulo.split())[:200] or reports.default_title(
        since, until, media.name if media else None)
    row = SavedReport(title=title, period_start=since, period_end=until,
                      media_slug=media.slug if media else None,
                      data=json.dumps(data, ensure_ascii=False),
                      algorithm_version=data["algorithm_version"], created_by_id=user.id)
    db.add(row)
    db.flush()
    return RedirectResponse(f"/informes/{row.id}", status_code=303)


# --- Informe por nombre ------------------------------------------------------------------


def _period_or_default(desde: str, hasta: str) -> tuple[date, date, str]:
    try:
        first, last = _parse_period(desde, hasta)
        return first, last, ""
    except PeriodError as exc:
        first, last = _parse_period("", "")
        return first, last, str(exc)


@router.get("/informes/nombre")
def report_actor(request: Request, q: str = Query("", max_length=80),
                 desde: str = Query("", max_length=10), hasta: str = Query("", max_length=10),
                 db: Session = Depends(get_db), user: User = Depends(viewer)):
    first, last, error = _period_or_default(desde, hasta)
    data = None
    if q.strip():
        since, until = reports.period_from_dates(first, last)
        data = actor_report(db, q, since, until, include_demo=demo_visible(request))
    query = urlencode({"q": q, "desde": first.isoformat(), "hasta": last.isoformat()})
    return render(request, "report_actor.html", {
        "data": data, "q": q, "desde": first, "hasta": last, "error": error, "query": query,
        "today": to_local(utcnow()).date(), "nav": "informes", "tab": "nombre"}, user=user)


@router.get("/informes/nombre/grafo")
def report_actor_graph(request: Request, q: str = Query("", max_length=80),
                       desde: str = Query("", max_length=10), hasta: str = Query("", max_length=10),
                       db: Session = Depends(get_db), user: User = Depends(viewer)):
    first, last, _error = _period_or_default(desde, hasta)
    since, until = reports.period_from_dates(first, last)
    data = actor_report(db, q, since, until, include_demo=demo_visible(request))
    return JSONResponse(actor_graph(data))


# --- Cambios de titular, nota por nota ---------------------------------------------------


def _headline_data(request: Request, db: Session, desde: str, hasta: str, medio: str,
                   categoria: str):
    first, last, error = _period_or_default(desde, hasta)
    options = _media_options(db, request)
    try:
        media = _find_media(options, medio)
    except PeriodError as exc:
        media, error = None, str(exc)
    categoria = categoria if categoria in CATEGORIES else ""
    since, until = reports.period_from_dates(first, last)
    data = change_list(db, since, until, media.id if media else None, categoria or None,
                       include_demo=demo_visible(request))
    return first, last, media, categoria, options, data, error


@router.get("/informes/titulares")
def report_headlines(request: Request, desde: str = Query("", max_length=10),
                     hasta: str = Query("", max_length=10), medio: str = Query("", max_length=64),
                     categoria: str = Query("", max_length=16), db: Session = Depends(get_db),
                     user: User = Depends(viewer)):
    first, last, media, categoria, options, data, error = _headline_data(
        request, db, desde, hasta, medio, categoria)
    query = urlencode({"desde": first.isoformat(), "hasta": last.isoformat(),
                       "medio": media.slug if media else "", "categoria": categoria})
    return render(request, "report_headlines.html", {
        "data": data, "desde": first, "hasta": last, "medio": media.slug if media else "",
        "media_name": media.name if media else None, "categoria": categoria,
        "media_options": options, "error": error, "query": query,
        "today": to_local(utcnow()).date(), "nav": "informes", "tab": "titulares"}, user=user)


@router.get("/informes/titulares/csv")
def report_headlines_csv(request: Request, desde: str = Query("", max_length=10),
                         hasta: str = Query("", max_length=10),
                         medio: str = Query("", max_length=64),
                         categoria: str = Query("", max_length=16),
                         db: Session = Depends(get_db), user: User = Depends(viewer)):
    first, last, _media, _cat, _opts, data, _error = _headline_data(
        request, db, desde, hasta, medio, categoria)
    rows = []
    for item in data["items"]:
        for i, v in enumerate(item["versions"]):
            rows.append([item["article_id"], item["media"], i + 1,
                         to_local(v["detected_at"]).strftime("%d/%m/%Y %H:%M"), v["title"],
                         v["subtitle"] or "", ", ".join(CATEGORIES[c] for c in v["categories"])])
    body = reports.rows_to_csv(["Nota", "Medio", "Versión", "Detectada", "Titular", "Bajada",
                                "Qué cambió respecto de la anterior"], rows)
    return Response(body, media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="cambios-de-titular-{first}-{last}.csv"'})


# --- Resúmenes con IA (borradores con revisión) ------------------------------------------


def _summary_visible(row: AiSummary | None, request: Request) -> bool:
    if row is None:
        return False
    return not is_public(request) or (row.status == "ok" and row.review_status == "aprobado")


def _back(path: str, message: str) -> RedirectResponse:
    return RedirectResponse(f"{path}?{urlencode({'mensaje': message})}", status_code=303)


@router.get("/informes/resumenes")
def summary_list(request: Request, mensaje: str = Query("", max_length=300),
                 db: Session = Depends(get_db), user: User = Depends(viewer)):
    stmt = select(AiSummary).order_by(AiSummary.created_at.desc()).limit(50)
    if is_public(request):
        stmt = stmt.where(AiSummary.status == "ok", AiSummary.review_status == "aprobado")
    rows = db.scalars(stmt).all()
    today = to_local(utcnow()).date()
    return render(request, "report_summaries.html", {
        "rows": rows, "message": mensaje, "nav": "informes", "tab": "resumenes",
        "default_desde": today - timedelta(days=6), "today": today,
        "blocked": blocked_reason(db, load_ai_config(db)) if getattr(user, "is_admin", False) else None,
        "parsed": {r.id: reports._json(r.result, {}) for r in rows},
        "last_day": {r.id: to_local(r.period_end - timedelta(microseconds=1)).date() for r in rows}},
        user=user)


@router.post("/informes/resumenes/generar", dependencies=[Depends(verify_csrf)])
def summary_generate(request: Request, desde: str = Form(""), hasta: str = Form(""),
                     db: Session = Depends(get_db), user: User = Depends(require_admin)):
    try:
        first, last = _parse_period(desde, hasta)
    except PeriodError as exc:
        return _back("/informes/resumenes", str(exc))
    since, until = reports.period_from_dates(first, last)
    row, message = generate_summary(db, since, until, user)
    if row is None:
        return _back("/informes/resumenes", message)
    db.flush()
    return _back(f"/informes/resumenes/{row.id}", message)


@router.get("/informes/resumenes/{summary_id}")
def summary_detail(request: Request, summary_id: int, mensaje: str = Query("", max_length=300),
                   db: Session = Depends(get_db), user: User = Depends(viewer)):
    row = db.get(AiSummary, summary_id)
    if not _summary_visible(row, request):
        raise HTTPException(status_code=404, detail="El resumen no existe.")
    reviewer = db.get(User, row.reviewed_by_id) if row.reviewed_by_id else None
    return render(request, "report_summary.html", {
        "row": row, "out": reports._json(row.result, {}), "errors": reports._json(row.errors, []),
        "input_text": json.dumps(reports._json(row.input_data, {}), ensure_ascii=False, indent=2),
        "message": mensaje,
        "reviewer": reviewer.username if reviewer else None,
        "first": to_local(row.period_start).date(),
        "last": to_local(row.period_end - timedelta(microseconds=1)).date(),
        "nav": "informes", "tab": "resumenes"}, user=user)


@router.post("/informes/resumenes/{summary_id}/revisar", dependencies=[Depends(verify_csrf)])
def summary_review(summary_id: int, decision: str = Form(...), db: Session = Depends(get_db),
                   user: User = Depends(require_admin)):
    row = db.get(AiSummary, summary_id)
    if row is None:
        raise HTTPException(status_code=404, detail="El resumen no existe.")
    if decision not in SUMMARY_REVIEW or (decision == "aprobado" and row.status != "ok"):
        raise HTTPException(status_code=400, detail="Decisión inválida.")
    row.review_status = decision
    row.reviewed_by_id = user.id if decision != "pendiente" else None
    row.reviewed_at = utcnow() if decision != "pendiente" else None
    return RedirectResponse(f"/informes/resumenes/{summary_id}", status_code=303)


def _saved(db: Session, report_id: int) -> tuple[SavedReport, dict]:
    row = db.get(SavedReport, report_id)
    if row is None:
        raise HTTPException(status_code=404, detail="El informe no existe.")
    try:
        data = json.loads(row.data)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="El informe guardado está dañado.") from exc
    return row, data


@router.get("/informes/{report_id}")
def report_saved(request: Request, report_id: int, db: Session = Depends(get_db),
                 user: User = Depends(require_user)):
    row, data = _saved(db, report_id)
    first = to_local(row.period_start).date()
    last = to_local(row.period_end - timedelta(microseconds=1)).date()
    author = db.get(User, row.created_by_id) if row.created_by_id else None
    return render(request, "reports.html", {
        "data": data, "desde": first, "hasta": last, "medio": row.media_slug or "",
        "media_options": _media_options(db, request), "report": row,
        "author": author.username if author else None,
        "csv_base": f"/informes/{row.id}/csv?", "saved": [], "error": "",
        "sections": reports.SECTIONS, "nav": "informes",
        "query": urlencode({"desde": first.isoformat(), "hasta": last.isoformat(),
                            "medio": row.media_slug or ""})}, user=user)


@router.get("/informes/{report_id}/csv")
def report_saved_csv(report_id: int, seccion: str = Query(..., max_length=32),
                     db: Session = Depends(get_db), user: User = Depends(require_user)):
    _row, data = _saved(db, report_id)
    return _csv_response(data, seccion, f"informe-{report_id}-{seccion}.csv")


@router.post("/informes/{report_id}/borrar", dependencies=[Depends(verify_csrf)])
def report_delete(report_id: int, db: Session = Depends(get_db),
                  user: User = Depends(require_admin)):
    row = db.get(SavedReport, report_id)
    if row is None:
        raise HTTPException(status_code=404, detail="El informe no existe.")
    db.delete(row)
    return RedirectResponse("/informes", status_code=303)
