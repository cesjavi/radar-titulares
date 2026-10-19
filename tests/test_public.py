"""Vista pública de solo lectura (RADAR_PUBLIC_MODE)."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from radar.alerts.engine import set_status, update_alerts
from radar.analysis.engine import run_analysis
from radar.config import get_settings
from radar.models import (
    AiAnalysis,
    AiGroupAnalysis,
    Alert,
    ArticleRelation,
    StoryGroup,
    Subsource,
    User,
)
from radar.timeutil import utcnow
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


def _group_ai(db, group, sujeto):
    from tests.test_ai import group_output

    arts = [m.article for m in group.members if m.status == "activo"]
    tones = [{"articulo": chr(65 + i), "sujeto": sujeto, "valoracion": "critico", "fragmento": ""}
             for i in range(len(arts))]
    row = AiGroupAnalysis(cache_key=sujeto, group_id=group.id,
                          article_ids=json.dumps([a.id for a in arts]), provider="fake",
                          model="modelo-x", prompt_version="v", status="ok",
                          result=json.dumps(group_output(arts, tono_por_nota=tones)), flags="[]")
    db.add(row)
    db.commit()
    return row


def test_group_ai_reading_public_only_after_review(monkeypatch, db, story, admin):
    group = db.scalar(select(StoryGroup))
    ai = _group_ai(db, group, "sujeto-sintetico")
    with public_client(monkeypatch) as visitor:
        staff = TestClient(visitor.app)
        login(staff)
        # Pendiente: el usuario la ve, el visitante no.
        assert "sujeto-sintetico" in staff.get("/sentimiento").text
        assert "sujeto-sintetico" not in visitor.get("/sentimiento").text
        assert "modelo-x" not in visitor.get(f"/grupos/{group.id}").text

        token = csrf_from(visitor.get("/login").text)
        r = visitor.post(f"/grupos/{group.id}/ia/{ai.id}/revisar",
                         data={"csrf_token": token, "decision": "confirmada"}, follow_redirects=False)
        assert r.status_code in (303, 401, 403)
        db.expire_all()
        assert db.get(AiGroupAnalysis, ai.id).review_status == "pendiente"

        def review(decision):
            token = csrf_from(staff.get(f"/grupos/{group.id}").text)
            r = staff.post(f"/grupos/{group.id}/ia/{ai.id}/revisar",
                           data={"csrf_token": token, "decision": decision}, follow_redirects=False)
            assert r.status_code == 303

        review("confirmada")
        assert "sujeto-sintetico" in visitor.get("/sentimiento").text
        assert "modelo-x" in visitor.get(f"/grupos/{group.id}").text

        # Rechazada: no cuenta para nadie (el detalle del grupo la sigue mostrando al usuario).
        review("rechazada")
        assert "sujeto-sintetico" not in staff.get("/sentimiento").text
        assert "sujeto-sintetico" not in visitor.get("/sentimiento").text
        assert "Revisión: rechazada" in staff.get(f"/grupos/{group.id}").text

        # Un grupo oculto no aparece en Sentimiento aunque esté confirmado.
        review("confirmada")
        db.expire_all()
        db.get(StoryGroup, group.id).hidden_at = utcnow()
        db.commit()
        assert "sujeto-sintetico" not in staff.get("/sentimiento").text


def test_summary_wording_and_stale_data_notice(monkeypatch, db, story):
    with public_client(monkeypatch, RADAR_PUBLIC_ALERTS="todas") as visitor:
        body = visitor.get("/").text
        assert "coordinadas" not in body and "coordinados" not in body
        # Con «todas» se ven alertas pendientes: el título no puede decir que están revisadas.
        assert "Alertas recientes" in body and "Alertas revisadas" not in body
        assert "Todavía no hubo ninguna recolección exitosa" in body

        sub = db.scalar(select(Subsource))
        sub.last_success_at = utcnow() - timedelta(hours=7)
        db.commit()
        assert "hace más de 6 horas que no entran notas nuevas" in visitor.get("/").text
        sub.last_success_at = utcnow()
        db.commit()
        assert "no entran notas nuevas" not in visitor.get("/").text


def test_summary_differs_for_visitors_and_team(monkeypatch, db, story, admin):
    with public_client(monkeypatch) as visitor:
        body = visitor.get("/?orden=interes&limite=24").text
        assert "Hoy en los medios" in body and "Cómo leer este radar" in body
        # Sin filtros ni pendientes internos para el visitante (los parámetros se ignoran).
        assert "Ordenar por:" not in body and "Para hacer" not in body
        # Las notas del grupo se acaban de detectar: hay barras en la actividad por hora.
        assert 'class="activity-bar"' in body
        staff = TestClient(visitor.app)
        login(staff)
        body = staff.get("/").text
        assert "Para hacer" in body and "Ordenar por:" in body
        assert "Cómo leer este radar" not in body
        assert "alerta" in body.split("Para hacer", 1)[1].split("</section>", 1)[0]
