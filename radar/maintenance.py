"""Backups consistentes (API de backup de SQLite), verificación de restauración y retención.

Nunca se copia el archivo .db directamente: con WAL activo una copia del archivo puede
quedar inconsistente. La API de backup produce una copia coherente aunque la app esté en uso.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from radar.config import Settings
from radar.timeutil import utcnow

PREFIX = "radar-"
SUFFIX = ".db"


class MaintenanceError(RuntimeError):
    pass


def _db_path(settings: Settings) -> Path:
    path = settings.sqlite_path
    if path is None:
        raise MaintenanceError("La base no es un archivo SQLite local.")
    if not path.exists():
        raise MaintenanceError(f"No existe la base: {path}")
    return path


def _integrity(path: Path) -> str:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()


def backup(settings: Settings, dest_dir: Path | None = None, keep: int | None = None) -> Path:
    """Copia coherente con sqlite3.Connection.backup, verificada y con rotación."""
    src_path = _db_path(settings)
    dest_dir = Path(dest_dir or settings.backup_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(dest_dir, 0o750)
    except OSError:
        pass
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    final = dest_dir / f"{PREFIX}{stamp}{SUFFIX}"
    tmp = dest_dir / f".{final.name}.tmp"
    src = sqlite3.connect(f"file:{src_path.as_posix()}?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst, pages=1024)  # por tramos: no bloquea a los escritores mucho tiempo
    finally:
        dst.close()
        src.close()
    result = _integrity(tmp)
    if result != "ok":
        tmp.unlink(missing_ok=True)
        raise MaintenanceError(f"El backup no pasó integrity_check: {result}")
    try:
        os.chmod(tmp, 0o640)
    except OSError:
        pass
    os.replace(tmp, final)
    rotate(dest_dir, keep or settings.backup_keep)
    return final


def list_backups(dest_dir: Path) -> list[Path]:
    if not dest_dir.exists():
        return []
    return sorted((p for p in dest_dir.iterdir()
                   if p.name.startswith(PREFIX) and p.name.endswith(SUFFIX)), key=lambda p: p.name)


def rotate(dest_dir: Path, keep: int) -> list[Path]:
    """Conserva los `keep` backups más recientes; devuelve los eliminados."""
    backups = list_backups(dest_dir)
    removed = backups[:-keep] if keep > 0 else []
    for p in removed:
        p.unlink(missing_ok=True)
    return removed


@dataclass
class RestoreReport:
    ok: bool
    integrity: str
    alembic_version: str | None
    counts: dict
    size_bytes: int


def restore_check(backup_path: Path) -> RestoreReport:
    """Verifica un backup restaurándolo sobre una COPIA temporal (no toca la base real)."""
    backup_path = Path(backup_path)
    if not backup_path.exists():
        raise MaintenanceError(f"No existe el backup: {backup_path}")
    with tempfile.TemporaryDirectory(prefix="radar-restore-") as tmpdir:
        copy = Path(tmpdir) / "restaurada.db"
        shutil.copyfile(backup_path, copy)
        integrity = _integrity(copy)
        conn = sqlite3.connect(copy)
        try:
            version = None
            try:
                version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            except sqlite3.Error:
                pass
            counts = {}
            for table in ("articles", "media", "users", "story_groups", "alerts"):
                try:
                    counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                except sqlite3.Error:
                    counts[table] = None
        finally:
            conn.close()
    ok = integrity == "ok" and version is not None and counts.get("users") is not None
    return RestoreReport(ok, integrity, version, counts, backup_path.stat().st_size)


def restore(settings: Settings, backup_path: Path) -> Path:
    """Reemplaza la base por un backup verificado. Requiere los servicios detenidos.

    La base actual NO se borra: se renombra a radar.db.antes-de-restaurar-<fecha>.
    """
    report = restore_check(backup_path)
    if not report.ok:
        raise MaintenanceError(f"Backup inválido: integridad={report.integrity}, "
                               f"versión={report.alembic_version}")
    target = settings.sqlite_path
    if target is None:
        raise MaintenanceError("La base no es un archivo SQLite local.")
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    saved = None
    if target.exists():
        # Checkpoint para incorporar el WAL a la base antes de apartarla.
        conn = sqlite3.connect(target)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        saved = target.with_name(f"{target.name}.antes-de-restaurar-{stamp}")
        os.replace(target, saved)
    for suffix in ("-wal", "-shm"):
        Path(str(target) + suffix).unlink(missing_ok=True)
    tmp = target.with_name(f".{target.name}.restaurando")
    shutil.copyfile(backup_path, tmp)
    os.replace(tmp, target)
    return saved or target


@dataclass
class PurgeReport:
    articles: int = 0
    runs: int = 0
    ai_usage: int = 0
    notifications: int = 0
    groups: int = 0


def purge(db: Session, settings: Settings, dry_run: bool = False) -> PurgeReport:
    """Retención configurable. Conserva los artículos con decisiones humanas: los de grupos
    corregidos o con alertas revisadas/descartadas, y los de relaciones confirmadas o
    rechazadas."""
    from radar.models import (
        AiUsage,
        Alert,
        Article,
        ArticleRelation,
        CollectionRun,
        Notification,
        StoryGroup,
        StoryGroupMember,
    )

    report = PurgeReport()
    now = utcnow()
    if settings.retention_days:
        cutoff = now - timedelta(days=settings.retention_days)
        protected = (select(StoryGroupMember.article_id).join(StoryGroup)
                     .where((StoryGroup.locked.is_(True))
                            | StoryGroup.id.in_(select(Alert.group_id).where(
                                Alert.status.in_(("revisada", "descartada"))))))
        reviewed = ArticleRelation.review_status != "pendiente"
        reviewed_a = select(ArticleRelation.article_a_id).where(reviewed)
        reviewed_b = select(ArticleRelation.article_b_id).where(reviewed)
        cond = ((Article.last_seen_at < cutoff) & Article.id.not_in(protected)
                & Article.id.not_in(reviewed_a) & Article.id.not_in(reviewed_b))
        report.articles = db.scalar(select(func.count()).select_from(Article).where(cond)) or 0
        if not dry_run:
            db.execute(delete(Article).where(cond))
            # Grupos que quedaron sin miembros.
            empty = select(StoryGroup.id).where(~StoryGroup.id.in_(select(StoryGroupMember.group_id)))
            report.groups = db.scalar(select(func.count()).select_from(empty.subquery())) or 0
            db.execute(delete(Alert).where(Alert.group_id.in_(empty)))
            db.execute(delete(StoryGroup).where(StoryGroup.id.in_(empty)))
    if settings.runs_retention_days:
        cutoff = now - timedelta(days=settings.runs_retention_days)
        for model, col, attr in ((CollectionRun, CollectionRun.started_at, "runs"),
                                 (AiUsage, AiUsage.created_at, "ai_usage"),
                                 (Notification, Notification.created_at, "notifications")):
            where = col < cutoff
            if model is Notification:
                where = where & Notification.status.in_(("enviada", "error", "descartada"))
            setattr(report, attr, db.scalar(select(func.count()).select_from(model).where(where)) or 0)
            if not dry_run:
                db.execute(delete(model).where(where))
    return report
