"""Redes sociales: pantalla de posteos y administración de canales (Configuración)."""

from __future__ import annotations

import json
from collections import Counter

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from radar import runtime
from radar.collector.runner import _make_fetcher
from radar.config import get_settings
from radar.models import Article, PostArticleLink, SocialAccount, SocialPost, User
from radar.net import BlockedError, FetchError, UnsafeURLError
from radar.seed import seed_social_accounts
from radar.social import telegram as tg
from radar.social.collector import fetch_channel
from radar.social import x_import
from radar.social.match import LINK_LABELS, link_posts
from radar.social.references import x_status_refs
from radar.web.deps import get_db, render, require_admin, require_user, verify_csrf

router = APIRouter()

KINDS = {"persona": "Persona", "organizacion": "Organización", "medio": "Medio"}
REVIEW_STATES = {"pendiente", "confirmada", "rechazada"}
POSTS_LIMIT = 100
MAX_ACCOUNTS = 60


def _open_fetcher():
    """Descargador seguro (SSRF, robots.txt, límites). Las pruebas lo reemplazan."""
    return _make_fetcher(get_settings())


def _loads(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


@router.get("/redes")
def social_page(request: Request, cuenta: str = "", vinculados: str = "",
                db: Session = Depends(get_db), user: User = Depends(require_user)):
    return _social_page(request, db, user, cuenta, vinculados)


def _social_page(request, db, user, cuenta="", vinculados="", status_code=200, **extra):
    accounts = db.scalars(select(SocialAccount).order_by(SocialAccount.name)).all()
    counts = {aid: (n, last) for aid, n, last in db.execute(
        select(SocialPost.account_id, func.count(), func.max(SocialPost.posted_at))
        .group_by(SocialPost.account_id)).all()}
    q = (select(SocialPost)
         .options(selectinload(SocialPost.account),
                  selectinload(SocialPost.article_links).selectinload(PostArticleLink.article)
                  .selectinload(Article.media))
         .order_by(SocialPost.posted_at.desc()).limit(POSTS_LIMIT))
    if cuenta:
        q = q.join(SocialAccount).where(SocialAccount.handle == cuenta)
    if vinculados:
        q = q.where(SocialPost.id.in_(select(PostArticleLink.post_id).where(
            PostArticleLink.review_status != "rechazada")))
    posts = []
    for p in db.scalars(q):
        links = []
        for link in sorted(p.article_links, key=lambda x: -x.score):
            if link.review_status == "rechazada":
                continue
            ev = _loads(link.evidence)
            links.append({"link": link, "article": link.article, "label": LINK_LABELS.get(
                link.link_type, link.link_type), "terms": ev.get("terminos_distintivos") or [],
                "entities": ev.get("entidades_distintivas") or [],
                "notes": ev.get("notas") or []})
        posts.append({"post": p, "links": links, "x_refs": x_status_refs(json.loads(p.links or "[]"))})
    x_top = Counter(r["handle"] for item in posts for r in item["x_refs"] if r["handle"])
    return render(request, "social.html", {
        "nav": "redes", "accounts": accounts, "counts": counts, "posts": posts,
        "kinds": KINDS, "f_cuenta": cuenta, "f_vinculados": bool(vinculados),
        "social_enabled": runtime.get("social_enabled", db),
        "limit": POSTS_LIMIT, "x_top": x_top.most_common(10),
        "x_max_mb": x_import.MAX_FILE_BYTES // (1024 * 1024), **extra,
    }, user=user, status_code=status_code)


@router.post("/redes/importar-x", dependencies=[Depends(verify_csrf)])
async def import_x(request: Request, archivo: UploadFile = File(...),
                   db: Session = Depends(get_db), user: User = Depends(require_admin)):
    """Importa un archivo exportado con twitter-web-exporter. No se conecta a X."""
    content = await archivo.read(x_import.MAX_FILE_BYTES + 1)
    try:
        parsed = x_import.parse_export(content, archivo.filename or "")
    except x_import.XImportError as exc:
        return _social_page(request, db, user, status_code=400, import_error=str(exc))
    summary = x_import.import_posts(db, parsed, runtime.get("social_retention_days", db))
    links = link_posts(db)  # relaciona con las notas de las últimas 72 h
    return _social_page(request, db, user, import_result=summary, import_links=links)


@router.post("/redes/vinculo/{link_id}/revision", dependencies=[Depends(verify_csrf)])
def review_link(link_id: int, estado: str = Form(...), volver: str = Form("/redes"),
                db: Session = Depends(get_db), user: User = Depends(require_admin)):
    link = db.get(PostArticleLink, link_id)
    if link is None:
        raise HTTPException(status_code=404, detail="Vínculo inexistente.")
    if estado not in REVIEW_STATES:
        raise HTTPException(status_code=400, detail="Estado de revisión no válido.")
    link.review_status = estado
    target = volver if volver.startswith("/redes") and "//" not in volver else "/redes"
    return RedirectResponse(target, status_code=303)


# --- administración de canales (Configuración) -----------------------------------------------


def _settings_page(request, db, user, status_code=200, **extra):
    from radar.web.routes.settings_routes import _page

    return _page(request, db, user, status_code=status_code, **extra)


def _account(db: Session, account_id: int) -> SocialAccount:
    acc = db.get(SocialAccount, account_id)
    if acc is None:
        raise HTTPException(status_code=404, detail="Canal inexistente.")
    return acc


@router.post("/configuracion/redes/canal", dependencies=[Depends(verify_csrf)])
def account_add(request: Request, handle: str = Form(""), name: str = Form(""),
                kind: str = Form("persona"), db: Session = Depends(get_db),
                user: User = Depends(require_admin)):
    try:
        clean = tg.normalize_handle(handle)
        if kind not in KINDS:
            raise tg.ChannelError("Elegí el tipo de cuenta.")
        if db.scalar(select(SocialAccount).where(SocialAccount.platform == "telegram",
                                                 func.lower(SocialAccount.handle) == clean.lower())):
            raise tg.ChannelError(f"@{clean} ya está en la lista.")
        if (db.scalar(select(func.count()).select_from(SocialAccount)) or 0) >= MAX_ACCOUNTS:
            raise tg.ChannelError(f"Hay un máximo de {MAX_ACCOUNTS} canales.")
        # Se verifica en vivo: evita cargar canales inexistentes o sin vista web pública.
        with _open_fetcher() as fetcher:
            channel = fetch_channel(fetcher, clean, 1)
    except (tg.ChannelError, FetchError, BlockedError, UnsafeURLError) as exc:
        return _settings_page(request, db, user, 400, social_error=str(exc))
    db.add(SocialAccount(platform="telegram", handle=clean, kind=kind,
                         name=(name.strip() or channel.title or clean)[:128],
                         subscribers=channel.subscribers))
    db.flush()
    return RedirectResponse("/configuracion#redes", status_code=303)


@router.post("/configuracion/redes/canal/{account_id}", dependencies=[Depends(verify_csrf)])
def account_update(account_id: int, name: str = Form(""), kind: str = Form("persona"),
                   minutos: str = Form("10"), enabled: str | None = Form(None),
                   db: Session = Depends(get_db), user: User = Depends(require_admin)):
    acc = _account(db, account_id)
    if kind in KINDS:
        acc.kind = kind
    acc.name = name.strip()[:128] or acc.name
    acc.enabled = enabled is not None
    if minutos.strip().isdigit() and 5 <= int(minutos) <= 1440:
        acc.min_interval_seconds = int(minutos) * 60 - 30  # margen: el timer no cae justo
    return RedirectResponse("/configuracion#redes", status_code=303)


@router.post("/configuracion/redes/canal/{account_id}/quitar", dependencies=[Depends(verify_csrf)])
def account_delete(account_id: int, db: Session = Depends(get_db),
                   user: User = Depends(require_admin)):
    acc = _account(db, account_id)
    db.delete(acc)  # borra también sus posteos y vínculos (cascada en la base)
    db.flush()
    return RedirectResponse("/configuracion#redes", status_code=303)


@router.post("/configuracion/redes/sugeridos", dependencies=[Depends(verify_csrf)])
def account_defaults(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    """Agrega los canales sugeridos que falten (no toca los existentes)."""
    seed_social_accounts(db, force=True)
    return RedirectResponse("/configuracion#redes", status_code=303)


@router.post("/configuracion/redes/ajustes", dependencies=[Depends(verify_csrf)])
async def social_settings(request: Request, db: Session = Depends(get_db),
                          user: User = Depends(require_admin)):
    form = await request.form()
    try:
        values = {name: runtime.parse(name, form.get(name), label) for name, label in {
            "social_enabled": "Recolectar canales de Telegram",
            "social_retention_days": "Retención de posteos (días)"}.items()}
    except runtime.SettingError as exc:
        return _settings_page(request, db, user, 400, social_error=str(exc))
    runtime.save(db, values)
    return RedirectResponse("/configuracion#redes", status_code=303)
