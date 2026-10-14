"""Referencias a posteos de X (Twitter) dentro de mensajes de Telegram.

No se descarga nada de X: su `robots.txt` bloquea todo acceso automatizado y sus términos lo
prohíben. Solo se leen los enlaces que ya trae el mensaje y el texto de la vista previa que
Telegram guardó. Ese texto es lo que Telegram mostró, no una verificación con X: el posteo pudo
editarse o borrarse después.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

X_HOSTS = {"x.com", "twitter.com", "mobile.twitter.com", "vxtwitter.com", "fxtwitter.com",
           "fixupx.com", "fixvx.com"}
_STATUS = re.compile(r"^/(?:(?P<handle>[A-Za-z0-9_]{1,15})|i/web)/status(?:es)?/(?P<id>\d{5,25})")


def x_status_refs(urls: list[str]) -> list[dict]:
    """Posteos de X citados en `urls`: [{"handle": str | None, "id": str, "url": str}], sin repetir."""
    refs: dict[str, dict] = {}
    for raw in urls:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower().removeprefix("www.")
        if host not in X_HOSTS:
            continue
        m = _STATUS.match(parts.path)
        if not m:
            continue
        status_id = m.group("id")
        handle = m.group("handle")
        if status_id not in refs or (handle and not refs[status_id]["handle"]):
            refs[status_id] = {"handle": handle, "id": status_id,
                               "url": f"https://x.com/{handle or 'i/web'}/status/{status_id}"}
    return list(refs.values())
