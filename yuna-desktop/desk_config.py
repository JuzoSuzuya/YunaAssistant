#!/usr/bin/env python3
"""Конфигурация локального слоя Yuna Desktop."""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*_a, **_k):  # type: ignore[misc]
        return False

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
HOSHI_CORE = PROJECT / "hoshi-core"
DATA = HOSHI_CORE / "data"
LOCAL_INBOX = DATA / "local_inbox"
LOCAL_OUTBOX = DATA / "local_outbox"
LOCAL_CONTEXT = DATA / "local_context.json"
VOICE_STATUS = ROOT / "voice_status.json"
BRIDGE_STATUS = ROOT / "bridge_status.json"
YUNA_STATUS = ROOT / "yuna_status.json"
MEMORY_SYNC_STATE = ROOT / "memory_sync_state.json"
VOICE_SOCKET = Path(os.environ.get("YUNA_VOICE_SOCKET", "/tmp/yuna-voice.sock"))

load_dotenv(PROJECT / ".env")

SERVER_HOST = os.environ.get("YUNA_SERVER_HOST", "151.247.197.252").strip()
SERVER_USER = os.environ.get("YUNA_SERVER_USER", "root").strip()
SERVER_HOSHI_PATH = os.environ.get("YUNA_SERVER_HOSHI_PATH", "/root/projects/Hoshi").strip()
SERVER_SSH_PASS = os.environ.get("YUNA_SERVER_SSH_PASS", "").strip()

BRIDGE_HOST = os.environ.get("YUNA_BRIDGE_HOST", "127.0.0.1").strip()
BRIDGE_PORT = int(os.environ.get("YUNA_BRIDGE_PORT", "8765"))
MEMORY_SYNC_INTERVAL = float(os.environ.get("YUNA_MEMORY_SYNC_INTERVAL", "8"))
DEFAULT_PERSONA = os.environ.get("YUNA_DEFAULT_PERSONA", "yuna").strip()
VOICE_AUTOSPEAK = os.environ.get("YUNA_VOICE_AUTOSPEAK", "true").lower() in ("1", "true", "yes")
MIC_TARGET = os.environ.get("YUNA_MIC_TARGET", "").strip()
VOICE_TTS_RATE = os.environ.get("YUNA_VOICE_TTS_RATE", "-4%").strip()
VOICE_TTS_NATURAL = os.environ.get("YUNA_VOICE_TTS_NATURAL", "false").lower() in ("1", "true", "yes")
VOICE_TTS_VOICE = os.environ.get("YUNA_VOICE_TTS_VOICE", "ru-RU-DariyaNeural").strip()
VOICE_TTS_PITCH = os.environ.get("YUNA_VOICE_TTS_PITCH", "+4Hz").strip()
VOICE_TTS_PITCH_SHIFT = float(os.environ.get("YUNA_VOICE_TTS_PITCH_SHIFT", "1.0"))
RVC_ENABLED = os.environ.get("YUNA_VOICE_RVC", "true").lower() in ("1", "true", "yes")
RVC_SOCKET = Path(os.environ.get("YUNA_RVC_SOCKET", "/tmp/yuna-rvc.sock"))
RVC_MODEL = os.environ.get("YUNA_RVC_MODEL", "yuna").strip().lower()
RVC_PITCH = int(os.environ.get("YUNA_RVC_PITCH", "6"))
RVC_INDEX_RATE = float(os.environ.get("YUNA_RVC_INDEX_RATE", "0.66"))
RVC_TIMEOUT = float(os.environ.get("YUNA_RVC_TIMEOUT", "45"))
SCREEN_WATCH_INTERVAL = float(os.environ.get("YUNA_SCREEN_INTERVAL", "8"))
# Мозг панели/демона: ollama (бесплатно) | cursor | auto (cursor→ollama)
BRAIN = os.environ.get("YUNA_BRAIN", "ollama").strip().lower()
VOICE_LLM = os.environ.get("YUNA_VOICE_LLM", "ollama").strip().lower()
CURSOR_VOICE_MODEL = os.environ.get("YUNA_CURSOR_VOICE_MODEL", "auto").strip()
OLLAMA_URL = os.environ.get("YUNA_OLLAMA_URL", "http://127.0.0.1:11435").strip()
OLLAMA_MODEL = os.environ.get(
    "YUNA_OLLAMA_MODEL", "llama3.1:8b-instruct-q4_K_M"
).strip()
OLLAMA_FAST_MODEL = os.environ.get(
    "YUNA_OLLAMA_FAST_MODEL", "llama3.1:8b-instruct-q4_K_M"
).strip()
OLLAMA_VISION_MODEL = os.environ.get(
    "YUNA_OLLAMA_VISION_MODEL", "llama3.1:8b-instruct-q4_K_M"
).strip()
OLLAMA_HEAVY_MODEL = os.environ.get(
    "YUNA_OLLAMA_HEAVY_MODEL", OLLAMA_MODEL
).strip()

# Дефолты голоса (выбор хозяина: Silero Baya, без RVC, с лёгкой чисткой эха).
VOICE_DEFAULTS = {
    "engine": os.environ.get("YUNA_TTS_ENGINE", "silero").strip().lower(),
    "silero_speaker": os.environ.get("YUNA_SILERO_SPEAKER", "baya").strip(),
    "use_rvc": os.environ.get("YUNA_VOICE_USE_RVC", "false").lower() in ("1", "true", "yes"),
    "rvc_model": RVC_MODEL,
    "rvc_pitch": RVC_PITCH,
    "rvc_index": RVC_INDEX_RATE,
    "pitch_shift": VOICE_TTS_PITCH_SHIFT,
    "length_scale": float(os.environ.get("YUNA_PIPER_LENGTH_SCALE", "0.95")),
    "cleanup": os.environ.get(
        "YUNA_VOICE_CLEANUP",
        "highpass=f=85,lowpass=f=12000,acompressor=threshold=-16dB:ratio=2.5:attack=6:release=90",
    ),
}


def load_voice_config() -> dict[str, object]:
    """Голосовые настройки из settings.json (desktop.voice) поверх дефолтов."""
    cfg = dict(VOICE_DEFAULTS)
    try:
        from storage import load_settings

        saved = ((load_settings().get("desktop") or {}).get("voice") or {})
        for k, v in saved.items():
            if v is not None:
                cfg[k] = v
    except Exception:
        pass
    return cfg


def desktop_voice_persona() -> dict[str, object]:
    """Персона голоса Юны, собранная из настроек (движок, диктор, RVC, чистка)."""
    cfg = load_voice_config()
    use_rvc = bool(cfg.get("use_rvc"))
    return {
        "tts_engine": str(cfg.get("engine", "silero")),
        "tts_silero_speaker": str(cfg.get("silero_speaker", "baya")),
        "tts_voice": VOICE_TTS_VOICE,
        "tts_rate": VOICE_TTS_RATE,
        "tts_pitch": VOICE_TTS_PITCH,
        "tts_volume": "+0%",
        "tts_pitch_shift": float(cfg.get("pitch_shift", 1.0)),
        "tts_rubberband": False,
        "tts_audio_filter": "none",
        "tts_post_af": str(cfg.get("cleanup") or ""),
        "tts_direct_mp3": True,
        "tts_rvc": use_rvc,
        "rvc_model": str(cfg.get("rvc_model", RVC_MODEL)),
        "rvc_pitch": int(cfg.get("rvc_pitch", RVC_PITCH)),
        "rvc_index": float(cfg.get("rvc_index", RVC_INDEX_RATE)),
    }


OWNER_ID = int(os.environ.get("HOSHI_OWNER_ID", "1477417142"))

# Файлы единой памяти (синхронизируются с сервером)
MEMORY_FILES = (
    "data/agent_sessions",
    "data/settings.json",
    "data/saved_ideas.json",
    "data/local_context.json",
    "data/voice_tg_feed.json",
    "data/game_memory.json",
)

for d in (LOCAL_INBOX, LOCAL_OUTBOX):
    d.mkdir(parents=True, exist_ok=True)
