#!/usr/bin/env bash
# Start the service and a Cloudflare quick tunnel if they are not already up.
set -euo pipefail
cd "$(dirname "$0")"
source ./env.sh
mkdir -p logs state

CLOUDFLARED="${CLOUDFLARED_BIN:-$(command -v cloudflared || true)}"
CLOUDFLARED="${CLOUDFLARED:-./bin/cloudflared}"

start_app() {
  if curl -sf http://127.0.0.1:8788/healthz >/dev/null 2>&1; then
    echo "app already responding on :8788"
    return 0
  fi
  if systemctl --user is-active --quiet near-video-clipper-app.service 2>/dev/null; then
    echo "app service is active"
    return 0
  fi
  if systemctl --user is-enabled --quiet near-video-clipper-app.service 2>/dev/null; then
    systemctl --user start near-video-clipper-app.service 2>/dev/null || true
    echo "started app service"
    return 0
  fi
  if [ -f state/app.pid ] && kill -0 "$(cat state/app.pid)" 2>/dev/null; then
    echo "app already running (pid $(cat state/app.pid))"
    return 0
  fi
  nohup ./.venv/bin/python app.py >> logs/app.out 2>&1 &
  echo $! > state/app.pid
  echo "started app (pid $(cat state/app.pid))"
}

start_tunnel() {
  if [ -f state/tunnel.pid ] && kill -0 "$(cat state/tunnel.pid)" 2>/dev/null; then
    echo "tunnel already running (pid $(cat state/tunnel.pid))"
    return 0
  fi
  nohup "$CLOUDFLARED" tunnel --url http://127.0.0.1:8788 --no-autoupdate > logs/tunnel.log 2>&1 &
  echo $! > state/tunnel.pid
  echo "started tunnel (pid $(cat state/tunnel.pid))"
}

start_app
start_tunnel

URL=""
for _ in $(seq 1 40); do
  sleep 2
  URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' logs/tunnel.log 2>/dev/null | tail -1 || true)
  if [ -n "$URL" ]; then break; fi
done
if [ -z "$URL" ]; then
  echo "tunnel did not publish a URL; see logs/tunnel.log" >&2
  exit 1
fi
python3 - "$URL" <<'PY'
import json, os, sys
url = sys.argv[1].rstrip('/')
with open('state/tunnel.json', 'w', encoding='utf-8') as fh:
    json.dump({'url': url, 'updated_at': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()}, fh, indent=1)
print('tunnel URL:', url)
PY
CURRENT=$(.venv/bin/python -c 'import json; print(json.load(open("config.json")).get("public_base_url", ""))' 2>/dev/null || true)
if [ "$CURRENT" != "$URL" ]; then
  .venv/bin/python provision.py --update-webhook "$URL" || echo "warning: webhook update failed; will retry from the watchdog"
else
  echo "webhook already points at $URL"
fi
echo "health: $URL/healthz"
