#!/usr/bin/env python3
"""Захват экрана + описание (VLM / локальный мозг)."""
from __future__ import annotations

import logging
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

from desk_config import HOSHI_CORE

log = logging.getLogger("yuna.vision")
SCREEN_DIR = HOSHI_CORE / "data" / "incoming_media" / "screenshots"


def capture_screen() -> Path | None:
    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    token = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    out = SCREEN_DIR / f"{token}.png"

    for cmd in (
        ["grim", str(out)],
        ["import", "-window", "root", str(out)],
        ["spectacle", "-b", "-n", "-o", str(out)],
    ):
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=15)
            if proc.returncode == 0 and out.exists() and out.stat().st_size > 0:
                try:
                    from screen_context import capture_screen_meta
                    capture_screen_meta(out)
                except Exception:
                    pass
                return out
        except FileNotFoundError:
            continue
        except Exception:
            continue
    return None


def discard_screen(path: Path | str | None) -> None:
    """Удалить скрин после VLM — не копить гигабайты."""
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass


def prune_screenshots(*, keep: int = 3, max_age_sec: int = 3600) -> int:
    """Оставить последние keep файлов, старше max_age — тоже в мусор."""
    if not SCREEN_DIR.is_dir():
        return 0
    import time

    now = time.time()
    files = sorted(
        [p for p in SCREEN_DIR.glob("*.png") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    for i, p in enumerate(files):
        old = (now - p.stat().st_mtime) > max_age_sec
        if i >= keep or old:
            try:
                p.unlink(missing_ok=True)
                removed += 1
            except Exception:
                pass
    return removed


def describe_screen(path: Path | str | None = None) -> str:
    """Краткое описание кадра через Ollama VLM (бесплатно)."""
    shot = Path(path) if path else capture_screen()
    if not shot or not shot.is_file():
        return ""
    try:
        from ollama_brain import describe_image

        text = describe_image(shot)
        if text:
            try:
                from screen_context import load_context, save_context

                ctx = load_context()
                ls = dict(ctx.get("last_screen") or {})
                ls["path"] = str(shot)
                ls["caption"] = text[:800]
                ctx["last_screen"] = ls
                save_context(ctx)
            except Exception:
                pass
        return text
    except Exception as e:
        log.debug("describe_screen: %s", e)
        return ""
