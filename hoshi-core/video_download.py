#!/usr/bin/env python3
"""Скачивание видео по URL через yt-dlp (любой сайт, несколько стратегий)."""
from __future__ import annotations

import hashlib
import html
import json
import logging
import random
import re
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

from config import MEDIA_DIR, ROOT, USERBOT_MAX_FILE_BYTES, VIDEO_DOWNLOAD_TIMEOUT

log = logging.getLogger("hoshi.video")

_download_cancel = False


def request_download_cancel() -> None:
    global _download_cancel
    _download_cancel = True


def clear_download_cancel() -> None:
    global _download_cancel
    _download_cancel = False


def is_download_cancelled() -> bool:
    return _download_cancel


MAX_VIDEO_BYTES = USERBOT_MAX_FILE_BYTES
_MAX_G = max(1, MAX_VIDEO_BYTES // (1024 * 1024 * 1024))

VIDEO_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)

EXPLICIT_DOWNLOAD_INTENT_RE = re.compile(
    r"(?:"
    r"скачай|скинь|отправь|кидай|send|download|"
    r"лови|кинь|"
    r"покажи\s+(?:видео|ролик|клип|файл)|"
    r"дай\s+(?:видео|ролик|клип|файл)|"
    r"скинь\s+(?:видео|ролик|клип|файл)|"
    r"просит\s+видео|хочу\s+видео"
    r")",
    re.I,
)

VIDEO_NO_DOWNLOAD_RE = re.compile(
    r"(?:"
    r"не\s+качай|"
    r"не\s+скачивай|"
    r"не\s+кидай|"
    r"не\s+кинь|"
    r"не\s+надо\s+качать|"
    r"без\s+скачивания|"
    r"без\s+файла|"
    r"только\s+текст|"
    r"не\s+отправляй\s+видео"
    r")",
    re.I,
)

VIDEO_ANALYSIS_RE = re.compile(
    r"(?:"
    r"расскажи|скажи(?:те)?|опиши|объясни|"
    r"о\s+ч[её]м|что\s+(?:говорится|происходит|там|за)|"
    r"кто\s+(?:на|в)|"
    r"разбор|суть|анализ"
    r")",
    re.I,
)

VIDEO_DENY_OR_QUESTION_RE = re.compile(
    r"(?:"
    r"какое\s+(?:ещё|еще|это)?\s*видео|"
    r"какое\s+(?:ещё|еще)\s+видел|"
    r"какое\s+видео|"
    r"зачем\s+(?:ещё|еще|это)?\s*видео|"
    r"почему\s+(?:ещё|еще|это)?\s*видео|"
    r"что\s+за\s+видео|"
    r"я\s+не\s+просил(?:\s+видео)?|"
    r"не\s+просил(?:\s+видео)?|"
    r"не\s+надо\s+видео|"
    r"не\s+качай\s+видео|"
    r"зачем\s+.*\s+кидаешь\s+видео"
    r")",
    re.I,
)

# Backward-compatible alias
DOWNLOAD_INTENT_RE = EXPLICIT_DOWNLOAD_INTENT_RE


def is_video_denial_or_question(text: str) -> bool:
    """Вопрос или отказ про видео — не запрос на скачивание."""
    t = (text or "").strip()
    if not t:
        return False
    if VIDEO_DENY_OR_QUESTION_RE.search(t):
        return True
    if t.endswith("?") and re.search(r"видео|video", t, re.I):
        if re.search(r"какое|зачем|почему|что\s+за|откуда", t, re.I):
            return True
    return False

_VIDEO_PAGE_RE = re.compile(
    r"hanime1\.me/watch|rule34video\.com/video|redtube\.com/\d+|"
    r"b23\.tv|bilibili\.com/video|avbebe\.com/archives",
    re.I,
)

_YT_DLP_FORMAT = (
    f"bestvideo[filesize<={_MAX_G}G]+bestaudio/"
    f"best[filesize<={_MAX_G}G]/best"
)
_AVBEBE_YTDLP_FORMAT = "best[height<=480]/best[height<=720]/best/best"

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

_DIRECT_MEDIA_RE = (
    re.compile(r'"(?:file|src|video_url|contentUrl)"\s*:\s*"(https?://[^"]+)"', re.I),
    re.compile(r"https?://[^\s\"'<>]+\.m3u8[^\s\"'<>]*", re.I),
    re.compile(r"https?://[^\s\"'<>]+\.mp4[^\s\"'<>]*", re.I),
)

_AVBEBE_HOST_RE = re.compile(r"(?:^|\.)avbebe\.(?:com|love|net)$", re.I)
_AVBEBE_ARTICLE_RE = re.compile(r"https?://[^/]+/archives/\d+", re.I)
_AVBEBE_FLOWPLAYER_RE = re.compile(
    r"""data-item=(?:"(\{[^"]+\})"|'(\{[^']+\})')""",
    re.I,
)
_AVBEBE_M3U8_RE = re.compile(
    r"https?://[^\s\"'<>]+\.m3u8(?:\?[^\s\"'<>]*)?",
    re.I,
)

_HANIME_HOST_RE = re.compile(r"(?:^|\.)hanime1\.me$", re.I)

_TG_POST_RE = re.compile(
    r"https?://(?:t\.me|telegram\.me)/(?:c/(\d+)/|([^/?#]+)/)(\d+)",
    re.I,
)
_TG_EMBED_VIDEO_RE = re.compile(
    r'<video[^>]+src="([^"]+)"',
    re.I,
)
_TG_EMBED_THUMB_RE = re.compile(
    r"tgme_widget_message_video_thumb[^>]+background-image:url\('([^']+)'\)",
    re.I,
)

_BILIBILI_HOST_RE = re.compile(r"(?:^|\.)bilibili\.com$|(?:^|\.)b23\.tv$", re.I)
_BILIBILI_BV_RE = re.compile(r"/video/(BV[a-zA-Z0-9]+)", re.I)
_BILIBILI_PLAYINFO_RE = re.compile(
    r"window\.__playinfo__\s*=\s*(\{.+?\})\s*</script>",
    re.I | re.S,
)
_BILIBILI_BVID_RE = re.compile(r'"bvid"\s*:\s*"(BV[a-zA-Z0-9]+)"', re.I)
_BILIBILI_CID_RE = re.compile(r'"cid"\s*:\s*(\d+)')
_CURL_IMPERSONATE = "chrome131"
_YTDLP_IMPERSONATE = "Chrome-131"
_BILIBILI_REFERER = "https://www.bilibili.com/"
_WBI_KEY_CACHE: dict[str, float | str] = {"key": "", "ts": 0.0}
_WBI_KEY_CACHE_TIMEOUT = 3600
_WBI_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


def _is_telegram_url(url: str) -> bool:
    return bool(_TG_POST_RE.match((url or "").split("?")[0].strip()))


def _telegram_embed_url(url: str) -> str:
    base = (url or "").split("?")[0].strip()
    return f"{base}?embed=1&single=1"


def _normalize_video_url(url: str) -> str:
    """b23.tv → BV-страница; avbebe http → https — до любых yt-dlp попыток."""
    if _is_bilibili_url(url):
        return _bilibili_canonical_url(url)
    if _is_avbebe_url(url):
        return _avbebe_normalize(url)
    return url


def _yt_dlp_bin() -> str:
    venv = ROOT / ".venv" / "bin" / "yt-dlp"
    return str(venv) if venv.exists() else "yt-dlp"


def _impersonate_args() -> list[str]:
    """--impersonate только если curl_cffi доступен для yt-dlp."""
    try:
        import curl_cffi  # noqa: F401

        proc = subprocess.run(
            [_yt_dlp_bin(), "--list-impersonate-targets"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode != 0 or "curl_cffi" not in proc.stdout:
            return []
        best_chrome = ""
        best_ver = -1
        for line in proc.stdout.splitlines():
            parts = line.split()
            if not parts or not parts[0].startswith("Chrome-"):
                continue
            if "curl_cffi" not in line:
                continue
            try:
                ver = int(parts[0].split("-", 1)[1])
            except (IndexError, ValueError):
                ver = 0
            if ver > best_ver:
                best_ver = ver
                best_chrome = parts[0]
        if best_chrome:
            return ["--impersonate", best_chrome]
    except Exception:
        pass
    return []


def _curl_cffi_get(url: str, *, referer: str | None = None) -> str:
    from curl_cffi import requests

    headers = {"Referer": referer or url, "User-Agent": _USER_AGENT}
    resp = requests.get(url, impersonate=_CURL_IMPERSONATE, headers=headers, timeout=60)
    resp.raise_for_status()
    return resp.text


def _curl_cffi_download(
    url: str,
    dest: Path,
    *,
    referer: str,
    on_progress=None,
) -> bool:
    from curl_cffi import requests

    headers = {"Referer": referer, "User-Agent": _USER_AGENT}
    try:
        resp = requests.get(
            url,
            impersonate=_CURL_IMPERSONATE,
            headers=headers,
            timeout=VIDEO_DOWNLOAD_TIMEOUT,
            stream=True,
        )
        resp.raise_for_status()
        total = int(resp.headers.get("content-length") or 0)
        downloaded = 0
        with dest.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue
                fh.write(chunk)
                downloaded += len(chunk)
                if on_progress:
                    on_progress(downloaded, total, None)
        return dest.exists() and dest.stat().st_size > 1024
    except Exception as e:
        log.debug("curl_cffi download failed %s: %s", url[:80], e)
        return False


def _resolve_redirect_url(url: str) -> str:
    """b23.tv и прочие редиректы → канонический URL."""
    try:
        from curl_cffi import requests

        resp = requests.get(
            url,
            impersonate=_CURL_IMPERSONATE,
            allow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
            timeout=30,
        )
        return str(resp.url)
    except Exception:
        pass
    try:
        req = Request(url, headers={"User-Agent": _USER_AGENT})
        with urlopen(req, timeout=30) as resp:
            return resp.geturl()
    except Exception as e:
        log.debug("redirect resolve failed %s: %s", url, e)
        return url


def _is_hanime_url(url: str) -> bool:
    host = urlparse(url).netloc.split(":")[0]
    return bool(_HANIME_HOST_RE.search(host))


def _hanime_stream_urls(page_url: str) -> list[str]:
    """mp4 с hembed — 720p выше 480p."""
    raw = _page_direct_urls(page_url)
    seen: set[str] = set()
    urls: list[str] = []
    for u in raw:
        u = html.unescape(u).replace("&amp;", "&")
        if u in seen or not u.endswith(".mp4") and ".mp4?" not in u:
            continue
        seen.add(u)
        urls.append(u)
    urls.sort(
        key=lambda u: (
            0 if "1080" in u else 1 if "720" in u else 2 if "480" in u else 3,
            u,
        )
    )
    return urls


def _download_hanime(
    url: str,
    vid: str,
    *,
    on_progress=None,
) -> Path | None:
    """hanime1.me: Cloudflare — curl_cffi + прямой mp4 со страницы."""
    page_url = url.split("#")[0]
    referer = page_url
    out_mp4 = MEDIA_DIR / f"video_{vid}.mp4"
    for stream in _hanime_stream_urls(page_url):
        out_mp4.unlink(missing_ok=True)
        if _curl_cffi_download(stream, out_mp4, referer=referer, on_progress=on_progress):
            hit = _accept_video(out_mp4)
            if hit:
                log.info("hanime ok %s -> %s", page_url, hit.name)
                return hit
    out_tpl = str(MEDIA_DIR / f"video_{vid}.%(ext)s")
    extra = [
        "--add-headers",
        f"Referer:{referer}",
        "--add-headers",
        f"User-Agent:{_USER_AGENT}",
    ]
    if _run_yt_dlp(page_url, out_tpl, extra + (_impersonate_args() or [])):
        hit = _find_output(vid)
        if hit:
            log.info("hanime ok via yt-dlp %s -> %s", page_url, hit.name)
            return hit
    out_mp4.unlink(missing_ok=True)
    return None


def _is_bilibili_url(url: str) -> bool:
    host = urlparse(url).netloc.split(":")[0]
    return bool(_BILIBILI_HOST_RE.search(host))


def _bilibili_canonical_url(url: str) -> str:
    resolved = _resolve_redirect_url(url)
    m = _BILIBILI_BV_RE.search(resolved)
    if m:
        return f"https://www.bilibili.com/video/{m.group(1)}"
    bm = _BILIBILI_BVID_RE.search(resolved)
    if bm:
        return f"https://www.bilibili.com/video/{bm.group(1)}"
    return resolved


def _bilibili_wbi_key() -> str:
    """Ключ WBI из /x/web-interface/nav — для подписанного playurl API."""
    cached = _WBI_KEY_CACHE.get("key", "")
    if cached and time.time() < float(_WBI_KEY_CACHE.get("ts", 0)) + _WBI_KEY_CACHE_TIMEOUT:
        return str(cached)
    try:
        from curl_cffi import requests

        resp = requests.get(
            "https://api.bilibili.com/x/web-interface/nav",
            impersonate=_CURL_IMPERSONATE,
            headers={"Referer": _BILIBILI_REFERER, "User-Agent": _USER_AGENT},
            timeout=30,
        )
        resp.raise_for_status()
        wbi = (resp.json().get("data") or {}).get("wbi_img") or {}
        img = wbi.get("img_url", "").rpartition("/")[2].partition(".")[0]
        sub = wbi.get("sub_url", "").rpartition("/")[2].partition(".")[0]
        lookup = img + sub
        if len(lookup) < 64:
            return ""
        key = "".join(lookup[i] for i in _WBI_MIXIN_KEY_ENC_TAB)[:32]
        _WBI_KEY_CACHE.update({"key": key, "ts": time.time()})
        return key
    except Exception as e:
        log.debug("bilibili wbi key failed: %s", e)
        return ""


def _bilibili_sign_wbi(params: dict) -> dict:
    key = _bilibili_wbi_key()
    if not key:
        return params
    signed = dict(params)
    signed["wts"] = round(time.time())
    filtered = {
        k: "".join(c for c in str(v) if c not in "!'()*")
        for k, v in sorted(signed.items())
    }
    query = urlencode(filtered)
    signed["w_rid"] = hashlib.md5(f"{query}{key}".encode()).hexdigest()
    return signed


def _extract_json_object(text: str, marker: str) -> dict | None:
    """JSON после marker — подсчёт скобок, без хрупкого regex."""
    idx = text.find(marker)
    if idx < 0:
        return None
    start = text.find("{", idx)
    if start < 0:
        return None
    depth = 0
    for i in range(start, min(len(text), start + 5_000_000)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _extract_bilibili_meta(html_text: str, page_url: str) -> tuple[str, int] | None:
    """bvid + cid со страницы или INITIAL_STATE."""
    state = _extract_json_object(html_text, "window.__INITIAL_STATE__")
    if state:
        try:
            bvid = (
                state.get("videoData", {}).get("bvid")
                or state.get("bvid")
                or ""
            )
            pages = state.get("videoData", {}).get("pages") or state.get("pages") or []
            cid = pages[0].get("cid") if pages else 0
            if not cid:
                cid = state.get("cid") or state.get("videoData", {}).get("cid") or 0
            if bvid and cid:
                return str(bvid), int(cid)
        except (TypeError, ValueError, IndexError):
            pass
    m = _BILIBILI_BV_RE.search(page_url)
    bvid = m.group(1) if m else None
    if not bvid:
        bm = _BILIBILI_BVID_RE.search(html_text)
        bvid = bm.group(1) if bm else None
    cm = _BILIBILI_CID_RE.search(html_text)
    if bvid and cm:
        return bvid, int(cm.group(1))
    return None


def _dash_best_streams(dash: dict, *, prefer_mid: bool = True) -> tuple[str, str] | None:
    videos = dash.get("video") or []
    if not videos:
        return None
    if prefer_mid and len(videos) > 1:
        # Среднее качество — быстрее качается, для TG хватает.
        sorted_v = sorted(videos, key=lambda x: x.get("bandwidth") or 0)
        best_v = sorted_v[len(sorted_v) // 2]
    else:
        best_v = max(videos, key=lambda x: x.get("bandwidth") or 0)
    v_url = best_v.get("baseUrl") or (best_v.get("backupUrl") or [None])[0]
    if not v_url:
        return None
    a_url = ""
    audios = dash.get("audio") or []
    if audios:
        best_a = max(audios, key=lambda x: x.get("bandwidth") or 0)
        a_url = best_a.get("baseUrl") or (best_a.get("backupUrl") or [None])[0] or ""
    return v_url, a_url


def _parse_bilibili_playinfo(html_text: str) -> tuple[str, str] | None:
    """Из __playinfo__ на странице — лучшие video/audio dash URL."""
    data = _extract_json_object(html_text, "window.__playinfo__")
    if not data:
        m = _BILIBILI_PLAYINFO_RE.search(html_text)
        if not m:
            return None
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            return None
    dash = (data.get("data") or {}).get("dash") or data.get("dash") or {}
    return _dash_best_streams(dash)


def _fetch_bilibili_dash_api(bvid: str, cid: int) -> tuple[str, str] | None:
    """playurl API через curl_cffi + WBI — обход 412 yt-dlp."""
    try:
        from curl_cffi import requests
    except ImportError:
        return None
    headers = {"Referer": _BILIBILI_REFERER, "User-Agent": _USER_AGENT}
    apis = (
        "https://api.bilibili.com/x/player/wbi/playurl",
        "https://api.bilibili.com/x/player/playurl",
    )
    # qn: 80=1080p, 64=720p, 32=480p — пробуем от среднего к лучшему.
    for qn in (64, 80, 32, 16):
        base_params = {
            "bvid": bvid,
            "cid": cid,
            "qn": qn,
            "fnval": 4048,
            "fnver": 0,
            "fourk": 1,
        }
        for api in apis:
            params = _bilibili_sign_wbi(base_params) if "wbi" in api else base_params
            try:
                resp = requests.get(
                    api,
                    params=params,
                    impersonate=_CURL_IMPERSONATE,
                    headers=headers,
                    timeout=30,
                )
                resp.raise_for_status()
                payload = resp.json()
                if payload.get("code") != 0:
                    log.debug(
                        "bilibili api code %s qn=%s api=%s",
                        payload.get("code"), qn, api.rpartition("/")[2],
                    )
                    continue
                dash = (payload.get("data") or {}).get("dash") or {}
                hit = _dash_best_streams(dash)
                if hit:
                    return hit
            except Exception as e:
                log.debug(
                    "bilibili api failed %s/%s qn=%s api=%s: %s",
                    bvid, cid, qn, api.rpartition("/")[2], e,
                )
    return None


def _download_stream_ffmpeg(stream_url: str, dest: Path, *, referer: str) -> bool:
    """Запасной способ: ffmpeg + Referer, если curl_cffi не берёт CDN."""
    header_str = f"Referer: {referer}\r\nUser-Agent: {_USER_AGENT}\r\n"
    cmd = [
        "ffmpeg", "-y",
        "-headers", header_str,
        "-i", stream_url,
        "-c", "copy",
        str(dest),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=VIDEO_DOWNLOAD_TIMEOUT)
        return proc.returncode == 0 and dest.exists() and dest.stat().st_size > 1024
    except Exception as e:
        log.debug("ffmpeg stream download failed: %s", e)
        dest.unlink(missing_ok=True)
        return False


def _download_dash_segment(
    url: str,
    dest: Path,
    *,
    referer: str,
    on_progress=None,
) -> bool:
    if _curl_cffi_download(url, dest, referer=referer, on_progress=on_progress):
        return True
    return _download_stream_ffmpeg(url, dest, referer=referer)


def _merge_av_files(video: Path, audio: Path, out: Path) -> bool:
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video),
        "-i", str(audio),
        "-c", "copy",
        "-movflags", "+faststart",
        str(out),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=600)
        return proc.returncode == 0 and out.exists() and out.stat().st_size > 1024
    except Exception as e:
        log.debug("ffmpeg merge failed: %s", e)
        return False


def _save_bilibili_cover(html_text: str, vid: str) -> None:
    """Обложка из INITIAL_STATE — для превью в Telegram."""
    state = _extract_json_object(html_text, "window.__INITIAL_STATE__")
    if not state:
        return
    try:
        pic = state.get("videoData", {}).get("pic") or state.get("pic") or ""
    except (TypeError, AttributeError):
        return
    if not pic or not pic.startswith("http"):
        return
    try:
        from curl_cffi import requests

        resp = requests.get(
            pic,
            impersonate=_CURL_IMPERSONATE,
            headers={"Referer": "https://www.bilibili.com/", "User-Agent": _USER_AGENT},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.content
        if len(data) > 100:
            (MEDIA_DIR / f"video_{vid}.jpg").write_bytes(data)
    except Exception as e:
        log.debug("bilibili cover save failed: %s", e)


def _download_bilibili_dash(
    v_url: str,
    a_url: str,
    *,
    vid: str,
    referer: str,
    page_url: str,
    via: str,
    on_progress=None,
) -> Path | None:
    tmp_v = MEDIA_DIR / f"video_{vid}_v.m4s"
    tmp_a = MEDIA_DIR / f"video_{vid}_a.m4s"
    out_mp4 = MEDIA_DIR / f"video_{vid}.mp4"
    try:
        if not _download_dash_segment(v_url, tmp_v, referer=referer, on_progress=on_progress):
            return None
        if a_url and _download_dash_segment(a_url, tmp_a, referer=referer):
            if _merge_av_files(tmp_v, tmp_a, out_mp4):
                log.info("bilibili ok via %s %s", via, page_url)
                return _accept_video(out_mp4)
        try:
            proc = subprocess.run(
                [
                    "ffmpeg", "-y", "-i", str(tmp_v),
                    "-c", "copy", "-movflags", "+faststart", str(out_mp4),
                ],
                capture_output=True,
                timeout=600,
            )
            if proc.returncode == 0:
                hit = _accept_video(out_mp4)
                if hit:
                    log.info("bilibili ok via %s video-only %s", via, page_url)
                    return hit
        except Exception as e:
            log.debug("bilibili video-only remux failed: %s", e)
    finally:
        tmp_v.unlink(missing_ok=True)
        tmp_a.unlink(missing_ok=True)
    return None


def _download_bilibili(
    url: str,
    out_tpl: str,
    *,
    on_progress=None,
) -> Path | None:
    """Bilibili: playurl API / HTML __playinfo__ + curl_cffi (обход 412 yt-dlp)."""
    try:
        import curl_cffi  # noqa: F401
    except ImportError:
        return None

    page_url = _normalize_video_url(url)
    try:
        html_text = _curl_cffi_get(page_url, referer=_BILIBILI_REFERER)
    except Exception as e:
        log.debug("bilibili page fetch failed %s: %s", page_url, e)
        return None

    referer = _BILIBILI_REFERER
    vid = Path(out_tpl.replace("%(ext)s", "mp4")).stem.replace("video_", "")
    streams: tuple[str, str] | None = None
    via = ""

    meta = _extract_bilibili_meta(html_text, page_url)
    if meta:
        bvid, cid = meta
        streams = _fetch_bilibili_dash_api(bvid, cid)
        if streams:
            via = "api"

    if not streams:
        streams = _parse_bilibili_playinfo(html_text)
        if streams:
            via = "html"

    if not streams:
        log.debug("bilibili streams not found for %s", page_url)
        return None

    _save_bilibili_cover(html_text, vid)
    v_url, a_url = streams
    return _download_bilibili_dash(
        v_url, a_url, vid=vid, referer=referer, page_url=page_url, via=via,
        on_progress=on_progress,
    )


def _download_bilibili_via_html(url: str, out_tpl: str) -> Path | None:
    return _download_bilibili(url, out_tpl)


def _download_bilibili_audio_only(url: str, *, dest: Path) -> Path | None:
    """Только аудиодорожка Bilibili — без скачивания полного видео."""
    try:
        import curl_cffi  # noqa: F401
    except ImportError:
        return None

    page_url = _normalize_video_url(url)
    try:
        html_text = _curl_cffi_get(page_url, referer=_BILIBILI_REFERER)
    except Exception as e:
        log.debug("bilibili audio page fetch failed %s: %s", page_url, e)
        return None

    streams: tuple[str, str] | None = None
    meta = _extract_bilibili_meta(html_text, page_url)
    if meta:
        bvid, cid = meta
        streams = _fetch_bilibili_dash_api(bvid, cid)
    if not streams:
        streams = _parse_bilibili_playinfo(html_text)
    if not streams:
        return None

    _v_url, a_url = streams
    if not a_url:
        return None

    vid = uuid.uuid4().hex[:8]
    tmp_a = MEDIA_DIR / f"audio_{vid}_a.m4s"
    try:
        if not _download_dash_segment(a_url, tmp_a, referer=_BILIBILI_REFERER):
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "ffmpeg", "-y", "-i", str(tmp_a),
            "-vn", "-acodec", "libmp3lame", "-q:a", "4", str(dest),
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=300)
        if proc.returncode == 0 and dest.exists() and dest.stat().st_size > 1024:
            log.info("bilibili audio ok %s -> %s", page_url, dest.name)
            return dest
    except Exception as e:
        log.debug("bilibili audio extract failed %s: %s", page_url, e)
    finally:
        tmp_a.unlink(missing_ok=True)
    return None


def extract_video_urls(text: str) -> list[str]:
    seen: list[str] = []
    for m in VIDEO_URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(".,;:!?)\"'")
        if url not in seen:
            seen.append(url)
    return seen


def blocks_video_delivery(text: str, *, owner: bool = False) -> bool:
    """True — не скачивать и не ставить [[video:]] (разбор, отказ, «не качай»)."""
    t = text or ""
    if is_video_denial_or_question(t):
        return True
    if VIDEO_NO_DOWNLOAD_RE.search(t):
        return True
    urls = extract_video_urls(t)
    if not urls:
        return False
    if EXPLICIT_DOWNLOAD_INTENT_RE.search(t):
        return False
    if VIDEO_ANALYSIS_RE.search(t):
        return True
    # Ссылка без явной просьбы скачать/показать — только текст (кроме хозяина).
    return not owner


def wants_video_delivery(text: str, *, owner: bool = False) -> bool:
    if blocks_video_delivery(text, owner=owner):
        return False
    return bool(extract_video_urls(text))


def _accept_video(path: Path) -> Path | None:
    if not path.exists():
        return None
    size = path.stat().st_size
    if size > MAX_VIDEO_BYTES:
        log.warning("video too large: %s", size)
        path.unlink(missing_ok=True)
        return None
    if size < 1024:
        path.unlink(missing_ok=True)
        return None
    return path


_VIDEO_EXTS = ("mp4", "webm", "mkv", "mov", "m4v", "flv")
_THUMB_EXTS = ("jpg", "jpeg", "webp", "png")
_TG_THUMB_MAX_BYTES = 20_000


def _find_output(vid: str) -> Path | None:
    for ext in _VIDEO_EXTS:
        path = MEDIA_DIR / f"video_{vid}.{ext}"
        if path.exists():
            return _accept_video(path)
    matches = [
        p for p in MEDIA_DIR.glob(f"video_{vid}.*")
        if p.suffix.lstrip(".").lower() in _VIDEO_EXTS
    ]
    if not matches:
        return None
    return _accept_video(matches[0])


def _find_thumbnail(vid: str) -> Path | None:
    for ext in _THUMB_EXTS:
        path = MEDIA_DIR / f"video_{vid}.{ext}"
        if path.exists() and path.stat().st_size > 100:
            return _as_jpeg_thumb(path)
    return None


def _fit_telegram_thumb(path: Path) -> Path | None:
    """JPEG ≤320px и ≤20 КБ — иначе Telegram игнорирует обложку."""
    if not path.exists() or path.stat().st_size < 100:
        return None
    out = path.with_name(f"{path.stem}_tg.jpg")
    if out.exists() and out.stat().st_size >= 100:
        return out
    try:
        from PIL import Image

        with Image.open(path) as im:
            im = im.convert("RGB")
            w, h = im.size
            if max(w, h) > 320:
                scale = 320 / max(w, h)
                im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            for quality in (75, 65, 55, 45, 35, 25):
                im.save(out, "JPEG", quality=quality, optimize=True)
                if out.stat().st_size <= _TG_THUMB_MAX_BYTES:
                    return out
        if out.exists() and out.stat().st_size > 100:
            return out
    except Exception as e:
        log.debug("thumb resize failed: %s", e)
    out.unlink(missing_ok=True)
    return None


def _as_jpeg_thumb(path: Path) -> Path | None:
    if path.suffix.lower() not in (".jpg", ".jpeg"):
        out = path.with_suffix(".jpg")
        if out.exists() and out.stat().st_size > 100:
            return _fit_telegram_thumb(out)
        try:
            proc = subprocess.run(
                ["ffmpeg", "-y", "-i", str(path), "-q:v", "2", str(out)],
                capture_output=True,
                timeout=20,
            )
            if proc.returncode == 0 and out.exists() and out.stat().st_size > 100:
                return _fit_telegram_thumb(out)
        except Exception as e:
            log.debug("thumb convert failed: %s", e)
        return None
    return _fit_telegram_thumb(path)


def _extract_thumb_ffmpeg(video: Path) -> Path | None:
    out = video.with_name(f"{video.stem}_thumb.jpg")
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-ss", "1", "-i", str(video),
                "-vframes", "1", "-q:v", "2", str(out),
            ],
            capture_output=True,
            timeout=30,
        )
        if proc.returncode == 0 and out.exists() and out.stat().st_size > 100:
            return _fit_telegram_thumb(out)
    except Exception as e:
        log.debug("ffmpeg thumb failed: %s", e)
    out.unlink(missing_ok=True)
    return None


def resolve_video_thumbnail(video: Path) -> Path | None:
    """Обложка для Telegram: превью yt-dlp или кадр из видео."""
    if not video.exists():
        return None
    stem = video.stem
    if stem.startswith("video_"):
        thumb = _find_thumbnail(stem[6:])
        if thumb:
            return thumb
    return _extract_thumb_ffmpeg(video)


def fetch_video_metadata(url: str) -> dict[str, str]:
    """Метаданные ролика без полного скачивания (yt-dlp --dump-json)."""
    url = _normalize_video_url(url)
    cmd = [
        _yt_dlp_bin(),
        "--dump-json",
        "--no-playlist",
        "--no-warnings",
    ]
    if _impersonate_args():
        cmd.extend(_impersonate_args())
    cmd.append(url)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=min(90, VIDEO_DOWNLOAD_TIMEOUT),
        )
        if proc.returncode != 0:
            return {"url": url}
        data = json.loads(proc.stdout)
        desc = (data.get("description") or "").strip()
        if len(desc) > 280:
            desc = desc[:277] + "…"
        return {
            "title": (data.get("title") or "").strip(),
            "uploader": (data.get("uploader") or data.get("channel") or "").strip(),
            "description": desc,
            "url": (data.get("webpage_url") or url).strip(),
        }
    except Exception as e:
        log.debug("fetch_video_metadata failed %s: %s", url, e)
        return {"url": url}


def _video_sidecar_path(path: str | Path) -> Path:
    return Path(path).with_suffix(Path(path).suffix + ".meta.json")


def save_video_sidecar(path: str | Path, meta: dict[str, str]) -> None:
    """Сохраняет url/название рядом с файлом — для подписи после удаления исходника."""
    try:
        p = Path(path)
        payload = {
            k: str(v).strip()
            for k, v in (meta or {}).items()
            if v and str(v).strip()
        }
        if not payload:
            return
        _video_sidecar_path(p).write_text(
            json.dumps(payload, ensure_ascii=False, indent=0),
            encoding="utf-8",
        )
    except Exception as e:
        log.debug("save_video_sidecar failed %s: %s", path, e)


def load_video_sidecar(path: str | Path) -> dict[str, str]:
    try:
        raw = _video_sidecar_path(path).read_text(encoding="utf-8")
        data = json.loads(raw)
        return {k: str(v).strip() for k, v in data.items() if v} if isinstance(data, dict) else {}
    except Exception:
        return {}


def _title_from_video_url(url: str) -> str:
    """Человекочитаемое название из slug URL (rule34video, hanime, youtube и т.д.)."""
    link = (url or "").strip()
    if not link:
        return ""
    path = urlparse(link).path.strip("/")
    if not path:
        return ""
    slug = path.split("/")[-1]
    if slug.isdigit() and "/" in path:
        parts = [p for p in path.split("/") if p and not p.isdigit()]
        slug = parts[-1] if parts else slug
    title = re.sub(r"[-_]+", " ", slug).strip()
    if not title or title.lower() in {"video", "watch", "embed"}:
        return ""
    return title.title()


def _source_label_from_url(url: str) -> str:
    host = (urlparse(url).netloc or "").lower().replace("www.", "")
    if "rule34video" in host or host == "rule34.xxx":
        return "rule34video"
    if "rule34" in host:
        return "rule34"
    if host:
        return host.split(".")[0]
    return ""


def _with_video_sidecar(path: Path | None, source_url: str = "") -> Path | None:
    if not path:
        return None
    link = (source_url or "").strip()
    if not link.startswith(("http://", "https://")):
        return path
    meta = fetch_video_metadata(link)
    if not (meta.get("title") or "").strip():
        guessed = _title_from_video_url(link)
        if guessed:
            meta["title"] = guessed
    src = _source_label_from_url(link)
    if src and not (meta.get("uploader") or "").strip():
        meta["uploader"] = src
    meta["url"] = (meta.get("url") or link).strip()
    save_video_sidecar(path, meta)
    return path


def format_local_video_caption(path: str | Path, *, extra: str = "") -> str:
    """Короткая подпись для локального файла, если агент не дал текст перед маркером."""
    p = Path(path)
    sidecar = load_video_sidecar(p)
    if sidecar:
        built = format_video_caption(sidecar, sidecar.get("url", ""), extra=extra)
        if not is_weak_video_caption(built):
            return built[:1024]
    parts: list[str] = []
    extra = (extra or "").strip()
    if extra and not is_weak_video_caption(extra):
        parts.append(extra)
    stem = p.stem.replace("_", " ").strip()
    if stem and not stem.lower().startswith("video") and stem not in extra:
        parts.append(f"🎬 {stem}")
    if not parts:
        parts.append("🎬 видео")
    return "\n".join(parts)[:1024]


def format_video_caption(
    meta: dict[str, str] | None,
    url: str = "",
    *,
    extra: str = "",
) -> str:
    """Короткая подпись к видео: название, автор, ссылка."""
    meta = meta or {}
    parts: list[str] = []
    extra = (extra or "").strip()
    if extra:
        parts.append(extra)
    title = (meta.get("title") or "").strip()
    uploader = (meta.get("uploader") or "").strip()
    if title and title not in extra:
        parts.append(f"🎬 {title}")
    if uploader and uploader not in extra:
        parts.append(f"👤 {uploader}")
    link = (meta.get("url") or url or "").strip()
    if link and link not in extra:
        parts.append(link)
    return "\n".join(parts)[:1024]


_WEAK_VIDEO_CAPTIONS = frozenset({
    "",
    "видео",
    "🎬 видео",
    "отправленное видео",
    "[видео]",
})


def is_weak_video_caption(caption: str) -> bool:
    cap = (caption or "").strip()
    if not cap or cap.lower() in _WEAK_VIDEO_CAPTIONS:
        return True
    if len(cap) < 14 and not re.search(r"https?://|🎬|👤|r34|rule34|hent", cap, re.I):
        return True
    return False


def ensure_video_caption(
    caption: str,
    *,
    url: str = "",
    path: str | Path = "",
    hint: str = "",
) -> str:
    """Гарантирует осмысленную подпись: персонажи/название/источник/ссылка."""
    cap = (caption or "").strip()
    hint = (hint or "").strip()
    if not is_weak_video_caption(cap):
        return cap[:1024]
    if hint and not is_weak_video_caption(hint):
        return hint[:1024]
    link = (url or "").strip()
    sidecar: dict[str, str] = {}
    if path:
        sidecar = load_video_sidecar(path)
        if not link:
            link = (sidecar.get("url") or "").strip()
    meta: dict[str, str] = dict(sidecar)
    if link.startswith(("http://", "https://")):
        fetched = fetch_video_metadata(link)
        for key, val in fetched.items():
            if val and not meta.get(key):
                meta[key] = val
    if link and not (meta.get("title") or "").strip():
        guessed = _title_from_video_url(link)
        if guessed:
            meta["title"] = guessed
    if link and not (meta.get("uploader") or "").strip():
        src = _source_label_from_url(link)
        if src:
            meta["uploader"] = src
    extra = cap if cap and cap.lower() not in _WEAK_VIDEO_CAPTIONS else ""
    built = format_video_caption(meta, link, extra=extra)
    if not is_weak_video_caption(built):
        return built[:1024]
    if path:
        local = format_local_video_caption(path, extra=cap)
        if not is_weak_video_caption(local):
            return local[:1024]
    if link:
        fallback = format_video_caption(
            {"title": meta.get("title", ""), "uploader": meta.get("uploader", ""), "url": link},
            link,
        )
        if not is_weak_video_caption(fallback):
            return fallback[:1024]
        return link[:1024]
    return (cap or "🎬 видео")[:1024]


def probe_video_meta(video: Path) -> tuple[int, int, int]:
    """width, height, duration_sec для DocumentAttributeVideo."""
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-show_entries", "format=duration",
                "-of", "csv=p=0",
                str(video),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode != 0:
            raise ValueError(proc.stderr or proc.stdout)
        lines = [ln.strip() for ln in proc.stdout.strip().splitlines() if ln.strip()]
        w, h, dur = 1, 1, 0
        if lines:
            parts = lines[0].split(",")
            if len(parts) >= 2:
                w, h = int(float(parts[0])), int(float(parts[1]))
        if len(lines) > 1:
            dur = int(float(lines[1].split(",")[0]))
        return max(w, 1), max(h, 1), max(dur, 0)
    except Exception as e:
        log.debug("ffprobe failed: %s", e)
        return 1, 1, 0


def _run_yt_dlp(
    url: str,
    out_tpl: str,
    extra: list[str] | None = None,
    *,
    timeout: int | None = None,
    format_spec: str | None = None,
) -> bool:
    cmd = [
        _yt_dlp_bin(),
        "-f",
        format_spec or _YT_DLP_FORMAT,
        "--merge-output-format",
        "mp4",
        "--write-thumbnail",
        "--convert-thumbnails",
        "jpg",
        "--no-playlist",
        "--no-warnings",
        "-o",
        out_tpl,
    ]
    if extra:
        cmd.extend(extra)
    elif _impersonate_args():
        cmd.extend(_impersonate_args())
    cmd.append(url)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout or VIDEO_DOWNLOAD_TIMEOUT,
        )
        if proc.returncode != 0:
            log.debug("yt-dlp fail: %s", (proc.stderr or proc.stdout)[:400])
            return False
        return True
    except subprocess.TimeoutExpired:
        log.warning("yt-dlp timeout for %s", url)
        return False
    except Exception as e:
        log.warning("yt-dlp error: %s", e)
        return False


def _fetch_page_html(page_url: str, *, limit: int = 2_000_000) -> str:
    try:
        return _curl_cffi_get(page_url, referer=page_url)[:limit]
    except Exception as e:
        log.debug("curl_cffi page fetch failed %s: %s", page_url, e)
    req = Request(
        page_url,
        headers={"User-Agent": _USER_AGENT, "Referer": page_url},
    )
    with urlopen(req, timeout=45) as resp:
        return resp.read(limit).decode("utf-8", errors="ignore")


def _is_avbebe_url(url: str) -> bool:
    host = urlparse(url).netloc.split(":")[0]
    return bool(_AVBEBE_HOST_RE.search(host))


def _avbebe_normalize(url: str) -> str:
    if url.startswith("http://") and _is_avbebe_url(url):
        return "https://" + url[7:]
    return url


def _avbebe_article_links(page_url: str, html_text: str) -> list[str]:
    """Все /archives/ID на странице — новые первыми (сайдбар часто без видео)."""
    seen: set[int] = set()
    ids: list[int] = []
    for m in re.finditer(r"/archives/(\d+)", html_text, re.I):
        aid = int(m.group(1))
        if aid not in seen:
            seen.add(aid)
            ids.append(aid)
    ids.sort(reverse=True)
    base = f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}"
    return [f"{base}/archives/{aid}" for aid in ids[:80]]


def _probe_m3u8_url(stream_url: str, referer: str) -> bool:
    """Быстрая проверка: CDN отдаёт m3u8 с Referer."""
    try:
        from curl_cffi import requests

        resp = requests.head(
            stream_url,
            impersonate=_CURL_IMPERSONATE,
            headers={"User-Agent": _USER_AGENT, "Referer": referer},
            timeout=15,
            allow_redirects=True,
        )
        if resp.status_code == 200:
            return True
    except Exception as e:
        log.debug("m3u8 probe head failed %s: %s", stream_url[:80], e)
    try:
        from curl_cffi import requests

        resp = requests.get(
            stream_url,
            impersonate=_CURL_IMPERSONATE,
            headers={"User-Agent": _USER_AGENT, "Referer": referer},
            timeout=20,
            stream=True,
        )
        if resp.status_code != 200:
            return False
        chunk = next(resp.iter_content(512), b"")
        return bool(chunk) and b"#EXTM3U" in chunk
    except Exception as e:
        log.debug("m3u8 probe get failed %s: %s", stream_url[:80], e)
    return False


def _avbebe_480_variant(stream_url: str) -> str | None:
    """52cute watch/ID → watch/ID-480 (если есть)."""
    m = re.search(r"(/watch/\d+)/video\.m3u8", stream_url, re.I)
    if not m:
        return None
    return stream_url.replace(f"{m.group(1)}/", f"{m.group(1)}-480/")


def _extract_avbebe_data_items(html_text: str) -> list[str]:
    """data-item JSON с &quot; — подсчёт скобок, не хрупкий regex."""
    found: list[str] = []
    marker = 'data-item="'
    pos = 0
    while True:
        idx = html_text.find(marker, pos)
        if idx < 0:
            break
        start = idx + len(marker)
        if start >= len(html_text) or html_text[start] != "{":
            pos = start
            continue
        depth = 0
        i = start
        while i < len(html_text):
            if html_text.startswith("&quot;", i):
                i += 6
                continue
            ch = html_text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    found.append(html_text[start : i + 1])
                    break
            i += 1
        pos = start + 1
    return found


def _parse_avbebe_flowplayer_item(raw_json: str) -> tuple[list[str], str | None]:
    raw = html.unescape(raw_json).replace("&quot;", '"').replace("\\/", "/")
    try:
        item = json.loads(raw)
    except json.JSONDecodeError:
        return [], None
    splash = item.get("splash") or None
    urls: list[str] = []
    for src in item.get("sources") or []:
        u = (src.get("src") or "").strip()
        if u.startswith("http") and u not in urls:
            urls.append(u)
    return urls, splash


def _avbebe_article_streams(article_url: str) -> list[tuple[str, str | None]]:
    """Flowplayer на avbebe хранит m3u8 в data-item JSON."""
    found: list[tuple[str, str | None]] = []
    try:
        html_text = _fetch_page_html(article_url)
    except Exception as e:
        log.debug("avbebe article fetch failed %s: %s", article_url, e)
        return found
    splash: str | None = None
    raw_items: list[str] = []
    for m in _AVBEBE_FLOWPLAYER_RE.finditer(html_text):
        raw = m.group(1) or m.group(2) or ""
        if raw:
            raw_items.append(raw)
    for raw in _extract_avbebe_data_items(html_text):
        if raw not in raw_items:
            raw_items.append(raw)
    for raw in raw_items:
        urls, sp = _parse_avbebe_flowplayer_item(raw)
        if sp and not splash:
            splash = sp
        for u in urls:
            if not any(u == x[0] for x in found):
                found.append((u, splash))
    if not found:
        for m in _AVBEBE_M3U8_RE.finditer(html_text):
            u = m.group(0)
            if not any(u == x[0] for x in found):
                found.append((u, splash))
    expanded: list[tuple[str, str | None]] = []
    for u, sp in found:
        alt = _avbebe_480_variant(u)
        if alt and alt != u and _probe_m3u8_url(alt, article_url):
            if not any(alt == x[0] for x in expanded):
                expanded.append((alt, sp))
        if not any(u == x[0] for x in expanded):
            expanded.append((u, sp))
    found = expanded
    # 52cute надёжнее cdn2020 (451); 480p — быстрее
    found.sort(
        key=lambda x: (
            0 if "52cute.com" in x[0] else 1,
            0 if "-480" in x[0] else 1,
            x[0],
        )
    )
    return found


def _avbebe_article_candidates(url: str, *, attempt: int = 0) -> list[str]:
    """Главная/категория → список постов; прямой /archives/ID — один элемент."""
    url = _avbebe_normalize(url)
    if not _is_avbebe_url(url):
        return []
    if _AVBEBE_ARTICLE_RE.match(url):
        return [url.rstrip("/")]
    try:
        page_url = urljoin(url, "/")
        html_text = _fetch_page_html(page_url)
    except Exception as e:
        log.debug("avbebe page fetch failed %s: %s", url, e)
        return []
    articles = _avbebe_article_links(page_url, html_text)
    if not articles:
        return []
    head, tail = articles[:12], articles[12:]
    if attempt:
        random.shuffle(head)
    random.shuffle(tail)
    return head + tail[:68]


def _avbebe_pick_streams(
    article: str,
) -> list[tuple[str, str | None]]:
    streams = _avbebe_article_streams(article)
    if not streams:
        return []
    good = [s for s in streams if "52cute.com" in s[0]]
    if good:
        return good
    # cdn2020 отдаёт 451 — не тратим время на мёртвые ссылки
    return []


def _resolve_avbebe_targets(url: str) -> list[tuple[str, str, str | None]]:
    """Первый рабочий пост → (m3u8, referer, splash)."""
    for article in _avbebe_article_candidates(url):
        pick = _avbebe_pick_streams(article)
        if pick:
            log.info("avbebe pick %s -> %s", url, article)
            return [(s, article, splash) for s, splash in pick]
    log.warning("avbebe: no streams for %s", url)
    return []


def _download_m3u8_ffmpeg(
    stream_url: str,
    out_path: Path,
    referer: str,
    *,
    on_progress=None,
) -> bool:
    """ffmpeg + Referer — CDN 52cute.com отдаёт 403 без заголовка."""
    header_str = f"Referer: {referer}\r\nUser-Agent: {_USER_AGENT}\r\n"
    cmd = [
        "ffmpeg", "-y",
        "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_at_eof", "1",
        "-reconnect_delay_max", "10",
        "-headers", header_str,
        "-i", stream_url,
        "-c", "copy",
        str(out_path),
    ]
    out_path.unlink(missing_ok=True)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + VIDEO_DOWNLOAD_TIMEOUT
        last_poll = 0.0
        while proc.poll() is None:
            if time.monotonic() > deadline:
                proc.kill()
                log.warning("ffmpeg m3u8 timeout for %s", stream_url[:80])
                break
            if on_progress and out_path.exists():
                now = time.monotonic()
                if now - last_poll >= 2.0:
                    on_progress(out_path.stat().st_size, 0, None)
                    last_poll = now
            time.sleep(0.5)
        if proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 1024:
            return True
    except Exception as e:
        log.debug("ffmpeg m3u8 error: %s", e)
    out_path.unlink(missing_ok=True)
    return False


def _save_avbebe_splash(splash_url: str, vid: str, *, referer: str) -> None:
    out = MEDIA_DIR / f"video_{vid}.jpg"
    try:
        from curl_cffi import requests

        resp = requests.get(
            splash_url,
            impersonate=_CURL_IMPERSONATE,
            headers={"User-Agent": _USER_AGENT, "Referer": referer},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.content[:5_000_000]
        if len(data) > 100:
            out.write_bytes(data)
            return
    except Exception as e:
        log.debug("avbebe splash curl failed: %s", e)
    try:
        req = Request(
            splash_url,
            headers={"User-Agent": _USER_AGENT, "Referer": referer},
        )
        with urlopen(req, timeout=30) as resp:
            data = resp.read(5_000_000)
        if len(data) > 100:
            out.write_bytes(data)
    except Exception as e:
        log.debug("avbebe splash save failed: %s", e)


def _download_avbebe(
    url: str,
    vid: str,
    out_tpl: str,
    *,
    on_progress=None,
    attempt: int = 0,
) -> Path | None:
    """avbebe/52cute: yt-dlp+Referer (без impersonate), затем ffmpeg fallback."""
    articles = _avbebe_article_candidates(url, attempt=attempt)
    if not articles:
        log.warning("avbebe: no articles for %s", url)
        return None
    out_mp4 = MEDIA_DIR / f"video_{vid}.mp4"
    ref_hdr_base = ["--add-headers", f"User-Agent:{_USER_AGENT}"]
    tier480: list[tuple[str, list[tuple[str, str | None]]]] = []
    tier_watch: list[tuple[str, list[tuple[str, str | None]]]] = []
    tier_3d: list[tuple[str, list[tuple[str, str | None]]]] = []
    for article in articles:
        pick = _avbebe_pick_streams(article)
        if not pick:
            continue
        if any("-480" in s[0] for s in pick):
            tier480.append((article, pick))
        elif any("/3D/" in s[0] for s in pick):
            tier_3d.append((article, pick))
        else:
            tier_watch.append((article, pick))
    if attempt:
        random.shuffle(tier480)
        random.shuffle(tier_watch)
        random.shuffle(tier_3d)
    pool = tier480 + tier_watch + tier_3d
    for article, pick in pool[:12]:
        log.info("avbebe try %s -> %s", url, article)
        for stream, splash in pick:
            referer = article
            ref_hdr = ref_hdr_base + ["--add-headers", f"Referer:{referer}"]
            # yt-dlp + Referer быстрее ffmpeg на 52cute; --impersonate на m3u8 висит.
            if _run_yt_dlp(
                stream,
                out_tpl,
                ref_hdr,
                format_spec=_AVBEBE_YTDLP_FORMAT,
            ):
                hit = _find_output(vid)
                if hit:
                    log.info("video ok via avbebe yt-dlp %s", stream[:80])
                    if splash and not _find_thumbnail(vid):
                        _save_avbebe_splash(splash, vid, referer=referer)
                    return hit
            if ".m3u8" in stream.lower() and _download_m3u8_ffmpeg(
                stream, out_mp4, referer, on_progress=on_progress,
            ):
                hit = _accept_video(out_mp4)
                if hit:
                    log.info("video ok via avbebe ffmpeg %s", stream[:80])
                    if splash:
                        _save_avbebe_splash(splash, vid, referer=referer)
                    return hit
    log.warning("avbebe: all articles failed for %s", url)
    return None


def _telegram_embed_video_urls(page_url: str) -> list[str]:
    """Прямые mp4 из embed-страницы t.me (публичные посты)."""
    embed_url = _telegram_embed_url(page_url)
    try:
        html_text = _fetch_page_html(embed_url)
    except Exception as e:
        log.debug("telegram embed fetch failed %s: %s", embed_url, e)
        return []
    if "tgme_widget_message_error" in html_text or "not found" in html_text.lower():
        return []
    found: list[str] = []
    for pat in (_TG_EMBED_VIDEO_RE,):
        for m in pat.finditer(html_text):
            u = html.unescape(m.group(1)).replace("&amp;", "&")
            if u.startswith("http") and u not in found:
                found.append(u)
    return found


def _save_telegram_embed_thumb(page_url: str, vid: str) -> None:
    embed_url = _telegram_embed_url(page_url)
    try:
        html_text = _fetch_page_html(embed_url)
    except Exception:
        return
    m = _TG_EMBED_THUMB_RE.search(html_text)
    if not m:
        return
    thumb_url = html.unescape(m.group(1)).replace("&amp;", "&")
    if not thumb_url.startswith("http"):
        return
    out = MEDIA_DIR / f"video_{vid}.jpg"
    try:
        from curl_cffi import requests

        resp = requests.get(
            thumb_url,
            impersonate=_CURL_IMPERSONATE,
            headers={"Referer": page_url, "User-Agent": _USER_AGENT},
            timeout=30,
        )
        resp.raise_for_status()
        if len(resp.content) > 100:
            out.write_bytes(resp.content)
    except Exception as e:
        log.debug("telegram thumb save failed: %s", e)


def _download_telegram_userbot(
    url: str,
    *,
    on_progress=None,
) -> Path | None:
    try:
        from user_client import download_telegram_post_media_sync

        path = download_telegram_post_media_sync(url, on_progress=on_progress)
        if path:
            return _accept_video(Path(path))
    except Exception as e:
        log.debug("telegram userbot download failed %s: %s", url, e)
    return None


def _download_telegram(
    url: str,
    *,
    on_progress=None,
) -> Path | None:
    """t.me: юзербот (приватные каналы), затем yt-dlp embed и парсинг HTML."""
    page_url = (url or "").split("?")[0].strip()
    vid = uuid.uuid4().hex[:8]
    out_tpl = str(MEDIA_DIR / f"video_{vid}.%(ext)s")
    out_mp4 = MEDIA_DIR / f"video_{vid}.mp4"
    referer = page_url

    hit = _download_telegram_userbot(url, on_progress=on_progress)
    if hit:
        return hit

    embed_url = _telegram_embed_url(page_url)
    imp = _impersonate_args()
    strategies: list[list[str]] = [
        imp + [
            "--add-headers",
            f"Referer:{referer}",
            "--add-headers",
            f"User-Agent:{_USER_AGENT}",
        ],
        [
            "--add-headers",
            f"Referer:https://t.me/",
            "--add-headers",
            f"User-Agent:{_USER_AGENT}",
        ],
        imp,
        [],
    ]
    for extra in strategies:
        if _run_yt_dlp(embed_url, out_tpl, extra or None, format_spec="best/bestvideo+bestaudio/best"):
            hit = _find_output(vid)
            if hit:
                log.info("telegram ok via yt-dlp %s -> %s", page_url, hit.name)
                return hit

    _save_telegram_embed_thumb(page_url, vid)
    for stream in _telegram_embed_video_urls(page_url):
        out_mp4.unlink(missing_ok=True)
        if _curl_cffi_download(stream, out_mp4, referer=referer, on_progress=on_progress):
            hit = _accept_video(out_mp4)
            if hit:
                log.info("telegram ok via embed html %s -> %s", page_url, hit.name)
                return hit
        if _download_stream_ffmpeg(stream, out_mp4, referer=referer):
            hit = _accept_video(out_mp4)
            if hit:
                log.info("telegram ok via ffmpeg %s -> %s", page_url, hit.name)
                return hit

    stale = list(MEDIA_DIR.glob(f"video_{vid}.*"))
    for p in stale:
        p.unlink(missing_ok=True)
    return None


def _page_direct_urls(page_url: str) -> list[str]:
    """Ищет прямые mp4/m3u8 в HTML/JSON страницы."""
    found: list[str] = []
    try:
        html = _fetch_page_html(page_url)
    except Exception as e:
        log.debug("page fetch failed %s: %s", page_url, e)
        return found
    for pat in _DIRECT_MEDIA_RE:
        for m in pat.finditer(html):
            raw = m.group(1) if m.lastindex else m.group(0)
            raw = raw.replace("\\/", "/")
            if raw.startswith("http"):
                u = raw
            else:
                u = urljoin(page_url, raw)
            if u not in found:
                found.append(u)
    return found[:5]


def format_download_progress(downloaded: int, total: int, eta: int | None) -> str:
    pct = int(downloaded * 100 / total) if total else 0
    if total:
        size = f"{downloaded // (1024 * 1024)}/{total // (1024 * 1024)} МБ"
    else:
        size = f"{downloaded // (1024 * 1024)} МБ"
    eta_s = ""
    if eta is not None and eta >= 0:
        m, s = divmod(int(eta), 60)
        eta_s = f" · ~{m}:{s:02d}" if m else f" · ~{s}с"
    return f"📥 Качаю видео… {pct}% · {size}{eta_s}"


def prepare_video_for_send(path: Path) -> tuple[Path, Path | None]:
    """Remux в mp4 при необходимости и кадр-превью для Telegram."""
    video = path
    if path.suffix.lower() not in (".mp4", ".m4v", ".mov"):
        mp4 = path.with_suffix(".mp4")
        try:
            proc = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(path),
                    "-c",
                    "copy",
                    "-movflags",
                    "+faststart",
                    str(mp4),
                ],
                capture_output=True,
                timeout=600,
            )
            if proc.returncode == 0 and mp4.exists() and mp4.stat().st_size > 1024:
                video = mp4
        except Exception as e:
            log.debug("mp4 remux failed: %s", e)

    return video, resolve_video_thumbnail(video)


def format_upload_progress(current: int, total: int) -> str:
    pct = int(current * 100 / total) if total else 0
    if total:
        size = f"{current // (1024 * 1024)}/{total // (1024 * 1024)} МБ"
        return f"📤 Отправляю видео… {pct}% · {size}"
    return f"📤 Отправляю видео… {pct}%"


def _download_via_api(url: str, *, on_progress) -> Path | None:
    import yt_dlp

    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    vid = uuid.uuid4().hex[:8]
    out_tpl = str(MEDIA_DIR / f"video_{vid}.%(ext)s")

    def hook(d: dict) -> None:
        if d.get("status") != "downloading":
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        on_progress(int(d.get("downloaded_bytes") or 0), int(total), d.get("eta"))

    ydl_opts = {
        "format": _YT_DLP_FORMAT,
        "merge_output_format": "mp4",
        "writethumbnail": True,
        "convert_thumbnails": "jpg",
        "noplaylist": True,
        "outtmpl": out_tpl,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
    }
    url = _normalize_video_url(url)
    if _is_hanime_url(url):
        hit = _download_hanime(url, vid, on_progress=on_progress)
        if hit:
            return hit
        return None
    if _is_avbebe_url(url):
        hit = _download_avbebe(url, vid, out_tpl, on_progress=on_progress)
        if hit:
            return hit
        return None
    if _is_bilibili_url(url):
        hit = _download_bilibili(url, out_tpl, on_progress=on_progress)
        if hit:
            return hit
        ydl_opts["http_headers"] = {"Referer": _BILIBILI_REFERER}
        ydl_opts.setdefault("extractor_args", {})["bilibili"] = {
            "player_client": ["android", "web", "bilibili_tv"],
        }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        return _find_output(vid)
    except Exception as e:
        log.debug("yt-dlp api error: %s", e)
        return None


def download_video(url: str, *, on_progress=None) -> Path | None:
    norm = _normalize_video_url(url) if (url or "").strip() else ""
    hit = _download_video_impl(url, on_progress=on_progress)
    return _with_video_sidecar(hit, norm)


def _download_video_impl(url: str, *, on_progress=None) -> Path | None:
    if is_download_cancelled():
        log.info("download_video cancelled before start %s", url)
        return None
    url = _normalize_video_url(url)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    if _is_avbebe_url(url):
        log.info("avbebe download start %s", url)
        for attempt in range(3):
            if is_download_cancelled():
                return None
            vid = uuid.uuid4().hex[:8]
            out_tpl = str(MEDIA_DIR / f"video_{vid}.%(ext)s")
            hit = _download_avbebe(
                url, vid, out_tpl, on_progress=on_progress, attempt=attempt,
            )
            if hit:
                return hit
            log.debug("avbebe attempt %d failed for %s", attempt + 1, url)
        log.warning("download_video failed for avbebe %s", url)
        return None
    if _is_hanime_url(url):
        log.info("hanime download start %s", url)
        vid = uuid.uuid4().hex[:8]
        hit = _download_hanime(url, vid, on_progress=on_progress)
        if hit:
            return hit
        log.warning("download_video failed for hanime %s", url)
        return None
    if _is_bilibili_url(url):
        log.info("bilibili download start %s", url)
        for attempt in range(2):
            vid = uuid.uuid4().hex[:8]
            out_tpl = str(MEDIA_DIR / f"video_{vid}.%(ext)s")
            hit = _download_bilibili(url, out_tpl, on_progress=on_progress)
            if hit:
                return hit
            log.debug("bilibili attempt %d failed for %s", attempt + 1, url)
        log.warning("download_video failed for bilibili %s", url)
        return None
    if _is_telegram_url(url):
        log.info("telegram download start %s", url)
        hit = _download_telegram(url, on_progress=on_progress)
        if hit:
            return hit
        log.warning("download_video failed for telegram %s", url)
        return None
    if on_progress:
        hit = _download_via_api(url, on_progress=on_progress)
        if hit:
            return hit
    return _download_video_core(url)


def _download_video_core(url: str) -> Path | None:
    """Скачивает видео с любого URL — несколько попыток yt-dlp + прямые ссылки со страницы."""
    url = _normalize_video_url(url)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    vid = uuid.uuid4().hex[:8]
    out_tpl = str(MEDIA_DIR / f"video_{vid}.%(ext)s")
    domain = urlparse(url).netloc
    fetch_url = url

    if _is_avbebe_url(url):
        hit = _download_avbebe(url, vid, out_tpl)
        if hit:
            return hit
        log.warning("download_video failed for %s", url)
        return None

    if _is_hanime_url(url):
        hit = _download_hanime(url, vid)
        if hit:
            return hit

    if _is_bilibili_url(url):
        hit = _download_bilibili(url, out_tpl)
        if hit:
            return hit

    if _is_telegram_url(url):
        hit = _download_telegram(url)
        if hit:
            return hit

    imp = _impersonate_args()
    strategies: list[list[str]] = []
    if _is_bilibili_url(url):
        strategies.extend([
            imp + [
                "--extractor-args",
                "bilibili:player_client=android,web,bilibili_tv",
                "--add-headers",
                f"Referer:{_BILIBILI_REFERER}",
            ],
            [
                "--extractor-args",
                "bilibili:player_client=android,web,bilibili_tv",
                "--add-headers",
                f"Referer:{_BILIBILI_REFERER}",
            ],
        ])
    strategies.extend([
        imp,
        [
            "--add-headers",
            f"Referer:{fetch_url}",
            "--add-headers",
            f"User-Agent:{_USER_AGENT}",
        ],
        imp + [
            "--add-headers",
            f"Referer:https://{domain}/",
        ],
        ["-f", "best/bestvideo+bestaudio/best"] + imp,
    ])
    for extra in strategies:
        if not extra:
            continue
        if _run_yt_dlp(fetch_url, out_tpl, extra):
            hit = _find_output(vid)
            if hit:
                log.info("video ok via yt-dlp %s -> %s", url, hit.name)
                return hit

    for direct in _page_direct_urls(fetch_url):
        if direct == fetch_url:
            continue
        for extra in (imp, imp + ["--add-headers", f"Referer:{fetch_url}"]):
            if extra and _run_yt_dlp(direct, out_tpl, extra):
                hit = _find_output(vid)
                if hit:
                    log.info("video ok via direct %s", direct[:80])
                    return hit

    stale = list(MEDIA_DIR.glob(f"video_{vid}.*"))
    for p in stale:
        p.unlink(missing_ok=True)
    log.warning("download_video failed for %s", url)
    return None


def _cleanup_converted_video(src: Path) -> None:
    """Удаляет исходное видео после успешной конвертации в mp3."""
    from config import DATA

    try:
        resolved = src.resolve()
    except OSError:
        return
    for root in (MEDIA_DIR.resolve(), (DATA / "incoming_media").resolve()):
        try:
            resolved.relative_to(root)
            src.unlink(missing_ok=True)
            return
        except ValueError:
            continue


def _url_from_audio_hint(path: Path) -> str | None:
    """b23-код из имени файла → короткая ссылка."""
    parts = path.stem.split("_")
    if len(parts) < 2:
        return None
    code = parts[-1]
    if 4 <= len(code) <= 12 and re.fullmatch(r"[a-zA-Z0-9]+", code):
        return f"https://b23.tv/{code}"
    return None


def _audio_search_roots() -> list[Path]:
    from config import DATA

    return [MEDIA_DIR, DATA / "incoming_media"]


def download_audio_mp3(
    url: str,
    *,
    out_path: Path | None = None,
    title_hint: str = "",
) -> Path | None:
    """Скачивает только аудио (быстрее, чем видео + ffmpeg)."""
    import yt_dlp

    from voice_delivery import audio_display_name

    url = _normalize_video_url(url)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    if out_path is None:
        label = audio_display_name(title_hint or url, title="")
        safe = re.sub(r'[<>:"/\\|?*]', "", label).strip() or uuid.uuid4().hex[:8]
        dest = MEDIA_DIR / f"{safe}.mp3"
    else:
        dest = Path(out_path)
    if dest.exists() and dest.stat().st_size > 1024:
        return dest

    vid = uuid.uuid4().hex[:8]
    out_tpl = str(dest.with_suffix(".%(ext)s"))
    ydl_opts: dict = {
        "format": "bestaudio/best",
        "outtmpl": out_tpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "4",
            }
        ],
    }
    if _is_bilibili_url(url):
        ydl_opts["http_headers"] = {"Referer": _BILIBILI_REFERER}
        ydl_opts.setdefault("extractor_args", {})["bilibili"] = {
            "player_client": ["android", "web", "bilibili_tv"],
        }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        log.warning("download_audio_mp3 yt-dlp failed %s: %s", url, e)
        if _is_bilibili_url(url):
            hit = _download_bilibili_audio_only(url, dest=dest)
            if hit:
                return hit
        return None

    if dest.exists() and dest.stat().st_size > 1024:
        log.info("download_audio_mp3 ok %s -> %s", url, dest.name)
        return dest
    for hit in MEDIA_DIR.glob(f"*.{vid}.mp3"):
        if hit.stat().st_size > 1024:
            return hit
    for hit in MEDIA_DIR.glob("*.mp3"):
        try:
            if hit.stat().st_mtime >= time.time() - 120 and hit.stat().st_size > 1024:
                return hit
        except OSError:
            continue
    return None


def ensure_audio_file(audio_path: str | Path) -> Path | None:
    """Готовый mp3: существующий путь или быстрая загрузка/конвертация."""
    p = Path(audio_path)
    if p.exists() and p.stat().st_size > 1024:
        return p
    for root in _audio_search_roots():
        if not root.exists():
            continue
        alt = root / p.name
        if alt.exists() and alt.stat().st_size > 1024:
            return alt
        for hit in root.glob(f"{p.stem}*.mp3"):
            try:
                if hit.stat().st_size > 1024:
                    return hit
            except OSError:
                continue
    hint_url = _url_from_audio_hint(p)
    if hint_url:
        hit = download_audio_mp3(hint_url, out_path=p if p.parent.exists() else None, title_hint=p.stem)
        if hit:
            return hit
    return None


def video_to_mp3(
    video_path: str | Path,
    *,
    out_path: Path | None = None,
    title: str = "",
    delete_source: bool = True,
) -> Path | None:
    """Извлекает аудиодорожку из локального видео (ffmpeg)."""
    from voice_delivery import audio_display_name

    src = Path(video_path)
    if not src.exists():
        log.warning("video_to_mp3: missing %s", src)
        return None
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    if out_path is None:
        label = audio_display_name(src, title=title)
        safe = re.sub(r'[<>:"/\\|?*]', "", label).strip() or src.stem
        dest = MEDIA_DIR / f"{safe}.mp3"
    else:
        dest = Path(out_path)
    if dest.exists() and dest.stat().st_size > 1024:
        return dest
    cmd = [
        "ffmpeg",
        "-y",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-vn",
        "-sn",
        "-dn",
        "-map",
        "0:a:0?",
        "-acodec",
        "libmp3lame",
        "-q:a",
        "4",
        "-threads",
        "0",
        str(dest),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=120)
        if proc.returncode == 0 and dest.exists() and dest.stat().st_size > 1024:
            log.info("video_to_mp3 ok %s -> %s", src.name, dest.name)
            if delete_source:
                _cleanup_converted_video(src)
            return dest
    except Exception as e:
        log.warning("video_to_mp3 failed %s: %s", src, e)
    return None


def find_recent_video_file(*, max_age_sec: float = 7200.0) -> Path | None:
    """Последний скачанный видеофайл в MEDIA_DIR / incoming_media."""
    from config import DATA

    roots = [MEDIA_DIR, DATA / "incoming_media"]
    candidates: list[Path] = []
    cutoff = time.time() - max_age_sec
    for root in roots:
        if not root.exists():
            continue
        for ext in ("*.mp4", "*.mkv", "*.webm", "*.mov"):
            for p in root.glob(ext):
                try:
                    if p.stat().st_mtime >= cutoff and p.stat().st_size > 50_000:
                        candidates.append(p)
                except OSError:
                    continue
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)
