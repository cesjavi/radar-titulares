"""Notificaciones por Telegram (opcional, desactivadas por defecto).

- Configuración por entorno: RADAR_TELEGRAM_ENABLED, RADAR_TELEGRAM_BOT_TOKEN,
  RADAR_TELEGRAM_CHAT_ID, RADAR_PUBLIC_URL.
- Cola persistente en SQLite (tabla notifications) con clave de deduplicación.
- Reintentos limitados con espera creciente; registro de cada envío.
- Durante las pruebas no se envían mensajes reales salvo RADAR_TELEGRAM_ALLOW_TEST_SEND=1.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from radar.models import Alert, AlertEvent, Notification
from radar.timeutil import utcnow

log = logging.getLogger("radar.notify")

API = "https://api.telegram.org"
MAX_ATTEMPTS = 5
BACKOFF_MINUTES = (1, 5, 15, 60)


def enabled() -> bool:
    return os.getenv("RADAR_TELEGRAM_ENABLED", "").strip().lower() in {"1", "true", "si", "sí", "yes"}


def configured() -> bool:
    return enabled() and bool(os.getenv("RADAR_TELEGRAM_BOT_TOKEN")) and bool(
        os.getenv("RADAR_TELEGRAM_CHAT_ID"))


def build_message(alert: Alert) -> str:
    evidence = json.loads(alert.evidence or "{}")
    media = ", ".join(evidence.get("medios_independientes", []))
    base = os.getenv("RADAR_PUBLIC_URL", "http://127.0.0.1:8000").rstrip("/")
    title = alert.title if len(alert.title) <= 160 else alert.title[:159] + "…"
    lines = [
        f"Radar de Titulares · prioridad {alert.priority.upper()}"
        + (f" (actualizada v{alert.version})" if alert.version > 1 else ""),
        f"{alert.independent_media_count} medios: {media}",
        title,
        f"{base}/alertas/{alert.id}",
        "La prioridad indica qué revisar primero; no implica falsedad ni coordinación.",
    ]
    return "\n".join(lines)


def enqueue(db: Session, alert: Alert, now=None) -> bool:
    """Encola un aviso por versión de alerta. Repetir la llamada no duplica."""
    key = f"telegram:alerta:{alert.id}:v{alert.version}"
    if db.scalar(select(Notification.id).where(Notification.dedupe_key == key)):
        return False
    try:
        with db.begin_nested():
            db.add(Notification(channel="telegram", alert_id=alert.id, dedupe_key=key,
                                message=build_message(alert), status="pendiente",
                                next_attempt_at=now or utcnow(), created_at=now or utcnow()))
    except IntegrityError:
        return False
    return True


def _guard_tests(transport) -> None:
    if transport is None and "PYTEST_CURRENT_TEST" in os.environ and \
            os.getenv("RADAR_TELEGRAM_ALLOW_TEST_SEND") != "1":
        raise RuntimeError("Envío real a Telegram bloqueado durante las pruebas.")


def process_queue(db: Session, transport: httpx.BaseTransport | None = None, now=None,
                  limit: int = 20) -> dict:
    """Envía los avisos pendientes. Sin configuración no hace nada (quedan en cola)."""
    now = now or utcnow()
    stats = {"enviadas": 0, "errores": 0, "pendientes": 0}
    if not configured():
        return stats
    _guard_tests(transport)
    token, chat_id = os.environ["RADAR_TELEGRAM_BOT_TOKEN"], os.environ["RADAR_TELEGRAM_CHAT_ID"]
    rows = db.scalars(select(Notification).where(
        Notification.channel == "telegram", Notification.status == "pendiente",
        Notification.next_attempt_at <= now).order_by(Notification.id).limit(limit)).all()
    if not rows:
        return stats
    with httpx.Client(timeout=15, transport=transport) as client:
        for n in rows:
            n.attempts += 1
            try:
                resp = client.post(f"{API}/bot{token}/sendMessage", json={
                    "chat_id": chat_id, "text": n.message, "disable_web_page_preview": True})
                ok = resp.status_code == 200 and resp.json().get("ok") is True
                error = None if ok else f"HTTP {resp.status_code}: {resp.text[:200]}"
            except (httpx.HTTPError, ValueError) as exc:
                ok, error = False, f"{type(exc).__name__}: {exc}"[:300]
            if ok:
                n.status, n.sent_at, n.last_error = "enviada", now, None
                stats["enviadas"] += 1
            else:
                # El token nunca se guarda en el error.
                n.last_error = (error or "").replace(token, "***")
                if n.attempts >= MAX_ATTEMPTS:
                    n.status = "error"
                    stats["errores"] += 1
                else:
                    wait = BACKOFF_MINUTES[min(n.attempts - 1, len(BACKOFF_MINUTES) - 1)]
                    n.next_attempt_at = now + timedelta(minutes=wait)
                    stats["pendientes"] += 1
            if n.alert_id:
                db.add(AlertEvent(alert_id=n.alert_id, event="notificacion", created_at=now,
                                  detail=json.dumps({"canal": "telegram", "estado": n.status,
                                                     "intento": n.attempts, "error": n.last_error},
                                                    ensure_ascii=False)))
    log.info("Telegram: %s", stats)
    return stats
