"""Ejecución de la recolección.

- Bloqueo entre procesos (no hay dos recolecciones a la vez).
- Descarga y parseo concurrentes (máx. RADAR_MAX_CONCURRENCY, por defecto 2), con límite
  de solicitudes por dominio; la escritura en SQLite ocurre en el hilo principal, en
  transacciones breves.
- Una fuente que falla no interrumpe a las demás.
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from radar import runtime
from radar.collector import enrich
from radar.collector.adapters import ADAPTERS, MediaAdapter
from radar.collector.generic import adapter_for_media
from radar.collector.ingest import NEW, SKIPPED, UPDATED, upsert_item
from radar.collector.parsers import FeedBlockedError, FeedItem, FeedParseError
from radar.config import get_settings
from radar.joblock import LockBusy, job_lock
from radar.db import session_scope
from radar.models import CollectionRun, Media, Subsource
from radar.net import BlockedError, DomainRateLimiter, FetchError, SafeFetcher, UnsafeURLError
from radar.seed import get_setting
from radar.timeutil import utcnow

log = logging.getLogger("radar.collector")

BATCH_SIZE = 50
FAILURES_FOR_DOWN = 3
# Si es None se usa RADAR_DATA_DIR/collect.lock (las pruebas lo reemplazan).
LOCK_PATH: Path | None = None


def lock_file() -> Path:
    return LOCK_PATH or (get_settings().data_dir / "collect.lock")


# Mismo tipo para el bloqueo de archivo (SQLite) y el bloqueo con vencimiento (PostgreSQL).
CollectorBusy = LockBusy


def check_media_slug(slug: str) -> None:
    """Falla si no hay un medio con ese slug (con adaptador propio o dado de alta en el panel)."""
    with session_scope() as db:
        slugs = sorted(db.scalars(select(Media.slug).where(Media.is_demo.is_(False))).all())
    if slug not in slugs:
        raise ValueError(f"Medio desconocido: {slug}. Opciones: {', '.join(slugs)}")


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        return default


class ProcessLock:
    """Bloqueo exclusivo entre procesos sobre un archivo (fcntl en Linux, msvcrt en Windows)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt

                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.fh.close()
            self.fh = None
            raise CollectorBusy("Ya hay una recolección en curso.") from exc
        return self

    def __exit__(self, *exc):
        if self.fh:
            if os.name == "nt":
                import msvcrt

                try:
                    self.fh.seek(0)
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            self.fh.close()
            self.fh = None


@dataclass
class SubsourceJob:
    sub_id: int
    run_id: int
    media_slug: str
    url: str
    kind: str
    max_items: int
    etag: str | None
    last_modified: str | None
    section: str = "general"
    adapter: MediaAdapter | None = None


@dataclass
class FetchOutcome:
    job: SubsourceJob
    status: str  # ok | not_modified | empty | error | blocked
    items: list[FeedItem] = field(default_factory=list)
    http_status: int | None = None
    error: str | None = None
    attempts: int = 1
    etag: str | None = None
    last_modified: str | None = None
    duration_ms: int = 0


@dataclass
class CollectionSummary:
    runs: list[CollectionRun] = field(default_factory=list)
    enrich_runs: list[CollectionRun] = field(default_factory=list)

    @property
    def errors(self) -> list[CollectionRun]:
        return [r for r in self.runs if r.status in ("error", "blocked", "empty")]


def fetch_subsource(fetcher: SafeFetcher, adapter: MediaAdapter, job: SubsourceJob) -> FetchOutcome:
    """Descarga y parsea una subfuente. Corre en un hilo y no toca la base."""
    t0 = time.monotonic()
    headers = {"Accept": "application/rss+xml, application/xml, text/xml, text/html;q=0.8"}
    if job.etag:
        headers["If-None-Match"] = job.etag
    if job.last_modified:
        headers["If-Modified-Since"] = job.last_modified
    out = FetchOutcome(job=job, status="error")
    try:
        resp = fetcher.get(job.url, adapter.allowed_domains, headers=headers)
        out.http_status, out.attempts = resp.status_code, resp.attempts
        out.etag, out.last_modified = resp.headers.get("etag"), resp.headers.get("last-modified")
        if resp.status_code == 304:
            out.status = "not_modified"
            out.etag, out.last_modified = out.etag or job.etag, out.last_modified or job.last_modified
        else:
            out.items = adapter.parse(resp.content, job.kind, resp.url,
                                      resp.headers.get("content-type"), job.max_items,
                                      None if job.section == "general" else job.section)
            if out.items:
                out.status = "ok"
            else:
                out.status = "empty"
                out.error = ("La respuesta no contiene notas reconocibles: posible cambio de "
                             "formato o de URL. No significa que no haya noticias.")
    except (BlockedError, FeedBlockedError) as exc:
        out.status, out.error = "blocked", str(exc)
        out.http_status = getattr(exc, "status_code", None) or out.http_status
    except FetchError as exc:
        out.status, out.error = "error", str(exc)
        out.http_status, out.attempts = exc.status_code or out.http_status, exc.attempts
    except (UnsafeURLError, FeedParseError) as exc:
        out.status, out.error = "error", f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # cualquier fallo inesperado queda acotado a esta fuente
        log.exception("Fallo inesperado en %s", job.url)
        out.status, out.error = "error", f"{type(exc).__name__}: {exc}"
    out.error = out.error[:2000] if out.error else None
    out.duration_ms = int((time.monotonic() - t0) * 1000)
    return out


def _due_jobs(only_media: str | None, force: bool) -> list[SubsourceJob]:
    now = utcnow()
    jobs: list[SubsourceJob] = []
    with session_scope() as db:
        q = (select(Subsource).join(Media).options(selectinload(Subsource.media))
             .where(Subsource.enabled.is_(True), Media.enabled.is_(True),
                    Media.is_demo.is_(False))
             .order_by(Subsource.id))
        if only_media:
            q = q.where(Media.slug == only_media)
        for sub in db.scalars(q):
            if not force and sub.last_fetch_at and \
                    sub.last_fetch_at + timedelta(seconds=sub.min_interval_seconds) > now:
                continue
            run = CollectionRun(subsource_id=sub.id, media_id=sub.media_id, run_type="feed",
                                status="running")
            db.add(run)
            sub.last_fetch_at = now
            db.flush()
            jobs.append(SubsourceJob(sub.id, run.id, sub.media.slug, sub.url, sub.kind,
                                     sub.max_items, sub.etag, sub.last_modified_header,
                                     sub.section, adapter_for_media(sub.media)))
    return jobs


def _store_outcome(out: FetchOutcome) -> CollectionRun:
    job = out.job
    new = updated = skipped = 0
    adapter = job.adapter
    try:
        for start in range(0, len(out.items), BATCH_SIZE):
            with session_scope() as db:
                sub = db.get(Subsource, job.sub_id)
                for item in out.items[start:start + BATCH_SIZE]:
                    result = upsert_item(db, sub, item, job.run_id,
                                         own_domains=adapter.allowed_domains)
                    new += result == NEW
                    updated += result == UPDATED
                    skipped += result == SKIPPED
    except Exception as exc:  # error al guardar: se registra y se sigue con las demás
        log.exception("Error guardando %s", job.url)
        out.status, out.error = "error", f"Error al guardar: {type(exc).__name__}: {exc}"[:2000]

    with session_scope() as db:
        sub = db.get(Subsource, job.sub_id)
        sub.last_status, sub.last_http_status = out.status, out.http_status
        if out.status in ("ok", "not_modified"):
            sub.etag, sub.last_modified_header = out.etag, out.last_modified
            sub.last_success_at = utcnow()
            sub.consecutive_failures = 0
            if out.status == "ok":
                sub.last_items_found = len(out.items)
        else:
            sub.last_error_at = utcnow()
            sub.last_error = out.error
            sub.consecutive_failures += 1
        run = db.get(CollectionRun, job.run_id)
        run.status, run.error, run.http_status = out.status, out.error, out.http_status
        run.attempts = out.attempts
        run.items_found, run.items_new, run.items_updated = len(out.items), new, updated
        run.items_skipped = skipped
        run.finished_at = utcnow()
        run.duration_ms = out.duration_ms
        _ = run.subsource.media  # cargado para usarlo fuera de la sesión
    log.info("%s %s: %s, %d ítems, %d nuevos, %d cambios%s", job.media_slug, job.url, out.status,
             len(out.items), new, updated, f" — {out.error}" if out.error else "")
    return run


def _make_fetcher(settings) -> SafeFetcher:
    limiter = DomainRateLimiter(float(os.getenv("RADAR_REQUEST_DELAY", "2")))
    for adapter in ADAPTERS.values():
        host = urlsplit(adapter.base_url).hostname or ""
        limiter.set_interval(host, adapter.request_interval)
    return SafeFetcher(user_agent=settings.user_agent, timeout=settings.http_timeout,
                       max_bytes=_env_int("RADAR_MAX_RESPONSE_BYTES", 10 * 1024 * 1024),
                       retries=_env_int("RADAR_HTTP_RETRIES", 2), rate_limiter=limiter)


def _run_enrichment(fetcher: SafeFetcher, pool: ThreadPoolExecutor, only_media: str | None,
                    per_media: int) -> list[CollectionRun]:
    jobs: list[tuple[MediaAdapter, int, list[enrich.EnrichTarget]]] = []
    with session_scope() as db:
        sections = [s.strip() for s in get_setting(db, "priority_sections").split(",") if s.strip()]
        q = select(Media).where(Media.enabled.is_(True), Media.is_demo.is_(False))
        if only_media:
            q = q.where(Media.slug == only_media)
        for media in db.scalars(q):
            adapter = adapter_for_media(media)
            targets = enrich.select_targets(db, media, per_media, sections)
            if not targets:
                continue
            run = CollectionRun(media_id=media.id, run_type="pagina", status="running",
                                items_found=len(targets))
            db.add(run)
            db.flush()
            jobs.append((adapter, run.id, targets))

    runs = []
    for adapter, run_id, targets in jobs:
        t0 = time.monotonic()
        counts = {"ok": 0, "partial": 0, "failed": 0, "blocked": 0, "skipped": 0}
        errors: list[str] = []
        futures = [pool.submit(enrich.fetch_page, fetcher, adapter, t) for t in targets]
        for fut in as_completed(futures):
            result = fut.result()
            try:
                with session_scope() as db:
                    status = enrich.apply_result(db, adapter, result, run_id)
            except Exception as exc:
                log.exception("Error guardando metadatos de #%s", result.article_id)
                status = "failed"
                errors.append(f"{type(exc).__name__}: {exc}")
            counts[status] = counts.get(status, 0) + 1
            if result.error and result.status != "ok":
                errors.append(result.error)
        with session_scope() as db:
            run = db.get(CollectionRun, run_id)
            run.items_updated = counts["ok"] + counts["partial"]
            run.items_skipped = counts["failed"] + counts["blocked"]
            run.status = ("blocked" if counts["blocked"] and not run.items_updated
                          else "error" if run.items_skipped and not run.items_updated else "ok")
            run.error = "; ".join(dict.fromkeys(errors))[:2000] or None
            run.finished_at = utcnow()
            run.duration_ms = int((time.monotonic() - t0) * 1000)
            _ = run.media
            runs.append(run)
        log.info("%s páginas: %s", adapter.slug, counts)
    return runs


def run_collection(only_media: str | None = None, force: bool = False, enrich_pages: bool = True,
                   fetcher: SafeFetcher | None = None, lock_path: Path | None = None) -> CollectionSummary:
    settings = get_settings()
    if only_media:
        check_media_slug(only_media)
    workers = max(1, _env_int("RADAR_MAX_CONCURRENCY", 2))
    summary = CollectionSummary()
    with job_lock(lock_path):
        jobs = _due_jobs(only_media, force)
        own_fetcher = fetcher is None
        fetcher = fetcher or _make_fetcher(settings)
        try:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="radar") as pool:
                futures = [pool.submit(fetch_subsource, fetcher, j.adapter, j)
                           for j in jobs]
                for fut in as_completed(futures):
                    summary.runs.append(_store_outcome(fut.result()))
                if enrich_pages:
                    per_media = runtime.get("enrich_per_media")
                    if per_media:
                        summary.enrich_runs = _run_enrichment(fetcher, pool, only_media, per_media)
        finally:
            if own_fetcher:
                fetcher.close()
    if not jobs:
        log.info("No hay subfuentes pendientes (intervalo mínimo no cumplido o todas pausadas).")
    return summary
