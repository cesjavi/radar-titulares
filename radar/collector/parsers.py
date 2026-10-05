"""Parsers de RSS 2.0, Atom y sitemaps de noticias (Google News). XML seguro con defusedxml."""

from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field
from xml.etree.ElementTree import Element

from defusedxml import ElementTree as SafeET

from radar.text import MAX_BODY, clean_ws, strip_html, truncate

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
    "news": "http://www.google.com/schemas/sitemap-news/0.9",
}

_BLOCK_MARKERS = (b"captcha", b"cf-chl", b"attention required", b"access denied",
                  b"are you a robot", b"verifica que eres", b"unusual traffic")


class FeedParseError(ValueError):
    pass


class FeedBlockedError(FeedParseError):
    """La respuesta parece una página de bloqueo o desafío (CAPTCHA). No se evade."""


@dataclass
class FeedItem:
    url: str
    title: str
    subtitle: str | None = None
    author: str | None = None
    published_raw: str | None = None
    modified_raw: str | None = None
    section: str | None = None
    keywords: list[str] = field(default_factory=list)
    guid: str | None = None
    body: str | None = None  # texto plano, si el feed publica el cuerpo
    body_links: list[str] = field(default_factory=list)
    # Completados por el adaptador del medio:
    source_id: str | None = None
    origin_label: str | None = None
    is_syndicated: bool = False


def _text(el: Element | None) -> str | None:
    if el is None or el.text is None:
        return None
    value = clean_ws(el.text)
    return value or None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def looks_blocked(content: bytes) -> bool:
    head = content[:20000].lower()
    return b"<html" in head and any(m in head for m in _BLOCK_MARKERS)


def parse_feed(content: bytes, kind: str, max_items: int = 1000) -> list[FeedItem]:
    stripped = content.lstrip()
    if stripped[:15].lower().startswith((b"<!doctype html", b"<html")):
        if looks_blocked(stripped):
            raise FeedBlockedError("La fuente devolvió una página de bloqueo/desafío en lugar del feed")
        raise FeedParseError("Se esperaba XML y llegó HTML (¿cambió la URL del feed?)")
    try:
        root = SafeET.fromstring(content)
    except Exception as exc:  # defusedxml lanza varios tipos
        raise FeedParseError(f"XML inválido: {exc}") from exc
    if kind == "sitemap_news":
        items = _parse_sitemap_news(root)
    elif kind == "rss":
        items = _parse_atom(root) if _local(root.tag) == "feed" else _parse_rss(root)
    else:
        raise FeedParseError(f"Tipo de fuente no soportado: {kind}")
    return items[:max_items]


def _body_from_html(raw: str | None) -> tuple[str | None, list[str]]:
    if not raw:
        return None, []
    from radar.collector.html import _collect

    links = [link.url for link in _collect(raw, "https://invalid.invalid/").links
             if link.url.startswith(("http://", "https://")) and "invalid.invalid" not in link.url]
    return truncate(strip_html_keep_anchors(raw), MAX_BODY), links[:50]


def strip_html_keep_anchors(raw: str) -> str | None:
    """Texto del cuerpo, incluido el texto de los enlaces (strip_html los descarta)."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return clean_ws(_html.unescape(text)) or None


def _parse_rss(root: Element) -> list[FeedItem]:
    channel = root.find("channel")
    if channel is None:
        raise FeedParseError("RSS sin <channel>")
    items: list[FeedItem] = []
    for it in channel.findall("item"):
        link = _text(it.find("link"))
        guid_el = it.find("guid")
        guid = _text(guid_el)
        if not link and guid_el is not None and guid_el.get("isPermaLink", "true") == "true":
            link = guid
        title = strip_html(_text(it.find("title")))
        if not link or not title:
            continue
        author = _text(it.find("dc:creator", NS)) or _text(it.find("author"))
        categories = [c for c in (_text(c) for c in it.findall("category")) if c]
        body, links = _body_from_html(it.findtext("content:encoded", namespaces=NS))
        items.append(
            FeedItem(
                url=link,
                title=title,
                subtitle=strip_html(it.findtext("description")),
                author=author,
                published_raw=_text(it.find("pubDate")) or _text(it.find("dc:date", NS)),
                section=categories[0] if categories else None,
                keywords=categories,
                guid=guid,
                body=body,
                body_links=links,
            )
        )
    return items


def _parse_atom(root: Element) -> list[FeedItem]:
    items: list[FeedItem] = []
    for entry in root.findall("atom:entry", NS):
        link = None
        for ln in entry.findall("atom:link", NS):
            if ln.get("rel", "alternate") == "alternate" and ln.get("href"):
                link = ln.get("href")
                break
        title = strip_html(_text(entry.find("atom:title", NS)))
        if not link or not title:
            continue
        items.append(
            FeedItem(
                url=link,
                title=title,
                subtitle=strip_html(entry.findtext("atom:summary", namespaces=NS)),
                author=_text(entry.find("atom:author/atom:name", NS)),
                published_raw=_text(entry.find("atom:published", NS)),
                modified_raw=_text(entry.find("atom:updated", NS)),
                guid=_text(entry.find("atom:id", NS)),
            )
        )
    return items


def _parse_sitemap_news(root: Element) -> list[FeedItem]:
    if _local(root.tag) != "urlset":
        raise FeedParseError("Sitemap sin <urlset>")
    items: list[FeedItem] = []
    for url_el in root.findall("sm:url", NS):
        loc = _text(url_el.find("sm:loc", NS))
        news = url_el.find("news:news", NS)
        if not loc or news is None:
            continue
        title = strip_html(_text(news.find("news:title", NS)))
        if not title:
            continue
        kw_raw = _text(news.find("news:keywords", NS)) or ""
        keywords = [k.strip() for k in kw_raw.split(",") if k.strip()]
        items.append(
            FeedItem(
                url=loc,
                title=title,
                published_raw=_text(news.find("news:publication_date", NS)),
                modified_raw=_text(url_el.find("sm:lastmod", NS)),
                keywords=keywords,
            )
        )
    return items
