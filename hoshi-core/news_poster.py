#!/usr/bin/env python3
"""Автопубликация аниме-новостей в Telegram-канал."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
from urllib.request import Request, urlopen

from config import DATA, MEDIA_DIR, OWNER_ID
from storage import load_settings, save_settings

log = logging.getLogger("hoshi.news")

STATE_PATH = DATA / "news_poster_state.json"
FORCE_PATH = DATA / "news_poster_force.post"

DEFAULT_TG_SOURCES = ("animetarakans", "AniMirNews")
DEFAULT_RSS_SOURCES = (
    "https://www.animenewsnetwork.com/news/rss.xml",
    "https://myanimelist.net/rss/news.xml",
)
DEFAULT_FOOTER = "#animenews\n" + "➿" * 10 + "\n✨Hoshi Kojima | Лучший впн ⛩️"
PRODUCTION_CHANNEL = "HoshiKojima"
TEST_CHANNEL_TITLE = "Hoshi News Test"

SKIP_RE = re.compile(
    r"(?:"
    r"реклам|подпис|vpn|впн|розыгрыш|giveaway|"
    r"продаю\s+реклам|joinchat|/\+|"
    r"animenews\d+|crypto|ставк"
    r")",
    re.I,
)
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
HASHTAG_RE = re.compile(r"#\w+", re.I)
TG_LINK_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/[^\s]+", re.I)
EMOJI_LEAD = ("📰", "✨", "🔥", "🎌", "📺", "🎬", "⭐")

REWRITE_PAIRS = (
    (r"\bанонсировали\b", "сообщили"),
    (r"\bанонсировал\b", "сообщил"),
    (r"\bобъявили\b", "рассказали"),
    (r"\bвышла\b", "появилась"),
    (r"\bвышел\b", "появился"),
    (r"\bстало известно\b", "узнали"),
    (r"\bофициально\b", "официально"),
    (r"\bновый сезон\b", "новый сезон"),
    (r"\bпремьера\b", "премьера"),
)

ENABLE_OWNER_RE = re.compile(
    r"(?:"
    r"публик(?:уй|овала|овать).*(?:новост|канал)|"
    r"(?:новост|канал).*(?:публик(?:уй|овала|овать)|HoshiKojima)|"
    r"animenews|#animenews|"
    r"новости\s+за\s+меня"
    r")",
    re.I,
)


@dataclass
class NewsItem:
    title: str
    body: str
    url: str
    source: str
    has_media: bool = False
    media_msg_ids: list[int] = field(default_factory=list)
    tg_entity: Any = None
    image_url: str = ""
    published_at: datetime | None = None
    dedup_key: str = ""
    score: float = 0.0


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(data: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _cfg() -> dict[str, Any]:
    return load_settings().get("news_poster") or {}


def is_enabled() -> bool:
    return bool(_cfg().get("enabled"))


def interval_seconds() -> float:
    return max(600.0, float(_cfg().get("interval_minutes", 30)) * 60)


def _normalize_key(text: str) -> str:
    t = (text or "").lower()
    t = URL_RE.sub(" ", t)
    t = HASHTAG_RE.sub(" ", t)
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:160]


def _dedup_hash(title: str, url: str = "") -> str:
    base = _normalize_key(title) or _normalize_key(url)
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:20]


def _is_duplicate(item: NewsItem, state: dict[str, Any]) -> bool:
    published = set(state.get("published_hashes") or [])
    key = item.dedup_key or _dedup_hash(item.title, item.url)
    if key in published:
        return True
    norm = _normalize_key(item.title)
    if not norm:
        return True
    for prev in (state.get("published_titles") or [])[-200:]:
        prev_norm = _normalize_key(prev)
        if not prev_norm:
            continue
        if norm == prev_norm:
            return True
        if len(norm) > 20 and (norm in prev_norm or prev_norm in norm):
            return True
    return False


def _post_timezone() -> ZoneInfo:
    tz_name = _cfg().get("post_timezone") or "Europe/Moscow"
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("Europe/Moscow")


def _posts_today(state: dict[str, Any]) -> int:
    today = datetime.now(_post_timezone()).strftime("%Y-%m-%d")
    days = state.get("posts_by_day") or {}
    return int(days.get(today) or 0)


def _effective_max_per_day(cfg: dict[str, Any] | None = None) -> int:
    cfg = cfg or _cfg()
    if cfg.get("test_mode", True):
        limit = cfg.get("test_max_per_day") or cfg.get("max_per_day")
    else:
        limit = cfg.get("max_per_day")
    if limit is None:
        limit = load_settings()["posts"].get("per_day") or 4
    return int(limit)


def _within_post_hours() -> bool:
    cfg = _cfg()
    start = int(cfg.get("post_hour_start", 11))
    end = int(cfg.get("post_hour_end", 21))
    hour = datetime.now(_post_timezone()).hour
    return start <= hour <= end


def _can_post_today(state: dict[str, Any]) -> bool:
    limit = _effective_max_per_day()
    return _posts_today(state) < limit


def _min_gap_ok(state: dict[str, Any]) -> bool:
    last = state.get("last_post_at")
    if not last:
        return True
    try:
        dt = datetime.fromisoformat(last)
    except Exception:
        return True
    gap = int(_cfg().get("min_gap_minutes") or 90)
    return datetime.now() - dt >= timedelta(minutes=gap)


def enable(*, test_mode: bool = True) -> None:
    settings = load_settings()
    prev = settings.get("news_poster") or {}
    mon = settings.setdefault("monitoring", {})
    tg = list(mon.get("telegram_channels") or [])
    for src in DEFAULT_TG_SOURCES:
        tag = f"@{src.lstrip('@')}"
        if tag not in tg:
            tg.append(tag)
    mon["telegram_channels"] = tg
    sites = list(mon.get("news_sites") or [])
    for url in DEFAULT_RSS_SOURCES:
        if url not in sites:
            sites.append(url)
    mon["news_sites"] = sites
    settings["news_poster"] = {
        "enabled": True,
        "test_mode": test_mode,
        "production_channel": prev.get("production_channel") or f"@{PRODUCTION_CHANNEL}",
        "test_channel_id": prev.get("test_channel_id"),
        "test_channel_username": prev.get("test_channel_username") or "",
        "footer": prev.get("footer") or DEFAULT_FOOTER,
        "interval_minutes": int(prev.get("interval_minutes") or 30),
        "max_per_day": int(prev.get("max_per_day") or 4),
        "test_max_per_day": int(prev.get("test_max_per_day") or 20),
        "min_gap_minutes": int(prev.get("min_gap_minutes") or 30),
        "post_hour_start": int(prev.get("post_hour_start", 11)),
        "post_hour_end": int(prev.get("post_hour_end", 21)),
        "post_timezone": prev.get("post_timezone") or "Europe/Moscow",
        "prefer_media": prev.get("prefer_media", True),
        "enabled_at": prev.get("enabled_at") or _now(),
    }
    settings["posts"]["per_day"] = min(4, int(settings["posts"].get("per_day") or 3))
    settings["posts"]["style"] = (
        settings["posts"].get("style")
        or "короткие новости, эмодзи, ссылка на источник"
    )
    settings["channel"]["username"] = f"@{PRODUCTION_CHANNEL}"
    save_settings(settings)
    log.info("news poster enabled test_mode=%s", test_mode)


def maybe_enable_from_owner_text(text: str) -> bool:
    if not text or not ENABLE_OWNER_RE.search(text):
        return False
    test_mode = bool(re.search(r"тестов", text, re.I))
    if not test_mode and re.search(r"HoshiKojima|t\.me/Hoshi", text, re.I):
        test_mode = "тест" in text.lower() and "канал" in text.lower()
    if re.search(r"тестов(?:ый|ом)?\s+канал", text, re.I):
        test_mode = True
    was_enabled = is_enabled()
    enable(test_mode=test_mode if test_mode else True)
    return not was_enabled


def news_policy_prompt() -> str:
    if not is_enabled():
        return ""
    cfg = _cfg()
    mode = "тестовый канал" if cfg.get("test_mode", True) else cfg.get("production_channel", "")
    limit = _effective_max_per_day(cfg)
    h0 = int(cfg.get("post_hour_start", 11))
    h1 = int(cfg.get("post_hour_end", 21))
    return (
        "**Аниме-новости (фон):**\n"
        f"- Автопостинг в **{mode}** через `news_poster.py` — до **{limit}** постов/день ({h0}:00–{h1}:00 МСК).\n"
        "- Источники: сайты (раньше) + @animetarakans, @AniMirNews — **перефразируй**, не копируй дословно.\n"
        "- В конце поста — футер `#animenews` + VPN-реклама (премиум-эмодзи).\n"
        "- **Приоритет:** посты с фото/альбомами.\n"
        "- Сводки владельцу в бот — только о публикации или проблемах."
    )


def _fetch_rss(url: str) -> list[NewsItem]:
    items: list[NewsItem] = []
    try:
        req = Request(url, headers={"User-Agent": "HoshiNewsBot/1.0"})
        with urlopen(req, timeout=20) as resp:
            raw = resp.read()
        root = ET.fromstring(raw)
    except Exception as e:
        log.debug("rss fetch failed %s: %s", url, e)
        return items

    for node in root.findall(".//item"):
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        desc = (node.findtext("description") or "").strip()
        desc = re.sub(r"<[^>]+>", " ", desc)
        desc = re.sub(r"\s+", " ", desc).strip()
        if not title or SKIP_RE.search(title + " " + desc):
            continue
        pub = None
        pub_raw = node.findtext("pubDate") or node.findtext("{http://purl.org/dc/elements/1.1/}date")
        if pub_raw:
            for fmt in (
                "%a, %d %b %Y %H:%M:%S %z",
                "%a, %d %b %Y %H:%M:%S %Z",
                "%Y-%m-%dT%H:%M:%S%z",
            ):
                try:
                    pub = datetime.strptime(pub_raw.strip(), fmt)
                    break
                except ValueError:
                    continue
        body = desc or title
        image_url = ""
        for tag in (
            "{http://search.yahoo.com/mrss/}content",
            "{http://search.yahoo.com/mrss/}thumbnail",
        ):
            for node_img in node.findall(tag):
                img = (node_img.get("url") or "").strip()
                if img and not image_url:
                    image_url = img
        item = NewsItem(
            title=title,
            body=body,
            url=link,
            source=url,
            has_media=bool(image_url),
            image_url=image_url or "",
            published_at=pub,
            dedup_key=_dedup_hash(title, link),
            score=1.5 if image_url else 1.0,
        )
        items.append(item)
    return items


async def _album_msg_ids(client, entity, msg) -> list[int]:
    gid = getattr(msg, "grouped_id", None)
    if not gid:
        return [int(msg.id)]
    try:
        around = await client.get_messages(entity, limit=40)
    except Exception:
        return [int(msg.id)]
    ids = sorted(
        int(m.id)
        for m in around
        if m and getattr(m, "grouped_id", None) == gid
    )
    return ids or [int(msg.id)]


async def _fetch_tg_sources(client) -> list[NewsItem]:
    settings = load_settings()
    channels = settings.get("monitoring", {}).get("telegram_channels") or []
    if not channels:
        channels = [f"@{s}" for s in DEFAULT_TG_SOURCES]
    state = _load_state()
    seen_ids: set[str] = set(state.get("seen_tg_msg") or [])
    items: list[NewsItem] = []
    for ch_ref in channels:
        username = str(ch_ref).strip().lstrip("@")
        if not username:
            continue
        try:
            entity = await client.get_entity(username)
            msgs = await client.get_messages(entity, limit=25)
        except Exception as e:
            log.debug("tg source %s failed: %s", username, e)
            continue
        for msg in msgs:
            if not msg or not getattr(msg, "message", None):
                continue
            text = (msg.message or "").strip()
            if len(text) < 30 or SKIP_RE.search(text):
                continue
            sid = f"{username}:{msg.id}"
            if sid in seen_ids:
                continue
            has_media = bool(
                msg.photo
                or getattr(msg, "video", None)
                or getattr(msg, "document", None)
                or getattr(msg, "grouped_id", None)
            )
            album_ids = await _album_msg_ids(client, entity, msg)
            title_line = text.split("\n", 1)[0].strip()
            if len(title_line) < 12:
                title_line = text[:80].strip()
            url = ""
            m = URL_RE.search(text)
            if m and "t.me/" not in m.group(0):
                url = m.group(0)
            item = NewsItem(
                title=title_line[:200],
                body=text,
                url=url,
                source=f"@{username}",
                has_media=has_media,
                media_msg_ids=album_ids,
                tg_entity=entity,
                published_at=getattr(msg, "date", None),
                dedup_key=_dedup_hash(title_line, sid),
                score=2.0 if has_media else 0.5,
            )
            items.append(item)
    return items


def _clean_source_text(text: str) -> str:
    paragraphs: list[str] = []
    block: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if block:
                paragraphs.append(" ".join(block))
                block = []
            continue
        if TG_LINK_RE.search(line) and len(line) < 80:
            continue
        if SKIP_RE.search(line) and len(line) < 100:
            continue
        line = URL_RE.sub("", line).strip()
        line = HASHTAG_RE.sub("", line).strip()
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            block.append(line)
    if block:
        paragraphs.append(" ".join(block))
    body = "\n\n".join(paragraphs)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body


def _expand_short_body(body: str, item: NewsItem) -> str:
    if len(body) >= 120:
        return body
    extra = _clean_source_text(item.title)
    if extra and extra not in body:
        body = f"{extra}\n\n{body}".strip() if body else extra
    if len(body) < 80 and item.url and "t.me/" not in item.url:
        body = (body + "\n\nПодробности — в источнике.").strip()
    return body


def _build_post_caption(item: NewsItem) -> tuple[str, list]:
    from text_format import news_footer_entities, news_footer_text

    body = _clean_source_text(item.body or item.title)
    if not body:
        body = (item.title or "").strip()
    body = _expand_short_body(body, item)
    for pat, repl in REWRITE_PAIRS:
        body = re.sub(pat, repl, body, flags=re.I)
    if len(body) > 900:
        cut = body[:900]
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        body = cut.rstrip(".,;:!?") + "…"
    if body and body[0].isalnum():
        lead = EMOJI_LEAD[hash(item.dedup_key) % len(EMOJI_LEAD)]
        body = f"{lead} {body}"
    footer = news_footer_text()
    caption = f"{body}\n\n{footer}" if body else footer
    entities = news_footer_entities(offset=_utf16_len(f"{body}\n\n") if body else 0)
    return caption, entities


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _fetch_og_image(url: str) -> str | None:
    if not url or "t.me/" in url:
        return None
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; HoshiNews/1.0)"})
        with urlopen(req, timeout=20) as resp:
            html = resp.read(250_000).decode("utf-8", errors="replace")
    except Exception as e:
        log.debug("og image fetch failed %s: %s", url, e)
        return None
    for pat in (
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image',
        r'<meta[^>]+property=["\']twitter:image["\'][^>]+content=["\']([^"\']+)',
    ):
        m = re.search(pat, html, re.I)
        if m:
            img = m.group(1).strip()
            if img.startswith("http"):
                return img
    return None


async def _download_image_url(url: str, dest: Path) -> str | None:
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; HoshiNews/1.0)"})
        with urlopen(req, timeout=25) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            data = resp.read(8_000_000)
        ext = ".png" if "png" in ctype else ".jpg"
        path = dest / f"preview{ext}"
        path.write_bytes(data)
        return str(path) if path.exists() and path.stat().st_size > 1000 else None
    except Exception as e:
        log.debug("image download failed %s: %s", url, e)
        return None


async def _download_video_thumb(client, msg, dest: Path, mid: int) -> str | None:
    try:
        path = await client.download_media(msg, file=str(dest / f"{mid}_thumb"), thumb=-1)
        if path and Path(path).exists() and Path(path).stat().st_size > 500:
            return str(path)
    except Exception as e:
        log.debug("tg video thumb failed %s: %s", mid, e)
    return None


def _is_video_message(msg) -> bool:
    if not msg:
        return False
    if getattr(msg, "video", None) or getattr(msg, "gif", None):
        return True
    doc = getattr(msg, "document", None)
    if not doc:
        return False
    mime = (getattr(doc, "mime_type", "") or "").lower()
    if mime.startswith("video/"):
        return True
    from telethon.tl.types import DocumentAttributeAnimated, DocumentAttributeVideo

    for attr in getattr(doc, "attributes", []) or []:
        if isinstance(attr, (DocumentAttributeVideo, DocumentAttributeAnimated)):
            return True
    return False


async def _download_tg_media(client, item: NewsItem) -> list[str]:
    dest = MEDIA_DIR / f"news_{uuid.uuid4().hex[:8]}"
    dest.mkdir(parents=True, exist_ok=True)
    photos: list[str] = []
    if item.tg_entity and item.media_msg_ids:
        try:
            for mid in item.media_msg_ids[:10]:
                msg = await client.get_messages(item.tg_entity, ids=mid)
                if not msg or not msg.media:
                    continue
                if msg.photo:
                    path = await client.download_media(msg, file=str(dest / f"{mid}"))
                    if path and Path(path).exists():
                        photos.append(str(path))
                    continue
                if _is_video_message(msg):
                    thumb = await _download_video_thumb(client, msg, dest, mid)
                    if thumb:
                        photos.append(thumb)
                        continue
                    path = await client.download_media(msg, file=str(dest / f"{mid}"))
                    if not path or not Path(path).exists():
                        continue
                    from video_download import resolve_video_thumbnail

                    gen_thumb = resolve_video_thumbnail(Path(path))
                    if gen_thumb:
                        photos.append(str(gen_thumb))
                    else:
                        photos.append(str(path))
                    continue
                path = await client.download_media(msg, file=str(dest / f"{mid}"))
                if path and Path(path).exists():
                    photos.append(str(path))
        except Exception as e:
            log.warning("tg media download failed: %s", e)
    paths = photos
    if not paths:
        img_url = (item.image_url or "").strip()
        if not img_url and item.url:
            img_url = _fetch_og_image(item.url) or ""
        if img_url:
            dl = await _download_image_url(img_url, dest)
            if dl:
                paths = [dl]
    if not paths:
        import shutil

        shutil.rmtree(dest, ignore_errors=True)
    return paths


async def _ensure_test_channel(client) -> int | None:
    cfg = _cfg()
    cid = cfg.get("test_channel_id")
    if cid:
        return int(cid)
    from telethon.tl.functions.channels import CreateChannelRequest
    from telethon.utils import get_peer_id

    try:
        result = await client(
            CreateChannelRequest(
                title=TEST_CHANNEL_TITLE,
                about="🧪 Тестовый канал новостей Hoshi — черновик перед HoshiKojima",
                megagroup=False,
                broadcast=True,
            )
        )
        channel = result.chats[0]
        peer_id = get_peer_id(channel)
        username = getattr(channel, "username", None) or ""
        settings = load_settings()
        settings.setdefault("news_poster", {})
        settings["news_poster"]["test_channel_id"] = peer_id
        settings["news_poster"]["test_channel_username"] = f"@{username}" if username else ""
        save_settings(settings)
        state = _load_state()
        state["test_channel_created_at"] = _now()
        _save_state(state)
        log.info("created test channel id=%s username=%s", peer_id, username)
        from notify import send_message

        link = f"https://t.me/{username}" if username else f"chat id `{peer_id}`"
        await send_message(
            OWNER_ID,
            f"📰 **Тестовый канал новостей создан**\n\n{link}\n\n"
            "Сюда пойдут черновики постов — скажешь, что поправить, потом переключу на HoshiKojima.",
        )
        return int(peer_id)
    except Exception as e:
        log.warning("create test channel failed: %s", e)
        return None


async def _resolve_target_channel(client) -> int | None:
    cfg = _cfg()
    if cfg.get("test_mode", True):
        return await _ensure_test_channel(client)
    prod = str(cfg.get("production_channel") or f"@{PRODUCTION_CHANNEL}").lstrip("@")
    try:
        entity = await client.get_entity(prod)
        from telethon.utils import get_peer_id

        return int(get_peer_id(entity))
    except Exception as e:
        log.warning("resolve production channel failed: %s", e)
        return None


def _pick_candidate(items: list[NewsItem], state: dict[str, Any]) -> NewsItem | None:
    cfg = _cfg()
    prefer_media = cfg.get("prefer_media", True)
    fresh: list[NewsItem] = []
    for item in items:
        if _is_duplicate(item, state):
            continue
        if item.published_at:
            age = datetime.now(item.published_at.tzinfo) - item.published_at
            if age > timedelta(days=3):
                continue
        fresh.append(item)
    if not fresh:
        return None
    fresh.sort(
        key=lambda x: (
            (2 if x.has_media else 0) if prefer_media else 0,
            x.score,
            x.published_at or datetime.min.replace(tzinfo=None),
        ),
        reverse=True,
    )
    return fresh[0]


def _mark_published(item: NewsItem, state: dict[str, Any]) -> None:
    key = item.dedup_key or _dedup_hash(item.title, item.url)
    hashes = list(state.get("published_hashes") or [])
    hashes.append(key)
    state["published_hashes"] = hashes[-500:]
    titles = list(state.get("published_titles") or [])
    titles.append(item.title[:200])
    state["published_titles"] = titles[-200:]
    today = datetime.now(_post_timezone()).strftime("%Y-%m-%d")
    days = dict(state.get("posts_by_day") or {})
    days[today] = int(days.get(today) or 0) + 1
    state["posts_by_day"] = days
    state["last_post_at"] = _now()
    if item.media_msg_ids and item.source:
        src = item.source.lstrip("@")
        seen = list(state.get("seen_tg_msg") or [])
        for mid in item.media_msg_ids:
            seen.append(f"{src}:{mid}")
        state["seen_tg_msg"] = seen[-2000:]


async def _send_channel_media(
    client,
    channel_id: int,
    media_paths: list[str],
    caption: str,
    formatting_entities: list | None,
):
    from telethon.tl.types import DocumentAttributeVideo

    from video_download import prepare_video_for_send, probe_video_meta

    if len(media_paths) > 1:
        return await client.send_file(
            channel_id,
            media_paths,
            caption=caption,
            formatting_entities=formatting_entities,
            link_preview=False,
        )
    path = media_paths[0]
    p = Path(path)
    if p.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm", ".m4v"}:
        send_path, thumb = await asyncio.to_thread(prepare_video_for_send, p)
        w, h, dur = await asyncio.to_thread(probe_video_meta, send_path)
        return await client.send_file(
            channel_id,
            str(send_path),
            caption=caption,
            formatting_entities=formatting_entities,
            link_preview=False,
            supports_streaming=True,
            force_document=False,
            thumb=str(thumb) if thumb else None,
            attributes=[
                DocumentAttributeVideo(
                    duration=dur,
                    w=w,
                    h=h,
                    supports_streaming=True,
                )
            ],
        )
    return await client.send_file(
        channel_id,
        path,
        caption=caption,
        formatting_entities=formatting_entities,
        link_preview=False,
    )


async def _publish(
    client,
    channel_id: int,
    item: NewsItem,
    caption: str,
    formatting_entities: list | None = None,
) -> bool:
    media_paths = await _download_tg_media(client, item)
    use_preview = not media_paths and bool(item.url and "t.me/" not in item.url)
    try:
        if media_paths:
            msg = await _send_channel_media(
                client, channel_id, media_paths, caption, formatting_entities
            )
        else:
            msg = await client.send_message(
                channel_id,
                caption,
                formatting_entities=formatting_entities,
                link_preview=use_preview,
                no_webpage=not use_preview,
            )
        return bool(msg)
    except Exception as e:
        log.warning("channel publish failed: %s", e)
        return False
    finally:
        for p in media_paths:
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:
                pass
        if media_paths:
            parent = Path(media_paths[0]).parent
            if parent.name.startswith("news_"):
                import shutil

                shutil.rmtree(parent, ignore_errors=True)


def request_force_post() -> None:
    FORCE_PATH.write_text(_now(), encoding="utf-8")


def is_force_pending() -> bool:
    return FORCE_PATH.exists()


def _consume_force() -> bool:
    if FORCE_PATH.exists():
        FORCE_PATH.unlink(missing_ok=True)
        return True
    return False


async def news_poster_tick(*, force: bool = False) -> bool:
    if not is_enabled():
        return False
    settings = load_settings()
    if not settings.get("linked_account", {}).get("user_id"):
        return False

    state = _load_state()
    force_mode = force or is_force_pending()
    if not force_mode:
        if not _within_post_hours():
            log.debug("news: outside post hours")
            return False
        if not _can_post_today(state):
            log.debug("news: daily limit reached")
            return False
        if not _min_gap_ok(state):
            log.debug("news: min gap not elapsed")
            return False
    elif is_force_pending():
        log.info("news: force post requested")

    from user_client import get_client

    client = await get_client()
    if not client:
        return False

    channel_id = await _resolve_target_channel(client)
    if not channel_id:
        return False

    candidates: list[NewsItem] = []
    for url in settings.get("monitoring", {}).get("news_sites") or DEFAULT_RSS_SOURCES:
        candidates.extend(_fetch_rss(url))
    try:
        candidates.extend(await _fetch_tg_sources(client))
    except Exception as e:
        log.warning("tg sources failed: %s", e)

    item = _pick_candidate(candidates, state)
    if not item:
        if force_mode:
            log.warning("news: force post — no fresh candidates (%d scanned)", len(candidates))
        else:
            log.debug("news: no fresh candidates")
        return False

    caption, entities = _build_post_caption(item)
    ok = await _publish(client, channel_id, item, caption, entities)
    if not ok:
        log.warning("news publish failed: %s", item.title[:80])
        return False

    _mark_published(item, state)
    _save_state(state)
    if force_mode:
        _consume_force()
    log.info("news published: %s -> %s", item.title[:60], channel_id)

    from notify import send_message

    preview = caption[:350] + ("…" if len(caption) > 350 else "")
    try:
        await send_message(
            OWNER_ID,
            f"📰 **Опубликовано** ({'тест' if _cfg().get('test_mode', True) else 'prod'})\n\n{preview}",
        )
    except Exception as e:
        log.debug("owner notify failed: %s", e)
    return True


async def publish_test_now() -> bool:
    """Разовая публикация по запросу владельца (без лимита и паузы)."""
    return await news_poster_tick(force=True)


def publish_test_now_sync(*, timeout: float = 120.0) -> bool:
    """Синхронная обёртка — выполняется в цикле bridge."""
    import asyncio

    coro = publish_test_now()
    try:
        from user_client import _bridge_loop

        if _bridge_loop and _bridge_loop.is_running():
            return bool(asyncio.run_coroutine_threadsafe(coro, _bridge_loop).result(timeout=timeout))
    except Exception as e:
        log.warning("publish_test_now_sync via bridge failed: %s", e)
    try:
        return bool(asyncio.run(coro))
    except Exception as e:
        log.warning("publish_test_now_sync failed: %s", e)
        return False
