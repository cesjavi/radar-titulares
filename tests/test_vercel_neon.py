"""PostgreSQL/Neon y despliegue en Vercel: configuración, motor, cron, IP real y concurrencia."""

from __future__ import annotations

import importlib
import sys
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from radar.config import direct_url, get_settings, normalize_database_url
from radar.db import make_engine

NEON_POOLED = ("postgresql://u:p@ep-cool-darkness-123456-pooler.us-east-2.aws.neon.tech/db"
               "?sslmode=require&channel_binding=require")


# --- configuración -------------------------------------------------------------------------


def test_normalize_url_for_psycopg_and_neon_params():
    assert normalize_database_url(NEON_POOLED).startswith("postgresql+psycopg://u:p@ep-cool")
    assert normalize_database_url(NEON_POOLED).endswith("?sslmode=require&channel_binding=require")
    assert normalize_database_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_database_url("postgresql+psycopg://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_database_url("sqlite:///x.db") == "sqlite:///x.db"


def test_direct_url_strips_pooler_only_from_the_host():
    direct = direct_url(normalize_database_url(NEON_POOLED))
    assert "-pooler" not in direct and "ep-cool-darkness-123456.us-east-2" in direct
    assert direct_url("postgresql://u:p-pooler@host/db") == "postgresql://u:p-pooler@host/db"  # clave intacta
    assert direct_url("sqlite:///a-pooler.db") == "sqlite:///a-pooler.db"


def test_settings_flags_and_direct_override(monkeypatch):
    monkeypatch.setenv("RADAR_DATABASE_URL", NEON_POOLED)
    get_settings.cache_clear()
    s = get_settings()
    assert s.is_postgres and s.uses_pooler and "-pooler" not in s.direct_database_url
    monkeypatch.setenv("RADAR_DATABASE_URL_DIRECT", "postgresql://u:p@otro-host/db")
    get_settings.cache_clear()
    assert get_settings().direct_database_url == "postgresql+psycopg://u:p@otro-host/db"


def test_generic_db_env_vars_are_only_read_on_vercel(monkeypatch):
    monkeypatch.delenv("RADAR_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@ajeno/db")  # variable de otro proyecto
    monkeypatch.delenv("VERCEL", raising=False)
    get_settings.cache_clear()
    assert get_settings().database_url.startswith("sqlite:///")
    monkeypatch.setenv("VERCEL", "1")
    get_settings.cache_clear()
    assert get_settings().database_url == "postgresql+psycopg://u:p@ajeno/db"
    assert get_settings().serverless


def test_engine_options_for_pooler_and_serverless(monkeypatch):
    import radar.db as dbmod

    captured = {}
    monkeypatch.setattr(dbmod, "create_engine", lambda url, **kw: captured.update(url=url, **kw) or object())
    dbmod.make_engine(normalize_database_url(NEON_POOLED), serverless=True)
    assert captured["connect_args"]["prepare_threshold"] is None  # PgBouncer en modo transacción
    assert captured["connect_args"]["connect_timeout"] == 15 and captured["pool_pre_ping"] is True
    from sqlalchemy.pool import NullPool

    assert captured["poolclass"] is NullPool
    captured.clear()
    dbmod.make_engine("postgresql+psycopg://u:p@db.interno/db", serverless=False)
    assert "prepare_threshold" not in captured["connect_args"]  # conexión directa: sin cambios
    assert captured["pool_size"] == 3 and captured["pool_recycle"] == 240 and "poolclass" not in captured


# --- restricciones y bloqueo (corren en SQLite y en PostgreSQL) ------------------------------


def _media(db):
    from radar.models import Media

    m = Media(slug="m", name="M", base_url="https://m.example")
    db.add(m)
    db.flush()
    return m


def _article(media_id, url, source_id):
    from radar.models import Article

    return Article(media_id=media_id, url=url, canonical_url=url, title="t", source_id=source_id,
                   discovery_method="rss", content_hash="h", search_text="t")


def test_unique_source_id_per_media_but_nulls_are_free(db):
    m = _media(db)
    db.add_all([_article(m.id, "https://m.example/1", None), _article(m.id, "https://m.example/2", None),
                _article(m.id, "https://m.example/3", "ID-1")])
    db.commit()
    db.add(_article(m.id, "https://m.example/4", "ID-1"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_one_alert_per_group_constraint(db):
    from radar.models import Alert, StoryGroup

    g = StoryGroup(title="g")
    db.add(g)
    db.flush()
    db.add(Alert(alert_type="x", title="a", group_id=g.id))
    db.add(Alert(alert_type="x", title="sin grupo 1"))
    db.add(Alert(alert_type="x", title="sin grupo 2"))
    db.commit()
    db.add(Alert(alert_type="x", title="duplicada", group_id=g.id))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_lease_acquire_is_atomic_under_concurrency(db):
    """Con 8 intentos simultáneos, exactamente uno obtiene el bloqueo."""
    from radar.joblock import DbLease

    leases = [DbLease("carrera", 600) for _ in range(8)]
    barrier = threading.Barrier(len(leases))
    results: list[bool] = []

    def attempt(lease):
        barrier.wait()
        try:
            results.append(lease.acquire())
        except Exception:  # un error de base nunca debe contar como "obtenido"
            results.append(False)

    threads = [threading.Thread(target=attempt, args=(lease,)) for lease in leases]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert results.count(True) == 1 and len(results) == 8


# --- app.py (Vercel) -------------------------------------------------------------------------


@pytest.fixture
def vercel_app(monkeypatch):
    """Importa app.py como lo haría Vercel (SQLite efímera permitida solo en pruebas)."""
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("RADAR_ALLOW_EPHEMERAL_DB", "true")
    monkeypatch.setenv("CRON_SECRET", "secreto-de-cron-de-prueba")
    get_settings.cache_clear()
    sys.modules.pop("app", None)
    module = importlib.import_module("app")
    yield module
    sys.modules.pop("app", None)


def _auth(secret="secreto-de-cron-de-prueba"):
    return {"Authorization": f"Bearer {secret}"}


def test_app_refuses_ephemeral_db_on_vercel(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("RADAR_ALLOW_EPHEMERAL_DB", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("RADAR_DATABASE_URL", "sqlite:///rechazada.db")  # aunque la suite corra en PostgreSQL
    get_settings.cache_clear()
    sys.modules.pop("app", None)
    from radar.config import ConfigError

    with pytest.raises(ConfigError, match="PostgreSQL"):
        importlib.import_module("app")
    sys.modules.pop("app", None)


def test_no_default_secret_key_anywhere(monkeypatch):
    from radar.config import ConfigError

    monkeypatch.delenv("RADAR_SECRET_KEY")
    monkeypatch.setenv("VERCEL", "1")
    get_settings.cache_clear()
    sys.modules.pop("app", None)
    with pytest.raises(ConfigError, match="RADAR_SECRET_KEY"):
        importlib.import_module("app")
    sys.modules.pop("app", None)


def test_cron_requires_secret_and_correct_bearer(vercel_app, monkeypatch):
    client = TestClient(vercel_app.app)
    assert client.get("/api/cron").status_code == 401
    assert client.get("/api/cron", headers=_auth("otro")).status_code == 401
    assert client.get("/api/cron", headers={"Authorization": "secreto-de-cron-de-prueba"}).status_code == 401
    monkeypatch.delenv("CRON_SECRET")
    assert client.get("/api/cron", headers=_auth()).status_code == 503  # sin secreto configurado: cerrado


def test_cron_runs_requested_phases_and_validates_input(vercel_app, monkeypatch):
    calls = []
    import radar.pipeline as pipeline

    monkeypatch.setattr(pipeline, "collect_phase", lambda media, force: calls.append(("c", media)) or {"estado": "ok"})
    monkeypatch.setattr(pipeline, "analysis_phase", lambda: calls.append(("a",)) or {"estado": "ok"})
    client = TestClient(vercel_app.app)
    r = client.get("/api/cron?tarea=recolectar&media=pagina12", headers=_auth())
    assert r.status_code == 200 and r.json()["recoleccion"] == {"estado": "ok"} and "analisis" not in r.json()
    client.get("/api/cron?tarea=analizar", headers=_auth())
    client.get("/api/cron", headers=_auth())
    assert calls == [("c", "pagina12"), ("a",), ("c", None), ("a",)]
    assert client.get("/api/cron?tarea=borrar", headers=_auth()).status_code == 400
    assert client.get("/api/cron?tarea=recolectar&media=clarin", headers=_auth()).status_code == 400


def test_cron_duplicate_run_is_skipped_not_repeated(vercel_app):
    """Vercel puede invocar dos veces el mismo cron: la segunda no hace trabajo."""
    from radar.joblock import job_lock

    client = TestClient(vercel_app.app)
    with job_lock():
        r = client.get("/api/cron?tarea=analizar", headers=_auth())
    assert r.status_code == 200 and r.json()["analisis"]["estado"] == "omitido"


def test_cron_error_does_not_leak_details(vercel_app, monkeypatch):
    import radar.pipeline as pipeline

    def boom(*a, **k):
        raise RuntimeError("postgresql://usuario:CLAVE-SECRETA@host/db")

    monkeypatch.setattr(pipeline, "collect_phase", boom)
    r = TestClient(vercel_app.app, raise_server_exceptions=False).get("/api/cron?tarea=recolectar", headers=_auth())
    assert r.status_code == 500 and "CLAVE-SECRETA" not in r.text


def test_real_pipeline_cycle_with_simulated_network(vercel_app, db, monkeypatch):
    """El ciclo completo (recolección → análisis → alertas) corre contra el motor activo."""
    from radar.collector.runner import run_collection as real
    from radar.seed import seed_settings, seed_sources
    from radar.models import Subsource
    from tests.helpers import Routes, fixture, make_fetcher
    import radar.pipeline as pipeline

    seed_sources(db)
    seed_settings(db)
    for sub in db.scalars(select(Subsource)):
        sub.enabled = sub.url == "https://www.pagina12.com.ar/arc/outboundfeeds/rss/secciones/el-pais/notas/"
    db.commit()
    routes = Routes({"https://www.pagina12.com.ar/arc/outboundfeeds/rss/secciones/el-pais/notas/":
                     (200, fixture("pagina12_feed.xml"))})

    def collect_with_mock(only_media=None, force=False, **kw):
        with make_fetcher(routes) as f:
            return real(only_media=only_media, force=force, enrich_pages=False, fetcher=f)

    monkeypatch.setattr(pipeline, "run_collection", collect_with_mock)
    result = TestClient(vercel_app.app).get("/api/cron?tarea=todo&media=pagina12", headers=_auth()).json()
    assert result["recoleccion"]["notas_nuevas"] == 3 and result["recoleccion"]["con_problemas"] == 0
    assert result["analisis"]["estado"] == "ok"


# --- IP real: solo se confía en x-forwarded-for dentro de Vercel -------------------------------


def _public_client(monkeypatch, vercel: bool):
    from radar.web.app import create_app

    monkeypatch.setenv("RADAR_PUBLIC_MODE", "true")
    monkeypatch.setenv("RADAR_PUBLIC_RATE_LIMIT", "2")
    if vercel:
        monkeypatch.setenv("VERCEL", "1")
    else:
        monkeypatch.delenv("VERCEL", raising=False)
    get_settings.cache_clear()
    return TestClient(create_app())


def test_rate_limit_uses_forwarded_ip_on_vercel(monkeypatch):
    c = _public_client(monkeypatch, vercel=True)
    for _ in range(2):
        assert c.get("/salud", headers={"x-forwarded-for": "203.0.113.5"}).status_code == 200
    assert c.get("/salud", headers={"x-forwarded-for": "203.0.113.5"}).status_code == 429
    assert c.get("/salud", headers={"x-forwarded-for": "203.0.113.6"}).status_code == 200  # otro visitante


def test_forwarded_header_cannot_evade_limits_outside_vercel(monkeypatch):
    c = _public_client(monkeypatch, vercel=False)
    codes = [c.get("/salud", headers={"x-forwarded-for": f"198.51.100.{i}"}).status_code for i in range(3)]
    assert codes == [200, 200, 429]  # cambiar el encabezado no da un cupo nuevo


def test_cron_maintenance_task_applies_retention(vercel_app, db, monkeypatch):
    from datetime import timedelta

    from radar.models import Article, Media
    from radar.timeutil import utcnow

    monkeypatch.setenv("RADAR_RETENTION_DAYS", "30")
    get_settings.cache_clear()
    m = Media(slug="m", name="M", base_url="https://m.example")
    db.add(m)
    db.flush()
    old = Article(media_id=m.id, url="https://m.example/vieja", canonical_url="https://m.example/vieja",
                  title="vieja", discovery_method="rss", content_hash="h", search_text="vieja",
                  last_seen_at=utcnow() - timedelta(days=100))
    new = Article(media_id=m.id, url="https://m.example/nueva", canonical_url="https://m.example/nueva",
                  title="nueva", discovery_method="rss", content_hash="h", search_text="nueva")
    db.add_all([old, new])
    db.commit()
    r = TestClient(vercel_app.app).get("/api/cron?tarea=mantenimiento", headers=_auth())
    assert r.status_code == 200 and r.json()["mantenimiento"]["notas_borradas"] == 1
    db.expire_all()
    assert [a.title for a in db.scalars(select(Article))] == ["nueva"]
