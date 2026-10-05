#!/usr/bin/env bash
# Instalación inicial de Radar de Titulares en un VPS Linux (sin Docker).
#
#   sudo deploy/scripts/install.sh [DIR_FUENTE]
#
# - Crea el usuario de servicio sin privilegios y los directorios.
# - Construye una release con su propio virtualenv y la activa.
# - Crea /etc/radar/radar.env SOLO si no existe (con clave secreta aleatoria).
# - Aplica migraciones, instala las unidades systemd y la retención de logs.
# - NO toca Nginx, DNS ni firewall; no crea el administrador (pide la contraseña).
# Es idempotente: volver a correrlo no pisa la configuración ni los datos.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

SRC="${1:-$(cd "$HERE/../.." && pwd)}"
require_root

log "Verificaciones previas"
command -v systemctl >/dev/null || die "Se requiere systemd."
command -v curl >/dev/null || die "Se requiere curl (apt install curl)."
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 11), sys.version' \
    || die "Se requiere Python 3.11 o superior ($PYTHON_BIN)."
"$PYTHON_BIN" -m venv --help >/dev/null 2>&1 || die "Falta el módulo venv (apt install python3-venv)."
avail_mb=$(df -Pm "$(dirname "$RADAR_HOME")" | awk 'NR==2 {print $4}')
(( avail_mb > 1024 )) || die "Menos de 1 GB libre en $(dirname "$RADAR_HOME")."
if [[ -e "$RADAR_HOME/current" ]]; then
    warn "Ya hay una instalación en $RADAR_HOME. Para actualizar usá update.sh."
fi

log "Usuario de servicio '$RADAR_USER'"
if ! id "$RADAR_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$RADAR_VAR" --no-create-home --shell /usr/sbin/nologin "$RADAR_USER"
fi

log "Directorios"
install -d -m 0755 -o root -g root "$RADAR_HOME" "$RADAR_HOME/releases"
install -d -m 0750 -o root -g "$RADAR_USER" "$RADAR_ETC"
install -d -m 0750 -o "$RADAR_USER" -g "$RADAR_USER" "$RADAR_VAR" "$RADAR_VAR/backups"

log "Archivo de entorno $RADAR_ENV_FILE"
if [[ -f "$RADAR_ENV_FILE" ]]; then
    log "Ya existe: no se modifica."
else
    secret=$("$PYTHON_BIN" -c 'import secrets; print(secrets.token_urlsafe(48))')
    umask 027
    cat > "$RADAR_ENV_FILE" <<EOF
# Radar de Titulares - entorno de producción. Permisos 0640 root:$RADAR_USER.
RADAR_ENV=production
RADAR_SECRET_KEY=$secret
RADAR_DATA_DIR=$RADAR_VAR
RADAR_DATABASE_URL=sqlite:///$RADAR_VAR/radar.db
RADAR_BACKUP_DIR=$RADAR_VAR/backups
RADAR_BACKUP_KEEP=14
RADAR_RETENTION_DAYS=365
RADAR_RUNS_RETENTION_DAYS=30
RADAR_HOST=127.0.0.1
RADAR_PORT=8000
RADAR_DEMO_MODE=false
# Completar con un contacto real (los medios ven este identificador):
RADAR_USER_AGENT=RadarTitulares/0.1 (+https://CAMBIAR-DOMINIO; CAMBIAR-CONTACTO)
RADAR_PUBLIC_URL=https://CAMBIAR-DOMINIO
# IA externa y Telegram: desactivados. Ver DEPLOYMENT.md para activarlos.
RADAR_AI_ENABLED=false
RADAR_TELEGRAM_ENABLED=false
EOF
    chown root:"$RADAR_USER" "$RADAR_ENV_FILE"
    chmod 0640 "$RADAR_ENV_FILE"
    warn "Editá $RADAR_ENV_FILE: RADAR_USER_AGENT y RADAR_PUBLIC_URL."
fi

log "Construyendo release desde $SRC"
release=$(build_release "$SRC")
switch_release "$release"
log "Release activa: $release"

log "Migraciones y datos iniciales"
radar_cli init-db

log "Unidades systemd"
for unit in "${UNITS[@]}"; do
    target="$SYSTEMD_DIR/$unit"
    if [[ -f "$target" ]] && ! cmp -s "$HERE/../systemd/$unit" "$target"; then
        cp -p "$target" "$target.bak-$(date +%Y%m%d%H%M%S)"
        warn "$target existía con otro contenido: se guardó una copia .bak"
    fi
    install -m 0644 "$HERE/../systemd/$unit" "$target"
done
if [[ ! -f /etc/systemd/journald@radar.conf ]]; then
    install -m 0644 "$HERE/../journald/journald@radar.conf" /etc/systemd/journald@radar.conf
fi
systemctl daemon-reload
systemctl enable --now radar-web.service
systemctl enable --now radar-collect.timer radar-maintenance.timer

log "Healthcheck"
if healthcheck 30; then
    log "Panel respondiendo en 127.0.0.1:$(env_value RADAR_PORT 8000)"
else
    die "El panel no respondió. Revisá: journalctl --namespace=radar -u radar-web -n 50"
fi

cat <<EOF

Instalación completa. Pasos siguientes (ver DEPLOYMENT.md):
  1. Crear el administrador:
       sudo -u $RADAR_USER bash -c 'set -a; . $RADAR_ENV_FILE; set +a; cd $RADAR_HOME/current && .venv/bin/python -m radar create-admin admin'
  2. Configurar Nginx y HTTPS:  sudo deploy/scripts/nginx-site.sh SUBDOMINIO
  3. Revisar $RADAR_ENV_FILE (RADAR_USER_AGENT, RADAR_PUBLIC_URL) y reiniciar radar-web.
EOF
