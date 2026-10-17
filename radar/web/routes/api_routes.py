"""API JSON para la extensión de Chrome «Radar · Captura de X» y administración de sus tokens.

La extensión lee las respuestas que X ya le manda al navegador de la persona mientras navega (no
hace pedidos propios a X, no desplaza la página ni abre pestañas) y envía los posteos acá con un
token. Radar sigue sin conectarse a X: esto es la misma importación manual de `x_import`, pero
sin pasar por un archivo.

Las rutas `/api/x/*` no usan la cookie de sesión sino `Authorization: Bearer <token>`; por eso no
llevan CSRF (un sitio ajeno no puede hacer que el navegador agregue ese encabezado).
"""

from __future__ import annotations

import json
from dataclasses import asdict

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from radar import __version__, runtime
from radar.models import ApiToken, User
from radar.security import MAX_API_TOKENS, authenticate_api_token, create_api_token
from radar.social import x_import
from radar.social.match import link_posts
from radar.timeutil import utcnow
from radar.web.deps import get_db, require_admin, verify_csrf

router = APIRouter()

# Vercel acepta cuerpos de hasta 4,5 MB; la extensión manda lotes más chicos.
MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_POSTS_PER_REQUEST = 1000


def _error(status: int, motivo: str) -> JSONResponse:
    return JSONResponse({"estado": "error", "motivo": motivo}, status_code=status)


def _api_user(request: Request, db: Session) -> User | None:
    header = request.headers.get("authorization", "")
    scheme, _, raw = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return authenticate_api_token(db, raw)


@router.get("/api/x/estado", include_in_schema=False)
def api_status(request: Request, db: Session = Depends(get_db)):
    """Para el botón «Probar conexión» de la extensión."""
    user = _api_user(request, db)
    if user is None:
        return _error(401, "Token inválido o revocado.")
    return {"estado": "ok", "usuario": user.username, "admin": user.is_admin, "perfil": user.role,
            "version": __version__, "max_posteos": MAX_POSTS_PER_REQUEST,
            "max_bytes": MAX_BODY_BYTES}


@router.post("/api/x/posteos", include_in_schema=False)
async def api_import_posts(request: Request, db: Session = Depends(get_db)):
    """Recibe `{"posteos": [...], "vincular": true}` con filas en el formato plano de
    twitter-web-exporter. Idempotente: reenviar un lote no duplica nada."""
    user = _api_user(request, db)
    if user is None:
        return _error(401, "Token inválido o revocado.")
    if not user.is_admin:
        return _error(403, "El token pertenece a un usuario sin rol de administrador.")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return _error(413, f"El lote supera {MAX_BODY_BYTES // (1024 * 1024)} MB; mandalo en partes.")
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return _error(413, f"El lote supera {MAX_BODY_BYTES // (1024 * 1024)} MB; mandalo en partes.")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _error(400, "El cuerpo no es JSON válido en UTF-8.")
    rows = data.get("posteos") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return _error(400, "Falta la lista «posteos».")
    if len(rows) > MAX_POSTS_PER_REQUEST:
        return _error(413, f"Máximo {MAX_POSTS_PER_REQUEST} posteos por lote.")
    try:
        parsed = x_import.parse_rows(rows)
    except x_import.XImportError as exc:
        return _error(400, str(exc))
    summary = x_import.import_posts(db, parsed, runtime.get("social_retention_days", db))
    result = {"estado": "ok", "importacion": asdict(summary)}
    if data.get("vincular", True):
        db.flush()
        result["vinculos"] = asdict(link_posts(db))  # notas de las últimas 72 h
    return result


# --- administración de tokens (Configuración) -----------------------------------------------


def _settings_page(request, db, user, status_code=200, **extra):
    from radar.web.routes.settings_routes import _page

    return _page(request, db, user, status_code=status_code, **extra)


@router.post("/configuracion/tokens", dependencies=[Depends(verify_csrf)])
def token_create(request: Request, name: str = Form("", max_length=64),
                 db: Session = Depends(get_db), user: User = Depends(require_admin)):
    active = db.scalar(select(func.count()).select_from(ApiToken)
                       .where(ApiToken.revoked_at.is_(None))) or 0
    if active >= MAX_API_TOKENS:
        return _settings_page(request, db, user, status_code=400,
                              token_error=f"Hay {active} tokens activos; revocá alguno antes.")
    token, raw = create_api_token(db, user, name)
    return _settings_page(request, db, user, new_token=raw, new_token_name=token.name)


@router.post("/configuracion/tokens/{token_id}/revocar", dependencies=[Depends(verify_csrf)])
def token_revoke(token_id: int, db: Session = Depends(get_db),
                 user: User = Depends(require_admin)):
    token = db.get(ApiToken, token_id)
    if token is None:
        raise HTTPException(status_code=404, detail="Token inexistente.")
    if token.revoked_at is None:
        token.revoked_at = utcnow()
    return RedirectResponse("/configuracion#extension", status_code=303)
