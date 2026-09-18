#!/usr/bin/env python3
"""
Память чата в стиле Jarvis (isair/jarvis, three-tier):
  1. Все сообщения хранятся (не выкидываются каждые 80).
  2. rolling_summary — сжатое «что просили / что обещала».
  3. memory_diary — факты и просьбы для «заглянуть» в прошлое.
  4. В промпт идут summary + последние N реплик.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from desk_config import OWNER_ID

RECENT_VERBATIM = 40
MAX_STORED = 3000
MAX_DIARY = 250
DIARY_SNIPPET = 60

_WAKE = re.compile(r"^(юна|дюна|yuna|юну|юной|юно)[\s!.,?…]*$", re.I)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _user_text(text: str) -> str:
    head = (text or "").split("\n---\n", 1)[0].strip()
    return head


def _is_trivial(text: str) -> bool:
    t = _user_text(text)
    if not t or len(t) < 4:
        return True
    if _WAKE.match(t.lower()):
        return True
    return False


def ensure_fields(session: dict[str, Any]) -> dict[str, Any]:
    session.setdefault("rolling_summary", "")
    session.setdefault("memory_diary", [])
    session.setdefault("summary_covers", 0)
    return session


def refresh_memory(session: dict[str, Any]) -> dict[str, Any]:
    """Обновить diary и rolling_summary из старых сообщений."""
    session = ensure_fields(session)
    msgs: list[dict] = session.get("messages") or []
    split = max(0, len(msgs) - RECENT_VERBATIM)
    if split <= session.get("summary_covers", 0):
        return session

    old = msgs[:split]
    diary: list[str] = list(session.get("memory_diary") or [])

    for m in old:
        role = m.get("role")
        raw = (m.get("text") or "").strip()
        if not raw:
            continue
        if role == "user":
            t = _user_text(raw)
            if _is_trivial(t):
                continue
            line = f"[просьба] {t[:400]}"
        else:
            t = raw[:300]
            if len(t) < 25:
                continue
            line = f"[ответ] {t}"
        if line not in diary:
            diary.append(line)

    session["memory_diary"] = diary[-MAX_DIARY:]
    user_facts = [d for d in session["memory_diary"] if d.startswith("[просьба]")][-DIARY_SNIPPET:]
    bot_facts = [d for d in session["memory_diary"] if d.startswith("[ответ]")][-15:]

    parts: list[str] = []
    if user_facts:
        parts.append(
            "Что просил хозяин (память, не забывай):\n"
            + "\n".join(f"- {x[9:]}" for x in user_facts)
        )
    if bot_facts:
        parts.append(
            "Что отвечала ранее:\n"
            + "\n".join(f"- {x[8:]}" for x in bot_facts)
        )
    session["rolling_summary"] = "\n\n".join(parts)
    session["summary_covers"] = split
    session["summary_updated_at"] = _now()
    return session


def smart_compact(user_id: int | None = None, *, keep: int = 80) -> int:
    """Не удаляет историю — только обновляет summary (совместимость с hoshi_daemon)."""
    from storage import load_agent_session, save_agent_session

    uid = user_id or OWNER_ID
    session = refresh_memory(load_agent_session(uid))
    msgs = session.get("messages") or []
    if len(msgs) > MAX_STORED:
        # Крайний случай: старое уже в diary, оставляем хвост
        session["messages"] = msgs[-MAX_STORED:]
        session["summary_covers"] = min(session.get("summary_covers", 0), len(session["messages"]) - RECENT_VERBATIM)
    save_agent_session(session, uid)
    return 0


def append_message(
    session: dict[str, Any],
    role: str,
    text: str,
    *,
    origin: str = "desktop",
) -> dict[str, Any]:
    """Добавить сообщение с меткой источника."""
    stored = text
    if role == "assistant":
        try:
            from text_format import is_iris_greeting_template, strip_iris_greeting_template

            cleaned = strip_iris_greeting_template(stored)
            if cleaned:
                stored = cleaned
            elif is_iris_greeting_template(stored):
                return session
        except Exception:
            pass
        if len(stored) > 2000:
            stored = stored[:2000] + "…"
    elif role == "user" and "\n---\n" in stored:
        head, _ = stored.split("\n---\n", 1)
        stored = head[:1500]

    session.setdefault("messages", []).append(
        {
            "role": role,
            "text": stored,
            "at": _now(),
            "origin": origin,
        }
    )
    if len(session["messages"]) > MAX_STORED:
        session = refresh_memory(session)
        session["messages"] = session["messages"][-MAX_STORED:]
    return refresh_memory(session)


def memory_block_for_prompt(session: dict[str, Any]) -> str:
    session = refresh_memory(ensure_fields(session))
    parts: list[str] = []
    summary = (session.get("rolling_summary") or "").strip()
    if summary:
        parts.append(f"**ПАМЯТЬ СЕССИИ (раньше в разговорах):**\n{summary}")

    recent_lines: list[str] = []
    for m in (session.get("messages") or [])[-RECENT_VERBATIM:]:
        tag = "USER" if m.get("role") == "user" else "ASSISTANT"
        body = (m.get("text") or "")[:800]
        recent_lines.append(f"{tag}: {body}")
    if recent_lines:
        parts.append("**Недавний диалог:**\n" + "\n".join(recent_lines))
    return "\n\n".join(parts)


def desktop_messages(session: dict[str, Any], *, limit: int = 200) -> list[dict]:
    """Сообщения для UI десктопа — только локальный чат, без старого TG."""
    msgs = session.get("messages") or []
    tagged = [m for m in msgs if m.get("origin") == "desktop"]
    if tagged:
        return tagged[-limit:]
    return msgs[-30:]
