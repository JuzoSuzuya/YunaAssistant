#!/bin/bash
# Yuna Desktop — supervisor (единая память + локальные демоны)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DESKTOP="$ROOT/yuna-desktop"
HOSHI="$ROOT/hoshi-core"
PY="${ROOT}/.venv/bin/python3"
LOG_DIR="$DESKTOP/logs"
mkdir -p "$LOG_DIR"

export PYTHONPATH="$HOSHI:$DESKTOP${PYTHONPATH:+:$PYTHONPATH}"
export PATH="$HOME/.local/bin:$PATH"
# Wayland + MPRIS для мыши/буфера/плеера
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

wait_stop() {
  local pattern="$1"
  local i=0
  while pgrep -f "$pattern" >/dev/null 2>&1 && [[ $i -lt 40 ]]; do
    sleep 0.25
    i=$((i + 1))
  done
}

start_ollama() {
  if curl -sf "http://127.0.0.1:11435/api/tags" >/dev/null 2>&1; then
    echo "ollama: already on 11435"
    return 0
  fi
  local script="$DESKTOP/scripts/start_ollama.sh"
  if [[ ! -x "$script" ]]; then
    echo "ollama: missing $script"
    return 1
  fi
  nohup bash "$script" >>"$LOG_DIR/ollama.log" 2>&1 &
  echo "ollama: started pid $! (11435)"
  local i=0
  while [[ $i -lt 40 ]]; do
    if curl -sf "http://127.0.0.1:11435/api/tags" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.25
    i=$((i + 1))
  done
  echo "ollama: ещё стартует — смотри $LOG_DIR/ollama.log"
}

start_memory() {
  if pgrep -f "yuna-desktop/memory_hub.py" >/dev/null 2>&1; then
    echo "memory_hub: already running"
    return 0
  fi
  nohup "$PY" "$DESKTOP/memory_hub.py" >>"$LOG_DIR/memory_hub.log" 2>&1 &
  echo "memory_hub: started pid $!"
}

stop_memory() {
  pkill -f "yuna-desktop/memory_hub.py" 2>/dev/null || true
  wait_stop "yuna-desktop/memory_hub.py"
  echo "memory_hub: stopped"
}

start_rvc() {
  if pgrep -f "rvc/rvc_worker.py" >/dev/null 2>&1; then
    echo "rvc: already running"
    return 0
  fi
  local rvc_py="$DESKTOP/rvc/.venv/bin/python"
  if [[ ! -x "$rvc_py" ]]; then
    echo "rvc: venv missing — run $DESKTOP/rvc/setup.sh"
    return 1
  fi
  nohup "$rvc_py" "$DESKTOP/rvc/rvc_worker.py" >>"$LOG_DIR/rvc.log" 2>&1 &
  echo "rvc: started pid $!"
}

stop_rvc() {
  pkill -f "rvc/rvc_worker.py" 2>/dev/null || true
  wait_stop "rvc/rvc_worker.py"
  rm -f /tmp/yuna-rvc.sock 2>/dev/null || true
  echo "rvc: stopped"
}

start_voice() {
  start_rvc || true
  pkill -9 -f "pw-record.*voice_in/in_" 2>/dev/null || true
  pkill -9 -f "yuna-desktop/voice_daemon.py" 2>/dev/null || true
  wait_stop "yuna-desktop/voice_daemon.py"
  rm -f /tmp/yuna-voice.sock 2>/dev/null || true
  nohup "$PY" "$DESKTOP/voice_daemon.py" >>"$LOG_DIR/voice.log" 2>&1 &
  echo "voice: started pid $!"
}

stop_voice() {
  pkill -9 -f "yuna-desktop/voice_daemon.py" 2>/dev/null || true
  wait_stop "yuna-desktop/voice_daemon.py"
  rm -f /tmp/yuna-voice.sock 2>/dev/null || true
  stop_rvc
  echo "voice: stopped"
}

start_bridge() {
  if pgrep -f "yuna-desktop/local_bridge.py" >/dev/null 2>&1; then
    echo "bridge: already running"
    return 0
  fi
  nohup "$PY" "$DESKTOP/local_bridge.py" >>"$LOG_DIR/bridge.log" 2>&1 &
  echo "bridge: started pid $!"
}

stop_bridge() {
  pkill -9 -f "yuna-desktop/local_bridge.py" 2>/dev/null || true
  wait_stop "yuna-desktop/local_bridge.py"
  echo "bridge: stopped"
}

start_daemon() {
  if pgrep -f "yuna-desktop/local_daemon.py" >/dev/null 2>&1; then
    echo "daemon: already running"
    return 0
  fi
  nohup "$PY" "$DESKTOP/local_daemon.py" >>"$LOG_DIR/daemon.log" 2>&1 &
  echo "daemon: started pid $!"
}

stop_daemon() {
  pkill -f "yuna-desktop/local_daemon.py" 2>/dev/null || true
  wait_stop "yuna-desktop/local_daemon.py"
  echo "daemon: stopped"
}

start_screen() {
  if pgrep -f "yuna-desktop/screen_watcher.py" >/dev/null 2>&1; then
    echo "screen: already running"
    return 0
  fi
  nohup "$PY" "$DESKTOP/screen_watcher.py" >>"$LOG_DIR/screen.log" 2>&1 &
  echo "screen: started pid $!"
}

stop_screen() {
  pkill -f "yuna-desktop/screen_watcher.py" 2>/dev/null || true
  wait_stop "yuna-desktop/screen_watcher.py"
  echo "screen: stopped"
}

open_app() {
  local app_py="$DESKTOP/app/yuna_app.py"
  if [[ ! -f "$app_py" ]]; then
    echo "app missing: $app_py"
    return 1
  fi
  if pgrep -f "yuna_app.py" >/dev/null 2>&1; then
    return 0
  fi
  nohup env PYTHONPATH="$HOSHI:$DESKTOP" python3 "$app_py" >>"$LOG_DIR/app.log" 2>&1 &
  echo "app: started pid $!"
}

open_panel() {
  open_app
}

sync_now() {
  "$PY" "$DESKTOP/memory_sync.py"
}

sync_server() {
  "$PY" -c "
from server_task import _sync_server_files
_sync_server_files()
print('server: code synced')
"
  local ssh_cmd=(ssh -F /dev/null -o StrictHostKeyChecking=no)
  if [[ -n "${YUNA_SERVER_SSH_PASS:-}" ]]; then
    ssh_cmd=(sshpass -p "$YUNA_SERVER_SSH_PASS" "${ssh_cmd[@]}")
  fi
  "${ssh_cmd[@]}" "${YUNA_SERVER_USER:-root}@${YUNA_SERVER_HOST}" \
    "cd ${YUNA_SERVER_HOSHI_PATH:-/root/projects/Hoshi} && ./hoshi_ctl.sh stop daemon bridge 2>/dev/null; sleep 1; ./hoshi_ctl.sh start daemon bridge" \
    2>/dev/null && echo "server: daemon+bridge restarted" || echo "server: restart failed (ssh)"
}

status() {
  for name in memory_hub rvc voice bridge daemon screen pilot; do
    case $name in
      memory_hub) pat="yuna-desktop/memory_hub.py" ;;
      rvc) pat="rvc/rvc_worker.py" ;;
      voice) pat="yuna-desktop/voice_daemon.py" ;;
      bridge) pat="yuna-desktop/local_bridge.py" ;;
      daemon) pat="yuna-desktop/local_daemon.py" ;;
      screen) pat="yuna-desktop/screen_watcher.py" ;;
      pilot)
        if [[ -f "$HOSHI/data/neuro_pilot.json" ]] && grep -q '"running": true' "$HOSHI/data/neuro_pilot.json" 2>/dev/null; then
          echo "pilot: running"
        else
          echo "pilot: stopped"
        fi
        continue
        ;;
    esac
    if pgrep -f "$pat" >/dev/null 2>&1; then
      echo "$name: running"
    else
      echo "$name: stopped"
    fi
  done
  if [[ -f "$DESKTOP/memory_sync_state.json" ]]; then
    echo "last sync: $(grep last_sync "$DESKTOP/memory_sync_state.json" | head -1)"
  fi
}

case "${1:-}" in
  start)
    start_ollama || true
    start_memory
    start_voice
    start_bridge
    start_daemon
    start_screen
    ;;
  stop)
    stop_screen
    stop_daemon
    stop_bridge
    stop_voice
    stop_memory
    ;;
  restart)
    "$0" stop
    sleep 1
    "$0" start
    ;;
  status) status ;;
  sync) sync_now ;;
  sync-server) sync_server ;;
  panel|open|app) open_app ;;
  memory) stop_memory; start_memory ;;
  voice) start_voice ;;
  rvc) stop_rvc; start_rvc ;;
  bridge) stop_bridge; start_bridge ;;
  daemon) stop_daemon; start_daemon ;;
  screen) stop_screen; start_screen ;;
  ollama) start_ollama ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|sync|sync-server|panel|memory|voice|bridge|daemon|screen|ollama}"
    exit 1
    ;;
esac
