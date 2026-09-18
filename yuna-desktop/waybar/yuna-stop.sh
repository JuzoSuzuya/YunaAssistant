#!/bin/bash
# Правый клик по кнопке Юны — мгновенно остановить речь/музыку.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [[ -f "$ROOT/../.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/../.env"
  set +a
fi

curl -sf --max-time 3 -X POST \
  "http://127.0.0.1:${YUNA_BRIDGE_PORT:-8765}/api/stop" \
  -H "Content-Type: application/json" -d '{}' >/dev/null 2>&1 || true

# запасной путь, если мост недоступен — глушим плееры напрямую
pkill -f "voice_out" 2>/dev/null || true
