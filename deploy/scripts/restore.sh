#!/usr/bin/env bash
# Restauración de un backup.
#
#   sudo deploy/scripts/restore.sh /var/lib/radar/backups/radar-AAAAMMDD-HHMMSS.db
#
# 1. Verifica el backup sobre una copia temporal (restore-check).
# 2. Detiene el panel y la recolección.
# 3. Restaura: la base actual NO se borra, queda como radar.db.antes-de-restaurar-<fecha>.
# 4. Aplica migraciones (por si el backup es de un esquema anterior) y vuelve a iniciar todo.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"
require_root
[[ $# -eq 1 && -f "$1" ]] || die "Uso: restore.sh ARCHIVO_DE_BACKUP"

log "Verificando el backup sobre una copia"
radar_cli restore-check "$1" || die "El backup no es restaurable."
read -r -p "¿Restaurar $1? Se detendrán los servicios. Escribí 'restaurar': " answer
[[ "$answer" == "restaurar" ]] || die "Cancelado."

systemctl stop radar-collect.timer
for _ in $(seq 120); do systemctl is-active --quiet radar-collect.service || break; sleep 5; done
systemctl stop radar-web.service
radar_cli restore "$1" --yes
radar_cli init-db
systemctl start radar-web.service
healthcheck 30 || die "El panel no respondió tras restaurar."
systemctl start radar-collect.timer
log "Restauración completa."
