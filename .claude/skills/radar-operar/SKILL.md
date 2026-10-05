---
name: radar-operar
description: Levanta y opera Radar de Titulares en local (panel, recolección, análisis, alertas, IA, backups, vista pública) y diagnostica fuentes caídas, alertas o configuración. Usar para "cómo la levanto", "no aparecen noticias", "por qué sale modo demo", "revisá las fuentes" o "hacé un backup".
---

# Operar Radar de Titulares

Leé `AGENTS.md` para los principios y `skills.md` para la lista completa de comandos. En Windows usá **siempre** `.\.venv-win\Scripts\python` (el `.venv` del proyecto quedó atado a WSL). No corras la app desde Windows y WSL a la vez sobre la misma base.

## Levantar
```powershell
python -m venv .venv-win ; .\.venv-win\Scripts\pip install -r requirements-dev.txt   # una vez
.\.venv-win\Scripts\python -m radar init-db
.\.venv-win\Scripts\python -m radar create-admin NOMBRE        # una vez
.\.venv-win\Scripts\python -m radar collect                    # trae noticias, analiza y genera alertas
.\.venv-win\Scripts\uvicorn radar.web.app:create_app --factory --port 8000
```
En Windows la recolección no es automática: repetir `collect` o dejar un bucle (`while ($true) { …collect; Start-Sleep 600 }`).

## Diagnóstico (en este orden)
1. **`/salud`** → `{"estado": "ok|degradado", "base": true, "ultima_recoleccion_min": N}`. `degradado` = la última recolección exitosa tiene más de 30 minutos. 503 = la base no responde.
2. **`python -m radar probe`** → ¿responden las fuentes? Distingue `OK`, `VACÍA` y `FALLA`. Un medio que falla no significa que no haya noticias: el panel lo muestra como "sin datos".
3. **Panel → Fuentes y Ejecuciones** → estado por subfuente (operativa, con fallas, caída, bloqueada, vacía), error actual, intentos y duración.
4. **`python -m radar ai-status`** → por qué la IA no llama (desactivada, falta clave o modelo, límite diario, circuit breaker, cuota agotada).

## Base de datos: SQLite o PostgreSQL (Neon)
- `/salud` con `"base": false` (HTTP 503): la app no llega a la base. Con Neon: revisar `RADAR_DATABASE_URL` (con `-pooler`), que el proyecto no esté suspendido sin red, y que la base tenga las migraciones (`init-db` con la URL **directa**).
- Migraciones y backups van por la conexión **directa**; la app, por el pooler (PgBouncer en modo transacción: no admite bloqueos de sesión ni `SET`).
- `backup` en PostgreSQL necesita `pg_dump`/`pg_restore` instalados; la restauración no se automatiza.
- Plan gratuito de Neon: 1 GB y 100 CU-horas por mes; la base ocupa ~7 KB por nota más ~8 MB fijos. Si se acerca al límite, bajar `RADAR_RETENTION_DAYS` y espaciar la recolección.
- Vercel: `app.py` exige `RADAR_SECRET_KEY` y PostgreSQL; `/api/cron` exige `CRON_SECRET`. Ver `VERCEL.md`.

## Síntomas frecuentes
| Síntoma | Causa y solución |
|---|---|
| Aviso "MODO DEMO" y titulares `[DEMO]` | `RADAR_DEMO_MODE=true` en `.env`. Ponerlo en `false`, reiniciar, y `demo-clear` para borrarlos |
| "no existe `.venv-win`" o errores de `pyvenv.cfg` | Crear el venv de Windows (arriba); no usar `.venv` |
| `database disk image is malformed` | Se compartió la base entre Windows y WSL. Restaurar con `restore-check` y `restore` desde un backup; la base dañada se conserva |
| Notas sin bajada (El Destape) | Su sitemap no la trae; se completa leyendo páginas (`RADAR_ENRICH_PER_MEDIA`, 10 por ciclo) |
| Alertas que reaparecen como pendientes | Hubo evidencia nueva relevante (otro medio, prioridad o frases); ver el historial de la alerta |
| Fuente en `bloqueada` | HTTP 401/403/429 o página de desafío. No se reintenta ni se evade; revisar `robots.txt` y el User-Agent |
| `/api/cron` responde 503 o 401 | Falta `CRON_SECRET` en Vercel (503) o el `Bearer` no coincide (401) |
| El cron responde `"omitido"` | Hay otra ejecución en curso (esperado); un bloqueo colgado vence solo a los 20 minutos |
| `ai-analyze` no hace nada | `RADAR_AI_ENABLED=false`, o proveedor sin clave/modelo, o límite diario alcanzado |

## IA y costos
Está desactivada por defecto. **Cada análisis es una llamada al proveedor y puede tener costo**: no activarla ni llamarla sin que el usuario lo autorice. Primera prueba con `RADAR_AI_MAX_PER_RUN=1` y `ai-analyze --limit 1`. Las tarifas (`RADAR_AI_PRICE_*`) son opcionales y el costo se muestra como estimación, no como facturación.

## Backups y restauración
`backup` usa la API de SQLite (nunca copiar el `.db` a mano con WAL activo) y verifica la copia. `restore-check` prueba un backup sobre una copia temporal. `restore ARCHIVO --yes` requiere los servicios detenidos y conserva la base anterior renombrada.

## Vista pública
`RADAR_PUBLIC_MODE=true`: los visitantes ven Resumen, Noticias, Grupos, Cronología, Métricas y Fuentes en solo lectura, y las alertas según `RADAR_PUBLIC_ALERTS` (`revisadas` por defecto). Nunca ven datos DEMO, notas de revisión, errores internos ni análisis de IA sin confirmar.

## Cuidados
- No mostrar el contenido del `.env`: enmascarar claves y tokens.
- No ejecutar acciones que modifiquen datos de producción ni llamadas pagas sin autorización.
- Despliegue en el VPS: usar `DEPLOYMENT.md` y los scripts de `deploy/scripts/`; pedir acceso y autorización antes de ejecutar nada en el servidor.
