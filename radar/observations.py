"""Agregados livianos de cobertura de recolección que sobreviven a la retención."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from radar.models import CollectionRun, DailyObservation
from radar.timeutil import to_local


SUCCESS = {"ok", "not_modified"}


def record_run(db: Session, run: CollectionRun) -> DailyObservation:
    day = to_local(run.started_at).date()
    scope = f"s{run.subsource_id}" if run.subsource_id is not None else f"m{run.media_id or 0}"
    key = f"{day.isoformat()}:{run.run_type}:{scope}"
    row = db.get(DailyObservation, key)
    if row is None:
        row = DailyObservation(key=key, day=day, media_id=run.media_id,
                               subsource_id=run.subsource_id, run_type=run.run_type,
                               attempted=0, successful=0, empty=0, failed=0,
                               items_found=0, items_new=0, duration_ms=0, last_run_id=0)
        db.add(row)
    if run.id and run.id <= row.last_run_id:
        return row
    media = run.media
    subsource = run.subsource
    row.media_name = (media.name if media else row.media_name or "")[:128]
    row.subsource_name = (subsource.name if subsource else row.subsource_name or "")[:128]
    row.attempted += 1
    row.successful += run.status in SUCCESS
    row.empty += run.status == "empty"
    row.failed += run.status not in SUCCESS | {"empty", "skipped"}
    row.items_found += run.items_found or 0
    row.items_new += run.items_new or 0
    row.duration_ms += run.duration_ms or 0
    row.first_run_at = min(filter(None, (row.first_run_at, run.started_at)), default=run.started_at)
    row.last_run_at = max(filter(None, (row.last_run_at, run.finished_at or run.started_at)),
                          default=run.finished_at or run.started_at)
    row.last_run_id = max(row.last_run_id, run.id or 0)
    return row


def backfill(db: Session) -> int:
    count = 0
    runs = db.scalars(select(CollectionRun).options(selectinload(CollectionRun.media),
                                                    selectinload(CollectionRun.subsource))
                      .where(CollectionRun.finished_at.is_not(None),
                             CollectionRun.media_id.is_not(None),
                             CollectionRun.run_type.in_(("feed", "page")))
                      .order_by(CollectionRun.id)).all()
    for run in runs:
        before = db.get(DailyObservation, _key(run))
        last_id = before.last_run_id if before else 0
        record_run(db, run)
        count += bool(run.id and run.id > last_id)
    return count


def _key(run: CollectionRun) -> str:
    day = to_local(run.started_at).date()
    scope = f"s{run.subsource_id}" if run.subsource_id is not None else f"m{run.media_id or 0}"
    return f"{day.isoformat()}:{run.run_type}:{scope}"


def coverage_metrics(db: Session, since: datetime, until: datetime,
                     media_id: int | None = None) -> dict:
    first = to_local(since).date()
    last = to_local(until - timedelta(microseconds=1)).date()
    stmt = select(DailyObservation).where(DailyObservation.day >= first,
                                           DailyObservation.day <= last)
    if media_id is not None:
        stmt = stmt.where(DailyObservation.media_id == media_id)
    totals: dict[str, dict] = defaultdict(lambda: {
        "attempted": 0, "successful": 0, "empty": 0, "failed": 0,
        "items_found": 0, "items_new": 0, "days": set(),
    })
    for row in db.scalars(stmt):
        item = totals[row.media_name or "Sin medio"]
        for field in ("attempted", "successful", "empty", "failed", "items_found", "items_new"):
            item[field] += getattr(row, field)
        item["days"].add(row.day)
    out = []
    period_days = max(0, (last - first).days + 1)
    for name, item in sorted(totals.items()):
        attempted = item["attempted"]
        out.append({"name": name, **{k: item[k] for k in item if k != "days"},
                    "days": len(item["days"]),
                    "success_rate": item["successful"] / attempted if attempted else None})
    return {"media": out, "from": first.isoformat(), "to": last.isoformat(),
            "period_days": period_days}
