from __future__ import annotations

import os
import re

import pytest
from fastapi.testclient import TestClient

TEST_SECRET = "clave-de-pruebas-solo-para-tests-0123456789abcdef"
ADMIN_PASSWORD = "contraseña-admin-de-prueba"


PG_URL = os.environ.get("RADAR_TEST_PG_URL", "").strip()  # p. ej. postgresql://u@127.0.0.1:55432/radar_test
ON_POSTGRES = bool(PG_URL)


def pytest_configure(config):
    config.addinivalue_line("markers", "sqlite_only: usa funciones propias de SQLite (WAL, backup, archivos)")
    config.addinivalue_line("markers", "pg_only: necesita PostgreSQL real (RADAR_TEST_PG_URL)")


def pytest_collection_modifyitems(config, items):
    """Con RADAR_TEST_PG_URL la suite corre sobre PostgreSQL; las pruebas de SQLite se omiten."""
    if not ON_POSTGRES:
        skip_pg = pytest.mark.skip(reason="necesita PostgreSQL real: definir RADAR_TEST_PG_URL")
        for item in items:
            if "pg_only" in item.keywords:
                item.add_marker(skip_pg)
        return
    skip = pytest.mark.skip(reason="prueba específica de SQLite (RADAR_TEST_PG_URL está definida)")
    for item in items:
        if "sqlite_only" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """Base nueva por prueba, migrada con Alembic: SQLite temporal o, con RADAR_TEST_PG_URL, un
    esquema propio de PostgreSQL (se elimina al terminar)."""
    monkeypatch.setenv("RADAR_SKIP_DOTENV", "1")  # aislar de las claves del .env local
    for key in ("FIREWORKS_API_KEY", "GROQ_API_KEY", "ANTHROPIC_API_KEY", "RADAR_FIREWORKS_MODEL",
                "RADAR_GROQ_MODEL", "RADAR_AI_ENABLED", "RADAR_AI_PROVIDER", "RADAR_TELEGRAM_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("RADAR_SECRET_KEY", TEST_SECRET)
    schema = None
    if ON_POSTGRES:
        import uuid

        schema = f"t_{uuid.uuid4().hex[:12]}"
        _pg_admin(f'CREATE SCHEMA "{schema}"')
        sep = "&" if "?" in PG_URL else "?"
        monkeypatch.setenv("RADAR_DATABASE_URL", f"{PG_URL}{sep}options=-csearch_path%3D{schema}")
    else:
        monkeypatch.setenv("RADAR_DATABASE_URL", f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    monkeypatch.setenv("RADAR_ENV", "development")
    monkeypatch.setenv("RADAR_DEMO_MODE", "false")
    monkeypatch.setenv("RADAR_REQUEST_DELAY", "0")

    from radar.collector import runner
    from radar.config import get_settings
    from radar.db import reset_engine

    monkeypatch.setattr(runner, "LOCK_PATH", tmp_path / "collect.lock")
    get_settings.cache_clear()
    reset_engine()
    from radar.cli import upgrade_db

    upgrade_db()
    yield
    reset_engine()
    get_settings.cache_clear()
    if schema:
        _pg_admin(f'DROP SCHEMA "{schema}" CASCADE')


def _pg_admin(sql: str) -> None:
    import psycopg

    from radar.config import normalize_database_url

    url = normalize_database_url(PG_URL).replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(url, autocommit=True, connect_timeout=15) as conn:
        conn.execute(sql)


@pytest.fixture
def db():
    from radar.db import SessionLocal

    session = SessionLocal()
    yield session
    session.close()


def make_user(username="admin", password=ADMIN_PASSWORD, is_admin=True, is_active=True):
    from radar.db import session_scope
    from radar.models import User
    from radar.security import hash_password

    with session_scope() as s:
        user = User(username=username, password_hash=hash_password(password),
                    is_admin=is_admin, is_active=is_active)
        s.add(user)
        s.flush()
        return user.id


@pytest.fixture
def admin():
    make_user()
    return "admin"


@pytest.fixture
def client():
    from radar.web.app import create_app

    with TestClient(create_app(), base_url="http://testserver") as c:
        yield c


def csrf_from(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "no se encontró token CSRF"
    return m.group(1)


def login(client, username="admin", password=ADMIN_PASSWORD, next_url="/"):
    token = csrf_from(client.get("/login").text)
    return client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": token, "next": next_url},
        follow_redirects=False,
    )
