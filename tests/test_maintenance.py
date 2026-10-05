"""Backup (API de SQLite), restauración verificada, retención, healthcheck y directorios."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from radar.config import get_settings
from radar.maintenance import (
    MaintenanceError,
    backup,
    list_backups,
    purge,
    restore,
    restore_check,
)
from radar.models import Article, CollectionRun, StoryGroup, User
from radar.timeutil import utcnow
from tests.conftest import make_user
from tests.test_analysis import add, media, seed_story  # noqa: F401


@pytest.mark.sqlite_only
def test_backup_is_consistent_with_wal_active(db, media, tmp_path):
    seed_story(db, media)
    db.commit()
    # Escritura pendiente en el WAL de otra conexión abierta mientras se hace el backup.
    add(db, media["perfil"], "Nota escrita mientras se respalda", minutes=5)
    db.flush()
    settings = get_settings()
    wal = Path(str(settings.sqlite_path) + "-wal")
    assert wal.exists()
    path = backup(settings, tmp_path / "bk", keep=5)
    db.rollback()
    assert path.name.startswith("radar-") and path.suffix == ".db"
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    titles = {r[0] for r in conn.execute("SELECT title FROM articles")}
    conn.close()
    assert "Nota escrita mientras se respalda" not in titles  # no confirmada: no entra
    assert any("Claudia Pérez" in t for t in titles)


@pytest.mark.sqlite_only
def test_backup_rotation(db, tmp_path, monkeypatch):
    import radar.maintenance as m

    settings = get_settings()
    stamps = iter([f"2026010{i}-000000" for i in range(1, 6)])

    class FakeNow:
        def strftime(self, fmt):
            return next(stamps)

    monkeypatch.setattr(m, "utcnow", lambda: FakeNow())
    for _ in range(5):
        backup(settings, tmp_path, keep=3)
    names = [p.name for p in list_backups(tmp_path)]
    assert names == ["radar-20260103-000000.db", "radar-20260104-000000.db", "radar-20260105-000000.db"]


@pytest.mark.sqlite_only
def test_restore_check_on_copy_and_corrupt_backup(db, tmp_path):
    make_user("admin")
    path = backup(get_settings(), tmp_path, keep=2)
    before = path.read_bytes()
    report = restore_check(path)
    assert report.ok and report.integrity == "ok" and report.counts["users"] == 1
    assert report.alembic_version
    assert path.read_bytes() == before  # se verificó sobre una copia
    bad = tmp_path / "radar-roto.db"
    bad.write_bytes(b"esto no es una base sqlite" * 100)
    with pytest.raises(sqlite3.DatabaseError):
        restore_check(bad)
    with pytest.raises(MaintenanceError):
        restore_check(tmp_path / "no-existe.db")


@pytest.mark.sqlite_only
def test_restore_replaces_db_and_keeps_previous(db, tmp_path):
    from radar.db import reset_engine

    make_user("admin")
    settings = get_settings()
    path = backup(settings, tmp_path, keep=2)
    make_user("posterior")  # cambio posterior al backup
    db.close()
    reset_engine()
    saved = restore(settings, path)
    assert saved.exists() and "antes-de-restaurar" in saved.name
    conn = sqlite3.connect(settings.sqlite_path)
    users = {r[0] for r in conn.execute("SELECT username FROM users")}
    conn.close()
    assert users == {"admin"}
    conn = sqlite3.connect(saved)
    assert {r[0] for r in conn.execute("SELECT username FROM users")} == {"admin", "posterior"}
    conn.close()


@pytest.mark.sqlite_only
def test_restore_cli_requires_confirmation(db, tmp_path, capsys):
    from radar.cli import main

    path = backup(get_settings(), tmp_path, keep=1)
    assert main(["restore", str(path)]) == 2
    assert "--yes" in capsys.readouterr().err
    assert main(["restore-check", str(path)]) == 0


def test_purge_respects_retention_and_protected_groups(db, media, monkeypatch):
    from radar.analysis.engine import run_analysis

    a, b, c = seed_story(db, media)
    old = add(db, media["perfil"], "Nota muy vieja sin relevancia", minutes=60)
    old.last_seen_at = utcnow() - timedelta(days=400)
    run = CollectionRun(run_type="feed", status="ok", started_at=utcnow() - timedelta(days=60))
    db.add(run)
    db.commit()
    run_id, old_id = run.id, old.id
    run_analysis(db)
    db.commit()
    group = db.scalar(select(StoryGroup))
    group.locked = True  # corrección manual: se protege
    for art in (a, b, c):
        art.last_seen_at = utcnow() - timedelta(days=400)
    db.commit()
    monkeypatch.setenv("RADAR_RETENTION_DAYS", "365")
    get_settings.cache_clear()
    settings = get_settings()
    dry = purge(db, settings, dry_run=True)
    db.rollback()
    assert dry.articles == 1 and dry.runs >= 1
    assert db.get(Article, old_id) is not None
    r = purge(db, settings)
    db.commit()
    assert r.articles == 1
    db.expire_all()
    assert db.get(Article, old_id) is None
    assert db.get(Article, a.id) is not None  # protegida por el grupo corregido
    assert db.get(CollectionRun, run_id) is None


def test_retention_zero_disables_article_purge(db, media, monkeypatch):
    old = add(db, media["perfil"], "Vieja", minutes=60)
    old.last_seen_at = utcnow() - timedelta(days=4000)
    db.commit()
    monkeypatch.setenv("RADAR_RETENTION_DAYS", "0")
    get_settings.cache_clear()
    assert purge(db, get_settings()).articles == 0


def test_healthcheck_has_no_secrets(client, db, media):
    from radar.models import Subsource

    r = client.get("/salud")
    assert r.status_code == 200
    data = r.json()
    assert data["base"] is True and data["estado"] == "degradado"  # nunca recolectó
    assert set(data) == {"estado", "base", "ultima_recoleccion_min", "version"}
    sub = db.scalar(select(Subsource))
    sub.last_success_at = utcnow() - timedelta(minutes=3)
    db.commit()
    assert client.get("/salud").json()["estado"] == "ok"


def test_data_dir_controls_lock_and_default_db(monkeypatch, tmp_path):
    from radar.collector import runner

    monkeypatch.setenv("RADAR_DATA_DIR", str(tmp_path / "datos"))
    monkeypatch.delenv("RADAR_DATABASE_URL")
    monkeypatch.setattr(runner, "LOCK_PATH", None)
    get_settings.cache_clear()
    s = get_settings()
    assert s.sqlite_path == tmp_path / "datos" / "radar.db"
    assert runner.lock_file() == tmp_path / "datos" / "collect.lock"
    assert s.backup_dir == tmp_path / "datos" / "backups"


def test_job_lock_is_exclusive_and_expires(db):
    """El bloqueo de ejecución exclusiva funciona con el motor activo (archivo o tabla con
    vencimiento) y se puede volver a tomar tras liberarlo."""
    import pytest as _pytest

    from radar.joblock import LockBusy, job_lock

    with job_lock():
        with _pytest.raises(LockBusy), job_lock():
            pass
    with job_lock():  # liberado: se puede volver a tomar
        pass


def test_db_lease_expires_and_only_owner_releases(db):
    from datetime import timedelta

    from sqlalchemy import update

    from radar.joblock import DbLease
    from radar.models import JobLock
    from radar.timeutil import utcnow

    first, second = DbLease("prueba", 600), DbLease("prueba", 600)
    assert first.acquire() and not second.acquire()
    second.release()  # no es el dueño: no libera
    assert not DbLease("prueba", 600).acquire()
    from radar.db import session_scope

    with session_scope() as s:  # simula un proceso que murió: el bloqueo vence solo
        s.execute(update(JobLock).where(JobLock.name == "prueba")
                  .values(locked_until=utcnow() - timedelta(seconds=1)))
    assert second.acquire()
    second.release()
    assert first.acquire()
