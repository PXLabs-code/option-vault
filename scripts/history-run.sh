#!/bin/sh
set -eu
PROJECT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
mkdir -p "$PROJECT/data/logs"
DAY=$(date -u +%Y-%m-%d)
exec >> "$PROJECT/data/logs/history-launchd-$DAY.log" 2>&1
exec "$PROJECT/.venv/bin/option-vault-history" --config "$1"
