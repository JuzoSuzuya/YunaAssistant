#!/usr/bin/env bash
# Yuna Desktop — idempotent stack launcher (ollama → bridge → voice daemon).
# Safe to run when everything is already up: detects running processes and
# never double-starts. Mirrors yuna_ctl.sh start logic.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DESKTOP="$ROOT/yuna-desktop"
HOSHI="$ROOT/hoshi-core"
PY="${ROOT}/.venv/bin/python3"
LOG_DIR="$DESKTOP/logs"
mkdir -p "$LOG_DIR"

export PYTHONPATH="$HOSHI:$DESKTOP${PYTHONPATH:+:$PYTHONPATH}"
export PATH="$HOME/.local/bin:$PATH"
# Wayland + DBus for voice/input daemons
if [[ -z "${XDG_RUNTIME_DIR:-}" && -d "/run/user/$(id -u)" ]]; then
  export XDG_RUNTIME_DIR="/run/user/$(id -u)"
fi
if [[ -z "${WAYLAND_DISPLAY:-}" && -n "${XDG_RUNTIME_DIR:-}" ]]; then
  for _w in wayland-1 wayland-0; do
    if [[ -S "${XDG_RUNTIME_DIR}/${_w}" ]]; then
      export WAYLAND_DISPLAY="$_w"
      break
    fi
  done
fi
if [[ -z "${DBUS_SESSION_BUS_ADDRESS:-}" && -S "/run/user/$(id -u)/bus" ]]; then
  export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"
fi

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

OLLAMA_URL="http://127.0.0.1:11435"
BRIDGE_URL="http://127.0.0.1:8765"

# --- ollama (port 11435) -----------------------------------------------------
if curl -sf "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
  echo "start_stack: ollama already on 11435"
else
  local_script="$DESKTOP/scripts/start_ollama.sh"
  if [[ -x "$local_script" ]]; then
    echo "start_stack: starting ollama (11435)"
    nohup bash "$local_script" >>"$LOG_DIR/ollama.log" 2>&1 &
  else
    echo "start_stack: starting ollama serve (11435)"
    nohup env OLLAMA_HOST=127.0.0.1:11435 \
      OLLAMA_MODELS=/mnt/disk03/SteamLibrary/YunaAI_ollama/models \
      /usr/local/bin/ollama serve >>"$LOG_DIR/ollama.log" 2>&1 &
  fi
  for _i in $(seq 1 40); do
    if curl -sf "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
      echo "start_stack: ollama up"
      break
    fi
    sleep 0.25
  done
fi

# --- bridge (http://127.0.0.1:8765) ------------------------------------------
if pgrep -f "yuna-desktop/local_bridge.py" >/dev/null 2>&1; then
  echo "start_stack: bridge already running"
else
  echo "start_stack: starting bridge"
  nohup "$PY" "$DESKTOP/local_bridge.py" >>"$LOG_DIR/bridge.log" 2>&1 &
fi

# Wait for bridge health (max 60s)
for _i in $(seq 1 240); do
  if curl -sf "$BRIDGE_URL/health" >/dev/null 2>&1; then
    echo "start_stack: bridge healthy on 8765"
    break
  fi
  if [[ $_i -eq 240 ]]; then
    echo "start_stack: WARNING bridge not healthy after 60s — see $LOG_DIR/bridge.log"
  fi
  sleep 0.25
done

# --- voice daemon -------------------------------------------------------------
if pgrep -f "yuna-desktop/voice_daemon.py" >/dev/null 2>&1; then
  echo "start_stack: voice daemon already running"
else
  echo "start_stack: starting voice daemon"
  nohup "$PY" "$DESKTOP/voice_daemon.py" >>"$LOG_DIR/voice.log" 2>&1 &
fi

echo "start_stack: done"