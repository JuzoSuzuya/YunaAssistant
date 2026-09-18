#!/bin/bash
set -euo pipefail
ROOT="/root/projects/Hoshi"
cd "$ROOT"
PY="$ROOT/.venv/bin/python3"
BRIDGE="$ROOT/tg_bridge.py"
DAEMON="$ROOT/hoshi_daemon.py"
HEAVY="$ROOT/hoshi_heavy_worker.py"
HEAVY_COUNT="${HOSHI_HEAVY_WORKERS:-2}"
IRIS="$ROOT/iris_daemon.py"

wait_for_stop() {
  local pattern="$1"
  local i=0
  while pgrep -f "$pattern" >/dev/null 2>&1 && [ "$i" -lt 40 ]; do
    sleep 0.25
    i=$((i + 1))
  done
  if pgrep -f "$pattern" >/dev/null 2>&1; then
    pkill -9 -f "$pattern" 2>/dev/null || true
    sleep 0.5
  fi
}

start_bridge() {
  if pgrep -f "$PY $BRIDGE" >/dev/null 2>&1; then
    echo "Bridge already running"
    pgrep -af "$PY $BRIDGE"
    return 0
  fi
  nohup "$PY" "$BRIDGE" >> "$ROOT/bridge.log" 2>&1 &
  disown 2>/dev/null || true
  sleep 0.5
  echo "Bridge started PID $(pgrep -f "$PY $BRIDGE" | head -1)"
}

stop_bridge() {
  pkill -f "$PY $BRIDGE" 2>/dev/null || true
  rm -f "$ROOT/.bridge.lock"
  echo "Bridge stopped"
}

start_daemon() {
  if pgrep -f "$PY $DAEMON" >/dev/null 2>&1; then
    echo "Daemon already running"
    pgrep -af "$PY $DAEMON"
    return 0
  fi
  nohup "$PY" "$DAEMON" >> "$ROOT/daemon.log" 2>&1 &
  disown 2>/dev/null || true
  sleep 0.5
  echo "Daemon started PID $(pgrep -f "$PY $DAEMON" | head -1)"
}

stop_daemon() {
  pkill -f "$PY $DAEMON" 2>/dev/null || true
  rm -f "$ROOT/.daemon.lock"
  "$PY" -c "from hoshi_daemon import cleanup_cursor_agents; cleanup_cursor_agents(reason='ctl stop')" 2>/dev/null || true
  echo "Daemon stopped"
}

start_heavy_worker() {
  local wid="${1:-1}"
  if pgrep -f "$PY $HEAVY $wid" >/dev/null 2>&1; then
    echo "Heavy worker $wid already running"
    pgrep -af "$PY $HEAVY $wid"
    return 0
  fi
  nohup "$PY" "$HEAVY" "$wid" >> "$ROOT/daemon.log" 2>&1 &
  disown 2>/dev/null || true
  sleep 0.3
  echo "Heavy worker $wid started PID $(pgrep -f "$PY $HEAVY $wid" | head -1)"
}

start_heavy() {
  local i=1
  while [ "$i" -le "$HEAVY_COUNT" ]; do
    start_heavy_worker "$i"
    i=$((i + 1))
  done
}

stop_heavy() {
  pkill -f "$PY $HEAVY" 2>/dev/null || true
  rm -f "$ROOT"/.heavy_worker.*.lock "$ROOT/.heavy_worker.lock"
  "$PY" -c "from hoshi_daemon import cleanup_cursor_agents; cleanup_cursor_agents(reason='heavy ctl stop', owner='heavy')" 2>/dev/null || true
  echo "Heavy worker(s) stopped"
}

start_iris() {
  if pgrep -f "$PY $IRIS" >/dev/null 2>&1; then
    echo "Iris daemon already running"
    pgrep -af "$PY $IRIS"
    return 0
  fi
  nohup "$PY" "$IRIS" >> "$ROOT/daemon.log" 2>&1 &
  disown 2>/dev/null || true
  sleep 0.5
  echo "Iris daemon started PID $(pgrep -f "$PY $IRIS" | head -1)"
}

stop_iris() {
  pkill -f "$PY $IRIS" 2>/dev/null || true
  rm -f "$ROOT/.iris_daemon.lock"
  echo "Iris daemon stopped"
}

restart_bridge() {
  stop_bridge
  wait_for_stop "$PY $BRIDGE"
  start_bridge
}

restart_daemon() {
  stop_daemon
  wait_for_stop "$PY $DAEMON"
  start_daemon
}

# Сначала bridge (остаётся живым), потом daemon — без двойного убийства bridge
safe_restart() {
  restart_bridge
  restart_daemon
  stop_heavy
  wait_for_stop "$PY $HEAVY"
  start_heavy
  stop_iris
  wait_for_stop "$PY $IRIS"
  start_iris
}

status_all() {
  echo "=== Bridge ==="
  pgrep -af "$PY $BRIDGE" || echo "Not running"
  echo
  echo "=== Daemon (light) ==="
  pgrep -af "$PY $DAEMON" || echo "Not running"
  echo
  echo "=== Heavy workers ($HEAVY_COUNT) ==="
  pgrep -af "$PY $HEAVY" || echo "Not running"
  echo
  echo "=== Iris daemon ==="
  pgrep -af "$PY $IRIS" || echo "Not running"
  echo
  if [[ -f "$ROOT/daemon_status.json" ]]; then
    cat "$ROOT/daemon_status.json"
  else
    echo "No daemon_status.json"
  fi
  echo
  if [[ -f "$ROOT/heavy_worker_status.json" ]]; then
    cat "$ROOT/heavy_worker_status.json"
  else
    echo "No heavy_worker_status.json"
  fi
  echo
  if [[ -f "$ROOT/iris_daemon_status.json" ]]; then
    cat "$ROOT/iris_daemon_status.json"
  else
    echo "No iris_daemon_status.json"
  fi
  echo
  pending=$("$PY" "$ROOT/process_queue.py")
  echo "Pending queue: $pending"
}

case "${1:-start}" in
  start)
    start_bridge
    start_daemon
    start_heavy
    start_iris
    ;;
  stop)
    stop_bridge
    stop_daemon
    stop_heavy
    stop_iris
    ;;
  restart)
    stop_bridge
    stop_daemon
    stop_heavy
    stop_iris
    wait_for_stop "$PY $BRIDGE"
    wait_for_stop "$PY $DAEMON"
    wait_for_stop "$PY $HEAVY"
    wait_for_stop "$PY $IRIS"
    sleep 1
    start_bridge
    start_daemon
    start_heavy
    start_iris
    ;;
  safe)
    safe_restart
    ;;
  status)
    status_all
    ;;
  bridge)
    case "${2:-start}" in
      start) start_bridge ;;
      stop) stop_bridge ;;
      restart) restart_bridge ;;
      *) echo "Usage: $0 bridge {start|stop|restart}" ;;
    esac
    ;;
  daemon)
    case "${2:-start}" in
      start) start_daemon ;;
      stop) stop_daemon ;;
      restart) restart_daemon ;;
      *) echo "Usage: $0 daemon {start|stop|restart}" ;;
    esac
    ;;
  heavy)
    case "${2:-start}" in
      start) start_heavy ;;
      stop) stop_heavy ;;
      restart)
        stop_heavy
        wait_for_stop "$PY $HEAVY"
        start_heavy
        ;;
      *) echo "Usage: $0 heavy {start|stop|restart}" ;;
    esac
    ;;
  iris)
    case "${2:-start}" in
      start) start_iris ;;
      stop) stop_iris ;;
      restart)
        stop_iris
        wait_for_stop "$PY $IRIS"
        start_iris
        ;;
      *) echo "Usage: $0 iris {start|stop|restart}" ;;
    esac
    ;;
  link)
    "$PY" "$ROOT/link_cli.py" "${@:2}"
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|safe|status|bridge|daemon|heavy|iris|link}"
    ;;
esac
