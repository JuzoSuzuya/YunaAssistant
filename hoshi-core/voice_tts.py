#!/usr/bin/env python3
"""Синтез и распознавание голосовых сообщений."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from config import MEDIA_DIR
from personas import get_persona

log = logging.getLogger("hoshi.voice")

VOICE_CACHE = MEDIA_DIR / "voice_out"
VOICE_CACHE.mkdir(parents=True, exist_ok=True)

_CYRILLIC_RE = re.compile(r"[а-яА-ЯёЁ]")
_RU_VOICE = "ru-RU-SvetlanaNeural"


def _resolve_tts_voice(text: str, persona: dict[str, object] | None) -> str:
    voice = str((persona or {}).get("tts_voice") or _RU_VOICE)
    if _CYRILLIC_RE.search(text) and voice.startswith("ja-"):
        return _RU_VOICE
    return voice


def _resolve_tts_prosody(persona: dict[str, object] | None) -> dict[str, str]:
    p = persona or {}
    return {
        "rate": str(p.get("tts_rate") or "+0%"),
        "pitch": str(p.get("tts_pitch") or "+0Hz"),
        "volume": str(p.get("tts_volume") or "+0%"),
    }


def _apply_pitch_shift(src: Path, persona: dict[str, object] | None) -> Path:
    """Доп. сдвиг тембра (1.0 = без изменений). rubberband сохраняет форманты."""
    shift = float((persona or {}).get("tts_pitch_shift") or 1.0)
    if abs(shift - 1.0) < 0.01:
        return src
    out = src.with_name(f"{src.stem}_ps{src.suffix}")
    use_rubberband = (persona or {}).get("tts_rubberband", True)
    if use_rubberband:
        rb = [f"pitch={shift:.4f}"]
        formant = str((persona or {}).get("tts_rubberband_formant") or "").strip()
        if formant:
            rb.append(f"formant={formant}")
        transients = str((persona or {}).get("tts_rubberband_transients") or "").strip()
        if transients:
            rb.append(f"transients={transients}")
        af = "rubberband=" + ":".join(rb)
    else:
        af = f"asetrate=44100*{shift:.3f},aresample=44100,atempo={1/shift:.3f}"
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-filter:a", af, str(out)],
            capture_output=True,
            timeout=60,
        )
        if proc.returncode == 0 and out.exists() and out.stat().st_size > 0:
            src.unlink(missing_ok=True)
            return out
    except Exception as e:
        log.debug("pitch shift failed: %s", e)
    out.unlink(missing_ok=True)
    return src


def _persona_ffmpeg_af(persona: dict[str, object] | None) -> str | None:
    custom = str((persona or {}).get("tts_audio_filter") or "").strip()
    if custom in ("", "none", "anull"):
        return None
    if custom:
        return custom
    return "highpass=f=60,lowpass=f=14000"


def _piper_model_path() -> Path | None:
    p = os.environ.get("YUNA_PIPER_MODEL", "").strip()
    if p and Path(p).exists():
        return Path(p)
    return None


def _tts_engine(persona: dict[str, object] | None = None) -> str:
    """Движок TTS: silero/piper (локальные) или edge. Из персоны → env → авто."""
    eng = str((persona or {}).get("tts_engine") or "").strip().lower()
    if eng not in ("silero", "piper", "edge"):
        eng = os.environ.get("YUNA_TTS_ENGINE", "").strip().lower()
    if eng in ("silero", "piper", "edge"):
        return eng
    return "piper" if _piper_model_path() else "edge"


def _synthesize_silero(text: str, out_wav: Path, persona: dict[str, object]) -> bool:
    """Silero через тёплый RVC-воркер (живая русская интонация)."""
    speaker = str(persona.get("tts_silero_speaker") or os.environ.get("YUNA_SILERO_SPEAKER", "baya"))
    try:
        from rvc_client import synth_silero

        got = synth_silero(text, out_wav, speaker=speaker)
        return bool(got and out_wav.exists() and out_wav.stat().st_size > 200)
    except Exception as e:
        log.warning("silero synth failed: %s", e)
        return False


def _apply_cleanup_filter(src: Path, persona: dict[str, object]) -> Path:
    """Мягкая чистка (срез эха/гула) для сырого нейро-TTS."""
    af = str(persona.get("tts_post_af") or "").strip()
    if not af or af in ("none", "anull"):
        return src
    out = src.with_name(f"{src.stem}_cl{src.suffix}")
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-af", af, str(out)],
            capture_output=True,
            timeout=60,
        )
        if proc.returncode == 0 and out.exists() and out.stat().st_size > 0:
            src.unlink(missing_ok=True)
            return out
    except Exception as e:
        log.debug("cleanup filter failed: %s", e)
    out.unlink(missing_ok=True)
    return src


def _synthesize_piper(text: str, out_wav: Path) -> bool:
    """Локальный TTS через Piper. Возвращает True при успехе."""
    model = _piper_model_path()
    if not model:
        return False
    length_scale = os.environ.get("YUNA_PIPER_LENGTH_SCALE", "1.0").strip() or "1.0"
    try:
        proc = subprocess.run(
            [
                sys.executable, "-m", "piper",
                "-m", str(model),
                "--length-scale", length_scale,
                "-f", str(out_wav),
            ],
            input=text.encode("utf-8"),
            capture_output=True,
            timeout=90,
        )
        if proc.returncode == 0 and out_wav.exists() and out_wav.stat().st_size > 200:
            return True
        log.warning("piper rc=%s err=%s", proc.returncode, proc.stderr.decode(errors="replace")[:300])
    except Exception as e:
        log.warning("piper synth failed: %s", e)
    return False


def _finalize_audio(
    audio_path: Path, persona: dict[str, object], ogg_path: Path
) -> Path | None:
    """Постобработка: чистка → pitch-shift → RVC → mp3/ogg. Общая для всех движков."""
    audio_path = _apply_cleanup_filter(audio_path, persona)
    audio_path = _apply_pitch_shift(audio_path, persona)
    if persona.get("tts_rvc"):
        try:
            from rvc_client import apply_rvc

            rvc_path = apply_rvc(
                audio_path,
                want_mp3=bool(persona.get("tts_direct_mp3", True)),
                pitch=persona.get("rvc_pitch"),
                index_rate=persona.get("rvc_index"),
                model=persona.get("rvc_model"),
            )
            if rvc_path and rvc_path.exists():
                audio_path = rvc_path
        except Exception as e:
            log.warning("RVC post-process failed: %s", e)
    if persona.get("tts_direct_mp3"):
        return audio_path
    if _to_ogg_opus(audio_path, ogg_path, persona=persona):
        return ogg_path
    return audio_path


def _to_ogg_opus(src: Path, dst: Path, *, persona: dict[str, object] | None = None) -> bool:
    af = _persona_ffmpeg_af(persona)
    cmd = ["ffmpeg", "-y", "-i", str(src), "-c:a", "libopus", "-b:a", "64k", "-vbr", "on", "-application", "audio"]
    if af:
        cmd[4:4] = ["-af", af]
    cmd.append(str(dst))
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
        return proc.returncode == 0 and dst.exists() and dst.stat().st_size > 0
    except Exception as e:
        log.warning("ffmpeg convert failed: %s", e)
        return False


async def synthesize_voice_async(
    text: str,
    *,
    persona_id: str = "yuna",
    tts_rate: str | None = None,
    natural: bool = False,
    persona_override: dict[str, object] | None = None,
) -> Path | None:
    text = (text or "").strip()
    if not text:
        return None

    persona = dict(get_persona(persona_id) or get_persona("yuna") or {})
    if persona_override:
        persona.update(persona_override)
    elif natural:
        persona.update(
            {
                "tts_rate": tts_rate or "+0%",
                "tts_pitch": "+0Hz",
                "tts_volume": "+0%",
                "tts_pitch_shift": 1.0,
                "tts_rubberband": False,
                "tts_audio_filter": "none",
            }
        )
    prosody = _resolve_tts_prosody(persona)
    if tts_rate and not natural and not persona_override:
        prosody["rate"] = tts_rate
    voices = [_resolve_tts_voice(text, persona)]
    if voices[0] != _RU_VOICE:
        voices.append(_RU_VOICE)

    token = uuid.uuid4().hex[:10]
    mp3_path = VOICE_CACHE / f"{token}.mp3"
    ogg_path = VOICE_CACHE / f"{token}.ogg"

    engine = _tts_engine(persona)

    # 1) Silero — живая русская интонация (тёплый воркер), основной путь.
    if engine == "silero":
        wav_path = VOICE_CACHE / f"{token}.wav"
        if await asyncio.to_thread(_synthesize_silero, text, wav_path, persona):
            result = await asyncio.to_thread(_finalize_audio, wav_path, persona, ogg_path)
            if result and result.exists():
                return result
        log.warning("silero TTS failed, fallback to piper/edge")
        engine = "piper" if _piper_model_path() else "edge"

    # 2) Локальный Piper (офлайн) — резерв.
    if engine == "piper":
        wav_path = VOICE_CACHE / f"{token}.wav"
        if await asyncio.to_thread(_synthesize_piper, text, wav_path):
            result = await asyncio.to_thread(_finalize_audio, wav_path, persona, ogg_path)
            if result and result.exists():
                return result
        log.warning("piper TTS failed, fallback to edge-tts")

    # 2) Резерв: edge-tts (сетевой).
    try:
        import edge_tts
    except ImportError:
        log.warning("edge-tts not installed and piper failed")
        return None

    last_err: Exception | None = None
    for voice in voices:
        try:
            comm = edge_tts.Communicate(
                text,
                voice,
                rate=prosody["rate"],
                pitch=prosody["pitch"],
                volume=prosody["volume"],
            )
            await comm.save(str(mp3_path))
            if not mp3_path.exists():
                continue
            result = await asyncio.to_thread(_finalize_audio, mp3_path, persona, ogg_path)
            if result and result.exists():
                return result
        except Exception as e:
            last_err = e
            log.debug("TTS retry voice=%s persona=%s: %s", voice, persona_id, e)
        finally:
            if not persona.get("tts_direct_mp3"):
                mp3_path.unlink(missing_ok=True)

    if last_err:
        log.warning("TTS failed persona=%s: %s", persona_id, last_err)
    return None


def synthesize_voice(text: str, *, persona_id: str = "yuna") -> Path | None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(synthesize_voice_async(text, persona_id=persona_id))
    return loop.run_until_complete(synthesize_voice_async(text, persona_id=persona_id))


async def transcribe_voice_file(path: Path) -> str:
    """Базовое распознавание входящих голосовых (Google STT через speech_recognition)."""
    converted: Path | None = None
    try:
        if path.suffix.lower() == ".wav":
            wav_path = path
        else:
            converted = path.with_suffix(".conv.wav")
            proc = subprocess.run(
                ["ffmpeg", "-y", "-i", str(path), "-ar", "16000", "-ac", "1", str(converted)],
                capture_output=True,
                timeout=90,
            )
            if proc.returncode != 0 or not converted.exists():
                return ""
            wav_path = converted

        import speech_recognition as sr

        recognizer = sr.Recognizer()
        with sr.AudioFile(str(wav_path)) as source:
            audio = recognizer.record(source)
        try:
            return recognizer.recognize_google(audio, language="ru-RU").strip()
        except Exception:
            try:
                return recognizer.recognize_google(audio, language="ja-JP").strip()
            except Exception:
                return ""
    except Exception as e:
        log.debug("transcribe failed: %s", e)
        return ""
    finally:
        if converted is not None:
            converted.unlink(missing_ok=True)
