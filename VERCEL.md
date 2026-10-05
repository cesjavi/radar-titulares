# Despliegue en Vercel con base PostgreSQL en Neon

> **Estado: preparado y probado en local, NO desplegado.** No hubo cuenta de Vercel ni de Neon, así que nada se ejecutó contra los servicios reales. Lo que sí se verificó: la suite completa contra un PostgreSQL 18 real y descartable, una ingesta real de los cuatro medios sobre esa base, el arranque de `app.py` y el cron autenticado (ver [Verificaciones](#verificaciones)). Los límites de Vercel y Neon salen de su documentación oficial, consultada el 2026-10-05.

## Qué cambió respecto de la primera adaptación

La primera versión de `api/index.py` **no arrancaba**: importaba funciones que no existen (`seed_all`, `collect_all`, `run_alerts`). Además tenía una clave secreta fija en el código (permitía falsificar sesiones de administrador), un `/api/cron` abierto a cualquiera y una base SQLite en `/tmp`, que se pierde y difiere entre instancias. Se reemplazó por:

| Antes | Ahora |
|---|---|
| `api/index.py` con imports inexistentes | `app.py` en la raíz (Vercel detecta la variable `app` de FastAPI) |
| `vercel.json` con `builds` y `routes` (formato antiguo; `functions` no se puede combinar con `builds`) | `vercel.json` con `functions` (`maxDuration` 300 s y `excludeFiles`) |
| Clave secreta por defecto en el código | Sin valor por defecto: si falta `RADAR_SECRET_KEY`, no arranca |
| `/api/cron` anónimo | Exige `Authorization: Bearer $CRON_SECRET`; sin `CRON_SECRET` responde 503 |
| SQLite en `/tmp` | PostgreSQL (Neon); en Vercel se niega a arrancar con SQLite salvo `RADAR_ALLOW_EPHEMERAL_DB=true` |
| Bloqueo por archivo | Bloqueo con vencimiento en la tabla `job_locks` (migración `0006`) |
| IP de visitante desconocida | En Vercel se usa `x-forwarded-for` (Vercel lo sobrescribe, no se falsifica) |

## Cómo queda la arquitectura

```
Visitantes ──► Vercel (app.py, FastAPI)  ──┐
                                           ├──► Neon (PostgreSQL)
Recolección ──► Vercel Cron  |  VPS / PC ──┘
```

- **Vercel** sirve el panel. Es sin estado: todo lo persistente está en Neon.
- **La recolección** puede correr en tres lugares (elegí uno):
  1. **Vercel Cron** (`/api/cron`). Requiere el plan **Pro**: en Hobby un cron solo puede correr **una vez por día** y una expresión más frecuente hace fallar el despliegue.
  2. **Un VPS o tu PC**, con `python -m radar collect` apuntando a Neon (systemd, Programador de tareas, etc.). Es lo más simple si ya tenés el VPS (ver [DEPLOYMENT.md](DEPLOYMENT.md)); Vercel queda solo para el panel.
  3. Cualquier otro programador externo que ejecute el mismo comando.

## Paso a paso

### 1. Neon
1. Creá un proyecto en [neon.com](https://neon.com) (región cercana a la de las funciones de Vercel; por defecto Vercel usa `iad1`).
2. En **Connect** copiá **dos** cadenas de conexión:
   - **Con pooler** (host con `-pooler`) → para la aplicación (`RADAR_DATABASE_URL`).
   - **Directa** (sin `-pooler`) → para migraciones y backups (`RADAR_DATABASE_URL_DIRECT`). Neon recomienda la conexión directa para migraciones y `pg_dump`; el pooler usa PgBouncer en modo transacción y no admite, por ejemplo, bloqueos de sesión ni `SET`.
   - Si solo definís `RADAR_DATABASE_URL` con la URL del pooler, la directa se deriva quitando `-pooler` del host.
3. Las cadenas llevan `sslmode=require&channel_binding=require`: se conservan tal cual.

### 2. Crear el esquema y el administrador (desde tu PC, una sola vez)
```powershell
$env:RADAR_SECRET_KEY = "<48+ caracteres aleatorios>"
$env:RADAR_DATABASE_URL = "<cadena CON pooler>"
$env:RADAR_DATABASE_URL_DIRECT = "<cadena directa>"
.\.venv-win\Scripts\pip install -r requirements.txt
.\.venv-win\Scripts\python -m radar init-db          # migraciones 0001–0006 + medios, temas, secciones
.\.venv-win\Scripts\python -m radar create-admin admin
.\.venv-win\Scripts\python -m radar collect --force  # primera carga de datos (opcional)
```
Las migraciones **no** se aplican al arrancar la función: así un arranque en frío no depende de ellas y no hay carreras entre instancias. Después de cada actualización con migraciones nuevas, repetí `init-db` (con la URL directa) **antes** de desplegar el código nuevo.

### 3. Variables en Vercel (Settings → Environment Variables)

| Variable | Valor | Notas |
|---|---|---|
| `RADAR_SECRET_KEY` | 48+ caracteres aleatorios | **Obligatoria.** Sin valor por defecto |
| `RADAR_ENV` | `production` | Cookies Secure y HSTS |
| `RADAR_DATABASE_URL` | cadena de Neon **con pooler** | Obligatoria en Vercel |
| `RADAR_DATABASE_URL_DIRECT` | cadena directa | Para migraciones y backups |
| `RADAR_PUBLIC_MODE` | `true` | Vista pública de solo lectura. Sin esta variable el sitio exige login |
| `RADAR_PUBLIC_ALERTS` | `revisadas` | Recomendado. `todas` publica alertas **sin revisión humana** |
| `CRON_SECRET` | 16+ caracteres aleatorios | Solo si usás Vercel Cron. Vercel lo envía como `Authorization: Bearer …` |
| `RADAR_USER_AGENT` | con un contacto real | Los medios ven este identificador |
| `RADAR_RETENTION_DAYS` | `90` en el plan gratuito de Neon | Ver [Tamaño de la base](#tamaño-de-la-base-y-plan-gratuito-de-neon) |
| `RADAR_ENRICH_PER_MEDIA` | `3` (si recolecta Vercel) | Acota la duración del ciclo |

Si Vercel inyecta `DATABASE_URL` o `POSTGRES_URL` (integración con Neon), la app las usa **solo dentro de Vercel** y si `RADAR_DATABASE_URL` no está definida. En una PC esas variables se ignoran, para no conectarse por error a la base de otro proyecto.

### 4. Desplegar
- **Con Git:** `git init`, commit y push; importá el repositorio en Vercel. El repositorio actual **no** es un repositorio git. `.gitignore` ya excluye `.env`, `.env.*`, `.venv*/` y `data/`.
- **Con la CLI** (`vercel`): subí solo lo necesario; `vercel.json` excluye de la función `.venv*`, `data`, `tests`, `deploy`, `alembic`, `*.md` y `.env*`. Ante la duda, agregá un `.vercelignore`.
- Python: Vercel usa 3.12 por defecto (el proyecto exige ≥ 3.11). `requirements.txt` incluye `psycopg-binary`; existen ruedas para Linux x86_64 con Python 3.12 y 3.13.

### 5. Verificar
```
curl https://TU-DOMINIO/salud
→ {"estado":"ok"|"degradado","base":true,"ultima_recoleccion_min":N,"version":"…"}
```
- `base: true` confirma que la función llega a Neon. `degradado` solo indica que la última recolección tiene más de 30 minutos (esperable antes de la primera).
- Si da 500: ver los logs de la función. Los motivos habituales son `RADAR_SECRET_KEY` faltante, base sin migrar (`init-db`) o una URL de Neon incorrecta.

## Recolección con Vercel Cron (solo plan Pro)

Agregá `crons` a `vercel.json`. El trabajo se reparte para que cada invocación entre en `maxDuration`:

```json
"crons": [
  { "path": "/api/cron?tarea=recolectar&media=perfil",    "schedule": "0,10,20,30,40,50 * * * *" },
  { "path": "/api/cron?tarea=recolectar&media=infobae",   "schedule": "2,12,22,32,42,52 * * * *" },
  { "path": "/api/cron?tarea=recolectar&media=pagina12",  "schedule": "4,14,24,34,44,54 * * * *" },
  { "path": "/api/cron?tarea=recolectar&media=eldestape", "schedule": "6,16,26,36,46,56 * * * *" },
  { "path": "/api/cron?tarea=analizar",                   "schedule": "8,18,28,38,48,58 * * * *" },
  { "path": "/api/cron?tarea=mantenimiento",              "schedule": "30 7 * * *" }
]
```

- **Autenticación:** el endpoint exige `Authorization: Bearer $CRON_SECRET` (Vercel lo envía solo si definís `CRON_SECRET`). Sin la variable responde 503; con un valor incorrecto, 401.
- **Duplicados y superposición:** Vercel no reintenta, la entrega es "best effort" y a veces se duplica. Cada fase toma un bloqueo con vencimiento en Neon y respeta el intervalo mínimo de cada fuente, así que una invocación repetida responde `"omitido"` sin hacer trabajo.
- **Duración:** `maxDuration` está en 300 s (el máximo del plan Hobby; Pro admite hasta 800 s). El ciclo completo con los cuatro medios tardó unos 150–200 s en pruebas locales, casi todo en pausas de cortesía entre solicitudes (El Destape pide 10 s); en Vercel no se midió. Por eso se recomienda un cron por medio.
- **Tareas:** `tarea=recolectar|analizar|mantenimiento|todo` y `media=perfil|eldestape|infobae|pagina12`.
- **Probarlo a mano:** `curl -H "Authorization: Bearer $CRON_SECRET" "https://TU-DOMINIO/api/cron?tarea=analizar"`.

## Tamaño de la base y plan gratuito de Neon

Límites del plan gratuito según la documentación de Neon: **1 GB por proyecto**, **100 CU-horas por mes por proyecto** y suspensión del cómputo a los **5 minutos** (no se puede desactivar).

**Almacenamiento (medido en PostgreSQL 18 local):**
- Una base vacía ocupa 7,9 MB. Con 856 notas reales de los cuatro medios, más sus relaciones, grupos y alertas, ocupó 14,2 MB: unos **7,4 KB por nota**.
- Esa carga fue una única captura de los feeds (≈ un día), así que el ritmo diario real puede variar. Si fuera de ~850 notas por día (≈ 6 MB/día), **1 GB se alcanzaría en 4–5 meses**. Con la retención por defecto (365 días) se desbordaría; por eso conviene `RADAR_RETENTION_DAYS=90` (≈ 0,6 GB). Es una estimación: verificá el uso real en la consola de Neon a las pocas semanas.
- Neon puede contar el almacenamiento de otra manera (por ejemplo, el historial para restauración); el número local es una referencia, no la facturación.

**Cómputo (estimación, no medida):** un ciclo cada 10 minutos casi no deja dormir al cómputo (el ciclo dura ~2–3 minutos y luego hay 5 de espera hasta la suspensión). Si el cómputo mínimo fuera 0,25 CU (supuesto no verificado), eso daría ~140 CU-horas por mes, por encima de las 100 gratuitas. Con un ciclo cada 30 minutos serían ~50. Las visitas al sitio también despiertan la base. Si usás el plan gratuito, recolectá cada 30 minutos o más, o vigilá el consumo en la consola de Neon.

**Arranque en frío:** tras suspenderse, la primera consulta tarda más. `connect_timeout` está en 15 s.

## Backups y restauración con Neon
- `python -m radar backup` usa `pg_dump` (formato custom) con la **conexión directa**, verifica el resultado con `pg_restore --list` y rota los últimos `RADAR_BACKUP_KEEP`. Necesita las herramientas cliente de PostgreSQL (`pg_dump`, `pg_restore`) en la máquina donde se ejecuta; en Vercel **no** se puede, así que hay que correrlo desde tu PC o el VPS.
- `python -m radar restore-check ARCHIVO.dump` valida un backup sin tocar ninguna base.
- **La restauración no se automatiza** (es destructiva). Opciones: la restauración a un punto en el tiempo o desde una rama que ofrece Neon, o `pg_restore --clean --if-exists --no-owner --no-privileges --dbname="<URL directa>" ARCHIVO.dump` con la recolección detenida.

## Verificaciones

| Qué | Cómo | Resultado |
|---|---|---|
| Suite completa sobre SQLite | `pytest` | 261 pasan, 4 omitidas (las que exigen Postgres) |
| Suite completa sobre PostgreSQL 18.6 real | `RADAR_TEST_PG_URL=… pytest` (un esquema descartable por prueba) | 258 pasan, 7 omitidas (las propias de SQLite), 0 fallas |
| Migraciones 0001–0006 | `init-db` sobre PostgreSQL | sin cambios en las migraciones |
| Ingesta real de los 4 medios sobre PostgreSQL | `collect --force` | 17 fuentes, 856 notas, 36 relaciones, 12 grupos, 12 alertas; el segundo análisis no crea nada |
| Pantallas con datos reales sobre PostgreSQL | visitante y administrador | 45 páginas, ningún error 5xx |
| `app.py` | se niega sin `RADAR_SECRET_KEY` y sin PostgreSQL; arranca con PostgreSQL | comprobado |
| `/api/cron` | 401 sin `Bearer`, 503 sin `CRON_SECRET`, 400 con parámetros inválidos, "omitido" ante duplicados | pruebas automáticas |
| Bloqueo `job_locks` | 8 intentos simultáneos | exactamente 1 lo obtiene |

## Lo que NO está verificado
- Un despliegue real en Vercel y una base real en Neon (no hubo acceso). En particular: que `excludeFiles` y `maxDuration` se apliquen como se espera y que el paquete incluya `radar/templates` y `radar/static` (la documentación indica que se incluyen los archivos del proyecto alcanzables en la compilación).
- Los tiempos del ciclo y del arranque en frío dentro de Vercel.
- El comportamiento exacto de la suspensión de Neon durante un ciclo largo.

## Si algo falla
| Síntoma | Causa probable |
|---|---|
| 500 en todo el sitio | Falta `RADAR_SECRET_KEY`, `RADAR_DATABASE_URL` o la base no tiene las migraciones (`init-db`) |
| `/api/cron` responde 503 | Falta `CRON_SECRET` en Vercel |
| `/api/cron` responde 401 | El `Bearer` no coincide con `CRON_SECRET` |
| `"omitido"` en el cron | Hay otra ejecución en curso (esperado) o quedó un bloqueo: vence solo a los 20 minutos |
| Sitio pide login | Falta `RADAR_PUBLIC_MODE=true` |
| 429 de visitantes | Se superó `RADAR_PUBLIC_RATE_LIMIT` (por IP) |
| Timeouts al conectar | La base de Neon estaba suspendida; reintentar |
