#!/usr/bin/env python3
"""Запуск серверных процессов по запросу из Telegram."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from config import ROOT

DUBBING_ROOT = Path(os.environ.get("DUBBING_AI_ROOT", str(ROOT.parent / "DUBBING_AI")))

BILIBILI_URL_RE = re.compile(
    r"https?://(?:www\.)?bilibili\.com/(?:video/)?[A-Za-z0-9]+(?:[/?#][^\s]*)?",
    re.I,
)

DUB_TRIGGER_RE = re.compile(
    r"(?:"
    r"переведи(?:\s+мне)?\s+(?:видео|ролик)|"
    r"дубляж|"
    r"озвуч(?:ь|и)|"
    r"dub(?:bing)?|"
    r"билибили|bilibili"
    r")",
    re.I,
)

DAEMON_CANDIDATES = (
    "run_daemon.sh",
    "daemon.sh",
    "start.sh",
    "run.sh",
    "daemon.py",
    "worker.py",
)


def _extract_bilibili_url(text: str) -> str:
    m = BILIBILI_URL_RE.search(text or "")
    return (m.group(0) if m else "").strip()


def detect_bilibili_dub_task(text: str) -> dict[str, Any] | None:
    if not DUB_TRIGGER_RE.search(text or ""):
        return None
    url = _extract_bilibili_url(text)
    if not url:
        return None
    return {"task": "bilibili_dub", "url": url}


def _find_daemon(root: Path) -> Path | None:
    if not root.is_dir():
        return None
    for name in DAEMON_CANDIDATES:
        p = root / name
        if p.is_file():
            return p
    return None


def _write_job(root: Path, url: str) -> Path:
    inbox = root / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.-]+", "_", url)[:120]
    job = inbox / f"job_{safe}.url"
    job.write_text(url + "\n", encoding="utf-8")
    return job


def run_bilibili_dub(url: str) -> str:
    root = DUBBING_ROOT
    if not root.is_dir():
        return (
            "Проект дубляжа ещё не разложен на сервере — жду папку с настройками. "
            "Ссылку сохранила, как только всё будет на месте — запущу."
        )
    daemon = _find_daemon(root)
    if not daemon:
        _write_job(root, url)
        return (
            "Папка дубляжа на месте, демон пока не найден — ссылку положила в очередь. "
            "Когда скрипт появится, процесс подхватит задачу."
        )
    _write_job(root, url)
    cmd: list[str]
    if daemon.suffix == ".py":
        cmd = ["python3", str(daemon), url]
    else:
        cmd = ["bash", str(daemon), url]
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return f"Запустила перевод — процесс ушёл в фон (pid {proc.pid}). Отпишусь, когда будет готово."
    except Exception as e:
        return f"Не смогла стартовать демон: {e}"


def try_run_server_task(text: str) -> str | None:
    spec = detect_bilibili_dub_task(text)
    if not spec:
        return None
    if spec["task"] == "bilibili_dub":
        return run_bilibili_dub(spec["url"])
    return None
