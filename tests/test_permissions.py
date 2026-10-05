from __future__ import annotations

import pytest

from tests.conftest import csrf_from, login, make_user

PROTECTED = ["/", "/noticias", "/noticias/1", "/fuentes", "/configuracion"]


@pytest.mark.parametrize("path", PROTECTED)
def test_anonymous_is_redirected_to_login(client, path):
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login?next=")


def test_anonymous_htmx_gets_hx_redirect(client):
    r = client.get("/noticias", headers={"HX-Request": "true"}, follow_redirects=False)
    assert r.status_code == 401
    assert r.headers["hx-redirect"].startswith("/login")


def test_anonymous_post_is_rejected(client):
    r = client.post("/configuracion/temas", data={"name": "x"}, follow_redirects=False)
    assert r.status_code in (303, 403)


def test_non_admin_cannot_access_configuration(client):
    make_user("lector", is_admin=False)
    assert login(client, username="lector").status_code == 303
    assert client.get("/").status_code == 200
    assert client.get("/configuracion").status_code == 403
    token = csrf_from(client.get("/fuentes").text)
    r = client.post("/fuentes/subfuente/1/alternar", headers={"X-CSRF-Token": token})
    assert r.status_code == 403


def test_admin_post_without_csrf_rejected(client, admin):
    login(client)
    r = client.post("/configuracion/temas", data={"name": "Nuevo"})
    assert r.status_code == 403
    r = client.post("/configuracion/temas", data={"name": "Nuevo", "csrf_token": "falso"})
    assert r.status_code == 403


def test_admin_can_create_and_edit_topic(client, admin, db):
    from radar.models import Topic

    login(client)
    token = csrf_from(client.get("/configuracion").text)
    r = client.post("/configuracion/temas", data={
        "csrf_token": token, "name": "Tarifas de energía", "keywords": "tarifas\nluz\nluz\n gas ",
    }, follow_redirects=False)
    assert r.status_code == 303
    topic = db.query(Topic).filter_by(slug="tarifas-de-energia").one()
    assert topic.keyword_list() == ["tarifas", "luz", "gas"]

    r = client.post(f"/configuracion/temas/{topic.id}", data={
        "csrf_token": token, "name": "Tarifas", "keywords": "tarifas", "exclude_keywords": "",
    }, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert db.get(Topic, topic.id).enabled is False  # casilla no enviada = inactivo


def test_admin_toggles_subsource_via_htmx(client, admin, db):
    from radar.models import Media, Subsource

    m = Media(slug="m", name="Medio", base_url="https://m.example")
    db.add(m)
    db.flush()
    s = Subsource(media_id=m.id, name="RSS", kind="rss", url="https://m.example/rss")
    db.add(s)
    db.commit()

    login(client)
    token = csrf_from(client.get("/fuentes").text)
    r = client.post(f"/fuentes/subfuente/{s.id}/alternar",
                    headers={"X-CSRF-Token": token, "HX-Request": "true"})
    assert r.status_code == 200 and "Pausada" in r.text
    db.expire_all()
    assert db.get(Subsource, s.id).enabled is False
