#!/usr/bin/env python3
"""
Голосовой демон: «Юна» + фраза → ответ за секунды (Jarvis-mode).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOSHI = ROOT.parent / "hoshi-core"
sys.path.insert(0, str(HOSHI))
sys.path.insert(0, str(ROOT))

os.environ.setdefault("VOSK_LOG_LEVEL", "-1")

from desk_config import (  # noqa: E402
    DEFAULT_PERSONA,
    VOICE_LLM,
    VOICE_SOCKET,
    VOICE_STATUS,
    VOICE_TTS_NATURAL,
    VOICE_TTS_RATE,
    desktop_voice_persona,
)
from fast_reply import is_wake_only, try_fast_reply  # noqa: E402
from voice_agent import _speech_text, keep_brain_hot, voice_chat, warm_brain  # noqa: E402
from voice_listen import (  # noqa: E402
    cleanup_stale_recorders,
    record_sync,
    wait_for_wake_then_phrase,
)
from wake_word import (  # noqa: E402
    _get_whisper,
    command_after_yuna,
    starts_with_yuna,
    transcribe_vosk,
)
from yuna_status import set_component  # noqa: E402

log = logging.getLogger("yuna.voice")
_running = True


def _stop(*_args: object) -> None:
    global _running
    _running = False


def _save_status(**kw: object) -> None:
    VOICE_STATUS.write_text(json.dumps({"running": True, **kw}, ensure_ascii=False, indent=2), encoding="utf-8")


async def _speak(text: str, persona: str) -> dict:
    from voice_tts import synthesize_voice_async

    text = _speech_text((text or "").strip())
    if not text:
        return {"ok": False, "error": "пустой текст"}
    set_component("voice", "speaking", text[:60])
    path = await synthesize_voice_async(
        text,
        persona_id=persona or DEFAULT_PERSONA,
        tts_rate=VOICE_TTS_RATE or None,
        natural=VOICE_TTS_NATURAL,
        persona_override=None if VOICE_TTS_NATURAL else desktop_voice_persona(),
    )
    if not path or not path.exists():
        set_component("voice", "idle")
        return {"ok": False, "error": "tts failed"}
    for player in (
        ["mpv", "--really-quiet", "--no-video", "--gapless-audio=fully", str(path)],
        ["pw-play", str(path)],
    ):
        try:
            proc = await asyncio.create_subprocess_exec(*player)
            await proc.wait()
            if proc.returncode == 0:
                set_component("voice", "idle")
                return {"ok": True, "path": str(path)}
        except FileNotFoundError:
            continue
    set_component("voice", "idle")
    return {"ok": True, "path": str(path)}


async def _process_command(command: str, full_phrase: str, persona: str) -> dict:
    command = command.strip()
    full_phrase = full_phrase.strip()
    t0 = time.perf_counter()

    fast = try_fast_reply(full_phrase)
    if fast:
        await _speak(fast, persona)
        return {"ok": True, "heard": full_phrase, "reply": fast, "fast": True}

    # Музыка/обои — сразу действием, не через «болтовню» LLM
    try:
        from fast_reply import try_action_reply

        acted = try_action_reply(full_phrase) or try_action_reply(command)
    except Exception:
        acted = None
    if acted:
        await _speak(acted, persona)
        return {"ok": True, "heard": full_phrase, "reply": acted, "fast": True, "action": True}

    if not command:
        await _speak("Слушаю.", persona)
        return {"ok": True, "heard": full_phrase, "reply": "Слушаю.", "fast": True}

    set_component("agent", "thinking", command[:50])
    try:
        reply = await asyncio.to_thread(voice_chat, command)
    except Exception as e:
        log.warning("voice agent: %s", e)
        return {"ok": False, "error": str(e), "heard": full_phrase}
    finally:
        set_component("agent", "idle")

    reply = (reply or "").strip()
    if not reply:
        return {"ok": False, "error": "пустой ответ", "heard": full_phrase}

    await _speak(reply, persona)
    log.info("total reply %.2fs", time.perf_counter() - t0)
    return {"ok": True, "heard": full_phrase, "reply": reply, "agent": True}


async def _wake_loop(persona: str) -> None:
    log.info("wake: invoke-only «Юна» → phrase")
    await asyncio.sleep(0.3)
    while _running:
        wav: Path | None = None
        try:
            set_component("voice", "idle", "жду «Юна»")
            wav = await asyncio.to_thread(wait_for_wake_then_phrase)
            set_component("voice", "listening", "слышу…")

            full = await asyncio.to_thread(transcribe_vosk, wav)
            if not full or not starts_with_yuna(full):
                log.debug("skip (no invoke): %s", (full or "")[:60])
                continue

            command = command_after_yuna(full)
            log.info("phrase: %s", full[:120])

            if is_wake_only(full):
                await _speak("Да?", persona)
            else:
                await _process_command(command, full, persona)

            set_component("voice", "idle", "жду «Юна»")
        except asyncio.CancelledError:
            break
        except RuntimeError as e:
            if "no speech" not in str(e) and "microphone" not in str(e):
                log.debug("record: %s", e)
            continue
        except Exception as e:
            log.warning("wake loop: %s", e)
            if "microphone capture failed" in str(e):
                await asyncio.to_thread(cleanup_stale_recorders)
            set_component("voice", "idle", "жду «Юна»")
            await asyncio.sleep(0.2)
        finally:
            if wav and wav.exists():
                wav.unlink(missing_ok=True)


async def _brain_keepalive() -> None:
    while _running:
        await asyncio.sleep(240)
        await asyncio.to_thread(keep_brain_hot)


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        raw = await asyncio.wait_for(reader.read(65536), timeout=30)
        req = json.loads(raw.decode("utf-8") or "{}")
        action = req.get("action", "speak")
        persona = str(req.get("persona") or DEFAULT_PERSONA)
        if action == "ping":
            resp = {"ok": True, "pong": True}
        elif action == "speak":
            resp = await _speak(str(req.get("text") or ""), persona)
        elif action == "listen":
            set_component("voice", "listening")
            wav = await asyncio.to_thread(record_sync, float(req.get("seconds") or 3))
            try:
                from wake_word import transcribe_vosk

                heard = await asyncio.to_thread(transcribe_vosk, wav)
            finally:
                wav.unlink(missing_ok=True)
            set_component("voice", "idle")
            resp = {"ok": bool(heard), "heard": heard}
        else:
            resp = {"ok": False, "error": f"unknown: {action}"}
    except Exception as e:
        resp = {"ok": False, "error": str(e)}
    writer.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def _serve() -> None:
    from desk_config import VOICE_LLM

    await asyncio.to_thread(cleanup_stale_recorders)
    if VOICE_SOCKET.exists():
        VOICE_SOCKET.unlink()
    server = await asyncio.start_unix_server(_handle, path=str(VOICE_SOCKET))
    _save_status(socket=str(VOICE_SOCKET), wake_word=f"yuna-{VOICE_LLM}")
    log.info("voice on %s (llm=%s)", VOICE_SOCKET, VOICE_LLM)
    asyncio.create_task(asyncio.to_thread(warm_brain))
    from voice_listen import _get_vosk_model

    asyncio.create_task(asyncio.to_thread(_get_vosk_model))

    if VOICE_LLM in ("ollama", "auto"):
        from wake_word import _get_whisper

        asyncio.create_task(asyncio.to_thread(_get_whisper))
    asyncio.create_task(_brain_keepalive())
    wake = asyncio.create_task(_wake_loop(DEFAULT_PERSONA))
    try:
        async with server:
            await server.serve_forever()
    finally:
        wake.cancel()
        try:
            await wake
        except asyncio.CancelledError:
            pass


def main() -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass
    finally:
        VOICE_SOCKET.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
