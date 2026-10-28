"""Cobertura diaria persistente de la recolección."""

from datetime import datetime, timedelta

from radar.models import CollectionRun, Media, Subsource
from radar.observations import coverage_metrics, record_run


def test_daily_observation_is_idempotent_and_reports_failures(db):
    media = Media(slug="m", name="Medio", base_url="https://m.example")
    db.add(media)
    db.flush()
    source = Subsource(media_id=media.id, name="RSS", kind="rss", url="https://m.example/rss")
    db.add(source)
    db.flush()
    when = datetime(2026, 10, 20, 12)
    ok = CollectionRun(media_id=media.id, subsource_id=source.id, run_type="feed", status="ok",
                       started_at=when, finished_at=when + timedelta(seconds=2), items_found=10,
                       items_new=3, duration_ms=2000)
    failed = CollectionRun(media_id=media.id, subsource_id=source.id, run_type="feed",
                           status="error", started_at=when + timedelta(hours=1),
                           finished_at=when + timedelta(hours=1, seconds=1), duration_ms=1000)
    db.add_all([ok, failed])
    db.flush()
    record_run(db, ok)
    record_run(db, ok)
    record_run(db, failed)
    db.commit()

    result = coverage_metrics(db, when - timedelta(hours=1), when + timedelta(days=1))
    assert result["media"] == [{"name": "Medio", "attempted": 2, "successful": 1,
                                 "empty": 0, "failed": 1, "items_found": 10,
                                 "items_new": 3, "days": 1, "success_rate": 0.5}]
