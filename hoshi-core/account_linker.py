#!/usr/bin/env python3
"""Привязка личного Telegram-аккаунта через Telethon."""
from __future__ import annotations

import asyncio
import io
import os
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path

import qrcode
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SendCodeUnavailableError,
    SessionPasswordNeededError,
)

# Клиент держим в памяти: phone_code_hash не пишется в .session на диск.
_link_clients: dict[str, TelegramClient] = {}
from telethon.tl import types
from telethon.tl.functions.auth import ResendCodeRequest

from config import ROOT, USER_SESSIONS, get_telegram_api
from storage import load_settings, save_settings

_qr_state: dict[str, tuple[TelegramClient, object]] = {}


def session_path(phone: str) -> Path:
    safe = phone.replace("+", "").replace(" ", "")
    return USER_SESSIONS / f"{safe}.session"


def api_ready() -> bool:
    api_id, api_hash = get_telegram_api()
    return bool(api_id and api_hash)


_CODE_HINT = re.compile(
    r"(?:"
    r"код\s+для\s+входа\s+в\s+telegram|"
    r"login\s+code(?:\s+for\s+telegram)?|"
    r"ваш\s+код|your\s+code|"
    r"код\s+подтверждения|confirmation\s+code"
    r")[:\s]+(\d{4,8})",
    re.I,
)


def extract_login_code(text: str) -> str | None:
    """Извлекает код входа: чистые цифры или из текста уведомления Telegram."""
    raw = text.strip()
    compact = raw.replace(" ", "")
    if re.fullmatch(r"\d{4,8}", compact):
        return compact
    m = _CODE_HINT.search(raw)
    if m:
        return m.group(1)
    return None


def is_login_blocked_notice(text: str) -> bool:
    low = text.lower()
    return (
        "незавершённая попытка входа" in low
        or "незавершенная попытка входа" in low
        or "остановил попытку входа" in low
        or "вход был заблокирован" in low
        or "login attempt" in low and "blocked" in low
    )


def normalize_phone(text: str) -> str | None:
    """Нормализует номер: +7999…, 7999…, 8999… → +7999…"""
    raw = re.sub(r"[\s\-()]", "", text.strip())
    if re.fullmatch(r"\+\d{10,15}", raw):
        return raw
    if re.fullmatch(r"8\d{10}", raw):
        return "+7" + raw[1:]
    if re.fullmatch(r"7\d{10}", raw):
        return "+" + raw
    if re.fullmatch(r"9\d{9}", raw):
        return "+7" + raw
    return None


def _code_delivery_message(sent) -> str:
    from telethon.tl.types.auth import SentCodeTypeApp, SentCodeTypeCall, SentCodeTypeSms

    t = sent.type
    length = getattr(t, "length", 5)
    wait = ""
    if getattr(sent, "timeout", None):
        wait = f"\nПовторная отправка возможна через ~{sent.timeout} сек."
    if isinstance(t, SentCodeTypeApp):
        return (
            f"Код ({length} цифр) придёт в приложение Telegram — открой чат "
            f"<b>Telegram</b> (служебные уведомления). Это <b>не SMS</b>."
            f"{wait}\n\n"
            f"⚠️ Напиши сюда <b>только цифры</b> кода. "
            f"Не пересылай сообщения из Telegram — из‑за этого вход блокируется."
        )
    if isinstance(t, SentCodeTypeSms):
        return f"Код ({length} цифр) отправлен по SMS на этот номер.{wait}"
    if isinstance(t, SentCodeTypeCall):
        return f"Код ({length} цифр) продиктуют звонком на этот номер.{wait}"
    return "Пришли код, который пришёл от Telegram." + wait


def _delivery_type_name(sent) -> str:
    from telethon.tl.types.auth import SentCodeTypeApp, SentCodeTypeCall, SentCodeTypeSms

    t = sent.type
    if isinstance(t, SentCodeTypeApp):
        return "app"
    if isinstance(t, SentCodeTypeSms):
        return "sms"
    if isinstance(t, SentCodeTypeCall):
        return "call"
    return "unknown"


def _phone_key(phone: str) -> str:
    return phone.lstrip("+")


async def _link_client(phone: str) -> TelegramClient:
    api_id, api_hash = get_telegram_api()
    existing = _link_clients.get(phone)
    if existing and existing.is_connected():
        return existing
    if existing:
        try:
            await existing.disconnect()
        except Exception:
            pass
    client = TelegramClient(str(session_path(phone)), int(api_id), api_hash)
    await client.connect()
    _link_clients[phone] = client
    return client


async def drop_link_client(phone: str = "") -> None:
    """Отключает клиент привязки (все или для одного номера)."""
    if phone:
        client = _link_clients.pop(phone, None)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        return
    for p, client in list(_link_clients.items()):
        _link_clients.pop(p, None)
        try:
            await client.disconnect()
        except Exception:
            pass


def _save_code_state(phone: str, sent) -> None:
    settings = load_settings()
    settings["account_link"] = {
        "step": "await_code",
        "phone": phone,
        "phone_code_hash": sent.phone_code_hash,
        "code_sent_at": datetime.now(timezone.utc).isoformat(),
        "resend_timeout": getattr(sent, "timeout", None),
        "delivery_type": _delivery_type_name(sent),
        "link_mode": "phone",
    }
    save_settings(settings)


def _unavailable_message() -> str:
    return (
        "Telegram временно не шлёт новые коды на этот номер "
        "(слишком много запросов подряд).\n\n"
        "Подожди 10–30 минут и напиши <code>повтор</code>, "
        "или привяжи через <b>QR</b>: /settings → Аккаунт → <b>Войти по QR</b>."
    )


async def _request_code(
    client: TelegramClient,
    phone: str,
    *,
    phone_code_hash: str = "",
    resend: bool = False,
) -> types.auth.SentCode:
    if resend and phone_code_hash:
        try:
            return await client(ResendCodeRequest(phone, phone_code_hash))
        except (PhoneCodeExpiredError, SendCodeUnavailableError):
            pass
    return await client.send_code_request(phone)


def parse_telegram_api(text: str) -> tuple[str, str] | None:
    """Парсит api_id и api_hash из сообщения владельца."""
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    api_id = ""
    api_hash = ""

    for i, ln in enumerate(lines):
        low = re.sub(r"[\s_]", "", ln.lower())
        if low in ("apiid", "telegramapiid") and i + 1 < len(lines):
            api_id = lines[i + 1].strip()
        if low in ("apihash", "telegramapihash") and i + 1 < len(lines):
            api_hash = lines[i + 1].strip()

    m_id = re.search(r"api[_\s]?id[:\s=]+(\d{5,10})", text, re.I)
    m_hash = re.search(r"api[_\s]?hash[:\s=]+([a-fA-F0-9]{32})", text, re.I)
    if m_id:
        api_id = api_id or m_id.group(1)
    if m_hash:
        api_hash = api_hash or m_hash.group(1)

    if not api_id:
        for ln in lines:
            if re.fullmatch(r"\d{5,10}", ln):
                api_id = ln
                break
    if not api_hash:
        for ln in lines:
            if re.fullmatch(r"[a-fA-F0-9]{32}", ln):
                api_hash = ln
                break

    if (
        api_id
        and api_hash
        and re.fullmatch(r"\d{5,10}", api_id)
        and re.fullmatch(r"[a-fA-F0-9]{32}", api_hash)
    ):
        return api_id, api_hash.lower()
    return None


def save_telegram_api(api_id: str, api_hash: str) -> None:
    """Сохраняет API-данные в .env и settings.json."""
    env_path = ROOT / ".env"
    keys = {"TELEGRAM_API_ID": api_id, "TELEGRAM_API_HASH": api_hash}
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    found: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        matched = False
        for key, val in keys.items():
            if line.startswith(f"{key}="):
                new_lines.append(f"{key}={val}")
                found.add(key)
                matched = True
                break
        if not matched:
            new_lines.append(line)
    for key, val in keys.items():
        if key not in found:
            new_lines.append(f"{key}={val}")
    env_path.write_text("\n".join(new_lines).rstrip() + "\n", encoding="utf-8")

    settings = load_settings()
    settings["telegram_api"] = {"api_id": api_id, "api_hash": api_hash}
    save_settings(settings)

    load_dotenv(ROOT / ".env", override=True)
    os.environ["TELEGRAM_API_ID"] = api_id
    os.environ["TELEGRAM_API_HASH"] = api_hash


def save_telegram_api_id(api_id: str) -> None:
    """Обновляет только api_id, hash оставляет прежним."""
    _, api_hash = get_telegram_api()
    if api_hash:
        save_telegram_api(api_id, api_hash)
        return
    _write_env_key("TELEGRAM_API_ID", api_id)
    settings = load_settings()
    settings.setdefault("telegram_api", {})["api_id"] = api_id
    save_settings(settings)
    os.environ["TELEGRAM_API_ID"] = api_id


def _write_env_key(key: str, value: str) -> None:
    env_path = ROOT / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    found = False
    new_lines: list[str] = []
    for line in lines:
        if line.startswith(f"{key}="):
            new_lines.append(f"{key}={value}")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={value}")
    env_path.write_text("\n".join(new_lines).rstrip() + "\n", encoding="utf-8")
    load_dotenv(ROOT / ".env", override=True)


def save_telegram_2fa_password(password: str) -> None:
    _write_env_key("TELEGRAM_2FA_PASSWORD", password)
    os.environ["TELEGRAM_2FA_PASSWORD"] = password


def parse_owner_secrets(text: str) -> dict:
    """Только явные API-данные (api_id + hash). Коды входа сюда не попадают."""
    out: dict = {}
    creds = parse_telegram_api(text)
    if creds:
        out["api_id"], out["api_hash"] = creds
    return out


def apply_owner_secrets(text: str) -> str | None:
    """Сохраняет только полную пару api_id + api_hash из настройки API."""
    secrets = parse_owner_secrets(text)
    if not secrets.get("api_id") or not secrets.get("api_hash"):
        return None
    save_telegram_api(secrets["api_id"], secrets["api_hash"])
    return f"API ID {secrets['api_id']} и hash сохранены"


async def try_auto_2fa() -> tuple[bool, str]:
    from config import get_telegram_2fa_password

    pw = get_telegram_2fa_password()
    if not pw:
        return False, ""
    return await confirm_2fa(pw)


async def start_link(
    phone: str,
    *,
    resend: bool = False,
    force_sms: bool = False,
) -> tuple[bool, str]:
    api_id, api_hash = get_telegram_api()
    if not api_id or not api_hash:
        return False, (
            "Сначала пришли API-данные с https://my.telegram.org "
            "(api_id и api_hash одним сообщением)."
        )

    phone = normalize_phone(phone) or phone
    settings = load_settings()
    link = settings.get("account_link", {})
    existing_hash = link.get("phone_code_hash", "") if link.get("phone") == phone else ""

    client = await _link_client(phone)
    try:
        if (force_sms or resend) and existing_hash:
            sent = await _request_code(
                client, phone, phone_code_hash=existing_hash, resend=True,
            )
        else:
            sent = await client.send_code_request(phone)
    except SendCodeUnavailableError:
        return False, _unavailable_message()
    except FloodWaitError as e:
        return False, f"Telegram просит подождать {e.seconds} сек перед новым кодом."
    except Exception as e:
        return False, f"Не удалось отправить код: {e}"

    _save_code_state(phone, sent)
    hint = _code_delivery_message(sent)
    action = "Код отправлен повторно" if resend and existing_hash else "Код отправлен"
    return (
        True,
        f"{action} на {phone}.\n\n{hint}\n\n"
        f"⚠️ Код в чат бота <b>нельзя</b> — Telegram сразу инвалидирует его.\n"
        f"Введи код в терминале: <code>./hoshi_ctl.sh link phone</code>",
    )


def qr_png_bytes(url: str) -> bytes:
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def start_qr_link() -> tuple[bool, str, bytes | None]:
    """Запускает QR-вход. Возвращает (ok, caption, png)."""
    api_id, api_hash = get_telegram_api()
    if not api_id or not api_hash:
        return False, "Сначала настрой API в /settings → Аккаунт.", None

    path = USER_SESSIONS / "qr_link.session"
    client = TelegramClient(str(path), int(api_id), api_hash)
    await client.connect()
    qr = await client.qr_login()
    _qr_state["default"] = (client, qr)

    settings = load_settings()
    settings["account_link"] = {
        "step": "await_qr",
        "phone": "",
        "phone_code_hash": "",
        "link_mode": "qr",
    }
    save_settings(settings)

    caption = (
        "Отсканируй QR в Telegram на телефоне:\n"
        "<b>Настройки → Устройства → Подключить устройство</b>\n\n"
        "⏱ QR живёт ~30 сек — если не успел, пришлю <b>обновлённый</b>.\n\n"
        "Или открой ссылку:\n"
        f"<code>{qr.url}</code>\n\n"
        "Если включена 2FA — после скана попрошу пароль."
    )
    return True, caption, qr_png_bytes(qr.url)


def _save_linked_account(me, phone: str = "") -> str:
    settings = load_settings()
    settings["linked_account"] = {
        "phone": phone or getattr(me, "phone", "") or "",
        "user_id": me.id,
        "username": me.username or "",
        "first_name": me.first_name or "",
        "linked_at": settings.get("updated_at", ""),
    }
    settings["account_link"] = {"step": "", "phone": "", "phone_code_hash": ""}
    save_settings(settings)
    return me.username or me.first_name or str(me.id)


async def wait_qr_link(
    timeout: float = 300.0,
    on_refresh: Callable[[str, bytes], Awaitable[None]] | None = None,
) -> tuple[bool, str, bool]:
    """Ждёт сканирования QR, обновляет токен до истечения. (ok, message, need_password)."""
    state = _qr_state.get("default")
    if not state:
        return False, "QR-сессия не найдена. Запусти снова.", False

    client, qr = state
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    while loop.time() < deadline:
        expires_in = (qr.expires - datetime.now(timezone.utc)).total_seconds()
        wait_chunk = min(max(expires_in - 2, 1), deadline - loop.time(), 25)

        try:
            me = await qr.wait(timeout=wait_chunk)
        except asyncio.TimeoutError:
            if loop.time() >= deadline:
                break
            try:
                await qr.recreate()
            except Exception as e:
                _qr_state.pop("default", None)
                try:
                    await client.disconnect()
                except Exception:
                    pass
                return False, f"Не удалось обновить QR: {e}", False
            if on_refresh:
                await on_refresh(qr.url, qr_png_bytes(qr.url))
            continue
        except SessionPasswordNeededError:
            settings = load_settings()
            settings["account_link"]["step"] = "await_2fa"
            settings["account_link"]["link_mode"] = "qr"
            save_settings(settings)
            ok, msg = await try_auto_2fa()
            if ok:
                return True, msg, False
            return True, "QR принят. Нужен пароль 2FA — пришли его.", True
        except Exception as e:
            _qr_state.pop("default", None)
            try:
                await client.disconnect()
            except Exception:
                pass
            err = str(e).lower()
            if "auth_token_expired" in err:
                return (
                    False,
                    "QR протух до сканирования. Запусти снова — теперь обновляется автоматически.",
                    False,
                )
            return False, f"QR не подтверждён: {e}", False
        else:
            _qr_state.pop("default", None)
            await client.disconnect()
            name = _save_linked_account(me)
            return True, f"Аккаунт @{name} привязан (QR).", False

    _qr_state.pop("default", None)
    try:
        await client.disconnect()
    except Exception:
        pass
    return (
        False,
        "QR не подтверждён — время вышло.\n"
        "Попробуй снова и сканируй сразу после получения QR.",
        False,
    )


async def confirm_code(code: str) -> tuple[bool, str, bool]:
    """Возвращает (ok, message, need_password)."""
    settings = load_settings()
    link = settings.get("account_link", {})
    phone = link.get("phone", "")
    phone_code_hash = link.get("phone_code_hash", "")
    if not phone:
        return False, "Сначала начни привязку в /settings.", False
    if not phone_code_hash:
        return (
            False,
            "Сессия кода потеряна (перезапуск бота?).\n"
            "Напиши <code>повтор</code> для нового кода "
            "или /settings → Аккаунт → <b>Войти по QR</b>.",
            False,
        )

    client = await _link_client(phone)
    sign_err: Exception | None = None
    for attempt_phone in (phone, _phone_key(phone)):
        try:
            await client.sign_in(
                phone=attempt_phone,
                code=code,
                phone_code_hash=phone_code_hash,
            )
            sign_err = None
            break
        except SessionPasswordNeededError:
            settings = load_settings()
            settings["account_link"]["step"] = "await_2fa"
            save_settings(settings)
            ok, msg = await try_auto_2fa()
            if ok:
                return True, msg, False
            return True, "Нужен пароль 2FA. Пришли его.", True
        except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
            sign_err = e
            break
        except ValueError:
            continue
        except Exception as e:
            sign_err = e
            break

    if sign_err is not None:
        if isinstance(sign_err, PhoneCodeInvalidError):
            return (
                False,
                "Неверный код.\n\n"
                "Проверь цифры в чате <b>Telegram</b> (не SMS). "
                "Не пересылай сообщения — Telegram блокирует вход.\n"
                "Новый код — <code>повтор</code> или /settings → Аккаунт → <b>Войти по QR</b>.",
                False,
            )
        if isinstance(sign_err, PhoneCodeExpiredError):
            settings = load_settings()
            settings["account_link"] = {
                "step": "await_code",
                "phone": phone,
                "phone_code_hash": "",
                "link_mode": "phone",
            }
            save_settings(settings)
            await drop_link_client(phone)
            return (
                False,
                "Код истёк.\n"
                "Напиши <code>повтор</code> — пришлю новый, "
                "или /settings → Аккаунт → <b>Войти по QR</b>.",
                False,
            )
        return False, f"Ошибка входа: {sign_err}", False

    me = await client.get_me()
    await drop_link_client(phone)
    name = _save_linked_account(me, phone)
    return True, f"Аккаунт @{name} привязан.", False


async def confirm_2fa(password: str) -> tuple[bool, str]:
    settings = load_settings()
    link = settings.get("account_link", {})
    phone = link.get("phone", "")
    link_mode = link.get("link_mode", "phone")

    api_id, api_hash = get_telegram_api()
    if link_mode == "qr":
        state = _qr_state.get("default")
        if state:
            client = state[0]
        else:
            path = USER_SESSIONS / "qr_link.session"
            client = TelegramClient(str(path), int(api_id), api_hash)
            await client.connect()
    else:
        if not phone:
            return False, "Нет активной привязки."
        client = _link_clients.get(phone)
        if not client or not client.is_connected():
            client = await _link_client(phone)
    try:
        await client.sign_in(password=password)
    except Exception as e:
        if link_mode != "qr":
            await drop_link_client(phone)
        else:
            await client.disconnect()
        return False, f"Неверный пароль 2FA: {e}"

    me = await client.get_me()
    if link_mode == "qr":
        await client.disconnect()
        _qr_state.pop("default", None)
    else:
        await drop_link_client(phone)

    settings["linked_account"] = {
        "phone": phone or getattr(me, "phone", "") or "",
        "user_id": me.id,
        "username": me.username or "",
        "first_name": me.first_name or "",
        "linked_at": settings.get("updated_at", ""),
    }
    settings["account_link"] = {"step": "", "phone": "", "phone_code_hash": ""}
    save_settings(settings)
    name = me.username or me.first_name or str(me.id)
    return True, f"Аккаунт @{name} привязан (2FA)."


async def unlink_account() -> str:
    settings = load_settings()
    phone = settings.get("linked_account", {}).get("phone", "")
    pending = settings.get("account_link", {}).get("phone", "")
    await drop_link_client()
    if phone:
        session_path(phone).unlink(missing_ok=True)
    if pending and pending != phone:
        session_path(pending).unlink(missing_ok=True)
    settings["linked_account"] = {
        "phone": "",
        "user_id": None,
        "username": "",
        "first_name": "",
        "linked_at": "",
    }
    settings["account_link"] = {"step": "", "phone": "", "phone_code_hash": ""}
    save_settings(settings)
    return "Аккаунт отвязан."
