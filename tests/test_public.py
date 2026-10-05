"""Vista pública de solo lectura (RADAR_PUBLIC_MODE)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from radar.alerts.engine import set_status, update_alerts
from radar.analysis.engine import run_analysis
from radar.config import get_settings
from radar.models import AiAnalysis, Alert, ArticleRelation, StoryGroup, Subsource, User
from tests.conftest import csrf_from, login
from tests.test_analysis import add, media, seed_story  # noqa: F401


def public_client(monkeypatch, **env):
    from radar.web.app import create_app

    monkeypatch.setenv("RADAR_PUBLIC_MODE", "true")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    return TestClient(create_app())


@pytest.fixture
def story(db, media):
    a, b, c = seed_story(db, media)
    run_analysis(db)
    db.commit()
    update_alerts(db)
    db.commit()
    return a, b, c


def test_disabled_by_default_requires_login(client):
    assert client.get("/", follow_redirects=False).status_code == 303
    assert client.get("/grupos", follow_redirects=False).status_code == 303


def test_visitor_can_read_public_pages(monkeypatch, db, story):
    a, b, c = story
    group = db.scalar(select(StoryGroup))
    rel = db.scalar(select(ArticleRelation))
    with public_client(monkeypatch) as visitor:
        for path in ("/", "/noticias", f"/noticias/{a.id}", "/grupos", f"/grupos/{group.id}",
                     f"/relaciones/{rel.id}", "/cronologia", "/metricas", "/fuentes", "/alertas"):
            r = visitor.get(path, follow_redirects=False)
            assert r.status_code == 200, path
            assert "Vista pública de solo lectura" in r.text
            assert "Ingresar" in r.text and "Salir (" not in r.text
        body = visitor.get(f"/grupos/{group.id}").text
        assert "Correcciones manuales" not in body and "Separar seleccionadas" not in body


@pytest.mark.parametrize("path", ["/revision", "/historial", "/ejecuciones", "/configuracion"])
def test_internal_pages_still_require_login(monkeypatch, db, path):
    with public_client(monkeypatch) as visitor:
        r = visitor.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_visitor_cannot_modify_anything(monkeypatch, db, story):
    group = db.scalar(select(StoryGroup))
    rel = db.scalar(select(ArticleRelation))
    sub_id = db.scalar(select(Subsource.id))
    with public_client(monkeypatch) as visitor:
        token = csrf_from(visitor.get("/login").text)
        for path, data in ((f"/relaciones/{rel.id}/revisar", {"decision": "confirmada"}),
                           (f"/grupos/{group.id}/quitar", {"article_id": "1"}),
                           (f"/fuentes/subfuente/{sub_id}/alternar", {}),
                           ("/configuracion/temas", {"name": "x"})):
            r = visitor.post(path, data={"csrf_token": token, **data}, follow_redirects=False)
            assert r.status_code in (303, 401, 403), path
    db.expire_all()
    assert db.get(ArticleRelation, rel.id).review_status == "pendiente"


def test_pending_alerts_hidden_reviewed_alerts_public(monkeypatch, db, story, admin):
    alert = db.scalar(select(Alert))
    with public_client(monkeypatch) as visitor:
        assert alert.title not in visitor.get("/alertas").text
        assert visitor.get(f"/alertas/{alert.id}").status_code == 404
        set_status(db, alert, "revisada", db.scalar(select(User)), "nota interna del revisor")
        db.commit()
        body = visitor.get(f"/alertas/{alert.id}").text
        assert alert.title in body and "nota interna del revisor" not in body
        assert "Historial" not in body
        assert alert.title in visitor.get("/alertas").text
        assert alert.title in visitor.get("/").text


def test_public_alerts_none(monkeypatch, db, story, admin):
    alert = db.scalar(select(Alert))
    set_status(db, alert, "revisada", db.scalar(select(User)), None)
    db.commit()
    with public_client(monkeypatch, RADAR_PUBLIC_ALERTS="ninguna") as visitor:
        assert 'href="/alertas"' not in visitor.get("/").text
        assert visitor.get(f"/alertas/{alert.id}").status_code == 404


def test_demo_data_never_shown_to_visitors(monkeypatch, db, media, client, admin):
    from radar.demo import load_demo

    load_demo(db)
    db.commit()
    with public_client(monkeypatch, RADAR_DEMO_MODE="true") as visitor:
        body = visitor.get("/noticias").text
        assert "[DEMO]" not in body and "MODO DEMO" not in body
        from radar.models import Article

        art = db.scalar(select(Article).where(Article.is_demo.is_(True)))
        assert visitor.get(f"/noticias/{art.id}").status_code == 404
        # Un usuario logueado con modo demo sí los ve.
        login(visitor)
        assert "[DEMO]" in visitor.get("/noticias").text


def test_ai_analysis_and_source_errors_hidden(monkeypatch, db, story):
    from tests.test_ai import output

    rel = db.scalar(select(ArticleRelation))
    a, b = rel.article_a, rel.article_b
    db.add(AiAnalysis(cache_key="x", relation_id=rel.id, article_a_id=a.id, article_b_id=b.id,
                      provider="fake", model="modelo-x", prompt_version="v", status="ok",
                      result=json.dumps(output(a.title, b.title)), flags="[]"))
    sub = db.scalar(select(Subsource))
    sub.last_status, sub.last_error, sub.consecutive_failures = "error", "detalle interno 500", 1
    db.commit()
    with public_client(monkeypatch) as visitor:
        body = visitor.get(f"/relaciones/{rel.id}").text
        assert "modelo-x" not in body and "Analizada por IA" not in body
        assert "detalle interno 500" not in visitor.get("/fuentes").text
        rel.review_status = "confirmada"
        db.commit()
        assert "Analizada por IA" in visitor.get(f"/relaciones/{rel.id}").text


def test_visitor_rate_limit(monkeypatch, db):
    with public_client(monkeypatch, RADAR_PUBLIC_RATE_LIMIT="3") as visitor:
        codes = [visitor.get("/salud").status_code for _ in range(4)]
        assert codes == [200, 200, 200, 429]
        assert visitor.get("/static/css/app.css").status_code == 200  # estáticos exentos
