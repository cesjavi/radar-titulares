"""Orden del resumen por interés: aprende de clics y ocultos, y solo reordena."""

from __future__ import annotations

from datetime import timedelta

from radar.analysis import relevance
from radar.models import AppSetting, Article, Media, StoryGroup, StoryGroupMember
from radar.timeutil import utcnow
from tests.conftest import login


def _media(db):
    m = Media(slug="m1", name="Medio Uno", base_url="https://m1.example")
    db.add(m)
    db.flush()
    return m


def _article(db, m, n, title, clicks=0, hidden=False):
    a = Article(media_id=m.id, url=f"https://m1.example/{n}", canonical_url=f"https://m1.example/{n}",
                title=title, search_text=title.lower(), discovery_method="rss",
                content_hash=f"h{n}", clicks=clicks, first_seen_at=utcnow() - timedelta(hours=n),
                hidden_at=utcnow() if hidden else None)
    db.add(a)
    db.flush()
    return a


def _seed_labels(db, m):
    # Interesan las notas de tarifas; no interesan las de fútbol (sintéticas).
    for i in range(12):
        _article(db, m, 100 + i, f"Suben las tarifas de luz y gas en la provincia {i}", clicks=2)
        _article(db, m, 200 + i, f"Resultado del partido de fútbol del domingo {i}", hidden=True)


def test_train_needs_both_classes():
    model = relevance.train([{"tarifa"}] * 3, [{"futbol"}] * 30)
    assert not relevance.usable(model) and model["positives"] == 3


def test_model_scores_interest_and_hidden_wins(db):
    m = _media(db)
    _seed_labels(db, m)
    both = _article(db, m, 300, "Tarifas de luz: nuevo aumento", clicks=5, hidden=True)
    db.commit()
    pos, neg = relevance.training_examples(db)
    assert len(pos) == 12 and len(neg) == 13  # la nota abierta y oculta cuenta como desinterés
    model = relevance.train_and_store(db)
    db.commit()
    assert relevance.usable(relevance.load_model(db))
    assert (relevance.article_score(model, "Otra suba de tarifas de gas", None)
            > 0 > relevance.article_score(model, "Partido de fútbol suspendido", None))
    assert db.get(AppSetting, relevance.SETTING_KEY) is not None and both.id


def _group(db, title, articles, media_count=1):
    g = StoryGroup(title=title, status="open", article_count=len(articles),
                   independent_media_count=media_count, first_seen_at=utcnow())
    db.add(g)
    db.flush()
    for a in articles:
        db.add(StoryGroupMember(group_id=g.id, article_id=a.id, status="activo"))
    return g


def test_summary_orders_by_interest_without_hiding(client, admin, db):
    m = _media(db)
    _seed_labels(db, m)
    futbol = _group(db, "Grupo fútbol", [_article(db, m, 1, "Partido de fútbol con polémica")],
                    media_count=3)
    tarifas = _group(db, "Grupo tarifas", [_article(db, m, 2, "Nuevas tarifas de luz y gas")])
    relevance.train_and_store(db)
    db.commit()
    login(client)
    body = client.get("/?orden=interes&limite=6").text
    assert "Solo cambia el orden" in body
    assert body.index(f"/grupos/{tarifas.id}") < body.index(f"/grupos/{futbol.id}")
    # Por medios, el de fútbol (más medios) va primero: el orden normal no cambia.
    body = client.get("/?orden=medios&limite=6").text
    assert body.index(f"/grupos/{futbol.id}") < body.index(f"/grupos/{tarifas.id}")


def test_summary_without_model_falls_back(client, admin, db):
    login(client)
    r = client.get("/?orden=interes")
    assert r.status_code == 200 and "Todavía no hay datos para ordenar por interés" in r.text
