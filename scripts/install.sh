#!/bin/sh
set -eu

if [ ! -f pyproject.toml ] || [ ! -d web ]; then
    printf '%s\n' 'Error: run scripts/install.sh from the harbor-ledger-memory repository root.' >&2
    exit 1
fi

(cd web && npm ci && npm run build)
uv tool install .
hlm setup
