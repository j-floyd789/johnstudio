#!/usr/bin/env bash
# Start the JohnStudio FastAPI backend bound to localhost.
set -euo pipefail
exec johnstudio server --host "${HOST:-127.0.0.1}" --port "${PORT:-8765}" "$@"
