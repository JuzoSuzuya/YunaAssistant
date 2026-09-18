#!/usr/bin/env python3
"""Контекст Telegram с сервера — тот же enrich_owner_task, что у Юны в боте."""
from __future__ import annotations

import base64
import json
import logging
import shlex
import subprocess
from typing import Any

from desk_config import SERVER_HOSHI_PATH, SERVER_HOST, SERVER_SSH_PASS, SERVER_USER

log = logging.getLogger("yuna.remote_brain")


def _ssh_shell() -> str:
    if SERVER_SSH_PASS:
        return (
            f"sshpass -p {shlex.quote(SERVER_SSH_PASS)} ssh -F /dev/null "
            "-o StrictHostKeyChecking=no -o ConnectTimeout=10"
        )
    return "ssh -F /dev/null -o StrictHostKeyChecking=no -o ConnectTimeout=10"


def fetch_owner_enrich(text: str, *, timeout: int = 6) -> tuple[str, dict[str, Any]]:
    """Подтягивает live-переписку (Лега и др.) через Telethon на сервере."""
    body = (text or "").strip()
    if not body:
        return text, {}

    payload = base64.b64encode(body.encode("utf-8")).decode("ascii")
    hoshi = shlex.quote(SERVER_HOSHI_PATH)
    remote_py = f"""
import asyncio, base64, json, os, sys
os.chdir({hoshi})
sys.path.insert(0, {hoshi})
text = base64.b64decode({json.dumps(payload)}).decode()
async def main():
    from chat_router import enrich_owner_task
    t, e = await enrich_owner_task(text)
    print(json.dumps({{"text": t, "extra": e}}, ensure_ascii=False))
asyncio.run(main())
""".strip()

    cmd = [
        "ssh",
        "-F",
        "/dev/null",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "ConnectTimeout=10",
        f"{SERVER_USER}@{SERVER_HOST}",
        f"cd {SERVER_HOSHI_PATH} && python3 -c {shlex.quote(remote_py)}",
    ]
    if SERVER_SSH_PASS:
        cmd = ["sshpass", "-p", SERVER_SSH_PASS] + cmd

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.debug("remote enrich timeout")
        return text, {}
    except Exception as e:
        log.debug("remote enrich: %s", e)
        return text, {}

    if proc.returncode != 0:
        log.debug("remote enrich rc=%s: %s", proc.returncode, (proc.stderr or "")[:160])
        return text, {}

    raw = (proc.stdout or "").strip()
    if not raw:
        return text, {}
    try:
        data = json.loads(raw.splitlines()[-1])
        enriched = str(data.get("text") or text)
        extra = dict(data.get("extra") or {})
        if extra.get("chat_context"):
            log.info("remote tg context: %s", extra.get("resolved_chat_title", "?"))
        return enriched, extra
    except Exception as e:
        log.debug("remote enrich parse: %s", e)
        return text, {}
