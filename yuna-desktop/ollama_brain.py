#!/usr/bin/env python3
"""Локальный мозг Юны (Ollama): чат, зрение, правка файлов без Cursor."""
from __future__ import annotations

import base64
import json
import logging
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from desk_config import HOSHI_CORE, OLLAMA_MODEL, OLLAMA_URL, PROJECT

log = logging.getLogger("yuna.ollama_brain")

# Корень, в котором разрешены правки
_ALLOWED_ROOTS = (
    PROJECT.resolve(),
    HOSHI_CORE.resolve(),
    (PROJECT / "yuna-desktop").resolve(),
)

_TOOL_SCHEMA = """
Ты Юна — локальный агент на ПК хозяина. Отвечай по-русски, коротко и по делу.

Когда нужно читать/менять код или файлы — верни РОВНО один JSON-блок (без markdown) такого вида:
{"tool":"list_dir","path":"yuna-desktop"}
{"tool":"read_file","path":"yuna-desktop/desk_config.py","offset":1,"limit":120}
{"tool":"write_file","path":"yuna-desktop/foo.py","content":"...полный текст файла..."}
{"tool":"replace_in_file","path":"yuna-desktop/foo.py","old":"старое","new":"новое"}
{"tool":"run_shell","cmd":"python -m py_compile yuna-desktop/foo.py"}
{"tool":"describe_screen","path":"/path/to/shot.png"}
{"tool":"control_enable","goal":"поиграть в майн за хозяина"}
{"tool":"control_disable"}
{"tool":"control_status"}
{"tool":"mouse_move","x":900,"y":500}
{"tool":"mouse_rel","dx":50,"dy":-20}
{"tool":"click","button":"left","count":1}
{"tool":"key","name":"w"}
{"tool":"hold","keys":["w"],"ms":500}
{"tool":"play_step","goal":"добыть дерево"}
{"tool":"play_session","goal":"день 1 ресурсы","steps":3}
{"tool":"done","reply":"итог хозяину"}

Правила:
- Пути только относительно проекта Yuna или абсолютные внутри него.
- Не трогай .env с секретами, .git, .venv.
- После правок проверь синтаксис (run_shell + py_compile) и закончи tool=done.
- Мышь/клаву — ТОЛЬКО если хозяин явно просил управлять / играть за него, или control уже включён.
- Если правка не нужна — сразу {"tool":"done","reply":"..."}.
"""

_JSON_RE = re.compile(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", re.S)
_SECRET_NAMES = {".env", ".env.local", "id_ed25519", "credentials.json"}


def _http_json(url: str, payload: dict[str, Any], *, timeout: float = 180) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _tags(url: str) -> list[str]:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/api/tags", timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [m.get("name", "") for m in (data.get("models") or [])]
    except Exception:
        return []


def _ensure_ollama_up() -> None:
    """Если оба порта мёртвы — поднять user ollama (best-effort)."""
    if _tags("http://127.0.0.1:11435") or _tags("http://127.0.0.1:11434"):
        return
    script = Path(__file__).resolve().parent / "scripts" / "start_ollama.sh"
    if not script.is_file():
        return
    try:
        subprocess.Popen(
            ["bash", str(script)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        import time as _t

        for _ in range(24):
            _t.sleep(0.25)
            if _tags("http://127.0.0.1:11435"):
                return
    except Exception as e:
        log.debug("ensure ollama: %s", e)


def resolve_endpoint(preferred_model: str | None = None) -> tuple[str, str]:
    """Вернёт (url, model): сначала game-disk 11435, иначе системный 11434."""
    _ensure_ollama_up()
    wanted = preferred_model or OLLAMA_MODEL
    candidates = [OLLAMA_URL.rstrip("/")]
    if "11435" in candidates[0]:
        candidates.append("http://127.0.0.1:11434")
    elif "11434" in candidates[0]:
        candidates.append("http://127.0.0.1:11435")
    else:
        candidates.extend(["http://127.0.0.1:11435", "http://127.0.0.1:11434"])

    fallbacks = [
        wanted,
        OLLAMA_MODEL,
        "llama3.1:8b-instruct-q4_K_M",
        "llama3.1:8b-instruct-q4_K_M",
        "llama3.1:8b",
    ]
    for url in candidates:
        names = _tags(url)
        if not names:
            continue
        for m in fallbacks:
            if not m:
                continue
            if m in names:
                return url, m
            if any(n == m or n.startswith(m + ":") for n in names):
                return url, m
        return url, names[0]
    raise RuntimeError(
        "Ollama не запущена. В терминале: "
        "bash yuna-desktop/scripts/start_ollama.sh && ./yuna_ctl.sh bridge"
    )


def ollama_ready(url: str | None = None) -> bool:
    if url:
        return bool(_tags(url))
    try:
        resolve_endpoint()
        return True
    except Exception:
        return False


def chat_once(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    images: list[str] | None = None,
    temperature: float = 0.5,
    num_predict: int = 1024,
    timeout: float = 180,
) -> str:
    """Один ход /api/chat. images — пути к PNG/JPG (для VLM)."""
    url, model = resolve_endpoint(model or OLLAMA_MODEL)
    msgs = list(messages)
    if images:
        # Ollama: images на последнем user-сообщении (base64)
        b64s: list[str] = []
        for p in images[:3]:
            path = Path(p)
            if path.is_file() and path.stat().st_size < 12_000_000:
                b64s.append(base64.b64encode(path.read_bytes()).decode("ascii"))
        if b64s:
            last = dict(msgs[-1]) if msgs else {"role": "user", "content": ""}
            if last.get("role") != "user":
                msgs.append({"role": "user", "content": "Смотри скрин.", "images": b64s})
            else:
                last["images"] = b64s
                msgs[-1] = last

    payload = {
        "model": model,
        "messages": msgs,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": num_predict},
        "keep_alive": "30m",
    }
    last_err: Exception | None = None
    # 11435 → 11434 при Connection refused
    urls = [url]
    alt = "http://127.0.0.1:11434" if "11435" in url else "http://127.0.0.1:11435"
    if alt not in urls:
        urls.append(alt)
    for attempt_url in urls:
        try:
            data = _http_json(f"{attempt_url}/api/chat", payload, timeout=timeout)
            msg = data.get("message") or {}
            return str(msg.get("content") or "").strip()
        except Exception as e:
            last_err = e
            err = str(e).lower()
            if "111" in err or "refused" in err or "timed out" in err or "timeout" in err:
                log.warning("ollama %s fail: %s — try next", attempt_url, e)
                _ensure_ollama_up()
                continue
            break
    raise RuntimeError(f"Ollama недоступна: {last_err}") from last_err


def describe_image(path: str | Path, *, model: str | None = None) -> str:
    from desk_config import OLLAMA_VISION_MODEL

    p = Path(path)
    if not p.is_file():
        return ""
    vision = model or OLLAMA_VISION_MODEL or OLLAMA_MODEL
    try:
        return chat_once(
            [
                {
                    "role": "user",
                    "content": (
                        "Кратко по-русски: что на экране? Окна, текст, чаты — "
                        "только факты, до 60 слов."
                    ),
                }
            ],
            model=vision,
            images=[str(p)],
            temperature=0.2,
            num_predict=160,
            timeout=120,
        )
    except Exception as e:
        log.warning("describe_image: %s", e)
        return ""


def _safe_path(rel: str) -> Path:
    raw = (rel or "").strip()
    if not raw:
        raise ValueError("пустой path")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (PROJECT / p).resolve()
    else:
        p = p.resolve()
    if p.name in _SECRET_NAMES or ".git" in p.parts or ".venv" in p.parts:
        raise PermissionError(f"запрещённый путь: {p}")
    if not any(str(p).startswith(str(root)) for root in _ALLOWED_ROOTS):
        raise PermissionError(f"вне проекта: {p}")
    return p


def _run_tool(call: dict[str, Any]) -> str:
    tool = str(call.get("tool") or "").strip()
    if tool == "done":
        return str(call.get("reply") or "").strip()
    if tool == "list_dir":
        p = _safe_path(str(call.get("path") or "."))
        if not p.is_dir():
            return f"не директория: {p}"
        names = sorted(x.name for x in p.iterdir())[:80]
        return "\n".join(names) or "(пусто)"
    if tool == "read_file":
        p = _safe_path(str(call.get("path")))
        text = p.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        offset = max(1, int(call.get("offset") or 1))
        limit = min(200, max(1, int(call.get("limit") or 120)))
        chunk = lines[offset - 1 : offset - 1 + limit]
        numbered = [f"{i + offset}: {ln}" for i, ln in enumerate(chunk)]
        return "\n".join(numbered) if numbered else "(пусто)"
    if tool == "write_file":
        p = _safe_path(str(call.get("path")))
        content = str(call.get("content") or "")
        if len(content) > 400_000:
            return "слишком большой content"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"записано {p.relative_to(PROJECT)} ({len(content)} байт)"
    if tool == "replace_in_file":
        p = _safe_path(str(call.get("path")))
        old = str(call.get("old") or "")
        new = str(call.get("new") or "")
        if not old:
            return "нужен old"
        text = p.read_text(encoding="utf-8")
        if old not in text:
            return "old не найден"
        count = text.count(old)
        if count > 1 and not call.get("replace_all"):
            return f"old встречается {count} раз — уточни или replace_all"
        p.write_text(text.replace(old, new) if call.get("replace_all") else text.replace(old, new, 1), encoding="utf-8")
        return f"заменено в {p.relative_to(PROJECT)}"
    if tool == "run_shell":
        cmd = str(call.get("cmd") or "").strip()
        if not cmd:
            return "пустая cmd"
        banned = ("rm -rf /", "mkfs", "dd if=", ":(){", "shutdown", "reboot", "sudo ")
        low = cmd.lower()
        if any(b in low for b in banned):
            return "команда запрещена"
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=str(PROJECT),
            capture_output=True,
            text=True,
            timeout=float(call.get("timeout") or 60),
        )
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return f"exit={proc.returncode}\n{out[:4000]}"
    if tool == "describe_screen":
        return describe_image(str(call.get("path") or "")) or "не удалось описать"
    if tool == "control_enable":
        from computer_control import enable

        return enable(reason="tool", goal=str(call.get("goal") or ""))
    if tool == "control_disable":
        from computer_control import disable

        return disable()
    if tool == "control_status":
        from computer_control import status_block

        return status_block()
    if tool == "mouse_move":
        from computer_control import mouse_move_to

        return mouse_move_to(int(call.get("x", 0)), int(call.get("y", 0)))
    if tool == "mouse_rel":
        from computer_control import mouse_move_rel

        return mouse_move_rel(int(call.get("dx", 0)), int(call.get("dy", 0)))
    if tool == "click":
        from computer_control import click

        return click(str(call.get("button") or "left"), count=int(call.get("count") or 1))
    if tool == "key":
        from computer_control import key_tap

        return key_tap(str(call.get("name") or call.get("key") or ""))
    if tool == "hold":
        from computer_control import hold_keys

        keys = call.get("keys") or [call.get("key") or call.get("name")]
        keys = [str(k) for k in keys if k]
        return hold_keys(keys, int(call.get("ms") or 300))
    if tool == "play_step":
        from computer_control import play_step

        return play_step(goal=str(call.get("goal") or ""), max_actions=int(call.get("max") or 8))
    if tool == "play_session":
        from computer_control import play_session

        return play_session(goal=str(call.get("goal") or ""), steps=int(call.get("steps") or 3))
    return f"неизвестный tool: {tool}"


def _extract_tool(text: str) -> dict[str, Any] | None:
    t = (text or "").strip()
    if not t:
        return None
    # чистый JSON
    try:
        obj = json.loads(t)
        if isinstance(obj, dict) and obj.get("tool"):
            return obj
    except json.JSONDecodeError:
        pass
    # в fence или среди текста
    for m in _JSON_RE.finditer(t):
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("tool"):
            return obj
    return None


def run_agent(
    user_text: str,
    *,
    system_extra: str = "",
    model: str | None = None,
    images: list[str] | None = None,
    max_steps: int = 12,
    voice_short: bool = False,
) -> str:
    """Цикл tools → итоговый ответ. Без Cursor."""
    model = model or OLLAMA_MODEL
    system = _TOOL_SCHEMA
    if system_extra:
        system = f"{system}\n\n{system_extra}"
    if voice_short:
        system += "\nДля голоса: финальный reply — 1–2 коротких предложения, без markdown."

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_text},
    ]
    # первый ход с картинками если есть
    reply = chat_once(
        messages,
        model=model,
        images=images,
        temperature=0.35 if max_steps > 1 else 0.5,
        num_predict=80 if voice_short and max_steps <= 1 else 1400,
        timeout=90 if voice_short else 240,
    )
    messages.append({"role": "assistant", "content": reply})

    for step in range(max_steps):
        call = _extract_tool(reply)
        if not call:
            # модель ответила текстом — ок для обычного чата
            return reply.strip() or "…"
        if call.get("tool") == "done":
            return str(call.get("reply") or reply).strip() or "…"

        try:
            result = _run_tool(call)
        except Exception as e:
            result = f"ошибка tool: {e}"
        log.info("ollama tool step=%s %s → %s", step, call.get("tool"), result[:120])
        messages.append({"role": "user", "content": f"TOOL_RESULT:\n{result}"})
        reply = chat_once(
            messages,
            model=model,
            temperature=0.3,
            num_predict=1200,
            timeout=240,
        )
        messages.append({"role": "assistant", "content": reply})

    call = _extract_tool(reply)
    if call and call.get("tool") == "done":
        return str(call.get("reply") or "").strip() or "…"
    return reply.strip() or "Не успела закончить — скажи ещё раз."


def quick_reply(user_text: str, *, model: str | None = None, images: list[str] | None = None) -> str:
    """Быстрый ответ без tool-loop (голос)."""
    sys_msg = (
        "Ты Юна, голос на ПК хозяина. Ответь по-русски одним-двумя короткими "
        "предложениями (до 20 слов). Без markdown."
    )
    return chat_once(
        [{"role": "system", "content": sys_msg}, {"role": "user", "content": user_text}],
        model=model or OLLAMA_MODEL,
        images=images,
        temperature=0.45,
        num_predict=60,
        timeout=120,
    )
