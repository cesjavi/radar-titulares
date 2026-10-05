"""Ajustes operativos que un administrador edita desde el panel.

Lo guardado en `app_settings` (claves `cfg_*`) pisa al entorno (`.env`); sin valor guardado rige
el entorno. Los secretos (claves, tokens, URL de la base) nunca pasan por acá.

Las lecturas por solicitud (vista pública) usan una caché de pocos segundos para no consultar la
base en cada visita; guardar desde el panel la invalida en el mismo proceso.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

from sqlalchemy import select

PREFIX = "cfg_"
CACHE_SECONDS = 10.0
PUBLIC_ALERT_CHOICES = ("revisadas", "todas", "ninguna")


@dataclass(frozen=True)
class Spec:
    kind: str  # bool | int | choice
    lo: int = 0
    hi: int = 0
    choices: tuple[str, ...] = ()


SPECS: dict[str, Spec] = {
    "public_mode": Spec("bool"),
    "public_alerts": Spec("choice", choices=PUBLIC_ALERT_CHOICES),
    "public_rate_limit": Spec("int", 0, 10_000),
    "enrich_per_media": Spec("int", 0, 100),
    "retention_days": Spec("int", 0, 3650),
    "runs_retention_days": Spec("int", 0, 3650),
    "telegram_enabled": Spec("bool"),
}

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "values": {}}


class SettingError(ValueError):
    """Valor no válido para un ajuste (el mensaje se muestra al administrador)."""


def invalidate() -> None:
    with _lock:
        _cache["at"] = 0.0
        _cache["values"] = {}


def _read(db) -> dict[str, str]:
    from radar.models import AppSetting

    rows = db.execute(select(AppSetting.key, AppSetting.value)
                      .where(AppSetting.key.like(PREFIX + "%"))).all()
    return {k[len(PREFIX):]: v for k, v in rows if k[len(PREFIX):] in SPECS}


def stored(db=None) -> dict[str, str]:
    """Valores guardados desde el panel. Sin base disponible, vacío (rige el entorno)."""
    if db is not None:
        try:
            return _read(db)
        except Exception:
            return {}
    now = time.monotonic()
    with _lock:
        if now - _cache["at"] < CACHE_SECONDS:
            return dict(_cache["values"])
    try:
        from radar.db import session_scope

        with session_scope() as session:
            values = _read(session)
    except Exception:  # base sin migrar o sin conexión: se usa el entorno
        values = {}
    with _lock:
        _cache["at"], _cache["values"] = now, values
    return dict(values)


def _env_bool(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "si", "sí", "on"}


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return min(max(int(os.getenv(name, str(default))), lo), hi)
    except ValueError:
        return default


def env_defaults() -> dict:
    """Valor que rige cuando el panel no guardó nada: el del entorno."""
    from radar.config import get_settings

    s = get_settings()
    return {
        "public_mode": s.public_mode,
        "public_alerts": s.public_alerts,
        "public_rate_limit": s.public_rate_limit,
        "enrich_per_media": _env_int("RADAR_ENRICH_PER_MEDIA", 10, 0, 100),
        "retention_days": s.retention_days,
        "runs_retention_days": s.runs_retention_days,
        "telegram_enabled": _env_bool("RADAR_TELEGRAM_ENABLED"),
    }


def _convert(name: str, raw: str):
    spec = SPECS[name]
    if spec.kind == "bool":
        return raw == "true"
    if spec.kind == "int":
        return min(max(int(raw), spec.lo), spec.hi) if raw.isdigit() else None
    return raw if raw in spec.choices else None


def get(name: str, db=None):
    """Valor vigente: lo guardado en el panel o, si no hay, el del entorno."""
    raw = stored(db).get(name)
    if raw is not None:
        value = _convert(name, raw)
        if value is not None:
            return value
    return env_defaults()[name]


def effective(db=None) -> dict:
    values = stored(db)
    out = env_defaults()
    for name, raw in values.items():
        value = _convert(name, raw)
        if value is not None:
            out[name] = value
    return out


def parse(name: str, raw: str | None, label: str) -> str:
    """Valida un valor del formulario y lo devuelve listo para guardar."""
    spec = SPECS[name]
    if spec.kind == "bool":
        return "true" if raw else "false"
    value = (raw or "").strip()
    if spec.kind == "int":
        if not value.isdigit() or not spec.lo <= int(value) <= spec.hi:
            raise SettingError(f"«{label}» debe ser un número entre {spec.lo} y {spec.hi}.")
        return value
    if value not in spec.choices:
        raise SettingError(f"«{label}»: elegí una de las opciones ({', '.join(spec.choices)}).")
    return value


def save(db, values: dict[str, str]) -> None:
    from radar.models import AppSetting

    for name, value in values.items():
        if name not in SPECS:
            continue
        row = db.get(AppSetting, PREFIX + name)
        if row is None:
            db.add(AppSetting(key=PREFIX + name, value=value))
        else:
            row.value = value
    invalidate()


# Accesos directos para el código que lee un valor por solicitud o por ciclo.
def public_mode() -> bool:
    return bool(get("public_mode"))
