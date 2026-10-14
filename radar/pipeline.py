"""Ciclos de procesamiento reutilizables (los usa el endpoint de cron de Vercel).

Equivalen a `python -m radar collect` y `python -m radar analyze`, pero devuelven datos en lugar
de imprimir. Cada fase toma su propio bloqueo de ejecución exclusiva (archivo en SQLite, tabla
con vencimiento en PostgreSQL), así que un cron duplicado o superpuesto no hace trabajo doble.
"""

from __future__ import annotations

import logging

from radar.collector.runner import CollectorBusy, _make_fetcher, check_media_slug, run_collection
from radar.db import session_scope
from radar.joblock import job_lock
from radar.security import purge_old_attempts

log = logging.getLogger("radar.pipeline")

TASKS = ("todo", "recolectar", "analizar", "mantenimiento", "redes")


def collect_phase(media: str | None = None, force: bool = False, enrich: bool = True,
                  social: bool = True) -> dict:
    if media is not None:
        check_media_slug(media)
    try:
        summary = run_collection(only_media=media, force=force, enrich_pages=enrich, social=social)
    except CollectorBusy:
        return {"estado": "omitido", "motivo": "ya hay una ejecución en curso"}
    with session_scope() as db:
        purge_old_attempts(db)
    result = {
        "estado": "ok",
        "subfuentes": len(summary.runs),
        "con_problemas": len(summary.errors),
        "notas_nuevas": sum(r.items_new for r in summary.runs),
        "paginas_leidas": sum(r.items_updated for r in summary.enrich_runs),
    }
    if summary.social is not None:
        result.update({"canales": len(summary.social.outcomes),
                       "canales_con_problemas": len(summary.social.errors),
                       "posteos_nuevos": summary.social.new_posts})
    return result


def social_phase(force: bool = False) -> dict:
    """Solo los canales de Telegram (un cron propio: `recolectar` con `media` no los incluye)."""
    from radar.config import get_settings
    from radar.social.collector import run_social_collection

    try:
        with job_lock(name="redes"):
            with _make_fetcher(get_settings()) as fetcher:
                s = run_social_collection(fetcher, force=force)
    except CollectorBusy:
        return {"estado": "omitido", "motivo": "ya hay una ejecución en curso"}
    return {"estado": "ok", "canales": len(s.outcomes), "canales_con_problemas": len(s.errors),
            "canales_salteados": s.skipped, "posteos_nuevos": s.new_posts}


def analysis_phase() -> dict:
    from radar.cli import _analyze

    try:
        with job_lock():
            s = _analyze()
    except CollectorBusy:
        return {"estado": "omitido", "motivo": "ya hay una ejecución en curso"}
    alerts = s.alerts
    return {
        "estado": "ok",
        "notas_recientes": s.docs_recent,
        "relaciones_nuevas": s.relations_new,
        "grupos_nuevos": s.groups_new,
        "alertas_nuevas": getattr(alerts, "created", 0),
        "alertas_actualizadas": getattr(alerts, "updated", 0),
        "posteos_vinculados": getattr(getattr(s, "social", None), "links_new", 0),
    }


def ai_phase(limit: int | None = None) -> dict:
    """Equivale a `python -m radar ai-analyze`. Cada par analizado es una llamada al proveedor."""
    from radar.ai.service import run_ai

    try:
        with job_lock(name="ia"):
            with session_scope() as db:
                s = run_ai(db, limit=limit)
    except CollectorBusy:
        return {"estado": "omitido", "motivo": "ya hay una ejecución en curso"}
    return {"estado": "ok", "candidatos": s.candidates, "en_cache": s.cached, "llamadas": s.calls,
            "validos": s.ok, "invalidos": s.invalid, "errores": s.errors,
            "motivo": s.stopped or s.reason}


def maintenance_phase(dry_run: bool = False) -> dict:
    """Retención del histórico (equivale a `python -m radar purge`). No hace backups: eso
    necesita pg_dump y se ejecuta desde una máquina propia (ver VERCEL.md)."""
    from radar.config import get_settings
    from radar.maintenance import purge

    try:
        with job_lock(name="mantenimiento"):
            with session_scope() as db:
                r = purge(db, get_settings(), dry_run=dry_run)
                if dry_run:
                    db.rollback()
    except CollectorBusy:
        return {"estado": "omitido", "motivo": "ya hay una ejecución en curso"}
    return {"estado": "simulación (no se borró nada)" if dry_run else "ok",
            "notas_borradas": r.articles, "grupos_vacios_borrados": r.groups,
            "ejecuciones_borradas": r.runs, "registros_ia_borrados": r.ai_usage,
            "avisos_borrados": r.notifications, "posteos_borrados": r.posts}


def run_cycle(task: str = "todo", media: str | None = None, force: bool = False) -> dict:
    if task not in TASKS:
        raise ValueError(f"Tarea desconocida: {task}. Opciones: {', '.join(TASKS)}")
    result: dict = {}
    if task in ("todo", "recolectar"):
        result["recoleccion"] = collect_phase(media, force)
    if task in ("todo", "analizar"):
        result["analisis"] = analysis_phase()
    if task == "mantenimiento":
        result["mantenimiento"] = maintenance_phase()
    if task == "redes":
        result["redes"] = social_phase(force)
    return result
