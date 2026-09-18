#!/usr/bin/env python3
"""Очередь тяжёлых задач: light-демон ставит job, heavy-worker исполняет."""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from config import HEAVY_INBOX, HEAVY_OUTBOX, HEAVY_PICK_LOCK


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def heavy_job_id() -> str:
    return f"heavy_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"


def enqueue_heavy_job(
    *,
    parent_task_id: str,
    intent: str,
    prompt: str,
    focus: dict[str, Any],
    parent_item: dict[str, Any],
) -> str:
    job_id = heavy_job_id()
    job = {
        "id": job_id,
        "parent_task_id": parent_task_id,
        "intent": intent,
        "prompt": prompt,
        "focus": focus,
        "parent_item": parent_item,
        "kind": parent_item.get("kind") or "agent_message",
        "received_at": _now(),
        "status": "pending",
        "note": "",
    }
    HEAVY_INBOX.mkdir(parents=True, exist_ok=True)
    (HEAVY_INBOX / f"{job_id}.json").write_text(
        json.dumps(job, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return job_id


def list_heavy_items(folder: Path, status: str | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not folder.exists():
        return items
    for path in sorted(folder.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if status is None or item.get("status") == status:
            items.append(item)
    return items


def update_heavy_item(folder: Path, job_id: str, **fields: Any) -> None:
    path = folder / f"{job_id}.json"
    if not path.exists():
        return
    item = json.loads(path.read_text(encoding="utf-8"))
    item.update(fields)
    item["updated_at"] = _now()
    path.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")


def move_heavy_item(
    src: Path,
    dst: Path,
    job_id: str,
    **fields: Any,
) -> dict[str, Any] | None:
    src_path = src / f"{job_id}.json"
    if not src_path.exists():
        return None
    item = json.loads(src_path.read_text(encoding="utf-8"))
    item.update(fields)
    item["updated_at"] = _now()
    dst.mkdir(parents=True, exist_ok=True)
    (dst / f"{job_id}.json").write_text(
        json.dumps(item, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    src_path.unlink(missing_ok=True)
    return item


def pending_heavy_count() -> int:
    return len(list_heavy_items(HEAVY_INBOX, status="pending"))


class _HeavyPickLock:
    def __enter__(self):
        import fcntl

        HEAVY_PICK_LOCK.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(HEAVY_PICK_LOCK, "w")
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb):
        import fcntl

        fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        self._fh.close()
        return False


def try_claim_pending_job(*, worker_id: str) -> dict[str, Any] | None:
    """Атомарно забирает одну pending-задачу (несколько heavy-worker)."""
    with _HeavyPickLock():
        pending = list_heavy_items(HEAVY_INBOX, status="pending")
        if not pending:
            return None
        pending.sort(key=lambda x: x.get("received_at") or "")
        for job in pending:
            path = HEAVY_INBOX / f"{job['id']}.json"
            if not path.exists():
                continue
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if item.get("status") != "pending":
                continue
            item["status"] = "in_progress"
            item["claimed_by"] = worker_id
            item["updated_at"] = _now()
            path.write_text(
                json.dumps(item, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return item
    return None
