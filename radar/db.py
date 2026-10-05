"""Motor SQLAlchemy y sesiones. SQLite con WAL, busy_timeout y claves foráneas."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.pool import NullPool
from sqlalchemy.orm import Session, sessionmaker

from radar.config import get_settings

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _ensure_sqlite_dir(url: str) -> None:
    prefix = "sqlite:///"
    if url.startswith(prefix) and ":memory:" not in url:
        Path(url[len(prefix):]).parent.mkdir(parents=True, exist_ok=True)


def make_engine(url: str, serverless: bool = False) -> Engine:
    """SQLite (WAL, un solo servidor) o PostgreSQL (Neon u otro, por psycopg 3).

    En PostgreSQL con pooler de transacciones (Neon `-pooler`) se desactivan las sentencias
    preparadas del lado del cliente; en serverless no se mantiene un pool entre invocaciones.
    """
    if url.startswith("postgresql"):
        kwargs: dict = {"pool_pre_ping": True}
        connect_args: dict = {"connect_timeout": 15}  # Neon puede estar suspendido al conectar
        if "-pooler" in url:
            connect_args["prepare_threshold"] = None
        if serverless:
            kwargs["poolclass"] = NullPool
        else:
            kwargs.update(pool_size=3, max_overflow=2, pool_recycle=240)  # Neon cierra las inactivas
        return create_engine(url, connect_args=connect_args, **kwargs)
    _ensure_sqlite_dir(url)
    engine = create_engine(url, connect_args={"timeout": 5} if url.startswith("sqlite") else {})
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

    return engine


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        settings = get_settings()
        _engine = make_engine(settings.database_url, settings.serverless)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def reset_engine() -> None:
    """Descarta el motor cacheado (lo usan las pruebas al cambiar de base)."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def SessionLocal() -> Session:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transacción breve: commit al salir, rollback ante error."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
