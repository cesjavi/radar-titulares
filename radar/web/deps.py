"""Dependencias compartidas: sesión de BD, usuario actual, CSRF y plantillas."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from radar.config import BASE_DIR, get_settings
from radar.db import SessionLocal
from radar.models import User
from radar.security import (
    CSRF_FORM_FIELD,
    CSRF_HEADER,
    CSRF_SESSION_KEY,
    csrf_tokens_match,
    get_or_create_csrf_token,
)
from radar.timeutil import PRECISION_DATE, PRECISION_DATETIME, to_local

templates = Jinja2Templates(directory=str(BASE_DIR / "radar" / "templates"))


def _fmt_dt(value: datetime | None, fmt: str = "%d/%m/%Y %H:%M") -> str:
    return to_local(value).strftime(fmt) if value else "—"


def _fmt_pub(article, kind: str = "published") -> str:
    """Muestra fecha de publicación/modificación respetando la precisión disponible."""
    precision = getattr(article, f"{kind}_precision")
    if precision == PRECISION_DATETIME:
        return _fmt_dt(getattr(article, f"{kind}_at"))
    if precision == PRECISION_DATE:
        d: date | None = getattr(article, f"{kind}_date")
        return f"{d:%d/%m/%Y} (sin hora)" if d else "—"
    return "sin dato"


templates.env.filters["local_dt"] = _fmt_dt
templates.env.globals["fmt_pub"] = _fmt_pub


class LoginRequired(Exception):
    """Se convierte en redirección a /login (o HX-Redirect para HTMX)."""


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def current_user_optional(request: Request, db: Session = Depends(get_db)) -> User | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    user = db.get(User, uid)
    if user is None or not user.is_active or request.session.get("sv") != user.session_version:
        request.session.clear()
        return None
    return user


def require_user(user: User | None = Depends(current_user_optional)) -> User:
    if user is None:
        raise LoginRequired()
    return user


class Visitor:
    """Visitante sin login de la vista pública: solo lectura."""

    id = None
    username = "visitante"
    is_admin = False
    is_active = True
    is_anonymous = True


VISITOR = Visitor()


def viewer(request: Request, user: User | None = Depends(current_user_optional)):
    """Usuario logueado o, si la vista pública está activa, el visitante anónimo."""
    if user is not None:
        request.state.public = False
        return user
    if get_settings().public_mode:
        request.state.public = True
        return VISITOR
    raise LoginRequired()


def is_public(request: Request) -> bool:
    return bool(getattr(request.state, "public", False))


def demo_visible(request: Request) -> bool:
    """Los datos DEMO nunca se muestran a visitantes anónimos."""
    return get_settings().demo_mode and not is_public(request)


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Permiso insuficiente.")
    return user


async def verify_csrf(request: Request) -> None:
    expected = request.session.get(CSRF_SESSION_KEY)
    provided = request.headers.get(CSRF_HEADER)
    if not provided:
        form = await request.form()
        provided = form.get(CSRF_FORM_FIELD)
    if not csrf_tokens_match(expected, provided if isinstance(provided, str) else None):
        raise HTTPException(status_code=403, detail="Token CSRF inválido. Recargá la página.")


def render(request: Request, name: str, context: dict | None = None, status_code: int = 200,
           user: User | None = None):
    ctx = {
        "user": user,
        "csrf_token": get_or_create_csrf_token(request.session),
        "demo_mode": demo_visible(request),
        "public_view": is_public(request),
        "public_alerts": get_settings().public_alerts,
    }
    ctx.update(context or {})
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def client_ip(request: Request) -> str:
    # VPS: detrás de un proxy confiable, uvicorn --proxy-headers ya reemplaza request.client.
    # Vercel: x-forwarded-for trae la IP pública del cliente y Vercel la sobrescribe (no se puede
    # falsificar); en cualquier otro entorno ese encabezado no es confiable y se ignora.
    if get_settings().serverless:
        forwarded = (request.headers.get("x-vercel-forwarded-for")
                     or request.headers.get("x-forwarded-for") or "")
        first = forwarded.split(",")[0].strip()
        if first:
            return first[:64]
    return request.client.host if request.client else "desconocida"
