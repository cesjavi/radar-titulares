"""Página/12 (pagina12.com.ar).

Fuentes verificadas el 2026-10-04 (ver README):
- RSS por sección: /arc/outboundfeeds/rss/portada y /rss/secciones/{el-pais,economia,sociedad,
  el-mundo}/notas (25 notas cada uno). El índice público de feeds está en /rss.
- Sitemap de noticias: /arc/outboundfeeds/breakingnews-short.xml (declarado en robots.txt;
  ~100 notas de las últimas horas, con título y hora de publicación).
- Secciones HTML: /el-pais/ y /economia/ (respaldo, desactivadas; /secciones/... redirige).
- Las URLs de los feeds se declaran con barra final: sin ella el sitio responde 301.
- NO se usa /arc/outboundfeeds/rss/ (sin sufijo): es el feed de la edición regional "Salta|12".
- NO se usa /arc/outboundfeeds/breakingnews-sitemap.xml: sus notas son de oct. 2025 a ago. 2026.

Particularidades del medio:
- La URL es /AAAA/MM/DD/slug/ y NO incluye la sección: se toma del feed de origen.
- El guid es un identificador alfanumérico opaco (p. ej. YGM7O4LHHJGNZAY7356H3KSN3E) que se
  usa como identificador de origen.
- dc:creator llega a veces con HTML y prefijo ("Por <b>Ana Pérez</b>"); se limpia a "Ana Pérez".
robots.txt: Allow: /, sin Crawl-delay.
"""

from __future__ import annotations

import re

from radar.collector.adapters.base import Endpoint, MediaAdapter
from radar.collector.parsers import FeedItem

_RSS = "https://www.pagina12.com.ar/arc/outboundfeeds/rss"
_GUID_RE = re.compile(r"^[A-Z0-9]{20,32}$")


class Pagina12Adapter(MediaAdapter):
    slug = "pagina12"
    name = "Página/12"
    base_url = "https://www.pagina12.com.ar"
    allowed_domains = ("pagina12.com.ar",)
    # /2026/10/05/slug/ (sin sección). Se excluyen los clones de prueba que el medio deja
    # publicados (slug terminado en "-clone").
    article_path_re = re.compile(r"^/\d{4}/\d{2}/\d{2}/(?!.*-clone/?$)[^/]+/?$")
    endpoints = (
        Endpoint("RSS Portada", "rss", f"{_RSS}/portada/", max_items=30),
        Endpoint("RSS El País", "rss", f"{_RSS}/secciones/el-pais/notas/", section="el-pais"),
        Endpoint("RSS Economía", "rss", f"{_RSS}/secciones/economia/notas/", section="economia"),
        Endpoint("RSS Sociedad", "rss", f"{_RSS}/secciones/sociedad/notas/", section="sociedad"),
        Endpoint("RSS El Mundo", "rss", f"{_RSS}/secciones/el-mundo/notas/", section="el-mundo"),
        Endpoint("Sitemap de noticias (últimas horas)", "sitemap_news",
                 "https://www.pagina12.com.ar/arc/outboundfeeds/breakingnews-short.xml"),
        Endpoint("Sección El País (HTML)", "html_section",
                 "https://www.pagina12.com.ar/el-pais/", section="el-pais",
                 enabled=False, max_items=60, note="Respaldo si falla el RSS"),
        Endpoint("Sección Economía (HTML)", "html_section",
                 "https://www.pagina12.com.ar/economia/", section="economia",
                 enabled=False, max_items=60, note="Respaldo si falla el RSS"),
    )

    def section_from_url(self, url: str) -> str | None:
        return None  # la URL no trae la sección

    def source_id(self, item: FeedItem) -> str | None:
        return item.guid if item.guid and _GUID_RE.match(item.guid) else None
