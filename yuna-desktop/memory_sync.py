#!/usr/bin/env python3
"""
Единая память: синхронизация agent_sessions, settings, ideas между ПК и сервером.
Сервер — источник истины при конфликте настроек; история диалога сливается по timestamp.
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from desk_config import (
    HOSHI_CORE,
    MEMORY_FILES,
    MEMORY_SYNC_STATE,
    SERVER_HOST,
    SERVER_HOSHI_PATH,
    SERVER_SSH_PASS,
    SERVER_USER,
)

log = logging.getLogger("yuna.memory_sync")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _rsync_ssh_shell() -> str:
    if SERVER_SSH_PASS:
        return f"sshpass -p {shlex.quote(SERVER_SSH_PASS)} ssh -F /dev/null -o StrictHostKeyChecking=no -o ConnectTimeout=15"
    return "ssh -F /dev/null -o StrictHostKeyChecking=no -o ConnectTimeout=15"


def _remote(spec: str) -> str:
    return f"{SERVER_USER}@{SERVER_HOST}:{SERVER_HOSHI_PATH}/{spec}"


def _run(cmd: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)


def _load_state() -> dict[str, Any]:
    if not MEMORY_SYNC_STATE.exists():
        return {"last_sync": "", "last_error": ""}
    try:
        return json.loads(MEMORY_SYNC_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {"last_sync": "", "last_error": ""}


def _save_state(**fields: Any) -> None:
    state = _load_state()
    state.update(fields)
    state["last_sync"] = _now()
    MEMORY_SYNC_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _merge_messages(local: list[dict], remote: list[dict]) -> list[dict]:
    seen: set[str] = set()
    merged: list[dict] = []
    for msg in sorted([*local, *remote], key=lambda m: m.get("at") or ""):
        key = f"{msg.get('role')}|{msg.get('at')}|{(msg.get('text') or '')[:200]}"
        if key in seen:
            continue
        seen.add(key)
        merged.append(msg)
    return merged[-500:]


def _merge_agent_session(local: dict, remote: dict) -> dict:
    out = dict(remote)
    out["messages"] = _merge_messages(
        list(local.get("messages") or []),
        list(remote.get("messages") or []),
    )
    out["updated_at"] = max(local.get("updated_at") or "", remote.get("updated_at") or "") or _now()
    out["user_id"] = local.get("user_id") or remote.get("user_id")
    if local.get("pending_images") and not remote.get("pending_images"):
        out["pending_images"] = local["pending_images"]
    return out


def _deep_merge_local_wins(remote: dict, local: dict) -> dict:
    """Глубокое слияние словарей; при конфликте побеждает локальное значение."""
    out = dict(remote or {})
    for k, v in (local or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge_local_wins(out[k], v)
        else:
            out[k] = v
    return out


def _merge_settings(local: dict, remote: dict) -> dict:
    out = dict(remote)
    for key, val in local.items():
        if key == "desktop":
            # Настройки рабочего стола (голос, тема, поведение) — локальные авторитетны,
            # чтобы выбор голоса/кастомизация никогда не сбрасывались сервером.
            out["desktop"] = _deep_merge_local_wins(remote.get("desktop") or {}, val or {})
        elif key not in out:
            out[key] = val
    out["updated_at"] = _now()
    return out


def _merge_ideas(local: dict, remote: dict) -> dict:
    seen: set[str] = set()
    ideas: list[dict] = []
    for idea in list(remote.get("ideas") or []) + list(local.get("ideas") or []):
        iid = str(idea.get("id") or idea.get("text", "")[:80])
        if iid in seen:
            continue
        seen.add(iid)
        ideas.append(idea)
    return {"ideas": ideas[-200:], "updated_at": _now()}


def _merge_local_context(local: dict, remote: dict) -> dict:
    projects: dict[str, dict] = {}
    for src in (remote, local):
        for p in src.get("projects") or []:
            pid = str(p.get("id") or p.get("path") or p.get("name") or "")
            if pid:
                projects[pid] = p
    remote_notes = list(remote.get("notes") or [])
    extra = [n for n in (local.get("notes") or []) if n not in remote_notes]
    return {
        "updated_at": _now(),
        "active_project": local.get("active_project") or remote.get("active_project"),
        "projects": list(projects.values())[-20:],
        "last_screen": local.get("last_screen") or remote.get("last_screen"),
        "notes": (remote_notes + extra)[-50:],
    }


def _merge_file(rel: str, local_path: Path, remote_path: Path) -> None:
    if rel == "data/agent_sessions" and local_path.suffix == ".json":
        merged = _merge_agent_session(_read_json(local_path, {"messages": []}), _read_json(remote_path, {"messages": []}))
        _write_json(local_path, merged)
        _write_json(remote_path, merged)
        return
    if rel == "data/settings.json":
        _write_json(local_path, _merge_settings(_read_json(local_path, {}), _read_json(remote_path, {})))
        return
    if rel == "data/saved_ideas.json":
        _write_json(local_path, _merge_ideas(_read_json(local_path, {"ideas": []}), _read_json(remote_path, {"ideas": []})))
        return
    if rel == "data/local_context.json":
        _write_json(local_path, _merge_local_context(_read_json(local_path, {}), _read_json(remote_path, {})))
        return
    if rel == "data/voice_tg_feed.json":
        remote = _read_json(remote_path, {"chats": {}})
        local = _read_json(local_path, {"chats": {}})
        chats = dict(remote.get("chats") or {})
        for cid, entry in (local.get("chats") or {}).items():
            if cid not in chats:
                chats[cid] = entry
        merged = {"chats": chats, "updated_at": _now()}
        _write_json(local_path, merged)
        return
    if not local_path.exists():
        local_path.write_bytes(remote_path.read_bytes())


def _rsync_pull(remote_spec: str, local_dir: Path) -> bool:
    local_dir.mkdir(parents=True, exist_ok=True)
    proc = _run(
        ["rsync", "-az", "-e", _rsync_ssh_shell(), _remote(remote_spec), str(local_dir) + "/"],
    )
    if proc.returncode != 0 and "No such file" not in (proc.stderr or ""):
        log.error("rsync pull %s: %s", remote_spec, proc.stderr)
        return False
    return True


def _rsync_push(local_path: Path, remote_spec: str) -> bool:
    if not local_path.exists():
        return True
    src = str(local_path) + ("/" if local_path.is_dir() else "")
    proc = _run(["rsync", "-az", "-e", _rsync_ssh_shell(), src, _remote(remote_spec)])
    if proc.returncode != 0:
        log.error("rsync push %s: %s", remote_spec, proc.stderr)
        return False
    return True


def pull_sessions_fast(timeout: int = 8) -> bool:
    """Быстрая подтяжка сессий и TG-ленты с сервера перед голосовым ответом."""
    local = HOSHI_CORE / "data/agent_sessions"
    local.mkdir(parents=True, exist_ok=True)
    proc = _run(
        ["rsync", "-az", "-e", _rsync_ssh_shell(), _remote("data/agent_sessions/"), str(local) + "/"],
        timeout=timeout,
    )
    ok = proc.returncode == 0
    feed_local = HOSHI_CORE / "data/voice_tg_feed.json"
    feed_tmp = feed_local.with_suffix(".tmp.json")
    proc2 = _run(
        ["rsync", "-az", "-e", _rsync_ssh_shell(), _remote("data/voice_tg_feed.json"), str(feed_tmp)],
        timeout=timeout,
    )
    if proc2.returncode == 0 and feed_tmp.exists():
        _merge_file("data/voice_tg_feed.json", feed_local, feed_tmp)
        feed_tmp.unlink(missing_ok=True)
    elif proc.returncode != 0:
        log.debug("fast pull: %s", (proc.stderr or "")[:120])
    return ok


def pull_from_server() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        ok = True

        remote_sess = tmp_path / "agent_sessions"
        if _rsync_pull("data/agent_sessions/", remote_sess):
            local_sess = HOSHI_CORE / "data/agent_sessions"
            local_sess.mkdir(parents=True, exist_ok=True)
            for rf in remote_sess.glob("*.json"):
                lf = local_sess / rf.name
                if lf.exists():
                    _merge_file("data/agent_sessions", lf, rf)
                else:
                    lf.write_bytes(rf.read_bytes())

        for rel in ("data/settings.json", "data/saved_ideas.json", "data/local_context.json", "data/voice_tg_feed.json", "data/game_memory.json"):
            remote_tmp = tmp_path / rel.replace("/", "_")
            remote_tmp.parent.mkdir(parents=True, exist_ok=True)
            proc = _run(["rsync", "-az", "-e", _rsync_ssh_shell(), _remote(rel), str(remote_tmp)])
            if proc.returncode != 0:
                if "No such file" in (proc.stderr or ""):
                    continue
                ok = False
                log.error("pull %s: %s", rel, proc.stderr)
                continue
            local_target = HOSHI_CORE / rel
            if local_target.exists():
                _merge_file(rel, local_target, remote_tmp)
            else:
                local_target.parent.mkdir(parents=True, exist_ok=True)
                local_target.write_bytes(remote_tmp.read_bytes())

    if ok:
        _save_state(last_error="")
    else:
        _save_state(last_error="pull partial failure")
    return ok


def push_to_server() -> bool:
    ok = True
    ok = _rsync_push(HOSHI_CORE / "data/agent_sessions", "data/agent_sessions/") and ok
    for rel in ("data/settings.json", "data/saved_ideas.json", "data/local_context.json", "data/game_memory.json"):
        ok = _rsync_push(HOSHI_CORE / rel, rel) and ok
    if ok:
        _save_state(last_error="")
    return ok


def sync_bidirectional() -> bool:
    pull_ok = pull_from_server()
    push_ok = push_to_server()
    return pull_ok and push_ok


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(0 if sync_bidirectional() else 1)
