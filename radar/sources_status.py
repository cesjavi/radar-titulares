"""Estado operativo y cobertura de medios y subfuentes para el panel."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from radar.models import Article, ArticleSighting, Media, Subsource

FAILURES_FOR_DOWN = 3

STATE_LABELS = {
    "operativa": "Operativa",
    "sin_cambios": "Operativa (sin cambios)",
    "con_fallas": "Con fallas",
    "caida": "Caída",
    "bloqueada": "Bloqueada",
    "vacia": "Respuesta sin notas",
    "pausada": "Pausada",
    "sin_consultar": "Sin consultar",
}


def subsource_state(sub: Subsource) -> str:
    if not sub.enabled:
        return "pausada"
    if sub.last_status is None and sub.last_success_at is None:
        return "sin_consultar"
    if sub.last_status == "blocked":
        return "bloqueada"
    if sub.consecutive_failures >= FAILURES_FOR_DOWN:
        return "caida"
    if sub.last_status == "empty":
        return "vacia"
    if sub.last_status == "error" or sub.consecutive_failures:
        return "con_fallas"
    if sub.last_status == "not_modified":
        return "sin_cambios"
    return "operativa"


def media_state(states: list[str]) -> str:
    active = [s for s in states if s != "pausada"]
    if not active:
        return "pausada"
    if all(s in ("operativa", "sin_cambios") for s in active):
        return "operativa"
    if any(s in ("operativa", "sin_cambios") for s in active):
        return "con_fallas"
    if any(s == "bloqueada" for s in active):
        return "bloqueada"
    if all(s == "sin_consultar" for s in active):
        return "sin_consultar"
    return "caida"


@dataclass
class Coverage:
    total: int = 0
    with_subtitle: int = 0
    with_body: int = 0

    def pct(self, n: int) -> int:
        return round(100 * n / self.total) if self.total else 0

    @property
    def level(self) -> str:
        """Cobertura disponible predominante."""
        if not self.total:
            return "—"
        if self.with_body * 2 >= self.total:
            return "cuerpo"
        if self.with_subtitle * 2 >= self.total:
            return "bajada"
        return "titular"


def _cov_columns():
    return (
        func.count(Article.id),
        func.sum(case((Article.subtitle.is_not(None), 1), else_=0)),
        func.sum(case((Article.body_text.is_not(None), 1), else_=0)),
    )


def media_coverage(db: Session) -> dict[int, Coverage]:
    rows = db.execute(
        select(Article.media_id, *_cov_columns()).group_by(Article.media_id)
    ).all()
    return {r[0]: Coverage(r[1] or 0, r[2] or 0, r[3] or 0) for r in rows}


def subsource_coverage(db: Session) -> dict[int, Coverage]:
    rows = db.execute(
        select(ArticleSighting.subsource_id, *_cov_columns())
        .join(Article, Article.id == ArticleSighting.article_id)
        .group_by(ArticleSighting.subsource_id)
    ).all()
    return {r[0]: Coverage(r[1] or 0, r[2] or 0, r[3] or 0) for r in rows}


def media_health(db: Session, demo_mode: bool) -> dict[int, str]:
    """Estado operativo agregado por medio (id → clave de STATE_LABELS)."""
    q = select(Media)
    if not demo_mode:
        q = q.where(Media.is_demo.is_(False))
    out = {}
    for media in db.scalars(q):
        if media.is_demo:
            out[media.id] = "operativa"
            continue
        out[media.id] = media_state([subsource_state(s) for s in media.subsources])
    return out
