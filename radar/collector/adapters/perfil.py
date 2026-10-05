"""Perfil (perfil.com).

Fuentes verificadas el 2026-10-04 (ver README):
- RSS: /feed, /feed/politica, /feed/economia (guid numérico estable, isPermaLink=false).
- Sitemap de noticias: /sitemap/google-news-lastposts (declarado en robots.txt).
- Secciones HTML: /seccion/politica, /seccion/economia (respaldo, desactivadas).

Subfuentes editoriales detectadas por evidencia en la URL:
- /noticias/canal-e/...       → Canal E
- noticias.perfil.com         → Revista Noticias
- /noticias/cordoba/...       → Perfil Córdoba
- /noticias/bloomberg/...     → Bloomberg (republicación: no es producción propia)
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from radar.collector.adapters.base import Endpoint, MediaAdapter
from radar.collector.parsers import FeedItem

_SECTION_LABELS = {
    "canal-e": ("Canal E", False),
    "cordoba": ("Perfil Córdoba", False),
    "bloomberg": ("Bloomberg", True),
}


class PerfilAdapter(MediaAdapter):
    slug = "perfil"
    name = "Perfil"
    base_url = "https://www.perfil.com"
    allowed_domains = ("perfil.com",)
    article_path_re = re.compile(r"^/noticias/[a-z0-9-]+/[^/]+\.phtml$")
    endpoints = (
        Endpoint("RSS general", "rss", "https://www.perfil.com/feed"),
        Endpoint("RSS Política", "rss", "https://www.perfil.com/feed/politica", section="politica"),
        Endpoint("RSS Economía", "rss", "https://www.perfil.com/feed/economia", section="economia"),
        Endpoint("Sitemap de noticias", "sitemap_news",
                 "https://www.perfil.com/sitemap/google-news-lastposts"),
        Endpoint("Sección Política (HTML)", "html_section", "https://www.perfil.com/seccion/politica",
                 section="politica", enabled=False, max_items=60, note="Respaldo si falla el RSS"),
        Endpoint("Sección Economía (HTML)", "html_section", "https://www.perfil.com/seccion/economia",
                 section="economia", enabled=False, max_items=60, note="Respaldo si falla el RSS"),
    )

    def section_from_url(self, url: str) -> str | None:
        parts = [s for s in urlsplit(url).path.split("/") if s]
        return parts[1].lower() if len(parts) >= 3 and parts[0] == "noticias" else None

    def source_id(self, item: FeedItem) -> str | None:
        if item.guid and item.guid.isdigit():
            return item.guid
        return None

    def classify(self, item: FeedItem) -> tuple[str | None, bool]:
        host = (urlsplit(item.url).hostname or "").lower()
        section = self.section_from_url(item.url)
        if section in _SECTION_LABELS:
            return _SECTION_LABELS[section]
        if host == "noticias.perfil.com":
            return "Revista Noticias", False
        return super().classify(item)
