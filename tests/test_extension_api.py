"""API de la extensión de Chrome: tokens, permisos e importación de posteos de X sin archivo."""

from __future__ import annotations

import re

from sqlalchemy import select

from radar.models import ApiToken, SocialPost
from tests.conftest import csrf_from, login, make_user
from tests.test_social import x_row


def _create_token(client, name="Chrome de prueba") -> str:
    t = csrf_from(client.get("/configuracion").text)
    page = client.post("/configuracion/tokens", data={"name": name, "csrf_token": t})
    assert page.status_code == 200 and "no se vuelve a mostrar" in page.text
    return re.search(r"(rdx_[A-Za-z0-9_-]+)", page.text).group(1)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_token_is_shown_once_and_stored_hashed(client, admin, db):
    login(client)
    raw = _create_token(client)
    assert raw not in client.get("/configuracion").text
    stored = db.scalar(select(ApiToken))
    assert stored.token_hash != raw and raw not in stored.token_hash
    st = client.get("/api/x/estado", headers=_auth(raw))
    assert st.status_code == 200 and st.json()["usuario"] == "admin"


def test_api_rejects_missing_wrong_and_revoked_tokens(client, admin, db):
    assert client.get("/api/x/estado").status_code == 401
    assert client.get("/api/x/estado", headers=_auth("rdx_inventado")).status_code == 401
    assert client.post("/api/x/posteos", json={"posteos": []}).status_code == 401
    login(client)
    raw = _create_token(client)
    token_id = db.scalar(select(ApiToken.id))
    # La sesión del panel no alcanza para la API, y revocar exige CSRF.
    assert client.post(f"/configuracion/tokens/{token_id}/revocar").status_code == 403
    t = csrf_from(client.get("/configuracion").text)
    assert client.post(f"/configuracion/tokens/{token_id}/revocar", data={"csrf_token": t},
                       follow_redirects=False).status_code == 303
    assert client.get("/api/x/estado", headers=_auth(raw)).status_code == 401


def test_token_creation_needs_admin(client):
    make_user("lector", is_admin=False)
    login(client, "lector")
    t = csrf_from(client.get("/").text)
    assert client.post("/configuracion/tokens", data={"csrf_token": t}).status_code == 403


def test_token_of_deactivated_user_stops_working(client, admin, db):
    login(client)
    raw = _create_token(client)
    token = db.scalar(select(ApiToken))
    token.user.is_active = False
    db.commit()
    assert client.get("/api/x/estado", headers=_auth(raw)).status_code == 401


def test_api_imports_posts_idempotently(client, admin, db):
    login(client)
    raw = _create_token(client)
    client.cookies.clear()  # la extensión no tiene sesión
    rows = [x_row(2000001, "cuentaprueba", "Texto sintético de prueba uno"),
            x_row(2000002, "cuentaprueba", "Texto sintético de prueba dos", view_count="15")]
    r = client.post("/api/x/posteos", json={"posteos": rows}, headers=_auth(raw))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["importacion"]["new"] == 2 and "vinculos" in body
    again = client.post("/api/x/posteos", json={"posteos": rows, "vincular": False}, headers=_auth(raw))
    assert again.json()["importacion"]["unchanged"] == 2 and "vinculos" not in again.json()
    assert len(db.scalars(select(SocialPost)).all()) == 2
    assert db.scalar(select(ApiToken)).last_used_at is not None


def test_api_validates_body(client, admin, monkeypatch):
    login(client)
    raw = _create_token(client)
    h = _auth(raw)
    assert client.post("/api/x/posteos", content=b"no es json", headers=h).status_code == 400
    assert client.post("/api/x/posteos", json={"otra": 1}, headers=h).status_code == 400
    bad = client.post("/api/x/posteos", json={"posteos": [{"foo": 1}]}, headers=h)
    assert bad.status_code == 400 and "No se reconoció" in bad.json()["motivo"]
    monkeypatch.setattr("radar.web.routes.api_routes.MAX_POSTS_PER_REQUEST", 1)
    rows = [x_row(2000010 + i, "cuentaprueba", f"Texto {i}") for i in range(2)]
    assert client.post("/api/x/posteos", json={"posteos": rows}, headers=h).status_code == 413


def test_non_admin_token_cannot_import(client, admin, db):
    from radar.db import session_scope
    from radar.models import User
    from radar.security import create_api_token

    make_user("lector", is_admin=False)
    with session_scope() as s:
        _, raw = create_api_token(s, s.scalar(select(User).where(User.username == "lector")), "x")
    assert client.get("/api/x/estado", headers=_auth(raw)).json()["admin"] is False
    r = client.post("/api/x/posteos", json={"posteos": [x_row(2000020, "cuentaprueba", "hola")]},
                    headers=_auth(raw))
    assert r.status_code == 403
