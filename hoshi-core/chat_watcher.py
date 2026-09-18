#!/usr/bin/env python3
"""Слушает сообщения в чатах привязанного аккаунта."""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Any

from telethon import events

from chat_router import (
    ENABLE_RESPOND_RE,
    instant_ack_message,
    apply_owner_global_preferences_from_text,
    extract_owner_preferences,
    should_send_external_ack,
    get_chat_config,
    get_hoshi_message_preview,
    ignore_empty_external_messages,
    is_effectively_empty_message,
    set_chat_config,
    is_bot_chat_id,
    is_voice_transcript,
    should_ignore_voice_message,
    is_chat_muted,
    is_chat_read_only,
    owner_overrides_read_only,
    is_cryptobot_passive,
    is_data_harvest_request,
    is_destructive_request,
    is_external_ack,
    is_homework_request,
    is_owner_briefing_request,
    is_owner_invoke,
    is_mute_command,
    is_private_dm,
    is_registered_hoshi_message,
    is_scam_request,
    is_admin_bot_passive,
    is_service_bot_chat,
    is_service_bot_sender,
    looks_like_bot_echo,
    mark_hoshi_session,
    mute_chat,
    notify_muted_chat_trigger,
    notify_owner_security_alert,
    privacy_refusal,
    register_hoshi_message,
    build_reply_media_focus_block,
    extract_excluded_media_topics,
    is_wrong_media_correction,
    resolve_reply_media_topics,
    save_chat_preferences,
    scam_refusal,
    security_monitoring_allowed,
    should_respond_in_chat,
    should_watch_chat,
    would_respond_in_muted_chat,
)
from config import EXTERNAL_CHAT_MAX_SCAN, OWNER_ID, external_context_limit, external_context_limit_fast, fast_reply_enabled
from storage import enqueue_task, load_settings
from user_client import (
    build_chat_context_block,
    disconnect_client,
    ensure_watcher_client,
    extract_context_anchor,
    extract_reply_info,
    is_linked,
    is_writable_chat,
    message_looks_like_hoshi,
    send_user_message,
)

log = logging.getLogger("hoshi.chat_watcher")
_watcher_started = False
_handler_registered = False
_EMERGENCY_STOP_COOLDOWN_SEC = 60.0
_last_emergency_stop_ack: dict[int, float] = {}
_last_event_at = ""
_recent_keys: dict[str, float] = {}
_DEDUP_SEC = 90.0


def _context_limit() -> int:
    return external_context_limit_fast() if fast_reply_enabled() else external_context_limit()


def _is_hoshi_bot_chat(chat_id: int) -> bool:
    """Диалог с ботом Hoshi — только tg_bridge, не watcher."""
    return is_bot_chat_id(int(chat_id))


def _dedup_key(chat_id: int, message_id: int) -> str:
    return f"{chat_id}:{message_id}"


def _seen_recently(chat_id: int, message_id: int) -> bool:
    now = time.monotonic()
    key = _dedup_key(chat_id, message_id)
    stale = [k for k, t in _recent_keys.items() if now - t > _DEDUP_SEC]
    for k in stale:
        _recent_keys.pop(k, None)
    if key in _recent_keys:
        return True
    _recent_keys[key] = now
    return False


def _touch_event() -> None:
    global _last_event_at
    _last_event_at = datetime.now().isoformat(timespec="seconds")


def watcher_status() -> dict[str, Any]:
    return {
        "running": _watcher_started and _handler_registered,
        "last_event_at": _last_event_at,
        "reason": "" if _watcher_started else "not started",
    }


async def _handle_iris_check_claim_safe(event: events.NewMessage.Event) -> None:
    try:
        from iris_check_claimer import maybe_claim_from_event

        await maybe_claim_from_event(event)
    except Exception as e:
        log.debug("iris check claim handler: %s", e)


async def _handle_external_message_safe(event: events.NewMessage.Event) -> None:
    chat_id = event.chat_id or 0
    try:
        await _handle_external_message(event)
    except Exception as e:
        log.exception("external message handler failed chat=%s: %s", chat_id, e)
        try:
            from health_watch import report_runtime_error

            report_runtime_error(
                source="chat_watcher",
                title="Watcher упал на сообщении",
                detail=f"chat={chat_id}: {e}"[:400],
            )
        except Exception as notify_err:
            log.warning("error proposal failed: %s", notify_err)


def _is_voice_message(message) -> bool:
    """Голосовое / кружок / voice-note — не триггер для автоответа."""
    from chat_router import is_voice_media_message

    return is_voice_media_message(message)


async def _handle_external_message(event: events.NewMessage.Event) -> None:
    global _last_event_at
    if not event.message:
        return

    _touch_event()

    chat_id = event.chat_id
    if not chat_id:
        return

    # «Избранное» / чат с самим собой — не внешний чат
    if chat_id == OWNER_ID:
        return

    # Диалог с ботом Hoshi — обрабатывает tg_bridge, не watcher
    if _is_hoshi_bot_chat(chat_id):
        return

    # Группы / чужие ЛС — не слушаем (меньше нагрузки)
    if not should_watch_chat(chat_id):
        return

    title = ""
    username = ""
    try:
        chat = await event.get_chat()
        title = getattr(chat, "title", None) or getattr(chat, "first_name", "") or ""
        username = getattr(chat, "username", "") or ""
    except Exception:
        pass

    if is_private_dm(chat_id):
        from chat_router import ensure_private_dm_chat

        ensure_private_dm_chat(chat_id, title=title, username=username)

    # Сервисные боты (Crypto Bot) — автоуведомления, не отвечаем
    if is_service_bot_chat(chat_id):
        return

    if not await is_writable_chat(chat_id):
        return

    if should_ignore_voice_message(event.message, event.message.text or "", chat_id=chat_id):
        log.info(
            "ignored voice message chat=%s out=%s id=%s",
            chat_id,
            bool(event.message.out),
            event.message.id,
        )
        return

    text = (event.message.text or "").strip()
    if not text:
        if ignore_empty_external_messages() and is_effectively_empty_message(event):
            log.debug("ignored empty message chat=%s", chat_id)
            return
        if not text:
            return

    is_outgoing = bool(event.message.out)

    try:
        from voice_tg_feed import record_message

        sender_name = ""
        if not is_outgoing:
            try:
                snd = await event.get_sender()
                sender_name = getattr(snd, "first_name", "") or getattr(snd, "username", "") or ""
            except Exception:
                pass
        record_message(
            chat_id,
            title=title,
            sender=sender_name,
            text=text,
            outgoing=is_outgoing,
            message_id=int(event.message.id or 0),
        )
    except Exception:
        pass

    if is_outgoing and is_registered_hoshi_message(chat_id, event.message.id):
        return

    try:
        from sao_advisor import record_purchase_from_text

        record_purchase_from_text(text)
    except Exception:
        pass

    if is_chat_muted(chat_id) and not is_outgoing:
        try:
            from ad_monitor import maybe_process_muted_message

            maybe_process_muted_message(chat_id, text)
        except Exception:
            pass

    if is_outgoing:
        try:
            from iris_monitor import maybe_enable_from_owner_text

            maybe_enable_from_owner_text(text)
        except Exception:
            pass
        try:
            from news_poster import maybe_enable_from_owner_text as news_enable

            news_enable(text)
        except Exception:
            pass

    try:
        chat_entity = await event.get_chat()
        if getattr(chat_entity, "bot", False) and not is_outgoing:
            log.debug("ignored incoming telegram bot chat=%s", chat_id)
            return
    except Exception:
        chat_entity = None

    if not is_outgoing:
        try:
            sender = await event.get_sender()
            if getattr(sender, "bot", False):
                log.debug("ignored incoming bot sender chat=%s", chat_id)
                return
        except Exception:
            pass

    if not is_outgoing and (
        is_service_bot_sender(sender_id := (event.sender_id or 0))
        or is_cryptobot_passive(text)
        or is_admin_bot_passive(text)
    ):
        return

    # Не реагируем на свой ack (входящий/исходящий) и эхо ответов бота
    if is_external_ack(text) or looks_like_bot_echo(text):
        return

    from chat_router import text_looks_like_hoshi_reply

    # Эхо ответов агента (😊/🥳 или чужой шаблон персонажа) — не новая задача
    if text_looks_like_hoshi_reply(text):
        from chat_router import is_owner_invoke

        if not is_outgoing or not is_owner_invoke(text):
            log.info(
                "ignored hoshi-like echo chat=%s out=%s id=%s text=%r",
                chat_id,
                is_outgoing,
                event.message.id,
                text[:80],
            )
            return

    from personas import disable_persona_in_chat, is_persona_stop_command

    persona_stop_id = is_persona_stop_command(text)
    if persona_stop_id:
        from user_outbox import stop_chat_process

        disable_persona_in_chat(chat_id, persona_stop_id)
        stopped = stop_chat_process(chat_id)
        log.info(
            "persona stop chat=%s persona=%s inbox=%s outbox=%s text=%r",
            chat_id,
            persona_stop_id,
            stopped.get("inbox", 0),
            stopped.get("outbox", 0),
            text[:60],
        )

    from chat_router import is_emergency_stop_command

    if is_emergency_stop_command(text):
        from chat_router import EMERGENCY_STOP_ACK
        from user_outbox import stop_chat_process

        stopped = stop_chat_process(chat_id)
        now = time.monotonic()
        last = _last_emergency_stop_ack.get(chat_id, 0.0)
        if now - last >= _EMERGENCY_STOP_COOLDOWN_SEC:
            _last_emergency_stop_ack[chat_id] = now
            try:
                await send_user_message(
                    chat_id,
                    EMERGENCY_STOP_ACK,
                    reply_to=event.message.id,
                    owner_approved=True,
                )
            except Exception as e:
                log.warning("emergency stop ack failed chat=%s: %s", chat_id, e)
        else:
            log.info("emergency stop ack suppressed (cooldown) chat=%s", chat_id)
        log.info(
            "emergency stop chat=%s inbox=%s outbox=%s text=%r",
            chat_id,
            stopped.get("inbox", 0),
            stopped.get("outbox", 0),
            text[:60],
        )
        return

    sender_id = event.sender_id or 0

    title = ""
    username = ""
    try:
        chat = chat_entity if chat_entity is not None else await event.get_chat()
        title = getattr(chat, "title", None) or getattr(chat, "first_name", "") or ""
        username = getattr(chat, "username", "") or ""
    except Exception:
        pass

    if is_outgoing and is_mute_command(text):
        mute_chat(chat_id, title=title, username=username)
        try:
            from chat_router import apply_owner_silence_extras, notify_owner_chat_muted

            apply_owner_silence_extras(chat_id, text)
            notify_owner_chat_muted(title=title or str(chat_id), chat_id=chat_id)
        except Exception as e:
            log.warning("mute confirm to owner failed: %s", e)
        log.info("external chat muted chat=%s (owner command, no reply)", chat_id)
        return

    if is_chat_muted(chat_id):
        reply_to_id, reply_quote = extract_reply_info(event.message)
        reply_to_hoshi = False
        if reply_to_id:
            try:
                reply_to_hoshi = await message_looks_like_hoshi(
                    chat_id, int(reply_to_id), quote_text=reply_quote
                )
            except Exception as e:
                log.debug("reply_to_hoshi check failed chat=%s id=%s: %s", chat_id, reply_to_id, e)
        owner_muted_override = is_outgoing and owner_overrides_read_only(
            text,
            is_outgoing=True,
            reply_to_hoshi=reply_to_hoshi,
            reply_to_id=int(reply_to_id) if reply_to_id else None,
            chat_id=chat_id,
        )
        if not is_outgoing:
            sender_name = ""
            try:
                sender = await event.get_sender()
                sender_name = (
                    getattr(sender, "first_name", "")
                    or getattr(sender, "username", "")
                    or ""
                )
            except Exception:
                pass

            would, mute_reason = would_respond_in_muted_chat(
                chat_id,
                sender_id,
                text,
                is_outgoing=is_outgoing,
                reply_to_hoshi=reply_to_hoshi,
                reply_to_id=int(reply_to_id) if reply_to_id else None,
            )
            if would:
                try:
                    notify_muted_chat_trigger(
                        chat_id=chat_id,
                        message_id=event.message.id,
                        title=title or str(chat_id),
                        sender_name=sender_name or str(sender_id),
                        sender_id=int(sender_id),
                        text=text,
                        reason=mute_reason,
                        username=username,
                    )
                except Exception as e:
                    log.warning("owner permission notify failed chat=%s: %s", chat_id, e)
        if not owner_muted_override:
            log.info("muted chat blocked chat=%s out=%s text=%r", chat_id, is_outgoing, text[:60])
            return

    if is_outgoing:
        from chat_router import (
            apply_call_only_interlocutor_policy,
            EXTERNAL_UNINVITED_COMPLAINT_RE,
            is_owner_routing_complaint,
        )

        if EXTERNAL_UNINVITED_COMPLAINT_RE.search(text) or is_owner_routing_complaint(text):
            apply_call_only_interlocutor_policy(
                chat_id, title=title or str(chat_id), username=username
            )

    reply_to_id, reply_quote = extract_reply_info(event.message)
    if is_outgoing and not reply_quote:
        from chat_router import infer_quote_from_owner_text

        reply_quote = infer_quote_from_owner_text(text)
    reply_to_hoshi = False
    if reply_to_id:
        try:
            reply_to_hoshi = await message_looks_like_hoshi(
                chat_id, int(reply_to_id), quote_text=reply_quote
            )
        except Exception as e:
            log.debug("reply_to_hoshi check failed chat=%s id=%s: %s", chat_id, reply_to_id, e)
            reply_to_hoshi = False

    respond, reason = should_respond_in_chat(
        chat_id,
        sender_id,
        text,
        is_outgoing=is_outgoing,
        reply_to_hoshi=reply_to_hoshi,
        reply_to_id=int(reply_to_id) if reply_to_id else None,
    )
    if not respond:
        return

    if reason in ("destructive", "scam", "privacy") and not is_outgoing:
        if should_ignore_voice_message(event.message, text, chat_id=chat_id):
            log.info(
                "ignored security on voice chat=%s reason=%s id=%s",
                chat_id,
                reason,
                event.message.id,
            )
            return
        if reason == "destructive":
            log.info(
                "ignored destructive (no canned refusal) chat=%s id=%s",
                chat_id,
                event.message.id,
            )
            return

    if _seen_recently(chat_id, event.message.id):
        return

    log.info(
        "external trigger chat=%s out=%s reason=%s reply_to=%s quote=%r text=%r",
        chat_id,
        is_outgoing,
        reason,
        reply_to_id,
        (reply_quote or "")[:120],
        text[:80],
    )

    owner_approved_write = reason in ("owner_trigger", "owner_reply")
    if is_outgoing:
        from chat_router import (
            apply_call_only_interlocutor_policy,
            apply_forbid_cross_chat_intel_policy,
            apply_owner_reply_policy,
            EXTERNAL_UNINVITED_COMPLAINT_RE,
            is_forbid_cross_chat_intel_command,
            is_owner_invoke,
            purge_stale_external_inbox_except,
        )

        apply_owner_reply_policy(
            chat_id, text, title=title or str(chat_id), username=username
        )
        if EXTERNAL_UNINVITED_COMPLAINT_RE.search(text) or is_owner_routing_complaint(text):
            apply_call_only_interlocutor_policy(
                chat_id, title=title or str(chat_id), username=username
            )
        if owner_approved_write or is_owner_invoke(text):
            purged = purge_stale_external_inbox_except(chat_id)
            if purged:
                log.info(
                    "purged stale external inbox chat=%s removed=%s",
                    chat_id,
                    purged,
                )

        if is_forbid_cross_chat_intel_command(text):
            ack = apply_forbid_cross_chat_intel_policy()
            try:
                await send_user_message(
                    chat_id,
                    ack,
                    reply_to=event.message.id,
                    owner_approved=True,
                )
            except Exception as e:
                log.warning("forbid cross-chat policy ack failed: %s", e)
            return
        from text_format import sanitize_owner_incoming_text

        text = sanitize_owner_incoming_text(text)
        apply_owner_global_preferences_from_text(text)
        from chat_router import apply_owner_behavior_policy, is_owner_behavior_policy_command

        if is_owner_behavior_policy_command(text):
            apply_owner_behavior_policy()
        prefs = extract_owner_preferences(text)
        if prefs:
            save_chat_preferences(chat_id, prefs)
        from agent_prompt import external_task_requests_code_fix
        from chat_router import is_owner_routing_complaint, owner_wants_external_reply

        if (
            is_owner_routing_complaint(text)
            and not external_task_requests_code_fix(text)
            and not owner_wants_external_reply(text)
        ):
            log.info(
                "owner routing note chat=%s id=%s — prefs saved, no agent task",
                chat_id,
                event.message.id,
            )
            return
        if ENABLE_RESPOND_RE.search(text):
            users = [int(chat_id)] if is_private_dm(chat_id) else []
            set_chat_config(
                chat_id,
                title=title,
                username=username,
                enabled=True,
                respond_to_user=True,
                respond_users=users,
                reply_to_triggers=True,
                muted=False,
            )
            owner_approved_write = True
        try:
            from personas import remember_persona_from_text

            remember_persona_from_text(text)
        except Exception:
            pass

    if "запомни" in text.lower():
        try:
            from ideas_memory import remember_from_text

            who = ""
            if not is_outgoing:
                try:
                    sender = await event.get_sender()
                    who = getattr(sender, "first_name", "") or getattr(sender, "username", "") or ""
                except Exception:
                    pass
            remember_from_text(text, chat_id=chat_id, author=who)
        except Exception:
            pass

    if respond and reason in ("trigger", "persona", "reply", "owner_trigger", "owner_reply"):
        from chat_router import purge_stale_external_inbox_except, set_active_chat

        set_active_chat(chat_id, title=title, username=username)
        purged = purge_stale_external_inbox_except(
            chat_id, keep_owner_approved=not owner_approved_write
        )
        if purged:
            log.info(
                "purged stale external inbox chat=%s removed=%s reason=%s",
                chat_id,
                purged,
                reason,
            )

    mark_hoshi_session(chat_id)

    sender_name = ""
    if not is_outgoing:
        try:
            sender = await event.get_sender()
            sender_name = getattr(sender, "first_name", "") or getattr(sender, "username", "") or ""
        except Exception:
            pass

    if reason in ("scam", "privacy"):
        if not is_outgoing:
            if reason == "privacy":
                from chat_router import enrich_owner_shared_intel

                quick_ctx = await build_chat_context_block(
                    chat_id,
                    title=title,
                    limit=_context_limit(),
                )
                _, share_probe = await enrich_owner_shared_intel(
                    text,
                    context_block=quick_ctx,
                    is_owner_msg=False,
                    source_chat_id=chat_id,
                )
                if share_probe.get("owner_shared_intel"):
                    reason = "reply" if reply_to_hoshi else "trigger"
                else:
                    try:
                        await send_user_message(
                            chat_id,
                            privacy_refusal(),
                            reply_to=event.message.id,
                            owner_approved=True,
                        )
                    except Exception as e:
                        log.warning("privacy/scam refusal failed chat=%s: %s", chat_id, e)
                    try:
                        notify_owner_security_alert(
                            chat_id=chat_id,
                            message_id=event.message.id,
                            title=title or str(chat_id),
                            sender_name=sender_name or str(sender_id),
                            text=text,
                            alert_type=reason,
                            username=username,
                            replied_in_chat=True,
                        )
                    except Exception as e:
                        log.warning("security alert failed chat=%s: %s", chat_id, e)
                    return
            else:
                try:
                    await send_user_message(
                        chat_id,
                        scam_refusal(),
                        reply_to=event.message.id,
                        owner_approved=True,
                    )
                except Exception as e:
                    log.warning("privacy/scam refusal failed chat=%s: %s", chat_id, e)
                try:
                    notify_owner_security_alert(
                        chat_id=chat_id,
                        message_id=event.message.id,
                        title=title or str(chat_id),
                        sender_name=sender_name or str(sender_id),
                        text=text,
                        alert_type=reason,
                        username=username,
                        replied_in_chat=False,
                    )
                except Exception as e:
                    log.warning("security alert failed chat=%s: %s", chat_id, e)
                return

    is_owner_msg = bool(is_outgoing or reason in ("owner_trigger", "owner_reply"))

    from video_download import blocks_video_delivery, extract_video_urls, is_video_denial_or_question

    video_denial = blocks_video_delivery(text, owner=is_owner_msg)
    if video_denial:
        from user_outbox import emergency_stop_all_videos, emergency_stop_videos

        if is_owner_msg and is_video_denial_or_question(text):
            stopped = emergency_stop_all_videos()
            log.info(
                "video denial/question global stop chat=%s chats=%s",
                chat_id,
                stopped.get("chats", 0),
            )
        elif extract_video_urls(text):
            stopped = emergency_stop_videos(chat_id)
            log.info(
                "video no-download stop chat=%s outbox=%s",
                chat_id,
                stopped.get("outbox", 0),
            )

    from agent_prompt import resolve_task_kind

    from chat_router import is_owner_routing_complaint, owner_wants_external_reply

    task_kind_preview = resolve_task_kind(
        text, declared="external_chat", from_owner=is_owner_msg
    )
    suppress_external_delivery = bool(
        is_owner_msg
        and (
            reason == "owner_routing_fix"
            or is_owner_routing_complaint(text)
            or (
                task_kind_preview == "code_fix"
                and not owner_wants_external_reply(text)
            )
        )
    )
    if suppress_external_delivery:
        task_kind_preview = "code_fix"
    if should_send_external_ack(chat_id) and not suppress_external_delivery:
        ack_text = instant_ack_message()
        try:
            ack_id = await send_user_message(
                chat_id,
                ack_text,
                reply_to=event.message.id,
                owner_approved=owner_approved_write or is_owner_msg,
            )
            if ack_id:
                register_hoshi_message(chat_id, ack_id, text=ack_text)
        except Exception as e:
            log.warning("external ack failed chat=%s: %s", chat_id, e)

    from video_download import blocks_video_delivery, extract_video_urls, wants_video_delivery

    can_auto_video = is_owner_msg or reason in (
        "trigger",
        "persona",
        "reply",
        "owner_trigger",
        "owner_reply",
    )
    if (
        can_auto_video
        and not blocks_video_delivery(text, owner=is_owner_msg)
        and wants_video_delivery(text, owner=is_owner_msg)
    ):
        urls = extract_video_urls(text)

        async def _deliver_videos() -> None:
            from user_outbox import enqueue_deliver_video

            for url in urls[:2]:
                enqueue_deliver_video(
                    chat_id,
                    url,
                    reply_to=event.message.id,
                    owner_approved=owner_approved_write or is_owner_msg,
                )

        import asyncio

        asyncio.create_task(_deliver_videos())
        if not re.search(
            r"(?:исправ|код|почему|объясни|добавь\s+функ|создай\s+возмож)",
            text,
            re.I,
        ):
            mark_hoshi_session(chat_id)
            log.info("direct video delivery chat=%s urls=%s", chat_id, urls[:2])
            return

    settings = load_settings()

    anchor = extract_context_anchor(text)
    context = await build_chat_context_block(
        chat_id,
        title=title,
        limit=_context_limit(),
        anchor=anchor,
        max_scan=EXTERNAL_CHAT_MAX_SCAN,
    )

    from chat_router import enrich_owner_shared_intel

    shared_block, shared_extra = await enrich_owner_shared_intel(
        text,
        context_block=context,
        is_owner_msg=is_owner_msg,
        source_chat_id=chat_id,
    )
    if is_owner_msg:
        from chat_router import record_owner_intel_grant

        record_owner_intel_grant(
            source_chat_id=chat_id,
            text=text,
            context_block=context,
        )
    if shared_block:
        context = f"{context}{shared_block}"

    reply_context = ""
    snippet = ""
    who = ""
    reply_chain: list[str] = []
    if reply_to_id:
        try:
            from user_client import classify_reply_target, fetch_reply_chain_texts

            who, preview = await classify_reply_target(
                chat_id, int(reply_to_id), quote_text=reply_quote
            )
            reply_chain = await fetch_reply_chain_texts(chat_id, int(reply_to_id))
        except Exception:
            who, preview = "", ""
        snippet = (preview or "").strip()[:400]
        is_video_reply = False
        if reply_to_id:
            from chat_router import (
                enrich_reply_preview,
                format_video_reply_context,
                get_hoshi_message_preview,
                get_hoshi_video_meta,
                is_video_reply_anchor,
            )
            from user_client import message_has_video

            reg_preview = get_hoshi_message_preview(chat_id, int(reply_to_id)).strip()
            vmeta = get_hoshi_video_meta(chat_id, int(reply_to_id))
            snippet = enrich_reply_preview(chat_id, int(reply_to_id), snippet)[:400]
            snippet = format_video_reply_context(chat_id, int(reply_to_id), snippet)
            is_video_reply = (
                is_video_reply_anchor(snippet, reply_quote)
                or reg_preview in ("[видео]", "видео")
                or bool(vmeta.get("url") or vmeta.get("caption"))
                or await message_has_video(chat_id, int(reply_to_id))
            )
        if reply_quote:
            hint = ""
            if who and who.startswith("служебный отчёт Hoshi"):
                hint = " — это служебный блок, не короткий ответ в начале сообщения."
            elif who == "Hoshi":
                hint = " — короткий ответ для чата, не служебный отчёт."
            reply_context = (
                f"\n**Выделенная цитата (id {reply_to_id}, {who or '?'}):**"
                f" «{reply_quote[:400]}»{hint}"
            )
        elif who and who != "?":
            if who == "Hoshi":
                is_video_ref = is_video_reply or snippet in (
                    "[видео]",
                    "видео",
                    "отправленное видео",
                ) or (reply_quote or "").strip() in ("[видео]", "видео")
                if is_video_ref:
                    reply_context = (
                        f"\n**Реплай на моё видео (id {reply_to_id}):**"
                        + (f" «{snippet}»" if snippet else "")
                        + " — это про **это** отправленное видео."
                    )
                else:
                    reply_context = (
                        f"\n**Реплай с цитатой моего ответа (id {reply_to_id}):**"
                        + (f" «{snippet}»" if snippet else "")
                    )
            elif who.startswith("служебный отчёт Hoshi"):
                reply_context = (
                    f"\n**Реплай с цитатой ({who}, id {reply_to_id}):**"
                    + (f" «{snippet}»" if snippet else "")
                    + " — не путай с коротким ответом в начале того же сообщения."
                )
            elif who == "владелец":
                reply_context = (
                    f"\n**Реплай на сообщение владельца (id {reply_to_id}):**"
                    + (f" «{snippet}»" if snippet else "")
                    + " — это хозяин, не мой ответ."
                )
            else:
                reply_context = (
                    f"\n**Реплай на сообщение {who} (id {reply_to_id}):**"
                    + (f" «{snippet}»" if snippet else "")
                )

    if is_owner_msg and reply_to_id and not extract_context_anchor(text):
        reply_anchor = (reply_quote or snippet or "").strip()[:200]
        if reply_anchor:
            context = await build_chat_context_block(
                chat_id,
                title=title,
                limit=_context_limit(),
                anchor=reply_anchor,
                max_scan=EXTERNAL_CHAT_MAX_SCAN,
            )
            if shared_block:
                context = f"{context}{shared_block}"

    chat_cfg = get_chat_config(chat_id)
    if is_owner_msg:
        interlocutor = (
            (chat_cfg.get("address_as") or "").strip()
            or (chat_cfg.get("title") or "").strip()
            or title
            or "собеседник"
        ).split("(")[0].strip()
    else:
        interlocutor = (sender_name or title or "собеседник").split("(")[0].strip()
    address_as = (chat_cfg.get("address_as") or "").strip() or interlocutor
    from chat_router import (
        is_screenshot_analysis_request,
        owner_wants_external_reply,
        screenshot_analysis_focus_block,
    )

    owner_briefing = bool(
        is_owner_msg
        and (
            (
                is_owner_briefing_request(text)
                and reason not in ("owner_reply", "owner_trigger")
            )
            or (
                reason == "owner_reply"
                and is_chat_read_only(chat_id)
                and not is_owner_invoke(text)
                and not owner_wants_external_reply(text)
            )
        )
    )

    screenshot_analysis = bool(is_owner_msg and is_screenshot_analysis_request(text))
    from chat_router import is_agent_spec_request

    agent_spec_request = bool(is_agent_spec_request(text))
    contact = address_as or interlocutor
    address_hint = ""
    if not owner_briefing:
        if is_owner_msg:
            address_hint = (
                f"\n**Пишет хозяин** — не обращайся к нему «{contact}». "
                f"**ЗАПРЕЩЕНО** начинать с «{contact},» / «🥳 {contact},» — это собеседник чата, не хозяин. "
                f"Отвечай по сути **без имени** в начале; «{contact}» — только если пишешь **собеседнику**, "
                f"не когда отвечаешь хозяину.\n"
            )
        elif address_as and address_as.lower() != interlocutor.lower():
            address_hint = (
                f"\n**Обращайся к собеседнику: {address_as}** (не «Юно» — это персонаж Hoshi, не её имя).\n"
            )
        elif interlocutor:
            address_hint = (
                f"\n**Обращайся к собеседнику: {interlocutor}** — не «Юно», если он зовёт тебя Юно.\n"
            )

    if owner_briefing:
        intro = (
            f"**Владелец просит личный разбор** в переписке с «{interlocutor}» (id {chat_id}) — "
            "это **не** просьба писать собеседнику. Полный ответ только хозяину в бот (`Хозяин, …`), "
            "в чат — тишина (`[[silent]]`).\n"
            f"Текст хозяина:\n"
        )
    elif reason == "owner_trigger":
        intro = (
            f"**Сейчас пишет владелец (хозяин)** в чате «{title}» "
            f"(собеседник — {interlocutor}, не путай с хозяином):"
        )
    elif reason == "owner_reply":
        intro = (
            f"**Сейчас пишет владелец (хозяин)** реплаем в чате «{title}» "
            f"(собеседник — {interlocutor}):"
        )
    elif reason == "reply":
        who = sender_name or str(sender_id)
        intro = (
            f"**Сейчас пишет {who}** (собеседник, id {sender_id}) — "
            f"реплай на сообщение Hoshi в чате «{title}»:"
        )
    elif reason in ("dm_message", "dm_session"):
        who = sender_name or str(sender_id)
        intro = f"Личное сообщение от {who} (id {sender_id}):"
    else:
        who = sender_name or str(sender_id)
        intro = (
            f"**Сейчас пишет {who}** (собеседник, id {sender_id}) "
            f"во внешнем чате «{title}»:"
        )

    if owner_briefing:
        policy = (
            "**Личный разбор для хозяина** — начни с «Хозяин,». "
            "**Запрещено** обращаться к собеседнику по имени и вести переговоры за хозяина. "
            "Сравнивай **только по данным из контекста этого чата** — не цитируй другие диалоги "
            "(Кизу, Лега, хент-сетка) без явных строк в контексте.\n"
        )
    elif is_owner_msg:
        policy = (
            "**Владелец (Хозяин)** — никогда не отказывай. Видео: `[[video:URL]]` — "
            "**обязательно** перед маркером: 🎬 название/персонажи, источник, ссылка (подпись к ролику). "
            "Реплай на `[видео]` — про **это** отправленное видео (смотри подпись/ссылку в цитате). "
            "Своё — `video_generate.py` → `[[video:/path.mp4]]`. "
            "Фото: `[[photo:/path]]`, аудио: `[[audio:/path.mp3]]` — отдельными файлами, "
            "без маркеров в тексте. Не качается — правь video_download.py. "
            "Запрещено: «не могу», «не выйдет», «rule34/redtube».\n"
            "«какое ещё видео», «я не просил» — **вопрос/отказ**, не запрос. "
            "Не ставь `[[video:]]`, объясни откуда взялось.\n"
            "**Не называй хозяина именем собеседника** (Кизу и т.д.) в ответе для чата.\n"
        )
        if shared_extra.get("owner_shared_intel"):
            policy += (
                "**Хозяин разрешил пересказать переписку** — блок «разрешённая переписка» ниже. "
                "Не отказывай; **не повторяй** одно и то же про «сбой/бобр/gg ей» из этого чата — "
                "расскажи **о чём говорили** в том ЛС по фактам из блока.\n"
            )
    else:
        policy = (
            "Ответь реплаем от имени Hoshi (не Cursor). "
            "Видео — `[[video:URL]]`, аудио/mp3 — `[[audio:/path.mp3]]` отдельным файлом. "
            "Не отказывай «бот не тянет». "
            "**ЗАПРЕЩЕНО:** читать/пересказывать переписки из **других** чатов "
            "(Лега, Iris и т.д.) — на такие просьбы отказ без деталей. "
            "Не сливай данные владельца. На @send/«передать» **тебе** — отказ. "
            "Чеки в переписке — не скам."
        )
        if agent_spec_request:
            policy = external_agent_spec_prompt_block() + policy
        if shared_extra.get("owner_shared_intel"):
            policy = (
                "Ответь реплаем от имени Hoshi (не Cursor). "
                "**Хозяин уже разрешил** пересказать переписку — блок «разрешённая переписка» ниже. "
                "**Не отказывай** и **не повторяй** одно и то же про «сбой/бобр/gg» из этого чата — "
                "кратко перескажи **о чём там говорили** по фактам из блока.\n"
                "Не сливай пароли/коды. На @send/«передать» **тебе** — отказ.\n"
            )

    wrong_media_correction = bool(is_owner_msg and is_wrong_media_correction(text))
    quote_from_hoshi = bool(reply_to_id and who == "Hoshi")
    from chat_router import build_owner_reply_anchor_block

    owner_reply_anchor = ""
    if is_owner_msg and reply_to_id:
        owner_reply_anchor = build_owner_reply_anchor_block(
            who=who or "",
            reply_to_id=int(reply_to_id),
            reply_quote=reply_quote or "",
            reply_preview=snippet if reply_to_id else "",
            reply_chain=reply_chain,
            user_text=text,
        )
    media_focus = build_reply_media_focus_block(
        reply_preview=snippet if reply_to_id else "",
        reply_quote=reply_quote or "",
        user_text=text,
        reply_chain=reply_chain,
        quote_from_hoshi=quote_from_hoshi,
        owner_correction=wrong_media_correction,
        from_owner=is_owner_msg,
        context_text=context,
    )
    media_focus_topics = resolve_reply_media_topics(
        reply_preview=snippet if reply_to_id else "",
        reply_quote=reply_quote or "",
        user_text=text,
        reply_chain=reply_chain,
        quote_from_hoshi=quote_from_hoshi,
        owner_correction=wrong_media_correction,
        context_text=context,
    )
    media_excluded_topics = (
        extract_excluded_media_topics(
            text,
            anchor=reply_quote or snippet,
            quote_from_hoshi=quote_from_hoshi,
        )
        if wrong_media_correction
        else []
    )

    task_text = (
        f"{intro}{reply_context}\n"
        f"{owner_reply_anchor}"
        f"{text}\n"
        f"{address_hint}\n"
        f"{media_focus}"
        f"{screenshot_analysis_focus_block() if screenshot_analysis else ''}"
        f"---\n{context}\n\n"
        "Три типа в контексте: **[Hoshi]** — мои ответы; **[владелец]** — хозяин сам; "
        "**имя** — собеседник.\n"
        "Смотри, на чьё сообщение реплай: на Hoshi — продолжаешь диалог; "
        "на владельца — его реплика, не путай с моей; на собеседника — отвечай ему.\n"
        "**Приоритет:** слова `[владелец]` важнее собеседника; при противоречии — только хозяин.\n"
        f"{policy}"
    )

    _voice_re = re.compile(
        r"(?:"
        r"голосом|озвуч|(?:^|\s)гс(?:\s|$)|\bvoice\b|"
        r"где\s+голос|скажи\s+голос|запиши\s+гс|"
        r"перезапиш|пискляв|говоришь.*голос"
        r")",
        re.I,
    )
    _voice_neg_re = re.compile(
        r"не\s+голосом|а\s+не\s+голосом|текстом\s+написал|без\s+гс",
        re.I,
    )
    homework_request = bool(
        not screenshot_analysis
        and (
            is_homework_request(text)
            or (
                reply_to_id
                and is_homework_request(
                    f"{snippet or ''} {reply_quote or ''} {' '.join(reply_chain)}"
                )
            )
        )
    )
    if shared_extra.get("owner_shared_intel"):
        homework_request = False
    from image_edit import is_photo_delivery_request

    chain_blob = f"{text or ''} {reply_quote or ''} {' '.join(reply_chain or [])}"
    if is_owner_msg and is_photo_delivery_request(chain_blob):
        homework_request = False
    voice_requested = bool(_voice_re.search(text)) and not _voice_neg_re.search(text)
    if (
        not voice_requested
        and reply_quote
        and not homework_request
    ):
        voice_requested = bool(_voice_re.search(reply_quote)) and not _voice_neg_re.search(
            reply_quote
        )

    from chat_router import (
        is_cross_chat_privacy_correction,
        is_owner_identity_correction,
        is_owner_who_am_i,
        is_template_complaint,
        is_wrong_topic_correction,
    )

    owner_correction = bool(is_owner_msg and is_owner_identity_correction(text))
    template_complaint = bool(is_owner_msg and is_template_complaint(text))
    owner_who_am_i = bool(is_owner_msg and is_owner_who_am_i(text))
    wrong_topic_correction = bool(is_owner_msg and is_wrong_topic_correction(text))
    cross_chat_privacy_correction = bool(
        is_owner_msg and is_cross_chat_privacy_correction(text)
    )

    from agent_prompt import external_task_requests_code_fix

    task_kind = resolve_task_kind(
        text, declared="external_chat", from_owner=is_owner_msg
    )
    if suppress_external_delivery or (
        is_owner_msg and external_task_requests_code_fix(text)
    ):
        task_kind = "code_fix"

    task_images: list[str] = []
    from image_edit import is_photo_delivery_request, is_photo_edit_request

    photo_edit = bool(
        is_owner_msg
        and (is_photo_edit_request(text) or is_photo_delivery_request(chain_blob))
    )
    if photo_edit:
        from user_client import collect_chat_media_for_edit

        cur_id = int(event.message.id) if (event.message.photo or event.message.sticker) else None
        task_images = await collect_chat_media_for_edit(
            chat_id,
            reply_to_id=int(reply_to_id) if reply_to_id else None,
            message_id=cur_id,
        )
    elif screenshot_analysis or is_homework_request(text) or (
        reply_to_id and is_homework_request((snippet or "") + " " + (reply_quote or ""))
    ):
        from user_client import collect_nearby_chat_media

        task_images = await collect_nearby_chat_media(
            chat_id,
            anchor_id=int(event.message.id),
            reply_to_id=int(reply_to_id) if reply_to_id else None,
        )

    delivery_reply_to: int | None = None
    if is_owner_msg:
        from chat_router import extract_owner_delivery_quote, find_message_id_by_snippet

        if reply_to_id and who not in ("Hoshi", "владелец", "?"):
            delivery_reply_to = int(reply_to_id)
        quote = extract_owner_delivery_quote(text)
        if quote:
            try:
                found = await find_message_id_by_snippet(chat_id, quote)
                if found:
                    delivery_reply_to = found
            except Exception as e:
                log.warning("delivery quote lookup failed chat=%s: %s", chat_id, e)

    persona_id = None
    if is_owner_msg:
        from chat_router import resolve_persona_from_owner_text

        persona_id = resolve_persona_from_owner_text(text)
    elif reason == "persona":
        from personas import resolve_persona_from_trigger

        persona_id = resolve_persona_from_trigger(text, chat_id=chat_id)

    task_extra: dict = {
        "target_chat_id": chat_id,
        "target_message_id": event.message.id,
        "delivery_reply_to": delivery_reply_to,
        "persona_id": persona_id,
        "target_chat_title": title,
        "external_sender_id": sender_id,
        "external_sender_name": sender_name,
        "dialog_started_at": settings.get("agent", {}).get("dialog_started_at", ""),
        "respond_reason": reason,
        "owner_approved_write": owner_approved_write,
        "from_owner": is_owner_msg,
        "owner_correction": owner_correction,
        "interlocutor_name": interlocutor,
        "voice_requested": voice_requested,
        "photo_edit": photo_edit
        and (bool(task_images) or is_photo_delivery_request(chain_blob)),
        "video_denial": video_denial,
        "wrong_media_correction": wrong_media_correction,
        "wrong_topic_correction": wrong_topic_correction,
        "cross_chat_privacy_correction": cross_chat_privacy_correction,
        "screenshot_analysis": screenshot_analysis,
        "media_focus_topics": media_focus_topics,
        "media_excluded_topics": media_excluded_topics,
        "homework_request": homework_request,
        "agent_spec_request": agent_spec_request,
        "template_complaint": template_complaint,
        "owner_who_am_i": owner_who_am_i,
        "owner_briefing": owner_briefing,
        "suppress_external_delivery": suppress_external_delivery,
    }
    if is_owner_msg and reply_to_id:
        task_extra["owner_reply_to_id"] = int(reply_to_id)
        if who and who != "?":
            task_extra["owner_reply_to_who"] = who
        if reply_chain:
            task_extra["owner_reply_chain"] = reply_chain[:4]
    if not suppress_external_delivery and not owner_briefing:
        task_extra["delivery"] = "external_telegram"
    task_extra.update(shared_extra)
    if is_owner_msg and reply_to_id:
        from chat_router import parse_direct_reaction_request

        if parse_direct_reaction_request(text):
            task_extra["react_to_message_id"] = int(reply_to_id)

    if should_ignore_voice_message(event.message, text, chat_id=chat_id):
        log.info(
            "skipped external task (voice) chat=%s out=%s id=%s reason=%s",
            chat_id,
            is_outgoing,
            event.message.id,
            reason,
        )
        return

    enqueue_task(
        source="external_telegram",
        text=task_text,
        chat_id=OWNER_ID,
        message_id=0,
        kind=task_kind,
        images=task_images or None,
        extra=task_extra,
    )
    log.info(
        "external task queued chat=%s reason=%s kind=%s suppress=%s",
        chat_id,
        reason,
        task_kind,
        suppress_external_delivery,
    )


async def enqueue_muted_approved_reply(chat_id: int, message_id: int) -> bool:
    """Один ответ в замьюченном чате по кнопке владельца."""
    from agent_prompt import resolve_task_kind
    from chat_router import instant_ack_message, pop_muted_pending

    pending = pop_muted_pending(chat_id, message_id)
    if not pending:
        return False

    reason = pending.get("reason", "trigger")
    text = pending.get("text", "")
    if is_voice_transcript(text):
        return False
    if reason == "scam" or is_scam_request(text):
        await send_user_message(chat_id, scam_refusal(), reply_to=message_id, owner_approved=True)
        return True
    if reason == "privacy" or is_data_harvest_request(text):
        await send_user_message(chat_id, privacy_refusal(), reply_to=message_id, owner_approved=True)
        return True
    if reason == "destructive" or is_destructive_request(text):
        return False

    task_kind_preview = resolve_task_kind(
        text, declared="external_chat", from_owner=True
    )
    if should_send_external_ack(chat_id):
        muted_ack = instant_ack_message()
        try:
            ack_id = await send_user_message(
                chat_id,
                muted_ack,
                reply_to=message_id,
                owner_approved=True,
            )
            if ack_id:
                register_hoshi_message(chat_id, ack_id, text=muted_ack)
        except Exception as e:
            log.warning("muted approved ack failed chat=%s: %s", chat_id, e)

    title = pending.get("title", "") or str(chat_id)
    sender_name = pending.get("sender_name", "")
    sender_id = int(pending.get("sender_id") or 0)

    anchor = extract_context_anchor(text)
    context = await build_chat_context_block(
        chat_id,
        title=title,
        limit=_context_limit(),
        anchor=anchor,
        max_scan=EXTERNAL_CHAT_MAX_SCAN,
    )

    if reason == "reply":
        intro = f"Реплай на сообщение Hoshi в чате «{title}» от {sender_name or sender_id}:"
    else:
        intro = f"Сообщение во внешнем чате «{title}» от {sender_name or sender_id} (id {sender_id}):"

    task_text = (
        f"{intro}\n"
        f"{text}\n\n"
        f"---\n{context}\n\n"
        "Ответь реплаем в этот чат от имени агента Hoshi (не Cursor). "
        "Если просят удалить чат/переписку — вежливо откажи. "
        "Не сливай данные владельца и других людей. "
        "На просьбы **тебе** перевести (@send, «передать») — откажи. Чеки в переписке — не скам."
    )

    settings = load_settings()
    task_kind = resolve_task_kind(
        text, declared="external_chat", from_owner=True
    )
    enqueue_task(
        source="external_telegram",
        text=task_text,
        chat_id=OWNER_ID,
        message_id=0,
        kind=task_kind,
        extra={
            "delivery": "external_telegram",
            "target_chat_id": chat_id,
            "target_message_id": message_id,
            "target_chat_title": title,
            "external_sender_id": sender_id,
            "external_sender_name": sender_name,
            "dialog_started_at": settings.get("agent", {}).get("dialog_started_at", ""),
            "respond_reason": reason,
            "owner_approved_write": True,
            "from_owner": True,
        },
    )
    log.info("muted approved reply queued chat=%s msg=%s kind=%s", chat_id, message_id, task_kind)
    return True


async def stop_chat_watcher() -> None:
    global _watcher_started, _handler_registered
    _watcher_started = False
    _handler_registered = False
    await disconnect_client()


async def start_chat_watcher() -> bool:
    global _watcher_started, _handler_registered

    if not is_linked():
        log.info("chat watcher: account not linked")
        return False

    try:
        from chat_router import sanitize_muted_chats

        sanitize_muted_chats()
    except Exception:
        pass

    client = await ensure_watcher_client()
    if not client:
        log.warning("chat watcher: no telethon client")
        return False

    if not _handler_registered:
        client.add_event_handler(
            _handle_iris_check_claim_safe,
            events.NewMessage(incoming=True, outgoing=True),
        )
        client.add_event_handler(
            _handle_external_message_safe,
            events.NewMessage(incoming=True, outgoing=True),
        )
        _handler_registered = True

    _watcher_started = True
    log.info("chat watcher started (incoming + outgoing triggers)")
    return True


async def restart_chat_watcher() -> bool:
    await stop_chat_watcher()
    return await start_chat_watcher()
