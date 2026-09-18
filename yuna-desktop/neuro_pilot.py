#!/usr/bin/env python3
"""
NeuroPilot v2 — как Neurosama: глаза ∥ игровой агент ∥ чат.

  кадр → kind → политика (самообучение) → действия → награда (изменился ли экран)

Самообучение: neuro_learn.py (Q по политикам + playbook).
Стоп: «стоп» / physical mouse / stop_pilot().
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from desk_config import DATA, HOSHI_CORE, OLLAMA_VISION_MODEL  # noqa: E402

log = logging.getLogger("yuna.neuro_pilot")

PILOT_STATE = DATA / "neuro_pilot.json"
TICK_SEC = 0.12  # быстрее: нейросеть крутится ~20 FPS
VLM_EVERY_SEC = 5.5

_running = False
_thread: threading.Thread | None = None
_stop_evt = threading.Event()
_lock = threading.Lock()

_last_see = ""
_last_kind = "unknown"
_last_vlm = 0.0
_last_fp = ""
_ticks = 0
_actions_done = 0
_same_see = 0
_vlm_busy = False
_vlm_lock = threading.Lock()
_vpt_ready: bool | None = None  # None = ещё не пробовали
_prev_kind_for_vpt = ""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_pilot() -> dict[str, Any]:
    if not PILOT_STATE.exists():
        return {"running": False, "goal": "", "see": "", "kind": "", "ticks": 0}
    try:
        raw = PILOT_STATE.read_text(encoding="utf-8").strip()
        if raw.count("{") > 1:
            raw = raw[: raw.find("}") + 1]
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {"running": False}


def save_pilot(data: dict[str, Any]) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    data["at"] = _now()
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = PILOT_STATE.with_suffix(".tmp")
    try:
        import fcntl

        with open(tmp, "w", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(PILOT_STATE)
    except Exception:
        try:
            PILOT_STATE.write_text(payload, encoding="utf-8")
        except Exception as e:
            log.debug("save_pilot: %s", e)


def is_pilot_running() -> bool:
    return bool(load_pilot().get("running")) and (_thread is not None and _thread.is_alive())


def _focus_game() -> str:
    from screen_context import hypr_active_window

    win = hypr_active_window() or ""
    if re.search(r"minecraft|java|prism|lunar|forge|fabric|terraria", win, re.I):
        return win
    try:
        subprocess.run(
            [
                "hyprctl",
                "dispatch",
                "focuswindow",
                "class:^(Minecraft|java|org.prismlauncher|Terraria)",
            ],
            capture_output=True,
            timeout=2,
        )
    except Exception:
        pass
    time.sleep(0.12)
    return hypr_active_window() or win


_DEATH_RE = re.compile(
    r"ты\s+погиб|вы\s+погибли|экран\s+смерт|you\s+died|respawn\s+screen|"
    r"возродиться|возрождени|нажми.{0,15}(?:чтобы\s+)?возроди|"
    r"click\s+to\s+respawn|game\s*over",
    re.I,
)


def _quick_kind(see: str, win: str) -> str:
    blob = f"{see} {win}".lower()
    in_mc = bool(re.search(r"minecraft|java|prism|forge|fabric", win, re.I))
    forge_world = bool(
        in_mc
        and re.search(r"одиночн\w*\s+игра|singleplayer|многопользоват|multiplayer", win, re.I)
        and not re.search(r"launcher|prism", win, re.I)
    )
    if _DEATH_RE.search(see.lower()):
        return "death"
    if re.search(r"cursor agents|firefox|chrome|telegram|kotatogram|юна \[", blob) and not in_mc:
        return "wrong_window"
    if re.search(
        r"кнопк\w*\s+(?:одиноч|single|выход|options|настрой)|title screen|главное меню|"
        r"save and quit|сохранить и выйти|меню паузы|pause menu",
        see.lower(),
    ):
        return "menu"
    if re.search(r"инвентар|inventory|crafting|верстак|сундук|chest ui", see.lower()):
        return "inventory"
    if forge_world:
        return "world"
    if re.search(r"меню|menu|title screen|pause|пауза", see.lower()):
        return "menu"
    if in_mc or re.search(r"блок|дерев|трава|небо|hud|hotbar|мир|dirt|wood|камень", blob):
        return "world"
    if re.search(r"cursor agents|firefox|chrome|telegram", blob):
        return "wrong_window"
    return "unknown"


def _fast_vlm_see(frame: Path) -> str:
    try:
        from ollama_brain import chat_once
        from screen_context import hypr_active_window

        win = hypr_active_window() or ""
        raw = chat_once(
            [
                {
                    "role": "system",
                    "content": (
                        "Глаза игрового агента Minecraft. Одна короткая фраза: "
                        "если экран смерти/респавна (надпись «Ты погиб»/«You died»/кнопка "
                        "«Возродиться»/«Respawn») — начни со слова «экран смерти»; "
                        "иначе скажи меню|мир|инвентарь + что видно (блоки, враг, здоровье). "
                        "Не выдумывай."
                    ),
                },
                {"role": "user", "content": f"Окно: {win}. Что на экране?"},
            ],
            model=OLLAMA_VISION_MODEL,
            images=[str(frame)],
            temperature=0.1,
            num_predict=40,
            timeout=20,
        )
        return (raw or "").strip().split("\n")[0][:160]
    except Exception as e:
        log.debug("fast vlm: %s", e)
        return ""


def _vlm_bg(frame_path: str, win: str) -> None:
    global _last_see, _last_vlm, _vlm_busy, _last_kind
    try:
        see = _fast_vlm_see(Path(frame_path))
        if see:
            with _vlm_lock:
                _last_see = see
                _last_kind = _quick_kind(see, win)
            try:
                from live_vision import load_live, save_live

                st = load_live()
                st["see"] = see
                st["window"] = win
                save_live(st, touched_see=True)
            except Exception:
                pass
        _last_vlm = time.time()
    finally:
        _vlm_busy = False


def _vpt_available() -> bool:
    """Есть ли живой VPT-демон (настоящая нейросеть OpenAI)."""
    global _vpt_ready
    if _vpt_ready is True:
        return True
    try:
        from vpt_agent.client import ensure_server

        _vpt_ready = bool(ensure_server())
    except Exception as e:
        log.warning("VPT недоступна: %s", e)
        _vpt_ready = False
    return bool(_vpt_ready)


def _run_vpt_tick(frame: Path | None) -> list[str]:
    """Один тик настоящей нейросети: кадр → действие → мышь/клавиши."""
    from vpt_agent.client import act as vpt_act
    from vpt_agent.executor import apply_action

    if not frame or not Path(frame).exists():
        return ["vpt: нет кадра"]
    action = vpt_act(frame)
    if not action:
        return ["vpt: пустой ответ"]
    return apply_action(action, tick_sec=0.04)


# ── исполнение политик ─────────────────────────────────────────────────────
def _run_policy(policy: str) -> list[str]:
    from computer_control import (
        click,
        hold_keys,
        key_tap,
        mouse_down,
        mouse_move_to,
        mouse_turn_smooth,
        mouse_up,
        screen_size,
    )

    out: list[str] = []
    w, h = screen_size()

    def mine_hold(sec: float = 0.35) -> None:
        mouse_down("left")
        time.sleep(sec)
        mouse_up("left")
        out.append(f"ЛКМ {sec:.2f}s")

    if policy == "esc":
        out.append(key_tap("esc"))
        time.sleep(0.25)
    elif policy == "enter":
        out.append(key_tap("enter"))
    elif policy == "wait":
        time.sleep(0.35)
        out.append("wait")
    elif policy == "focus_game":
        _focus_game()
        out.append("focus")
    elif policy == "close_inv":
        out.append(key_tap("e"))
    elif policy == "click_craft_area":
        out.append(mouse_move_to(int(w * 0.45), int(h * 0.4), duration=0.2))
        out.append(click("left"))
    elif policy.startswith("click_mid"):
        yf = {"click_mid_high": 0.40, "click_mid": 0.48, "click_mid_low": 0.58}.get(policy, 0.48)
        out.append(mouse_move_to(w // 2, int(h * yf), duration=0.22))
        time.sleep(0.05)
        out.append(click("left"))
    elif policy == "forward":
        out.append(hold_keys(["w"], random.randint(400, 750)))
    elif policy == "forward_mine":
        out.append(mouse_turn_smooth(random.randint(-20, 20), random.randint(-10, 8), duration=0.15))
        out.append(hold_keys(["w"], 280))
        mine_hold(0.45)
        out.append("W+mine")
    elif policy == "look_left_mine":
        out.append(mouse_turn_smooth(-70, random.randint(-15, 10), duration=0.3))
        mine_hold(0.45)
    elif policy == "look_right_mine":
        out.append(mouse_turn_smooth(70, random.randint(-15, 10), duration=0.3))
        mine_hold(0.45)
    elif policy == "look_up_mine":
        out.append(mouse_turn_smooth(random.randint(-30, 30), -45, duration=0.28))
        mine_hold(0.5)
    elif policy == "strafe_left":
        out.append(hold_keys(["a"], random.randint(300, 550)))
    elif policy == "strafe_right":
        out.append(hold_keys(["d"], random.randint(300, 550)))
    elif policy == "jump_forward":
        out.append(key_tap("space"))
        out.append(hold_keys(["w"], 450))
    elif policy == "turn_around":
        out.append(mouse_turn_smooth(150, 0, duration=0.45))
        out.append(hold_keys(["w"], 300))
    elif policy == "place_try":
        out.append(click("right"))
    elif policy == "respawn_click":
        out.append(mouse_move_to(w // 2, int(h * 0.52), duration=0.2))
        out.append(click("left"))
        out.append(key_tap("enter"))
    elif policy == "respawn_enter":
        out.append(key_tap("enter"))
        time.sleep(0.15)
        out.append(mouse_move_to(w // 2, int(h * 0.52), duration=0.18))
        out.append(click("left"))
    else:
        out.append(hold_keys(["w"], 400))
        mine_hold(0.3)
    return out


def _pilot_tick(goal: str) -> None:
    global _ticks, _actions_done, _last_kind, _last_vlm, _last_see, _last_fp, _same_see, _vlm_busy
    global _prev_kind_for_vpt

    from computer_control import is_enabled
    from live_vision import LIVE_FRAME, capture_live_frame, get_live_see
    from neuro_learn import (
        choose_policy,
        compute_reward,
        frame_fingerprint,
        learn,
        screen_changed,
        skill_summary,
    )

    if not is_enabled():
        from computer_control import enable

        enable(reason="neuro_pilot", goal=goal)

    win = _focus_game()
    frame = capture_live_frame()
    fp_before = frame_fingerprint(frame or LIVE_FRAME)
    see = get_live_see(max_age_sec=14) or _last_see or ""

    now = time.time()
    if frame and (not _vlm_busy) and (now - _last_vlm) >= VLM_EVERY_SEC:
        _vlm_busy = True
        threading.Thread(
            target=_vlm_bg, args=(str(frame), win), daemon=True, name="pilot-vlm"
        ).start()

    kind = _quick_kind(see, win) if see else (_last_kind or "unknown")
    if not see and kind == "unknown":
        kind = "world" if re.search(r"minecraft|java|prism", win, re.I) else "wrong_window"

    if see and see[:50] == (_last_see or "")[:50] and fp_before == _last_fp:
        _same_see += 1
    else:
        _same_see = 0
    _last_see = see or _last_see
    _last_kind = kind
    _ticks += 1

    # После смерти/меню — сбросить память нейросети (новый эпизод)
    if _prev_kind_for_vpt in ("death", "menu") and kind == "world":
        try:
            from vpt_agent.client import reset as vpt_reset

            vpt_reset()
        except Exception:
            pass
    _prev_kind_for_vpt = kind

    # Мир → настоящая нейросеть VPT; меню/смерть/инвентарь → эвристики-супервизор
    use_vpt = kind == "world" and _vpt_available()
    policy = "vpt_neuro" if use_vpt else choose_policy(kind, stuck=_same_see)
    try:
        if use_vpt:
            results = _run_vpt_tick(frame or LIVE_FRAME)
        else:
            try:
                from vpt_agent.executor import reset_holds

                reset_holds()
            except Exception:
                pass
            results = _run_policy(policy)
        _actions_done += max(1, len(results))
    except PermissionError:
        log.info("pilot: control disabled — stop")
        _stop_evt.set()
        return
    except Exception as e:
        log.warning("pilot act: %s", e)
        results = []

    # дать экрану обновиться, затем награда
    time.sleep(0.08 if use_vpt else 0.18)
    frame2 = capture_live_frame()
    fp_after = frame_fingerprint(frame2 or LIVE_FRAME)
    changed = screen_changed(fp_before, fp_after, min_diff=1)
    see_after = get_live_see(max_age_sec=3) or see
    kind_after = _quick_kind(see_after, win)

    reward = compute_reward(
        kind_before=kind,
        kind_after=kind_after,
        changed=changed,
        policy=policy,
        stuck_before=_same_see,
    )
    if not use_vpt:
        learn(kind=kind, policy=policy, reward=reward, see=see)
    _last_fp = fp_after or fp_before

    engine = "VPT" if use_vpt else "heur"
    save_pilot(
        {
            "running": True,
            "pid": os.getpid(),
            "goal": goal,
            "see": (see or _last_see)[:180],
            "kind": kind_after or kind,
            "policy": policy,
            "engine": engine,
            "reward": round(reward, 3),
            "changed": changed,
            "window": win[:120],
            "ticks": _ticks,
            "actions": _actions_done,
            "stuck": _same_see,
            "learn": (skill_summary()[:120] if not use_vpt else "нейросеть OpenAI VPT 1x"),
            "last_results": [str(r)[:70] for r in (results or [])[:4]],
        }
    )
    try:
        from game_memory import record_action

        record_action(
            f"pilot/{engine}/{kind}->{policy} r={reward:.2f}",
            role="yuna",
        )
    except Exception:
        pass
    try:
        from yuna_status import set_component

        set_component(
            "pilot",
            "playing",
            f"{engine} {kind_after or kind}/{policy} r={reward:+.1f}",
        )
    except Exception:
        pass


def _loop(goal: str) -> None:
    global _running, _ticks, _actions_done, _same_see, _last_vlm, _last_fp
    log.info("NeuroPilot v2 START goal=%s", goal[:80])
    _ticks = 0
    _actions_done = 0
    _same_see = 0
    _last_vlm = 0.0
    _last_fp = ""
    save_pilot({"running": True, "goal": goal, "see": "", "kind": "boot", "ticks": 0, "pid": os.getpid()})

    try:
        from computer_control import enable
        from game_memory import enable_companion

        enable(reason="neuro_pilot", goal=goal)
        enable_companion("Minecraft")
    except Exception as e:
        log.warning("pilot boot: %s", e)

    while not _stop_evt.is_set():
        try:
            from computer_control import is_enabled

            if not is_enabled():
                log.info("pilot: host took mouse — pause")
                break
            _pilot_tick(goal)
        except Exception as e:
            log.warning("pilot tick: %s", e)
        _stop_evt.wait(TICK_SEC)

    _running = False
    st = load_pilot()
    st["running"] = False
    st["stopped_at"] = _now()
    save_pilot(st)
    try:
        from yuna_status import set_component

        set_component("pilot", "idle", "стоп")
    except Exception:
        pass
    log.info("NeuroPilot STOP ticks=%s actions=%s", _ticks, _actions_done)


def _pid_is_our_pilot(pid: int) -> bool:
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "ignore")
        return "yuna" in cmd.lower() or "neuro_pilot" in cmd or "local_bridge" in cmd or "voice" in cmd
    except Exception:
        return False


def start_pilot(goal: str = "играть в Minecraft") -> str:
    global _thread, _running, _stop_evt
    with _lock:
        st0 = load_pilot()
        other_pid = int(st0.get("pid") or 0)
        if other_pid and other_pid != os.getpid() and _pid_is_our_pilot(other_pid):
            # живой пилот в bridge/voice — обновить цель
            st0["cmd"] = "start"
            st0["goal"] = goal[:300]
            st0["running"] = True
            save_pilot(st0)
            return f"NeuroPilot уже крутится (pid {other_pid}). Цель: {goal[:80]}"
        # протухший pid
        if other_pid and other_pid != os.getpid() and not _pid_is_our_pilot(other_pid):
            st0["running"] = False
            st0["pid"] = 0
            save_pilot(st0)

        if _thread and _thread.is_alive():
            st = load_pilot()
            st["goal"] = goal[:300]
            save_pilot(st)
            return f"Уже играю. Цель: {goal[:80]}. {st.get('learn') or ''}"
        _stop_evt = threading.Event()
        _running = True
        _thread = threading.Thread(target=_loop, args=(goal[:300],), daemon=True, name="neuro-pilot")
        _thread.start()
        st = load_pilot()
        st.update({"running": True, "pid": os.getpid(), "goal": goal[:300], "cmd": ""})
        save_pilot(st)
    try:
        from neuro_learn import skill_summary

        learn_bit = skill_summary()
    except Exception:
        learn_bit = ""
    return (
        "NeuroPilot: играю нейросетью OpenAI VPT (пиксели → действия, как Neurosama-мотор). "
        f"Скажи «стоп» чтобы отдать мышь. Цель: {goal[:90]}. {learn_bit}"
    )


def stop_pilot() -> str:
    global _running
    _stop_evt.set()
    try:
        from vpt_agent.executor import reset_holds

        reset_holds()
    except Exception:
        pass
    try:
        from computer_control import disable

        disable()
    except Exception:
        pass
    st = load_pilot()
    st["running"] = False
    save_pilot(st)
    _running = False
    try:
        from yuna_status import set_component

        set_component("pilot", "idle", "стоп")
    except Exception:
        pass
    return "NeuroPilot стоп. Мышь снова твоя."


def pilot_status() -> str:
    st = load_pilot()
    if not st.get("running"):
        return "NeuroPilot выключен."
    return (
        f"Играю: {st.get('kind')}/{st.get('policy')} r={st.get('reward')} "
        f"ticks={st.get('ticks')} | {st.get('learn') or ''} | вижу: {st.get('see') or '—'}"
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
    HOSHI_CORE.mkdir(parents=True, exist_ok=True)
    log.info("neuro_pilot daemon v2 ready")

    def _sig(*_a: object) -> None:
        stop_pilot()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    st = load_pilot()
    if st.get("running") and st.get("goal"):
        start_pilot(str(st.get("goal") or "играть"))

    while True:
        time.sleep(1.0)
        st = load_pilot()
        cmd = str(st.get("cmd") or "")
        if cmd == "start":
            st["cmd"] = ""
            save_pilot(st)
            start_pilot(str(st.get("goal") or "играть в Minecraft"))
        elif cmd == "stop":
            st["cmd"] = ""
            save_pilot(st)
            stop_pilot()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "once":
        logging.basicConfig(level=logging.INFO)
        start_pilot(sys.argv[2] if len(sys.argv) > 2 else "minecraft")
        try:
            while is_pilot_running():
                time.sleep(1)
                print(pilot_status())
        except KeyboardInterrupt:
            print(stop_pilot())
    else:
        main()
