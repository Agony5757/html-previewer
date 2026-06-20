#!/usr/bin/env bash
# Convenience client for publishing previews to the preview hosting service.
#
# Usage:
#   preview-publish.sh <path> [topic] [days]
#     <path>  : a folder (multi-file site), a .zip, or a single .html file
#     topic   : URL segment (default: derived from the file/folder name)
#     days    : expiry in days, 1..30 (default: 7)
#
#   preview-publish.sh --list            # list your live previews
#   preview-publish.sh --list-all        # include expired/deleted
#   preview-publish.sh --delete <hash>   # reclaim a preview now
#
# Config (env or ~/.config/preview/config):
#   PREVIEW_API_BASE   default https://preview.example.com
#   PREVIEW_API_KEY    required
set -euo pipefail

CFG="${HOME}/.config/preview/config"
[[ -f "$CFG" ]] && . "$CFG"
API_BASE="${PREVIEW_API_BASE:-https://preview.example.com}"
: "${PREVIEW_API_KEY:?set PREVIEW_API_KEY (env or $CFG)}"

auth=(-H "X-API-Key: ${PREVIEW_API_KEY}")

case "${1:-}" in
  --list)      curl -fsS "${auth[@]}" "${API_BASE}/_api/list"; echo; exit 0 ;;
  --list-all)  curl -fsS "${auth[@]}" "${API_BASE}/_api/list?all=1"; echo; exit 0 ;;
  --delete)    curl -fsS -X DELETE "${auth[@]}" "${API_BASE}/_api/preview/${2:?hash required}"; echo; exit 0 ;;
  ""|-h|--help)
     grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
esac

SRC="${1%/}"
[[ -e "$SRC" ]] || { echo "no such file or directory: $SRC" >&2; exit 1; }
base="$(basename "$SRC")"
DAYS="${3:-7}"

TMPZIP=""
cleanup() { [[ -n "$TMPZIP" ]] && rm -f "$TMPZIP"; }
trap cleanup EXIT

if [[ -d "$SRC" ]]; then
  TYPE=zip
  TOPIC="${2:-$base}"
  TMPZIP="$(mktemp --suffix=.zip)"
  # Zip the folder's *contents* so files sit at the archive root
  # (index.html lands directly under {topic}/).
  if command -v zip >/dev/null 2>&1; then
    ( cd "$SRC" && zip -qr - . ) > "$TMPZIP"
  else
    python3 - "$SRC" "$TMPZIP" <<'PY'
import os, sys, zipfile
src, out = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
    for root, _, files in os.walk(src):
        for name in files:
            full = os.path.join(root, name)
            zf.write(full, os.path.relpath(full, src))
PY
  fi
  FILE="$TMPZIP"
else
  FILE="$SRC"
  TOPIC="${2:-${base%.*}}"
  case "$SRC" in
    *.zip)  TYPE=zip  ;;
    *.htm|*.html) TYPE=html ;;
    *) echo "unsupported type (use a folder, .zip, or .html): $SRC" >&2; exit 1 ;;
  esac
fi

echo "publishing $SRC  topic=$TOPIC  days=$DAYS  type=$TYPE" >&2
curl -fsS "${auth[@]}" \
     -H "X-Topic: ${TOPIC}" -H "X-Days: ${DAYS}" -H "X-Type: ${TYPE}" \
     --data-binary "@${FILE}" \
     "${API_BASE}/_api/publish"
echo
