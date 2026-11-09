#!/usr/bin/env bash
# Foreground app process for systemd.
set -euo pipefail
cd "$(dirname "$0")"
source ./env.sh
exec ./.venv/bin/python app.py
