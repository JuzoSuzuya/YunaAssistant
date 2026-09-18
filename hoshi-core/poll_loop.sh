#!/bin/bash
INTERVAL="${HOSHI_POLL_INTERVAL:-30}"
ROOT="/root/projects/Hoshi"
PY="$ROOT/.venv/bin/python3"
LAST_WAKE="$ROOT/.last_wake"

while true; do
  sleep "$INTERVAL"
  pending=$("$PY" "$ROOT/process_queue.py")
  if [[ "$pending" -gt 0 ]]; then
    now=$(date +%s)
    last=0
    [[ -f "$LAST_WAKE" ]] && last=$(cat "$LAST_WAKE")
    if (( now - last >= 60 )); then
      echo "$now" > "$LAST_WAKE"
      echo "AGENT_LOOP_TICK_hoshi {\"prompt\":\"There are $pending pending Hoshi tasks in $ROOT/data/tg_inbox. Process only pending items.\",\"interval_sec\":$INTERVAL}"
    fi
  fi
done
