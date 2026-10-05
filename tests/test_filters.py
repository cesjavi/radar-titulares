"""Filtros y orden de las listas de alertas y grupos."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest

from radar.models import Alert, StoryGroup
from radar.timeutil import utcnow
from tests.conftest import login


@pytest.fixture
def data(db):
    now = utcnow()
    specs = [  # título, prioridad, notas, medios independientes, minutos atrás, corregido
        ("Flybondi pidió concurso de acreedores", "media", 4, 4, 10, False),
        ("Inflación de septiembre según el INDEC", "alta", 3, 3, 30, True),
        ("Paro de transporte en el conurbano", "baja", 2, 2, 20, False),
    ]
    for title, prio, notes, media, ago, locked in specs:
        g = StoryGroup(title=title, status="open", article_count=notes, media_count=media,
                       independent_media_count=media, locked=locked,
                       first_seen_at=now - timedelta(minutes=ago))
        db.add(g)
        db.flush()
        db.add(Alert(alert_type="coincidencia_grupo", title=title, group_id=g.id, status="pendiente",
                     priority=prio, article_count=notes, media_count=media,
                     independent_media_count=media, created_at=now - timedelta(minutes=ago)))
    db.commit()


def titles(html: str) -> list[str]:
    return re.findall(r'class="headline" href="/(?:alertas|grupos)/\d+">([^<]+)<', html)


def test_alerts_default_order_is_priority_for_pending(client, db, admin, data):
    login(client)
    got = titles(client.get("/alertas").text)
    assert got == ["Inflación de septiembre según el INDEC", "Flybondi pidió concurso de acreedores",
                   "Paro de transporte en el conurbano"]


def test_alerts_filter_and_sort(client, db, admin, data):
    login(client)
    assert titles(client.get("/alertas?q=flybondi").text) == ["Flybondi pidió concurso de acreedores"]
    assert titles(client.get("/alertas?min_medios=3&orden=notas").text) == [
        "Flybondi pidió concurso de acreedores", "Inflación de septiembre según el INDEC"]
    assert titles(client.get("/alertas?orden=novedad_asc").text)[0] == "Inflación de septiembre según el INDEC"
    assert titles(client.get("/alertas?orden=novedad").text)[0] == "Flybondi pidió concurso de acreedores"
    body = client.get("/alertas?q=zzz").text
    assert "con esos filtros" in body
    # Un orden desconocido no rompe la página.
    assert client.get("/alertas?orden=drop table").status_code == 200


def test_search_escapes_like_wildcards(client, db, admin, data):
    login(client)
    assert titles(client.get("/alertas?q=%25").text) == []
    assert titles(client.get("/grupos?q=_").text) == []


def test_groups_filter_and_sort(client, db, admin, data):
    login(client)
    assert titles(client.get("/grupos").text)[0] == "Flybondi pidió concurso de acreedores"
    assert titles(client.get("/grupos?orden=antiguos").text)[0] == "Inflación de septiembre según el INDEC"
    assert titles(client.get("/grupos?orden=notas").text)[0] == "Flybondi pidió concurso de acreedores"
    assert titles(client.get("/grupos?corregidos=true").text) == ["Inflación de septiembre según el INDEC"]
    assert len(titles(client.get("/grupos?min_medios=3").text)) == 2
    assert titles(client.get("/grupos?q=paro+transporte").text) == ["Paro de transporte en el conurbano"]
    assert "Ningún grupo coincide" in client.get("/grupos?q=zzz").text


def test_pager_keeps_filters(client, db, admin, data, monkeypatch):
    monkeypatch.setattr("radar.web.routes.analysis_routes.PER_PAGE", 1)
    login(client)
    body = client.get("/grupos?min_medios=3&orden=notas").text
    assert "min_medios=3" in body and "orden=notas" in body and "page=2" in body


def test_search_ignores_accents_and_case(client, db, admin, data):
    login(client)
    assert titles(client.get("/alertas?q=inflacion").text) == ["Inflación de septiembre según el INDEC"]
    assert titles(client.get("/grupos?q=INFLACIÓN+segun").text) == ["Inflación de septiembre según el INDEC"]
    assert titles(client.get("/grupos?q=Inflación").text) == ["Inflación de septiembre según el INDEC"]
