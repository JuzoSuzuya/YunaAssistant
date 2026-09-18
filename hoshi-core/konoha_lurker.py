#!/usr/bin/env python3
"""Фоновые сообщения в Конохе от имени владельца (stealth, без маркеров бота)."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from datetime import datetime, timedelta
from typing import Any

from config import DATA, OWNER_ID
from storage import enqueue_task, load_settings, save_settings

log = logging.getLogger("hoshi.konoha")

STATE_PATH = DATA / "konoha_lurker_state.json"
KONOHA_CHAT_ID = -1002685666919
KONOHA_TITLE = "Konoha"

ENABLE_OWNER_RE = re.compile(
    r"(?:"
    r"конох|konoha"
    r").*(?:"
    r"стил|пал|эмодж|автомат|сама\s+когда|сам\s+когда|самостоят"
    r")|(?:"
    r"стил|пал|эмодж|автомат|сама\s+когда"
    r").*(?:конох|konoha)",
    re.I,
)

HOSHI_MARKERS_RE = re.compile(
    r"🥳|✨\s*Юна|Юна\s+на\s+связи|агент\s+hoshi|секунду…\s*готовлю",
    re.I,
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _cfg() -> dict[str, Any]:
    return load_settings().get("konoha_lurker") or {}


def _load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(data: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def chat_id() -> int:
    try:
        return int(_cfg().get("chat_id") or KONOHA_CHAT_ID)
    except (TypeError, ValueError):
        return KONOHA_CHAT_ID


def is_enabled() -> bool:
    return bool(_cfg().get("enabled"))


def interval_seconds() -> float:
    return max(300.0, float(_cfg().get("check_interval_minutes", 20)) * 60)


def enable(*, chat_id_override: int | None = None) -> None:
    cid = int(chat_id_override or KONOHA_CHAT_ID)
    settings = load_settings()
    settings["konoha_lurker"] = {
        "enabled": True,
        "chat_id": cid,
        "check_interval_minutes": 6,
        "max_per_day": 20,
        "min_gap_minutes": 12,
        "post_chance": 0.58,
        "enabled_at": _now(),
    }
    ext = settings.setdefault("external_chats", {})
    chats = ext.setdefault("chats", {})
    chats[str(cid)] = {
        **chats.get(str(cid), {}),
        "title": KONOHA_TITLE,
        "enabled": True,
        "respond_to_user": False,
        "reply_to_triggers": False,
        "muted": True,
        "stealth_mode": True,
        "proactive": True,
    }
    prefs = ext.setdefault("preferences", {})
    prefs[str(cid)] = {
        **prefs.get(str(cid), {}),
        "owner_style": True,
        "stealth": True,
        "no_greeting": True,
        "no_internal": True,
        "concise": True,
    }
    save_settings(settings)
    log.info("konoha stealth enabled chat=%s", cid)


def disable() -> None:
    settings = load_settings()
    cfg = settings.setdefault("konoha_lurker", {})
    cfg["enabled"] = False
    save_settings(settings)


def enable_from_owner_text(text: str) -> str | None:
    if not ENABLE_OWNER_RE.search(text or ""):
        return None
    from chat_router import is_chat_write_forbidden

    if is_chat_write_forbidden(KONOHA_CHAT_ID):
        return None
    enable()
    return (
        "Ок — в **Конохе** буду иногда писать **твоим стилем**: без 🥳 и «Юна на связи», "
        "коротко и по теме. Реактивно на hoshi/юна там **не отвечаю** — только фоновые реплики, "
        "когда уместно. Данные не сливаю, отказы — без «мне запрещено»."
    )


def _can_post_today(state: dict[str, Any]) -> bool:
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("day") != today:
        return True
    return int(state.get("posts_today") or 0) < int(_cfg().get("max_per_day") or 4)


def _msg_datetime(m: dict[str, Any]) -> datetime | None:
    dt_raw = m.get("date") or ""
    try:
        dt = datetime.fromisoformat(dt_raw.replace("Z", "+00:00"))
        if dt.tzinfo:
            dt = dt.replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _min_gap_ok(state: dict[str, Any], *, recent_count: int = 0) -> bool:
    last = state.get("last_post_at")
    if not last:
        return True
    try:
        base = float(_cfg().get("min_gap_minutes") or 12)
        if recent_count >= 8:
            base = max(5.0, base * 0.45)
        elif recent_count >= 5:
            base = max(8.0, base * 0.65)
        gap = timedelta(minutes=base)
        return datetime.now() - datetime.fromisoformat(last) >= gap
    except Exception:
        return True


def _recent_owner_minutes(messages: list[dict[str, Any]], *, within: int = 5) -> bool:
    cutoff = datetime.now() - timedelta(minutes=within)
    for m in messages[:8]:
        if not m.get("out"):
            continue
        dt = _msg_datetime(m)
        if dt and dt >= cutoff:
            return True
    return False


def _chat_activity(messages: list[dict[str, Any]]) -> tuple[bool, int, int]:
    """(активен, сообщений за 2ч, сообщений за 30м)"""
    cutoff_2h = datetime.now() - timedelta(hours=2)
    cutoff_30m = datetime.now() - timedelta(minutes=30)
    recent_2h = 0
    recent_30m = 0
    for m in messages[:20]:
        text = (m.get("text") or "").strip()
        if not text:
            continue
        dt = _msg_datetime(m)
        if not dt:
            recent_2h += 1
            continue
        if dt >= cutoff_2h:
            recent_2h += 1
        if dt >= cutoff_30m:
            recent_30m += 1
    active = recent_30m >= 2 or recent_2h >= 3
    return active, recent_2h, recent_30m


def _post_chance(recent_2h: int, recent_30m: int) -> float:
    base = float(_cfg().get("post_chance") or 0.58)
    if recent_30m >= 4:
        return min(0.85, base + 0.25)
    if recent_2h >= 6:
        return min(0.75, base + 0.15)
    if recent_2h >= 3:
        return min(0.7, base + 0.08)
    return base


def _pick_reply_target(messages: list[dict[str, Any]]) -> tuple[int | None, str]:
    """Чужое сообщение, куда уместно вписаться реплаем (не обязательно последнее)."""
    candidates: list[tuple[int, int, str]] = []
    for i, m in enumerate(messages):
        if m.get("out"):
            continue
        text = (m.get("text") or "").strip()
        if not text or text.startswith("/") or len(text) < 2:
            continue
        if i + 1 < len(messages) and messages[i + 1].get("out"):
            continue

        score = 0
        dt = _msg_datetime(m)
        if dt:
            age_min = (datetime.now() - dt).total_seconds() / 60
            if age_min <= 20:
                score += 45
            elif age_min <= 60:
                score += 30
            elif age_min <= 120:
                score += 15
            elif age_min > 240:
                score -= 25
        else:
            score += 10

        if "?" in text:
            score += 18
        low = text.lower()
        if low.startswith(("а ", "ну ", "кст", "блин", "ого", "жесть")):
            score += 10
        if len(text) >= 12:
            score += 6
        score += i

        candidates.append((score, int(m["id"]), text[:200]))

    if not candidates:
        return None, ""
    candidates.sort(key=lambda x: x[0], reverse=True)
    _, mid, preview = candidates[0]
    return mid, preview


def _looks_like_bot_spam(messages: list[dict[str, Any]]) -> bool:
    for m in messages[:6]:
        if m.get("out") and HOSHI_MARKERS_RE.search(m.get("text") or ""):
            return True
    return False


async def konoha_lurker_tick() -> bool:
    if not is_enabled():
        return False
    from chat_router import is_chat_write_forbidden

    if is_chat_write_forbidden(chat_id()):
        disable()
        return False
    settings = load_settings()
    if not settings.get("linked_account", {}).get("user_id"):
        return False

    state = _load_state()
    if not _can_post_today(state):
        return False

    cid = chat_id()
    from user_client import build_chat_context_block, get_recent_messages, is_linked

    if not is_linked():
        return False

    messages = await get_recent_messages(cid, limit=30)
    if not messages:
        return False
    active, recent_2h, recent_30m = _chat_activity(messages)
    if not active:
        return False
    if not _min_gap_ok(state, recent_count=recent_2h):
        return False
    if _recent_owner_minutes(messages):
        return False
    if _looks_like_bot_spam(messages):
        return False

    roll = _post_chance(recent_2h, recent_30m)
    if random.random() > roll:
        state["last_skip_at"] = _now()
        _save_state(state)
        return False

    reply_to, reply_preview = _pick_reply_target(messages)
    title = (
        settings.get("external_chats", {})
        .get("chats", {})
        .get(str(cid), {})
        .get("title")
        or KONOHA_TITLE
    )
    context = await build_chat_context_block(cid, title=title, limit=20)

    reply_hint = ""
    if reply_to and reply_preview:
        reply_hint = f"\n**Реплай на id {reply_to}:** «{reply_preview}»\n"

    task_text = (
        "Автоматическое сообщение в **Коноху** (stealth, по просьбе хозяина).\n"
        "Это **живой чат**, не канал — вписывайся в разговор, когда в тему.\n"
        "Можно **реплаем к другому участнику** (не только к хозяину): поддакнуть, "
        "коротко отреагировать, пошутить, уточнить — как обычный человек в чате.\n"
        "Одна короткая реплика **от имени владельца**. Если повода нет — только `[[silent]]`.\n"
        f"{reply_hint}\n---\n{context}"
    )

    extra: dict[str, Any] = {
        "delivery": "external_telegram",
        "target_chat_id": cid,
        "target_chat_title": title,
        "target_message_id": reply_to or 0,
        "owner_approved_write": True,
        "stealth_mode": True,
        "respond_reason": "stealth_proactive",
        "chat_context": context,
        "from_owner": True,
    }

    task_id = enqueue_task(
        source="konoha_lurker",
        text=task_text,
        chat_id=OWNER_ID,
        message_id=0,
        kind="external_chat",
        extra=extra,
    )
    if not task_id:
        return False

    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("day") != today:
        state["day"] = today
        state["posts_today"] = 0
    state["posts_today"] = int(state.get("posts_today") or 0) + 1
    state["last_post_at"] = _now()
    state["last_task_id"] = task_id
    _save_state(state)
    log.info("konoha stealth task queued id=%s reply_to=%s", task_id, reply_to)
    return True


async def konoha_loop_sleep() -> None:
    total = interval_seconds()
    step = 5.0
    waited = 0.0
    while waited < total:
        await asyncio.sleep(min(step, total - waited))
        waited += step
