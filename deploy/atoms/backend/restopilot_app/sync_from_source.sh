#!/usr/bin/env bash
# Prepare the self-contained service directory required by Atoms Lambda packaging.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="${HERE}/../../../../service"
# Only this generated directory is replaced. The repository service/ is authoritative.
rm -rf "${HERE}/service"
cp -a "$SOURCE" "${HERE}/service"
find "${HERE}/service" -type d -name __pycache__ -prune -exec rm -rf {} +
diff -rq "$SOURCE" "${HERE}/service" --exclude=__pycache__
