#!/usr/bin/env python3
"""Контекст рабочего стола — синхронизируется с ПК, виден в Telegram и локально."""
from __future__ import annotations

import json
from pathlib import Path

from config import DATA

LOCAL_CONTEXT = DATA / "local_context.json"


def load_desktop_context() -> dict:
    if not LOCAL_CONTEXT.exists():
        return {}
    try:
        return json.loads(LOCAL_CONTEXT.read_text(encoding="utf-8"))
    except Exception:
        return {}


def desktop_context_block() -> str:
    ctx = load_desktop_context()
    if not ctx:
        return ""
    lines = ["**Контекст ПК (единая память с Telegram):**"]
    ap = ctx.get("active_project")
    if ap:
        lines.append(f"- Активный проект: {ap.get('name', '')} — `{ap.get('path', '')}`")
    for p in (ctx.get("projects") or [])[:5]:
        lines.append(f"- Проект: {p.get('name', '')} — `{p.get('path', '')}`")
    ls = ctx.get("last_screen")
    if ls:
        lines.append(f"- Последний скрин: `{ls.get('path', '')}` ({ls.get('at', '')})")
    for n in (ctx.get("notes") or [])[-5:]:
        lines.append(f"- Заметка: {str(n.get('text', ''))[:300]}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines) + "\n\n"
