"""Procesamiento de texto en español que preserva negaciones, cifras y nombres."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Palabras vacías. NO incluye negaciones ("no", "ni", "nunca", "sin", "tampoco", "jamás")
# ni marcadores de cambio ("dejó", "sigue"), que alteran el sentido.
STOPWORDS = frozenset("""
a al algo algun alguna algunas alguno algunos ante antes aqui asi aun aunque bajo bien cada
como con contra cual cuales cuando de del desde donde dos e el ella ellas ello ellos en entre
era eran es esa esas ese eso esos esta estaba estaban estan estar este esto estos fue fueron
ha habia han hasta hay la las le les lo los mas me mi mientras muy nos o otra otras otro otros
para pero poco por porque que quien quienes se segun ser si sido sobre su sus tambien tan tanto
te tiene tienen todo todos tras tu un una unas uno unos y ya yo e u vs ante cual hoy ayer
cómo qué cuál quién dónde cuándo será serán sería puede pueden podría fue tras sus les esto
""".split())

NEGATIONS = frozenset({"no", "ni", "nunca", "jamas", "tampoco", "sin", "nadie", "ningun",
                       "ninguna", "ninguno", "nada"})
# Expresiones de interrupción o cese ("dejó de aumentar"): distinto de afirmar o negar.
# Se aplica sobre texto en minúsculas y sin tildes.
CESSATION_RE = re.compile(
    r"\bdej(?:o|a|an|aron|ara|aria|aran)\s+de\b"
    r"|\bya no\b"
    r"|\bfren(?:o|a|an|aron)\b"
    r"|\bces(?:o|a|an|aron)\b"
    r"|\binterrump(?:e|en|io|ieron)\b")

# Fechas: no cuentan como contenido de una expresión distintiva.
DATE_WORDS = frozenset("""
lunes martes miercoles jueves viernes sabado domingo enero febrero marzo abril mayo junio julio
agosto septiembre setiembre octubre noviembre diciembre hoy ayer manana semana mes ano
""".split())

# Fórmulas de formato comunes a muchos medios: no son "expresiones distintivas".
BOILERPLATE = frozenset({
    "en vivo", "minuto a minuto", "ultimas noticias", "lo que hay que saber", "que paso",
    "uno por uno", "paso a paso", "de hoy", "hoy domingo", "hoy lunes", "hoy martes",
    "hoy miercoles", "hoy jueves", "hoy viernes", "hoy sabado", "todo lo que", "que se sabe",
    "cuales son", "que es", "como es", "a que hora", "donde ver",
    "primera vez en la historia", "en las ultimas horas", "que hay que saber", "lo que se sabe",
    "en el marco de", "a traves de", "en medio de", "de acuerdo con", "quien gana hoy",
})


def expressive_words(phrase: str, entity_tokens: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """Palabras de una frase que aportan contenido: no vacías, no fechas, no cifras sueltas,
    no partes de nombres propios."""
    return [w for w in phrase.split()
            if w not in STOPWORDS and w not in DATE_WORDS and not w[0].isdigit()
            and w not in entity_tokens and len(w) > 2]

_WORD_RE = re.compile(r"\d+(?:[.,]\d+)*%?|[^\W\d_]+(?:[-'][^\W\d_]+)*", re.UNICODE)
_QUOTE_RE = re.compile(r"[\"“«‘']([^\"”»’']{8,200})[\"”»’']")
_NUMBER_RE = re.compile(
    r"(?:US\$|u\$s|\$|€)?\s?\d+(?:[.,]\d+)*\s?(?:%|por ciento|millones|mil millones|"
    r"billones|puntos|pp)?", re.I)
# Mayúsculas/minúsculas latinas, incluidas las del portugués y el francés ("São Paulo").
_UP = "A-ZÁÉÍÓÚÑÜÃÕÂÊÔÀÈÌÒÙÇÄËÏÖ"
_LOW = "a-záéíóúñüãõâêôàèìòùçäëïö"
_ENTITY_RE = re.compile(
    rf"\b([{_UP}][{_LOW}]+(?:\s+(?:de|del|da|do|dos|la|las|los|y)?\s*[{_UP}][{_LOW}]+)*"
    rf"|[{_UP}]{{2,8}}(?:\d+)?)\b")
_ENTITY_STOP = frozenset({
    "El", "La", "Los", "Las", "Un", "Una", "En", "Con", "Por", "Para", "Qué", "Cómo", "Cuál",
    "Quién", "Dónde", "Cuándo", "Así", "Tras", "Ante", "Según", "Hoy", "Ayer", "Este", "Esta",
    "EN", "VIVO", "Además", "Mientras", "Desde", "Sin", "No", "Ya", "Del", "Al", "Lo", "Se",
    "Ejemplo", "Video", "Fotos", "Galería", "Opinión", "Análisis", "Exclusivo",
    # Pronombres y verbos frecuentes al inicio de citas ("Le robaron...", "Estoy cansado...").
    "Le", "Les", "Su", "Sus", "Mi", "Me", "Nos", "Te", "Es", "Son", "Fue", "Era", "Hay", "Estoy",
    "Está", "Están", "Somos", "Vamos", "Mañana", "Crisis",
})


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", strip_accents(text.lower())).strip()


def raw_tokens(text: str) -> list[str]:
    """Tokens normalizados en orden, conservando cifras ("2,1%") y negaciones."""
    return [m.group(0) for m in _WORD_RE.finditer(norm(text))]


def light_stem(token: str) -> str:
    """Plurales simples. No toca cifras ni palabras cortas (no confunde 'no' con nada)."""
    if token[0].isdigit() or len(token) <= 4:
        return token
    if token.endswith("ces") and len(token) > 5:
        return token[:-3] + "z"
    if token.endswith("es") and len(token) > 5 and token[-3] not in "aeiou":
        return token[:-2]
    if token.endswith("s") and token[-2] in "aeiou":
        return token[:-1]
    return token


def content_tokens(text: str) -> list[str]:
    """Tokens con contenido (sin palabras vacías), con negaciones y cifras preservadas."""
    out = []
    for tok in raw_tokens(text):
        if tok in NEGATIONS or tok[0].isdigit():
            out.append(tok)
        elif tok not in STOPWORDS and len(tok) > 1:
            out.append(light_stem(tok))
    return out


def word_features(text: str) -> list[str]:
    """Unigramas y bigramas de contenido para TF-IDF."""
    toks = content_tokens(text)
    return toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:])]


def char_ngrams(text: str, n_min: int = 3, n_max: int = 5) -> list[str]:
    s = f" {' '.join(raw_tokens(text))} "
    return [s[i:i + n] for n in range(n_min, n_max + 1) for i in range(len(s) - n + 1)]


def numbers(text: str) -> set[str]:
    found = set()
    for m in _NUMBER_RE.finditer(text):
        value = re.sub(r"\s+", " ", m.group(0).strip().lower())
        if any(ch.isdigit() for ch in value) and not re.fullmatch(r"20\d\d|19\d\d", value):
            found.add(value.replace(" ", ""))
    return found


def entities(text: str, known: set[str] | frozenset[str] = frozenset()) -> set[str]:
    """Nombres propios y siglas (heurística por mayúsculas; sin modelo de NER).

    Una palabra suelta al inicio del texto se acepta solo si está en `known`
    (vista como nombre propio en otra posición del lote).
    """
    found = set()
    for m in _ENTITY_RE.finditer(text):
        ent = m.group(1).strip()
        words = ent.split()
        if not words:
            continue
        if len(words) == 1 and (ent in _ENTITY_STOP or (
                m.start() == 0 and not ent.isupper() and norm(ent) not in known)):
            # Una sola palabra al inicio del titular suele ser mayúscula de oración.
            continue
        while words and words[0] in _ENTITY_STOP:
            words = words[1:]
        # Al inicio del texto, la primera palabra puede ser solo mayúscula de oración
        # ("Renunció Claudia Pérez"): se descarta salvo que se la conozca como nombre propio.
        if m.start() == 0 and len(words) > 1 and not words[0].isupper()                 and norm(words[0]) not in known and norm(" ".join(words)) not in known:
            words = words[1:]
        if words:
            found.add(norm(" ".join(words)))
    return found


def quotes(text: str) -> list[str]:
    out = []
    for m in _QUOTE_RE.finditer(text):
        q = norm(m.group(1))
        if len(q.split()) >= 3:
            out.append(q)
    return out


@dataclass
class Polarity:
    negated: bool = False
    cessation: bool = False
    markers: list[str] = field(default_factory=list)


def polarity(text: str) -> Polarity:
    toks = raw_tokens(text)
    neg = [t for t in toks if t in NEGATIONS]
    ces = [m.group(0) for m in CESSATION_RE.finditer(norm(text))]
    return Polarity(negated=bool(neg), cessation=bool(ces), markers=neg + ces)


def polarity_conflict(a: Polarity, b: Polarity) -> str | None:
    """Explica por qué dos textos parecidos no afirman lo mismo (o None)."""
    if a.cessation != b.cessation:
        return "uno indica que algo dejó de ocurrir y el otro no"
    if a.negated != b.negated:
        return "uno contiene una negación y el otro no"
    return None


def phrase_ngrams(text: str, n_min: int = 3, n_max: int = 6) -> set[str]:
    """Frases de 3-6 palabras con al menos 2 palabras de contenido (para expresiones distintivas)."""
    toks = raw_tokens(text)
    out = set()
    for n in range(n_min, n_max + 1):
        for i in range(len(toks) - n + 1):
            gram = toks[i:i + n]
            if gram[0] in STOPWORDS or gram[-1] in STOPWORDS:
                continue
            if len(expressive_words(" ".join(gram))) < 2:
                continue
            phrase = " ".join(gram)
            if phrase in BOILERPLATE or any(b in phrase for b in BOILERPLATE):
                continue
            out.add(phrase)
    return out


def maximal_phrases(phrases: set[str]) -> list[str]:
    """Quita frases contenidas en otras más largas."""
    ordered = sorted(phrases, key=len, reverse=True)
    kept: list[str] = []
    for p in ordered:
        if not any(f" {p} " in f" {k} " for k in kept):
            kept.append(p)
    return kept


def highlight(text: str, phrases: list[str], terms: set[str]) -> list[tuple[str, bool]]:
    """Segmentos (texto, resaltado) del texto original: frases y términos compartidos.

    Trabaja sobre los tokens normalizados para ubicar las coincidencias y devuelve el
    texto original sin modificar (el escape HTML lo hace la plantilla).
    """
    spans = [(m.start(), m.end(), norm(m.group(0))) for m in _WORD_RE.finditer(text)]
    if not spans:
        return [(text, False)]
    marked = [False] * len(spans)
    words = [s[2] for s in spans]
    for phrase in phrases:
        parts = phrase.split()
        for i in range(len(words) - len(parts) + 1):
            if words[i:i + len(parts)] == parts:
                for j in range(i, i + len(parts)):
                    marked[j] = True
    for i, w in enumerate(words):
        if w in terms or light_stem(w) in terms:
            marked[i] = True
    segments: list[tuple[str, bool]] = []
    pos = 0
    i = 0
    while i < len(spans):
        if not marked[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(spans) and marked[j + 1]:
            j += 1
        start, end = spans[i][0], spans[j][1]
        if start > pos:
            segments.append((text[pos:start], False))
        segments.append((text[start:end], True))
        pos = end
        i = j + 1
    if pos < len(text):
        segments.append((text[pos:], False))
    return segments
