"""Datos iniciales: medios y subfuentes (desde los adaptadores) y temas por defecto. Idempotente."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from radar.collector.adapters import ADAPTERS
from radar.models import AppSetting, Media, Subsource, Topic

# Valor anterior de priority_sections: si la base todavía lo tiene, se actualiza al nuevo.
_OLD_PRIORITY_SECTIONS = "politica,economia"

DEFAULT_SETTINGS = {
    # Secciones cuyas notas se leen en la página para completar metadatos.
    # "el-pais" es la sección de política nacional de Página/12 (su URL no trae la sección).
    "priority_sections": "politica,economia,el-pais",
}

TOPICS = [
    {
        "slug": "milei", "name": "Javier Milei",
        "description": "Menciones directas al presidente.",
        "keywords": "milei\njavier milei\nkarina milei",
    },
    {
        "slug": "gobierno", "name": "Gobierno nacional",
        "description": "Gabinete, Casa Rosada y decisiones del Poder Ejecutivo.",
        "keywords": "casa rosada\ngabinete\njefe de gabinete\nadorni\ncaputo\nbullrich\n"
                    "decreto\ndnu\nboletin oficial\nla libertad avanza",
    },
    {
        "slug": "economia", "name": "Economía argentina",
        "description": "Inflación, dólar, actividad, deuda y medidas económicas.",
        "keywords": "inflacion\ndolar\nbcra\nbanco central\nindec\nfmi\nriesgo pais\n"
                    "reservas\nsalarios\njubilaciones\ntarifas\nrecesion\nactividad economica",
        "exclude_keywords": "",
    },
]


def seed_sources(db: Session) -> int:
    """Crea o actualiza medios y subfuentes según los adaptadores.

    Las subfuentes existentes conservan su estado activo/pausado elegido en el panel.
    """
    changed = 0
    for adapter in ADAPTERS.values():
        media = db.scalar(select(Media).where(Media.slug == adapter.slug))
        if media is None:
            media = Media(slug=adapter.slug, name=adapter.name, base_url=adapter.base_url)
            db.add(media)
            changed += 1
        media.allowed_domains = ",".join(adapter.allowed_domains)
        db.flush()
        for ep in adapter.endpoints:
            sub = db.scalar(
                select(Subsource).where(Subsource.media_id == media.id, Subsource.url == ep.url)
            )
            if sub is None:
                sub = Subsource(media_id=media.id, url=ep.url, enabled=ep.enabled)
                db.add(sub)
                changed += 1
            sub.name, sub.kind, sub.section = ep.name, ep.kind, ep.section
            sub.min_interval_seconds, sub.max_items = ep.min_interval_seconds, ep.max_items
    return changed


def seed_topics(db: Session) -> int:
    created = 0
    for spec in TOPICS:
        if db.scalar(select(Topic).where(Topic.slug == spec["slug"])) is None:
            db.add(Topic(**spec))
            created += 1
    return created


def seed_settings(db: Session) -> int:
    created = 0
    for key, value in DEFAULT_SETTINGS.items():
        row = db.get(AppSetting, key)
        if row is None:
            db.add(AppSetting(key=key, value=value))
            created += 1
        elif key == "priority_sections" and row.value == _OLD_PRIORITY_SECTIONS:
            row.value = value  # no pisa lo que el usuario haya personalizado
    return created


def get_setting(db: Session, key: str) -> str:
    row = db.get(AppSetting, key)
    return row.value if row else DEFAULT_SETTINGS.get(key, "")
