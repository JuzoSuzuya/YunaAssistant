#!/usr/bin/env python3
"""
Живое зрение Юны (в духе Neurosama): один текущий кадр + краткое «что вижу».
Фон постоянно обновляет кадр; действия читают кэш — без 2–3 минут на каждый клик.
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from desk_config import DATA, HOSHI_CORE

log = logging.getLogger("yuna.live_vision")

LIVE_DIR = HOSHI_CORE / "data" / "incoming_media" / "screenshots"
LIVE_FRAME = LIVE_DIR / "live.png"
LIVE_STATE = DATA / "live_vision.json"

# Как часто освежать описание VLM (кадр чаще)
DESCRIBE_EVERY_SEC = 12.0
FRAME_EVERY_SEC = 1.0


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_live() -> dict[str, Any]:
    if not LIVE_STATE.exists():
        return {"see": "", "window": "", "at": "", "see_at": "", "frame": str(LIVE_FRAME)}
    try:
        data = json.loads(LIVE_STATE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {"see": "", "window": "", "at": "", "see_at": "", "frame": str(LIVE_FRAME)}


def save_live(data: dict[str, Any], *, touched_see: bool = False) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    data["at"] = _now()
    if touched_see and data.get("see"):
        data["see_at"] = _now()
    data["frame"] = str(LIVE_FRAME)
    LIVE_STATE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def capture_live_frame() -> Path | None:
    """Один живой кадр — всегда перезапись live.png (без кучи файлов)."""
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = LIVE_DIR / "live.tmp.png"
    for cmd in (
        ["grim", str(tmp)],
        ["import", "-window", "root", str(tmp)],
    ):
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=8)
            if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
                tmp.replace(LIVE_FRAME)
                return LIVE_FRAME
        except FileNotFoundError:
            continue
        except Exception as e:
            log.debug("live frame: %s", e)
    return LIVE_FRAME if LIVE_FRAME.exists() else None


def get_live_frame(*, max_age_sec: float = 8.0) -> Path | None:
    """Свежий live.png для кликов/описаний — без нового grim если кадр свежий."""
    if not LIVE_FRAME.exists():
        return capture_live_frame()
    try:
        age = time.time() - LIVE_FRAME.stat().st_mtime
        if age <= max_age_sec:
            return LIVE_FRAME
    except Exception:
        pass
    return capture_live_frame()


def get_live_see(*, max_age_sec: float = 40.0) -> str:
    """Что Юна «видит сейчас» из кэша (без нового тяжёлого VLM)."""
    st = load_live()
    see = str(st.get("see") or "").strip()
    see_at = str(st.get("see_at") or st.get("at") or "")
    win = str(st.get("window") or "").strip()
    age_ok = True
    if see_at:
        try:
            ts = datetime.fromisoformat(see_at).timestamp()
            age_ok = (time.time() - ts) <= max_age_sec
        except Exception:
            age_ok = False
    if see and age_ok:
        return see
    if win:
        return f"окно: {win}"
    return ""


def refresh_live_description(*, force: bool = False) -> str:
    """Короткое VLM-описание текущего live.png (быстро, маленький ответ)."""
    st = load_live()
    if not force and st.get("see_at"):
        try:
            ts = datetime.fromisoformat(str(st["see_at"])).timestamp()
            if time.time() - ts < DESCRIBE_EVERY_SEC * 0.85 and st.get("see"):
                return str(st.get("see") or "")
        except Exception:
            pass

    frame = LIVE_FRAME if LIVE_FRAME.exists() else capture_live_frame()
    if not frame or not frame.exists():
        return str(st.get("see") or "")

    try:
        from desk_config import OLLAMA_VISION_MODEL
        from ollama_brain import chat_once
        from screen_context import hypr_active_window

        win = hypr_active_window() or ""
        raw = chat_once(
            [
                {
                    "role": "system",
                    "content": "Опиши экран в 1 короткой фразе по-русски. Без JSON, без списков.",
                },
                {
                    "role": "user",
                    "content": f"Активное окно: {win or '?'}. Что на экране? Одна фраза.",
                },
            ],
            model=OLLAMA_VISION_MODEL,
            images=[str(frame)],
            temperature=0.2,
            num_predict=60,
            timeout=35,
        )
        see = (raw or "").strip().split("\n")[0][:180]
        if see:
            save_live({"see": see, "window": win, "ts": time.time()}, touched_see=True)
            return see
    except Exception as e:
        log.debug("live describe: %s", e)
        try:
            from screen_context import hypr_active_window

            win = hypr_active_window() or ""
            save_live({"see": st.get("see") or "", "window": win})
        except Exception:
            pass
    return str(load_live().get("see") or "")


def live_tick(*, want_describe: bool = False) -> None:
    """Один тик фона: обновить кадр; иногда описание."""
    capture_live_frame()
    try:
        from screen_context import hypr_active_window

        win = hypr_active_window() or ""
        st = load_live()
        st["window"] = win
        if not want_describe:
            # только локальный кэш — без push на сервер каждый кадр
            save_live(st)
    except Exception:
        pass
    if want_describe:
        refresh_live_description(force=False)
