"""Medios agregados desde el panel: adaptador genérico y descubrimiento de feeds.

Un medio sin adaptador propio (código) se recolecta con `GenericAdapter`: las notas salen de los
feeds RSS/Atom, sitemaps de noticias o portadas que el administrador confirmó. No hay reglas
particulares del medio: la sección sale del primer tramo de la URL (o del endpoint), no hay
identificador de origen y la única republicación que se reconoce es la firmada por agencias.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from radar.collector.adapters import ADAPTERS, MediaAdapter
from radar.collector.html import decode_html
from radar.collector.parsers import FeedBlockedError, FeedParseError
from radar.net import BlockedError, FetchError, SafeFetcher, UnsafeURLError, validate_url
from radar.text import normalize_for_search

KINDS = ("rss", "sitemap_news", "html_section")
KIND_LABELS = {"rss": "RSS / Atom", "sitemap_news": "Sitemap de noticias",
               "html_section": "Portada o sección (HTML)"}
MAX_ENDPOINTS = 12

# Rutas que no son notas aunque estén en el dominio del medio.
_NOT_ARTICLE = re.compile(
    r"^/(?:tag|tags|etiqueta|etiquetas|autor|autores|author|authors|category|categoria|"
    r"categorias|buscar|search|feed|rss|wp-content|wp-json|static|assets)(?:/|$)", re.I)

_COMMON_FEEDS = ("/feed/", "/rss/", "/rss.xml", "/feed", "/rss", "/arc/outboundfeeds/rss/")
_COMMON_SITEMAPS = ("/news-sitemap.xml", "/sitemap-news.xml")
_LINK_RE = re.compile(r"<link\b[^>]*>", re.I)
_ATTR_RE = re.compile(r"""([a-zA-Z:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")


class GenericAdapter(MediaAdapter):
    """Adaptador por defecto para los medios que solo existen en la base de datos."""

    def __init__(self, slug: str, name: str, base_url: str, allowed_domains: tuple[str, ...]):
        self.slug, self.name, self.base_url = slug, name, base_url
        self.allowed_domains = allowed_domains
        self.endpoints = ()

    def is_article_url(self, url: str) -> bool:
        parts = urlsplit(url)
        path = parts.path or "/"
        return (parts.scheme in ("http", "https") and self.host_ok(url)
                and path.strip("/") != "" and not _NOT_ARTICLE.match(path))


def adapter_for_media(media) -> MediaAdapter:
    """Adaptador con código si existe; si no, el genérico armado con los datos del medio."""
    coded = ADAPTERS.get(media.slug)
    if coded is not None:
        return coded
    return GenericAdapter(media.slug, media.name, media.base_url, tuple(media.domain_list()))


# --- alta de medios ----------------------------------------------------------------------


class SiteError(ValueError):
    """Dato inválido al dar de alta un medio (el mensaje se muestra tal cual)."""


def normalize_site_url(raw: str) -> str:
    text = (raw or "").strip()
    if text and "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise SiteError("La dirección del sitio no es válida.") from None
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username \
            or parts.password:
        raise SiteError("Ingresá la dirección del sitio, por ejemplo https://www.ejemplo.com.ar")
    host = parts.hostname.lower()
    if "." not in host:
        raise SiteError("La dirección no parece un sitio público.")
    suffix = f":{port}" if port and port not in (80, 443) else ""
    return f"{parts.scheme}://{host}{suffix}"


def domain_of(site_url: str) -> str:
    host = urlsplit(site_url).hostname or ""
    return host[4:] if host.startswith("www.") else host


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", normalize_for_search(name)).strip("-")[:56]


def make_slug(name: str, taken: set[str]) -> str:
    base = slugify(name)
    if not base:
        raise SiteError("Poné un nombre para el medio.")
    slug, n = base, 2
    while slug in taken or slug in ADAPTERS:
        slug, n = f"{base}-{n}", n + 1
    return slug


@dataclass
class Candidate:
    kind: str
    url: str
    items: int
    sample: str

    @property
    def label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)


def check_endpoint(fetcher: SafeFetcher, adapter: MediaAdapter, kind: str, url: str,
                   max_items: int = 300) -> Candidate:
    """Descarga y parsea una fuente; devuelve cuántas notas del medio contiene o falla."""
    if kind not in KINDS:
        raise SiteError("Tipo de fuente no válido.")
    if len(url) > 500:
        raise SiteError("La dirección de la fuente es demasiado larga.")
    try:
        validate_url(url, adapter.allowed_domains, fetcher.resolver)
        resp = fetcher.get(url, adapter.allowed_domains)
        items = adapter.parse(resp.content, kind, resp.url, resp.headers.get("content-type"),
                              max_items)
    except FeedBlockedError as exc:
        raise SiteError(f"El sitio devolvió una página de bloqueo: {exc}") from exc
    except FeedParseError as exc:
        raise SiteError(f"No se pudo leer como {KIND_LABELS[kind]}: {exc}") from exc
    except BlockedError as exc:
        raise SiteError(f"El sitio no permite la lectura: {exc}") from exc
    except (FetchError, UnsafeURLError) as exc:
        raise SiteError(f"No se pudo descargar: {exc}") from exc
    if not items:
        raise SiteError("La dirección responde pero no contiene notas reconocibles del medio.")
    return Candidate(kind, url, len(items), items[0].title[:90])


def _feed_links(html: bytes, content_type: str | None, page_url: str) -> list[str]:
    """URL de los <link rel="alternate"> a feeds que declara la portada."""
    out: list[str] = []
    for tag in _LINK_RE.findall(decode_html(html, content_type)[:200_000]):
        attrs = {m[0].lower(): (m[1] or m[2]) for m in _ATTR_RE.findall(tag)}
        mime = attrs.get("type", "").lower()
        if "alternate" in attrs.get("rel", "").lower() and attrs.get("href") \
                and ("rss" in mime or "atom" in mime):
            out.append(urljoin(page_url, attrs["href"]))
    return out


def discover(fetcher: SafeFetcher, site_url: str, name: str = "") -> list[Candidate]:
    """Busca fuentes utilizables: feeds declarados en la portada y rutas habituales.

    Solo hace solicitudes al dominio del propio sitio, respetando robots.txt y los límites de
    `SafeFetcher`. No garantiza encontrar algo: el administrador puede cargar una URL a mano.
    """
    domain = domain_of(site_url)
    adapter = GenericAdapter("nuevo", name or domain, site_url, (domain,))
    try:
        home = fetcher.get(site_url + "/", adapter.allowed_domains)
    except (FetchError, BlockedError, UnsafeURLError) as exc:
        raise SiteError(f"No se pudo abrir el sitio: {exc}") from exc
    queue = [("rss", u) for u in _feed_links(home.content, home.headers.get("content-type"),
                                             home.url)]
    queue += [("rss", site_url + p) for p in _COMMON_FEEDS]
    queue += [("sitemap_news", site_url + p) for p in _COMMON_SITEMAPS]

    found: list[Candidate] = []
    tried: set[str] = set()
    for kind, url in queue:
        if len(tried) >= MAX_ENDPOINTS:
            break
        if url in tried or not adapter.host_ok(url):
            continue
        tried.add(url)
        try:
            found.append(check_endpoint(fetcher, adapter, kind, url))
        except SiteError:
            continue
    return found
