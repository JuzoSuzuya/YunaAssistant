#!/usr/bin/env python3
"""HTTP API для панели и waybar. Единая память + локальная очередь задач."""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import sys
import uuid
from datetime import datetime
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parent
HOSHI = ROOT.parent / "hoshi-core"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HOSHI))

from desk_config import (  # noqa: E402
    BRIDGE_HOST,
    BRIDGE_PORT,
    BRIDGE_STATUS,
    DEFAULT_PERSONA,
    LOCAL_INBOX,
    OWNER_ID,
    VOICE_AUTOSPEAK,
    VOICE_SOCKET,
)
from desktop_agent import chat as desktop_chat  # noqa: E402
from fast_reply import try_action_reply, try_fast_reply  # noqa: E402
from memory_sync import pull_from_server, push_to_server, sync_bidirectional  # noqa: E402
from screen_context import context_for_prompt, hypr_active_window, set_active_project  # noqa: E402
from chat_memory import desktop_messages, refresh_memory  # noqa: E402
from unified_storage import (  # noqa: E402
    append_agent_message as storage_append,
    load_agent_session as storage_load_session,
    load_settings as storage_load_settings,
    save_settings as storage_save_settings,
)
from storage import _write_json  # noqa: E402
from yuna_status import get_status, set_component  # noqa: E402

log = logging.getLogger("yuna.bridge")
PANEL = ROOT / "panel"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _save_status(**kw: object) -> None:
    BRIDGE_STATUS.write_text(json.dumps({"updated_at": _now(), **kw}, ensure_ascii=False, indent=2), encoding="utf-8")


async def _voice_speak(text: str, persona: str = DEFAULT_PERSONA) -> bool:
    if not text.strip() or not VOICE_SOCKET.exists():
        return False
    try:
        reader, writer = await asyncio.open_unix_connection(str(VOICE_SOCKET))
        payload = json.dumps({"action": "speak", "text": text, "persona": persona}, ensure_ascii=False)
        writer.write(payload.encode("utf-8"))
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(65536), timeout=120)
        writer.close()
        await writer.wait_closed()
        return bool(json.loads(raw.decode()).get("ok"))
    except Exception as e:
        log.debug("voice speak failed: %s", e)
        return False


async def _bg_push() -> None:
    try:
        await asyncio.to_thread(push_to_server)
    except Exception as e:
        log.debug("bg push: %s", e)


def _is_routed_chat_write(text: str) -> bool:
    from chat_router import owner_wants_routed_chat_reply

    head = (text or "").split("\n---\n")[0]
    return owner_wants_routed_chat_reply(head) or owner_wants_routed_chat_reply(text or "")


def _enqueue_desktop_message(
    text: str,
    *,
    images: list[str] | None = None,
    append_user: bool = True,
) -> str:
    ctx = context_for_prompt()
    win = hypr_active_window()
    full = text.strip()
    if ctx or win:
        full = f"{text.strip()}\n\n---\n{ctx}{f'Активное окно: {win}' if win else ''}"
    if append_user:
        storage_append("user", full, OWNER_ID, origin="desktop")
    task_id = f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
    item = {
        "id": task_id,
        "source": "desktop",
        "kind": "agent_message",
        "text": full,
        "chat_id": OWNER_ID,
        "message_id": 0,
        "draft_id": abs(hash(task_id)) % (2**31 - 1),
        "received_at": _now(),
        "status": "pending",
        "note": "",
        "extra": {"desktop": True, "origin": "yuna-panel"},
        "images": images or [],
    }
    LOCAL_INBOX.mkdir(parents=True, exist_ok=True)
    _write_json(LOCAL_INBOX / f"{task_id}.json", item)
    return task_id


async def handle_health(_req: web.Request) -> web.Response:
    session = storage_load_session(OWNER_ID)
    pending = len(list(LOCAL_INBOX.glob("*.json")))
    st = get_status()
    _save_status(pending=pending, messages=len(session.get("messages") or []), **st)
    return web.json_response({
        "ok": True,
        "pending": pending,
        "messages": len(session.get("messages") or []),
        "voice_socket": VOICE_SOCKET.exists(),
        "status": st,
    })


async def handle_status(_req: web.Request) -> web.Response:
    return web.json_response(get_status())


async def handle_session(_req: web.Request) -> web.Response:
    session = refresh_memory(storage_load_session(OWNER_ID))
    return web.json_response(session)


async def handle_session_desktop(_req: web.Request) -> web.Response:
    session = refresh_memory(storage_load_session(OWNER_ID))
    msgs = desktop_messages(session)
    return web.json_response({
        "messages": msgs,
        "total": len(session.get("messages") or []),
        "memory_diary": len(session.get("memory_diary") or []),
        "has_summary": bool(session.get("rolling_summary")),
    })


async def handle_chat(req: web.Request) -> web.Response:
    body = await req.json()
    text = str(body.get("text") or "").strip()
    if not text:
        return web.json_response({"ok": False, "error": "empty"}, status=400)

    images: list[str] = []
    if body.get("screen"):
        from vision import capture_screen
        shot = capture_screen()
        if shot:
            images.append(str(shot))

    if _is_routed_chat_write(text):
        from server_task import submit_owner_task

        user_line = text.strip()
        storage_append("user", user_line, OWNER_ID, origin="desktop")

        async def _bg_submit() -> None:
            try:
                confirm = await asyncio.to_thread(
                    submit_owner_task, user_line, images=images or None, wait_timeout=150
                )
                if confirm:
                    storage_append("assistant", confirm, OWNER_ID, origin="desktop")
                    await _bg_push()
            except Exception as e:
                log.warning("routed submit failed: %s", e)
                storage_append(
                    "assistant",
                    "Сервер тормозит — задача в очереди, хозяин.",
                    OWNER_ID,
                    origin="desktop",
                )

        asyncio.create_task(_bg_submit())
        asyncio.create_task(_bg_push())
        return web.json_response(
            {"ok": True, "task_id": "server", "routed": True, "reply": "Пишу…"}
        )

    task_id = _enqueue_desktop_message(text, images=images)
    asyncio.create_task(_bg_push())
    return web.json_response({"ok": True, "task_id": task_id})


async def handle_chat_wait(req: web.Request) -> web.Response:
    body = await req.json()
    text = str(body.get("text") or "").strip()
    if not text:
        return web.json_response({"ok": False, "error": "empty"}, status=400)

    # Wake мгновенно; обои/музыка — actions (Cursor сейчас в лимите = игнор).
    fast = try_fast_reply(text) or try_action_reply(text)
    if fast:
        storage_append("user", text, OWNER_ID, origin="desktop")
        storage_append("assistant", fast, OWNER_ID, origin="desktop")
        asyncio.create_task(_bg_push())
        prefs = storage_load_settings().get("desktop") or {}
        if prefs.get("voice_autospeak", VOICE_AUTOSPEAK):
            persona = prefs.get("persona", DEFAULT_PERSONA)
            asyncio.create_task(_voice_speak(fast, persona))
        return web.json_response({"ok": True, "reply": fast, "fast": True})

    set_component("agent", "thinking", text[:60])
    try:
        reply = await asyncio.wait_for(
            asyncio.to_thread(desktop_chat, text),
            timeout=300.0,
        )
        # Всегда что-то в чат — даже если это сообщение про лимит.
        if not (reply or "").strip():
            reply = "Хозяин, ответа нет — сбой агента. Попробуй ещё раз."
        asyncio.create_task(_bg_push())
        prefs = storage_load_settings().get("desktop") or {}
        if prefs.get("voice_autospeak", VOICE_AUTOSPEAK):
            persona = prefs.get("persona", DEFAULT_PERSONA)
            asyncio.create_task(_voice_speak(reply[:1500], persona))
        return web.json_response({"ok": True, "reply": reply})
    except Exception as e:
        # Не глотаем ошибку молча: сначала пробуем обои/действия.
        # user уже мог быть записан в desktop_chat — дублировать не надо.
        from fast_reply import try_action_reply as _act

        fb = _act(text)
        if fb:
            storage_append("assistant", fb, OWNER_ID, origin="desktop")
            asyncio.create_task(_bg_push())
            return web.json_response({"ok": True, "reply": fb, "fast": True, "fallback": True})
        err = str(e)
        if "usage limit" in err.lower() or "ActionRequiredError" in err:
            msg = (
                "Лимит Cursor Agent — из‑за этого был игнор. "
                "Обои/музыку ставлю напрямую; для остального нужен сброс лимита."
            )
            storage_append("assistant", msg, OWNER_ID, origin="desktop")
            asyncio.create_task(_bg_push())
            return web.json_response({"ok": True, "reply": msg, "limited": True})
        log.warning("chat error: %s", e)
        msg = f"Сбой: {err[:180]}"
        storage_append("assistant", msg, OWNER_ID, origin="desktop")
        asyncio.create_task(_bg_push())
        return web.json_response({"ok": True, "reply": msg})
    finally:
        set_component("agent", "idle")


_DEFAULT_CLEANUP = (
    "highpass=f=85,lowpass=f=12000,"
    "acompressor=threshold=-16dB:ratio=2.5:attack=6:release=90"
)
_VOICE_CATALOG = {
    "engines": ["silero", "piper", "edge"],
    "silero_speakers": ["baya", "xenia", "kseniya", "aidar", "eugene"],
    "rvc_models": ["yuna", "hikari", "haruka"],
}


def _voice_cfg(desktop: dict) -> dict:
    from desk_config import VOICE_DEFAULTS

    cfg = dict(VOICE_DEFAULTS)
    cfg.update(desktop.get("voice") or {})
    return cfg


async def handle_settings_get(_req: web.Request) -> web.Response:
    s = storage_load_settings()
    desktop = s.setdefault("desktop", {})
    v = _voice_cfg(desktop)
    return web.json_response({
        "persona": desktop.get("persona", DEFAULT_PERSONA),
        "voice_autospeak": desktop.get("voice_autospeak", VOICE_AUTOSPEAK),
        "active_project": desktop.get("active_project", ""),
        "accent": desktop.get("accent", "#7c9eff"),
        "screen_watch": desktop.get("screen_watch", True),
        "screen_interval": desktop.get("screen_interval", 18),
        "reply_style": desktop.get("reply_style", "balanced"),
        "favorite_music": desktop.get("favorite_music", "lofi hip hop radio"),
        "favorite_wallpaper": desktop.get("favorite_wallpaper", "sword art online"),
        "voice": {
            "engine": v.get("engine", "silero"),
            "silero_speaker": v.get("silero_speaker", "baya"),
            "use_rvc": bool(v.get("use_rvc", False)),
            "rvc_model": v.get("rvc_model", "yuna"),
            "rvc_pitch": int(v.get("rvc_pitch", 4)),
            "rvc_index": float(v.get("rvc_index", 0.6)),
            "cleanup_on": bool(str(v.get("cleanup") or "").strip()),
        },
        "catalog": _VOICE_CATALOG,
    })


async def handle_settings_post(req: web.Request) -> web.Response:
    body = await req.json()
    s = storage_load_settings()
    desktop = s.setdefault("desktop", {})
    if "persona" in body:
        desktop["persona"] = str(body["persona"])
    if "voice_autospeak" in body:
        desktop["voice_autospeak"] = bool(body["voice_autospeak"])
    if body.get("active_project"):
        path = str(body["active_project"])
        desktop["active_project"] = path
        set_active_project(path)
    if "accent" in body:
        desktop["accent"] = str(body["accent"])[:16]
    if "screen_watch" in body:
        desktop["screen_watch"] = bool(body["screen_watch"])
    if "screen_interval" in body:
        try:
            desktop["screen_interval"] = max(4, int(body["screen_interval"]))
        except (TypeError, ValueError):
            pass
    if "reply_style" in body:
        desktop["reply_style"] = str(body["reply_style"])
    if "favorite_music" in body:
        desktop["favorite_music"] = str(body["favorite_music"])[:200]
    if "favorite_wallpaper" in body:
        desktop["favorite_wallpaper"] = str(body["favorite_wallpaper"])[:200]
    if isinstance(body.get("voice"), dict):
        vb = body["voice"]
        voice = desktop.setdefault("voice", {})
        if "engine" in vb:
            voice["engine"] = str(vb["engine"]).lower()
        if "silero_speaker" in vb:
            voice["silero_speaker"] = str(vb["silero_speaker"]).lower()
        if "use_rvc" in vb:
            voice["use_rvc"] = bool(vb["use_rvc"])
        if "rvc_model" in vb:
            voice["rvc_model"] = str(vb["rvc_model"]).lower()
        if "rvc_pitch" in vb:
            try:
                voice["rvc_pitch"] = int(vb["rvc_pitch"])
            except (TypeError, ValueError):
                pass
        if "rvc_index" in vb:
            try:
                voice["rvc_index"] = float(vb["rvc_index"])
            except (TypeError, ValueError):
                pass
        if "cleanup_on" in vb:
            voice["cleanup"] = _DEFAULT_CLEANUP if vb["cleanup_on"] else ""
    storage_save_settings(s)
    asyncio.create_task(_bg_push())
    return web.json_response({"ok": True})


async def handle_voice_test(req: web.Request) -> web.Response:
    """Сохраняет настройки голоса (если переданы) и проговаривает пробную фразу."""
    try:
        body = await req.json()
    except Exception:
        body = {}
    if isinstance(body.get("voice"), dict) or "voice_autospeak" in body:
        s = storage_load_settings()
        desktop = s.setdefault("desktop", {})
        vb = body.get("voice") or {}
        voice = desktop.setdefault("voice", {})
        for k in ("engine", "silero_speaker", "rvc_model"):
            if k in vb:
                voice[k] = str(vb[k]).lower()
        if "use_rvc" in vb:
            voice["use_rvc"] = bool(vb["use_rvc"])
        if "rvc_pitch" in vb:
            try:
                voice["rvc_pitch"] = int(vb["rvc_pitch"])
            except (TypeError, ValueError):
                pass
        if "rvc_index" in vb:
            try:
                voice["rvc_index"] = float(vb["rvc_index"])
            except (TypeError, ValueError):
                pass
        if "cleanup_on" in vb:
            voice["cleanup"] = _DEFAULT_CLEANUP if vb["cleanup_on"] else ""
        storage_save_settings(s)
    phrase = str(body.get("text") or "Привет, хозяин! Это мой голос. Как тебе звучание?")
    ok = await _voice_speak(phrase, DEFAULT_PERSONA)
    return web.json_response({"ok": ok})


async def handle_panel(_req: web.Request) -> web.Response:
    index = PANEL / "index.html"
    return web.FileResponse(index) if index.exists() else web.Response(text="panel missing", status=404)


async def poll_outbox(app: web.Application) -> None:
    """Опрос local_outbox — озвучивание ответов агента."""
    outbox = HOSHI / "data" / "local_outbox"
    seen: set[str] = set(app.get("seen_replies") or set())
    app["seen_replies"] = seen
    while True:
        try:
            prefs = storage_load_settings().get("desktop") or {}
            autospeak = prefs.get("voice_autospeak", VOICE_AUTOSPEAK)
            persona = prefs.get("persona", DEFAULT_PERSONA)
            for path in sorted(outbox.glob("*.json")):
                tid = path.stem
                if tid in seen:
                    continue
                item = json.loads(path.read_text(encoding="utf-8"))
                if item.get("status") != "done":
                    continue
                reply = (item.get("reply") or "").strip()
                if reply and autospeak:
                    await _voice_speak(reply[:1500], persona)
                seen.add(tid)
        except Exception as e:
            log.debug("outbox poll: %s", e)
        await asyncio.sleep(0.5)


async def handle_stop(req: web.Request) -> web.Response:
    """Стоп: прерывает речь/музыку и помечает старые ответы прочитанными."""
    outbox = HOSHI / "data" / "local_outbox"
    seen = req.app.get("seen_replies")
    if seen is not None:
        for path in outbox.glob("*.json"):
            seen.add(path.stem)
    from actions import stop_all

    msg = await asyncio.to_thread(stop_all)
    set_component("voice", "idle", "остановлена")
    return web.json_response({"ok": True, "reply": msg})


def _seed_seen_replies() -> set[str]:
    """Старые готовые ответы помечаем прочитанными, чтобы не озвучивать их при старте."""
    seen: set[str] = set()
    outbox = HOSHI / "data" / "local_outbox"
    if outbox.is_dir():
        for path in outbox.glob("*.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                if item.get("status") == "done":
                    seen.add(path.stem)
            except Exception:
                seen.add(path.stem)
    return seen


async def _on_startup(app: web.Application) -> None:
    app["seen_replies"] = _seed_seen_replies()
    app["poll_task"] = asyncio.create_task(poll_outbox(app))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/status", handle_status)
    app.router.add_get("/api/session", handle_session)
    app.router.add_get("/api/session/desktop", handle_session_desktop)
    app.router.add_post("/api/chat", handle_chat)
    app.router.add_post("/api/chat/wait", handle_chat_wait)
    app.router.add_get("/api/settings", handle_settings_get)
    app.router.add_post("/api/settings", handle_settings_post)
    app.router.add_post("/api/voice/test", handle_voice_test)
    app.router.add_post("/api/stop", handle_stop)
    app.router.add_get("/", handle_panel)
    assets = ROOT.parent / "assets"
    if assets.is_dir():
        app.router.add_static("/assets", assets, show_index=False)
    app.on_startup.append(_on_startup)
    log.info("bridge http://%s:%s", BRIDGE_HOST, BRIDGE_PORT)
    web.run_app(app, host=BRIDGE_HOST, port=BRIDGE_PORT, print=None, handle_signals=True)


if __name__ == "__main__":
    main()
