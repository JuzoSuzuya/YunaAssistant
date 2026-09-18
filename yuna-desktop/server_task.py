#!/usr/bin/env python3
"""Голосовые приказы «напиши …» — в очередь сервера (Telethon), не Cursor Ask."""
from __future__ import annotations

import base64
import json
import logging
import shlex
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from desk_config import (
    HOSHI_CORE,
    OWNER_ID,
    SERVER_HOSHI_PATH,
    SERVER_HOST,
    SERVER_SSH_PASS,
    SERVER_USER,
)

log = logging.getLogger("yuna.server_task")

SSH_CONTROL = Path("/tmp") / f"yuna-ssh-{SERVER_USER}@{SERVER_HOST}.sock"


def _ssh_opts() -> list[str]:
    opts = [
        "-F",
        "/dev/null",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "ConnectTimeout=20",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        f"ControlPath={SSH_CONTROL}",
        "-o",
        "ControlMaster=auto",
        "-o",
        "ControlPersist=600",
    ]
    return opts


def _ssh_cmd(remote: str, *, timeout: float = 120) -> list[str]:
    base = ["ssh", *_ssh_opts(), f"{SERVER_USER}@{SERVER_HOST}", remote]
    if SERVER_SSH_PASS:
        return ["sshpass", "-p", SERVER_SSH_PASS, *base]
    return base


def _rsync_ssh() -> str:
    opts = " ".join(shlex.quote(x) for x in _ssh_opts())
    if SERVER_SSH_PASS:
        return f"sshpass -p {shlex.quote(SERVER_SSH_PASS)} ssh {opts}"
    return f"ssh {opts}"


def _sync_server_files() -> None:
    hoshi = HOSHI_CORE.parent / "hoshi-core"
    names = (
        "voice_server_submit.py",
        "hoshi_daemon.py",
        "chat_router.py",
    )
    sources = [str(hoshi / n) for n in names if (hoshi / n).exists()]
    if not sources:
        return
    try:
        proc = subprocess.run(
            [
                "rsync",
                "-avz",
                "-e",
                _rsync_ssh(),
                *sources,
                f"{SERVER_USER}@{SERVER_HOST}:{SERVER_HOSHI_PATH}/",
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
        if proc.returncode != 0:
            log.warning("rsync sync failed: %s", (proc.stderr or proc.stdout or "")[:200])
    except Exception as e:
        log.warning("sync batch: %s", e)


def _task_draft_id(task_id: str) -> int:
    return abs(hash(task_id)) % (2**31 - 1)


def _build_routed_task_item(text: str, *, images: list[str] | None = None) -> dict | None:
    sys.path.insert(0, str(HOSHI_CORE))
    from agent_prompt import detect_task_kind
    from chat_router import owner_wants_routed_chat_reply
    from voice_server_submit import _fast_routed_extra

    body = (text or "").strip()
    if not body:
        return None
    head = body.split("\n---\n")[0]
    if not owner_wants_routed_chat_reply(head):
        return None
    task_text, extra = _fast_routed_extra(body)
    if extra.get("delivery") != "external_telegram":
        return None
    task_id = f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
    return {
        "id": task_id,
        "source": "voice",
        "kind": "agent_message",
        "text": task_text or body,
        "chat_id": OWNER_ID,
        "message_id": 0,
        "draft_id": _task_draft_id(task_id),
        "received_at": datetime.now().isoformat(timespec="seconds"),
        "status": "pending",
        "note": "",
        "extra": {
            "user_id": OWNER_ID,
            "origin": "yuna-desktop",
            "voice": True,
            "from_owner": True,
            **extra,
        },
        "images": images or [],
    }


def _enqueue_on_server_via_rsync(item: dict) -> str:
    """Кладём JSON в tg_inbox — без 70с SSH+python на VPS."""
    task_id = str(item["id"])
    tmp = HOSHI_CORE / "data" / f"_remote_{task_id}.json"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
    remote = f"{SERVER_USER}@{SERVER_HOST}:{SERVER_HOSHI_PATH}/data/tg_inbox/{task_id}.json"
    try:
        proc = subprocess.run(
            ["rsync", "-az", "-e", _rsync_ssh(), str(tmp), remote],
            capture_output=True,
            text=True,
            timeout=90,
        )
        tmp.unlink(missing_ok=True)
        if proc.returncode != 0:
            log.warning("inbox rsync failed: %s", (proc.stderr or "")[:200])
            return ""
        return task_id
    except Exception as e:
        log.warning("inbox rsync: %s", e)
        tmp.unlink(missing_ok=True)
        return ""


def _submit_via_ssh_python(text: str, *, images: list[str] | None = None) -> dict:
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    imgs = json.dumps(images or [], ensure_ascii=False)
    remote = (
        f"cd {shlex.quote(SERVER_HOSHI_PATH)} && "
        f".venv/bin/python3 voice_server_submit.py --text-b64 {shlex.quote(payload)} "
        f"--images-json {shlex.quote(imgs)}"
    )
    proc = subprocess.run(
        _ssh_cmd(remote),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "ssh failed")[:200])
    return json.loads((proc.stdout or "").strip().splitlines()[-1])


def submit_owner_task(text: str, *, images: list[str] | None = None, wait_timeout: float = 150) -> str:
    body = (text or "").strip()
    if not body:
        return ""

    task_id = ""
    title = ""
    item = _build_routed_task_item(body, images=images)
    if item:
        task_id = _enqueue_on_server_via_rsync(item)
        title = str((item.get("extra") or {}).get("target_chat_title") or "")
    if not task_id:
        try:
            data = _submit_via_ssh_python(body, images=images)
        except subprocess.TimeoutExpired:
            log.warning("server submit timeout")
            return "Сервер не ответил, хозяин."
        except Exception as e:
            log.warning("server submit: %s", e)
            return "Не вышло достучаться до сервера."
        if not data.get("ok"):
            return "Не смогла поставить задачу."
        task_id = str(data.get("task_id") or "")
        title = str(data.get("target_title") or "")

    name = (title.split("(")[0].strip().split() or ["ей"])[0]
    if not task_id:
        return f"Пишу {name}."

    reply = _poll_server_outbox(task_id, timeout=wait_timeout)
    if reply:
        return _short_voice_confirm(reply, name)
    return f"Поставила в очередь для {name} — скоро ответит, хозяин."


def _poll_server_outbox(task_id: str, *, timeout: float) -> str:
    tmp = HOSHI_CORE / "data" / f"_voice_poll_{uuid.uuid4().hex[:8]}.json"
    deadline = time.monotonic() + timeout
    remote_path = f"{SERVER_HOSHI_PATH}/data/tg_outbox/{task_id}.json"
    while time.monotonic() < deadline:
        try:
            proc = subprocess.run(
                [
                    "rsync",
                    "-az",
                    "-e",
                    _rsync_ssh(),
                    f"{SERVER_USER}@{SERVER_HOST}:{remote_path}",
                    str(tmp),
                ],
                capture_output=True,
                text=True,
                timeout=45,
            )
            if proc.returncode == 0 and tmp.exists():
                item = json.loads(tmp.read_text(encoding="utf-8"))
                if item.get("status") == "done":
                    return str(item.get("reply") or "")
                if item.get("status") == "error":
                    return ""
        except Exception as e:
            log.debug("poll: %s", e)
        time.sleep(2.0)
    tmp.unlink(missing_ok=True)
    return ""


def _short_voice_confirm(reply: str, name: str) -> str:
    low = reply.lower()
    if "не удалось отправить" in low:
        return reply[:200]
    if "[[silent]]" in low and "напис" not in low:
        return f"Написала {name}."
    if "скопируй" in low or "ask mode" in low or "agent mode" in low:
        return f"Написала {name}."
    if reply.strip().lower().startswith("написала "):
        return reply.strip()[:400]
    for line in reply.splitlines():
        t = line.strip().strip("*")
        if t and len(t) < 200 and not t.startswith("Хозяин"):
            return t[:180]
    return f"Готово, написала {name}."
