#!/usr/bin/env python3
"""Ветки диалога в боте — отдельные контексты по запросу хозяина."""
from __future__ import annotations

import html
import logging
import re
import uuid
from datetime import datetime
from typing import Any

from storage import load_settings, save_settings

log = logging.getLogger("hoshi.bot_branches")

BRANCH_CREATE_RE = re.compile(
    r"(?:"
    r"(?:создай|открой|новая|сделай)\s+ветк(?:у|а)?(?:\s+(?:для|про|называется|с\s+названием))?\s*[:\-]?\s*"
    r"|"
    r"ветка\s+"
    r")(?P<name>.+)$",
    re.I,
)

BRANCH_LIST_RE = re.compile(
    r"(?:список\s+веток|мои\s+ветки|какие\s+ветки)",
    re.I,
)

BRANCH_VISIBILITY_RE = re.compile(
    r"(?:"
    r"не\s+вижу\s+(?:эту\s+)?ветк"
    r"|ветк\w*\s+(?:должн\w+|надо)\s+(?:быть\s+)?видн"
    r"|(?:она|ветка)\s+должн\w+\s+быть\s+видн"
    r")",
    re.I,
)

BRANCH_TOPIC_ENSURE_RE = re.compile(
    r"(?:"
    r"(?:создай|сделай|открой)\s+(?:такую\s+же\s+)?(?:тему|ветку|вкладку)?"
    r"(?:\s+(?:с\s+)?названием)?\s*[:\-]?\s*(?P<name>[a-zA-Zа-яА-ЯёЁ0-9_\-]+)"
    r"|"
    r"(?:новые\s+)?(?:функци\w*|тем\w*|ветк\w*)\s+телеграм.*бот"
    r")",
    re.I,
)


def _branches_root(settings: dict | None = None) -> dict[str, Any]:
    s = settings if settings is not None else load_settings()
    return s.setdefault("agent", {}).setdefault("branches", {})


def is_branch_create_command(text: str) -> bool:
    head = (text or "").split("\n---\n")[0].strip()
    return bool(BRANCH_CREATE_RE.search(head))


def is_branch_list_command(text: str) -> bool:
    head = (text or "").split("\n---\n")[0].strip()
    return bool(BRANCH_LIST_RE.search(head))


def parse_branch_name(text: str) -> str:
    head = (text or "").split("\n---\n")[0].strip()
    m = BRANCH_CREATE_RE.search(head)
    if not m:
        return ""
    name = (m.group("name") or "").strip().strip("\"'«»")
    name = re.sub(r"\s+", " ", name)
    return name[:80]


def create_bot_branch(
    name: str,
    *,
    anchor_message_id: int | None = None,
    topic_id: int | None = None,
) -> dict[str, Any]:
    """Новая ветка в боте — отдельный контекст для задач."""
    clean = (name or "").strip() or "без названия"
    s = load_settings()
    branches = _branches_root(s)
    branch_id = uuid.uuid4().hex[:10]
    entry = {
        "id": branch_id,
        "name": clean,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "anchor_message_id": int(anchor_message_id) if anchor_message_id else None,
        "topic_id": int(topic_id) if topic_id else None,
        "active": True,
    }
    branches[branch_id] = entry
    s.setdefault("agent", {})["active_branch_id"] = branch_id
    save_settings(s)
    return entry


def list_bot_branches(*, active_only: bool = False) -> list[dict[str, Any]]:
    branches = _branches_root()
    out = list(branches.values())
    if active_only:
        out = [b for b in out if b.get("active")]
    out.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return out


def get_branch_by_id(branch_id: str | None) -> dict[str, Any] | None:
    if not branch_id:
        return None
    return _branches_root().get(str(branch_id))


def get_active_branch() -> dict[str, Any] | None:
    s = load_settings()
    bid = (s.get("agent") or {}).get("active_branch_id")
    if not bid:
        return None
    return _branches_root(s).get(str(bid))


def set_active_branch(branch_id: str | None) -> None:
    s = load_settings()
    s.setdefault("agent", {})["active_branch_id"] = branch_id
    save_settings(s)


def update_branch_anchor(branch_id: str, message_id: int) -> None:
    s = load_settings()
    branch = _branches_root(s).get(str(branch_id))
    if not branch:
        return
    branch["anchor_message_id"] = int(message_id)
    save_settings(s)


def set_branch_topic_id(branch_id: str, topic_id: int) -> None:
    s = load_settings()
    branch = _branches_root(s).get(str(branch_id))
    if not branch:
        return
    branch["topic_id"] = int(topic_id)
    save_settings(s)


def branch_topic_id(branch: dict[str, Any] | None) -> int | None:
    if not branch:
        return None
    tid = branch.get("topic_id")
    return int(tid) if tid else None


def extract_message_topic_id(message) -> int | None:
    if not message:
        return None
    if getattr(message, "is_topic_message", None) and getattr(message, "message_thread_id", None):
        return int(message.message_thread_id)
    dmt = getattr(message, "direct_messages_topic", None)
    if dmt and getattr(dmt, "topic_id", None):
        return int(dmt.topic_id)
    return None


def resolve_branch_by_topic_id(topic_id: int | None) -> dict[str, Any] | None:
    if not topic_id:
        return None
    for branch in list_bot_branches():
        if int(branch.get("topic_id") or 0) == int(topic_id):
            return branch
    return None


def resolve_branch_by_name(name: str) -> dict[str, Any] | None:
    needle = (name or "").strip().lower()
    if not needle:
        return None
    for branch in list_bot_branches():
        if str(branch.get("name") or "").strip().lower() == needle:
            return branch
    return None


async def ensure_branch_telegram_topic(
    bot,
    chat_id: int,
    branch: dict[str, Any],
    *,
    force: bool = False,
) -> tuple[int | None, str | None]:
    """Telegram-тема в ЛС с ботом — вкладка как в группах."""
    existing = branch_topic_id(branch)
    if existing and not force:
        return existing, None
    if not await bot_private_topics_enabled(bot):
        return None, "topics_disabled"
    name = str(branch.get("name") or "задача").strip()[:128] or "задача"
    try:
        from telegram.constants import ForumIconColor

        topic = await bot.create_forum_topic(
            chat_id=chat_id,
            name=name,
            icon_color=ForumIconColor.BLUE,
        )
        tid = int(topic.message_thread_id)
        set_branch_topic_id(str(branch["id"]), tid)
        branch["topic_id"] = tid
        log.info("branch topic created name=%s id=%s topic=%s", name, branch.get("id"), tid)
        return tid, None
    except Exception as e:
        log.warning("branch topic create failed name=%s: %s", name, e)
        err = str(e).lower()
        if "not a forum" in err or "topics" in err:
            return None, "topics_disabled"
        return None, "create_failed"


async def sync_all_branch_topics(bot, chat_id: int) -> None:
    if not await bot_private_topics_enabled(bot):
        log.info("branch topic sync skipped: bot topics disabled in BotFather")
        return
    for branch in list_bot_branches(active_only=True):
        if not branch_topic_id(branch):
            await ensure_branch_telegram_topic(bot, chat_id, branch)


def is_branch_visibility_request(text: str) -> bool:
    head = (text or "").split("\n---\n")[0].strip()
    if "ветк" not in head.lower() and "видна" not in head.lower():
        return False
    return bool(BRANCH_VISIBILITY_RE.search(head))


def clear_external_delivery(extra: dict[str, Any] | None) -> None:
    """Убирает маршрутизацию во внешний чат — ответ только в бот."""
    if not extra:
        return
    for key in (
        "delivery",
        "target_chat_id",
        "target_message_id",
        "target_chat_title",
        "owner_approved_write",
        "resolved_chat_id",
        "resolved_chat_title",
        "chat_context",
    ):
        extra.pop(key, None)


def is_bot_internal_branch_task(text: str) -> bool:
    """Задачи про ветки/темы бота — не уходят во внешний Telegram-чат."""
    head = (text or "").split("\n---\n")[0].strip()
    if not head:
        return False
    if is_branch_topic_ensure_command(head) or is_branch_create_command(head):
        return True
    if is_branch_visibility_request(head) or is_branch_list_command(head):
        return True
    low = head.lower()
    if re.search(r"(?:ветк|тем[аы]?|вкладк)\w*\s+(?:бот|телеграм)", low):
        return True
    if re.search(r"(?:создай|сделай|открой).*(?:ветк|тем[аы]?|вкладк)", low):
        return True
    if re.search(r"format_branch_badge|bot_branches|message_thread_id", low):
        return True
    return False


def is_branch_topic_ensure_command(text: str) -> bool:
    head = (text or "").split("\n---\n")[0].strip()
    if not head:
        return False
    low = head.lower()
    if BRANCH_TOPIC_ENSURE_RE.search(head):
        return True
    if ("тем" in low or "ветк" in low) and re.search(
        r"создай|сделай|открой|такую\s+же",
        low,
    ):
        return True
    return False


def parse_branch_topic_ensure_name(text: str) -> str:
    head = (text or "").split("\n---\n")[0].strip()
    m = BRANCH_TOPIC_ENSURE_RE.search(head)
    if m and m.group("name"):
        return str(m.group("name")).strip()[:80]
    m2 = re.search(
        r"(?:названием|назови|название)\s+([a-zA-Zа-яА-ЯёЁ0-9_\-]+)",
        head,
        re.I,
    )
    if m2:
        return str(m2.group(1)).strip()[:80]
    return ""


async def bot_private_topics_enabled(bot) -> bool:
    try:
        me = await bot.get_me()
        return bool(getattr(me, "has_topics_enabled", False))
    except Exception as e:
        log.warning("bot topics check failed: %s", e)
        return False


def branch_reply_topic_kw(topic_id: int | None) -> dict[str, int]:
    """Тема в ЛС с ботом — message_thread_id, не direct_messages_topic_id."""
    if not topic_id:
        return {}
    return {"message_thread_id": int(topic_id)}


def format_topics_disabled_message() -> str:
    return (
        "🌿 Темы в ЛС с ботом пока выключены.\n"
        "Включи их в <b>@BotFather</b> → твой бот → <b>Bot Settings</b> → "
        "<b>Topics in private chats</b>.\n"
        "После этого напиши снова — создам вкладку автоматически."
    )


def resolve_branch_by_anchor(message_id: int) -> dict[str, Any] | None:
    if not message_id:
        return None
    for branch in list_bot_branches():
        if int(branch.get("anchor_message_id") or 0) == int(message_id):
            return branch
    return None


def format_branch_badge(branch: dict[str, Any] | None) -> str:
    if not branch:
        return ""
    name = html.escape(str(branch.get("name") or "?"))
    return f"🌿 <b>{name}</b>"


def format_branch_ack_prefix(branch_name: str | None) -> str:
    name = (branch_name or "").strip()
    if not name:
        return ""
    return f"🌿 {name} · "


def format_branch_reply_header(branch_name: str | None) -> str:
    name = (branch_name or "").strip()
    if not name:
        return ""
    return f"🌿 **{name}**\n\n"


def reply_already_has_branch_header(text: str, branch_name: str | None) -> bool:
    head = (text or "").lstrip()[:80]
    name = (branch_name or "").strip()
    if not name:
        return False
    return head.startswith("🌿") and name.lower() in head.lower()


def format_branch_created_message(name: str, branch_id: str, *, has_topic: bool = True) -> str:
    clean = html.escape(name or "задача")
    bid = html.escape(branch_id)
    if has_topic:
        return (
            f"🌿 Тема «{clean}» в чате с ботом.\n"
            f"Открой её во вкладках — пиши там <b>юна</b> + задача (id <code>{bid}</code>)."
        )
    return (
        f"🌿 Ветка «{clean}» создана.\n"
        f"Пиши реплаем сюда: <b>юна</b> + задача (id <code>{bid}</code>)."
    )


def format_branch_list() -> str:
    branches = list_bot_branches(active_only=True)
    if not branches:
        return "Активных веток нет. Напиши: <b>юна создай ветку Название</b>"
    lines = ["<b>Ветки в боте:</b>"]
    active = (load_settings().get("agent") or {}).get("active_branch_id")
    for b in branches[:12]:
        mark = " ← сейчас" if b.get("id") == active else ""
        name = html.escape(str(b.get("name") or "?"))
        bid = html.escape(str(b.get("id") or ""))
        topic = " · тема ✅" if b.get("topic_id") else ""
        lines.append(f"• <b>{name}</b> (<code>{bid}</code>){topic}{mark}")
    return "\n".join(lines)


BRANCH_CONTEXT_BLOCK_RE = re.compile(
    r"^\*\*Ветка бота:\*\*[^\n]*\n?",
    re.I,
)


def strip_branch_context_block(text: str) -> str:
    """Убирает служебную шапку ветки — не путать с упоминанием чата Iris."""
    return BRANCH_CONTEXT_BLOCK_RE.sub("", text or "", count=1).lstrip()


def branch_context_block(branch: dict[str, Any] | None) -> str:
    if not branch:
        return ""
    name = str(branch.get("name") or "?")
    if name.lower() == IRIS_BRANCH_NAME:
        return (
            "Ветка iris — **только** ответ на последнее сообщение хозяина. "
            "Без «на связи», без мешка/биржи, без «чем помочь».\n"
        )
    return (
        f"**Ветка бота:** «{name}» (id `{branch.get('id', '')}`). "
        "Держи контекст задачи в этой ветке, не смешивай с другими.\n"
    )


IRIS_BRANCH_NAME = "iris"

IRIS_TASK_RE = re.compile(
    r"(?:"
    r"iris|ирис|black\s*diamond|"
    r"бирж(?:а|и|у|е|ей)?|"
    r"\.?\s*мешок|"
    r"ирис(?:ок|ки|-голд|голд|к)|"
    r"i[¢c]|"
    r"стакан|"
    r"покуп(?:ать|ай|и)|продав(?:ать|ай|и)"
    r")",
    re.I,
)


def get_iris_branch() -> dict[str, Any] | None:
    return resolve_branch_by_name(IRIS_BRANCH_NAME)


def iris_branch_extra() -> dict[str, Any]:
    branch = get_iris_branch()
    if not branch:
        return {}
    extra: dict[str, Any] = {
        "branch_id": branch.get("id"),
        "branch_name": branch.get("name"),
    }
    tid = branch_topic_id(branch)
    if tid:
        extra["branch_topic_id"] = int(tid)
    return extra


def apply_iris_branch_extra(extra: dict[str, Any] | None) -> dict[str, Any]:
    """Подставляет ветку iris в extra, если ещё не задана."""
    out = extra if extra is not None else {}
    if out.get("branch_id"):
        return out
    out.update(iris_branch_extra())
    return out


def is_iris_related_task(text: str, extra: dict[str, Any] | None = None) -> bool:
    head = (text or "").split("\n---\n")[0].strip()
    if not head:
        return False
    if re.search(
        r"(?:общ(?:ей|ая)|general).*(?:не\s+)?iris|"
        r"не\s+iris|а\s+не\s+iris|"
        r"научись\s+различать|"
        r"удали\s+шаблон",
        head,
        re.I,
    ):
        return False
    ex = extra or {}
    if str(ex.get("branch_name") or "").lower() == "iris":
        topic = ex.get("branch_topic_id")
        msg_topic = ex.get("message_topic_id")
        if topic and msg_topic and int(topic) != int(msg_topic):
            return False
    if IRIS_TASK_RE.search(head):
        return True
    ex = extra or {}
    title = str(ex.get("resolved_chat_title") or ex.get("target_chat_title") or "")
    if IRIS_TASK_RE.search(title):
        return True
    ctx = str(ex.get("chat_context") or "")
    if "iris" in ctx.lower() or "black diamond" in ctx.lower() or "ирис" in ctx.lower():
        return True
    return False


def ensure_iris_branch_on_task(text: str, extra: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    """Маршрут Iris-задач в ветку iris."""
    ex = extra if extra is not None else {}
    if ex.get("branch_id"):
        return text, ex
    if not is_iris_related_task(text, ex):
        return text, ex
    branch = get_iris_branch()
    if not branch:
        return text, ex
    set_active_branch(branch.get("id"))
    ex.update(iris_branch_extra())
    if branch and not ex.get("branch_prompt"):
        ex["branch_prompt"] = branch_context_block(branch)
    return text, ex
