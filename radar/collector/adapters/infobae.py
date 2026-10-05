"""Infobae (infobae.com).

Fuentes verificadas el 2026-10-04 (ver README):
- RSS general: /arc/outboundfeeds/rss/ (incluye el cuerpo en content:encoded).
- RSS por categoría: /arc/outboundfeeds/rss/category/politica/ y /economia/ (~1 MB cada uno).
- Sitemaps de noticias: /arc/outboundfeeds/news-sitemap/ y /category/politica|economia/.
- Secciones HTML: /politica/, /economia/ (respaldo, desactivadas).
robots.txt solo excluye /buscador.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from radar.collector.adapters.base import Endpoint, MediaAdapter
from radar.collector.parsers import FeedItem

_FEEDS = "https://www.infobae.com/arc/outboundfeeds"


class InfobaeAdapter(MediaAdapter):
    slug = "infobae"
    name = "Infobae"
    base_url = "https://www.infobae.com"
    allowed_domains = ("infobae.com",)
    article_path_re = re.compile(r"^/[a-z0-9-]+(?:/[a-z0-9-]+)*/\d{4}/\d{2}/\d{2}/[^/]+/?$")
    endpoints = (
        Endpoint("RSS general", "rss", f"{_FEEDS}/rss/"),
        Endpoint("Sitemap de noticias", "sitemap_news", f"{_FEEDS}/news-sitemap/"),
        Endpoint("Sitemap Política", "sitemap_news", f"{_FEEDS}/news-sitemap/category/politica/",
                 section="politica"),
        Endpoint("Sitemap Economía", "sitemap_news", f"{_FEEDS}/news-sitemap/category/economia/",
                 section="economia"),
        # Feeds pesados (~1 MB) con el cuerpo: cada 20 minutos.
        Endpoint("RSS Política (con cuerpo)", "rss", f"{_FEEDS}/rss/category/politica/",
                 section="politica", min_interval_seconds=1170, max_items=100),
        Endpoint("RSS Economía (con cuerpo)", "rss", f"{_FEEDS}/rss/category/economia/",
                 section="economia", min_interval_seconds=1170, max_items=100),
        Endpoint("Sección Política (HTML)", "html_section", "https://www.infobae.com/politica/",
                 section="politica", enabled=False, max_items=60, note="Respaldo"),
        Endpoint("Sección Economía (HTML)", "html_section", "https://www.infobae.com/economia/",
                 section="economia", enabled=False, max_items=60, note="Respaldo"),
    )

    def classify(self, item: FeedItem) -> tuple[str | None, bool]:
        path = urlsplit(item.url).path
        if "/agencias/" in path:
            return None, True
        return super().classify(item)
