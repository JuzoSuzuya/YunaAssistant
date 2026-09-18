#!/usr/bin/env python3
"""Обёртка над storage: синхронизация + долгая память (push не блокирует голос)."""
from __future__ import annotations

import sys
import threading
from pathlib import Path

HOSHI = Path(__file__).resolve().parent.parent / "hoshi-core"
if str(HOSHI) not in sys.path:
    sys.path.insert(0, str(HOSHI))

from storage import (  # noqa: E402
    load_agent_session as _load,
    load_settings as _load_settings,
    save_agent_session as _save,
    save_settings as _save_settings,
)

from chat_memory import append_message, refresh_memory, smart_compact  # noqa: E402
from memory_sync import push_to_server  # noqa: E402

_push_lock = threading.Lock()
_push_timer: threading.Timer | None = None
_PUSH_DELAY = 4.0


def _do_push() -> None:
    try:
        push_to_server()
    except Exception:
        pass


def _schedule_push() -> None:
    global _push_timer
    with _push_lock:
        if _push_timer is not None:
            _push_timer.cancel()
        _push_timer = threading.Timer(_PUSH_DELAY, _do_push)
        _push_timer.daemon = True
        _push_timer.start()


def load_agent_session(user_id: int | None = None):
    return refresh_memory(_load(user_id))


def save_agent_session(session: dict, user_id: int | None = None) -> None:
    session = refresh_memory(session)
    _save(session, user_id)
    _schedule_push()


def append_agent_message(
    role: str,
    text: str,
    user_id: int | None = None,
    *,
    origin: str = "desktop",
    sync: bool = True,
) -> None:
    session = _load(user_id)
    session = append_message(session, role, text, origin=origin)
    _save(session, user_id)
    if sync:
        _schedule_push()


def compact_agent_session(user_id: int | None = None, *, keep: int = 80) -> int:
    return smart_compact(user_id, keep=keep)


def load_settings():
    return _load_settings()


def save_settings(data: dict) -> None:
    _save_settings(data)
    _schedule_push()
