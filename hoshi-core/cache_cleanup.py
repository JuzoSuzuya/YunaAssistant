#!/usr/bin/env python3
"""Очистка временного кеша без потери памяти (сессии, настройки, история)."""
from __future__ import annotations

import logging
import re
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

from config import DATA, INBOX, MEDIA_DIR, OUTBOX, ROOT, SESSIONS

log = logging.getLogger("hoshi.cache")

PRESERVE_MEDIA_DIRS = frozenset({"avatars", "saved_gifs", "refs", "ref", "voice_out"})
OWNER_BATCH_RE = re.compile(r"^20\d{6}_\d{6}_[0-9a-f]{6}$")
VIDEO_STEM_RE = re.compile(r"^video_[0-9a-f]+$", re.I)
GEN_PHOTO_RE = re.compile(r"^(gen_|ctx_)", re.I)


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                total += p.stat().st_size
    except OSError:
        pass
    return total


def _unlink(path: Path) -> int:
    try:
        size = path.stat().st_size if path.is_file() else _dir_size(path)
    except OSError:
        return 0
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
        return size
    except OSError as e:
        log.debug("unlink %s: %s", path, e)
        return 0


def _pending_image_paths() -> set[str]:
    paths: set[str] = set()
    if not SESSIONS.exists():
        return paths
    for session_path in SESSIONS.glob("*.json"):
        try:
            import json

            data = json.loads(session_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        pending = data.get("pending_images") or {}
        for p in pending.get("paths") or []:
            paths.add(str(p))
    return paths


def is_generated_photo_cache(path: Path) -> bool:
    """gen_/ctx_ — результат обработки, не удалять до отправки."""
    name = path.name
    return bool(GEN_PHOTO_RE.match(name) or "_fixed" in name.lower())


def is_preserved_media_path(path: Path) -> bool:
    try:
        rel = path.resolve().relative_to(MEDIA_DIR.resolve())
    except (OSError, ValueError):
        return False
    if not rel.parts:
        return False
    if rel.parts[0] in PRESERVE_MEDIA_DIRS:
        return True
    if OWNER_BATCH_RE.fullmatch(rel.parts[0]):
        return True
    return False


def cleanup_video_artifacts(path: Path) -> int:
    """Удаляет видео и связанные превью/частичные загрузки."""
    if is_preserved_media_path(path):
        return 0
    freed = 0
    stem = path.stem
    if stem.endswith("_thumb"):
        stem = stem[: -len("_thumb")]
    if stem.endswith("_tg"):
        stem = stem[: -len("_tg")]
    if VIDEO_STEM_RE.fullmatch(stem):
        for sibling in MEDIA_DIR.glob(f"{stem}*"):
            if is_preserved_media_path(sibling):
                continue
            freed += _unlink(sibling)
        return freed
    if path.parent == MEDIA_DIR:
        if is_generated_photo_cache(path):
            return 0
        return _unlink(path)
    if path.suffix.lower() == ".part" and path.name.startswith("video_"):
        return _unlink(path)
    if path.name.startswith("video_"):
        return _unlink(path)
    return 0


def purge_media_cache(*, max_owner_batch_age_sec: int = 3600) -> dict[str, int]:
    """Удаляет временные медиа; память (sessions/settings) не трогает."""
    stats = {"files": 0, "dirs": 0, "bytes": 0}
    pending = _pending_image_paths()
    cutoff = time.time() - max(0, max_owner_batch_age_sec)

    if not MEDIA_DIR.exists():
        return stats

    for path in sorted(MEDIA_DIR.iterdir(), key=lambda p: (p.is_file(), p.name)):
        if path.name in {"__pycache__"}:
            continue
        if path.is_dir():
            if path.name in PRESERVE_MEDIA_DIRS:
                if path.name == "voice_out":
                    for child in list(path.iterdir()):
                        stats["bytes"] += _unlink(child)
                        stats["files"] += 1
                continue
            if OWNER_BATCH_RE.fullmatch(path.name):
                keep = any(str(path) in p or p.startswith(str(path)) for p in pending)
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    mtime = 0
                if keep or mtime >= cutoff:
                    continue
            stats["bytes"] += _unlink(path)
            stats["dirs"] += 1
            continue

        # Файлы в корне incoming_media — кеш; gen_/ctx_ не трогаем (ждут отправки).
        if path.is_file() and is_generated_photo_cache(path):
            continue
        stats["bytes"] += cleanup_video_artifacts(path) or _unlink(path)
        stats["files"] += 1

    for stray in ROOT.glob("video*.mp4"):
        stats["bytes"] += _unlink(stray)
        stats["files"] += 1

    return stats


def purge_stale_queue_json(*, max_age_hours: int = 48) -> dict[str, int]:
    """Старые done-записи inbox/outbox — не память, а очередь."""
    stats = {"files": 0, "bytes": 0}
    cutoff = datetime.now() - timedelta(hours=max(1, max_age_hours))
    for folder in (INBOX, OUTBOX):
        if not folder.exists():
            continue
        for path in folder.glob("*.json"):
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime)
            except OSError:
                continue
            if mtime >= cutoff:
                continue
            try:
                import json

                item = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                item = {}
            if item.get("status") in ("pending", "in_progress"):
                continue
            stats["bytes"] += _unlink(path)
            stats["files"] += 1
    return stats


def run_cache_maintenance(*, log_fn=None) -> dict[str, dict[str, int]]:
    """Полная уборка кеша; вызывается при старте и периодически."""
    media = purge_media_cache()
    queue = purge_stale_queue_json()
    result = {"media": media, "queue": queue}
    total_mb = (media["bytes"] + queue["bytes"]) / (1024 * 1024)
    if log_fn and (media["files"] + media["dirs"] + queue["files"]):
        log_fn(
            f"Cache cleanup: media {media['files']} files, {media['dirs']} dirs, "
            f"queue {queue['files']} — freed ~{total_mb:.1f} MB"
        )
    return result


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    r = run_cache_maintenance(log_fn=print)
    print(r, file=sys.stderr)
