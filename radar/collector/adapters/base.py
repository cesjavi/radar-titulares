"""Base de los adaptadores por medio."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from radar.collector.html import extract_section_links
from radar.collector.parsers import FeedItem, parse_feed
from radar.text import strip_html

# Firmas de agencias: el contenido firmado así es una republicación, no producción propia.
AGENCY_AUTHORS = {
    "efe": "EFE", "agencia efe": "EFE", "afp": "AFP", "europa press": "Europa Press",
    "reuters": "Reuters", "ap": "AP", "associated press": "AP", "bloomberg": "Bloomberg",
    "noticias argentinas": "Noticias Argentinas", "na": "Noticias Argentinas",
    "ansa": "ANSA", "dpa": "DPA", "sputnik": "Sputnik", "xinhua": "Xinhua",
}


@dataclass(frozen=True)
class Endpoint:
    name: str
    kind: str  # rss | sitemap_news | html_section
    url: str
    section: str = "general"
    enabled: bool = True
    min_interval_seconds: int = 570  # ~10 min con el timer cada 10 min
    max_items: int = 300
    note: str = ""


class MediaAdapter:
    slug: str
    name: str
    base_url: str
    allowed_domains: tuple[str, ...]
    endpoints: tuple[Endpoint, ...] = ()
    # Separación mínima entre solicitudes al mismo host (segundos).
    request_interval: float = 2.0
    article_path_re: re.Pattern[str]

    # --- reglas de URL ---------------------------------------------------------------

    def host_ok(self, url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        return any(host == d or host.endswith("." + d) for d in self.allowed_domains)

    def is_article_url(self, url: str) -> bool:
        parts = urlsplit(url)
        return (parts.scheme in ("http", "https") and self.host_ok(url)
                and bool(self.article_path_re.match(parts.path)))

    def section_from_url(self, url: str) -> str | None:
        segments = [s for s in urlsplit(url).path.split("/") if s]
        return segments[0].lower() if segments else None

    def source_id(self, item: FeedItem) -> str | None:
        return None

    def classify(self, item: FeedItem) -> tuple[str | None, bool]:
        """(subfuente editorial, es republicación) según evidencia en los metadatos."""
        return None, self._agency_author(item.author) is not None

    @staticmethod
    def _agency_author(author: str | None) -> str | None:
        if not author:
            return None
        return AGENCY_AUTHORS.get(author.strip().lower().rstrip("."))

    # --- parseo -------------------------------------------------------------------------

    def parse(self, content: bytes, endpoint_kind: str, page_url: str,
              content_type: str | None = None, max_items: int = 300,
              default_section: str | None = None) -> list[FeedItem]:
        """`default_section`: sección del endpoint, para medios cuya URL no incluye la sección."""
        if endpoint_kind == "html_section":
            links = extract_section_links(content, page_url, self.is_article_url, content_type)
            items = [FeedItem(url=u, title=t) for u, t in links[:max_items]]
        else:
            items = parse_feed(content, endpoint_kind, max_items=max_items * 3)
        out = []
        for item in items:
            if not self.is_article_url(item.url):
                continue  # enlaces a otros dominios, videos, galerías externas...
            out.append(self.normalize(item, default_section))
            if len(out) >= max_items:
                break
        return out

    @staticmethod
    def clean_author(raw: str | None) -> str | None:
        """Firma sin HTML ni el prefijo "Por " ("Por <b>Ana Pérez</b>" → "Ana Pérez")."""
        text = strip_html(raw) if raw else None
        if not text:
            return None
        text = re.sub(r"^\s*por(?:\s+|$)", "", text, flags=re.I).strip(" ,;:-")
        return text or None

    def normalize(self, item: FeedItem, default_section: str | None = None) -> FeedItem:
        item.section = self.section_from_url(item.url) or default_section or item.section
        item.author = self.clean_author(item.author)
        item.source_id = self.source_id(item)
        item.origin_label, item.is_syndicated = self.classify(item)
        return item
