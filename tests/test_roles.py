"""Perfiles de usuario: el analizador analiza grupos con IA y los oculta; nada más."""

from __future__ import annotations

import pytest

from radar.models import StoryGroup, User
from tests.conftest import csrf_from, login, make_user
from tests.test_analysis import add, media, seed_story  # noqa: F401  (fixture reutilizado)
from tests.test_panel_groups import _setup


def test_role_keeps_is_admin_in_sync():
    assert User(username="a", password_hash="x", is_admin=True).role == "admin"
    u = User(username="b", password_hash="x", role="admin")
    assert u.is_admin
    u.role = "analizador"
    assert not u.is_admin and u.can_analyze
    with pytest.raises(ValueError):
        u.role = "superusuario"


def test_analyst_can_hide_and_analyze_groups_only(client, db, media):
    a, b, c, group, rel = _setup(db, media)
    make_user("analista", role="analizador")
    login(client, username="analista")
    page = client.get(f"/grupos/{group.id}").text
    assert "Ocultar del resumen" in page and "Correcciones manuales" not in page
    token = csrf_from(page)

    r = client.post(f"/grupos/{group.id}/ocultar", data={"csrf_token": token, "ocultar": "true"},
                    follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert db.get(StoryGroup, group.id).hidden_at is not None

    # La IA está desactivada en las pruebas: el pedido se acepta y se informa por qué no corrió.
    r = client.post(f"/grupos/{group.id}/analizar-ia", data={"csrf_token": token},
                    follow_redirects=False)
    assert r.status_code == 303 and "?ia=" in r.headers["location"]

    # Lo demás sigue siendo del administrador.
    assert client.post(f"/relaciones/{rel.id}/revisar",
                       data={"csrf_token": token, "decision": "rechazada"}).status_code == 403
    assert client.post(f"/relaciones/{rel.id}/analizar-ia",
                       data={"csrf_token": token}).status_code == 403
    assert client.post(f"/grupos/{group.id}/separar",
                       data={"csrf_token": token, "article_ids": [str(c.id)]}).status_code == 403
    assert client.get("/configuracion").status_code == 403
    assert client.get("/configuracion/cuenta").status_code == 200


def test_admin_manages_users(client, admin, db):
    login(client)
    token = csrf_from(client.get("/configuracion").text)
    r = client.post("/configuracion/usuarios", data={
        "csrf_token": token, "username": "analista", "password": "una-clave-segura-123",
        "role": "analizador"}, follow_redirects=False)
    assert r.status_code == 303
    new = db.query(User).filter_by(username="analista").one()
    assert new.role == "analizador" and not new.is_admin

    r = client.post("/configuracion/usuarios", data={
        "csrf_token": token, "username": "analista", "password": "una-clave-segura-123"})
    assert r.status_code == 400 and "ya existe" in r.text

    # Nadie puede quitarse a sí mismo el perfil de administrador (el panel quedaría sin admin).
    me = db.query(User).filter_by(username="admin").one()
    r = client.post(f"/configuracion/usuarios/{me.id}", data={
        "csrf_token": token, "role": "analizador", "is_active": "on"})
    assert r.status_code == 400
    db.expire_all()
    assert db.get(User, me.id).is_admin

    # Desactivarlo le impide ingresar.
    r = client.post(f"/configuracion/usuarios/{new.id}", data={
        "csrf_token": token, "role": "analizador"}, follow_redirects=False)
    assert r.status_code == 303
    client.cookies.clear()
    assert login(client, username="analista", password="una-clave-segura-123").status_code != 303


def test_analyst_cannot_manage_users(client, db):
    make_user("analista", role="analizador")
    login(client, username="analista")
    token = csrf_from(client.get("/configuracion/cuenta").text)
    r = client.post("/configuracion/usuarios", data={
        "csrf_token": token, "username": "otro", "password": "una-clave-segura-123",
        "role": "admin"})
    assert r.status_code == 403
    assert db.query(User).filter_by(username="otro").first() is None
