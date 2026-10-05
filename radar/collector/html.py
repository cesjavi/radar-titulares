"""Extracción liviana de HTML (sin navegador): metadatos de notas y enlaces de secciones."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from radar.text import clean_ws

MAX_HTML_CHARS = 3_000_000
_CHARSET = re.compile(rb'charset=["\']?([A-Za-z0-9_-]+)', re.I)


def decode_html(content: bytes, content_type: str | None = None) -> str:
    charset = None
    if content_type and "charset=" in content_type.lower():
        charset = content_type.lower().split("charset=", 1)[1].split(";")[0].strip()
    if not charset:
        m = _CHARSET.search(content[:4096])
        charset = m.group(1).decode("ascii") if m else "utf-8"
    try:
        return content.decode(charset, errors="replace")[:MAX_HTML_CHARS]
    except LookupError:
        return content.decode("utf-8", errors="replace")[:MAX_HTML_CHARS]


@dataclass
class Link:
    url: str
    text: str
    in_article: bool
    heading: str = ""  # texto de un h1-h4 que contiene al enlace o que está dentro de él


@dataclass
class PageMeta:
    canonical: str | None = None
    title: str | None = None
    description: str | None = None
    published_raw: str | None = None
    modified_raw: str | None = None
    authors: list[str] = field(default_factory=list)
    section: str | None = None
    keywords: list[str] = field(default_factory=list)
    site_name: str | None = None
    links: list[Link] = field(default_factory=list)
    has_paywall_marker: bool = False


_HEADINGS = {"h1", "h2", "h3", "h4"}


class _Collector(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.meta: dict[str, str] = {}
        self.canonical: str | None = None
        self.ldjson: list[str] = []
        self.links: list[Link] = []
        self._in_ld = False
        self._ld_buf: list[str] = []
        self._article_depth = 0
        self._a_href: str | None = None
        self._a_text: list[str] = []
        self._a_in_article = False
        self._a_heading: list[str] = []
        self._a_heading_done = False
        self._a_in_heading_outer = False
        self._heading_depth = 0
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key and "content" in a and key not in self.meta:
                self.meta[key] = a["content"]
        elif tag == "link" and "canonical" in a.get("rel", "").lower().split():
            self.canonical = self.canonical or a.get("href")
        elif tag == "script":
            if a.get("type", "").lower() == "application/ld+json":
                self._in_ld = True
                self._ld_buf = []
            else:
                self._skip += 1
        elif tag in ("style", "noscript"):
            self._skip += 1
        elif tag == "article":
            self._article_depth += 1
        elif tag == "a" and a.get("href"):
            self._a_href = a["href"]
            self._a_text = []
            self._a_heading = []
            self._a_heading_done = False
            self._a_in_article = self._article_depth > 0
            self._a_in_heading_outer = self._heading_depth > 0
        elif tag in _HEADINGS:
            self._heading_depth += 1

    def handle_endtag(self, tag):
        if tag == "script":
            if self._in_ld:
                self._in_ld = False
                self.ldjson.append("".join(self._ld_buf))
            elif self._skip:
                self._skip -= 1
        elif tag in ("style", "noscript") and self._skip:
            self._skip -= 1
        elif tag == "article" and self._article_depth:
            self._article_depth -= 1
        elif tag in _HEADINGS and self._heading_depth:
            self._heading_depth -= 1
            if self._a_href is not None and self._a_heading and not self._heading_depth:
                self._a_heading_done = True  # solo el primer título (no la bajada en h3)
        elif tag == "a" and self._a_href is not None:
            href = self._a_href.strip()
            if not href.startswith(("#", "javascript:", "mailto:", "tel:")):
                text = clean_ws(" ".join(self._a_text))
                heading = text if self._a_in_heading_outer else clean_ws(" ".join(self._a_heading))
                self.links.append(Link(urljoin(self.base_url, href), text, self._a_in_article,
                                       heading))
            self._a_href = None

    def handle_data(self, data):
        if self._in_ld:
            self._ld_buf.append(data)
        elif self._a_href is not None and not self._skip:
            self._a_text.append(data)
            if self._heading_depth and not self._a_in_heading_outer and not self._a_heading_done:
                self._a_heading.append(data)


def _collect(html: str, base_url: str) -> _Collector:
    c = _Collector(base_url)
    try:
        c.feed(html)
        c.close()
    except Exception:  # HTML roto: se devuelve lo recolectado hasta ahí
        pass
    return c


def _iter_ld_objects(blobs: list[str]):
    for blob in blobs:
        try:
            data = json.loads(blob)
        except ValueError:
            continue
        stack = [data]
        while stack:
            obj = stack.pop()
            if isinstance(obj, list):
                stack.extend(obj)
            elif isinstance(obj, dict):
                yield obj
                if "@graph" in obj:
                    stack.append(obj["@graph"])


_ARTICLE_TYPES = {"newsarticle", "article", "reportagenewsarticle", "analysisnewsarticle",
                  "opinionnewsarticle", "liveblogposting", "blogposting"}


def _names(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [value["name"]] if isinstance(value.get("name"), str) else []
    if isinstance(value, list):
        return [n for v in value for n in _names(v)]
    return []


def parse_article_page(content: bytes, url: str, content_type: str | None = None) -> PageMeta:
    html = decode_html(content, content_type)
    c = _collect(html, url)
    m = c.meta
    meta = PageMeta(
        canonical=urljoin(url, c.canonical) if c.canonical else None,
        title=clean_ws(m.get("og:title")) or None,
        description=clean_ws(m.get("og:description") or m.get("description")) or None,
        published_raw=m.get("article:published_time") or m.get("datepublished"),
        modified_raw=m.get("article:modified_time") or m.get("datemodified"),
        section=clean_ws(m.get("article:section")) or None,
        site_name=clean_ws(m.get("og:site_name")) or None,
        links=c.links,
    )
    if m.get("author") and not m["author"].startswith("http"):
        meta.authors.append(clean_ws(m["author"]))
    kw = m.get("news_keywords") or m.get("keywords") or ""
    meta.keywords = [k.strip() for k in kw.split(",") if k.strip()]

    for obj in _iter_ld_objects(c.ldjson):
        types = obj.get("@type")
        types = {t.lower() for t in (types if isinstance(types, list) else [types]) if isinstance(t, str)}
        if not types & _ARTICLE_TYPES:
            continue
        meta.published_raw = meta.published_raw or obj.get("datePublished")
        meta.modified_raw = meta.modified_raw or obj.get("dateModified")
        meta.description = meta.description or (clean_ws(obj.get("description")) or None
                                                if isinstance(obj.get("description"), str) else None)
        for name in _names(obj.get("author")):
            name = clean_ws(name)
            if name and name not in meta.authors:
                meta.authors.append(name)
        if not meta.keywords and isinstance(obj.get("keywords"), str):
            meta.keywords = [k.strip() for k in obj["keywords"].split(",") if k.strip()]
        if obj.get("isAccessibleForFree") in (False, "False", "false"):
            meta.has_paywall_marker = True
    return meta


def extract_section_links(content: bytes, page_url: str, accept: callable,
                          content_type: str | None = None) -> list[tuple[str, str]]:
    """Enlaces a notas de una portada de sección: [(url absoluta sin fragmento, texto)].

    `accept(url)` decide qué es una nota. Si la portada marca los titulares con h1-h4,
    solo se usan esos enlaces (descarta menús, volantas y horarios); si no, el texto más
    largo de cada enlace.
    """
    c = _collect(decode_html(content, content_type), page_url)
    links = [(link.url.split("#", 1)[0], link) for link in c.links]
    links = [(u, link) for u, link in links if accept(u)]
    use_headings = any(link.heading for _, link in links)
    best: dict[str, str] = {}
    order: list[str] = []
    for url, link in links:
        text = link.heading if use_headings else link.text
        if not text:
            continue
        if url not in best:
            order.append(url)
            best[url] = text
        elif len(text) > len(best[url]):
            best[url] = text
    return [(u, best[u]) for u in order if len(best[u]) >= 15]


def same_site(url: str, domains: list[str]) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)
