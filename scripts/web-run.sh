#!/bin/sh
set -eu
PROJECT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
mkdir -p "$PROJECT/data/logs"
DAY=$(date -u +%Y-%m-%d)
exec >> "$PROJECT/data/logs/web-launchd-$DAY.log" 2>&1
if [ "$#" -eq 2 ]; then
    exec "$PROJECT/.venv/bin/option-vault-web" --config "$1" --tailscale-ip "$2"
fi
exec "$PROJECT/.venv/bin/option-vault-web" --config "$1"
