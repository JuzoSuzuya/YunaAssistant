#!/usr/bin/env python3
"""Советы по @SaoVpnBot: сбор продаж из чатов и проактивные подсказки."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from config import DATA, OWNER_ID

log = logging.getLogger("hoshi.sao")

STATS_PATH = DATA / "sao_stats.json"
BOT_MARKERS = ("saovpnbot", "@saovpnbot", "sao vpn")

_PURCHASE_RE = re.compile(
    r"Покупка подписки.*?@(\w+).*?(\d+)\s*₽",
    re.IGNORECASE | re.DOTALL,
)
_PLAN_RE = re.compile(
    r"📦\s*([^—]+)—\s*\*\*(\d+)\s*₽\*\*",
    re.IGNORECASE,
)
_DEVICES_RE = re.compile(r"Устройств:\s*\*\*(\d+)\*\*")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load_stats() -> dict[str, Any]:
    if not STATS_PATH.exists():
        return {"purchases": [], "last_scan_at": ""}
    try:
        return json.loads(STATS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"purchases": [], "last_scan_at": ""}


def _save_stats(data: dict[str, Any]) -> None:
    STATS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def is_sao_purchase_message(text: str) -> bool:
    low = (text or "").lower()
    if "покупка подписки" not in low:
        return False
    return any(m in low for m in BOT_MARKERS)


def parse_purchase(text: str) -> dict[str, Any] | None:
    if not is_sao_purchase_message(text):
        return None
    m = _PURCHASE_RE.search(text)
    plan_m = _PLAN_RE.search(text)
    dev_m = _DEVICES_RE.search(text)
    amount = int(m.group(2)) if m else 0
    username = m.group(1) if m else ""
    plan = (plan_m.group(1).strip() if plan_m else "") or "?"
    devices = int(dev_m.group(1)) if dev_m else 0
    if not amount:
        return None
    return {
        "username": username,
        "amount": amount,
        "plan": plan,
        "devices": devices,
        "at": _now(),
        "text_hash": str(hash(text[:200])),
    }


def record_purchase_from_text(text: str) -> bool:
    """Сохраняет покупку, если ещё не было (по hash текста)."""
    parsed = parse_purchase(text)
    if not parsed:
        return False
    data = _load_stats()
    purchases = data.setdefault("purchases", [])
    if any(p.get("text_hash") == parsed["text_hash"] for p in purchases):
        return False
    purchases.append(parsed)
    # Храним последние 200
    if len(purchases) > 200:
        data["purchases"] = purchases[-200:]
    _save_stats(data)
    log.info("sao purchase recorded: %s ₽ %s", parsed["amount"], parsed["plan"])
    return True


def _purchases_since(hours: float) -> list[dict[str, Any]]:
    cutoff = datetime.now() - timedelta(hours=hours)
    out = []
    for p in _load_stats().get("purchases", []):
        try:
            if datetime.fromisoformat(p["at"]) >= cutoff:
                out.append(p)
        except Exception:
            continue
    return out


def _revenue(purchases: list[dict[str, Any]]) -> int:
    return sum(int(p.get("amount") or 0) for p in purchases)


def _plan_counts(purchases: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for p in purchases:
        plan = (p.get("plan") or "?").strip().lower()
        counts[plan] = counts.get(plan, 0) + 1
    return counts


async def scan_dialogs_for_purchases(*, dialog_limit: int = 25, msg_limit: int = 40) -> int:
    """Сканирует недавние диалоги владельца на уведомления о покупках."""
    from user_client import get_recent_messages, list_dialogs

    found = 0
    dialogs = await list_dialogs(limit=dialog_limit)
    for d in dialogs:
        try:
            messages = await get_recent_messages(d["id"], limit=msg_limit)
        except Exception as e:
            log.debug("scan chat %s failed: %s", d.get("id"), e)
            continue
        for m in messages:
            if record_purchase_from_text(m.get("text", "")):
                found += 1
    data = _load_stats()
    data["last_scan_at"] = _now()
    _save_stats(data)
    return found


def build_sao_digest() -> str:
    """Краткая сводка по продажам для проактивного сообщения."""
    day = _purchases_since(24)
    week = _purchases_since(24 * 7)
    rev_day = _revenue(day)
    rev_week = _revenue(week)
    lines = [
        f"**За 24 ч:** {len(day)} покупок, **{rev_day} ₽**",
        f"**За 7 дней:** {len(week)} покупок, **{rev_week} ₽**",
    ]
    if day:
        plans = _plan_counts(day)
        top = sorted(plans.items(), key=lambda x: -x[1])[:3]
        lines.append("Тарифы сегодня: " + ", ".join(f"{p} ×{n}" for p, n in top))
    return "\n".join(lines)


SAO_TIPS = (
    "Триал 40₽ → пуш за **2 дня** до конца: «продли на 3 мес со скидкой» — это главная конверсия.",
    "В стате Леги **69 подписок / 240 юзеров** (~29%). Сравни с теми, кто **не продлил** триал — там деньги.",
    "Добавь в бота **рефералку** (как KOTI шлёт чеки): «приведи друга — неделя бесплатно».",
    "Если много недельных — **апселл** на 3 мес сразу после первой оплаты, пока доволен скоростью.",
    "Проверь **СБП-вебхук**: если продажи в чате есть, а в админке нет — рассинхрон.",
    "Сделай **/stats** в боте для себя: юзеры, MRR, конверсия триал→месяц — Hoshi сможет советовать точнее.",
    "118 новых за 3 дня — круто. Закрепи: **онбординг** в 3 шага (ключ → тест → «всё ок?») снижает отвал.",
    "Пакет **3 устройства** продаётся — можно добавить тариф «семья 5 устройств» с маржой +30%.",
)


def next_sao_tip() -> str:
    """Совет с учётом свежих продаж."""
    day = _purchases_since(24)
    week = _purchases_since(24 * 7)

    if not day and not week:
        return (
            "Покупок в логе пока нет — Hoshi начнёт считать, когда увижу уведомления "
            "«Покупка подписки» в твоих чатах. Пока: проверь, что бот шлёт тебе алерты на каждую оплату."
        )

    if not day:
        return (
            f"**Сутки без продаж** ({len(week)} за неделю). "
            "Проверь: бот жив, СБП отвечает, триалы не отваливаются на выдаче ключа."
        )

    plans = _plan_counts(day)
    trial_like = sum(n for p, n in plans.items() if "недел" in p or "40" in p)
    if trial_like and trial_like >= len(day) // 2:
        return SAO_TIPS[0]

    long_term = sum(n for p, n in plans.items() if "мес" in p)
    if long_term:
        return (
            f"Сегодня **{len(day)}** продаж на **{_revenue(day)} ₽**, есть долгие тарифы — "
            "хороший знак. Дожми: напоминание тем, у кого триал кончается через 2–3 дня."
        )

    data = _load_stats()
    idx = int(data.get("tip_index", 0))
    tip = SAO_TIPS[idx % len(SAO_TIPS)]
    data["tip_index"] = idx + 1
    _save_stats(data)
    return tip


def format_proactive_sao_message() -> str:
    digest = build_sao_digest()
    tip = next_sao_tip()
    return (
        "📊 **SAO VPN бот** (@SaoVpnBot)\n\n"
        f"{digest}\n\n"
        f"💡 **Совет:** {tip}"
    )
