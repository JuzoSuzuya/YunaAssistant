#!/usr/bin/env python3
"""Запись с микрофона — WebRTC VAD, одна непрерывная фраза (как voice input)."""
from __future__ import annotations

import asyncio
import logging
import signal
import struct
import subprocess
import threading
import uuid
import wave
from pathlib import Path

from desk_config import HOSHI_CORE, MIC_TARGET

log = logging.getLogger("yuna.listen")
CAPTURE_DIR = HOSHI_CORE / "data" / "incoming_media" / "voice_in"
CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
_MIC_LOCK = threading.Lock()
_SAMPLE_RATE = 16000
_FRAME_MS = 30
_CHUNK_SEC = 0.1
_PRE_ROLL_CHUNKS = 4
_SILENCE_SEC = 0.42
_MAX_UTTERANCE_SEC = 6.0
_MIN_UTTERANCE_SEC = 0.45
_SPEECH_START_CHUNKS = 2
VOSK_DIR = Path(__file__).resolve().parent / "models" / "vosk-model-small-ru-0.22"

_VOSK_MODEL: object | None = None
_VOSK_LOCK = threading.Lock()

_noise_floor = 4.0
_noise_calibrated = False


def _read_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wf:
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2 or wf.getframerate() != _SAMPLE_RATE:
            return b""
        return wf.readframes(wf.getnframes())


def _audio_energy(path: Path) -> float:
    try:
        frames = _read_pcm(path)
        if len(frames) < 4:
            return 0.0
        samples = struct.unpack(f"<{len(frames) // 2}h", frames)
        return sum(abs(s) for s in samples) / len(samples)
    except Exception:
        return 0.0


def _vad_ratio(pcm: bytes, *, aggressiveness: int | None = None) -> float:
    if len(pcm) < 320:
        return 0.0
    import webrtcvad

    vad = webrtcvad.Vad(aggressiveness if aggressiveness is not None else 2)
    frame_len = int(_SAMPLE_RATE * 2 * _FRAME_MS / 1000)
    speech = 0
    total = 0
    for i in range(0, len(pcm) - frame_len + 1, frame_len):
        frame = pcm[i : i + frame_len]
        if len(frame) < frame_len:
            break
        try:
            if vad.is_speech(frame, _SAMPLE_RATE):
                speech += 1
        except Exception:
            pass
        total += 1
    return speech / total if total else 0.0


def _chunk_is_speech(path: Path, *, sensitive: bool = False) -> bool:
    pcm = _read_pcm(path)
    energy = _audio_energy(path)
    min_energy = _noise_floor * (2.8 if sensitive else 3.5)
    if energy < max(min_energy, 10.0):
        return False
    if not pcm:
        return False
    ratio = _vad_ratio(pcm, aggressiveness=2)
    need = 0.32 if sensitive else 0.42
    return ratio >= need


def _calibrate_noise(samples: int = 4) -> float:
    levels: list[float] = []
    for _ in range(samples):
        p = CAPTURE_DIR / f"cal_{uuid.uuid4().hex[:6]}.wav"
        try:
            _capture_to(p, _CHUNK_SEC)
            levels.append(_audio_energy(p))
        finally:
            p.unlink(missing_ok=True)
    if not levels:
        return 4.0
    avg = sum(levels) / len(levels)
    return max(2.0, min(avg * 1.4, 25.0))


def cleanup_stale_recorders() -> None:
    try:
        proc = subprocess.run(
            ["pgrep", "-af", "pw-record"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if proc.returncode != 0:
            return
        for line in (proc.stdout or "").splitlines():
            if "voice_in/in_" not in line and "voice_in/cal_" not in line:
                continue
            pid = line.split(None, 1)[0]
            if pid.isdigit():
                subprocess.run(["kill", "-9", pid], capture_output=True, timeout=2)
    except Exception as e:
        log.debug("cleanup recorders: %s", e)


def _pw_record_cmd(out: Path, seconds: float) -> list[str]:
    samples = max(1, int(_SAMPLE_RATE * seconds))
    cmd = [
        "pw-record",
        "--rate",
        str(_SAMPLE_RATE),
        "--channels",
        "1",
        "-n",
        str(samples),
    ]
    if MIC_TARGET:
        cmd.extend(["--target", MIC_TARGET])
    cmd.append(str(out))
    return cmd


def _run_capture(cmd: list[str], out: Path, seconds: float) -> bool:
    try:
        if cmd[0] == "pw-record":
            proc = subprocess.Popen(cmd, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
            try:
                proc.wait(timeout=seconds + 2.5)
            except subprocess.TimeoutExpired:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1)
            return out.exists() and out.stat().st_size > 200
        proc = subprocess.run(cmd, capture_output=True, timeout=seconds + 3)
        return proc.returncode == 0 and out.exists() and out.stat().st_size > 200
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        log.debug("capture error: %s", e)
        return False


def _capture_to(out: Path, seconds: float) -> Path:
    seconds = max(0.1, float(seconds))
    for cmd in (
        _pw_record_cmd(out, seconds),
        [
            "arecord",
            "-q",
            "-f",
            "S16_LE",
            "-r",
            str(_SAMPLE_RATE),
            "-c",
            "1",
            "-d",
            str(max(1, int(seconds + 0.5))),
            str(out),
        ],
    ):
        if _run_capture(cmd, out, seconds):
            return out
        out.unlink(missing_ok=True)
    raise RuntimeError("microphone capture failed")


def record_sync(seconds: float = 5.0) -> Path:
    with _MIC_LOCK:
        out = CAPTURE_DIR / f"in_{uuid.uuid4().hex[:8]}.wav"
        return _capture_to(out, seconds)


def _concat_wavs(paths: list[Path]) -> Path:
    out = CAPTURE_DIR / f"in_{uuid.uuid4().hex[:8]}_cat.wav"
    frames = b""
    params = None
    for p in paths:
        if not p.exists():
            continue
        with wave.open(str(p), "rb") as wf:
            if params is None:
                params = wf.getparams()
            elif wf.getparams()[:3] != params[:3]:
                continue
            frames += wf.readframes(wf.getnframes())
    if not params or not frames:
        raise ValueError("empty wav concat")
    with wave.open(str(out), "wb") as wf:
        wf.setparams(params)
        wf.writeframes(frames)
    return out


_POLL_WAKE_SEC = 0.12


def wait_for_wake_then_phrase() -> Path:
    """
    Фаза 1: тихий poll — реагируем ТОЛЬКО на «Юна»/«Юно» в начале.
    Фаза 2: дописываем фразу до паузы (один поток, без probe/phrase).
    """
    from wake_word import chunk_has_wake_word

    global _noise_floor, _noise_calibrated
    with _MIC_LOCK:
        if not _noise_calibrated:
            _noise_floor = _calibrate_noise(3)
            _noise_calibrated = True
            log.info("noise floor %.1f", _noise_floor)

        tmp_paths: list[Path] = []
        try:
            while True:
                p = CAPTURE_DIR / f"wk_{uuid.uuid4().hex[:6]}.wav"
                tmp_paths.append(p)
                _capture_to(p, _POLL_WAKE_SEC)
                if not chunk_has_wake_word(p):
                    p.unlink(missing_ok=True)
                    tmp_paths.remove(p)
                    continue

                parts = [p]
                silent_run = 0.0
                speech_sec = _POLL_WAKE_SEC
                log.info("wake detected, recording phrase…")

                while speech_sec < _MAX_UTTERANCE_SEC:
                    p2 = CAPTURE_DIR / f"in_{uuid.uuid4().hex[:8]}.wav"
                    tmp_paths.append(p2)
                    _capture_to(p2, _CHUNK_SEC)
                    if p2 not in parts:
                        parts.append(p2)
                        speech_sec += _CHUNK_SEC
                    if _chunk_is_speech(p2):
                        silent_run = 0.0
                    else:
                        silent_run += _CHUNK_SEC
                        if silent_run >= _SILENCE_SEC and speech_sec >= _MIN_UTTERANCE_SEC:
                            break

                if len(parts) == 1:
                    out = parts[0]
                else:
                    out = _concat_wavs(parts)
                for t in tmp_paths:
                    if t != out and t.exists():
                        t.unlink(missing_ok=True)
                log.info("phrase %.2fs (%d chunks)", speech_sec, len(parts))
                return out
        except Exception:
            for t in tmp_paths:
                t.unlink(missing_ok=True)
            raise


def record_utterance(
    *,
    chunk_sec: float = _CHUNK_SEC,
    silence_sec: float = _SILENCE_SEC,
    max_sec: float = _MAX_UTTERANCE_SEC,
    pre_roll_chunks: int = _PRE_ROLL_CHUNKS,
) -> Path:
    """
    Ждёт речь (VAD), пишет одну непрерывную фразу до паузы ~0.38 с.
    «Юна, привет» — всё в одном файле, без разрыва probe/phrase.
    """
    global _noise_floor, _noise_calibrated
    with _MIC_LOCK:
        if not _noise_calibrated:
            _noise_floor = _calibrate_noise(3)
            _noise_calibrated = True
            log.info("noise floor %.1f", _noise_floor)
        ring: list[Path] = []
        parts: list[Path] = []
        phase = "wait"
        silent_run = 0.0
        speech_sec = 0.0
        speech_hits = 0
        tmp_paths: list[Path] = []

        try:
            while speech_sec < max_sec:
                p = CAPTURE_DIR / f"in_{uuid.uuid4().hex[:8]}.wav"
                tmp_paths.append(p)
                _capture_to(p, chunk_sec)

                if phase == "wait":
                    if len(ring) >= pre_roll_chunks:
                        dropped = ring.pop(0)
                        if dropped.exists():
                            dropped.unlink(missing_ok=True)
                        if dropped in tmp_paths:
                            tmp_paths.remove(dropped)
                    ring.append(p)
                    if _chunk_is_speech(p, sensitive=True):
                        speech_hits += 1
                    else:
                        speech_hits = 0
                    if speech_hits >= _SPEECH_START_CHUNKS:
                        phase = "record"
                        parts = list(ring)
                        speech_sec = len(parts) * chunk_sec
                        silent_run = 0.0
                        log.debug("speech start (%.2fs buffered)", speech_sec)
                    continue

                if phase == "record":
                    if p not in parts:
                        parts.append(p)
                        speech_sec += chunk_sec
                    if _chunk_is_speech(p):
                        silent_run = 0.0
                    else:
                        silent_run += chunk_sec
                        if silent_run >= silence_sec and speech_sec >= _MIN_UTTERANCE_SEC:
                            break

            if not parts:
                raise RuntimeError("no speech")

            if len(parts) == 1:
                out = parts[0]
                for t in tmp_paths:
                    if t != out and t.exists():
                        t.unlink(missing_ok=True)
                log.info("utterance %.2fs (1 chunk)", chunk_sec)
                return out

            out = _concat_wavs(parts)
            for t in tmp_paths:
                if t != out and t.exists():
                    t.unlink(missing_ok=True)
            dur = speech_sec
            log.info("utterance %.2fs (%d chunks)", dur, len(parts))
            return out
        except Exception:
            for t in tmp_paths:
                t.unlink(missing_ok=True)
            raise


def record_until_silence(
    *,
    max_sec: float = 3.2,
    chunk_sec: float = 0.22,
    min_energy: float = 8.0,
    silence_sec: float = 0.45,
) -> Path:
    """Legacy wrapper — делегирует в record_utterance."""
    return record_utterance(
        chunk_sec=chunk_sec,
        silence_sec=silence_sec,
        max_sec=max_sec,
    )


def _get_vosk_model():
    global _VOSK_MODEL
    with _VOSK_LOCK:
        if _VOSK_MODEL is not None:
            return _VOSK_MODEL
        if not VOSK_DIR.is_dir():
            log.warning("vosk model missing: %s", VOSK_DIR)
            return None
        from vosk import Model

        _VOSK_MODEL = Model(str(VOSK_DIR))
        log.info("vosk model loaded")
        return _VOSK_MODEL


async def record_and_transcribe(seconds: float = 5.0) -> tuple[str, Path | None]:
    from wake_word import transcribe_fast

    wav = await asyncio.to_thread(record_sync, seconds)
    try:
        text = await asyncio.to_thread(transcribe_fast, wav)
        return (text or "").strip(), wav
    finally:
        wav.unlink(missing_ok=True)
