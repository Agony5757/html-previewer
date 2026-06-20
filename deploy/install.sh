#!/usr/bin/env bash
# One entry point for all three deployment forms.
#
#   bash deploy/install.sh user      # systemd --user, no sudo, self-contained
#   bash deploy/install.sh system    # nginx + systemd system units (needs root)
#   bash deploy/install.sh docker    # docker compose up -d --build
#
# With no argument it prints this help.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mode="${1:-}"

usage() {
  sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

case "$mode" in
  user)
    exec bash "$HERE/install-user.sh"
    ;;
  system)
    if [[ $EUID -ne 0 ]]; then
      echo "==> system install needs root; re-running with sudo"
      exec sudo -E bash "$HERE/install-system.sh"
    fi
    exec bash "$HERE/install-system.sh"
    ;;
  docker)
    command -v docker >/dev/null || { echo "docker required"; exit 1; }
    if docker compose version >/dev/null 2>&1; then
      exec docker compose -f "$HERE/docker/docker-compose.yml" up -d --build
    elif command -v docker-compose >/dev/null 2>&1; then
      exec docker-compose -f "$HERE/docker/docker-compose.yml" up -d --build
    else
      echo "docker compose plugin not found"; exit 1
    fi
    ;;
  ""|-h|--help)
    usage 0
    ;;
  *)
    echo "unknown mode: $mode" >&2
    usage 1
    ;;
esac
