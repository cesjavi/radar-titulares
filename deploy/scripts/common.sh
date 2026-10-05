#!/usr/bin/env bash
# Configuración compartida de los scripts. Todo es sobreescribible por entorno.
set -euo pipefail

RADAR_USER="${RADAR_USER:-radar}"
RADAR_HOME="${RADAR_HOME:-/opt/radar}"            # código: releases/ y current
RADAR_ETC="${RADAR_ETC:-/etc/radar}"              # radar.env (secretos)
RADAR_VAR="${RADAR_VAR:-/var/lib/radar}"          # base, bloqueo, backups
RADAR_ENV_FILE="${RADAR_ENV_FILE:-$RADAR_ETC/radar.env}"
RADAR_KEEP_RELEASES="${RADAR_KEEP_RELEASES:-3}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
UNITS=(radar-web.service radar-collect.service radar-collect.timer
       radar-maintenance.service radar-maintenance.timer)

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mAVISO:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

require_root() { [[ $EUID -eq 0 ]] || die "Ejecutar como root (sudo)."; }

# Ejecuta un comando de la app como el usuario de servicio, con el entorno de producción.
as_radar() {
    runuser -u "$RADAR_USER" -- env -i PATH=/usr/bin:/bin HOME="$RADAR_VAR" \
        PYTHONDONTWRITEBYTECODE=1 bash -c \
        'set -a; . "$0"; set +a; cd "$1"; shift 2; exec "$@"' \
        "$RADAR_ENV_FILE" "$RADAR_HOME/current" _ "$@"
}

radar_cli() { as_radar "$RADAR_HOME/current/.venv/bin/python" -m radar "$@"; }

env_value() {  # env_value CLAVE [defecto]
    local v
    v=$(grep -E "^$1=" "$RADAR_ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2- || true)
    echo "${v:-${2:-}}"
}

healthcheck() {  # healthcheck [intentos]
    local port tries=${1:-20}
    port=$(env_value RADAR_PORT 8000)
    for _ in $(seq "$tries"); do
        if curl -fsS "http://127.0.0.1:${port}/salud" 2>/dev/null | grep -q '"base":true'; then
            return 0
        fi
        sleep 1
    done
    return 1
}

current_release() { readlink -f "$RADAR_HOME/current" 2>/dev/null || true; }

schema_version() {  # versión de Alembic de la base actual (SQLite o PostgreSQL), o "vacia"
    as_radar "$RADAR_HOME/current/.venv/bin/python" - <<'PY' 2>/dev/null || echo "desconocida"
from sqlalchemy import text
from radar.db import get_engine
try:
    with get_engine().connect() as c:
        print(c.execute(text("select version_num from alembic_version")).scalar() or "vacia")
except Exception:
    print("vacia")
PY
}

is_postgres() {  # ¿la base configurada es PostgreSQL?
    as_radar "$RADAR_HOME/current/.venv/bin/python" -c \
        'import sys; from radar.config import get_settings; sys.exit(0 if get_settings().is_postgres else 1)' \
        2>/dev/null
}

# Crea una release nueva desde un directorio fuente (sin .venv, datos ni secretos).
build_release() {  # build_release DIR_FUENTE -> imprime ruta de la release
    local src="$1" stamp dest
    [[ -f "$src/requirements.txt" && -d "$src/radar" ]] || die "No parece el proyecto: $src"
    stamp=$(date -u +%Y%m%d-%H%M%S)
    dest="$RADAR_HOME/releases/$stamp"
    mkdir -p "$dest"
    tar -C "$src" \
        --exclude='./.venv*' --exclude='./data' --exclude='./.env' --exclude='*.pyc' \
        --exclude='__pycache__' --exclude='./.pytest_cache' --exclude='./.git' \
        -cf - . | tar -C "$dest" -xf -
    "$PYTHON_BIN" -m venv "$dest/.venv" >&2
    "$dest/.venv/bin/pip" install --quiet --upgrade pip >&2
    "$dest/.venv/bin/pip" install --quiet -r "$dest/requirements.txt" >&2
    chown -R root:"$RADAR_USER" "$dest"
    chmod -R go-w "$dest"
    chmod -R g+rX "$dest"
    echo "$dest"
}

switch_release() {  # switch_release RUTA  (cambio atómico del enlace current)
    ln -sfn "$1" "$RADAR_HOME/current.tmp"
    mv -T "$RADAR_HOME/current.tmp" "$RADAR_HOME/current"
}

prune_releases() {
    local cur keep=$RADAR_KEEP_RELEASES
    cur=$(current_release)
    mapfile -t old < <(ls -1d "$RADAR_HOME"/releases/*/ 2>/dev/null | sed 's#/$##' | sort | head -n -"$keep")
    for r in "${old[@]}"; do
        [[ "$r" == "$cur" ]] && continue
        log "Eliminando release antigua $r"
        rm -rf -- "$r"
    done
}
