"""Lectura acotada de páginas de notas para completar metadatos (sin cuerpo, sin imágenes).

Solo para notas recientes de secciones prioritarias, de temas activos o sin bajada,
con un máximo por medio y por ejecución. Valida la URL final y la canónica.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from radar.collector.adapters import MediaAdapter
from radar.collector.html import PageMeta, parse_article_page
from radar.collector.ingest import build_search_text, save_references
from radar.collector.references import find_link_references, find_text_mentions
from radar.models import Article, HeadlineVersion, Media, Topic
from radar.net import BlockedError, FetchError, SafeFetcher, UnsafeURLError
from radar.queries import topic_condition
from radar.text import MAX_AUTHOR, MAX_SUBTITLE, canonicalize_url, content_hash, truncate
from radar.timeutil import PRECISION_DATETIME, parse_feed_date, utcnow

MAX_ATTEMPTS = 3
RECENT_HOURS = 48


@dataclass
class EnrichTarget:
    article_id: int
    url: str


@dataclass
class EnrichResult:
    article_id: int
    status: str  # ok | failed | blocked
    meta: PageMeta | None = None
    final_url: str | None = None
    redirects: tuple[str, ...] = ()
    error: str | None = None
    http_status: int | None = None
    attempts: int = 1


def select_targets(db: Session, media: Media, limit: int,
                   priority_sections: list[str]) -> list[EnrichTarget]:
    since = utcnow() - timedelta(hours=RECENT_HOURS)
    wanted = [Article.subtitle.is_(None)]
    if priority_sections:
        wanted.append(Article.section.in_(priority_sections))
    for topic in db.scalars(select(Topic).where(Topic.enabled.is_(True))):
        cond = topic_condition(topic)
        if cond is not None:
            wanted.append(cond)
    rows = db.execute(
        select(Article.id, Article.url)
        .where(Article.media_id == media.id, Article.is_demo.is_(False),
               Article.extraction_status == "feed_only",
               Article.enrich_attempts < MAX_ATTEMPTS,
               Article.first_seen_at >= since, or_(*wanted))
        .order_by(Article.first_seen_at.desc())
        .limit(limit)
    ).all()
    return [EnrichTarget(r.id, r.url) for r in rows]


def fetch_page(fetcher: SafeFetcher, adapter: MediaAdapter, target: EnrichTarget) -> EnrichResult:
    """Se ejecuta en un hilo: no toca la base de datos."""
    try:
        resp = fetcher.get(target.url, adapter.allowed_domains,
                           headers={"Accept": "text/html,application/xhtml+xml"})
    except BlockedError as exc:
        return EnrichResult(target.article_id, "blocked", error=str(exc), http_status=exc.status_code)
    except (FetchError, UnsafeURLError) as exc:
        return EnrichResult(target.article_id, "failed", error=str(exc)[:500],
                            http_status=getattr(exc, "status_code", None),
                            attempts=getattr(exc, "attempts", 1))
    if not adapter.is_article_url(resp.url):
        return EnrichResult(target.article_id, "failed", final_url=resp.url,
                            error=f"La redirección terminó fuera de una nota: {resp.url}"[:500],
                            http_status=resp.status_code, attempts=resp.attempts)
    ctype = resp.headers.get("content-type", "")
    if "html" not in ctype.lower():
        return EnrichResult(target.article_id, "failed", error=f"Tipo de contenido inesperado: {ctype}",
                            http_status=resp.status_code, attempts=resp.attempts)
    meta = parse_article_page(resp.content, resp.url, ctype)
    return EnrichResult(target.article_id, "ok", meta=meta, final_url=resp.url,
                        redirects=tuple(resp.redirects), http_status=resp.status_code,
                        attempts=resp.attempts)


def apply_result(db: Session, adapter: MediaAdapter, result: EnrichResult,
                 run_id: int | None) -> str:
    article = db.get(Article, result.article_id)
    if article is None:
        return "skipped"
    now = utcnow()
    article.enrich_attempts += 1
    article.enriched_at = now
    if result.status == "blocked":
        article.extraction_status = "blocked"
        article.enrich_error = result.error
        return "blocked"
    if result.status != "ok" or result.meta is None:
        article.enrich_error = result.error
        if article.enrich_attempts >= MAX_ATTEMPTS:
            article.extraction_status = "failed"
        return "failed"

    meta = result.meta
    notes: list[str] = []
    article.final_url = (result.final_url or "")[:2048] or None
    if result.redirects:
        notes.append(f"Redirigida {len(result.redirects)} vez/veces")

    # URL canónica declarada por la página: solo si es una nota del mismo medio.
    if meta.canonical:
        if adapter.is_article_url(meta.canonical):
            canonical = canonicalize_url(meta.canonical)
            if canonical != article.canonical_url:
                other = db.scalar(select(Article.id).where(
                    Article.media_id == article.media_id, Article.canonical_url == canonical,
                    Article.id != article.id))
                if other is None:
                    article.canonical_url = canonical[:2048]
                    notes.append("Canónica actualizada desde la página")
                else:
                    notes.append(f"La canónica coincide con la noticia #{other} (posible duplicado)")
        else:
            notes.append("Canónica de la página ignorada: no es una nota del medio")

    filled = False
    if meta.description and not article.subtitle:
        article.subtitle = truncate(meta.description, MAX_SUBTITLE)
        article.content_hash = content_hash(article.title, article.subtitle)
        article.search_text = build_search_text(article.title, article.subtitle, article.keywords, article.author)
        db.add(HeadlineVersion(article_id=article.id, title=article.title,
                               subtitle=article.subtitle, content_hash=article.content_hash,
                               detected_at=now, collection_run_id=run_id, origin="pagina"))
        filled = True
    if meta.authors and not article.author:
        article.author = truncate(", ".join(meta.authors), MAX_AUTHOR)
    if article.author and adapter._agency_author(article.author.split(",")[0]):
        article.is_syndicated = True
    if meta.keywords and not article.keywords:
        article.keywords = ", ".join(meta.keywords)[:2000]
    article.search_text = build_search_text(article.title, article.subtitle, article.keywords, article.author)
    if meta.section and not article.section:
        article.section = meta.section[:256]

    pub_at, pub_date, pub_prec = parse_feed_date(meta.published_raw)
    if pub_prec == PRECISION_DATETIME and article.published_precision != PRECISION_DATETIME:
        article.published_at, article.published_date, article.published_precision = (
            pub_at, pub_date, pub_prec)
    mod_at, mod_date, mod_prec = parse_feed_date(meta.modified_raw)
    if mod_prec == PRECISION_DATETIME:
        article.modified_at, article.modified_date, article.modified_precision = (
            mod_at, mod_date, mod_prec)

    refs = find_link_references(meta.links, adapter.allowed_domains)
    refs += find_text_mentions(meta.description, adapter.allowed_domains)
    save_references(db, article, refs)

    useful = filled or bool(meta.description or meta.authors or meta.published_raw)
    article.extraction_status = "ok" if useful else "partial"
    if meta.has_paywall_marker:
        notes.append("La página declara contenido no gratuito: solo se usan metadatos")
    article.enrich_error = "; ".join(notes) or None
    return article.extraction_status
