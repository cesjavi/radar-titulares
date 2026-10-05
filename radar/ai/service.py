"""Orquestación del análisis con IA: preselección, caché, límites, circuit breaker y registro."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from radar.ai import PROMPT_VERSION
from radar.ai.config import AiConfig, load_config
from radar.ai.prompt import SYSTEM, build_input
from radar.ai.providers import (
    Provider,
    ProviderError,
    ProviderQuotaExceeded,
    ProviderRateLimited,
    make_provider,
    providers_status,
)
from radar.ai.schema import SCHEMA, ValidationFailed, derive_flags, parse_and_validate
from radar.models import (
    AiAnalysis,
    AiUsage,
    Alert,
    AppSetting,
    Article,
    ArticleRelation,
    StoryGroupMember,
)
from radar.timeutil import LOCAL_TZ, to_local, utcnow

log = logging.getLogger("radar.ai")

# Solo se envían relaciones ya detectadas por el motor léxico, de estos tipos.
CANDIDATE_TYPES = ("mismo_hecho", "expresion_compartida", "titular_casi_identico",
                   "afirmaciones_distintas")
RECENT_HOURS = 72
BREAKER_UNTIL = "ai_breaker_until"
BREAKER_FAILS = "ai_breaker_failures"
DISABLED_DAY = "ai_disabled_day"


@dataclass
class AiRunSummary:
    enabled: bool = False
    reason: str | None = None
    candidates: int = 0
    cached: int = 0
    ok: int = 0
    invalid: int = 0
    errors: int = 0
    calls: int = 0
    stopped: str | None = None
    details: list = field(default_factory=list)


def _today():
    return utcnow().replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ).date()


def _setting(db: Session, key: str) -> str | None:
    row = db.get(AppSetting, key)
    return row.value if row else None


def _set(db: Session, key: str, value: str) -> None:
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value


# --- límites ------------------------------------------------------------------------------


def usage_today(db: Session) -> dict:
    day = _today()
    row = db.execute(select(
        func.count(AiUsage.id),
        func.coalesce(func.sum(AiUsage.input_tokens + AiUsage.output_tokens), 0),
        func.sum(AiUsage.estimated_cost_usd),
    ).where(AiUsage.day == day)).one()
    return {"day": day, "requests": row[0] or 0, "tokens": int(row[1] or 0),
            "estimated_cost_usd": round(row[2], 4) if row[2] is not None else None}


def blocked_reason(db: Session, cfg: AiConfig) -> str | None:
    """Motivo por el que no se deben hacer llamadas ahora (o None)."""
    if not cfg.enabled:
        return "IA desactivada (RADAR_AI_ENABLED)"
    problems = providers_status(cfg)
    if problems:
        return "Ningún proveedor listo: " + "; ".join(problems)
    if _setting(db, DISABLED_DAY) == str(_today()):
        return "Cuota del proveedor agotada: llamadas desactivadas hasta mañana"
    until = _setting(db, BREAKER_UNTIL)
    if until:
        if datetime.fromisoformat(until) > utcnow():
            return f"Circuit breaker abierto hasta {to_local(datetime.fromisoformat(until)):%d/%m %H:%M}"
    used = usage_today(db)
    if cfg.daily_max_requests and used["requests"] >= cfg.daily_max_requests:
        return f"Límite diario de solicitudes alcanzado ({cfg.daily_max_requests})"
    if cfg.daily_max_tokens and used["tokens"] >= cfg.daily_max_tokens:
        return f"Límite diario de tokens alcanzado ({cfg.daily_max_tokens})"
    if cfg.daily_budget_usd is not None and cfg.has_prices and \
            (used["estimated_cost_usd"] or 0) >= cfg.daily_budget_usd:
        return f"Presupuesto diario estimado alcanzado (USD {cfg.daily_budget_usd})"
    return None


def _record_failure(db: Session, cfg: AiConfig) -> None:
    fails = int(_setting(db, BREAKER_FAILS) or 0) + 1
    _set(db, BREAKER_FAILS, str(fails))
    if fails >= cfg.breaker_failures:
        _set(db, BREAKER_UNTIL, (utcnow() + timedelta(minutes=cfg.breaker_minutes)).isoformat())
        _set(db, BREAKER_FAILS, "0")
        log.warning("Circuit breaker de IA abierto por %s minutos", cfg.breaker_minutes)


def _record_success(db: Session) -> None:
    _set(db, BREAKER_FAILS, "0")


def _cost(cfg: AiConfig, input_tokens: int, output_tokens: int) -> float | None:
    if not cfg.has_prices:
        return None
    return round(input_tokens / 1e6 * cfg.price_input_mtok + output_tokens / 1e6 * cfg.price_output_mtok, 6)


# --- preparación ----------------------------------------------------------------------------


def _pub(a: Article) -> str:
    if a.published_precision == "datetime" and a.published_at:
        return to_local(a.published_at).strftime("%d/%m/%Y %H:%M")
    if a.published_date:
        return a.published_date.strftime("%d/%m/%Y") + " (sin hora)"
    return "sin dato"


def _article_payload(a: Article) -> dict:
    return {"media": a.media.name, "author": a.author, "title": a.title, "subtitle": a.subtitle,
            "body": a.body_text, "published": _pub(a)}


def cache_key(cfg: AiConfig, a: Article, b: Article) -> str:
    parts = [PROMPT_VERSION, cfg.models_label, str(cfg.max_input_chars)]
    for art in sorted((a, b), key=lambda x: x.id):
        parts += [str(art.id), art.content_hash or "", (art.body_text or "")[:20000]]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def select_candidates(db: Session, limit: int) -> list[ArticleRelation]:
    """Relaciones preseleccionadas: recientes, de tipos relevantes, priorizando las que
    forman parte de alertas pendientes. Nunca el histórico completo."""
    since = utcnow() - timedelta(hours=RECENT_HOURS)
    rels = db.scalars(
        select(ArticleRelation)
        .options(selectinload(ArticleRelation.article_a).selectinload(Article.media),
                 selectinload(ArticleRelation.article_b).selectinload(Article.media))
        .where(ArticleRelation.relation_type.in_(CANDIDATE_TYPES),
               ArticleRelation.review_status != "rechazada",
               func.coalesce(ArticleRelation.updated_at, ArticleRelation.created_at) >= since)
        .order_by(ArticleRelation.score.desc()).limit(limit * 10)).all()
    alert_articles = set(db.scalars(
        select(StoryGroupMember.article_id).join(Alert, Alert.group_id == StoryGroupMember.group_id)
        .where(Alert.status == "pendiente", StoryGroupMember.status == "activo")))
    rels = [r for r in rels if not (r.article_a.is_demo or r.article_b.is_demo)]
    rels.sort(key=lambda r: (not ({r.article_a_id, r.article_b_id} <= alert_articles), -(r.score or 0)))
    return rels


# --- ejecución ----------------------------------------------------------------------------


def analyze_relation(db: Session, rel: ArticleRelation, cfg: AiConfig, provider: Provider,
                     use_cache: bool = True) -> AiAnalysis:
    a, b = rel.article_a, rel.article_b
    key = cache_key(cfg, a, b)
    if use_cache:
        cached = db.scalar(select(AiAnalysis).where(AiAnalysis.cache_key == key,
                                                    AiAnalysis.status == "ok"))
        if cached is not None:
            if cached.relation_id is None:
                cached.relation_id = rel.id
            return cached

    user, sources, chars = build_input(_article_payload(a), _article_payload(b), cfg.max_input_chars)
    analysis = AiAnalysis(cache_key=key, relation_id=rel.id, article_a_id=a.id, article_b_id=b.id,
                          provider=provider.name, model=provider.model,
                          prompt_version=PROMPT_VERSION, status="error", input_chars=chars,
                          created_at=utcnow())
    db.add(analysis)
    db.flush()

    attempt, result, last_exc = 0, None, None
    while True:
        attempt += 1
        try:
            result = provider.complete(SYSTEM, user, SCHEMA)
            break
        except ProviderError as exc:
            last_exc = exc
            failures = getattr(exc, "failures", None) or [(provider.name, exc.kind, str(exc))]
            for name, kind, msg in failures:
                db.add(AiUsage(day=_today(), provider=name, model=provider.model,
                               status=kind, error=msg[:500], analysis_id=analysis.id))
            if isinstance(exc, ProviderQuotaExceeded):
                _set(db, DISABLED_DAY, str(_today()))
                break
            if not exc.retryable or attempt > cfg.max_retries:
                break
            wait = exc.retry_after if isinstance(exc, ProviderRateLimited) and exc.retry_after else 2 ** attempt
            time.sleep(min(wait, 30) if not getattr(provider, "no_sleep", False) else 0)

    if result is None:
        analysis.status = "rechazado" if getattr(last_exc, "kind", "") == "rechazo" else "error"
        analysis.errors = json.dumps([str(last_exc)], ensure_ascii=False)
        _record_failure(db, cfg)
        return analysis

    used_provider = result.extra.get("provider", provider.name)
    for name, kind, msg in result.extra.get("fallos_previos", []):
        db.add(AiUsage(day=_today(), provider=name, model=provider.model, status=kind,
                       error=msg[:500], analysis_id=analysis.id))
    analysis.provider = used_provider
    db.add(AiUsage(day=_today(), provider=used_provider, model=result.model or provider.model,
                   status="ok", input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                   cache_read_tokens=result.cache_read_tokens, cache_write_tokens=result.cache_write_tokens,
                   estimated_cost_usd=_cost(cfg, result.input_tokens, result.output_tokens),
                   latency_ms=result.latency_ms, request_id=result.request_id, analysis_id=analysis.id))
    analysis.model = result.model or provider.model
    try:
        data = parse_and_validate(result.text, sources)
    except ValidationFailed as exc:
        analysis.status = "invalido"
        analysis.errors = json.dumps(exc.errors[:20], ensure_ascii=False)
        _record_failure(db, cfg)
        return analysis
    pub = {"A": _pub(a), "B": _pub(b)}
    analysis.status = "ok"
    analysis.result = json.dumps(data, ensure_ascii=False)
    analysis.flags = json.dumps(derive_flags(data, pub, sources), ensure_ascii=False)
    _record_success(db)
    return analysis


def run_ai(db: Session, provider: Provider | None = None, limit: int | None = None,
           cfg: AiConfig | None = None) -> AiRunSummary:
    cfg = cfg or load_config()
    summary = AiRunSummary(enabled=cfg.enabled)
    reason = blocked_reason(db, cfg)
    if reason and not cfg.enabled:
        summary.reason = reason
        return summary
    limit = cfg.max_per_run if limit is None else limit
    # Se examinan como máximo `limit` pares (los ya analizados se resuelven desde la caché).
    candidates = select_candidates(db, limit)[:limit]
    pending = []
    for rel in candidates:
        key = cache_key(cfg, rel.article_a, rel.article_b)
        cached = db.scalar(select(AiAnalysis).where(AiAnalysis.cache_key == key,
                                                    AiAnalysis.status == "ok"))
        if cached:
            summary.cached += 1
            if cached.relation_id is None:
                cached.relation_id = rel.id
            continue
        pending.append(rel)
    summary.candidates = len(pending)
    if not pending:
        return summary
    if reason:
        summary.reason = summary.stopped = reason
        return summary
    provider = provider or make_provider(cfg)
    for rel in pending[:limit]:
        reason = blocked_reason(db, cfg)
        if reason:
            summary.stopped = reason
            break
        analysis = analyze_relation(db, rel, cfg, provider, use_cache=False)
        summary.calls += 1
        if analysis.status == "ok":
            summary.ok += 1
        elif analysis.status == "invalido":
            summary.invalid += 1
        else:
            summary.errors += 1
        summary.details.append((rel.id, analysis.status))
        db.flush()
    log.info("IA: %s", summary)
    return summary


def pair_filter(col_a, col_b, x: int, y: int):
    """El par (x, y) en cualquier orden. Portable (min/max de dos argumentos es solo de SQLite)."""
    return or_(and_(col_a == x, col_b == y), and_(col_a == y, col_b == x))


def latest_for_pair(db: Session, a_id: int, b_id: int) -> AiAnalysis | None:
    return db.scalar(select(AiAnalysis).where(
        pair_filter(AiAnalysis.article_a_id, AiAnalysis.article_b_id, a_id, b_id),
        AiAnalysis.status == "ok").order_by(AiAnalysis.created_at.desc()).limit(1))


def supports_focus(analysis: AiAnalysis | None) -> bool:
    """"Enfoque similar respaldado por análisis": mismo hecho, enfoque similar y sin
    discrepancias marcadas."""
    if analysis is None or analysis.status != "ok":
        return False
    data = json.loads(analysis.result or "{}")
    flags = json.loads(analysis.flags or "[]")
    return data.get("mismo_hecho") == "si" and data.get("enfoque") == "similar" and not flags
