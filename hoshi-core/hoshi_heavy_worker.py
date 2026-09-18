#!/usr/bin/env python3
"""Heavy worker: исполняет готовые job от light-демона, без переосмысления запроса."""
from __future__ import annotations

import atexit
import json
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

from config import (
    HEAVY_INBOX,
    HEAVY_OUTBOX,
    HEAVY_STATUS,
    HOSHI_HEAVY_WORKERS,
    INBOX,
    LOG,
    ROOT,
    heavy_worker_lock_path,
)
from heavy_queue import list_heavy_items, move_heavy_item, try_claim_pending_job, update_heavy_item
from storage import update_queue_item


def log(msg: str, *, worker_id: str = "?") -> None:
    line = f"[heavy-{worker_id} {datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def save_status(**kw) -> None:
    data: dict = {}
    if HEAVY_STATUS.exists():
        try:
            data = json.loads(HEAVY_STATUS.read_text(encoding="utf-8"))
        except Exception:
            pass
    data.update(kw)
    data["updated_at"] = datetime.now().isoformat()
    HEAVY_STATUS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def acquire_lock(*, worker_id: int) -> bool:
    lock_path = heavy_worker_lock_path(worker_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.exists():
        try:
            old = int(lock_path.read_text(encoding="utf-8").strip())
            os.kill(old, 0)
            return False
        except (ProcessLookupError, ValueError, OSError):
            lock_path.unlink(missing_ok=True)
    lock_path.write_text(str(os.getpid()), encoding="utf-8")
    return True


def release_lock(*, worker_id: int) -> None:
    heavy_worker_lock_path(worker_id).unlink(missing_ok=True)


def recover_stale_heavy_in_progress(*, max_age_sec: int = 1800, worker_id: str = "?") -> int:
    """Сбрасывает зависшие heavy in_progress (worker упал mid-job)."""
    from datetime import datetime

    n = 0
    for job in list_heavy_items(HEAVY_INBOX, status="in_progress"):
        stamp = job.get("updated_at") or job.get("received_at") or ""
        try:
            age = (datetime.now() - datetime.fromisoformat(stamp)).total_seconds()
        except Exception:
            age = max_age_sec + 1
        if age < max_age_sec:
            continue
        update_heavy_item(
            HEAVY_INBOX,
            job["id"],
            status="pending",
            note="recovered stale heavy in_progress",
        )
        n += 1
        log(f"Job {job['id']}: recovered stale heavy in_progress", worker_id=worker_id)
    return n


def pick_job(*, worker_id: str) -> dict | None:
    from hoshi_daemon import (
        _is_futile_transient_heavy_job,
        cursor_cooldown_remaining,
        finish_skipped_transient_code_fix,
    )

    job = try_claim_pending_job(worker_id=worker_id)
    if not job:
        if cursor_cooldown_remaining() > 0:
            return None
        pending = list_heavy_items(HEAVY_INBOX, status="pending")
        if not pending:
            return None
        pending.sort(key=lambda x: x.get("received_at") or "")
        for job in pending:
            if not _is_futile_transient_heavy_job(job):
                continue
            job_id = job["id"]
            parent_id = job.get("parent_task_id") or ""
            move_heavy_item(
                HEAVY_INBOX,
                HEAVY_OUTBOX,
                job_id,
                status="error",
                note="skipped: cursor API limit",
            )
            if parent_id:
                parent_path = INBOX / f"{parent_id}.json"
                if parent_path.exists():
                    try:
                        parent_item = json.loads(parent_path.read_text(encoding="utf-8"))
                        finish_skipped_transient_code_fix(parent_item, source="heavy")
                    except Exception:
                        pass
            log(f"Job {job_id}: skipped futile transient code_fix", worker_id=worker_id)
        return None
    if _is_futile_transient_heavy_job(job):
        job_id = job["id"]
        parent_id = job.get("parent_task_id") or ""
        move_heavy_item(
            HEAVY_INBOX,
            HEAVY_OUTBOX,
            job_id,
            status="error",
            note="skipped: cursor API limit",
        )
        if parent_id:
            parent_path = INBOX / f"{parent_id}.json"
            if parent_path.exists():
                try:
                    parent_item = json.loads(parent_path.read_text(encoding="utf-8"))
                    finish_skipped_transient_code_fix(parent_item, source="heavy")
                except Exception:
                    pass
        log(f"Job {job_id}: skipped futile transient code_fix", worker_id=worker_id)
        return None
    return job


def _fail_parent(parent_task_id: str, note: str) -> None:
    path = INBOX / f"{parent_task_id}.json"
    if not path.exists():
        return
    update_queue_item(
        INBOX,
        parent_task_id,
        status="pending",
        note=f"heavy failed: {note[:200]}",
    )


def process_job(job: dict, *, worker_id: str) -> None:
    from config import CURSOR_HEAVY_MODEL
    from hoshi_daemon import (
        CursorAuthError,
        CursorInterruptedError,
        CursorTransientError,
        _is_futile_transient_heavy_job,
        _set_cursor_cooldown,
        _wait_cursor_cooldown,
        append_agent_message,
        finish_skipped_transient_code_fix,
        run_cursor,
    )
    from notify import WorkIndicator

    job_id = job["id"]
    parent_id = job.get("parent_task_id") or ""
    prompt = job.get("prompt") or ""
    kind = job.get("kind") or "agent_message"
    parent_item = job.get("parent_item") or {}
    chat_id = parent_item.get("chat_id")
    draft_id = parent_item.get("draft_id")
    extra = parent_item.get("extra") or {}
    indicator = WorkIndicator(
        chat_id,
        draft_id,
        external=extra.get("delivery") == "external_telegram",
    )
    intent = job.get("intent") or ""

    if _is_futile_transient_heavy_job(job):
        move_heavy_item(
            HEAVY_INBOX,
            HEAVY_OUTBOX,
            job_id,
            status="error",
            note="skipped: cursor API limit",
        )
        if parent_id:
            parent_path = INBOX / f"{parent_id}.json"
            if parent_path.exists():
                try:
                    parent_item = json.loads(parent_path.read_text(encoding="utf-8"))
                    finish_skipped_transient_code_fix(parent_item, source="heavy")
                except Exception:
                    pass
        log(f"Job {job_id}: skipped futile transient code_fix", worker_id=worker_id)
        return

    if intent == "photo_edit":
        from hoshi_daemon import _try_photo_edit_fast_path

        head = (parent_item.get("text") or "").split("\n---\n")[0].strip()
        try:
            indicator.start()
            photo_reply, _updated = _try_photo_edit_fast_path(
                parent_item,
                kind=kind,
                head=head,
            )
        except Exception as e:
            photo_reply = None
            log(f"Job {job_id}: photo_edit failed: {e}", worker_id=worker_id)
        finally:
            indicator.stop()
        if photo_reply:
            append_agent_message("assistant", photo_reply)
            move_heavy_item(
                HEAVY_INBOX,
                HEAVY_OUTBOX,
                job_id,
                status="done",
                reply=photo_reply,
            )
            log(f"Job {job_id}: photo_edit done parent={parent_id}", worker_id=worker_id)
            return
        log(f"Job {job_id}: photo_edit fallback → cursor", worker_id=worker_id)

    if not prompt.strip():
        move_heavy_item(
            HEAVY_INBOX,
            HEAVY_OUTBOX,
            job_id,
            status="error",
            note="empty prompt",
        )
        if parent_id:
            _fail_parent(parent_id, "empty prompt")
        log(f"Job {job_id}: empty prompt", worker_id=worker_id)
        return

    log(f"Job {job_id}: start parent={parent_id} intent={intent}", worker_id=worker_id)
    try:
        indicator.start()
        heavy_model = CURSOR_HEAVY_MODEL or None
        reply = run_cursor(prompt, model=heavy_model, owner="heavy")
        append_agent_message("assistant", reply)

        if kind == "code_fix":
            from code_fix_verify import syntax_errors_text

            syn_err = syntax_errors_text()
            if syn_err:
                log(f"Job {job_id}: syntax retry", worker_id=worker_id)
                retry_prompt = (
                    f"{prompt}\n\n"
                    "**Автопроверка синтаксиса не прошла — исправь до рабочего состояния:**\n"
                    f"{syn_err}\n"
                )
                reply = run_cursor(retry_prompt, model=heavy_model, owner="heavy")
                append_agent_message("assistant", reply)

        move_heavy_item(
            HEAVY_INBOX,
            HEAVY_OUTBOX,
            job_id,
            status="done",
            reply=reply,
        )
        log(f"Job {job_id}: done parent={parent_id}", worker_id=worker_id)
    except CursorAuthError as e:
        move_heavy_item(
            HEAVY_INBOX,
            HEAVY_OUTBOX,
            job_id,
            status="error",
            note=str(e)[:400],
        )
        if parent_id:
            update_queue_item(
                INBOX,
                parent_id,
                status="pending",
                note="heavy cursor auth retry",
            )
        log(f"Job {job_id}: cursor auth error", worker_id=worker_id)
    except CursorInterruptedError:
        move_heavy_item(
            HEAVY_INBOX,
            HEAVY_OUTBOX,
            job_id,
            status="error",
            note="cursor interrupted",
        )
        if parent_id:
            update_queue_item(
                INBOX,
                parent_id,
                status="pending",
                note="heavy interrupted retry",
            )
        log(f"Job {job_id}: interrupted", worker_id=worker_id)
    except CursorTransientError as e:
        _set_cursor_cooldown(180)
        move_heavy_item(
            HEAVY_INBOX,
            HEAVY_OUTBOX,
            job_id,
            status="error",
            note=str(e)[:400],
        )
        if parent_id:
            update_queue_item(
                INBOX,
                parent_id,
                status="pending",
                note="heavy cursor transient retry",
            )
        log(f"Job {job_id}: cursor transient error", worker_id=worker_id)
    except Exception as e:
        move_heavy_item(
            HEAVY_INBOX,
            HEAVY_OUTBOX,
            job_id,
            status="error",
            note=str(e)[:400],
        )
        if parent_id:
            _fail_parent(parent_id, str(e))
        log(f"Job {job_id}: error {e}", worker_id=worker_id)
    finally:
        indicator.stop()


def _parse_worker_id() -> int:
    if len(sys.argv) > 1:
        try:
            wid = int(sys.argv[1])
            if 1 <= wid <= HOSHI_HEAVY_WORKERS:
                return wid
        except ValueError:
            pass
    env = os.environ.get("HOSHI_HEAVY_WORKER_ID", "").strip()
    if env.isdigit():
        wid = int(env)
        if 1 <= wid <= HOSHI_HEAVY_WORKERS:
            return wid
    return 1


def main() -> None:
    worker_id = _parse_worker_id()
    worker_tag = str(worker_id)
    if not acquire_lock(worker_id=worker_id):
        log("Another heavy worker with this id is already running", worker_id=worker_tag)
        sys.exit(0)

    from hoshi_daemon import cleanup_cursor_agents, cursor_cooldown_remaining, purge_futile_transient_code_fix_queue

    atexit.register(
        lambda: cleanup_cursor_agents(reason=f"heavy worker {worker_tag} exit", owner="heavy")
    )
    atexit.register(lambda: release_lock(worker_id=worker_id))
    cleanup_cursor_agents(reason=f"heavy worker {worker_tag} startup", owner="heavy")
    if worker_id == 1:
        purged = purge_futile_transient_code_fix_queue()
        if purged:
            log(f"Purged {purged} futile transient code_fix item(s) on startup", worker_id=worker_tag)
        recovered = recover_stale_heavy_in_progress(worker_id=worker_tag)
        if recovered:
            log(f"Recovered {recovered} stale heavy in_progress job(s) on startup", worker_id=worker_tag)
    save_status(running=True, processed=0, last_error="", worker_id=worker_id)
    log("Heavy worker started", worker_id=worker_tag)

    processed = 0
    try:
        while True:
            wait = cursor_cooldown_remaining()
            if wait > 0:
                save_status(
                    running=True,
                    processed=processed,
                    pending=len(list_heavy_items(HEAVY_INBOX, status="pending")),
                    in_progress=len(list_heavy_items(HEAVY_INBOX, status="in_progress")),
                    last_error=f"cursor cooldown {int(wait)}s",
                )
                time.sleep(min(wait, 30))
                continue
            job = pick_job(worker_id=worker_tag)
            if not job:
                save_status(
                    running=True,
                    processed=processed,
                    pending=len(list_heavy_items(HEAVY_INBOX, status="pending")),
                    in_progress=len(list_heavy_items(HEAVY_INBOX, status="in_progress")),
                    worker_id=worker_id,
                )
                time.sleep(0.08)
                continue
            process_job(job, worker_id=worker_tag)
            processed += 1
            save_status(running=True, processed=processed, worker_id=worker_id)
    finally:
        release_lock(worker_id=worker_id)
        save_status(running=False, processed=processed, worker_id=worker_id)


if __name__ == "__main__":
    main()
