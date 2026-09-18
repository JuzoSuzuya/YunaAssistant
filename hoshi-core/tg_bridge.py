#!/usr/bin/env python3
"""Telegram ingress: настройки, диалог с агентом, очередь задач."""
from __future__ import annotations

import asyncio
import html
import logging
import re
import sys
import uuid
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from account_linker import (
    api_ready,
    apply_owner_secrets,
    confirm_2fa,
    drop_link_client,
    extract_login_code,
    is_login_blocked_notice,
    normalize_phone,
    parse_telegram_api,
    save_telegram_api,
    start_link,
    start_qr_link,
    unlink_account,
    wait_qr_link,
)
from agent_prompt import detect_task_kind
from text_format import sanitize_owner_incoming_text
from chat_router import (
    DISABLE_DM_RE,
    ENABLE_CHAT_WRITE_RE,
    ENABLE_DM_RE,
    ENABLE_RESPOND_RE,
    SEE_CHAT_RE,
    apply_owner_chat_command,
    is_global_groups_silence_command,
    is_owner_behavior_policy_command,
    is_all_dms_no_groups_policy_command,
    is_restricted_dm_policy_command,
    resolve_bot_thread_context,
    register_bot_thread_context,
    apply_owner_global_preferences_from_text,
    enrich_owner_task,
    is_bot_chat_id,
    is_chat_write_forbidden,
    is_external_ack,
    is_mute_command,
    is_owner_routing_complaint,
    looks_like_bot_echo,
    owner_wants_external_reply,
    owner_wants_routed_chat_reply,
    sanitize_external_chats,
)
from chat_watcher import restart_chat_watcher, start_chat_watcher, stop_chat_watcher
from health_watch import (
    close_proposal,
    execute_proposal_action,
    get_proposal,
    health_check_issues,
)
from user_client import set_bridge_loop
from user_outbox import list_pending_actions, mark_action_done
from config import (
    AGENT_EXIT_TRIGGERS,
    AGENT_TRIGGERS,
    BOT_TOKEN,
    OWNER_ID,
)
from storage import (
    append_agent_message,
    enqueue_task,
    load_agent_session,
    load_settings,
    save_settings,
    set_pending_images,
    take_pending_images,
)
from tg_media import caption_from_messages, download_images

logging.basicConfig(
    format="[%(asctime)s] %(levelname)s %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("hoshi.bridge")

_MEDIA_GROUP_DELAY = 2.0
_media_group_buffers: dict[str, list] = {}
_media_group_tasks: dict[str, asyncio.Task] = {}
_media_group_captions: dict[str, str] = {}
_qr_wait_tasks: dict[int, asyncio.Task] = {}

RESEND_TRIGGERS = {
    "повтор",
    "resend",
    "новый код",
    "новыйкод",
    "ещё раз",
    "еще раз",
}


def is_owner(user_id: int | None) -> bool:
    return user_id == OWNER_ID


def normalize_trigger(text: str) -> str:
    return text.strip().lower().lstrip("/")


def is_agent_start(text: str) -> bool:
    return normalize_trigger(text) in {t.lstrip("/") for t in AGENT_TRIGGERS} or text.strip().lower() in AGENT_TRIGGERS


def is_agent_exit(text: str) -> bool:
    t = text.strip().lower()
    word = normalize_trigger(text)
    return t in AGENT_EXIT_TRIGGERS or word in {x.lstrip("/") for x in AGENT_EXIT_TRIGGERS}


def is_wizard_cancel(text: str) -> bool:
    return text.strip().lower() in {"отмена", "cancel", "назад", "exit", "/exit", "выход"}


def is_linking_step(step: str) -> bool:
    return step in {"await_api", "await_phone", "await_code", "await_2fa", "await_qr"}


def has_pending_link(s: dict) -> bool:
    link = s.get("account_link", {})
    return bool(link.get("phone") and link.get("phone_code_hash"))


def is_resend_request(text: str) -> bool:
    return text.strip().lower() in RESEND_TRIGGERS


def is_pure_link_code(text: str) -> bool:
    return bool(re.fullmatch(r"\d{4,8}", text.strip().replace(" ", "")))


def should_try_link_code(text: str, link: dict, step: str) -> bool:
    if is_pure_link_code(text):
        return True
    if is_linking_step(step) or has_pending_link(link):
        return extract_login_code(text) is not None
    return False


def _message_forwarded(message) -> bool:
    return bool(getattr(message, "forward_origin", None))


def _cli_code_hint() -> str:
    return (
        "Код нельзя вводить в боте — Telegram сразу его инвалидирует.\n\n"
        "На сервере выполни:\n"
        "<code>./hoshi_ctl.sh link phone</code>\n\n"
        "Или только код (если номер уже запрошен):\n"
        "<code>./hoshi_ctl.sh link code</code>\n\n"
        "QR без кода: /settings → Аккаунт → <b>Войти по QR</b>"
    )


async def _try_confirm_link_code(
    update: Update,
    text: str,
    link: dict,
    *,
    forwarded: bool = False,
) -> bool:
    """Пробует принять код привязки. True = обработано."""
    link = load_settings().get("account_link", link)
    code = extract_login_code(text)
    if not code:
        return False

    if is_login_blocked_notice(text):
        await update.message.reply_text(
            "Telegram <b>заблокировал вход</b> — код нельзя пересылать в чат.\n\n"
            "Попробуй <b>QR</b>: /settings → Аккаунт → <b>Войти по QR</b>\n"
            "или CLI: <code>./hoshi_ctl.sh link qr</code>",
            parse_mode="HTML",
        )
        return True

    await update.message.reply_text(_cli_code_hint(), parse_mode="HTML")
    return True


async def _send_link_phone(
    update: Update,
    phone: str,
    *,
    resend: bool = False,
    force_sms: bool = False,
) -> None:
    ok, msg = await start_link(phone, resend=resend, force_sms=force_sms)
    if not ok and not api_ready():
        s = load_settings()
        s["account_link"]["step"] = "await_api"
        save_settings(s)
    await update.message.reply_text(msg, parse_mode="HTML")


async def _run_qr_wait(reply_message) -> None:
    async def on_refresh(_url: str, png: bytes) -> None:
        await reply_message.reply_photo(
            photo=png,
            caption="🔄 <b>QR обновлён</b> — сканируй сразу (~30 сек).",
            parse_mode="HTML",
        )

    ok, msg, need_pw = await wait_qr_link(timeout=300.0, on_refresh=on_refresh)
    if need_pw:
        await reply_message.reply_text(msg)
        return
    await reply_message.reply_text(msg, parse_mode="HTML" if "<" in msg else None)
    if ok:
        clear_wizard_step()
        if await restart_chat_watcher():
            log.info("chat watcher started after QR link")


def clear_wizard_step(s: dict | None = None, *, keep_link_data: bool = False) -> None:
    s = s or load_settings()
    link = s.setdefault("account_link", {})
    link["step"] = ""
    if not keep_link_data:
        phone = link.get("phone", "")
        link["phone"] = ""
        link["phone_code_hash"] = ""
        if phone:
            asyncio.create_task(drop_link_client(phone))
    save_settings(s)


def api_setup_text() -> str:
    return (
        "Нужны API-данные с https://my.telegram.org\n\n"
        "Пришли <b>api_id</b> и <b>api_hash</b> одним сообщением, например:\n"
        "<code>20319869\nae8af501c3abe02a57ca31792d9170ba</code>\n\n"
        "Или с подписями API_id / API_hash."
    )


def settings_keyboard() -> InlineKeyboardMarkup:
    s = load_settings()
    linked = s["linked_account"]
    linked_label = "✅" if linked.get("user_id") else "❌"
    write_label = "✅" if s["permissions"]["can_write_chats"] else "❌"
    dm_label = "✅" if s["permissions"].get("can_write_private_dms", True) else "❌"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"Аккаунт {linked_label}", callback_data="set:account")],
            [InlineKeyboardButton(f"Писать в чаты {write_label}", callback_data="set:write_toggle")],
            [InlineKeyboardButton(f"Личные сообщения {dm_label}", callback_data="set:dm_toggle")],
            [InlineKeyboardButton("Посты в день", callback_data="set:posts_day")],
            [InlineKeyboardButton("Стиль постов", callback_data="set:posts_style")],
            [InlineKeyboardButton("Наш канал", callback_data="set:channel")],
            [InlineKeyboardButton("Источники новостей", callback_data="set:sources")],
            [InlineKeyboardButton("ТГ-каналы для анализа", callback_data="set:tg_channels")],
            [InlineKeyboardButton("Биржи рекламы", callback_data="set:exchanges")],
            [InlineKeyboardButton("Статус агента", callback_data="set:agent_status")],
        ]
    )


def settings_text() -> str:
    s = load_settings()
    linked = s["linked_account"]
    linked_info = "не привязан"
    if linked.get("user_id"):
        uname = linked.get("username") or linked.get("first_name") or linked["user_id"]
        linked_info = f"@{uname} ({linked.get('phone', '')})"

    agent = s["agent"]
    dialog = "активен" if agent.get("dialog_active") else "выключен"
    msgs = len(load_agent_session().get("messages", []))
    from bot_branches import get_active_branch

    branch = get_active_branch()
    if branch:
        branch_info = f"🌿 {html.escape(branch.get('name') or '?')} (<code>{html.escape(branch.get('id') or '')}</code>)"
    else:
        branch_info = "нет"

    return (
        "<b>⚙️ Настройки Hoshi</b>\n\n"
        f"<b>Аккаунт:</b> {linked_info}\n"
        f"<b>Писать в чаты:</b> {'да' if s['permissions']['can_write_chats'] else 'нет (только чтение)'}\n"
        f"<b>Личные сообщения:</b> {'да' if s['permissions'].get('can_write_private_dms', True) else 'нет'}\n"
        f"<b>Постов в день:</b> {s['posts']['per_day']}\n"
        f"<b>Стиль:</b> {s['posts']['style']}\n"
        f"<b>Канал:</b> {s['channel'].get('username') or 'не задан'}\n"
        f"<b>Источники:</b> {len(s['monitoring']['news_sites'])}\n"
        f"<b>ТГ-каналы:</b> {len(s['monitoring']['telegram_channels'])}\n"
        f"<b>Биржи:</b> {len(s['monitoring']['exchanges'])}\n"
        f"<b>Диалог агента:</b> {dialog}\n"
        f"<b>Активная ветка:</b> {branch_info}\n"
        f"<b>История сообщений:</b> {msgs} (не удаляется при /exit)\n\n"
        "Напиши <code>agent</code> или <code>cursor</code> чтобы начать диалог.\n"
        "Проси исправить код бота прямо здесь — агент правит файлы сам.\n"
        "<code>/exit</code> — завершить (с подтверждением)."
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not is_owner(update.effective_user.id):
        if update.message:
            await update.message.reply_text("Бот доступен только владельцу.")
        return
    await update.message.reply_text(
        "Привет! Я Hoshi — твой агент для аниме-канала.\n\n"
        "/settings — настройки\n"
        "agent / cursor — начать диалог\n"
        "Проси исправить бота прямо в чате — я правлю свой код\n"
        "/exit — завершить диалог",
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not is_owner(update.effective_user.id):
        return
    s = load_settings()
    step = s.get("account_link", {}).get("step", "")
    if step in ("await_code", "await_2fa"):
        await update.message.reply_text(
            "Идёт привязка аккаунта — пришли код из чата <b>Telegram</b> в приложении"
            + (" или пароль 2FA." if step == "await_2fa" else " (не SMS).")
            + "\nОтмена: <code>отмена</code>",
            parse_mode="HTML",
        )
        return
    if step and not is_linking_step(step):
        clear_wizard_step(s)
    elif step == "await_api":
        clear_wizard_step(s, keep_link_data=True)
    await update.message.reply_text(
        settings_text(),
        parse_mode="HTML",
        reply_markup=settings_keyboard(),
    )


async def on_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user or not is_owner(query.from_user.id):
        return
    await query.answer()

    data = query.data or ""
    s = load_settings()

    if data == "set:account":
        s["account_link"]["step"] = ""
        save_settings(s)
        api_note = "API настроен ✅" if api_ready() else "API не настроен ❌"
        rows = []
        if not api_ready():
            rows.append([InlineKeyboardButton("Настроить API", callback_data="acc:api_setup")])
        rows.extend(
            [
                [InlineKeyboardButton("Привязать", callback_data="acc:link")],
                [InlineKeyboardButton("Войти по QR", callback_data="acc:qr")],
                [InlineKeyboardButton("Отвязать", callback_data="acc:unlink")],
                [InlineKeyboardButton("« Назад", callback_data="set:back")],
            ]
        )
        await query.edit_message_text(
            f"<b>Аккаунт Telegram</b>\n{api_note}\n\n"
            "Привязка нужна чтобы читать чаты и публиковать посты с твоего аккаунта.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    if data == "acc:api_setup":
        s["account_link"]["step"] = "await_api"
        save_settings(s)
        await query.edit_message_text(
            api_setup_text(),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Отмена", callback_data="set:account")]]),
        )
        return

    if data == "acc:link":
        if not api_ready():
            s["account_link"]["step"] = "await_api"
            save_settings(s)
            await query.edit_message_text(
                api_setup_text() + "\n\nПосле сохранения API пришлю запрос номера.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Отмена", callback_data="set:account")]]),
            )
            return
        link = s.get("account_link", {})
        if link.get("step") == "await_code" and link.get("phone_code_hash"):
            await query.edit_message_text(
                f"Код уже отправлен на {link.get('phone', '')}.\n\n"
                "Пришли код из чата <b>Telegram</b> в приложении.\n"
                "Повторить — пришли номер ещё раз.\n"
                "SMS — напиши <code>смс</code>.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Отмена", callback_data="set:account")]]),
            )
            return
        if link.get("step") == "await_2fa":
            await query.edit_message_text(
                "Нужен пароль 2FA — пришли его сюда.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Отмена", callback_data="set:account")]]),
            )
            return
        s["account_link"]["step"] = "await_phone"
        save_settings(s)
        await query.edit_message_text(
            "Пришли номер телефона: +79991234567 или 79991234567",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Отмена", callback_data="set:account")]]),
        )
        return

    if data == "acc:qr":
        if not api_ready():
            await query.edit_message_text(
                api_setup_text() + "\n\nПосле API можно войти по QR.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Назад", callback_data="set:account")]]),
            )
            return
        ok, caption, png = await start_qr_link()
        if not ok:
            await query.edit_message_text(caption, parse_mode="HTML")
            return
        await query.message.reply_photo(photo=png, caption=caption, parse_mode="HTML")
        uid = query.from_user.id
        old = _qr_wait_tasks.pop(uid, None)
        if old and not old.done():
            old.cancel()
        _qr_wait_tasks[uid] = asyncio.create_task(_run_qr_wait(query.message))
        return

    if data == "acc:unlink":
        msg = await unlink_account()
        await stop_chat_watcher()
        await query.edit_message_text(msg, reply_markup=settings_keyboard())
        return

    if data == "set:write_toggle":
        s["permissions"]["can_write_chats"] = not s["permissions"]["can_write_chats"]
        save_settings(s)
        state = "разрешено" if s["permissions"]["can_write_chats"] else "запрещено"
        await query.answer(f"Писать в чаты: {state}")
        await query.edit_message_text(settings_text(), parse_mode="HTML", reply_markup=settings_keyboard())
        return

    if data == "set:dm_toggle":
        perms = s.setdefault("permissions", {})
        perms["can_write_private_dms"] = not perms.get("can_write_private_dms", True)
        save_settings(s)
        state = "да" if perms["can_write_private_dms"] else "нет"
        await query.answer(f"Личные сообщения: {state}")
        await query.edit_message_text(settings_text(), parse_mode="HTML", reply_markup=settings_keyboard())
        return

    if data == "set:posts_day":
        s["account_link"]["step"] = "await_posts_day"
        save_settings(s)
        await query.edit_message_text(
            f"Сейчас: {s['posts']['per_day']} постов/день.\nПришли число (1-20).",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Назад", callback_data="set:back")]]),
        )
        return

    if data == "set:posts_style":
        s["account_link"]["step"] = "await_posts_style"
        save_settings(s)
        await query.edit_message_text(
            f"Сейчас: {s['posts']['style']}\n\nОпиши стиль постов одним сообщением.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Назад", callback_data="set:back")]]),
        )
        return

    if data == "set:channel":
        s["account_link"]["step"] = "await_channel"
        save_settings(s)
        await query.edit_message_text(
            "Пришли @username канала или ссылку t.me/...",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Назад", callback_data="set:back")]]),
        )
        return

    if data == "set:sources":
        s["account_link"]["step"] = "await_sources"
        save_settings(s)
        cur = ", ".join(s["monitoring"]["news_sites"]) or "пусто"
        await query.edit_message_text(
            f"Сайты: {cur}\n\nПришли список URL (по одному в строке).",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Назад", callback_data="set:back")]]),
        )
        return

    if data == "set:tg_channels":
        s["account_link"]["step"] = "await_tg_channels"
        cur = ", ".join(s["monitoring"]["telegram_channels"]) or "пусто"
        await query.edit_message_text(
            f"Каналы: {cur}\n\nПришли @username (по одному в строке).",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Назад", callback_data="set:back")]]),
        )
        return

    if data == "set:exchanges":
        s["account_link"]["step"] = "await_exchanges"
        cur = ", ".join(s["monitoring"]["exchanges"]) or "пусто"
        await query.edit_message_text(
            f"Биржи: {cur}\n\nПришли список бирж/площадок (по одной в строке).",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Назад", callback_data="set:back")]]),
        )
        return

    if data == "set:agent_status":
        agent = s["agent"]
        hist = load_agent_session()
        last = hist["messages"][-3:] if hist["messages"] else []
        preview = (
            "\n".join(
                f"• [{html.escape(m['role'])}] {html.escape(m['text'][:80])}" for m in last
            )
            or "пусто"
        )
        await query.edit_message_text(
            f"<b>Агент</b>\n"
            f"Диалог: {'активен' if agent.get('dialog_active') else 'выключен'}\n"
            f"Сообщений в истории: {len(hist['messages'])}\n\n"
            f"<b>Последние:</b>\n{preview}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Назад", callback_data="set:back")]]),
        )
        return

    if data == "set:back":
        s["account_link"]["step"] = ""
        save_settings(s)
        await query.edit_message_text(settings_text(), parse_mode="HTML", reply_markup=settings_keyboard())
        return

    if data == "agent:exit_yes":
        s = load_settings()
        s["agent"]["dialog_active"] = False
        s["agent"]["pending_exit_confirm"] = False
        s["agent"]["dialog_started_at"] = ""
        save_settings(s)
        hist_len = len(load_agent_session().get("messages", []))
        await query.edit_message_text(
            f"Диалог закрыт. История сохранена ({hist_len} сообщений).\n"
            "Напиши agent/cursor чтобы продолжить.",
        )
        return

    if data == "agent:exit_no":
        s = load_settings()
        s["agent"]["pending_exit_confirm"] = False
        save_settings(s)
        await query.edit_message_text("Ок, диалог продолжается.")
        return

    if data.startswith("fix:run:"):
        proposal_id = data.split(":", 2)[2]
        proposal = get_proposal(proposal_id)
        if not proposal or proposal.get("status") != "open":
            await query.edit_message_text("Предложение уже закрыто или не найдено.")
            return
        result = await execute_proposal_action(proposal)
        close_proposal(proposal_id, status="executed")
        await query.edit_message_text(f"✅ {result}")
        return

    if data.startswith("fix:stop:"):
        proposal_id = data.split(":", 2)[2]
        close_proposal(proposal_id, status="dismissed")
        await query.edit_message_text("Ок, остановился. Ничего не менял.")
        return

    if data.startswith("mute:reply:"):
        parts = data.split(":")
        if len(parts) < 4:
            await query.answer("Некорректные данные.")
            return
        try:
            target_chat = int(parts[2])
            target_msg = int(parts[3])
        except ValueError:
            await query.answer("Некорректные данные.")
            return
        from chat_watcher import enqueue_muted_approved_reply

        ok = await enqueue_muted_approved_reply(target_chat, target_msg)
        if ok:
            await query.edit_message_text(
                f"✅ Отвечу в чате (реплай на сообщение {target_msg}). Готовлю…"
            )
        else:
            await query.edit_message_text(
                "Не нашла сохранённое сообщение — возможно, уже обработано. "
                "Напиши `agent ответь там` со ссылкой."
            )
        return

    if data.startswith("mute:ignore:"):
        await query.edit_message_text("Ок — в этом чате молчу.")
        return




def ensure_owner_dialog_active(settings: dict | None = None) -> bool:
    """Диалог с хозяином в боте всегда открыт, если аккаунт привязан."""
    from storage import load_settings, save_settings

    s = settings if settings is not None else load_settings()
    if not s.get("linked_account", {}).get("user_id"):
        return False
    agent = s.setdefault("agent", {})
    changed = False
    if not agent.get("dialog_active"):
        agent["dialog_active"] = True
        changed = True
    if not agent.get("dialog_started_at"):
        agent["dialog_started_at"] = datetime.now().isoformat(timespec="seconds")
        changed = True
    agent["dialog_always_on"] = True
    if changed:
        save_settings(s)
    return changed


def is_owner_ping(text: str) -> bool:
    """Короткий пинг «Юна?» — ответить мгновенно."""
    t = (text or "").strip()
    if not t or len(t) > 48:
        return False
    low = t.lower()
    if re.match(r"^(?:@?юна|@?юно|@?yuna|@?hoshi)[!.?…\s]*$", low):
        return True
    if re.match(
        r"^(?:юна|юно)\s*(?:ты\s+)?(?:тут|жива|работаешь|на\s+связи)[!.?…]*$",
        low,
    ):
        return True
    return any(
        x in low
        for x in (
            "ты точно сейчас работаешь",
            "никогда не падай",
            "не должна падать",
            "померла",
            "молчишь",
        )
    ) and len(low) < 80


async def handle_owner_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not update.message.text:
        return
    if not is_owner(update.effective_user.id):
        return

    text = update.message.text.strip()
    s = load_settings()
    step = s.get("account_link", {}).get("step", "")
    link = s.get("account_link", {})
    forwarded = _message_forwarded(update.message)

    if step == "await_api":
        applied = apply_owner_secrets(text)
        if applied:
            await update.message.reply_text(f"{applied} ✅")
            return

    # Код привязки важнее диалога агента
    if should_try_link_code(text, link, step):
        if await _try_confirm_link_code(update, text, link, forwarded=forwarded):
            return

    if is_resend_request(text) and (step == "await_code" or link.get("phone")):
        phone = link.get("phone", "")
        if phone:
            await _send_link_phone(update, phone, resend=True)
        else:
            s["account_link"]["step"] = "await_phone"
            save_settings(s)
            await update.message.reply_text("Пришли номер телефона: +79991234567")
        return

    if is_wizard_cancel(text) and step:
        clear_wizard_step(s)
        await update.message.reply_text("Настройка отменена.")
        return

    # exit в мастере настроек — отмена, а не закрытие диалога агента
    if step and is_agent_exit(text):
        clear_wizard_step(s)
        await update.message.reply_text("Настройка отменена.")
        return

    # Команды важнее пошагового мастера (иначе agent ловится как «телефон»)
    if is_agent_start(text):
        if step in ("await_code", "await_2fa"):
            await update.message.reply_text(
                "Сначала заверши привязку — пришли "
                + ("пароль 2FA." if step == "await_2fa" else "код из чата Telegram.")
                + "\nОтмена: <code>отмена</code>",
                parse_mode="HTML",
            )
            return
        clear_wizard_step(s, keep_link_data=has_pending_link(s))
        s = load_settings()
        s["agent"]["dialog_active"] = True
        s["agent"]["dialog_started_at"] = datetime.now().isoformat(timespec="seconds")
        s["agent"]["pending_exit_confirm"] = False
        save_settings(s)
        hist = len(load_agent_session().get("messages", []))
        await update.message.reply_text(
            f"Диалог с агентом открыт. История: {hist} сообщений.\n"
            "Пиши задачи здесь. Если что-то сломалось — попроси исправить код бота.\n"
            "В чаты пишу только с разрешения. /exit — завершить."
        )
        return

    if is_agent_exit(text):
        if not s["agent"].get("dialog_active"):
            return
        if s["agent"].get("pending_exit_confirm"):
            return
        s["agent"]["pending_exit_confirm"] = True
        save_settings(s)
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Закрыть диалог", callback_data="agent:exit_yes"),
                    InlineKeyboardButton("❌ Продолжить", callback_data="agent:exit_no"),
                ]
            ]
        )
        await update.message.reply_text(
            "Закрыть диалог с агентом?\nИстория сообщений сохранится.",
            reply_markup=kb,
        )
        return

    # Пошаговые настройки
    if step == "await_api":
        creds = parse_telegram_api(text)
        if not creds:
            await update.message.reply_text(
                "Не распознал API-данные.\n\n"
                "Пришли api_id (число) и api_hash (32 символа) — двумя строками "
                "или с подписями API_id / API_hash."
            )
            return
        api_id, api_hash = creds
        save_telegram_api(api_id, api_hash)
        s = load_settings()
        s["account_link"]["step"] = "await_phone"
        save_settings(s)
        await update.message.reply_text(
            "API сохранён ✅\n\nТеперь пришли номер телефона в формате +79991234567"
        )
        return

    if step == "await_phone":
        creds = parse_telegram_api(text)
        if creds:
            save_telegram_api(*creds)
            s = load_settings()
            s["account_link"]["step"] = "await_phone"
            save_settings(s)
            await update.message.reply_text(
                "API сохранён ✅\n\nТеперь пришли номер телефона в формате +79991234567"
            )
            return

        phone = normalize_phone(text)
        if not phone:
            if re.fullmatch(r"\d{5,10}", text.replace(" ", "")):
                s = load_settings()
                s["account_link"]["step"] = "await_api"
                save_settings(s)
                await update.message.reply_text(
                    "Похоже на api_id, а не телефон.\n\n" + api_setup_text(),
                    parse_mode="HTML",
                )
                return
            await update.message.reply_text("Формат: +79991234567 или 79991234567")
            return
        await _send_link_phone(update, phone)
        return

    if step == "await_code":
        if should_try_link_code(text, link, step):
            if await _try_confirm_link_code(update, text, link, forwarded=forwarded):
                return
        if text.strip().lower() in {"смс", "sms"}:
            phone = link.get("phone", "")
            if phone:
                await _send_link_phone(update, phone, resend=True, force_sms=True)
            else:
                await update.message.reply_text("Сначала пришли номер телефона.")
            return
        phone = normalize_phone(text)
        if phone:
            if phone == link.get("phone", ""):
                await update.message.reply_text(
                    "Код уже отправлен на этот номер.\n"
                    "Пришли <b>только цифры</b> из чата <b>Telegram</b>.\n"
                    "Новый код — <code>повтор</code> или <code>смс</code>.",
                    parse_mode="HTML",
                )
                return
            await _send_link_phone(update, phone, resend=False)
            return
        await update.message.reply_text(_cli_code_hint(), parse_mode="HTML")
        return

    if step == "await_2fa":
        ok, msg = await confirm_2fa(text)
        await update.message.reply_text(msg)
        if ok:
            s["account_link"]["step"] = ""
            save_settings(s)
            if await restart_chat_watcher():
                log.info("chat watcher started after 2FA link")
        return

    if step == "await_posts_day":
        try:
            n = int(text)
            if not 1 <= n <= 20:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Нужно число от 1 до 20.")
            return
        s["posts"]["per_day"] = n
        s["account_link"]["step"] = ""
        save_settings(s)
        await update.message.reply_text(f"Постов в день: {n}")
        return

    if step == "await_posts_style":
        s["posts"]["style"] = text
        s["account_link"]["step"] = ""
        save_settings(s)
        await update.message.reply_text("Стиль постов сохранён.")
        return

    if step == "await_channel":
        username = text.strip().lstrip("@")
        username = username.replace("https://t.me/", "").split("/")[0]
        s["channel"]["username"] = f"@{username}" if username else ""
        s["account_link"]["step"] = ""
        save_settings(s)
        await update.message.reply_text(f"Канал сохранён: @{username}")
        return

    if step == "await_sources":
        urls = [ln.strip() for ln in text.splitlines() if ln.strip()]
        s["monitoring"]["news_sites"] = urls
        s["account_link"]["step"] = ""
        save_settings(s)
        await update.message.reply_text(f"Сохранено сайтов: {len(urls)}")
        return

    if step == "await_tg_channels":
        channels = []
        for ln in text.splitlines():
            ln = ln.strip().lstrip("@")
            if ln:
                channels.append(f"@{ln}")
        s["monitoring"]["telegram_channels"] = channels
        s["account_link"]["step"] = ""
        save_settings(s)
        await update.message.reply_text(f"Сохранено каналов: {len(channels)}")
        return

    if step == "await_exchanges":
        exchanges = [ln.strip() for ln in text.splitlines() if ln.strip()]
        s["monitoring"]["exchanges"] = exchanges
        s["account_link"]["step"] = ""
        save_settings(s)
        await update.message.reply_text(f"Сохранено бирж: {len(exchanges)}")
        return

    # Вне мастера настроек — можно принять API-данные без кнопки
    if not step and not api_ready():
        creds = parse_telegram_api(text)
        if creds:
            save_telegram_api(*creds)
            await update.message.reply_text(
                "API сохранён ✅\n\n"
                "Для привязки аккаунта: /settings → Аккаунт → Привязать."
            )
            return

    # Привязка важнее диалога агента
    if is_linking_step(step) or link.get("phone"):
        if step == "await_phone":
            phone = normalize_phone(text)
            if phone:
                await _send_link_phone(update, phone)
                return
        if step in ("await_code", "await_2fa", "await_qr"):
            return
        if link.get("phone") and not s["agent"].get("dialog_active"):
            return

    # Привязка в процессе — не уводим в агента
    if is_linking_step(step) or has_pending_link(s):
        return

    # Диалог с хозяином в боте — всегда открыт
    if ensure_owner_dialog_active(s):
        s = load_settings()

    # Сообщение в активном диалоге
    if not s["agent"].get("dialog_active"):
        await update.message.reply_text(
            "Диалог закрыт. Напиши <code>agent</code> или <code>юна</code> — открою.",
            parse_mode="HTML",
        )
        return

    # Эхо ack / пересланный ответ бота — не задача агенту
    if is_external_ack(text):
        return
    if looks_like_bot_echo(text):
        return
    from chat_router import is_owner_chat_config_ack

    if is_owner_chat_config_ack(text):
        return

    chat_id = update.effective_chat.id
    for key in list(_media_group_buffers.keys()):
        if key.startswith(f"{chat_id}:") and _media_group_buffers.get(key):
            _media_group_captions[key] = text
            n = len(_media_group_buffers[key])
            await update.message.reply_text(
                f"Жду альбом ({n} фото) — вопрос добавлю к нему…"
            )
            return

    from health_watch import health_send_proactive, is_proactive_owner_request

    if is_proactive_owner_request(text):
        asyncio.create_task(health_send_proactive(force=True))

    cmd_ack = await apply_owner_chat_command(text)
    if cmd_ack:
        await update.message.reply_text(cmd_ack)
        head = text.split("\n---\n")[0]
        if (
            cmd_ack.strip().startswith(("Запомнила", "Ок —", "Ок -", "Поняла"))
            or is_mute_command(head)
            or is_global_groups_silence_command(head)
            or (ENABLE_RESPOND_RE.search(head) and not SEE_CHAT_RE.search(head))
            or ENABLE_CHAT_WRITE_RE.search(head)
            or ENABLE_DM_RE.search(head)
            or DISABLE_DM_RE.search(head)
            or is_owner_behavior_policy_command(head)
            or is_restricted_dm_policy_command(head)
            or is_all_dms_no_groups_policy_command(head)
        ):
            return

    task_extra: dict = {}
    reply_msg = update.message.reply_to_message
    if reply_msg and reply_msg.from_user and reply_msg.from_user.is_bot:
        thread = resolve_bot_thread_context(reply_msg.message_id)
        if thread:
            head = text.split("\n---\n")[0]
            if owner_wants_routed_chat_reply(head):
                task_extra.update(thread)
            preview = (reply_msg.text or reply_msg.caption or "")[:400]
            if preview:
                title = thread.get("target_chat_title") or thread.get("resolved_chat_title") or ""
                label = f" «{title}»" if title else ""
                text = (
                    f"Реплай с цитатой на моё сообщение{label}:\n«{preview}»\n\n{text}"
                )

    from bot_branches import (
        branch_context_block,
        branch_reply_topic_kw,
        branch_topic_id,
        clear_external_delivery,
        create_bot_branch,
        ensure_branch_telegram_topic,
        extract_message_topic_id,
        format_branch_ack_prefix,
        format_branch_created_message,
        format_branch_list,
        format_topics_disabled_message,
        get_active_branch,
        get_branch_by_id,
        is_bot_internal_branch_task,
        is_branch_create_command,
        is_branch_list_command,
        is_branch_topic_ensure_command,
        is_branch_visibility_request,
        parse_branch_name,
        parse_branch_topic_ensure_name,
        resolve_branch_by_anchor,
        resolve_branch_by_name,
        resolve_branch_by_topic_id,
        set_active_branch,
        update_branch_anchor,
    )
    from chat_router import should_accept_yuna_task

    if is_branch_list_command(text) and should_accept_yuna_task(text):
        await update.message.reply_text(format_branch_list(), parse_mode="HTML")
        return

    if is_branch_topic_ensure_command(text) and should_accept_yuna_task(text):
        name = (
            parse_branch_topic_ensure_name(text)
            or (get_active_branch() or {}).get("name")
            or "задача"
        )
        branch = resolve_branch_by_name(name) or get_branch_by_id(
            (get_active_branch() or {}).get("id")
        )
        if not branch or str(branch.get("name") or "").lower() != name.lower():
            branch = create_bot_branch(name)
        else:
            set_active_branch(branch.get("id"))
        ack = await update.message.reply_text(f"Создаю тему «{name}»…")
        topic_id, err = await ensure_branch_telegram_topic(
            context.bot, update.effective_chat.id, branch
        )
        if err == "topics_disabled":
            await ack.edit_text(format_topics_disabled_message(), parse_mode="HTML")
            return
        created_text = format_branch_created_message(
            name, branch["id"], has_topic=bool(topic_id)
        )
        topic_kw = branch_reply_topic_kw(topic_id)
        await ack.edit_text(created_text, parse_mode="HTML", **topic_kw)
        update_branch_anchor(branch["id"], ack.message_id)
        register_bot_thread_context(
            ack.message_id,
            {"branch_id": branch["id"], "branch_name": name},
        )
        return

    if is_branch_create_command(text) and should_accept_yuna_task(text):
        name = parse_branch_name(text) or "задача"
        ack = await update.message.reply_text(f"Создаю тему «{name}»…")
        branch = create_bot_branch(name, anchor_message_id=ack.message_id)
        topic_id, err = await ensure_branch_telegram_topic(
            context.bot, update.effective_chat.id, branch
        )
        if err == "topics_disabled":
            await ack.edit_text(format_topics_disabled_message(), parse_mode="HTML")
            return
        created_text = format_branch_created_message(
            name, branch["id"], has_topic=bool(topic_id)
        )
        topic_kw = branch_reply_topic_kw(topic_id)
        await ack.edit_text(created_text, parse_mode="HTML", **topic_kw)
        register_bot_thread_context(
            ack.message_id,
            {"branch_id": branch["id"], "branch_name": name},
        )
        return

    if not should_accept_yuna_task(text) and not is_owner_ping(text):
        return

    branch = None
    msg_topic_id = extract_message_topic_id(update.message)
    if msg_topic_id:
        branch = resolve_branch_by_topic_id(msg_topic_id)
    if not branch and reply_msg:
        branch = resolve_branch_by_anchor(reply_msg.message_id)
        if not branch:
            thread = resolve_bot_thread_context(reply_msg.message_id)
            if thread and thread.get("branch_id"):
                branch = get_branch_by_id(thread.get("branch_id"))
    task_extra["message_topic_id"] = msg_topic_id
    if branch:
        set_active_branch(branch.get("id"))
        task_extra["branch_id"] = branch.get("id")
        task_extra["branch_name"] = branch.get("name")
        tid = branch_topic_id(branch) or msg_topic_id
        if tid:
            task_extra["branch_topic_id"] = int(tid)
        task_extra["branch_prompt"] = branch_context_block(branch)
        if is_branch_visibility_request(text):
            topic_id, err = await ensure_branch_telegram_topic(
                context.bot, update.effective_chat.id, branch
            )
            if err == "topics_disabled":
                await update.message.reply_text(
                    format_topics_disabled_message(), parse_mode="HTML"
                )
                return
            created_text = format_branch_created_message(
                branch["name"], branch["id"], has_topic=bool(topic_id)
            )
            topic_kw = branch_reply_topic_kw(topic_id)
            marker = await update.message.reply_text(
                created_text,
                parse_mode="HTML",
                **topic_kw,
            )
            update_branch_anchor(branch["id"], marker.message_id)
            register_bot_thread_context(
                marker.message_id,
                {"branch_id": branch["id"], "branch_name": branch.get("name")},
            )

    text = sanitize_owner_incoming_text(text)
    apply_owner_global_preferences_from_text(text)
    task_text, enriched_extra = await enrich_owner_task(
        text, branch_name=str(task_extra.get("branch_name") or "")
    )
    task_extra.update(enriched_extra)
    if str(task_extra.get("branch_name") or "").lower() == "iris":
        from chat_router import strip_auto_iris_chat_enrichment

        task_text, task_extra = strip_auto_iris_chat_enrichment(task_text, task_extra)
    from bot_branches import ensure_iris_branch_on_task

    task_text, task_extra = ensure_iris_branch_on_task(task_text, task_extra)
    if is_bot_internal_branch_task(task_text):
        clear_external_delivery(task_extra)
    if looks_like_bot_echo(task_text):
        return
    await _submit_agent_message(
        context,
        chat_id=update.effective_chat.id,
        message_id=update.message.message_id,
        user_id=update.effective_user.id,
        reply_message=update.message,
        text=task_text,
        extra=task_extra,
    )


async def _submit_agent_message(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    message_id: int,
    user_id: int,
    reply_message,
    text: str,
    images: list[str] | None = None,
    extra: dict | None = None,
) -> None:
    if not images:
        images = take_pending_images(user_id) or None

    hist_text = text
    if images:
        note = f" [фото: {len(images)} шт.]"
        hist_text = (text + note) if text else f"(фото без подписи{note})"

    if is_agent_exit(text):
        return

    settings = load_settings()
    append_agent_message("user", hist_text)
    head = (text or "").split("\n---\n")[0]
    kind = detect_task_kind(head)
    from bot_branches import (
        branch_reply_topic_kw,
        clear_external_delivery,
        format_branch_ack_prefix,
        is_bot_internal_branch_task,
    )

    bot_internal = is_bot_internal_branch_task(text)
    if bot_internal:
        kind = "code_fix"
    if owner_wants_routed_chat_reply(head) and not bot_internal:
        kind = "agent_message"
    task_extra = {
        "user_id": user_id,
        "dialog_started_at": settings["agent"].get("dialog_started_at", ""),
    }
    if extra:
        task_extra.update(extra)
    head_for_flags = (text or "").split("\n---\n")[0]
    try:
        from chat_router import is_template_complaint

        if is_template_complaint(head_for_flags):
            task_extra["template_complaint"] = True
            kind = "code_fix"
    except Exception:
        pass
    if bot_internal or not owner_wants_routed_chat_reply(text):
        clear_external_delivery(task_extra)
    elif not owner_wants_routed_chat_reply(text):
        for key in (
            "delivery",
            "target_chat_id",
            "target_message_id",
            "target_chat_title",
            "owner_approved_write",
        ):
            task_extra.pop(key, None)
    from image_edit import is_photo_edit_request

    if images and is_photo_edit_request(text or ""):
        task_extra["photo_edit"] = True
        task_extra["from_owner"] = True
    if owner_wants_routed_chat_reply(text):
        task_extra["from_owner"] = True
    target = task_extra.get("target_chat_id")
    if target and is_bot_chat_id(int(target)):
        for key in ("delivery", "target_chat_id", "target_message_id", "target_chat_title"):
            task_extra.pop(key, None)
    if (
        target
        and is_chat_write_forbidden(int(target))
        and not task_extra.get("owner_approved_write")
    ):
        for key in (
            "delivery",
            "target_chat_id",
            "target_message_id",
            "target_chat_title",
            "owner_approved_write",
        ):
            task_extra.pop(key, None)
    if kind == "code_fix" and not owner_wants_routed_chat_reply(head):
        task_extra.pop("delivery", None)
        task_extra.pop("target_chat_id", None)
        task_extra.pop("target_message_id", None)
        task_extra.pop("target_chat_title", None)
    if is_owner_routing_complaint(head):
        kind = "code_fix"
        for key in (
            "delivery",
            "target_chat_id",
            "target_message_id",
            "target_chat_title",
            "owner_approved_write",
        ):
            task_extra.pop(key, None)
    task_id = enqueue_task(
        source="telegram",
        text=text or "(фото без подписи)",
        chat_id=chat_id,
        message_id=message_id,
        kind=kind,
        extra=task_extra,
        images=images,
    )
    from chat_router import instant_ack_message

    branch_name = task_extra.get("branch_name")
    branch_prefix = format_branch_ack_prefix(branch_name)
    ack = f"{branch_prefix}{instant_ack_message()}"
    topic_kw = branch_reply_topic_kw(task_extra.get("branch_topic_id"))
    ack_msg = await reply_message.reply_text(ack, **topic_kw)
    if ack_msg and task_extra.get("branch_id"):
        register_bot_thread_context(
            ack_msg.message_id,
            {
                "branch_id": task_extra.get("branch_id"),
                "branch_name": task_extra.get("branch_name"),
            },
        )
    if ack_msg and task_extra.get("delivery") == "external_telegram":
        register_bot_thread_context(
            ack_msg.message_id,
            {
                "task_id": task_id,
                "target_chat_id": task_extra.get("target_chat_id"),
                "target_chat_title": task_extra.get("target_chat_title"),
                "target_message_id": task_extra.get("target_message_id"),
                "delivery": "external_telegram",
            },
        )

    if images and (not text or text == "(фото без подписи)"):
        set_pending_images(images, user_id)


async def _process_photo_messages(
    messages: list,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    extra_caption: str = "",
) -> None:
    if not messages:
        return
    first = messages[0]
    if not first.from_user or not is_owner(first.from_user.id):
        return

    s = load_settings()
    if not s["agent"].get("dialog_active"):
        return

    batch_id = f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
    try:
        image_paths = await download_images(context.bot, messages, batch_id)
    except Exception as e:
        log.exception("photo download failed")
        await first.reply_text(f"Не удалось скачать фото: {e}")
        return

    if not image_paths:
        await first.reply_text("Не удалось получить изображение.")
        return

    caption = caption_from_messages(messages) or extra_caption.strip()
    await _submit_agent_message(
        context,
        chat_id=first.chat_id,
        message_id=first.message_id,
        user_id=first.from_user.id,
        reply_message=first,
        text=caption,
        images=image_paths,
    )


async def _schedule_media_group(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    key = f"{message.chat_id}:{message.media_group_id}"

    if key not in _media_group_buffers:
        _media_group_buffers[key] = []
    _media_group_buffers[key].append(message)

    old = _media_group_tasks.pop(key, None)
    if old and not old.done():
        old.cancel()

    async def flush() -> None:
        try:
            await asyncio.sleep(_MEDIA_GROUP_DELAY)
            batch = _media_group_buffers.pop(key, [])
            _media_group_tasks.pop(key, None)
            extra_caption = _media_group_captions.pop(key, "")
            if batch:
                await _process_photo_messages(batch, context, extra_caption=extra_caption)
        except asyncio.CancelledError:
            return

    _media_group_tasks[key] = asyncio.create_task(flush())


async def handle_owner_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not is_owner(update.effective_user.id):
        return

    s = load_settings()
    if s.get("account_link", {}).get("step"):
        await update.message.reply_text("Сейчас жду текст для настройки.")
        return

    message = update.message
    if message.media_group_id:
        await _schedule_media_group(message, context)
        return

    await _process_photo_messages([message], context)


_VIDEO_OUTBOX_ACTIONS = frozenset({"deliver_video", "send_video", "generate_video"})
_video_tasks_running: set[str] = set()
_video_chat_busy: set[int] = set()


def release_video_chat(chat_id: int) -> None:
    _video_chat_busy.discard(int(chat_id))


async def _run_video_outbox_item(item: dict) -> None:
    """Скачивание/отправка видео в фоне — не блокирует текст и другие ответы."""
    from chat_router import is_chat_muted, is_chat_write_forbidden, register_hoshi_message
    from user_client import deliver_generate_video, deliver_video_from_url, send_user_video
    from user_outbox import mark_action_done

    item_id = str(item.get("id") or "")
    if not item_id or item_id in _video_tasks_running:
        return
    chat_id = item.get("chat_id")
    if chat_id:
        from user_outbox import is_chat_video_stopped

        if is_chat_video_stopped(int(chat_id)):
            return
    _video_tasks_running.add(item_id)
    action = item.get("action")
    extra = item.get("extra") or {}
    try:
        if (
            chat_id
            and is_chat_write_forbidden(int(chat_id))
            and not extra.get("owner_approved_write")
        ):
            log.info("user outbox skipped forbidden chat %s", chat_id)
            return
        if chat_id and is_chat_muted(int(chat_id)) and not extra.get("owner_approved_write"):
            log.info("user outbox skipped muted chat %s", chat_id)
            return
        if action == "deliver_video" and chat_id:
            from user_outbox import is_chat_video_stopped

            cid = int(chat_id)
            if is_chat_video_stopped(cid):
                log.info("user outbox skipped stopped chat %s", cid)
                return
            url = extra.get("video_url", "")
            from video_download import ensure_video_caption

            cap = ensure_video_caption(extra.get("caption", ""), url=url)
            msg_id = await deliver_video_from_url(
                cid,
                url,
                reply_to=item.get("reply_to"),
                owner_approved=bool(extra.get("owner_approved_write")),
                caption=cap,
            )
        elif action == "send_video" and chat_id:
            cid = int(chat_id)
            video_path = extra.get("video_path", "")
            from video_download import ensure_video_caption

            cap = ensure_video_caption(
                (extra.get("caption", "") or "").strip(),
                path=video_path,
            )
            msg_id = await send_user_video(
                cid,
                video_path,
                reply_to=item.get("reply_to"),
                caption=cap,
            )
            if msg_id:
                register_hoshi_message(
                    cid,
                    msg_id,
                    text=cap,
                    video_url=video_path,
                )
        elif action == "generate_video" and chat_id:
            cid = int(chat_id)
            msg_id = await deliver_generate_video(
                cid,
                extra.get("gen_idea", ""),
                reply_to=item.get("reply_to"),
                owner_approved=bool(extra.get("owner_approved_write")),
            )
            if msg_id:
                register_hoshi_message(cid, msg_id)
    except Exception as e:
        log.warning("user outbox video %s failed: %s", item.get("id"), e)
    finally:
        _video_tasks_running.discard(item_id)
        cid = int(chat_id) if chat_id else 0
        if cid:
            _video_chat_busy.discard(cid)
        mark_action_done(item_id)


async def _start_video_outbox_item(item: dict) -> None:
    cid = int(item.get("chat_id") or 0)
    if cid:
        _video_chat_busy.add(cid)
    try:
        await _run_video_outbox_item(item)
    except Exception as e:
        log.warning("video outbox wrapper failed %s: %s", item.get("id"), e)
        if cid:
            _video_chat_busy.discard(cid)


async def _process_user_outbox() -> None:
    from chat_router import is_chat_muted, is_chat_write_forbidden, register_hoshi_message
    from user_client import (
        send_message_reaction,
        send_typing_action,
        send_user_audio,
        send_user_message,
        send_user_photo,
        send_user_voice,
    )
    from user_outbox import recover_stale_outbox_actions

    recover_stale_outbox_actions()

    pending = list_pending_actions()
    quick = [i for i in pending if i.get("action") not in _VIDEO_OUTBOX_ACTIONS]
    videos = [i for i in pending if i.get("action") in _VIDEO_OUTBOX_ACTIONS]

    for item in quick:
        action = item.get("action")
        chat_id = item.get("chat_id")
        extra = item.get("extra") or {}
        mark_done = True
        if (
            chat_id
            and is_chat_write_forbidden(int(chat_id))
            and not extra.get("owner_approved_write")
        ):
            mark_action_done(item["id"])
            log.info("user outbox skipped forbidden chat %s", chat_id)
            continue
        if chat_id and is_chat_muted(int(chat_id)) and not extra.get("owner_approved_write"):
            mark_action_done(item["id"])
            log.info("user outbox skipped muted chat %s", chat_id)
            continue
        try:
            if action == "typing" and chat_id:
                await send_typing_action(int(chat_id))
            elif action == "reaction" and chat_id:
                msg_id = extra.get("message_id")
                if msg_id:
                    await send_message_reaction(
                        int(chat_id),
                        int(msg_id),
                        reaction=extra.get("reaction", "paid"),
                    )
            elif action == "send" and chat_id:
                cid = int(chat_id)
                raw = item.get("text", "")
                from user_outbox import (
                    enqueue_deliver_video,
                    enqueue_generate_video,
                    enqueue_user_video,
                )
                from voice_delivery import (
                    extract_audio_directives,
                    extract_gen_video_directives,
                    extract_photo_directives,
                    extract_reaction_directives,
                    extract_video_caption,
                    extract_video_directives,
                    strip_audio_markers,
                    strip_gen_video_markers,
                    strip_photo_markers,
                    strip_reaction_markers,
                    strip_video_markers,
                )

                video_cap = extract_video_caption(raw)
                plain, videos = extract_video_directives(raw)
                plain, gen_ideas = extract_gen_video_directives(plain)
                plain, photos = extract_photo_directives(plain)
                plain, audios = extract_audio_directives(plain)
                plain, reactions = extract_reaction_directives(plain)
                plain = strip_reaction_markers(
                    strip_video_markers(
                        strip_gen_video_markers(strip_photo_markers(strip_audio_markers(plain)))
                    )
                )
                photos = list(dict.fromkeys(photos))
                audios = list(dict.fromkeys(audios))
                videos = list(dict.fromkeys(videos))
                gen_ideas = list(dict.fromkeys(gen_ideas))
                msg_id = None
                if plain.strip():
                    from text_format import hoshi_outbound_parts
                    from user_client import get_client

                    client = await get_client()
                    if client:
                        outbound, outbound_entities = hoshi_outbound_parts(
                            plain.strip(), cid
                        )
                        msg = await client.send_message(
                            cid,
                            outbound,
                            reply_to=item.get("reply_to"),
                            link_preview=False,
                            formatting_entities=outbound_entities,
                        )
                        msg_id = int(msg.id) if msg else None
                        if msg_id:
                            register_hoshi_message(cid, msg_id, text=outbound)
                for photo_path in photos:
                    photo_id = await send_user_photo(
                        cid,
                        photo_path,
                        reply_to=None,
                        caption="",
                    )
                    if photo_id:
                        register_hoshi_message(cid, photo_id, text="[фото]")
                        msg_id = photo_id
                from voice_delivery import audio_display_name

                for i, audio_path in enumerate(audios):
                    audio_id = await send_user_audio(
                        cid,
                        audio_path,
                        reply_to=item.get("reply_to") if i == 0 and not plain and not photos else None,
                        caption=plain[:1024] if i == 0 and plain and not photos else "",
                        title=audio_display_name(audio_path),
                    )
                    if audio_id:
                        register_hoshi_message(
                            cid,
                            audio_id,
                            text=plain[:1024] if i == 0 and plain else "[аудио]",
                        )
                        msg_id = audio_id
                    else:
                        log.warning("send_audio from send action failed: %s", audio_path)
                owner_ok = bool(extra.get("owner_approved_write"))
                from video_download import ensure_video_caption

                video_cap = video_cap.strip()[:1024] if video_cap.strip() and not photos and not audios else ""
                if not video_cap and plain.strip() and not photos and not audios:
                    video_cap = plain.strip()[:1024]
                for i, url in enumerate(videos):
                    cap = ensure_video_caption(
                        video_cap if i == 0 and video_cap else "",
                        url=url if url.startswith(("http://", "https://")) else "",
                        path=url,
                    )
                    if url.startswith(("http://", "https://")):
                        vid_id = enqueue_deliver_video(
                            cid,
                            url,
                            reply_to=item.get("reply_to"),
                            caption=cap,
                            owner_approved=owner_ok,
                        )
                    else:
                        from user_client import send_user_video

                        vid_id = await send_user_video(
                            cid,
                            url,
                            reply_to=item.get("reply_to"),
                            caption=cap,
                        )
                        if vid_id:
                            msg_id = vid_id
                for idea in gen_ideas:
                    from user_client import deliver_generate_video

                    gen_id = await deliver_generate_video(
                        cid,
                        idea,
                        reply_to=item.get("reply_to"),
                        owner_approved=owner_ok,
                    )
                    if gen_id:
                        register_hoshi_message(cid, gen_id, text="[видео]")
                        msg_id = gen_id
                react_to = extra.get("react_to_message_id") or item.get("reply_to")
                for reaction_kind, reaction_mid in reactions:
                    target = reaction_mid or react_to
                    if target:
                        await send_message_reaction(
                            cid,
                            int(target),
                            reaction=reaction_kind,
                        )
            elif action == "send_voice" and chat_id:
                cid = int(chat_id)
                voice_path = extra.get("voice_path", "")
                msg_id = await send_user_voice(
                    cid,
                    voice_path,
                    reply_to=item.get("reply_to"),
                )
                if msg_id:
                    register_hoshi_message(cid, msg_id)
            elif action == "send_photo" and chat_id:
                cid = int(chat_id)
                photo_path = extra.get("photo_path", "")
                msg_id = await send_user_photo(
                    cid,
                    photo_path,
                    reply_to=item.get("reply_to"),
                    caption=extra.get("caption", ""),
                )
                if msg_id:
                    register_hoshi_message(
                        cid, msg_id, text=extra.get("caption", "") or "[фото]"
                    )
            elif action == "send_album" and chat_id:
                from user_client import send_user_album

                cid = int(chat_id)
                photo_paths = extra.get("photo_paths") or []
                msg_id = await send_user_album(
                    cid,
                    photo_paths,
                    reply_to=item.get("reply_to"),
                    caption=extra.get("caption", ""),
                )
                if msg_id:
                    register_hoshi_message(
                        cid, msg_id, text=extra.get("caption", "") or "[альбом]"
                    )
            elif action == "send_audio" and (chat_id or extra.get("target_username")):
                from user_client import resolve_username_chat_id
                from voice_delivery import audio_display_name

                target_user = (extra.get("target_username") or "").strip()
                cid = await resolve_username_chat_id(target_user) if target_user else None
                if not cid and chat_id:
                    cid = int(chat_id)
                if not cid:
                    log.warning("send_audio: no chat for username=%s", target_user)
                    continue
                audio_path = extra.get("audio_path", "")
                from video_download import ensure_audio_file

                resolved = await asyncio.to_thread(ensure_audio_file, audio_path)
                if resolved:
                    audio_path = str(resolved)
                track_title = extra.get("title") or audio_display_name(audio_path)
                msg_id = await send_user_audio(
                    cid,
                    audio_path,
                    reply_to=item.get("reply_to"),
                    caption=extra.get("caption", ""),
                    title=track_title,
                )
                if msg_id:
                    register_hoshi_message(
                        cid,
                        msg_id,
                        text=extra.get("caption", "") or f"[аудио: {track_title}]",
                    )
                else:
                    mark_done = False
                    log.warning(
                        "send_audio failed chat=%s path=%s",
                        cid,
                        extra.get("audio_path", ""),
                    )
            elif action == "relay_group_media" and chat_id:
                from user_client import download_random_chat_media, send_user_photo, send_user_video

                cid = int(chat_id)
                source = int(extra.get("source_chat_id") or 0)
                reply_to = item.get("reply_to")
                media_path = await download_random_chat_media(source) if source else None
                if media_path:
                    low = media_path.lower()
                    if low.endswith((".mp4", ".mov", ".webm", ".mkv")):
                        msg_id = await send_user_video(
                            cid, media_path, reply_to=reply_to, caption=""
                        )
                    else:
                        msg_id = await send_user_photo(
                            cid, media_path, reply_to=reply_to, caption=""
                        )
                    if msg_id:
                        register_hoshi_message(cid, msg_id, text="[медиа из группы]")
                else:
                    log.warning("relay_group_media: no media source=%s target=%s", source, cid)
            else:
                log.warning("user outbox unknown action=%s id=%s", action, item.get("id"))
        except Exception as e:
            mark_done = False
            log.warning("user outbox item %s failed: %s", item.get("id"), e)
        finally:
            if mark_done:
                mark_action_done(item["id"])

    from user_outbox import claim_action, is_chat_video_stopped

    for item in videos:
        cid = int(item.get("chat_id") or 0)
        if cid and is_chat_video_stopped(cid):
            mark_action_done(item["id"])
            continue
        if cid and cid in _video_chat_busy:
            continue
        if claim_action(item["id"]):
            asyncio.create_task(_start_video_outbox_item(item))


async def _background_loops() -> None:
    from restart_util import ensure_daemon_running, ensure_iris_daemon_running

    while True:
        try:
            await _process_user_outbox()
            ensure_daemon_running()
            ensure_iris_daemon_running()
            await health_check_issues()
        except Exception as e:
            log.warning("background loop error: %s", e)
        await asyncio.sleep(0.5)


async def _health_loop() -> None:
    from health_watch import health_send_proactive

    await asyncio.sleep(45)
    while True:
        try:
            await health_send_proactive()
        except Exception as e:
            log.warning("health loop error: %s", e)
        await asyncio.sleep(120)


async def _konoha_loop() -> None:
    from konoha_lurker import is_enabled, konoha_loop_sleep, konoha_lurker_tick

    await asyncio.sleep(180)
    while True:
        try:
            if is_enabled():
                await konoha_lurker_tick()
        except Exception as e:
            log.warning("konoha loop error: %s", e)
        await konoha_loop_sleep()


async def _iris_check_loop() -> None:
    from iris_check_claimer import is_enabled, poll_recent_checks

    await asyncio.sleep(8)
    while True:
        try:
            if is_enabled():
                await poll_recent_checks(limit=8)
        except Exception as e:
            log.warning("iris check loop error: %s", e)
        await asyncio.sleep(12 if is_enabled() else 60)


async def _ad_loop() -> None:
    from ad_monitor import ad_monitor_tick, interval_seconds, is_enabled

    await asyncio.sleep(120)
    while True:
        try:
            if is_enabled():
                await ad_monitor_tick()
        except Exception as e:
            log.warning("ad loop error: %s", e)
        await asyncio.sleep(interval_seconds() if is_enabled() else 600)


async def _news_loop() -> None:
    from news_poster import interval_seconds, is_enabled, is_force_pending, news_poster_tick

    async def _sleep_interval() -> None:
        total = interval_seconds() if is_enabled() else 600
        step = 3
        waited = 0
        while waited < total:
            if is_force_pending():
                return
            chunk = min(step, total - waited)
            await asyncio.sleep(chunk)
            waited += chunk

    while True:
        try:
            if is_enabled():
                await news_poster_tick()
        except Exception as e:
            log.warning("news loop error: %s", e)
        if is_force_pending():
            await asyncio.sleep(2)
        else:
            await _sleep_interval()


async def _send_post_restart_notice() -> None:
    from restart_util import consume_post_restart
    from notify import send_message

    data = consume_post_restart()
    if not data:
        return
    if data.get("external_delivery"):
        return

    await asyncio.sleep(2)
    from chat_watcher import watcher_status
    from code_fix_verify import verify_all

    st = watcher_status()
    watcher_line = "активен ✅" if st.get("running") else f"не запущен ❌ ({st.get('reason', '')})"
    verify = verify_all()
    parts = [
        "✅ **Перезапуск завершён**" if verify.ok else "⚠️ **Перезапуск — есть проблемы**",
        "",
        f"**Bridge:** работает",
        f"**Watcher:** {watcher_line}",
    ]
    if data.get("task_id"):
        parts.append(f"**Задача:** `#{data['task_id']}`")
    if data.get("note") and not data.get("external_delivery"):
        parts.append("")
        parts.append("**Что сделано:**")
        parts.append(data["note"][:1500])
    parts.append("")
    parts.append(verify.report_text())
    parts.append("")
    parts.append(
        "Бот снова на связи — можно проверять."
        if verify.ok
        else "Часть проверок не прошла — напиши «дочини», добью."
    )

    chat_id = int(data.get("chat_id") or OWNER_ID)
    try:
        await send_message(chat_id, "\n".join(parts))
    except Exception as e:
        log.warning("post-restart notice failed: %s", e)


async def _post_init(application: Application) -> None:
    from datetime import datetime
    from health_watch import _load_state, _save_state

    ensure_owner_dialog_active()
    set_bridge_loop(asyncio.get_running_loop())

    from restart_util import set_process_start_time

    set_process_start_time()
    st = _load_state()
    st["bridge_started_at"] = datetime.now().isoformat(timespec="seconds")
    _save_state(st)

    try:
        if await start_chat_watcher():
            log.info("chat watcher active")
        else:
            log.info("chat watcher skipped (account not linked)")
    except Exception as e:
        log.warning("chat watcher failed to start: %s", e)

    asyncio.create_task(_background_loops())
    asyncio.create_task(_health_loop())
    asyncio.create_task(_konoha_loop())
    asyncio.create_task(_iris_check_loop())
    asyncio.create_task(_news_loop())
    asyncio.create_task(_ad_loop())
    asyncio.create_task(_send_post_restart_notice())
    asyncio.create_task(_sync_branch_topics(application))


async def _sync_branch_topics(application: Application) -> None:
    from bot_branches import sync_all_branch_topics

    await asyncio.sleep(3)
    try:
        await sync_all_branch_topics(application.bot, OWNER_ID)
    except Exception as e:
        log.warning("branch topic sync failed: %s", e)

async def _post_shutdown(_application: Application) -> None:
    try:
        await stop_chat_watcher()
    except Exception as e:
        log.warning("chat watcher shutdown failed: %s", e)


def main() -> None:
    if not BOT_TOKEN:
        log.error("HOSHI_BOT_TOKEN не задан в .env")
        sys.exit(1)

    load_settings()
    if sanitize_external_chats():
        log.info("cleared bot chat from external_chats active")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("exit", handle_owner_message))
    app.add_handler(CallbackQueryHandler(on_settings_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_owner_message))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_owner_photo))

    log.info("Hoshi bridge started (owner=%s)", OWNER_ID)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
