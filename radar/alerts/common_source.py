"""Posible fuente común (informe, conferencia, agencia, entrevista) según evidencia textual.

Solo se informa cuando hay indicios explícitos en titulares, bajadas o metadatos. Es una
pista para revisar, no una conclusión.
"""

from __future__ import annotations

import re

from radar.analysis.textproc import norm

PATTERNS = {
    "informe": [r"\binforme\b", r"\brelevamiento\b", r"\bencuesta\b", r"\bestudio\b",
                r"\breporte\b", r"\bsegun datos de\b", r"\bdatos del indec\b", r"\bindec\b",
                r"\bindice\b", r"\bboletin oficial\b"],
    "conferencia": [r"\bconferencia de prensa\b", r"\bconferencia\b", r"\bcadena nacional\b",
                    r"\bcomunicado\b", r"\bdiscurso\b", r"\banuncio oficial\b", r"\bvocero\b"],
    "agencia": [r"\(na\)", r"\bnoticias argentinas\b", r"\bagencia\b", r"\befe\b", r"\bafp\b",
                r"\breuters\b", r"\btelam\b", r"\beuropa press\b", r"\bbloomberg\b"],
    # "Entrevista" sola no alcanza ("le robaron mientras daba una entrevista"): se busca
    # la fórmula que atribuye declaraciones a un medio o programa.
    "entrevista": [r"\ben (?:una )?entrevista con\b", r"\bentrevistado por\b",
                   r"\ben dialogo con\b", r"\ben declaraciones a\b", r"\bdijo a\b",
                   r"\ben una charla con\b"],
}
_COMPILED = {k: [re.compile(p) for p in v] for k, v in PATTERNS.items()}


def detect(articles) -> list[dict]:
    """[{tipo, articulos, indicios}] para tipos con evidencia en al menos 2 notas o en una
    republicación de agencia."""
    hits: dict[str, dict] = {}
    for a in articles:
        text = norm(" ".join(x for x in (a.title, a.subtitle or "", a.author or "") if x))
        for kind, regexes in _COMPILED.items():
            for rx in regexes:
                m = rx.search(text)
                if m:
                    h = hits.setdefault(kind, {"tipo": kind, "articulos": [], "indicios": []})
                    if a.id not in h["articulos"]:
                        h["articulos"].append(a.id)
                        h["indicios"].append(m.group(0))
                    break
        if a.is_syndicated:
            h = hits.setdefault("agencia", {"tipo": "agencia", "articulos": [], "indicios": []})
            if a.id not in h["articulos"]:
                h["articulos"].append(a.id)
                h["indicios"].append(f"republicación ({a.origin_label or a.author or 'agencia'})")
    out = []
    for h in hits.values():
        if len(h["articulos"]) >= 2 or (h["tipo"] == "agencia" and h["articulos"]):
            h["indicios"] = sorted(set(h["indicios"]))
            out.append(h)
    return sorted(out, key=lambda h: -len(h["articulos"]))
