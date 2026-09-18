#!/usr/bin/env python3
"""Диспетчер: только light-демон понимает запрос и решает light/heavy."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from agent_prompt import _external_user_message, _kind_source


_DESCRIBE_MEDIA_RE = re.compile(
    r"(?:"
    r"опиш\w*|разбер\w*|рассмотр\w*|что\s+на\s+(?:гиф|аватар|фото|картин)|"
    r"гифк\w*|аватар\w*|скрин\w*|кто\s+там|откуда\s+это|"
    r"describe|avatar|gif\b"
    r")",
    re.I,
)

_ARCHIVE_RE = re.compile(
    r"(?:помнишь|вспомни|из\s+архив|переписк\w+\s+с|что\s+было\s+с)",
    re.I,
)

_HEAVY_TEXT_LEN = 5500


@dataclass
class RouteDecision:
    worker: str  # "light" | "heavy"
    intent: str
    ack: str | None = None
    focus: dict[str, Any] = field(default_factory=dict)


def _user_probe(item: dict[str, Any], *, kind: str, head: str) -> str:
    text = item.get("text") or ""
    if kind == "external_chat":
        probe = _external_user_message(text)
        if probe:
            return probe
        try:
            from chat_router import extract_external_user_text

            return extract_external_user_text(head) or head
        except Exception:
            return head
    return _kind_source(text) or head


def _build_focus(item: dict[str, Any], *, intent: str, probe: str) -> dict[str, Any]:
    extra = item.get("extra") or {}
    focus: dict[str, Any] = {
        "intent": intent,
        "target_chat_id": extra.get("target_chat_id"),
        "target_message_id": extra.get("target_message_id"),
        "delivery_reply_to": extra.get("delivery_reply_to"),
        "interlocutor": extra.get("interlocutor_name") or extra.get("external_sender_name"),
        "reply_to_message_id": extra.get("target_message_id"),
    }
    if intent == "describe_media":
        focus["anchor"] = "reply_chain_and_attached_media"
        focus["ignore_stale_hoshi"] = True
        if re.search(r"месмарайзер|mesmerizer", probe, re.I):
            focus["warn_wrong_topic"] = "Не путай с Mesmerizer, если реплай на другую гифку"
        if re.search(r"triple\s*baka|трипл", probe, re.I):
            focus["prefer_topic"] = "Triple Baka"
    if extra.get("media_excluded_topics"):
        focus["exclude_topics"] = list(extra.get("media_excluded_topics") or [])
    if extra.get("media_focus_topics"):
        focus["focus_topics"] = list(extra.get("media_focus_topics") or [])
    return focus


def _ack_for_intent(intent: str) -> str:
    from chat_router import instant_ack_message

    return instant_ack_message()


def route_task(
    item: dict[str, Any],
    *,
    kind: str,
    head: str,
) -> RouteDecision:
    """Единственная точка классификации: light или heavy."""
    extra = item.get("extra") or {}
    probe = _user_probe(item, kind=kind, head=head)
    text = item.get("text") or ""
    images = item.get("images") or []

    if kind == "code_fix":
        return RouteDecision(
            worker="heavy",
            intent="code_fix",
            ack=_ack_for_intent("code_fix"),
            focus=_build_focus(item, intent="code_fix", probe=probe),
        )

    if extra.get("screenshot_analysis"):
        return RouteDecision(
            worker="heavy",
            intent="screenshot_analysis",
            ack=_ack_for_intent("screenshot_analysis"),
            focus=_build_focus(item, intent="screenshot_analysis", probe=probe),
        )

    if extra.get("photo_edit") and images:
        return RouteDecision(
            worker="heavy",
            intent="photo_edit",
            ack=_ack_for_intent("photo_edit"),
            focus=_build_focus(item, intent="photo_edit", probe=probe),
        )

    if extra.get("homework_request"):
        return RouteDecision(
            worker="heavy",
            intent="homework",
            ack=_ack_for_intent("homework"),
            focus=_build_focus(item, intent="homework", probe=probe),
        )

    if images and kind == "external_chat":
        return RouteDecision(
            worker="heavy",
            intent="describe_media",
            ack=_ack_for_intent("describe_media"),
            focus=_build_focus(item, intent="describe_media", probe=probe),
        )

    if probe and _DESCRIBE_MEDIA_RE.search(probe):
        return RouteDecision(
            worker="heavy",
            intent="describe_media",
            ack=_ack_for_intent("describe_media"),
            focus=_build_focus(item, intent="describe_media", probe=probe),
        )

    if probe and _ARCHIVE_RE.search(probe):
        return RouteDecision(
            worker="heavy",
            intent="archive_search",
            ack=_ack_for_intent("archive_search"),
            focus=_build_focus(item, intent="archive_search", probe=probe),
        )

    if len(text) >= _HEAVY_TEXT_LEN:
        return RouteDecision(
            worker="heavy",
            intent="long_analysis",
            ack=_ack_for_intent("long_analysis"),
            focus=_build_focus(item, intent="long_analysis", probe=probe),
        )

    if kind == "external_chat" and extra.get("agent_spec_request"):
        return RouteDecision(
            worker="heavy",
            intent="long_analysis",
            ack=_ack_for_intent("long_analysis"),
            focus=_build_focus(item, intent="long_analysis", probe=probe),
        )

    return RouteDecision(
        worker="light",
        intent="conversational",
        focus=_build_focus(item, intent="conversational", probe=probe),
    )


def build_dispatcher_preamble(focus: dict[str, Any], *, intent: str) -> str:
    """Жёсткое ТЗ для heavy-worker — не переинтерпретировать запрос."""
    lines = [
        "**Задача от диспетчера Юны (исполняй как есть, не переосмысливай):**",
        f"- intent: `{intent}`",
    ]
    if focus.get("reply_to_message_id"):
        lines.append(
            f"- Отвечай в контексте сообщения id `{focus['reply_to_message_id']}` "
            "(реплай-цепочка в задаче ниже)."
        )
    if focus.get("interlocutor"):
        lines.append(f"- Собеседник: **{focus['interlocutor']}**")
    if focus.get("ignore_stale_hoshi"):
        lines.append(
            "- Старые ответы **[Hoshi]** в контексте могли быть ошибочны — "
            "опирайся на реплай, медиа и новые реплики собеседника."
        )
    if focus.get("prefer_topic"):
        lines.append(f"- Тема запроса: **{focus['prefer_topic']}**")
    if focus.get("warn_wrong_topic"):
        lines.append(f"- {focus['warn_wrong_topic']}")
    if focus.get("exclude_topics"):
        lines.append(f"- Не упоминай: {', '.join(focus['exclude_topics'])}")
    if focus.get("focus_topics"):
        lines.append(f"- Фокус: {', '.join(focus['focus_topics'])}")
    lines.append("")
    return "\n".join(lines)
