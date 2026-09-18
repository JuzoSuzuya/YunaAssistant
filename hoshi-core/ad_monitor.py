#!/usr/bin/env python3
"""Мониторинг бирж рекламы: разбор офферов для SAO VPN, алерты хозяину."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any

from config import DATA, OWNER_ID
from storage import load_settings, save_settings

log = logging.getLogger("hoshi.ad")

STATE_PATH = DATA / "ad_monitor_state.json"
ALERT_CHAT_ID = 7527090820  # чат «Лега» — сюда кидаем находки

# Эталон кампании хозяина: 550₽ → >100 переходов, >50 подписок, 2 оплаты VPN
BENCH_COST = 550
BENCH_CLICKS = 100
BENCH_SUBS = 50
BENCH_COST_PER_CLICK = BENCH_COST / BENCH_CLICKS  # ~5.5₽
BENCH_COST_PER_SUB = BENCH_COST / BENCH_SUBS  # ~11₽

DEFAULT_EXCHANGES = [
    -1001532204567,
    -1001597675229,
    -1002070388712,
    -1001974172031,
    -1001962443433,
]

ENABLE_OWNER_RE = re.compile(
    r"(?:"
    r"реклам.*(?:впн|vpn|sao|сао)|"
    r"(?:впн|vpn|sao|сао).*(?:реклам|бирж|канал)|"
    r"рассмотр.*реклам|"
    r"выгодн.*(?:реклам|предложен|канал)|"
    r"монитор.*(?:бирж|реклам)"
    r")",
    re.I,
)

AD_SCAN_TRIGGER_RE = re.compile(
    r"реклам|бирж|монитор|cpm|оффер|"
    r"рассмотр|выгодн|канал|переход|подписчик|пдп",
    re.I,
)

CHAT_ID_RE = re.compile(r"tg://chat\?id=(\d+)", re.I)
TG_LINK_RE = re.compile(r"(?:https?://)?t\.me/(?:\+|joinchat/)?[\w-]+", re.I)

AD_KEYWORDS_RE = re.compile(
    r"продам|продаю|реклам|размещ|мест[ао]|cpm|закуп|канал|сетк",
    re.I,
)
SKIP_RE = re.compile(
    r"куплю\s+(?:канал|чат|мест|траф)|"
    r"продам\s+vpn.?бот|"
    r"баз[аы]\s+чат|"
    r"телеграм\s+акк|"
    r"крипт|обмен\s+крипт|"
    r"накрутк|seensub|tiktokbst",
    re.I,
)

THEME_GOOD_RE = re.compile(
    r"аним|anime|манг|manga|игр|game|sao|сао|sword|"
    r"vpn|впн|тех|it|код|dev|стрим|twitch|cosplay|косплей|"
    r"новост|news|otaku|отаку|naruto|genshin|genshin",
    re.I,
)
THEME_BAD_RE = re.compile(
    r"hent|хент|эро\b|18\+|adult|фетиш|фемдом|порно|прон|"
    r"модел|onlyfans|foot\b|ног[иа]",
    re.I,
)

PRICE_RE = re.compile(
    r"(?:"
    r"(\d{2,6})\s*(?:₽|руб\.?)|"
    r"(?:₽|руб\.?)\s*(\d{2,6})|"
    r"(\d{2,6})\s*/\s*(?:место|слот|пост)|"
    r"подписчик\w*\s*(?:по|за)\s*(\d{2,5})"
    r")",
    re.I,
)
CPM_RE = re.compile(r"(\d{2,4})\s*cpm", re.I)
SUBS_RE = re.compile(
    r"(?:"
    r"подписчик\w*|subs?|subscribers?|пдп|пдпч"
    r")\s*[:\-—]?\s*(\d[\d\s]{2,8})|"
    r"(\d[\d\s]{2,8})\s*(?:подписчик|subs?|пдп)",
    re.I,
)
VIEWS_RE = re.compile(
    r"(?:"
    r"просмотр\w*|views?|охват\w*|reach|показ\w*"
    r")\s*[:\-—]?\s*(\d[\d\s]{2,9})|"
    r"(\d[\d\s]{2,9})\s*(?:просмотр|views?|охват)",
    re.I,
)
CLICKS_RE = re.compile(
    r"(?:переход\w*|клик\w*|ctr|заявк\w*)"
    r"\s*[:\-—]?\s*(\d[\d\s]{1,7})|"
    r"(\d[\d\s]{1,7})\s*(?:переход|клик)",
    re.I,
)


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
    return load_settings().get("ad_monitor") or {}


def is_enabled() -> bool:
    return bool(_cfg().get("enabled"))


def interval_seconds() -> float:
    return max(300.0, float(_cfg().get("interval_minutes", 15)) * 60)


def _normalize_chat_id(raw: str | int) -> int | None:
    try:
        cid = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if cid > 0 and len(str(cid)) < 12:
        return int(f"-100{cid}")
    return cid


def exchange_chat_ids() -> list[int]:
    settings = load_settings()
    raw = settings.get("monitoring", {}).get("exchanges") or []
    ids: list[int] = []
    for item in raw:
        s = str(item).strip()
        m = CHAT_ID_RE.search(s)
        if m:
            cid = _normalize_chat_id(m.group(1))
        elif s.lstrip("-").isdigit():
            cid = _normalize_chat_id(s)
        else:
            continue
        if cid and cid not in ids:
            ids.append(cid)
    if not ids:
        ids = list(DEFAULT_EXCHANGES)
    return ids


def _parse_int(s: str) -> int:
    return int(re.sub(r"\s+", "", s))


def _text_hash(text: str) -> str:
    return hashlib.sha256((text or "")[:500].encode()).hexdigest()[:16]


def _strip_urls(text: str) -> str:
    return TG_LINK_RE.sub(" ", text or "")


def parse_ad_offer(text: str) -> dict[str, Any] | None:
    if not text or len(text) < 30:
        return None
    if not AD_KEYWORDS_RE.search(text):
        return None
    if SKIP_RE.search(text):
        return None

    stat_text = _strip_urls(text)
    prices: list[int] = []
    for m in PRICE_RE.finditer(text):
        for g in m.groups():
            if g:
                try:
                    v = _parse_int(g)
                    if 30 <= v <= 500_000:
                        prices.append(v)
                except ValueError:
                    pass

    cpm_m = CPM_RE.search(text)
    cpm = int(cpm_m.group(1)) if cpm_m else None

    subs = None
    for m in SUBS_RE.finditer(stat_text):
        for g in m.groups():
            if g:
                try:
                    subs = _parse_int(g)
                    break
                except ValueError:
                    pass
        if subs:
            break

    views = None
    for m in VIEWS_RE.finditer(stat_text):
        for g in m.groups():
            if g:
                try:
                    views = _parse_int(g)
                    break
                except ValueError:
                    pass
        if views:
            break

    clicks = None
    for m in CLICKS_RE.finditer(stat_text):
        for g in m.groups():
            if g:
                try:
                    clicks = _parse_int(g)
                    break
                except ValueError:
                    pass
        if clicks:
            break

    links = TG_LINK_RE.findall(text)
    theme_good = bool(THEME_GOOD_RE.search(text))
    theme_bad = bool(THEME_BAD_RE.search(text))

    price = min(prices) if prices else None
    if not price and cpm and views:
        price = max(50, int(cpm * views / 1000))

    return {
        "price": price,
        "cpm": cpm,
        "subs": subs,
        "views": views,
        "clicks": clicks,
        "links": links[:5],
        "theme_good": theme_good,
        "theme_bad": theme_bad,
        "text_preview": re.sub(r"\s+", " ", text)[:280],
    }


def score_offer(offer: dict[str, Any]) -> tuple[int, list[str]]:
    """Оценка 0–100 и список заметок."""
    score = 0
    notes: list[str] = []

    if offer.get("theme_bad") and not offer.get("theme_good"):
        notes.append("тематика 18+/эро — для VPN слабее")
        score -= 15
    elif offer.get("theme_good"):
        score += 25
        notes.append("тематика близка к аниме/играм/VPN")

    price = offer.get("price")
    subs = offer.get("subs")
    views = offer.get("views")
    clicks = offer.get("clicks")
    cpm = offer.get("cpm")

    if price:
        if price <= 700:
            score += 20
            notes.append(f"цена {price}₽ — в коридоре микрорекламы")
        elif price <= 1500:
            score += 8
        else:
            score -= 10
            notes.append(f"дорого: {price}₽")

        if subs and subs > 0:
            cps = price / subs
            if cps <= BENCH_COST_PER_SUB:
                score += 25
                notes.append(f"~{cps:.0f}₽/подписчик — лучше эталона ({BENCH_COST_PER_SUB:.0f}₽)")
            elif cps <= BENCH_COST_PER_SUB * 2:
                score += 10
                notes.append(f"~{cps:.0f}₽/подписчик — терпимо")
            else:
                score -= 15
                notes.append(f"~{cps:.0f}₽/подписчик — дороже эталона")

        if clicks and clicks > 0:
            cpc = price / clicks
            if cpc <= BENCH_COST_PER_CLICK:
                score += 25
                notes.append(f"~{cpc:.1f}₽/переход — отлично (эталон ~{BENCH_COST_PER_CLICK:.1f}₽)")
            elif cpc <= BENCH_COST_PER_CLICK * 2:
                score += 10
            else:
                score -= 10
                notes.append(f"~{cpc:.1f}₽/переход — выше эталона")

    if cpm is not None:
        if cpm <= 120:
            score += 15
            notes.append(f"CPM {cpm} — низкий")
        elif cpm <= 200:
            score += 5
        elif cpm <= 250:
            score += 0
        else:
            score -= 15
            notes.append(f"CPM {cpm} — высокий для микрорекламы")

    if views and subs:
        ratio = views / max(subs, 1)
        if ratio < 0.05:
            score -= 20
            notes.append("подозрительно: мало просмотров на подписчиков")
        elif ratio > 50:
            score -= 10
            notes.append("подозрительно: охват >> подписчиков")

    if views and not subs and price and views > 5000:
        est_cpm = price * 1000 / views
        if est_cpm <= 150:
            score += 10
            notes.append(f"оценочный CPM ~{est_cpm:.0f}₽")

    if not offer.get("links"):
        score -= 5

    return max(0, min(100, score)), notes


def format_alert(
    offer: dict[str, Any],
    score: int,
    notes: list[str],
    *,
    exchange_title: str = "",
) -> str:
    lines = [f"📢 **Реклама для VPN** — оценка **{score}/100**"]
    if exchange_title:
        lines.append(f"Биржа: {exchange_title}")
    if offer.get("price"):
        lines.append(f"Цена: **{offer['price']}₽**")
    stats = []
    if offer.get("subs"):
        stats.append(f"пдп **{offer['subs']:,}**".replace(",", " "))
    if offer.get("views"):
        stats.append(f"просмотры **{offer['views']:,}**".replace(",", " "))
    if offer.get("clicks"):
        stats.append(f"переходы **{offer['clicks']}**")
    if offer.get("cpm"):
        stats.append(f"CPM **{offer['cpm']}**")
    if stats:
        lines.append(" · ".join(stats))
    if offer.get("links"):
        lines.append(offer["links"][0])
    if notes:
        lines.append("— " + "; ".join(notes[:3]))
    preview = offer.get("text_preview", "")
    if preview:
        lines.append(f"_{preview[:180]}_")
    return "\n".join(lines)


def enable(*, alert_chat_id: int | None = None) -> None:
    settings = load_settings()
    prev = settings.get("ad_monitor") or {}
    exchanges = prev.get("exchange_ids") or []
    if not exchanges:
        exchanges = DEFAULT_EXCHANGES
    mon = settings.setdefault("monitoring", {})
    if not mon.get("exchanges"):
        mon["exchanges"] = [f"tg://chat?id={abs(cid) - 10**12}" for cid in DEFAULT_EXCHANGES]

    settings["ad_monitor"] = {
        "enabled": True,
        "interval_minutes": int(prev.get("interval_minutes") or 15),
        "min_score": int(prev.get("min_score") or 55),
        "alert_chat_id": int(alert_chat_id or prev.get("alert_chat_id") or ALERT_CHAT_ID),
        "exchange_ids": exchanges,
        "enabled_at": prev.get("enabled_at") or _now(),
    }
    save_settings(settings)
    log.info("ad monitor enabled alert_chat=%s", settings["ad_monitor"]["alert_chat_id"])


def disable() -> None:
    settings = load_settings()
    cfg = settings.setdefault("ad_monitor", {})
    cfg["enabled"] = False
    save_settings(settings)


def _extract_exchange_ids_from_text(text: str) -> list[int]:
    ids: list[int] = []
    for m in CHAT_ID_RE.finditer(text or ""):
        cid = _normalize_chat_id(m.group(1))
        if cid and cid not in ids:
            ids.append(cid)
    return ids


def maybe_enable_from_owner_text(text: str) -> bool:
    if not text:
        return False
    ids = _extract_exchange_ids_from_text(text)
    if not ENABLE_OWNER_RE.search(text) and not ids:
        return False
    was_enabled = is_enabled()
    settings = load_settings()
    if ids:
        mon = settings.setdefault("monitoring", {})
        mon["exchanges"] = [f"tg://chat?id={abs(i) - 10**12}" for i in ids]
        cfg = settings.setdefault("ad_monitor", {})
        cfg["exchange_ids"] = ids
        save_settings(settings)
    enable()
    return not was_enabled


def owner_wants_ad_scan(text: str) -> bool:
    if not is_enabled():
        return False
    return bool(AD_SCAN_TRIGGER_RE.search(text or ""))


def ad_policy_prompt() -> str:
    if not is_enabled():
        return ""
    return (
        "**Биржи рекламы:**\n"
        "- **Сама** листаешь все 5 бирж и разбираешь офферы с умом — не полагайся на фоновый цикл.\n"
        "- Эталон: ~5₽/переход, ~11₽/подписчик (кампания 550₽, >100 переходов, >50 подписок).\n"
        "- Пересчитывай CPM, пдп, переходы; отсекай 18+/эро и пустые объявления без цифр.\n"
        "- Хорошие находки — кидай сюда; в чужие чаты сводки не пиши."
    )


async def scan_exchanges_summary(*, limit_per_chat: int = 40, top_n: int = 8) -> str:
    """Свежий разбор бирж для контекста агента."""
    from user_client import get_recent_messages, is_linked

    if not is_linked():
        return "_(биржи: юзербот не подключён)_"

    from chat_router import build_chat_message_link

    seen: set[str] = set()
    ranked: list[tuple[int, list[str], dict[str, Any], str]] = []

    for cid in exchange_chat_ids():
        try:
            messages = await get_recent_messages(cid, limit=limit_per_chat)
        except Exception as e:
            log.debug("ad scan chat %s failed: %s", cid, e)
            continue
        for m in messages:
            if m.get("out"):
                continue
            text = m.get("text") or ""
            h = _text_hash(text)
            if h in seen:
                continue
            offer = parse_ad_offer(text)
            if not offer:
                continue
            seen.add(h)
            score, notes = score_offer(offer)
            mid = m.get("id")
            link = build_chat_message_link(cid, int(mid)) if mid else ""
            ranked.append((score, notes, offer, link))

    if not ranked:
        return "_(свежих офферов на биржах не нашла)_"

    ranked.sort(key=lambda x: -x[0])
    lines = ["**Свежий разбор бирж (сейчас):**"]
    shown = 0
    for score, notes, offer, link in ranked:
        if shown >= top_n:
            break
        parts = [f"• **{score}/100**"]
        if offer.get("price"):
            parts.append(f"{offer['price']}₽")
        stats = []
        if offer.get("subs"):
            stats.append(f"пдп {offer['subs']}")
        if offer.get("clicks"):
            stats.append(f"переходы {offer['clicks']}")
        if offer.get("cpm"):
            stats.append(f"CPM {offer['cpm']}")
        if offer.get("views"):
            stats.append(f"охват {offer['views']}")
        if stats:
            parts.append(", ".join(stats))
        if link:
            parts.append(link)
        elif offer.get("links"):
            parts.append(offer["links"][0])
        if notes:
            parts.append(f"({notes[0]})")
        lines.append(" ".join(parts))
        shown += 1

    if shown == 0:
        lines.append("• Свежих офферов не нашла.")
    return "\n".join(lines)


def scan_exchanges_summary_sync(**kwargs: Any) -> str:
    import asyncio

    try:
        return asyncio.run(scan_exchanges_summary(**kwargs))
    except Exception as e:
        log.warning("ad scan summary failed: %s", e)
        return ""


def _already_seen(h: str, state: dict[str, Any]) -> bool:
    seen = state.setdefault("seen_hashes", [])
    return h in seen


def _mark_seen(h: str, state: dict[str, Any]) -> None:
    seen = state.setdefault("seen_hashes", [])
    seen.append(h)
    if len(seen) > 500:
        state["seen_hashes"] = seen[-400:]


def _cooldown_ok(state: dict[str, Any], *, hours: float = 2.0) -> bool:
    last = state.get("last_alert_at")
    if not last:
        return True
    try:
        return datetime.fromisoformat(last) <= datetime.now() - timedelta(hours=hours)
    except Exception:
        return True


def process_ad_text(
    text: str,
    *,
    chat_id: int = 0,
    exchange_title: str = "",
    force: bool = False,
) -> bool:
    """Разбор одного сообщения; True если отправлен алерт."""
    offer = parse_ad_offer(text)
    if not offer:
        return False
    score, notes = score_offer(offer)
    min_score = int(_cfg().get("min_score") or 55)
    if score < min_score and not force:
        return False

    state = _load_state()
    h = _text_hash(text)
    if _already_seen(h, state):
        return False
    if not force and not _cooldown_ok(state, hours=1.5):
        return False

    alert_chat = int(_cfg().get("alert_chat_id") or ALERT_CHAT_ID)
    msg = format_alert(offer, score, notes, exchange_title=exchange_title)
    try:
        from user_outbox import enqueue_user_message

        enqueue_user_message(alert_chat, msg, owner_approved=True)
        _mark_seen(h, state)
        state["last_alert_at"] = _now()
        state["last_alert_score"] = score
        _save_state(state)
        log.info("ad alert sent score=%s chat=%s", score, alert_chat)
        return True
    except Exception as e:
        log.warning("ad alert failed: %s", e)
        return False


def maybe_process_muted_message(chat_id: int, text: str) -> None:
    if not is_enabled():
        return
    if chat_id not in exchange_chat_ids():
        return
    process_ad_text(text, chat_id=chat_id)


async def ad_monitor_tick() -> None:
    if not is_enabled():
        return
    settings = load_settings()
    if not settings.get("linked_account", {}).get("user_id"):
        return

    from user_client import get_recent_messages, is_linked

    if not is_linked():
        return

    state = _load_state()
    min_score = int(_cfg().get("min_score") or 55)
    found = 0

    for cid in exchange_chat_ids():
        try:
            messages = await get_recent_messages(cid, limit=25)
        except Exception as e:
            log.debug("ad scan chat %s failed: %s", cid, e)
            continue
        title = (
            settings.get("external_chats", {})
            .get("chats", {})
            .get(str(cid), {})
            .get("title")
            or str(cid)
        )
        for m in messages:
            if m.get("out"):
                continue
            text = m.get("text") or ""
            offer = parse_ad_offer(text)
            if not offer:
                continue
            score, notes = score_offer(offer)
            if score < min_score:
                continue
            h = _text_hash(text)
            if _already_seen(h, state):
                continue
            if process_ad_text(text, chat_id=cid, exchange_title=title):
                found += 1
                if found >= 2:
                    break
        if found >= 2:
            break

    state["last_tick_at"] = _now()
    _save_state(state)
