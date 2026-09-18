#!/usr/bin/env python3
"""Клиент привязанного Telegram-аккаунта (Telethon)."""
from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import shutil
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from telethon import TelegramClient
from telethon.tl.functions.messages import GetSavedGifsRequest, SetTypingRequest
from telethon.tl.types.messages import SavedGifs
from telethon.tl.types import Channel, Chat, SendMessageTypingAction, User

from account_linker import session_path
from config import MEDIA_DIR, OWNER_ID, ROOT, USER_SESSIONS, get_telegram_api
from storage import load_settings

log = logging.getLogger("hoshi.user_client")

_runtime_client: TelegramClient | None = None
_client_lock = asyncio.Lock()
_bridge_loop: asyncio.AbstractEventLoop | None = None
_READONLY_SESSIONS = Path("/tmp/hoshi_ro_sessions")
_readonly_pool_client: TelegramClient | None = None
_readonly_pool_until: float = 0.0
_readonly_pool_lock = asyncio.Lock()
_READONLY_POOL_TTL = 45.0


def set_bridge_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _bridge_loop
    _bridge_loop = loop


def _session_file() -> str | None:
    phone = load_settings().get("linked_account", {}).get("phone", "")
    if not phone:
        return None
    path = session_path(phone)
    return str(path) if path.exists() else None


def is_linked() -> bool:
    s = load_settings()
    return bool(s.get("linked_account", {}).get("user_id") and _session_file())


async def _make_client() -> TelegramClient | None:
    session = _session_file()
    api_id, api_hash = get_telegram_api()
    if not session or not api_id or not api_hash:
        return None
    return TelegramClient(session, int(api_id), api_hash)


async def _ensure_started_client() -> TelegramClient | None:
    """Единый клиент с активным циклом обновлений (только в bridge)."""
    global _runtime_client

    async with _client_lock:
        if _runtime_client and _runtime_client.is_connected():
            return _runtime_client

        client = await _make_client()
        if not client:
            return None

        await client.start()
        if not await client.is_user_authorized():
            await client.disconnect()
            return None

        _runtime_client = client
        log.info("user client started")
        return client


async def get_client() -> TelegramClient | None:
    """Основная SQLite-сессия — только в bridge; иначе копия без блокировки."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return await _acquire_readonly_client()
    if _bridge_loop is not None and loop is _bridge_loop:
        return await _ensure_started_client()
    return await _acquire_readonly_client()


async def ensure_watcher_client() -> TelegramClient | None:
    return await _ensure_started_client()


async def disconnect_client() -> None:
    global _runtime_client
    async with _client_lock:
        if _runtime_client:
            try:
                await _runtime_client.disconnect()
            except Exception:
                pass
            _runtime_client = None


def _dialog_info(d) -> dict[str, Any]:
    ent = d.entity
    username = getattr(ent, "username", None) or ""
    is_user = isinstance(ent, User)
    is_group = isinstance(ent, (Chat, Channel)) and not getattr(ent, "broadcast", False)
    return {
        "id": d.id,
        "title": d.name or username or str(d.id),
        "username": username,
        "is_user": is_user,
        "is_group": is_group,
    }


async def list_dialogs(*, limit: int = 60) -> list[dict[str, Any]]:
    client = await get_client()
    if not client:
        return []
    out = []
    async for d in client.iter_dialogs(limit=limit):
        out.append(_dialog_info(d))
    return out


CHAT_ALIASES: dict[str, set[str]] = {
    "лега": {"лега", "леге", "легой", "легу", "lega", "legendaah"},
}


def _canonical_query(query: str) -> str:
    q = query.strip().lower().lstrip("@")
    for canonical, aliases in CHAT_ALIASES.items():
        if q in aliases:
            return canonical
    return q


def _match_score(query: str, dialog: dict[str, Any]) -> int:
    q = _canonical_query(query)
    if not q:
        return 0
    title = (dialog.get("title") or "").lower()
    username = (dialog.get("username") or "").lower()
    if title == q or username == q:
        return 100
    if title.startswith(q) or username.startswith(q):
        return 90
    if len(q) >= 4 and (q in title or q in username):
        return 70
    if dialog.get("is_user") and q in title.split():
        return 85
    return 0


async def find_dialogs(query: str, *, limit: int = 8, users_only: bool = False) -> list[dict[str, Any]]:
    dialogs = await list_dialogs(limit=120)
    if users_only:
        dialogs = [d for d in dialogs if d.get("is_user")]
    scored = [(d, _match_score(query, d)) for d in dialogs]
    scored = [(d, s) for d, s in scored if s > 0]
    scored.sort(key=lambda x: (-x[1], not x[0].get("is_user"), x[0]["title"]))
    return [d for d, _ in scored[:limit]]


async def _runtime_read_client() -> TelegramClient | None:
    """Основной клиент bridge — без лишних connect/disconnect."""
    if _runtime_client and _runtime_client.is_connected():
        return _runtime_client
    return None


def _copy_session_files(src: Path, copy_base: Path) -> None:
    dst = Path(str(copy_base) + ".session")
    last_err: Exception | None = None
    for attempt in range(4):
        try:
            shutil.copy2(src, dst)
            journal = Path(str(src) + "-journal")
            if journal.exists():
                shutil.copy2(journal, Path(str(copy_base) + ".session-journal"))
            return
        except OSError as e:
            last_err = e
            time.sleep(0.05 * (attempt + 1))
    if last_err:
        raise last_err


async def _spawn_readonly_client() -> TelegramClient | None:
    session = _session_file()
    api_id, api_hash = get_telegram_api()
    if not session or not api_id or not api_hash:
        return None
    copy_base: Path | None = None
    client: TelegramClient | None = None
    try:
        _READONLY_SESSIONS.mkdir(parents=True, exist_ok=True)
        src = Path(session)
        copy_base = _READONLY_SESSIONS / f"{src.stem}_{os.getpid()}"
        _copy_session_files(src, copy_base)
        client = TelegramClient(str(copy_base), int(api_id), api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return None
        return client
    except Exception as e:
        log.warning("readonly_client spawn failed: %s", e)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        return None
    finally:
        if copy_base:
            Path(str(copy_base) + ".session").unlink(missing_ok=True)
            Path(str(copy_base) + ".session-journal").unlink(missing_ok=True)


async def _acquire_readonly_client() -> TelegramClient | None:
    runtime = await _runtime_read_client()
    if runtime:
        return runtime

    global _readonly_pool_client, _readonly_pool_until
    async with _readonly_pool_lock:
        now = time.monotonic()
        if (
            _readonly_pool_client
            and _readonly_pool_client.is_connected()
            and now < _readonly_pool_until
        ):
            return _readonly_pool_client

        if _readonly_pool_client:
            try:
                await _readonly_pool_client.disconnect()
            except Exception:
                pass
            _readonly_pool_client = None

        client = await _spawn_readonly_client()
        if client:
            _readonly_pool_client = client
            _readonly_pool_until = now + _READONLY_POOL_TTL
        return client


@asynccontextmanager
async def readonly_client():
    """Клиент для чтения: bridge — основной; иначе пул с копией сессии."""
    client = await _acquire_readonly_client()
    yield client


async def _fetch_recent_messages(
    client: TelegramClient, chat_id: int, *, limit: int = 25
) -> list[dict[str, Any]]:
    out = []
    async for m in client.iter_messages(chat_id, limit=limit):
        out.append(_message_row(m))
    out.reverse()
    return out


async def get_entity(chat_id: int):
    client = await get_client()
    if not client:
        return None
    return await client.get_entity(chat_id)


async def get_entity_readonly(chat_id: int, *, client: TelegramClient | None = None):
    if client is None:
        client = await _acquire_readonly_client()
    if not client:
        return None
    try:
        return await client.get_entity(chat_id)
    except Exception as e:
        log.warning("get_entity_readonly failed %s: %s", chat_id, e)
        return None


async def resolve_username_chat_id_readonly(
    username: str, *, client: TelegramClient | None = None
) -> int | None:
    if client is None:
        client = await _acquire_readonly_client()
    if not client:
        return None
    try:
        ent = await client.get_entity(username.lstrip("@"))
        return int(ent.id)
    except Exception as e:
        log.warning("resolve_username_chat_id_readonly failed for %s: %s", username, e)
        return None


async def resolve_username_chat_id(username: str) -> int | None:
    client = await get_client()
    if not client:
        return None
    try:
        ent = await client.get_entity(username.lstrip("@"))
        return int(ent.id)
    except Exception as e:
        log.warning("resolve_username_chat_id failed for %s: %s", username, e)
        return None


CONTEXT_ANCHOR_RE = re.compile(
    r"(?:начни\s+)?(?:читать\s+)?(?:с\s+)?момента\s+[\"«']?([^\"»'\n]+)",
    re.IGNORECASE,
)


def extract_context_anchor(text: str) -> str | None:
    """Якорь из «начни читать с момента "…"»."""
    m = CONTEXT_ANCHOR_RE.search(text)
    if not m:
        return None
    anchor = m.group(1).strip().strip("\"'«»")
    return anchor or None


def _text_matches_anchor(msg_text: str, anchor: str) -> bool:
    if not msg_text or not anchor:
        return False
    low_msg = msg_text.lower()
    low_anchor = anchor.lower()
    if low_anchor in low_msg:
        return True
    words = [w for w in re.findall(r"\w+", low_anchor) if len(w) >= 3]
    if words and sum(1 for w in words if w in low_msg) >= max(1, len(words) - 1):
        return True
    return False


def _message_has_video(m) -> bool:
    if getattr(m, "video", None):
        return True
    doc = getattr(m, "document", None)
    if not doc:
        return False
    try:
        from telethon.tl.types import DocumentAttributeVideo

        attrs = getattr(doc, "attributes", None) or []
        return any(isinstance(a, DocumentAttributeVideo) for a in attrs)
    except Exception:
        return False


def _message_row(m) -> dict[str, Any]:
    sender = m.sender
    name = ""
    username = ""
    if sender:
        name = getattr(sender, "first_name", "") or ""
        if getattr(sender, "last_name", None):
            name = f"{name} {sender.last_name}".strip()
        username = getattr(sender, "username", "") or ""
    raw_text = (getattr(m, "text", None) or getattr(m, "message", None) or "").strip()
    if not raw_text:
        from chat_router import is_voice_media_message

        if is_voice_media_message(m):
            raw_text = "[голосовое]"
    return {
        "id": m.id,
        "sender_id": m.sender_id or 0,
        "sender_name": name or username or str(m.sender_id or "?"),
        "sender_username": username,
        "text": raw_text,
        "date": m.date.isoformat() if m.date else "",
        "out": bool(m.out),
    }


async def get_recent_messages(chat_id: int, *, limit: int = 25) -> list[dict[str, Any]]:
    client = await get_client()
    if not client:
        return []
    out = []
    async for m in client.iter_messages(chat_id, limit=limit):
        out.append(_message_row(m))
    out.reverse()
    return out


async def get_recent_messages_readonly(
    chat_id: int,
    *,
    limit: int = 25,
    client: TelegramClient | None = None,
) -> list[dict[str, Any]]:
    if client is None:
        client = await _acquire_readonly_client()
    if not client:
        return []
    return await _fetch_recent_messages(client, chat_id, limit=limit)


async def get_messages_from_anchor(
    chat_id: int,
    anchor: str,
    *,
    max_scan: int = 200,
) -> list[dict[str, Any]]:
    """Сообщения с первого, где встречается якорь, до конца (сканирует до max_scan)."""
    client = await get_client()
    if not client:
        return []
    batch: list[dict[str, Any]] = []
    async for m in client.iter_messages(chat_id, limit=max_scan):
        batch.append(_message_row(m))
    batch.reverse()
    start = 0
    for i, m in enumerate(batch):
        if _text_matches_anchor(m.get("text", ""), anchor):
            start = i
            break
    return batch[start:]


def format_messages_for_prompt(
    messages: list[dict[str, Any]],
    *,
    chat_title: str = "",
    chat_id: int | None = None,
    owner_id: int = OWNER_ID,
) -> str:
    lines = []
    if chat_title:
        lines.append(f"Чат: {chat_title}")
    from chat_router import (
        classify_chat_message,
        is_external_ack,
        is_registered_hoshi_message,
        is_security_alert_echo,
        text_looks_like_hoshi_reply,
    )

    for m in messages:
        who = classify_chat_message(m, chat_id=chat_id, owner_id=owner_id)
        raw = (m.get("text") or "").replace("\n", " ")
        msg_id = m.get("id")
        if (
            text_looks_like_hoshi_reply(raw)
            and chat_id
            and msg_id
            and not is_registered_hoshi_message(chat_id, int(msg_id))
        ):
            continue
        if is_external_ack(raw) or is_security_alert_echo(raw):
            continue
        if raw.strip().startswith("⚠️") or "Подозрение на скам" in raw:
            continue
        if re.search(r"(?:^|\s)Хозяин,\s*отвечаю\b", raw, flags=re.I):
            continue
        text = raw[:500]
        line = f"[{who}] {text}"
        if lines and lines[-1] == line:
            continue
        lines.append(line)
    return "\n".join(lines)


async def is_writable_chat(chat_id: int) -> bool:
    """Можно ли писать в чат с привязанного аккаунта (не broadcast-канал)."""
    client = await get_client()
    if not client:
        return False
    try:
        entity = await client.get_entity(chat_id)
    except Exception:
        return True
    if isinstance(entity, Channel) and getattr(entity, "broadcast", False):
        return False
    return True


async def send_typing_action(chat_id: int) -> None:
    client = await get_client()
    if not client:
        return
    entity = await client.get_entity(chat_id)
    await client(SetTypingRequest(peer=entity, action=SendMessageTypingAction()))


async def send_message_reaction(
    chat_id: int,
    message_id: int,
    *,
    reaction: str = "paid",
    big: bool = True,
) -> bool:
    from telethon.tl.functions.messages import SendReactionRequest
    from telethon.tl.types import ReactionEmoji, ReactionPaid

    client = await get_client()
    if not client:
        return False
    norm = (reaction or "paid").strip().lower()
    if norm in ("paid", "premium", "stars"):
        reacts = [ReactionPaid()]
    else:
        reacts = [ReactionEmoji(emoticon=reaction)]
    try:
        await client(
            SendReactionRequest(
                peer=chat_id,
                msg_id=int(message_id),
                reaction=reacts,
                big=big,
                add_to_recent=True,
            )
        )
        return True
    except Exception as e:
        log.warning(
            "send_message_reaction failed chat=%s msg=%s: %s",
            chat_id,
            message_id,
            e,
        )
        return False


def send_typing_action_sync(chat_id: int) -> None:
    from user_outbox import enqueue_user_typing

    enqueue_user_typing(chat_id)


def typing_loop(
    chat_id: int,
    stop: threading.Event,
    *,
    interval: float = 4.0,
    max_duration: float = 480.0,
) -> None:
    """Периодически шлёт «печатает…» через outbox bridge (не дольше max_duration)."""
    started = time.monotonic()
    while not stop.is_set():
        if time.monotonic() - started > max_duration:
            break
        try:
            send_typing_action_sync(chat_id)
        except Exception as e:
            log.debug("typing loop failed: %s", e)
        stop.wait(interval)


_ANIMATED_SUFFIXES = {".mp4", ".webm", ".gif"}
_STATIC_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def _normalize_media_path(path: Path) -> Path:
    """Абсолютный путь: data/... относительно корня проекта."""
    p = Path(path)
    if p.stem.endswith("_gif") and p.suffix.lower() in _STATIC_SUFFIXES:
        p = p.with_suffix("")
    if p.is_absolute():
        return p
    if p.parts and p.parts[0] == "data":
        return ROOT / p
    return (ROOT / p).resolve()


def _profile_gif_collection(profile_id: int, parent: Path) -> list[Path]:
    """Анимированные гифки из набора профиля (не текущая ава): <id>_<n>.mp4, n >= 1."""
    out: list[Path] = []
    for p in parent.glob(f"{profile_id}_*.mp4"):
        tag = p.stem.rsplit("_", 1)[-1]
        if tag.isdigit() and int(tag) >= 1:
            out.append(p)
    return sorted(out, key=lambda x: int(x.stem.rsplit("_", 1)[-1]))


def _pick_profile_gif(profile_id: int, parent: Path) -> Path | None:
    pool = _profile_gif_collection(profile_id, parent)
    return random.choice(pool) if pool else None


def _stable_profile_gif_path(path: Path, source: Path) -> Path:
    """Копирует анимацию в avatars/<id>_gif.mp4 для стабильной отправки."""
    profile_id, _, want_animated = _avatar_path_profile_id(path)
    if not want_animated or not profile_id:
        return source
    if source.suffix.lower() not in _ANIMATED_SUFFIXES:
        return source
    stable = source.parent / f"{profile_id}_gif{source.suffix.lower()}"
    if stable.resolve() == source.resolve():
        return source
    try:
        stable.parent.mkdir(parents=True, exist_ok=True)
        if stable.exists():
            return stable
        import shutil

        shutil.copy2(source, stable)
        return stable
    except OSError as e:
        log.warning("stable profile gif failed %s: %s", stable, e)
        return source


def _is_saved_gif_path(path: Path) -> bool:
    return _normalize_media_path(path).parent.name == "saved_gifs"


def _is_avatar_path(path: Path) -> bool:
    return _normalize_media_path(path).parent.name == "avatars"


def _avatar_file_hashes(profile_id: int) -> set[str]:
    """Хеши всех файлов профиля в avatars/ — чтобы не путать с saved_gifs."""
    import hashlib

    avatars_dir = MEDIA_DIR / "avatars"
    prefix = str(profile_id)
    out: set[str] = set()
    if not avatars_dir.is_dir():
        return out
    for p in avatars_dir.iterdir():
        if not p.is_file():
            continue
        stem = p.stem
        if stem != prefix and not stem.startswith(f"{prefix}_"):
            continue
        try:
            out.add(hashlib.md5(p.read_bytes()).hexdigest())
        except OSError:
            pass
    return out


def _is_avatar_duplicate(path: Path, profile_id: int) -> bool:
    import hashlib

    try:
        digest = hashlib.md5(path.read_bytes()).hexdigest()
    except OSError:
        return False
    return digest in _avatar_file_hashes(profile_id)


def _resolve_avatar_media_path(path: Path) -> Path | None:
    """Находит файл аватара: статику или анимацию (mp4/gif). Только avatars/."""
    path = _normalize_media_path(path)
    if path.parent.name != "avatars":
        return None
    if path.exists():
        return path
    for alt in sorted(path.parent.glob(f"{path.stem}.*")):
        if alt.suffix.lower() in _STATIC_SUFFIXES | _ANIMATED_SUFFIXES:
            return alt
    profile_id, _, want_animated = _avatar_path_profile_id(path)
    if want_animated and profile_id:
        picked = _pick_profile_gif(profile_id, path.parent)
        if picked:
            return picked
        for ext in (".gif", ".webm"):
            alt_stable = path.parent / f"{profile_id}_gif{ext}"
            if alt_stable.exists():
                return alt_stable
    return None


def _finalize_avatar_path(path: Path, saved: str | None) -> Path | None:
    """Приводит скачанный аватар к пути; анимации (mp4/gif) сохраняет как есть."""
    if saved:
        saved_path = Path(saved)
        if saved_path.exists() and saved_path != path:
            if path.exists():
                saved_path.unlink(missing_ok=True)
            elif (
                saved_path.suffix.lower() in _ANIMATED_SUFFIXES
                and path.suffix.lower() not in _ANIMATED_SUFFIXES
            ):
                return _stable_profile_gif_path(path, saved_path)
            else:
                saved_path.rename(path)
    if not path.exists():
        for alt in path.parent.glob(f"{path.stem}.*"):
            if alt.suffix.lower() in _STATIC_SUFFIXES:
                if alt != path:
                    alt.rename(path)
                break
            if alt.suffix.lower() in _ANIMATED_SUFFIXES:
                if path.suffix.lower() in _ANIMATED_SUFFIXES:
                    if alt != path:
                        alt.rename(path)
                else:
                    return _stable_profile_gif_path(path, alt)
                break
    resolved = _resolve_avatar_media_path(path)
    if resolved and resolved.suffix.lower() in _ANIMATED_SUFFIXES:
        return _stable_profile_gif_path(path, resolved)
    return resolved


def _extract_video_frame(video: Path, out_jpg: Path) -> bool:
    try:
        out_jpg.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(out_jpg),
            ],
            capture_output=True,
            check=True,
            timeout=30,
        )
        return out_jpg.exists()
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("avatar frame extract failed %s: %s", video, e)
        return False


def _avatar_path_profile_id(path: Path) -> tuple[int | None, int | None, bool]:
    """(user_id, offset_0based|None, first_animated) из avatars/<id>.ext или <id>_<n>.ext / <id>_gif."""
    stem = path.stem
    if stem.isdigit():
        return int(stem), 0, False
    if "_" not in stem:
        return None, None, False
    base, tag = stem.rsplit("_", 1)
    if not base.isdigit():
        return None, None, False
    profile_id = int(base)
    if tag.lower() in {"gif", "anim", "animated"}:
        return profile_id, None, True
    if tag.isdigit():
        return profile_id, max(0, int(tag) - 1), path.suffix.lower() in _ANIMATED_SUFFIXES
    return None, None, False


def _saved_gif_index(path: Path) -> int | None:
    """saved_gifs/<n> → 0-based; random → -1; иначе 0."""
    if path.parent.name != "saved_gifs":
        return None
    stem = path.stem.lower()
    if stem == "random":
        return -1
    if stem.isdigit():
        return max(0, int(stem) - 1)
    return 0


def _resolve_saved_gif_path(path: Path) -> Path | None:
    """Находит скачанную гифку из saved_gifs/<n>."""
    path = _normalize_media_path(path)
    if path.parent.name != "saved_gifs":
        return None
    if path.exists() and path.suffix.lower() in _ANIMATED_SUFFIXES:
        return path
    for alt in sorted(path.parent.glob(f"{path.stem}.*")):
        if alt.suffix.lower() in _ANIMATED_SUFFIXES:
            return alt
    return None


async def _fetch_saved_gifs() -> list:
    client = await get_client()
    if not client:
        return []
    try:
        result = await client(GetSavedGifsRequest(hash=0))
    except Exception as e:
        log.warning("get saved gifs failed: %s", e)
        return []
    if isinstance(result, SavedGifs):
        return list(result.gifs)
    return []


async def _ensure_saved_gif_file(path: Path) -> bool:
    """Скачивает гифку из сохранённого набора Telegram (не аватар)."""
    path = _normalize_media_path(path)
    if path.parent.name != "saved_gifs":
        return False
    idx_spec = _saved_gif_index(path)
    resolved = _resolve_saved_gif_path(path)
    if resolved:
        if _is_avatar_duplicate(resolved, OWNER_ID):
            log.warning("saved_gifs cache matches avatar, re-downloading %s", path.stem)
            resolved.unlink(missing_ok=True)
        elif idx_spec == -1:
            resolved.unlink(missing_ok=True)
        else:
            return True
    gifs = await _fetch_saved_gifs()
    if not gifs:
        log.warning("no saved gifs in telegram collection")
        return False
    avatar_hashes = _avatar_file_hashes(OWNER_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    client = await get_client()
    if not client:
        return False
    candidates: list[int]
    if idx_spec == -1:
        candidates = list(range(len(gifs)))
        random.shuffle(candidates)
    elif idx_spec is not None and idx_spec >= 0:
        primary = min(idx_spec, len(gifs) - 1)
        rest = [i for i in range(len(gifs)) if i != primary]
        random.shuffle(rest)
        candidates = [primary, *rest]
    else:
        candidates = list(range(len(gifs)))
        random.shuffle(candidates)
    for idx in candidates:
        try:
            saved = await client.download_media(
                gifs[idx],
                file=str(path.with_suffix("")),
            )
            if not saved or not Path(saved).exists():
                continue
            saved_path = Path(saved)
            if avatar_hashes and _is_avatar_duplicate(saved_path, OWNER_ID):
                log.warning("saved gif idx=%s is avatar duplicate, skip", idx)
                saved_path.unlink(missing_ok=True)
                continue
            return True
        except Exception as e:
            log.warning("download saved gif idx=%s failed: %s", idx, e)
    return False


async def _ensure_avatar_file(chat_id: int, path: Path) -> bool:
    """Скачивает аватар: avatars/<user_id>.jpg или avatars/<user_id>_<n>.ext (n с 1)."""
    if _resolve_avatar_media_path(path):
        return True
    if path.parent.name != "avatars":
        return False
    profile_id, offset, want_animated = _avatar_path_profile_id(path)
    if profile_id is None:
        return False
    client = await get_client()
    if not client:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if want_animated and offset is None:
            photos = await client.get_profile_photos(profile_id, limit=50)
            animated = [
                i
                for i, photo in enumerate(photos)
                if getattr(photo, "video_sizes", None) or getattr(photo, "document", None)
            ]
            pool = [i for i in animated if i > 0] or animated
            if not pool:
                log.warning("no animated avatar profile=%s", profile_id)
                return False
            offset = random.choice(pool)
        if offset == 0 and not want_animated:
            saved = await client.download_profile_photo(profile_id, file=str(path))
            return bool(_finalize_avatar_path(path, saved))
        photos = await client.get_profile_photos(profile_id, limit=(offset or 0) + 1)
        if not photos or len(photos) <= (offset or 0):
            log.warning(
                "avatar offset %s missing profile=%s (have %s)",
                offset,
                profile_id,
                len(photos),
            )
            return False
        saved = await client.download_media(
            photos[offset or 0],
            file=str(path.with_suffix("")),
        )
        return bool(_finalize_avatar_path(path, saved))
    except Exception as e:
        log.warning("download avatar failed profile=%s offset=%s: %s", profile_id, offset, e)
        return False


def _cleanup_sent_media(path: Path) -> None:
    """Удаляет временные медиа после успешной отправки."""
    import re

    from cache_cleanup import cleanup_video_artifacts, is_preserved_media_path
    from config import DATA, MEDIA_DIR

    try:
        resolved = path.resolve()
    except OSError:
        return
    if is_preserved_media_path(resolved):
        return
    # Входные фото владельца (batch YYYYMMDD_HHMMSS_hex) — не трогаем, агент может догенерировать.
    if re.fullmatch(r"20\d{6}_\d{6}_[0-9a-f]{6}", resolved.parent.name):
        return
    if cleanup_video_artifacts(resolved):
        return
    media_root = MEDIA_DIR.resolve()
    data_root = DATA.resolve()
    try:
        if resolved.is_relative_to(media_root):
            try:
                resolved.unlink(missing_ok=True)
            except OSError as e:
                log.debug("cleanup media %s: %s", path, e)
            return
    except (ValueError, AttributeError):
        if media_root in resolved.parents or resolved.parent == media_root:
            try:
                resolved.unlink(missing_ok=True)
            except OSError as e:
                log.debug("cleanup media %s: %s", path, e)
            return
    try:
        rel = resolved.relative_to(data_root)
    except ValueError:
        return
    if rel.parts[0] != "incoming_media":
        return
    try:
        resolved.unlink(missing_ok=True)
        parent = resolved.parent
        if parent.name == "avatars" and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError as e:
        log.debug("cleanup media %s: %s", path, e)


async def _wait_for_photo_path(
    path: Path,
    *,
    timeout: float = 90.0,
    interval: float = 2.0,
) -> Path | None:
    """Ждёт появления файла — генерация агентом может отставать от доставки."""
    deadline = time.monotonic() + timeout
    while True:
        if _is_saved_gif_path(path):
            resolved = _resolve_saved_gif_path(path)
        else:
            resolved = _resolve_avatar_media_path(path)
        if not resolved and path.exists() and path.suffix.lower() in (
            _STATIC_SUFFIXES | _ANIMATED_SUFFIXES
        ):
            resolved = path
        if resolved:
            return resolved
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(interval)


def _hoshi_outbound_send(text: str, chat_id: int) -> tuple[str, list | None]:
    from text_format import hoshi_outbound_parts

    if not (text or "").strip():
        return text or "", None
    return hoshi_outbound_parts(text, chat_id)


async def send_user_album(
    chat_id: int,
    photo_paths: list[str],
    *,
    reply_to: int | None = None,
    caption: str = "",
) -> int | None:
    """Отправляет несколько фото одним альбомом."""
    caption, caption_entities = (
        _hoshi_outbound_send(caption, chat_id) if caption.strip() else (caption, None)
    )
    client = await get_client()
    if not client:
        return None
    files: list[str] = []
    for photo_path in photo_paths:
        path = _normalize_media_path(Path(photo_path))
        if _is_saved_gif_path(path):
            resolved = _resolve_saved_gif_path(path)
            if not resolved:
                await _ensure_saved_gif_file(path)
                resolved = _resolve_saved_gif_path(path)
        elif path.parent.name == "avatars":
            resolved = _resolve_avatar_media_path(path)
            if not resolved:
                await _ensure_avatar_file(chat_id, path)
                resolved = _resolve_avatar_media_path(path)
        elif path.exists():
            resolved = path
        else:
            resolved = None
        if resolved:
            files.append(str(resolved))
    if not files:
        log.warning("album files missing: %s", photo_paths)
        if caption.strip():
            try:
                msg = await client.send_message(
                    chat_id,
                    caption,
                    reply_to=reply_to,
                    link_preview=False,
                    formatting_entities=caption_entities,
                )
                msg_id = int(msg.id) if msg else None
                if msg_id:
                    from chat_router import register_hoshi_message

                    register_hoshi_message(chat_id, msg_id, text=caption)
                return msg_id
            except Exception as e:
                log.warning("send_user_album caption fallback failed: %s", e)
        return None
    try:
        msg = await client.send_file(
            chat_id,
            files,
            caption=caption or None,
            reply_to=reply_to,
            formatting_entities=caption_entities,
        )
        msg_id = int(msg.id) if msg else None
        if msg_id:
            from chat_router import register_hoshi_message

            register_hoshi_message(chat_id, msg_id, text=caption or "[альбом]")
        return msg_id
    except Exception as e:
        log.warning("send_user_album failed: %s", e)
        return None


async def send_user_photo(
    chat_id: int,
    photo_path: str,
    *,
    reply_to: int | None = None,
    caption: str = "",
) -> int | None:
    caption, caption_entities = (
        _hoshi_outbound_send(caption, chat_id) if caption.strip() else (caption, None)
    )
    client = await get_client()
    if not client:
        return None
    path = _normalize_media_path(Path(photo_path))
    if _is_saved_gif_path(path):
        resolved = _resolve_saved_gif_path(path)
        if not resolved:
            await _ensure_saved_gif_file(path)
            resolved = _resolve_saved_gif_path(path)
    else:
        resolved = _resolve_avatar_media_path(path)
        if not resolved:
            await _ensure_avatar_file(chat_id, path)
            resolved = _resolve_avatar_media_path(path)
        if not resolved and path.exists() and path.suffix.lower() in (
            _STATIC_SUFFIXES | _ANIMATED_SUFFIXES
        ):
            resolved = path
    if not resolved:
        resolved = await _wait_for_photo_path(path)
    if resolved and _is_saved_gif_path(path) and _is_avatar_duplicate(resolved, OWNER_ID):
        log.warning("saved_gifs send blocked: avatar duplicate %s", resolved.name)
        resolved.unlink(missing_ok=True)
        await _ensure_saved_gif_file(path)
        resolved = _resolve_saved_gif_path(path)
    if not resolved:
        log.warning("photo file missing: %s", photo_path)
        from cache_cleanup import is_generated_photo_cache

        # Не шлём «Готово» без файла — иначе кажется, что фото ушло.
        if caption.strip() and not is_generated_photo_cache(Path(photo_path)):
            try:
                msg = await client.send_message(
                    chat_id,
                    caption,
                    reply_to=reply_to,
                    link_preview=False,
                    formatting_entities=caption_entities,
                )
                msg_id = int(msg.id) if msg else None
                if msg_id:
                    from chat_router import register_hoshi_message

                    register_hoshi_message(chat_id, msg_id, text=caption)
                return msg_id
            except Exception as e:
                log.warning("send_user_photo caption fallback failed: %s", e)
        return None
    try:
        send_kwargs: dict = {
            "caption": caption or None,
            "reply_to": reply_to,
            "force_document": False,
            "formatting_entities": caption_entities,
        }
        if resolved.suffix.lower() in _ANIMATED_SUFFIXES:
            from telethon.tl.types import DocumentAttributeAnimated

            send_kwargs["attributes"] = [DocumentAttributeAnimated()]
        msg = await client.send_file(
            chat_id,
            str(resolved),
            **send_kwargs,
        )
        msg_id = int(msg.id) if msg else None
        if msg_id:
            from chat_router import register_hoshi_message

            register_hoshi_message(chat_id, msg_id, text=caption or "[фото]")
            _cleanup_sent_media(path)
        return msg_id
    except Exception as e:
        log.warning("send_user_photo failed: %s", e)
        return None


def _probe_audio_duration(path: Path) -> int:
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=8,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return max(0, int(float(proc.stdout.strip())))
    except Exception:
        pass
    return 0


async def send_user_audio(
    chat_id: int,
    audio_path: str,
    *,
    reply_to: int | None = None,
    caption: str = "",
    title: str = "",
) -> int | None:
    """Отправляет аудио (mp3/m4a) как трек в плеере."""
    from telethon.tl.types import DocumentAttributeAudio, DocumentAttributeFilename
    from voice_delivery import audio_display_name

    caption, caption_entities = (
        _hoshi_outbound_send(caption, chat_id) if caption.strip() else (caption, None)
    )
    client = await get_client()
    if not client:
        return None
    path = Path(audio_path)
    if not path.exists() or path.stat().st_size <= 1024:
        from video_download import ensure_audio_file

        resolved = await asyncio.to_thread(ensure_audio_file, audio_path)
        if not resolved:
            log.warning("audio file missing: %s", audio_path)
            return None
        path = resolved
    display_title = audio_display_name(path, title=title)
    file_name = f"{display_title}{path.suffix.lower() or '.mp3'}"
    duration = await asyncio.to_thread(_probe_audio_duration, path)
    try:
        attrs = [
            DocumentAttributeFilename(file_name=file_name),
            DocumentAttributeAudio(duration=duration, title=display_title, voice=False),
        ]
        msg = await client.send_file(
            chat_id,
            str(path),
            caption=caption or None,
            attributes=attrs,
            reply_to=reply_to,
            formatting_entities=caption_entities,
        )
        msg_id = int(msg.id) if msg else None
        if msg_id:
            from chat_router import register_hoshi_message

            register_hoshi_message(
                chat_id, msg_id, text=caption or f"[аудио: {display_title}]"
            )
            _cleanup_sent_media(path)
        return msg_id
    except Exception as e:
        log.warning("send_user_audio failed: %s", e)
        return None


async def send_user_voice(
    chat_id: int,
    voice_path: str,
    *,
    reply_to: int | None = None,
) -> int | None:
    """Отправляет голосовое (OGG/OPUS)."""
    client = await get_client()
    if not client:
        return None
    path = Path(voice_path)
    if not path.exists():
        log.warning("voice file missing: %s", voice_path)
        return None
    try:
        msg = await client.send_file(
            chat_id,
            str(path),
            voice_note=True,
            reply_to=reply_to,
        )
        msg_id = int(msg.id) if msg else None
        if msg_id:
            from chat_router import register_hoshi_message

            register_hoshi_message(chat_id, msg_id, text="[голосовое]")
        return msg_id
    except Exception as e:
        log.warning("send_user_voice failed: %s", e)
        return None


async def edit_user_message(chat_id: int, message_id: int, text: str) -> bool:
    client = await get_client()
    if not client:
        return False
    try:
        await client.edit_message(chat_id, message_id, text, link_preview=False)
        return True
    except Exception as e:
        log.debug("edit_user_message failed: %s", e)
        return False


async def delete_user_message(chat_id: int, message_id: int) -> None:
    client = await get_client()
    if not client:
        return
    try:
        await client.delete_messages(chat_id, message_id)
    except Exception as e:
        log.debug("delete_user_message failed: %s", e)


async def send_user_video(
    chat_id: int,
    video_path: str,
    *,
    reply_to: int | None = None,
    caption: str = "",
    status_message_id: int | None = None,
    source_url: str = "",
) -> int | None:
    """Отправляет видеофайл с анимацией «отправляет видео»."""
    from telethon.tl.types import DocumentAttributeVideo

    from video_download import (
        ensure_video_caption,
        format_upload_progress,
        prepare_video_for_send,
        probe_video_meta,
    )

    client = await get_client()
    if not client:
        return None
    path = Path(video_path)
    if not path.exists():
        log.warning("video file missing: %s", video_path)
        return None
    video_caption = ensure_video_caption(caption, url=source_url, path=path)
    send_path, thumb = await asyncio.to_thread(prepare_video_for_send, path)
    w, h, dur = await asyncio.to_thread(probe_video_meta, send_path)
    last_status = 0.0
    try:
        async with client.action(chat_id, "video") as action:

            def _progress(current: int, total: int) -> None:
                nonlocal last_status
                action.progress(current, total)
                if not status_message_id or not total:
                    return
                now = time.monotonic()
                if now - last_status < 2.0:
                    return
                last_status = now
                text = format_upload_progress(current, total)
                asyncio.create_task(edit_user_message(chat_id, status_message_id, text))

            msg = await client.send_file(
                chat_id,
                str(send_path),
                caption=video_caption or None,
                reply_to=reply_to,
                supports_streaming=True,
                force_document=False,
                thumb=str(thumb) if thumb else None,
                attributes=[
                    DocumentAttributeVideo(
                        duration=dur,
                        w=w,
                        h=h,
                        supports_streaming=True,
                    )
                ],
                progress_callback=_progress,
            )
        msg_id = int(msg.id) if msg else None
        if msg_id:
            from chat_router import register_hoshi_message

            register_hoshi_message(
                chat_id,
                msg_id,
                text=video_caption,
                video_url=(source_url or "").strip() or str(path),
            )
            _cleanup_sent_media(path)
            if send_path != path:
                _cleanup_sent_media(send_path)
        return msg_id
    except Exception as e:
        log.warning("send_user_video failed: %s", e)
        return None


_video_status_ids: dict[int, int] = {}


def cancel_chat_video_state(chat_id: int) -> None:
    _video_status_ids.pop(int(chat_id), None)


async def deliver_video_from_url(
    chat_id: int,
    url: str,
    *,
    reply_to: int | None = None,
    owner_approved: bool = False,
    caption: str = "",
) -> int | None:
    """Скачивает по URL и отправляет с прогрессом и анимацией."""
    from video_download import download_video, format_download_progress, is_download_cancelled

    cid = int(chat_id)
    from user_outbox import is_chat_video_stopped

    if is_chat_video_stopped(cid) or is_download_cancelled():
        return None
    from video_download import ensure_video_caption, fetch_video_metadata

    meta = await asyncio.to_thread(fetch_video_metadata, url)
    video_caption = ensure_video_caption(caption, url=url, hint=(meta.get("title") or ""))
    status_id = _video_status_ids.get(cid)
    if status_id:
        await edit_user_message(cid, status_id, "📥 Качаю видео…")
    else:
        status_id = await send_user_message(
            cid,
            "📥 Качаю видео…",
            reply_to=reply_to,
            owner_approved=owner_approved,
        )
        if status_id:
            _video_status_ids[cid] = status_id
    loop = asyncio.get_running_loop()
    last_dl = 0.0

    def on_dl_progress(downloaded: int, total: int, eta: int | None) -> None:
        nonlocal last_dl
        if not status_id:
            return
        now = time.monotonic()
        if now - last_dl < 2.0:
            return
        last_dl = now
        text = format_download_progress(downloaded, total, eta)
        asyncio.run_coroutine_threadsafe(
            edit_user_message(chat_id, status_id, text),
            loop,
        )

    path = await asyncio.to_thread(download_video, url, on_progress=on_dl_progress)
    if not path:
        if owner_approved:
            from config import OWNER_ID
            from storage import enqueue_task, list_queue_items
            from config import INBOX

            code_fix_running = any(
                i.get("kind") == "code_fix"
                and i.get("status") in ("pending", "in_progress")
                for i in list_queue_items(INBOX)
            )
            from video_download import is_download_cancelled

            from user_outbox import is_chat_video_stopped

            if (
                not code_fix_running
                and not is_download_cancelled()
                and not is_chat_video_stopped(cid)
            ):
                enqueue_task(
                    source="telegram",
                    text=(
                        f"Не удалось скачать видео: {url}\n"
                        "download_video() вернул None. Улучши video_download.py "
                        "(yt-dlp стратегии, impersonate, referer, парсинг HTML). "
                        "Владельцу не отказывать."
                    ),
                    chat_id=OWNER_ID,
                    message_id=0,
                    kind="code_fix",
                    extra={
                        "retry_video_chat_id": chat_id,
                        "retry_video_url": url,
                        "retry_video_reply_to": reply_to,
                    },
                )
            if status_id:
                await edit_user_message(
                    chat_id,
                    status_id,
                    "🔧 Копаю этот сайт — пробую другой способ качалки…",
                )
        elif status_id:
            await edit_user_message(chat_id, status_id, "❌ Не скачалось с этой ссылки")
        return None

    if status_id:
        await edit_user_message(chat_id, status_id, "📤 Отправляю видео…")

    from video_download import prepare_video_for_send

    path, _thumb = await asyncio.to_thread(prepare_video_for_send, path)
    msg_id = await send_user_video(
        chat_id,
        str(path),
        reply_to=reply_to,
        caption=video_caption,
        status_message_id=status_id,
        source_url=url,
    )
    if status_id:
        _video_status_ids.pop(cid, None)
    if msg_id and status_id:
        await delete_user_message(chat_id, status_id)
    elif status_id and not msg_id:
        await edit_user_message(chat_id, status_id, "❌ Не удалось отправить видео")
    return msg_id


async def deliver_generate_video(
    chat_id: int,
    idea: str = "",
    *,
    reply_to: int | None = None,
    owner_approved: bool = False,
) -> int | None:
    """Генерирует ролик с нуля и отправляет — со статусом прогресса в чате."""
    from video_generate import generate_creative_video

    cid = int(chat_id)
    from user_outbox import is_chat_video_stopped

    if is_chat_video_stopped(cid):
        return None
    concept = (idea or "").strip() or "своя идея"
    status_id = await send_user_message(
        cid,
        f"🎬 Генерирую: {concept[:80]}",
        reply_to=reply_to,
        owner_approved=owner_approved,
    )
    loop = asyncio.get_running_loop()
    last_upd = 0.0

    def on_progress(step: str, current: int, total: int) -> None:
        nonlocal last_upd
        if not status_id:
            return
        now = time.monotonic()
        if now - last_upd < 1.5 and current < total:
            return
        last_upd = now
        pct = int(100 * current / max(total, 1))
        text = f"🎨 {step} — {current}/{total} ({pct}%)"
        asyncio.run_coroutine_threadsafe(
            edit_user_message(cid, status_id, text),
            loop,
        )

    path = await asyncio.to_thread(
        generate_creative_video,
        idea,
        on_progress=on_progress,
    )
    if not path:
        if status_id:
            await edit_user_message(cid, status_id, "❌ Не собрала ролик")
        return None
    is_gif = path.suffix.lower() == ".gif"
    if status_id:
        await edit_user_message(
            cid,
            status_id,
            "📤 Отправляю гифку…" if is_gif else "📤 Отправляю видео…",
        )
    if is_gif:
        msg_id = await send_user_photo(
            cid,
            str(path),
            reply_to=reply_to,
            caption="",
        )
    else:
        gen_caption = f"🎬 {concept[:200]}" if concept else ""
        msg_id = await send_user_video(
            cid,
            str(path),
            reply_to=reply_to,
            caption=gen_caption,
            status_message_id=status_id,
        )
    if msg_id and status_id:
        await delete_user_message(cid, status_id)
    elif status_id and not msg_id:
        await edit_user_message(
            cid,
            status_id,
            "❌ Не удалось отправить гифку" if is_gif else "❌ Не удалось отправить видео",
        )
    return msg_id


async def send_user_message(
    chat_id: int,
    text: str,
    *,
    reply_to: int | None = None,
    owner_approved: bool = False,
) -> int | None:
    """Отправляет сообщение. Возвращает id отправленного сообщения или None."""
    from chat_router import is_chat_muted, is_chat_write_forbidden
    from voice_delivery import (
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

    if is_chat_write_forbidden(int(chat_id)) and not owner_approved:
        log.info("send_user_message blocked (forbidden chat) chat=%s", chat_id)
        return None
    if is_chat_muted(int(chat_id)) and not owner_approved:
        log.info("send_user_message blocked (muted chat) chat=%s", chat_id)
        return None

    video_cap = extract_video_caption(text)
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
    if not plain and not photos and not audios and not videos and not gen_ideas:
        return None
    client = await get_client()
    if not client:
        return None
    msg_id: int | None = None
    try:
        if plain:
            outbound, outbound_entities = _hoshi_outbound_send(plain, chat_id)
            msg = await client.send_message(
                chat_id,
                outbound,
                reply_to=reply_to,
                link_preview=False,
                formatting_entities=outbound_entities,
            )
            msg_id = int(msg.id) if msg else None
            if msg_id:
                from chat_router import register_hoshi_message

                register_hoshi_message(chat_id, msg_id, text=outbound)
        for photo_path in photos:
            photo_id = await send_user_photo(
                chat_id, photo_path, reply_to=None, caption=""
            )
            if photo_id:
                msg_id = photo_id
        for audio_path in audios:
            audio_id = await send_user_audio(
                chat_id, audio_path, reply_to=None, caption=""
            )
            if audio_id:
                msg_id = audio_id
        for i, url in enumerate(videos):
            cap = video_cap if i == 0 and video_cap else ""
            if url.startswith(("http://", "https://")):
                vid_id = await deliver_video_from_url(
                    chat_id,
                    url,
                    reply_to=reply_to,
                    owner_approved=owner_approved,
                    caption=cap,
                )
            else:
                vid_id = await send_user_video(
                    chat_id, url, reply_to=reply_to, caption=cap
                )
            if vid_id:
                msg_id = vid_id
        for idea in gen_ideas:
            gen_id = await deliver_generate_video(
                chat_id,
                idea,
                reply_to=reply_to,
                owner_approved=owner_approved,
            )
            if gen_id:
                msg_id = gen_id
        react_to = reply_to
        for reaction_kind, reaction_mid in reactions:
            target = reaction_mid or react_to
            if target:
                await send_message_reaction(
                    chat_id,
                    int(target),
                    reaction=reaction_kind,
                )
        return msg_id
    except Exception as e:
        log.warning("send_user_message failed: %s", e)
        return None


def _normalize_telegram_message(msg):
    if msg is None:
        return None
    if isinstance(msg, list):
        return msg[0] if msg else None
    try:
        if not hasattr(msg, "id") and len(msg) > 0:
            return msg[0]
    except TypeError:
        pass
    return msg


async def get_message_text_preview(chat_id: int, message_id: int) -> str:
    """Короткий текст сообщения по id (для контекста реплая)."""
    client = await get_client()
    if not client:
        return ""
    try:
        raw = await client.get_messages(chat_id, ids=message_id)
        msg = _normalize_telegram_message(raw)
    except Exception:
        return ""
    if not msg:
        return ""
    body = (getattr(msg, "text", None) or getattr(msg, "message", None) or "").strip()
    return body[:500]


def extract_reply_info(message) -> tuple[int | None, str]:
    """id сообщения-родителя и выделенная цитата (Telegram quote_text)."""
    reply_to_id = getattr(message, "reply_to_msg_id", None)
    quote_text = ""
    reply_hdr = getattr(message, "reply_to", None)
    if reply_hdr is not None:
        if not reply_to_id:
            reply_to_id = getattr(reply_hdr, "reply_to_msg_id", None)
        quote_text = (getattr(reply_hdr, "quote_text", None) or "").strip()
    return (int(reply_to_id) if reply_to_id else None), quote_text


async def message_looks_like_hoshi(
    chat_id: int,
    message_id: int,
    *,
    quote_text: str = "",
) -> bool:
    """Реплай к ответу Hoshi (по реестру, сессии или по тексту исходящего)."""
    from chat_router import (
        classify_quote_fragment,
        is_registered_hoshi_message,
    )

    if not message_id:
        return False
    from chat_router import classify_chat_message, text_looks_like_hoshi_reply

    quote = (quote_text or "").strip()
    if quote:
        who = classify_quote_fragment(quote)
        if who == "Hoshi" or who.startswith("служебный отчёт Hoshi"):
            return True
        if who == "владелец":
            return False

    if is_registered_hoshi_message(chat_id, message_id):
        return True

    client = await get_client()
    if not client:
        return False
    try:
        raw = await client.get_messages(chat_id, ids=message_id)
        msg = _normalize_telegram_message(raw)
    except Exception:
        return False
    if not msg:
        return False

    row = _message_row(msg)
    is_out = bool(row.get("out"))

    # Реплай хозяина на сообщение собеседника — не считаем Hoshi по эвристикам текста.
    if not is_out:
        return False

    if classify_chat_message(row, chat_id=chat_id) == "Hoshi":
        return True

    body = (msg.text or "").strip()
    if body and text_looks_like_hoshi_reply(body):
        return True
    return False


async def fetch_reply_chain_texts(
    chat_id: int,
    message_id: int | None,
    *,
    max_depth: int = 4,
) -> list[str]:
    """Тексты сообщений в цепочке реплаев (цитируемое → родитель → …)."""
    if not message_id:
        return []
    client = await get_client()
    if not client:
        return []
    texts: list[str] = []
    cur = int(message_id)
    for _ in range(max_depth):
        try:
            raw = await client.get_messages(chat_id, ids=cur)
            msg = _normalize_telegram_message(raw)
        except Exception:
            break
        if not msg:
            break
        body = (getattr(msg, "text", None) or getattr(msg, "message", None) or "").strip()
        if not body or body in ("[видео]", "[фото]", "[аудио]"):
            from chat_router import enrich_reply_preview

            body = enrich_reply_preview(chat_id, cur, body)
        if body:
            texts.append(body[:500])
        parent = getattr(msg, "reply_to_msg_id", None)
        if not parent:
            break
        cur = int(parent)
    return texts


async def message_has_video(chat_id: int, message_id: int) -> bool:
    client = await get_client()
    if not client:
        return False
    try:
        raw = await client.get_messages(chat_id, ids=int(message_id))
        msg = _normalize_telegram_message(raw)
        return bool(msg and _message_has_video(msg))
    except Exception:
        return False


async def classify_reply_target(
    chat_id: int,
    message_id: int,
    *,
    quote_text: str = "",
) -> tuple[str, str]:
    """Кому принадлежит цитируемое сообщение: Hoshi / владелец / имя собеседника."""
    from chat_router import (
        classify_chat_message,
        classify_quote_fragment,
        enrich_reply_preview,
        format_video_reply_context,
        get_hoshi_message_preview,
        is_registered_hoshi_message,
    )

    preview = enrich_reply_preview(
        chat_id, message_id, get_hoshi_message_preview(chat_id, message_id)
    )
    client = await get_client()
    row: dict[str, Any] | None = None
    msg = None
    if client:
        try:
            raw = await client.get_messages(chat_id, ids=message_id)
            msg = _normalize_telegram_message(raw)
            if msg:
                row = _message_row(msg)
                if not preview:
                    preview = (row.get("text") or "").strip()[:500]
                preview = enrich_reply_preview(chat_id, message_id, preview)
                if _message_has_video(msg):
                    preview = format_video_reply_context(chat_id, message_id, preview)
        except Exception:
            pass

    quote = (quote_text or "").strip()
    if quote:
        who = classify_quote_fragment(quote, full_text=preview)
        return who, quote[:400]

    if is_registered_hoshi_message(chat_id, message_id):
        if not preview:
            preview = await get_message_text_preview(chat_id, message_id)
        from chat_router import get_hoshi_video_meta

        vmeta = get_hoshi_video_meta(chat_id, message_id)
        if vmeta.get("caption") or vmeta.get("url"):
            preview = format_video_reply_context(chat_id, message_id, preview)
        elif client:
            try:
                if msg and _message_has_video(msg):
                    preview = format_video_reply_context(chat_id, message_id, preview)
            except Exception:
                pass
        return "Hoshi", preview

    who = "?"
    if row:
        who = classify_chat_message(row, chat_id=chat_id)
    elif preview:
        from chat_router import text_looks_like_hoshi_reply

        who = "Hoshi" if text_looks_like_hoshi_reply(preview) else "владелец"
    return who, preview


def send_user_message_sync(chat_id: int, text: str, *, reply_to: int | None = None) -> bool:
    from user_outbox import enqueue_user_message

    enqueue_user_message(chat_id, text, reply_to=reply_to)
    return True


async def resolve_tg_chat_id(raw_id: int) -> int | None:
    """tg://chat?id=3867855265 → -1003867855265 или -3867855265."""
    client = await get_client()
    if not client:
        return None
    raw = int(raw_id)
    candidates: list[int] = []
    if raw > 0:
        candidates.extend([-int(f"100{raw}"), -raw, raw])
    else:
        candidates.append(raw)
    seen: set[int] = set()
    for cid in candidates:
        if cid in seen:
            continue
        seen.add(cid)
        try:
            await client.get_entity(cid)
            return cid
        except Exception:
            continue
    return None


async def download_chat_message_media(chat_id: int, message_id: int) -> str | None:
    """Скачивает фото/стикер/gif из конкретного сообщения чата."""
    client = await get_client()
    if not client:
        return None
    try:
        msg = await client.get_messages(chat_id, ids=int(message_id))
    except Exception as e:
        log.warning("get message failed chat=%s id=%s: %s", chat_id, message_id, e)
        return None
    if not msg or not (msg.photo or msg.sticker or msg.gif or getattr(msg, "document", None)):
        return None
    dest = MEDIA_DIR / f"ctx_{chat_id}_{message_id}"
    dest.mkdir(parents=True, exist_ok=True)
    try:
        path = await client.download_media(msg, file=str(dest / "media"))
        if not path or not Path(path).exists():
            return None
        resolved = Path(path)
        if resolved.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            stable = MEDIA_DIR / f"ctx_{chat_id}_{message_id}.jpg"
            try:
                from PIL import Image

                Image.open(resolved).convert("RGB").save(stable, "JPEG", quality=92)
                resolved.unlink(missing_ok=True)
                resolved = stable
            except Exception:
                stable = MEDIA_DIR / f"ctx_{chat_id}_{message_id}{resolved.suffix or '.bin'}"
                resolved.rename(stable)
                resolved = stable
        else:
            stable = MEDIA_DIR / f"ctx_{chat_id}_{message_id}{resolved.suffix.lower()}"
            if resolved.resolve() != stable.resolve():
                resolved.rename(stable)
                resolved = stable
        return str(resolved)
    except Exception as e:
        log.warning("download_chat_message_media failed chat=%s id=%s: %s", chat_id, message_id, e)
        return None


async def _resolve_photo_edit_message_id(
    chat_id: int,
    message_id: int,
    *,
    depth: int = 6,
) -> int | None:
    """Исходник для догенерации: фото хозяина, не ответ Hoshi с хентай/роликами."""
    from chat_router import is_registered_hoshi_message

    client = await get_client()
    if not client or depth <= 0:
        return None
    try:
        msg = await client.get_messages(chat_id, ids=int(message_id))
    except Exception:
        return None
    if not msg:
        return None
    has_photo = bool(msg.photo or msg.sticker)
    if has_photo:
        if msg.out and not is_registered_hoshi_message(chat_id, int(msg.id)):
            return int(msg.id)
        if msg.reply_to and getattr(msg.reply_to, "reply_to_msg_id", None):
            parent = await _resolve_photo_edit_message_id(
                chat_id,
                int(msg.reply_to.reply_to_msg_id),
                depth=depth - 1,
            )
            if parent:
                return parent
        if msg.out and is_registered_hoshi_message(chat_id, int(msg.id)):
            if msg.reply_to and getattr(msg.reply_to, "reply_to_msg_id", None):
                return await _resolve_photo_edit_message_id(
                    chat_id,
                    int(msg.reply_to.reply_to_msg_id),
                    depth=depth - 1,
                )
            return None
        return int(msg.id)
    if msg.reply_to and getattr(msg.reply_to, "reply_to_msg_id", None):
        return await _resolve_photo_edit_message_id(
            chat_id,
            int(msg.reply_to.reply_to_msg_id),
            depth=depth - 1,
        )
    return None


async def collect_chat_media_for_edit(
    chat_id: int,
    *,
    reply_to_id: int | None = None,
    message_id: int | None = None,
    limit: int = 40,
) -> list[str]:
    """Фото/стикер хозяина или реплая — для «измени это фото», без хентай из чата."""
    from chat_router import is_registered_hoshi_message

    if message_id:
        path = await download_chat_message_media(chat_id, int(message_id))
        if path:
            return [path]
    if reply_to_id:
        source_id = await _resolve_photo_edit_message_id(chat_id, int(reply_to_id))
        if source_id:
            path = await download_chat_message_media(chat_id, source_id)
            if path:
                return [path]
    client = await get_client()
    if not client:
        return []
    owner_path: str | None = None
    fallback: str | None = None
    try:
        async for msg in client.iter_messages(chat_id, limit=limit):
            if not (msg.photo or msg.sticker):
                continue
            if msg.out and is_registered_hoshi_message(chat_id, int(msg.id)):
                continue
            saved = await download_chat_message_media(chat_id, int(msg.id))
            if not saved:
                continue
            if msg.out:
                owner_path = saved
                break
            if not fallback:
                fallback = saved
    except Exception as e:
        log.warning("collect_chat_media_for_edit failed chat=%s: %s", chat_id, e)
    if owner_path:
        return [owner_path]
    return [fallback] if fallback else []


async def _album_message_ids(client, chat_id: int, msg) -> list[int]:
    gid = getattr(msg, "grouped_id", None)
    if not gid:
        return [int(msg.id)]
    ids: list[int] = []
    async for m in client.iter_messages(chat_id, limit=20, max_id=int(msg.id) + 1):
        if m and getattr(m, "grouped_id", None) == gid:
            ids.append(int(m.id))
    return sorted(ids) or [int(msg.id)]


async def collect_nearby_chat_media(
    chat_id: int,
    *,
    anchor_id: int | None = None,
    reply_to_id: int | None = None,
    limit: int = 30,
) -> list[str]:
    """Фото/альбомы рядом с реплаем — для заданий по скринам."""
    from chat_router import is_registered_hoshi_message

    client = await get_client()
    if not client:
        return []
    resolved = await resolve_tg_chat_id(chat_id)
    if not resolved:
        return []
    chat_id = int(resolved)
    seen: set[int] = set()
    paths: list[str] = []

    async def _grab(msg) -> None:
        if not msg or not (msg.photo or msg.sticker):
            return
        if msg.out and is_registered_hoshi_message(chat_id, int(msg.id)):
            return
        for mid in await _album_message_ids(client, chat_id, msg):
            if mid in seen:
                continue
            seen.add(mid)
            saved = await download_chat_message_media(chat_id, mid)
            if saved:
                paths.append(saved)

    try:
        if reply_to_id:
            msg = await client.get_messages(chat_id, ids=int(reply_to_id))
            await _grab(msg)
            if msg and msg.reply_to and getattr(msg.reply_to, "reply_to_msg_id", None):
                parent = await client.get_messages(
                    chat_id, ids=int(msg.reply_to.reply_to_msg_id)
                )
                await _grab(parent)
        start_id = anchor_id or reply_to_id
        async for msg in client.iter_messages(chat_id, limit=limit, max_id=(start_id or 0) + 1):
            if not (msg.photo or msg.sticker):
                continue
            if start_id and int(msg.id) > int(start_id) + 3:
                break
            await _grab(msg)
            if len(paths) >= 12:
                break
    except Exception as e:
        log.warning("collect_nearby_chat_media failed chat=%s: %s", chat_id, e)
    return paths


async def download_random_chat_media(
    raw_chat_id: int,
    *,
    limit: int = 200,
) -> str | None:
    """Скачивает случайное фото/видео/gif из чата (для пересылки по просьбе владельца)."""
    import random
    import uuid

    client = await get_client()
    if not client:
        return None
    chat_id = await resolve_tg_chat_id(raw_chat_id)
    if not chat_id:
        log.warning("resolve chat failed raw=%s", raw_chat_id)
        return None
    candidates = []
    async for m in client.iter_messages(chat_id, limit=limit):
        if m.photo or m.video or m.gif:
            candidates.append(m)
    if not candidates:
        return None
    msg = random.choice(candidates)
    dest = MEDIA_DIR / f"group_relay_{uuid.uuid4().hex[:8]}"
    dest.mkdir(parents=True, exist_ok=True)
    try:
        path = await client.download_media(msg, file=str(dest / "media"))
        return str(path) if path and Path(path).exists() else None
    except Exception as e:
        log.warning("download_random_chat_media failed chat=%s: %s", chat_id, e)
        return None


_TG_POST_URL_RE = re.compile(
    r"https?://(?:t\.me|telegram\.me)/(?:c/(\d+)/|([^/?#]+)/)(\d+)",
    re.I,
)


def parse_telegram_post_url(url: str) -> tuple[str | int, int] | None:
    """t.me/user/123 или t.me/c/1234567890/123 → (entity, message_id)."""
    m = _TG_POST_URL_RE.match((url or "").split("?")[0].strip())
    if not m:
        return None
    internal_id, username, msg_id = m.group(1), m.group(2), int(m.group(3))
    if internal_id:
        return int(f"-100{internal_id}"), msg_id
    return username, msg_id


def _message_has_video(msg) -> bool:
    if not msg:
        return False
    if msg.video or msg.gif:
        return True
    doc = getattr(msg, "document", None)
    if not doc:
        return False
    mime = (getattr(doc, "mime_type", "") or "").lower()
    if mime.startswith("video/"):
        return True
    from telethon.tl.types import DocumentAttributeAnimated, DocumentAttributeVideo

    for attr in getattr(doc, "attributes", []) or []:
        if isinstance(attr, (DocumentAttributeVideo, DocumentAttributeAnimated)):
            return True
    return False


async def download_telegram_post_media(
    url: str,
    *,
    on_progress=None,
) -> str | None:
    """Скачивает видео из поста t.me через привязанный аккаунт (приватные каналы)."""
    import uuid

    parsed = parse_telegram_post_url(url)
    if not parsed:
        return None
    entity_ref, message_id = parsed
    client = await get_client()
    if not client:
        log.warning("telegram post download: no user client for %s", url)
        return None
    try:
        entity = await client.get_entity(entity_ref)
        msg = await client.get_messages(entity, ids=message_id)
    except Exception as e:
        log.warning("telegram post fetch failed %s: %s", url, e)
        return None
    if not msg or not _message_has_video(msg):
        log.debug("telegram post %s: no video in message %s", url, message_id)
        return None

    dest = MEDIA_DIR / f"tg_dl_{uuid.uuid4().hex[:8]}"
    dest.mkdir(parents=True, exist_ok=True)
    last_prog = 0.0

    def progress_cb(current: int, total: int) -> None:
        nonlocal last_prog
        if not on_progress:
            return
        now = time.monotonic()
        if now - last_prog < 1.5:
            return
        last_prog = now
        on_progress(int(current or 0), int(total or 0), None)

    try:
        path = await client.download_media(
            msg,
            file=str(dest / "media"),
            progress_callback=progress_cb if on_progress else None,
        )
        if not path or not Path(path).exists():
            return None
        resolved = Path(path)
        if resolved.stat().st_size < 1024:
            resolved.unlink(missing_ok=True)
            return None
        stable = MEDIA_DIR / f"video_{uuid.uuid4().hex[:8]}{resolved.suffix.lower() or '.mp4'}"
        if resolved.resolve() != stable.resolve():
            resolved.rename(stable)
            resolved = stable
        log.info("telegram post ok %s -> %s", url, resolved.name)
        return str(resolved)
    except Exception as e:
        log.warning("telegram post download failed %s: %s", url, e)
        return None


def _run_on_bridge(coro, *, timeout: int = 120):
    try:
        if _bridge_loop and _bridge_loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(coro, _bridge_loop)
            return fut.result(timeout=timeout)
    except Exception as e:
        log.debug("bridge coro failed: %s", e)
    try:
        return asyncio.run(coro)
    except Exception as e:
        log.warning("asyncio.run coro failed: %s", e)
        return None


def download_chat_message_media_sync(
    chat_id: int, message_id: int, *, timeout: int = 120
) -> str | None:
    coro = download_chat_message_media(chat_id, int(message_id))
    return _run_on_bridge(coro, timeout=timeout)


def collect_chat_media_for_edit_sync(
    chat_id: int,
    *,
    reply_to_id: int | None = None,
    message_id: int | None = None,
    timeout: int = 120,
) -> list[str]:
    coro = collect_chat_media_for_edit(
        chat_id, reply_to_id=reply_to_id, message_id=message_id
    )
    hit = _run_on_bridge(coro, timeout=timeout)
    return hit or []


def download_telegram_post_media_sync(
    url: str,
    *,
    on_progress=None,
    timeout: int | None = None,
) -> str | None:
    """Синхронная обёртка для video_download (поток to_thread + bridge loop)."""
    from config import VIDEO_DOWNLOAD_TIMEOUT

    coro = download_telegram_post_media(url, on_progress=on_progress)
    wait = timeout or VIDEO_DOWNLOAD_TIMEOUT
    try:
        if _bridge_loop and _bridge_loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(coro, _bridge_loop)
            return fut.result(timeout=wait)
    except Exception as e:
        log.debug("telegram post sync via bridge failed %s: %s", url, e)
    try:
        return asyncio.run(coro)
    except Exception as e:
        log.warning("telegram post sync failed %s: %s", url, e)
        return None


async def build_chat_context_block(
    chat_id: int,
    *,
    title: str = "",
    limit: int = 25,
    anchor: str | None = None,
    max_scan: int = 200,
    readonly: bool = False,
) -> str:
    read_client = await _acquire_readonly_client() if readonly else await get_client()
    if not title:
        if readonly:
            ent = await get_entity_readonly(chat_id, client=read_client)
        else:
            ent = await get_entity(chat_id) if read_client else None
        if ent:
            title = getattr(ent, "title", None) or getattr(ent, "first_name", "") or str(chat_id)
    if anchor:
        messages = await get_messages_from_anchor(
            chat_id, anchor, max_scan=max(max_scan, limit)
        )
        header = (
            f"Переписка в чате «{title}» (id {chat_id}) "
            f"с сообщения «{anchor}» ({len(messages)} сообщ.):"
        )
    else:
        if readonly:
            messages = await get_recent_messages_readonly(
                chat_id, limit=limit, client=read_client
            )
        else:
            messages = await get_recent_messages(chat_id, limit=limit)
        header = f"Последние сообщения в чате «{title}» (id {chat_id}):"
    if not messages:
        return f"Чат {title} ({chat_id}): сообщений не найдено."
    body = format_messages_for_prompt(messages, chat_title=title, chat_id=chat_id)
    return f"{header}\n{body}"
