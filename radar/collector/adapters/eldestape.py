"""El Destape (eldestapeweb.com).

Fuentes verificadas el 2026-10-04 (ver README):
- No hay RSS: /rss, /rss/, /feed y /arc/outboundfeeds/rss/ devuelven 404.
- Sitemap de noticias: /sitemap-news.xml (declarado en robots.txt, ~170 notas recientes).
- Secciones HTML: /politica, /economia (respaldo, desactivadas).
robots.txt pide Crawl-delay: 10, que se respeta como separación mínima entre solicitudes.

Las URLs terminan en un número (p. ej. ...-2026104183232) que NO es un identificador:
es la fecha y hora de publicación sin ceros (2026-10-4 18:32:32). En la recolección real
del 2026-10-04 aparecieron notas distintas con el mismo número (publicadas en el mismo
segundo), así que la deduplicación de El Destape usa solo la URL canónica.
"""

from __future__ import annotations

import re

from radar.collector.adapters.base import Endpoint, MediaAdapter


class ElDestapeAdapter(MediaAdapter):
    slug = "eldestape"
    name = "El Destape Web"
    base_url = "https://www.eldestapeweb.com"
    allowed_domains = ("eldestapeweb.com",)
    request_interval = 10.0
    article_path_re = re.compile(r"^/[a-z0-9-]+(?:/[a-z0-9-]+)*/[^/]*-\d{10,}$")
    endpoints = (
        Endpoint("Sitemap de noticias", "sitemap_news",
                 "https://www.eldestapeweb.com/sitemap-news.xml"),
        Endpoint("Sección Política (HTML)", "html_section", "https://www.eldestapeweb.com/politica",
                 section="politica", enabled=False, max_items=60, note="Respaldo del sitemap"),
        Endpoint("Sección Economía (HTML)", "html_section", "https://www.eldestapeweb.com/economia",
                 section="economia", enabled=False, max_items=60, note="Respaldo del sitemap"),
    )

