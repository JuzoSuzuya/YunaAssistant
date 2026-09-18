#!/usr/bin/env python3
"""Конфигурация Hoshi daemon."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

DATA = ROOT / "data"
INBOX = DATA / "tg_inbox"
OUTBOX = DATA / "tg_outbox"
HEAVY_INBOX = DATA / "heavy_inbox"
HEAVY_OUTBOX = DATA / "heavy_outbox"
SESSIONS = DATA / "agent_sessions"
USER_SESSIONS = DATA / "user_sessions"
ERRORS = DATA / "errors"
MEDIA_DIR = DATA / "incoming_media"

for d in (DATA, INBOX, OUTBOX, HEAVY_INBOX, HEAVY_OUTBOX, SESSIONS, USER_SESSIONS, ERRORS, MEDIA_DIR):
    d.mkdir(parents=True, exist_ok=True)

BOT_TOKEN = os.environ.get("HOSHI_BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("HOSHI_OWNER_ID", "1477417142"))


def owner_profile_gif_path() -> Path:
    """Анимированная аватарка владельца: avatars/<OWNER_ID>_gif (mp4/gif)."""
    return MEDIA_DIR / "avatars" / f"{OWNER_ID}_gif"


def owner_saved_gif_path(index: int | str = 1) -> Path:
    """Гифка из сохранённого набора Telegram: saved_gifs/<n> или random."""
    tag = str(index).strip() or "1"
    return MEDIA_DIR / "saved_gifs" / tag


def get_bot_id() -> int | None:
    """User id бота из токена (до двоеточия)."""
    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        return None
    try:
        return int(BOT_TOKEN.split(":", 1)[0])
    except ValueError:
        return None
CURSOR_AGENT_BIN = os.environ.get("CURSOR_AGENT_BIN", "cursor-agent")
CURSOR_API_KEY = os.environ.get("CURSOR_API_KEY", "").strip()
CURSOR_LIGHT_MODEL = os.environ.get("HOSHI_CURSOR_LIGHT_MODEL", "auto").strip()
CURSOR_HEAVY_MODEL = os.environ.get("HOSHI_CURSOR_HEAVY_MODEL", "").strip()
CURSOR_LIGHT_MODE = os.environ.get("HOSHI_CURSOR_LIGHT_MODE", "ask").strip().lower()
EXTERNAL_CHAT_CONTEXT_LIGHT = int(os.environ.get("HOSHI_EXTERNAL_CONTEXT_LIGHT", "20"))


def cursor_agent_env() -> dict[str, str]:
    """Окружение для subprocess cursor-agent: HOME, PATH, CURSOR_API_KEY из .env."""
    load_dotenv(ROOT / ".env", override=True)
    env = dict(os.environ)
    home = (env.get("HOME") or str(Path.home())).strip() or str(Path.home())
    env["HOME"] = home
    local_bin = str(Path(home) / ".local" / "bin")
    path = env.get("PATH", "")
    if local_bin not in path.split(":"):
        env["PATH"] = f"{local_bin}:{path}" if path else local_bin
    key = (env.get("CURSOR_API_KEY") or CURSOR_API_KEY or "").strip()
    if key:
        env["CURSOR_API_KEY"] = key
    return env
EXTERNAL_CHAT_CONTEXT_LIMIT = int(os.environ.get("HOSHI_EXTERNAL_CONTEXT", "35"))


def external_context_limit() -> int:
    try:
        from storage import load_settings

        n = int((load_settings().get("memory") or {}).get("external_context_limit") or 0)
        if n > 0:
            return n
    except Exception:
        pass
    return EXTERNAL_CHAT_CONTEXT_LIMIT


def external_context_limit_fast() -> int:
    """Меньший контекст для быстрых диалоговых ответов."""
    try:
        from storage import load_settings

        mem = load_settings().get("memory") or {}
        n = int(mem.get("external_context_light_limit") or 0)
        if n > 0:
            return n
    except Exception:
        pass
    return min(external_context_limit(), EXTERNAL_CHAT_CONTEXT_LIGHT)


def fast_reply_enabled() -> bool:
    try:
        from storage import load_settings

        prefs = load_settings().get("owner_preferences") or {}
        return bool(prefs.get("fast_reply") or prefs.get("bot_fast_reply"))
    except Exception:
        return True
EXTERNAL_CHAT_MAX_SCAN = int(os.environ.get("HOSHI_EXTERNAL_MAX_SCAN", "200"))
MAX_CONCURRENT_TASKS = int(os.environ.get("HOSHI_MAX_WORKERS", "3"))
# Лимит отправки файлов через user-client (MTProto), не Bot API (50 МБ).
USERBOT_MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024
VIDEO_DOWNLOAD_TIMEOUT = int(os.environ.get("HOSHI_VIDEO_TIMEOUT", "1800"))


def get_telegram_api() -> tuple[str, str]:
    """API ID/hash из .env или settings.json (после настройки через бота)."""
    load_dotenv(ROOT / ".env", override=True)
    api_id = os.environ.get("TELEGRAM_API_ID", "").strip()
    api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()
    if api_id and api_hash:
        return api_id, api_hash
    try:
        from storage import load_settings

        ta = load_settings().get("telegram_api", {})
        api_id = api_id or str(ta.get("api_id", "")).strip()
        api_hash = api_hash or str(ta.get("api_hash", "")).strip()
    except Exception:
        pass
    return api_id, api_hash


TELEGRAM_API_ID, TELEGRAM_API_HASH = get_telegram_api()


def get_telegram_2fa_password() -> str:
    load_dotenv(ROOT / ".env", override=True)
    return os.environ.get("TELEGRAM_2FA_PASSWORD", "").strip()

LOG = ROOT / "daemon.log"
LOCK = ROOT / ".daemon.lock"
HEAVY_LOCK = ROOT / ".heavy_worker.lock"
HEAVY_PICK_LOCK = DATA / "heavy_pick.lock"
HOSHI_HEAVY_WORKERS = max(1, int(os.environ.get("HOSHI_HEAVY_WORKERS", "2")))


def heavy_worker_lock_path(worker_id: int) -> Path:
    return ROOT / f".heavy_worker.{worker_id}.lock"

IRIS_LOCK = ROOT / ".iris_daemon.lock"
BRIDGE_LOCK = ROOT / ".bridge.lock"
STATUS = ROOT / "daemon_status.json"
HEAVY_STATUS = ROOT / "heavy_worker_status.json"
IRIS_STATUS = ROOT / "iris_daemon_status.json"
STATE = ROOT / "pipeline_state.json"
SETTINGS = DATA / "settings.json"

AGENT_TRIGGERS = {
    "agent",
    "cursor",
    "curosr",
    "hoshi",
    "хоши",
    "yuna",
    "юна",
    "юно",
    "агент",
    "/agent",
    "/cursor",
    "/curosr",
    "/hoshi",
    "/yuna",
    "/юна",
    "/агент",
}

AGENT_EXIT_TRIGGERS = {
    "exit",
    "/exit",
    "eixt",
    "exti",
    "exitt",
    "выход",
}
