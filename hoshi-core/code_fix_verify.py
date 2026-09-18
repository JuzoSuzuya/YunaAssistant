#!/usr/bin/env python3
"""Проверка проекта после code_fix — синтаксис и живые процессы."""
from __future__ import annotations

import json
import py_compile
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from config import CURSOR_AGENT_BIN, DATA, ROOT, STATUS

_SKIP_DIRS = {".venv", "data", "__pycache__", ".git", "node_modules"}


@dataclass
class VerifyResult:
    ok: bool = True
    checks: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def add_ok(self, line: str) -> None:
        self.checks.append(f"✅ {line}")

    def add_fail(self, line: str) -> None:
        self.ok = False
        self.errors.append(line)
        self.checks.append(f"❌ {line}")

    def report_text(self) -> str:
        lines = ["**Проверка после правок:**", ""]
        lines.extend(self.checks)
        if not self.ok:
            lines.append("")
            lines.append("Нужно ещё поправить — не всё прошло.")
        return "\n".join(lines)


def _iter_py_files() -> list[Path]:
    out: list[Path] = []
    for path in ROOT.rglob("*.py"):
        if _SKIP_DIRS.intersection(path.parts):
            continue
        out.append(path)
    return out


def verify_syntax() -> VerifyResult:
    res = VerifyResult()
    failed: list[str] = []
    files = _iter_py_files()
    for path in files:
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as e:
            failed.append(f"{path.relative_to(ROOT)}: {e}")
    if failed:
        for hit in failed[:8]:
            res.add_fail(hit)
        if len(failed) > 8:
            res.add_fail(f"…ещё {len(failed) - 8} файл(ов) с ошибкой")
    else:
        res.add_ok(f"Синтаксис Python — {len(files)} файлов ок")
    return res


def verify_processes() -> VerifyResult:
    from restart_util import bridge_running, daemon_running

    res = VerifyResult()
    if bridge_running():
        res.add_ok("Bridge запущен")
    else:
        res.add_fail("Bridge не запущен")
    if daemon_running():
        res.add_ok("Daemon запущен")
    else:
        res.add_fail("Daemon не запущен")
    try:
        from chat_watcher import watcher_status

        st = watcher_status()
        if st.get("running"):
            res.add_ok("Chat watcher активен")
        else:
            res.add_fail(f"Watcher: {st.get('reason', 'не запущен')}")
    except Exception as e:
        res.add_fail(f"Watcher: {e}")
    return res


def verify_daemon_status() -> VerifyResult:
    res = VerifyResult()
    if not STATUS.exists():
        res.add_fail("status.json отсутствует")
        return res
    try:
        data = json.loads(STATUS.read_text(encoding="utf-8"))
    except Exception as e:
        res.add_fail(f"status.json: {e}")
        return res
    if not data.get("running"):
        res.add_fail("Демон помечен как остановленный")
    else:
        res.add_ok("Демон в статусе running")
    err = (data.get("last_error") or "").strip()
    if err:
        res.add_fail(f"last_error: {err[:200]}")
    pending = data.get("pending")
    if isinstance(pending, int) and pending > 20:
        res.add_fail(f"Очередь раздута: pending={pending}")
    elif isinstance(pending, int):
        res.add_ok(f"Очередь pending={pending}")
    return res


def verify_cursor_agent() -> VerifyResult:
    res = VerifyResult()
    try:
        from config import cursor_agent_env

        proc = subprocess.run(
            [CURSOR_AGENT_BIN, "status"],
            capture_output=True,
            text=True,
            timeout=12,
            cwd=str(ROOT),
            env=cursor_agent_env(),
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        low = out.lower()
        if proc.returncode != 0 or "log in" in low or "login" in low:
            res.add_fail("cursor-agent не авторизован")
        else:
            res.add_ok("cursor-agent авторизован")
    except FileNotFoundError:
        res.add_fail("cursor-agent не найден")
    except subprocess.TimeoutExpired:
        res.add_fail("cursor-agent status — таймаут")
    except Exception as e:
        res.add_fail(f"cursor-agent: {e}")
    return res


def verify_all(*, include_cursor: bool = True) -> VerifyResult:
    merged = VerifyResult()
    for part in (
        verify_syntax(),
        verify_processes(),
        verify_daemon_status(),
        *( [verify_cursor_agent()] if include_cursor else [] ),
    ):
        merged.checks.extend(part.checks)
        merged.errors.extend(part.errors)
        if not part.ok:
            merged.ok = False
    return merged


def syntax_errors_text() -> str:
    res = verify_syntax()
    if res.ok:
        return ""
    return "\n".join(res.errors)
