"""Verificación en vivo de las fuentes (`python -m radar probe` y la pantalla de configuración).

No guarda nada: descarga cada fuente con el descargador seguro y cuenta las notas que reconoce.
"""

from __future__ import annotations

from sqlalchemy import select

from radar.collector.adapters import ADAPTERS, Endpoint
from radar.collector.generic import adapter_for_media
from radar.collector.parsers import FeedParseError
from radar.config import get_settings
from radar.db import session_scope
from radar.models import Media
from radar.net import BlockedError, FetchError, SafeFetcher, UnsafeURLError


def _targets(media: str | None) -> list[tuple]:
    """(adaptador, endpoints) de los medios con adaptador propio y de los agregados en el panel."""
    out = []
    with session_scope() as db:
        q = select(Media).where(Media.is_demo.is_(False)).order_by(Media.id)
        if media:
            q = q.where(Media.slug == media)
        for m in db.scalars(q):
            adapter = adapter_for_media(m)
            if m.slug in ADAPTERS:
                eps = list(adapter.endpoints)
            else:
                eps = [Endpoint(s.name, s.kind, s.url, s.section, s.enabled, max_items=s.max_items)
                       for s in m.subsources]
            out.append((adapter, eps))
    if media and not out and media in ADAPTERS:  # base sin cargar: se verifican los adaptadores
        out.append((ADAPTERS[media], list(ADAPTERS[media].endpoints)))
    return out


def probe_media(media: str | None = None) -> list[dict]:
    """Una fila por fuente: medio, tipo, URL, estado (ok | vacia | falla) y detalle."""
    settings = get_settings()
    rows: list[dict] = []
    with SafeFetcher(settings.user_agent, timeout=settings.http_timeout, retries=0) as fetcher:
        for adapter, endpoints in _targets(media):
            host = adapter.base_url.split("/")[2]
            fetcher.rate.set_interval(host, adapter.request_interval)
            for ep in endpoints:
                row = {"medio": adapter.name, "tipo": ep.kind, "url": ep.url}
                try:
                    resp = fetcher.get(ep.url, adapter.allowed_domains)
                    items = adapter.parse(resp.content, ep.kind, resp.url,
                                          resp.headers.get("content-type"), ep.max_items)
                    row.update(estado="ok" if items else "vacia", http=resp.status_code,
                               bytes=len(resp.content), notas=len(items),
                               ejemplo=items[0].title[:70] if items else "-")
                except (BlockedError, FetchError, UnsafeURLError, FeedParseError) as exc:
                    row.update(estado="falla", error=f"{type(exc).__name__}: {exc}")
                rows.append(row)
    return rows
