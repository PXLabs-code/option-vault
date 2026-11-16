#!/bin/sh
set -eu
if [ "$#" -ne 2 ]; then
    printf 'Usage: sh scripts/backup.sh /absolute/config.local.json /backup/destination\n' >&2
    exit 2
fi
PROJECT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec "$PROJECT/.venv/bin/option-vault" --config "$1" --no-telegram backup --destination "$2"
