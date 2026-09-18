#!/usr/bin/env python3
"""Command registry: 1000+ builtin commands, custom JSON commands, multi-action execution.

Additive module over actions.py / computer_control.py (both READ-ONLY).

Design
------
- ``BUILTIN_FAMILIES``: ~66 pattern families. Each family = {id, category, patterns,
  params, actions, description, sample, stop_on_error}. Parameterized patterns expand
  to many concrete invocations (counted by ``count_commands()`` via param domains).
- Custom commands live in ``hoshi-core/data/commands/custom.json``:
  {name, patterns[], actions[], enabled, stop_on_error}. Missing/corrupt file → [].
- ``dispatch(text)`` → {command, actions, params, stop_on_error, source} | None.
  Custom commands are matched first, then builtin families. Never raises.
- ``execute(command)`` → results[] of {action, ok, output}. Actions run in order;
  ``stop_on_error`` (default True) stops the chain on the first failure.
  Action specs: {"handle": "text {param}"} → actions.handle_action(rendered);
  {"fn": "name", "args": {...}} → actions.<name> / computer_control.<name> / internal.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
import urllib.parse
from pathlib import Path

log = logging.getLogger("yuna.commands")

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
CUSTOM_DIR = PROJECT / "hoshi-core" / "data" / "commands"
CUSTOM_PATH = CUSTOM_DIR / "custom.json"

# ── param domains (concrete value sets used by count_commands) ──────────────
_PARAM_DOMAINS: dict[str, list[str]] = {
    "theme": [
        "anime",
        "stars",
        "sky",
        "night",
        "sakura",
        "city",
        "girl",
        "landscape",
        "kawaii",
        "dark",
        "space",
        "cyberpunk",
        "sword art online",
        "neon",
        "ocean",
        "forest",
        "mountain",
        "sunset",
        "rain",
        "cat",
        "dog",
        "mecha",
        "fantasy",
        "minimal",
    ],  # 24
    "app": [
        "firefox",
        "chrome",
        "chromium",
        "telegram",
        "discord",
        "steam",
        "spotify",
        "vlc",
        "obs",
        "code",
        "cursor",
        "terminal",
        "kitty",
        "alacritty",
        "files",
        "nautilus",
        "thunar",
        "gimp",
        "blender",
        "audacity",
        "libreoffice",
        "calculator",
        "calendar",
        "mail",
        "thunderbird",
        "slack",
        "zoom",
        "whatsapp",
        "signal",
        "obsidian",
        "notion",
        "youtube",
    ],  # 32
    "game": [
        "minecraft",
        "майнкрафт",
        "майн",
        "terraria",
        "stardew valley",
        "hollow knight",
        "celeste",
        "hades",
        "dead cells",
        "cuphead",
        "undertale",
        "deltarune",
        "portal",
        "half-life",
        "csgo",
        "cs2",
        "dota 2",
        "league of legends",
        "valorant",
        "genshin impact",
        "osu",
        "geometry dash",
        "subnautica",
        "stardew",
    ],  # 24
    "volume": [str(v) for v in range(0, 101, 5)],  # 21
    "brightness": [str(v) for v in range(0, 101, 5)],  # 21
    "monitor": ["1", "2", "3"],  # 3
    "workspace": [str(v) for v in range(1, 11)],  # 10
    "player": ["mpv", "firefox", "spotify", "all"],  # 4
    "source": ["bing", "pinterest", "wallhaven"],  # 3
    "count": ["1", "2", "3", "4", "5"],  # 5
    "key": [
        "enter",
        "space",
        "esc",
        "tab",
        "up",
        "down",
        "left",
        "right",
        "w",
        "a",
        "s",
        "d",
    ],  # 12
    # free-text params have no enumerable domain → count as 1 invocation each
    "query": [],
    "url": [],
    "file": [],
    "folder": [],
    "text": [],
    "x": [],
    "y": [],
}


# ── internal helpers (stdlib-only, never raise) ─────────────────────────────
def _sh(cmd: list[str], timeout: int = 8) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout or "").strip()
    except Exception as e:
        return False, str(e)


_APP_LAUNCH: dict[str, list[str]] = {
    "firefox": ["firefox"],
    "chrome": ["google-chrome"],
    "chromium": ["chromium"],
    "telegram": ["telegram-desktop"],
    "discord": ["discord"],
    "steam": ["steam"],
    "spotify": ["spotify"],
    "vlc": ["vlc"],
    "obs": ["obs"],
    "code": ["code"],
    "cursor": ["cursor"],
    "terminal": ["kitty"],
    "kitty": ["kitty"],
    "alacritty": ["alacritty"],
    "files": ["xdg-open", str(Path.home())],
    "nautilus": ["nautilus"],
    "thunar": ["thunar"],
    "gimp": ["gimp"],
    "blender": ["blender"],
    "audacity": ["audacity"],
    "libreoffice": ["libreoffice"],
    "calculator": ["gnome-calculator"],
    "calendar": ["gnome-calendar"],
    "mail": ["thunderbird"],
    "thunderbird": ["thunderbird"],
    "slack": ["slack"],
    "zoom": ["zoom"],
    "whatsapp": ["whatsapp-for-linux"],
    "signal": ["signal-desktop"],
    "obsidian": ["obsidian"],
    "notion": ["notion"],
    "youtube": ["xdg-open", "https://www.youtube.com"],
    "браузер": ["xdg-open", "https://www.google.com"],
}

_GAME_LAUNCH: dict[str, list[str]] = {
    "minecraft": ["minecraft-launcher"],
    "майнкрафт": ["minecraft-launcher"],
    "майн": ["minecraft-launcher"],
    "terraria": ["terraria"],
    "stardew valley": ["stardew-valley"],
    "hollow knight": ["hollow-knight"],
    "celeste": ["celeste"],
    "hades": ["hades"],
    "dead cells": ["dead-cells"],
    "cuphead": ["cuphead"],
    "undertale": ["undertale"],
    "deltarune": ["deltarune"],
    "portal": ["portal"],
    "half-life": ["hl2"],
    "csgo": ["csgo"],
    "cs2": ["cs2"],
    "dota 2": ["dota2"],
    "league of legends": ["leagueoflegends"],
    "valorant": ["valorant"],
    "genshin impact": ["genshin-impact"],
    "osu": ["osu"],
    "geometry dash": ["geometry-dash"],
    "subnautica": ["subnautica"],
    "stardew": ["stardew-valley"],
}


def _popen(cmd: list[str]) -> bool:
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception:
        return False


def _open_app(app: str) -> str:
    app = (app or "").strip().lower()
    cmd = _APP_LAUNCH.get(app) or [app]
    if _popen(cmd):
        return f"Открыла {app}."
    return f"Не смогла открыть {app}."


def _close_app(app: str) -> str:
    app = (app or "").strip().lower()
    ok, _ = _sh(["pkill", "-x", app])
    return f"Закрыла {app}." if ok else f"{app} и так не запущен."


def _launch_game(game: str) -> str:
    game = (game or "").strip().lower()
    cmd = _GAME_LAUNCH.get(game) or [game]
    if _popen(cmd):
        return f"Запустила игру {game}."
    return f"Не смогла запустить {game}."


def _close_game() -> str:
    ok, _ = _sh(["pkill", "-x", "minecraft-launcher"])
    return "Закрыла игру." if ok else "Игра и так не запущена."


def _screenshot() -> str:
    d = Path.home() / "Pictures" / "Screenshots"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    out = d / f"yuna-{int(time.time())}.png"
    ok, _ = _sh(["grim", str(out)])
    return f"Скриншот: {out}" if ok else "Не смогла сделать скриншот (grim?)."


def _lock_screen() -> str:
    ok, _ = _sh(["hyprctl", "dispatch", "lock"])
    if not ok:
        ok, _ = _sh(["loginctl", "lock-session"])
    return "Заблокировала экран." if ok else "Не смогла заблокировать экран."


def _sleep_system() -> str:
    ok, _ = _sh(["systemctl", "suspend"])
    return "Усыпила комп." if ok else "Не смогла усыпить комп."


def _volume_up() -> str:
    ok, _ = _sh(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "5%+"])
    if not ok:
        ok, _ = _sh(["pactl", "set-sink-volume", "@DEFAULT_SINK@", "+5%"])
    return "Громче." if ok else "Не смогла изменить громкость."


def _volume_down() -> str:
    ok, _ = _sh(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "5%-"])
    if not ok:
        ok, _ = _sh(["pactl", "set-sink-volume", "@DEFAULT_SINK@", "-5%"])
    return "Тише." if ok else "Не смогла изменить громкость."


def _set_volume(pct: str) -> str:
    try:
        pct = max(0, min(100, int(pct)))
    except Exception:
        pct = 50
    ok, _ = _sh(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{pct}%"])
    if not ok:
        ok, _ = _sh(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{pct}%"])
    return f"Громкость {pct}%." if ok else "Не смогла изменить громкость."


def _mute() -> str:
    ok, _ = _sh(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"])
    if not ok:
        ok, _ = _sh(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"])
    return "Переключила mute." if ok else "Не смогла переключить mute."


def _brightness_up() -> str:
    ok, _ = _sh(["brightnessctl", "set", "5%+"])
    return "Ярче." if ok else "Не смогла (brightnessctl?)."


def _brightness_down() -> str:
    ok, _ = _sh(["brightnessctl", "set", "5%-"])
    return "Темнее." if ok else "Не смогла (brightnessctl?)."


def _set_brightness(pct: str) -> str:
    try:
        pct = max(0, min(100, int(pct)))
    except Exception:
        pct = 50
    ok, _ = _sh(["brightnessctl", "set", f"{pct}%"])
    return f"Яркость {pct}%." if ok else "Не смогла (brightnessctl?)."


def _open_file(path: str) -> str:
    if _popen(["xdg-open", str(path)]):
        return f"Открыла: {path}"
    return f"Не смогла открыть: {path}"


def _open_folder(path: str) -> str:
    return _open_file(path)


def _open_home() -> str:
    return _open_file(str(Path.home()))


def _open_downloads() -> str:
    return _open_file(str(Path.home() / "Downloads"))


def _open_browser() -> str:
    if _popen(["xdg-open", "https://www.google.com"]):
        return "Открыла браузер."
    return "Не смогла открыть браузер."


def _focus_browser() -> str:
    try:
        from computer_control import focus_browser

        return focus_browser()
    except Exception as e:
        return f"Не смогла сфокусировать браузер: {e}"


def _browser_new_tab() -> str:
    return _open_browser()


def _browser_search(q: str) -> str:
    url = "https://www.google.com/search?q=" + urllib.parse.quote(str(q or ""))
    if _popen(["xdg-open", url]):
        return f"Ищу: {q}"
    return "Не смогла открыть поиск."


def _goto_workspace(n: str) -> str:
    try:
        n = max(1, min(20, int(n)))
    except Exception:
        n = 1
    ok, _ = _sh(["hyprctl", "dispatch", "workspace", str(n)])
    return f"Перешла на workspace {n}." if ok else "Не смогла сменить workspace."


def _workspace_next() -> str:
    ok, _ = _sh(["hyprctl", "dispatch", "workspace", "+1"])
    return "Следующий workspace." if ok else "Не смогла сменить workspace."


def _workspace_prev() -> str:
    ok, _ = _sh(["hyprctl", "dispatch", "workspace", "-1"])
    return "Предыдущий workspace." if ok else "Не смогла сменить workspace."


def _telegram_stats() -> str:
    inbox = PROJECT / "hoshi-core" / "data" / "tg_inbox"
    outbox = PROJECT / "hoshi-core" / "data" / "tg_outbox"
    ni = len(list(inbox.glob("*"))) if inbox.exists() else 0
    no = len(list(outbox.glob("*"))) if outbox.exists() else 0
    return f"Telegram: входящих {ni}, исходящих {no}."


def _telegram_inbox() -> str:
    inbox = PROJECT / "hoshi-core" / "data" / "tg_inbox"
    if not inbox.exists():
        return "Входящих сообщений нет."
    try:
        files = sorted(inbox.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)[
            :5
        ]
    except Exception:
        files = []
    if not files:
        return "Входящих сообщений нет."
    return "Последние входящие: " + ", ".join(p.name for p in files)


def _control_enable() -> str:
    try:
        from computer_control import enable

        return enable(reason="command")
    except Exception as e:
        return f"Не смогла включить управление: {e}"


def _control_disable() -> str:
    try:
        from computer_control import disable

        return disable()
    except Exception as e:
        return f"Не смогла выключить управление: {e}"


def _control_status() -> str:
    try:
        from computer_control import status_block

        return status_block()
    except Exception as e:
        return f"Статус недоступен: {e}"


def _control_click() -> str:
    try:
        from computer_control import click

        return click("left")
    except Exception as e:
        return f"Не смогла кликнуть: {e}"


def _control_scroll() -> str:
    try:
        from computer_control import scroll

        return scroll(-3)
    except Exception as e:
        return f"Не смогла проскроллить: {e}"


def _control_key(key: str) -> str:
    try:
        from computer_control import key_tap

        return key_tap(str(key or ""))
    except Exception as e:
        return f"Не смогла нажать клавишу: {e}"


def _control_type(text: str) -> str:
    try:
        from computer_control import enter_text

        return "ok" if enter_text(str(text or "")) else "Не смогла ввести текст."
    except Exception as e:
        return f"Не смогла ввести текст: {e}"


def _control_mouse(x: str, y: str) -> str:
    try:
        from computer_control import mouse_move_to

        return mouse_move_to(int(x), int(y))
    except Exception as e:
        return f"Не смогла подвинуть мышь: {e}"


_INTERNAL: dict[str, object] = {
    "_open_app": _open_app,
    "_close_app": _close_app,
    "_launch_game": _launch_game,
    "_close_game": _close_game,
    "_screenshot": _screenshot,
    "_lock_screen": _lock_screen,
    "_sleep_system": _sleep_system,
    "_volume_up": _volume_up,
    "_volume_down": _volume_down,
    "_set_volume": _set_volume,
    "_mute": _mute,
    "_brightness_up": _brightness_up,
    "_brightness_down": _brightness_down,
    "_set_brightness": _set_brightness,
    "_open_file": _open_file,
    "_open_folder": _open_folder,
    "_open_home": _open_home,
    "_open_downloads": _open_downloads,
    "_open_browser": _open_browser,
    "_focus_browser": _focus_browser,
    "_browser_new_tab": _browser_new_tab,
    "_browser_search": _browser_search,
    "_goto_workspace": _goto_workspace,
    "_workspace_next": _workspace_next,
    "_workspace_prev": _workspace_prev,
    "_telegram_stats": _telegram_stats,
    "_telegram_inbox": _telegram_inbox,
    "_control_enable": _control_enable,
    "_control_disable": _control_disable,
    "_control_status": _control_status,
    "_control_click": _control_click,
    "_control_scroll": _control_scroll,
    "_control_key": _control_key,
    "_control_type": _control_type,
    "_control_mouse": _control_mouse,
}


# ── builtin families ────────────────────────────────────────────────────────
# Each family: {id, category, patterns, params, actions, description, sample}.
# `sample` is a concrete input that matches patterns[0] (used by smoke tests).
# `actions` are ordered action specs: {"handle": text} → actions.handle_action(),
# {"fn": name, "args": {...}} → actions/computer_control/internal function.
BUILTIN_FAMILIES: list[dict] = [
    # ── wallpapers ──────────────────────────────────────────────────────────
    {
        "id": "wallpaper_monitor",
        "category": "wallpapers",
        "patterns": [
            r"поставь обои (?P<theme>.+) на (?P<monitor>\d+)-й монитор",
            r"обои (?P<theme>.+) на (?P<monitor>\d+) монитор",
        ],
        "params": {"theme": "theme", "monitor": "monitor"},
        "actions": [{"handle": "поставь обои {theme} на {monitor}-й монитор"}],
        "description": "Поставить обои на конкретный монитор.",
        "sample": "поставь обои anime на 1-й монитор",
    },
    {
        "id": "wallpaper_dual",
        "category": "wallpapers",
        "patterns": [
            r"поставь разные обои (?P<theme>.+)",
            r"разные обои (?P<theme>.+)",
        ],
        "params": {"theme": "theme"},
        "actions": [{"handle": "поставь разные обои {theme}"}],
        "description": "Разные обои на каждый монитор.",
        "sample": "поставь разные обои anime",
    },
    {
        "id": "wallpaper_wallhaven",
        "category": "wallpapers",
        "patterns": [
            r"поставь обои (?P<theme>.+) с wallhaven",
            r"wallhaven (?P<theme>.+)",
        ],
        "params": {"theme": "theme"},
        "actions": [{"handle": "поставь обои {theme} с wallhaven"}],
        "description": "Обои из wallhaven.",
        "sample": "поставь обои anime с wallhaven",
    },
    {
        "id": "wallpaper_pinterest",
        "category": "wallpapers",
        "patterns": [
            r"поставь обои (?P<theme>.+) с pinterest",
            r"pinterest (?P<theme>.+)",
        ],
        "params": {"theme": "theme"},
        "actions": [{"handle": "поставь обои {theme} с pinterest"}],
        "description": "Обои из pinterest.",
        "sample": "поставь обои anime с pinterest",
    },
    {
        "id": "wallpaper_bing",
        "category": "wallpapers",
        "patterns": [
            r"поставь обои (?P<theme>.+) с bing",
            r"bing (?P<theme>.+)",
        ],
        "params": {"theme": "theme"},
        "actions": [{"handle": "поставь обои {theme} с bing"}],
        "description": "Обои через Bing Images.",
        "sample": "поставь обои anime с bing",
    },
    {
        "id": "wallpaper_source",
        "category": "wallpapers",
        "patterns": [
            r"поставь обои (?P<theme>.+) с (?P<source>\w+)",
        ],
        "params": {"theme": "theme", "source": "source"},
        "actions": [{"handle": "поставь обои {theme} с {source}"}],
        "description": "Обои с указанным источником.",
        "sample": "поставь обои anime с unsplash",
    },
    {
        "id": "wallpaper_count",
        "category": "wallpapers",
        "patterns": [
            r"найди (?P<count>\d+) обоев (?P<theme>.+)",
        ],
        "params": {"count": "count", "theme": "theme"},
        "actions": [{"handle": "поставь обои {theme}"}],
        "description": "Найти несколько обоев по теме.",
        "sample": "найди 3 обоев anime",
    },
    {
        "id": "wallpaper_theme",
        "category": "wallpapers",
        "patterns": [
            r"поставь обои (?P<theme>.+)",
            r"смени обои на (?P<theme>.+)",
            r"обои (?P<theme>.+)",
            r"поставь на фон (?P<theme>.+)",
        ],
        "params": {"theme": "theme"},
        "actions": [{"handle": "поставь обои {theme}"}],
        "description": "Поставить обои по теме.",
        "sample": "поставь обои anime",
    },
    {
        "id": "wallpaper_again",
        "category": "wallpapers",
        "patterns": [
            r"ещё обои",
            r"другие обои",
            r"смени обои",
            r"ещё раз",
        ],
        "params": {},
        "actions": [{"handle": "ещё обои"}],
        "description": "Повторить/сменить обои.",
        "sample": "ещё обои",
    },
    {
        "id": "wallpaper_favorite",
        "category": "wallpapers",
        "patterns": [
            r"поставь любимые обои",
            r"любимые обои",
        ],
        "params": {},
        "actions": [{"handle": "поставь любимые обои"}],
        "description": "Поставить любимые обои из настроек.",
        "sample": "поставь любимые обои",
    },
    # ── music ───────────────────────────────────────────────────────────────
    {
        "id": "music_play",
        "category": "music",
        "patterns": [
            r"включи музыку (?P<query>.+)",
            r"поставь песню (?P<query>.+)",
            r"play (?P<query>.+)",
        ],
        "params": {"query": "query"},
        "actions": [{"handle": "включи музыку {query}"}],
        "description": "Включить музыку по запросу (yt-dlp + mpv).",
        "sample": "включи музыку lofi hip hop",
    },
    {
        "id": "music_play_fav",
        "category": "music",
        "patterns": [
            r"включи любимую песню",
            r"поставь любимый трек",
        ],
        "params": {},
        "actions": [{"handle": "включи любимую песню"}],
        "description": "Включить любимую песню.",
        "sample": "включи любимую песню",
    },
    {
        "id": "music_stop",
        "category": "music",
        "patterns": [
            r"выключи музыку",
            r"останови музыку",
            r"стоп музыка",
        ],
        "params": {},
        "actions": [{"handle": "выключи музыку"}],
        "description": "Остановить музыку.",
        "sample": "выключи музыку",
    },
    {
        "id": "music_pause",
        "category": "music",
        "patterns": [
            r"поставь на паузу",
            r"пауза",
        ],
        "params": {},
        "actions": [{"fn": "music_control", "args": {"action": "pause"}}],
        "description": "Пауза через playerctl.",
        "sample": "поставь на паузу",
    },
    {
        "id": "music_resume",
        "category": "music",
        "patterns": [
            r"продолжи музыку",
            r"продолжай играть",
        ],
        "params": {},
        "actions": [{"fn": "music_control", "args": {"action": "resume"}}],
        "description": "Продолжить воспроизведение.",
        "sample": "продолжи музыку",
    },
    {
        "id": "music_next",
        "category": "music",
        "patterns": [
            r"следующий трек",
            r"следующая песня",
            r"next",
        ],
        "params": {},
        "actions": [{"fn": "music_control", "args": {"action": "next"}}],
        "description": "Следующий трек.",
        "sample": "следующий трек",
    },
    {
        "id": "music_prev",
        "category": "music",
        "patterns": [
            r"предыдущий трек",
            r"предыдущая песня",
        ],
        "params": {},
        "actions": [{"fn": "music_control", "args": {"action": "prev"}}],
        "description": "Предыдущий трек.",
        "sample": "предыдущий трек",
    },
    {
        "id": "music_other",
        "category": "music",
        "patterns": [
            r"включи другую музыку",
            r"смени музыку",
        ],
        "params": {},
        "actions": [{"fn": "play_other_music"}],
        "description": "Другая музыка по вкусу Юны.",
        "sample": "включи другую музыку",
    },
    {
        "id": "music_yuna_pick",
        "category": "music",
        "patterns": [
            r"включи что-нибудь на свой вкус",
            r"поставь что-нибудь своё",
        ],
        "params": {},
        "actions": [{"fn": "play_yuna_pick"}],
        "description": "Музыка на вкус Юны.",
        "sample": "включи что-нибудь на свой вкус",
    },
    {
        "id": "music_now_playing",
        "category": "music",
        "patterns": [
            r"что сейчас играет",
            r"что я слушаю",
            r"какая песня играет",
        ],
        "params": {},
        "actions": [{"fn": "now_playing"}],
        "description": "Что сейчас играет.",
        "sample": "что сейчас играет",
    },
    {
        "id": "music_set_favorite",
        "category": "music",
        "patterns": [
            r"запомни любимую песню (?P<query>.+)",
            r"это моя любимая песня (?P<query>.+)",
        ],
        "params": {"query": "query"},
        "actions": [{"fn": "set_favorite_music", "args": {"query": "{query}"}}],
        "description": "Сохранить любимый трек.",
        "sample": "запомни любимую песню lofi",
    },
    {
        "id": "music_ytdlp",
        "category": "music",
        "patterns": [
            r"включи (?P<query>.+) с ютуба",
            r"скачай звук (?P<query>.+)",
        ],
        "params": {"query": "query"},
        "actions": [{"handle": "включи {query} с ютуба"}],
        "description": "Включить звук с YouTube через yt-dlp.",
        "sample": "включи lofi с ютуба",
    },
    {
        "id": "music_volume_player",
        "category": "music",
        "patterns": [
            r"сделай громче в (?P<player>\w+)",
            r"громче в (?P<player>\w+)",
        ],
        "params": {"player": "player"},
        "actions": [{"fn": "music_control", "args": {"action": "resume"}}],
        "description": "Громкость в конкретном плеере.",
        "sample": "сделай громче в spotify",
    },
    # ── youtube ─────────────────────────────────────────────────────────────
    {
        "id": "youtube_open",
        "category": "youtube",
        "patterns": [
            r"открой ютуб",
            r"открой youtube",
        ],
        "params": {},
        "actions": [{"handle": "открой ютуб"}],
        "description": "Открыть YouTube в браузере.",
        "sample": "открой ютуб",
    },
    {
        "id": "youtube_search",
        "category": "youtube",
        "patterns": [
            r"найди (?P<query>.+) на ютубе",
            r"ютуб (?P<query>.+)",
        ],
        "params": {"query": "query"},
        "actions": [{"handle": "найди {query} на ютубе"}],
        "description": "Поиск на YouTube.",
        "sample": "найди lofi на ютубе",
    },
    {
        "id": "youtube_music",
        "category": "youtube",
        "patterns": [
            r"включи (?P<query>.+) на ютубе",
        ],
        "params": {"query": "query"},
        "actions": [{"handle": "включи {query} на ютубе"}],
        "description": "Включить звук с YouTube.",
        "sample": "включи lofi на ютубе",
    },
    # ── workspaces ──────────────────────────────────────────────────────────
    {
        "id": "workspace_goto",
        "category": "workspaces",
        "patterns": [
            r"перейди на (?P<workspace>\d+) рабочий стол",
            r"workspace (?P<workspace>\d+)",
            r"на (?P<workspace>\d+) окно",
        ],
        "params": {"workspace": "workspace"},
        "actions": [{"fn": "_goto_workspace", "args": {"n": "{workspace}"}}],
        "description": "Перейти на workspace (Hyprland).",
        "sample": "перейди на 3 рабочий стол",
    },
    {
        "id": "workspace_next",
        "category": "workspaces",
        "patterns": [
            r"следующий рабочий стол",
            r"следующий workspace",
        ],
        "params": {},
        "actions": [{"fn": "_workspace_next"}],
        "description": "Следующий workspace.",
        "sample": "следующий рабочий стол",
    },
    {
        "id": "workspace_prev",
        "category": "workspaces",
        "patterns": [
            r"предыдущий рабочий стол",
        ],
        "params": {},
        "actions": [{"fn": "_workspace_prev"}],
        "description": "Предыдущий workspace.",
        "sample": "предыдущий рабочий стол",
    },
    # ── urls ────────────────────────────────────────────────────────────────
    {
        "id": "open_url",
        "category": "urls",
        "patterns": [
            r"открой сайт (?P<url>.+)",
            r"перейди на (?P<url>.+)",
            r"открой ссылку (?P<url>.+)",
        ],
        "params": {"url": "url"},
        "actions": [{"fn": "open_url", "args": {"url": "{url}"}}],
        "description": "Открыть URL через xdg-open.",
        "sample": "открой сайт example.com",
    },
    # ── browser ─────────────────────────────────────────────────────────────
    {
        "id": "browser_open",
        "category": "browser",
        "patterns": [
            r"открой браузер",
            r"запусти браузер",
        ],
        "params": {},
        "actions": [{"fn": "_open_browser"}],
        "description": "Открыть браузер.",
        "sample": "открой браузер",
    },
    {
        "id": "browser_focus",
        "category": "browser",
        "patterns": [
            r"сфокусируй браузер",
            r"переключись на браузер",
        ],
        "params": {},
        "actions": [{"fn": "_focus_browser"}],
        "description": "Сфокусировать окно браузера.",
        "sample": "сфокусируй браузер",
    },
    {
        "id": "browser_new_tab",
        "category": "browser",
        "patterns": [
            r"открой новую вкладку",
        ],
        "params": {},
        "actions": [{"fn": "_browser_new_tab"}],
        "description": "Новая вкладка браузера.",
        "sample": "открой новую вкладку",
    },
    {
        "id": "browser_search",
        "category": "browser",
        "patterns": [
            r"поищи (?P<query>.+) в браузере",
            r"загугли (?P<query>.+)",
        ],
        "params": {"query": "query"},
        "actions": [{"fn": "_browser_search", "args": {"q": "{query}"}}],
        "description": "Поиск в браузере.",
        "sample": "поищи lofi в браузере",
    },
    # ── files ───────────────────────────────────────────────────────────────
    {
        "id": "file_open",
        "category": "files",
        "patterns": [
            r"открой файл (?P<file>.+)",
            r"покажи файл (?P<file>.+)",
        ],
        "params": {"file": "file"},
        "actions": [{"fn": "_open_file", "args": {"path": "{file}"}}],
        "description": "Открыть файл.",
        "sample": "открой файл report.pdf",
    },
    {
        "id": "folder_open",
        "category": "files",
        "patterns": [
            r"открой папку (?P<folder>.+)",
            r"покажи папку (?P<folder>.+)",
        ],
        "params": {"folder": "folder"},
        "actions": [{"fn": "_open_folder", "args": {"path": "{folder}"}}],
        "description": "Открыть папку.",
        "sample": "открой папку Projects",
    },
    {
        "id": "home_open",
        "category": "files",
        "patterns": [
            r"открой домашнюю папку",
            r"открой файлы",
        ],
        "params": {},
        "actions": [{"fn": "_open_home"}],
        "description": "Открыть домашнюю папку.",
        "sample": "открой домашнюю папку",
    },
    {
        "id": "downloads_open",
        "category": "files",
        "patterns": [
            r"открой загрузки",
            r"покажи загрузки",
        ],
        "params": {},
        "actions": [{"fn": "_open_downloads"}],
        "description": "Открыть папку загрузок.",
        "sample": "открой загрузки",
    },
    # ── games ───────────────────────────────────────────────────────────────
    {
        "id": "game_play",
        "category": "games",
        "patterns": [
            r"запусти игру (?P<game>\w+)",
            r"поиграем в (?P<game>\w+)",
            r"открой игру (?P<game>\w+)",
            r"включи игру (?P<game>\w+)",
        ],
        "params": {"game": "game"},
        "actions": [{"fn": "_launch_game", "args": {"game": "{game}"}}],
        "description": "Запустить игру.",
        "sample": "запусти игру minecraft",
    },
    {
        "id": "game_stop",
        "category": "games",
        "patterns": [
            r"закрой игру",
            r"выйди из игры",
        ],
        "params": {},
        "actions": [{"fn": "_close_game"}],
        "description": "Закрыть игру.",
        "sample": "закрой игру",
    },
    # ── apps ────────────────────────────────────────────────────────────────
    {
        "id": "app_focus",
        "category": "apps",
        "patterns": [
            r"переключись на (?P<app>\w+)",
            r"открой окно (?P<app>\w+)",
        ],
        "params": {"app": "app"},
        "actions": [{"fn": "_open_app", "args": {"app": "{app}"}}],
        "description": "Переключиться на приложение.",
        "sample": "переключись на firefox",
    },
    {
        "id": "app_open",
        "category": "apps",
        "patterns": [
            r"открой (?P<app>\w+)",
            r"запусти (?P<app>\w+)",
            r"запусти приложение (?P<app>\w+)",
            r"открой программу (?P<app>\w+)",
        ],
        "params": {"app": "app"},
        "actions": [{"fn": "_open_app", "args": {"app": "{app}"}}],
        "description": "Открыть/запустить приложение.",
        "sample": "открой firefox",
    },
    {
        "id": "app_close",
        "category": "apps",
        "patterns": [
            r"закрой (?P<app>\w+)",
            r"выключи приложение (?P<app>\w+)",
        ],
        "params": {"app": "app"},
        "actions": [{"fn": "_close_app", "args": {"app": "{app}"}}],
        "description": "Закрыть приложение.",
        "sample": "закрой firefox",
    },
    # ── system ──────────────────────────────────────────────────────────────
    {
        "id": "screenshot",
        "category": "system",
        "patterns": [
            r"сделай скриншот",
            r"скриншот",
            r"сфотографируй экран",
        ],
        "params": {},
        "actions": [{"fn": "_screenshot"}],
        "description": "Скриншот экрана (grim).",
        "sample": "сделай скриншот",
    },
    {
        "id": "lock",
        "category": "system",
        "patterns": [
            r"заблокируй экран",
            r"заблокируй комп",
            r"lock",
        ],
        "params": {},
        "actions": [{"fn": "_lock_screen"}],
        "description": "Заблокировать экран.",
        "sample": "заблокируй экран",
    },
    {
        "id": "sleep",
        "category": "system",
        "patterns": [
            r"усыпи комп",
            r"переведи в сон",
            r"sleep",
        ],
        "params": {},
        "actions": [{"fn": "_sleep_system"}],
        "description": "Усыпить систему.",
        "sample": "усыпи комп",
    },
    {
        "id": "volume_up",
        "category": "system",
        "patterns": [
            r"сделай громче",
            r"прибавь звук",
            r"громче",
        ],
        "params": {},
        "actions": [{"fn": "_volume_up"}],
        "description": "Громкость выше.",
        "sample": "сделай громче",
    },
    {
        "id": "volume_down",
        "category": "system",
        "patterns": [
            r"сделай тише",
            r"убавь звук",
            r"тише",
        ],
        "params": {},
        "actions": [{"fn": "_volume_down"}],
        "description": "Громкость ниже.",
        "sample": "сделай тише",
    },
    {
        "id": "volume_set",
        "category": "system",
        "patterns": [
            r"сделай громкость (?P<volume>\d+)",
            r"громкость (?P<volume>\d+) процентов",
        ],
        "params": {"volume": "volume"},
        "actions": [{"fn": "_set_volume", "args": {"pct": "{volume}"}}],
        "description": "Установить громкость.",
        "sample": "сделай громкость 50",
    },
    {
        "id": "volume_mute",
        "category": "system",
        "patterns": [
            r"выключи звук",
            r"замьють",
            r"mute",
        ],
        "params": {},
        "actions": [{"fn": "_mute"}],
        "description": "Переключить mute.",
        "sample": "выключи звук",
    },
    {
        "id": "brightness_up",
        "category": "system",
        "patterns": [
            r"ярче",
            r"прибавь яркость",
        ],
        "params": {},
        "actions": [{"fn": "_brightness_up"}],
        "description": "Яркость выше.",
        "sample": "ярче",
    },
    {
        "id": "brightness_down",
        "category": "system",
        "patterns": [
            r"темнее",
            r"убавь яркость",
        ],
        "params": {},
        "actions": [{"fn": "_brightness_down"}],
        "description": "Яркость ниже.",
        "sample": "темнее",
    },
    {
        "id": "brightness_set",
        "category": "system",
        "patterns": [
            r"яркость (?P<brightness>\d+)",
            r"сделай яркость (?P<brightness>\d+)",
        ],
        "params": {"brightness": "brightness"},
        "actions": [{"fn": "_set_brightness", "args": {"pct": "{brightness}"}}],
        "description": "Установить яркость.",
        "sample": "яркость 50",
    },
    # ── telegram ────────────────────────────────────────────────────────────
    {
        "id": "tg_stats",
        "category": "telegram",
        "patterns": [
            r"сколько сообщений в телеграме",
            r"статистика телеграма",
            r"tg stats",
        ],
        "params": {},
        "actions": [{"fn": "_telegram_stats"}],
        "description": "Статистика Telegram-очередей.",
        "sample": "сколько сообщений в телеграме",
    },
    {
        "id": "tg_inbox",
        "category": "telegram",
        "patterns": [
            r"что нового в телеграме",
            r"проверь телеграм",
        ],
        "params": {},
        "actions": [{"fn": "_telegram_inbox"}],
        "description": "Последние входящие Telegram.",
        "sample": "что нового в телеграме",
    },
    # ── control ─────────────────────────────────────────────────────────────
    {
        "id": "control_enable",
        "category": "control",
        "patterns": [
            r"управляй моим компьютером",
            r"возьми управление",
            r"играй за меня",
        ],
        "params": {},
        "actions": [{"fn": "_control_enable"}],
        "description": "Включить управление ПК.",
        "sample": "управляй моим компьютером",
    },
    {
        "id": "control_disable",
        "category": "control",
        "patterns": [
            r"хватит управлять",
            r"отдай управление",
            r"стоп управление",
        ],
        "params": {},
        "actions": [{"fn": "_control_disable"}],
        "description": "Выключить управление ПК.",
        "sample": "хватит управлять",
    },
    {
        "id": "control_status",
        "category": "control",
        "patterns": [
            r"статус управления",
            r"ты управляешь",
        ],
        "params": {},
        "actions": [{"fn": "_control_status"}],
        "description": "Статус управления ПК.",
        "sample": "статус управления",
    },
    {
        "id": "control_click",
        "category": "control",
        "patterns": [
            r"кликни",
            r"кликни левой кнопкой",
        ],
        "params": {},
        "actions": [{"fn": "_control_click"}],
        "description": "Клик левой кнопкой.",
        "sample": "кликни",
    },
    {
        "id": "control_scroll",
        "category": "control",
        "patterns": [
            r"проскролль вниз",
            r"прокрути вниз",
        ],
        "params": {},
        "actions": [{"fn": "_control_scroll"}],
        "description": "Скролл вниз.",
        "sample": "проскролль вниз",
    },
    {
        "id": "control_key",
        "category": "control",
        "patterns": [
            r"нажми (?P<key>\w+)",
            r"нажми клавишу (?P<key>\w+)",
        ],
        "params": {"key": "key"},
        "actions": [{"fn": "_control_key", "args": {"key": "{key}"}}],
        "description": "Нажать клавишу.",
        "sample": "нажми enter",
    },
    {
        "id": "control_type",
        "category": "control",
        "patterns": [
            r"набери (?P<text>.+)",
            r"напиши (?P<text>.+) на клавиатуре",
        ],
        "params": {"text": "text"},
        "actions": [{"fn": "_control_type", "args": {"text": "{text}"}}],
        "description": "Ввести текст.",
        "sample": "набери hello world",
    },
    {
        "id": "control_mouse",
        "category": "control",
        "patterns": [
            r"подвинь мышь в (?P<x>\d+) (?P<y>\d+)",
        ],
        "params": {"x": "x", "y": "y"},
        "actions": [{"fn": "_control_mouse", "args": {"x": "{x}", "y": "{y}"}}],
        "description": "Плавно подвинуть мышь.",
        "sample": "подвинь мышь в 100 200",
    },
    # ── general ─────────────────────────────────────────────────────────────
    {
        "id": "smart_off",
        "category": "general",
        "patterns": [
            r"отключи",
            r"стоп",
            r"замолчи",
        ],
        "params": {},
        "actions": [{"handle": "отключи"}],
        "description": "Умное «отключи» — музыка, не выключение ПК.",
        "sample": "отключи",
    },
    {
        "id": "stop_all",
        "category": "general",
        "patterns": [
            r"тихо",
            r"замолчи и выключи музыку",
        ],
        "params": {},
        "actions": [{"fn": "stop_all"}],
        "description": "Остановить речь и музыку.",
        "sample": "тихо",
    },
    {
        "id": "repeat_last",
        "category": "general",
        "patterns": [
            r"давай ещё",
            r"ещё раз",
            r"повтори",
        ],
        "params": {},
        "actions": [{"handle": "давай ещё"}],
        "description": "Повторить последнее действие.",
        "sample": "давай ещё",
    },
]


# ── custom commands (hoshi-core/data/commands/custom.json) ──────────────────
_custom_cache: tuple[float, list[dict]] | None = None


def load_custom_commands() -> list[dict]:
    """Load custom commands. Missing/corrupt file → [] (never raises)."""
    global _custom_cache
    try:
        mtime = CUSTOM_PATH.stat().st_mtime
    except OSError:
        _custom_cache = None
        return []
    if _custom_cache is not None and _custom_cache[0] == mtime:
        return _custom_cache[1]
    try:
        data = json.loads(CUSTOM_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            cmds = data
        elif isinstance(data, dict):
            cmds = data.get("commands") or []
        else:
            cmds = []
        cmds = [c for c in cmds if isinstance(c, dict)]
    except Exception:
        cmds = []
    _custom_cache = (mtime, cmds)
    return cmds


def save_custom_commands(commands: list[dict]) -> None:
    """Persist custom commands to custom.json (creates dir/file if needed)."""
    global _custom_cache
    CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
    CUSTOM_PATH.write_text(
        json.dumps(list(commands), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _custom_cache = None


def add_custom_command(cmd: dict) -> dict:
    """Add or replace a custom command by name. Returns {ok, name|error}."""
    name = str(cmd.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    cmds = [c for c in load_custom_commands() if str(c.get("name") or "") != name]
    cmds.append(cmd)
    save_custom_commands(cmds)
    return {"ok": True, "name": name}


def remove_custom_command(name: str) -> bool:
    """Remove a custom command by name. Returns True if removed."""
    cmds = load_custom_commands()
    before = len(cmds)
    cmds = [c for c in cmds if str(c.get("name") or "") != name]
    if len(cmds) == before:
        return False
    save_custom_commands(cmds)
    return True


# ── dispatch ────────────────────────────────────────────────────────────────
def _render(template: str, params: dict) -> str:
    def repl(m: re.Match) -> str:
        name = m.group(1)
        return str(params.get(name, m.group(0)))

    return re.sub(r"\{(\w+)\}", repl, template)


def dispatch(text: str) -> dict | None:
    """Match text against custom + builtin patterns.

    Returns {command, actions, params, stop_on_error, source} or None.
    Never raises: nonsense input → None.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    t = text.strip()

    # custom commands first
    for cmd in load_custom_commands():
        if not cmd.get("enabled", True):
            continue
        for pat in cmd.get("patterns") or []:
            try:
                m = re.search(pat, t, re.I)
            except re.error:
                continue
            if m:
                params = {k: v for k, v in m.groupdict().items() if v is not None}
                return {
                    "command": str(cmd.get("name") or "custom"),
                    "actions": cmd.get("actions") or [],
                    "params": params,
                    "stop_on_error": bool(cmd.get("stop_on_error", True)),
                    "source": "custom",
                }

    # builtin families
    for fam in BUILTIN_FAMILIES:
        for pat in fam["patterns"]:
            try:
                m = re.search(pat, t, re.I)
            except re.error:
                continue
            if m:
                params = {k: v for k, v in m.groupdict().items() if v is not None}
                return {
                    "command": fam["id"],
                    "actions": fam["actions"],
                    "params": params,
                    "stop_on_error": bool(fam.get("stop_on_error", True)),
                    "source": "builtin",
                    "family": fam["id"],
                    "category": fam["category"],
                }
    return None


# ── execute ─────────────────────────────────────────────────────────────────
_actions_mod = None


def _get_actions():
    global _actions_mod
    if _actions_mod is None:
        import actions

        _actions_mod = actions
    return _actions_mod


def _resolve_fn(fn: str):
    if "." in fn:
        mod_name, _, func = fn.partition(".")
        mod = {"actions": _get_actions(), "computer_control": None}.get(mod_name)
        if mod_name == "computer_control":
            try:
                import computer_control

                mod = computer_control
            except Exception:
                mod = None
        if mod is None:
            return None
        return getattr(mod, func, None)
    if fn in _INTERNAL:
        return _INTERNAL[fn]
    return getattr(_get_actions(), fn, None)


def _run_action(spec: dict, params: dict) -> dict:
    """Run one action spec → {action, ok, output}. Never raises."""
    try:
        if "handle" in spec:
            text = _render(str(spec["handle"]), params)
            out = _get_actions().handle_action(text)
            if out is None:
                return {"action": spec, "ok": False, "output": "no match"}
            return {"action": spec, "ok": True, "output": str(out)}
        if "fn" in spec:
            fn = _resolve_fn(str(spec["fn"]))
            if fn is None:
                return {
                    "action": spec,
                    "ok": False,
                    "output": f"unknown function: {spec['fn']}",
                }
            args = {}
            for k, v in (spec.get("args") or {}).items():
                args[k] = _render(str(v), params)
            out = fn(**args)
            return {"action": spec, "ok": True, "output": str(out)}
        if "error" in spec:
            return {"action": spec, "ok": False, "output": str(spec["error"])}
        return {"action": spec, "ok": False, "output": "invalid action spec"}
    except Exception as e:
        return {"action": spec, "ok": False, "output": f"{type(e).__name__}: {e}"}


def execute(command: dict) -> list[dict]:
    """Run a dispatched command's actions in order.

    Returns results[] of {action, ok, output}. With stop_on_error=True (default)
    the chain stops at the first failed action.
    """
    if not isinstance(command, dict):
        return []
    actions = command.get("actions") or []
    stop_on_error = bool(command.get("stop_on_error", True))
    params = command.get("params") or {}
    results: list[dict] = []
    for spec in actions:
        if not isinstance(spec, dict):
            results.append(
                {"action": spec, "ok": False, "output": "invalid action spec"}
            )
            if stop_on_error:
                break
            continue
        r = _run_action(spec, params)
        results.append(r)
        if stop_on_error and not r["ok"]:
            break
    return results


# ── counting ────────────────────────────────────────────────────────────────
def count_commands() -> int:
    """Total concrete builtin + custom command invocations."""
    total = 0
    for fam in BUILTIN_FAMILIES:
        params = fam.get("params") or {}
        for pat in fam["patterns"]:
            names = set(re.findall(r"\(\?P<(\w+)>", pat))
            if not names:
                total += 1
                continue
            n = 1
            for name in names:
                ptype = params.get(name, "query")
                domain = _PARAM_DOMAINS.get(ptype)
                if domain:
                    n *= len(domain)
            total += max(n, 1)
    total += len(load_custom_commands())
    return total


def list_families() -> list[dict]:
    """Return builtin families (for introspection/UI)."""
    return list(BUILTIN_FAMILIES)
