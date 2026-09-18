#!/usr/bin/env python3
"""Клиент RVC worker — edge-tts → аниме-голос."""
from __future__ import annotations

import json
import logging
import socket
import subprocess
import tempfile
import uuid
from pathlib import Path

from desk_config import (
    RVC_ENABLED,
    RVC_INDEX_RATE,
    RVC_PITCH,
    RVC_SOCKET,
    RVC_TIMEOUT,
)

log = logging.getLogger("yuna.rvc_client")


def rvc_available() -> bool:
    return RVC_ENABLED and RVC_SOCKET.exists()


def _to_wav(src: Path, dst: Path, *, sr: int = 40000) -> bool:
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-ar", str(sr), "-ac", "1", str(dst)],
            capture_output=True,
            timeout=60,
        )
        return proc.returncode == 0 and dst.exists() and dst.stat().st_size > 0
    except Exception as e:
        log.debug("wav convert failed: %s", e)
        return False


def _to_mp3(src: Path, dst: Path) -> bool:
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-codec:a", "libmp3lame", "-q:a", "2", str(dst)],
            capture_output=True,
            timeout=60,
        )
        return proc.returncode == 0 and dst.exists() and dst.stat().st_size > 0
    except Exception as e:
        log.debug("mp3 convert failed: %s", e)
        return False


def synth_silero(
    text: str,
    out_wav: Path,
    *,
    speaker: str = "baya",
    sample_rate: int = 48000,
    timeout: float | None = None,
) -> Path | None:
    """Просим тёплый воркер сгенерить речь через Silero. Возвращает путь к wav."""
    if not text.strip() or not RVC_SOCKET.exists():
        return None
    payload = json.dumps(
        {
            "tts_text": text,
            "output": str(out_wav),
            "speaker": speaker,
            "sample_rate": sample_rate,
        },
        ensure_ascii=False,
    ) + "\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout or max(RVC_TIMEOUT, 90))
            sock.connect(str(RVC_SOCKET))
            sock.sendall(payload.encode())
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
        resp = json.loads(buf.decode().strip() or "{}")
        if resp.get("ok") and out_wav.exists():
            return out_wav
        log.warning("silero synth failed: %s", resp.get("error"))
    except Exception as e:
        log.warning("silero client error: %s", e)
    return None


def apply_rvc(
    src: Path,
    *,
    want_mp3: bool = True,
    pitch: int | None = None,
    index_rate: float | None = None,
    model: str | None = None,
) -> Path | None:
    """Конвертирует TTS-файл через RVC worker. Возвращает путь к результату."""
    if not RVC_ENABLED:
        return src
    if not src.exists():
        return None
    pitch = RVC_PITCH if pitch is None else int(pitch)
    index_rate = RVC_INDEX_RATE if index_rate is None else float(index_rate)

    token = uuid.uuid4().hex[:10]
    work = src.parent
    wav_in = work / f"{token}_rvc_in.wav"
    wav_out = work / f"{token}_rvc_out.wav"
    mp3_out = work / f"{token}_rvc.mp3"

    if src.suffix.lower() == ".wav":
        wav_in = src
    elif not _to_wav(src, wav_in):
        log.warning("RVC: cannot prepare wav from %s", src)
        return src

    if not RVC_SOCKET.exists():
        log.warning("RVC worker not running (%s), using raw TTS", RVC_SOCKET)
        return src

    try:
        rvc_req = {
            "input": str(wav_in),
            "output": str(wav_out),
            "pitch": pitch,
            "index_rate": index_rate,
        }
        if model:
            rvc_req["model_name"] = str(model).lower()
        payload = json.dumps(rvc_req) + "\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(RVC_TIMEOUT)
            sock.connect(str(RVC_SOCKET))
            sock.sendall(payload.encode())
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
        resp = json.loads(buf.decode().strip() or "{}")
        if not resp.get("ok"):
            log.warning("RVC failed: %s", resp.get("error"))
            return src
        out = Path(str(resp.get("path", wav_out)))
        if not out.exists():
            return src
        if want_mp3 and _to_mp3(out, mp3_out):
            if wav_in != src:
                wav_in.unlink(missing_ok=True)
            out.unlink(missing_ok=True)
            if src.suffix.lower() != ".wav" and src != mp3_out:
                src.unlink(missing_ok=True)
            return mp3_out
        if want_mp3:
            return out
        if src.suffix.lower() != ".wav" and src != out:
            src.unlink(missing_ok=True)
        if wav_in != src:
            wav_in.unlink(missing_ok=True)
        return out
    except Exception as e:
        log.warning("RVC client error: %s", e)
        return src
    finally:
        if wav_in != src and wav_in.exists() and wav_in.name.endswith("_rvc_in.wav"):
            wav_in.unlink(missing_ok=True)
