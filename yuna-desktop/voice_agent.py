#!/usr/bin/env python3
"""Голосовой агент — тот же cursor + память + экран, что у Юны на сервере."""
from __future__ import annotations

import logging
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOSHI = ROOT.parent / "hoshi-core"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HOSHI))

from chat_memory import memory_block_for_prompt  # noqa: E402
from desk_config import (  # noqa: E402
    CURSOR_VOICE_MODEL,
    OLLAMA_FAST_MODEL,
    OLLAMA_MODEL,
    OWNER_ID,
    VOICE_LLM,
)
from screen_context import context_for_prompt, hypr_active_window  # noqa: E402
from unified_storage import append_agent_message, load_agent_session  # noqa: E402

log = logging.getLogger("yuna.voice_agent")

_TAG_RE = re.compile(r"\[\[(?:voice|audio):[^\]]+\]\]", re.I)
_SCREEN_HINTS = (
    "видишь", "экран", "скрин", "что пишет", "что напис", "написал", "написала",
    "лега", "lega", "telegram", "телеграм", "чат", "диалог", "монитор", "окно", "смотришь",
    "показыва", "на экране", "в телеге", "в тг", "кизу", "iris", "биржа",
)


def _bg_memory_pull() -> None:
    try:
        from memory_sync import pull_sessions_fast

        pull_sessions_fast(timeout=1)
    except Exception as e:
        log.debug("memory pull: %s", e)


def _speech_text(text: str) -> str:
    """Убирает [[voice:yuna]] и дубли вроде «текст. [[voice]]текст»."""
    t = (text or "").strip()
    m = re.search(r"\[\[voice:\w+\]\](.+)$", t, re.I | re.S)
    if m:
        t = m.group(1).strip()
    t = _TAG_RE.sub("", t).strip()
    t = re.sub(r"\*+|`+|#+", "", t).strip()
    sentences = [s.strip() for s in re.split(r"(?<=[.!?…])\s+", t) if s.strip()]
    if len(sentences) >= 2:
        a = re.sub(r"[^\wа-яё]", "", sentences[0].lower())
        b = re.sub(r"[^\wа-яё]", "", sentences[1].lower())
        if a and a == b:
            return sentences[0]
    return t


def _screen_age_sec(at: str) -> float | None:
    if not at:
        return None
    try:
        return (datetime.now() - datetime.fromisoformat(at)).total_seconds()
    except Exception:
        return None


def _latest_watched_screen(max_age: float = 28) -> str | None:
    from screen_context import load_context

    ls = (load_context().get("last_screen") or {})
    path = ls.get("path")
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    age = _screen_age_sec(str(ls.get("at") or ""))
    if age is not None and age > max_age:
        return None
    return str(p)


def _needs_screen(text: str, active_win: str) -> bool:
    low = (text or "").lower()
    if any(h in low for h in _SCREEN_HINTS):
        return True
    win = (active_win or "").lower()
    return "telegram" in win or "kotatogram" in win or "ayugram" in win


def _capture_for_voice(text: str) -> list[str]:
    win = hypr_active_window()
    want = _needs_screen(text, win)
    # живой кадр из фона — без нового grim на каждую фразу
    try:
        from live_vision import LIVE_FRAME, get_live_frame, get_live_see

        see = get_live_see(max_age_sec=55)
        if see:
            log.debug("voice live see: %s", see[:80])
        if want:
            shot = get_live_frame(max_age_sec=6.0)
            if shot and shot.exists():
                log.info("voice screen live %s", shot.name)
                return [str(shot)]
        if LIVE_FRAME.exists() and (want or see):
            return [str(LIVE_FRAME)]
    except Exception as e:
        log.debug("live vision: %s", e)

    if want:
        try:
            from vision import capture_screen

            shot = capture_screen()
            if shot and shot.exists():
                log.info("voice screen fresh %s", shot.name)
                return [str(shot)]
        except Exception as e:
            log.debug("screen capture: %s", e)

    recent = _latest_watched_screen()
    if recent:
        log.info("voice screen cached %s", Path(recent).name)
        return [recent]

    if want:
        return []
    return []


def _enrich_voice_task(text: str) -> tuple[str, dict]:
    """Тот же контекст чатов (Лега и др.), что у Юны на сервере."""
    try:
        from remote_brain import fetch_owner_enrich

        return fetch_owner_enrich(text)
    except Exception as e:
        log.debug("enrich: %s", e)
        return text, {}


def _build_voice_prompt(text: str, session: dict, images: list[str], task_extra: dict | None = None) -> str:
    from hoshi_daemon import build_agent_prompt  # noqa: E402

    ctx = context_for_prompt()
    win = hypr_active_window()
    tg_feed = ""
    try:
        from voice_tg_feed import feed_block_for_text

        tg_feed = feed_block_for_text(text)
    except Exception:
        pass
    body = text.strip()
    parts = [body]
    if tg_feed:
        parts.append(tg_feed)
    if ctx:
        parts.append(ctx)
    if win:
        parts.append(f"Активное окно: {win}")
    try:
        from game_memory import (
            companion_system_hint,
            game_memory_block_for_prompt,
            handle_failure_feedback,
            ingest_user_utterance,
            is_companion_active,
        )

        ingest_user_utterance(body)
        if handle_failure_feedback(body):
            parts.append(
                "Хозяин сказал, что прошлый совет не сработал — дай другой способ."
            )
        if is_companion_active(win):
            g = game_memory_block_for_prompt()
            if g:
                parts.append(g)
            parts.insert(0, companion_system_hint())
    except Exception:
        pass
    body = "\n\n---\n".join(parts)

    extra: dict = {
        "desktop": True,
        "voice": True,
        "origin": "yuna-voice",
        "desktop_memory": True,
    }
    if task_extra:
        extra.update(task_extra)
    if images:
        extra["screenshot_analysis"] = True

    base = build_agent_prompt(
        body,
        kind="agent_message",
        images=images or None,
        task_extra=extra,
        light=True,
    )
    memory = memory_block_for_prompt(session)
    voice_head = (
        "**Голосовой ответ хозяину.** Одно-два коротких предложения (до 20 слов). "
        "Только русский. Без markdown, без [[voice:]] и без повторов текста.\n\n"
    )
    if memory:
        return f"{voice_head}{memory}\n\n---\n\n{base}"
    return voice_head + base


def _cursor_chat(prompt: str) -> str:
    from config import CURSOR_LIGHT_MODE  # noqa: E402
    from hoshi_daemon import run_cursor  # noqa: E402

    model = CURSOR_VOICE_MODEL or "auto"
    mode = CURSOR_LIGHT_MODE if CURSOR_LIGHT_MODE in ("ask", "plan") else "ask"
    t0 = time.perf_counter()
    reply = run_cursor(
        prompt,
        model=model,
        mode=mode,
        owner="voice",
        on_partial=None,
    )
    reply = _speech_text(reply) or "…"
    log.info("cursor auto %.2fs: %s", time.perf_counter() - t0, reply[:80])
    return reply


def _wants_chat_write(text: str) -> bool:
    try:
        from chat_router import owner_wants_routed_chat_reply

        return bool(owner_wants_routed_chat_reply(text))
    except Exception:
        return False


def voice_chat(text: str, *, owner_id: int | None = None) -> str:
    uid = owner_id if owner_id is not None else OWNER_ID
    body = text.strip()
    if not body:
        return ""

    if _wants_chat_write(body):
        threading.Thread(target=_bg_memory_pull, daemon=True).start()
        images = _capture_for_voice(body)
        try:
            from server_task import submit_owner_task

            reply = submit_owner_task(body, images=images or None)
            append_agent_message("user", body, uid, origin="desktop", sync=False)
            append_agent_message("assistant", reply, uid, origin="desktop", sync=False)
            return reply or "Пишу."
        except Exception as e:
            log.warning("server task: %s", e)

    # «стоп» / «отключи» пока пилот играет или мышь у Юны — это стоп управления,
    # а не стоп музыки (раньше bare «стоп» перехватывал resolve_music_intent и она
    # отвечала «музыка и так не играет», хотя реально играла в Minecraft).
    try:
        from computer_control import is_enabled as _ctl_on, wants_stop_priority as _wants_ctl_stop

        try:
            from neuro_pilot import is_pilot_running as _pilot_on
        except Exception:
            def _pilot_on() -> bool:
                return False

        if (_ctl_on() or _pilot_on()) and _wants_ctl_stop(body):
            from computer_control import disable as _ctl_off

            try:
                from neuro_pilot import stop_pilot

                stop_pilot()
            except Exception:
                pass
            reply = _ctl_off()
            append_agent_message("user", body, uid, origin="desktop", sync=False)
            append_agent_message("assistant", reply, uid, origin="desktop", sync=False)
            return reply
    except Exception as e:
        log.debug("voice pilot stop gate: %s", e)

    # Управление ПК голосом
    try:
        from actions import resolve_music_intent, run_music_intent

        intent = resolve_music_intent(body)
        if intent:
            reply = run_music_intent(intent, open_browser=False)
            append_agent_message("user", body, uid, origin="desktop", sync=False)
            append_agent_message("assistant", reply, uid, origin="desktop", sync=False)
            return _speech_text(reply) or reply
    except Exception as e:
        log.debug("voice music: %s", e)

    # Управление ПК голосом
    try:
        from computer_control import (
            disable as control_disable,
            enable as control_enable,
            is_enabled as control_enabled,
            play_session,
            wants_play,
            wants_stop,
            wants_takeover,
        )

        if wants_stop(body):
            try:
                from neuro_pilot import stop_pilot

                stop_pilot()
            except Exception:
                pass
            reply = control_disable()
            append_agent_message("user", body, uid, origin="desktop", sync=False)
            append_agent_message("assistant", reply, uid, origin="desktop", sync=False)
            return reply
        if wants_play(body) or wants_takeover(body):
            if not control_enabled():
                control_enable(reason=body[:120], goal=body[:200])
            try:
                from game_memory import enable_companion

                enable_companion("Minecraft")
            except Exception:
                pass
            try:
                from neuro_pilot import start_pilot

                reply = start_pilot(body[:200])
            except Exception as e:
                reply = play_session(goal=body[:200], steps=4)
                reply = f"Пилот: {e}. {reply}"
            append_agent_message("user", body, uid, origin="desktop", sync=False)
            append_agent_message("assistant", reply, uid, origin="desktop", sync=False)
            return _speech_text(reply) or reply
    except Exception as e:
        log.debug("voice control: %s", e)

    threading.Thread(target=_bg_memory_pull, daemon=True).start()
    session = load_agent_session(uid)

    enrich_box: list = [body, {}]

    def _run_enrich() -> None:
        enrich_box[0], enrich_box[1] = _enrich_voice_task(body)

    enrich_thr = threading.Thread(target=_run_enrich, daemon=True)
    enrich_thr.start()
    images = _capture_for_voice(body)
    enrich_thr.join(timeout=7)
    enriched, tg_extra = enrich_box[0], enrich_box[1]
    if not images:
        images = _capture_for_voice(enriched)
    if images and not tg_extra.get("screenshot_analysis"):
        tg_extra["screenshot_analysis"] = True
    prompt = _build_voice_prompt(enriched, session, images, tg_extra)
    append_agent_message("user", body, uid, origin="desktop", sync=False)

    reply = ""
    if VOICE_LLM in ("ollama", "auto"):
        try:
            reply = _ollama_chat(prompt, images=images or None)
        except Exception as e:
            log.warning("ollama voice: %s", e)
            if VOICE_LLM == "ollama":
                reply = f"Ollama: {str(e)[:80]}"

    if (not reply or reply.startswith("Ollama:")) and VOICE_LLM in ("cursor", "auto"):
        try:
            reply = _cursor_chat(prompt)
        except Exception as e:
            log.warning("cursor voice: %s", e)
            err = str(e)
            if "ActionRequiredError" in err or "out of usage" in err.lower():
                reply = reply or "Лимит Cursor — нужен локальный Ollama."
            elif "Authentication" in err or "login" in err.lower():
                reply = reply or "Нужен локальный мозг Ollama."
            else:
                reply = reply or f"Ошибка: {err[:80]}"

    if not reply:
        reply = "Слушаю."

    append_agent_message("assistant", reply, uid, origin="desktop", sync=False)
    return reply


def _ollama_chat(prompt: str, *, images: list[str] | None = None) -> str:
    from ollama_brain import quick_reply, run_agent

    # Голос: если просит починить код — полный agent; иначе быстрый ответ
    low = prompt.lower()
    wants_fix = any(
        w in low
        for w in ("исправ", "почини", "правь", "код", "файл", "баг", "сломал")
    )
    t0 = time.perf_counter()
    if wants_fix:
        reply = run_agent(
            prompt,
            model=OLLAMA_MODEL,
            images=images,
            max_steps=10,
            voice_short=True,
        )
    else:
        reply = quick_reply(prompt, model=OLLAMA_FAST_MODEL or OLLAMA_MODEL, images=images)
    reply = _speech_text(reply) or "…"
    log.info("ollama voice %.2fs: %s", time.perf_counter() - t0, reply[:80])
    try:
        from game_memory import is_companion_active, record_action

        if is_companion_active():
            record_action(reply, role="yuna")
    except Exception:
        pass
    return reply


def warm_brain() -> None:
    if VOICE_LLM in ("ollama", "auto"):
        try:
            from ollama_brain import ollama_ready

            if ollama_ready():
                log.info("ollama ready model=%s", OLLAMA_MODEL)
            else:
                log.warning("ollama не отвечает — запусти scripts/start_ollama.sh")
        except Exception as e:
            log.debug("ollama warm: %s", e)
    if VOICE_LLM in ("cursor", "auto"):
        try:
            from hoshi_daemon import verify_cursor_auth

            if verify_cursor_auth():
                log.info("cursor ready (model=%s)", CURSOR_VOICE_MODEL or "auto")
            else:
                log.debug("cursor-agent: optional")
        except Exception as e:
            log.debug("cursor warm: %s", e)


def keep_brain_hot() -> None:
    warm_brain()
