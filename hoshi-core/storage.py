#!/usr/bin/env python3
"""Персистентное хранилище на JSON."""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from config import INBOX, OUTBOX, SESSIONS, SETTINGS, OWNER_ID


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def default_settings() -> dict:
    return {
        "owner_id": OWNER_ID,
        "owner_title": "",
        "linked_account": {
            "phone": "",
            "user_id": None,
            "username": "",
            "first_name": "",
            "linked_at": "",
        },
        "permissions": {
            "can_write_chats": False,
            "can_write_private_dms": True,
            "dm_on_call_only": True,
            "ignore_empty_messages": True,
            "owner_never_refuse": True,
            "forbid_third_party_chat_intel": True,
            "watch_groups": False,
            "allowed_dm_chats": None,
            "allowed_mentions": [],
        },
        "external_chats": {
            "active_chat_id": None,
            "chats": {},
            "hoshi_message_ids": {},
            "active_sessions": {},
            "preferences": {},
            "trigger_reactions": {},
        },
        "channel": {
            "username": "",
            "chat_id": None,
            "title": "",
        },
        "posts": {
            "per_day": 3,
            "style": "короткие новости, эмодзи, ссылка на источник",
        },
        "monitoring": {
            "news_sites": [],
            "telegram_channels": [],
            "exchanges": [],
        },
        "proactive": {
            "enabled": True,
            "interval_hours": 2.0,
            "include_sao_tips": True,
        },
        "iris_monitor": {
            "enabled": False,
            "interval_minutes": 5,
            "chart_days": 30,
            "alert_only": True,
            "auto_trade": False,
            "silent_external": False,
            "enabled_at": "",
        },
        "iris_check_claimer": {
            "enabled": False,
            "chat_id": -1002941121338,
            "topic_id": 707783,
            "enabled_at": "",
        },
        "ad_monitor": {
            "enabled": False,
            "interval_minutes": 15,
            "min_score": 55,
            "alert_chat_id": 7527090820,
            "exchange_ids": [],
            "enabled_at": "",
        },
        "news_poster": {
            "enabled": False,
            "test_mode": True,
            "production_channel": "@HoshiKojima",
            "test_channel_id": None,
            "test_channel_username": "",
            "footer": "#animenews\n" + "➿" * 10 + "\n✨Hoshi Kojima | Лучший впн ⛩️",
            "interval_minutes": 30,
            "max_per_day": 4,
            "test_max_per_day": 20,
            "min_gap_minutes": 30,
            "post_hour_start": 11,
            "post_hour_end": 21,
            "post_timezone": "Europe/Moscow",
            "prefer_media": True,
            "enabled_at": "",
        },
        "konoha_lurker": {
            "enabled": False,
            "chat_id": -1002685666919,
            "check_interval_minutes": 6,
            "max_per_day": 20,
            "min_gap_minutes": 12,
            "post_chance": 0.58,
            "enabled_at": "",
        },
        "personas": {
            "active": {},
            "aliases": {},
        },
        "agent": {
            "dialog_active": False,
            "dialog_started_at": "",
            "pending_exit_confirm": False,
        },
        "account_link": {
            "step": "",
            "phone": "",
            "phone_code_hash": "",
        },
        "telegram_api": {
            "api_id": "",
            "api_hash": "",
        },
        "updated_at": _now(),
    }


def load_settings() -> dict:
    data = _read_json(SETTINGS, default_settings())
    base = default_settings()
    for key, val in base.items():
        if key not in data:
            data[key] = val
        elif isinstance(val, dict) and isinstance(data.get(key), dict):
            for sub_key, sub_val in val.items():
                data[key].setdefault(sub_key, sub_val)
    return data


def save_settings(data: dict) -> None:
    data["updated_at"] = _now()
    _write_json(SETTINGS, data)


def agent_session_path(user_id: int | None = None) -> Path:
    uid = user_id or OWNER_ID
    return SESSIONS / f"{uid}.json"


def load_agent_session(user_id: int | None = None) -> dict:
    default = {
        "user_id": user_id or OWNER_ID,
        "messages": [],
        "created_at": _now(),
        "updated_at": _now(),
    }
    data = _read_json(agent_session_path(user_id), default)
    data.setdefault("messages", [])
    return data


def save_agent_session(session: dict, user_id: int | None = None) -> None:
    session["updated_at"] = _now()
    _write_json(agent_session_path(user_id), session)


def compact_agent_session(user_id: int | None = None, *, keep: int = 80) -> int:
    """Урезает историю сессии — память сохраняется, хвост лишнего убирается."""
    session = load_agent_session(user_id)
    msgs = session.get("messages") or []
    before = len(msgs)
    if before <= keep:
        for m in msgs:
            t = m.get("text") or ""
            if len(t) > 1200:
                m["text"] = t[:1200] + "…"
        save_agent_session(session, user_id)
        return 0
    session["messages"] = msgs[-keep:]
    for m in session["messages"]:
        t = m.get("text") or ""
        if len(t) > 1200:
            m["text"] = t[:1200] + "…"
    save_agent_session(session, user_id)
    return before - keep


def append_agent_message(role: str, text: str, user_id: int | None = None) -> None:
    session = load_agent_session(user_id)
    stored = text
    if role == "assistant":
        try:
            from text_format import is_iris_greeting_template, strip_iris_greeting_template

            cleaned = strip_iris_greeting_template(stored)
            if cleaned:
                stored = cleaned
            elif is_iris_greeting_template(stored):
                return
        except Exception:
            pass
    if role == "assistant" and len(stored) > 2000:
        stored = stored[:2000] + "…"
    elif role == "user" and "\n---\n" in stored:
        head, _ = stored.split("\n---\n", 1)
        stored = head[:1500]
    session["messages"].append(
        {
            "role": role,
            "text": stored,
            "at": _now(),
        }
    )
    if len(session["messages"]) > 500:
        session["messages"] = session["messages"][-500:]
    save_agent_session(session, user_id)


def set_pending_images(images: list[str], user_id: int | None = None) -> None:
    if not images:
        return
    session = load_agent_session(user_id)
    session["pending_images"] = {"paths": images, "at": _now()}
    save_agent_session(session, user_id)


def take_pending_images(user_id: int | None = None, *, max_age_sec: int = 600) -> list[str]:
    session = load_agent_session(user_id)
    pending = session.pop("pending_images", None)
    save_agent_session(session, user_id)
    if not pending:
        return []
    try:
        at = datetime.fromisoformat(pending["at"])
        if (datetime.now() - at).total_seconds() > max_age_sec:
            return []
    except Exception:
        return []
    return list(pending.get("paths") or [])


def task_draft_id(task_id: str) -> int:
    suffix = task_id.rsplit("_", 1)[-1]
    try:
        n = int(suffix, 16)
    except ValueError:
        n = abs(hash(task_id))
    return (n % (2**31 - 1)) + 1


def _code_fix_already_queued(text: str) -> bool:
    needle = (text or "").strip()[:120]
    if not needle:
        return False
    try:
        from hoshi_daemon import is_cursor_transient_error

        if is_cursor_transient_error(text):
            return True
    except Exception:
        pass
    for item in list_queue_items(INBOX):
        if item.get("kind") != "code_fix":
            continue
        if item.get("status") not in ("pending", "in_progress"):
            continue
        if needle in (item.get("text") or ""):
            return True
    return False


def enqueue_task(
    *,
    source: str,
    text: str,
    chat_id: int,
    message_id: int,
    kind: str = "agent_message",
    extra: dict | None = None,
    images: list[str] | None = None,
) -> str:
    if kind == "code_fix" and _code_fix_already_queued(text):
        return ""
    task_id = f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
    item = {
        "id": task_id,
        "source": source,
        "kind": kind,
        "text": text,
        "chat_id": chat_id,
        "message_id": message_id,
        "draft_id": task_draft_id(task_id),
        "received_at": _now(),
        "status": "pending",
        "note": "",
        "extra": extra or {},
        "images": images or [],
    }
    _write_json(INBOX / f"{task_id}.json", item)
    return task_id


def list_queue_items(folder: Path, status: str | None = None) -> list[dict]:
    items = []
    for path in sorted(folder.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if status is None or item.get("status") == status:
            items.append(item)
    return items


def update_queue_item(folder: Path, task_id: str, **fields) -> None:
    path = folder / f"{task_id}.json"
    if not path.exists():
        return
    item = json.loads(path.read_text(encoding="utf-8"))
    item.update(fields)
    item["updated_at"] = _now()
    _write_json(path, item)


def purge_inbox_for_target_chat(target_chat_id: int) -> int:
    """Отменяет pending/in_progress задачи для внешнего чата."""
    cid = int(target_chat_id)
    removed = 0
    for path in list(INBOX.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if item.get("status") not in ("pending", "in_progress"):
            continue
        extra = item.get("extra") or {}
        if int(extra.get("target_chat_id") or 0) != cid:
            continue
        path.unlink(missing_ok=True)
        removed += 1
    return removed


def move_queue_item(src: Path, dst_dir: Path, task_id: str, **fields) -> None:
    src_path = src / f"{task_id}.json"
    if not src_path.exists():
        return
    item = json.loads(src_path.read_text(encoding="utf-8"))
    item.update(fields)
    item["updated_at"] = _now()
    dst_dir.mkdir(parents=True, exist_ok=True)
    _write_json(dst_dir / f"{task_id}.json", item)
    src_path.unlink(missing_ok=True)
