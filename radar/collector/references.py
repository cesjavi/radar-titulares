"""Referencias explícitas a otros medios: menciones en titular/bajada y enlaces en el cuerpo."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

# (nombre, dominios, patrón de texto o None). Patrones sensibles a mayúsculas para
# evitar confundir nombres con palabras comunes ("perfil", "ámbito", "nación").
KNOWN_MEDIA: list[tuple[str, tuple[str, ...], str | None]] = [
    ("Perfil", ("perfil.com",), r"\bDiario Perfil\b|\bPerfil\.com\b"),
    ("El Destape", ("eldestapeweb.com",), r"\bEl Destape\b"),
    ("Infobae", ("infobae.com",), r"\bInfobae\b"),
    ("Clarín", ("clarin.com",), r"\bClar[ií]n\b"),
    ("La Nación", ("lanacion.com.ar",), r"\bLa Naci[oó]n\b(?! Argentina)"),
    ("Página/12", ("pagina12.com.ar",), r"\bP[aá]gina\s?/?\s?12\b"),
    ("Ámbito", ("ambito.com",), r"\b[ÁA]mbito Financiero\b|\bdiario [ÁA]mbito\b"),
    ("El Cronista", ("cronista.com",), r"\bEl Cronista\b"),
    ("TN", ("tn.com.ar",), r"\bTN\b"),
    ("C5N", ("c5n.com",), r"\bC5N\b"),
    ("A24", ("a24.com",), r"\bA24\b"),
    ("La Política Online", ("lapoliticaonline.com",), r"\bLa Pol[ií]tica Online\b|\bLPO\b"),
    ("elDiarioAR", ("eldiarioar.com",), r"\belDiarioAR\b"),
    ("Letra P", ("letrap.com.ar",), r"\bLetra P\b"),
    ("Noticias Argentinas", ("noticiasargentinas.com",), r"\bNoticias Argentinas\b"),
    ("Télam", ("telam.com.ar",), r"\bT[ée]lam\b"),
    ("Radio Mitre", ("radiomitre.cienradios.com",), r"\bRadio Mitre\b"),
    ("Reuters", ("reuters.com",), r"\bReuters\b"),
    ("Bloomberg", ("bloomberg.com", "bloomberglinea.com"), r"\bBloomberg\b"),
    ("AFP", ("afp.com",), r"\bAFP\b"),
    ("EFE", ("efe.com",), r"\bEFE\b"),
    ("The New York Times", ("nytimes.com",), r"\bThe New York Times\b|\bNew York Times\b"),
    ("Financial Times", ("ft.com",), r"\bFinancial Times\b"),
    ("The Wall Street Journal", ("wsj.com",), r"\bWall Street Journal\b"),
]

_COMPILED = [(name, domains, re.compile(p) if p else None) for name, domains, p in KNOWN_MEDIA]


@dataclass(frozen=True)
class Reference:
    media_name: str
    kind: str  # mencion | enlace
    evidence: str  # fragmento del texto o URL enlazada


def _own(name: str, own_domains: tuple[str, ...] | list[str]) -> bool:
    for n, domains, _ in _COMPILED:
        if n == name:
            return any(d in own_domains or any(o.endswith(d) for o in own_domains) for d in domains)
    return False


def find_text_mentions(text: str | None, own_domains) -> list[Reference]:
    if not text:
        return []
    found: list[Reference] = []
    for name, _domains, rx in _COMPILED:
        if rx is None or _own(name, own_domains):
            continue
        m = rx.search(text)
        if m:
            start, end = max(0, m.start() - 60), min(len(text), m.end() + 60)
            snippet = ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")
            found.append(Reference(name, "mencion", snippet))
    return found


def media_for_url(url: str) -> str | None:
    host = (urlsplit(url).hostname or "").lower()
    for name, domains, _ in _COMPILED:
        if any(host == d or host.endswith("." + d) for d in domains):
            return name
    return None


def find_link_references(links, own_domains, limit: int = 20) -> list[Reference]:
    """Enlaces dentro del cuerpo (<article>) hacia otros medios conocidos."""
    found: dict[tuple[str, str], Reference] = {}
    for link in links:
        if not link.in_article:
            continue
        name = media_for_url(link.url)
        if name is None or _own(name, own_domains):
            continue
        key = (name, link.url)
        if key not in found:
            found[key] = Reference(name, "enlace", link.url[:1000])
        if len(found) >= limit:
            break
    return list(found.values())
