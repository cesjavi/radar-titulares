from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import ADMIN_PASSWORD, csrf_from, login


def test_login_page_has_csrf_and_security_headers(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert csrf_from(r.text)
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"


def test_login_success_sets_httponly_samesite_cookie(client, admin):
    r = login(client)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    cookie = r.headers["set-cookie"].lower()
    assert "radar_session=" in cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "secure" not in cookie  # desarrollo sin HTTPS
    assert client.get("/").status_code == 200


def test_wrong_password_rejected(client, admin):
    r = login(client, password="incorrecta-incorrecta")
    assert r.status_code == 401
    assert "incorrectos" in r.text
    assert client.get("/", follow_redirects=False).status_code == 303


def test_unknown_user_rejected_same_message(client, admin):
    r = login(client, username="nadie", password="lo-que-sea-largo")
    assert r.status_code == 401
    assert "Usuario o contraseña incorrectos." in r.text


def test_login_without_csrf_is_forbidden(client, admin):
    client.get("/login")
    r = client.post("/login", data={"username": "admin", "password": ADMIN_PASSWORD},
                    follow_redirects=False)
    assert r.status_code == 403


def test_login_rate_limited_after_failures(client, admin):
    for _ in range(5):
        assert login(client, password="mal-mal-mal-mal").status_code == 401
    r = login(client)  # incluso con la contraseña correcta
    assert r.status_code == 429
    assert "Demasiados intentos" in r.text


def test_successful_login_resets_failures(client, admin):
    for _ in range(4):
        login(client, password="mal-mal-mal-mal")
    assert login(client).status_code == 303


def test_inactive_user_cannot_login(client):
    from tests.conftest import make_user

    make_user("baja", is_active=False)
    assert login(client, username="baja").status_code == 401


def test_open_redirect_blocked(client, admin):
    r = login(client, next_url="//malicioso.example/x")
    assert r.headers["location"] == "/"
    client.cookies.clear()
    r = login(client, next_url="/noticias")
    assert r.headers["location"] == "/noticias"


def test_logout_requires_csrf_and_clears_session(client, admin):
    login(client)
    page = client.get("/")
    assert client.post("/logout", follow_redirects=False).status_code == 403
    r = client.post("/logout", data={"csrf_token": csrf_from(page.text)}, follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 303


def test_password_change_invalidates_other_sessions(admin):
    from radar.web.app import create_app

    app = create_app()
    with TestClient(app) as a, TestClient(app) as b:
        login(a)
        login(b)
        token = csrf_from(a.get("/configuracion").text)
        r = a.post("/configuracion/cuenta/clave", data={
            "csrf_token": token, "current": ADMIN_PASSWORD,
            "new": "otra-clave-bien-larga", "confirm": "otra-clave-bien-larga",
        })
        assert r.status_code == 200 and "Contraseña actualizada" in r.text
        assert a.get("/", follow_redirects=False).status_code == 200
        assert b.get("/", follow_redirects=False).status_code == 303


def test_secure_cookie_in_production(monkeypatch, admin):
    from radar.config import get_settings
    from radar.web.app import create_app

    monkeypatch.setenv("RADAR_ENV", "production")
    get_settings.cache_clear()
    with TestClient(create_app(), base_url="https://testserver") as c:
        r = login(c)
        assert "secure" in r.headers["set-cookie"].lower()
        assert "strict-transport-security" in r.headers


def test_password_hash_is_argon2_and_min_length(db, admin):
    import pytest

    from radar.models import User
    from radar.security import hash_password

    user = db.query(User).one()
    assert user.password_hash.startswith("$argon2id$")
    assert ADMIN_PASSWORD not in user.password_hash
    with pytest.raises(ValueError):
        hash_password("corta")


def test_missing_secret_key_fails(monkeypatch):
    import pytest

    from radar.config import ConfigError, load_settings

    monkeypatch.setenv("RADAR_SECRET_KEY", "")
    with pytest.raises(ConfigError):
        load_settings()


def test_cli_create_admin_reads_password_from_stdin(monkeypatch, capsys, db):
    import io

    from radar.cli import main
    from radar.models import User
    from radar.security import verify_password

    monkeypatch.setattr("sys.stdin", io.StringIO("una-clave-segura-123\n"))
    assert main(["create-admin", "jefa", "--password-stdin"]) == 0
    user = db.query(User).filter_by(username="jefa").one()
    assert user.is_admin and verify_password(user.password_hash, "una-clave-segura-123")

    monkeypatch.setattr("sys.stdin", io.StringIO("corta\n"))
    try:
        main(["create-admin", "otro", "--password-stdin"])
    except SystemExit as exc:
        assert "al menos" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("debió rechazar la contraseña corta")
