"""Relaciones entre artículos por reglas léxicas explícitas.

Cada relación guarda puntajes parciales, términos y frases compartidas, reglas aplicadas y
diferencia temporal solo cuando ambas notas tienen hora. Los puntajes miden coincidencia
léxica; no son probabilidades de coordinación ni prueban que el mensaje sea el mismo.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from radar.analysis import textproc as tp
from radar.analysis.vectorize import TfIdf, Vector, cosine, top_shared

# Umbrales (versión lexico-1.0). Cambiarlos implica subir ALGORITHM_VERSION.
CHAR_NEAR_IDENTICAL = 0.85
CHAR_POLARITY_CHECK = 0.70
WORD_POLARITY_CHECK = 0.60
WORD_SAME_EVENT = 0.30
WORD_SAME_TOPIC = 0.18
WORD_PHRASE_MIN = 0.15
WORD_REAPPEARANCE = 0.35
WORD_MENTION = 0.25
SAME_EVENT_MAX_HOURS = 48
MENTION_MAX_HOURS = 72
MAX_CANDIDATES_PER_DOC = 40

RULES = {
    "R1": "Titulares idénticos tras normalizar (mayúsculas, tildes, espacios), mismas cifras y misma polaridad.",
    "R2": f"Titulares casi idénticos: similitud de caracteres ≥ {CHAR_NEAR_IDENTICAL}, mismas cifras y misma polaridad.",
    "R3": f"Expresión distintiva compartida: frase (o cita textual) poco frecuente en el período, con al menos dos palabras de contenido que no son nombres propios ni fechas, y similitud de palabras ≥ {WORD_PHRASE_MIN}.",
    "R4": f"Mismo hecho (candidato): similitud de palabras ≥ {WORD_SAME_EVENT}, al menos dos coincidencias distintivas (entidades o términos poco frecuentes) y publicación/detección a ≤ {SAME_EVENT_MAX_HOURS} h.",
    "R5": f"Mismo tema: comparten un tema de seguimiento, una entidad distintiva y similitud de palabras ≥ {WORD_SAME_TOPIC}.",
    "R6": f"Reaparición: nota reciente parecida (≥ {WORD_REAPPEARANCE}) a otra de entre 3 y 90 días antes, con coincidencias distintivas.",
    "R7": "Referencia explícita: una nota enlaza a la otra, o menciona al otro medio y trata lo mismo.",
    "RP": "Polaridad: titulares parecidos donde uno niega o dice que algo dejó de ocurrir y el otro no. No es el mismo mensaje.",
    "RN": "Cifras distintas: titulares parecidos con números diferentes; no se consideran idénticos.",
}

HEADLINE_TYPES = ("titular_identico", "titular_casi_identico")
GROUPING_TYPES = ("titular_identico", "titular_casi_identico", "expresion_compartida", "mismo_hecho")

TYPE_LABELS = {
    "titular_identico": "Titular idéntico",
    "titular_casi_identico": "Titular casi idéntico",
    "expresion_compartida": "Expresión distintiva compartida",
    "mismo_hecho": "Posible mismo hecho (candidato)",
    "mismo_tema": "Mismo tema y entidades",
    "reaparicion": "Reaparición de tema",
    "referencia_explicita": "Referencia explícita",
    "afirmaciones_distintas": "Afirmaciones distintas (no es el mismo mensaje)",
}


@dataclass
class Doc:
    id: int
    media_id: int
    title: str
    subtitle: str | None
    canonical_url: str
    is_demo: bool = False
    is_syndicated: bool = False
    origin_label: str | None = None
    published_at: datetime | None = None
    published_precision: str = "none"
    first_seen_at: datetime | None = None
    recent: bool = True
    topic_ids: set[int] = field(default_factory=set)
    linked_urls: set[str] = field(default_factory=set)  # enlaces a otros medios (canónicos)
    mentioned_media: set[str] = field(default_factory=set)
    media_name: str = ""
    # calculados
    norm_title: str = ""
    word_feats: list[str] = field(default_factory=list)
    word_vec: Vector = field(default_factory=dict)
    char_vec: Vector = field(default_factory=dict)
    ents: set[str] = field(default_factory=set)
    nums: set[str] = field(default_factory=set)
    pol: tp.Polarity = field(default_factory=tp.Polarity)
    phrases: set[str] = field(default_factory=set)
    quotes: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return f"{self.title}. {self.subtitle}" if self.subtitle else self.title

    def time_for_proximity(self) -> tuple[datetime | None, str]:
        if self.published_precision == "datetime" and self.published_at:
            return self.published_at, "publicacion"
        return self.first_seen_at, "deteccion"


@dataclass
class RelationResult:
    a: int
    b: int
    relation_type: str
    score: float
    scores: dict
    evidence: dict
    rules: list[str]
    time_delta_seconds: int | None
    time_note: str


class Corpus:
    """Documentos de la ventana + estadísticas de frecuencia (df) para distinguir lo común."""

    def __init__(self, docs: list[Doc], max_features: int = 20000):
        self.docs = {d.id: d for d in docs}
        n = len(docs)
        self.n = n
        # Entidades conocidas: vistas como nombre propio fuera del inicio del texto.
        # Se suma el apellido de nombres compuestos ("javier milei" → "milei").
        known: set[str] = set()
        for d in docs:
            seen = tp.entities(d.title) | (tp.entities(d.subtitle) if d.subtitle else set())
            for e in seen:
                known.add(e)
                if " " in e:
                    known.add(e.split()[-1])
        for d in docs:
            d.norm_title = " ".join(tp.raw_tokens(d.title))
            d.word_feats = tp.word_features(d.text)
            d.ents = tp.entities(d.title, known) | (tp.entities(d.subtitle, known) if d.subtitle else set())
            d.nums = tp.numbers(d.title)
            d.pol = tp.polarity(d.title)
            d.phrases = tp.phrase_ngrams(d.text)
            d.quotes = tp.quotes(d.text)
        self.word = TfIdf(max_features=max_features).fit(d.word_feats for d in docs)
        self.char = TfIdf(max_features=max_features * 2).fit(tp.char_ngrams(d.title) for d in docs)
        for d in docs:
            d.word_vec = self.word.transform(d.word_feats)
            d.char_vec = self.char.transform(tp.char_ngrams(d.title))
        self.ent_df = Counter(e for d in docs for e in d.ents)
        self.phrase_df = Counter(p for d in docs for p in d.phrases)
        self.term_df = self.word.df
        # Umbrales de "poco frecuente" relativos al tamaño del corpus.
        self.rare_entity_max = max(3, int(0.06 * n))
        self.rare_term_max = max(3, int(0.05 * n))
        self.rare_phrase_max = max(3, int(0.01 * n))
        self.index_term_max = max(4, int(0.08 * n))

    def distinctive_entities(self, ents: set[str]) -> set[str]:
        return {e for e in ents if self.ent_df[e] <= self.rare_entity_max}

    def distinctive_terms(self, terms: list[str]) -> list[str]:
        return [t for t in terms if self.term_df.get(t, 0) <= self.rare_term_max]

    # --- candidatos ---------------------------------------------------------------

    def candidate_pairs(self) -> set[tuple[int, int]]:
        """Pares que comparten algún término poco frecuente (no se compara todo contra todo)."""
        index: dict[str, list[int]] = defaultdict(list)
        for d in self.docs.values():
            for t in set(d.word_feats):
                if 0 < self.term_df.get(t, 0) <= self.index_term_max:
                    index[t].append(d.id)
            for e in d.ents:
                if self.ent_df[e] <= self.index_term_max:
                    index["ent:" + e].append(d.id)
        shared: dict[int, Counter[int]] = defaultdict(Counter)
        for ids in index.values():
            if len(ids) < 2:
                continue
            for i in ids:
                for j in ids:
                    if i != j:
                        shared[i][j] += 1
        pairs: set[tuple[int, int]] = set()
        for i, counter in shared.items():
            for j, _ in counter.most_common(MAX_CANDIDATES_PER_DOC):
                a, b = (i, j) if i < j else (j, i)
                da, db = self.docs[a], self.docs[b]
                if da.is_demo != db.is_demo:
                    continue
                if not (da.recent or db.recent):
                    continue  # dos antecedentes entre sí no interesan
                if da.media_id == db.media_id and da.recent and db.recent:
                    continue  # mismo medio en la ventana reciente: no es coincidencia entre medios
                pairs.add((a, b))
        return pairs

    # --- evaluación de un par ---------------------------------------------------------

    def evaluate(self, a: Doc, b: Doc) -> list[RelationResult]:
        if a.id > b.id:
            a, b = b, a
        char_sim = cosine(a.char_vec, b.char_vec)
        word_sim = cosine(a.word_vec, b.word_vec)
        shared_ents = sorted(a.ents & b.ents)
        dist_ents = sorted(self.distinctive_entities(set(shared_ents)))
        ent_tokens = {tok for e in shared_ents for tok in tp.content_tokens(e)}
        shared_terms = [t for t in top_shared(a.word_vec, b.word_vec, 12) if "_" not in t]
        dist_terms = [t for t in self.distinctive_terms(shared_terms)
                      if t not in ent_tokens and not t[0].isdigit()]
        name_tokens = {w for e in a.ents | b.ents for w in tp.raw_tokens(e)}
        common_phrases = {p for p in a.phrases & b.phrases if self.phrase_df[p] <= self.rare_phrase_max}
        phrases = tp.maximal_phrases(common_phrases)
        # Una expresión distintiva no puede ser solo un nombre propio, una fecha o una fórmula.
        # Con solo dos palabras de contenido ("boca de urna") se exige que sea muy rara.
        strong_phrases = [p for p in phrases
                          if (n_expr := len(tp.expressive_words(p, name_tokens))) >= 3
                          or (n_expr == 2 and self.phrase_df[p] <= 3)]
        shared_quotes = [q for q in a.quotes for r in b.quotes
                         if (q == r or q in r or r in q)
                         and len(tp.expressive_words(min(q, r, key=len), name_tokens)) >= 2]
        conflict = tp.polarity_conflict(a.pol, b.pol)
        nums_differ = bool(a.nums or b.nums) and a.nums != b.nums

        delta, time_note, hours = self._time(a, b)
        scores = {
            "caracteres_titular": round(char_sim, 3),
            "palabras": round(word_sim, 3),
            "entidades_distintivas": len(dist_ents),
            "terminos_distintivos": len(dist_terms),
            "frases_compartidas": len(strong_phrases) + len(shared_quotes),
        }
        combined = round(min(1.0, 0.35 * char_sim + 0.45 * word_sim
                             + 0.1 * min(1.0, len(dist_ents) / 3)
                             + 0.1 * (1.0 if strong_phrases or shared_quotes else 0.0)), 3)
        evidence = {
            "terminos": shared_terms[:10],
            "terminos_distintivos": dist_terms[:10],
            "entidades": shared_ents[:10],
            "entidades_distintivas": dist_ents[:10],
            "frases": (shared_quotes + strong_phrases)[:6],
            "cifras_a": sorted(a.nums), "cifras_b": sorted(b.nums),
            "polaridad_a": a.pol.markers, "polaridad_b": b.pol.markers,
            "enfoque": "sin verificar (el análisis es léxico, no semántico)",
            "notas": [],
        }
        rules: list[str] = []
        results: list[RelationResult] = []

        def make(rtype: str, rule_list: list[str]) -> RelationResult:
            return RelationResult(a.id, b.id, rtype, combined, scores, evidence, rule_list,
                                  delta, time_note)

        # Referencia explícita (independiente del tipo principal).
        ref = self._explicit_reference(a, b, word_sim, hours)
        if ref:
            ev = dict(evidence, referencia=ref)
            results.append(RelationResult(a.id, b.id, "referencia_explicita",
                                          max(combined, 0.9 if ref["tipo"] == "enlace" else combined),
                                          scores, ev, ["R7"], delta, time_note))

        same_media = a.media_id == b.media_id
        if not (a.recent and b.recent):
            # Antecedente (3-90 días): solo reaparición.
            if word_sim >= WORD_REAPPEARANCE and (len(dist_ents) + len(dist_terms)) >= 2:
                results.append(make("reaparicion", ["R6"]))
            return results
        if same_media:
            return results

        very_similar = char_sim >= CHAR_POLARITY_CHECK or word_sim >= WORD_POLARITY_CHECK
        if very_similar and conflict:
            evidence["notas"].append(f"Afirmaciones distintas: {conflict}.")
            results.append(make("afirmaciones_distintas", ["RP"]))
            return results
        if nums_differ and very_similar:
            evidence["notas"].append("Cifras distintas en los titulares.")
            rules.append("RN")
        if not conflict and not nums_differ and a.norm_title == b.norm_title and a.norm_title:
            results.append(make("titular_identico", ["R1"]))
            return results
        if not conflict and not nums_differ and char_sim >= CHAR_NEAR_IDENTICAL:
            results.append(make("titular_casi_identico", ["R2"] + rules))
            return results
        if (strong_phrases or shared_quotes) and word_sim >= WORD_PHRASE_MIN:
            if conflict:
                evidence["notas"].append(f"Comparten una expresión, pero {conflict}.")
            results.append(make("expresion_compartida", ["R3"] + rules))
            return results
        distinctive_hits = len(dist_ents) + len(dist_terms)
        within = hours is None or hours <= SAME_EVENT_MAX_HOURS
        if word_sim >= WORD_SAME_EVENT and distinctive_hits >= 2 and within:
            if hours is None:
                evidence["notas"].append("Sin datos de hora para evaluar la cercanía temporal.")
            results.append(make("mismo_hecho", ["R4"] + rules))
            return results
        if (word_sim >= WORD_SAME_TOPIC and a.topic_ids & b.topic_ids and dist_ents
                and len(shared_terms) >= 2):
            evidence["temas"] = sorted(a.topic_ids & b.topic_ids)
            results.append(make("mismo_tema", ["R5"] + rules))
        return results

    @staticmethod
    def _time(a: Doc, b: Doc) -> tuple[int | None, str, float | None]:
        if (a.published_precision == "datetime" and b.published_precision == "datetime"
                and a.published_at and b.published_at):
            delta = int((b.published_at - a.published_at).total_seconds())
            return delta, "Según la hora de publicación declarada por cada medio.", abs(delta) / 3600
        ta, _ = a.time_for_proximity()
        tb, _ = b.time_for_proximity()
        hours = abs((tb - ta).total_seconds()) / 3600 if ta and tb else None
        return None, ("Al menos una nota no tiene hora de publicación: no se establece orden "
                      "entre ellas (la cercanía se estima con la detección)."), hours

    @staticmethod
    def _explicit_reference(a: Doc, b: Doc, word_sim: float, hours: float | None) -> dict | None:
        if a.media_id == b.media_id:
            return None
        for x, y in ((a, b), (b, a)):
            if y.canonical_url in x.linked_urls:
                return {"tipo": "enlace", "desde": x.id, "hacia": y.id, "url": y.canonical_url}
        for x, y in ((a, b), (b, a)):
            if y.media_name in x.mentioned_media and word_sim >= WORD_MENTION and (
                    hours is None or hours <= MENTION_MAX_HOURS):
                return {"tipo": "mencion", "desde": x.id, "medio": y.media_name}
        return None


def find_relations(corpus: Corpus) -> list[RelationResult]:
    out: list[RelationResult] = []
    for a_id, b_id in sorted(corpus.candidate_pairs()):
        out.extend(corpus.evaluate(corpus.docs[a_id], corpus.docs[b_id]))
    return out
