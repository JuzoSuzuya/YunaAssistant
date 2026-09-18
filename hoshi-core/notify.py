#!/usr/bin/env python3
"""Отправка сообщений в Telegram."""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path

from telegram import Bot, InlineKeyboardMarkup
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest

from config import BOT_TOKEN
from text_format import prepare_telegram_html, strip_formatting


def _format_outgoing_text(text: str) -> str:
    try:
        from rich_content import process_rich_content

        return process_rich_content(text)
    except Exception:
        return text

_DRAFT_MAX_LEN = 4000

log = logging.getLogger("hoshi.notify")


async def send_message(
    chat_id: int,
    text: str,
    *,
    parse_mode: str | None = ParseMode.HTML,
    reply_to: int | None = None,
    entities: list | None = None,
    message_thread_id: int | None = None,
    direct_messages_topic_id: int | None = None,
) -> int | None:
    bot = Bot(BOT_TOKEN)
    topic_kw: dict[str, int] = {}
    if message_thread_id:
        topic_kw["message_thread_id"] = int(message_thread_id)
    elif direct_messages_topic_id:
        topic_kw["direct_messages_topic_id"] = int(direct_messages_topic_id)

    if entities is not None:
        try:
            msg = await bot.send_message(
                chat_id=chat_id,
                text=text,
                entities=entities,
                reply_to_message_id=reply_to,
                **topic_kw,
            )
            return int(msg.message_id)
        except BadRequest as e:
            log.warning("entities send failed, fallback to html: %s", e)

    if parse_mode == ParseMode.HTML:
        html_text = prepare_telegram_html(_format_outgoing_text(text))
        try:
            msg = await bot.send_message(
                chat_id=chat_id,
                text=html_text,
                parse_mode=ParseMode.HTML,
                reply_to_message_id=reply_to,
                **topic_kw,
            )
            return int(msg.message_id)
        except BadRequest as e:
            log.warning("HTML send failed, fallback to plain: %s", e)

    msg = await bot.send_message(
        chat_id=chat_id,
        text=strip_formatting(text),
        reply_to_message_id=reply_to,
        **topic_kw,
    )
    return int(msg.message_id)


def _run_on_bridge_loop(coro) -> None:
    """Безопасно из async-handler (watcher) и из daemon-потока."""
    try:
        asyncio.get_running_loop()
        in_async = True
    except RuntimeError:
        in_async = False

    if in_async:
        asyncio.ensure_future(coro)
        return

    try:
        from user_client import _bridge_loop

        if _bridge_loop and _bridge_loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, _bridge_loop)
            return
    except Exception:
        pass

    asyncio.run(coro)


async def send_photo(
    chat_id: int,
    photo_path: str,
    *,
    reply_to: int | None = None,
    caption: str = "",
) -> int | None:
    """Фото владельцу через Bot API (чат с ботом)."""
    from user_client import _wait_for_photo_path

    path = Path(photo_path)
    resolved = await _wait_for_photo_path(path)
    if not resolved:
        log.warning("photo file missing: %s", photo_path)
        return None
    bot = Bot(BOT_TOKEN)
    cap = strip_formatting(caption)[:1024] if caption else None
    try:
        with open(resolved, "rb") as f:
            msg = await bot.send_photo(
                chat_id=chat_id,
                photo=f,
                caption=cap,
                reply_to_message_id=reply_to,
            )
        return int(msg.message_id)
    except BadRequest as e:
        log.warning("photo send failed: %s", e)
        return None


def send_photo_sync(
    chat_id: int,
    photo_path: str,
    *,
    reply_to: int | None = None,
    caption: str = "",
) -> int | None:
    fut = None
    try:
        from user_client import _bridge_loop

        if _bridge_loop and _bridge_loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(
                send_photo(chat_id, photo_path, reply_to=reply_to, caption=caption),
                _bridge_loop,
            )
            return fut.result(timeout=120)
    except Exception:
        pass
    return asyncio.run(send_photo(chat_id, photo_path, reply_to=reply_to, caption=caption))


def send_message_sync(
    chat_id: int,
    text: str,
    *,
    reply_to: int | None = None,
    entities: list | None = None,
    message_thread_id: int | None = None,
    direct_messages_topic_id: int | None = None,
) -> int | None:
    fut = None
    try:
        from user_client import _bridge_loop

        if _bridge_loop and _bridge_loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(
                send_message(
                    chat_id,
                    text,
                    reply_to=reply_to,
                    entities=entities,
                    message_thread_id=message_thread_id,
                    direct_messages_topic_id=direct_messages_topic_id,
                ),
                _bridge_loop,
            )
            return fut.result(timeout=30)
    except Exception:
        pass
    return asyncio.run(
        send_message(
            chat_id,
            text,
            reply_to=reply_to,
            entities=entities,
            message_thread_id=message_thread_id,
            direct_messages_topic_id=direct_messages_topic_id,
        )
    )


def send_owner_message_sync(
    chat_id: int,
    text: str,
    *,
    reply_to: int | None = None,
    message_thread_id: int | None = None,
    direct_messages_topic_id: int | None = None,
) -> int | None:
    """Ответ хозяину в бот — с премиум ✨ в начале."""
    from config import OWNER_ID
    from text_format import owner_bot_outbound_parts

    if int(chat_id) == int(OWNER_ID):
        outbound, entities = owner_bot_outbound_parts(text[:4000])
        return send_message_sync(
            chat_id,
            outbound,
            reply_to=reply_to,
            entities=entities,
            message_thread_id=message_thread_id,
            direct_messages_topic_id=direct_messages_topic_id,
        )
    return send_message_sync(
        chat_id,
        text,
        reply_to=reply_to,
        message_thread_id=message_thread_id,
        direct_messages_topic_id=direct_messages_topic_id,
    )


def send_message_with_keyboard_sync(
    chat_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    _run_on_bridge_loop(send_message_with_keyboard(chat_id, text, reply_markup))


async def send_message_with_keyboard(
    chat_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup,
    *,
    parse_mode: str | None = ParseMode.HTML,
) -> None:
    bot = Bot(BOT_TOKEN)
    html_text = text
    if parse_mode == ParseMode.HTML:
        html_text = prepare_telegram_html(_format_outgoing_text(text))
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=html_text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
    except BadRequest as e:
        log.warning("keyboard send failed, fallback: %s", e)
        await bot.send_message(
            chat_id=chat_id,
            text=strip_formatting(text),
            reply_markup=reply_markup,
        )


async def send_typing(chat_id: int) -> None:
    bot = Bot(BOT_TOKEN)
    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)


def send_typing_sync(chat_id: int) -> None:
    asyncio.run(send_typing(chat_id))


def typing_loop(
    chat_id: int,
    stop: threading.Event,
    *,
    interval: float = 4.0,
    max_duration: float = 480.0,
) -> None:
    """Периодически шлёт «печатает…» (не дольше max_duration, TG ~5 с на action)."""
    started = time.monotonic()
    while not stop.is_set():
        if time.monotonic() - started > max_duration:
            break
        try:
            send_typing_sync(chat_id)
        except Exception as e:
            log.debug("typing action failed: %s", e)
        stop.wait(interval)


async def send_message_draft(
    chat_id: int,
    draft_id: int,
    text: str,
    *,
    parse_mode: str | None = ParseMode.HTML,
    entities: list | None = None,
) -> None:
    """Потоковый черновик (пустой text → placeholder «Thinking…»)."""
    bot = Bot(BOT_TOKEN)
    payload = text[:_DRAFT_MAX_LEN]
    if entities:
        draft_parse_mode = None
    elif parse_mode == ParseMode.HTML and payload:
        payload = prepare_telegram_html(_format_outgoing_text(payload))
        draft_parse_mode = parse_mode
    else:
        draft_parse_mode = parse_mode if (payload and parse_mode) else None
    try:
        await bot.send_message_draft(
            chat_id=chat_id,
            draft_id=draft_id,
            text=payload,
            parse_mode=draft_parse_mode,
            entities=entities if payload else None,
        )
    except BadRequest as e:
        if not payload:
            raise
        log.debug("draft HTML failed, fallback plain: %s", e)
        await bot.send_message_draft(
            chat_id=chat_id,
            draft_id=draft_id,
            text=strip_formatting(text[:_DRAFT_MAX_LEN]),
        )


def send_message_draft_sync(
    chat_id: int,
    draft_id: int,
    text: str,
    *,
    parse_mode: str | None = ParseMode.HTML,
    entities: list | None = None,
) -> None:
    try:
        from user_client import _bridge_loop

        if _bridge_loop and _bridge_loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(
                send_message_draft(
                    chat_id,
                    draft_id,
                    text,
                    parse_mode=parse_mode,
                    entities=entities,
                ),
                _bridge_loop,
            )
            fut.result(timeout=30)
            return
    except Exception:
        pass
    asyncio.run(
        send_message_draft(
            chat_id,
            draft_id,
            text,
            parse_mode=parse_mode,
            entities=entities,
        )
    )


def owner_draft_status_text(partial: str) -> str:
    """Короткий статус для черновика хозяину — не дублирует финальный ответ."""
    partial = strip_formatting((partial or "").strip())
    if not partial:
        return "думаю…"
    tier = min(len(partial) // 120, 2)
    return ("думаю…", "пишу…", "почти…")[tier]


class WorkIndicator:
    """«Печатает» / статус-черновик — только пока агент реально работает."""

    def __init__(
        self,
        chat_id: int | None,
        draft_id: int | None = None,
        *,
        external: bool = False,
    ) -> None:
        self.chat_id = int(chat_id) if chat_id else 0
        self.draft_id = int(draft_id) if draft_id else 0
        self.external = external
        self._streamer: DraftStreamer | None = None
        self._typing_stop: threading.Event | None = None
        self._active = False

    def start(self) -> None:
        if self._active or not self.chat_id:
            return
        from config import OWNER_ID

        self._active = True
        if self.external:
            from user_client import typing_loop as user_typing_loop

            self._typing_stop = threading.Event()
            threading.Thread(
                target=user_typing_loop,
                args=(self.chat_id, self._typing_stop),
                kwargs={"interval": 3.0},
                daemon=True,
            ).start()
            return
        if self.chat_id == OWNER_ID and self.draft_id:
            self._streamer = DraftStreamer(
                self.chat_id,
                self.draft_id,
                owner_status_only=True,
            )
            self._streamer.update("", force=True)
        elif self.chat_id == OWNER_ID:
            self._typing_stop = threading.Event()
            threading.Thread(
                target=typing_loop,
                args=(self.chat_id, self._typing_stop),
                daemon=True,
            ).start()

    def update(self, text: str) -> None:
        if self._streamer and text:
            self._streamer.update(text)

    def stop(self) -> None:
        if not self._active:
            return
        self._active = False
        if self._typing_stop:
            self._typing_stop.set()
            self._typing_stop = None
        if self._streamer:
            try:
                self._streamer.clear()
            except Exception as e:
                log.debug("work indicator clear failed: %s", e)
            self._streamer = None


class DraftStreamer:
    """Throttled sendMessageDraft из фонового потока."""

    def __init__(
        self,
        chat_id: int,
        draft_id: int,
        *,
        min_interval: float = 0.35,
        owner_status_only: bool = False,
    ) -> None:
        self.chat_id = chat_id
        self.draft_id = draft_id
        self.min_interval = min_interval
        self.owner_status_only = owner_status_only
        self._last_text = ""
        self._last_sent_at = 0.0
        self._delivered = False
        self._lock = threading.Lock()

    def update(self, text: str, *, force: bool = False) -> None:
        entities: list | None = None
        if self.owner_status_only:
            from text_format import owner_bot_outbound_parts

            status = owner_draft_status_text(text)
            outbound, entities = owner_bot_outbound_parts(status)
            text = outbound
        else:
            # Черновик — только plain preview: sendMessageDraft не рендерит HTML/Markdown.
            text = strip_formatting(text[:_DRAFT_MAX_LEN])
        with self._lock:
            if text == self._last_text and not force:
                return
            self._last_text = text
            now = time.monotonic()
            if not force and (now - self._last_sent_at) < self.min_interval:
                return
            self._last_sent_at = now
        try:
            send_message_draft_sync(
                self.chat_id,
                self.draft_id,
                text,
                parse_mode=None,
                entities=entities,
            )
            self._delivered = True
        except Exception as e:
            log.debug("draft update failed: %s", e)

    def clear(self) -> None:
        """Сбрасывает черновик перед финальным sendMessage."""
        with self._lock:
            self._last_text = ""
        try:
            send_message_draft_sync(self.chat_id, self.draft_id, "", parse_mode=None)
        except Exception as e:
            log.debug("draft clear failed: %s", e)

    @property
    def delivered(self) -> bool:
        return self._delivered
