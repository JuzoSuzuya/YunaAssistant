#!/usr/bin/env python3
"""Очередь исходящих сообщений через user-client (обрабатывает bridge)."""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

_VIDEO_STOP_COOLDOWN_SEC = 600.0
_OUTBOX_STALE_SEC = 300.0

from config import DATA

_VIDEO_STOP_FILE = DATA / "video_stop.json"
OUTBOX = DATA / "user_outbox"
OUTBOX.mkdir(parents=True, exist_ok=True)

_seq = 0


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _next_seq() -> int:
    global _seq
    _seq += 1
    return _seq


def enqueue_user_action(
    action: str,
    *,
    chat_id: int | None = None,
    text: str = "",
    reply_to: int | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    if chat_id and action in (
        "send", "send_voice", "send_video", "send_photo", "send_audio",
        "deliver_video", "generate_video", "typing", "reaction",
    ):
        from chat_router import is_chat_muted, is_chat_write_forbidden

        if is_chat_write_forbidden(int(chat_id)) and not (extra or {}).get(
            "owner_approved_write"
        ):
            return ""
        if is_chat_muted(int(chat_id)) and not (extra or {}).get("owner_approved_write"):
            return ""
    embedded_photos: list[str] = []
    embedded_audios: list[str] = []
    embedded_videos: list[str] = []
    embedded_gen: list[str] = []
    embedded_reactions: list[tuple[str, int | None]] = []
    if action == "send" and text:
        from voice_delivery import (
            audio_display_name,
            extract_audio_directives,
            extract_gen_video_directives,
            extract_photo_directives,
            extract_reaction_directives,
            extract_video_caption,
            extract_video_directives,
            strip_audio_markers,
            strip_gen_video_markers,
            strip_photo_markers,
            strip_reaction_markers,
            strip_video_markers,
        )

        text, embedded_videos = extract_video_directives(text)
        text, embedded_gen = extract_gen_video_directives(text)
        text, embedded_photos = extract_photo_directives(text)
        text, embedded_audios = extract_audio_directives(text)
        text, embedded_reactions = extract_reaction_directives(text)
        text = strip_reaction_markers(
            strip_video_markers(
                strip_gen_video_markers(strip_photo_markers(strip_audio_markers(text)))
            )
        )
        from text_format import is_canned_tz_template, strip_canned_tz_template

        if is_canned_tz_template(text):
            text = strip_canned_tz_template(text)
            if not (text or "").strip():
                return ""
    item_id = f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
    item = {
        "id": item_id,
        "action": action,
        "chat_id": chat_id,
        "text": text,
        "reply_to": reply_to,
        "extra": extra or {},
        "created_at": _now(),
        "seq": _next_seq(),
        "status": "pending",
    }
    (OUTBOX / f"{item_id}.json").write_text(
        json.dumps(item, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if chat_id and action == "send":
        from voice_delivery import audio_display_name

        owner_ok = bool((extra or {}).get("owner_approved_write"))
        for photo_path in embedded_photos:
            enqueue_user_photo(
                int(chat_id),
                photo_path,
                reply_to=None,
                caption="",
                owner_approved=owner_ok,
            )
        for i, audio_path in enumerate(embedded_audios):
            enqueue_user_audio(
                int(chat_id),
                audio_path,
                reply_to=reply_to if i == 0 and not embedded_photos else None,
                caption=text[:1024] if i == 0 and text.strip() and not embedded_photos else "",
                title=audio_display_name(audio_path),
                owner_approved=owner_ok,
            )
        emb_cap = extract_video_caption(text)
        from text_format import sanitize_external_caption

        emb_cap = sanitize_external_caption(emb_cap)
        for i, url in enumerate(embedded_videos):
            cap = emb_cap if i == 0 and emb_cap else ""
            if url.startswith(("http://", "https://")):
                enqueue_deliver_video(
                    int(chat_id),
                    url,
                    reply_to=reply_to,
                    caption=cap,
                    owner_approved=owner_ok,
                )
            else:
                enqueue_user_video(
                    int(chat_id),
                    url,
                    reply_to=reply_to,
                    caption=cap,
                    owner_approved=owner_ok,
                )
        for idea in embedded_gen:
            enqueue_generate_video(
                int(chat_id),
                idea,
                reply_to=reply_to,
                owner_approved=owner_ok,
            )
        for reaction_kind, reaction_mid in embedded_reactions:
            react_to = reaction_mid or reply_to
            if react_to:
                enqueue_user_reaction(
                    int(chat_id),
                    int(react_to),
                    reaction=reaction_kind,
                    owner_approved=owner_ok,
                )
    return item_id


def enqueue_user_message(
    chat_id: int,
    text: str,
    *,
    reply_to: int | None = None,
    owner_approved: bool = False,
) -> str:
    from voice_delivery import (
        audio_display_name,
        extract_audio_directives,
        extract_gen_video_directives,
        extract_photo_directives,
        extract_reaction_directives,
        extract_video_caption,
        extract_video_directives,
        strip_audio_markers,
        strip_gen_video_markers,
        strip_photo_markers,
        strip_reaction_markers,
        strip_video_markers,
    )

    video_cap = extract_video_caption(text)
    from text_format import sanitize_external_caption

    video_cap = sanitize_external_caption(video_cap)
    plain, videos = extract_video_directives(text)
    plain, gen_ideas = extract_gen_video_directives(plain)
    plain, photos = extract_photo_directives(plain)
    plain, audios = extract_audio_directives(plain)
    plain, reactions = extract_reaction_directives(plain)
    plain = strip_reaction_markers(
        strip_video_markers(
            strip_gen_video_markers(strip_photo_markers(strip_audio_markers(plain)))
        )
    )
    extra = {"owner_approved_write": True} if owner_approved else None
    msg_id = ""
    caption = plain.strip()
    if photos and caption:
        msg_id = enqueue_user_photo(
            chat_id,
            photos[0],
            reply_to=reply_to,
            caption=caption[:1024],
            owner_approved=owner_approved,
        )
        for photo_path in photos[1:]:
            enqueue_user_photo(
                chat_id,
                photo_path,
                reply_to=None,
                caption="",
                owner_approved=owner_approved,
            )
    elif photos:
        for i, photo_path in enumerate(photos):
            msg_id = enqueue_user_photo(
                chat_id,
                photo_path,
                reply_to=reply_to if i == 0 else None,
                caption="",
                owner_approved=owner_approved,
            )
    elif caption and not audios:
        msg_id = enqueue_user_action(
            "send", chat_id=chat_id, text=caption, reply_to=reply_to, extra=extra
        )
    for i, audio_path in enumerate(audios):
        audio_id = enqueue_user_audio(
            chat_id,
            audio_path,
            reply_to=reply_to if i == 0 and not photos and not caption else None,
            caption=caption[:1024] if i == 0 and caption and not photos else "",
            title=audio_display_name(audio_path),
            owner_approved=owner_approved,
        )
        if audio_id:
            msg_id = audio_id
    from video_download import ensure_video_caption

    for i, url in enumerate(videos):
        cap = ensure_video_caption(
            video_cap if i == 0 and video_cap else "",
            url=url if url.startswith(("http://", "https://")) else "",
            path=url,
        )
        if url.startswith(("http://", "https://")):
            vid_id = enqueue_deliver_video(
                chat_id,
                url,
                reply_to=reply_to,
                caption=cap,
                owner_approved=owner_approved,
            )
        else:
            vid_id = enqueue_user_video(
                chat_id,
                url,
                reply_to=reply_to,
                caption=cap,
                owner_approved=owner_approved,
            )
        if vid_id:
            msg_id = vid_id
    for idea in gen_ideas:
        gen_id = enqueue_generate_video(
            chat_id,
            idea,
            reply_to=reply_to,
            owner_approved=owner_approved,
        )
        if gen_id:
            msg_id = gen_id
    for reaction_kind, reaction_mid in reactions:
        react_to = reaction_mid or reply_to
        if react_to:
            enqueue_user_reaction(
                chat_id,
                int(react_to),
                reaction=reaction_kind,
                owner_approved=owner_approved,
            )
    return msg_id


def enqueue_user_album(
    chat_id: int,
    photo_paths: list[str],
    *,
    reply_to: int | None = None,
    caption: str = "",
    owner_approved: bool = False,
) -> str:
    extra: dict[str, Any] = {"photo_paths": photo_paths, "caption": caption}
    if owner_approved:
        extra["owner_approved_write"] = True
    return enqueue_user_action(
        "send_album",
        chat_id=chat_id,
        reply_to=reply_to,
        extra=extra,
    )


def enqueue_user_photo(
    chat_id: int,
    photo_path: str,
    *,
    reply_to: int | None = None,
    caption: str = "",
    owner_approved: bool = False,
) -> str:
    extra: dict[str, Any] = {"photo_path": photo_path, "caption": caption}
    if owner_approved:
        extra["owner_approved_write"] = True
    return enqueue_user_action(
        "send_photo",
        chat_id=chat_id,
        reply_to=reply_to,
        extra=extra,
    )


def enqueue_user_audio(
    chat_id: int,
    audio_path: str,
    *,
    reply_to: int | None = None,
    caption: str = "",
    title: str = "",
    owner_approved: bool = False,
    target_username: str = "",
) -> str:
    extra: dict[str, Any] = {
        "audio_path": audio_path,
        "caption": caption,
        "title": title,
    }
    if target_username:
        extra["target_username"] = target_username.lstrip("@")
    if owner_approved:
        extra["owner_approved_write"] = True
    return enqueue_user_action(
        "send_audio",
        chat_id=chat_id,
        reply_to=reply_to,
        extra=extra,
    )


def enqueue_user_video(
    chat_id: int,
    video_path: str,
    *,
    reply_to: int | None = None,
    caption: str = "",
    owner_approved: bool = False,
) -> str:
    extra: dict[str, Any] = {"video_path": video_path, "caption": caption}
    if owner_approved:
        extra["owner_approved_write"] = True
    return enqueue_user_action(
        "send_video",
        chat_id=chat_id,
        reply_to=reply_to,
        extra=extra,
    )


def enqueue_generate_video(
    chat_id: int,
    idea: str = "",
    *,
    reply_to: int | None = None,
    owner_approved: bool = False,
) -> str:
    extra: dict[str, Any] = {"gen_idea": idea}
    if owner_approved:
        extra["owner_approved_write"] = True
    return enqueue_user_action(
        "generate_video",
        chat_id=chat_id,
        reply_to=reply_to,
        extra=extra,
    )


def _pending_deliver_video_urls(chat_id: int) -> set[str]:
    urls: set[str] = set()
    for item in list_active_actions():
        if item.get("action") != "deliver_video":
            continue
        if int(item.get("chat_id") or 0) != int(chat_id):
            continue
        url = (item.get("extra") or {}).get("video_url", "")
        if url:
            urls.add(url)
    return urls


def has_pending_video(chat_id: int) -> bool:
    cid = int(chat_id)
    for item in list_active_actions():
        if item.get("action") not in ("deliver_video", "send_video", "generate_video"):
            continue
        if int(item.get("chat_id") or 0) == cid:
            return True
    return False


def _load_video_stop_until() -> dict[str, float]:
    if not _VIDEO_STOP_FILE.exists():
        return {}
    try:
        raw = json.loads(_VIDEO_STOP_FILE.read_text(encoding="utf-8"))
        return {str(k): float(v) for k, v in (raw or {}).items()}
    except Exception:
        return {}


def _save_video_stop_until(data: dict[str, float]) -> None:
    _VIDEO_STOP_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def mark_chat_video_stopped(chat_id: int, *, cooldown_sec: float = _VIDEO_STOP_COOLDOWN_SEC) -> None:
    cid = str(int(chat_id))
    data = _load_video_stop_until()
    data[cid] = time.time() + max(30.0, cooldown_sec)
    _save_video_stop_until(data)


def clear_chat_video_stop(chat_id: int) -> None:
    cid = str(int(chat_id))
    data = _load_video_stop_until()
    if cid not in data:
        return
    data.pop(cid, None)
    _save_video_stop_until(data)


def is_chat_video_stopped(chat_id: int) -> bool:
    cid = str(int(chat_id))
    data = _load_video_stop_until()
    until = float(data.get(cid, 0.0))
    if until and time.time() < until:
        return True
    if until:
        data.pop(cid, None)
        _save_video_stop_until(data)
    return False


def emergency_stop_all_videos() -> dict[str, int]:
    """Останавливает скачивания и очередь видео во всех активных чатах."""
    from storage import load_settings
    from video_download import request_download_cancel

    totals = {"outbox": 0, "inbox": 0, "killed": 0, "chats": 0}
    chat_ids: set[int] = set()

    sessions = load_settings().get("external_chats", {}).get("active_sessions") or {}
    for key in sessions:
        try:
            chat_ids.add(int(key))
        except (TypeError, ValueError):
            pass

    for path in OUTBOX.glob("*.json"):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if item.get("action") not in ("deliver_video", "send_video", "generate_video"):
            continue
        if item.get("status") not in ("pending", "in_progress"):
            continue
        cid = int(item.get("chat_id") or 0)
        if cid:
            chat_ids.add(cid)

    request_download_cancel()
    for cid in chat_ids:
        r = emergency_stop_videos(cid)
        totals["outbox"] += r.get("outbox", 0)
        totals["inbox"] += r.get("inbox", 0)
        totals["killed"] += r.get("killed", 0)
        totals["chats"] += 1
    return totals


def stop_chat_process(chat_id: int) -> dict[str, int]:
    """Полная остановка фоновой работы в чате: видео, inbox, outbox."""
    from chat_router import purge_forbidden_chat_tasks

    totals = dict(emergency_stop_videos(chat_id))
    totals["outbox"] += purge_forbidden_chat_outbox(chat_id)
    totals["inbox"] += purge_forbidden_chat_tasks(chat_id)
    return totals


def emergency_stop_videos(chat_id: int) -> dict[str, int]:
    """Останавливает скачивания и очищает очередь видео для чата."""
    import subprocess

    from storage import purge_inbox_for_target_chat
    from user_client import cancel_chat_video_state
    from video_download import request_download_cancel

    mark_chat_video_stopped(chat_id)
    request_download_cancel()
    removed_outbox = purge_chat_video_outbox(chat_id)
    removed_inbox = purge_inbox_for_target_chat(chat_id)
    cancel_chat_video_state(chat_id)
    try:
        from tg_bridge import release_video_chat

        release_video_chat(chat_id)
    except Exception:
        pass
    killed = 0
    try:
        r = subprocess.run(
            ["pkill", "-9", "-f", "incoming_media/video_"],
            capture_output=True,
            timeout=3,
        )
        if r.returncode == 0:
            killed = 1
    except Exception:
        pass
    return {
        "outbox": removed_outbox,
        "inbox": removed_inbox,
        "killed": killed,
    }


def purge_chat_video_outbox(chat_id: int) -> int:
    """Удаляет все незавершённые видео-задачи для чата."""
    cid = int(chat_id)
    removed = 0
    for path in list(OUTBOX.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if item.get("status") not in ("pending", "in_progress"):
            continue
        if item.get("action") not in ("deliver_video", "send_video", "generate_video"):
            continue
        if int(item.get("chat_id") or 0) != cid:
            continue
        path.unlink(missing_ok=True)
        removed += 1
    return removed


def enqueue_deliver_video(
    chat_id: int,
    url: str,
    *,
    reply_to: int | None = None,
    caption: str = "",
    owner_approved: bool = False,
) -> str:
    norm = (url or "").strip()
    if not norm:
        return ""
    if is_chat_video_stopped(chat_id):
        return ""
    if has_pending_video(chat_id):
        return ""
    if norm in _pending_deliver_video_urls(chat_id):
        return ""
    extra: dict[str, Any] = {"video_url": norm}
    if (caption or "").strip():
        extra["caption"] = (caption or "").strip()[:1024]
    if owner_approved:
        extra["owner_approved_write"] = True
    return enqueue_user_action(
        "deliver_video",
        chat_id=chat_id,
        reply_to=reply_to,
        extra=extra,
    )


def enqueue_user_voice(
    chat_id: int,
    voice_path: str,
    *,
    reply_to: int | None = None,
    persona: str = "",
    owner_approved: bool = False,
) -> str:
    extra: dict[str, Any] = {"voice_path": voice_path, "persona": persona}
    if owner_approved:
        extra["owner_approved_write"] = True
    return enqueue_user_action(
        "send_voice",
        chat_id=chat_id,
        reply_to=reply_to,
        extra=extra,
    )


def enqueue_user_typing(chat_id: int) -> str:
    return enqueue_user_action("typing", chat_id=chat_id)


def enqueue_user_reaction(
    chat_id: int,
    message_id: int,
    *,
    reaction: str = "paid",
    owner_approved: bool = False,
) -> str:
    if chat_id:
        from chat_router import is_chat_muted, is_chat_write_forbidden

        if is_chat_write_forbidden(int(chat_id)) and not owner_approved:
            return ""
        if is_chat_muted(int(chat_id)) and not owner_approved:
            return ""
    extra: dict[str, Any] = {
        "message_id": int(message_id),
        "reaction": (reaction or "paid").strip(),
    }
    if owner_approved:
        extra["owner_approved_write"] = True
    item_id = f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
    item = {
        "id": item_id,
        "action": "reaction",
        "chat_id": chat_id,
        "text": "",
        "reply_to": None,
        "extra": extra,
        "created_at": _now(),
        "seq": _next_seq(),
        "status": "pending",
    }
    (OUTBOX / f"{item_id}.json").write_text(
        json.dumps(item, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return item_id


def enqueue_relay_group_media(
    target_chat_id: int,
    source_chat_id: int,
    *,
    reply_to: int | None = None,
    owner_approved: bool = False,
) -> str:
    extra: dict[str, Any] = {"source_chat_id": int(source_chat_id)}
    if owner_approved:
        extra["owner_approved_write"] = True
    return enqueue_user_action(
        "relay_group_media",
        chat_id=int(target_chat_id),
        reply_to=reply_to,
        extra=extra,
    )


def _iter_outbox_items(*, statuses: tuple[str, ...] = ("pending",)) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path in sorted(OUTBOX.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if item.get("status") in statuses:
            items.append(item)
    items.sort(key=lambda i: (i.get("created_at", ""), i.get("seq", 0), i.get("id", "")))
    return items


def list_pending_actions() -> list[dict[str, Any]]:
    return _iter_outbox_items(statuses=("pending",))


def list_active_actions() -> list[dict[str, Any]]:
    return _iter_outbox_items(statuses=("pending", "in_progress"))


def recover_stale_outbox_actions(*, max_age_sec: float = _OUTBOX_STALE_SEC) -> int:
    """Сбрасывает зависшие in_progress (bridge упал mid-send) обратно в pending."""
    now = time.time()
    recovered = 0
    for path in list(OUTBOX.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if item.get("status") != "in_progress":
            continue
        started = item.get("started_at") or item.get("created_at") or ""
        try:
            started_ts = datetime.fromisoformat(started).timestamp()
        except (TypeError, ValueError):
            started_ts = path.stat().st_mtime
        if now - started_ts < max_age_sec:
            continue
        item["status"] = "pending"
        item.pop("started_at", None)
        path.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
        recovered += 1
    return recovered


def claim_action(item_id: str) -> bool:
    """pending → in_progress. False если уже взято или готово."""
    path = OUTBOX / f"{item_id}.json"
    if not path.exists():
        return False
    try:
        item = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        path.unlink(missing_ok=True)
        return False
    if item.get("status") != "pending":
        return False
    item["status"] = "in_progress"
    item["started_at"] = _now()
    path.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def purge_muted_chat_outbox(chat_id: int) -> int:
    """Удаляет неотправленные сообщения в замьюченный чат."""
    from chat_router import is_chat_muted, peer_chat_id_variants

    if not is_chat_muted(int(chat_id)):
        return 0
    variants = peer_chat_id_variants(int(chat_id))
    removed = 0
    for path in list(OUTBOX.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if item.get("status") != "pending":
            continue
        if int(item.get("chat_id") or 0) not in variants:
            continue
        if (item.get("extra") or {}).get("owner_approved_write"):
            continue
        path.unlink(missing_ok=True)
        removed += 1
    return removed


def purge_forbidden_chat_outbox(chat_id: int) -> int:
    """Удаляет все неотправленные сообщения в запрещённый чат."""
    from chat_router import peer_chat_id_variants

    variants = peer_chat_id_variants(int(chat_id))
    removed = 0
    for path in list(OUTBOX.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if item.get("status") != "pending":
            continue
        if int(item.get("chat_id") or 0) not in variants:
            continue
        path.unlink(missing_ok=True)
        removed += 1
    return removed


def purge_all_outbox_except(allowed_chat_ids: list[int]) -> int:
    """Удаляет pending outbox для чатов вне whitelist."""
    allowed = {int(x) for x in allowed_chat_ids}
    removed = 0
    for path in list(OUTBOX.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if item.get("status") != "pending":
            continue
        cid = int(item.get("chat_id") or 0)
        if cid in allowed:
            continue
        if (item.get("extra") or {}).get("owner_approved_write"):
            continue
        path.unlink(missing_ok=True)
        removed += 1
    return removed


def mark_action_done(item_id: str) -> None:
    path = OUTBOX / f"{item_id}.json"
    if not path.exists():
        return
    try:
        item = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        path.unlink(missing_ok=True)
        return
    item["status"] = "done"
    item["done_at"] = _now()
    path.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
    path.unlink(missing_ok=True)
