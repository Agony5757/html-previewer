#!/usr/bin/env bash
# System install (root): nginx vhost + systemd services, content served by nginx.
# Idempotent; safe to re-run (preserves an existing API key and domain).
#
# Domain resolution order (so a re-run never clobbers a live config):
#   $PREVIEW_DOMAIN  >  existing /etc/preview/preview.env PREVIEW_PUBLIC_HOST  >  preview.example.com
#
# Usage:
#   sudo PREVIEW_DOMAIN=preview.example.com bash deploy/install-system.sh
#   sudo bash deploy/install-system.sh                # reuse domain from existing config
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "this installer must run as root (use sudo)"; exit 1; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(cd "$HERE/.." && pwd)"

APP=/opt/preview
ETC=/etc/preview
STATE=/var/lib/preview
WWW=/var/www/preview
LOG=/var/log/preview

echo "==> sanity checks"
command -v python3 >/dev/null || { echo "python3 required"; exit 1; }
command -v nginx   >/dev/null || { echo "nginx required";   exit 1; }
command -v git     >/dev/null || echo "WARN: git not found; git archiving will fail until installed"

# --- resolve the domain (never break a live config on re-run) ---
DOMAIN="${PREVIEW_DOMAIN:-}"
if [[ -z "$DOMAIN" && -f "$ETC/preview.env" ]]; then
  DOMAIN="$(sed -n 's/^PREVIEW_PUBLIC_HOST=//p' "$ETC/preview.env" | head -1)"
fi
DOMAIN="${DOMAIN:-preview.example.com}"
NGINX_AVAIL="/etc/nginx/sites-available/preview.${DOMAIN}.conf"
NGINX_ENABL="/etc/nginx/sites-enabled/preview.${DOMAIN}.conf"
echo "    domain = $DOMAIN"

echo "==> migrate away from legacy dedicated user (if present)"
if id -u preview >/dev/null 2>&1; then
  systemctl stop preview-api 2>/dev/null || true
  userdel preview 2>/dev/null || true
  echo "    removed legacy 'preview' user"
fi

echo "==> directories (root-owned; world-readable so nginx can serve)"
install -d -m 755 "$APP" "$ETC" "$STATE" "$STATE/.ssh" "$LOG"
install -d -m 755 "$WWW" "$WWW/live" "$WWW/static" "$WWW/acme"
chown -R root:root "$APP" "$ETC" "$STATE" "$LOG" "$WWW"

echo "==> application code -> $APP"
install -m 644 "$SRC/server/preview_common.py"  "$APP/preview_common.py"
install -m 644 "$SRC/server/preview_archive.py" "$APP/preview_archive.py"
install -m 644 "$SRC/server/preview_api.py"     "$APP/preview_api.py"
install -m 644 "$SRC/server/reaper.py"          "$APP/reaper.py"

echo "==> 404 page"
install -m 644 "$SRC/deploy/__notfound.html" "$WWW/static/__notfound.html"

echo "==> admin homepage (served at /)"
install -m 644 "$SRC/deploy/admin.html" "$WWW/live/index.html"

echo "==> config $ETC/preview.env"
if [[ -f "$ETC/preview.env" ]]; then
  echo "    keeping existing preview.env (API key preserved)"
else
  KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(40))')"
  sed -e "s|__REPLACE_WITH_RANDOM__|$KEY|" \
      -e "s|^PREVIEW_PUBLIC_HOST=.*|PREVIEW_PUBLIC_HOST=$DOMAIN|" \
      "$SRC/deploy/preview.env.example" > "$ETC/preview.env"
  chmod 600 "$ETC/preview.env"
  echo "    generated new API key"
fi

echo "==> systemd units"
install -m 644 "$SRC/deploy/preview-api.service"     /etc/systemd/system/preview-api.service
install -m 644 "$SRC/deploy/preview-reaper.service"  /etc/systemd/system/preview-reaper.service
install -m 644 "$SRC/deploy/preview-reaper.timer"    /etc/systemd/system/preview-reaper.timer
systemctl daemon-reload
systemctl enable --now preview-api.service
systemctl enable --now preview-reaper.timer
systemctl restart preview-api.service

echo "==> nginx vhost ($DOMAIN)"
sed "s|__SERVER_NAME__|$DOMAIN|g" "$SRC/deploy/preview.nginx.conf" > "$NGINX_AVAIL"
ln -sfn "$NGINX_AVAIL" "$NGINX_ENABL"
nginx -t
systemctl reload nginx

echo
echo "================ preview service installed (system) ================"
echo "API base (after DNS+cert):  https://$DOMAIN/_api/"
echo
echo "API KEY:"
grep '^PREVIEW_API_KEY=' "$ETC/preview.env" | cut -d= -f2-
echo
echo "Next steps:"
echo "  1) DNS A record:  $DOMAIN -> <YOUR_SERVER_IP>"
echo "  2) TLS cert:      sudo PREVIEW_DOMAIN=$DOMAIN bash $SRC/deploy/obtain-cert.sh"
echo "  3) (optional) git archiving: edit $ETC/preview.env (PREVIEW_GIT_*), then"
echo "     systemctl restart preview-api"
echo "==================================================================="
