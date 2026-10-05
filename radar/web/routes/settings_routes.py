"""Configuración básica: temas de seguimiento y cuenta."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from radar import runtime
from radar.alerts.config import DEFAULTS as ALERT_DEFAULTS
from radar.alerts.config import PRIORITIES, raw_settings, save_settings
from radar.models import AppSetting, Topic, User
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
from radar.web.deps import get_db, render, require_admin, verify_csrf

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
           "alert_cfg": raw_settings(db), "priorities": PRIORITIES,
           "telegram_enabled": telegram.enabled(), "telegram_configured": telegram.configured(),
           "ai_cfg": ai_cfg, "ai_usage": ai_usage_today(db),
           "ai_blocked": ai_blocked_reason(db, ai_cfg),
           "ai_providers": SUPPORTED_PROVIDERS, "ai_limits": INT_LIMITS,
           "ai_saved": stored_overrides(db), "rt": runtime.effective(db),
           "public_alert_choices": runtime.PUBLIC_ALERT_CHOICES, "rt_limits": runtime.SPECS}
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


@router.post("/cuenta/clave", dependencies=[Depends(verify_csrf)])
def change_password(
    request: Request,
    current: str = Form(..., max_length=256),
    new: str = Form(..., max_length=256),
    confirm: str = Form(..., max_length=256),
    db: Session = Depends(get_db), user: User = Depends(require_admin),
):
    if not verify_password(user.password_hash, current):
        return _page(request, db, user, pw_error="La contraseña actual no es correcta.",
                     status_code=400)
    if new != confirm:
        return _page(request, db, user, pw_error="Las contraseñas nuevas no coinciden.",
                     status_code=400)
    try:
        user.password_hash = hash_password(new)
    except ValueError as exc:
        return _page(request, db, user, pw_error=str(exc), status_code=400)
    user.session_version += 1
    request.session["sv"] = user.session_version  # esta sesión sigue; las demás caducan
    return _page(request, db, user, pw_ok="Contraseña actualizada.")
