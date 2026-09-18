#!/usr/bin/env python3
"""Фоновая синхронизация единой памяти с сервером."""
from __future__ import annotations

import fcntl
import logging
import signal
import sys
import time

from desk_config import MEMORY_SYNC_INTERVAL, ROOT
from memory_sync import sync_bidirectional

log = logging.getLogger("yuna.memory_hub")
_running = True
_LOCK = ROOT / ".memory_sync.lock"


def _stop(*_args: object) -> None:
    global _running
    _running = False


def _try_sync() -> None:
    _LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(_LOCK, "w", encoding="utf-8") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        try:
            sync_bidirectional()
        except Exception as e:
            log.warning("sync error: %s", e)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def main() -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log.info("memory hub started (interval=%.1fs)", MEMORY_SYNC_INTERVAL)
    while _running:
        _try_sync()
        time.sleep(MEMORY_SYNC_INTERVAL)
    log.info("memory hub stopped")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
    main()
