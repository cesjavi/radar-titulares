from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from radar.config import get_settings
from radar.models import User
from radar.security import (
    authenticate,
    get_or_create_csrf_token,
    is_login_blocked,
    record_login_attempt,
)
from radar.timeutil import utcnow
from radar.web.deps import client_ip, current_user_optional, get_db, render, verify_csrf

router = APIRouter()


def _safe_next(target: str | None) -> str:
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"


@router.get("/login")
def login_form(request: Request, next: str = "/", user: User | None = Depends(current_user_optional)):
    if user:
        return RedirectResponse(_safe_next(next), status_code=303)
    return render(request, "login.html", {"next": _safe_next(next)})


@router.post("/login", dependencies=[Depends(verify_csrf)])
def login_submit(
    request: Request,
    username: str = Form(..., max_length=64),
    password: str = Form(..., max_length=256),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    settings = get_settings()
    ip = client_ip(request)
    username = username.strip()

    if is_login_blocked(db, ip, username, settings.login_max_failures,
                        settings.login_window_seconds):
        record_login_attempt(db, ip, username, success=False)
        return render(
            request, "login.html",
            {"error": "Demasiados intentos fallidos. Esperá unos minutos e intentá de nuevo.",
             "next": _safe_next(next), "username": username},
            status_code=429,
        )

    user = authenticate(db, username, password)
    if user is None:
        record_login_attempt(db, ip, username, success=False)
        return render(
            request, "login.html",
            {"error": "Usuario o contraseña incorrectos.", "next": _safe_next(next),
             "username": username},
            status_code=401,
        )

    record_login_attempt(db, ip, username, success=True)
    user.last_login_at = utcnow()
    # Sesión nueva tras autenticar (evita fijación de sesión) y token CSRF nuevo.
    request.session.clear()
    request.session["uid"] = user.id
    request.session["sv"] = user.session_version
    get_or_create_csrf_token(request.session)
    return RedirectResponse(_safe_next(next), status_code=303)


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
