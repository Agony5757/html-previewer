#!/usr/bin/env bash
# Convenience client for publishing previews to the preview hosting service.
#
# Usage:
#   preview-publish.sh <path> [topic] [expiry]
#     <path>   : a folder (multi-file site), a .zip, or a single .html file
#     topic    : URL segment / name (default: derived from the file/folder name)
#     expiry   : N        -> N days (1..30, default 7)
#                N{s,m,h,d} -> seconds / minutes / hours / days (e.g. 2m, 36h)
#
#   Same-name update (default): re-publishing under a name (topic) that is still
#   live AND not past its expiry overwrites it in place, keeping the SAME URL. A
#   name whose previous preview has expired gets a brand-new URL. Set PREVIEW_NEW=1
#   to always force a brand-new capability URL.
#
#   preview-publish.sh --dry-run <path> [topic]   # predict update vs new (no upload)
#   preview-publish.sh --list                     # list your live previews
#   preview-publish.sh --list-all                 # include expired/deleted
#   preview-publish.sh --delete <hash>            # reclaim a preview now
#
# Config (env or ~/.config/preview/config):
#   PREVIEW_API_BASE   default https://preview.example.com
#   PREVIEW_API_KEY    required
#   PREVIEW_NEW=1      force a new URL instead of same-name update (optional)
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

DRY=0
if [[ "${1:-}" == "--dry-run" || "${1:-}" == "--check" ]]; then
  DRY=1
  shift
fi

SRC="${1%/}"
[[ -e "$SRC" ]] || { echo "no such file or directory: $SRC" >&2; exit 1; }
base="$(basename "$SRC")"

# Derive topic (the "name") + type the same way for dry-run and publish.
if [[ -d "$SRC" ]]; then
  TYPE=zip
  TOPIC="${2:-$base}"
else
  case "$SRC" in
    *.zip)        TYPE=zip  ;;
    *.htm|*.html) TYPE=html ;;
    *) echo "unsupported type (use a folder, .zip, or .html): $SRC" >&2; exit 1 ;;
  esac
  TOPIC="${2:-${base%.*}}"
fi

# --- dry-run: ask the server what a real publish WOULD do (no upload) ---
if [[ "$DRY" == "1" ]]; then
  check_args=(-G "${auth[@]}" --data-urlencode "topic=${TOPIC}")
  [[ "${PREVIEW_NEW:-}" == "1" ]] && check_args+=(--data-urlencode "new=1")
  echo "dry-run: topic=$TOPIC" >&2
  curl -fsS "${check_args[@]}" "${API_BASE}/_api/check"
  echo
  exit 0
fi

# --- expiry: N days, or N{s,m,h,d} -> X-TTL seconds ---
EXPIRY="${3:-}"
expiry_hdr=()
if [[ -z "$EXPIRY" ]]; then
  expiry_hdr=(-H "X-Days: 7")
elif [[ "$EXPIRY" =~ ^[0-9]+$ ]]; then
  expiry_hdr=(-H "X-Days: $EXPIRY")
elif [[ "$EXPIRY" =~ ^([0-9]+)([smhd])$ ]]; then
  n="${BASH_REMATCH[1]}"; u="${BASH_REMATCH[2]}"
  case "$u" in
    s) ttl="$n" ;;
    m) ttl=$(( n * 60 )) ;;
    h) ttl=$(( n * 3600 )) ;;
    d) ttl=$(( n * 86400 )) ;;
  esac
  expiry_hdr=(-H "X-TTL: $ttl")
else
  echo "bad expiry '$EXPIRY' (use N for days, or N{s,m,h,d}, e.g. 2m)" >&2; exit 1
fi

TMPZIP=""
cleanup() { [[ -n "$TMPZIP" ]] && rm -f "$TMPZIP"; }
trap cleanup EXIT

if [[ -d "$SRC" ]]; then
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
fi

echo "publishing $SRC  topic=$TOPIC  expiry=${EXPIRY:-7(days)}  type=$TYPE" >&2
extra=()
[[ "${PREVIEW_NEW:-}" == "1" ]] && extra+=(-H "X-New: 1")
curl -fsS "${auth[@]}" \
     -H "X-Topic: ${TOPIC}" "${expiry_hdr[@]}" -H "X-Type: ${TYPE}" \
     "${extra[@]}" \
     --data-binary "@${FILE}" \
     "${API_BASE}/_api/publish"
echo
