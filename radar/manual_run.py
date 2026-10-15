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
SERVERLESS_AI_LIMIT_MAX = 5  # cada par es una llamada al proveedor dentro de la misma solicitud

_thread: threading.Thread | None = None
_guard = threading.Lock()


class RunError(ValueError):
    """Pedido inválido o imposible ahora (el mensaje se muestra tal cual)."""


def parse_options(form) -> dict:
    task = str(form.get("tarea", ""))
    if task not in TASKS:
        raise RunError("Elegí una tarea.")
    opts: dict = {"tarea": task, "medio": "", "medios": [], "force": False, "no_enrich": False,
                  "no_analyze": False, "limit": None, "dry_run": False}
    if task in ("recolectar", "probar"):
        from radar.collector.runner import check_media_slug

        raw_medios = form.getlist("medios") if hasattr(form, "getlist") else form.get("medios", [])
        if isinstance(raw_medios, str):
            raw_medios = [raw_medios]
        elif not isinstance(raw_medios, list):
            raw_medios = list(raw_medios)

        selected = [str(m).strip()[:64] for m in raw_medios if str(m).strip()]
        single = str(form.get("medio", "")).strip()[:64]
        if single and single not in selected:
            selected.append(single)

        for m in selected:
            try:
                check_media_slug(m)
            except ValueError as exc:
                raise RunError(str(exc)) from exc

        opts["medios"] = selected
        opts["medio"] = selected[0] if len(selected) == 1 else (single or "")

    if task == "recolectar":
        for name in ("force", "no_enrich", "no_analyze"):
            opts[name] = bool(form.get(name))
    serverless = get_settings().serverless
    if serverless and task == "recolectar":
        # Todo corre dentro de una sola solicitud: sin tope, Vercel responde 504.
        if not opts["medio"] and not opts["medios"]:
            raise RunError("En Vercel elegí al menos un medio: recolectar todos juntos supera el tiempo "
                           "máximo de la función. Repetí por medio, o usá los crons.")
        opts["no_analyze"] = True  # el análisis se pide aparte (tarea «analizar»)
    if task == "ia":
        raw = str(form.get("limit", "")).strip() or "1"
        limit_max = SERVERLESS_AI_LIMIT_MAX if serverless else AI_LIMIT_MAX
        if not raw.isdigit() or not 1 <= int(raw) <= limit_max:
            raise RunError(f"El máximo de pares debe estar entre 1 y {limit_max}.")
        opts["limit"] = int(raw)
    if task == "mantenimiento":
        opts["dry_run"] = bool(form.get("dry_run"))
    return opts


def describe(opts: dict) -> str:
    parts = [TASKS[opts["tarea"]].split(" (")[0]]
    medios = opts.get("medios", [])
    if len(medios) > 1:
        parts.append(f"{len(medios)} medios encadenados ({', '.join(medios)})")
    elif opts.get("medio"):
        parts.append(f"medio {opts['medio']}")
    elif medios:
        parts.append(f"medio {medios[0]}")
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


def _execute(opts: dict, on_progress=None) -> dict:
    """Corre la tarea y devuelve el resultado (diccionario serializable)."""
    from radar import pipeline

    task = opts["tarea"]
    if task == "recolectar":
        medios = opts.get("medios", [])
        if len(medios) > 1:
            total_subfuentes = 0
            total_problemas = 0
            total_notas = 0
            total_paginas = 0
            for idx, m in enumerate(medios, 1):
                if on_progress:
                    on_progress(f"recolectando {m} [{idx}/{len(medios)}]")
                res = pipeline.collect_phase(m, opts["force"], enrich=not opts["no_enrich"], social=False)
                total_subfuentes += res.get("subfuentes", 0)
                total_problemas += res.get("con_problemas", 0)
                total_notas += res.get("notas_nuevas", 0)
                total_paginas += res.get("paginas_leidas", 0)

            result = {
                "recoleccion": {
                    "estado": "ok",
                    "medios": f"{len(medios)} encadenados ({', '.join(medios)})",
                    "subfuentes": total_subfuentes,
                    "con_problemas": total_problemas,
                    "notas_nuevas": total_notas,
                    "paginas_leidas": total_paginas,
                }
            }
            if not opts["no_analyze"]:
                if on_progress:
                    on_progress("analizando relaciones y alertas")
                result["analisis"] = pipeline.analysis_phase()
            return result
        else:
            medio = medios[0] if medios else (opts.get("medio") or None)
            result = {"recoleccion": pipeline.collect_phase(medio, opts["force"],
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

    medios = opts.get("medios", [])
    if len(medios) > 1:
        all_rows = []
        for m in medios:
            all_rows.extend(probe_media(m))
        rows = all_rows
    else:
        medio = medios[0] if medios else (opts.get("medio") or None)
        rows = probe_media(medio)
    return {"fuentes": rows, "resumen": {
        "ok": sum(r["estado"] == "ok" for r in rows),
        "vacias": sum(r["estado"] == "vacia" for r in rows),
        "con_falla": sum(r["estado"] == "falla" for r in rows)}}


def _run(opts: dict, user: str) -> None:
    base_desc = describe(opts)
    state = {"estado": "en_curso", "inicio": utcnow().isoformat(), "opciones": opts,
             "descripcion": base_desc, "usuario": user}
    _save(state)

    def on_progress(step_info: str) -> None:
        state["descripcion"] = f"{base_desc} · {step_info}"
        _save(state)

    try:
        state["resultado"] = _execute(opts, on_progress=on_progress)
        state["descripcion"] = base_desc
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
