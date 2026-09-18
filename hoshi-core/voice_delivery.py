#!/usr/bin/env python3
"""Разбор директив голоса в ответах агента."""
from __future__ import annotations

import re

VOICE_DIRECTIVE_RE = re.compile(
    r"\[\[voice:(?P<persona>[a-zA-Z0-9_-]+)\]\]"
    r"(?P<text>[а-яА-ЯёЁa-zA-Z0-9«»\"'].+?)(?:\]\])?(?:\s*(?:\n|$))",
    re.I | re.M,
)

VIDEO_DIRECTIVE_RE = re.compile(
    r"\[\[video:(?P<url>(?:https?://|/|\./|data/)[^\]]+)\]\]",
    re.I,
)

_VIDEO_MARKER_TAIL_RE = re.compile(
    r"`?\[\[video:(?:https?://|/|\./|data/)[^\]]*\]?\]?`?",
    re.I,
)

GEN_VIDEO_DIRECTIVE_RE = re.compile(
    r"\[\[gen_video:(?P<idea>[^\]]*)\]\]",
    re.I,
)

_GEN_VIDEO_MARKER_TAIL_RE = re.compile(
    r"`?\[\[gen_video:[^\]]*\]?\]?`?",
    re.I,
)

PHOTO_DIRECTIVE_RE = re.compile(
    r"\[\[photo:(?P<path>(?:/|\./|https?://|data/)[^\]]+)\]\]",
    re.I,
)

ALBUM_DIRECTIVE_RE = re.compile(
    r"\[\[album:(?P<paths>(?:/|\./|data/)[^\]]+)\]\]",
    re.I,
)

AUDIO_DIRECTIVE_RE = re.compile(
    r"\[\[audio:(?P<path>(?:/|\./|data/)[^\]]+)\]\]",
    re.I,
)

_AUDIO_MARKER_TAIL_RE = re.compile(
    r"`?\[\[audio:(?:/|\./|data/)[^\]]*\]?\]?`?",
    re.I,
)

_KNOWN_AUDIO_TITLES: dict[str, str] = {
    "luotianyi": "洛天依 - 你的歌真的好难唱",
}

GROUP_MEDIA_DIRECTIVE_RE = re.compile(
    r"\[\[group_media:(?P<chat_id>\d+)\]\]",
    re.I,
)

SEND_TO_DIRECTIVE_RE = re.compile(
    r"\[\[send_to:(?P<target>@?[\w]{2,32})\]\]",
    re.I,
)

REACTION_DIRECTIVE_RE = re.compile(
    r"\[\[reaction:(?P<kind>paid|premium|stars|[^\]:\s]{1,8})(?::(?P<msg_id>\d+))?\]\]",
    re.I,
)

_REACTION_MARKER_TAIL_RE = re.compile(
    r"`?\[\[reaction:(?P<kind>paid|premium|stars|[^\]:\s]{1,8})(?::(?P<msg_id>\d+))?`?",
    re.I,
)

_PHOTO_MARKER_TAIL_RE = re.compile(
    r"`?\[\[photo:(?:/|\./|https?://|data/)[^\]]*\]?\]?`?",
    re.I,
)


def is_silent_reply(text: str) -> bool:
    """Ответ с [[silent]] — в чат не пишем (маркер может быть в конце, не только один)."""
    return bool(re.search(r"\[\[silent\]\]", text or "", flags=re.I))


def extract_voice_directives(text: str) -> tuple[str, list[dict[str, str]]]:
    if is_silent_reply(text):
        return "", []
    voices: list[dict[str, str]] = []
    for m in VOICE_DIRECTIVE_RE.finditer(text):
        voices.append({
            "persona": m.group("persona").lower(),
            "text": m.group("text").strip(),
        })
    clean = VOICE_DIRECTIVE_RE.sub("", text)
    clean = re.sub(r"\]\]", "", clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return clean, voices


def _extract_bare_video_paths(text: str) -> list[str]:
    """Маркеры без закрывающих ]] — агент иногда обрезает хвост."""
    hits: list[str] = []
    for m in re.finditer(
        r"\[\[video:((?:https?://|/|\./|data/)[^\]\n]+)",
        text or "",
        re.I,
    ):
        url = m.group(1).strip().rstrip("]`")
        if url and url not in hits:
            hits.append(url)
    return hits


def strip_video_markers(text: str) -> str:
    """Убирает все маркеры [[video:...]] из текста (в т.ч. обрезанные)."""
    clean = VIDEO_DIRECTIVE_RE.sub("", text or "")
    clean = _VIDEO_MARKER_TAIL_RE.sub("", clean)
    clean = re.sub(r"`?\[\[video:\]\]`?", "", clean, flags=re.I)
    clean = re.sub(
        r"`?\[\[video:(?:https?://|/|\./|data/)[^\]\n]+`?",
        "",
        clean,
        flags=re.I,
    )
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean.strip()


def _clean_video_caption_candidate(text: str) -> str:
    cap = (text or "").strip()
    if not cap:
        return ""
    try:
        from text_format import strip_bot_reply

        cap = strip_bot_reply(cap)
    except Exception:
        pass
    cap = strip_video_markers(cap)
    cap = re.sub(
        r"(?i)(?:видео\s+скачал|уже\s+ушло|маркер|дополнительно\s+ничего).*$",
        "",
        cap,
        flags=re.S,
    ).strip()
    cap = re.sub(r"\n{3,}", "\n\n", cap).strip()
    return cap[:1024]


def extract_video_caption(text: str) -> str:
    """Текст перед/рядом с [[video:...]] — подпись к ролику."""
    raw = text or ""
    m = re.search(r"\[\[video:", raw, re.I)
    if not m:
        return ""
    prefix = _clean_video_caption_candidate(raw[: m.start()])
    if prefix:
        return prefix
    tail = raw[m.end() :]
    inline = re.match(
        r"(?P<url>(?:https?://|/|\./|data/)[^\]]*)\]\]"
        r"(?P<suffix>[^\n\[\]`]{0,200})",
        tail,
        re.I,
    )
    if inline:
        suffix = _clean_video_caption_candidate(inline.group("suffix"))
        if suffix and len(suffix) >= 8:
            return suffix
    return ""


def extract_video_directives(text: str) -> tuple[str, list[str]]:
    urls: list[str] = []
    for m in VIDEO_DIRECTIVE_RE.finditer(text or ""):
        urls.append(m.group("url").strip())
    for hit in _extract_bare_video_paths(text):
        if hit not in urls:
            urls.append(hit)
    clean = strip_video_markers(text)
    return clean, urls


def strip_gen_video_markers(text: str) -> str:
    clean = GEN_VIDEO_DIRECTIVE_RE.sub("", text or "")
    clean = _GEN_VIDEO_MARKER_TAIL_RE.sub("", clean)
    clean = re.sub(r"`?\[\[gen_video:[^\]\n]*`?", "", clean, flags=re.I)
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean.strip()


def extract_gen_video_directives(text: str) -> tuple[str, list[str]]:
    ideas: list[str] = []
    for m in GEN_VIDEO_DIRECTIVE_RE.finditer(text or ""):
        ideas.append((m.group("idea") or "").strip())
    for m in re.finditer(r"\[\[gen_video:([^\]\n]*)", text or "", re.I):
        idea = (m.group(1) or "").strip().rstrip("]`")
        if idea not in ideas:
            ideas.append(idea)
    if not ideas:
        for m in re.finditer(r"\[\[gen_video:\s*\]\]", text or "", re.I):
            ideas.append("")
    clean = strip_gen_video_markers(text)
    return clean, ideas


def strip_photo_markers(text: str) -> str:
    """Убирает все маркеры [[photo:...]] из текста (в т.ч. обрезанные)."""
    clean = PHOTO_DIRECTIVE_RE.sub("", text or "")
    clean = _PHOTO_MARKER_TAIL_RE.sub("", clean)
    clean = re.sub(r"`?\[\[photo:\]\]`?", "", clean, flags=re.I)
    clean = re.sub(r"`?\[\[photo:[^\]]+\]?\]?`?", "", clean, flags=re.I)
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean.strip()


def extract_photo_directives(text: str) -> tuple[str, list[str]]:
    paths: list[str] = []
    for m in PHOTO_DIRECTIVE_RE.finditer(text or ""):
        p = m.group("path").strip()
        if p:
            paths.append(p)
    return strip_photo_markers(text), paths


def strip_album_markers(text: str) -> str:
    clean = ALBUM_DIRECTIVE_RE.sub("", text or "")
    clean = re.sub(r"`?\[\[album:[^\]]+\]\]`?", "", clean, flags=re.I)
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean.strip()


def extract_album_directives(text: str) -> tuple[str, list[list[str]]]:
    albums: list[list[str]] = []
    for m in ALBUM_DIRECTIVE_RE.finditer(text or ""):
        paths = [p.strip() for p in m.group("paths").split("|") if p.strip()]
        if paths:
            albums.append(paths)
    return strip_album_markers(text), albums


def _sanitize_audio_label(name: str) -> str:
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", (name or "").strip())
    clean = re.sub(r"\s+", " ", clean).strip(" .")
    return clean[:120] or "audio"


def audio_display_name(path: str, *, title: str = "") -> str:
    """Человекочитаемое имя трека для Telegram."""
    if title:
        return _sanitize_audio_label(title)
    from pathlib import Path

    p = Path(path)
    try:
        from mutagen.easyid3 import EasyID3

        tags = EasyID3(str(p))
        tag_title = (tags.get("title") or [None])[0]
        if tag_title:
            return _sanitize_audio_label(str(tag_title))
    except Exception:
        pass
    stem = p.stem.lower()
    for key, label in _KNOWN_AUDIO_TITLES.items():
        if key in stem:
            return label
    nice = re.sub(r"[_-]+", " ", p.stem).strip()
    nice = re.sub(r"\s+[a-z0-9]{6,8}$", "", nice, flags=re.I)
    return _sanitize_audio_label(nice)


def strip_audio_markers(text: str) -> str:
    clean = AUDIO_DIRECTIVE_RE.sub("", text or "")
    clean = _AUDIO_MARKER_TAIL_RE.sub("", clean)
    clean = re.sub(r"`?\[\[audio:\]\]`?", "", clean, flags=re.I)
    clean = re.sub(r"`?\[\[audio:[^\]]+\]?\]?`?", "", clean, flags=re.I)
    clean = re.sub(r"`?\[\[send_to:[^\]]+\]\]`?", "", clean, flags=re.I)
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean.strip()


def extract_audio_directives(text: str) -> tuple[str, list[str]]:
    paths: list[str] = []
    for m in AUDIO_DIRECTIVE_RE.finditer(text or ""):
        p = m.group("path").strip()
        if p:
            paths.append(p)
    return strip_audio_markers(text), paths


def extract_group_media_directives(text: str) -> tuple[str, list[int]]:
    ids: list[int] = []
    for m in GROUP_MEDIA_DIRECTIVE_RE.finditer(text or ""):
        ids.append(int(m.group("chat_id")))
    clean = GROUP_MEDIA_DIRECTIVE_RE.sub("", text or "").strip()
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean, ids


def extract_send_to_directive(text: str) -> tuple[str, str]:
    target = ""
    for m in SEND_TO_DIRECTIVE_RE.finditer(text or ""):
        target = m.group("target").lstrip("@")
    clean = SEND_TO_DIRECTIVE_RE.sub("", text or "").strip()
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean, target


def has_reaction_directives(text: str) -> bool:
    """Ответ содержит маркер реакции — нужна свежая доставка."""
    return bool(re.search(r"\[\[reaction:", text or "", flags=re.I))


def strip_reaction_markers(text: str) -> str:
    """Убирает все маркеры [[reaction:...]] из текста (в т.ч. обрезанные)."""
    clean = REACTION_DIRECTIVE_RE.sub("", text or "")
    clean = _REACTION_MARKER_TAIL_RE.sub("", clean)
    clean = re.sub(r"`?\[\[reaction:\]\]`?", "", clean, flags=re.I)
    clean = re.sub(r"`?\[\[reaction:[^\]\n]*`?", "", clean, flags=re.I)
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean.strip()


def extract_reaction_directives(text: str) -> tuple[str, list[tuple[str, int | None]]]:
    hits: list[tuple[str, int | None]] = []
    seen: set[tuple[str, int | None]] = set()
    for m in REACTION_DIRECTIVE_RE.finditer(text or ""):
        kind = (m.group("kind") or "").strip()
        if not kind:
            continue
        mid = int(m.group("msg_id")) if m.group("msg_id") else None
        key = (kind, mid)
        if key not in seen:
            seen.add(key)
            hits.append(key)
    for m in _REACTION_MARKER_TAIL_RE.finditer(text or ""):
        kind = (m.group("kind") or "").strip()
        if not kind:
            continue
        mid = int(m.group("msg_id")) if m.group("msg_id") else None
        key = (kind, mid)
        if key not in seen:
            seen.add(key)
            hits.append(key)
    clean = strip_reaction_markers(text)
    return clean, hits
