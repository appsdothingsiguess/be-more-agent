#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

if [ -n "${BMO_VENV:-}" ]; then
    PY="$BMO_VENV/bin/python"
else
    PY="./venv/bin/python"
fi

exec "$PY" -m app "$@"
