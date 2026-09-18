#!/usr/bin/env python3
"""Единый статус Юны для Waybar, голоса и bridge."""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from desk_config import YUNA_STATUS

_LOCK = threading.Lock()
_PRIORITY = {"speaking": 5, "listening": 4, "playing": 4, "thinking": 3, "watching": 2, "idle": 1}
_SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_WAVE = "▁▂▃▄▅▆▇█▇▅▃▁"
_SPEAK = "◖◗◘◙"
_DEFAULT: dict[str, Any] = {
    "voice": "idle",
    "agent": "idle",
    "screen": "idle",
    "pilot": "idle",
    "detail": "Готова",
    "updated_at": "",
}

LOGO_SVG = Path(__file__).resolve().parent.parent / "assets" / "yuna.svg"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read() -> dict[str, Any]:
    if not YUNA_STATUS.exists():
        return dict(_DEFAULT)
    try:
        data = json.loads(YUNA_STATUS.read_text(encoding="utf-8"))
        out = dict(_DEFAULT)
        out.update(data)
        return out
    except Exception:
        return dict(_DEFAULT)


def _write(data: dict[str, Any]) -> None:
    data["updated_at"] = _now()
    YUNA_STATUS.parent.mkdir(parents=True, exist_ok=True)
    YUNA_STATUS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def set_component(name: str, state: str, detail: str = "") -> None:
    with _LOCK:
        data = _read()
        data[name] = state
        if detail:
            data["detail"] = detail
        elif state == "idle" and name == "voice":
            data["detail"] = data.get("detail") or "Готова"
        _write(data)


def set_state(mode: str, detail: str = "") -> None:
    set_component("voice", mode, detail)


def get_status() -> dict[str, Any]:
    data = _read()
    # подтянуть NeuroPilot, если файл есть
    pilot = str(data.get("pilot") or "idle")
    try:
        from desk_config import DATA

        pp = DATA / "neuro_pilot.json"
        if pp.exists():
            pj = json.loads(pp.read_text(encoding="utf-8"))
            if pj.get("running"):
                pilot = "playing"
                if not data.get("detail") or data.get("detail") in ("Готова", "стоп"):
                    data["detail"] = str(pj.get("see") or pj.get("kind") or "играю")[:60]
            else:
                pilot = "idle"
    except Exception:
        pass
    data["pilot"] = pilot

    mode = "idle"
    for key in ("voice", "agent", "screen", "pilot"):
        st = str(data.get(key) or "idle")
        # pilot.playing → mode playing
        if key == "pilot" and st == "playing":
            st = "playing"
        if _PRIORITY.get(st, 0) > _PRIORITY.get(mode, 0):
            mode = st
    return {
        "mode": mode,
        "detail": data.get("detail") or "Готова",
        "voice": data.get("voice", "idle"),
        "agent": data.get("agent", "idle"),
        "screen": data.get("screen", "idle"),
        "pilot": pilot,
        "updated_at": data.get("updated_at", ""),
    }


def _activity_text(st: dict[str, Any]) -> str:
    t = time.time()
    frame = int(t * 4) % len(_WAVE)
    spin = _SPIN[int(t * 8) % len(_SPIN)]
    parts: list[str] = []
    if st.get("voice") == "listening":
        parts.append(_WAVE[frame : frame + 3] if frame + 3 <= len(_WAVE) else _WAVE[frame])
    if st.get("agent") == "thinking":
        parts.append(spin)
    if st.get("voice") == "speaking":
        parts.append(_SPEAK[int(t * 3) % len(_SPEAK)])
    if st.get("screen") == "watching" and st.get("voice") == "idle" and st.get("agent") == "idle":
        parts.append("◉" if int(t * 2) % 2 else "◎")
    if st.get("pilot") == "playing":
        parts.append("▶" if int(t * 3) % 2 else "▸")
    return " ".join(parts)


def waybar_payload() -> dict[str, str]:
    st = get_status()
    mode = st["mode"]
    activity = _activity_text(st)
    text = f" {activity}" if activity else " "

    classes = ["yuna-bar"]
    if st.get("voice") == "listening":
        classes.append("listening")
    if st.get("voice") == "speaking":
        classes.append("speaking")
    if st.get("agent") == "thinking":
        classes.append("thinking")
    if st.get("screen") == "watching":
        classes.append("watching")
    if st.get("pilot") == "playing":
        classes.append("playing")
    if mode == "idle":
        classes.append("idle")

    parts = []
    if st.get("voice") == "listening":
        parts.append("слушаю")
    elif st.get("voice") == "speaking":
        parts.append("говорю")
    if st.get("agent") == "thinking":
        parts.append("думаю")
    if st.get("screen") == "watching":
        parts.append("экран")
    if st.get("pilot") == "playing":
        parts.append("играю")
    tooltip = "Юна — " + (", ".join(parts) if parts else st.get("detail") or "готова")

    return {
        "text": text,
        "tooltip": tooltip,
        "class": " ".join(classes),
    }
