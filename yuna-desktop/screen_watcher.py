#!/usr/bin/env python3
"""Фоновое живое зрение: постоянно кадр + редкое короткое описание (без кучи скринов)."""
from __future__ import annotations

import logging
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from desk_config import HOSHI_CORE, SCREEN_WATCH_INTERVAL  # noqa: E402
from live_vision import (  # noqa: E402
    DESCRIBE_EVERY_SEC,
    FRAME_EVERY_SEC,
    live_tick,
    load_live,
)
from screen_context import hypr_active_window  # noqa: E402
from vision import prune_screenshots  # noqa: E402
from yuna_status import set_component  # noqa: E402

log = logging.getLogger("yuna.screen")
_running = True
_last_frame = 0.0
_last_describe = 0.0
_last_win: str | None = None


def _stop(*_a: object) -> None:
    global _running
    _running = False


def _settings() -> tuple[bool, float]:
    enabled, interval = True, min(SCREEN_WATCH_INTERVAL, 8.0)
    try:
        from storage import load_settings

        d = load_settings().get("desktop") or {}
        enabled = d.get("screen_watch", True)
        interval = float(d.get("screen_interval", interval))
    except Exception:
        pass
    return bool(enabled), max(2.0, min(interval, 12.0))


def _tick() -> None:
    global _last_frame, _last_describe, _last_win
    now = time.time()
    win = hypr_active_window() or ""
    window_changed = bool(win) and win != _last_win

    frame_every = FRAME_EVERY_SEC
    describe_every = DESCRIBE_EVERY_SEC
    try:
        from computer_control import is_enabled
        from game_memory import is_companion_active
        from neuro_pilot import is_pilot_running

        if is_companion_active(win) or is_enabled() or is_pilot_running():
            frame_every = 0.8
            describe_every = 5.0
    except Exception:
        pass

    if (now - _last_frame) >= frame_every or window_changed:
        live_tick(want_describe=False)
        _last_frame = now
        try:
            prune_screenshots(keep=2, max_age_sec=600)
        except Exception:
            pass

    if window_changed:
        describe_every = min(describe_every, 6.0)
    if (now - _last_describe) >= describe_every or (window_changed and (now - _last_describe) > 4.0):
        live_tick(want_describe=True)
        _last_describe = now

    _last_win = win or _last_win
    see = str(load_live().get("see") or "")
    detail = (see or win or "слежу")[:80]
    set_component("screen", "watching", detail)


def main() -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
    HOSHI_CORE.mkdir(parents=True, exist_ok=True)
    log.info("live vision watcher started (frame~%.1fs, describe~%.0fs)", FRAME_EVERY_SEC, DESCRIBE_EVERY_SEC)
    while _running:
        enabled, interval = _settings()
        if enabled:
            try:
                _tick()
            except Exception as e:
                log.warning("tick: %s", e)
        else:
            set_component("screen", "idle", "слежение выключено")
        # короткий сон — кадр живой
        sleep_for = min(interval, FRAME_EVERY_SEC)
        for _ in range(int(sleep_for * 2)):
            if not _running:
                break
            time.sleep(0.5)
    set_component("screen", "idle")


if __name__ == "__main__":
    main()
