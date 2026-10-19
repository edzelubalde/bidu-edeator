#!/usr/bin/env sh
# Build the pages (first run only) and start the editor at http://localhost:8000.
set -e
cd "$(dirname "$0")/.."
if [ ! -d apps/web/out ]; then
  (cd apps/web && npm ci && npm run build)
fi
uv sync
exec uv run bidu-web "$@"
