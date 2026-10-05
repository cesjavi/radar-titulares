# Despliegue en VPS Linux (2 GB de RAM)

> **Estado: preparado, NO desplegado.** No hubo acceso al VPS, así que nada de esto se ejecutó en el servidor. Los scripts y las unidades se verificaron en Ubuntu 24.04 (WSL2) con `bash -n` y `systemd-analyze verify`, y la aplicación se probó y midió ahí sobre una copia aislada (ver [Rendimiento](#rendimiento)).

## Datos que faltan

1. **Acceso al VPS:** IP o host, usuario SSH con sudo.
2. **Distribución y versión**, por ejemplo Ubuntu 24.04 o Debian 12. Se necesita Python ≥ 3.11 y systemd ≥ 245.
3. **Subdominio** para el panel (ej. `radar.midominio.com`). El **registro DNS** lo creás vos; no se toca DNS ni firewall sin tu autorización.
4. **Email** para Let's Encrypt.
5. **Nginx:** si ya hay uno funcionando con otros sitios. El script agrega un sitio nuevo sin tocar `nginx.conf`.
6. **Puerto local libre** para uvicorn (por defecto 8000).

## Diseño

| Elemento | Ubicación / valor |
|---|---|
| Usuario de servicio | `radar` (sistema, sin login, sin sudo) |
| Código | `/opt/radar/releases/<fecha>/` (root:radar, solo lectura para el servicio); `/opt/radar/current` → release activa |
| Virtualenv | uno por release: `/opt/radar/current/.venv` |
| Secretos y entorno | `/etc/radar/radar.env` (0640 root:radar) |
| Datos (SQLite, lock, backups) | `/var/lib/radar/` (0750 radar:radar), fuera de cualquier directorio público |
| Panel | uvicorn, **1 worker**, `127.0.0.1:${RADAR_PORT}` (8000), detrás de Nginx con HTTPS |
| Recolección | `radar-collect.service` (oneshot) + `radar-collect.timer` (cada 10 min) |
| Backup y retención | `radar-maintenance.service` + timer diario (04:30 hora de Buenos Aires) |
| Logs | journald, espacio propio `radar` (200 MB, 30 días); no modifica la configuración global |

**Ejecuciones superpuestas.** Están bloqueadas de tres maneras:
- systemd no inicia un oneshot mientras otro corre;
- la app toma un `flock` en `/var/lib/radar/collect.lock` (también lo usan `analyze`, `restore` y las ejecuciones manuales);
- `update.sh` y `restore.sh` detienen el timer y esperan a que termine el ciclo en curso.

**Hardening.**
- `ProtectSystem=strict` con `ReadWritePaths=/var/lib/radar`: lo único escribible es el directorio de datos.
- Además: `NoNewPrivileges`, `PrivateTmp`, `PrivateDevices`, `ProtectHome`, sin capabilities, `RestrictAddressFamilies`, `SystemCallArchitectures=native` y `UMask=0027`.

**Límites de recursos.**

| Servicio | Memoria | Otros |
|---|---|---|
| Panel | `MemoryHigh=250M`, `MemoryMax=350M` | `CPUQuota=60%` |
| Recolección | `MemoryMax=450M` | `TimeoutStartSec=9min`, `Nice=10`, E/S en modo ocioso |
| Mantenimiento | `MemoryMax=300M` | `TimeoutStartSec=30min` |

**Reinicios.** El panel se reinicia ante fallos (`Restart=on-failure`), con un límite de 5 reinicios cada 5 minutos.

## Instalación

En tu máquina, copiá el proyecto al servidor (sin `.venv`, `data` ni `.env`):

```bash
rsync -av --exclude '.venv*' --exclude data --exclude .env --exclude '__pycache__' \
  ./ USUARIO@VPS:/tmp/radar-src/
```

En el VPS, primero los comandos de **solo lectura** para inspeccionar:

```bash
cat /etc/os-release; python3 --version; systemctl --version | head -1
free -m; df -h /opt /var/lib
sudo ss -ltnp                           # servicios y puertos en uso
nginx -v 2>&1; ls /etc/nginx/sites-enabled /etc/nginx/conf.d 2>/dev/null
```

Paquetes necesarios en Debian/Ubuntu (instalarlos solo si faltan):

```bash
sudo apt install python3 python3-venv curl            # app
sudo apt install nginx certbot                        # solo si no hay proxy todavía
```

Instalación:

```bash
cd /tmp/radar-src
sudo bash deploy/scripts/install.sh /tmp/radar-src
```

`install.sh` hace lo siguiente:
- verifica Python, venv, systemd y espacio en disco;
- crea el usuario y los directorios, y construye la release con su venv;
- crea `/etc/radar/radar.env` **solo si no existe**, con `RADAR_SECRET_KEY` aleatoria;
- aplica las migraciones e instala las unidades (si había una con el mismo nombre y distinto contenido, guarda un `.bak`);
- habilita el panel y los timers, y corre el healthcheck.

No toca Nginx, DNS ni el firewall.

Después:

```bash
# 1. Completar el entorno (contacto real en el User-Agent, URL pública)
sudo nano /etc/radar/radar.env
sudo systemctl restart radar-web

# 2. Crear el administrador (pide la contraseña; no hay contraseña por defecto)
sudo -u radar bash -c 'set -a; . /etc/radar/radar.env; set +a; cd /opt/radar/current && .venv/bin/python -m radar create-admin admin'

# 3. Nginx + HTTPS (requiere que el DNS del subdominio ya apunte al VPS y los puertos 80/443 abiertos)
sudo bash deploy/scripts/nginx-site.sh radar.midominio.com correo@midominio.com
```

### Nginx y HTTPS
- **Plantillas:** `deploy/nginx/radar-http-bootstrap.conf` (solo para el desafío de Let's Encrypt) y `deploy/nginx/radar.conf` (redirección a HTTPS y proxy a `127.0.0.1:8000` con `Host`, `X-Forwarded-For` y `X-Forwarded-Proto`).
  - Las cabeceras de seguridad (CSP, HSTS, X-Frame-Options…) las envía la app.
  - uvicorn confía en `X-Forwarded-*` solo si vienen de 127.0.0.1.
- **Instalación del sitio:** `nginx-site.sh` lo instala en `sites-available/radar.conf` (o en `conf.d/radar.conf`).
  - No toca `nginx.conf` ni otros sitios, y se niega a continuar si otro sitio ya usa el subdominio.
  - Corre **`nginx -t` antes de cada recarga**. Si la prueba falla, restaura la configuración anterior y no recarga.
- **Certificado:** se emite con `certbot certonly --webroot` y un *deploy hook* que valida la configuración y recarga Nginx.
- **Renovación:** la hace el timer o cron del paquete certbot. Para comprobarla: `sudo certbot renew --dry-run`.
- **Manual:** si preferís hacerlo a mano, copiá la plantilla, reemplazá `radar.example.com` y corré `sudo nginx -t && sudo systemctl reload nginx`.

## Variables de entorno (`/etc/radar/radar.env`)

| Variable | Obligatoria | Valor en producción / descripción |
|---|---|---|
| `RADAR_ENV` | sí | `production` (cookie Secure, HSTS) |
| `RADAR_SECRET_KEY` | sí | ≥ 32 caracteres aleatorios (la genera `install.sh`) |
| `RADAR_DATA_DIR` | sí | `/var/lib/radar` (base, lock; backups por defecto en `backups/`) |
| `RADAR_DATABASE_URL` | no | `sqlite:////var/lib/radar/radar.db` |
| `RADAR_BACKUP_DIR` / `RADAR_BACKUP_KEEP` | no | `/var/lib/radar/backups` / `14` |
| `RADAR_RETENTION_DAYS` | no | `365` (notas; `0` = no borrar). `RADAR_RUNS_RETENTION_DAYS=30` (ejecuciones, uso de IA, avisos) |
| `RADAR_HOST` / `RADAR_PORT` | no | `127.0.0.1` / `8000` (si cambia el puerto, ajustar Nginx) |
| `RADAR_USER_AGENT` | recomendado | Identificador con contacto real para los medios |
| `RADAR_PUBLIC_URL` | recomendado | `https://subdominio` (enlaces de los avisos) |
| `RADAR_MAX_CONCURRENCY`, `RADAR_REQUEST_DELAY`, `RADAR_ENRICH_PER_MEDIA`, `RADAR_HTTP_TIMEOUT`, `RADAR_HTTP_RETRIES`, `RADAR_MAX_RESPONSE_BYTES` | no | Recolección (ver `.env.example`) |
| `RADAR_TELEGRAM_*` | no | Avisos opcionales (desactivados) |
| `RADAR_AI_*`, `GROQ_API_KEY`, `RADAR_GROQ_MODEL`, `FIREWORKS_API_KEY`, `RADAR_FIREWORKS_MODEL` | no | IA opcional (desactivada) |
| `RADAR_DEMO_MODE` | no | `false` en producción |
| `RADAR_PUBLIC_MODE` / `RADAR_PUBLIC_ALERTS` / `RADAR_PUBLIC_RATE_LIMIT` | no | Vista pública de solo lectura (`false` por defecto) / `revisadas` / `120` por minuto e IP |

### Vista pública en producción
- **Activación:** con `RADAR_PUBLIC_MODE=true`, el subdominio queda visible sin login, en modo de solo lectura. Antes, conviene revisar qué alertas se publican: con `revisadas`, solo las que marcaste como revisadas.
- **Límite por IP:** lo aplica la app con la IP real que pasa Nginx (`X-Forwarded-For`, que se acepta solo desde 127.0.0.1). Si el sitio recibe mucho tráfico, se puede sumar `limit_req` en Nginx dentro de `location /`: necesita una `limit_req_zone`, que va en el contexto `http`, es decir, en un archivo propio de `conf.d/` y no en `nginx.conf`.
- **Indexación:** los buscadores pueden indexar el sitio. Si no querés que aparezca en ellos, agregá en `radar.conf` la línea `add_header X-Robots-Tag "noindex, nofollow" always;`.

## Variante: PostgreSQL (Neon) en lugar de SQLite
El VPS también puede usar una base PostgreSQL gestionada (por ejemplo Neon): definí `RADAR_DATABASE_URL` (con pooler) y `RADAR_DATABASE_URL_DIRECT` en `/etc/radar/radar.env` y corré `init-db`. Diferencias operativas:
- El backup diario (`radar-maintenance.service`) usa `pg_dump`: instalá `postgresql-client` (`sudo apt install postgresql-client`) con una versión **igual o mayor** a la del servidor.
- `update.sh` hace el backup previo igual, pero ante un fallo **no restaura la base sola**: las migraciones solo agregan, así que el código anterior suele funcionar; si no, restaurá a mano o con la restauración a un punto en el tiempo de Neon.
- `restore.sh` es solo para SQLite; con PostgreSQL ver [VERCEL.md](VERCEL.md#backups-y-restauración-con-neon).
- Conviene `RADAR_RETENTION_DAYS=90` en el plan gratuito de Neon (1 GB).

## Operación

| Tarea | Comando |
|---|---|
| Estado | `systemctl status radar-web radar-collect.timer radar-maintenance.timer` |
| Próximas ejecuciones | `systemctl list-timers 'radar-*'` |
| Logs | `journalctl --namespace=radar -u radar-web -f` · `journalctl --namespace=radar -u radar-collect -n 100` |
| Healthcheck | `curl -s http://127.0.0.1:8000/salud` → `{"estado":"ok","base":true,"ultima_recoleccion_min":N,"version":"…"}` (sin datos ni secretos; `degradado` si la última recolección exitosa tiene más de 30 min; HTTP 503 si la base no responde) |
| Ciclo manual | `sudo systemctl start radar-collect.service` |
| Comandos de la app | `sudo -u radar bash -c 'set -a; . /etc/radar/radar.env; set +a; cd /opt/radar/current && .venv/bin/python -m radar <comando>'` |
| Migraciones | `… -m radar init-db` (idempotente; `update.sh` lo corre solo) |
| Retención | `… -m radar purge --dry-run` y luego `… -m radar purge` (también corre a diario) |

### Backups
- **Automático:** todos los días, con `radar-maintenance.timer`. Se usa la **API de backup de SQLite** (`sqlite3.Connection.backup`), nunca una copia directa del `.db`, porque con WAL activo esa copia puede quedar inconsistente.
- **Verificación:** cada backup pasa `PRAGMA integrity_check`. Se conservan los últimos `RADAR_BACKUP_KEEP`.
- **Manual:** `sudo bash deploy/scripts/backup.sh`. Hace el backup y lo verifica restaurándolo sobre una copia.
- **Copia externa:** recomendada, porque los backups quedan en el mismo disco. Por ejemplo `rsync` de `/var/lib/radar/backups/` a otro equipo, o un bucket. No se configuró.

### Restauración (documentada y comprobada sobre una copia)

```bash
ls /var/lib/radar/backups/
sudo -u radar bash -c 'set -a; . /etc/radar/radar.env; set +a; cd /opt/radar/current && .venv/bin/python -m radar restore-check /var/lib/radar/backups/radar-AAAAMMDD-HHMMSS.db'
sudo bash deploy/scripts/restore.sh /var/lib/radar/backups/radar-AAAAMMDD-HHMMSS.db
```

`restore-check` restaura en un directorio temporal y comprueba la integridad, la versión de esquema y los conteos, sin tocar la base real.

`restore.sh` hace lo siguiente:
- pide confirmación;
- detiene el timer y el panel, y restaura;
- **no borra** la base actual: queda como `radar.db.antes-de-restaurar-<fecha>`;
- aplica las migraciones, reinicia y corre el healthcheck.

Las pruebas automáticas (`tests/test_maintenance.py`) cubren el backup con WAL activo, la rotación, la verificación, la restauración y la detección de un backup dañado.

### Actualización (con respaldo previo)

```bash
rsync -av --exclude '.venv*' --exclude data --exclude .env ./ USUARIO@VPS:/tmp/radar-src/
sudo bash /tmp/radar-src/deploy/scripts/update.sh /tmp/radar-src
```

`update.sh` hace lo siguiente:
1. Hace un backup verificado. Si falla, aborta sin cambios.
2. Construye la release nueva mientras la actual sigue funcionando.
3. Pausa el timer y espera a que termine un ciclo en curso.
4. Cambia el enlace `current` de forma atómica, migra y reinicia el panel.
5. Corre el healthcheck. **Si algo falla, vuelve sola a la release anterior.** Si el esquema ya había cambiado, también restaura el backup del paso 1.

Se conservan las últimas 3 releases (`RADAR_KEEP_RELEASES`).

### Rollback de código y cambios de esquema

```bash
sudo bash deploy/scripts/rollback.sh                       # a la release previa
sudo bash deploy/scripts/rollback.sh 20261004-120000 /var/lib/radar/backups/radar-….db   # release + base
```

- **Migraciones:** las de Alembic agregan tablas o columnas con valores por defecto, así que el código anterior suele funcionar con el esquema nuevo y alcanza con volver el código.
- **Cambio de esquema incompatible:** si una migración no es compatible hacia atrás, se vuelve también la base con el backup que `update.sh` informa al empezar. Antes de restaurar, el script respalda la base actual.
- **Para migraciones futuras:** conviene hacerlas en dos pasos (primero agregar, después dejar de usar) para que el rollback de código no requiera restaurar la base.

## Rendimiento

Medido el 2026-10-04 en **WSL2 con Ubuntu 24.04, Python 3.12, 12 núcleos y 15 GB de RAM**, sobre una copia aislada con datos reales. **No es el VPS:** la CPU, el disco y la red difieren, y la memoria de un proceso Python es comparable, pero los tiempos no. Conviene repetir las mediciones en el servidor (comandos abajo).

| Medición | Resultado |
|---|---|
| Panel en reposo (RSS) | 77 MB |
| Panel tras login y 30 páginas | 85 MB |
| Pico del ciclo completo (feeds + 30 páginas + análisis + alertas) | 117 MB |
| Pico del análisis solo (676 notas, ~9.900 pares candidatos) | 100 MB, 2,1 s |
| Duración del primer ciclo (676 notas nuevas) | 153 s, casi todo por las pausas entre solicitudes; El Destape exige 10 s |
| Duración de un ciclo sin fuentes vencidas (solo páginas pendientes) | 158 s (30 páginas) |
| Tamaño de la base con 676 notas | 3,8 MB (~5,8 KB por nota, incluido el cuerpo de Infobae) |

**Lectura.** El total en uso simultáneo (panel más ciclo) ronda los 200 MB, holgado para 2 GB compartidos. Los `MemoryMax` de las unidades dejan margen.

El ciclo está dominado por las esperas de cortesía, no por la CPU. Con 10 páginas por medio, entra en 9 minutos. Si en el VPS se acerca al límite, bajá `RADAR_ENRICH_PER_MEDIA`.

**Proyección de la base** (supuesto a confirmar en el VPS): unas 600 notas nuevas por día da unos 3,5 MB por día, o sea ~1,3 GB por año con retención de 365 días. Se ajusta con `RADAR_RETENTION_DAYS`.

Para medir en el VPS:

```bash
systemctl show radar-web -p MemoryCurrent
systemctl show radar-collect -p MemoryPeak        # systemd ≥ 255; si no, ver el journal
journalctl --namespace=radar -u radar-collect -n 5 | grep -E 'Subfuentes|Análisis'
systemctl show radar-collect -p ExecMainStartTimestamp -p ExecMainExitTimestamp
du -h /var/lib/radar/radar.db
```

## Verificaciones realizadas

- **Pruebas:** 200 en verde en Windows (Python 3.11). En Linux (Ubuntu 24.04, Python 3.12) la suite corrió completa y sin fallos.
- **Corrección para producción:** el archivo de bloqueo ya no queda en el directorio del código, que es de solo lectura con `ProtectSystem=strict`. Ahora va en `RADAR_DATA_DIR`.
- **Scripts:** `bash -n` sin errores; sin finales de línea CRLF.
- **Unidades:** `systemd-analyze verify` sin errores de directivas. Solo avisa que faltan los binarios, porque no se instaló nada.
- **En Linux, sobre una copia en `/tmp`:** se probaron la recolección real, el análisis, las alertas, el healthcheck, el backup con WAL activo, `restore-check` y `purge --dry-run`.

## Pendientes

- [ ] Ejecutar en el VPS, cuando haya acceso y autorización: inspección de solo lectura, `install.sh`, creación del administrador y `nginx-site.sh`.
- [ ] Copia de backups fuera del servidor.
- [ ] Repetir las mediciones de rendimiento en el VPS.
- [ ] Monitoreo externo del endpoint `/salud` (por ejemplo, un servicio de uptime).
- [ ] Confirmar que la versión de systemd del VPS admite `LogNamespace` (≥ 245). Si no, quitar esa línea de las unidades y la retención queda en la configuración global de journald.
