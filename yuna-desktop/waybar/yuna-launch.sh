#!/bin/bash
# Клик по кнопке Юны — нативное приложение
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CTL="$ROOT/yuna_ctl.sh"

if [[ -f "$ROOT/../.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/../.env"
  set +a
fi

if ! curl -sf --max-time 1 "http://127.0.0.1:${YUNA_BRIDGE_PORT:-8765}/health" >/dev/null 2>&1; then
  "$CTL" start
  sleep 2
fi
"$CTL" app
