"""Informes por período: vista, exportación a CSV y fotos guardadas."""

from __future__ import annotations

import json
from datetime import date, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from radar import reports
from radar.models import Media, SavedReport, User
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
