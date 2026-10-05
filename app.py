"""Punto de entrada para Vercel (runtime de Python): Vercel carga la variable `app`.

Requisitos en Vercel (ver VERCEL.md):
- `RADAR_SECRET_KEY`: sin valor por defecto. Si falta, el arranque falla con un mensaje claro
  (una clave conocida permitiría falsificar sesiones de administrador).
- Base PostgreSQL (Neon): el disco de Vercel es efímero. Las migraciones se aplican desde una
  máquina con la conexión DIRECTA (`python -m radar init-db`), no al arrancar la función.
- `CRON_SECRET`: sin él, `/api/cron` responde 503 (no se puede ejecutar anónimamente).
"""

from __future__ import annotations

import hmac
import logging
import os

from fastapi import Request
from fastapi.responses import JSONResponse

from radar.collector.adapters import ADAPTERS
from radar.config import ConfigError, get_settings
from radar.pipeline import TASKS, run_cycle
from radar.web.app import create_app

log = logging.getLogger("radar.vercel")

settings = get_settings()  # lanza ConfigError si falta RADAR_SECRET_KEY
if (settings.serverless and not settings.is_postgres
        and os.getenv("RADAR_ALLOW_EPHEMERAL_DB", "").strip().lower() not in {"1", "true", "yes", "si"}):
    raise ConfigError(
        "En Vercel el disco es efímero y SQLite perdería los datos. Configurá RADAR_DATABASE_URL "
        "con una base PostgreSQL (por ejemplo Neon). Solo para una demostración: "
        "RADAR_ALLOW_EPHEMERAL_DB=true."
    )

app = create_app()


@app.get("/api/cron", include_in_schema=False)
def cron(request: Request, tarea: str = "todo", media: str | None = None):
    """Ciclo programado (Vercel Cron). Protegido con `Authorization: Bearer $CRON_SECRET`.

    ?tarea=todo|recolectar|analizar   (por defecto, todo)
    ?media=perfil|eldestape|infobae|pagina12   (solo con tarea=recolectar o todo)

    Para repartir el trabajo (duración máxima por invocación) se pueden definir varios crons,
    uno por medio y otro para el análisis. Es seguro repetirlo: el bloqueo y los intervalos
    mínimos por fuente evitan trabajo duplicado.
    """
    secret = os.getenv("CRON_SECRET", "")
    if not secret:
        return JSONResponse({"estado": "error", "motivo": "CRON_SECRET no configurado"}, status_code=503)
    if not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {secret}"):
        return JSONResponse({"estado": "error", "motivo": "no autorizado"}, status_code=401)
    if tarea not in TASKS:
        return JSONResponse({"estado": "error", "motivo": f"tarea inválida; opciones: {', '.join(TASKS)}"},
                            status_code=400)
    if media is not None and media not in ADAPTERS:
        return JSONResponse({"estado": "error", "motivo": f"medio inválido; opciones: {', '.join(sorted(ADAPTERS))}"},
                            status_code=400)
    try:
        return {"estado": "ok", **run_cycle(tarea, media)}
    except ValueError as exc:
        return JSONResponse({"estado": "error", "motivo": str(exc)}, status_code=400)
    except Exception:  # el detalle va al log; la respuesta no expone datos internos
        log.exception("Falló el ciclo programado")
        return JSONResponse({"estado": "error", "motivo": "falló el ciclo; ver los logs"}, status_code=500)
