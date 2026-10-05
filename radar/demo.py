"""Modo demo: medios y titulares FICTICIOS, marcados como demo. No son noticias reales.

Solo se pueden cargar con RADAR_DEMO_MODE=true. Usan dominios .invalid (RFC 2606)
y medios inventados, para que no puedan confundirse con publicaciones reales.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from radar.collector.ingest import upsert_item
from radar.collector.parsers import FeedItem
from radar.models import Article, Media, Subsource
from radar.timeutil import utcnow

DEMO_MEDIA = [
    ("demo-norte", "Diario Demo Norte"),
    ("demo-sur", "Portal Demo Sur"),
    ("demo-centro", "Agencia Demo Centro"),
]

# (medio, minutos atrás, titular, bajada, solo_fecha)
DEMO_ITEMS = [
    (0, 30, "Ejemplo: el Gobierno anuncia un paquete de medidas económicas",
     "Texto sintético de prueba para el modo demo.", False),
    (1, 45, "Ejemplo: medidas económicas del Gobierno generan debate",
     "Texto sintético de prueba para el modo demo.", False),
    (2, 60, "Ejemplo: qué incluye el paquete económico anunciado por el Gobierno",
     None, False),
    (0, 120, "Ejemplo: dato ficticio de inflación mensual", "Cifra inventada para pruebas.", False),
    (1, 130, "Ejemplo: la inflación ficticia del mes, según el dato demo", None, True),
    (2, 300, "Ejemplo: el presidente encabeza una reunión de gabinete", None, False),
    (0, 400, "Ejemplo: titular sin relación con los temas seguidos", None, True),
]


def _ensure_media(db: Session) -> list[Subsource]:
    subs = []
    for slug, name in DEMO_MEDIA:
        media = db.scalar(select(Media).where(Media.slug == slug))
        if media is None:
            media = Media(slug=slug, name=name, base_url=f"https://{slug}.invalid", is_demo=True)
            db.add(media)
            db.flush()
        sub = db.scalar(select(Subsource).where(Subsource.media_id == media.id))
        if sub is None:
            sub = Subsource(media_id=media.id, name="Fuente demo", kind="demo",
                            url=f"https://{slug}.invalid/feed", enabled=False)
            db.add(sub)
            db.flush()
        subs.append(sub)
    return subs


def load_demo(db: Session) -> int:
    subs = _ensure_media(db)
    now = utcnow()
    count = 0
    for i, (media_idx, minutes, title, subtitle, date_only) in enumerate(DEMO_ITEMS):
        published = now - timedelta(minutes=minutes)
        raw = published.strftime("%Y-%m-%d") if date_only else published.isoformat() + "+00:00"
        item = FeedItem(
            url=f"https://{DEMO_MEDIA[media_idx][0]}.invalid/nota-{i + 1}",
            title=f"[DEMO] {title}",
            subtitle=subtitle,
            author="Autor ficticio",
            published_raw=raw,
            keywords=["demo"],
        )
        upsert_item(db, subs[media_idx], item, run_id=None, discovery_method="demo", is_demo=True)
        count += 1
    return count


def clear_demo(db: Session) -> None:
    db.execute(delete(Article).where(Article.is_demo.is_(True)))
    db.execute(delete(Media).where(Media.is_demo.is_(True)))
