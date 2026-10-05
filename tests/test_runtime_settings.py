"""Ajustes operativos editables desde el panel: recolección, vista pública y Telegram."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from radar import runtime
from radar.models import AppSetting, Subsource
from tests.conftest import csrf_from, login


def _post(client, path, **fields):
    token = csrf_from(client.get("/configuracion").text)
    return client.post(path, data={"csrf_token": token, **fields}, follow_redirects=False)


def test_env_rules_until_the_panel_saves(monkeypatch, db):
    monkeypatch.setenv("RADAR_ENRICH_PER_MEDIA", "7")
    monkeypatch.setenv("RADAR_PUBLIC_MODE", "true")
    runtime.invalidate()
    from radar.config import get_settings

    get_settings.cache_clear()
    assert runtime.get("enrich_per_media") == 7 and runtime.get("public_mode") is True
    runtime.save(db, {"enrich_per_media": "3", "public_mode": "false"})
    db.commit()
    assert runtime.get("enrich_per_media") == 3 and runtime.get("public_mode") is False


def test_collection_settings_saved_and_validated(client, admin, db):
    login(client)
    ok = _post(client, "/configuracion/recoleccion", enrich_per_media="4",
               retention_days="90", runs_retention_days="15")
    assert ok.status_code == 303
    assert runtime.get("retention_days") == 90
    assert runtime.get("enrich_per_media") == 4 and runtime.get("runs_retention_days") == 15
    for bad in ({"enrich_per_media": "500"}, {"retention_days": "-1"}, {"retention_days": "abc"}):
        data = {"enrich_per_media": "4", "retention_days": "90", "runs_retention_days": "15"} | bad
        assert _post(client, "/configuracion/recoleccion", **data).status_code == 400
    assert runtime.get("retention_days") == 90  # un envío inválido no cambia nada


def test_retention_from_panel_is_used_by_purge(client, admin, db):
    from radar.config import get_settings
    from radar.maintenance import purge

    login(client)
    _post(client, "/configuracion/recoleccion", enrich_per_media="0", retention_days="0",
          runs_retention_days="0")
    report = purge(db, get_settings(), dry_run=True)
    assert (report.articles, report.runs) == (0, 0)  # 0 días = no borrar


def test_public_mode_toggled_from_panel_applies_immediately(client, admin, db):
    visitor = TestClient(client.app, base_url="http://testserver")
    assert visitor.get("/", follow_redirects=False).status_code == 303  # privado por defecto
    login(client)
    assert _post(client, "/configuracion/publica", public_mode="on", public_alerts="ninguna",
                 public_rate_limit="50").status_code == 303
    assert visitor.get("/", follow_redirects=False).status_code == 200
    assert visitor.get("/configuracion", follow_redirects=False).status_code in (303, 401, 403)
    _post(client, "/configuracion/publica", public_alerts="revisadas", public_rate_limit="50")
    assert visitor.get("/", follow_redirects=False).status_code == 303  # se apagó sin reiniciar


def test_public_rate_limit_from_panel(client, admin, db):
    visitor = TestClient(client.app, base_url="http://testserver")
    login(client)
    _post(client, "/configuracion/publica", public_mode="on", public_alerts="revisadas",
          public_rate_limit="2")
    codes = [visitor.get("/").status_code for _ in range(4)]
    assert codes[:2] == [200, 200] and 429 in codes[2:]


def test_public_settings_validate(client, admin):
    login(client)
    assert _post(client, "/configuracion/publica", public_alerts="todo",
                 public_rate_limit="10").status_code == 400
    assert _post(client, "/configuracion/publica", public_alerts="todas",
                 public_rate_limit="99999").status_code == 400


def test_telegram_switch_from_panel(client, admin, db, monkeypatch):
    from radar.notify import telegram

    assert telegram.enabled() is False
    login(client)
    assert _post(client, "/configuracion/telegram", telegram_enabled="on").status_code == 303
    assert telegram.enabled() is True and telegram.configured() is False  # falta token/chat
    monkeypatch.setenv("RADAR_TELEGRAM_BOT_TOKEN", "123456:secreto-que-no-debe-verse")
    monkeypatch.setenv("RADAR_TELEGRAM_CHAT_ID", "999")
    assert telegram.configured() is True
    assert "secreto-que-no-debe-verse" not in client.get("/configuracion").text
    _post(client, "/configuracion/telegram")
    assert telegram.enabled() is False


def test_settings_posts_require_admin_and_csrf(client, admin):
    for path in ("/configuracion/recoleccion", "/configuracion/publica", "/configuracion/telegram"):
        assert client.post(path, data={}, follow_redirects=False).status_code in (303, 401, 403)
    login(client)
    for path in ("/configuracion/recoleccion", "/configuracion/publica", "/configuracion/telegram"):
        assert client.post(path, data={}).status_code == 403  # sin CSRF


def test_only_known_runtime_keys_are_read(db):
    db.add(AppSetting(key="cfg_inventado", value="1"))
    db.add(AppSetting(key="cfg_public_alerts", value="valor-raro"))
    db.commit()
    runtime.invalidate()
    assert "inventado" not in runtime.stored(db)
    assert runtime.get("public_alerts", db) == "revisadas"  # valor inválido: rige el entorno


@pytest.fixture
def sub(db):
    from radar.seed import seed_sources

    seed_sources(db)
    db.commit()
    return db.scalars(select(Subsource)).first()


def test_per_source_interval(client, admin, db, sub):
    login(client)
    token = csrf_from(client.get("/fuentes").text)
    r = client.post(f"/fuentes/subfuente/{sub.id}/intervalo", data={"csrf_token": token, "minutos": "30"})
    assert r.status_code == 200
    db.expire_all()
    assert db.get(Subsource, sub.id).min_interval_seconds == 30 * 60 - 30
    for bad in ("1", "5000", "abc", ""):
        assert client.post(f"/fuentes/subfuente/{sub.id}/intervalo",
                           data={"csrf_token": token, "minutos": bad}).status_code == 400
    assert client.post(f"/fuentes/subfuente/{sub.id}/intervalo",
                       data={"minutos": "30"}).status_code == 403
