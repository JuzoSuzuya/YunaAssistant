#!/usr/bin/env python3
"""Постановка задачи владельца в очередь сервера (голос / desktop)."""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


async def _submit(text: str, *, images: list[str] | None = None) -> dict:
    import re

    from agent_prompt import detect_task_kind
    from bot_branches import clear_external_delivery
    from chat_router import (
        enrich_owner_task,
        owner_wants_routed_chat_reply,
        resolve_kizu_chat_id_sync,
    )
    from config import OWNER_ID
    from storage import enqueue_task, load_settings

    body = (text or "").strip()
    if not body:
        return {"ok": False, "error": "empty"}

    head = body.split("\n---\n")[0]
    if owner_wants_routed_chat_reply(body):
        task_text, extra = _fast_routed_extra(body)
    else:
        task_text, extra = await enrich_owner_task(body)

    settings = load_settings()
    kind = detect_task_kind(head)
    if owner_wants_routed_chat_reply(body):
        kind = "agent_message"

    task_extra: dict = {
        "user_id": OWNER_ID,
        "origin": "yuna-voice",
        "voice": True,
        "from_owner": True,
        "dialog_started_at": settings.get("agent", {}).get("dialog_started_at", ""),
        **extra,
    }
    if not owner_wants_routed_chat_reply(body):
        clear_external_delivery(task_extra)

    task_id = enqueue_task(
        source="voice",
        text=task_text or body,
        chat_id=OWNER_ID,
        message_id=0,
        kind=kind,
        extra=task_extra,
        images=images or [],
    )
    if not task_id:
        return {"ok": False, "error": "duplicate or enqueue failed"}

    return {
        "ok": True,
        "task_id": task_id,
        "target_title": task_extra.get("target_chat_title")
        or task_extra.get("resolved_chat_title")
        or "",
        "delivery": task_extra.get("delivery") or "",
    }


def _fast_routed_extra(text: str) -> tuple[str, dict]:
    """«напиши Кизяке» — без Telethon-контекста (иначе SSH >80с)."""
    import re

    from chat_router import owner_wants_routed_chat_reply, resolve_kizu_chat_id_sync
    from storage import load_settings

    head = (text or "").split("\n---\n")[0]
    extra: dict = {}
    if not owner_wants_routed_chat_reply(head):
        return text, extra
    low = head.lower()
    cid: int | None = None
    title = ""
    if re.search(r"кизяк|kizu", low, re.I):
        cid, title = resolve_kizu_chat_id_sync()
    elif re.search(r"лег[аеу]?|lega", low, re.I):
        for key, cfg in (load_settings().get("external_chats", {}).get("chats") or {}).items():
            uname = (cfg.get("username") or "").lower()
            tname = (cfg.get("title") or "").lower()
            if uname == "legendaah" or tname == "лега" or "лега" in tname:
                try:
                    cid = int(key)
                    title = str(cfg.get("title") or "Лега")
                    break
                except (TypeError, ValueError):
                    continue
    if not cid:
        return text, extra
    extra.update(
        {
            "delivery": "external_telegram",
            "target_chat_id": int(cid),
            "target_chat_title": title or str(cid),
            "resolved_chat_id": int(cid),
            "resolved_chat_title": title or str(cid),
            "owner_approved_write": True,
        }
    )
    return text, extra


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--text-b64", required=True)
    p.add_argument("--images-json", default="[]")
    args = p.parse_args()
    text = base64.b64decode(args.text_b64).decode("utf-8")
    try:
        images = json.loads(args.images_json)
        if not isinstance(images, list):
            images = []
    except Exception:
        images = []
    out = asyncio.run(_submit(text, images=[str(x) for x in images if x]))
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
