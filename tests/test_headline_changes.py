"""Clasificación de cambios de titular y resumen por medio (titulares sintéticos)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from radar.analysis.headline_changes import change_stats, classify, transitions
from radar.models import Article, CollectionRun, HeadlineVersion, Media, Subsource
from radar.timeutil import utcnow
from tests.conftest import login


@pytest.mark.parametrize("old, new, expected", [
    ("La inflación de marzo fue del 3,1%", "La inflación de marzo fue del 3,4%", ["cifra"]),
    ("El gobierno aprobará la ley de tarifas", "El gobierno no aprobará la ley de tarifas",
     ["negacion"]),
    ("La empresa publica el informe mensual", "La empresa dejó de publicar el informe mensual",
     ["negacion"]),
    ("Reunión entre Ana Pérez y el gremio docente", "Reunión entre Juan Gómez y el gremio docente",
     ["nombre"]),
    ("suben las tarifas de luz", "Suben las tarifas de luz.", ["forma"]),
    ("Suben las tarifas de luz y gas en la provincia",
     "Tormenta deja calles anegadas en el conurbano", ["reescritura"]),
    ("Suben las tarifas de luz en la provincia", "Suben las tarifas de luz en toda la provincia",
     ["otro"]),
])
def test_classify(old, new, expected):
    assert classify(old, None, new, None) == expected


def test_subtitle_only_and_no_change():
    assert classify("Titular igual", "Bajada uno", "Titular igual", "Bajada dos") == ["bajada"]
    assert classify("Titular igual", "Bajada", "Titular igual", "Bajada") == []
    # Que la bajada aparezca (otra subfuente) o llegue recortada no es un cambio del medio.
    assert classify("Titular igual", None, "Titular igual", "Bajada nueva") == []
    assert classify("Titular igual", "Una bajada larga con más", "Titular igual",
                    "Una bajada larga con más palabras al final") == []
    assert classify("Titular igual", "Una tormenta el lunes", "Titular igual",
                    "Una tormenta, el lunes") == []  # solo puntuación


def _seed(db):
    m = Media(slug="m1", name="Medio Uno", base_url="https://m1.example")
    db.add(m)
    db.flush()
    t0 = utcnow() - timedelta(days=1)
    arts = []
    for i, titles in enumerate([["La inflación fue del 3,1%", "La inflación fue del 3,4%"],
                                ["Sin cambios"],
                                ["El juez aprobó el pedido", "El juez no aprobó el pedido",
                                 "El juez no aprobó el pedido."]]):
        a = Article(media_id=m.id, url=f"https://m1.example/{i}", canonical_url=f"https://m1.example/{i}",
                    title=titles[-1], search_text=titles[-1].lower(), discovery_method="rss",
                    content_hash=f"h{i}", first_seen_at=t0)
        db.add(a)
        db.flush()
        for j, t in enumerate(titles):
            db.add(HeadlineVersion(article_id=a.id, title=t, content_hash=f"v{i}{j}",
                                   detected_at=t0 + timedelta(minutes=30 * j)))
        # Completar la bajada desde la página no es un cambio del medio.
        db.add(HeadlineVersion(article_id=a.id, title=titles[-1], subtitle="Bajada de la página",
                               content_hash=f"p{i}", origin="pagina",
                               detected_at=t0 + timedelta(hours=5)))
        arts.append(a)
    db.commit()
    return arts


def test_stats_per_media(db):
    arts = _seed(db)
    stats = change_stats(db)
    s = stats["media"][0]
    assert (s.articles, s.changed, s.changes) == (3, 2, 3)
    assert s.by_category == {"cifra": 1, "negacion": 1, "forma": 1}
    assert s.median_first_change_min == 30
    db.refresh(arts[2])
    cats = list(transitions(arts[2].versions).values())
    assert cats == [["negacion"], ["forma"]]


def test_history_page_shows_categories(client, admin, db):
    _seed(db)
    login(client)
    body = client.get("/historial?tipo=titulares").text
    assert "Qué cambian los medios" in body
    assert "Cambia una cifra" in body and "Agrega o quita una negación" in body
    assert "no dicen por qué se cambió" in body


def test_cross_subsource_variant_is_not_counted_as_rewrite(db):
    media = Media(slug="m1", name="Medio Uno", base_url="https://m1.example")
    db.add(media)
    db.flush()
    rss = Subsource(media_id=media.id, name="RSS", kind="rss", url="https://m1.example/rss")
    section = Subsource(media_id=media.id, name="Sección", kind="html_section",
                        url="https://m1.example/seccion")
    db.add_all([rss, section])
    db.flush()
    run_rss = CollectionRun(media_id=media.id, subsource_id=rss.id, status="ok")
    run_section = CollectionRun(media_id=media.id, subsource_id=section.id, status="ok")
    db.add_all([run_rss, run_section])
    db.flush()
    now = utcnow()
    article = Article(media_id=media.id, url="https://m1.example/nota",
                      canonical_url="https://m1.example/nota", title="Versión de portada",
                      search_text="version de portada", discovery_method="rss", content_hash="h",
                      first_seen_at=now)
    db.add(article)
    db.flush()
    db.add_all([
        HeadlineVersion(article_id=article.id, title="Versión del RSS", content_hash="a",
                        subsource_id=rss.id, collection_run_id=run_rss.id, detected_at=now),
        HeadlineVersion(article_id=article.id, title="Versión de portada", content_hash="b",
                        subsource_id=section.id, collection_run_id=run_section.id,
                        detected_at=now + timedelta(minutes=1)),
    ])
    db.commit()

    stats = change_stats(db)
    row = stats["media"][0]
    assert row.changed == 0
    assert row.cross_source_variants == 1
