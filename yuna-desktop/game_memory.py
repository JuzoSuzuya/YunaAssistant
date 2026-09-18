#!/usr/bin/env python3
"""Память напарника в играх (Minecraft и др.): цели, счётчики, уроки, самоисправление."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from desk_config import HOSHI_CORE

GAME_MEMORY_PATH = HOSHI_CORE / "data" / "game_memory.json"

_GAME_WINDOW_RE = re.compile(
    r"minecraft|prismlauncher|curseforge|modrinth|lunar\s*client|badlion|"
    r"fabric|forge|quilt|steam.*(game|minecraft)|java.*(minecraft)|"
    r"terraria|valheim|factorio|rust\b|cs2|counter-strike|dota|"
    r"genshin|honkai|osu!",
    re.I,
)

_FAILURE_RE = re.compile(
    r"(?:не\s+так|не\s+то|не\s+правильно|неправильно|сломал(?:а|о)?|"
    r"не\s+работает|не\s+получил(?:ось)?|опять\s+не|ошиб(?:ка|ся|лась)|"
    r"зря|плохо|неверно|переделай|другой\s+способ|не\s+помог)",
    re.I,
)

_GOAL_RE = re.compile(
    r"(?:цель|давай|нужно|надо|хочу|будем|давай\s+сделаем)\s*[:\-]?\s*(.+)$",
    re.I,
)

_COUNT_RE = re.compile(
    r"(?:есть|набрал(?:а|и)?|добыл(?:а|и)?|сделал(?:а|и)?|осталось|"
    r"сейчас)\s+(\d+)\s+([a-zA-Zа-яА-ЯёЁ_\-]+)",
    re.I,
)

_DEFAULT: dict[str, Any] = {
    "active_game": None,
    "companion_mode": False,
    "goals": [],
    "last_actions": [],
    "inventory_notes": [],
    "counts": {},
    "failures": [],
    "lessons": [],
    "session_notes": [],
    # прогресс «умнеет со временем»
    "skill_xp": 0,
    "skill_level": 1,
    "sessions_played": 0,
    "steps_ok": 0,
    "steps_fail": 0,
    "strategies_ok": [],  # что сработало
    "playbook": [],  # короткие правила, выученные самой
    "phase": "day1_gather",  # day1_gather → tools → shelter → mine → …
    "pending_question": "",
    "last_plan": "",
    "updated_at": "",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_game_memory() -> dict[str, Any]:
    if not GAME_MEMORY_PATH.exists():
        return dict(_DEFAULT)
    try:
        data = json.loads(GAME_MEMORY_PATH.read_text(encoding="utf-8"))
        out = dict(_DEFAULT)
        out.update(data)
        return out
    except Exception:
        return dict(_DEFAULT)


def save_game_memory(data: dict[str, Any]) -> None:
    data = dict(data)
    data["updated_at"] = _now()
    GAME_MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    GAME_MEMORY_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def detect_game_from_window(win: str) -> str | None:
    if not win:
        return None
    m = _GAME_WINDOW_RE.search(win)
    if not m:
        return None
    hit = m.group(0).lower()
    if "minecraft" in hit or "prism" in hit or "lunar" in hit or "badlion" in hit:
        return "Minecraft"
    if "terraria" in hit:
        return "Terraria"
    if "valheim" in hit:
        return "Valheim"
    if "factorio" in hit:
        return "Factorio"
    return hit[:40]


def is_companion_active(win: str | None = None) -> bool:
    gm = load_game_memory()
    if gm.get("companion_mode"):
        return True
    if win and detect_game_from_window(win):
        return True
    return False


def touch_game_window(win: str) -> None:
    """Вызывать из screen_watcher при активном игровом окне."""
    game = detect_game_from_window(win)
    if not game:
        return
    gm = load_game_memory()
    gm["active_game"] = game
    gm["companion_mode"] = True
    gm["last_seen_window"] = win[:120]
    gm["last_seen_at"] = _now()
    save_game_memory(gm)


def enable_companion(game: str = "Minecraft") -> None:
    gm = load_game_memory()
    gm["companion_mode"] = True
    gm["active_game"] = game
    save_game_memory(gm)


def disable_companion() -> None:
    gm = load_game_memory()
    gm["companion_mode"] = False
    save_game_memory(gm)


def record_action(text: str, *, role: str = "yuna") -> None:
    gm = load_game_memory()
    actions = list(gm.get("last_actions") or [])
    actions.append({"at": _now(), "role": role, "text": (text or "")[:400]})
    gm["last_actions"] = actions[-40:]
    save_game_memory(gm)


def record_lesson(*, bad: str, user_complaint: str) -> None:
    gm = load_game_memory()
    lesson = f"Не повторять: «{(bad or '')[:160]}» — хозяин: «{(user_complaint or '')[:120]}»"
    lessons = list(gm.get("lessons") or [])
    if lesson not in lessons:
        lessons.append(lesson)
    gm["lessons"] = lessons[-30:]
    fails = list(gm.get("failures") or [])
    fails.append({"at": _now(), "user": user_complaint[:300], "bad_advice": (bad or "")[:300]})
    gm["failures"] = fails[-30:]
    save_game_memory(gm)


def add_goal(text: str) -> None:
    gm = load_game_memory()
    goals = list(gm.get("goals") or [])
    g = text.strip()[:200]
    if g and g not in goals:
        goals.append(g)
    gm["goals"] = goals[-20:]
    save_game_memory(gm)


def set_count(name: str, value: int) -> None:
    gm = load_game_memory()
    counts = dict(gm.get("counts") or {})
    counts[name[:40]] = int(value)
    gm["counts"] = counts
    save_game_memory(gm)


def add_note(text: str) -> None:
    gm = load_game_memory()
    notes = list(gm.get("session_notes") or [])
    notes.append({"at": _now(), "text": text[:300]})
    gm["session_notes"] = notes[-40:]
    save_game_memory(gm)


def ingest_user_utterance(text: str) -> None:
    """Вытащить цели/счётчики из реплики хозяина."""
    t = (text or "").strip()
    if not t:
        return
    low = t.lower()
    if any(w in low for w in ("minecraft", "майнкрафт", "играем", "в игре", "напарник")):
        enable_companion("Minecraft" if "terraria" not in low else "Terraria")

    gm = load_game_memory()
    # ответ на её вопрос
    if gm.get("pending_question"):
        add_note(f"ответ на «{gm['pending_question']}»: {t[:200]}")
        record_action(f"owner answer: {t[:200]}", role="owner")
        clear_pending_question()
        # положительный фидбек на вопрос = xp
        gain_xp(2, reason="answered question")

    if not gm.get("companion_mode") and not detect_game_from_window(
        gm.get("last_seen_window") or ""
    ):
        if not any(w in low for w in ("майн", "minecraft", "добы", "крафт", "кирк", "железо", "алмаз")):
            return
        enable_companion("Minecraft")

    m = _GOAL_RE.search(t)
    if m:
        add_goal(m.group(1).strip())

    for cm in _COUNT_RE.finditer(t):
        try:
            set_count(cm.group(2), int(cm.group(1)))
        except Exception:
            pass

    record_action(t, role="owner")


def is_failure_feedback(text: str) -> bool:
    return bool(_FAILURE_RE.search(text or ""))


def last_yuna_advice() -> str:
    gm = load_game_memory()
    for item in reversed(gm.get("last_actions") or []):
        if item.get("role") == "yuna" and item.get("text"):
            return str(item["text"])
    return ""


def handle_failure_feedback(user_text: str) -> bool:
    if not is_failure_feedback(user_text):
        return False
    bad = last_yuna_advice()
    record_lesson(bad=bad, user_complaint=user_text)
    record_action(f"урок после ошибки: {user_text[:160]}", role="system")
    return True


def record_strategy_ok(text: str) -> None:
    gm = load_game_memory()
    ok = list(gm.get("strategies_ok") or [])
    t = (text or "").strip()[:200]
    if t and t not in ok:
        ok.append(t)
    gm["strategies_ok"] = ok[-40:]
    save_game_memory(gm)


def add_playbook_rule(rule: str) -> None:
    gm = load_game_memory()
    pb = list(gm.get("playbook") or [])
    r = (rule or "").strip()[:180]
    if r and r not in pb:
        pb.append(r)
    gm["playbook"] = pb[-50:]
    save_game_memory(gm)


def set_pending_question(q: str) -> None:
    gm = load_game_memory()
    gm["pending_question"] = (q or "").strip()[:240]
    save_game_memory(gm)


def clear_pending_question() -> None:
    gm = load_game_memory()
    gm["pending_question"] = ""
    save_game_memory(gm)


def set_last_plan(plan: str) -> None:
    gm = load_game_memory()
    gm["last_plan"] = (plan or "").strip()[:400]
    save_game_memory(gm)


def phase_for_level(level: int) -> str:
    if level <= 2:
        return "day1_gather"  # дерево, стол, базовые ресурсы
    if level <= 4:
        return "tools"  # кирка/топор/еда
    if level <= 6:
        return "shelter"  # дом, кровать, ночь
    if level <= 8:
        return "mine"  # шахта, руды
    return "advanced"  # фермы, исследование, бой


def gain_xp(amount: int, *, reason: str = "") -> dict[str, Any]:
    """Рост навыка. Уровень = 1 + xp//30."""
    gm = load_game_memory()
    xp = int(gm.get("skill_xp") or 0) + max(0, int(amount))
    level = 1 + xp // 30
    old = int(gm.get("skill_level") or 1)
    gm["skill_xp"] = xp
    gm["skill_level"] = level
    gm["phase"] = phase_for_level(level)
    if reason:
        add_note(f"xp+{amount}: {reason}")
    save_game_memory(gm)
    return {"xp": xp, "level": level, "leveled_up": level > old, "phase": gm["phase"]}


def mark_step_result(ok: bool, *, summary: str = "") -> None:
    gm = load_game_memory()
    if ok:
        gm["steps_ok"] = int(gm.get("steps_ok") or 0) + 1
        if summary:
            ok_list = list(gm.get("strategies_ok") or [])
            t = summary.strip()[:200]
            if t and t not in ok_list:
                ok_list.append(t)
            gm["strategies_ok"] = ok_list[-40:]
        save_game_memory(gm)
        gain_xp(3, reason=summary or "step ok")
    else:
        gm["steps_fail"] = int(gm.get("steps_fail") or 0) + 1
        save_game_memory(gm)
        gain_xp(1, reason="step fail learn")


def bump_session() -> None:
    gm = load_game_memory()
    gm["sessions_played"] = int(gm.get("sessions_played") or 0) + 1
    save_game_memory(gm)


def skill_curriculum() -> str:
    gm = load_game_memory()
    phase = gm.get("phase") or phase_for_level(int(gm.get("skill_level") or 1))
    level = int(gm.get("skill_level") or 1)
    table = {
        "day1_gather": (
            "Сейчас уровень новичка: добыть дерево (удары ЛКМ по стволу), "
            "открыть инвентарь E, скрафтить верстак и палки. Спрашивай хозяина, если неясно управление."
        ),
        "tools": "Делай каменные инструменты, еду, не лезь глубоко без кирки.",
        "shelter": "Дом/нора + свет + кровать до ночи. Не гуляй в темноте.",
        "mine": "Шахта аккуратно, лестница вверх, железо → печка.",
        "advanced": "Фермы, исследование, осторожный бой — планируй и спрашивай.",
    }
    return f"Уровень навыка {level}, фаза «{phase}». {table.get(phase, table['day1_gather'])}"


def game_memory_block_for_prompt(*, max_chars: int = 1800) -> str:
    gm = load_game_memory()
    if not gm.get("companion_mode") and not gm.get("active_game"):
        return ""

    lines = ["## Напарник в игре (память + рост навыка)"]
    if gm.get("active_game"):
        lines.append(f"Игра: {gm['active_game']}")
    lines.append(skill_curriculum())
    lines.append(
        f"XP={gm.get('skill_xp', 0)} ok_steps={gm.get('steps_ok', 0)} "
        f"fail={gm.get('steps_fail', 0)} sessions={gm.get('sessions_played', 0)}"
    )
    goals = gm.get("goals") or []
    if goals:
        lines.append("Цели: " + "; ".join(goals[-5:]))
    if gm.get("last_plan"):
        lines.append(f"Последний план: {gm['last_plan']}")
    if gm.get("pending_question"):
        lines.append(f"Жду ответ хозяина на: {gm['pending_question']}")
    counts = gm.get("counts") or {}
    if counts:
        bits = [f"{k}={v}" for k, v in list(counts.items())[-12:]]
        lines.append("Счётчики: " + ", ".join(bits))
    ok = gm.get("strategies_ok") or []
    if ok:
        lines.append("Сработало раньше:")
        for s in ok[-5:]:
            lines.append(f"- {s}")
    pb = gm.get("playbook") or []
    if pb:
        lines.append("Плейбук:")
        for r in pb[-6:]:
            lines.append(f"- {r}")
    lessons = gm.get("lessons") or []
    if lessons:
        lines.append("Уроки (не повторять):")
        for L in lessons[-6:]:
            lines.append(f"- {L}")
    actions = gm.get("last_actions") or []
    if actions:
        lines.append("Недавно:")
        for a in actions[-5:]:
            lines.append(f"- [{a.get('role')}] {a.get('text', '')[:120]}")
    lines.append(
        "Правила: общайся с хозяином, задавай 1 короткий вопрос если неуверена; "
        "учись на ошибках; с каждым шагом играй чуть умнее; day1 = ресурсы, потом сложнее."
    )
    return "\n".join(lines)[:max_chars]


def companion_system_hint() -> str:
    gm = load_game_memory()
    game = (gm.get("active_game") or "игра").strip()
    return (
        f"Ты Юна — умный напарник в «{game}» (локальная модель). "
        f"{skill_curriculum()} "
        "Говори живо: что делаешь, почему, и иногда спроси хозяина. "
        "Если прошлый ход плохой — не повторяй. Расти от простого к сложному."
    )
