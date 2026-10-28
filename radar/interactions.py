"""Registro deduplicado de exposición y acciones para evaluar el orden por interés."""

from __future__ import annotations

from datetime import datetime
import math

from sqlalchemy import select
from sqlalchemy.orm import Session

from radar.models import ArticleInteraction
from radar.timeutil import to_local, utcnow


EVENTS = {"impresion", "detalle", "original", "ocultar"}


def _wilson(successes: int, total: int) -> tuple[float, float] | None:
    if not total:
        return None
    z = 1.96
    proportion = successes / total
    denominator = 1 + z * z / total
    midpoint = (proportion + z * z / (2 * total)) / denominator
    half = z * math.sqrt(proportion * (1 - proportion) / total
                         + z * z / (4 * total * total)) / denominator
    return max(0.0, midpoint - half), min(1.0, midpoint + half)


def record(db: Session, article_ids: list[int], user_id: int | None, event: str,
           context: str = "", ranks: list[int] | None = None) -> int:
    if event not in EVENTS or not article_ids or user_id is None:
        return 0
    now = utcnow()
    day = to_local(now).date()
    context = context[:64]
    existing = set(db.scalars(select(ArticleInteraction.article_id).where(
        ArticleInteraction.article_id.in_(article_ids), ArticleInteraction.user_id == user_id,
        ArticleInteraction.event == event, ArticleInteraction.day == day,
        ArticleInteraction.context == context)))
    count = 0
    for index, article_id in enumerate(article_ids):
        if article_id in existing:
            continue
        db.add(ArticleInteraction(article_id=article_id, user_id=user_id, event=event,
                                  context=context,
                                  rank=ranks[index] if ranks and index < len(ranks) else None,
                                  day=day, created_at=now))
        existing.add(article_id)
        count += 1
    return count


def interest_metrics(db: Session, since: datetime) -> dict:
    rows = db.scalars(select(ArticleInteraction)
                      .where(ArticleInteraction.created_at >= since)
                      .order_by(ArticleInteraction.created_at, ArticleInteraction.id)).all()
    impressions = {(row.article_id, row.user_id, row.day) for row in rows
                   if row.event == "impresion"}
    actions = {(row.article_id, row.user_id, row.day) for row in rows
               if row.event in {"detalle", "original"}}
    exposed_actions = impressions & actions
    rank_by_key = {}
    for row in rows:
        if row.event != "impresion" or row.rank is None:
            continue
        key = (row.article_id, row.user_id, row.day)
        rank_by_key.setdefault(key, row.rank)
    buckets = []
    for label, low, high in (("1–5", 1, 5), ("6–15", 6, 15), ("16+", 16, 10_000)):
        keys = {key for key, rank in rank_by_key.items() if low <= rank <= high}
        engaged = len(keys & actions)
        buckets.append({"label": label, "impressions": len(keys), "actions": engaged,
                        "rate": engaged / len(keys) if keys else None,
                        "interval": _wilson(engaged, len(keys))})
    interval = _wilson(len(exposed_actions), len(impressions))
    return {
        "impressions": len(impressions),
        "actions": len(actions),
        "exposed_actions": len(exposed_actions),
        "rate": len(exposed_actions) / len(impressions) if impressions else None,
        "interval": interval,
        "buckets": buckets,
    }
