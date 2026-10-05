# AGENTS.md — Radar de Titulares

Guía para agentes y desarrolladores que trabajen en este repositorio. Complementa a [README.md](README.md) (uso), [DEPLOYMENT.md](DEPLOYMENT.md) (VPS) y [PROGRESS.md](PROGRESS.md) (decisiones e historial). Las habilidades reutilizables están en [skills.md](skills.md) y en `.claude/skills/`.

## Qué es
Monitorea Perfil, El Destape Web, Infobae y Página/12, guarda el historial de titulares, detecta coincidencias entre medios con un motor **léxico** (sin IA), arma grupos y alertas, y las muestra en un panel en español. La IA externa y Telegram son opcionales y vienen desactivados.

## Restricciones que no se negocian
- **Producción:** VPS Linux de 2 GB de RAM, posiblemente compartido. Sin Docker, Redis, Celery, Elasticsearch ni modelos locales.
- **Stack:** Python 3.11+, FastAPI, Jinja2, HTMX (servido localmente), SQLAlchemy 2 + Alembic. Base: SQLite (WAL) por defecto, o PostgreSQL (Neon) con `psycopg` 3. **Un solo worker** web. La recolección es un comando aparte (`python -m radar collect`) que dispara un timer.
- **Interfaz en español.** Fechas en UTC en la base y en hora de Buenos Aires en pantalla.
- **Dependencias fijadas** en `requirements.txt` (verificadas juntas). No agregar una dependencia sin necesidad y sin fijarla.

## Principios del producto (afectan al código y a los textos)
1. **Léxico no es semántico.** Una coincidencia indica palabras, frases o entidades compartidas; no que el mensaje sea el mismo ni que haya coordinación. El "enfoque" queda *sin verificar* salvo análisis de IA, y aun así pasa por revisión humana.
2. **El orden temporal no es causalidad.** Nunca escribir ni mostrar que un medio "originó", "copió", "dirige" o "marcó la agenda". Se separan *primera detección* (radar) y *primera publicación según la fuente*.
3. **No inventar horas.** Si la fuente solo da la fecha, `published_at` queda vacío y solo hay `published_date`. La diferencia temporal entre notas solo se calcula si ambas tienen hora.
4. **Una réplica no es confirmación independiente.** Las republicaciones (agencias, Bloomberg en Perfil) no cuentan como medio independiente. Canal E, Revista Noticias y Perfil Córdoba cuentan como Perfil.
5. **Un nombre compartido no basta.** Las coincidencias distintivas excluyen entidades y términos frecuentes ("Milei").
6. **Afirmaciones opuestas no son el mismo mensaje.** Se conservan negaciones, cifras y el cese ("dejó de"); nunca se eliminan como palabras vacías.
7. **La revisión humana manda.** Reprocesar no puede borrar ni cambiar revisiones, grupos corregidos (`locked`) ni notas separadas a mano.
8. **No determinar la verdad.** El sistema marca discrepancias para revisión; no dice qué afirmación es cierta.

## Estructura
```
radar/
  config.py db.py models.py security.py timeutil.py text.py queries.py net.py
  joblock.py (bloqueo exclusivo SQLite/PostgreSQL)  pipeline.py (ciclos)  pgbackup.py
app.py + vercel.json   punto de entrada y configuración para Vercel (VERCEL.md)
  collector/   adapters/ (uno por medio), generic.py (medios dados de alta en el panel), parsers.py, html.py, ingest.py, enrich.py,
               references.py, runner.py
  analysis/    textproc.py, vectorize.py (TF-IDF propio), relations.py (reglas),
               grouping.py (anti-encadenamiento), engine.py, manual.py
  alerts/      config.py, engine.py (una alerta por grupo), common_source.py
  ai/          config.py, providers.py, openai_compat.py (Groq/Fireworks), prompt.py,
               schema.py, service.py
  notify/      telegram.py          maintenance.py (backup/retención)       metrics.py
  web/         app.py, deps.py, routes/      templates/      static/
alembic/versions/   0001 … 0006        deploy/   systemd, nginx, scripts
tests/              fixtures/ (sintéticos)
```

## Comandos
```powershell
# Windows: usar SIEMPRE un venv propio. El .venv del proyecto quedó atado a WSL.
python -m venv .venv-win ; .\.venv-win\Scripts\pip install -r requirements-dev.txt
.\.venv-win\Scripts\python -m radar init-db            # migraciones + datos iniciales (idempotente)
.\.venv-win\Scripts\python -m radar create-admin NOMBRE
.\.venv-win\Scripts\python -m radar collect [--media M] [--force] [--no-enrich] [--no-analyze]
.\.venv-win\Scripts\python -m radar probe [--media M]  # verifica las fuentes en vivo, no guarda nada
.\.venv-win\Scripts\python -m radar analyze            # relaciones, grupos, IA (si está activa) y alertas
.\.venv-win\Scripts\python -m radar ai-status | ai-analyze --limit 1
.\.venv-win\Scripts\python -m radar backup | restore-check ARCHIVO | purge --dry-run
.\.venv-win\Scripts\uvicorn radar.web.app:create_app --factory --port 8000
.\.venv-win\Scripts\python -m pytest                   # toda la suite (SQLite)
# La misma suite sobre PostgreSQL (un esquema descartable por prueba):
$env:RADAR_TEST_PG_URL = "postgresql://postgres@127.0.0.1:55432/radar_test" ; .\.venv-win\Scripts\python -m pytest
```
Para levantar un PostgreSQL descartable sin tocar el que esté instalado: `initdb -D <carpeta temporal> -U postgres -A trust -E UTF8 --locale=C` y `pg_ctl -D <carpeta> -o "-p 55432 -c listen_addresses=127.0.0.1" -l <log> start` (puerto distinto del 5432, solo localhost); al terminar, `pg_ctl -D <carpeta> stop` y borrar la carpeta. Detener **solo** ese clúster, por su carpeta de datos.
Ver `skills.md` para la lista completa y qué hace cada una.

## Reglas de trabajo
- **Antes de modificar:** leer el código existente y este archivo. No sobrescribir trabajo ajeno ni borrar archivos sin necesidad.
- **Cambios de modelo → migración Alembic** nueva (`000N_nombre.py`, con `alembic revision --autogenerate` y renombrando el archivo). Compatible hacia atrás: agregar con valores por defecto.
- **Cambios en las reglas del motor léxico → subir `ALGORITHM_VERSION`** (`radar/analysis/__init__.py`): se guarda en cada relación y grupo.
- **Cambios en el prompt de IA → subir `PROMPT_VERSION`** (`radar/ai/__init__.py`); invalida la caché.
- **Todo POST exige CSRF** (`verify_csrf`) y las acciones, rol administrador. Las rutas de lectura usan `viewer` (permite la vista pública si está activa) o `require_user`. El contenido externo se escapa siempre (Jinja2 autoescape; nunca `|safe` con datos de medios).
- **Red:** toda descarga pasa por `radar/net.py` (SSRF, dominios permitidos, redirecciones validadas, robots.txt, límites). No usar `httpx` directo para contenido de medios.
- **Transacciones breves**; la recolección escribe en lotes y desde un solo hilo.
- **Consultas y modelos portables entre SQLite y PostgreSQL.** No usar funciones propias de SQLite (por ejemplo `func.min(a, b)` de dos argumentos: usar `pair_filter`), ni `PRAGMA` fuera de `radar/db.py`. Los índices parciales llevan `sqlite_where` **y** `postgresql_where`. PostgreSQL sí aplica los largos de `String(n)` y SQLite no: truncar al guardar. Todo cambio de consultas, modelos o migraciones se prueba en **los dos** motores (`pytest` y `pytest` con `RADAR_TEST_PG_URL`); las pruebas que usan archivos o funciones de SQLite van con `@pytest.mark.sqlite_only`, y las que exigen `pg_dump`, con `pg_only`.
- **Bloqueo de ejecución exclusiva:** siempre `radar.joblock.job_lock()` (archivo en SQLite, tabla `job_locks` con vencimiento en PostgreSQL). No usar `ProcessLock` directamente ni advisory locks de sesión: el pooler de Neon (PgBouncer, modo transacción) no los admite.
- **PostgreSQL / Neon:** la app usa la URL **con pooler** (`RADAR_DATABASE_URL`); las migraciones y `pg_dump` usan la **directa** (`RADAR_DATABASE_URL_DIRECT`, o la misma sin `-pooler`). Las migraciones no se aplican al arrancar la función de Vercel: se corre `init-db` aparte, antes de desplegar el código nuevo.
- **Vercel (`app.py`):** sin clave secreta por defecto (si falta `RADAR_SECRET_KEY` no arranca), se niega a usar SQLite salvo `RADAR_ALLOW_EPHEMERAL_DB=true`, y `/api/cron` exige `CRON_SECRET`. La IP del visitante se toma de `x-forwarded-for` **solo** en Vercel (lo sobrescribe); en cualquier otro entorno ese encabezado es falsificable y se ignora.
- **Textos de interfaz y mensajes en español rioplatense.** Código, nombres de variables y commits en el idioma del código existente.
- **Pruebas:** agregar pruebas solo donde cubran un riesgo concreto, con fixtures **sintéticos**. No usar noticias reales ni acusaciones reales como fixtures.

## Pruebas: aislamiento obligatorio
- `tests/conftest.py` crea una base nueva por prueba, define `RADAR_SKIP_DOTENV=1` y borra las claves de proveedores. **Las pruebas nunca leen el `.env` local.**
- **Ninguna prueba hace llamadas reales** a proveedores de IA ni a Telegram (están bloqueadas con `PYTEST_CURRENT_TEST`). Se usan proveedores y transportes simulados.
- No hacer llamadas pagas ni enviar mensajes reales sin configuración y autorización explícita del usuario.

## Secretos y datos
- Los secretos van en `.env` (local, ignorado por git) o en `/etc/radar/radar.env` (producción, 0640). **Nunca** en el código, en la salida de comandos ni en los logs. Al mostrar un `.env`, enmascarar claves.
- `data/` (base, bloqueo, backups) no se versiona. No hay contraseña de administrador por defecto: se crea con `create-admin`.

## Trampas conocidas
- **Windows y WSL no deben compartir la base ni el `.venv`.** SQLite en WAL sobre `/mnt/*` no coordina bloqueos entre ambos y la base puede corromperse (ya pasó una vez). En WSL, usar un venv propio y una base en el sistema de archivos de Linux.
- **Medios que cambian su estructura:** `probe` verifica las fuentes. Un feed vacío se registra como `empty` ("posible cambio de formato"), no como "sin noticias". Los identificadores de origen deben ser realmente únicos: el número final de las URLs de El Destape es la hora de publicación, no un id.
- **Heredocs con comillas anidadas en Git Bash** fallan con facilidad al editar archivos grandes: escribir un script `.py` aparte y ejecutarlo.
- **Finales de línea en Windows:** `Path.write_text()` de Python escribe CRLF. Los scripts de `deploy/` (shell, systemd, nginx) **deben ser LF**: con CRLF, bash falla (`unexpected token $'do\r'`). Al editarlos desde Python, escribir bytes con `\n` o usar `newline="\n"`, y comprobar con `bash -n` (en WSL). `.gitattributes` fuerza LF en `deploy/`.
- **Una base PostgreSQL vacía ya ocupa ~8 MB**, y cuesta ~7 KB por nota; el plan gratuito de Neon tiene 1 GB, así que conviene `RADAR_RETENTION_DAYS=90`.
- **Probar contra datos reales** después de tocar el motor de coincidencias: los fixtures sintéticos no detectan falsos positivos como "estado de São Paulo" o "boca de urna".

## Estado de verificación
Ver [PROGRESS.md](PROGRESS.md). Lo que **no** está verificado: el despliegue en el VPS, las llamadas reales a Groq/Fireworks/Anthropic y los envíos reales de Telegram.
