#!/usr/bin/env python3
"""
Управление ПК Юной: мышь + клавиатура через /dev/uinput (Wayland/Hyprland).

Включается ТОЛЬКО по явной просьбе хозяина («управляй», «играй за меня»).
Стоп: «стоп», «хватит управлять», или control_stop().
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from desk_config import HOSHI_CORE

log = logging.getLogger("yuna.control")

STATE_PATH = HOSHI_CORE / "data" / "control_state.json"

_TAKEOVER_RE = re.compile(
    r"(?:управляй|возьми\s+управлени|играй\s+за\s+меня|поиграй\s+за\s+меня|"
    r"веди\s+(?:игру|за\s+меня)|control\s+my\s+(?:pc|computer|mouse)|"
    r"play\s+for\s+me|бери\s+мышь|води\s+мышь|сама\s+играй|"
    r"перехвати\s+управлени|автопилот|управл\w*\s+(?:моей\s+)?мышь)",
    re.I,
)
_STOP_RE = re.compile(
    r"(?:стоп(?:и|\s+управлени)?|хватит\w*\s+(?:управля|игра\w*)|отдай\s+управлени|"
    r"перестань\s+(?:управля|игра\w*)|stop\s+(?:control|playing)|отмена\s+управлени|"
    r"хватит\w*\s+игра|не\s+играй|выключ\w*\s+(?:пилот|автопилот))",
    re.I,
)
_FOCUS_FIRST_WIN_RE = re.compile(
    r"(?:открой|активируй|сфокус\w*|переключ\w*\s+на)\s+"
    r"(?:перв\w*\s+)?(?:окн\w*|window)|перв\w*\s+окн|"
    r"на\s+(?:перв\w*|\d+)\s+окн",
    re.I,
)
_WORKSPACE_RE = re.compile(
    r"(?:на\s+)?(?:рабоч\w*\s+стол\w*|workspace|воркспейс\w*|окн\w*)\s*"
    r"(?:№\s*)?(\d+)|"
    r"на\s+(\d+)\s+окн|"
    r"(?:перв\w+)\s+(?:окн|рабоч|workspace)|"
    r"workspace\s*(\d+)",
    re.I,
)
_DESKTOP_APP_RE = re.compile(
    r"\b(?:ютуб\w*|youtube|ютюб\w*|браузер|firefox|chrome|chromium|"
    r"сайт|ссылк\w*|xdg-open|telegram|discord)\b",
    re.I,
)

# Минимальный набор клавиш для игр / ОС
_KEY_MAP: dict[str, int] = {}


def _ecodes():
    from evdev import ecodes as e

    return e


def _build_key_map() -> dict[str, int]:
    e = _ecodes()
    m: dict[str, int] = {
        "esc": e.KEY_ESC,
        "enter": e.KEY_ENTER,
        "return": e.KEY_ENTER,
        "space": e.KEY_SPACE,
        "tab": e.KEY_TAB,
        "backspace": e.KEY_BACKSPACE,
        "shift": e.KEY_LEFTSHIFT,
        "ctrl": e.KEY_LEFTCTRL,
        "alt": e.KEY_LEFTALT,
        "super": e.KEY_LEFTMETA,
        "up": e.KEY_UP,
        "down": e.KEY_DOWN,
        "left": e.KEY_LEFT,
        "right": e.KEY_RIGHT,
        "w": e.KEY_W,
        "a": e.KEY_A,
        "s": e.KEY_S,
        "d": e.KEY_D,
        "e": e.KEY_E,
        "q": e.KEY_Q,
        "r": e.KEY_R,
        "f": e.KEY_F,
        "c": e.KEY_C,
        "v": e.KEY_V,
        "x": e.KEY_X,
        "z": e.KEY_Z,
        "1": e.KEY_1,
        "2": e.KEY_2,
        "3": e.KEY_3,
        "4": e.KEY_4,
        "5": e.KEY_5,
        "6": e.KEY_6,
        "7": e.KEY_7,
        "8": e.KEY_8,
        "9": e.KEY_9,
        "0": e.KEY_0,
        "f1": e.KEY_F1,
        "f2": e.KEY_F2,
        "f3": e.KEY_F3,
        "f5": e.KEY_F5,
        "f11": e.KEY_F11,
    }
    return m


_ui = None
_ui_lock = threading.Lock()
_yuna_moving = False
_last_yuna_pos: tuple[int, int] | None = None
_watch_stop = threading.Event()
_watch_thread: threading.Thread | None = None
_DEFAULT_STATE = {
    "enabled": False,
    "reason": "",
    "started_at": "",
    "last_action_at": "",
    "actions_total": 0,
    "goal": "",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return dict(_DEFAULT_STATE)
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        out = dict(_DEFAULT_STATE)
        out.update(data)
        return out
    except Exception:
        return dict(_DEFAULT_STATE)


def save_state(data: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = dict(data)
    STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def wants_takeover(text: str) -> bool:
    return bool(_TAKEOVER_RE.search(text or ""))


def wants_stop(text: str) -> bool:
    return bool(_STOP_RE.search(text or ""))


_BARE_STOP_RE = re.compile(r"^(?:юна|yuna|юно)?\s*(?:стоп|stop)[\s!.…]*$", re.I)


def wants_stop_priority(text: str) -> bool:
    """Голое «стоп»/«stop» — когда управление/пилот активны, ЭТО значит «отдай мышь»,
    а не «выключи музыку». В отличие от wants_stop, ловит и голое слово без «управление».
    """
    return bool(_BARE_STOP_RE.match((text or "").strip())) or wants_stop(text)


def wants_play(text: str) -> bool:
    """«играй / начинай играть / майн / продолжай» — реальный автопилот, не болтовня."""
    t = (text or "").strip()
    if not t:
        return False
    if re.search(
        r"(?:начинай|начни|продолж\w*|дальше|давай).{0,20}(?:игра\w*|майн|minecraft)|"
        r"(?:играй|поиграй|поиграем|сама\s+играй|веди\s+игру)|"
        r"(?:игра\w*|поиграй).{0,15}(?:в\s+)?(?:майн|minecraft|терр)|"
        r"\b(?:minecraft|майнкрафт|майн)\b|"
        r"сделай\s+ход|добыва\w*\s+(?:дерев|ресурс)|руби\s+дерев",
        t,
        re.I,
    ):
        return True
    return False


def wants_focus_first_window(text: str) -> bool:
    return bool(_FOCUS_FIRST_WIN_RE.search(text or ""))


def wants_desktop_app_task(text: str) -> bool:
    """Ютуб/браузер/сайт — не Minecraft-автопилот."""
    return bool(_DESKTOP_APP_RE.search(text or ""))


def parse_workspace(text: str) -> int | None:
    m = _WORKSPACE_RE.search(text or "")
    if not m:
        return None
    for g in m.groups():
        if g and str(g).isdigit():
            return max(1, min(20, int(g)))
    if re.search(r"перв\w+\s+(?:окн|рабоч|workspace)", text or "", re.I):
        return 1
    return None


def goto_workspace(n: int) -> str:
    _require_enabled()
    n = max(1, min(20, int(n)))
    try:
        subprocess.run(
            ["hyprctl", "dispatch", "workspace", str(n)],
            capture_output=True,
            timeout=2,
            check=False,
        )
        _bump()
        return f"Перешла на workspace {n}"
    except Exception as e:
        return f"Не смогла сменить workspace: {e}"


def focus_browser() -> str:
    """Сфокусировать Firefox/Chrome, не Cursor/Yuna."""
    _require_enabled()
    prefer = ("firefox", "chrome", "chromium", "brave", "vivaldi", "zen")
    try:
        clients = json.loads(
            subprocess.check_output(["hyprctl", "clients", "-j"], text=True, timeout=3)
        )
        cands = []
        for c in clients:
            cls = (c.get("class") or "").lower()
            if not any(p in cls for p in prefer):
                continue
            if not c.get("address"):
                continue
            cands.append(c)
        if not cands:
            return focus_first_window()
        # самый свежий / большой
        cands.sort(key=lambda c: (c.get("size") or [0, 0])[0] * (c.get("size") or [0, 0])[1], reverse=True)
        c = cands[0]
        addr = c["address"]
        subprocess.run(
            ["hyprctl", "dispatch", "focuswindow", f"address:{addr}"],
            capture_output=True,
            timeout=2,
            check=False,
        )
        _bump()
        return f"Браузер: {c.get('title') or c.get('class')}"
    except Exception as e:
        return f"Браузер не сфокусировала: {e}"


def _active_window_geom() -> tuple[int, int, int, int] | None:
    """x, y, w, h активного окна или None."""
    try:
        w = json.loads(
            subprocess.check_output(["hyprctl", "activewindow", "-j"], text=True, timeout=2)
        )
        at = w.get("at") or [0, 0]
        size = w.get("size") or [0, 0]
        return int(at[0]), int(at[1]), int(size[0]), int(size[1])
    except Exception:
        return None


def _vision_ready() -> tuple[bool, str]:
    from desk_config import OLLAMA_VISION_MODEL
    from ollama_brain import _tags, resolve_endpoint

    vision = OLLAMA_VISION_MODEL
    try:
        url, _ = resolve_endpoint(vision)
        names = _tags(url)
    except Exception as e:
        return False, f"Ollama зрение недоступно: {e}"
    if vision not in names and not any(vision in n for n in names):
        return False, f"Нет модели зрения «{vision}»."
    return True, vision


def _parse_json_obj(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    m = re.search(r"\{.*\}", raw or "", re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def see_point(target: str, *, hint: str = "") -> dict[str, Any]:
    """
    Живой кадр → VLM: куда кликнуть. Возвращает {x,y,see,ok,error}.
    Берёт live.png из фона (не ждёт новый grim+длинный VLM как раньше).
    """
    ok, vision_or_err = _vision_ready()
    if not ok:
        return {"ok": False, "error": vision_or_err, "see": "", "x": 0, "y": 0}

    from desk_config import OLLAMA_VISION_MODEL
    from live_vision import get_live_frame
    from ollama_brain import chat_once

    shot = get_live_frame(max_age_sec=4.0)
    if not shot:
        return {"ok": False, "error": "нет живого кадра", "see": "", "x": 0, "y": 0}

    tw, th = screen_size()
    iw, ih = tw, th
    try:
        from PIL import Image

        with Image.open(shot) as im:
            iw, ih = im.size
    except Exception:
        pass

    cx, cy = cursor_pos()
    prompt = f"""Ты глаза Юны. На скриншоте рабочий стол / браузер.
Найди на картинке: {target}
{hint}

Разрешение кадра {iw}x{ih}. Курсор сейчас примерно ({cx},{cy}).
Верни ТОЛЬКО JSON без markdown:
{{"x": <int пиксель по горизонтали от левого края кадра>,
 "y": <int пиксель по вертикали от верха кадра>,
 "see": "<что именно видишь в 1 короткой фразе>",
 "confidence": <0..1>}}

x,y — центр кликабельного элемента. Не выдумывай UI которого нет.
"""
    raw = ""
    try:
        raw = chat_once(
            [
                {
                    "role": "system",
                    "content": "Смотри на картинку. Ответь только JSON с координатами.",
                },
                {"role": "user", "content": prompt},
            ],
            model=OLLAMA_VISION_MODEL,
            images=[str(shot)],
            temperature=0.1,
            num_predict=160,
            timeout=45,
        )
    except Exception as e:
        return {"ok": False, "error": f"зрение: {e}", "see": "", "x": 0, "y": 0}
    data = _parse_json_obj(raw or "")
    try:
        x = int(data.get("x", 0))
        y = int(data.get("y", 0))
    except Exception:
        return {
            "ok": False,
            "error": "VLM не дала координаты",
            "see": str(data.get("see") or raw or "")[:120],
            "x": 0,
            "y": 0,
        }

    # масштаб кадра → экран (если grim другой dpi)
    if iw > 0 and ih > 0 and (iw != tw or ih != th):
        x = int(x * tw / iw)
        y = int(y * th / ih)
    x = max(0, min(tw - 1, x))
    y = max(0, min(th - 1, y))
    see = str(data.get("see") or "").strip()[:160]
    conf = float(data.get("confidence") or 0.5)

    # запомнить что видели
    st = load_state()
    hist = list(st.get("vision_log") or [])
    hist.append({"target": target[:80], "see": see, "x": x, "y": y, "at": _now()})
    st["vision_log"] = hist[-12:]
    st["last_see"] = see
    save_state(st)

    return {
        "ok": conf >= 0.25 and (x > 5 or y > 5),
        "x": x,
        "y": y,
        "see": see,
        "confidence": conf,
        "error": "" if conf >= 0.25 else "низкая уверенность",
    }


def click_seen(target: str, *, hint: str = "", do_click: bool = True) -> str:
    """Посмотреть → плавно вести → клик. Короткий отчёт."""
    _require_enabled()
    spot = see_point(target, hint=hint)
    if not spot.get("ok"):
        return f"Не вижу «{target}»: {spot.get('error') or spot.get('see') or '?'}"
    x, y = int(spot["x"]), int(spot["y"])
    mouse_move_to(x, y, duration=0.55)
    if not is_enabled():
        return "Остановилась — ты взял мышь."
    msg = f"Вижу: {spot.get('see') or target} → ({x},{y})"
    if do_click:
        click("left")
        msg += " · клик"
    try:
        from game_memory import record_action

        record_action(f"eyes/{target}: {spot.get('see')} @ {x},{y}", role="yuna")
    except Exception:
        pass
    return msg


def youtube_browse_with_mouse(query: str | None = None) -> str:
    """
    Музыка = реальный звук (mpv). Браузер не открываем каждый раз —
    иначе только плодятся вкладки без звука с точки зрения хозяина.
    """
    try:
        from actions import _recent_heard_queries, pick_yuna_music, play_music
    except Exception:
        return "Не могу включить музыку."

    q = (query or "").strip()
    boring = not q or q.lower() in ("lofi hip hop radio", "lofi", "lofi hip hop")
    if boring:
        search_q = _short_search_query(pick_yuna_music(other=False))
    else:
        try:
            if q.lower() in _recent_heard_queries():
                search_q = _short_search_query(pick_yuna_music(other=True))
            else:
                search_q = _short_search_query(q)
        except Exception:
            search_q = _short_search_query(q)

    if not is_enabled():
        enable(reason="youtube_music", goal=search_q)

    audio = play_music(search_q)
    set_goal(f"music:{search_q}")
    st = load_state()
    st["last_youtube"] = {"q": search_q, "at": _now()}
    save_state(st)
    return audio


def _short_search_query(q: str) -> str:
    t = (q or "").strip()
    t = re.sub(r"\s*#\S+", "", t)
    t = re.sub(r"\([^)]*\)", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    parts = t.split()
    if len(parts) > 6:
        t = " ".join(parts[:6])
    return t[:80] or "music"


def _needs_paste(text: str) -> bool:
    return any(ord(c) > 127 for c in (text or ""))


def _sleep_or_abort(sec: float) -> bool:
    end = time.time() + max(0.0, sec)
    while time.time() < end:
        if not is_enabled():
            return False
        time.sleep(min(0.08, end - time.time()))
    return is_enabled()


def _stopped_msg(logs: list[str] | None = None) -> str:
    return "Остановилась — ты взял мышь."


def focus_first_window() -> str:
    """Сфокусировать первое нормальное окно на активном workspace (Hyprland)."""
    _require_enabled()
    try:
        raw = subprocess.check_output(["hyprctl", "clients", "-j"], text=True, timeout=3)
        clients = json.loads(raw)
        aw = subprocess.check_output(
            ["hyprctl", "activeworkspace", "-j"], text=True, timeout=2
        )
        ws_id = json.loads(aw).get("id")
        cands = []
        for c in clients:
            if c.get("workspace", {}).get("id") != ws_id:
                continue
            if c.get("mapped") is False:
                continue
            cls = (c.get("class") or "").lower()
            title = (c.get("title") or "").lower()
            if "yuna" in cls or "yuna" in title:
                continue
            if not c.get("address"):
                continue
            cands.append(c)
        cands.sort(key=lambda c: (c.get("at") or [0, 0])[0])
        if not cands:
            return "На этом workspace окон не вижу."
        addr = cands[0]["address"]
        title = cands[0].get("title") or cands[0].get("class") or addr
        subprocess.run(
            ["hyprctl", "dispatch", "focuswindow", f"address:{addr}"],
            capture_output=True,
            timeout=2,
            check=False,
        )
        _bump()
        return f"Открыла/сфокусировала окно: {title}"
    except Exception as e:
        log.warning("focus_first_window: %s", e)
        return f"Не смогла сфокусировать окно: {e}"


def is_enabled() -> bool:
    return bool(load_state().get("enabled"))


def enable(reason: str = "", goal: str = "") -> str:
    global _last_yuna_pos
    st = load_state()
    st["enabled"] = True
    st["reason"] = (reason or "owner asked")[:200]
    st["goal"] = (goal or st.get("goal") or "")[:300]
    st["started_at"] = _now()
    save_state(st)
    _ensure_ui()
    _last_yuna_pos = cursor_pos()
    _start_user_watch()
    return "Управление включено. Если сам пошевелишь мышь/кликнешь — отпущу. «стоп» тоже."


def prove_control() -> str:
    """Плавный короткий ход — без телепорта."""
    _require_enabled()
    x0, y0 = cursor_pos()
    try:
        mouse_move_to(x0 + 90, y0 + 20, duration=0.35)
        mouse_move_to(x0, y0, duration=0.35)
    except Exception as e:
        return f"Управление по флагу есть, но мышь не слушается: {e}"
    x1, y1 = cursor_pos()
    if abs(x1 - x0) < 2 and abs(y1 - y0) < 2:
        return "Курсор не сдвинулся. Проверь /dev/uinput."
    return f"Мышь у меня (плавно): ({x0},{y0}) → ({x1},{y1})."


def disable() -> str:
    _stop_user_watch()
    st = load_state()
    st["enabled"] = False
    st["reason"] = "stopped"
    save_state(st)
    _close_ui()
    return "Управление выключено, мышь снова твоя."


def _physical_mice() -> list:
    """Реальные мыши (не yuna-control uinput)."""
    from evdev import InputDevice, ecodes, list_devices

    out = []
    for path in list_devices():
        try:
            d = InputDevice(path)
        except Exception:
            continue
        name = (d.name or "").lower()
        if "yuna-control" in name or "virtual" in name:
            continue
        caps = d.capabilities()
        rels = caps.get(ecodes.EV_REL, [])
        keys = caps.get(ecodes.EV_KEY, [])
        has_move = ecodes.REL_X in rels and ecodes.REL_Y in rels
        has_btn = any(
            b in keys for b in (ecodes.BTN_LEFT, ecodes.BTN_RIGHT, ecodes.BTN_MIDDLE, ecodes.BTN_MOUSE)
        )
        if has_move or has_btn:
            out.append(d)
    return out


def _drain_mice(mice: list) -> None:
    for d in mice:
        try:
            while d.read_one() is not None:
                pass
        except Exception:
            pass


def _start_user_watch() -> None:
    """Стоп только от физической мыши — не от прыжка курсора при открытии Firefox."""
    global _watch_thread
    _watch_stop.clear()
    if _watch_thread and _watch_thread.is_alive():
        return

    def _loop() -> None:
        import select as sel

        from evdev import ecodes

        mice = _physical_mice()
        _drain_mice(mice)
        if not mice:
            log.warning("no physical mouse for watch — only verbal «стоп»")
            while not _watch_stop.wait(0.5):
                if not is_enabled():
                    break
            return

        fds = {d.fd: d for d in mice}
        while not _watch_stop.is_set():
            if not is_enabled():
                break
            try:
                ready, _, _ = sel.select(list(fds.keys()), [], [], 0.25)
            except Exception:
                time.sleep(0.1)
                continue
            if not ready:
                continue
            hit = False
            for fd in ready:
                d = fds.get(fd)
                if d is None:
                    continue
                try:
                    for ev in d.read():
                        if ev.type == ecodes.EV_REL and ev.code in (
                            ecodes.REL_X,
                            ecodes.REL_Y,
                            ecodes.REL_WHEEL,
                        ):
                            if ev.value != 0:
                                hit = True
                        elif (
                            ev.type == ecodes.EV_KEY
                            and ev.value == 1
                            and ev.code
                            in (ecodes.BTN_LEFT, ecodes.BTN_RIGHT, ecodes.BTN_MIDDLE, ecodes.BTN_MOUSE)
                        ):
                            hit = True
                except BlockingIOError:
                    pass
                except OSError:
                    pass
            if hit:
                log.info("physical mouse used — releasing control")
                try:
                    disable()
                except Exception:
                    pass
                break

    _watch_thread = threading.Thread(target=_loop, name="yuna-mouse-watch", daemon=True)
    _watch_thread.start()


def _stop_user_watch() -> None:
    _watch_stop.set()


def set_goal(goal: str) -> None:
    st = load_state()
    st["goal"] = (goal or "")[:300]
    save_state(st)


def _ensure_ui():
    global _ui, _KEY_MAP
    with _ui_lock:
        if _ui is not None:
            return _ui
        from evdev import UInput, ecodes as e

        if not _KEY_MAP:
            _KEY_MAP = _build_key_map()
        keys = list(_KEY_MAP.values()) + [
            e.BTN_LEFT,
            e.BTN_RIGHT,
            e.BTN_MIDDLE,
            e.KEY_LEFTSHIFT,
            e.KEY_LEFTCTRL,
        ]
        # буквы для type_text
        for code in range(e.KEY_A, e.KEY_Z + 1):
            if code not in keys:
                keys.append(code)
        for code in range(e.KEY_1, e.KEY_0 + 1):
            if code not in keys:
                keys.append(code)
        cap = {
            e.EV_KEY: keys,
            e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL],
        }
        _ui = UInput(cap, name="yuna-control")
        log.info("UInput device ready")
        return _ui


def _close_ui() -> None:
    global _ui
    with _ui_lock:
        if _ui is not None:
            try:
                _ui.close()
            except Exception:
                pass
            _ui = None


def _require_enabled() -> None:
    if not is_enabled():
        raise PermissionError(
            "Управление выключено. Хозяин должен сказать «управляй» / «играй за меня»."
        )


def _bump() -> None:
    st = load_state()
    st["actions_total"] = int(st.get("actions_total") or 0) + 1
    st["last_action_at"] = _now()
    save_state(st)


def cursor_pos() -> tuple[int, int]:
    try:
        out = subprocess.check_output(["hyprctl", "cursorpos"], text=True, timeout=2).strip()
        # "414, 570" or "414x570"
        out = out.replace("x", ",")
        parts = [p.strip() for p in out.split(",")]
        return int(parts[0]), int(parts[1])
    except Exception:
        return 0, 0


def screen_size() -> tuple[int, int]:
    try:
        data = json.loads(
            subprocess.check_output(["hyprctl", "monitors", "-j"], text=True, timeout=3)
        )
        # focused or first
        mon = next((m for m in data if m.get("focused")), data[0] if data else {})
        return int(mon.get("width") or 1920), int(mon.get("height") or 1080)
    except Exception:
        return 1920, 1080


def mouse_move_rel(dx: int, dy: int) -> str:
    _require_enabled()
    _rel_raw(int(dx), int(dy))
    _note_yuna_pos()
    _bump()
    x, y = cursor_pos()
    return f"мышь +({dx},{dy}) → сейчас {x},{y}"


def _rel_raw(dx: int, dy: int) -> None:
    """Один относительный шаг без bump (для плавной анимации)."""
    ui = _ensure_ui()
    e = _ecodes()
    # режем крупные рывки
    while abs(dx) > 127 or abs(dy) > 127:
        sx = max(-127, min(127, dx))
        sy = max(-127, min(127, dy))
        with _ui_lock:
            ui.write(e.EV_REL, e.REL_X, sx)
            ui.write(e.EV_REL, e.REL_Y, sy)
            ui.syn()
        dx -= sx
        dy -= sy
        time.sleep(0.004)
    if dx or dy:
        with _ui_lock:
            ui.write(e.EV_REL, e.REL_X, int(dx))
            ui.write(e.EV_REL, e.REL_Y, int(dy))
            ui.syn()


def _note_yuna_pos() -> None:
    global _last_yuna_pos
    _last_yuna_pos = cursor_pos()


def mouse_move_to(x: int, y: int, *, duration: float = 0.5, steps: int | None = None) -> str:
    """Плавный ход относительными шагами. Без hyprctl-телепорта."""
    global _yuna_moving
    _require_enabled()
    tw, th = screen_size()
    x = max(0, min(tw - 1, int(x)))
    y = max(0, min(th - 1, int(y)))
    cx, cy = cursor_pos()
    dist = max(1.0, ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5)
    n = steps if steps is not None else max(12, min(90, int(dist / 8)))
    n = max(8, int(n * max(0.25, duration) / 0.45))
    _yuna_moving = True
    try:
        for i in range(1, n + 1):
            if not is_enabled():
                return "стоп — ты забрал мышь"
            t = i / n
            ease = t * t * (3.0 - 2.0 * t)  # smoothstep
            tx = cx + (x - cx) * ease
            ty = cy + (y - cy) * ease
            curx, cury = cursor_pos()
            dx = int(round(tx - curx))
            dy = int(round(ty - cury))
            if dx or dy:
                _rel_raw(dx, dy)
            time.sleep(max(0.004, duration / n))
        # доводка если промах
        curx, cury = cursor_pos()
        if abs(x - curx) > 2 or abs(y - cury) > 2:
            _rel_raw(x - curx, y - cury)
        _note_yuna_pos()
    finally:
        _yuna_moving = False
    _bump()
    return f"плавно → {x},{y} (факт {cursor_pos()})"


def mouse_turn_smooth(dx: int, dy: int, *, duration: float = 0.32, steps: int | None = None) -> str:
    """Плавный ОТНОСИТЕЛЬНЫЙ поворот камеры кусочками — не рывок/телепорт.

    В отличие от mouse_move_rel (один большой relative-скачок), тут поворот
    размазан на N мелких шагов с равным интервалом — глазами это выглядит
    как обычное вращение камеры, а не «прыжок» взгляда.
    """
    _require_enabled()
    global _yuna_moving
    dx = int(dx)
    dy = int(dy)
    mag = max(abs(dx), abs(dy), 1)
    n = steps if steps is not None else max(5, min(36, mag // 4))
    n = max(4, n)
    _yuna_moving = True
    acc_x = 0.0
    acc_y = 0.0
    try:
        for i in range(1, n + 1):
            if not is_enabled():
                return "стоп — ты забрал мышь"
            t = i / n
            tx = dx * t
            ty = dy * t
            step_x = int(round(tx - acc_x))
            step_y = int(round(ty - acc_y))
            if step_x or step_y:
                _rel_raw(step_x, step_y)
            acc_x += step_x
            acc_y += step_y
            time.sleep(max(0.006, duration / n))
        _note_yuna_pos()
    finally:
        _yuna_moving = False
    _bump()
    return f"поворот ({dx},{dy}) плавно за {duration:.2f}s"


def click(button: str = "left", *, count: int = 1) -> str:
    _require_enabled()
    ui = _ensure_ui()
    e = _ecodes()
    btn = {
        "left": e.BTN_LEFT,
        "right": e.BTN_RIGHT,
        "middle": e.BTN_MIDDLE,
    }.get((button or "left").lower(), e.BTN_LEFT)
    count = max(1, min(3, int(count)))
    with _ui_lock:
        for _ in range(count):
            ui.write(e.EV_KEY, btn, 1)
            ui.syn()
            time.sleep(0.03)
            ui.write(e.EV_KEY, btn, 0)
            ui.syn()
            time.sleep(0.05)
    _bump()
    return f"клик {button} x{count} @ {cursor_pos()}"


def mouse_down(button: str = "left") -> str:
    _require_enabled()
    ui = _ensure_ui()
    e = _ecodes()
    btn = e.BTN_LEFT if button != "right" else e.BTN_RIGHT
    with _ui_lock:
        ui.write(e.EV_KEY, btn, 1)
        ui.syn()
    _bump()
    return f"зажала {button}"


def mouse_up(button: str = "left") -> str:
    _require_enabled()
    ui = _ensure_ui()
    e = _ecodes()
    btn = e.BTN_LEFT if button != "right" else e.BTN_RIGHT
    with _ui_lock:
        ui.write(e.EV_KEY, btn, 0)
        ui.syn()
    _bump()
    return f"отпустила {button}"


def scroll(dy: int = -3) -> str:
    _require_enabled()
    ui = _ensure_ui()
    e = _ecodes()
    dy = max(-10, min(10, int(dy)))
    with _ui_lock:
        ui.write(e.EV_REL, e.REL_WHEEL, dy)
        ui.syn()
    _bump()
    return f"скролл {dy}"


def key_tap(name: str, *, hold_ms: int = 40) -> str:
    _require_enabled()
    if not _KEY_MAP:
        globals()["_KEY_MAP"] = _build_key_map()
    key = (name or "").strip().lower()
    code = _KEY_MAP.get(key)
    if code is None and len(key) == 1:
        e = _ecodes()
        if key.isalpha():
            code = getattr(e, f"KEY_{key.upper()}", None)
        elif key.isdigit():
            code = e.KEY_0 if key == "0" else getattr(e, f"KEY_{key}", None)
    if code is None:
        return f"неизвестная клавиша: {name}"
    ui = _ensure_ui()
    e = _ecodes()
    with _ui_lock:
        ui.write(e.EV_KEY, code, 1)
        ui.syn()
        time.sleep(max(0.02, hold_ms / 1000.0))
        ui.write(e.EV_KEY, code, 0)
        ui.syn()
    _bump()
    _note_yuna_pos()
    return f"клавиша {key}"


def key_down(name: str) -> str:
    _require_enabled()
    if not _KEY_MAP:
        globals()["_KEY_MAP"] = _build_key_map()
    key = (name or "").strip().lower()
    code = _KEY_MAP.get(key)
    if code is None and len(key) == 1 and key.isalpha():
        e = _ecodes()
        code = getattr(e, f"KEY_{key.upper()}", None)
    if code is None:
        return f"неизвестная клавиша: {name}"
    ui = _ensure_ui()
    e = _ecodes()
    with _ui_lock:
        ui.write(e.EV_KEY, code, 1)
        ui.syn()
    _bump()
    return f"зажала {key}"


def key_up(name: str) -> str:
    _require_enabled()
    if not _KEY_MAP:
        globals()["_KEY_MAP"] = _build_key_map()
    key = (name or "").strip().lower()
    code = _KEY_MAP.get(key)
    if code is None and len(key) == 1 and key.isalpha():
        e = _ecodes()
        code = getattr(e, f"KEY_{key.upper()}", None)
    if code is None:
        return f"неизвестная клавиша: {name}"
    ui = _ensure_ui()
    e = _ecodes()
    with _ui_lock:
        ui.write(e.EV_KEY, code, 0)
        ui.syn()
    _bump()
    return f"отпустила {key}"


def hold_keys(keys: list[str], duration_ms: int = 200) -> str:
    """Зажать несколько клавиш (WASD), подождать, отпустить."""
    _require_enabled()
    for k in keys:
        key_down(k)
    time.sleep(max(0.05, min(3.0, duration_ms / 1000.0)))
    for k in reversed(keys):
        key_up(k)
    return f"удержала {keys} {duration_ms}мс"


def type_text(text: str) -> str:
    """Ввод латиницы/цифр/пробела. Для кириллицы — paste_text / enter_text."""
    _require_enabled()
    t = (text or "")[:120]
    for ch in t:
        if not is_enabled():
            return "стоп — ты забрал мышь"
        if ch == " ":
            key_tap("space")
        elif ch.lower().isalpha() and ch.isascii():
            if ch.isupper():
                key_down("shift")
                key_tap(ch.lower())
                key_up("shift")
            else:
                key_tap(ch.lower())
        elif ch.isdigit():
            key_tap(ch)
        else:
            return ""  # нужен paste
        time.sleep(0.025)
        _note_yuna_pos()
    return f"набрала {len(t)}"


def _wayland_env() -> dict[str, str]:
    import os
    from pathlib import Path

    env = os.environ.copy()
    uid = os.getuid()
    if not env.get("XDG_RUNTIME_DIR"):
        env["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
    rd = Path(env["XDG_RUNTIME_DIR"])
    if not env.get("WAYLAND_DISPLAY"):
        for name in ("wayland-1", "wayland-0"):
            if (rd / name).exists():
                env["WAYLAND_DISPLAY"] = name
                break
    return env


def paste_text(text: str) -> str:
    """Вставка через wl-copy + Ctrl+V."""
    _require_enabled()
    t = (text or "")[:200]
    if not t:
        return ""
    env = _wayland_env()
    ok = False
    try:
        # НЕ capture_output — иначе wl-copy часто зависает в демоне
        subprocess.Popen(
            ["wl-copy", "--", t],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        time.sleep(0.2)
        ok = True
    except Exception:
        ok = False
    if not ok:
        try:
            p = subprocess.Popen(
                ["wl-copy"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
            )
            assert p.stdin is not None
            p.stdin.write(t.encode("utf-8"))
            p.stdin.close()
            time.sleep(0.2)
            ok = True
        except Exception:
            return ""
    key_down("ctrl")
    key_tap("v")
    key_up("ctrl")
    time.sleep(0.12)
    _note_yuna_pos()
    return "ok"


def enter_text(text: str) -> bool:
    """Ввести текст в активное поле. True если получилось."""
    t = (text or "").strip()
    if not t:
        return False
    # латиница — клавишами; кириллица — буфер
    if not _needs_paste(t):
        r = type_text(t)
        if isinstance(r, str) and r.startswith("набрала"):
            return True
    return bool(paste_text(t))


def status_block() -> str:
    st = load_state()
    if not st.get("enabled"):
        return "Управление ПК: выкл (скажи «играй за меня» / «управляй»)."
    x, y = cursor_pos()
    return (
        f"Управление ПК: ВКЛ. Цель: {st.get('goal') or '—'}. "
        f"Курсор: {x},{y}. Действий: {st.get('actions_total', 0)}. "
        f"Стоп: скажи «стоп»."
    )


def run_action_batch(actions: list[dict[str, Any]], *, max_n: int = 12) -> list[str]:
    """Выполнить список действий [{op, ...}, ...]."""
    _require_enabled()
    out: list[str] = []
    for raw in (actions or [])[:max_n]:
        if not isinstance(raw, dict):
            continue
        op = str(raw.get("op") or raw.get("action") or "").lower()
        try:
            if op in ("move_rel", "mouse_rel"):
                out.append(mouse_move_rel(int(raw.get("dx", 0)), int(raw.get("dy", 0))))
            elif op in ("move_to", "mouse_to"):
                out.append(mouse_move_to(int(raw.get("x", 0)), int(raw.get("y", 0))))
            elif op == "click":
                out.append(click(str(raw.get("button", "left")), count=int(raw.get("count", 1))))
            elif op == "scroll":
                out.append(scroll(int(raw.get("dy", -3))))
            elif op in ("key", "tap"):
                out.append(key_tap(str(raw.get("key") or raw.get("name") or "")))
            elif op == "hold":
                keys = raw.get("keys") or [raw.get("key")]
                keys = [str(k) for k in keys if k]
                out.append(hold_keys(keys, int(raw.get("ms", 200))))
            elif op == "type":
                out.append(type_text(str(raw.get("text") or "")))
            elif op == "wait":
                time.sleep(min(2.0, float(raw.get("ms", 200)) / 1000.0))
                out.append("wait")
            elif op == "down":
                out.append(key_down(str(raw.get("key") or "")))
            elif op == "up":
                out.append(key_up(str(raw.get("key") or "")))
            else:
                out.append(f"неизвестный op: {op}")
        except Exception as ex:
            out.append(f"ошибка {op}: {ex}")
            break
    return out


def play_step(*, goal: str = "", max_actions: int = 8) -> str:
    """
    Умный шаг С ЗРЕНИЕМ: свежий кадр → VLM → действия.
    Не Neurosama-stream: один кадр на шаг, но свежий и с анти-залипанием.
    """
    if not is_enabled():
        enable(reason="play_step", goal=goal)
    if goal:
        set_goal(goal)

    from desk_config import OLLAMA_VISION_MODEL
    from game_memory import (
        add_playbook_rule,
        bump_session,
        clear_pending_question,
        game_memory_block_for_prompt,
        load_game_memory,
        mark_step_result,
        record_action,
        set_last_plan,
        set_pending_question,
        skill_curriculum,
    )
    from live_vision import LIVE_FRAME, capture_live_frame, get_live_see, refresh_live_description
    from ollama_brain import _tags, chat_once, resolve_endpoint
    from screen_context import hypr_active_window

    # Зрение обязательно
    try:
        url, _ = resolve_endpoint(OLLAMA_VISION_MODEL)
        names = _tags(url)
    except Exception:
        names = []
    vision = OLLAMA_VISION_MODEL
    if vision not in names and not any(vision in n for n in names):
        return (
            "Без зрения играть не могу — как слепой за клавиатурой. "
            f"Нужна модель «{vision}»."
        )

    gm0 = load_game_memory()
    if int(gm0.get("sessions_played") or 0) == 0:
        bump_session()

    # Фокус на игре, не на Cursor/чате
    win = hypr_active_window() or ""
    if not re.search(r"minecraft|java|prism|lunar|badlion|forge|fabric", win, re.I):
        try:
            subprocess.run(
                ["hyprctl", "dispatch", "focuswindow", "class:^(Minecraft|java|org.prismlauncher)"],
                capture_output=True,
                timeout=3,
            )
        except Exception:
            pass
        time.sleep(0.25)
        win = hypr_active_window() or win

    # свежий кадр (перезапись live.png) + короткое «что вижу»
    shot = capture_live_frame() or (LIVE_FRAME if LIVE_FRAME.exists() else None)
    if not shot or not shot.exists():
        return "Не смогла сделать кадр — разверни игру на монитор."
    try:
        live_see = refresh_live_description(force=True) or get_live_see(max_age_sec=20)
    except Exception:
        live_see = get_live_see(max_age_sec=20) or ""

    x, y = cursor_pos()
    w, h = screen_size()
    st = load_state()
    mem = game_memory_block_for_prompt(max_chars=1200)
    curriculum = skill_curriculum()
    last_see = str(st.get("last_play_see") or "")
    last_click = st.get("last_play_click") or []
    stuck = bool(last_see and live_see and last_see[:60] == live_see[:60])

    menuish = bool(
        re.search(
            r"меню|menu|pause|пауза|title|главн|singleplayer|одиноч|настрой|options|cursor agents",
            f"{live_see} {win}",
            re.I,
        )
    )

    anti_stuck = ""
    if stuck or menuish:
        anti_stuck = """
ВАЖНО — антизалипание:
- Если это МЕНЮ / пауза / title screen — НЕ руби дерево и НЕ ищи ресурсы.
- Сначала выйди в мир: Esc (закрыть паузу) ИЛИ кликни «Singleplayer»/«Одиночная игра»/«Играть»/«Продолжить».
- НЕ кликай снова в те же координаты что в прошлый раз.
- Если застряла в меню 2+ шага — нажми Esc, потом кликни кнопку ближе к центру экрана.
"""
        if last_click and len(last_click) >= 2:
            anti_stuck += f"- Прошлый клик был ({last_click[0]},{last_click[1]}) — НЕ повторяй его.\n"

    prompt = f"""Ты Юна. Ты ВИДИШЬ ОДИН свежий кадр игры (картинка). Это не видеопоток — только этот кадр.
Не выдумывай то, чего нет на кадре. Если меню — скажи меню. Если мир — опиши блоки/мобов/HUD.

Фоновое «глаза сейчас»: {live_see or '—'}
Активное окно: {win or '?'}
{curriculum}
{mem}
{anti_stuck}

Цель хозяина: {st.get('goal') or goal or 'играть в Minecraft'}
Разрешение {w}x{h}, курсор ({x},{y}).

Правила действий:
- В меню/паузе: только Esc / клик по кнопке входа в мир. Никаких w+рубка.
- В мире: WASD, поворот мышью (move_rel), ЛКМ/ПКМ по тому что видишь.
- max {max_actions} действий.
- self_ok_guess=false если не в мире или ничего полезного не сделала.

Верни ТОЛЬКО JSON:
{{
  "reply": "коротко хозяину",
  "question": "",
  "plan": "",
  "playbook_rule": "",
  "see": "что на ЭТОМ кадре",
  "screen_kind": "menu|world|inventory|other",
  "self_ok_guess": false,
  "actions": [{{"op":"key","key":"esc"}}]
}}

op: move_rel(dx,dy), move_to(x,y), click(button,count), scroll(dy),
key/tap(key), hold(keys,ms), type(text), wait(ms), down(key), up(key).
"""
    raw = ""
    try:
        raw = chat_once(
            [
                {
                    "role": "system",
                    "content": "Юна с глазами. Только JSON. Не выдумывай мир, если на кадре меню.",
                },
                {"role": "user", "content": prompt},
            ],
            model=vision,
            images=[str(shot)],
            temperature=0.2,
            num_predict=500,
            timeout=90,
        )
    except Exception as e:
        return f"Зрение/мозг не ответили: {e}"

    data: dict[str, Any] = {}
    try:
        data = json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw or "", re.S)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = {}

    actions = data.get("actions") if isinstance(data, dict) else None
    if not isinstance(actions, list):
        actions = []

    # Жёсткий fallback: меню → Esc + клик центр (Singleplayer часто там)
    screen_kind = str((data or {}).get("screen_kind") or "").lower()
    see = str((data or {}).get("see") or live_see or "").strip()
    if (menuish or screen_kind == "menu") and not actions:
        actions = [
            {"op": "key", "key": "esc"},
            {"op": "wait", "ms": 400},
            {"op": "move_to", "x": w // 2, "y": int(h * 0.42)},
            {"op": "click", "button": "left"},
        ]
    elif stuck and menuish:
        # не повторять тот же клик
        actions = [
            {"op": "key", "key": "esc"},
            {"op": "wait", "ms": 350},
            {"op": "move_to", "x": w // 2, "y": int(h * 0.48)},
            {"op": "click", "button": "left"},
        ]

    # выкинуть повтор прошлого move_to
    if last_click and len(last_click) >= 2:
        filtered = []
        for a in actions:
            if not isinstance(a, dict):
                continue
            if a.get("op") == "move_to":
                try:
                    if abs(int(a.get("x", -1)) - int(last_click[0])) < 25 and abs(
                        int(a.get("y", -1)) - int(last_click[1])
                    ) < 25:
                        continue
                except Exception:
                    pass
            filtered.append(a)
        if filtered:
            actions = filtered

    results = run_action_batch(actions, max_n=max_actions)

    # запомнить клик
    for a in actions:
        if isinstance(a, dict) and a.get("op") == "move_to":
            try:
                st2 = load_state()
                st2["last_play_click"] = [int(a.get("x", 0)), int(a.get("y", 0))]
                st2["last_play_see"] = see[:120]
                save_state(st2)
            except Exception:
                pass
            break
    else:
        try:
            st2 = load_state()
            st2["last_play_see"] = see[:120]
            save_state(st2)
        except Exception:
            pass

    reply = str((data or {}).get("reply") or "Сделала шаг.").strip()
    question = str((data or {}).get("question") or "").strip()
    plan = str((data or {}).get("plan") or "").strip()
    rule = str((data or {}).get("playbook_rule") or "").strip()
    ok_guess = bool((data or {}).get("self_ok_guess", False))
    if menuish or screen_kind == "menu":
        ok_guess = False  # меню ≠ прогресс в мире

    if plan:
        set_last_plan(plan)
    if rule:
        add_playbook_rule(rule)
    if question:
        set_pending_question(question)
    else:
        clear_pending_question()

    ok = ok_guess and bool(results) and screen_kind == "world"
    mark_step_result(ok, summary=(see or reply)[:160])
    record_action(f"play/see: {see or reply} | {results[:4]}", role="yuna")

    gm = load_game_memory()
    parts = [reply]
    if see:
        parts.append(f"Вижу: {see}")
    if live_see and live_see[:40] != see[:40]:
        parts.append(f"Кадр: {live_see}")
    parts.append(f"(навык {gm.get('skill_level')}, xp {gm.get('skill_xp')}, kind={screen_kind or '?'})")
    if plan and not menuish:
        parts.append(f"План: {plan}")
    if question:
        parts.append(f"Вопрос: {question}")
    if results:
        parts.append("Действия: " + "; ".join(results[:8]))
    else:
        parts.append("Действий не было — ответь или скажи «продолжай».")
    if menuish:
        parts.append("Я ещё в меню/не в мире — сначала зайду в игру, потом дерево.")
    return "\n".join(parts)


def play_session(*, goal: str = "", steps: int = 6) -> str:
    """
    Играть подряд: не останавливаться после «закрыла меню».
    Цель — добрать шаги в мире (world), меню не считается прогрессом.
    """
    if not is_enabled():
        enable(reason="play_session", goal=goal)
    from game_memory import bump_session, load_game_memory

    bump_session()
    logs: list[str] = []
    n = max(2, min(10, int(steps)))
    world_steps = 0
    need_world = max(2, n // 2)
    for i in range(n):
        if not is_enabled():
            logs.append("Остановилась — управление выключили.")
            break
        logs.append(f"— шаг {i + 1}/{n} —")
        step = play_step(goal=goal, max_actions=8)
        logs.append(step)
        low = step.lower()
        in_world = "kind=world" in low or (
            "меню" not in low and "kind=menu" not in low and "ещё в меню" not in low
        )
        if "kind=world" in low:
            world_steps += 1
            if world_steps >= need_world:
                logs.append(f"В мире уже {world_steps} шага — продолжаю по команде «продолжай».")
                break
        # если всё ещё меню — не сдаёмся раньше n
        time.sleep(0.4)
    else:
        if world_steps == 0:
            logs.append(
                "Так и не вышла в мир стабильно. Разверни Minecraft, зайди в мир руками "
                "или скажи «продолжай» — попробую ещё."
            )

    gm = load_game_memory()
    logs.append(
        f"Итог: уровень {gm.get('skill_level')} xp={gm.get('skill_xp')} "
        f"фаза={gm.get('phase')}, шагов в мире={world_steps}. "
        f"Скажи «продолжай» чтобы играла дальше (без болтовни)."
    )
    return "\n".join(logs)
