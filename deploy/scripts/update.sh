#!/usr/bin/env bash
# Actualización con respaldo previo y vuelta atrás automática.
#
#   sudo deploy/scripts/update.sh [DIR_FUENTE]
#
# 1. Backup consistente de la base (si falla, se aborta sin cambios).
# 2. Construye la release nueva con su virtualenv (la actual sigue funcionando).
# 3. Pausa el timer y espera a que termine un ciclo en curso.
# 4. Activa la release (enlace atómico), migra y reinicia el panel.
# 5. Healthcheck. Si falla: vuelve a la release anterior y, si el esquema cambió,
#    restaura el backup del paso 1. Luego reactiva el timer.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

SRC="${1:-$(cd "$HERE/../.." && pwd)}"
require_root
previous=$(current_release)
[[ -n "$previous" && -d "$previous" ]] || die "No hay instalación previa: usá install.sh."

log "Backup previo"
backup_out=$(radar_cli backup) || die "Falló el backup: no se actualiza."
echo "$backup_out"
backup_file=$(sed -n 's/^Backup verificado: \(.*\) (.*/\1/p' <<<"$backup_out")
[[ -f "$backup_file" ]] || die "No se encontró el archivo de backup."
schema_before=$(schema_version)

log "Construyendo release nueva desde $SRC"
release=$(build_release "$SRC")

log "Pausando la recolección"
systemctl stop radar-collect.timer
for _ in $(seq 120); do
    systemctl is-active --quiet radar-collect.service || break
    sleep 5
done
systemctl is-active --quiet radar-collect.service && die "El ciclo de recolección no terminó; reintentar luego (timer detenido: systemctl start radar-collect.timer)."

rollback() {
    warn "Fallo: volviendo a $previous"
    switch_release "$previous"
    schema_now=$(schema_version)
    if [[ "$schema_now" != "$schema_before" ]]; then
        if is_postgres; then
            # La restauración sobre PostgreSQL es destructiva y no se automatiza.
            warn "El esquema cambió ($schema_before → $schema_now). Las migraciones solo agregan, así que el"
            warn "código anterior suele funcionar. Si no, restaurá a mano: backup $backup_file o la"
            warn "restauración a un punto en el tiempo de Neon (ver VERCEL.md)."
        else
            warn "El esquema cambió ($schema_before → $schema_now): restaurando $backup_file"
            systemctl stop radar-web.service
            radar_cli restore "$backup_file" --yes
        fi
    fi
    systemctl restart radar-web.service
    systemctl start radar-collect.timer
    healthcheck 30 && log "Versión anterior restablecida." || warn "Revisar manualmente: journalctl --namespace=radar -u radar-web"
    exit 1
}

log "Activando $release"
switch_release "$release"
radar_cli init-db || rollback
systemctl restart radar-web.service
healthcheck 30 || rollback
systemctl start radar-collect.timer
prune_releases
log "Actualización completa: $(schema_version) (antes: $schema_before). Backup previo: $backup_file"
