"""Fechas: todo se guarda en UTC (naive en SQLite) y se muestra en Buenos Aires."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

from radar.config import DISPLAY_TZ

LOCAL_TZ = ZoneInfo(DISPLAY_TZ)

PRECISION_DATETIME = "datetime"
PRECISION_DATE = "date"
PRECISION_NONE = "none"


def utcnow() -> datetime:
    """UTC naive: SQLite no guarda zona horaria, la convención es UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_utc_naive(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def to_local(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)


_DATE_ONLY = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})\s*$")


def parse_feed_date(raw: str | None) -> tuple[datetime | None, date | None, str]:
    """Interpreta una fecha de RSS/Atom/sitemap.

    Devuelve (datetime UTC o None, fecha calendario o None, precisión).
    Si la fuente solo trae la fecha, no se inventa una hora.
    Si trae hora sin zona horaria, se asume hora de Buenos Aires.
    """
    if not raw or not raw.strip():
        return None, None, PRECISION_NONE
    raw = raw.strip()

    m = _DATE_ONLY.match(raw)
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None, None, PRECISION_NONE
        return None, d, PRECISION_DATE

    dt: datetime | None = None
    try:
        iso = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
        dt = datetime.fromisoformat(iso)
    except ValueError:
        try:
            dt = parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError):
            dt = None
    if dt is None:
        return None, None, PRECISION_NONE

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    local_date = dt.astimezone(LOCAL_TZ).date()
    return to_utc_naive(dt), local_date, PRECISION_DATETIME
