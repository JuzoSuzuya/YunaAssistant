#!/usr/bin/env python3
"""
Wake word «Юна» — Vosk быстрый STT, Whisper только запасной.
"""
from __future__ import annotations

import json
import logging
import re
import time
import wave
from pathlib import Path

from voice_listen import VOSK_DIR, _audio_energy, _get_vosk_model, record_sync

log = logging.getLogger("yuna.wake")

WAKE_GRAMMAR = '["юна", "юно", "[unk]"]'
WAKE_TOKENS = frozenset({"юна", "yuna", "юно"})
WAKE_START = ("юна", "yuna", "юно")
MIN_ENERGY = 8
POLL_SEC = 0.12


def _norm_word(w: str) -> str:
    return re.sub(r"[^\wа-яё]", "", w.lower(), flags=re.I)


def _vosk_recognize(path: Path, *, grammar: str | None = None) -> str:
    model = _get_vosk_model()
    if model is None or not VOSK_DIR.is_dir():
        return ""
    from vosk import KaldiRecognizer

    rec = KaldiRecognizer(model, 16000, grammar) if grammar else KaldiRecognizer(model, 16000)
    with wave.open(str(path), "rb") as wf:
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
            return ""
        while True:
            data = wf.readframes(4000)
            if not data:
                break
            rec.AcceptWaveform(data)
    try:
        result = json.loads(rec.FinalResult())
        return (result.get("text") or "").strip().lower()
    except Exception:
        return ""


def chunk_has_wake_word(path: Path) -> bool:
    """Строго: первое слово чанка = «юна»/«юно» (не дюна/ТВ)."""
    if _audio_energy(path) < max(MIN_ENERGY, 6):
        return False
    text = _vosk_recognize(path, grammar=WAKE_GRAMMAR)
    if not text:
        return False
    first = _norm_word(text.split()[0])
    if first in {_norm_word(w) for w in WAKE_TOKENS}:
        log.info("wake chunk: %s", first)
        return True
    return False


def vosk_wake_at_start(path: Path) -> bool:
    """Полная фраза должна начинаться с «Юна» — без ложных дюна/уна."""
    text = _vosk_recognize(path, grammar=None)
    if not text:
        return False
    return starts_with_yuna(text)


def starts_with_yuna(text: str) -> bool:
    raw = (text or "").strip().lower()
    if not raw:
        return False
    for wake in sorted(WAKE_START, key=len, reverse=True):
        if raw == wake or raw.startswith(wake + " ") or raw.startswith(wake + ","):
            return True
        if raw.startswith(wake):
            rest = raw[len(wake) :]
            if not rest or rest[0] in " ,.!?—":
                return True
    first = raw.split(maxsplit=1)[0] if raw.split() else ""
    return _norm_word(first) in {_norm_word(w) for w in WAKE_START}


def command_after_yuna(text: str) -> str:
    raw = (text or "").strip()
    low = raw.lower()
    for wake in sorted(WAKE_START, key=len, reverse=True):
        if low.startswith(wake):
            return raw[len(wake) :].lstrip(" ,.—!?…").strip()
    return raw.strip()


_WHISPER: object | None = None


def _get_whisper():
    global _WHISPER
    if _WHISPER is None:
        from faster_whisper import WhisperModel

        _WHISPER = WhisperModel("base", device="cpu", compute_type="int8")
        log.info("whisper base loaded (fallback)")
    return _WHISPER


def transcribe_vosk(path: Path) -> str:
    """Только Vosk — без Whisper (голосовой путь, ~0.2 с)."""
    t0 = time.perf_counter()
    text = _vosk_recognize(path, grammar=None)
    if text:
        log.info("vosk (%.2fs): %s", time.perf_counter() - t0, text[:100])
    return text


def transcribe_fast(path: Path) -> str:
    """Vosk ~0.2с, Whisper только если Vosk пустой."""
    t0 = time.perf_counter()
    text = _vosk_recognize(path, grammar=None)
    if len(text) >= 2:
        log.info("vosk heard (%.2fs): %s", time.perf_counter() - t0, text[:100])
        return text
    try:
        model = _get_whisper()
        segments, _ = model.transcribe(
            str(path),
            language="ru",
            beam_size=1,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        parts = [s.text.strip() for s in segments if s.text.strip()]
        text = re.sub(r"\s+", " ", " ".join(parts)).strip()
        if text:
            log.info("whisper heard (%.2fs): %s", time.perf_counter() - t0, text[:100])
        return text
    except Exception as e:
        log.warning("whisper: %s", e)
        return ""


def concat_wavs(paths: list[Path]) -> Path:
    from voice_listen import _concat_wavs

    return _concat_wavs(paths)


transcribe_ru = transcribe_fast
transcribe_command = transcribe_fast
