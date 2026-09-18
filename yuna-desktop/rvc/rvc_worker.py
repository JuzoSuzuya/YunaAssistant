#!/usr/bin/env python3
"""RVC worker — держит модель в GPU, конвертирует по unix-socket."""
from __future__ import annotations

import json
import logging
import os
import signal
import socket
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
VENV_PY = ROOT / ".venv" / "bin" / "python"
if VENV_PY.exists():
    # Перезапуск в изолированном venv (Python 3.10 + torch cu128).
    if Path(sys.executable).resolve() != VENV_PY.resolve():
        os.execv(str(VENV_PY), [str(VENV_PY), str(__file__), *sys.argv[1:]])

import torch

_orig_torch_load = torch.load


def _torch_load_compat(*args, **kwargs):
    kwargs.setdefault("weights_only", False)
    return _orig_torch_load(*args, **kwargs)


torch.load = _torch_load_compat  # type: ignore[method-assign]

from rvc_convert import _MODELS, DEFAULT_INDEX, DEFAULT_MODEL, convert_file  # noqa: E402

log = logging.getLogger("yuna.rvc_worker")
_running = True
_rvc = None
_rvc_lock = threading.Lock()

SOCKET = Path(os.environ.get("YUNA_RVC_SOCKET", "/tmp/yuna-rvc.sock"))

_silero = None
_silero_lock = threading.Lock()
_SILERO_SPEAKERS = {"aidar", "baya", "kseniya", "xenia", "eugene", "random"}


def _get_silero():
    """Грузим Silero v4 (ru) один раз и держим в GPU."""
    global _silero
    if _silero is None:
        with _silero_lock:
            if _silero is None:
                device = torch.device(os.environ.get("YUNA_RVC_DEVICE", "cuda:0"))
                model, _ = torch.hub.load(
                    repo_or_dir="snakers4/silero-models",
                    model="silero_tts",
                    language="ru",
                    speaker="v4_ru",
                    trust_repo=True,
                )
                model.to(device)
                _silero = model
                log.info("Silero TTS loaded (v4_ru, device=%s)", device)
    return _silero


def _split_for_silero(text: str, limit: int = 800) -> list[str]:
    """Silero ограничивает длину — режем по предложениям."""
    import re

    text = text.strip()
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    buf = ""
    for chunk in re.split(r"(?<=[.!?…])\s+", text):
        if len(buf) + len(chunk) + 1 > limit and buf:
            parts.append(buf.strip())
            buf = chunk
        else:
            buf = f"{buf} {chunk}".strip()
    if buf.strip():
        parts.append(buf.strip())
    return parts or [text[:limit]]


def _silero_tts(text: str, out_wav: Path, *, speaker: str, sample_rate: int = 48000) -> None:
    import numpy as np
    import soundfile as sf

    if speaker not in _SILERO_SPEAKERS:
        speaker = "baya"
    model = _get_silero()
    pieces = []
    for part in _split_for_silero(text):
        if not part.strip():
            continue
        audio = model.apply_tts(
            text=part,
            speaker=speaker,
            sample_rate=sample_rate,
            put_accent=True,
            put_yo=True,
        )
        pieces.append(audio.cpu().numpy())
        pieces.append(np.zeros(int(sample_rate * 0.12), dtype=pieces[-1].dtype))
    if not pieces:
        raise RuntimeError("silero produced no audio")
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out_wav), np.concatenate(pieces), sample_rate)


def _stop(*_a: object) -> None:
    global _running
    _running = False


def _warm_model() -> None:
    """Прогрев: загрузка hubert + haruka при старте."""
    tmp_in = Path(tempfile.mkstemp(suffix=".wav")[1])
    tmp_out = Path(tempfile.mkstemp(suffix=".wav")[1])
    try:
        import subprocess

        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.3", "-ar", "40000", "-ac", "1", str(tmp_in)],
            capture_output=True,
            timeout=30,
        )
        convert_file(tmp_in, tmp_out)
        log.info("RVC model warmed (%s)", DEFAULT_MODEL.name)
    except Exception as e:
        log.warning("RVC warm-up failed (will retry on first request): %s", e)
    finally:
        tmp_in.unlink(missing_ok=True)
        tmp_out.unlink(missing_ok=True)


def _handle(conn: socket.socket) -> None:
    try:
        data = b""
        while b"\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return
            data += chunk
        req = json.loads(data.decode("utf-8").strip())

        if req.get("tts_text"):
            dst = Path(str(req.get("output", "")))
            with _rvc_lock:
                _silero_tts(
                    str(req["tts_text"]),
                    dst,
                    speaker=str(req.get("speaker", "baya")),
                    sample_rate=int(req.get("sample_rate", 48000)),
                )
            conn.sendall(json.dumps({"ok": True, "path": str(dst)}).encode() + b"\n")
            return

        src = Path(str(req.get("input", "")))
        dst = Path(str(req.get("output", "")))
        if not src.exists():
            conn.sendall(json.dumps({"ok": False, "error": f"input missing: {src}"}).encode() + b"\n")
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        model_path = Path(req.get("model", str(DEFAULT_MODEL)))
        index_path = Path(req["index"]) if req.get("index") else DEFAULT_INDEX
        name = str(req.get("model_name", "")).lower()
        if name and name in _MODELS:
            model_path, index_path = _MODELS[name]
        with _rvc_lock:
            convert_file(
                src,
                dst,
                model=model_path,
                index=index_path,
                device=os.environ.get("YUNA_RVC_DEVICE", "cuda:0"),
                pitch=int(req.get("pitch", os.environ.get("YUNA_RVC_PITCH", "2"))),
                index_rate=float(req.get("index_rate", os.environ.get("YUNA_RVC_INDEX_RATE", "0.75"))),
                f0_method=str(req.get("f0", os.environ.get("YUNA_RVC_F0", "rmvpe"))),
            )
        conn.sendall(json.dumps({"ok": True, "path": str(dst)}).encode() + b"\n")
    except Exception as e:
        log.exception("rvc request failed")
        try:
            conn.sendall(json.dumps({"ok": False, "error": str(e)}).encode() + b"\n")
        except OSError:
            pass
    finally:
        conn.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    SOCKET.unlink(missing_ok=True)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(SOCKET))
    srv.listen(4)
    srv.settimeout(1.0)
    log.info("RVC worker on %s (model=%s)", SOCKET, DEFAULT_MODEL)

    threading.Thread(target=_warm_model, daemon=True).start()

    while _running:
        try:
            conn, _ = srv.accept()
            threading.Thread(target=_handle, args=(conn,), daemon=True).start()
        except TimeoutError:
            continue
        except OSError:
            break
    srv.close()
    SOCKET.unlink(missing_ok=True)
    log.info("RVC worker stopped")


if __name__ == "__main__":
    main()
