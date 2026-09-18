"""
Исполнитель действий VPT → UInput (мышь/клавиши Minecraft).

VPT выдаёт dict как в MineRL TARGET_ACTION_SPACE:
  buttons 0/1 + camera=[pitch, yaw] в градусах.
Мы переводим это в реальные нажатия через computer_control Юны.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Сколько пикселей мыши на 1 градус камеры VPT.
# Подбирается под чувствительность мыши в Minecraft (по умолчанию ~0.5).
CAMERA_PX_PER_DEG = 8.0

# Кнопки, которые держим между тиками (не импульс)
_HOLD_KEYS = ("forward", "back", "left", "right", "jump", "sneak", "sprint")
_KEY_MAP = {
    "forward": "w",
    "back": "s",
    "left": "a",
    "right": "d",
    "jump": "space",
    "sneak": "shift",
    "sprint": "ctrl",
    "drop": "q",
    "inventory": "e",
    "ESC": "esc",
    "hotbar.1": "1",
    "hotbar.2": "2",
    "hotbar.3": "3",
    "hotbar.4": "4",
    "hotbar.5": "5",
    "hotbar.6": "6",
    "hotbar.7": "7",
    "hotbar.8": "8",
    "hotbar.9": "9",
}

_prev_hold: set[str] = set()
_attack_down = False
_use_down = False


def reset_holds() -> None:
    """Отпустить всё (стоп пилота / смерть / меню)."""
    global _prev_hold, _attack_down, _use_down
    from computer_control import key_up, mouse_up

    for name in list(_prev_hold):
        try:
            key_up(_KEY_MAP[name])
        except Exception:
            pass
    _prev_hold.clear()
    if _attack_down:
        try:
            mouse_up("left")
        except Exception:
            pass
        _attack_down = False
    if _use_down:
        try:
            mouse_up("right")
        except Exception:
            pass
        _use_down = False


def apply_action(action: dict[str, Any], *, tick_sec: float = 0.05) -> list[str]:
    """Применить один тик действий VPT. Возвращает краткий лог."""
    global _prev_hold, _attack_down, _use_down
    from computer_control import key_down, key_tap, key_up, mouse_down, mouse_up, mouse_move_rel

    log: list[str] = []
    want_hold: set[str] = set()
    for name in _HOLD_KEYS:
        if int(action.get(name) or 0) == 1:
            want_hold.add(name)

    # отпустить то, что больше не нужно
    for name in _prev_hold - want_hold:
        key_up(_KEY_MAP[name])
        log.append(f"-{name}")
    # зажать новое
    for name in want_hold - _prev_hold:
        key_down(_KEY_MAP[name])
        log.append(f"+{name}")
    _prev_hold = want_hold

    # импульсные клавиши
    for name in ("drop", "inventory", "ESC"):
        if int(action.get(name) or 0) == 1:
            key_tap(_KEY_MAP[name])
            log.append(name)
    for i in range(1, 10):
        k = f"hotbar.{i}"
        if int(action.get(k) or 0) == 1:
            key_tap(str(i))
            log.append(k)

    # атака / использование (ЛКМ / ПКМ)
    want_attack = int(action.get("attack") or 0) == 1
    want_use = int(action.get("use") or 0) == 1
    if want_attack and not _attack_down:
        mouse_down("left")
        _attack_down = True
        log.append("+attack")
    elif not want_attack and _attack_down:
        mouse_up("left")
        _attack_down = False
        log.append("-attack")
    if want_use and not _use_down:
        mouse_down("right")
        _use_down = True
        log.append("+use")
    elif not want_use and _use_down:
        mouse_up("right")
        _use_down = False
        log.append("-use")

    # камера: [pitch, yaw] градусы → мышь (dy, dx)
    cam = action.get("camera") or [0.0, 0.0]
    try:
        pitch = float(cam[0])
        yaw = float(cam[1])
    except Exception:
        pitch, yaw = 0.0, 0.0
    dx = int(round(yaw * CAMERA_PX_PER_DEG))
    dy = int(round(pitch * CAMERA_PX_PER_DEG))
    if dx or dy:
        mouse_move_rel(dx, dy)
        log.append(f"cam({dx},{dy})")

    # держим тик, чтобы удержание клавиш ощущалось
    if tick_sec > 0:
        time.sleep(tick_sec)
    return log
