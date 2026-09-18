#!/usr/bin/env python3
"""Привязка Telegram-аккаунта через терминал.

Код входа нельзя вводить в чат бота — Telegram сразу его инвалидирует.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import re
import sys

from account_linker import (
    api_ready,
    confirm_2fa,
    confirm_code,
    drop_link_client,
    normalize_phone,
    qr_png_bytes,
    start_link,
    start_qr_link,
    wait_qr_link,
)
from config import ROOT, USER_SESSIONS
from storage import load_settings


def _strip_html(text: str) -> str:
    return re.sub(r"</?[^>]+>", "", text)


async def cmd_phone(phone_arg: str | None) -> int:
    if not api_ready():
        print("Нет API-данных. Добавь TELEGRAM_API_ID и TELEGRAM_API_HASH в .env")
        return 1

    settings = load_settings()
    link = settings.get("account_link", {})
    phone = normalize_phone(phone_arg) if phone_arg else None
    if not phone:
        phone = normalize_phone(input("Номер (+7999...): ").strip())
    if not phone:
        print("Неверный формат номера")
        return 1

    if link.get("phone") == phone and link.get("phone_code_hash"):
        print(f"Используем ожидающий код для {phone}")
    else:
        ok, msg = await start_link(phone)
        print(_strip_html(msg))
        if not ok:
            return 1

    print()
    print("Код придёт в чат «Telegram» в приложении (не SMS).")
    print("⚠️  Вводи код ТОЛЬКО здесь — не в боте и не пересылай сообщения!")
    print()

    for _ in range(3):
        code = input("Код: ").strip()
        if not code:
            print("Отменено.")
            return 1
        ok, msg, need_pw = await confirm_code(code)
        print(_strip_html(msg))
        if need_pw:
            from account_linker import try_auto_2fa

            ok, msg = await try_auto_2fa()
            if not ok:
                pw = getpass.getpass("Пароль 2FA: ")
                ok, msg = await confirm_2fa(pw)
            print(msg)
        if ok:
            print("Готово ✅")
            return 0
        if "истёк" in msg.lower():
            again = input("Запросить новый код? [Y/n]: ").strip().lower()
            if again not in ("n", "no", "нет"):
                ok, msg = await start_link(phone, resend=True)
                print(_strip_html(msg))
                continue
        return 1
    return 1


async def cmd_qr() -> int:
    if not api_ready():
        print("Нет API-данных. Добавь TELEGRAM_API_ID и TELEGRAM_API_HASH в .env")
        return 1

    ok, caption, png = await start_qr_link()
    if not ok:
        print(caption)
        return 1

    qr_path = ROOT / "data" / "qr_login.png"
    qr_path.write_bytes(png)
    print(_strip_html(caption))
    print(f"\nQR сохранён: {qr_path}")
    print("Сканируй: Настройки → Устройства → Подключить устройство")
    print("QR обновляется автоматически каждые ~25 сек.\n")

    async def on_refresh(_url: str, fresh_png: bytes) -> None:
        qr_path.write_bytes(fresh_png)
        print(f"🔄 QR обновлён → {qr_path} (сканируй сразу!)")

    ok, msg, need_pw = await wait_qr_link(timeout=300.0, on_refresh=on_refresh)
    print(_strip_html(msg))
    if need_pw:
        from account_linker import try_auto_2fa

        ok, msg = await try_auto_2fa()
        if not ok:
            pw = getpass.getpass("Пароль 2FA: ")
            ok, msg = await confirm_2fa(pw)
        print(msg)
    if ok:
        print("Готово ✅")
        return 0
    return 1


async def cmd_code(code_arg: str | None) -> int:
    """Только ввод кода (если номер уже запрошен через бота)."""
    settings = load_settings()
    link = settings.get("account_link", {})
    if not link.get("phone") or not link.get("phone_code_hash"):
        print("Нет ожидающего кода. Сначала: ./hoshi_ctl.sh link phone")
        return 1
    code = (code_arg or input("Код: ")).strip()
    ok, msg, need_pw = await confirm_code(code)
    print(_strip_html(msg))
    if need_pw:
        from account_linker import try_auto_2fa

        ok, msg = await try_auto_2fa()
        if not ok:
            pw = getpass.getpass("Пароль 2FA: ")
            ok, msg = await confirm_2fa(pw)
        print(msg)
    return 0 if ok else 1


async def cmd_status() -> int:
    s = load_settings()
    linked = s.get("linked_account", {})
    link = s.get("account_link", {})
    if linked.get("user_id"):
        name = linked.get("username") or linked.get("first_name") or linked["user_id"]
        print(f"Привязан: @{name} ({linked.get('phone', '')})")
    else:
        print("Аккаунт не привязан")
    step = link.get("step", "")
    if step:
        print(f"Привязка в процессе: {step}, телефон: {link.get('phone', '—')}")
    return 0


async def _main_async(args: argparse.Namespace) -> int:
    try:
        if args.command == "phone":
            return await cmd_phone(args.phone)
        if args.command == "qr":
            return await cmd_qr()
        if args.command == "code":
            return await cmd_code(args.code)
        if args.command == "status":
            return await cmd_status()
        print(f"Неизвестная команда: {args.command}")
        return 1
    finally:
        await drop_link_client()


def main() -> None:
    parser = argparse.ArgumentParser(description="Привязка Telegram-аккаунта Hoshi")
    sub = parser.add_subparsers(dest="command")

    p_phone = sub.add_parser("phone", help="Привязка по номеру и коду")
    p_phone.add_argument("phone", nargs="?", help="+7999...")

    sub.add_parser("qr", help="Вход по QR (обновляется автоматически)")

    p_code = sub.add_parser("code", help="Только ввести код (номер уже запрошен)")
    p_code.add_argument("code", nargs="?", help="5 цифр")

    sub.add_parser("status", help="Статус привязки")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    sys.exit(asyncio.run(_main_async(args)))


if __name__ == "__main__":
    main()
