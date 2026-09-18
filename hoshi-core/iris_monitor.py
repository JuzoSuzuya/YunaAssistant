#!/usr/bin/env python3
"""Мониторинг Iris-биржи: опрос графика/стакана, алерты и автоторговля владельцу."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any

from config import DATA, OWNER_ID
from storage import load_settings, save_settings

log = logging.getLogger("hoshi.iris")

STATE_PATH = DATA / "iris_monitor_state.json"
IRIS_GROUP_CHAT = -1001463965279
IRIS_BOT_USERNAME = "iris_black_bot"
IRIS_SENDER_RE = re.compile(r"iris|black\s*diamond|ирис", re.I)

CHART_RE = re.compile(
    r"За\s+(\d+)\s+дн.*?([+-][\d\s,]+)\s*%",
    re.I | re.DOTALL,
)
BOOK_LINE_RE = re.compile(
    r"`([\d,]+)`\s+ирисок\s*\|\s*`([\d\s]+)`\s+голд",
    re.I,
)
BALANCE_IRIS_RE = re.compile(r"🍬\s*([\d\s]+)\s+ирис(?:ок|ки)", re.I)
BALANCE_GOLD_RE = re.compile(r"🌕\s*([\d\s]+)\s+ирис-голд", re.I)

OWNER_TRADE_CMD_RE = re.compile(
    r"^(?:\.?\s*)?биржа\s+(купить|продать)\s+(\d+)\s+([\d,\.]+)\s*$",
    re.I,
)
OWNER_TRADE_EXAMPLE_RE = re.compile(
    r"(?:типа|например|работает\s+лучше|как\s+)|"
    r"(?:науч|как\s+.*(?:продав|покуп|истор))",
    re.I,
)
OWNER_IRIS_QUERY_RE = re.compile(
    r"(?:^|\s)(?:\.?\s*)?(?:биржа\s+)?(?:"
    r"история(?:\s+продаж|\s+сделок|\s+торгов)?|"
    r"баланс|мешок|"
    r"плюс.*ирис|сколько.*ирис|ириски\s+сейчас"
    r")",
    re.I,
)

ENABLE_OWNER_RE = re.compile(
    r"(?:iris|ирис|бирж).*(?:график|монитор|покуп|продав|изуч)|"
    r"(?:график|монитор|покуп|продав|изуч).*(?:iris|ирис|бирж)|"
    r"(?:резко\s+пиш|алерт|сигнал).*(?:покуп|продав)|"
    r"(?:покуп|продав).*(?:резко\s+пиш|алерт|сигнал)",
    re.I,
)
SILENT_EXTERNAL_RE = re.compile(
    r"(?:не\s+(?:нужно|надо)\s+писать|молчи|без\s+отч[её]т|не\s+пиши\s+сюда).*(?:iris|ирис|бирж|график)|"
    r"(?:iris|ирис|бирж|график).*(?:не\s+(?:нужно|надо)\s+писать|молчи|без\s+отч[её]т|не\s+пиши\s+сюда)",
    re.I,
)
AUTO_TRADE_OWNER_RE = re.compile(
    r"(?:можешь|можно|начни|будь|да)\s*(?:,\s*)?(?:сама|самостоятельно).*(?:делать|торгов|покуп|продав|вс[ёе])|"
    r"(?:сама|самостоятельно).*(?:делать|торгов|покуп|продав|вс[ёе])|"
    r"(?:торгуй|торгов).*(?:сама|авто|самостоятельно)|"
    r"авто.*(?:торгов|trade)",
    re.I,
)
TRADE_OK_RE = re.compile(
    r"купил|продал|заявк|создан|успеш|исполн|✅|📝.*(?:куп|прод)",
    re.I,
)
TRADE_ERR_RE = re.compile(
    r"нет\s+(?:золот|ирис)|недостат|не\s+хватает|ошибк|отмен|❌",
    re.I,
)
CONFIRM_BTN_RE = re.compile(r"да|yes|✅|подтверд|confirm|ок", re.I)

MIN_IRIS_RESERVE = 400
AUTO_TRADE_COOLDOWN = timedelta(minutes=45)
MAX_BUY_SPEND_RATIO = 0.25
MAX_SELL_GOLD_RATIO = 0.5
MIN_SELL_PROFIT = 0.03
MAX_BUY_BID = 2.50
MIN_SELL_BID = 2.53
PROFIT_BASELINE_IRIS = 1175
IRIS_TRADE_TIMEOUT = 30.0


def _iris_cmd(command: str) -> str:
    cmd = (command or "").strip()
    if not cmd.startswith("."):
        cmd = "." + cmd
    return cmd


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
    return load_settings().get("iris_monitor") or {}


def is_enabled() -> bool:
    return bool(_cfg().get("enabled"))


def interval_seconds() -> float:
    return max(180.0, float(_cfg().get("interval_minutes", 5)) * 60)


def is_auto_trade_enabled() -> bool:
    return bool(_cfg().get("auto_trade"))


def enable(*, alert_only: bool = True, silent_external: bool = False, auto_trade: bool | None = None) -> None:
    settings = load_settings()
    prev = settings.get("iris_monitor") or {}
    if auto_trade is None:
        auto_trade = bool(prev.get("auto_trade"))
    settings["iris_monitor"] = {
        "enabled": True,
        "interval_minutes": int(prev.get("interval_minutes") or 5),
        "chart_days": int(prev.get("chart_days") or 30),
        "alert_only": alert_only and not auto_trade,
        "auto_trade": auto_trade,
        "silent_external": silent_external or prev.get("silent_external", False),
        "enabled_at": prev.get("enabled_at") or _now(),
    }
    save_settings(settings)
    log.info(
        "iris monitor enabled alert_only=%s auto_trade=%s silent_external=%s",
        alert_only,
        auto_trade,
        silent_external,
    )


def enable_auto_trade() -> None:
    settings = load_settings()
    prev = settings.get("iris_monitor") or {}
    settings["iris_monitor"] = {
        **prev,
        "enabled": True,
        "auto_trade": True,
        "alert_only": False,
        "silent_external": prev.get("silent_external", True),
        "enabled_at": prev.get("enabled_at") or _now(),
    }
    save_settings(settings)
    log.info("iris auto_trade enabled")


def maybe_enable_from_owner_text(text: str) -> bool:
    if not text:
        return False
    if AUTO_TRADE_OWNER_RE.search(text):
        cfg = _cfg()
        changed = not cfg.get("auto_trade")
        enable_auto_trade()
        return changed or not is_enabled()
    silent = bool(SILENT_EXTERNAL_RE.search(text))
    if not ENABLE_OWNER_RE.search(text) and not silent:
        return False
    cfg = _cfg()
    changed = not is_enabled() or silent or not cfg.get("alert_only", True)
    enable(alert_only=True, silent_external=silent or cfg.get("silent_external", False))
    return changed


def iris_policy_prompt() -> str:
    cfg = _cfg()
    if not is_enabled():
        return ""
    lines = [
        "**Iris-биржа (фоновый мониторинг):**",
        "- Молча изучай график и стакан через `iris_monitor.py` — команды боту Iris не дублируй.",
        "- **Не пиши** в чужие чаты сводки, стакан, курс и «сейчас спокойно» — только по явной просьбе.",
    ]
    if cfg.get("auto_trade"):
        lines.append(
            "- **Автоторговля включена:** покупай/продавай сама осторожно; "
            "докладывай Хозяину только когда сделала сделку."
        )
    else:
        lines.append(
            "- Алерты **ПОКУПАТЬ/ПРОДАВАТЬ** (курс + сумма) — **только Хозяину в бот 1:1**, резко и по делу."
        )
    if cfg.get("silent_external"):
        lines.append("- Хозяин просил **без отчётов во внешние чаты** про Iris — только сигналы ему.")
    return "\n".join(lines)


def _parse_num(s: str) -> float:
    return float(s.replace(" ", "").replace(",", "."))


def _parse_int(s: str) -> int:
    return int(s.replace(" ", "").replace(",", ""))


def parse_chart(text: str) -> dict[str, Any] | None:
    m = CHART_RE.search(text or "")
    if not m:
        return None
    try:
        return {
            "days": int(m.group(1)),
            "change_pct": _parse_num(m.group(2)),
        }
    except (TypeError, ValueError):
        return None


def parse_order_book(text: str) -> dict[str, Any] | None:
    if "стакан" not in (text or "").lower() and "заявки" not in (text or "").lower():
        return None
    sells: list[tuple[float, int]] = []
    buys: list[tuple[float, int]] = []
    section = ""
    for line in (text or "").splitlines():
        low = line.lower()
        if "продаж" in low:
            section = "sell"
            continue
        if "покуп" in low:
            section = "buy"
            continue
        m = BOOK_LINE_RE.search(line)
        if not m:
            continue
        price = _parse_num(m.group(1))
        vol = _parse_int(m.group(2))
        if section == "sell":
            sells.append((price, vol))
        elif section == "buy":
            buys.append((price, vol))
    if not sells and not buys:
        return None
    sells.sort(key=lambda x: x[0])
    buys.sort(key=lambda x: -x[0])
    best_ask = sells[0][0] if sells else None
    best_bid = buys[0][0] if buys else None
    top_wall = max(sells, key=lambda x: x[1]) if sells else (0.0, 0)
    bid_vol_top = sum(v for _, v in buys[:3])
    ask_vol_top = sum(v for _, v in sells[:3])
    spread = (best_ask - best_bid) if best_ask and best_bid else None
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "top_sell_wall_price": top_wall[0],
        "top_sell_wall_vol": top_wall[1],
        "bid_vol_top3": bid_vol_top,
        "ask_vol_top3": ask_vol_top,
        "sells": sells[:8],
        "buys": buys[:8],
    }


def parse_balance(text: str) -> dict[str, int] | None:
    """Парсит мешок владельца из ответа Iris (только ЛС с ботом)."""
    raw = text or ""
    if "мешке" not in raw.lower():
        return None
    iris_m = BALANCE_IRIS_RE.search(raw)
    gold_m = BALANCE_GOLD_RE.search(raw)
    if not iris_m and not gold_m:
        return None
    try:
        out: dict[str, int] = {}
        if iris_m:
            out["iris"] = _parse_int(iris_m.group(1))
        if gold_m:
            out["gold"] = _parse_int(gold_m.group(1))
        return out or None
    except (TypeError, ValueError):
        return None


def _fmt_qty(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def _is_iris_bot_row(row: dict[str, Any]) -> bool:
    name = (row.get("sender_name") or "") + " " + (row.get("sender_username") or "")
    return bool(IRIS_SENDER_RE.search(name))


async def _latest_iris_texts(*, limit: int = 40) -> list[str]:
    from user_client import get_recent_messages_readonly

    texts: list[str] = []
    for chat_id in (IRIS_GROUP_CHAT, await _iris_bot_chat_id()):
        if not chat_id:
            continue
        try:
            msgs = await get_recent_messages_readonly(chat_id, limit=limit)
        except Exception as e:
            log.debug("read chat %s failed: %s", chat_id, e)
            continue
        for m in reversed(msgs):
            if _is_iris_bot_row(m):
                t = (m.get("text") or "").strip()
                if t and t not in texts:
                    texts.append(t)
    return texts


async def _iris_bot_chat_id() -> int | None:
    from user_client import resolve_username_chat_id_readonly

    return await resolve_username_chat_id_readonly(IRIS_BOT_USERNAME)


async def _query_iris_bot(command: str, *, timeout: float = 12.0) -> str:
    from user_client import (
        get_recent_messages_readonly,
        readonly_client,
        resolve_username_chat_id_readonly,
    )

    async with readonly_client() as client:
        if not client:
            return ""
        chat_id = await resolve_username_chat_id_readonly(IRIS_BOT_USERNAME, client=client)
        if not chat_id:
            return ""
        try:
            await client.send_message(chat_id, command)
        except Exception as e:
            log.debug("iris bot command failed: %s", e)
            return ""

        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(1.5)
            msgs = await get_recent_messages_readonly(chat_id, limit=10, client=client)
            for m in reversed(msgs):
                if m.get("out"):
                    continue
                if not _is_iris_bot_row(m):
                    continue
                t = (m.get("text") or "").strip()
                low = t.lower()
                if "график" in low or "стакан" in low or "мешке" in low:
                    return t
    return ""


async def collect_market_snapshot() -> dict[str, Any]:
    """Собирает график, стакан и баланс владельца."""
    texts = await _latest_iris_texts()
    chart = book = None

    for t in texts:
        if not chart:
            chart = parse_chart(t)
        if not book:
            book = parse_order_book(t)

    chart_days = int(_cfg().get("chart_days", 30))
    if not chart:
        t = await _query_iris_bot(_iris_cmd(f"биржа график {chart_days}"))
        if t:
            chart = parse_chart(t)
            texts.insert(0, t)
    if not book:
        t = await _query_iris_bot(_iris_cmd("биржа"))
        if t:
            book = parse_order_book(t)
            texts.insert(0, t)

    balance_iris = balance_gold = None
    t = await _query_iris_bot(_iris_cmd("мешок"))
    if t:
        parsed = parse_balance(t)
        if parsed:
            balance_iris = parsed.get("iris")
            balance_gold = parsed.get("gold")

    return {
        "chart": chart,
        "book": book,
        "balance_iris": balance_iris,
        "balance_gold": balance_gold,
        "collected_at": _now(),
    }


def _suggest_buy_amount(
    iris: int | None, price: float | None
) -> tuple[str, int | None, int | None]:
    """Сколько голда купить за ириски (биржа купить = голд за 🍬)."""
    if iris and iris > 100 and price and price > 0:
        spend = max(200, min(iris // 3, iris - 50))
        gold_qty = max(1, int(spend / price))
        spend_actual = int(gold_qty * price)
        return (
            f"{_fmt_qty(gold_qty)} голд (~{_fmt_qty(spend_actual)} ирисок)",
            gold_qty,
            spend_actual,
        )
    return "", None, None


def _suggest_sell_amount(gold: int | None) -> tuple[str, int | None]:
    """Сколько голда продать за ириски (биржа продать = 🌕 за 🍬)."""
    if gold and gold > 0:
        qty = max(1, gold // 3) if gold >= 3 else gold
        qty = min(qty, gold)
        return f"{_fmt_qty(qty)} голд", qty
    return "", None


def analyze_signals(snapshot: dict[str, Any], prev: dict[str, Any]) -> list[dict[str, Any]]:
    book = snapshot.get("book") or {}
    chart = snapshot.get("chart") or {}
    iris_bal = snapshot.get("balance_iris")
    gold_bal = snapshot.get("balance_gold")
    signals: list[dict[str, Any]] = []

    bid = book.get("best_bid")
    ask = book.get("best_ask")
    wall_vol = int(book.get("top_sell_wall_vol") or 0)
    wall_price = book.get("top_sell_wall_price")
    spread = book.get("spread")
    change = chart.get("change_pct")
    prev_book = prev.get("book") or {}
    prev_wall = int(prev_book.get("top_sell_wall_vol") or 0)

    def _sell_sig(reason: str, price: float) -> dict[str, Any] | None:
        if gold_bal is None or gold_bal <= 0:
            return None
        amount, qty = _suggest_sell_amount(gold_bal)
        if not qty:
            return None
        return {
            "kind": "sell",
            "reason": reason,
            "price": price,
            "amount": amount,
            "qty": qty,
        }

    def _buy_sig(reason: str, price: float) -> dict[str, Any] | None:
        if iris_bal is None or iris_bal <= 100:
            return None
        amount, qty, spend = _suggest_buy_amount(iris_bal, price)
        if not qty:
            return None
        return {
            "kind": "buy",
            "reason": reason,
            "price": price,
            "amount": amount,
            "qty": qty,
            "spend_iris": spend,
        }

    if bid and wall_vol >= 100_000 and wall_price and bid <= wall_price <= (bid + 0.12):
        sig = _sell_sig(
            f"стена {_fmt_qty(wall_vol)} на {wall_price:.2f} — потолок, выше не пройти",
            wall_price,
        )
        if sig:
            signals.append(sig)

    if bid and bid <= 2.52 and wall_vol < 10_000 and (prev_wall >= 50_000 or wall_vol <= 5000):
        sig = _buy_sig(f"низ {bid:.2f}, тяжёлая стена ослабла — хорошая точка входа", bid)
        if sig:
            signals.append(sig)

    if (
        bid
        and spread is not None
        and spread <= 0.06
        and int(book.get("bid_vol_top3") or 0) > int(book.get("ask_vol_top3") or 0) * 2
        and bid <= 2.54
    ):
        sig = _buy_sig(f"спрос перевешивает ({bid:.2f}, спред {spread:.2f})", bid)
        if sig:
            signals.append(sig)

    if change is not None and change >= 45 and ask and wall_vol >= 50_000:
        sig = _sell_sig(f"график +{change:.1f}% — перегрев у стены {ask:.2f}", ask)
        if sig:
            signals.append(sig)

    if (
        bid
        and change is not None
        and change > 15
        and bid <= 2.51
        and wall_vol < 20_000
    ):
        sig = _buy_sig(f"откат к {bid:.2f} при тренде +{change:.1f}%", bid)
        if sig:
            signals.append(sig)

    prev_chart = prev.get("chart") or {}
    prev_change = prev_chart.get("change_pct")
    if change is not None and prev_change is not None:
        delta = change - prev_change
        if delta <= -3 and bid and bid <= 2.55:
            sig = _buy_sig(f"график просел {delta:+.1f}п.п. — отскок с {bid:.2f}", bid)
            if sig:
                signals.append(sig)
        if delta >= 5 and ask and change >= 35:
            sig = _sell_sig(f"график разогнался +{delta:.1f}п.п. — фиксируй у {ask:.2f}", ask)
            if sig:
                signals.append(sig)

    return signals


def _signal_key(sig: dict[str, Any]) -> str:
    price = sig.get("price")
    p = f"{float(price):.2f}" if price else "?"
    return f"{sig.get('kind')}:{p}:{sig.get('reason', '')[:40]}"


def _cooldown_ok(sig: dict[str, Any], state: dict[str, Any]) -> bool:
    sent = state.get("last_alerts") or {}
    key = _signal_key(sig)
    last = sent.get(key)
    if not last:
        return True
    try:
        return datetime.now() - datetime.fromisoformat(last) > timedelta(hours=1)
    except Exception:
        return True


def _mark_alert(sig: dict[str, Any], state: dict[str, Any]) -> None:
    sent = state.setdefault("last_alerts", {})
    sent[_signal_key(sig)] = _now()
    if len(sent) > 40:
        for k in list(sent.keys())[:-30]:
            sent.pop(k, None)


def format_alert(sig: dict[str, Any], snapshot: dict[str, Any]) -> str:
    kind = sig.get("kind")
    emoji = "🟢" if kind == "buy" else "🔴"
    action = "ПОКУПАТЬ" if kind == "buy" else "ПРОДАВАТЬ"
    price = sig.get("price")
    price_s = f"{price:.2f}".replace(".", ",") if price else "?"
    amount = sig.get("amount", "?")
    qty = sig.get("qty")
    iris_bal = snapshot.get("balance_iris")
    gold_bal = snapshot.get("balance_gold")
    lines = [
        f"{emoji} **Iris — {action} СЕЙЧАС**",
        "",
    ]
    if kind == "sell" and qty:
        lines.append(f"**Продать {amount}** по **{price_s}** ирис/голд")
        lines.append(f"`.биржа продать {qty} {price_s}`")
    elif kind == "buy" and qty:
        lines.append(f"**Купить {amount}** по **{price_s}** ирис/голд")
        lines.append(f"`.биржа купить {qty} {price_s}`")
    else:
        lines.append(f"**{amount}** по **{price_s}** ирис/голд")
    lines.append(f"**Сигнал:** {sig.get('reason', '')}")
    if iris_bal is not None or gold_bal is not None:
        bag = []
        if iris_bal is not None:
            bag.append(f"**{_fmt_qty(iris_bal)}** 🍬")
        if gold_bal is not None:
            bag.append(f"**{_fmt_qty(gold_bal)}** 🌕")
        lines.append(f"Мешок: {' · '.join(bag)}")
    chart = snapshot.get("chart")
    if chart:
        lines.append(f"График {chart.get('days', '?')}д: {chart.get('change_pct', 0):+.2f}%")
    return "\n".join(lines)


def _fmt_price_iris(price: float) -> str:
    return f"{price:.2f}".replace(".", ",")


def _is_iris_bot_message(msg) -> bool:
    sender = getattr(msg, "sender", None)
    if not sender:
        return False
    name = (
        (getattr(sender, "first_name", "") or "")
        + " "
        + (getattr(sender, "username", "") or "")
    )
    return bool(IRIS_SENDER_RE.search(name))


def _trade_cooldown_ok(state: dict[str, Any]) -> bool:
    last = state.get("last_trade_at")
    if not last:
        return True
    try:
        return datetime.now() - datetime.fromisoformat(last) > AUTO_TRADE_COOLDOWN
    except Exception:
        return True


def _mark_trade(state: dict[str, Any], sig: dict[str, Any], result: dict[str, Any]) -> None:
    state["last_trade_at"] = _now()
    trades = state.setdefault("trades", [])
    trades.append(
        {
            "at": _now(),
            "kind": sig.get("kind"),
            "qty": sig.get("qty"),
            "price": sig.get("price"),
            "ok": bool(result.get("ok")),
            "command": result.get("command"),
            "detail": (result.get("detail") or "")[:300],
        }
    )
    state["trades"] = trades[-20:]


def _auto_trade_sizes(sig: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any] | None:
    kind = sig.get("kind")
    iris = int(snapshot.get("balance_iris") or 0)
    gold = int(snapshot.get("balance_gold") or 0)
    price = float(sig.get("price") or 0)
    if not price:
        return None
    if kind == "buy":
        spendable = iris - MIN_IRIS_RESERVE
        if spendable < 100:
            return None
        spend = max(100, int(spendable * MAX_BUY_SPEND_RATIO))
        gold_qty = max(1, int(spend / price))
        spend_actual = int(gold_qty * price)
        if spend_actual + MIN_IRIS_RESERVE > iris:
            return None
        return {
            **sig,
            "qty": gold_qty,
            "spend_iris": spend_actual,
            "amount": f"{_fmt_qty(gold_qty)} голд (~{_fmt_qty(spend_actual)} ирисок)",
        }
    if kind == "sell":
        if gold <= 0:
            return None
        qty = max(1, int(gold * MAX_SELL_GOLD_RATIO))
        qty = min(qty, gold)
        return {**sig, "qty": qty, "amount": f"{_fmt_qty(qty)} голд"}
    return None


def _auto_trade_allowed(sig: dict[str, Any], snapshot: dict[str, Any], state: dict[str, Any]) -> bool:
    if not _trade_cooldown_ok(state):
        return False
    book = snapshot.get("book") or {}
    bid = book.get("best_bid")
    kind = sig.get("kind")
    price = float(sig.get("price") or 0)
    if kind == "buy":
        if not bid or bid > MAX_BUY_BID:
            return False
        if price > MAX_BUY_BID:
            return False
        return bool(sig.get("qty"))
    if kind == "sell":
        gold = int(snapshot.get("balance_gold") or 0)
        if gold <= 0:
            return False
        entry = float(state.get("last_buy_price") or 0)
        if bid and bid < MIN_SELL_BID:
            if entry and bid < entry + MIN_SELL_PROFIT:
                return False
        if entry and bid and bid < entry + MIN_SELL_PROFIT:
            return False
        return bool(sig.get("qty"))
    return False


async def _wait_iris_trade_result(
    chat_id: int, after_msg_id: int, *, timeout: float = IRIS_TRADE_TIMEOUT
) -> tuple[bool, str]:
    from user_client import get_client

    client = await get_client()
    if not client:
        return False, "нет юзербота"
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    confirmed = False
    while loop.time() < deadline:
        await asyncio.sleep(1.5)
        async for msg in client.iter_messages(chat_id, min_id=after_msg_id, limit=8):
            if msg.out or not _is_iris_bot_message(msg):
                continue
            text = (msg.text or "").strip()
            if TRADE_OK_RE.search(text):
                return True, text
            if TRADE_ERR_RE.search(text):
                return False, text
            if msg.buttons and not confirmed:
                clicked = False
                for i, row in enumerate(msg.buttons):
                    for j, btn in enumerate(row):
                        label = btn.text or ""
                        if CONFIRM_BTN_RE.search(label):
                            try:
                                await msg.click(i, j)
                                clicked = True
                                break
                            except Exception as e:
                                log.debug("iris confirm click failed: %s", e)
                    if clicked:
                        break
                if not clicked:
                    try:
                        await msg.click(0, 0)
                        clicked = True
                    except Exception:
                        pass
                if clicked:
                    confirmed = True
                    await asyncio.sleep(2.5)
            elif re.search(r"подтверд|уверен", text, re.I) and not confirmed:
                try:
                    await client.send_message(chat_id, "да")
                    confirmed = True
                    await asyncio.sleep(2.5)
                except Exception as e:
                    log.debug("iris text confirm failed: %s", e)
    return False, "таймаут подтверждения"


async def _execute_iris_trade(sig: dict[str, Any]) -> dict[str, Any]:
    kind = sig.get("kind")
    qty = int(sig.get("qty") or 0)
    price = float(sig.get("price") or 0)
    if kind not in ("buy", "sell") or qty <= 0 or price <= 0:
        return {"ok": False, "error": "bad params"}
    price_s = _fmt_price_iris(price)
    verb = "купить" if kind == "buy" else "продать"
    cmd = _iris_cmd(f"биржа {verb} {qty} {price_s}")
    from user_client import get_client

    client = await get_client()
    chat_id = await _iris_bot_chat_id()
    if not client or not chat_id:
        return {"ok": False, "error": "no client", "command": cmd}
    try:
        sent = await client.send_message(chat_id, cmd)
        after_id = int(sent.id) if sent else 0
    except Exception as e:
        return {"ok": False, "error": str(e), "command": cmd}
    ok, detail = await _wait_iris_trade_result(chat_id, after_id)
    return {"ok": ok, "detail": detail, "command": cmd}


def format_trade_report(
    sig: dict[str, Any], result: dict[str, Any], snapshot: dict[str, Any]
) -> str:
    kind = sig.get("kind")
    ok = bool(result.get("ok"))
    emoji = "✅" if ok else "⚠️"
    action = "Купила" if kind == "buy" else "Продала"
    if not ok:
        action = "Не удалось " + ("купить" if kind == "buy" else "продать")
    price = sig.get("price")
    price_s = _fmt_price_iris(float(price)) if price else "?"
    qty = sig.get("qty")
    iris_bal = snapshot.get("balance_iris")
    gold_bal = snapshot.get("balance_gold")
    lines = [
        f"{emoji} **Iris — {action}**",
        "",
        f"**{sig.get('amount', qty)}** по **{price_s}**",
        f"`{result.get('command', '')}`",
        f"**Почему:** {sig.get('reason', '')}",
    ]
    detail = (result.get("detail") or result.get("error") or "").replace("\n", " ")
    if detail:
        lines.append(f"**Ответ Iris:** {detail[:220]}")
    if iris_bal is not None or gold_bal is not None:
        bag = []
        if iris_bal is not None:
            bag.append(f"**{_fmt_qty(iris_bal)}** 🍬")
        if gold_bal is not None:
            bag.append(f"**{_fmt_qty(gold_bal)}** 🌕")
        lines.append(f"Мешок: {' · '.join(bag)}")
    book = snapshot.get("book") or {}
    bid, ask = book.get("best_bid"), book.get("best_ask")
    if bid and ask:
        lines.append(f"Курс: **{_fmt_price_iris(bid)}** / **{_fmt_price_iris(ask)}**")
    return "\n".join(lines)


def _profit_baseline(state: dict[str, Any]) -> int:
    return int(state.get("profit_baseline") or PROFIT_BASELINE_IRIS)


def format_profit_summary(snapshot: dict[str, Any], state: dict[str, Any]) -> str:
    iris = snapshot.get("balance_iris")
    gold = int(snapshot.get("balance_gold") or 0)
    book = snapshot.get("book") or {}
    bid = float(book.get("best_bid") or 0)
    baseline = _profit_baseline(state)
    if iris is None:
        return "Не смогла прочитать мешок — напиши `.мешок` боту Iris."
    total = int(iris) + int(gold * bid) if bid else int(iris)
    delta = total - baseline
    sign = "+" if delta >= 0 else ""
    lines = [
        "**Iris — плюс по ирискам**",
        "",
        f"Сейчас: **{_fmt_qty(iris)}** 🍬 + **{_fmt_qty(gold)}** 🌕",
    ]
    if bid:
        lines.append(f"В 🍬 по курсу **{_fmt_price_iris(bid)}**: **~{_fmt_qty(total)}**")
    lines.append(f"Было (старт): **{_fmt_qty(baseline)}** 🍬")
    lines.append(f"**Итог: {sign}{_fmt_qty(delta)}** 🍬")
    ask = book.get("best_ask")
    if bid and ask:
        lines.append(f"Курс: **{_fmt_price_iris(bid)}** / **{_fmt_price_iris(float(ask))}**")
    return "\n".join(lines)


def format_trade_history(state: dict[str, Any], *, limit: int = 10) -> str:
    trades = list(reversed(state.get("trades") or []))[:limit]
    if not trades:
        return "История сделок пуста — автоторговля ещё не фиксировала."
    lines = ["**Iris — история сделок**", ""]
    for t in trades:
        ok = "✅" if t.get("ok") else "⚠️"
        kind = "купила" if t.get("kind") == "buy" else "продала"
        if not t.get("ok"):
            kind = "не " + kind
        price = t.get("price")
        price_s = _fmt_price_iris(float(price)) if price else "?"
        qty = t.get("qty") or "?"
        at = (t.get("at") or "")[:16].replace("T", " ")
        lines.append(f"{ok} `{at}` — {kind} **{qty}** 🌕 @ **{price_s}**")
        cmd = t.get("command") or ""
        if cmd:
            lines.append(f"   `{cmd}`")
    lines.append("")
    lines.append("Свежий мешок: `.мешок` · стакан: `.биржа`")
    return "\n".join(lines)


async def format_balance_report() -> str:
    t = await _query_iris_bot(_iris_cmd("мешок"), timeout=15.0)
    parsed = parse_balance(t or "")
    if not parsed:
        return "Мешок не прочитался — Iris не ответил."
    iris = parsed.get("iris")
    gold = parsed.get("gold")
    snap = await collect_market_snapshot()
    book = snap.get("book") or {}
    bid = book.get("best_bid")
    ask = book.get("best_ask")
    lines = ["**Iris — мешок**", ""]
    if iris is not None:
        lines.append(f"**{_fmt_qty(iris)}** 🍬 ирисок")
    if gold is not None:
        lines.append(f"**{_fmt_qty(gold)}** 🌕 ирис-голд")
    if bid and ask:
        lines.append(f"Курс: **{_fmt_price_iris(float(bid))}** / **{_fmt_price_iris(float(ask))}**")
    return "\n".join(lines)


def _extract_owner_trade_command(text: str) -> re.Match | None:
    """Торговля только если первая строка — чистая `.биржа …`, не пример в тексте."""
    raw = (text or "").strip()
    if not raw:
        return None
    first = raw.split("\n")[0].strip()
    m = OWNER_TRADE_CMD_RE.match(first)
    if m:
        return m
    if "\n" not in raw and not OWNER_TRADE_EXAMPLE_RE.search(raw):
        return OWNER_TRADE_CMD_RE.match(raw)
    return None


async def try_owner_iris_command(text: str) -> str | None:
    """Быстрые команды Хозяина: баланс, история, торговля через `.биржа`."""
    raw = (text or "").strip()
    if not raw:
        return None
    head = raw.split("\n")[0].strip()
    state = _load_state()

    m = _extract_owner_trade_command(raw)
    if m:
        kind = "buy" if m.group(1).lower() == "купить" else "sell"
        qty = int(m.group(2))
        price = _parse_num(m.group(3))
        sig = {"kind": kind, "qty": qty, "price": price, "amount": f"{_fmt_qty(qty)} голд"}
        result = await _execute_iris_trade(sig)
        snap = await collect_market_snapshot()
        _mark_trade(state, sig, result)
        if result.get("ok") and kind == "buy":
            state["last_buy_price"] = price
        _save_state(state)
        return format_trade_report(sig, result, snap)

    probe = raw if len(raw) <= 600 else head
    if len(head) > 120 and not OWNER_IRIS_QUERY_RE.search(probe):
        return None
    if not OWNER_IRIS_QUERY_RE.search(probe):
        return None

    parts: list[str] = []
    snap = None
    if re.search(r"история", probe, re.I):
        parts.append(format_trade_history(state))
    if re.search(r"плюс|сколько", probe, re.I):
        snap = await collect_market_snapshot()
        parts.append(format_profit_summary(snap, state))
    if re.search(r"науч|как\s+.*(?:продав|покуп|истор)", probe, re.I):
        parts.append(
            "**Iris — команды**\n\n"
            "`.биржа продать 10 2,62` — продать голд\n"
            "`.биржа купить 77 2,50` — купить голд\n"
            "`.мешок` — баланс · `.биржа` — стакан"
        )
    if parts:
        return "\n\n".join(parts)
    return await format_balance_report()


async def _send_owner_iris_message(text: str) -> None:
    from bot_branches import (
        branch_topic_id,
        format_branch_reply_header,
        get_iris_branch,
        reply_already_has_branch_header,
    )
    from notify import send_message

    branch = get_iris_branch()
    branch_name = (branch or {}).get("name")
    topic_id = branch_topic_id(branch)
    if branch_name and not reply_already_has_branch_header(text, branch_name):
        text = format_branch_reply_header(branch_name) + text
    await send_message(OWNER_ID, text, message_thread_id=topic_id)


async def iris_monitor_tick() -> None:
    if not is_enabled():
        return
    settings = load_settings()
    if not settings.get("linked_account", {}).get("user_id"):
        return

    state = _load_state()
    try:
        snapshot = await collect_market_snapshot()
    except Exception as e:
        log.warning("iris snapshot failed: %s", e)
        return

    if not snapshot.get("book") and not snapshot.get("chart"):
        log.debug("iris: no market data yet")
        return

    prev = state.get("last_snapshot") or {}
    signals = analyze_signals(snapshot, prev)
    state["last_snapshot"] = snapshot
    state["last_tick_at"] = _now()
    _save_state(state)

    if not signals:
        log.debug("iris tick: no signals")
        return

    auto = is_auto_trade_enabled()
    limit = 1 if auto else 2
    for sig in signals[:limit]:
        if auto:
            if not _trade_cooldown_ok(state):
                continue
            adj = _auto_trade_sizes(sig, snapshot)
            if not adj or not _auto_trade_allowed(adj, snapshot, state):
                log.debug("iris auto trade skipped: %s", sig.get("kind"))
                continue
            result = await _execute_iris_trade(adj)
            _mark_trade(state, adj, result)
            if result.get("ok"):
                if adj.get("kind") == "buy":
                    state["last_buy_price"] = adj.get("price")
                try:
                    fresh = await collect_market_snapshot()
                    for k in ("balance_iris", "balance_gold"):
                        if fresh.get(k) is not None:
                            snapshot[k] = fresh[k]
                            state.setdefault("last_snapshot", {})[k] = fresh[k]
                except Exception:
                    pass
            msg = format_trade_report(adj, result, snapshot)
            try:
                await _send_owner_iris_message(msg)
                _mark_alert(adj, state)
                log.info(
                    "iris auto trade %s ok=%s @ %s",
                    adj.get("kind"),
                    result.get("ok"),
                    adj.get("price"),
                )
            except Exception as e:
                log.warning("iris trade report failed: %s", e)
            _save_state(state)
            return

        if not _cooldown_ok(sig, state):
            continue
        msg = format_alert(sig, snapshot)
        try:
            await _send_owner_iris_message(msg)
            _mark_alert(sig, state)
            log.info("iris alert sent: %s @ %s", sig.get("kind"), sig.get("price"))
        except Exception as e:
            log.warning("iris alert failed: %s", e)
    _save_state(state)
