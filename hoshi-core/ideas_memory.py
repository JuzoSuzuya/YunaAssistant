#!/usr/bin/env python3
"""Долгосрочные идеи и правила из «запомни» / «запомни идею»."""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from config import DATA
from storage import _read_json, _write_json

IDEAS_PATH = DATA / "saved_ideas.json"
MAX_IDEAS = 200
MAX_PROMPT_IDEAS = 8

REMEMBER_RE = re.compile(
    r"запомни(?:\s+идею)?[,:]?\s*(.+)",
    re.I | re.S,
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load() -> dict[str, Any]:
    return _read_json(IDEAS_PATH, {"ideas": [], "updated_at": ""})


def _save(data: dict[str, Any]) -> None:
    data["updated_at"] = _now()
    _write_json(IDEAS_PATH, data)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _keywords(text: str) -> set[str]:
    low = text.lower()
    words = re.findall(r"[a-zа-яё0-9]{3,}", low, flags=re.I)
    return {w for w in words if w not in {"что", "это", "как", "для", "или", "ещё", "еще"}}


def remember_from_text(
    text: str,
    *,
    chat_id: int | None = None,
    author: str = "",
) -> str | None:
    """Сохраняет идею из «запомни …». Возвращает id или None."""
    if "запомни" not in (text or "").lower():
        return None
    m = REMEMBER_RE.search(text)
    if not m:
        return None
    body = _normalize(m.group(1))
    if len(body) < 8:
        return None
    data = _load()
    ideas: list[dict[str, Any]] = list(data.get("ideas") or [])
    idea_id = uuid.uuid4().hex[:10]
    ideas.append(
        {
            "id": idea_id,
            "text": body[:4000],
            "keywords": sorted(_keywords(body))[:40],
            "chat_id": chat_id,
            "author": (author or "").strip()[:80],
            "saved_at": _now(),
        }
    )
    data["ideas"] = ideas[-MAX_IDEAS:]
    _save(data)
    return idea_id


def search_ideas(query: str, *, limit: int = 5) -> list[dict[str, Any]]:
    qk = _keywords(query)
    if not qk:
        return []
    scored: list[tuple[int, dict[str, Any]]] = []
    for idea in reversed(_load().get("ideas") or []):
        ik = set(idea.get("keywords") or _keywords(idea.get("text", "")))
        score = len(qk & ik)
        if score:
            scored.append((score, idea))
    scored.sort(key=lambda x: (-x[0], x[1].get("saved_at", "")))
    return [item for _, item in scored[:limit]]


def ideas_for_prompt(text: str = "") -> str:
    """Блок для промпта: релевантные идеи + последние закреплённые."""
    data = _load()
    ideas: list[dict[str, Any]] = list(data.get("ideas") or [])
    if not ideas:
        return ""
    picked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idea in search_ideas(text, limit=MAX_PROMPT_IDEAS):
        iid = str(idea.get("id", ""))
        if iid and iid not in seen:
            picked.append(idea)
            seen.add(iid)
    tail = max(0, MAX_PROMPT_IDEAS - len(picked))
    if tail:
        for idea in reversed(ideas):
            iid = str(idea.get("id", ""))
            if iid in seen:
                continue
            picked.append(idea)
            seen.add(iid)
            if len(picked) >= MAX_PROMPT_IDEAS:
                break
    lines = ["**Закреплённые идеи (запомни):**"]
    for idea in picked:
        who = (idea.get("author") or "").strip()
        prefix = f"[{who}] " if who else ""
        lines.append(f"- {prefix}{idea.get('text', '')[:500]}")
    return "\n".join(lines) + "\n"
