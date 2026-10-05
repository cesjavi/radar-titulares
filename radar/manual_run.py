"""Ejecuciones manuales pedidas por un administrador desde Configuración.

Equivalen a los comandos de la CLI (`collect`, `analyze`, `ai-analyze`, `purge`, `probe`) y usan
las mismas fases que el cron de Vercel, con el mismo bloqueo de ejecución exclusiva: no pisan al
timer ni a otra ejecución en curso.

En un servidor propio corren en un hilo y la pantalla consulta el avance. En un entorno
serverless (Vercel) un hilo no sobrevive a la respuesta, así que se ejecutan en la misma
solicitud (y duran lo que permita la función).

El último resultado se guarda en `app_settings` (clave `manual_run`), visible desde cualquier
instancia.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import timedelta

from radar.config import get_settings
from radar.db import session_scope
from radar.models import AppSetting
from radar.timeutil import utcnow

log = logging.getLogger("radar.manual_run")

KEY = "manual_run"
STALE_AFTER = timedelta(minutes=30)  # un "en curso" más viejo se da por abandonado
TASKS = {
    "recolectar": "Recolectar (como collect)",
    "analizar": "Analizar relaciones, grupos y alertas (como analyze)",
    "ia": "Analizar pares con IA (como ai-analyze)",
    "mantenimiento": "Aplicar retención (como purge)",
    "probar": "Verificar fuentes sin guardar nada (como probe)",
}
AI_LIMIT_MAX = 50

_thread: threading.Thread | None = None
_guard = threading.Lock()


class RunError(ValueError):
    """Pedido inválido o imposible ahora (el mensaje se muestra tal cual)."""


def parse_options(form) -> dict:
    task = str(form.get("tarea", ""))
    if task not in TASKS:
        raise RunError("Elegí una tarea.")
    opts: dict = {"tarea": task, "medio": "", "force": False, "no_enrich": False,
                  "no_analyze": False, "limit": None, "dry_run": False}
    if task in ("recolectar", "probar"):
        opts["medio"] = str(form.get("medio", "")).strip()[:64]
        if opts["medio"]:
            from radar.collector.runner import check_media_slug

            try:
                check_media_slug(opts["medio"])
            except ValueError as exc:
                raise RunError(str(exc)) from exc
    if task == "recolectar":
        for name in ("force", "no_enrich", "no_analyze"):
            opts[name] = bool(form.get(name))
    if task == "ia":
        raw = str(form.get("limit", "")).strip() or "1"
        if not raw.isdigit() or not 1 <= int(raw) <= AI_LIMIT_MAX:
            raise RunError(f"El máximo de pares debe estar entre 1 y {AI_LIMIT_MAX}.")
        opts["limit"] = int(raw)
    if task == "mantenimiento":
        opts["dry_run"] = bool(form.get("dry_run"))
    return opts


def describe(opts: dict) -> str:
    parts = [TASKS[opts["tarea"]].split(" (")[0]]
    if opts.get("medio"):
        parts.append(f"medio {opts['medio']}")
    for flag, text in (("force", "ignorando el intervalo"), ("no_enrich", "sin leer páginas"),
                       ("no_analyze", "sin analizar al final"), ("dry_run", "solo simulación")):
        if opts.get(flag):
            parts.append(text)
    if opts.get("limit"):
        parts.append(f"hasta {opts['limit']} pares")
    return " · ".join(parts)


# --- estado ------------------------------------------------------------------------------------


def _save(state: dict) -> None:
    with session_scope() as db:
        row = db.get(AppSetting, KEY)
        text = json.dumps(state, ensure_ascii=False, default=str)
        if row is None:
            db.add(AppSetting(key=KEY, value=text))
        else:
            row.value = text


def last_state(db=None) -> dict | None:
    def read(session):
        row = session.get(AppSetting, KEY)
        return json.loads(row.value) if row and row.value else None

    try:
        state = read(db) if db is not None else _read_own(read)
    except Exception:
        return None
    if not state:
        return None
    from datetime import datetime

    for name in ("inicio", "fin"):  # se guardan como texto; la pantalla necesita fechas
        try:
            state[name] = datetime.fromisoformat(state[name]) if state.get(name) else None
        except ValueError:
            state[name] = None
    if state.get("estado") == "en_curso" and state["inicio"]             and utcnow() - state["inicio"] > STALE_AFTER:
        state["estado"] = "abandonada"
    return state


def _read_own(read):
    with session_scope() as db:
        return read(db)


def running(db=None) -> bool:
    state = last_state(db)
    return bool(state and state.get("estado") == "en_curso")


# --- ejecución -----------------------------------------------------------------------------------


def _execute(opts: dict) -> dict:
    """Corre la tarea y devuelve el resultado (diccionario serializable)."""
    from radar import pipeline

    task = opts["tarea"]
    if task == "recolectar":
        result = {"recoleccion": pipeline.collect_phase(opts["medio"] or None, opts["force"],
                                                        enrich=not opts["no_enrich"])}
        if not opts["no_analyze"]:
            result["analisis"] = pipeline.analysis_phase()
        return result
    if task == "analizar":
        return {"analisis": pipeline.analysis_phase()}
    if task == "mantenimiento":
        return {"mantenimiento": pipeline.maintenance_phase(dry_run=opts["dry_run"])}
    if task == "ia":
        return {"ia": pipeline.ai_phase(opts["limit"])}
    from radar.probe import probe_media

    rows = probe_media(opts["medio"] or None)
    return {"fuentes": rows, "resumen": {
        "ok": sum(r["estado"] == "ok" for r in rows),
        "vacias": sum(r["estado"] == "vacia" for r in rows),
        "con_falla": sum(r["estado"] == "falla" for r in rows)}}


def _run(opts: dict, user: str) -> None:
    state = {"estado": "en_curso", "inicio": utcnow().isoformat(), "opciones": opts,
             "descripcion": describe(opts), "usuario": user}
    _save(state)
    try:
        state["resultado"] = _execute(opts)
        state["estado"] = "ok"
    except Exception as exc:  # el detalle va al log; a la pantalla, solo el tipo y un mensaje corto
        log.exception("Ejecución manual fallida: %s", opts["tarea"])
        state["estado"] = "error"
        state["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    state["fin"] = utcnow().isoformat()
    _save(state)


def start(opts: dict, user: str) -> dict:
    """Lanza la tarea. Devuelve el estado inicial (o final, si corrió en la misma solicitud)."""
    global _thread
    with _guard:
        if running() or (_thread is not None and _thread.is_alive()):
            raise RunError("Ya hay una ejecución manual en curso. Esperá a que termine.")
        if get_settings().serverless:
            _run(opts, user)
            return last_state() or {}
        _save({"estado": "en_curso", "inicio": utcnow().isoformat(), "opciones": opts,
               "descripcion": describe(opts), "usuario": user})
        _thread = threading.Thread(target=_run, args=(opts, user), name="radar-manual", daemon=True)
        _thread.start()
    return last_state() or {}


def wait(timeout: float = 30.0) -> None:
    """Espera a que termine el hilo (lo usan las pruebas)."""
    t = _thread
    if t is not None:
        t.join(timeout)
