#!/usr/bin/env python3
"""Персонажи и роли агента (Юна и др.)."""
from __future__ import annotations

import re
from typing import Any

from storage import load_settings, save_settings

DEFAULT_PERSONAS: dict[str, dict[str, Any]] = {
    "yuna": {
        "id": "yuna",
        "names": ["Юна", "Юно", "Yuna", "юна", "юно", "yuna"],
        "source": "SAO (Sword Art Online)",
        "description": "Юна — персонаж из SAO, мягкий голос, озвучка на русском.",
        "tts_voice": "ru-RU-SvetlanaNeural",
        "tts_rate": "-18%",
        "tts_pitch": "-2Hz",
        "tts_volume": "+0%",
        "tts_pitch_shift": 0.95,
        "tts_audio_filter": "highpass=f=80,lowpass=f=12000",
        "enabled": True,
    },
    "atri": {
        "id": "atri",
        "names": ["Атри", "Atri", "atri", "атри"],
        "source": "ATRI: My Dear Moments",
        "description": "Атри — мягкий живой голос, тёплый молодой тембр, русская озвучка.",
        "tts_voice": "ru-RU-SvetlanaNeural",
        "tts_rate": "-5%",
        "tts_pitch": "+2Hz",
        "tts_volume": "+0%",
        "tts_pitch_shift": 1.0,
        "tts_rubberband": False,
        "tts_audio_filter": "highpass=f=80,lowpass=f=14000",
        "enabled": True,
    },
    "yui": {
        "id": "yui",
        "names": ["Юи", "Yui", "юи", "yui"],
        "source": "SAO (Sword Art Online)",
        "description": "Юи — ИИ-дочь из SAO, детский голос, на «ты», тепло и прямо.",
        "tts_voice": "ru-RU-DariyaNeural",
        "tts_rate": "+5%",
        "tts_pitch": "+8Hz",
        "tts_volume": "+0%",
        "tts_pitch_shift": 1.08,
        "tts_audio_filter": "highpass=f=120,lowpass=f=14000",
        "enabled": True,
    },
}

REMEMBER_PERSONA_RE = re.compile(
    r"запомни.*?(?:еще\s+и|ещё\s+и|также)\s+(.+?)(?:\s*\(|$|\n)",
    re.I | re.S,
)


def _personas_root(settings: dict | None = None) -> dict[str, Any]:
    s = settings if settings is not None else load_settings()
    bucket = s.setdefault("personas", {"active": {}, "aliases": {}})
    bucket.setdefault("active", {})
    bucket.setdefault("aliases", {})
    return bucket


def list_personas(settings: dict | None = None) -> dict[str, dict[str, Any]]:
    root = _personas_root(settings)
    out = dict(DEFAULT_PERSONAS)
    for pid, data in (root.get("active") or {}).items():
        base = dict(DEFAULT_PERSONAS.get(pid, {}))
        base.update(data)
        base["id"] = pid
        base.setdefault("enabled", True)
        out[pid] = base
    return {k: v for k, v in out.items() if v.get("enabled", True)}


def get_persona(persona_id: str, settings: dict | None = None) -> dict[str, Any] | None:
    return list_personas(settings).get(persona_id.lower())


def resolve_persona_id(name: str, settings: dict | None = None) -> str | None:
    key = name.strip().lower()
    if not key:
        return None
    root = _personas_root(settings)
    alias = (root.get("aliases") or {}).get(key)
    if alias:
        return alias
    for pid, data in list_personas(settings).items():
        names = [n.lower() for n in data.get("names", [])]
        if key in names or key == pid:
            return pid
    return None


def remember_persona_from_text(text: str) -> str | None:
    """Сохраняет персонажа из «запомни, ты теперь ещё и Юна/…»."""
    if "запомни" not in text.lower():
        return None
    m = REMEMBER_PERSONA_RE.search(text)
    if not m:
        return None
    raw = m.group(1)
    parts = [p.strip() for p in re.split(r"/", raw) if p.strip()]
    if not parts:
        return None

    persona_id = None
    for part in parts:
        persona_id = resolve_persona_id(part) or persona_id
    if not persona_id and "юн" in raw.lower():
        persona_id = "yuna"
    if not persona_id and "yuna" in raw.lower():
        persona_id = "yuna"
    if not persona_id:
        return None

    s = load_settings()
    root = _personas_root(s)
    active = root.setdefault("active", {})
    aliases = root.setdefault("aliases", {})
    base = dict(DEFAULT_PERSONAS.get(persona_id, {"id": persona_id}))
    active[persona_id] = {**base, "enabled": True, "remembered_at": __import__("datetime").datetime.now().isoformat(timespec="seconds")}
    for part in parts:
        aliases[part.lower()] = persona_id
    aliases[persona_id] = persona_id
    save_settings(s)
    return persona_id


_PERSONA_STOP_RE = re.compile(
    r"^(?:"
    r"(?:@?\w+\s*[,!\s]+)*"
    r"(?P<persona>юи|yui|юна|yuna|юно|atri|атри)"
    r"[\s,]+"
    r"(?:стоп|stop|хватит|off|выключ|отключ|замолчи|молчи)"
    r"|"
    r"(?:стоп|stop|хватит|off|выключ|отключ|замолчи|молчи)"
    r"[\s,]+"
    r"(?P<persona2>юи|yui|юна|yuna|юно|atri|атри)"
    r")\s*[!?.…]*$",
    re.I,
)


def is_persona_stop_command(text: str) -> str | None:
    """«юи стоп» / «стоп юи» — id персонажа для отключения в чате."""
    t = (text or "").strip()
    if not t:
        return None
    m = _PERSONA_STOP_RE.match(t)
    if not m:
        return None
    raw = (m.group("persona") or m.group("persona2") or "").strip().lower()
    if raw in ("юно",):
        return "yuna"
    return resolve_persona_id(raw)


def _persona_disabled_key(persona_id: str) -> str:
    return f"persona_{persona_id.lower()}_disabled"


def is_persona_disabled_in_chat(chat_id: int, persona_id: str) -> bool:
    from chat_router import get_chat_preferences

    return bool(get_chat_preferences(chat_id).get(_persona_disabled_key(persona_id)))


def disable_persona_in_chat(chat_id: int, persona_id: str) -> None:
    from chat_router import save_chat_preferences

    save_chat_preferences(chat_id, {_persona_disabled_key(persona_id): True})


def enable_persona_in_chat(chat_id: int, persona_id: str) -> None:
    from chat_router import save_chat_preferences

    save_chat_preferences(chat_id, {_persona_disabled_key(persona_id): False})


def persona_id_at_start(text: str) -> str | None:
    """Персона в начале сообщения — без учёта disabled (для фильтра Юны)."""
    if is_persona_stop_command(text):
        return None
    t = (text or "").strip()
    if not t:
        return None
    low = t.lower()
    for data in list_personas().values():
        pid = data.get("id") or ""
        for name in data.get("names", []):
            n = name.strip().lower()
            if not n:
                continue
            if low == n or low.startswith(n + " ") or low.startswith(n + ","):
                return pid
            if re.match(rf"^{re.escape(n)}(?:\s|$|[,.!?])", low):
                return pid
    return None


def resolve_persona_from_trigger(text: str, *, chat_id: int | None = None) -> str | None:
    """Персона из обращения «юи, …» если не отключена в чате."""
    if is_persona_stop_command(text):
        return None
    t = (text or "").strip()
    if not t:
        return None
    low = t.lower()
    for data in list_personas().values():
        pid = data.get("id") or ""
        for name in data.get("names", []):
            n = name.strip().lower()
            if not n:
                continue
            if low == n or low.startswith(n + " ") or low.startswith(n + ","):
                if chat_id and is_persona_disabled_in_chat(chat_id, pid):
                    return None
                return pid
            if re.match(rf"^{re.escape(n)}(?:\s|$|[,.!?])", low):
                if chat_id and is_persona_disabled_in_chat(chat_id, pid):
                    return None
                return pid
    return None


def is_persona_trigger(text: str) -> bool:
    """Обращение к персонажу (Юна и др.) — как к Hoshi."""
    if is_persona_stop_command(text):
        return False
    t = text.strip()
    if not t:
        return False
    low = t.lower()
    for data in list_personas().values():
        for name in data.get("names", []):
            n = name.strip().lower()
            if not n:
                continue
            if low == n:
                return True
            if low.startswith(n + " ") or low.startswith(n + ","):
                return True
            if re.search(rf"(?:^|\s)@?{re.escape(n)}(?:\s|$|[,.!?])", low):
                return True
    return False


def persona_prompt_block() -> str:
    personas = list_personas()
    if not personas:
        return ""
    lines = ["**Активные персонажи:**"]
    for pid, data in personas.items():
        names = ", ".join(data.get("names", [])[:4])
        src = data.get("source", "")
        lines.append(f"- **{names}** ({src}) — id `{pid}`")
    lines.append(
        "Во внешних чатах говори от лица персонажа в **первом лице** — не отделяй «Юну» и «меня». "
        "Юна — **девушка**: в русском тексте **женский род** (нашла, поняла, исправила; "
        "не «нашёл», «понял», «исправил»). "
        "Для голосового (только по явной просьбе, **на русском**) добавь в конец "
        "`[[voice:ID]]текст для озвучки` (строка не уйдёт в чат)."
    )
    return "\n".join(lines)
