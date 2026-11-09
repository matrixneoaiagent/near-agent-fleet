#!/usr/bin/env bash
# Foreground supervisor for systemd: app + quick tunnel, restart on either exit.
set -euo pipefail
cd "$(dirname "$0")"
source ./env.sh
mkdir -p logs state

CLOUDFLARED="${CLOUDFLARED_BIN:-$(command -v cloudflared || true)}"
CLOUDFLARED="${CLOUDFLARED:-./bin/cloudflared}"

cleanup() {
  for f in state/app.pid state/tunnel.pid; do
    if [ -f "$f" ]; then
      pid=$(cat "$f" 2>/dev/null || true)
      if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
      fi
    fi
  done
}
trap cleanup EXIT INT TERM

./.venv/bin/python app.py >> logs/app.out 2>&1 &
APP_PID=$!
echo "$APP_PID" > state/app.pid

"$CLOUDFLARED" tunnel --url http://127.0.0.1:8788 --no-autoupdate > logs/tunnel.log 2>&1 &
TUN_PID=$!
echo "$TUN_PID" > state/tunnel.pid

URL=""
for _ in $(seq 1 45); do
  sleep 2
  URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' logs/tunnel.log 2>/dev/null | tail -1 || true)
  if [ -n "$URL" ]; then break; fi
done
if [ -n "$URL" ]; then
  python3 - "$URL" <<'PY'
import json, sys
from datetime import datetime, timezone
url = sys.argv[1].rstrip('/')
with open('state/tunnel.json', 'w', encoding='utf-8') as fh:
    json.dump({'url': url, 'updated_at': datetime.now(timezone.utc).isoformat()}, fh, indent=1)
print('tunnel URL:', url)
PY
  ./.venv/bin/python provision.py --update-webhook "$URL" || echo "webhook update failed; retrying in the background"
else
  echo "no tunnel URL after 90s" >&2
fi

# Re-assert the webhook every 10 minutes in case the quick tunnel URL changed
# after an internal cloudflared reconnect.
(
  while true; do
    sleep 600
    CURRENT=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' logs/tunnel.log 2>/dev/null | tail -1 || true)
    if [ -n "$CURRENT" ]; then
      ./.venv/bin/python provision.py --update-webhook "$CURRENT" >> logs/webhook-update.log 2>&1 || true
    fi
  done
) &
WATCH_PID=$!

wait -n "$APP_PID" "$TUN_PID" || true
kill "$WATCH_PID" 2>/dev/null || true
echo "one of the supervised processes exited; shutting down the other"
