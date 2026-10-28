"""Temas en alza: ventana reciente contra el ritmo habitual del mismo tema (mediana + 3 MAD)."""

from __future__ import annotations

from datetime import timedelta

from radar.analysis import trends
from radar.models import Article, Media, Topic
from radar.timeutil import utcnow
from tests.conftest import login


def test_threshold_needs_minimum_and_beats_usual():
    assert trends.is_rising(3, [0] * 100)[0] is False  # menos del mínimo
    assert trends.is_rising(4, [0] * 100)[0] is True
    up, med, thr = trends.is_rising(6, [5, 6, 4, 5, 7, 5] * 10)
    assert up is False and med == 5  # dentro de lo habitual de un tema con mucho volumen
    assert trends.is_rising(15, [5, 6, 4, 5, 7, 5] * 10)[0] is True


def test_window_counts_buckets_and_future_dates():
    now = utcnow()
    times = [now - timedelta(minutes=10), now + timedelta(minutes=30),  # algo adelantada: ahora
             now + timedelta(hours=5),  # posterior al momento evaluado: no cuenta
             now - timedelta(hours=4), now - timedelta(days=20)]  # fuera del período: no cuenta
    current, baseline = trends.window_counts(times, now)
    assert current == 2 and baseline[0] == 1 and sum(baseline) == 1
    assert len(baseline) == trends.BASELINE_DAYS * 24 // trends.WINDOW_HOURS
    # Con un hueco de recolección, la base usa solo las ventanas con datos.
    _, only_active = trends.window_counts(times, now, active={0, 1, 5})
    assert only_active == [1, 0]


def test_gap_and_stale_collection(db):
    _seed(db)
    # Si en las últimas 3 horas no se recolectó nada, no se marca ningún tema.
    stale = trends.compute(db, now=utcnow() + timedelta(hours=10))
    assert stale["rising"] == [] and "incompletas" in stale["note"]


def test_needs_minimum_history(db):
    m = Media(slug="m1", name="Medio Uno", base_url="https://m1.example")
    db.add(m)
    db.add(Topic(slug="tarifas", name="Tarifas", keywords="tarifas"))
    db.flush()
    for i in range(6):
        db.add(Article(media_id=m.id, url=f"https://x.example/{i}", canonical_url=f"https://x.example/{i}",
                       title="Suben las tarifas", search_text="suben las tarifas",
                       discovery_method="rss", content_hash=f"h{i}",
                       first_seen_at=utcnow() - timedelta(minutes=i),
                       published_at=utcnow() - timedelta(minutes=i)))
    db.commit()
    result = trends.compute(db)
    assert result["rising"] == [] and "historia" in result["note"]


def _seed(db):
    m1 = Media(slug="m1", name="Medio Uno", base_url="https://m1.example")
    m2 = Media(slug="m2", name="Medio Dos", base_url="https://m2.example")
    db.add_all([m1, m2])
    db.add(Topic(slug="tarifas", name="Tarifas", keywords="tarifa\ntarifas"))
    db.add(Topic(slug="clima", name="Clima", keywords="lluvia"))
    db.flush()
    now = utcnow()
    n = 0

    def art(media, title, when, published=True, syndicated=False):
        nonlocal n
        n += 1
        db.add(Article(media_id=media.id, url=f"https://x.example/{n}",
                       canonical_url=f"https://x.example/{n}", title=title,
                       search_text=title.lower(), discovery_method="rss", content_hash=f"h{n}",
                       first_seen_at=when, published_at=when if published else None,
                       is_syndicated=syndicated))

    for h in range(4, 14 * 24, 3):  # recolección continua: una nota por ventana de 3 h
        art(m1, f"Otra cosa {h}", now - timedelta(hours=h))
    for d in range(1, 14):  # ritmo habitual: una nota de tarifas y una de lluvia por día
        art(m1, f"Nueva tarifa {d}", now - timedelta(days=d))
        art(m1, f"Pronóstico de lluvia {d}", now - timedelta(days=d))
    for i in range(5):  # alza de tarifas en la última hora, en dos medios
        art(m1 if i % 2 else m2, f"Suben las tarifas, nota {i}", now - timedelta(minutes=10 + i))
    art(m2, "Tarifas de agencia", now - timedelta(minutes=5), syndicated=True)
    for i in range(5):  # sin hora de publicación: no cuentan (no se inventa la hora)
        art(m1, f"Lluvia hoy {i}", now - timedelta(minutes=20), published=False)
    db.commit()


def test_compute_flags_only_the_rising_topic(db):
    _seed(db)
    result = trends.compute(db)
    assert [r["slug"] for r in result["rising"]] == ["tarifas"]
    r = result["rising"][0]
    assert r["count"] == 5 and r["usual"] == 1
    assert r["media"] == 2 and sorted(r["media_names"]) == ["Medio Dos", "Medio Uno"]


def test_summary_shows_rising_topics(client, admin, db):
    _seed(db)
    trends.compute_and_store(db)
    db.commit()
    login(client)
    body = client.get("/").text
    assert "Temas en alza" in body and "/noticias?topic=tarifas&periodo=24h" in body
    assert "no explica por qué" in body


def test_topic_baseline_uses_same_time_slot():
    now = utcnow().replace(minute=0, second=0, microsecond=0)
    times = [now - timedelta(days=day) for day in range(1, 8) for _ in range(0)]
    times += [now - timedelta(hours=3 * index)
              for index in range(1, 40) if index % (24 // trends.WINDOW_HOURS) != 0
              for _ in range(5)]
    times += [now - timedelta(minutes=10) for _ in range(4)]
    active = set(range(0, 40))
    current, baseline = trends.window_counts(times, now, active, same_slot=True)
    assert current == 4 and baseline == [0, 0, 0, 0]
    assert trends.is_rising(current, baseline)[0] is True
