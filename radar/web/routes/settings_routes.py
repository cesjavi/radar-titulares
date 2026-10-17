"""Configuración básica: temas de seguimiento y cuenta."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from radar import manual_run, runtime
from radar.config import get_settings
from radar.alerts.config import DEFAULTS as ALERT_DEFAULTS
from radar.alerts.config import PRIORITIES, raw_settings, save_settings
from radar.models import ROLES, ApiToken, AppSetting, Media, SocialAccount, Topic, User
from radar.notify import telegram
from radar.ai.config import (
    BOOL_FIELDS,
    INT_LIMITS,
    PRIORITIES as AI_PRIORITIES,
    SUPPORTED_PROVIDERS,
    normalize_providers,
    save_overrides,
    stored_overrides,
)
from radar.ai.config import load_config as load_ai_config
from radar.ai.service import blocked_reason as ai_blocked_reason
from radar.ai.service import usage_today as ai_usage_today
from radar.seed import get_setting
from radar.security import MIN_PASSWORD_LENGTH, hash_password, verify_password
from radar.text import normalize_for_search
from radar.web.deps import get_db, render, require_admin, require_user, verify_csrf

router = APIRouter(prefix="/configuracion")


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", normalize_for_search(name)).strip("-")[:64] or "tema"


def _clean_lines(raw: str) -> str:
    seen, out = set(), []
    for line in raw.splitlines():
        k = line.strip()[:100]
        if k and k.lower() not in seen:
            seen.add(k.lower())
            out.append(k)
    return "\n".join(out[:200])


def _page(request, db, user, status_code: int = 200, **extra):
    ai_cfg = load_ai_config(db)
    ctx = {"topics": db.scalars(select(Topic).order_by(Topic.name)).all(),
           "nav": "configuracion", "min_pw": MIN_PASSWORD_LENGTH,
           "priority_sections": get_setting(db, "priority_sections"),
           "summary_excluded_terms": get_setting(db, "summary_excluded_terms"),
           "alert_cfg": raw_settings(db), "priorities": PRIORITIES,
           "telegram_enabled": telegram.enabled(), "telegram_configured": telegram.configured(),
           "ai_cfg": ai_cfg, "ai_usage": ai_usage_today(db),
           "ai_blocked": ai_blocked_reason(db, ai_cfg),
           "ai_providers": SUPPORTED_PROVIDERS, "ai_limits": INT_LIMITS,
           "ai_saved": stored_overrides(db), "rt": runtime.effective(db),
           "public_alert_choices": runtime.PUBLIC_ALERT_CHOICES, "rt_limits": runtime.SPECS,
           "run_tasks": manual_run.TASKS, "run_state": manual_run.last_state(db),
           "run_media": [(m.slug, m.name) for m in db.scalars(
               select(Media).where(Media.is_demo.is_(False)).order_by(Media.name))],
           "run_ai_max": manual_run.AI_LIMIT_MAX, "run_serverless": get_settings().serverless,
           "social_accounts": db.scalars(select(SocialAccount).order_by(SocialAccount.name)).all(),
           "social_kinds": {"persona": "Persona", "organizacion": "Organización",
                            "medio": "Medio"},
           "api_tokens": db.scalars(select(ApiToken).where(ApiToken.revoked_at.is_(None))
                                    .order_by(ApiToken.created_at.desc())).all(),
           "users": db.scalars(select(User).order_by(User.username)).all(), "roles": ROLES}
    ctx.update(extra)
    return render(request, "settings.html", ctx, status_code=status_code, user=user)


@router.get("")
def settings_page(request: Request, db: Session = Depends(get_db),
                  user: User = Depends(require_admin)):
    return _page(request, db, user)


@router.post("/temas", dependencies=[Depends(verify_csrf)])
def topic_create(
    request: Request,
    name: str = Form(..., min_length=1, max_length=128),
    keywords: str = Form("", max_length=10000),
    exclude_keywords: str = Form("", max_length=10000),
    description: str = Form("", max_length=1000),
    db: Session = Depends(get_db), user: User = Depends(require_admin),
):
    name = name.strip()
    slug = _slugify(name)
    if not name or db.scalar(select(Topic).where(Topic.slug == slug)):
        return _page(request, db, user, topic_error="Ya existe un tema con ese nombre.",
                     status_code=400)
    db.add(Topic(name=name, slug=slug, description=description.strip() or None,
                 keywords=_clean_lines(keywords), exclude_keywords=_clean_lines(exclude_keywords)))
    return RedirectResponse("/configuracion#temas", status_code=303)


@router.post("/temas/{topic_id}", dependencies=[Depends(verify_csrf)])
def topic_update(
    topic_id: int,
    name: str = Form(..., min_length=1, max_length=128),
    keywords: str = Form("", max_length=10000),
    exclude_keywords: str = Form("", max_length=10000),
    description: str = Form("", max_length=1000),
    enabled: str | None = Form(None),
    db: Session = Depends(get_db), user: User = Depends(require_admin),
):
    topic = db.get(Topic, topic_id)
    if topic is None:
        raise HTTPException(status_code=404, detail="Tema inexistente.")
    topic.name = name.strip() or topic.name
    topic.description = description.strip() or None
    topic.keywords = _clean_lines(keywords)
    topic.exclude_keywords = _clean_lines(exclude_keywords)
    topic.enabled = enabled == "on"
    return RedirectResponse(f"/configuracion#tema-{topic.id}", status_code=303)


@router.post("/secciones", dependencies=[Depends(verify_csrf)])
def priority_sections_update(
    sections: str = Form("", max_length=1000),
    db: Session = Depends(get_db), user: User = Depends(require_admin),
):
    cleaned = []
    for raw in re.split(r"[,\n]", sections):
        slug = _slugify(raw) if raw.strip() else ""
        if slug and slug not in cleaned:
            cleaned.append(slug)
    row = db.get(AppSetting, "priority_sections")
    if row is None:
        db.add(AppSetting(key="priority_sections", value=",".join(cleaned[:30])))
    else:
        row.value = ",".join(cleaned[:30])
    return RedirectResponse("/configuracion#secciones", status_code=303)


@router.post("/resumen", dependencies=[Depends(verify_csrf)])
def summary_excluded_update(
    terms: str = Form("", max_length=10000),
    db: Session = Depends(get_db), user: User = Depends(require_admin),
):
    value = _clean_lines(terms)
    row = db.get(AppSetting, "summary_excluded_terms")
    if row is None:
        db.add(AppSetting(key="summary_excluded_terms", value=value))
    else:
        row.value = value
    return RedirectResponse("/configuracion#resumen", status_code=303)


@router.post("/alertas", dependencies=[Depends(verify_csrf)])
async def alert_settings_update(request: Request, db: Session = Depends(get_db),
                                user: User = Depends(require_admin)):
    form = await request.form()
    values = {k: str(form.get(k, ""))[:2000].strip() for k in ALERT_DEFAULTS if k in form}
    for k in ("alert_priority_shared_topic", "alert_priority_same_event",
              "alert_priority_shared_expression", "alert_min_priority", "telegram_min_priority"):
        if k in values and values[k] not in PRIORITIES:
            values.pop(k)
    for k in ("alert_min_independent_media", "alert_same_event_hours", "alert_cooldown_minutes"):
        if k in values and not values[k].isdigit():
            values.pop(k)
    save_settings(db, values)
    return RedirectResponse("/configuracion#alertas", status_code=303)


@router.post("/ia", dependencies=[Depends(verify_csrf)])
async def ai_settings_update(request: Request, db: Session = Depends(get_db),
                             user: User = Depends(require_admin)):
    """Valores de la IA que pisan al .env. Las claves y los modelos de Groq/Fireworks no se
    editan acá: siguen en el entorno."""
    form = await request.form()
    values: dict[str, str] = {}
    for name in BOOL_FIELDS:
        values[name] = "true" if form.get(name) else "false"
    for name, (lo, hi) in INT_LIMITS.items():
        raw = str(form.get(name, "")).strip()
        if not raw.isdigit() or not lo <= int(raw) <= hi:
            return _page(request, db, user, 400,
                         ai_error=f"«{name}» debe ser un número entre {lo} y {hi}.")
        values[name] = raw
    order = normalize_providers(",".join(str(form.get(f"provider_{i}", "")) for i in (1, 2, 3)))
    if not order:
        return _page(request, db, user, 400, ai_error="Elegí al menos un proveedor.")
    values["provider"] = order
    priority = str(form.get("min_priority", "baja"))
    values["min_priority"] = priority if priority in AI_PRIORITIES else "baja"
    save_overrides(db, values)
    return RedirectResponse("/configuracion#ia", status_code=303)


def _save_runtime(request, db, user, form, fields: dict[str, str], anchor: str):
    """Valida y guarda ajustes operativos (`radar.runtime`). `fields`: nombre -> etiqueta."""
    try:
        values = {name: runtime.parse(name, form.get(name), label) for name, label in fields.items()}
    except runtime.SettingError as exc:
        return _page(request, db, user, 400, rt_error=str(exc), rt_error_in=anchor)
    runtime.save(db, values)
    return RedirectResponse(f"/configuracion#{anchor}", status_code=303)


@router.post("/recoleccion", dependencies=[Depends(verify_csrf)])
async def collection_settings_update(request: Request, db: Session = Depends(get_db),
                                     user: User = Depends(require_admin)):
    form = await request.form()
    return _save_runtime(request, db, user, form, {
        "enrich_per_media": "Páginas a leer por medio y ciclo",
        "retention_days": "Retención de notas (días)",
        "runs_retention_days": "Retención de ejecuciones y avisos (días)"}, "recoleccion")


@router.post("/publica", dependencies=[Depends(verify_csrf)])
async def public_settings_update(request: Request, db: Session = Depends(get_db),
                                 user: User = Depends(require_admin)):
    form = await request.form()
    return _save_runtime(request, db, user, form, {
        "public_mode": "Vista pública", "public_alerts": "Alertas visibles para visitantes",
        "public_rate_limit": "Solicitudes por minuto e IP"}, "publica")


@router.post("/telegram", dependencies=[Depends(verify_csrf)])
async def telegram_settings_update(request: Request, db: Session = Depends(get_db),
                                   user: User = Depends(require_admin)):
    form = await request.form()
    return _save_runtime(request, db, user, form, {"telegram_enabled": "Telegram"}, "telegram")


@router.post("/ejecutar", dependencies=[Depends(verify_csrf)])
async def run_now(request: Request, db: Session = Depends(get_db),
                  user: User = Depends(require_admin)):
    """Ejecuta a mano una tarea (equivale a un comando de la CLI)."""
    form = await request.form()
    try:
        opts = manual_run.parse_options(form)
        db.commit()  # no dejar una transacción abierta mientras corre la tarea
        manual_run.start(opts, user.username)
    except manual_run.RunError as exc:
        return _page(request, db, user, 400, run_error=str(exc))
    return RedirectResponse("/configuracion#ejecutar", status_code=303)


@router.get("/ejecutar/estado")
def run_state(request: Request, db: Session = Depends(get_db),
              user: User = Depends(require_admin)):
    return render(request, "partials/manual_run.html",
                  {"run_state": manual_run.last_state(db)}, user=user)


USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{2,64}$")


@router.post("/usuarios", dependencies=[Depends(verify_csrf)])
def user_create(
    request: Request,
    username: str = Form(..., max_length=64),
    password: str = Form(..., max_length=256),
    role: str = Form("analizador", max_length=16),
    db: Session = Depends(get_db), user: User = Depends(require_admin),
):
    username = username.strip()
    if not USERNAME_RE.match(username):
        return _page(request, db, user, 400, user_error=(
            "El usuario debe tener entre 2 y 64 caracteres: letras, números, punto, guion o guion bajo."))
    if role not in ROLES:
        return _page(request, db, user, 400, user_error="Perfil desconocido.")
    if db.scalar(select(User).where(User.username == username)):
        return _page(request, db, user, 400, user_error=f"El usuario «{username}» ya existe.")
    try:
        pw_hash = hash_password(password)
    except ValueError as exc:
        return _page(request, db, user, 400, user_error=str(exc))
    db.add(User(username=username, password_hash=pw_hash, role=role))
    return RedirectResponse("/configuracion#usuarios", status_code=303)


@router.post("/usuarios/{user_id}", dependencies=[Depends(verify_csrf)])
def user_update(
    request: Request,
    user_id: int,
    role: str = Form(..., max_length=16),
    is_active: str | None = Form(None),
    password: str = Form("", max_length=256),
    db: Session = Depends(get_db), user: User = Depends(require_admin),
):
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Usuario inexistente.")
    if role not in ROLES:
        return _page(request, db, user, 400, user_error="Perfil desconocido.")
    active = is_active == "on"
    # Así nunca se queda el panel sin un administrador activo.
    if target.id == user.id and (role != "admin" or not active):
        return _page(request, db, user, 400, user_error=(
            "No podés quitarte el perfil de administrador ni desactivar tu propio usuario."))
    if password:
        try:
            target.password_hash = hash_password(password)
        except ValueError as exc:
            return _page(request, db, user, 400, user_error=str(exc))
        target.session_version += 1  # cierra sus sesiones abiertas
        if target.id == user.id:
            request.session["sv"] = target.session_version
    target.role = role
    target.is_active = active
    return RedirectResponse(f"/configuracion#usuario-{target.id}", status_code=303)


def _account_page(request, db, user, status_code: int = 200, **extra):
    """La contraseña propia se cambia desde Configuración (admin) o desde «Mi cuenta»."""
    if user.is_admin:
        return _page(request, db, user, status_code, **extra)
    return render(request, "account.html", {"nav": "cuenta", "min_pw": MIN_PASSWORD_LENGTH,
                                            **extra}, status_code=status_code, user=user)


@router.get("/cuenta")
def account_page(request: Request, db: Session = Depends(get_db),
                 user: User = Depends(require_user)):
    return _account_page(request, db, user)


@router.post("/cuenta/clave", dependencies=[Depends(verify_csrf)])
def change_password(
    request: Request,
    current: str = Form(..., max_length=256),
    new: str = Form(..., max_length=256),
    confirm: str = Form(..., max_length=256),
    db: Session = Depends(get_db), user: User = Depends(require_user),
):
    if not verify_password(user.password_hash, current):
        return _account_page(request, db, user, 400, pw_error="La contraseña actual no es correcta.")
    if new != confirm:
        return _account_page(request, db, user, 400, pw_error="Las contraseñas nuevas no coinciden.")
    try:
        user.password_hash = hash_password(new)
    except ValueError as exc:
        return _account_page(request, db, user, 400, pw_error=str(exc))
    user.session_version += 1
    request.session["sv"] = user.session_version  # esta sesión sigue; las demás caducan
    return _account_page(request, db, user, pw_ok="Contraseña actualizada.")
