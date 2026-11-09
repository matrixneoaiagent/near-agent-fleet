#!/usr/bin/env bash
# Start the app + quick tunnel if not already running (idempotent).
set -euo pipefail
cd "$(dirname "$0")"
source ./env.sh
mkdir -p logs state work

CLOUDFLARED="${CLOUDFLARED_BIN:-$(command -v cloudflared || true)}"
CLOUDFLARED="${CLOUDFLARED:-./bin/cloudflared}"
if curl -sf http://127.0.0.1:8790/healthz >/dev/null 2>&1; then
  echo "app already responding on :8790"
elif systemctl --user is-active --quiet near-media-generator.service 2>/dev/null; then
  echo "app service is active but not healthy yet"
elif systemctl --user is-enabled --quiet near-media-generator.service 2>/dev/null; then
  systemctl --user start near-media-generator.service 2>/dev/null || true
  echo "started app service"
else
  nohup ./.venv/bin/python app.py >> logs/app.out 2>&1 &
  echo $! > state/app.pid
  echo "started app (pid $(cat state/app.pid))"
fi
if [ -f state/tunnel.pid ] && kill -0 "$(cat state/tunnel.pid)" 2>/dev/null; then
  echo "tunnel already running (pid $(cat state/tunnel.pid))"
else
  nohup "$CLOUDFLARED" tunnel --url http://127.0.0.1:8790 --no-autoupdate > logs/tunnel.log 2>&1 &
  echo $! > state/tunnel.pid
  echo "started tunnel (pid $(cat state/tunnel.pid))"
fi
URL=""
for _ in $(seq 1 40); do
  sleep 2
  URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' logs/tunnel.log 2>/dev/null | tail -1 || true)
  if [ -n "$URL" ]; then break; fi
done
if [ -z "$URL" ]; then echo "tunnel did not publish a URL" >&2; exit 1; fi
python3 - "$URL" <<'PY'
import json, sys
from datetime import datetime, timezone
url = sys.argv[1].rstrip('/')
with open('state/tunnel.json','w',encoding='utf-8') as fh:
    json.dump({'url': url, 'updated_at': datetime.now(timezone.utc).isoformat()}, fh, indent=1)
print('tunnel URL:', url)
PY
CURRENT=$(.venv/bin/python -c 'import json; print(json.load(open("config.json")).get("public_base_url",""))' 2>/dev/null || true)
if [ "$CURRENT" != "$URL" ]; then ./.venv/bin/python provision.py --update-webhook "$URL" || echo "warning: webhook update failed"; else echo "webhook already points at $URL"; fi
echo "health: $URL/healthz"
