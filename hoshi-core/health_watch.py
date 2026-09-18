#!/usr/bin/env python3
"""Проактивный мониторинг: находит проблемы и предлагает исправления владельцу."""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import subprocess

from config import DATA, ERRORS, INBOX, LOG, OWNER_ID, ROOT, STATUS
from storage import list_queue_items, load_settings

log = logging.getLogger("hoshi.health")

PROPOSALS = DATA / "fix_proposals.json"
_STATE = DATA / "health_state.json"
_BRIDGE_LOG = ROOT / "bridge.log"
_DAEMON_LOG = LOG

_RUNTIME_ERR_MARKERS = (
    " error:",
    "traceback",
    "nameerror",
    "typeerror",
    "valueerror",
    "attributeerror",
    "keyerror",
    "importerror",
    "syntaxerror",
    "unhandled worker error",
    "external message handler failed",
)

_LOG_META_SKIP = (
    "health proposal:",
    "runtime error proposal sent:",
    "skipped stale cursor auth",
    "auto-healed:",
)

PROJECT_IDEAS = (
    "Автопостинг по расписанию: 3 новости в день из заданных источников.",
    "Дайджест аниме-новостей раз в неделю с картинкой и ссылками.",
    "Реакции-эмодзи на ключевые слова в чате с Легой (без текста).",
    "Команда «черновик поста» — Hoshi собирает пост из нескольких источников.",
    "Мониторинг цен на рекламу на биржах из настроек.",
    "Дашборд SAO VPN: MRR, конверсия триал→месяц, алерт если сутки без продаж.",
    "Авто-напоминание в SAO боте за 2 дня до конца триала.",
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load_proposals() -> dict[str, Any]:
    if not PROPOSALS.exists():
        return {"items": {}}
    try:
        return json.loads(PROPOSALS.read_text(encoding="utf-8"))
    except Exception:
        return {"items": {}}


def _save_proposals(data: dict[str, Any]) -> None:
    PROPOSALS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_state() -> dict[str, Any]:
    if not _STATE.exists():
        return {}
    try:
        return json.loads(_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(data: dict[str, Any]) -> None:
    _STATE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _process_running(script_name: str) -> bool:
    try:
        out = subprocess.run(
            ["pgrep", "-f", script_name],
            capture_output=True,
            text=True,
            check=False,
        )
        return bool(out.stdout.strip())
    except Exception:
        return False


def _error_fingerprint(text: str) -> str:
    cleaned = re.sub(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}", "", text or "")
    cleaned = re.sub(r"task \d{8}_\d{6}_[a-f0-9]+", "task", cleaned, flags=re.I)
    cleaned = re.sub(r"chat=\d+", "chat", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().lower()[:160]
    return cleaned or "unknown"


def _is_cursor_auth_issue(text: str) -> bool:
    try:
        from hoshi_daemon import is_cursor_auth_error

        return is_cursor_auth_error(text)
    except Exception:
        low = (text or "").lower()
        return (
            "authentication required" in low
            or "stored authentication is invalid" in low
            or "agent login" in low
            or "cursor_api_key" in low
        )


def _is_cursor_interrupted_error(text: str) -> bool:
    """Обрыв cursor-agent с частичным ответом — не баг кода."""
    blob = (text or "").strip()
    low = blob.lower()
    if "cursor-agent interrupted" in low or "cursor interrupted" in low:
        return True
    if "cursor-agent failed:" not in blob:
        return False
    try:
        from hoshi_daemon import _is_partial_assistant_reply

        detail = blob.split("cursor-agent failed:", 1)[-1].strip()
        return _is_partial_assistant_reply(detail)
    except Exception:
        return False


def _is_cursor_transient_issue(text: str) -> bool:
    try:
        from hoshi_daemon import is_cursor_transient_error

        return is_cursor_transient_error(text)
    except Exception:
        low = (text or "").lower()
        return "resource_exhausted" in low or "provider error" in low


def _issue_for_error(*, code: str, title: str, detail: str) -> dict[str, str]:
    if _is_cursor_auth_issue(detail):
        return {
            "code": f"cursor_auth:{_error_fingerprint(detail)}",
            "title": "cursor-agent: нужна авторизация",
            "detail": detail,
            "action": "cursor_auth_retry",
        }
    if _is_cursor_interrupted_error(detail):
        return {
            "code": f"cursor_interrupted:{_error_fingerprint(detail)}",
            "title": "cursor-agent прерван (повтор в очереди)",
            "detail": detail[:400],
            "action": "recover_queue",
        }
    if _is_cursor_transient_issue(detail):
        return {
            "code": f"cursor_transient:{_error_fingerprint(detail)}",
            "title": "Cursor API временно недоступен",
            "detail": detail[:400],
            "action": "cursor_transient_retry",
        }
    return {
        "code": code,
        "title": title,
        "detail": detail,
        "action": "code_fix_error",
    }


def _parse_log_ts(line: str) -> datetime | None:
    m = re.match(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", line or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _log_line_recent(line: str, *, max_age_sec: int = 1800) -> bool:
    ts = _parse_log_ts(line)
    if not ts:
        return True
    return (datetime.now() - ts).total_seconds() <= max_age_sec


def _is_task_recent(task: dict, *, max_age_sec: int = 1800) -> bool:
    ts = task.get("updated_at") or task.get("received_at") or ""
    if not ts:
        return False
    try:
        return (datetime.now() - datetime.fromisoformat(ts)).total_seconds() <= max_age_sec
    except (TypeError, ValueError):
        return False


def _cursor_auth_currently_ok() -> bool:
    try:
        from hoshi_daemon import verify_cursor_auth

        return bool(verify_cursor_auth())
    except Exception:
        return False


def _maybe_recover_auth_errors() -> int:
    if not _cursor_auth_currently_ok():
        return 0
    try:
        from hoshi_daemon import recover_auth_failed_tasks

        return int(recover_auth_failed_tasks() or 0)
    except Exception:
        return 0


def _scan_log_runtime_errors(
    path: Path,
    *,
    max_lines: int = 500,
    since: datetime | None = None,
    require_error_level: bool = False,
) -> list[str]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    hits: list[str] = []
    for line in lines[-max_lines:]:
        ts = _parse_log_ts(line)
        if since:
            if not ts or ts < since:
                continue
        if not _log_line_recent(line):
            continue
        low = line.lower()
        if any(skip in low for skip in _LOG_META_SKIP):
            continue
        if require_error_level and "] ERROR " not in line:
            continue
        if any(marker in low for marker in _RUNTIME_ERR_MARKERS):
            snippet = line.strip()[-240:]
            if snippet and (not hits or hits[-1] != snippet):
                hits.append(snippet)
    return hits[-3:]


def _bridge_log_since() -> datetime | None:
    bridge_started = _load_state().get("bridge_started_at")
    if not bridge_started:
        return None
    try:
        return datetime.fromisoformat(bridge_started)
    except (TypeError, ValueError):
        return None


def _recent_bridge_errors(*, max_lines: int = 400) -> list[str]:
    return _scan_log_runtime_errors(
        _BRIDGE_LOG,
        max_lines=max_lines,
        since=_bridge_log_since(),
        require_error_level=True,
    )


def _recent_daemon_errors(*, max_lines: int = 600) -> list[str]:
    return _scan_log_runtime_errors(_DAEMON_LOG, max_lines=max_lines)


def collect_issues() -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    settings = load_settings()

    if settings.get("linked_account", {}).get("user_id"):
        try:
            from chat_watcher import watcher_status

            st = watcher_status()
            if not st.get("running"):
                issues.append(
                    {
                        "code": "watcher_down",
                        "title": "Слушатель внешних чатов не работает",
                        "detail": st.get("reason") or "watcher не запущен после старта bridge",
                        "action": "restart_watcher",
                    }
                )
        except Exception as e:
            issues.append(
                {
                    "code": "watcher_error",
                    "title": "Ошибка chat watcher",
                    "detail": str(e)[:300],
                    "action": "restart_bridge",
                }
            )

    if not _process_running("tg_bridge.py"):
        issues.append(
            {
                "code": "bridge_down",
                "title": "Bridge не запущен",
                "detail": "tg_bridge.py не отвечает",
                "action": "restart_bridge",
            }
        )

    if not _process_running("hoshi_daemon.py"):
        issues.append(
            {
                "code": "daemon_down",
                "title": "Демон не запущен",
                "detail": "hoshi_daemon.py не обрабатывает очередь",
                "action": "restart_daemon",
            }
        )

    in_progress = list_queue_items(INBOX, status="in_progress")
    old_ip = []
    daemon_up = _process_running("hoshi_daemon.py")
    agents_alive = False
    if daemon_up:
        try:
            from hoshi_daemon import (
                cursor_agents_alive,
                is_task_in_progress_stale,
            )

            agents_alive = cursor_agents_alive()
            old_ip = [t for t in in_progress if is_task_in_progress_stale(t)]
        except Exception:
            for t in in_progress:
                ts = t.get("updated_at") or t.get("received_at") or ""
                try:
                    age = (datetime.now() - datetime.fromisoformat(ts)).total_seconds()
                except (TypeError, ValueError):
                    age = 999
                if age > 900:
                    old_ip.append(t)
    else:
        for t in in_progress:
            ts = t.get("updated_at") or t.get("received_at") or ""
            try:
                age = (datetime.now() - datetime.fromisoformat(ts)).total_seconds()
            except (TypeError, ValueError):
                age = 999
            if age > 180:
                old_ip.append(t)
    fresh_ip = [t for t in in_progress if t not in old_ip]
    if old_ip and not fresh_ip and not (daemon_up and agents_alive):
        issues.append(
            {
                "code": "tasks_stuck",
                "title": "Задачи зависли в обработке",
                "detail": f"{len(old_ip)} in_progress без активного агента — сброс очереди",
                "action": "recover_queue",
            }
        )

    pending = list_queue_items(INBOX, status="pending")
    old_external = [
        t
        for t in pending
        if t.get("kind") == "external_chat"
        and (datetime.now() - datetime.fromisoformat(t.get("received_at", _now()))).total_seconds()
        > 120
    ]
    if old_external:
        issues.append(
            {
                "code": "external_stuck",
                "title": "Завис ответ во внешний чат",
                "detail": f"В очереди {len(old_external)} задач external_chat > 2 мин",
                "action": "restart_daemon",
            }
        )

    state = _load_state()
    bridge_started = state.get("bridge_started_at")
    in_grace = False
    if bridge_started:
        try:
            in_grace = (
                datetime.now() - datetime.fromisoformat(bridge_started)
            ).total_seconds() < 180
        except Exception:
            in_grace = False

    if not in_grace:
        errs = _recent_bridge_errors()
        if errs and any("event loop is closed" in e.lower() for e in errs[-3:]):
            issues.append(
                {
                    "code": "telethon_loop",
                    "title": "Конфликт Telethon event loop",
                    "detail": "Демон и bridge дергали одну сессию — перезапусти bridge",
                    "action": "restart_bridge_only",
                }
            )

    try:
        from code_fix_verify import syntax_errors_text

        syn = syntax_errors_text()
        if syn:
            issues.append(
                {
                    "code": "syntax_error",
                    "title": "Синтаксическая ошибка в коде",
                    "detail": syn[:400],
                    "action": "code_fix_error",
                }
            )
    except Exception as e:
        log.debug("syntax scan failed: %s", e)

    if STATUS.exists():
        try:
            st = json.loads(STATUS.read_text(encoding="utf-8"))
            last_err = (st.get("last_error") or "").strip()
            updated = (st.get("updated_at") or "").strip()
            recent_status = True
            if updated:
                try:
                    recent_status = (
                        datetime.now() - datetime.fromisoformat(updated)
                    ).total_seconds() <= 1800
                except ValueError:
                    recent_status = True
            if last_err and recent_status:
                if _is_cursor_auth_issue(last_err):
                    if not _cursor_auth_currently_ok():
                        issues.append(
                            _issue_for_error(
                                code=f"daemon_status:{_error_fingerprint(last_err)}",
                                title="cursor-agent: нужна авторизация",
                                detail=last_err[:400],
                            )
                        )
                elif _is_cursor_transient_issue(last_err):
                    pass
                else:
                    issues.append(
                        _issue_for_error(
                            code=f"daemon_status:{_error_fingerprint(last_err)}",
                            title="Демон сообщил об ошибке",
                            detail=last_err[:400],
                        )
                    )
        except Exception:
            pass

    _maybe_recover_auth_errors()

    error_tasks = list_queue_items(ERRORS, status="error")
    if error_tasks:
        latest = sorted(error_tasks, key=lambda t: t.get("updated_at") or t.get("received_at") or "")[-1]
        note = (latest.get("note") or latest.get("text") or "")[:300]
        if note and _is_task_recent(latest):
            if _is_cursor_auth_issue(note) and _cursor_auth_currently_ok():
                pass
            elif _is_cursor_interrupted_error(note):
                pass
            elif _is_cursor_transient_issue(note):
                pass
            else:
                issues.append(
                    _issue_for_error(
                        code=f"task_error:{_error_fingerprint(note)}",
                        title=f"Задача упала ({latest.get('id', '?')})",
                        detail=note,
                    )
                )

    for err_line in _recent_daemon_errors():
        if _is_cursor_auth_issue(err_line):
            if _cursor_auth_currently_ok():
                continue
        elif _is_cursor_interrupted_error(err_line):
            continue
        elif _is_cursor_transient_issue(err_line):
            continue
        elif "cursor_agent_env" in err_line:
            try:
                from config import cursor_agent_env

                cursor_agent_env()
                continue
            except Exception:
                pass
        issues.append(
            {
                "code": f"daemon_log:{_error_fingerprint(err_line)}",
                "title": "Ошибка в daemon.log",
                "detail": err_line,
                "action": "code_fix_error",
            }
        )

    for err_line in _recent_bridge_errors():
        if "external message handler failed" in err_line.lower():
            issues.append(
                {
                    "code": f"watcher_log:{_error_fingerprint(err_line)}",
                    "title": "Watcher упал на сообщении",
                    "detail": err_line,
                    "action": "code_fix_error",
                }
            )

    return issues


def _proposal_stale_after_fix(item: dict[str, Any]) -> bool:
    """Не поднимать снова ошибку, если её уже чинили и bridge перезапустили."""
    if item.get("status") != "executed":
        return False
    bridge_started = _load_state().get("bridge_started_at")
    closed_at = item.get("closed_at")
    if not bridge_started or not closed_at:
        return False
    try:
        return datetime.fromisoformat(closed_at) <= datetime.fromisoformat(bridge_started)
    except (TypeError, ValueError):
        return False


def create_proposal(issue: dict[str, str]) -> dict[str, Any] | None:
    data = _load_proposals()
    items = data.setdefault("items", {})
    code = issue["code"]

    for item in items.values():
        if item.get("code") != code:
            continue
        if item.get("status") == "open":
            return None
        if _proposal_stale_after_fix(item):
            return None

    pid = uuid.uuid4().hex[:8]
    proposal = {
        "id": pid,
        "code": issue["code"],
        "title": issue["title"],
        "detail": issue["detail"],
        "action": issue.get("action", "restart_bridge"),
        "status": "open",
        "created_at": _now(),
    }
    items[pid] = proposal
    _save_proposals(data)
    return proposal


def get_proposal(proposal_id: str) -> dict[str, Any] | None:
    return _load_proposals().get("items", {}).get(proposal_id)


def close_proposal(proposal_id: str, *, status: str = "dismissed") -> None:
    data = _load_proposals()
    item = data.get("items", {}).get(proposal_id)
    if not item:
        return
    item["status"] = status
    item["closed_at"] = _now()
    _save_proposals(data)


def proposal_keyboard(proposal_id: str):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("▶️ Выполнить", callback_data=f"fix:run:{proposal_id}"),
                InlineKeyboardButton("⏹ Остановиться", callback_data=f"fix:stop:{proposal_id}"),
            ]
        ]
    )


def proposal_message(proposal: dict[str, Any]) -> str:
    return (
        f"⚠️ <b>{proposal['title']}</b>\n\n"
        f"{proposal['detail']}\n\n"
        f"<i>Действие:</i> <code>{proposal.get('action', '')}</code>"
    )


async def execute_proposal_action(proposal: dict[str, Any]) -> str:
    action = proposal.get("action") or "restart_bridge"

    if action == "code_fix_error":
        from storage import enqueue_task

        title = proposal.get("title") or "Ошибка"
        detail = proposal.get("detail") or ""
        if _is_cursor_auth_issue(detail) and _cursor_auth_currently_ok():
            n = _maybe_recover_auth_errors()
            return (
                "cursor-agent авторизован — это **старая** ошибка, код править не нужно. "
                + (f"Восстановила {n} задач(и) в очередь." if n else "Очередь уже чистая.")
            )
        if _is_cursor_transient_issue(detail):
            try:
                from hoshi_daemon import recover_transient_failed_tasks

                n = recover_transient_failed_tasks()
            except Exception as e:
                return f"Не удалось восстановить очередь: {e}"
            return (
                "Это **временный** лимит Cursor API — код править не нужно. "
                + (f"Восстановила {n} задач(и) в очередь — повторю позже." if n else "Задачи уже в очереди — подожду и повторю.")
            )
        text = (
            f"**Автоисправление по ошибке**\n\n"
            f"**{title}**\n\n"
            f"```\n{detail[:800]}\n```\n\n"
            "Найди и исправь **причину** в коде проекта. "
            "Проверь: `python3 -m py_compile` на изменённые файлы, `./hoshi_ctl.sh status`. "
            "Перезапуск сделает демон после ответа."
        )
        if _is_cursor_transient_issue(text):
            try:
                from hoshi_daemon import recover_transient_failed_tasks

                n = recover_transient_failed_tasks()
            except Exception as e:
                return f"Не удалось восстановить очередь: {e}"
            return (
                "Это **временный** лимит Cursor API — код править не нужно. "
                + (f"Восстановила {n} задач(и) в очередь — повторю позже." if n else "Задачи уже в очереди — подожду и повторю.")
            )
        task_id = enqueue_task(
            source="health_watch",
            text=text,
            chat_id=OWNER_ID,
            message_id=0,
            kind="code_fix",
        )
        if task_id:
            return "Поставила правку в очередь — займусь и отчитаюсь."
        return "Правка уже в очереди — жди отчёт."

    if action == "cursor_auth_retry":
        try:
            from hoshi_daemon import recover_auth_failed_tasks, verify_cursor_auth

            if verify_cursor_auth():
                n = recover_auth_failed_tasks()
                if n:
                    return f"Авторизация OK — восстановила {n} задач(и) в очередь ✅"
                return "Авторизация OK — упавших auth-задач в errors не осталось ✅"
            return (
                "cursor-agent не авторизован. Выполни `agent login` на сервере "
                "или добавь `CURSOR_API_KEY` в `.env`, затем нажми «Выполнить» снова."
            )
        except Exception as e:
            return f"Не удалось проверить авторизацию: {e}"

    if action == "cursor_transient_retry":
        try:
            from hoshi_daemon import recover_transient_failed_tasks

            n = recover_transient_failed_tasks()
            return (
                "Это **временный** лимит Cursor API, код править не нужно. "
                + (f"Восстановила {n} задач(и) в очередь — повторю позже." if n else "Задачи уже в очереди — подожду и повторю.")
            )
        except Exception as e:
            return f"Не удалось восстановить очередь: {e}"

    if action == "recover_queue":
        try:
            from hoshi_daemon import (
                cleanup_cursor_agents,
                cursor_agents_alive,
                recover_interrupted_failed_tasks,
                recover_stale_in_progress,
            )

            if not cursor_agents_alive():
                cleanup_cursor_agents(reason="health button")
            recover_stale_in_progress()
            n = recover_interrupted_failed_tasks()
            return (
                f"Очередь сброшена, зависшие задачи восстановлены ✅"
                + (f" (+{n} прерванных из errors)" if n else "")
            )
        except Exception as e:
            return f"Не удалось восстановить очередь: {e}"

    if action == "restart_watcher":
        from chat_watcher import restart_chat_watcher

        ok = await restart_chat_watcher()
        return "Слушатель перезапущен ✅" if ok else "Не удалось запустить слушатель (аккаунт не привязан?)"

    if action == "restart_daemon":
        import subprocess

        subprocess.Popen(
            ["/bin/bash", str(ROOT / "hoshi_ctl.sh"), "daemon", "restart"],
            cwd=str(ROOT),
            start_new_session=True,
        )
        return "Перезапуск демона запланирован."

    if action in ("restart_bridge", "restart_bridge_only"):
        from restart_util import schedule_restart

        if schedule_restart(scope="bridge", note="Перезапуск bridge по кнопке «Выполнить»."):
            return "Bridge перезапускается… Отчёт пришлю после старта."
        return "Перезапуск уже идёт (подожди ~минуту)."

    return f"Неизвестное действие: {action}"


PROACTIVE_OWNER_RE = re.compile(
    r"(?:"
    r"работай\s+тут\s+активн|"
    r"пиши\s+сам|"
    r"проактив|"
    r"совет.*sao|"
    r"не\s+да[её]шь\s+совет|"
    r"не\s+пишешь\s+сам"
    r")",
    re.I,
)


def is_proactive_owner_request(text: str) -> bool:
    head = text.split("\n---\n")[0].strip()
    return bool(PROACTIVE_OWNER_RE.search(head))


def _proactive_interval_hours() -> float:
    try:
        from storage import load_settings

        cfg = load_settings().get("proactive", {})
        if not cfg.get("enabled", True):
            return -1.0
        return float(cfg.get("interval_hours", 2.0))
    except Exception:
        return 2.0


def should_send_proactive() -> bool:
    interval = _proactive_interval_hours()
    if interval <= 0:
        return False
    state = _load_state()
    last = state.get("last_proactive_at")
    if not last:
        return True
    try:
        delta = (datetime.now() - datetime.fromisoformat(last)).total_seconds()
        return delta >= interval * 3600
    except Exception:
        return True


def _mark_proactive_sent() -> None:
    state = _load_state()
    state["last_proactive_at"] = _now()
    _save_state(state)


def format_owner_status_brief() -> str:
    """Краткий статус для проактивного сообщения владельцу."""
    from chat_router import get_chat_config

    settings = load_settings()
    muted = []
    for key, cfg in (settings.get("external_chats", {}).get("chats") or {}).items():
        if cfg.get("muted"):
            muted.append(cfg.get("title") or key)
    watcher = "?"
    try:
        from chat_watcher import watcher_status

        st = watcher_status()
        watcher = "✅" if st.get("running") else "❌"
    except Exception:
        pass
    lines = [f"**Watcher:** {watcher}"]
    if muted:
        lines.append("**Мут:** " + ", ".join(muted[:5]))
    pending = len(list_queue_items(INBOX, status="pending"))
    if pending:
        lines.append(f"**Очередь:** {pending} задач")
    return "📡 **Статус**\n\n" + "\n".join(lines)


def next_proactive_message() -> str:
    """Чередует SAO-совет, идею и краткий статус."""
    state = _load_state()
    kind = state.get("proactive_kind", "sao")
    if kind == "sao":
        from sao_advisor import format_proactive_sao_message

        state["proactive_kind"] = "idea"
        _save_state(state)
        return format_proactive_sao_message()
    if kind == "idea":
        idx = int(state.get("idea_index", 0))
        idea = PROJECT_IDEAS[idx % len(PROJECT_IDEAS)]
        state["idea_index"] = idx + 1
        state["proactive_kind"] = "status"
        _save_state(state)
        return f"💡 **Идея для проекта**\n\n{idea}"
    state["proactive_kind"] = "sao"
    _save_state(state)
    return format_owner_status_brief()


async def notify_owner_proposal(proposal: dict[str, Any]) -> None:
    from notify import send_message_with_keyboard

    await send_message_with_keyboard(
        OWNER_ID,
        proposal_message(proposal),
        proposal_keyboard(proposal["id"]),
    )


def notify_owner_proposal_sync(proposal: dict[str, Any]) -> None:
    from notify import send_message_with_keyboard_sync

    send_message_with_keyboard_sync(
        OWNER_ID,
        proposal_message(proposal),
        proposal_keyboard(proposal["id"]),
    )


def report_runtime_error(
    *,
    source: str,
    title: str,
    detail: str,
    action: str = "code_fix_error",
) -> dict[str, Any] | None:
    """Создать предложение исправления и уведомить владельца (sync, из daemon/watcher)."""
    detail = (detail or "").strip()
    if not detail:
        return None
    if action == "code_fix_error" and _is_cursor_auth_issue(detail):
        action = "cursor_auth_retry"
        title = "cursor-agent: нужна авторизация"
    if action == "code_fix_error" and _is_cursor_transient_issue(detail):
        action = "cursor_transient_retry"
        title = "Cursor API временно недоступен"
    issue = {
        "code": f"runtime:{source}:{_error_fingerprint(detail)}",
        "title": title,
        "detail": detail[:500],
        "action": action,
    }
    proposal = create_proposal(issue)
    if not proposal:
        return None
    try:
        notify_owner_proposal_sync(proposal)
        log.info("runtime error proposal sent: %s", proposal["code"])
    except Exception as e:
        log.warning("failed to notify runtime error: %s", e)
    return proposal


async def auto_heal_issue(issue: dict[str, str]) -> bool:
    """Тихое восстановление — без ожидания кнопки у владельца."""
    action = issue.get("action") or ""
    if action == "restart_daemon":
        from restart_util import ensure_daemon_running

        return ensure_daemon_running()
    if action == "recover_queue":
        try:
            from hoshi_daemon import (
                cleanup_cursor_agents,
                cursor_agents_alive,
                recover_interrupted_failed_tasks,
                recover_stale_in_progress,
            )

            if not cursor_agents_alive():
                cleanup_cursor_agents(reason="health recover")
            recover_stale_in_progress()
            recover_interrupted_failed_tasks()
            return True
        except Exception as e:
            log.warning("recover_queue failed: %s", e)
            return False
    if action == "cursor_auth_retry":
        try:
            from hoshi_daemon import recover_auth_failed_tasks, verify_cursor_auth

            if not verify_cursor_auth():
                return False
            recover_auth_failed_tasks()
            return True
        except Exception as e:
            log.warning("cursor_auth_retry failed: %s", e)
            return False
    if action == "cursor_transient_retry":
        try:
            from hoshi_daemon import recover_transient_failed_tasks

            recover_transient_failed_tasks()
            return True
        except Exception as e:
            log.warning("cursor_transient_retry failed: %s", e)
            return False
    if action == "restart_watcher":
        from chat_watcher import restart_chat_watcher

        return await restart_chat_watcher()
    return False


def _close_resolved_proposals(active_codes: set[str]) -> None:
    data = _load_proposals()
    items = data.setdefault("items", {})
    changed = False
    for item in items.values():
        if item.get("status") != "open":
            continue
        if (item.get("code") or "") not in active_codes:
            item["status"] = "resolved"
            item["closed_at"] = _now()
            changed = True
    if changed:
        _save_proposals(data)


async def health_check_issues() -> None:
    """Проверка ошибок (throttle 60 с) — не блокирует проактивные сообщения."""
    state = _load_state()
    now = datetime.now()
    last = state.get("last_check_at")
    if last:
        try:
            if (now - datetime.fromisoformat(last)).total_seconds() < 60:
                return
        except Exception:
            pass
    state["last_check_at"] = _now()
    _save_state(state)

    issues = collect_issues()
    _close_resolved_proposals({i.get("code", "") for i in issues})

    for issue in issues:
        detail = issue.get("detail") or ""
        if _is_cursor_auth_issue(detail) and _cursor_auth_currently_ok():
            n = _maybe_recover_auth_errors()
            log.info("skipped stale cursor auth issue: %s recovered=%s", issue.get("code"), n)
            continue
        if _is_cursor_transient_issue(detail):
            try:
                from hoshi_daemon import recover_transient_failed_tasks

                n = recover_transient_failed_tasks()
                log.info("auto-healed transient cursor issue: %s recovered=%s", issue.get("code"), n)
            except Exception as e:
                log.warning("cursor transient recover failed: %s", e)
            continue
        if issue.get("action") == "cursor_auth_retry":
            if await auto_heal_issue(issue):
                log.info("auto-healed: %s", issue["code"])
                continue
        if issue.get("code") in ("daemon_down", "tasks_stuck", "watcher_down"):
            if await auto_heal_issue(issue):
                log.info("auto-healed: %s", issue["code"])
                continue
        proposal = create_proposal(issue)
        if proposal:
            log.info("health proposal: %s", proposal["code"])
            try:
                await notify_owner_proposal(proposal)
            except Exception as e:
                log.warning("failed to notify proposal: %s", e)


async def _scan_sao_background() -> None:
    try:
        import asyncio
        from sao_advisor import scan_dialogs_for_purchases

        await asyncio.wait_for(scan_dialogs_for_purchases(), timeout=20.0)
    except Exception as e:
        log.debug("sao scan failed: %s", e)


async def health_send_proactive(*, force: bool = False) -> None:
    """Проактивные советы владельцу (SAO-бот, идеи) — отдельный интервал."""
    if not force and not should_send_proactive():
        return

    settings = load_settings()
    if not settings.get("linked_account", {}).get("user_id"):
        return

    # Не пишем в первые 90 с после старта bridge
    state = _load_state()
    bridge_started = state.get("bridge_started_at")
    if bridge_started and not force:
        try:
            if (datetime.now() - datetime.fromisoformat(bridge_started)).total_seconds() < 90:
                return
        except Exception:
            pass

    from notify import send_message

    msg = next_proactive_message()
    try:
        await send_message(OWNER_ID, msg)
        _mark_proactive_sent()
        log.info("proactive message sent")
    except Exception as e:
        log.warning("proactive send failed: %s", e)
        return

    import asyncio

    asyncio.ensure_future(_scan_sao_background())


async def health_tick(*, send_proactive: bool = False) -> None:
    await health_check_issues()
    if send_proactive:
        await health_send_proactive()
