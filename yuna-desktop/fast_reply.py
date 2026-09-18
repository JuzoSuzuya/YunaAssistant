#!/usr/bin/env python3
"""Мгновенные ответы — только если фраза начинается с «Юна»."""
from __future__ import annotations

import random
import re

ONLY_WAKE_RE = re.compile(r"^(юна|yuna|юно)[\s!.,?…]*$", re.I)

_ACK = ("Да?", "Слушаю.", "Я здесь.")


def starts_with_yuna(text: str) -> bool:
    from wake_word import starts_with_yuna as _s

    return _s(text)


def strip_wake(text: str) -> str:
    from wake_word import command_after_yuna

    return command_after_yuna(text)


def extract_command(text: str) -> str:
    if starts_with_yuna(text):
        return strip_wake(text)
    return ""


def is_wake_only(text: str) -> bool:
    cmd = strip_wake(text)
    return starts_with_yuna(text) and len(cmd) < 2


def try_fast_reply(text: str) -> str | None:
    """Только wake «Юна» и простые вежливости — без обоев/NLU."""
    if not starts_with_yuna(text):
        return None
    if is_wake_only(text):
        return random.choice(_ACK)
    command = strip_wake(text)
    low = command.lower()
    if low in ("спасибо", "thanks"):
        return "Пожалуйста, хозяин."
    if low in ("пока", "до свидания"):
        return "Пока!"
    # обои/музыка/остальное — не перехватываем, отвечает Cursor
    return None


def try_action_reply(text: str) -> str | None:
    """Устарело для панели: NLU через regex тупит. Оставляем для голоса/совместимости."""
    command = text.strip()
    if not command:
        return None
    if starts_with_yuna(command):
        command = strip_wake(command)
    if not command:
        return None
    try:
        from actions import handle_action

        done = handle_action(command)
    except Exception:
        return None
    if not done:
        return None
    low = done.lower()
    if "не нашла обоев" in low:
        return None
    return done
