#!/usr/bin/env bash
# Configura el sitio de Nginx para un subdominio, sin tocar nginx.conf ni otros sitios.
#
#   sudo deploy/scripts/nginx-site.sh radar.midominio.com correo@midominio.com
#
# Requisitos previos (no los hace este script): Nginx y certbot instalados, y el registro
# DNS del subdominio apuntando al VPS, con los puertos 80/443 abiertos.
#
# Pasos: instala la config HTTP mínima → nginx -t → reload → emite el certificado con
# certbot --webroot → instala la config HTTPS → nginx -t → reload. Si nginx -t falla, no
# recarga y restaura la config anterior.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"
require_root
[[ $# -eq 2 ]] || die "Uso: nginx-site.sh SUBDOMINIO EMAIL"
DOMAIN="$1"; EMAIL="$2"
[[ "$DOMAIN" =~ ^[a-z0-9.-]+\.[a-z]{2,}$ ]] || die "Subdominio inválido: $DOMAIN"
command -v nginx >/dev/null || die "Nginx no está instalado."
command -v certbot >/dev/null || die "certbot no está instalado (apt install certbot)."
PORT=$(env_value RADAR_PORT 8000)

if [[ -d /etc/nginx/sites-available ]]; then
    AVAIL=/etc/nginx/sites-available/radar.conf; LINK=/etc/nginx/sites-enabled/radar.conf
else
    AVAIL=/etc/nginx/conf.d/radar.conf; LINK=""
fi
grep -rl "server_name[^;]*\b$DOMAIN\b" /etc/nginx 2>/dev/null | grep -v "$AVAIL" \
    && die "Otro sitio ya usa $DOMAIN: no se modifica nada."

apply_conf() {  # apply_conf ARCHIVO_PLANTILLA
    local backup=""
    if [[ -f "$AVAIL" ]]; then backup="$AVAIL.bak-$(date +%Y%m%d%H%M%S)"; cp -p "$AVAIL" "$backup"; fi
    sed -e "s/radar\.example\.com/$DOMAIN/g" -e "s/127\.0\.0\.1:8000/127.0.0.1:$PORT/g" "$1" > "$AVAIL"
    [[ -n "$LINK" ]] && ln -sfn "$AVAIL" "$LINK"
    if nginx -t; then
        systemctl reload nginx
    else
        warn "nginx -t falló: se restaura la configuración anterior y no se recarga."
        if [[ -n "$backup" ]]; then cp -p "$backup" "$AVAIL"; else rm -f "$AVAIL"; [[ -n "$LINK" ]] && rm -f "$LINK"; fi
        die "Configuración inválida."
    fi
}

install -d -m 0755 /var/www/certbot
if [[ ! -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]]; then
    log "Paso 1: sitio HTTP para el desafío de Let's Encrypt"
    apply_conf "$HERE/../nginx/radar-http-bootstrap.conf"
    log "Paso 2: emisión del certificado"
    certbot certonly --webroot -w /var/www/certbot -d "$DOMAIN" \
        --email "$EMAIL" --agree-tos --no-eff-email \
        --deploy-hook "nginx -t && systemctl reload nginx"
fi
log "Paso 3: sitio HTTPS con proxy a 127.0.0.1:$PORT"
apply_conf "$HERE/../nginx/radar.conf"
log "Listo: https://$DOMAIN  (renovación: certbot renew --dry-run)"
