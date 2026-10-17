"""Importación manual de posteos de X exportados con twitter-web-exporter (JSON o CSV).

Radar NO se conecta a X: ni API, ni cuentas, ni cookies, ni navegador automatizado (su
`robots.txt` y sus términos lo prohíben). Una persona usa el userscript en su propio navegador,
exporta un archivo y lo sube al panel (o lo pasa a `python -m radar import-x`). Acá solo se lee ese
archivo. La extensión de Chrome (`radar/web/routes/api_routes.py`) manda las mismas filas en JSON.

Reglas de producto que se mantienen:
- Es material importado a mano y sin verificar: el posteo pudo editarse o borrarse después.
- Un posteo no es un medio independiente; un retuit es una réplica.
- La hora se guarda solo si el archivo la trae; un posteo sin fecha válida se descarta.
- La coincidencia con una nota es léxica (ver `radar.social.match`).
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from radar.models import SocialAccount, SocialPost
from radar.social.telegram import MAX_TEXT, ParsedPost, links_json, post_search_text
from radar.text import clean_ws, content_hash
from radar.timeutil import to_utc_naive, utcnow

PLATFORM = "x"
MAX_FILE_BYTES = 15 * 1024 * 1024
MAX_ITEMS = 20_000
MAX_NEW_ACCOUNTS = 100  # autores nuevos por importación (una línea de tiempo trae muchos)
MAX_X_ACCOUNTS = 300
HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_ID_RE = re.compile(r"^\d{5,25}$")
_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_TCO_RE = re.compile(r"https?://t\.co/\S+")
_LEGACY_DATE = "%a %b %d %H:%M:%S %z %Y"  # "Sat Jun 03 12:34:56 +0000 2023"


class XImportError(ValueError):
    """El archivo no se puede leer como una exportación de twitter-web-exporter."""


@dataclass
class XPost:
    handle: str
    name: str
    post: ParsedPost


@dataclass
class ParseResult:
    posts: list[XPost] = field(default_factory=list)
    discarded: int = 0  # sin id, autor, texto o fecha válidos


@dataclass
class ImportSummary:
    read: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    discarded: int = 0
    too_old: int = 0
    skipped_authors: int = 0  # posteos de autores que exceden el tope de cuentas nuevas
    new_accounts: int = 0
    handles: list[str] = field(default_factory=list)


def _get(item: dict, *paths: str):
    """Primer valor presente entre rutas con puntos ("legacy.full_text"); tolera estructuras
    planas y la forma cruda de GraphQL cuando se exportó con «incluir todos los metadatos»."""
    for path in paths:
        cur = item
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                cur = None
                break
        if cur not in (None, ""):
            return cur
    return None


def parse_datetime(value) -> datetime | None:
    """ISO 8601, formato clásico de Twitter, o epoch (segundos o milisegundos). → UTC sin zona."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
            number = float(value)
            if number > 1e11:
                number /= 1000
            return to_utc_naive(datetime.fromtimestamp(number, tz=timezone.utc))
        text = str(value).strip()
        try:
            parsed = datetime.strptime(text, _LEGACY_DATE)
        except ValueError:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace(" ", "T", 1))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)  # el exportador informa en UTC
        return to_utc_naive(parsed)
    except (ValueError, OverflowError, OSError):
        return None


def _to_int(value) -> int | None:
    try:
        return int(str(value).replace(",", "").strip()) if value not in (None, "") else None
    except ValueError:
        return None


def _expanded_urls(item: dict) -> dict[str, str]:
    """{t.co → URL real} cuando el archivo trae las URL expandidas."""
    out: dict[str, str] = {}
    candidates = _get(item, "urls", "entities.urls", "legacy.entities.urls") or []
    if isinstance(candidates, str):
        try:
            candidates = json.loads(candidates)
        except ValueError:
            candidates = [u for u in re.split(r"[\s,;|]+", candidates) if u]
    for entry in candidates if isinstance(candidates, list) else []:
        if isinstance(entry, dict):
            short, full = entry.get("url"), entry.get("expanded_url") or entry.get("expanded")
            if short and full:
                out[str(short)] = str(full)
        elif isinstance(entry, str):
            out.setdefault(entry, entry)
    return out


def _item_to_post(item: dict) -> XPost | None:
    ident = str(_get(item, "id", "id_str", "rest_id", "legacy.id_str", "tweet_id") or "").strip()
    handle = str(_get(item, "screen_name", "username", "user.screen_name", "author.screen_name",
                      "core.user_results.result.legacy.screen_name",
                      "core.user_results.result.core.screen_name") or "").strip().lstrip("@")
    if not _ID_RE.match(ident) or not HANDLE_RE.match(handle):
        return None
    posted = parse_datetime(_get(item, "created_at", "legacy.created_at", "date", "timestamp"))
    text = str(_get(item, "full_text", "text", "legacy.full_text") or "")
    if posted is None or not text.strip():
        return None
    expanded = _expanded_urls(item)
    links = list(expanded.values())
    for short, full in expanded.items():
        text = text.replace(short, full)
    # Los t.co que quedan sin expandir no sirven como enlace ni como texto.
    text = clean_ws(_TCO_RE.sub(" ", text)) if _TCO_RE.search(text) else text.strip()
    links += _URL_RE.findall(text)
    links = [u for u in dict.fromkeys(links) if len(u) <= 2048][:20]
    retweet = bool(_get(item, "retweeted_status_id", "retweeted_status", "legacy.retweeted_status_result")
                   or text.startswith("RT @"))
    forwarded = None
    if retweet:
        m = re.match(r"RT @([A-Za-z0-9_]{1,15}):", text)
        forwarded = f"@{m.group(1)}" if m else None
    views = _to_int(_get(item, "view_count", "views", "views.count", "ext_views.count"))
    name = str(_get(item, "name", "user.name", "author.name") or handle)[:128]
    return XPost(handle=handle, name=name, post=ParsedPost(
        external_id=ident, url=f"https://x.com/{handle}/status/{ident}", text=text[:MAX_TEXT],
        posted_at=posted, links=links, is_repost=retweet, forwarded_from=forwarded, views=views))


def _rows(content: bytes, filename: str) -> list[dict]:
    if len(content) > MAX_FILE_BYTES:
        raise XImportError(f"El archivo supera el máximo de {MAX_FILE_BYTES // (1024 * 1024)} MB.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise XImportError("El archivo no está en UTF-8.") from exc
    stripped = text.lstrip()
    if stripped[:1] in "[{" and not filename.lower().endswith(".csv"):
        try:
            data = json.loads(stripped)
        except ValueError as exc:
            raise XImportError(f"JSON no válido: {exc}") from exc
        if isinstance(data, dict):  # {"data": [...]} o un solo posteo
            data = data.get("data") or data.get("tweets") or [data]
        if not isinstance(data, list):
            raise XImportError("Se esperaba una lista de posteos.")
        return [r for r in data if isinstance(r, dict)]
    try:
        csv.field_size_limit(4_000_000)
        return list(csv.DictReader(io.StringIO(text)))
    except csv.Error as exc:
        raise XImportError(f"CSV no válido: {exc}") from exc


def parse_export(content: bytes, filename: str = "") -> ParseResult:
    return parse_rows(_rows(content, filename))


def parse_rows(rows: list) -> ParseResult:
    """Filas ya decodificadas (la extensión de Chrome las manda en JSON con el mismo formato
    plano que twitter-web-exporter)."""
    rows = [r for r in rows if isinstance(r, dict)]
    if not rows:
        raise XImportError("El archivo no trae posteos.")
    if len(rows) > MAX_ITEMS:
        raise XImportError(f"El archivo trae {len(rows)} posteos; el máximo es {MAX_ITEMS}.")
    result = ParseResult()
    for row in rows:
        post = _item_to_post(row)
        if post is None:
            result.discarded += 1
        else:
            result.posts.append(post)
    if not result.posts:
        raise XImportError(
            "No se reconoció ningún posteo. Se esperan las columnas id, screen_name, created_at y "
            "full_text de twitter-web-exporter (formato JSON o CSV).")
    return result


def import_posts(db: Session, parsed: ParseResult, retention_days: int | None = None,
                 now: datetime | None = None) -> ImportSummary:
    """Guarda los posteos (idempotente por cuenta + id). Crea cuentas de plataforma `x` para los
    autores nuevos hasta un tope; no toca vínculos ni revisiones existentes."""
    now = now or utcnow()
    summary = ImportSummary(read=len(parsed.posts) + parsed.discarded, discarded=parsed.discarded)
    horizon = now - timedelta(days=retention_days) if retention_days else None
    accounts = {a.handle.lower(): a for a in db.scalars(
        select(SocialAccount).where(SocialAccount.platform == PLATFORM))}
    total_x = len(accounts)
    seen_handles: dict[str, None] = {}
    for item in parsed.posts:
        post = item.post
        if horizon and post.posted_at < horizon:
            summary.too_old += 1  # el mantenimiento lo borraría enseguida
            continue
        key = item.handle.lower()
        account = accounts.get(key)
        if account is None:
            if summary.new_accounts >= MAX_NEW_ACCOUNTS or total_x >= MAX_X_ACCOUNTS:
                summary.skipped_authors += 1
                continue
            account = SocialAccount(platform=PLATFORM, handle=item.handle, name=item.name,
                                    kind="persona", enabled=True)
            db.add(account)
            db.flush()
            accounts[key] = account
            summary.new_accounts += 1
            total_x += 1
        seen_handles[account.handle] = None
        digest = content_hash(post.analysis_text, post.text)
        existing = db.scalar(select(SocialPost).where(
            SocialPost.account_id == account.id, SocialPost.external_id == post.external_id))
        if existing is None:
            db.add(SocialPost(
                account_id=account.id, external_id=post.external_id, url=post.url[:512],
                text=post.text, links=links_json(post), search_text=post_search_text(post),
                posted_at=post.posted_at, is_repost=post.is_repost,
                forwarded_from=post.forwarded_from, views=post.views, content_hash=digest,
                first_seen_at=now, last_seen_at=now))
            summary.new += 1
            continue
        existing.last_seen_at = now
        changed = False
        if post.views is not None and post.views != existing.views:
            existing.views, changed = post.views, True
        if digest != existing.content_hash:  # versión nueva del texto: se conserva la última
            existing.text, existing.links = post.text, links_json(post)
            existing.search_text, existing.content_hash = post_search_text(post), digest
            existing.edited_at, changed = now, True
        summary.updated += changed
        summary.unchanged += not changed
    summary.handles = list(seen_handles)
    for account in accounts.values():
        if account.handle in seen_handles:
            account.last_success_at, account.last_status = now, "ok"
            account.last_items_found = sum(1 for p in parsed.posts if p.handle.lower() == account.handle.lower())
    db.flush()
    return summary
