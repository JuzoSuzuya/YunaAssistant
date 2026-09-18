#!/usr/bin/env python3
"""Автозахват бесплатных чеков в ветке Iris (xRocket / Crypto Bot @send)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any

from config import DATA, OWNER_ID
from storage import load_settings, save_settings

log = logging.getLogger("hoshi.iris_checks")

STATE_PATH = DATA / "iris_check_claimer_state.json"
IRIS_CHECKS_CHAT_ID = -1002941121338
IRIS_CHECKS_TOPIC_ID = 707783

CHECK_TEXT_RE = re.compile(
    r"(?:"
    r"🦋\s*\[чек\]|"
    r"🚀\s*\*\*чек\*\*|"
    r"image\.api\.xrocket\.exchange/image/cheque|"
    r"t\.me/(?:send|CryptoBot|xrocket)\?start="
    r")",
    re.I,
)
SEND_LINK_RE = re.compile(r"t\.me/(?:send|CryptoBot)\?start=(\w+)", re.I)
CLAIM_BTN_RE = re.compile(r"^получить\b", re.I)
ALREADY_CLAIMED_BTN_RE = re.compile(r"получено|✅", re.I)
PAY_BTN_RE = re.compile(r"оплат|создать|отправить|купить", re.I)
PASSWORD_RE = re.compile(r"пароль от чека", re.I)
ENABLE_OWNER_RE = re.compile(
    r"(?:чек|раздач).*(?:получ|забир|лов)|"
    r"(?:получ|забир|лов).*(?:чек|раздач)|"
    r"t\.me/c/\d+/\d+.*чек|"
    r"чек.*t\.me/c/\d+/\d+",
    re.I,
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _cfg() -> dict[str, Any]:
    return load_settings().get("iris_check_claimer") or {}


def is_enabled() -> bool:
    return bool(_cfg().get("enabled"))


def _load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(data: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def enable(*, chat_id: int | None = None, topic_id: int | None = None) -> None:
    settings = load_settings()
    prev = settings.get("iris_check_claimer") or {}
    settings["iris_check_claimer"] = {
        "enabled": True,
        "chat_id": int(chat_id or prev.get("chat_id") or IRIS_CHECKS_CHAT_ID),
        "topic_id": int(topic_id or prev.get("topic_id") or IRIS_CHECKS_TOPIC_ID),
        "enabled_at": prev.get("enabled_at") or _now(),
    }
    save_settings(settings)
    log.info(
        "iris check claimer enabled chat=%s topic=%s",
        settings["iris_check_claimer"]["chat_id"],
        settings["iris_check_claimer"]["topic_id"],
    )


def enable_from_owner_text(text: str) -> bool:
    if not text or not ENABLE_OWNER_RE.search(text):
        return False
    was = is_enabled()
    m = re.search(r"t\.me/c/(\d+)/(\d+)", text, re.I)
    chat_id = topic_id = None
    if m:
        chat_id = -int(f"100{m.group(1)}")
        topic_id = int(m.group(2))
    enable(chat_id=chat_id, topic_id=topic_id)
    return not was


def _target_chat_id() -> int:
    try:
        return int(_cfg().get("chat_id") or IRIS_CHECKS_CHAT_ID)
    except (TypeError, ValueError):
        return IRIS_CHECKS_CHAT_ID


def _target_topic_id() -> int:
    try:
        return int(_cfg().get("topic_id") or IRIS_CHECKS_TOPIC_ID)
    except (TypeError, ValueError):
        return IRIS_CHECKS_TOPIC_ID


def _message_topic_id(msg) -> int | None:
    rt = getattr(msg, "reply_to", None)
    if not rt:
        return None
    top = getattr(rt, "reply_to_top_id", None)
    if top:
        return int(top)
    if getattr(rt, "forum_topic", False):
        mid = getattr(rt, "reply_to_msg_id", None)
        if mid:
            return int(mid)
    return None


def _in_target_topic(msg) -> bool:
    topic = _message_topic_id(msg)
    if topic == _target_topic_id():
        return True
    if int(getattr(msg, "id", 0) or 0) == _target_topic_id():
        return True
    return False


def _button_labels(msg) -> list[str]:
    labels: list[str] = []
    for row in getattr(msg, "buttons", None) or []:
        for btn in row:
            labels.append((btn.text or "").strip())
    return labels


def _is_claimable(msg) -> tuple[bool, str]:
    text = (getattr(msg, "text", None) or "").strip()
    if not text:
        return False, ""
    if PASSWORD_RE.search(text):
        return False, "password"
    labels = _button_labels(msg)
    for label in labels:
        if PAY_BTN_RE.search(label):
            return False, "pay_button"
    claim_btn = None
    for label in labels:
        if ALREADY_CLAIMED_BTN_RE.search(label):
            return False, "already_claimed"
        if CLAIM_BTN_RE.search(label):
            claim_btn = label
    if claim_btn:
        return True, f"button:{claim_btn}"
    if CHECK_TEXT_RE.search(text) and SEND_LINK_RE.search(text):
        return True, "send_link"
    if CHECK_TEXT_RE.search(text) and labels:
        return False, "no_claim_button"
    return False, ""


def _already_tried(state: dict[str, Any], msg_id: int) -> bool:
    tried = state.get("tried_ids") or []
    return int(msg_id) in {int(x) for x in tried}


def _mark_tried(state: dict[str, Any], msg_id: int) -> None:
    tried = [int(x) for x in (state.get("tried_ids") or [])]
    mid = int(msg_id)
    if mid not in tried:
        tried.append(mid)
    state["tried_ids"] = tried[-300:]


def _record_claim(state: dict[str, Any], *, msg_id: int, detail: str, ok: bool) -> None:
    claims = state.setdefault("claims", [])
    claims.append(
        {
            "at": _now(),
            "msg_id": int(msg_id),
            "ok": bool(ok),
            "detail": detail[:300],
        }
    )
    state["claims"] = claims[-50:]
    if ok:
        state["last_claim_at"] = _now()
        state["last_claim_detail"] = detail[:300]


async def _click_claim_button(msg, client=None) -> tuple[bool, str]:
    labels = _button_labels(msg)
    for i, row in enumerate(getattr(msg, "buttons", None) or []):
        for j, btn in enumerate(row):
            label = (btn.text or "").strip()
            if not CLAIM_BTN_RE.search(label):
                continue
            if ALREADY_CLAIMED_BTN_RE.search(label):
                continue
            if PAY_BTN_RE.search(label):
                continue
            try:
                await msg.click(i, j)
                return True, label
            except Exception as e:
                log.debug("check button click failed: %s", e)
                url = getattr(btn, "url", None)
                if url:
                    return await _open_deeplink(url, label, client=client)
    return False, "no_button"


async def _open_deeplink(url: str, label: str, client=None) -> tuple[bool, str]:
    from user_client import get_client

    m = re.search(r"t\.me/(\w+)\?start=(\w+)", url, re.I)
    if not m:
        return False, "bad_url"
    bot = m.group(1)
    code = m.group(2)
    client = client or await get_client()
    if not client:
        return False, "no_client"
    try:
        await client.send_message(bot, f"/start {code}")
        return True, f"{label} ({bot})"
    except Exception as e:
        log.debug("deeplink open failed %s: %s", url, e)
        return False, str(e)[:120]


async def _claim_send_link(text: str, client=None) -> tuple[bool, str]:
    m = SEND_LINK_RE.search(text or "")
    if not m:
        return False, "no_link"
    code = m.group(1)
    from user_client import get_client

    client = client or await get_client()
    if not client:
        return False, "no_client"
    for bot in ("send", "CryptoBot"):
        try:
            await client.send_message(bot, f"/start {code}")
            return True, f"send:{code}"
        except Exception as e:
            log.debug("send claim via %s failed: %s", bot, e)
    return False, "send_failed"


async def try_claim_message(msg, client=None) -> dict[str, Any]:
    """Пытается забрать чек из сообщения. Возвращает результат."""
    if not is_enabled():
        return {"ok": False, "skipped": "disabled"}
    chat_id = getattr(msg, "chat_id", None)
    if int(chat_id or 0) != _target_chat_id():
        return {"ok": False, "skipped": "wrong_chat"}
    if not _in_target_topic(msg):
        return {"ok": False, "skipped": "wrong_topic"}

    msg_id = int(getattr(msg, "id", 0) or 0)
    state = _load_state()
    if _already_tried(state, msg_id):
        return {"ok": False, "skipped": "already_tried"}

    claimable, reason = _is_claimable(msg)
    if not claimable:
        return {"ok": False, "skipped": reason or "not_check"}

    _mark_tried(state, msg_id)
    _save_state(state)

    text = (getattr(msg, "text", None) or "").strip()
    ok = False
    detail = ""
    if reason.startswith("button:"):
        ok, detail = await _click_claim_button(msg, client=client)
    elif reason == "send_link":
        ok, detail = await _claim_send_link(text, client=client)
    else:
        detail = reason

    state = _load_state()
    _record_claim(state, msg_id=msg_id, detail=detail, ok=ok)
    _save_state(state)

    if ok:
        log.info("iris check claimed msg=%s detail=%s", msg_id, detail[:80])
        await _notify_owner_claimed(msg_id=msg_id, text=text, detail=detail)
    else:
        log.debug("iris check claim failed msg=%s detail=%s", msg_id, detail)

    return {"ok": ok, "msg_id": msg_id, "detail": detail}


async def maybe_claim_from_event(event) -> bool:
    """Обработчик NewMessage — True если пытались забрать чек."""
    if not is_enabled() or not event.message:
        return False
    result = await try_claim_message(event.message)
    return bool(result.get("ok")) or result.get("skipped") not in (
        "disabled",
        "wrong_chat",
        "wrong_topic",
        "not_check",
        "already_claimed",
        "password",
        "pay_button",
        "no_claim_button",
    )


async def poll_recent_checks(*, limit: int = 12) -> int:
    """Резервный опрос свежих сообщений в ветке. Возвращает число успешных захватов."""
    if not is_enabled():
        return 0
    from user_client import get_client

    client = await get_client()
    if not client:
        return 0
    claimed = 0
    async for msg in client.iter_messages(
        _target_chat_id(), limit=limit, reply_to=_target_topic_id()
    ):
        result = await try_claim_message(msg, client=client)
        if result.get("ok"):
            claimed += 1
    return claimed


async def _notify_owner_claimed(*, msg_id: int, text: str, detail: str) -> None:
    preview = (text or "").replace("\n", " ")[:160]
    body = (
        "✅ **Iris — чек пойман**\n\n"
        f"**Ветка:** `{_target_topic_id()}` · msg `{msg_id}`\n"
        f"**Действие:** {detail}\n"
    )
    if preview:
        body += f"**Текст:** {preview}"
    try:
        from iris_monitor import _send_owner_iris_message

        await _send_owner_iris_message(body)
    except Exception as e:
        log.debug("owner notify failed: %s", e)


def status_summary() -> str:
    cfg = _cfg()
    state = _load_state()
    enabled = "вкл" if cfg.get("enabled") else "выкл"
    last = (state.get("last_claim_at") or "—")[:16].replace("T", " ")
    detail = (state.get("last_claim_detail") or "—")[:80]
    return (
        f"**Iris чеки:** {enabled} · ветка `{cfg.get('topic_id', IRIS_CHECKS_TOPIC_ID)}` · "
        f"последний: {last} ({detail})"
    )
