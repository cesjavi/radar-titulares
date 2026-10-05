# SKILLS.md — Habilidades y comandos de Radar de Titulares

Catálogo de lo que se puede hacer con el proyecto y cómo. Las instrucciones de trabajo y los principios están en [AGENTS.md](AGENTS.md).

**Skills para Claude Code** (se invocan por nombre; viven en `.claude/skills/`):

| Skill | Cuándo usarla |
|---|---|
| `radar-operar` | Levantar, recolectar, diagnosticar fuentes, alertas, backups y la vista pública |
| `radar-agregar-medio` | Sumar un medio nuevo con adaptador propio (descubrimiento de fuentes, adaptador, pruebas y documentación). Para medios sin reglas particulares alcanza con **Fuentes → Agregar medio** en el panel |
| `radar-auditar` | Auditar el sistema contra los principios del producto y verificar con datos reales |

## Comandos (`python -m radar …`)

En Windows usar `.\.venv-win\Scripts\python`. Todos leen la configuración de `.env`.

### Datos y fuentes
| Comando | Qué hace | Notas |
|---|---|---|
| `init-db` | Aplica migraciones y carga medios, subfuentes, temas y secciones prioritarias | Idempotente; no pisa lo que se pausó o personalizó en el panel |
| `probe [--media M]` | Verifica en vivo cada fuente declarada por los adaptadores | No guarda nada; muestra HTTP, tamaño y cantidad de notas |
| `collect [--media M] [--force] [--no-enrich] [--no-analyze]` | Recolecta feeds, sitemaps y portadas; lee páginas de notas; luego analiza y genera alertas | Respeta el intervalo mínimo por subfuente salvo `--force`; bloqueo contra ejecuciones simultáneas |
| `demo-load` / `demo-clear` | Carga o borra los datos ficticios marcados DEMO | `demo-load` requiere `RADAR_DEMO_MODE=true` |

Medios (`M`): `perfil`, `eldestape`, `infobae`, `pagina12` y los que se agreguen desde el panel (slug visible en `/fuentes`; `probe` solo cubre los de adaptador propio).

### Análisis, alertas e IA
| Comando | Qué hace |
|---|---|
| `analyze` | Detecta relaciones y grupos (ventana de 72 h), corre la IA si está activa y actualiza las alertas |
| `ai-status` | Proveedores configurados, uso de hoy, límites y por qué no se puede llamar |
| `ai-analyze [--limit N]` | Analiza con IA hasta N relaciones preseleccionadas (cada una es una llamada al proveedor) |
| `notify` | Envía los avisos pendientes de Telegram (solo si está activado) |

### Usuarios
| Comando | Qué hace |
|---|---|
| `create-admin NOMBRE [--password-stdin]` | Crea un administrador (mínimo 12 caracteres; sin contraseña por defecto) |
| `set-password NOMBRE [--password-stdin]` | Cambia la contraseña y cierra las demás sesiones |

### Mantenimiento
| Comando | Qué hace |
|---|---|
| `backup [--dest DIR] [--keep N]` | SQLite: copia consistente con la API del motor. PostgreSQL: `pg_dump` (formato custom) por la conexión directa. Verificada y con rotación |
| `restore-check ARCHIVO` | SQLite: restaura sobre una copia temporal. PostgreSQL (`.dump`): `pg_restore --list` |
| `restore ARCHIVO --yes` | SQLite: restaura con los servicios detenidos (la base actual se conserva renombrada). PostgreSQL: no se automatiza; informa el comando y la opción de Neon |
| `purge [--dry-run]` | Aplica la retención; conserva lo que tiene decisiones humanas |

Todo esto también se puede lanzar desde **Configuración → Ejecutar ahora** (solo administradores), con las mismas opciones.

### Aplicación web
```powershell
.\.venv-win\Scripts\uvicorn radar.web.app:create_app --factory --port 8000
```
- `RADAR_PUBLIC_MODE=true` habilita la vista pública de solo lectura.
- `RADAR_DEMO_MODE=true` muestra los datos DEMO a usuarios logueados (nunca a visitantes).

### Pruebas
```powershell
.\.venv-win\Scripts\python -m pytest                          # toda la suite (SQLite)
$env:RADAR_TEST_PG_URL="postgresql://postgres@127.0.0.1:55432/radar_test" ; .\.venv-win\Scripts\python -m pytest   # sobre PostgreSQL
.\.venv-win\Scripts\python -m pytest tests\test_analysis.py   # un archivo
```

### PostgreSQL / Neon y Vercel
- `RADAR_DATABASE_URL` (con pooler) y `RADAR_DATABASE_URL_DIRECT` (migraciones y backups) en `.env`; luego `init-db`.
- `app.py` + `vercel.json`: la aplicación en Vercel. `/api/cron?tarea=recolectar|analizar|mantenimiento|todo&media=…` exige `Authorization: Bearer $CRON_SECRET`.
- Guía completa, límites verificados y lo que falta verificar: [VERCEL.md](VERCEL.md).

### Despliegue (en el VPS, como root)
`deploy/scripts/`: `install.sh`, `update.sh` (con backup previo y vuelta atrás automática), `rollback.sh`, `backup.sh`, `restore.sh` y `nginx-site.sh`. Detalle en [DEPLOYMENT.md](DEPLOYMENT.md).

## Mapa de pantallas
| Ruta | Contenido | ¿Visible sin login con vista pública? |
|---|---|---|
| `/` | Resumen | Sí |
| `/noticias`, `/noticias/{id}` | Búsqueda y filtros; ficha con procedencia, versiones y firma | Sí |
| `/grupos`, `/grupos/{id}`, `/relaciones/{id}` | Grupos y comparación lado a lado | Sí (sin correcciones) |
| `/alertas`, `/alertas/{id}` | Alertas con evidencia | Solo las publicables (`RADAR_PUBLIC_ALERTS`) |
| `/cronologia`, `/metricas`, `/fuentes` | Cronología, secuencias entre medios, estado de fuentes | Sí (sin errores internos) |
| `/revision`, `/historial`, `/ejecuciones`, `/configuracion` | Revisión manual, historiales, ejecuciones, ajustes | No |
| `/salud` | Healthcheck sin secretos | Sí |

## Variables de entorno
Lista completa y comentada en `.env.example`. Las más usadas: `RADAR_ENV`, `RADAR_SECRET_KEY`, `RADAR_DATA_DIR`, `RADAR_PUBLIC_MODE`, `RADAR_AI_ENABLED`, `RADAR_AI_PROVIDER`, `FIREWORKS_API_KEY`, `RADAR_FIREWORKS_MODEL`.
