#!/usr/bin/env bash
# Obtain/renew the Let's Encrypt certificate for the preview vhost.
# Run as root AFTER the DNS A record (<domain> -> <YOUR_SERVER_IP>) has
# propagated. certbot --nginx rewrites the vhost to add 443 + redirect.
#
# Domain/email come from env (falling back to the installed config):
#   sudo PREVIEW_DOMAIN=preview.example.com PREVIEW_CERT_EMAIL=admin@example.com \
#        bash deploy/obtain-cert.sh
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }

DOMAIN="${PREVIEW_DOMAIN:-}"
if [[ -z "$DOMAIN" && -f /etc/preview/preview.env ]]; then
  DOMAIN="$(sed -n 's/^PREVIEW_PUBLIC_HOST=//p' /etc/preview/preview.env | head -1)"
fi
[[ -n "$DOMAIN" ]] || { echo "set PREVIEW_DOMAIN (no domain found in config)"; exit 1; }
EMAIL="${PREVIEW_CERT_EMAIL:-admin@$DOMAIN}"

echo "==> checking DNS for $DOMAIN"
if ! getent hosts "$DOMAIN" >/dev/null; then
  echo "ERROR: $DOMAIN does not resolve yet. Add the A record first." >&2
  exit 1
fi

certbot --nginx -d "$DOMAIN" --redirect --non-interactive --agree-tos \
  --keep-until-expiring --expand -m "$EMAIL" || {
    echo "certbot failed" >&2; exit 1; }

nginx -t && systemctl reload nginx
echo "==> done. https://$DOMAIN/ is live."
