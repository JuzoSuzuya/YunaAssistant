#!/usr/bin/env python3
"""Загрузка фото из Telegram для агента."""
from __future__ import annotations

from pathlib import Path

from telegram import Message
from telegram.ext import ExtBot

from config import MEDIA_DIR

_IMAGE_EXTS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def _ext_for_message(message: Message) -> str:
    if message.document and message.document.mime_type:
        return _IMAGE_EXTS.get(message.document.mime_type, ".jpg")
    return ".jpg"


async def download_message_image(
    bot: ExtBot,
    message: Message,
    dest_dir: Path,
    index: int,
) -> str | None:
    file_id: str | None = None
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        file_id = message.document.file_id
    if not file_id:
        return None

    tg_file = await bot.get_file(file_id)
    ext = _ext_for_message(message)
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"{index:02d}{ext}"
    await tg_file.download_to_drive(custom_path=path)
    return str(path.resolve())


async def download_images(
    bot: ExtBot,
    messages: list[Message],
    batch_id: str,
) -> list[str]:
    dest = MEDIA_DIR / batch_id
    paths: list[str] = []
    for i, msg in enumerate(messages, start=1):
        path = await download_message_image(bot, msg, dest, i)
        if path:
            paths.append(path)
    return paths


def caption_from_messages(messages: list[Message]) -> str:
    for msg in messages:
        if msg.caption and msg.caption.strip():
            return msg.caption.strip()
    return ""
