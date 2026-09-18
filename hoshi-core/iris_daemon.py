#!/usr/bin/env python3
"""Отдельный демон Iris-биржи: мониторинг, алерты, автоторговля."""
from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import signal
import sys
from datetime import datetime

from config import IRIS_LOCK, IRIS_STATUS, LOG

log = logging.getLogger("hoshi.iris_daemon")


def _log(msg: str) -> None:
    line = f"[iris {datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def save_status(**kw) -> None:
    data: dict = {}
    if IRIS_STATUS.exists():
        try:
            data = json.loads(IRIS_STATUS.read_text(encoding="utf-8"))
        except Exception:
            pass
    data.update(kw)
    data["updated_at"] = datetime.now().isoformat()
    IRIS_STATUS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def acquire_lock() -> bool:
    IRIS_LOCK.parent.mkdir(parents=True, exist_ok=True)
    if IRIS_LOCK.exists():
        try:
            old = int(IRIS_LOCK.read_text(encoding="utf-8").strip())
            os.kill(old, 0)
            return False
        except (ProcessLookupError, ValueError, OSError):
            IRIS_LOCK.unlink(missing_ok=True)
    IRIS_LOCK.write_text(str(os.getpid()), encoding="utf-8")
    return True


def release_lock() -> None:
    IRIS_LOCK.unlink(missing_ok=True)


async def _run_loop() -> None:
    from iris_monitor import interval_seconds, iris_monitor_tick, is_enabled

    await asyncio.sleep(90)
    ticks = 0
    while True:
        enabled = is_enabled()
        try:
            if enabled:
                await iris_monitor_tick()
                ticks += 1
                save_status(running=True, ticks=ticks, enabled=True, last_error="")
        except Exception as e:
            _log(f"tick error: {e}")
            save_status(running=True, ticks=ticks, enabled=enabled, last_error=str(e)[:300])
        await asyncio.sleep(interval_seconds() if enabled else 300)


def main() -> None:
    if not acquire_lock():
        _log("Another iris daemon is already running")
        sys.exit(0)

    atexit.register(release_lock)

    def _stop(*_args) -> None:
        save_status(running=False)
        release_lock()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    save_status(running=True, ticks=0, enabled=False, last_error="")
    _log("Iris daemon started")
    try:
        asyncio.run(_run_loop())
    finally:
        save_status(running=False)
        release_lock()


if __name__ == "__main__":
    main()
