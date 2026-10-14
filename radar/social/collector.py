"""Recolección de canales públicos de Telegram.

Corre dentro del mismo bloqueo de ejecución exclusiva que la recolección de medios (o con el
suyo, vía `collect_social_phase`). Es secuencial: son pocos canales y `t.me` es un solo host. Si
Telegram limita o niega el acceso (429, 403, robots.txt) se corta el ciclo sin insistir.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select

from radar.db import session_scope
from radar.models import SocialAccount, SocialPost
from radar.net import BlockedError, FetchError, SafeFetcher, UnsafeURLError
from radar.social import telegram as tg
from radar.text import content_hash
from radar.timeutil import utcnow

log = logging.getLogger("radar.social")

FIRST_RUN_PAGES = 3  # la primera vez se trae algo de historia (20 mensajes por página)
BACKFILL_DAYS = 7
FAILURES_FOR_DOWN = 3


@dataclass
class AccountOutcome:
    handle: str
    status: str  # ok | empty | error | blocked
    new: int = 0
    updated: int = 0
    error: str | None = None


@dataclass
class SocialSummary:
    outcomes: list[AccountOutcome] = field(default_factory=list)
    skipped: int = 0  # cuentas cuyo intervalo mínimo no se cumplió

    @property
    def new_posts(self) -> int:
        return sum(o.new for o in self.outcomes)

    @property
    def errors(self) -> list[AccountOutcome]:
        return [o for o in self.outcomes if o.status in ("error", "blocked")]


def fetch_channel(fetcher: SafeFetcher, handle: str, pages: int = 1) -> tg.ParsedChannel:
    """Descarga hasta `pages` páginas, de la más nueva hacia atrás. Lanza ChannelError, FetchError,
    BlockedError o UnsafeURLError."""
    merged: tg.ParsedChannel | None = None
    before: int | None = None
    horizon = utcnow() - timedelta(days=BACKFILL_DAYS)
    for _ in range(max(1, pages)):
        resp = fetcher.get(tg.channel_url(handle, before), tg.DOMAINS)
        page = tg.parse_channel(resp.content, handle, resp.headers.get("content-type"))
        if merged is None:
            if page.title is None and not page.posts:
                raise tg.ChannelError("El canal no existe o no tiene vista web pública "
                                      "(t.me/s/<canal> no devolvió mensajes ni datos del canal).")
            merged = page
        else:
            merged.posts = page.posts + merged.posts
            merged.older_than = page.older_than
        if not page.posts or page.older_than is None or min(
                p.posted_at for p in page.posts) < horizon:
            break
        before = page.older_than
    assert merged is not None
    return merged


def _store(account_id: int, channel: tg.ParsedChannel) -> AccountOutcome:
    now = utcnow()
    out = AccountOutcome(channel.handle, "ok" if channel.posts else "empty")
    with session_scope() as db:
        account = db.get(SocialAccount, account_id)
        if channel.subscribers is not None:
            account.subscribers = channel.subscribers
        ids = [p.external_id for p in channel.posts]
        existing = {}
        for start in range(0, len(ids), 400):
            existing.update({
                p.external_id: p for p in db.scalars(select(SocialPost).where(
                    SocialPost.account_id == account_id,
                    SocialPost.external_id.in_(ids[start:start + 400])))})
        for item in channel.posts:
            digest = content_hash(item.analysis_text, item.text)
            post = existing.get(item.external_id)
            if post is None:
                db.add(SocialPost(
                    account_id=account_id, external_id=item.external_id, url=item.url[:512],
                    text=item.text, preview_title=item.preview_title,
                    preview_text=item.preview_text, links=tg.links_json(item),
                    search_text=tg.post_search_text(item), posted_at=item.posted_at,
                    is_repost=item.is_repost, forwarded_from=item.forwarded_from,
                    views=item.views, content_hash=digest,
                    edited_at=now if item.edited else None, first_seen_at=now, last_seen_at=now))
                out.new += 1
                continue
            post.last_seen_at = now
            post.views = item.views if item.views is not None else post.views
            if digest != post.content_hash:  # texto editado: se conserva la última versión
                post.text, post.preview_title = item.text, item.preview_title
                post.preview_text, post.links = item.preview_text, tg.links_json(item)
                post.search_text, post.content_hash = tg.post_search_text(item), digest
                post.edited_at = now
                out.updated += 1
        account.last_items_found = len(channel.posts)
        account.last_status = out.status
        account.last_success_at = now
        account.consecutive_failures = 0
        account.last_error = None if out.status == "ok" else (
            "Sin mensajes con texto en la página (posible cambio de formato o canal solo con "
            "fotos y videos).")
    return out


def _record_failure(account_id: int, status: str, error: str) -> None:
    with session_scope() as db:
        account = db.get(SocialAccount, account_id)
        now = utcnow()
        account.last_status, account.last_error = status, error[:1000]
        account.last_error_at = now
        account.consecutive_failures = (account.consecutive_failures or 0) + 1


def _due(only_handle: str | None, force: bool) -> tuple[list[tuple[int, str, int]], int]:
    """((id, canal, páginas a pedir) de las cuentas habilitadas cuyo intervalo se cumplió,
    cantidad de cuentas salteadas por intervalo)."""
    now = utcnow()
    jobs: list[tuple[int, str, int]] = []
    skipped = 0
    with session_scope() as db:
        q = (select(SocialAccount).where(SocialAccount.enabled.is_(True),
                                         SocialAccount.platform == "telegram")
             .order_by(SocialAccount.id))
        if only_handle:
            q = q.where(SocialAccount.handle == only_handle)
        for acc in db.scalars(q):
            if not force and acc.last_fetch_at and \
                    acc.last_fetch_at + timedelta(seconds=acc.min_interval_seconds) > now:
                skipped += 1
                continue
            first_time = acc.last_success_at is None
            acc.last_fetch_at = now
            jobs.append((acc.id, acc.handle, FIRST_RUN_PAGES if first_time else 1))
    return jobs, skipped


def run_social_collection(fetcher: SafeFetcher, only_handle: str | None = None,
                          force: bool = False) -> SocialSummary:
    from radar import runtime

    summary = SocialSummary()
    if not runtime.get("social_enabled"):
        return summary
    jobs, summary.skipped = _due(only_handle, force)
    for account_id, handle, pages in jobs:
        t0 = time.monotonic()
        try:
            outcome = _store(account_id, fetch_channel(fetcher, handle, pages))
        except BlockedError as exc:
            _record_failure(account_id, "blocked", str(exc))
            summary.outcomes.append(AccountOutcome(handle, "blocked", error=str(exc)))
            log.warning("Telegram negó el acceso (%s); se corta el ciclo de redes.", exc)
            break  # no se insiste ni se prueba con otros canales en este ciclo
        except (tg.ChannelError, FetchError, UnsafeURLError) as exc:
            _record_failure(account_id, "error", str(exc))
            summary.outcomes.append(AccountOutcome(handle, "error", error=str(exc)))
            continue
        summary.outcomes.append(outcome)
        log.info("@%s: %s nuevos, %s editados (%d ms)", handle, outcome.new, outcome.updated,
                 int((time.monotonic() - t0) * 1000))
    return summary
