"""Ejecución manual desde Configuración: opciones, seguridad, bloqueo y resultado."""

from __future__ import annotations

import pytest

from radar import manual_run
from radar.collector.runner import CollectionSummary
from tests.conftest import csrf_from, login
from tests.test_add_media import SITE, FEED, routes
from tests.helpers import make_fetcher


def _run(client, **fields):
    token = csrf_from(client.get("/configuracion").text)
    r = client.post("/configuracion/ejecutar", data={"csrf_token": token, **fields},
                    follow_redirects=False)
    manual_run.wait()
    return r


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def fake_collection(only_media=None, force=False, enrich_pages=True, **kw):
        seen.append(("collect", only_media, force, enrich_pages))
        return CollectionSummary()

    monkeypatch.setattr("radar.pipeline.run_collection", fake_collection)
    monkeypatch.setattr("radar.cli._analyze", lambda: seen.append(("analyze",)) or type(
        "S", (), {"alerts": None, "docs_recent": 0, "relations_new": 0, "groups_new": 0})())
    return seen


def test_collect_options_are_passed_like_the_cli(client, admin, db, calls):
    login(client)
    r = _run(client, tarea="recolectar", force="on", no_enrich="on")
    assert r.status_code == 303
    assert calls == [("collect", None, True, False), ("analyze",)]
    page = client.get("/configuracion").text
    assert "Terminó" in page and "ignorando el intervalo" in page and "sin leer páginas" in page


def test_no_analyze_and_single_media(client, admin, db, calls):
    from radar.seed import seed_sources

    seed_sources(db)
    db.commit()
    login(client)
    _run(client, tarea="recolectar", medio="perfil", no_analyze="on")
    assert calls == [("collect", "perfil", False, True)]


def test_unknown_media_and_task_are_rejected(client, admin, calls):
    login(client)
    assert _run(client, tarea="recolectar", medio="inventado").status_code == 400
    assert _run(client, tarea="formatear").status_code == 400
    assert calls == []


def test_ai_task_validates_limit_and_respects_disabled_ai(client, admin, db):
    login(client)
    assert _run(client, tarea="ia", limit="0").status_code == 400
    assert _run(client, tarea="ia", limit="999").status_code == 400
    assert _run(client, tarea="ia", limit="2").status_code == 303  # IA desactivada: no llama a nadie
    st = manual_run.last_state(db)
    assert st["estado"] == "ok" and st["resultado"]["ia"]["llamadas"] == 0
    assert "desactivada" in (st["resultado"]["ia"]["motivo"] or "").lower()


def test_purge_dry_run_does_not_delete(client, admin, db):
    login(client)
    _run(client, tarea="mantenimiento", dry_run="on")
    st = manual_run.last_state(db)
    assert st["estado"] == "ok" and "simulación" in st["resultado"]["mantenimiento"]["estado"]


def test_probe_uses_safe_fetcher_and_stores_nothing(client, admin, db, monkeypatch):
    from radar.models import Article, Media

    db.add(Media(slug="ejemplo", name="Ejemplo", base_url=SITE, allowed_domains="ejemplo-diario.com.ar"))
    db.flush()
    from radar.models import Subsource

    media = db.query(Media).one()
    db.add(Subsource(media_id=media.id, name="RSS", kind="rss", url=FEED))
    db.commit()
    monkeypatch.setattr("radar.probe.SafeFetcher", lambda *a, **k: make_fetcher(routes()))
    login(client)
    _run(client, tarea="probar", medio="ejemplo")
    st = manual_run.last_state(db)
    assert st["resultado"]["resumen"] == {"ok": 1, "vacias": 0, "con_falla": 0}
    assert db.query(Article).count() == 0
    assert "Ejemplo" in client.get("/configuracion").text


def test_failure_is_reported_without_losing_the_page(client, admin, db, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("falla simulada " + "x" * 600)

    monkeypatch.setattr("radar.pipeline.analysis_phase", boom)
    login(client)
    assert _run(client, tarea="analizar").status_code == 303
    st = manual_run.last_state(db)
    assert st["estado"] == "error" and len(st["error"]) < 400
    assert client.get("/configuracion").status_code == 200


def test_only_one_manual_run_at_a_time(client, admin, db):
    from radar.models import AppSetting
    from radar.timeutil import utcnow
    import json

    db.add(AppSetting(key=manual_run.KEY, value=json.dumps({
        "estado": "en_curso", "inicio": utcnow().isoformat(), "descripcion": "x", "usuario": "a",
        "opciones": {}})))
    db.commit()
    login(client)
    r = _run(client, tarea="analizar")
    assert r.status_code == 400 and "en curso" in r.text
    # Una ejecución "en curso" muy vieja se considera abandonada y no bloquea.
    db.get(AppSetting, manual_run.KEY).value = json.dumps({
        "estado": "en_curso", "inicio": "2020-01-01T00:00:00", "descripcion": "x", "usuario": "a",
        "opciones": {}})
    db.commit()
    assert manual_run.last_state(db)["estado"] == "abandonada"


def test_requires_admin_and_csrf(client, admin, calls):
    assert client.post("/configuracion/ejecutar", data={"tarea": "analizar"},
                       follow_redirects=False).status_code in (303, 401, 403)
    login(client)
    assert client.post("/configuracion/ejecutar", data={"tarea": "analizar"}).status_code == 403
    assert client.get("/configuracion/ejecutar/estado").status_code == 200
    assert calls == []
