#!/usr/bin/env python3
"""Контекст рабочего стола — виден и в Telegram, и локально (единая память)."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from desk_config import HOSHI_CORE, LOCAL_CONTEXT, OWNER_ID

DEFAULT = {
    "updated_at": "",
    "active_project": None,
    "projects": [],
    "last_screen": None,
    "notes": [],
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_context() -> dict[str, Any]:
    if not LOCAL_CONTEXT.exists():
        return dict(DEFAULT)
    try:
        data = json.loads(LOCAL_CONTEXT.read_text(encoding="utf-8"))
        out = dict(DEFAULT)
        out.update(data)
        return out
    except Exception:
        return dict(DEFAULT)


def save_context(data: dict[str, Any]) -> None:
    data["updated_at"] = _now()
    LOCAL_CONTEXT.parent.mkdir(parents=True, exist_ok=True)
    LOCAL_CONTEXT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        from memory_sync import push_to_server
        push_to_server()
    except Exception:
        pass


def set_active_project(path: str, *, name: str = "") -> None:
    ctx = load_context()
    proj = {
        "id": path,
        "path": path,
        "name": name or Path(path).name,
        "updated_at": _now(),
    }
    projects = [p for p in ctx.get("projects") or [] if p.get("path") != path]
    projects.insert(0, proj)
    ctx["active_project"] = proj
    ctx["projects"] = projects[:20]
    save_context(ctx)


def add_note(text: str) -> None:
    ctx = load_context()
    notes = list(ctx.get("notes") or [])
    notes.append({"text": text[:2000], "at": _now()})
    ctx["notes"] = notes[-50:]
    save_context(ctx)


def capture_screen_meta(screenshot: Path) -> None:
    ctx = load_context()
    ctx["last_screen"] = {
        "path": str(screenshot),
        "at": _now(),
        "size": screenshot.stat().st_size if screenshot.exists() else 0,
    }
    save_context(ctx)


def context_for_prompt() -> str:
    """Блок для промпта агента — чтобы TG знал, что делали на ПК."""
    ctx = load_context()
    lines = ["**Контекст рабочего стола (единая память):**"]
    # Живое зрение (Neurosama-стиль): кэш из фона, без нового скрина на каждое сообщение
    try:
        from live_vision import get_live_see, load_live

        see = get_live_see(max_age_sec=55)
        lv = load_live()
        if see:
            lines.append(f"- Глаза сейчас: {see}")
        win_lv = str(lv.get("window") or "").strip()
        if win_lv:
            lines.append(f"- Окно (live): {win_lv}")
    except Exception:
        lv = ctx.get("live_vision") or {}
        if isinstance(lv, dict) and lv.get("see"):
            lines.append(f"- Глаза сейчас: {lv.get('see')}")
    ap = ctx.get("active_project")
    if ap:
        lines.append(f"- Активный проект: {ap.get('name')} ({ap.get('path')})")
    for p in (ctx.get("projects") or [])[:5]:
        lines.append(f"- Проект: {p.get('name')} — {p.get('path')}")
    for n in (ctx.get("notes") or [])[-5:]:
        lines.append(f"- Заметка: {n.get('text', '')[:300]}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines) + "\n"


def hypr_active_window() -> str:
    try:
        proc = subprocess.run(
            ["hyprctl", "activewindow", "-j"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if proc.returncode == 0:
            data = json.loads(proc.stdout)
            return f"{data.get('title', '')} [{data.get('class', '')}]"
    except Exception:
        pass
    return ""
