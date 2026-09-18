#!/usr/bin/env python3
"""
VPT-демон: держит нейросеть в GPU-памяти и отвечает на запросы по Unix-сокету.

Протокол (по одной JSON-строке на запрос/ответ):
  → {"cmd":"act","frame":"/path/to.png"}
  ← {"ok":true,"action":{...},"ms":12.3}
  → {"cmd":"reset"}
  ← {"ok":true}
  → {"cmd":"ping"}
  ← {"ok":true,"device":"cuda"}
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from model import VPTMotor  # noqa: E402

log = logging.getLogger("yuna.vpt_server")
SOCK = Path(os.environ.get("YUNA_VPT_SOCK", str(ROOT / "vpt.sock")))


def _load_frame(path: str) -> np.ndarray:
    img = cv2.imread(path, cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(f"не читается кадр: {path}")
    # BGR → RGB (VPT ждёт RGB)
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    # привести к ~640x360 (как MineRL), сохраняя пропорции
    h, w = rgb.shape[:2]
    target_w, target_h = 640, 360
    if (w, h) != (target_w, target_h):
        rgb = cv2.resize(rgb, (target_w, target_h), interpolation=cv2.INTER_LINEAR)
    return rgb


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
    if SOCK.exists():
        SOCK.unlink()
    log.info("загружаю VPT на GPU...")
    motor = VPTMotor()
    log.info("VPT готова, device=%s sock=%s", motor.device, SOCK)

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(SOCK))
    srv.listen(4)
    SOCK.chmod(0o666)

    while True:
        conn, _ = srv.accept()
        with conn:
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            if not buf:
                continue
            try:
                req = json.loads(buf.decode("utf-8"))
            except Exception as e:
                conn.sendall((json.dumps({"ok": False, "error": str(e)}) + "\n").encode())
                continue

            cmd = str(req.get("cmd") or "")
            try:
                if cmd == "ping":
                    resp = {"ok": True, "device": motor.device}
                elif cmd == "reset":
                    motor.reset()
                    resp = {"ok": True}
                elif cmd == "act":
                    frame_path = str(req.get("frame") or "")
                    t0 = time.perf_counter()
                    frame = _load_frame(frame_path)
                    action = motor.act(frame)
                    ms = (time.perf_counter() - t0) * 1000
                    resp = {"ok": True, "action": action, "ms": round(ms, 1)}
                else:
                    resp = {"ok": False, "error": f"unknown cmd: {cmd}"}
            except Exception as e:
                log.exception("vpt cmd %s", cmd)
                resp = {"ok": False, "error": str(e)}
            conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode())


if __name__ == "__main__":
    main()
