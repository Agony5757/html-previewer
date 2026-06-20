#!/bin/sh
# Container entrypoint: ensure a stable API key (persisted in the data volume so
# it survives restarts), run the expiry reaper once, then start the service.
set -e

: "${PREVIEW_STATE_DIR:=/data/state}"
mkdir -p "$PREVIEW_STATE_DIR" "${PREVIEW_LIVE_ROOT:=/data/live}"

# Install/refresh the admin homepage served at / (LIVE_ROOT/index.html).
cp /app/admin.html "$PREVIEW_LIVE_ROOT/index.html" 2>/dev/null || true

KEY_FILE="$PREVIEW_STATE_DIR/api_key"
if [ -z "${PREVIEW_API_KEY:-}" ]; then
  if [ -f "$KEY_FILE" ]; then
    PREVIEW_API_KEY="$(cat "$KEY_FILE")"
  else
    PREVIEW_API_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(40))')"
    printf '%s' "$PREVIEW_API_KEY" > "$KEY_FILE"
    chmod 600 "$KEY_FILE" 2>/dev/null || true
    echo "[entrypoint] generated a new API key (stored in $KEY_FILE)"
  fi
  export PREVIEW_API_KEY
fi

echo "[entrypoint] API key: $PREVIEW_API_KEY"
echo "[entrypoint] serving on ${PREVIEW_BIND:-0.0.0.0}:${PREVIEW_PORT:-8787} (static=${PREVIEW_SERVE_STATIC:-1})"

# Reclaim any already-expired previews on boot (best-effort).
python3 /app/reaper.py || true

exec python3 /app/preview_api.py
