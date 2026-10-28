#!/usr/bin/env bash
# RestoPilot dev tunnel: starts the local server and a stable public HTTPS
# tunnel for the Meta WhatsApp webhook, then prints the callback URL to paste
# into Meta's webhook configuration.
#
# Usage (from the repo root):
#   bash scripts/dev-tunnel.sh [--port 8000] [--db data/restopilot.sqlite3]
#
# Set NGROK_STATIC_DOMAIN in .env (claim one for free at
# https://dashboard.ngrok.com/domains) to keep the same URL across restarts:
#   NGROK_STATIC_DOMAIN=your-subdomain.ngrok-free.dev
# Without it the tunnel URL changes on every run, so Meta must be updated
# each time. NGROK_AUTHTOKEN is read from ngrok's own config.
set -euo pipefail

cd "$(dirname "$0")/.."
PORT=8000
DB="data/restopilot.sqlite3"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --db) DB="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

NGROK_BIN="${NGROK_BIN:-ngrok}"
command -v "$NGROK_BIN" >/dev/null 2>&1 || { echo "ngrok not found; set NGROK_BIN=/path/to/ngrok" >&2; exit 1; }

# Read a key from .env without echoing values (static domain is not a secret,
# but the file also holds keys this script must never print).
env_value() {
  python - "$1" <<'PY'
import sys
from pathlib import Path
path = Path(".env")
print("" if not path.exists() else next((v.strip().strip("'\"") for line in path.read_text(encoding="utf-8").splitlines()
      if line.strip() and not line.startswith("#") and line.partition("=")[0].strip() == sys.argv[1]), ""))
PY
}

STATIC_DOMAIN="$(env_value NGROK_STATIC_DOMAIN)"
VENV_PY="${PYTHON:-}"
if [[ -z "$VENV_PY" ]]; then
  VENV_PY=".venv/Scripts/python.exe"
  [[ -x "$VENV_PY" ]] || VENV_PY=".venv/bin/python"
  [[ -x "$VENV_PY" ]] || VENV_PY="$(command -v python3 || command -v python)"
fi

echo "Starting ngrok tunnel on port $PORT..."
if [[ -n "$STATIC_DOMAIN" ]]; then
  "$NGROK_BIN" http "$PORT" --domain="$STATIC_DOMAIN" --log stdout --log-format logfmt > /tmp/restopilot-ngrok.log 2>&1 &
else
  "$NGROK_BIN" http "$PORT" --log stdout --log-format logfmt > /tmp/restopilot-ngrok.log 2>&1 &
fi
NGROK_PID=$!
trap 'kill "$NGROK_PID" 2>/dev/null || true' EXIT

PUBLIC_URL=""
for _ in $(seq 1 20); do
  PUBLIC_URL=$(curl -s http://127.0.0.1:4040/api/tunnels 2>/dev/null | "$VENV_PY" -c "import sys,json; print(json.load(sys.stdin)['tunnels'][0]['public_url'])" 2>/dev/null || true)
  [[ -n "$PUBLIC_URL" ]] && break
  sleep 0.5
done
[[ -n "$PUBLIC_URL" ]] || { echo "ngrok tunnel did not come up; see /tmp/restopilot-ngrok.log" >&2; exit 1; }

echo ""
echo "=============================================================="
echo " Public URL   : $PUBLIC_URL"
echo " Meta webhook : $PUBLIC_URL/webhooks/meta/whatsapp"
if [[ -z "$STATIC_DOMAIN" ]]; then
  echo " (ephemeral URL - claim a free static domain and set"
  echo "  NGROK_STATIC_DOMAIN in .env to keep it stable)"
fi
echo "=============================================================="
echo ""

# PUBLIC_BASE_URL as a process env var wins over the .env file (the launcher
# checks os.environ first), so no file is rewritten by this script.
PUBLIC_BASE_URL="$PUBLIC_URL" "$VENV_PY" -m service --port "$PORT" --db "$DB"
