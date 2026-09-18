#!/usr/bin/env python3
"""Действия Юны на ПК: обои (Pinterest → wallhaven → mpvpaper) и музыка (mpv + yt-dlp)."""
from __future__ import annotations

import html as html_lib
import json
import logging
import os
import re
import socket
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("yuna.actions")

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
YTDLP = PROJECT / ".venv" / "bin" / "yt-dlp"
WALLPAPER_DIR = Path.home() / "Pictures" / "Wallpapers" / "yuna"
MUSIC_SOCK = "/tmp/yuna-music.sock"
DEFAULT_WALLPAPER_QUERY = "sword art online"
DEFAULT_MUSIC_QUERY = "lofi hip hop radio"
_LAST_ACTION_PATH = WALLPAPER_DIR / ".last_action.json"
_TASTE_PATH = WALLPAPER_DIR / ".music_taste.json"
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _desktop_settings() -> dict:
    try:
        from storage import load_settings

        return load_settings().get("desktop") or {}
    except Exception:
        return {}


def _save_last_action(kind: str, **payload: object) -> None:
    try:
        WALLPAPER_DIR.mkdir(parents=True, exist_ok=True)
        data = {"kind": kind, **payload}
        _LAST_ACTION_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log.debug("save last action failed: %s", e)


def _load_last_action() -> dict:
    try:
        if _LAST_ACTION_PATH.exists():
            data = json.loads(_LAST_ACTION_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


# ── обои ──────────────────────────────────────────────────────────────────
def list_monitors() -> list[str]:
    try:
        out = subprocess.run(
            ["hyprctl", "monitors", "-j"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        data = json.loads(out.stdout or "[]")
        names = [str(m.get("name") or "") for m in data if m.get("name")]
        return [n for n in names if n]
    except Exception as e:
        log.debug("hyprctl monitors failed: %s", e)
        return []


_THEME_MAP: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bsword\s*art(?:\s*online)?\b|\bса[оа]\b", re.I), "sword art online"),
    (re.compile(r"\bаниме\b|\banime\b", re.I), "anime"),
    (re.compile(r"зв[её]зд\w*", re.I), "stars"),
    (re.compile(r"\bнеб\w*|\bкосмос\w*|\bspace\b|\bsky\b", re.I), "sky"),
    (re.compile(r"\bноч\w*|\bnight\b", re.I), "night"),
    (re.compile(r"\bсакур\w*|\bsakura\b", re.I), "sakura"),
    (re.compile(r"\bгород\w*|\bcity\b|\bcyber\w*", re.I), "city"),
    (re.compile(r"\bдевушка\b|\bgirl\b", re.I), "girl"),
    (re.compile(r"\bпейзаж\w*|\blandscape\b", re.I), "landscape"),
    (re.compile(r"\bмилый\b|\bkawaii\b", re.I), "kawaii"),
    (re.compile(r"\bт[её]мн\w*|\bdark\b", re.I), "dark"),
]

_QUERY_STOP = {
    "мне", "меня", "мой", "моя", "мои", "на", "в", "с", "из", "по", "со", "для", "и", "а",
    "комп", "компьютер", "пк", "ноут", "монитор", "монитора", "мониторы", "экран", "десктоп",
    "поставь", "поставить", "поставь", "постав", "найди", "найти", "поищи", "скачай",
    "разные", "разный", "разное", "какие", "каких", "какую", "какой", "нибудь", "чёткие",
    "четких", "четкие", "чётких", "нормальные", "красивые", "красивых", "пожалуйста", "плиз",
    "давай", "обои", "обоев", "заставку", "заставка", "фон", "фона", "картинку", "картинки",
    "то", "что", "типа", "вроде", "какие-нибудь", "какая", "этот", "эта", "это", "там",
    "сюда", "сюда", "хочу", "нужны", "нужно", "сделай", "кинь", "скинь",
    "поменяй", "поменять", "смени", "сменить", "замени", "заменить", "другие", "другую",
    "другой", "другое", "только", "лишь", "именно",
    "первый", "первая", "первое", "первом", "первого", "первую",
    "второй", "вторая", "второе", "втором", "второго", "вторую",
    "третий", "третья", "третье", "третьем", "третьего",
    "левый", "левом", "левого", "правый", "правом", "правого",
    "оба", "обеих", "всех", "весь", "всё", "все",
}


_KNOWN_THEMES = {
    "anime", "stars", "sky", "night", "sakura", "city", "girl", "landscape",
    "kawaii", "dark", "sword", "art", "online", "space", "cyberpunk",
}


def _theme_looks_valid(theme: str) -> bool:
    toks = [t for t in (theme or "").lower().split() if t]
    if not toks:
        return False
    if any(t in _KNOWN_THEMES for t in toks):
        return True
    # мусор вроде «второго только»
    if any(t.startswith(("втор", "перв", "трет")) or t in _QUERY_STOP for t in toks):
        return False
    return len(toks) >= 1 and all(len(t) >= 3 for t in toks)


def _resolve_theme(raw: str | None) -> str:
    theme = _normalize_theme_query(raw or "") if raw else ""
    if theme and not _theme_looks_valid(theme):
        theme = ""
    if not theme:
        last = _load_last_action()
        if last.get("kind") == "wallpaper" and last.get("query"):
            theme = str(last["query"])
    if not theme:
        theme = (
            _normalize_theme_query(str(_desktop_settings().get("favorite_wallpaper") or ""))
            or DEFAULT_WALLPAPER_QUERY
        )
    return theme


def _monitor_index(text: str) -> int | None:
    """Какой монитор менять: 0/1/… или None = все / dual."""
    low = text.lower()
    if re.search(
        r"(?:только\s+)?(?:на\s+)?(?:втором|второй|2[\-\s]?м?)\s*монитор|"
        r"монитор(?:а)?\s*(?:номер\s*)?(?:2|два)|"
        r"второго\s+монитора|"
        r"только\s+(?:второго|второй|2)\b|"
        r"\bdp[\-\s]?1\b",
        low,
    ):
        return 1
    if re.search(
        r"(?:только\s+)?(?:на\s+)?(?:первом|первый|1[\-\s]?м?)\s*монитор|"
        r"монитор(?:а)?\s*(?:номер\s*)?(?:1|один)|"
        r"первого\s+монитора|"
        r"только\s+(?:первого|первый|1)\b|"
        r"\bhdmi[\-\s]?a?[\-\s]?1\b",
        low,
    ):
        return 0
    return None


def _normalize_theme_query(raw: str) -> str:
    """Вытащить тему из живой фразы → короткий поисковый запрос (EN предпочтительно)."""
    text = (raw or "").strip()
    if not text:
        return ""
    if re.search(r"\b(са[оа]|sao|sword\s*art)\b", text, re.I):
        return "sword art online"

    mapped: list[str] = []
    rest = text
    for pat, eng in _THEME_MAP:
        if pat.search(rest):
            if eng not in mapped:
                mapped.append(eng)
            rest = pat.sub(" ", rest)

    # остаток: осмысленные слова (имена, англ. теги)
    rest = re.sub(r"[^\w\s\-]+", " ", rest, flags=re.UNICODE)
    for tok in rest.lower().split():
        if tok in _QUERY_STOP or len(tok) < 3:
            continue
        if tok.isdigit():
            continue
        if tok not in mapped:
            mapped.append(tok)

    q = " ".join(mapped).strip()
    q = re.sub(r"\s{2,}", " ", q)
    return q


def _bing_image_urls(query: str, n: int = 4, *, site: str | None = None) -> list[str]:
    """Картинки через Bing Images (Pinterest — приоритетный site:)."""
    q = f"{query} wallpaper"
    if site:
        q = f"{q} site:{site}"
    params = urllib.parse.urlencode({
        "q": q,
        "qft": "+filterui:imagesize-wallpaper+filterui:aspect-wide",
        "form": "HDRSC2",
    })
    url = f"https://www.bing.com/images/search?{params}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"})
        with urllib.request.urlopen(req, timeout=18) as r:
            body = r.read().decode("utf-8", "ignore")
    except Exception as e:
        log.warning("bing image search failed: %s", e)
        return []

    found: list[str] = []
    for pat in (
        r'"murl"\s*:\s*"(https?://[^"]+)"',
        r"murl&quot;:&quot;(https?://[^&]+)",
        r"murl&quot;:&quot;(https?://[^&quot;]+)",
    ):
        for u in re.findall(pat, body):
            u = html_lib.unescape(u).replace("\\u0026", "&").strip()
            if not u.startswith("http"):
                continue
            low = u.lower()
            if any(x in low for x in (".svg", "logo", "avatar", "profile", "thumb")):
                continue
            if u not in found:
                found.append(u)
            if len(found) >= n * 3:
                break
        if len(found) >= n:
            break

    # Pinterest CDN вперёд
    pin = [u for u in found if "pinimg.com" in u.lower() or "pinterest" in u.lower()]
    other = [u for u in found if u not in pin]
    ordered = pin + other
    return ordered[:n]


def _pinterest_images(query: str, n: int = 2) -> list[str]:
    urls = _bing_image_urls(query, n=max(n, 4), site="pinterest.com")
    if len(urls) < n:
        # без site: — часто всё равно отдаёт pinimg
        extra = _bing_image_urls(f"{query} pinterest", n=max(n, 4), site=None)
        for u in extra:
            if u not in urls:
                urls.append(u)
    return urls[:n]


def _wallhaven_images(query: str, n: int = 1) -> list[str]:
    params = urllib.parse.urlencode({
        "q": query,
        "categories": "010",  # anime
        "purity": "100",  # SFW
        "sorting": "random",
        "atleast": "1920x1080",
    })
    url = f"https://wallhaven.cc/api/v1/search?{params}"
    paths: list[str] = []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "yuna/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
        for item in data.get("data") or []:
            p = item.get("path")
            if p and p not in paths:
                paths.append(p)
            if len(paths) >= n:
                break
    except Exception as e:
        log.warning("wallhaven search failed: %s", e)
    return paths


def _search_wallpaper_urls(query: str, n: int = 1) -> tuple[list[str], str]:
    """Приоритет: Pinterest → wallhaven. Возвращает (urls, source)."""
    urls = _pinterest_images(query, n)
    if len(urls) >= n:
        return urls[:n], "pinterest"
    if urls:
        # добрать с wallhaven
        for u in _wallhaven_images(query, n):
            if u not in urls:
                urls.append(u)
            if len(urls) >= n:
                break
        return urls[:n], "pinterest+wallhaven"
    urls = _wallhaven_images(query, n)
    if urls:
        return urls[:n], "wallhaven"
    # ослабленный запрос
    short = " ".join(query.split()[:2])
    if short and short != query:
        urls = _pinterest_images(short, n) or _wallhaven_images(short, n)
        if urls:
            return urls[:n], "fallback"
    return [], ""


def _download(url: str, dst: Path) -> bool:
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        headers = {"User-Agent": _UA, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}
        if "pinimg.com" in url or "pinterest" in url:
            headers["Referer"] = "https://www.pinterest.com/"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=40) as r:
            data = r.read()
            ctype = (r.headers.get("Content-Type") or "").lower()
        if len(data) < 1000:
            return False
        # если расширение странное — по content-type
        if dst.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            if "png" in ctype:
                dst = dst.with_suffix(".png")
            elif "webp" in ctype:
                dst = dst.with_suffix(".webp")
            else:
                dst = dst.with_suffix(".jpg")
        dst.write_bytes(data)
        return dst.exists() and dst.stat().st_size > 1000
    except Exception as e:
        log.warning("download failed (%s): %s", url[:80], e)
        return False


def _mpvpaper_sockets() -> list[str]:
    """Все IPC-сокеты работающих mpvpaper."""
    socks: list[str] = []
    try:
        out = subprocess.run(
            ["pgrep", "-a", "-f", "mpvpaper"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
        for m in re.finditer(r"input-ipc-server=(\S+)", out or ""):
            sock = m.group(1)
            if sock not in socks:
                socks.append(sock)
    except Exception:
        pass
    return socks


def _mpvpaper_socket() -> str | None:
    socks = _mpvpaper_sockets()
    return socks[0] if socks else None


def _mpv_ipc(sock: str, command: list) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(sock)
            s.sendall((json.dumps({"command": command}) + "\n").encode())
            s.recv(4096)
        return True
    except Exception as e:
        log.debug("mpv ipc failed (%s): %s", sock, e)
        return False


def _mpv_ipc_query(sock: str, command: list) -> object | None:
    """Запрос к mpv IPC — вернуть data или None."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(sock)
            s.sendall((json.dumps({"command": command}) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n") and len(buf) < 65536:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            data = json.loads(buf.decode("utf-8", errors="replace").strip().split("\n")[0])
            if data.get("error") == "success":
                return data.get("data")
    except Exception as e:
        log.debug("mpv ipc query failed (%s): %s", sock, e)
    return None


def _playerctl_env() -> dict[str, str]:
    """Демоны без сессии не видят MPRIS — подставляем user bus."""
    env = os.environ.copy()
    if not env.get("DBUS_SESSION_BUS_ADDRESS"):
        uid = os.getuid()
        bus = f"unix:path=/run/user/{uid}/bus"
        if Path(f"/run/user/{uid}/bus").exists():
            env["DBUS_SESSION_BUS_ADDRESS"] = bus
    return env


def now_playing() -> str:
    """Что сейчас играет (mpv Юны / Firefox/Spotify через playerctl)."""
    title = _mpv_ipc_query(MUSIC_SOCK, ["get_property", "media-title"])
    path = _mpv_ipc_query(MUSIC_SOCK, ["get_property", "path"])
    if title or path:
        last = _load_last_action()
        q = str(last.get("query") or "").strip() if last.get("kind") == "music" else ""
        bits = [str(title or path)]
        if q:
            bits.append(f"(запрос: {q})")
            remember_hearing(query=q)
        return "Сейчас играет: " + " ".join(bits)
    try:
        r = subprocess.run(
            [
                "playerctl",
                "-a",
                "metadata",
                "--format",
                "{{playerName}}|{{status}}|{{artist}} — {{title}}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            env=_playerctl_env(),
        )
        lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
        playing = []
        paused = []
        for ln in lines:
            parts = ln.split("|", 2)
            if len(parts) < 3:
                continue
            player, status, meta = parts[0], parts[1], parts[2]
            if not meta or meta.strip() in ("—", "-"):
                continue
            bit = f"{meta} ({player})"
            if status.lower() == "playing":
                playing.append(bit)
            else:
                paused.append(bit)
        if playing:
            note_playing_from_meta(playing[0])
            return "Сейчас играет: " + "; ".join(playing)
        if paused:
            note_playing_from_meta(paused[0])
            return "На паузе: " + "; ".join(paused)
    except Exception:
        pass
    last = _load_last_action()
    if last.get("kind") == "music" and last.get("query"):
        return (
            f"Метаданные плеера не вижу. Последнее, что включала сама: {last['query']}. "
            "Не выдумывай другое название."
        )
    return (
        "Метаданные трека не вижу (ни mpv, ни Firefox/Spotify через playerctl). "
        "Честно скажи, что не слышишь звук ушами и не знаешь название — не выдумывай."
    )


def _kill_mpvpaper() -> None:
    try:
        subprocess.run(["pkill", "-f", "mpvpaper"], timeout=5)
    except Exception:
        pass


def _start_mpvpaper(monitor: str, path: Path) -> None:
    sock = f"/tmp/yuna-mpvpaper-{monitor.replace('/', '_')}.sock"
    try:
        Path(sock).unlink(missing_ok=True)
    except Exception:
        pass
    subprocess.Popen(
        [
            "mpvpaper",
            "--fork",
            "-o",
            f"loop panscan=1.0 --mute=yes --input-ipc-server={sock}",
            monitor,
            str(path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _set_one_monitor(monitor: str, path: Path) -> bool:
    sock = f"/tmp/yuna-mpvpaper-{monitor.replace('/', '_')}.sock"
    if _mpv_ipc(sock, ["loadfile", str(path)]):
        return True
    try:
        subprocess.run(["pkill", "-f", f"mpvpaper.*{re.escape(monitor)}"], timeout=5)
    except Exception:
        pass
    _start_mpvpaper(monitor, path)
    return True


def set_wallpaper(
    query: str | None = None,
    *,
    dual_different: bool = False,
    monitor_index: int | None = None,
) -> str | None:
    """Ставит обои. None = не смогла → пусть ответит Cursor, без готовой отмазки."""
    theme = _resolve_theme(query)
    monitors = list_monitors()

    if monitor_index is not None:
        if not monitors:
            return "Не вижу мониторы через hyprctl."
        if monitor_index < 0 or monitor_index >= len(monitors):
            return f"Монитора №{monitor_index + 1} нет — у тебя {len(monitors)}."
        urls, source = _search_wallpaper_urls(theme, 4)
        if not urls:
            return None
        ext = os.path.splitext(urllib.parse.urlparse(urls[0]).path)[1] or ".jpg"
        if len(ext) > 5:
            ext = ".jpg"
        dst = WALLPAPER_DIR / f"wall{monitor_index + 1}{ext}"
        ok = False
        for u in urls:
            if _download(u, dst):
                ok = True
                break
        if not ok:
            return None
        mon = monitors[monitor_index]
        _set_one_monitor(mon, dst)
        _save_last_action(
            "wallpaper",
            query=theme,
            dual_different=False,
            monitor_index=monitor_index,
        )
        src_note = f", {source}" if source else ""
        return f"Поменяла обои только на {mon} (тема «{theme}»{src_note})."

    need = 2 if dual_different and len(monitors) >= 2 else 1
    urls, source = _search_wallpaper_urls(theme, need)
    if dual_different and len(urls) < 2:
        extra, src2 = _search_wallpaper_urls(f"{theme} landscape", 2)
        for u in extra:
            if u not in urls:
                urls.append(u)
            if len(urls) >= 2:
                break
        if src2 and source and src2 not in source:
            source = f"{source}+{src2}"
        elif src2 and not source:
            source = src2
    if not urls:
        return None

    paths: list[Path] = []
    for i, img_url in enumerate(urls[: max(need, 1)]):
        ext = os.path.splitext(urllib.parse.urlparse(img_url).path)[1] or ".jpg"
        if len(ext) > 5:
            ext = ".jpg"
        dst = WALLPAPER_DIR / f"wall{i + 1}{ext}"
        if not _download(img_url, dst):
            continue
        paths.append(dst)
    if dual_different and len(paths) < 2:
        for img_url in urls[len(paths):]:
            ext = os.path.splitext(urllib.parse.urlparse(img_url).path)[1] or ".jpg"
            if len(ext) > 5:
                ext = ".jpg"
            dst = WALLPAPER_DIR / f"wall{len(paths) + 1}{ext}"
            if _download(img_url, dst):
                paths.append(dst)
            if len(paths) >= 2:
                break
    if not paths:
        return None

    src_note = f", {source}" if source else ""
    if dual_different and len(monitors) >= 2 and len(paths) >= 2:
        _kill_mpvpaper()
        for mon, path in zip(monitors, paths):
            _start_mpvpaper(mon, path)
        names = " и ".join(monitors[: len(paths)])
        _save_last_action("wallpaper", query=theme, dual_different=True)
        return f"Поставила разные обои на {names} (тема «{theme}»{src_note})."

    path0 = paths[0]
    sock = _mpvpaper_socket()
    if sock and _mpv_ipc(sock, ["loadfile", str(path0)]):
        for s in _mpvpaper_sockets()[1:]:
            _mpv_ipc(s, ["loadfile", str(path0)])
        _save_last_action("wallpaper", query=theme, dual_different=False)
        return f"Поставила обои «{theme}»{src_note}."

    try:
        _kill_mpvpaper()
        targets = monitors if monitors else ["*"]
        if dual_different and len(paths) >= 2 and len(targets) >= 2:
            for mon, path in zip(targets, paths):
                _start_mpvpaper(mon, path)
            _save_last_action("wallpaper", query=theme, dual_different=True)
            return f"Поставила разные обои (тема «{theme}»{src_note})."
        for mon in targets:
            _start_mpvpaper(mon, path0)
        _save_last_action("wallpaper", query=theme, dual_different=False)
        return f"Поставила обои «{theme}»{src_note}."
    except Exception as e:
        log.warning("mpvpaper restart failed: %s", e)
        return None


# ── музыка ────────────────────────────────────────────────────────────────
def stop_music() -> str:
    stopped = _mpv_ipc(MUSIC_SOCK, ["quit"])
    try:
        # только mpv с нашим сокетом — не pkill по всей командной строке shell
        r = subprocess.run(["pgrep", "-af", "mpv"], capture_output=True, text=True, timeout=3)
        for ln in (r.stdout or "").splitlines():
            if MUSIC_SOCK not in ln:
                continue
            if "mpvpaper" in ln:
                continue
            pid = ln.split(None, 1)[0]
            if pid.isdigit():
                subprocess.run(["kill", pid], timeout=3, capture_output=True)
                stopped = True
    except Exception:
        pass
    try:
        Path(MUSIC_SOCK).unlink(missing_ok=True)
    except Exception:
        pass
    return "Музыка отключена." if stopped else "Музыка и так не играет."


def music_is_active() -> bool:
    """Играет ли наш mpv (IPC жив)."""
    pause = _mpv_ipc_query(MUSIC_SOCK, ["get_property", "pause"])
    return pause is False or pause is True


def _ytdlp_resolve(query: str, *, index: int = 1) -> tuple[str, str] | None:
    """Достать прямую audio-URL + title через yt-dlp (без кривого ytdl:// в mpv)."""
    if not YTDLP.exists():
        return None
    q = (query or "").strip()
    if not q:
        return None
    # ytsearchN:query печатает N строк — берём последнюю (= N-й результат)
    target = q if re.match(r"^https?://", q, re.I) else f"ytsearch{max(1, index)}:{q}"
    base = [
        str(YTDLP),
        "--js-runtimes",
        "node",
        "-f",
        "ba/bestaudio/best",
        "--no-playlist",
        "--no-warnings",
        "--print",
        "%(title)s\t%(url)s",
    ]
    # cookies Firefox сначала — без них YouTube часто режет «бот»
    attempts = [
        base + ["--cookies-from-browser", "firefox", target],
        base + [target],
        base + ["--cookies-from-browser", "chromium", target],
    ]

    for cmd in attempts:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=70,
            )
        except Exception as e:
            log.debug("yt-dlp resolve: %s", e)
            continue
        out = (proc.stdout or "").strip()
        if proc.returncode != 0 or not out:
            err = (proc.stderr or "")[-240:]
            log.info("yt-dlp fail: %s", err)
            continue
        lines = [ln for ln in out.splitlines() if "\t" in ln and "http" in ln]
        if not lines:
            continue
        line = lines[-1]
        title, url = line.split("\t", 1)
        title, url = title.strip(), url.strip()
        if url.startswith("http"):
            return title or q, url
    return None


def _mpv_really_playing(*, wait_sec: float = 6.0) -> bool:
    """Убедиться что не просто title мелькнул, а playback идёт."""
    import time

    deadline = time.time() + wait_sec
    saw_pos = False
    while time.time() < deadline:
        pause = _mpv_ipc_query(MUSIC_SOCK, ["get_property", "pause"])
        if pause is None:
            time.sleep(0.25)
            continue
        if pause is True:
            _mpv_ipc(MUSIC_SOCK, ["set_property", "pause", False])
        pos = _mpv_ipc_query(MUSIC_SOCK, ["get_property", "time-pos"])
        try:
            if pos is not None and float(pos) > 0.2:
                saw_pos = True
                break
        except Exception:
            pass
        time.sleep(0.3)
    return saw_pos and music_is_active()


def play_music(query: str | None = None) -> str:
    """Реально включить звук: yt-dlp → прямой URL → mpv. Не врать «включила»."""
    query = (query or "").strip()
    query = re.sub(r"^[\s.·…\-_,;:!?]+$", "", query).strip()
    if not query or not _looks_like_music(query):
        query = pick_yuna_music(other=False)
    if not query:
        query = DEFAULT_MUSIC_QUERY
    if not YTDLP.exists():
        return "Не установлен yt-dlp — не могу включить музыку."

    stop_music()

    resolved = None
    last_err = ""
    for idx in (1, 2, 3):
        resolved = _ytdlp_resolve(query, index=idx)
        if resolved:
            break
        last_err = f"не нашла поток (попытка {idx})"
    if not resolved:
        return (
            f"Не смогла включить «{query}»: YouTube не отдал звук. "
            f"{last_err}. Обнови yt-dlp или зайди в Firefox на youtube.com."
        )

    title, url = resolved
    try:
        Path(MUSIC_SOCK).unlink(missing_ok=True)
    except Exception:
        pass
    cmd = [
        "mpv",
        "--no-video",
        "--no-terminal",
        "--really-quiet",
        "--force-window=no",
        f"--input-ipc-server={MUSIC_SOCK}",
        "--volume=100",
        url,
    ]
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        log.warning("play_music mpv: %s", e)
        return "Не получилось запустить mpv."

    _save_last_action("music", query=query, title=title)
    if _mpv_really_playing(wait_sec=8.0):
        return f"Включила: {title}"
    stop_music()
    return (
        f"Нашла «{title}», но звук не пошёл (поток оборвался). "
        "Скажи ещё раз — попробую другой ролик."
    )


_BARE_OFF_RE = re.compile(
    r"^(?:(?:юна|yuna|юно)\s+)?"
    r"(?:отключ\w*|выключ\w*|стоп|stop|замолч\w*|выруб\w*)"
    r"(?:\s+(?:это|е[её]|музык\w*|песн\w*|трек\w*|звук\w*))?"
    r"[\s!.…]*$",
    re.I,
)


def wants_bare_off(text: str) -> bool:
    return bool(_BARE_OFF_RE.match((text or "").strip()))


def smart_off(text: str = "") -> str:
    """«отключи» без уточнения: музыка → стоп; никогда не «выключаю ПК»."""
    low = (text or "").lower()
    want_music = bool(re.search(r"музык|песн|трек|звук|е[её]|это", low)) or wants_bare_off(text)
    if want_music or music_is_active() or _load_last_action().get("kind") == "music":
        return stop_music()
    return "Что отключить — музыку? Скажи «отключи музыку». Комп сама не выключаю."


def set_favorite_music(query: str) -> str:
    q = (query or "").strip()
    if not q:
        return "Назови трек — сохраню как любимый."
    try:
        from storage import load_settings, save_settings

        s = load_settings()
        desk = dict(s.get("desktop") or {})
        desk["favorite_music"] = q
        s["desktop"] = desk
        save_settings(s)
    except Exception as e:
        log.warning("save favorite music: %s", e)
        return "Не смогла сохранить в настройках."
    return f"Запомнила любимый трек: {q}"


def open_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return "Нет ссылки."
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    try:
        subprocess.Popen(
            ["xdg-open", u],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        return f"Открыла: {u}"
    except Exception as e:
        return f"Не открылось: {e}"


def open_youtube(query: str | None = None, *, play_audio: bool = True, open_browser: bool = False) -> str:
    """По умолчанию только звук. Браузер — если явно open_browser или «открой ютуб»."""
    q = (query or "").strip()
    parts: list[str] = []
    if play_audio:
        if q:
            parts.append(play_music(q))
        else:
            parts.append(play_yuna_pick(other=False))
    if open_browser:
        if q:
            search = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(q)
            parts.append(open_url(search))
        else:
            parts.append(open_url("https://www.youtube.com"))
    return " ".join(parts) if parts else "Нет запроса."


def resolve_music_intent(text: str) -> dict | None:
    """
    Разобрать фразу про музыку.
    Возвращает {action: play|other|stop|fav, query: str} или None.
    """
    t = (text or "").strip()
    if not t:
        return None
    if wants_bare_off(t) or (
        _STOP_RE.search(t)
        and re.search(r"\b(музык\w*|песн\w*|трек\w*|звук\w*|е[её]|это)\b", t, re.I)
    ):
        return {"action": "stop", "query": ""}
    is_music = bool(_MUSIC_RE.search(t)) or bool(_FAV_MUSIC_RE.search(t))
    # «поставь gawr gura» / «включи sati akura» без слова музыка
    bare_play = bool(
        _PLAY_RE.search(t)
        and re.search(
            r"(?:постав\w*|включ\w*|вруб\w*|запусти\w*|play)\s+.{2,80}",
            t,
            re.I,
        )
        and not re.search(r"обо[ия]|wallpaper|заставк|мышь|управл|minecraft|майн", t, re.I)
    )
    if _OTHER_MUSIC_RE.search(t) or (is_music and _NEXT_RE.search(t) and not _PLAY_RE.search(t)):
        return {"action": "other", "query": ""}
    if (is_music and _PLAY_RE.search(t)) or _FAV_MUSIC_RE.search(t) or bare_play:
        q = _music_query(t)
        if _FAV_MUSIC_RE.search(t) and not q:
            return {"action": "fav", "query": ""}
        if _OTHER_MUSIC_RE.search(t):
            return {"action": "other", "query": ""}
        if not q and bare_play and not is_music:
            # хвост после поставь/включи
            m = re.search(
                r"(?:постав\w*|включ\w*|вруб\w*|запусти\w*|play)\s+(.+)$",
                t,
                re.I,
            )
            if m:
                q = m.group(1).strip()
                q = re.sub(
                    r"\b(?:на\s+ютуб\w*|youtube|ютуб\w*|пожалуйста|плиз)\b",
                    "",
                    q,
                    flags=re.I,
                ).strip()
                q = re.sub(r"^[\s.·…\-_,;:!?]+$", "", q).strip()
        return {"action": "play", "query": q or ""}
    if is_music and _STOP_RE.search(t):
        return {"action": "stop", "query": ""}
    return None


def run_music_intent(intent: dict, *, open_browser: bool = False) -> str:
    action = str(intent.get("action") or "")
    q = str(intent.get("query") or "").strip()
    if action == "stop":
        return stop_music()
    if action == "other":
        return play_other_music()
    if action == "fav":
        return play_yuna_pick(other=False)
    if action == "play":
        if not q:
            return play_yuna_pick(other=False)
        if open_browser:
            return open_youtube(q, play_audio=True, open_browser=True)
        return play_music(q)
    return "Не поняла какую музыку."


def stop_speaking() -> bool:
    """Прерывает текущую озвучку (плеер, играющий файл из voice_out)."""
    try:
        r = subprocess.run(["pkill", "-f", "voice_out"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def stop_all() -> str:
    """Останавливает речь и музыку — «тихо»."""
    spoke = stop_speaking()
    music = _mpv_ipc(MUSIC_SOCK, ["quit"])
    try:
        subprocess.run(["pkill", "-f", f"input-ipc-server={MUSIC_SOCK}"], timeout=5)
    except Exception:
        pass
    return "Поняла, молчу." if (spoke or music) else "И так тихо, хозяин."


def music_control(action: str) -> str:
    mapping = {"pause": "play-pause", "resume": "play-pause", "next": "next", "prev": "previous"}
    pc = mapping.get(action)
    if not pc:
        return ""
    try:
        subprocess.run(["playerctl", pc], timeout=5, capture_output=True, env=_playerctl_env())
        return {"pause": "Поставила на паузу.", "resume": "Продолжаю.", "next": "Следующий трек.", "prev": "Предыдущий трек."}[action]
    except Exception:
        return "Не вышло управлять плеером."


# ── разбор команд ────────────────────────────────────────────────────────
_WALL_RE = re.compile(r"\b(обо[ия]|wallpaper|заставк\w*|фон\w*)\b", re.I)
_DUAL_WALL_RE = re.compile(
    r"(?:два|2)\s*монитор|разн\w+\s+(?:обо|картин|фон)|на\s+кажд\w*\s+монитор|"
    r"разн\w+\s+в\s+(?:два|2)|по\s+монитор",
    re.I,
)
_SAO_RE = re.compile(r"\b(са[оа]|sao|sword\s*art)\b", re.I)
_NOW_PLAYING_RE = re.compile(
    r"(?:"
    r"что\s+(?:я\s+)?(?:слушаю|играет)|что\s+сейчас\s+(?:играет|звучит)|"
    r"now\s*playing|какая\s+песн|"
    r"как\s+(?:она\s+|он\s+|это\s+|эта\s+)?называ\w*|"
    r"послуш\w*|слуш\w*\s+(?:что|это|е[её]|песн|трек|музык)|"
    r"(?:песн\w*|трек\w*|музык\w*)|"
    r"(?:как\s+тебе|нрав\w*|оцен\w*).{0,40}(?:песн|трек|музык|эта|это|е[её])|"
    r"играет\s+(?:сейчас|другая|эта)|сейчас\s+(?:играет|звучит)"
    r")",
    re.I,
)

_PLAY_RE = re.compile(r"\b(включ\w*|постав\w*|вруб\w*|запусти\w*|play|открой|открыть)\b", re.I)
_MUSIC_RE = re.compile(
    r"\b(?:песн\w*|трек\w*|музык\w*|radio|радио|lofi|хит\w*|саундтрек\w*|ost)\b",
    re.I,
)
_FAV_MUSIC_RE = re.compile(
    r"любим\w*\s+(?:песн\w*|трек\w*|музык\w*)|favorite\s+song|"
    r"моя\s+любим\w*|любим\w*(?:\s+пожалуйста)?\s*$",
    re.I,
)
_YT_RE = re.compile(r"\b(youtube|ютуб\w*|ютюб\w*)\b", re.I)
_SAVE_FAV_RE = re.compile(
    r"(?:запомн\w*|сохран\w*|это\s+моя\s+любим)\w*.*(?:песн\w*|трек\w*|музык\w*)|"
    r"(?:любим\w*\s+(?:песн\w*|трек\w*))\s*[:\-]?\s*(.+)$",
    re.I,
)
_STOP_RE = re.compile(r"\b(выключ\w*|останов\w*|стоп|выруб\w*|pause|stop|замолч\w*)\b", re.I)
_NEXT_RE = re.compile(r"\b(следующ\w*|next|дальш\w*|переключ\w*)\b", re.I)
_OTHER_MUSIC_RE = re.compile(
    r"(?:друг\w+|смен\w+|поменя\w+|новый|новую|другие|другую).{0,30}(?:музык|песн|трек)|"
    r"(?:музык|песн|трек).{0,30}(?:друг\w+|смен\w+|поменя\w+)|"
    r"\b(?:смени|поменяй)\s+музык|"
    r"не\s+эт[уо]\s+(?:песн|трек|музык)|что[- ]то\s+друг",
    re.I,
)
_WALL_NOISE = re.compile(
    r"\b(найди|нормальн\w*|для\s+меня|пожалуйста|плиз|давай|поставь?|постав\w*|"
    r"смени|поменяй|разн\w*|в\s+два\s+монитора|на\s+два\s+монитора|"
    r"два\s+монитора|монитора?|и|мне|на\s+комп\w*|компьютер)\b",
    re.I,
)
# Только явное «ещё обои / ещё раз» — НЕ голое «другие/смени» (путало с музыкой)
_AGAIN_RE = re.compile(
    r"^(?:(?:юна|yuna|юно)\s+)?"
    r"(?:давай\s+)?"
    r"(?:ещё\s+раз|еще\s+раз|ещё\s+такие|еще\s+такие|"
    r"ещё\s+обои|еще\s+обои|другие\s+обои|другую\s+заставк\w*|"
    r"смени\s+обои|поменяй\s+обои)"
    r"(?:\s+пожалуйста)?[\s!.…]*$",
    re.I,
)

_ALT_MUSIC_POOL = [
    "anime opening mix",
    "j-pop mix",
    "city pop mix",
    "synthwave radio",
    "vocaloid mix",
    "nightcore mix",
    "osu game music mix",
    "hololive singing mix",
]

# Личный вкус Юны (не копия последнего трека хозяина)
_YUNA_MOOD_POOL = [
    "hololive singing cover mix",
    "cute anime song mix",
    "j-pop chill mix",
    "vocaloid best mix",
    "city pop night drive",
    "anime ending theme mix",
    "vtuber karaoke mix",
    "soft j-rock mix",
]

_STYLE_HINTS = (
    ("hololive", "hololive singing mix"),
    ("gawr gura", "hololive singing mix"),
    ("vtuber", "vtuber karaoke mix"),
    ("vocaloid", "vocaloid mix"),
    ("hatsune", "vocaloid mix"),
    ("anime", "anime opening mix"),
    ("opening", "anime opening mix"),
    ("ending", "anime ending theme mix"),
    ("nightcore", "nightcore mix"),
    ("city pop", "city pop mix"),
    ("j-pop", "j-pop mix"),
    ("jpop", "j-pop mix"),
    ("synthwave", "synthwave radio"),
    ("osu", "osu game music mix"),
    ("sati akura", "sati akura covers mix"),
    ("cover", "anime song cover mix"),
)


def _wall_query(text: str) -> str:
    """Тема для поиска: не вся фраза, а смысл (anime stars и т.п.)."""
    theme = _normalize_theme_query(text)
    if theme and _theme_looks_valid(theme):
        return theme
    m = re.search(r"(?:обо[ия]|wallpaper|заставк\w*|фон\w*)\s*(?:на|с|из|по|со)?\s+(.+)$", text, re.I)
    if m:
        theme = _normalize_theme_query(m.group(1))
        if theme and _theme_looks_valid(theme):
            return theme
    return ""


def _load_taste() -> dict:
    try:
        if _TASTE_PATH.exists():
            data = json.loads(_TASTE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {"heard": [], "yuna_likes": []}


def _save_taste(data: dict) -> None:
    try:
        WALLPAPER_DIR.mkdir(parents=True, exist_ok=True)
        _TASTE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.debug("save taste: %s", e)


def _looks_like_music(q: str, *, artist: str = "", title: str = "") -> bool:
    """Отсечь игровые обзоры/рекламу рюкзаков и прочий мусор из «вкуса»."""
    blob = f"{artist} {title} {q}".lower()
    junk = (
        "рюкзак", "stalzone", "stalcraft", "сталкрафт", "сталзон",
        "обзор", "гайд", "gameplay", "прохожден", "скин", "крафт",
        "unboxing", "распаков", "шмот", "лоут",
    )
    if any(j in blob for j in junk):
        return False
    # одни точки / мусор
    if re.fullmatch(r"[\s.·…\-_/]+", (q or "").strip()):
        return False
    if len(re.sub(r"\W+", "", q or "", flags=re.U)) < 3:
        return False
    return True


def remember_hearing(artist: str = "", title: str = "", query: str = "") -> None:
    """Запомнить что слушает хозяин → вкус Юны (только похожее на музыку)."""
    artist = (artist or "").strip()
    title = (title or "").strip()
    title = re.sub(r"\s*#\S+", "", title).strip()
    title = re.sub(r"\s*-\s*Topic\s*$", "", title, flags=re.I).strip()
    if artist.lower().endswith("- topic"):
        artist = artist[: -len("- topic")].strip(" -")
    q = (query or "").strip()
    if not q:
        if artist and title and artist.lower() not in title.lower():
            q = f"{artist} {title}"
        else:
            q = title or artist
    q = re.sub(r"\s+", " ", q).strip()[:120]
    if len(q) < 3:
        return
    if q.lower() in {x.lower() for x in _ALT_MUSIC_POOL} or "lofi" in q.lower():
        return
    if not _looks_like_music(q, artist=artist, title=title):
        return

    taste = _load_taste()
    heard: list[dict] = list(taste.get("heard") or [])
    key = q.lower()
    found = None
    for h in heard:
        if str(h.get("q") or "").lower() == key:
            found = h
            break
    if found:
        found["n"] = int(found.get("n") or 0) + 1
        found["last"] = __import__("datetime").datetime.now().isoformat(timespec="seconds")
    else:
        heard.append(
            {
                "q": q,
                "artist": artist[:80],
                "title": title[:120],
                "n": 1,
                "last": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
            }
        )
    # вычистить уже накопленный мусор
    heard = [
        h
        for h in heard
        if _looks_like_music(
            str(h.get("q") or ""),
            artist=str(h.get("artist") or ""),
            title=str(h.get("title") or ""),
        )
    ]
    heard.sort(key=lambda h: int(h.get("n") or 0), reverse=True)
    taste["heard"] = heard[:40]

    likes = [
        str(h.get("q"))
        for h in heard
        if int(h.get("n") or 0) >= 2 and h.get("q")
    ][:12]
    # если мало повторных — взять топ услышанного всё равно, но только music-like
    if not likes:
        likes = [str(h.get("q")) for h in heard[:5] if h.get("q")]
    taste["yuna_likes"] = likes[-20:]
    _save_taste(taste)


def note_playing_from_meta(meta: str) -> None:
    """meta вида 'Artist — Title (firefox)'."""
    m = (meta or "").strip()
    if not m or m.startswith("Метаданные") or m.startswith("Сейчас ничего"):
        return
    m = re.sub(r"\s*\((?:firefox|chrome|chromium|mpv|spotify)\)\s*$", "", m, flags=re.I)
    m = re.sub(r"^Сейчас играет:\s*", "", m, flags=re.I)
    m = re.sub(r"^На паузе:\s*", "", m, flags=re.I)
    if " — " in m:
        artist, title = m.split(" — ", 1)
    elif " - " in m:
        artist, title = m.split(" - ", 1)
    else:
        artist, title = "", m
    remember_hearing(artist.strip(), title.strip())


def _resolved_favorite() -> str | None:
    """Явно заданный любимый (не lofi-заглушка)."""
    fav = str(_desktop_settings().get("favorite_music") or "").strip()
    if not fav:
        return None
    if fav.lower() in {DEFAULT_MUSIC_QUERY.lower(), "lofi", "lofi hip hop"}:
        return None
    return fav


def _recent_heard_queries() -> set[str]:
    """Точные названия услышанного — их нельзя снова пихать в поиск как «своё»."""
    taste = _load_taste()
    out: set[str] = set()
    for h in taste.get("heard") or []:
        q = str(h.get("q") or "").strip().lower()
        if q:
            out.add(q)
        a = str(h.get("artist") or "").strip().lower()
        t = str(h.get("title") or "").strip().lower()
        if a and t:
            out.add(f"{a} {t}")
            out.add(t)
    for x in taste.get("yuna_likes") or []:
        s = str(x or "").strip().lower()
        if s:
            out.add(s)
    last = str(_load_last_action().get("query") or "").strip().lower()
    if last:
        out.add(last)
    return out


def _artist_mix_query(artist: str) -> str | None:
    a = re.sub(r"\s+", " ", (artist or "").strip())
    a = re.sub(r"\s*-\s*topic\s*$", "", a, flags=re.I).strip(" -")
    low = a.lower()
    if len(a) < 2:
        return None
    # каналы-клипы / мусорные «артисты»
    if any(j in low for j in ("clip", "clips", "channel", "vevo", "official video")):
        return None
    if not _looks_like_music(a):
        return None
    # случайные ники с иероглифами без смысла
    if re.search(r"[\u4e00-\u9fff]", a) and len(a) < 10:
        return None
    return f"{a} mix"


def _style_queries_from_taste() -> list[str]:
    """
    Из того, что слушает хозяин → стили/микс, а НЕ точное название трека.
    Юна ставит «что ей нравится в этом вайбе».
    """
    taste = _load_taste()
    scores: dict[str, float] = {}

    def bump(q: str, w: float = 1.0) -> None:
        q = re.sub(r"\s+", " ", (q or "").strip())
        if len(q) < 4 or not _looks_like_music(q):
            return
        scores[q] = scores.get(q, 0.0) + w

    for h in taste.get("heard") or []:
        n = float(h.get("n") or 1)
        artist = str(h.get("artist") or "")
        title = str(h.get("title") or "")
        blob = f"{artist} {title} {h.get('q') or ''}".lower()
        for needle, style in _STYLE_HINTS:
            if needle in blob:
                bump(style, n + 0.5)
        am = _artist_mix_query(artist)
        if am:
            bump(am, n)

    for i, mood in enumerate(_YUNA_MOOD_POOL):
        bump(mood, 0.35 + 0.02 * (len(_YUNA_MOOD_POOL) - i))

    for alt in _ALT_MUSIC_POOL:
        bump(alt, 0.2)

    forbidden = _recent_heard_queries()
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    out: list[str] = []
    for q, _w in ranked:
        if q.lower() in forbidden:
            continue
        out.append(q)

    seen: set[str] = set()
    uniq: list[str] = []
    for q in out:
        k = q.lower()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(q)
    return uniq or list(_YUNA_MOOD_POOL)


def _next_alt_music_query() -> str:
    last = _load_last_action()
    prev = str(last.get("query") or "").strip().lower()
    pool = list(_YUNA_MOOD_POOL) + list(_ALT_MUSIC_POOL)
    for q in pool:
        if q.lower() != prev:
            return q
    return _YUNA_MOOD_POOL[0]


def play_other_music() -> str:
    """«Другую музыку» — следующий трек по вкусу Юны (не копия последнего)."""
    return play_yuna_pick(other=True)


def pick_yuna_music(*, other: bool = False) -> str:
    """
    Что Юна ставит «своё»: микс/вайб по вкусу,
    никогда не копирует точный последний трек в поиск.
    """
    import random

    last = str(_load_last_action().get("query") or "").strip().lower()
    forbidden = _recent_heard_queries()
    pool = [q for q in _style_queries_from_taste() if q.lower() not in forbidden]

    if other:
        pool = [q for q in pool if q.lower() != last]
    if not pool:
        pool = [q for q in (_YUNA_MOOD_POOL + _ALT_MUSIC_POOL) if q.lower() != last]

    fav = _resolved_favorite()
    if fav and fav.lower() not in forbidden and (not other or fav.lower() != last):
        if pool and random.random() < 0.55:
            return random.choice(pool[:6])
        return fav

    if not pool:
        return _next_alt_music_query()

    top = pool[:8]
    if other and len(top) > 1:
        return random.choice(top[1:] if top[0].lower() == last else top)
    weights = [max(1, 9 - i) for i in range(len(top))]
    return random.choices(top, weights=weights, k=1)[0]


def play_yuna_pick(*, other: bool = False) -> str:
    q = pick_yuna_music(other=other)
    return play_music(q)


def _music_query(text: str) -> str:
    # «любимую песню» без названия → пусто → play_music возьмёт favorite
    if _FAV_MUSIC_RE.search(text) and not re.search(
        r"любим\w*\s+(?:песн\w*|трек\w*|музык\w*)\s+\S+", text, re.I
    ):
        return ""
    # «другую музыку» — не использовать как поисковый хвост
    if _OTHER_MUSIC_RE.search(text):
        return ""
    m = re.search(r"(?:музык\w*|песн\w*|трек\w*|music|song)\s*(.*)$", text, re.I)
    tail = (m.group(1).strip() if m else "")
    tail = re.sub(r"^\s*(?:по|про|типа|такую как|вроде)\s+", "", tail, flags=re.I)
    tail = re.sub(
        r"\b(?:на\s+ютуб\w*|youtube|ютуб\w*|ютюб\w*|друг\w+|смен\w+|поменя\w*|любим\w*)\b",
        "",
        tail,
        flags=re.I,
    ).strip()
    # «поставь музыку..» → хвост «..» не запрос
    tail = re.sub(r"^[\s.·…\-_,;:!?]+$", "", tail).strip()
    if not tail:
        m2 = re.search(r"(?:включ\w*|постав\w*|вруб\w*|запусти\w*|play)\s+(.+)$", text, re.I)
        if m2 and not _MUSIC_RE.search(m2.group(1)):
            tail = m2.group(1).strip()
            tail = re.sub(r"^[\s.·…\-_,;:!?]+$", "", tail).strip()
    if not tail and _YT_RE.search(text):
        m3 = re.search(
            r"(?:ютуб\w*|youtube|ютюб\w*)\s+(?:и\s+)?(?:поставь|включи|play)?\s*(.+)$",
            text,
            re.I,
        )
        if m3:
            tail = m3.group(1).strip()
            tail = re.sub(r"^[\s.·…\-_,;:!?]+$", "", tail).strip()
    return tail


def _repeat_last_action() -> str | None:
    """«давай ещё» — повтор последнего действия (обычно обои)."""
    last = _load_last_action()
    kind = str(last.get("kind") or "")
    if kind == "wallpaper":
        mon = last.get("monitor_index")
        mon_i = int(mon) if isinstance(mon, int) or (isinstance(mon, str) and str(mon).isdigit()) else None
        return set_wallpaper(
            str(last.get("query") or "") or None,
            dual_different=bool(last.get("dual_different")) and mon_i is None,
            monitor_index=mon_i,
        )
    if kind == "music":
        return play_music(str(last.get("query") or "") or None)
    dual = len(list_monitors()) >= 2
    return set_wallpaper(None, dual_different=dual)


def handle_action(command: str) -> str | None:
    """Возвращает ответ, если команда — действие на ПК; иначе None (тогда Cursor)."""
    if not command or not command.strip():
        return None
    text = command.strip()

    # «отключи» / «стоп» без уточнения — музыка, НЕ выключение ПК
    if wants_bare_off(text) or (
        _STOP_RE.search(text)
        and re.search(r"\b(музык\w*|песн\w*|трек\w*|звук\w*|е[её]|это)\b", text, re.I)
    ):
        return smart_off(text)

    # Мышь/управление — не перехватывать музыкой (пусть desktop_agent сделает оба)
    if re.search(
        r"управляй|мышь|клавиатур|открой\s+(?:перв|окн)|играй\s+за\s+меня|"
        r"возьми\s+управлени|води\s+мышь|control\s+my",
        text,
        re.I,
    ):
        return None

    if _AGAIN_RE.match(text):
        # «ещё обои» всегда обои, даже если last_action был music
        if re.search(r"обои|заставк|wallpaper|фон\w*", text, re.I):
            last = _load_last_action()
            q = str(last.get("query") or "") if last.get("kind") == "wallpaper" else ""
            return set_wallpaper(q or None, dual_different=False)
        return _repeat_last_action()

    if _WALL_RE.search(text):
        mon = _monitor_index(text)
        dual = bool(_DUAL_WALL_RE.search(text)) and mon is None
        return set_wallpaper(
            _wall_query(text) or None,
            dual_different=dual,
            monitor_index=mon,
        )

    # сохранить любимый трек: «запомни любимую песню X» / «любимая песня: X»
    sm = _SAVE_FAV_RE.search(text)
    if sm and sm.lastindex:
        name = (sm.group(sm.lastindex) or "").strip()
        if name and len(name) > 1:
            return set_favorite_music(name)

    if _YT_RE.search(text):
        only_open = bool(re.search(r"только\s+открой|без\s+музык|просто\s+открой", text, re.I))
        want_music = bool(_MUSIC_RE.search(text) or _FAV_MUSIC_RE.search(text) or _PLAY_RE.search(text))
        bare_open = bool(re.search(r"^\s*(?:юна\s+)?(?:открой|открыть)\s+(?:ютуб\w*|youtube|ютюб\w+)\s*$", text, re.I))
        if only_open or bare_open:
            return open_url("https://www.youtube.com")
        q = _music_query(text)
        q = re.sub(r"\b(?:только\s+открой|без\s+музык|просто\s+открой)\b", "", q or "", flags=re.I).strip()
        if _FAV_MUSIC_RE.search(text) and not q:
            q = ""
        # ютуб + музыка: звук обязателен; вкладку — только если «открой»
        want_tab = bool(re.search(r"открой|открыть|покажи", text, re.I))
        if want_music or q is not None:
            intent = resolve_music_intent(text) or {"action": "play", "query": q or ""}
            if intent.get("action") == "stop":
                return stop_music()
            return run_music_intent(intent, open_browser=want_tab)
        return open_url("https://www.youtube.com")

    intent = resolve_music_intent(text)
    if intent:
        return run_music_intent(intent, open_browser=False)

    return None
