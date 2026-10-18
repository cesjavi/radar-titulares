"""Agenda compartida entre medios: Jaccard por par y semana, y cobertura sin coincidencia."""

from __future__ import annotations

from datetime import datetime, timedelta

from radar.metrics import PairStats, agenda_metrics
from radar.models import Article, Media, StoryGroup, StoryGroupMember
from tests.conftest import login

NOW = datetime(2026, 10, 14, 18, 0)  # miércoles, UTC


def _seed(db):
    m = {s: Media(slug=s, name=s.upper(), base_url=f"https://{s}.example") for s in ("a", "b", "c")}
    db.add_all(m.values())
    db.flush()
    n = 0

    def art(slug, when, syndicated=False):
        nonlocal n
        n += 1
        a = Article(media_id=m[slug].id, url=f"https://x.example/{n}",
                    canonical_url=f"https://x.example/{n}", title=f"Nota {n}",
                    search_text=f"nota {n}", discovery_method="rss", content_hash=f"h{n}",
                    first_seen_at=when, is_syndicated=syndicated)
        db.add(a)
        db.flush()
        return a

    def group(*arts):
        g = StoryGroup(title="Historia", status="open", article_count=len(arts),
                       first_seen_at=arts[0].first_seen_at)
        db.add(g)
        db.flush()
        for x in arts:
            db.add(StoryGroupMember(group_id=g.id, article_id=x.id, status="activo"))

    this_week = NOW - timedelta(hours=2)
    last_week = NOW - timedelta(days=7)
    group(art("a", this_week), art("b", this_week))  # A y B comparten
    group(art("a", this_week), art("c", this_week, syndicated=True))  # réplica: C no cuenta
    art("a", this_week)  # nota sola de A
    art("b", this_week)  # nota sola de B
    group(art("a", last_week), art("b", last_week))  # otra semana
    db.commit()
    return m


def test_jaccard_by_week_and_exclusive_coverage(db):
    _seed(db)
    data = agenda_metrics(db, weeks=2, now=NOW)
    ab = next(p for p in data["pairs"] if {p.a, p.b} == {"A", "B"})
    # Esta semana: A tiene 3 historias (dos grupos y una sola), B tiene 2 (un grupo y una sola);
    # comparten 1 → 1/4. La semana anterior comparten la única historia de cada uno → 1/1.
    assert ab.weekly == [(1, 1), (1, 4)]
    assert ab.shared == 2 and ab.union == 5 and abs(ab.jaccard - 0.4) < 1e-9
    assert not any("C" in (p.a, p.b) for p in data["pairs"])  # solo tenía una réplica
    exclusive = {e["name"]: e for e in data["exclusive"]}
    # La nota de A del grupo con la réplica de C no cuenta como coincidencia en otro medio.
    assert exclusive["A"] == {"name": "A", "notes": 4, "shared": 2, "alone": 2}
    assert [w.weekday() for w in data["weeks"]] == [0, 0]


def test_weeks_without_both_media_do_not_count(db):
    m = _seed(db)
    # D aparece recién esta semana: la semana anterior no cuenta para sus pares.
    d = Media(slug="d", name="D", base_url="https://d.example")
    db.add(d)
    db.flush()
    db.add(Article(media_id=d.id, url="https://x.example/d1", canonical_url="https://x.example/d1",
                   title="Nota D", search_text="nota d", discovery_method="rss",
                   content_hash="hd1", first_seen_at=NOW - timedelta(hours=1)))
    db.commit()
    data = agenda_metrics(db, weeks=4, now=NOW)
    assert len(data["weeks"]) == 2  # las dos semanas iniciales vacías se recortan
    ad = next(p for p in data["pairs"] if {p.a, p.b} == {"A", "D"})
    assert ad.weekly[0] is None and ad.weekly[1] == (0, 4)
    assert ad.union == 4  # sin las historias de A de la semana en que D no estaba


def test_gap_iqr_needs_four_cases():
    p = PairStats("A", "B", pub_gaps_min=[5, 10, 15])
    assert p.gap_iqr_min is None
    p.pub_gaps_min.append(40)
    q1, q3 = p.gap_iqr_min
    assert q1 < p.median_gap_min < q3


def test_metrics_page_shows_agenda(client, admin, db):
    _seed(db)
    login(client)
    body = client.get("/metricas").text
    assert "Agenda compartida" in body and "no</strong> indica coordinación" in body
    assert "Sin coincidencia detectada" in body
