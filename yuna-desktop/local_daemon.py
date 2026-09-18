#!/usr/bin/env python3
"""
Локальный worker: тот же cursor-agent и та же память, что на сервере.
Обрабатывает data/local_inbox — не мешает Telegram-очереди на VPS.
«Напиши Кизяке» с панели — на сервер (Telethon), не локальный Cursor.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOSHI = ROOT.parent / "hoshi-core"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HOSHI))

os.chdir(HOSHI)

import config as hoshi_config  # noqa: E402

LOCAL_INBOX = hoshi_config.DATA / "local_inbox"
LOCAL_OUTBOX = hoshi_config.DATA / "local_outbox"
LOCAL_INBOX.mkdir(parents=True, exist_ok=True)
LOCAL_OUTBOX.mkdir(parents=True, exist_ok=True)

hoshi_config.INBOX = LOCAL_INBOX
hoshi_config.OUTBOX = LOCAL_OUTBOX
hoshi_config.LOG = ROOT / "local_daemon.log"
hoshi_config.LOCK = ROOT / ".local_daemon.lock"
hoshi_config.STATUS = ROOT / "local_daemon_status.json"

import restart_util  # noqa: E402

restart_util.ensure_bridge_running = lambda: True  # type: ignore[method-assign]
restart_util.ensure_heavy_worker_running = lambda: True  # type: ignore[method-assign]

import storage  # noqa: E402
import unified_storage as us  # noqa: E402

storage.load_agent_session = us.load_agent_session
storage.save_agent_session = us.save_agent_session
storage.append_agent_message = us.append_agent_message
storage.compact_agent_session = us.compact_agent_session
storage.load_settings = us.load_settings
storage.save_settings = us.save_settings

import hoshi_daemon  # noqa: E402
from chat_router import owner_wants_routed_chat_reply  # noqa: E402
from desk_config import BRAIN, OLLAMA_HEAVY_MODEL, OLLAMA_MODEL, OWNER_ID  # noqa: E402
from hoshi_daemon import _apply_owner_delivery_patches, log  # noqa: E402

log_desktop = logging.getLogger("yuna.local_daemon")
_orig_process_task = hoshi_daemon.process_task
_orig_run_cursor = hoshi_daemon.run_cursor


def _run_brain(prompt: str, model=None, mode=None, owner="local", **kwargs):
    """Локальный мозг: Ollama с tool-loop вместо Cursor."""
    if BRAIN == "cursor":
        return _orig_run_cursor(prompt, model=model, mode=mode, owner=owner, **kwargs)
    try:
        from ollama_brain import run_agent

        use_model = OLLAMA_HEAVY_MODEL if (model and "heavy" in str(model).lower()) else OLLAMA_MODEL
        # code_fix / agent — больше шагов tools
        heavy = owner in ("heavy", "desktop") or "исправ" in (prompt or "").lower()
        return run_agent(
            prompt,
            model=use_model,
            max_steps=14 if heavy else 8,
        )
    except Exception as e:
        log_desktop.warning("ollama brain failed: %s", e)
        if BRAIN == "auto":
            return _orig_run_cursor(prompt, model=model, mode=mode, owner=owner, **kwargs)
        raise


if BRAIN in ("ollama", "auto"):
    hoshi_daemon.run_cursor = _run_brain  # type: ignore[assignment]


def _relay_desktop_external_to_server(item: dict) -> bool:
    """Панель без Telethon — пишем в чат только через VPS."""
    patched = _apply_owner_delivery_patches(item)
    extra = patched.get("extra") or {}
    head = (patched.get("text") or "").split("\n---\n")[0].strip()
    if extra.get("delivery") != "external_telegram" and not owner_wants_routed_chat_reply(
        head
    ):
        return False
    from server_task import submit_owner_task

    task_id = patched["id"]
    confirm = submit_owner_task(head)
    owner_reply = confirm or "Не вышло отправить — сервер молчит, хозяин."
    storage.move_queue_item(LOCAL_INBOX, LOCAL_OUTBOX, task_id, status="done", reply=owner_reply)
    us.append_agent_message("assistant", owner_reply, OWNER_ID, origin="desktop")
    log(f"Task {task_id}: relayed desktop write → server")
    log_desktop.info("relayed %s → server: %s", task_id, owner_reply[:80])
    return True


def process_task(item: dict) -> None:
    if item.get("source") == "desktop":
        try:
            if _relay_desktop_external_to_server(item):
                return
        except Exception as e:
            log(f"desktop relay failed: {e}")
            log_desktop.warning("desktop relay failed: %s", e)
    _orig_process_task(item)


hoshi_daemon.process_task = process_task

from hoshi_daemon import main  # noqa: E402

if __name__ == "__main__":
    main()
