#!/usr/bin/env python3
"""Безопасный перезапуск и уведомление после старта bridge."""
from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

from config import DATA, OWNER_ID, ROOT

POST_RESTART = DATA / "post_restart.json"
PENDING_RESTART = DATA / "pending_restart.json"
RESTART_LOCK = ROOT / ".restart.lock"
RESTART_DEBOUNCE_SEC = 45
_PROCESS_START = time.time()
_SKIP_CODE_DIRS = {".venv", "data", "__pycache__", ".git"}

# Модули, которые импортирует bridge — им нужен перезапуск bridge.
_BRIDGE_PY = frozenset(
    {
        "tg_bridge.py",
        "chat_watcher.py",
        "user_client.py",
        "user_outbox.py",
        "chat_router.py",
        "voice_delivery.py",
        "voice_tts.py",
        "notify.py",
        "tg_media.py",
        "health_watch.py",
        "personas.py",
        "text_format.py",
        "video_download.py",
        "news_poster.py",
    }
)


def process_start_time() -> float:
    return _PROCESS_START


def set_process_start_time(ts: float | None = None) -> None:
    global _PROCESS_START
    _PROCESS_START = ts if ts is not None else time.time()


def _py_changed(names: frozenset[str], *, since: float) -> bool:
    for path in ROOT.rglob("*.py"):
        if _SKIP_CODE_DIRS.intersection(path.parts):
            continue
        if path.name not in names:
            continue
        try:
            if path.stat().st_mtime > since + 0.5:
                return True
        except OSError:
            continue
    return False


def bridge_code_changed_since(start: float | None = None) -> bool:
    since = _PROCESS_START if start is None else start
    return _py_changed(_BRIDGE_PY, since=since)


def code_changed_since(start: float | None = None) -> bool:
    """True если .py в проекте менялись после старта процесса."""
    since = _PROCESS_START if start is None else start
    for path in ROOT.rglob("*.py"):
        if _SKIP_CODE_DIRS.intersection(path.parts):
            continue
        try:
            if path.stat().st_mtime > since + 0.5:
                return True
        except OSError:
            continue
    return False


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def write_post_restart(
    *,
    chat_id: int | None = None,
    task_id: str = "",
    note: str = "",
    external_delivery: bool = False,
) -> None:
    payload = {
        "chat_id": chat_id or OWNER_ID,
        "task_id": task_id,
        "note": note[:2000],
        "external_delivery": external_delivery,
        "created_at": _now(),
    }
    POST_RESTART.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def consume_post_restart() -> dict | None:
    if not POST_RESTART.exists():
        return None
    try:
        data = json.loads(POST_RESTART.read_text(encoding="utf-8"))
    except Exception:
        data = None
    POST_RESTART.unlink(missing_ok=True)
    return data


def _queue_busy() -> bool:
    try:
        from config import INBOX
        from storage import list_queue_items

        if list_queue_items(INBOX, status="in_progress"):
            return True
        if list_queue_items(INBOX, status="pending"):
            return True
    except Exception:
        pass
    return False


def write_pending_restart(payload: dict) -> None:
    payload = {**payload, "created_at": _now()}
    PENDING_RESTART.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def flush_pending_restart() -> bool:
    """Перезапуск после code_fix, когда очередь опустела."""
    if not PENDING_RESTART.exists():
        return False
    try:
        payload = json.loads(PENDING_RESTART.read_text(encoding="utf-8"))
    except Exception:
        PENDING_RESTART.unlink(missing_ok=True)
        return False
    created = payload.get("created_at") or ""
    try:
        age = (datetime.now() - datetime.fromisoformat(created)).total_seconds()
    except Exception:
        age = 999
    urgent = (
        payload.get("bridge_restart")
        or payload.get("external_delivery")
        or payload.get("reason") == "stale_reaction_code"
        or age > 90
    )
    if _queue_busy() and not urgent:
        return False
    PENDING_RESTART.unlink(missing_ok=True)
    return _run_restart_cmd(payload)


def restart_recently() -> bool:
    if not RESTART_LOCK.exists():
        return False
    try:
        ts = float(RESTART_LOCK.read_text(encoding="utf-8").strip())
        return (time.time() - ts) < RESTART_DEBOUNCE_SEC
    except Exception:
        return False


def mark_restart() -> None:
    RESTART_LOCK.write_text(str(time.time()), encoding="utf-8")


def _restart_cmd(payload: dict) -> list[str]:
    ctl = ROOT / "hoshi_ctl.sh"
    scope = payload.get("scope") or "safe"
    reason = payload.get("reason") or ""
    code_fix = reason == "code_fix"
    pending_delivery = DATA / "pending_external_delivery.json"

    if scope == "bridge":
        return ["/bin/bash", str(ctl), "bridge", "restart"]
    # code_fix: никогда не убивать bridge полным restart — только safe или daemon
    if code_fix:
        if payload.get("bridge_restart") or pending_delivery.exists():
            return ["/bin/bash", str(ctl), "safe"]
        return ["/bin/bash", str(ctl), "daemon", "restart"]
    if (
        payload.get("bridge_restart")
        or pending_delivery.exists()
        or reason == "stale_reaction_code"
    ):
        return ["/bin/bash", str(ctl), "safe"]
    if scope == "daemon":
        return ["/bin/bash", str(ctl), "daemon", "restart"]
    return ["/bin/bash", str(ctl), "safe"]


def _run_restart_cmd(payload: dict) -> bool:
    mark_restart()
    cmd = _restart_cmd(payload)
    try:
        subprocess.run(
            cmd,
            cwd=str(ROOT),
            check=False,
            timeout=120,
            capture_output=True,
            text=True,
        )
    except Exception:
        pass
    time.sleep(1)
    ensure_bridge_running()
    ensure_daemon_running()
    return bridge_running() and daemon_running()


def schedule_restart(
    *,
    chat_id: int | None = None,
    task_id: str = "",
    note: str = "",
    scope: str = "safe",
    force: bool = False,
    reason: str = "",
    external_delivery: bool = False,
) -> bool:
    """Планирует перезапуск. Откладывает, если очередь занята — Юна не «умирает» mid-chat."""
    pending_delivery = DATA / "pending_external_delivery.json"
    code_fix = reason == "code_fix"
    stale_reaction = reason == "stale_reaction_code"
    must_restart = force or pending_delivery.exists() or code_fix or stale_reaction
    if not must_restart and restart_recently():
        return False

    payload = {
        "scope": scope,
        "reason": reason,
        "chat_id": chat_id or OWNER_ID,
        "task_id": task_id,
        "note": note[:2000],
        "external_delivery": external_delivery,
        "bridge_restart": bridge_code_changed_since(process_start_time()),
    }
    if note or task_id:
        write_post_restart(
            chat_id=chat_id,
            task_id=task_id,
            note=note,
            external_delivery=external_delivery,
        )

    # bridge/user_outbox правки требуют полного restart — не откладывать из‑за in_progress.
    urgent = (
        stale_reaction
        or pending_delivery.exists()
        or payload.get("bridge_restart")
        or external_delivery
    )
    if _queue_busy() and code_fix and not urgent:
        write_pending_restart(payload)
        return True

    return _run_restart_cmd(payload)


def bridge_running() -> bool:
    try:
        out = subprocess.run(
            ["pgrep", "-f", "tg_bridge.py"],
            capture_output=True,
            text=True,
            check=False,
        )
        return bool(out.stdout.strip())
    except Exception:
        return False


def daemon_running() -> bool:
    try:
        out = subprocess.run(
            ["pgrep", "-f", "hoshi_daemon.py"],
            capture_output=True,
            text=True,
            check=False,
        )
        return bool(out.stdout.strip())
    except Exception:
        return False


def ensure_bridge_running() -> bool:
    if bridge_running():
        return True
    ctl = ROOT / "hoshi_ctl.sh"
    subprocess.run(
        ["/bin/bash", str(ctl), "bridge", "start"],
        cwd=str(ROOT),
        check=False,
    )
    time.sleep(1)
    return bridge_running()


def ensure_daemon_running() -> bool:
    if daemon_running():
        return True
    ctl = ROOT / "hoshi_ctl.sh"
    subprocess.run(
        ["/bin/bash", str(ctl), "daemon", "start"],
        cwd=str(ROOT),
        check=False,
    )
    time.sleep(1)
    return daemon_running()


def heavy_worker_running(*, expected: int | None = None) -> bool:
    try:
        out = subprocess.run(
            ["pgrep", "-f", "hoshi_heavy_worker.py"],
            capture_output=True,
            text=True,
            check=False,
        )
        count = len([ln for ln in out.stdout.splitlines() if ln.strip()])
        if expected is None:
            from config import HOSHI_HEAVY_WORKERS

            expected = HOSHI_HEAVY_WORKERS
        return count >= expected
    except Exception:
        return False


def ensure_heavy_worker_running() -> bool:
    from config import HOSHI_HEAVY_WORKERS

    if heavy_worker_running(expected=HOSHI_HEAVY_WORKERS):
        return True
    ctl = ROOT / "hoshi_ctl.sh"
    subprocess.run(
        ["/bin/bash", str(ctl), "heavy", "start"],
        cwd=str(ROOT),
        check=False,
    )
    time.sleep(1)
    return heavy_worker_running(expected=HOSHI_HEAVY_WORKERS)


def iris_daemon_running() -> bool:
    try:
        out = subprocess.run(
            ["pgrep", "-f", "iris_daemon.py"],
            capture_output=True,
            text=True,
            check=False,
        )
        return bool(out.stdout.strip())
    except Exception:
        return False


def ensure_iris_daemon_running() -> bool:
    if iris_daemon_running():
        return True
    ctl = ROOT / "hoshi_ctl.sh"
    subprocess.run(
        ["/bin/bash", str(ctl), "iris", "start"],
        cwd=str(ROOT),
        check=False,
    )
    time.sleep(1)
    return iris_daemon_running()
