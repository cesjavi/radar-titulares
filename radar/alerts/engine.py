"""Generación y actualización de alertas: una por grupo, sin duplicados ni ruido."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from radar.alerts import common_source
from radar.ai.service import latest_for_pair, supports_focus
from radar.alerts.config import PRIORITY_RANK, AlertSettings, load_settings
from radar.analysis.relations import TYPE_LABELS
from radar.analysis.textproc import norm
from radar.models import (
    Alert,
    AlertEvent,
    Article,
    ArticleRelation,
    StoryGroup,
    StoryGroupMember,
    Topic,
)
from radar.notify import telegram
from radar.timeutil import to_local, utcnow

log = logging.getLogger("radar.alerts")

EXPRESSION_TYPES = {"titular_identico", "titular_casi_identico", "expresion_compartida"}
ACTIVE_DAYS = 7

LIMITATIONS = [
    "La coincidencia es léxica (palabras, frases, entidades): no verifica que el enfoque o el "
    "mensaje sean iguales.",
    "La prioridad indica qué revisar primero. No significa falsedad ni coordinación.",
    "El orden de detección depende de cuándo consultó el radar cada fuente; el orden de "
    "publicación solo se informa entre notas con hora declarada.",
    "Publicar antes no implica que un medio dirija o copie a otro.",
]


@dataclass
class AlertSummary:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    silenced: int = 0
    queued: int = 0
    reasons: dict = field(default_factory=dict)


def _fmt(dt):
    return to_local(dt).strftime("%d/%m/%Y %H:%M") if dt else None


def _pub(a: Article) -> dict:
    if a.published_precision == "datetime":
        return {"valor": _fmt(a.published_at), "precision": "fecha y hora"}
    if a.published_precision == "date" and a.published_date:
        return {"valor": a.published_date.strftime("%d/%m/%Y"), "precision": "solo fecha"}
    return {"valor": None, "precision": "sin dato"}


def evaluate_group(db: Session, group: StoryGroup, cfg: AlertSettings) -> dict | None:
    """Evidencia y prioridad de un grupo, o {"skip": motivo}."""
    members = [m for m in group.members if m.status == "activo"]
    arts = [m.article for m in members if m.article is not None and not m.article.is_demo] or \
        [m.article for m in members if m.article is not None]
    if len(arts) < 2:
        return {"skip": "menos de dos notas"}
    independent = {a.media_id for a in arts if not a.is_syndicated}
    if len(independent) < cfg.min_independent_media:
        return {"skip": "menos medios independientes que el mínimo"}
    if cfg.excluded_sections and all((a.section or "").lower() in cfg.excluded_sections for a in arts):
        return {"skip": "sección excluida"}
    haystack = norm(" ".join([group.title or "", group.common_terms or ""] + [a.title for a in arts]))
    for term in cfg.excluded_terms:
        if norm(term) and norm(term) in haystack:
            return {"skip": f"término excluido: {term}"}

    ids = [a.id for a in arts]
    by_id = {a.id: a for a in arts}
    rels = db.scalars(select(ArticleRelation).where(
        ArticleRelation.article_a_id.in_(ids), ArticleRelation.article_b_id.in_(ids),
        ArticleRelation.review_status != "rechazada")).all()

    priority, reasons = cfg.priority_shared_topic, ["tema compartido"]
    best_rank = PRIORITY_RANK[priority]
    phrases: list[str] = []
    rel_items = []
    ai_items: list[dict] = []
    warnings: list[str] = []
    for r in rels:
        a, b = by_id[r.article_a_id], by_id[r.article_b_id]
        if a.media_id == b.media_id or a.is_syndicated or b.is_syndicated:
            continue  # no es coincidencia entre medios independientes
        ev = json.loads(r.evidence or "{}")
        for note in ev.get("notas", []):
            if ("negación" in note or "dejó de" in note) and note not in warnings:
                warnings.append(note)
        if r.relation_type == "afirmaciones_distintas":
            msg = (f"Afirmaciones distintas entre {a.media.name} y {b.media.name}: "
                   "no es el mismo mensaje.")
            if msg not in warnings:
                warnings.append(msg)
        rel_items.append({"id": r.id, "tipo": r.relation_type,
                          "tipo_texto": TYPE_LABELS.get(r.relation_type, r.relation_type),
                          "puntaje": r.score, "revision": r.review_status,
                          "reglas": [x["id"] for x in json.loads(r.rules or "[]")],
                          "a": r.article_a_id, "b": r.article_b_id})
        for f in ev.get("frases", []):
            if f not in phrases:
                phrases.append(f)
        candidate, reason = None, None
        ai = latest_for_pair(db, r.article_a_id, r.article_b_id)
        if ai is not None:
            ai_flags = json.loads(ai.flags or "[]")
            ai_items.append({"relacion": r.id, "analisis": ai.id, "modelo": ai.model,
                             "enfoque": json.loads(ai.result or "{}").get("enfoque"),
                             "discrepancias": ai_flags})
        if supports_focus(ai):
            candidate = cfg.priority_shared_expression
            reason = "enfoque similar respaldado por análisis (IA, pendiente de confirmación humana)"
        elif r.relation_type in EXPRESSION_TYPES:
            candidate = cfg.priority_shared_expression
            reason = "expresión distintiva o titular compartido"
        elif r.relation_type == "mismo_hecho":
            if r.time_delta_seconds is not None:
                hours, basis = abs(r.time_delta_seconds) / 3600, "publicación"
            else:
                ta, tb = a.first_seen_at, b.first_seen_at
                hours, basis = abs((tb - ta).total_seconds()) / 3600, "detección"
            if hours <= cfg.same_event_hours:
                candidate = cfg.priority_same_event
                reason = f"mismo hecho a {hours:.0f} h (según {basis})"
        if candidate and PRIORITY_RANK[candidate] > best_rank:
            best_rank, priority, reasons = PRIORITY_RANK[candidate], candidate, [reason]
        elif candidate and PRIORITY_RANK[candidate] == best_rank and reason not in reasons:
            reasons.append(reason)
    if not rel_items:
        return {"skip": "sin relaciones entre medios independientes"}
    if warnings:
        reasons = reasons + ["con afirmaciones distintas: revisar antes de interpretar"]
    if best_rank < PRIORITY_RANK[cfg.min_priority]:
        return {"skip": "prioridad menor que la mínima configurada"}

    first_detected = min(arts, key=lambda a: a.first_seen_at)
    timed = [a for a in arts if a.published_precision == "datetime" and a.published_at]
    first_published = min(timed, key=lambda a: a.published_at) if timed else None
    common = json.loads(group.common_terms or "{}")
    for f in common.get("frases", []):
        if f not in phrases:
            phrases.append(f)
    media_names = {}
    for a in arts:
        media_names[a.media_id] = a.media.name
    evidence = {
        "que_coincide": reasons,
        "articulos": [{
            "id": a.id, "medio": a.media.name, "subfuente": a.origin_label,
            "autor": a.author,
            "republicada": a.is_syndicated, "titular": a.title, "url": a.url,
            "publicada": _pub(a), "detectada": _fmt(a.first_seen_at),
        } for a in sorted(arts, key=lambda a: a.first_seen_at)],
        "frases": phrases[:8],
        "terminos": common.get("terminos", [])[:8],
        "relaciones": sorted(rel_items, key=lambda x: -(x["puntaje"] or 0))[:20],
        "primera_deteccion": {"articulo": first_detected.id, "medio": first_detected.media.name,
                              "cuando": _fmt(first_detected.first_seen_at)},
        "primera_publicacion": ({"articulo": first_published.id, "medio": first_published.media.name,
                                 "cuando": _fmt(first_published.published_at),
                                 "nota": f"Entre {len(timed)} de {len(arts)} notas con hora declarada."}
                                if first_published else
                                {"nota": "Ninguna nota tiene hora de publicación declarada."}),
        "medios_independientes": sorted(media_names[m] for m in independent),
        "analisis_ia": ai_items,
        "advertencias": warnings,
    }
    sources = common_source.detect(arts)
    material = {
        "medios": sorted(independent), "prioridad": priority,
        "tipos": sorted({r["tipo"] for r in rel_items}), "frases": sorted(phrases[:5]),
        "fuente_comun": sorted(s["tipo"] for s in sources),
        "advertencias": bool(warnings),
        "ia": sorted((x["relacion"], x["enfoque"] or "", len(x["discrepancias"])) for x in ai_items),
    }
    return {
        "priority": priority, "reasons": reasons, "evidence": evidence, "sources": sources,
        "fingerprint": hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest(),
        "material": material, "articles": arts, "independent": independent,
    }


def _diff(old: dict, new: dict) -> dict:
    diff = {}
    added = sorted(set(new["medios"]) - set(old.get("medios", [])))
    if added:
        diff["medios_agregados"] = added
    if old.get("prioridad") != new["prioridad"]:
        diff["prioridad"] = [old.get("prioridad"), new["prioridad"]]
    new_phrases = sorted(set(new["frases"]) - set(old.get("frases", [])))
    if new_phrases:
        diff["frases_nuevas"] = new_phrases
    if old.get("tipos") != new["tipos"]:
        diff["tipos"] = new["tipos"]
    if old.get("fuente_comun", []) != new["fuente_comun"]:
        diff["fuente_comun"] = new["fuente_comun"]
    if bool(old.get("advertencias")) != bool(new.get("advertencias")):
        diff["advertencias"] = new.get("advertencias")
    if old.get("ia", []) != new.get("ia", []) and new.get("ia"):
        diff["analisis_ia"] = len(new["ia"])
    return diff


def update_alerts(db: Session, now=None) -> AlertSummary:
    now = now or utcnow()
    cfg = load_settings(db)
    summary = AlertSummary()
    silenced_ids = set(db.scalars(select(Topic.id).where(Topic.slug.in_(cfg.silenced_topics)))) \
        if cfg.silenced_topics else set()
    groups = db.scalars(
        select(StoryGroup)
        .options(selectinload(StoryGroup.members).selectinload(StoryGroupMember.article)
                 .selectinload(Article.media))
        .where(StoryGroup.status == "open", StoryGroup.updated_at >= now - timedelta(days=ACTIVE_DAYS))
    ).all()
    for group in groups:
        result = evaluate_group(db, group, cfg)
        alert = db.scalar(select(Alert).where(Alert.group_id == group.id))
        if "skip" in result:
            summary.skipped += 1
            summary.reasons[result["skip"]] = summary.reasons.get(result["skip"], 0) + 1
            continue
        silenced = group.topic_id in silenced_ids
        material = result["material"]
        payload = dict(
            title=group.title[:500], topic_id=group.topic_id, priority=result["priority"],
            priority_reason="; ".join(result["reasons"]),
            evidence=json.dumps(result["evidence"], ensure_ascii=False),
            common_source=json.dumps(result["sources"], ensure_ascii=False),
            limitations=json.dumps(LIMITATIONS, ensure_ascii=False),
            article_count=len(result["articles"]),
            media_count=len({a.media_id for a in result["articles"]}),
            independent_media_count=len(result["independent"]),
        )
        if alert is None:
            alert = Alert(alert_type="coincidencia_grupo", group_id=group.id,
                          status="silenciada" if silenced else "pendiente",
                          fingerprint=result["fingerprint"], version=1, created_at=now,
                          updated_at=now, last_material_change_at=now, **payload)
            db.add(alert)
            db.flush()
            db.add(AlertEvent(alert_id=alert.id, event="creada",
                              detail=json.dumps({"material": material}, ensure_ascii=False),
                              created_at=now))
            summary.created += 1
            summary.silenced += silenced
            if not silenced:
                summary.queued += _maybe_notify(db, alert, cfg, now)
            continue

        for k, v in payload.items():
            setattr(alert, k, v)
        alert.updated_at = now
        if silenced and alert.status == "pendiente":
            alert.status = "silenciada"
        if alert.fingerprint == result["fingerprint"]:
            summary.unchanged += 1  # cambio irrelevante: sin evento ni notificación
            continue
        old = {}
        last = db.scalar(select(AlertEvent).where(AlertEvent.alert_id == alert.id,
                                                  AlertEvent.event.in_(("creada", "actualizada")))
                         .order_by(AlertEvent.id.desc()))
        if last and last.detail:
            old = json.loads(last.detail).get("material", {})
        changes = _diff(old, material)
        alert.fingerprint = result["fingerprint"]
        if not changes:
            # La huella cambió de forma (p. ej. nueva versión del motor) sin diferencias
            # relevantes: no hay versión nueva, evento, reapertura ni aviso.
            summary.unchanged += 1
            continue
        alert.version += 1
        alert.last_material_change_at = now
        reopened = alert.status == "revisada"
        if reopened:
            alert.status = "pendiente"  # hay evidencia nueva: vuelve a revisión
        db.add(AlertEvent(alert_id=alert.id, event="actualizada", created_at=now,
                          detail=json.dumps({"material": material, "cambios": changes,
                                             "reabierta": reopened}, ensure_ascii=False)))
        summary.updated += 1
        if alert.status == "pendiente":
            summary.queued += _maybe_notify(db, alert, cfg, now)
    _close_orphans(db, now)
    db.flush()
    log.info("Alertas: %s", summary)
    return summary


def _close_orphans(db: Session, now) -> None:
    """Una alerta por historia: si su grupo se unió a otro o quedó vacío, la alerta pendiente
    se cierra (queda en el historial) para no duplicar la del grupo que sigue abierto."""
    rows = db.execute(
        select(Alert, StoryGroup).join(StoryGroup, StoryGroup.id == Alert.group_id)
        .where(Alert.status == "pendiente", StoryGroup.status != "open")).all()
    for alert, group in rows:
        if group.status == "merged" and group.merged_into_id:
            note = f"Cerrada automáticamente: el grupo se unió al grupo #{group.merged_into_id}."
        else:
            note = "Cerrada automáticamente: el grupo quedó sin notas."
        alert.status = "descartada"
        alert.review_note = note
        alert.updated_at = now
        db.add(AlertEvent(alert_id=alert.id, event="estado", created_at=now,
                          detail=json.dumps({"de": "pendiente", "a": "descartada", "nota": note,
                                             "automatico": True}, ensure_ascii=False)))


def _maybe_notify(db: Session, alert: Alert, cfg: AlertSettings, now) -> int:
    if not telegram.enabled():
        return 0
    if PRIORITY_RANK[alert.priority] < PRIORITY_RANK[cfg.telegram_min_priority]:
        return 0
    if alert.last_notified_at and alert.last_notified_at + timedelta(minutes=cfg.cooldown_minutes) > now:
        return 0  # dentro del cooldown
    if telegram.enqueue(db, alert, now):
        alert.last_notified_at = now
        return 1
    return 0


STATUSES = ("pendiente", "revisada", "descartada", "silenciada")


def set_status(db: Session, alert: Alert, status: str, user, note: str | None) -> None:
    if status not in STATUSES:
        raise ValueError("Estado inválido.")
    previous = alert.status
    alert.status = status
    alert.reviewed_by_id = user.id
    alert.reviewed_at = utcnow()
    alert.review_note = (note or "").strip()[:2000] or None
    db.add(AlertEvent(alert_id=alert.id, event="estado", user_id=user.id,
                      detail=json.dumps({"de": previous, "a": status, "nota": alert.review_note},
                                        ensure_ascii=False)))
