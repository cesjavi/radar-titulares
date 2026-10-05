#!/usr/bin/env bash
# Vuelve a una release anterior del código.
#
#   sudo deploy/scripts/rollback.sh                  # a la release previa
#   sudo deploy/scripts/rollback.sh RELEASE [BACKUP] # a una release y, opcional, restaurar un backup
#
# Estrategia de esquema: las migraciones agregan tablas/columnas con valores por defecto, por
# lo que el código anterior suele funcionar con el esquema nuevo. Si no es así, indicar el
# BACKUP tomado antes de la actualización (update.sh lo informa) para volver también la base.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"
require_root

current=$(current_release)
if [[ $# -ge 1 ]]; then
    target="$1"
    [[ "$target" == /* ]] || target="$RADAR_HOME/releases/$target"
else
    target=$(ls -1d "$RADAR_HOME"/releases/*/ | sed 's#/$##' | sort | grep -B1 -x "$current" | head -n1)
fi
[[ -d "$target" && "$target" != "$current" ]] || die "Release destino inválida: ${target:-ninguna}. Disponibles: $(ls "$RADAR_HOME/releases")"

log "Volviendo de $current a $target"
systemctl stop radar-collect.timer
for _ in $(seq 120); do systemctl is-active --quiet radar-collect.service || break; sleep 5; done
if [[ $# -ge 2 ]]; then
    systemctl stop radar-web.service
    radar_cli backup >/dev/null || die "No se pudo respaldar la base actual antes de restaurar."
    switch_release "$target"
    radar_cli restore "$2" --yes
else
    switch_release "$target"
fi
systemctl restart radar-web.service
healthcheck 30 || die "El panel no respondió tras la vuelta atrás."
systemctl start radar-collect.timer
log "Listo. Release activa: $(current_release)"
