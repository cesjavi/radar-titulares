"""Posible fuente de una nota según lo que el propio texto atribuye.

Busca en el titular, la bajada y el cuerpo (si el feed lo trae) las fórmulas con las que una
nota dice de dónde sale la información: «según…», un comunicado, una denuncia o expediente,
un posteo, una entrevista, fuentes sin nombre, una agencia u otro medio. Es una pista para
revisar: no confirma la fuente, y que dos notas citen lo mismo sugiere una fuente compartida,
no que un medio haya tomado de otro. Se calcula al mostrar la página; no se guarda.
"""

from __future__ import annotations

import re

from radar.analysis.textproc import strip_accents

LABELS = {
    "atribucion": "Atribución explícita",
    "fuentes": "Fuentes sin nombre",
    "declaraciones": "Declaraciones o descargo",
    "redes": "Publicación en redes o video",
    "comunicado": "Comunicado o conferencia",
    "judicial": "Denuncia o actuación judicial",
    "documento": "Informe, documento o norma",
    "agencia": "Agencia",
    "medio": "Otro medio",
}

_WHO = r"(?P<who>[^,.;:\"«»“”()\n]{3,70})"
# Se buscan sobre el texto en minúsculas y sin tildes (con las mismas posiciones que el original).
# El orden importa: «según pudo saber» es una fuente sin nombre, no una atribución.
PATTERNS: list[tuple[str, str]] = [
    ("fuentes", r"\bfuentes? (?:judiciales|oficiales|oficialistas|policiales|parlamentarias|diplomaticas"
                r"|cercanas|allegadas|consultadas|de la causa|del gobierno|de la investigacion)\b"),
    ("fuentes", r"\bsegun (?:pudo saber|pudieron saber|trascendio)\b"),
    ("fuentes", r"\b(?:le |les )?(?:confirmaron|revelaron|deslizaron) a\b"),
    ("atribucion", r"\bsegun (?!pudo |pudieron |trascendio)(?:informo|informaron |indico |indicaron |confirmo |confirmaron |publico "
                   r"|difundio |consigna |detallo |explico )?" + _WHO),
    ("atribucion", r"\bde acuerdo (?:con|a) " + _WHO),
    ("declaraciones", r"\b(?:en (?:una )?entrevista con|entrevistado por|en dialogo con"
                      r"|en declaraciones a|en una charla con|dijo a)\b"),
    ("declaraciones", r"\b(?:descargo|rompio el silencio|se defendio|salio a responder"
                      r"|respondio a las (?:acusaciones|criticas|denuncias))\b"),
    ("redes", r"\b(?:en (?:su|sus) (?:cuenta|cuentas|redes|perfil)|en redes sociales|posteo|tuit"
              r"|un video (?:publicado|que publico|difundido|que circula)|publico un (?:video|mensaje))\b"),
    ("comunicado", r"\b(?:comunicado|gacetilla|parte de prensa|conferencia de prensa|cadena nacional"
                   r"|anuncio oficial|vocero|portavoz)\b"),
    # «Fiscal» sola es también economía («déficit fiscal») y «fallo» es también «falló».
    ("judicial", r"\b(?:la denuncia|una denuncia|denuncia penal|expediente|imputacion|requerimiento"
                 r"|dictamen|procesamiento|el fallo|sentencia|(?:el|la) fiscal|fiscalia|juzgado"
                 r"|jueza?|tribunal|camara federal)\b"),
    ("documento", r"\b(?:informe|relevamiento|encuesta|(?:un|el) estudio (?:de|del)|reporte"
                  r"|boletin oficial|decreto|resolucion|datos del indec)\b"),
    ("agencia", r"\(na\)|\b(?:noticias argentinas|telam|efe|afp|reuters|europa press|ansa|dpa"
                r"|associated press|bloomberg)\b"),
]
_COMPILED = [(kind, re.compile(p)) for kind, p in PATTERNS]
# Palabras que cortan el «quién» de una atribución: «según la fiscalía que investiga…».
_WHO_STOP = re.compile(r"\s(?:que|quien|quienes|donde|cuando|para|porque|mientras|tras|y el|y la)\s")
_LEADING = re.compile(r"^(?:el|la|los|las|un|una|lo)\s+")
MAX_PER_ARTICLE = 12
SNIPPET = 70


def _fold(text: str) -> str:
    """Minúsculas y sin tildes, carácter por carácter: las posiciones coinciden con el original."""
    out = []
    for c in text:
        f = strip_accents(c.lower())
        out.append(f if len(f) == 1 else c)
    return "".join(out)


def _snippet(text: str, start: int, end: int) -> str:
    a, b = max(0, start - SNIPPET), min(len(text), end + SNIPPET)
    return ("…" if a else "") + " ".join(text[a:b].split()) + ("…" if b < len(text) else "")


def _key(phrase: str) -> str:
    words = _LEADING.sub("", " ".join(_fold(phrase).split())).split()
    return " ".join(words[:4])


def find_attributions(article, references=()) -> list[dict]:
    """[{tipo, etiqueta, fuente, clave, fragmento, donde}] de una nota. `references`: sus
    MediaReference (menciones y enlaces a otros medios), si se quieren incluir."""
    found: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, source: str, snippet: str, where: str) -> None:
        key = (kind, _key(source))
        if key in seen or len(found) >= MAX_PER_ARTICLE:
            return
        seen.add(key)
        found.append({"tipo": kind, "etiqueta": LABELS[kind], "fuente": source, "clave": key[1],
                      "fragmento": snippet, "donde": where})

    parts = [("titular", article.title or ""), ("bajada", article.subtitle or ""),
             ("cuerpo", (article.body_text or "")[:20000])]
    for where, text in parts:
        if not text:
            continue
        folded = _fold(text)
        for kind, rx in _COMPILED:
            for m in rx.finditer(folded):
                if "who" in m.groupdict() and m.group("who"):
                    start = m.start("who")
                    who = text[start:m.end("who")]
                    cut = _WHO_STOP.search(_fold(who))
                    who = " ".join((who[:cut.start()] if cut else who).split()[:7])
                    if len(who) < 3:
                        continue
                    source = who
                else:
                    source = " ".join(text[m.start():m.end()].split())
                add(kind, source, _snippet(text, m.start(), m.end()), where)
    if getattr(article, "is_syndicated", False):
        add("agencia", article.origin_label or article.author or "agencia o tercero",
            "Nota marcada como republicada (no cuenta como medio independiente).", "metadatos")
    for ref in references:
        add("medio", ref.referenced_media, ref.evidence,
            "enlace en el cuerpo" if ref.kind == "enlace" else "mención")
    return found


def group_sources(articles, refs_by_article: dict[int, list] | None = None) -> dict:
    """Atribuciones de cada nota del grupo y las que se repiten en notas de dos o más medios.

    {"por_nota": [(article, [hallazgos])], "repetidas": [{tipo, etiqueta, fuente, medios,
    notas}], "sin_atribucion": [article]}"""
    refs_by_article = refs_by_article or {}
    per_note, shared = [], {}
    for a in articles:
        hits = find_attributions(a, refs_by_article.get(a.id, ()))
        per_note.append((a, hits))
        for h in hits:
            s = shared.setdefault((h["tipo"], h["clave"]), {
                "tipo": h["tipo"], "etiqueta": h["etiqueta"], "fuente": h["fuente"],
                "medios": set(), "notas": 0})
            s["medios"].add(a.media.name)
            s["notas"] += 1
    repeated = [dict(s, medios=sorted(s["medios"])) for s in shared.values() if len(s["medios"]) >= 2]
    repeated.sort(key=lambda s: (-len(s["medios"]), -s["notas"], s["etiqueta"]))
    return {"por_nota": [(a, h) for a, h in per_note if h], "repetidas": repeated,
            "sin_atribucion": [a for a, h in per_note if not h]}
