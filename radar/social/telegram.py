"""Lectura de la vista web pública de un canal de Telegram (`https://t.me/s/<canal>`).

No usa la API ni cuentas: es el mismo HTML que ve cualquier visitante. `t.me` no publica
robots.txt, y de todos modos la descarga pasa por `radar.net.SafeFetcher`, que lo respetaría.
Un canal sin vista web pública devuelve la página común del canal, sin mensajes: se informa
como error y no se intenta evadir nada.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from radar.collector.html import decode_html
from radar.text import clean_ws, normalize_for_search
from radar.timeutil import to_utc_naive

DOMAINS = ("t.me",)
HANDLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,63}$")
MAX_TEXT = 4000
MAX_PREVIEW = 1000

_WRAP_SPLIT = re.compile(r'<div class="tgme_widget_message_wrap')
_POST_ID = re.compile(r'data-post="([A-Za-z0-9_]+)/(\d+)"')
_TEXT = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
_DATE = re.compile(r'<a class="tgme_widget_message_date"[^>]*>\s*<time[^>]*datetime="([^"]+)"', re.S)
_VIEWS = re.compile(r'<span class="tgme_widget_message_views">([^<]*)</span>')
_FWD = re.compile(r'tgme_widget_message_forwarded_from_name[^>]*>(.*?)</(?:a|span)>', re.S)
_FWD_ANY = re.compile(r'tgme_widget_message_forwarded_from\b')
_PREVIEW_TITLE = re.compile(r'<div class="link_preview_title"[^>]*>(.*?)</div>', re.S)
_PREVIEW_DESC = re.compile(r'<div class="link_preview_description"[^>]*>(.*?)</div>', re.S)
_PREVIEW_HREF = re.compile(r'<a class="tgme_widget_message_link_preview"[^>]*href="([^"]+)"')
_HREF = re.compile(r'<a [^>]*href="([^"]+)"')
_EDITED = re.compile(r'<span class="tgme_widget_message_meta">\s*edited', re.I)
_TAG = re.compile(r"<[^>]+>")
_BR = re.compile(r"<br\s*/?>", re.I)
_TITLE = re.compile(r'<div class="tgme_channel_info_header_title"[^>]*><span[^>]*>(.*?)</span>', re.S)
_COUNTER = re.compile(r'<span class="counter_value">([^<]+)</span>\s*<span class="counter_type">([^<]+)</span>')
_MORE = re.compile(r'<a href="/s/[A-Za-z0-9_]+\?before=(\d+)"')
_URL_IN_TEXT = re.compile(r"https?://\S+")


class ChannelError(RuntimeError):
    """El canal no existe, no tiene vista web pública o el formato cambió."""


@dataclass
class ParsedPost:
    external_id: str
    url: str
    text: str
    posted_at: datetime
    links: list[str] = field(default_factory=list)
    preview_title: str | None = None
    preview_text: str | None = None
    is_repost: bool = False
    forwarded_from: str | None = None
    views: int | None = None
    edited: bool = False

    @property
    def analysis_text(self) -> str:
        """Texto que se compara con las notas. Un mensaje que solo trae un enlace se completa con
        el título y la descripción de la vista previa (p. ej. el texto de un posteo de X)."""
        own = clean_ws(_URL_IN_TEXT.sub(" ", self.text))
        if len(own.split()) >= 5:
            return own
        extra = " ".join(p for p in (self.preview_title, self.preview_text) if p)
        return clean_ws(f"{own} {extra}") if extra else own


@dataclass
class ParsedChannel:
    handle: str
    title: str | None
    subscribers: int | None
    posts: list[ParsedPost]
    older_than: int | None  # parámetro `before` de la página anterior, si hay más historia


def parse_count(raw: str | None) -> int | None:
    """'24.9K' → 24900, '5.61K' → 5610, '1.2M' → 1200000, '521' → 521."""
    if not raw:
        return None
    raw = raw.strip().replace(" ", "")
    m = re.fullmatch(r"(\d+(?:[.,]\d+)?)([KkMm]?)", raw)
    if not m:
        return None
    number = float(m.group(1).replace(",", "."))
    factor = {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2).lower()]
    return int(round(number * factor))


def _plain(fragment: str, keep_breaks: bool = False) -> str:
    fragment = _BR.sub("\n", fragment) if keep_breaks else _BR.sub(" ", fragment)
    text = html.unescape(_TAG.sub("", fragment))
    if keep_breaks:
        text = "\n".join(clean_ws(line) for line in text.splitlines())
        return re.sub(r"\n{3,}", "\n\n", text).strip()
    return clean_ws(text)


def _clean_link(href: str) -> str | None:
    href = html.unescape(href).strip()
    return href if href.lower().startswith(("http://", "https://")) and len(href) <= 2048 else None


def parse_channel(content: bytes, handle: str, content_type: str | None = None) -> ParsedChannel:
    page = decode_html(content, content_type)
    title_m = _TITLE.search(page)
    title = _plain(title_m.group(1)) if title_m else None
    subscribers = None
    for value, kind in _COUNTER.findall(page):
        if kind.strip().lower().startswith("subscriber"):
            subscribers = parse_count(value)
    chunks = _WRAP_SPLIT.split(page)[1:]
    posts: list[ParsedPost] = []
    for chunk in chunks:
        post = _parse_message(chunk, handle)
        if post:
            posts.append(post)
    more = _MORE.search(page)
    return ParsedChannel(handle, title, subscribers, posts,
                         int(more.group(1)) if more else None)


def _parse_message(chunk: str, handle: str) -> ParsedPost | None:
    ident = _POST_ID.search(chunk)
    date = _DATE.search(chunk)
    if not ident or not date or ident.group(1).lower() != handle.lower():
        return None
    try:
        posted = datetime.fromisoformat(date.group(1).replace("Z", "+00:00"))
    except ValueError:
        return None
    text_m = _TEXT.search(chunk)
    text = _plain(text_m.group(1), keep_breaks=True)[:MAX_TEXT] if text_m else ""
    links: list[str] = []
    if text_m:
        links += [u for h in _HREF.findall(text_m.group(1)) if (u := _clean_link(h))]
    pv_href = _PREVIEW_HREF.search(chunk)
    if pv_href and (u := _clean_link(pv_href.group(1))):
        links.append(u)
    title_m, desc_m = _PREVIEW_TITLE.search(chunk), _PREVIEW_DESC.search(chunk)
    preview_title = _plain(title_m.group(1))[:MAX_PREVIEW] if title_m else None
    preview_text = _plain(desc_m.group(1))[:MAX_PREVIEW] if desc_m else None
    if not text and not preview_title and not preview_text:
        return None  # solo foto o video: no hay texto que analizar
    forwarded = _FWD.search(chunk)
    views = _VIEWS.search(chunk)
    return ParsedPost(
        external_id=ident.group(2),
        url=f"https://t.me/{handle}/{ident.group(2)}",
        text=text, posted_at=to_utc_naive(posted),
        links=list(dict.fromkeys(links))[:20],
        preview_title=preview_title or None, preview_text=preview_text or None,
        is_repost=bool(_FWD_ANY.search(chunk)),
        forwarded_from=_plain(forwarded.group(1))[:128] if forwarded else None,
        views=parse_count(views.group(1)) if views else None,
        edited=bool(_EDITED.search(chunk)),
    )


def channel_url(handle: str, before: int | None = None) -> str:
    url = f"https://t.me/s/{handle}"
    return f"{url}?before={before}" if before else url


def post_search_text(post: ParsedPost) -> str:
    return normalize_for_search(post.analysis_text)[:MAX_TEXT]


def links_json(post: ParsedPost) -> str:
    return json.dumps(post.links, ensure_ascii=False)


def normalize_handle(raw: str) -> str:
    """Acepta '@canal', 't.me/canal', 'https://t.me/s/canal'. Devuelve el nombre o falla."""
    value = (raw or "").strip()
    value = re.sub(r"^https?://", "", value, flags=re.I)
    value = re.sub(r"^(?:www\.)?t\.me/(?:s/)?", "", value, flags=re.I)
    value = value.lstrip("@").split("/")[0].split("?")[0]
    if not HANDLE_RE.match(value):
        raise ChannelError("Nombre de canal no válido: usá el usuario público, por ejemplo "
                           "@canal o https://t.me/canal (de 4 a 64 letras, números o guiones bajos).")
    return value
