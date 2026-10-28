"""Muestra de pares no relacionados para auditar posibles omisiones del motor."""

from __future__ import annotations

import json
import math
import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from radar.analysis import ALGORITHM_VERSION
from radar.analysis.textproc import content_tokens
from radar.models import AppSetting, Article, ArticleRelation, OmissionLabel, User
from radar.timeutil import utcnow

MAX_ARTICLES = 300
RECENT_HOURS = 72
PAIR_HOURS = 24
TARGET_PER_BUCKET = 50
LABELS = ("omitida", "no_relacionada", "dudosa")
BUCKETS = ("alta", "media", "baja")
SAMPLE_VERSION = f"{ALGORITHM_VERSION}+omisiones-1.0"


def sample_key(include_demo: bool = False) -> str:
    return f"{SAMPLE_VERSION}+demo" if include_demo else SAMPLE_VERSION


@dataclass
class Candidate:
    article_a: Article
    article_b: Article
    bucket: str
    similarity: float
    population: int
    time_basis: str


@dataclass
class BucketResult:
    omitted: int = 0
    unrelated: int = 0

    @property
    def n(self) -> int:
        return self.omitted + self.unrelated

    @property
    def rate(self) -> float | None:
        return self.omitted / self.n if self.n else None


@dataclass
class Report:
    by_bucket: dict[str, BucketResult] = field(default_factory=dict)
    weighted_rate: float | None = None
    weighted_interval: tuple[float, float] | None = None
    weighted_pending: int = 0
    unsure: int = 0
    disagreements: int = 0
    agreement: float | None = None
    kappa: float | None = None


def _similarity(tokens_a: set[str], tokens_b: set[str]) -> float:
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b) if tokens_a and tokens_b else 0.0


def _bucket(value: float) -> str:
    if value >= 0.30:
        return "alta"
    if value >= 0.12:
        return "media"
    return "baja"


def _time_distance(article_a: Article, article_b: Article) -> tuple[float, str]:
    if (article_a.published_precision == "datetime" and article_a.published_at is not None
            and article_b.published_precision == "datetime" and article_b.published_at is not None):
        return abs((article_a.published_at - article_b.published_at).total_seconds()), "publicación"
    return abs((article_a.first_seen_at - article_b.first_seen_at).total_seconds()), "detección"


def _universe(db: Session, include_demo: bool, sample_end: datetime,
              relation_version: str = ALGORITHM_VERSION) -> tuple[list[Article], list[tuple]]:
    stmt = (select(Article)
            .options(selectinload(Article.media))
            .where(Article.first_seen_at >= sample_end - timedelta(hours=RECENT_HOURS),
                   Article.first_seen_at <= sample_end,
                   Article.hidden_at.is_(None), Article.is_syndicated.is_(False))
            .order_by(Article.first_seen_at.desc()).limit(MAX_ARTICLES))
    if not include_demo:
        stmt = stmt.where(Article.is_demo.is_(False))
    articles = db.scalars(stmt).all()
    ids = [article.id for article in articles]
    related = {tuple(sorted((a, b))) for a, b in db.execute(
        select(ArticleRelation.article_a_id, ArticleRelation.article_b_id)
        .where(ArticleRelation.algorithm_version == relation_version,
               ArticleRelation.article_a_id.in_(ids), ArticleRelation.article_b_id.in_(ids)))}
    tokens = {article.id: set(content_tokens(f"{article.title} {article.subtitle or ''}"))
              for article in articles}
    pairs = []
    for index, article_a in enumerate(articles):
        for article_b in articles[index + 1:]:
            if article_a.media_id == article_b.media_id:
                continue
            if tuple(sorted((article_a.id, article_b.id))) in related:
                continue
            distance, time_basis = _time_distance(article_a, article_b)
            if distance > PAIR_HOURS * 3600:
                continue
            similarity = _similarity(tokens[article_a.id], tokens[article_b.id])
            pairs.append((article_a, article_b, _bucket(similarity), similarity, time_basis))
    return articles, pairs


def _frame(db: Session, sample_version: str, include_demo: bool,
           relation_version: str) -> tuple[datetime, list[tuple], Counter]:
    key = f"omission_frame:{sample_version}"
    row = db.get(AppSetting, key)
    sample_end = None
    stored_populations = Counter()
    if row is not None and row.value:
        try:
            data = json.loads(row.value)
            sample_end = datetime.fromisoformat(data["sample_end"])
            stored_populations = Counter(data.get("populations", {}))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            sample_end = None
    sample_end = sample_end or utcnow()
    _articles, pairs = _universe(db, include_demo, sample_end, relation_version)
    populations = stored_populations or Counter(
        bucket for _a, _b, bucket, _similarity_value, _basis in pairs)
    if row is None or not stored_populations:
        value = json.dumps({
            "sample_end": sample_end.isoformat(timespec="seconds"),
            "populations": dict(populations),
        }, ensure_ascii=False)
        if row is None:
            db.add(AppSetting(key=key, value=value))
        else:
            row.value = value
    return sample_end, pairs, populations


def _stored_populations(db: Session, sample_version: str) -> Counter:
    row = db.get(AppSetting, f"omission_frame:{sample_version}")
    if row is None or not row.value:
        return Counter()
    try:
        return Counter(json.loads(row.value).get("populations", {}))
    except (TypeError, ValueError, json.JSONDecodeError):
        return Counter()


def next_candidate(db: Session, user: User, include_demo: bool = False,
                   sample_version: str = SAMPLE_VERSION,
                   relation_version: str = ALGORITHM_VERSION,
                   rng: random.Random | None = None) -> Candidate | None:
    rng = rng or random.Random()
    sample_version = sample_key(include_demo) if sample_version == SAMPLE_VERSION else sample_version
    _sample_end, pairs, populations = _frame(db, sample_version, include_demo,
                                             relation_version)
    done = {(row.article_a_id, row.article_b_id) for row in db.scalars(
        select(OmissionLabel).where(OmissionLabel.user_id == user.id,
                                    OmissionLabel.algorithm_version == sample_version))}
    completed = Counter(row.bucket for row in db.scalars(
        select(OmissionLabel).where(OmissionLabel.user_id == user.id,
                                    OmissionLabel.algorithm_version == sample_version)))
    available = defaultdict(list)
    for article_a, article_b, bucket, similarity, time_basis in pairs:
        pair = tuple(sorted((article_a.id, article_b.id)))
        if pair not in done and completed[bucket] < TARGET_PER_BUCKET:
            available[bucket].append((article_a, article_b, similarity, time_basis))
    if not available:
        return None
    least = min(completed[bucket] for bucket in available)
    bucket = rng.choice(sorted(
        bucket for bucket in available if completed[bucket] == least))
    article_a, article_b, similarity, time_basis = rng.choice(available[bucket])
    if article_a.id > article_b.id:
        article_a, article_b = article_b, article_a
    return Candidate(article_a, article_b, bucket, similarity, populations[bucket], time_basis)


def save_label(db: Session, article_a_id: int, article_b_id: int, user: User, label: str,
               include_demo: bool = False, sample_version: str = SAMPLE_VERSION,
               relation_version: str = ALGORITHM_VERSION) -> OmissionLabel:
    if label not in LABELS:
        raise ValueError("Etiqueta desconocida.")
    sample_version = sample_key(include_demo) if sample_version == SAMPLE_VERSION else sample_version
    pair = tuple(sorted((article_a_id, article_b_id)))
    sample_end, pairs, populations = _frame(db, sample_version, include_demo,
                                            relation_version)
    match = next((item for item in pairs
                  if tuple(sorted((item[0].id, item[1].id))) == pair), None)
    if match is None:
        raise ValueError("El par ya no pertenece a la muestra vigente.")
    article_a, article_b, bucket, similarity, _time_basis = match
    row = db.scalar(select(OmissionLabel).where(
        OmissionLabel.article_a_id == pair[0], OmissionLabel.article_b_id == pair[1],
        OmissionLabel.user_id == user.id,
        OmissionLabel.algorithm_version == sample_version))
    if row is None:
        row = OmissionLabel(article_a_id=pair[0], article_b_id=pair[1], user_id=user.id,
                            algorithm_version=sample_version, created_at=sample_end)
        db.add(row)
    row.label = label
    row.bucket = bucket
    row.similarity = similarity
    row.sample_population = max(1, populations[bucket])
    db.flush()
    return row


def report(db: Session, sample_version: str = SAMPLE_VERSION,
           include_demo: bool = False) -> Report:
    sample_version = sample_key(include_demo) if sample_version == SAMPLE_VERSION else sample_version
    result = Report()
    votes = defaultdict(list)
    meta = {}
    for row in db.scalars(select(OmissionLabel).where(
            OmissionLabel.algorithm_version == sample_version)):
        key = (row.article_a_id, row.article_b_id)
        votes[key].append(row.label)
        previous = meta.get(key)
        meta[key] = (row.bucket, max(row.sample_population or 1,
                                     previous[1] if previous else 0))
    outcomes = defaultdict(list)
    populations = dict(_stored_populations(db, sample_version))
    pair_agreements = []
    all_decisive = []
    for key, labels in votes.items():
        decisive_labels = [label for label in labels if label != "dudosa"]
        decisive = Counter(decisive_labels)
        if len(decisive_labels) >= 2:
            all_decisive.extend(decisive_labels)
        for index, first in enumerate(decisive_labels):
            pair_agreements.extend(first == second for second in decisive_labels[index + 1:])
        if decisive["omitida"] and decisive["no_relacionada"]:
            result.disagreements += 1
        if decisive["omitida"] == decisive["no_relacionada"]:
            result.unsure += 1
            continue
        omitted = decisive["omitida"] > decisive["no_relacionada"]
        bucket, population = meta[key]
        cell = result.by_bucket.setdefault(bucket, BucketResult())
        if omitted:
            cell.omitted += 1
        else:
            cell.unrelated += 1
        outcomes[bucket].append(omitted)
        populations[bucket] = max(populations.get(bucket, 0), population, len(outcomes[bucket]))
    result.weighted_pending = len({bucket for bucket, population in populations.items()
                                   if population} - set(outcomes))
    total_population = sum(populations.values())
    if total_population and not result.weighted_pending:
        result.weighted_rate = sum(populations[bucket] * sum(values) / len(values)
                                   for bucket, values in outcomes.items()) / total_population
        if all(len(values) >= 2 for values in outcomes.values()):
            variance = 0.0
            for bucket, values in outcomes.items():
                population, sample = populations[bucket], len(values)
                proportion = sum(values) / sample
                sample_variance = sample * proportion * (1 - proportion) / (sample - 1)
                finite = (population - sample) / (population - 1) if population > 1 else 0.0
                variance += (population / total_population) ** 2 * finite * sample_variance / sample
            margin = 1.96 * math.sqrt(variance)
            result.weighted_interval = (max(0.0, result.weighted_rate - margin),
                                        min(1.0, result.weighted_rate + margin))
    if pair_agreements:
        result.agreement = sum(pair_agreements) / len(pair_agreements)
        omitted_share = sum(label == "omitida" for label in all_decisive) / len(all_decisive)
        expected = omitted_share * omitted_share + (1 - omitted_share) * (1 - omitted_share)
        result.kappa = ((result.agreement - expected) / (1 - expected)
                        if expected < 1 else None)
    result.by_bucket = {bucket: result.by_bucket[bucket]
                        for bucket in BUCKETS if bucket in result.by_bucket}
    return result
