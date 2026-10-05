"""Alta/actualización idempotente de artículos con historial de titulares y procedencia."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from radar.collector.html import Link
from radar.collector.parsers import FeedItem
from radar.collector.references import Reference, find_link_references, find_text_mentions
from radar.models import Article, ArticleSighting, HeadlineVersion, MediaReference, Subsource
from radar.text import (
    MAX_AUTHOR,
    MAX_BODY,
    MAX_SUBTITLE,
    MAX_TITLE,
    canonicalize_url,
    clean_ws,
    content_hash,
    normalize_for_search,
    title_similarity,
    truncate,
)
from radar.timeutil import PRECISION_NONE, parse_feed_date, utcnow

log = logging.getLogger("radar.collector")

NEW = "new"
UPDATED = "updated"
SEEN = "seen"
SKIPPED = "skipped"


def build_search_text(title: str, subtitle: str | None, keywords: str | None,
                      author: str | None = None) -> str:
    return normalize_for_search(" ".join(p for p in (title, subtitle, keywords, author) if p))


# Por debajo de esta similitud, el mismo identificador con otra URL no se considera la
# misma nota (protege contra identificadores que no son únicos).
SAME_ID_MIN_SIMILARITY = 0.3


def _find_existing(db: Session, media_id: int, source_id: str | None, canonical: str,
                   title: str) -> tuple[Article | None, bool]:
    """Devuelve (artículo, identificador_confiable)."""
    by_url = db.scalar(select(Article).where(Article.media_id == media_id,
                                             Article.canonical_url == canonical))
    if by_url is not None:
        return by_url, True
    if source_id:
        found = db.scalar(select(Article).where(Article.media_id == media_id,
                                                Article.source_id == source_id))
        if found is not None:
            if title_similarity(found.title, title) >= SAME_ID_MIN_SIMILARITY:
                return found, True
            log.warning("Mismo id %s con URL y titular distintos: se tratan como notas distintas",
                        source_id)
            return None, False
    return None, True


def _canonical_taken(db: Session, article: Article, canonical: str) -> bool:
    return db.scalar(select(Article.id).where(
        Article.media_id == article.media_id, Article.canonical_url == canonical,
        Article.id != article.id)) is not None


def _record_sighting(db: Session, article: Article, subsource: Subsource, item: FeedItem,
                     now) -> None:
    if article.id is None or subsource.id is None:
        return
    sighting = db.scalar(select(ArticleSighting).where(
        ArticleSighting.article_id == article.id, ArticleSighting.subsource_id == subsource.id))
    if sighting is None:
        db.add(ArticleSighting(article_id=article.id, subsource_id=subsource.id,
                               item_url=item.url[:2048], source_id=item.source_id,
                               first_seen_at=now, last_seen_at=now, times_seen=1))
    else:
        sighting.last_seen_at = now
        sighting.times_seen += 1
        sighting.item_url = item.url[:2048]


def save_references(db: Session, article: Article, refs: list[Reference]) -> int:
    added = 0
    for ref in refs:
        evidence = ref.evidence[:2000]
        exists = db.scalar(select(MediaReference.id).where(
            MediaReference.article_id == article.id, MediaReference.referenced_media == ref.media_name,
            MediaReference.kind == ref.kind, MediaReference.evidence == evidence))
        if exists is None:
            db.add(MediaReference(article_id=article.id, referenced_media=ref.media_name,
                                  kind=ref.kind, evidence=evidence))
            added += 1
    return added


def _item_references(item: FeedItem, title: str, subtitle: str | None,
                     own_domains) -> list[Reference]:
    refs = find_text_mentions(title, own_domains) + find_text_mentions(subtitle, own_domains)
    links = [Link(u, "", True) for u in item.body_links]
    return refs + find_link_references(links, own_domains)


def upsert_item(
    db: Session,
    subsource: Subsource,
    item: FeedItem,
    run_id: int | None,
    discovery_method: str | None = None,
    is_demo: bool = False,
    own_domains: tuple[str, ...] | list[str] = (),
) -> str:
    if not item.url.strip().lower().startswith(("http://", "https://")):
        return SKIPPED
    now = utcnow()
    canonical = canonicalize_url(item.url)
    title = truncate(clean_ws(item.title), MAX_TITLE)
    if not title:
        return SKIPPED
    subtitle = truncate(clean_ws(item.subtitle), MAX_SUBTITLE) or None
    author = truncate(clean_ws(item.author), MAX_AUTHOR) or None
    body = truncate(item.body, MAX_BODY) if item.body else None
    keywords = ", ".join(item.keywords)[:2000] or None
    pub_at, pub_date, pub_prec = parse_feed_date(item.published_raw)
    mod_at, mod_date, mod_prec = parse_feed_date(item.modified_raw)
    source_id = item.source_id[:128] if item.source_id else None
    own_domains = own_domains or (subsource.media.domain_list() if subsource.media else ())

    article, id_ok = _find_existing(db, subsource.media_id, source_id, canonical, title)
    if not id_ok:
        source_id = None

    if article is None:
        digest = content_hash(title, subtitle)
        article = Article(
            media_id=subsource.media_id,
            subsource_id=subsource.id,
            url=item.url.strip()[:2048],
            canonical_url=canonical[:2048],
            source_id=source_id,
            origin_label=item.origin_label,
            is_syndicated=item.is_syndicated,
            title=title,
            subtitle=subtitle,
            author=author,
            section=item.section,
            keywords=keywords,
            body_text=body,
            search_text=build_search_text(title, subtitle, keywords, author),
            published_at=pub_at,
            published_date=pub_date,
            published_precision=pub_prec,
            modified_at=mod_at,
            modified_date=mod_date,
            modified_precision=mod_prec,
            first_seen_at=now,
            last_seen_at=now,
            discovery_method=discovery_method or subsource.kind,
            content_hash=digest,
            extraction_status="feed_only",
            is_demo=is_demo,
        )
        db.add(article)
        db.flush()
        db.add(HeadlineVersion(article_id=article.id, title=title, subtitle=subtitle,
                               content_hash=digest, detected_at=now, collection_run_id=run_id,
                               origin="feed"))
        _record_sighting(db, article, subsource, item, now)
        save_references(db, article, _item_references(item, title, subtitle, own_domains))
        return NEW

    article.last_seen_at = now
    _record_sighting(db, article, subsource, item, now)

    # Identificador y URL: el mismo id con otra URL es la misma nota con slug nuevo.
    if source_id and not article.source_id:
        taken = db.scalar(select(Article.id).where(
            Article.media_id == article.media_id, Article.source_id == source_id))
        if taken is None:
            article.source_id = source_id
    if canonical != article.canonical_url and source_id and article.source_id == source_id \
            and not _canonical_taken(db, article, canonical):
        article.url, article.canonical_url = item.url.strip()[:2048], canonical[:2048]

    # Una fuente sin bajada/autor/cuerpo (p. ej. sitemap) no borra lo que aportó otra.
    new_subtitle = subtitle if subtitle is not None else article.subtitle
    if author and not article.author:
        article.author = author
    if keywords and not article.keywords:
        article.keywords = keywords
    if body:
        article.body_text = body
    if item.section and not article.section:
        article.section = item.section
    if item.origin_label and not article.origin_label:
        article.origin_label = item.origin_label
    if item.is_syndicated:
        article.is_syndicated = True
    if article.published_precision == PRECISION_NONE and pub_prec != PRECISION_NONE:
        article.published_at, article.published_date, article.published_precision = (
            pub_at, pub_date, pub_prec,
        )
    if mod_prec != PRECISION_NONE:
        article.modified_at, article.modified_date, article.modified_precision = (
            mod_at, mod_date, mod_prec,
        )
    # Idempotente: solo inserta referencias que todavía no estén registradas.
    save_references(db, article, _item_references(item, title, new_subtitle, own_domains))

    digest = content_hash(title, new_subtitle)
    if digest == article.content_hash:
        return SEEN

    article.title = title
    article.subtitle = new_subtitle
    article.content_hash = digest
    article.search_text = build_search_text(title, new_subtitle, article.keywords, article.author)
    db.add(HeadlineVersion(article_id=article.id, title=title, subtitle=new_subtitle,
                           content_hash=digest, detected_at=now, collection_run_id=run_id,
                           origin="feed"))
    return UPDATED
