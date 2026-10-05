"""Backups de PostgreSQL con pg_dump / pg_restore (solo con PostgreSQL real)."""

from __future__ import annotations

import pytest

from radar.config import get_settings
from radar.maintenance import MaintenanceError

pytestmark = pytest.mark.pg_only


def test_backup_verify_and_rotate(db, tmp_path):
    from radar.pgbackup import backup_postgres, check_dump
    from tests.conftest import make_user

    make_user("admin")
    first = backup_postgres(get_settings(), tmp_path, keep=2)
    assert first.suffix == ".dump" and first.name.startswith("radar-")
    info = check_dump(first)
    assert info["ok"] and "users" in info["tables"] and "articles" in info["tables"]
    for _ in range(2):  # distinta marca de tiempo por segundo
        import time

        time.sleep(1.1)
        backup_postgres(get_settings(), tmp_path, keep=2)
    assert len(list(tmp_path.glob("radar-*.dump"))) == 2  # rotación


def test_corrupt_dump_is_rejected(tmp_path):
    from radar.pgbackup import check_dump

    bad = tmp_path / "radar-roto.dump"
    bad.write_bytes(b"no es un backup de postgres" * 50)
    with pytest.raises(MaintenanceError):
        check_dump(bad)
    with pytest.raises(MaintenanceError):
        check_dump(tmp_path / "no-existe.dump")


def test_cli_backup_and_restore_message(db, tmp_path, capsys, monkeypatch):
    from radar.cli import main

    monkeypatch.setenv("RADAR_BACKUP_DIR", str(tmp_path))
    get_settings.cache_clear()
    assert main(["backup"]) == 0
    dump = next(tmp_path.glob("radar-*.dump"))
    assert "Backup verificado" in capsys.readouterr().out
    assert main(["restore-check", str(dump)]) == 0
    # La restauración sobre la base viva no se automatiza: informa el comando y no hace nada.
    assert main(["restore", str(dump), "--yes"]) == 2
    out = capsys.readouterr().out
    assert "pg_restore" in out and "Neon" in out


def test_error_messages_do_not_leak_the_password(tmp_path, monkeypatch):
    from radar.pgbackup import backup_postgres

    monkeypatch.setenv("RADAR_DATABASE_URL", "postgresql://usuario:CLAVE-SECRETA-123@127.0.0.1:1/inexistente")
    get_settings.cache_clear()
    with pytest.raises(MaintenanceError) as exc:
        backup_postgres(get_settings(), tmp_path)
    assert "CLAVE-SECRETA-123" not in str(exc.value)
