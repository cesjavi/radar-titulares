"""Bloqueo de ejecución exclusiva (recolección, análisis, restauración).

- SQLite (un solo servidor): bloqueo de archivo del sistema operativo (`ProcessLock`).
- PostgreSQL / Neon / Vercel: bloqueo con vencimiento en la tabla `job_locks`. No usa
  advisory locks de sesión (el pooler de transacciones de Neon no los admite) ni archivos
  (cada instancia serverless tiene su propio sistema de archivos). Si el proceso muere, el
  bloqueo caduca solo.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from radar.config import get_settings
from radar.db import session_scope
from radar.timeutil import utcnow


class LockBusy(RuntimeError):
    pass


DEFAULT_LEASE_SECONDS = 20 * 60  # más que el tiempo máximo de un ciclo (9 min por systemd)


class DbLease:
    """Bloqueo con vencimiento sobre `job_locks`; seguro entre procesos e instancias."""

    def __init__(self, name: str = "collect", lease_seconds: int = DEFAULT_LEASE_SECONDS):
        self.name = name
        self.lease = lease_seconds
        self.owner = f"{os.getpid()}-{uuid.uuid4().hex[:10]}"

    def acquire(self) -> bool:
        from radar.models import JobLock

        now = utcnow()
        until = now + timedelta(seconds=self.lease)
        with session_scope() as db:
            taken = db.execute(
                update(JobLock)
                .where(JobLock.name == self.name,
                       (JobLock.locked_until.is_(None)) | (JobLock.locked_until < now))
                .values(owner=self.owner, locked_until=until, acquired_at=now)
            ).rowcount
            if taken:
                return True
            if db.scalar(select(JobLock.name).where(JobLock.name == self.name)) is not None:
                return False  # lo tiene otro y todavía no venció
            try:
                with db.begin_nested():
                    db.add(JobLock(name=self.name, owner=self.owner, locked_until=until,
                                   acquired_at=now))
                return True
            except IntegrityError:
                return False  # otro proceso lo creó primero

    def release(self) -> None:
        from radar.models import JobLock

        with session_scope() as db:
            db.execute(update(JobLock)
                       .where(JobLock.name == self.name, JobLock.owner == self.owner)
                       .values(owner=None, locked_until=None))

    def __enter__(self):
        if not self.acquire():
            raise LockBusy("Ya hay una ejecución en curso.")
        return self

    def __exit__(self, *exc):
        self.release()


def job_lock(file_path: Path | None = None, name: str = "collect"):
    """Bloqueo adecuado al motor de base de datos. `file_path` solo aplica a SQLite."""
    settings = get_settings()
    if settings.is_postgres:
        return DbLease(name)
    from radar.collector.runner import ProcessLock, lock_file

    path = file_path or lock_file()
    if name != "collect":  # un archivo de bloqueo propio por tarea
        path = path.with_name(f"{name}.lock")
    return ProcessLock(path)


@contextmanager
def exclusive(name: str = "collect", file_path: Path | None = None):
    """`with exclusive(): …` — lanza `LockBusy` si hay otra ejecución."""
    lock = job_lock(file_path, name)
    try:
        with lock:
            yield
    except Exception as exc:  # CollectorBusy (archivo) se traduce al mismo tipo
        if type(exc).__name__ == "CollectorBusy":
            raise LockBusy(str(exc)) from exc
        raise
