"""
Клиент VPT-демона — вызывается из neuro_pilot (обычный Python Юны, без torch).
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("yuna.vpt_client")

ROOT = Path(__file__).resolve().parent
SOCK = Path(os.environ.get("YUNA_VPT_SOCK", str(ROOT / "vpt.sock")))
PY = ROOT / ".venv" / "bin" / "python"
SERVER = ROOT / "server.py"
LOG = ROOT / "vpt_server.log"


def _request(payload: dict, *, timeout: float = 8.0) -> dict[str, Any]:
    raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(SOCK))
        s.sendall(raw)
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    if not buf:
        return {"ok": False, "error": "empty response"}
    return json.loads(buf.decode("utf-8"))


def is_alive() -> bool:
    try:
        r = _request({"cmd": "ping"}, timeout=1.5)
        return bool(r.get("ok"))
    except Exception:
        return False


def ensure_server() -> bool:
    """Поднять VPT-демон, если ещё не крутится."""
    if is_alive():
        return True
    if not PY.exists() or not SERVER.exists():
        log.warning("vpt: нет .venv или server.py")
        return False
    try:
        if SOCK.exists():
            SOCK.unlink()
    except Exception:
        pass
    LOG.parent.mkdir(parents=True, exist_ok=True)
    log.info("стартую VPT-сервер...")
    with open(LOG, "a", encoding="utf-8") as lf:
        subprocess.Popen(
            [str(PY), str(SERVER)],
            cwd=str(ROOT),
            stdout=lf,
            stderr=lf,
            start_new_session=True,
        )
    for _ in range(40):
        time.sleep(0.5)
        if is_alive():
            log.info("VPT-сервер готов")
            return True
    log.error("VPT-сервер не ответил (см. %s)", LOG)
    return False


def act(frame_path: str | Path) -> dict[str, Any] | None:
    """Кадр → действие нейросети. None если сервер недоступен."""
    if not ensure_server():
        return None
    try:
        r = _request({"cmd": "act", "frame": str(frame_path)}, timeout=10.0)
    except Exception as e:
        log.warning("vpt act: %s", e)
        return None
    if not r.get("ok"):
        log.warning("vpt act fail: %s", r.get("error"))
        return None
    return r.get("action") if isinstance(r.get("action"), dict) else None


def reset() -> None:
    try:
        if is_alive():
            _request({"cmd": "reset"}, timeout=2.0)
    except Exception:
        pass
