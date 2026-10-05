"""Configuración de alertas (editable en el panel; se guarda en app_settings)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from radar.models import AppSetting

PRIORITIES = ("baja", "media", "alta")
PRIORITY_RANK = {p: i for i, p in enumerate(PRIORITIES)}

DEFAULTS = {
    "alert_min_independent_media": "2",
    # Prioridad asignada a cada tipo de evidencia (configurable).
    "alert_priority_shared_topic": "baja",
    "alert_priority_same_event": "media",
    "alert_priority_shared_expression": "alta",
    # Horas máximas entre publicaciones para considerar "proximidad temporal".
    "alert_same_event_hours": "24",
    # No se crean alertas por debajo de esta prioridad.
    "alert_min_priority": "baja",
    # Tras una actualización relevante, no se vuelve a notificar antes de este lapso.
    "alert_cooldown_minutes": "60",
    "alert_silenced_topics": "",  # slugs separados por coma
    "alert_excluded_terms": "",  # una por línea
    "alert_excluded_sections": "deportes,espectaculos,show,teleshow,famosos",
    "telegram_min_priority": "alta",
}


def _lines(value: str) -> list[str]:
    return [x.strip().lower() for x in value.replace(",", "\n").splitlines() if x.strip()]


@dataclass
class AlertSettings:
    min_independent_media: int
    priority_shared_topic: str
    priority_same_event: str
    priority_shared_expression: str
    same_event_hours: int
    min_priority: str
    cooldown_minutes: int
    silenced_topics: list[str]
    excluded_terms: list[str]
    excluded_sections: list[str]
    telegram_min_priority: str


def raw_settings(db: Session) -> dict[str, str]:
    values = dict(DEFAULTS)
    for row in db.query(AppSetting).filter(AppSetting.key.in_(DEFAULTS)):
        values[row.key] = row.value
    return values


def _prio(value: str, default: str) -> str:
    value = (value or "").strip().lower()
    return value if value in PRIORITY_RANK else default


def _int(value: str, default: int, lo: int, hi: int) -> int:
    try:
        return min(hi, max(lo, int(value)))
    except (TypeError, ValueError):
        return default


def load_settings(db: Session) -> AlertSettings:
    v = raw_settings(db)
    return AlertSettings(
        min_independent_media=_int(v["alert_min_independent_media"], 2, 2, 10),
        priority_shared_topic=_prio(v["alert_priority_shared_topic"], "baja"),
        priority_same_event=_prio(v["alert_priority_same_event"], "media"),
        priority_shared_expression=_prio(v["alert_priority_shared_expression"], "alta"),
        same_event_hours=_int(v["alert_same_event_hours"], 24, 1, 168),
        min_priority=_prio(v["alert_min_priority"], "baja"),
        cooldown_minutes=_int(v["alert_cooldown_minutes"], 60, 0, 7 * 24 * 60),
        silenced_topics=_lines(v["alert_silenced_topics"]),
        excluded_terms=_lines(v["alert_excluded_terms"]),
        excluded_sections=_lines(v["alert_excluded_sections"]),
        telegram_min_priority=_prio(v["telegram_min_priority"], "alta"),
    )


def save_settings(db: Session, values: dict[str, str]) -> None:
    for key, value in values.items():
        if key not in DEFAULTS:
            continue
        row = db.get(AppSetting, key)
        if row is None:
            db.add(AppSetting(key=key, value=value))
        else:
            row.value = value
