#!/usr/bin/env python3
"""Прямой ответ для десктопа: Ollama (бесплатно) или Cursor."""
from __future__ import annotations

import logging
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOSHI = ROOT.parent / "hoshi-core"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HOSHI))

from chat_memory import memory_block_for_prompt  # noqa: E402
from desk_config import (  # noqa: E402
    BRAIN,
    OLLAMA_FAST_MODEL,
    OLLAMA_HEAVY_MODEL,
    OLLAMA_MODEL,
    OWNER_ID,
)
from screen_context import context_for_prompt, hypr_active_window  # noqa: E402
from unified_storage import append_agent_message, load_agent_session  # noqa: E402

log = logging.getLogger("yuna.agent")

_ASK_MODE_REFUSAL = re.compile(
    r"(?:ask\s*mode|agent\s*mode|переключи(?:сь|те)?\s+на\s+\*?\*?agent|"
    r"в\s+код\s+лезть\s+нельзя|сама\s+режим\s+не\s+переключаю)",
    re.I,
)
_CURSOR_LIMIT = re.compile(
    r"usage\s*limit|out of usage|ActionRequiredError|hit your usage|Get Cursor Pro",
    re.I,
)

_DESKTOP_TOOLS = f"""
**ПК (панель Юны):** понимай запрос и действуй. Обои/музыка через Shell:

```bash
cd "{ROOT}" && ../.venv/bin/python - <<'PY'
from actions import set_wallpaper, list_monitors, play_music
print(set_wallpaper("anime stars", dual_different=True))
print(set_wallpaper("anime stars", monitor_index=1))  # второй монитор
PY
```
Pinterest уже внутри set_wallpaper. Короткий отчёт хозяину.
"""


def _is_ask_mode_refusal(reply: str) -> bool:
    return bool(_ASK_MODE_REFUSAL.search(reply or ""))


def _is_cursor_limit(err: str) -> bool:
    return bool(_CURSOR_LIMIT.search(err or ""))


def _action_fallback(text: str) -> str | None:
    try:
        from actions import handle_action

        return handle_action(text.strip())
    except Exception as e:
        log.warning("action fallback failed: %s", e)
        return None


def _ollama_chat(
    prompt: str,
    *,
    is_code_fix: bool,
    images: list[str] | None = None,
    user_text: str = "",
    companion: bool = False,
    control_mode: bool = False,
    memory: str = "",
) -> str:
    from ollama_brain import chat_once, quick_reply, run_agent

    # Обычный чат → быстрая 8B; тяжёлое (код/управление) → 14B
    model = OLLAMA_HEAVY_MODEL if (is_code_fix or control_mode) else (OLLAMA_FAST_MODEL or OLLAMA_MODEL)
    if is_code_fix or control_mode:
        extra = _DESKTOP_TOOLS
        if control_mode:
            extra += (
                "\nХозяин дал управление ПК. Используй tools: control_enable, play_step, "
                "mouse_move, click, hold, key. В конце control не выключай, пока не скажет стоп.\n"
            )
        packed = prompt if not user_text else f"{user_text}\n\n---\n{prompt[:3000]}"
        if memory:
            packed = f"{memory[:4500]}\n\n---\n{packed}"
        return run_agent(
            packed,
            system_extra=extra,
            model=OLLAMA_HEAVY_MODEL or OLLAMA_MODEL,
            images=images,
            max_steps=16 if control_mode else 14,
        )

    # Обычный чат в панели — быстрый ответ без tool-loop (иначе подвисает).
    body = (user_text or prompt).strip()
    # Не режем по --- : там факты с ПК (трек, окно) для ответа мозга
    head = body.strip()[:3500]
    has_pc_fact = "Факт с ПК" in head
    playing = has_pc_fact and ("Сейчас играет:" in head or "На паузе:" in head)
    sys_msg = (
        "Ты Юна — подруга и ассистент на ПК хозяина. Отвечай по-русски, живо, "
        "коротко (1–3 предложения), без markdown-стен. "
        "Не выдумывай названия песен/артистов от себя. "
        "Никогда не выключай ПК и не пиши «отключаю систему/ПК» — "
        "«отключи» значит музыку или управление мышью. "
        "Если в контексте есть память диалога — опирайся на неё: не путай музыку с обоями, "
        "помни что сейчас делали."
    )
    if has_pc_fact:
        if playing:
            sys_msg += (
                " В сообщении есть «Факт с ПК» с реальным треком. "
                "В первом предложении назови артиста и название из факта. "
                "Потом мнение, если спросили. Не пиши «не вижу» / «не слышу» — трек уже дан."
            )
        else:
            sys_msg += (
                " В сообщении есть «Факт с ПК». Опирайся только на него. "
                "Если там сказано что метаданных нет — честно скажи что не знаешь название, "
                "не придумывай ReoNa/Morbius/WoW и т.п."
            )
    temp = 0.35 if playing else (0.45 if has_pc_fact else 0.55)
    if companion:
        try:
            from game_memory import companion_system_hint

            sys_msg = companion_system_hint() + "\n" + sys_msg
        except Exception:
            sys_msg = "Ты напарник в игре. Помни цели и уроки.\n" + sys_msg
    # Память обязательно в user-контент (раньше терялась — казалось что «забывает»)
    mem = (memory or "").strip()
    if mem:
        user_content = f"{mem[:5000]}\n\n---\nСейчас хозяин сказал:\n{head}"
    else:
        user_content = head
    try:
        return chat_once(
            [{"role": "system", "content": sys_msg}, {"role": "user", "content": user_content}],
            model=model,
            images=images,
            temperature=temp,
            num_predict=160 if not companion else 220,
            timeout=45,
        )
    except Exception as e:
        log.warning("ollama chat fail (%s): %s — fallback", model, e)
        try:
            return quick_reply(head, model=OLLAMA_FAST_MODEL or model)
        except Exception as e2:
            return f"Сейчас мозг тупит ({e2}). Подожди секунду и напиши ещё раз."


def _cursor_chat(prompt: str, *, is_code_fix: bool) -> str:
    from config import CURSOR_HEAVY_MODEL, CURSOR_LIGHT_MODEL  # noqa: E402
    from hoshi_daemon import run_cursor  # noqa: E402

    model = (CURSOR_HEAVY_MODEL or CURSOR_LIGHT_MODEL or None) if is_code_fix else (CURSOR_LIGHT_MODEL or None)
    reply = run_cursor(prompt, model=model, mode=None, owner="desktop")
    reply = (reply or "").strip() or "…"
    if _is_ask_mode_refusal(reply):
        log.info("ask-mode refusal on desktop — retry agent")
        retry_prompt = f"{prompt}\n\n**Не пиши про Ask/Agent mode.** Сразу действуй и отчитайся."
        reply = run_cursor(
            retry_prompt,
            model=CURSOR_HEAVY_MODEL or CURSOR_LIGHT_MODEL or None,
            mode=None,
            owner="desktop",
        )
        reply = (reply or "").strip() or "…"
    return reply


def chat(text: str, *, owner_id: int | None = None) -> str:
    uid = owner_id if owner_id is not None else OWNER_ID
    body = text.strip()
    if not body:
        return ""

    from chat_router import owner_wants_routed_chat_reply

    head = body.split("\n---\n")[0]
    if owner_wants_routed_chat_reply(head) or owner_wants_routed_chat_reply(body):
        from server_task import submit_owner_task

        append_agent_message("user", body, uid, origin="desktop")
        confirm = submit_owner_task(body)
        if confirm:
            append_agent_message("assistant", confirm, uid, origin="desktop")
        return confirm or "Отправила."

    # «стоп» / «отключи» когда пилот/управление активны — ВСЕГДА значит «отдай мышь»,
    # а не «выключи музыку» (раньше голое «стоп» уходило в smart_off и она отвечала
    # «музыка и так не играет», хотя играла в Minecraft).
    try:
        from computer_control import is_enabled as _ctl_on, wants_stop_priority as _wants_ctl_stop

        try:
            from neuro_pilot import is_pilot_running as _pilot_on
        except Exception:
            def _pilot_on() -> bool:
                return False

        if (_ctl_on() or _pilot_on()) and _wants_ctl_stop(head):
            from computer_control import disable as _ctl_off

            try:
                from neuro_pilot import stop_pilot

                stop_pilot()
            except Exception:
                pass
            msg = _ctl_off()
            append_agent_message("user", body, uid, origin="desktop")
            append_agent_message("assistant", msg, uid, origin="desktop")
            return msg
    except Exception:
        pass

    # «отключи» → музыка, никогда «выключаю ПК»
    try:
        from actions import wants_bare_off, smart_off

        if wants_bare_off(head):
            msg = smart_off(head)
            append_agent_message("user", body, uid, origin="desktop")
            append_agent_message("assistant", msg, uid, origin="desktop")
            return msg
    except Exception:
        pass

    # Управление мышью — ДО music-fallback (иначе «управляй + музыка» = только lofi)
    control_mode = False
    try:
        from computer_control import (
            disable as control_disable,
            enable as control_enable,
            focus_first_window,
            goto_workspace,
            is_enabled as control_enabled,
            parse_workspace,
            play_step,
            prove_control,
            set_goal,
            status_block,
            wants_desktop_app_task,
            wants_focus_first_window,
            wants_play,
            wants_stop,
            wants_takeover,
            youtube_browse_with_mouse,
        )

        if wants_stop(head):
            try:
                from neuro_pilot import stop_pilot

                stop_pilot()
            except Exception:
                pass
            msg = control_disable()
            append_agent_message("user", body, uid, origin="desktop")
            append_agent_message("assistant", msg, uid, origin="desktop")
            return msg

        if wants_takeover(head) or control_enabled() or wants_play(head):
            control_mode = True
            parts: list[str] = []
            if wants_takeover(head) or (wants_play(head) and not control_enabled()):
                parts.append(control_enable(reason=head[:120], goal=head[:200]))

            low = head.lower()
            desktop_task = wants_desktop_app_task(head)
            ws = parse_workspace(head)
            if ws:
                try:
                    parts.append(goto_workspace(ws))
                except Exception as e:
                    parts.append(f"Workspace: {e}")

            # Музыка — звук через mpv. Не плодить вкладки YouTube.
            # Конкретный запрос («поставь X») важнее «своего вкуса».
            intent = None
            try:
                from actions import resolve_music_intent, run_music_intent

                intent = resolve_music_intent(head)
            except Exception:
                intent = None
            if intent:
                try:
                    want_tab = bool(re.search(r"открой|открыть|покажи", low)) and bool(
                        re.search(r"ютуб|youtube|ютюб", low)
                    )
                    parts.append(run_music_intent(intent, open_browser=want_tab))
                except Exception as e:
                    parts.append(f"Музыка: {e}")
            elif re.search(r"^\s*(?:открой|открыть)\s+(?:ютуб\w*|youtube|ютюб\w+)\s*$", low):
                try:
                    from actions import open_url

                    parts.append(open_url("https://www.youtube.com"))
                except Exception as e:
                    parts.append(f"YouTube: {e}")
            elif wants_focus_first_window(head):
                try:
                    parts.append(focus_first_window())
                except Exception as e:
                    parts.append(f"Окно: {e}")

            # Автопилот игры — «начинай играть» тоже, не только «играй»
            from computer_control import wants_play

            gamey = wants_play(head) or bool(
                re.search(r"играй|minecraft|майн|поиграй|сделай\s+ход|беги", low)
            )
            did_music = bool(intent)
            desktop_task = wants_desktop_app_task(head) or did_music or bool(
                re.search(r"ютуб|youtube|ютюб", low)
            )
            do_play = (not desktop_task) and (
                gamey
                or any(w in low for w in ("продолж", "дальше", "ещё шаг"))
                or (wants_takeover(head) and not parts and not ws)
            )
            if desktop_task or (parts and not gamey):
                do_play = False

            # Пустой takeover без задачи — хотя бы показать мышь
            if wants_takeover(head) and not do_play and not parts:
                try:
                    parts.append(prove_control())
                except Exception as e:
                    parts.append(f"Мышь не двигается: {e}")

            if do_play:
                append_agent_message("user", body, uid, origin="desktop")
                try:
                    from game_memory import enable_companion, ingest_user_utterance

                    ingest_user_utterance(head)
                    enable_companion("Minecraft")
                except Exception:
                    pass
                if not control_enabled():
                    parts.append(control_enable(reason=head[:120], goal=head[:200]))
                set_goal(head[:200])
                # Neurosama-style: фоновый непрерывный пилот, НЕ блокирующая сессия
                try:
                    from neuro_pilot import start_pilot

                    result = start_pilot(head[:200])
                except Exception as e:
                    from computer_control import play_session

                    result = play_session(goal=head[:200], steps=4)
                    result = f"Пилот не стартовал ({e}), короткая сессия:\n{result}"
                parts.append(f"{status_block()}\n\n{result}")
                reply = "\n".join(p for p in parts if p)
                append_agent_message("assistant", reply, uid, origin="desktop")
                return reply

            if parts:
                reply = "\n".join(p for p in parts if p)
                append_agent_message("user", body, uid, origin="desktop")
                append_agent_message("assistant", reply, uid, origin="desktop")
                return reply
    except Exception as e:
        log.warning("control gate: %s", e)

    # ПК-действия без LLM (чисто музыка/обои)
    direct = _action_fallback(head)
    if direct:
        append_agent_message("user", body, uid, origin="desktop")
        append_agent_message("assistant", direct, uid, origin="desktop")
        return direct

    # Факты для мозга (не готовый ответ): что играет и т.п.
    listen_fact = ""
    try:
        from actions import _NOW_PLAYING_RE, now_playing

        if _NOW_PLAYING_RE.search(head):
            listen_fact = now_playing()
            body = (
                f"{body}\n\n---\nФакт с ПК: {listen_fact}\n"
                "Ответь как Юна: если спросили название — назови трек из факта; "
                "если «как тебе» — название + короткое мнение. Не пиши «не вижу», если трек в факте есть."
            )
    except Exception:
        pass

    images: list[str] = []
    win = hypr_active_window()
    companion = False
    try:
        from game_memory import (
            game_memory_block_for_prompt,
            handle_failure_feedback,
            ingest_user_utterance,
            is_companion_active,
            record_action,
        )

        ingest_user_utterance(head)
        companion = is_companion_active(win)
        if handle_failure_feedback(head):
            body = (
                f"{body}\n\n---\nХозяин сказал, что прошлый совет не сработал. "
                "Дай ДРУГОЙ способ, учитывая уроки из памяти игры."
            )
            companion = True
    except Exception as e:
        log.debug("game memory: %s", e)

    try:
        from live_vision import LIVE_FRAME, get_live_frame, get_live_see

        low = head.lower()
        want_screen = any(x in low for x in ("экран", "скрин", "видишь", "монитор", "окно"))
        if companion and any(x in low for x in ("что у меня", "видишь", "смотри", "где я", "инвентарь")):
            want_screen = True
        # Всегда подмешиваем живое зрение из фона (без 2–3 мин VLM)
        live = get_live_see(max_age_sec=55)
        if live:
            body = f"{body}\n\n---\nГлаза (live): {live}"
        if want_screen:
            # свежий кадр в промпт, описание уже из кэша — без долгого describe_screen
            shot = get_live_frame(max_age_sec=6.0)
            if shot and shot.exists():
                images = [str(shot)]
            elif LIVE_FRAME.exists():
                images = [str(LIVE_FRAME)]
    except Exception as e:
        log.debug("screen enrich: %s", e)

    ctx = context_for_prompt()
    if ctx or win:
        body = f"{body}\n\n---\n{ctx}{f'Активное окно: {win}' if win else ''}"

    append_agent_message("user", body, uid, origin="desktop")
    session = load_agent_session(uid)
    memory = memory_block_for_prompt(session)
    gblock = ""
    try:
        from game_memory import game_memory_block_for_prompt as _gblock_fn

        gblock = _gblock_fn() if companion else ""
        if gblock:
            memory = f"{memory}\n\n{gblock}" if memory else gblock
    except Exception:
        gblock = ""

    from agent_prompt import detect_task_kind  # noqa: E402
    from hoshi_daemon import build_agent_prompt  # noqa: E402

    # Игровые «не работает» — не code_fix бота, а урок напарника
    kind = detect_task_kind(head)
    if companion and kind == "code_fix" and not re.search(
        r"(?:код|бот|юна|daemon|файл|скрипт)", head.lower()
    ):
        kind = "agent_message"
    if kind != "code_fix":
        kind = detect_task_kind(body) if not companion else kind
    is_code_fix = kind == "code_fix"
    task_extra = {"desktop": True, "origin": "yuna-desktop", "desktop_memory": True}
    if companion:
        task_extra["game_companion"] = True
    base = build_agent_prompt(
        body,
        kind=kind if is_code_fix else "agent_message",
        task_extra=task_extra,
        light=False,
    )
    # Для companion в ollama идёт короткий user_text; memory+game уже в session/prompt для cursor
    prompt = f"{memory}\n\n---\n\n{_DESKTOP_TOOLS}\n\n{base}" if memory else f"{_DESKTOP_TOOLS}\n\n{base}"
    if companion and gblock:
        # в быстрый ollama-чат передаём цели/уроки вместе с репликой
        head_for_llm = f"{head}\n\n{gblock}"
    else:
        head_for_llm = head
    if listen_fact:
        head_for_llm = (
            f"{head_for_llm}\n\n---\nФакт с ПК: {listen_fact}\n"
            "Если спросили название — назови трек из факта; если «как тебе» — название + мнение. "
            "Не пиши «не вижу», если в факте есть «Сейчас играет»."
        )

    reply = ""
    brain = BRAIN
    if brain in ("ollama", "auto"):
        try:
            reply = _ollama_chat(
                prompt,
                is_code_fix=is_code_fix,
                images=images or None,
                user_text=head_for_llm,
                companion=companion,
                control_mode=control_mode,
                memory=memory or "",
            )
        except Exception as e:
            log.warning("desktop ollama failed: %s", e)
            if brain == "ollama":
                fb = _action_fallback(head)
                if fb:
                    reply = fb
                else:
                    err = str(e)
                    if "111" in err or "refused" in err.lower():
                        reply = (
                            "Мозг на секунду отвалился (Ollama). "
                            "Напиши ещё раз — или в терминале: ./yuna_ctl.sh ollama && ./yuna_ctl.sh bridge"
                        )
                    else:
                        reply = "Сейчас туплю, напиши ещё раз через пару секунд."
            else:
                reply = ""

    if (not reply or reply.startswith("Ollama сбой")) and brain in ("cursor", "auto"):
        try:
            reply = _cursor_chat(prompt, is_code_fix=is_code_fix)
        except Exception as e:
            err = str(e)
            log.warning("desktop cursor failed: %s", err[:300])
            fb = _action_fallback(head)
            if fb:
                reply = fb
            elif _is_cursor_limit(err):
                reply = (
                    "Лимит Cursor — переключаюсь на локальный мозг. "
                    "Напиши ещё раз, если Ollama уже запущена."
                )
            else:
                reply = reply or f"Сбой агента: {err[:180]}"

    reply = (reply or "…").strip()
    if companion:
        try:
            record_action(reply, role="yuna")
        except Exception:
            pass
    append_agent_message("assistant", reply, uid, origin="desktop")
    return reply
