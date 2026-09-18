#!/usr/bin/env python3
"""
Самообучение NeuroPilot (в духе Neurosama game agent):
  состояние (kind + отпечаток кадра) → действие → награда → обновить веса.

Без RL-библиотеки: ε-greedy бандит по политикам + playbook правил.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from desk_config import DATA

log = logging.getLogger("yuna.neuro_learn")

SKILLS_PATH = DATA / "neuro_skills.json"

# Политики = именованные макросы действий
POLICIES: dict[str, list[str]] = {
    "menu": ["esc", "click_mid_high", "click_mid", "click_mid_low", "enter", "wait"],
    "world": [
        "forward_mine",
        "forward",
        "look_left_mine",
        "look_right_mine",
        "look_up_mine",
        "strafe_left",
        "strafe_right",
        "jump_forward",
        "turn_around",
        "place_try",
    ],
    "inventory": ["close_inv", "click_craft_area", "wait"],
    "wrong_window": ["focus_game", "wait"],
    "death": ["respawn_enter", "respawn_click", "wait"],
    "unknown": ["esc", "forward_mine", "click_mid", "look_left_mine"],
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _default() -> dict[str, Any]:
    return {
        "version": 2,
        "epsilon": 0.22,
        "alpha": 0.18,
        "visits": {},  # "kind|policy" -> n
        "value": {},  # "kind|policy" -> Q
        "playbook": [],  # [{when, do, score, n}]
        "episodes": [],  # последние исходы
        "stats": {"reward_sum": 0.0, "ticks": 0, "wins": 0, "fails": 0},
        "updated_at": "",
    }


def load_skills() -> dict[str, Any]:
    if not SKILLS_PATH.exists():
        return _default()
    try:
        data = json.loads(SKILLS_PATH.read_text(encoding="utf-8"))
        out = _default()
        out.update(data if isinstance(data, dict) else {})
        out.setdefault("visits", {})
        out.setdefault("value", {})
        out.setdefault("playbook", [])
        out.setdefault("episodes", [])
        out.setdefault("stats", _default()["stats"])
        return out
    except Exception:
        return _default()


def save_skills(data: dict[str, Any]) -> None:
    data = dict(data)
    data["updated_at"] = _now()
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = SKILLS_PATH.with_suffix(".tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    try:
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(SKILLS_PATH)
    except Exception as e:
        log.debug("save skills: %s", e)


def frame_fingerprint(path: Path | str | None, *, grid: int = 12) -> str:
    """Грубый perceptual hash кадра — чтобы понять, изменился ли экран."""
    if not path:
        return ""
    p = Path(path)
    if not p.exists():
        return ""
    try:
        from PIL import Image

        with Image.open(p) as im:
            im = im.convert("L").resize((grid, grid))
            pixels = list(im.getdata())
        avg = sum(pixels) / max(1, len(pixels))
        bits = "".join("1" if px >= avg else "0" for px in pixels)
        return hashlib.md5(bits.encode()).hexdigest()[:16]
    except Exception:
        try:
            # fallback: размер+mtime+кусок файла
            st = p.stat()
            chunk = p.read_bytes()[:4096]
            return hashlib.md5(f"{st.st_size}:{st.st_mtime_ns}".encode() + chunk).hexdigest()[:16]
        except Exception:
            return ""


def hamming(a: str, b: str) -> int:
    if not a or not b or len(a) != len(b):
        return 99
    # hex compare as nibble distance
    return sum(x != y for x, y in zip(a, b))


def screen_changed(fp_before: str, fp_after: str, *, min_diff: int = 1) -> bool:
    if not fp_before or not fp_after:
        return False
    return hamming(fp_before, fp_after) >= min_diff


def _key(kind: str, policy: str) -> str:
    return f"{kind}|{policy}"


def choose_policy(kind: str, *, stuck: int = 0) -> str:
    """ε-greedy + бонус playbook + штраф за залипание."""
    skills = load_skills()
    kind = kind or "unknown"
    options = list(POLICIES.get(kind) or POLICIES["unknown"])
    eps = float(skills.get("epsilon") or 0.22)
    # если застряли — больше исследования
    if stuck >= 3:
        eps = min(0.65, eps + 0.25)

    # playbook boost
    boost: dict[str, float] = {}
    for rule in skills.get("playbook") or []:
        if str(rule.get("when") or "") == kind:
            boost[str(rule.get("do") or "")] = float(rule.get("score") or 0) * 0.15

    if random.random() < eps:
        # не повторять ту же политику слишком часто при stuck
        return random.choice(options)

    best_p = options[0]
    best_v = -1e9
    values = skills.get("value") or {}
    visits = skills.get("visits") or {}
    for p in options:
        k = _key(kind, p)
        q = float(values.get(k) or 0.0)
        n = float(visits.get(k) or 0.0)
        # UCB-ish exploration bonus
        explore = 0.35 * math.sqrt(math.log(max(2.0, skills["stats"].get("ticks", 1) + 1)) / (n + 1))
        score = q + explore + boost.get(p, 0.0)
        if stuck >= 2 and n > 8 and q < 0.05:
            score -= 0.4  # не долбить провальную политику
        if score > best_v:
            best_v = score
            best_p = p
    return best_p


def compute_reward(
    *,
    kind_before: str,
    kind_after: str,
    changed: bool,
    policy: str,
    stuck_before: int,
) -> float:
    r = 0.0
    if kind_before == "menu" and kind_after == "world":
        r += 2.5  # вышла в мир — джекпот
    if kind_before == "wrong_window" and kind_after in ("world", "menu"):
        r += 1.5
    if kind_before == "inventory" and kind_after == "world":
        r += 1.0
    if changed:
        r += 0.55
    else:
        r -= 0.35
    if kind_after == "menu" and policy.startswith("forward"):
        r -= 0.8  # бегать в меню бесполезно
    if kind_after == "world" and "mine" in policy and changed:
        r += 0.35
    if stuck_before >= 4 and changed:
        r += 0.7  # вырвалась из залипания
    if stuck_before >= 4 and not changed:
        r -= 0.5
    # смерть — сильный сигнал: попала на экран смерти = штраф,
    # а успешный респавн обратно в мир = награда (учится не умирать / быстро возрождаться)
    if kind_after == "death" and kind_before != "death":
        r -= 1.6
    if kind_before == "death" and kind_after in ("world", "menu"):
        r += 1.4
    if kind_before == "death" and kind_after == "death" and not changed:
        r -= 0.3  # застряла на экране смерти — жать «возродиться» надо активнее
    return max(-2.0, min(3.0, r))


def learn(
    *,
    kind: str,
    policy: str,
    reward: float,
    see: str = "",
) -> None:
    skills = load_skills()
    alpha = float(skills.get("alpha") or 0.18)
    k = _key(kind, policy)
    visits = skills["visits"]
    values = skills["value"]
    n = int(visits.get(k) or 0) + 1
    visits[k] = n
    old = float(values.get(k) or 0.0)
    values[k] = old + alpha * (reward - old)

    stats = skills["stats"]
    stats["ticks"] = int(stats.get("ticks") or 0) + 1
    stats["reward_sum"] = float(stats.get("reward_sum") or 0) + reward
    if reward >= 1.0:
        stats["wins"] = int(stats.get("wins") or 0) + 1
    elif reward <= -0.4:
        stats["fails"] = int(stats.get("fails") or 0) + 1

    ep = {
        "at": _now(),
        "kind": kind,
        "policy": policy,
        "reward": round(reward, 3),
        "see": (see or "")[:100],
        "q": round(float(values[k]), 3),
        "n": n,
    }
    episodes = list(skills.get("episodes") or [])
    episodes.append(ep)
    skills["episodes"] = episodes[-80:]

    # playbook: если политика стабильно хороша
    if n >= 4 and values[k] >= 0.45:
        _upsert_playbook(skills, kind, policy, values[k], n)
    if n >= 5 and values[k] <= -0.35:
        # анти-правило
        _upsert_playbook(skills, kind, f"avoid:{policy}", values[k], n)

    # медленно снижать epsilon
    skills["epsilon"] = max(0.08, float(skills.get("epsilon") or 0.22) * 0.9995)

    save_skills(skills)

    # зеркало в game_memory playbook
    try:
        from game_memory import add_playbook_rule, gain_xp, mark_step_result

        if reward >= 1.2:
            add_playbook_rule(f"когда {kind} → {policy} (Q={values[k]:.2f})")
            gain_xp(3)
            mark_step_result(True, summary=f"{kind}/{policy} +{reward:.1f}")
        elif reward <= -0.5:
            mark_step_result(False, summary=f"{kind}/{policy} {reward:.1f}")
    except Exception:
        pass


def _upsert_playbook(skills: dict, when: str, do: str, score: float, n: int) -> None:
    book = list(skills.get("playbook") or [])
    for rule in book:
        if rule.get("when") == when and rule.get("do") == do:
            rule["score"] = round(0.7 * float(rule.get("score") or 0) + 0.3 * score, 3)
            rule["n"] = int(rule.get("n") or 0) + 1
            rule["at"] = _now()
            skills["playbook"] = book[-40:]
            return
    book.append({"when": when, "do": do, "score": round(score, 3), "n": n, "at": _now()})
    skills["playbook"] = book[-40:]


def top_policies(kind: str, *, k: int = 3) -> list[tuple[str, float, int]]:
    skills = load_skills()
    out = []
    for p in POLICIES.get(kind) or []:
        key = _key(kind, p)
        out.append((p, float((skills.get("value") or {}).get(key) or 0), int((skills.get("visits") or {}).get(key) or 0)))
    out.sort(key=lambda x: x[1], reverse=True)
    return out[:k]


def skill_summary() -> str:
    skills = load_skills()
    st = skills.get("stats") or {}
    lines = [
        f"навык: ticks={st.get('ticks', 0)} wins={st.get('wins', 0)} fails={st.get('fails', 0)} "
        f"ε={float(skills.get('epsilon') or 0):.2f}"
    ]
    for kind in ("menu", "world", "death"):
        tops = top_policies(kind, k=2)
        if tops:
            bits = ", ".join(f"{p}({q:.2f}/{n})" for p, q, n in tops)
            lines.append(f"{kind}: {bits}")
    pb = skills.get("playbook") or []
    if pb:
        last = pb[-3:]
        lines.append("правила: " + "; ".join(f"{r.get('when')}→{r.get('do')}" for r in last))
    return " | ".join(lines)
