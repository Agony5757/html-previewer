#!/usr/bin/env bash
# User install (NO sudo): runs the fully self-contained service as a
# `systemd --user` unit. The Python process serves both the API and the
# published previews (PREVIEW_SERVE_STATIC=1) — no nginx, no root.
#
# Everything lives under your home dir (XDG):
#   code     ${XDG_DATA_HOME:-~/.local/share}/preview/app
#   content  ${XDG_DATA_HOME:-~/.local/share}/preview/live
#   state    ${XDG_DATA_HOME:-~/.local/share}/preview/state
#   config   ${XDG_CONFIG_HOME:-~/.config}/preview/preview.env
#   units    ${XDG_CONFIG_HOME:-~/.config}/systemd/user/
#
# Tunables (env): PREVIEW_PORT (8787), PREVIEW_BIND (127.0.0.1).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(cd "$HERE/.." && pwd)"

PY="$(command -v python3 || true)"
[[ -n "$PY" ]] || { echo "python3 required"; exit 1; }

DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
BASE="$DATA_HOME/preview"
APP="$BASE/app"
LIVE="$BASE/live"
STATE="$BASE/state"
STATIC="$BASE/static"
CFG_DIR="$CONFIG_HOME/preview"
CFG="$CFG_DIR/preview.env"
UNIT_DIR="$CONFIG_HOME/systemd/user"

PORT="${PREVIEW_PORT:-8787}"
BIND="${PREVIEW_BIND:-127.0.0.1}"

echo "==> directories under $BASE"
mkdir -p "$APP" "$LIVE" "$STATE" "$STATIC" "$CFG_DIR" "$UNIT_DIR"

echo "==> application code -> $APP"
install -m 644 "$SRC/server/preview_common.py"  "$APP/preview_common.py"
install -m 644 "$SRC/server/preview_archive.py" "$APP/preview_archive.py"
install -m 644 "$SRC/server/preview_api.py"     "$APP/preview_api.py"
install -m 644 "$SRC/server/reaper.py"          "$APP/reaper.py"
install -m 644 "$SRC/deploy/__notfound.html"    "$STATIC/__notfound.html"
install -m 644 "$SRC/deploy/admin.html"         "$LIVE/index.html"

echo "==> config $CFG"
if [[ -f "$CFG" ]]; then
  echo "    keeping existing preview.env (API key preserved)"
else
  KEY="$($PY -c 'import secrets; print(secrets.token_urlsafe(40))')"
  cat > "$CFG" <<EOF
# Self-contained user-service config for the preview hosting service.
PREVIEW_API_KEY=$KEY
PREVIEW_BIND=$BIND
PREVIEW_PORT=$PORT
PREVIEW_SERVE_STATIC=1
PREVIEW_PUBLIC_HOST=localhost:$PORT
PREVIEW_PUBLIC_SCHEME=http

PREVIEW_DEFAULT_DAYS=7
PREVIEW_MAX_DAYS=30

PREVIEW_LIVE_ROOT=$LIVE
PREVIEW_STATE_DIR=$STATE
PREVIEW_STATIC_DIR=$STATIC

# Git archiving is off by default for the user service.
PREVIEW_GIT_ENABLED=0
EOF
  chmod 600 "$CFG"
  echo "    generated new API key"
fi

echo "==> systemd --user units -> $UNIT_DIR"
cat > "$UNIT_DIR/preview-api.service" <<EOF
[Unit]
Description=Preview hosting API + static (self-contained, user service)
After=default.target

[Service]
Type=simple
EnvironmentFile=$CFG
WorkingDirectory=$APP
ExecStart=$PY $APP/preview_api.py
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
EOF

cat > "$UNIT_DIR/preview-reaper.service" <<EOF
[Unit]
Description=Preview expiry reaper (user service)

[Service]
Type=oneshot
EnvironmentFile=$CFG
WorkingDirectory=$APP
ExecStart=$PY $APP/reaper.py
EOF

cat > "$UNIT_DIR/preview-reaper.timer" <<EOF
[Unit]
Description=Run the preview expiry reaper hourly (user service)

[Timer]
OnBootSec=5min
OnCalendar=*-*-* *:07:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

API_KEY="$(sed -n 's/^PREVIEW_API_KEY=//p' "$CFG" | head -1)"

STARTED=0
if systemctl --user daemon-reload 2>/dev/null \
   && systemctl --user enable --now preview-api.service 2>/dev/null \
   && systemctl --user enable --now preview-reaper.timer 2>/dev/null; then
  systemctl --user restart preview-api.service 2>/dev/null || true
  STARTED=1
  # Keep the service alive after logout (best-effort; may require sudo).
  loginctl enable-linger "$USER" >/dev/null 2>&1 \
    && echo "    lingering enabled (survives logout)" \
    || echo "    NOTE: 'sudo loginctl enable-linger $USER' to survive logout"
else
  echo "    systemd --user not usable here; start it manually:"
  echo "      set -a; . $CFG; set +a; $PY $APP/preview_api.py"
fi

sleep 1
echo
echo "================ preview service installed (user) ================"
echo "Local base URL:  http://$BIND:$PORT"
echo "API KEY:         $API_KEY"
echo
echo "Publish from this machine (client reads ~/.config/preview/config):"
echo "  echo 'PREVIEW_API_BASE=http://localhost:$PORT' >  ~/.config/preview/config"
echo "  echo 'PREVIEW_API_KEY=$API_KEY' >> ~/.config/preview/config"
echo "  client/preview-publish.sh ./site/ my-site 7"
[[ "${STARTED:-0}" = "1" ]] && {
  echo
  echo "Manage:  systemctl --user status preview-api"
  echo "         journalctl --user -u preview-api -f"
}
echo
echo "Health:  curl -s http://$BIND:$PORT/_api/health"
echo "================================================================="
