"""Medición de la precisión del motor léxico con etiquetas humanas.

Una persona (analizador o administrador) marca relaciones de una muestra estratificada como
correctas o incorrectas para su tipo. Las etiquetas se guardan aparte de la revisión del
administrador (`review_status`), que dispara alertas e IA; las revisiones operativas se muestran
aparte para no sesgar la muestra.

La precisión es de las relaciones detectadas (cuántas están bien), no la exhaustividad: los
pares que el motor no relacionó no entran en esta muestra.
"""

from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased, selectinload

from radar.analysis import ALGORITHM_VERSION
from radar.analysis.relations import TYPE_LABELS
from radar.models import Article, ArticleRelation, RelationLabel, User
from radar.timeutil import utcnow

LABELS = {"correcta": "Correcta", "incorrecta": "Incorrecta", "dudosa": "No sé"}
TARGET_PER_TYPE = 50  # ~150 etiquetas en los tres tipos más comunes: ±7 puntos de margen
SCORE_BUCKETS = ((0.0, 0.4, "< 0,40"), (0.4, 0.6, "0,40–0,59"), (0.6, 0.8, "0,60–0,79"),
                 (0.8, 1.01, "≥ 0,80"))
NO_SCORE = "sin puntaje"

# Qué significa "correcta" para cada tipo: es la pregunta que ve quien etiqueta.
QUESTIONS = {
    "titular_identico": "¿Los dos titulares son iguales?",
    "titular_casi_identico": "¿Los dos titulares son casi iguales (mismo texto con cambios menores)?",
    "expresion_compartida": "¿Comparten una expresión distintiva, y no solo un nombre o una frase hecha?",
    "mismo_hecho": "¿Las dos notas cuentan el mismo hecho?",
    "mismo_tema": "¿Las dos notas tratan el mismo tema?",
    "reaparicion": "¿La nota más nueva retoma el tema de la anterior?",
    "referencia_explicita": "¿Una de las notas cita o menciona a la otra?",
    "afirmaciones_distintas": "¿Hablan de lo mismo pero con afirmaciones distintas u opuestas?",
}


def bucket(score: float | None) -> str:
    if score is None:
        return NO_SCORE
    for lo, hi, name in SCORE_BUCKETS:
        if lo <= score < hi:
            return name
    return SCORE_BUCKETS[-1][2]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """Intervalo de Wilson al 95 % para k aciertos en n casos (sirve con muestras chicas)."""
    if n == 0:
        return None
    p = k / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, mid - half), min(1.0, mid + half)


def _demo_filter(stmt, include_demo: bool):
    if include_demo:
        return stmt
    a, b = aliased(Article), aliased(Article)
    return (stmt.join(a, a.id == ArticleRelation.article_a_id)
            .join(b, b.id == ArticleRelation.article_b_id)
            .where(a.is_demo.is_(False), b.is_demo.is_(False)))


def next_for_labeling(db: Session, user: User, include_demo: bool = False,
                      rng: random.Random | None = None,
                      version: str = ALGORITHM_VERSION) -> ArticleRelation | None:
    """Próxima relación de la muestra para esta persona, del tipo y tramo con menos casos.
    Personas distintas pueden recibir el mismo caso para medir acuerdo entre evaluadores."""
    rng = rng or random.Random()
    labeled = select(RelationLabel.relation_id).where(
        RelationLabel.user_id == user.id, RelationLabel.algorithm_version == version)
    rows = db.execute(_demo_filter(
        select(ArticleRelation.id, ArticleRelation.relation_type, ArticleRelation.score)
        .where(ArticleRelation.algorithm_version == version,
               ArticleRelation.id.not_in(labeled)), include_demo)).all()
    if not rows:
        return None
    done_ids: dict[tuple[str, str], set[int]] = defaultdict(set)
    per_type_ids: dict[str, set[int]] = defaultdict(set)
    for relation_id, rtype, score in db.execute(
            select(RelationLabel.relation_id, RelationLabel.relation_type, RelationLabel.score)
            .where(RelationLabel.algorithm_version == version,
                   RelationLabel.user_id == user.id)):
        done_ids[(rtype, bucket(score))].add(relation_id)
        per_type_ids[rtype].add(relation_id)
    done = Counter({key: len(ids) for key, ids in done_ids.items()})
    per_type = Counter({key: len(ids) for key, ids in per_type_ids.items()})
    population = Counter((rtype, bucket(score)) for _rid, rtype, score in db.execute(_demo_filter(
        select(ArticleRelation.id, ArticleRelation.relation_type, ArticleRelation.score)
        .where(ArticleRelation.algorithm_version == version), include_demo)))
    strata: dict[tuple[str, str], list[int]] = defaultdict(list)
    for rid, rtype, score in rows:
        if per_type[rtype] < TARGET_PER_TYPE:
            strata[(rtype, bucket(score))].append(rid)
    if not strata:
        return None
    fewest = min(done[k] for k in strata)
    key = rng.choice(sorted(k for k in strata if done[k] == fewest))
    rid = rng.choice(strata[key])
    rel = db.scalar(select(ArticleRelation)
                    .options(selectinload(ArticleRelation.article_a).selectinload(Article.media),
                             selectinload(ArticleRelation.article_b).selectinload(Article.media))
                    .where(ArticleRelation.id == rid))
    if rel is not None:
        rel.sample_population = population[key]
    return rel


def save_label(db: Session, rel: ArticleRelation, user: User, label: str) -> RelationLabel:
    """Guarda (o corrige) la etiqueta de esta persona. Copia tipo, puntaje y versión para que la
    medición sobreviva al reprocesamiento y a la retención."""
    if label not in LABELS:
        raise ValueError("Etiqueta desconocida.")
    version = rel.algorithm_version or ALGORITHM_VERSION
    row = db.scalar(select(RelationLabel).where(RelationLabel.relation_id == rel.id,
                                                RelationLabel.user_id == user.id,
                                                RelationLabel.algorithm_version == version))
    if row is None:
        row = RelationLabel(relation_id=rel.id, user_id=user.id)
        db.add(row)
    row.label = label
    row.relation_type = rel.relation_type
    row.score = rel.score
    row.algorithm_version = version
    row.sample_population = max(1, int(getattr(rel, "sample_population", 1)))
    row.created_at = utcnow()
    db.flush()
    return row


@dataclass
class Cell:
    correct: int = 0
    incorrect: int = 0

    @property
    def n(self) -> int:
        return self.correct + self.incorrect

    @property
    def precision(self) -> float | None:
        return self.correct / self.n if self.n else None

    @property
    def interval(self) -> tuple[float, float] | None:
        return wilson(self.correct, self.n)


@dataclass
class Report:
    version: str | None
    by_type: dict[str, Cell] = field(default_factory=dict)
    by_bucket: dict[tuple[str, str], Cell] = field(default_factory=dict)
    total: Cell = field(default_factory=Cell)
    operational: Cell = field(default_factory=Cell)
    weighted_precision: float | None = None
    weighted_interval: tuple[float, float] | None = None
    weighted_pending: int = 0
    agreement: float | None = None
    kappa: float | None = None
    unsure: int = 0  # «no sé» o empates entre personas: no cuentan
    disagreements: int = 0  # relaciones con etiquetas opuestas de distintas personas

    def as_dict(self) -> dict:
        def cell(c: Cell) -> dict:
            iv = c.interval
            return {"n": c.n, "correctas": c.correct, "incorrectas": c.incorrect,
                    "precision": c.precision, "ic95": list(iv) if iv else None}
        return {"version": self.version or "todas", "total": cell(self.total),
                "precision_ponderada": self.weighted_precision,
                "ic95_ponderado": (list(self.weighted_interval)
                                    if self.weighted_interval else None),
                "estratos_pendientes": self.weighted_pending,
                "revisiones_operativas": cell(self.operational),
                "acuerdo": self.agreement, "kappa": self.kappa,
                "por_tipo": {t: cell(c) for t, c in self.by_type.items()},
                "por_tramo": {f"{t} {b}": cell(c) for (t, b), c in self.by_bucket.items()},
                "dudosas": self.unsure, "desacuerdos": self.disagreements}


def precision_report(db: Session, version: str | None = ALGORITHM_VERSION) -> Report:
    """Precisión por tipo y por tramo de puntaje. Cada relación cuenta una vez: si la etiquetaron
    varias personas manda la mayoría (el empate no cuenta). Las revisiones operativas se informan
    aparte porque no salen de la muestra aleatoria."""
    report = Report(version=version)
    votes: dict[int, list[str]] = defaultdict(list)
    meta: dict[int, tuple[str, float | None, int]] = {}
    stmt = select(RelationLabel)
    if version:
        stmt = stmt.where(RelationLabel.algorithm_version == version)
    for row in db.scalars(stmt):
        votes[row.relation_id].append(row.label)
        previous = meta.get(row.relation_id)
        meta[row.relation_id] = (row.relation_type, row.score,
                                 max(row.sample_population or 1,
                                     previous[2] if previous else 0))
    live_population = Counter()
    if version:
        live_population.update((rtype, bucket(score)) for rtype, score in db.execute(_demo_filter(
            select(ArticleRelation.relation_type, ArticleRelation.score)
            .where(ArticleRelation.algorithm_version == version), False)))
    rev = select(ArticleRelation.id, ArticleRelation.relation_type, ArticleRelation.score,
                 ArticleRelation.review_status).where(
        ArticleRelation.review_status.in_(("confirmada", "rechazada")))
    if version:
        rev = rev.where(ArticleRelation.algorithm_version == version)
    for _rid, _rtype, _score, status in db.execute(rev):
        if status == "confirmada":
            report.operational.correct += 1
        else:
            report.operational.incorrect += 1

    outcomes: dict[tuple[str, str], list[bool]] = defaultdict(list)
    populations: dict[tuple[str, str], int] = {}
    pair_agreements = []
    all_decisive = []
    for key, vs in votes.items():
        c = Counter(v for v in vs if v != "dudosa")
        decisive = [v for v in vs if v != "dudosa"]
        if len(decisive) >= 2:
            all_decisive.extend(decisive)
        for i, first in enumerate(decisive):
            pair_agreements.extend(first == second for second in decisive[i + 1:])
        if c["correcta"] and c["incorrecta"]:
            report.disagreements += 1
        if c["correcta"] == c["incorrecta"]:
            report.unsure += 1
            continue
        ok = c["correcta"] > c["incorrecta"]
        rtype, score, population = meta[key]
        stratum = (rtype, bucket(score))
        outcomes[stratum].append(ok)
        populations[stratum] = max(populations.get(stratum, 0), population,
                                    len(outcomes[stratum]))
        for target in (report.by_type.setdefault(rtype, Cell()),
                       report.by_bucket.setdefault((rtype, bucket(score)), Cell()), report.total):
            if ok:
                target.correct += 1
            else:
                target.incorrect += 1
    report.by_type = dict(sorted(report.by_type.items(), key=lambda kv: -kv[1].n))
    order = [b for *_, b in SCORE_BUCKETS] + [NO_SCORE]
    report.by_bucket = dict(sorted(report.by_bucket.items(),
                                   key=lambda kv: (kv[0][0], order.index(kv[0][1]))))
    if live_population:
        report.weighted_pending = len(set(live_population) - set(outcomes))
        for stratum in outcomes:
            populations[stratum] = max(populations[stratum], live_population.get(stratum, 0))
    weighted_total = (sum(populations[stratum] for stratum in outcomes)
                      if not report.weighted_pending else 0)
    if weighted_total:
        report.weighted_precision = sum(
            populations[stratum] * (sum(values) / len(values))
            for stratum, values in outcomes.items()) / weighted_total
        if all(len(values) >= 2 for values in outcomes.values()):
            variance = 0.0
            for stratum, values in outcomes.items():
                population = populations[stratum]
                sample = len(values)
                proportion = sum(values) / sample
                sample_variance = sample * proportion * (1 - proportion) / (sample - 1)
                finite = ((population - sample) / (population - 1)
                          if population > 1 else 0.0)
                weight = population / weighted_total
                variance += weight * weight * finite * sample_variance / sample
            margin = 1.96 * math.sqrt(variance)
            report.weighted_interval = (max(0.0, report.weighted_precision - margin),
                                        min(1.0, report.weighted_precision + margin))
    if pair_agreements:
        report.agreement = sum(pair_agreements) / len(pair_agreements)
        positive = sum(v == "correcta" for v in all_decisive) / len(all_decisive)
        expected = positive * positive + (1 - positive) * (1 - positive)
        report.kappa = ((report.agreement - expected) / (1 - expected)
                        if expected < 1 else None)
    return report


def format_report(report: Report) -> str:
    def pct(c: Cell) -> str:
        if not c.n:
            return "sin datos"
        lo, hi = c.interval
        return f"{c.precision:.0%} (IC 95 %: {lo:.0%}–{hi:.0%}; n={c.n})"

    lines = [f"Precisión del motor léxico (versión {report.version or 'todas'})",
             f"Total: {pct(report.total)}"]
    if report.weighted_precision is not None:
        lines.append(f"Total ponderado por la población de cada estrato: "
                     f"{report.weighted_precision:.0%}")
        if report.weighted_interval:
            lines.append(f"  IC 95 % estratificado: {report.weighted_interval[0]:.0%}–"
                         f"{report.weighted_interval[1]:.0%}")
    for rtype, c in report.by_type.items():
        lines.append(f"  {TYPE_LABELS.get(rtype, rtype)}: {pct(c)}")
        for (t, b), cb in report.by_bucket.items():
            if t == rtype:
                lines.append(f"      puntaje {b}: {pct(cb)}")
    lines.append(f"Sin decidir («no sé» o empate): {report.unsure}; con desacuerdo: "
                 f"{report.disagreements}.")
    lines.append(f"Revisiones operativas (fuera de la muestra): {pct(report.operational)}")
    lines.append("Mide relaciones detectadas (precisión), no las que el motor no encontró.")
    return "\n".join(lines)
