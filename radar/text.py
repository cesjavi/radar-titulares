"""Utilidades de texto: limpieza de HTML, normalización y URL canónica."""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_WS = re.compile(r"\s+")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "a"}:
            # Los enlaces en descripciones de feeds suelen ser "Leer más".
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "a"} and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def strip_html(value: str | None) -> str | None:
    if not value:
        return None
    if "<" not in value:
        text = html.unescape(value)
    else:
        parser = _TextExtractor()
        parser.feed(value)
        parser.close()
        text = " ".join(parser.parts)
    text = clean_ws(text)
    return text or None


def clean_ws(value: str | None) -> str:
    return _WS.sub(" ", value or "").strip()


def normalize_for_search(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.lower())
    return clean_ws("".join(c for c in decomposed if not unicodedata.combining(c)))


def title_similarity(a: str, b: str) -> float:
    """Jaccard de palabras (normalizadas, de 3+ letras) entre dos titulares."""
    wa = {w for w in re.findall(r"\w+", normalize_for_search(a)) if len(w) > 2}
    wb = {w for w in re.findall(r"\w+", normalize_for_search(b)) if len(w) > 2}
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def content_hash(*parts: str | None) -> str:
    joined = "\x1f".join(clean_ws(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# Parámetros de seguimiento conocidos. No se tocan los demás (pueden ser funcionales).
_TRACKING_PREFIXES = ("utm_",)
_TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "gbraid", "wbraid", "msclkid", "yclid", "igshid", "twclid",
    "mc_cid", "mc_eid", "_ga", "_gl", "ocid", "cmpid", "s_cid", "__twitter_impression",
}

# Límites de texto guardado.
MAX_TITLE = 1000
MAX_SUBTITLE = 3000
MAX_AUTHOR = 512
MAX_BODY = 20000


def truncate(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def is_tracking_param(name: str) -> bool:
    n = name.lower()
    return n in _TRACKING_PARAMS or n.startswith(_TRACKING_PREFIXES)


def canonicalize_url(url: str) -> str:
    """URL canónica: https, host en minúsculas, sin fragmento ni parámetros de seguimiento.

    Se conserva la ruta tal cual y el resto de los parámetros (en su orden original).
    """
    parts = urlsplit(url.strip())
    scheme = "https" if parts.scheme in {"http", "https"} else parts.scheme
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not is_tracking_param(k)
    ]
    path = parts.path or "/"
    return urlunsplit((scheme, host, path, urlencode(query), ""))
