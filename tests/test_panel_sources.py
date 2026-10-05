"""Panel: estado de fuentes, cobertura y procedencia."""

from __future__ import annotations

from sqlalchemy import select

from radar.models import AppSetting, Article, Subsource
from tests.conftest import csrf_from, login
from tests.test_collector import (  # noqa: F401  (fixture reutilizado)
    DESTAPE_SITEMAP,
    PERFIL_FEED,
    base_routes,
    collect,
    seeded,
)


def test_sources_page_shows_state_coverage_and_errors(client, admin, seeded):
    collect(base_routes(**{DESTAPE_SITEMAP: (403, b"prohibido")}))
    login(client)
    body = client.get("/fuentes").text
    assert "Bloqueada" in body
    assert "Acceso denegado por el sitio (HTTP 403)" in body
    assert "Operativa" in body
    assert "Último intento" in body and "Último éxito" in body
    assert "cuerpo" in body and "bajada" in body  # cobertura disponible
    # Un medio cuya extracción falló no muestra "0 noticias".
    assert "sin datos" in body


def test_summary_marks_failed_media_instead_of_zero(client, admin, seeded):
    for _ in range(3):
        collect(base_routes(**{DESTAPE_SITEMAP: (500, b"")}), only_media="eldestape")
    login(client)
    body = client.get("/").text
    assert "Caída" in body and "sin datos" in body
    r = client.get("/noticias?media=eldestape")
    assert "Los resultados pueden estar incompletos" in r.text


def test_article_detail_shows_provenance_and_origin(client, admin, seeded):
    collect(base_routes())
    db = seeded
    art = db.scalar(select(Article).where(Article.source_id == "1001"))
    canal_e = db.scalar(select(Article).where(Article.source_id == "1002"))
    login(client)
    body = client.get(f"/noticias/{art.id}").text
    assert "Procedencia" in body and "RSS general" in body and "Sitemap de noticias" in body
    assert "Referencias explícitas a otros medios" in body and "Infobae" in body
    assert "Identificador de origen" in body and "1001" in body
    assert "Canal E" in client.get(f"/noticias/{canal_e.id}").text
    assert "Canal E" in client.get("/noticias?media=perfil").text


def test_admin_sets_priority_sections(client, admin, seeded):
    login(client)
    token = csrf_from(client.get("/configuracion").text)
    r = client.post("/configuracion/secciones",
                    data={"csrf_token": token, "sections": "Política, economia\ncanal-e, economia"},
                    follow_redirects=False)
    assert r.status_code == 303
    seeded.expire_all()
    assert seeded.get(AppSetting, "priority_sections").value == "politica,economia,canal-e"


def test_toggle_keeps_state_labels(client, admin, seeded):
    sub = seeded.scalar(select(Subsource).where(Subsource.url == PERFIL_FEED))
    login(client)
    token = csrf_from(client.get("/fuentes").text)
    r = client.post(f"/fuentes/subfuente/{sub.id}/alternar", headers={"X-CSRF-Token": token})
    assert r.status_code == 200 and "Pausada" in r.text


def test_summary_shows_data_freshness_and_refreshes_itself(client, admin, db):
    from tests.conftest import login

    login(client)
    body = client.get("/").text
    assert "Última recolección exitosa" in body and "pantalla actualizada" in body
    assert 'hx-trigger="every 120s"' in body and 'id="resumen"' in body
