"""Aplicación FastAPI."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from radar import __version__
from radar import runtime
from radar.config import BASE_DIR, get_settings
from radar.web.deps import LoginRequired, client_ip, render
from radar.web.routes import (
    alerts_routes,
    analysis_routes,
    articles,
    auth,
    dashboard,
    settings_routes,
    sources,
)

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; "
    "object-src 'none'"
)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Radar de Titulares", version=__version__,
        docs_url=None, redoc_url=None, openapi_url=None,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response: Response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if not request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        if settings.is_production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response

    hits: dict[str, deque] = defaultdict(deque)
    hits_lock = threading.Lock()

    @app.middleware("http")
    async def public_rate_limit(request: Request, call_next):
        """Límite por IP para visitantes anónimos de la vista pública (protege el VPS)."""
        if request.url.path.startswith("/static/") or request.session.get("uid"):
            return await call_next(request)
        rt = runtime.effective()
        limit = rt["public_rate_limit"]
        if rt["public_mode"] and limit:
            ip = client_ip(request)
            now = time.monotonic()
            with hits_lock:
                if len(hits) > 10000:  # memoria acotada
                    hits.clear()
                q = hits[ip]
                while q and now - q[0] > 60:
                    q.popleft()
                if len(q) >= limit:
                    return Response("Demasiadas solicitudes. Probá de nuevo en un minuto.",
                                    status_code=429, headers={"Retry-After": "60"},
                                    media_type="text/plain; charset=utf-8")
                q.append(now)
        return await call_next(request)

    # Registrado después: envuelve al anterior, así la sesión está disponible en todo.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="radar_session",
        max_age=settings.session_max_age,
        same_site="lax",
        https_only=settings.cookie_secure,  # Secure en producción; HttpOnly siempre.
    )

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "radar" / "static")), name="static")

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, _exc: LoginRequired):
        target = "/login?next=" + quote(request.url.path, safe="/")
        if request.headers.get("hx-request"):
            return Response(status_code=401, headers={"HX-Redirect": target})
        return RedirectResponse(target, status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        messages = {403: "Acceso denegado", 404: "No encontrado", 405: "Método no permitido"}
        return render(
            request, "error.html",
            {"status": exc.status_code, "title": messages.get(exc.status_code, "Error"),
             "detail": exc.detail if exc.status_code in (403, 404) else None},
            status_code=exc.status_code,
        )

    @app.get("/salud", include_in_schema=False)
    def health():
        """Estado mínimo sin secretos ni datos: base accesible y antigüedad de la última
        recolección exitosa. 503 si la base no responde."""
        from fastapi.responses import JSONResponse
        from sqlalchemy import func, select, text

        from radar.db import SessionLocal
        from radar.models import Subsource
        from radar.timeutil import utcnow

        try:
            db = SessionLocal()
            try:
                db.execute(text("SELECT 1"))
                last = db.scalar(select(func.max(Subsource.last_success_at)))
            finally:
                db.close()
        except Exception:
            return JSONResponse({"estado": "error", "base": False}, status_code=503)
        minutes = int((utcnow() - last).total_seconds() // 60) if last else None
        status = "ok" if minutes is not None and minutes <= 30 else "degradado"
        return {"estado": status, "base": True, "ultima_recoleccion_min": minutes,
                "version": __version__}

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return FileResponse(BASE_DIR / "radar" / "static" / "favicon.svg", media_type="image/svg+xml")

    for module in (auth, dashboard, alerts_routes, articles, analysis_routes, sources,
                   settings_routes):
        app.include_router(module.router)
    return app
