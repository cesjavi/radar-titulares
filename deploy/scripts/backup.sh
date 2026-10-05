#!/usr/bin/env bash
# Backup manual consistente (API de backup de SQLite) + verificación sobre una copia.
#
#   sudo deploy/scripts/backup.sh [DIR_DESTINO]
#
# No copia el .db directamente (con WAL activo esa copia puede ser inconsistente).
# El backup diario automático lo hace radar-maintenance.timer.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"
require_root

args=()
[[ $# -ge 1 ]] && { install -d -m 0750 -o "$RADAR_USER" -g "$RADAR_USER" "$1"; args=(--dest "$1"); }
out=$(radar_cli backup "${args[@]}")
echo "$out"
file=$(sed -n 's/^Backup verificado: \(.*\) (.*/\1/p' <<<"$out")
radar_cli restore-check "$file"
