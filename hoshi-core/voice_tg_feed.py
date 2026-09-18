#!/usr/bin/env python3
"""Кэш последних сообщений из watched-чатов — для голосовой Юны на ПК."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from config import DATA

FEED_PATH = DATA / "voice_tg_feed.json"
_MAX_PER_CHAT = 12
_MAX_CHATS = 24

_NAME_HINTS = re.compile(
    r"(?:лег[аеу]?|lega|кизу|kizu|iris|бирж)",
    re.I,
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load() -> dict[str, Any]:
    if not FEED_PATH.exists():
        return {"chats": {}, "updated_at": ""}
    try:
        data = json.loads(FEED_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("chats", {})
            return data
    except Exception:
        pass
    return {"chats": {}, "updated_at": ""}


def _save(data: dict[str, Any]) -> None:
    data["updated_at"] = _now()
    FEED_PATH.parent.mkdir(parents=True, exist_ok=True)
    FEED_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def record_message(
    chat_id: int,
    *,
    title: str = "",
    sender: str = "",
    text: str,
    outgoing: bool = False,
    message_id: int = 0,
) -> None:
    body = (text or "").strip()
    if not body or not chat_id:
        return
    data = _load()
    chats: dict[str, Any] = data.setdefault("chats", {})
    key = str(chat_id)
    entry = chats.get(key) or {
        "id": chat_id,
        "title": title or str(chat_id),
        "messages": [],
    }
    if title:
        entry["title"] = title
    msgs = list(entry.get("messages") or [])
    row = {
        "at": _now(),
        "sender": sender or ("[владелец]" if outgoing else "?"),
        "text": body[:500],
        "out": bool(outgoing),
        "id": message_id,
    }
    if msgs and msgs[-1].get("id") == message_id and msgs[-1].get("text") == row["text"]:
        return
    msgs.append(row)
    entry["messages"] = msgs[-_MAX_PER_CHAT:]
    chats[key] = entry
    if len(chats) > _MAX_CHATS:
        ranked = sorted(
            chats.items(),
            key=lambda kv: (kv[1].get("messages") or [{}])[-1].get("at", ""),
            reverse=True,
        )
        data["chats"] = dict(ranked[:_MAX_CHATS])
    _save(data)


def feed_block_for_text(text: str) -> str:
    """Блок для промпта — если в вопросе упомянут чат."""
    if not _NAME_HINTS.search(text or ""):
        return ""
    data = _load()
    chats = data.get("chats") or {}
    if not chats:
        return ""
    lines = ["**Свежие сообщения Telegram (кэш с сервера):**"]
    low = (text or "").lower()
    for entry in chats.values():
        title = str(entry.get("title") or "")
        if title and title.lower() not in low and not _NAME_HINTS.search(title):
            if not _NAME_HINTS.search(low):
                continue
        lines.append(f"\nЧат «{title}» (id {entry.get('id')}):")
        for msg in entry.get("messages") or []:
            who = msg.get("sender") or "?"
            lines.append(f"- [{msg.get('at', '')}] {who}: {msg.get('text', '')}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines) + "\n"
