# Cursor Daemon Guide

## What You Are Building

A "Cursor daemon" is usually not Cursor itself running as a daemon process.
Instead, it is a local automation system that uses Cursor as one of its workers.

The safest mental model is:

1. a long-running local process watches for work
2. it stores work and state on disk
3. it wakes Cursor only when needed
4. Cursor performs reasoning or transformation
5. the local process records results, status, and logs

If the next agent wants to build a similar system for a different direction,
reuse this architecture rather than trying to keep everything inside one fragile
chat session.

## The Five Layers

Every good Cursor daemon should have these five layers:

1. **Ingress**
   Receives new work from somewhere:
   Telegram, files, HTTP, cron, a database table, or a watched folder.

2. **Queue/State**
   Saves incoming tasks to disk so work survives restarts.

3. **Worker**
   The long-running process that picks tasks and executes them.

4. **Wake Mechanism**
   A loop or watcher that emits a unique sentinel line so Cursor can be woken only
   when needed.

5. **Operator Surface**
   Start/stop/status/restart, plus logs and status JSON.

If you skip any of these, the system becomes hard to recover, hard to inspect, or
too dependent on one live agent session.

## The Pattern Used in `imouto-localization`

Current project mapping:

- ingress: `tg_bridge.py`
- queue: `tg_inbox/*.json`, `tg_outbox/*.json`
- worker: `translate_daemon.py`
- wake mechanism: `tg_poll_loop.sh`
- operator surface: `daemon_ctl.sh`

That pattern is transferable to a completely different domain.

## The Correct File Set for a New Daemon

If another agent is asked to build a similar process for another purpose, it
should normally create files like these:

1. `my_daemon.py`
   Main worker loop

2. `my_daemon_ctl.sh`
   Operational wrapper

3. `event_bridge.py`
   Converts external events into local queue items

4. `process_queue.py`
   Cheap filter/classifier that decides whether Cursor wake-up is needed

5. `poll_loop.sh`
   Emits `AGENT_LOOP_TICK_<purpose> ...`

6. `notify.py`
   Sends results back to the user or upstream system

7. `daemon_status.json`
   Machine-readable live status

8. `pipeline_state.json`
   Resume state after restart

9. `daemon.log`
   Append-only text log

10. `.daemon.lock`
   Single-instance guard

## How to Design the Worker

The worker should be deterministic in structure, even if it calls AI inside.

A robust worker usually does this:

1. acquire lock
2. load persisted state
3. write `running=true` into status
4. loop forever
5. fetch next work item
6. if no work exists, sleep
7. if work exists, process it
8. write outputs
9. update status and state
10. on failure, log and retry after backoff
11. on exit, release lock and write `running=false`

### Recommended Python skeleton

```python
#!/usr/bin/env python3
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path("/absolute/path/to/project")
LOG = ROOT / "daemon.log"
LOCK = ROOT / ".daemon.lock"
STATUS = ROOT / "daemon_status.json"
STATE = ROOT / "pipeline_state.json"


def log(msg: str):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def save_status(**kw):
    data = {}
    if STATUS.exists():
        try:
            data = json.loads(STATUS.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data.update(kw)
    data["updated_at"] = datetime.now().isoformat()
    STATUS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"cursor": 0, "mode": "idle"}


def save_state(state):
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def acquire_lock():
    if LOCK.exists():
        try:
            pid = int(LOCK.read_text().strip())
            os.kill(pid, 0)
            return False
        except Exception:
            pass
    LOCK.write_text(str(os.getpid()))
    return True


def release_lock():
    LOCK.unlink(missing_ok=True)


def process_one_item(state):
    # Replace with real domain logic.
    return False


def main():
    if not acquire_lock():
        log("Another daemon is already running")
        sys.exit(0)

    state = load_state()
    save_status(running=True, state=state, last_error="")
    log("Daemon started")

    try:
        while True:
            try:
                did_work = process_one_item(state)
                save_state(state)
                save_status(running=True, state=state, last_error="")
                time.sleep(5 if did_work else 30)
            except Exception as e:
                log(f"ERROR: {e}")
                save_status(running=True, state=state, last_error=str(e))
                time.sleep(60)
    finally:
        release_lock()
        save_status(running=False, state=state)


if __name__ == "__main__":
    main()
```

## How to Call Cursor from the Daemon

Inside the worker, Cursor should be used as a subprocess, not as a magical shared
memory session.

Typical pattern:

```python
import subprocess

def run_cursor(prompt: str, cwd: str) -> str:
    r = subprocess.run(
        ["cursor-agent", "--trust", "--print", "-p", prompt],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if r.returncode != 0:
        raise RuntimeError(f"cursor-agent failed: {r.stderr[:500]}")
    return (r.stdout or "").strip()
```

### Critical rule

Never assume the model returns perfect JSON or perfect formatting.

Always add:

- output cleanup
- parse validation
- retry logic
- fallback behavior

The current localization daemon already does this with tolerant parsing in
`parse_translation_response()`.

## How to Wake Cursor Periodically

For recurring local work, use a shell loop that emits a unique sentinel line.
This follows the local Cursor loop pattern.

### Fixed interval example

```bash
#!/bin/bash
INTERVAL="${MY_POLL_INTERVAL:-30}"

while true; do
  sleep "$INTERVAL"
  echo "AGENT_LOOP_TICK_myqueue {\"prompt\":\"Check pending queue items in /path/to/queue and process them.\",\"interval_sec\":$INTERVAL}"
done
```

### Better event-gated example

```bash
#!/bin/bash
INTERVAL="${MY_POLL_INTERVAL:-30}"
QUEUE="/path/to/queue"
LAST_WAKE="/path/to/.last_wake"

while true; do
  sleep "$INTERVAL"

  pending=$(python3 /path/to/process_queue.py)
  if [[ "$pending" -gt 0 ]]; then
    now=$(date +%s)
    last=0
    [[ -f "$LAST_WAKE" ]] && last=$(cat "$LAST_WAKE")
    if (( now - last >= 60 )); then
      echo "$now" > "$LAST_WAKE"
      echo "AGENT_LOOP_TICK_myqueue {\"prompt\":\"There are $pending pending tasks. Process only pending items and mark completed ones done.\",\"interval_sec\":$INTERVAL}"
    fi
  fi
done
```

### Why the sentinel matters

The unique line such as `AGENT_LOOP_TICK_tg` is a wake signal. It lets another
Cursor agent reliably identify that meaningful work is available.

Without a sentinel, you get noisy polling with no clear trigger.

## How to Build the Queue Layer

Use JSON files on disk unless there is a strong reason not to.

A good queue item looks like this:

```json
{
  "id": "20260525_090700_001",
  "source": "telegram",
  "text": "user request here",
  "received_at": "2026-05-25T09:07:00",
  "status": "pending",
  "note": ""
}
```

### Recommended statuses

- `pending`
- `in_progress`
- `done`
- `error`
- `stale_skipped`

If another agent clones this pattern for a new direction, it should keep status
names boring and explicit.

## How to Build the Control Script

Every daemon should have a simple shell control script.

Example:

```bash
#!/bin/bash
set -euo pipefail
ROOT="/absolute/path/to/project"
cd "$ROOT"

start() {
  if pgrep -f "python3 $ROOT/my_daemon.py" >/dev/null 2>&1; then
    echo "Already running"
    pgrep -af "my_daemon.py"
    exit 0
  fi
  nohup python3 "$ROOT/my_daemon.py" >> "$ROOT/daemon.log" 2>&1 &
  echo "Started PID $!"
}

stop() {
  pkill -f "python3 $ROOT/my_daemon.py" 2>/dev/null || true
  rm -f "$ROOT/.daemon.lock"
  echo "Stopped"
}

status() {
  pgrep -af "my_daemon.py" || echo "Not running"
  python3 - <<'PY'
import json, pathlib
p = pathlib.Path("daemon_status.json")
print(p.read_text(encoding="utf-8") if p.exists() else "No status file")
PY
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  restart) stop; sleep 1; start ;;
  status) status ;;
  *) echo "Usage: $0 {start|stop|restart|status}" ;;
esac
```

## How to Split Responsibilities Correctly

Do not make the main daemon also do everything else.

Good split:

- bridge receives
- queue stores
- pre-processor decides if the agent is needed
- daemon executes business logic
- notifier sends results
- control script manages process lifecycle

This split matters because the next agent can then swap only one layer while
keeping the rest.

## How to Clone This for Another Direction

Suppose the next agent must build a similar process, but not for localization.
For example:

- product description generation
- moderation queue
- OCR correction
- screenshot summarization
- changelog generation

Then the correct adaptation steps are:

1. Keep the daemon skeleton unchanged.
2. Replace only the domain logic in `process_one_item()`.
3. Replace the queue input schema if needed.
4. Keep the same status/log/lock discipline.
5. Keep the same operator script.
6. Keep the same wake sentinel pattern.
7. Add domain-specific validation before marking tasks `done`.

## The Prompt the Next Agent Should Receive

If you want another Cursor agent to build a parallel daemon in a different
direction, give it a prompt like this:

```text
Build a local daemonized Cursor workflow in /absolute/path/to/project.

Requirements:
1. Create a long-running Python worker with:
   - daemon.log
   - daemon_status.json
   - pipeline_state.json
   - .daemon.lock
2. Create a shell control script with start/stop/restart/status.
3. Create a queue folder with JSON task files.
4. Create a bridge/pre-processor that writes tasks into the queue.
5. Create a poll loop that emits a unique AGENT_LOOP_TICK_<purpose> sentinel only when useful work exists.
6. Use cursor-agent as a subprocess, not as an implicit chat dependency.
7. Persist all important state to disk so the process can resume after restart.
8. Add bounded retries and clear logging.
9. Do not hardcode secrets; use environment variables where possible.
10. Add a short README describing how to operate the system.

Domain:
<describe the new direction here>

Task completion rules:
- Items start as pending
- Work items become done only after validation
- Failures must be logged and reflected in daemon_status.json
- The daemon must survive restarts and avoid duplicate concurrent instances
```

That prompt is much better than simply saying "make me a daemon".

## Mistakes to Avoid

1. **No lock file**
   Then two workers may process the same queue.

2. **No persistent state**
   Restart means total confusion.

3. **No status JSON**
   Users and bots cannot see what is happening.

4. **Trusting model output blindly**
   This is exactly how parsing failures happen.

5. **Doing all work in Telegram handler**
   Ingress should stay thin.

6. **No throttling on wake-up**
   Cursor gets spammed.

7. **No stale-message policy**
   Old queue items accumulate forever.

8. **Hardcoded secrets**
   Bad for reuse and unsafe for sharing.

## Operational Checklist

Before you call the daemon complete, verify all of this:

1. starting twice does not create a second worker
2. stopping removes the lock and actually stops the process
3. status shows useful state
4. the log file grows with timestamped entries
5. queue items survive restart
6. failed tasks are visible
7. retried tasks do not duplicate completed side effects
8. wake signal fires only when useful work exists
9. domain output is validated before `done`
10. secrets are not embedded in the clone unless strictly local and intentional

## Recommended Improvements Over the Current Project

If the next agent is building version 2 of this pattern, it should improve on the
current project in these ways:

1. move bot token and chat id to environment variables
2. avoid duplicate logging lines
3. remove shell `grep` from queue counting and use Python consistently
4. add `in_progress` status when an item is being handled
5. record per-task error details instead of only global errors
6. add a dead-letter or `error` folder for permanently failed tasks
7. add a small README for operators

## Final Mental Model

Think of the system like this:

- Cursor is the reasoning engine
- Python is the durable orchestrator
- shell is the scheduler/wake mechanism
- JSON files are the memory
- logs/status files are the observability layer

If the next agent keeps those boundaries, it can recreate the same pattern for a
completely different direction with much less risk.
