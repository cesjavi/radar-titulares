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
from radar.ai import group as group_ai
from radar.alerts.config import PRIORITY_RANK
from radar.ai.config import AiConfig, load_config
from radar.ai.prompt import SYSTEM, build_input, repair_message
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
    AiGroupAnalysis,
    AiUsage,
    Alert,
    AppSetting,
    Article,
    ArticleRelation,
    StoryGroup,
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


def select_candidates(db: Session, limit: int, min_priority: str = "baja") -> list[ArticleRelation]:
    """Relaciones preseleccionadas: recientes, de tipos relevantes, priorizando las que
    forman parte de alertas pendientes. Nunca el histórico completo.

    Con `min_priority` distinta de "baja" solo entran los pares cuyas dos notas están en
    grupos con alerta pendiente de esa prioridad o mayor."""
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
    if min_priority in PRIORITY_RANK and PRIORITY_RANK[min_priority] > 0:
        wanted = [p for p, rank in PRIORITY_RANK.items() if rank >= PRIORITY_RANK[min_priority]]
        priority_articles = set(db.scalars(
            select(StoryGroupMember.article_id)
            .join(Alert, Alert.group_id == StoryGroupMember.group_id)
            .where(Alert.status == "pendiente", Alert.priority.in_(wanted),
                   StoryGroupMember.status == "activo")))
        rels = [r for r in rels if {r.article_a_id, r.article_b_id} <= priority_articles]
    rels.sort(key=lambda r: (not ({r.article_a_id, r.article_b_id} <= alert_articles), -(r.score or 0)))
    return rels


# --- ejecución ----------------------------------------------------------------------------


def _complete_validated(db: Session, cfg: AiConfig, provider: Provider, analysis, usage_field: str,
                        system: str, user: str, schema: dict, validate) -> dict | None:
    """Llama al proveedor (con reintentos acotados), mide el uso y valida la salida.

    Un solo reintento de validación, con los errores a la vista y solo si los límites lo
    permiten. Si falla, deja el estado y los errores en `analysis` y devuelve None.
    `usage_field` es la columna de AiUsage que apunta a `analysis`."""

    def request(user_text: str):
        attempt, last_exc = 0, None
        while True:
            attempt += 1
            try:
                return provider.complete(system, user_text, schema), None
            except ProviderError as exc:
                last_exc = exc
                failures = getattr(exc, "failures", None) or [(provider.name, exc.kind, str(exc))]
                for name, kind, msg in failures:
                    db.add(AiUsage(day=_today(), provider=name, model=provider.model,
                                   status=kind, error=msg[:500], **{usage_field: analysis.id}))
                if isinstance(exc, ProviderQuotaExceeded):
                    _set(db, DISABLED_DAY, str(_today()))
                    return None, last_exc
                if not exc.retryable or attempt > cfg.max_retries:
                    return None, last_exc
                wait = exc.retry_after if isinstance(exc, ProviderRateLimited) and exc.retry_after else 2 ** attempt
                time.sleep(min(wait, 30) if not getattr(provider, "no_sleep", False) else 0)

    def meter(result) -> None:
        used_provider = result.extra.get("provider", provider.name)
        for name, kind, msg in result.extra.get("fallos_previos", []):
            db.add(AiUsage(day=_today(), provider=name, model=provider.model, status=kind,
                           error=msg[:500], **{usage_field: analysis.id}))
        analysis.provider = used_provider
        db.add(AiUsage(day=_today(), provider=used_provider, model=result.model or provider.model,
                       status="ok", input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                       cache_read_tokens=result.cache_read_tokens, cache_write_tokens=result.cache_write_tokens,
                       estimated_cost_usd=_cost(cfg, result.input_tokens, result.output_tokens),
                       latency_ms=result.latency_ms, request_id=result.request_id,
                       **{usage_field: analysis.id}))
        analysis.model = result.model or provider.model

    result, last_exc = request(user)
    if result is None:
        analysis.status = "rechazado" if getattr(last_exc, "kind", "") == "rechazo" else "error"
        analysis.errors = json.dumps([str(last_exc)], ensure_ascii=False)
        _record_failure(db, cfg)
        return None

    meter(result)
    try:
        return validate(result.text)
    except ValidationFailed as first:
        data = None
        errors = first.errors
        db.flush()
        if blocked_reason(db, cfg) is None:
            result2, last_exc = request(user + repair_message(first.errors))
            if result2 is not None:
                meter(result2)
                try:
                    data = validate(result2.text)
                except ValidationFailed as second:
                    errors = second.errors
        if data is None:
            analysis.status = "invalido"
            analysis.errors = json.dumps(errors[:20], ensure_ascii=False)
            _record_failure(db, cfg)
        return data


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

    data = _complete_validated(db, cfg, provider, analysis, "analysis_id", SYSTEM, user, SCHEMA,
                               lambda raw: parse_and_validate(raw, sources))
    if data is None:
        return analysis
    pub = {"A": _pub(a), "B": _pub(b)}
    analysis.status = "ok"
    analysis.result = json.dumps(data, ensure_ascii=False)
    analysis.flags = json.dumps(derive_flags(data, pub, sources), ensure_ascii=False)
    _record_success(db)
    return analysis


ANALYSIS_MESSAGES = {"ok": "Análisis guardado.",
                     "invalido": "El resultado no pasó la validación y se descartó.",
                     "rechazado": "El modelo declinó el análisis."}


def analyze_for_admin(db: Session, rel: ArticleRelation, cfg: AiConfig | None = None) -> str:
    """Analiza una relación pedida por un administrador y devuelve un mensaje para mostrar.

    Respeta los límites, usa la caché (un par ya analizado no se vuelve a enviar) y nunca
    lanza: un fallo de la IA no debe deshacer lo que el administrador acaba de hacer.
    """
    cfg = cfg or load_config(db)
    reason = blocked_reason(db, cfg)
    if reason:
        return reason
    try:
        analysis = analyze_relation(db, rel, cfg, make_provider(cfg))
    except Exception as exc:  # configuración inválida, red, etc.
        log.warning("Análisis de IA pedido por un administrador: %s", type(exc).__name__)
        return str(exc) if isinstance(exc, (RuntimeError, ValueError)) else "Falló la llamada al proveedor."
    return ANALYSIS_MESSAGES.get(analysis.status, "Falló la llamada al proveedor.")


def run_ai(db: Session, provider: Provider | None = None, limit: int | None = None,
           cfg: AiConfig | None = None) -> AiRunSummary:
    cfg = cfg or load_config(db)
    summary = AiRunSummary(enabled=cfg.enabled)
    reason = blocked_reason(db, cfg)
    if reason and not cfg.enabled:
        summary.reason = reason
        return summary
    limit = cfg.max_per_run if limit is None else limit
    # Se examinan como máximo `limit` pares (los ya analizados se resuelven desde la caché).
    candidates = select_candidates(db, limit, cfg.min_priority)[:limit]
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


# --- análisis de un grupo completo ----------------------------------------------------------


def group_articles(group: StoryGroup) -> list[Article]:
    """Notas activas del grupo que se envían a la IA, en orden estable (por id) y sin demo.

    Si hay más que `MAX_GROUP_NOTES` se usan las primeras detectadas por el radar."""
    arts = [m.article for m in group.members if m.status == "activo" and not m.article.is_demo]
    arts.sort(key=lambda a: a.id)
    if len(arts) > group_ai.MAX_GROUP_NOTES:
        arts = sorted(sorted(arts, key=lambda a: (a.first_seen_at, a.id))[:group_ai.MAX_GROUP_NOTES],
                      key=lambda a: a.id)
    return arts


def group_cache_key(cfg: AiConfig, arts: list[Article]) -> str:
    parts = [group_ai.GROUP_PROMPT_VERSION, cfg.models_label, str(cfg.max_input_chars)]
    for art in arts:
        parts += [str(art.id), art.content_hash or "", (art.body_text or "")[:20000]]
    return hashlib.sha256("".join(parts).encode("utf-8")).hexdigest()


def analyze_group(db: Session, group: StoryGroup, cfg: AiConfig, provider: Provider,
                  use_cache: bool = True) -> AiGroupAnalysis:
    arts = group_articles(group)
    if len(arts) < 2:
        raise ValueError("El grupo necesita al menos dos notas activas para compararlas.")
    key = group_cache_key(cfg, arts)
    if use_cache:
        cached = db.scalar(select(AiGroupAnalysis).where(
            AiGroupAnalysis.cache_key == key, AiGroupAnalysis.group_id == group.id,
            AiGroupAnalysis.status == "ok"))
        if cached is not None:
            return cached
    user, sources, chars = group_ai.build_input([_article_payload(a) for a in arts], cfg.max_input_chars)
    analysis = AiGroupAnalysis(cache_key=key, group_id=group.id,
                               article_ids=json.dumps([a.id for a in arts]),
                               provider=provider.name, model=provider.model,
                               prompt_version=group_ai.GROUP_PROMPT_VERSION, status="error",
                               input_chars=chars, created_at=utcnow())
    db.add(analysis)
    db.flush()
    data = _complete_validated(db, cfg, provider, analysis, "group_analysis_id", group_ai.SYSTEM, user,
                               group_ai.build_schema(list(sources)),
                               lambda raw: group_ai.parse_and_validate(raw, sources))
    if data is None:
        return analysis
    analysis.status = "ok"
    analysis.result = json.dumps(data, ensure_ascii=False)
    analysis.flags = json.dumps(group_ai.derive_flags(data), ensure_ascii=False)
    _record_success(db)
    return analysis


def analyze_group_for_admin(db: Session, group: StoryGroup, cfg: AiConfig | None = None) -> str:
    """Como `analyze_for_admin`, para un grupo: respeta límites y caché, y nunca lanza."""
    cfg = cfg or load_config(db)
    reason = blocked_reason(db, cfg)
    if reason:
        return reason
    try:
        analysis = analyze_group(db, group, cfg, make_provider(cfg))
    except Exception as exc:
        log.warning("Análisis de grupo pedido por un administrador: %s", type(exc).__name__)
        return str(exc) if isinstance(exc, (RuntimeError, ValueError)) else "Falló la llamada al proveedor."
    return ANALYSIS_MESSAGES.get(analysis.status, "Falló la llamada al proveedor.")


def latest_for_group(db: Session, group_id: int) -> AiGroupAnalysis | None:
    return db.scalar(select(AiGroupAnalysis).where(
        AiGroupAnalysis.group_id == group_id, AiGroupAnalysis.status == "ok")
        .order_by(AiGroupAnalysis.created_at.desc(), AiGroupAnalysis.id.desc()).limit(1))
