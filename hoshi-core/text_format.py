#!/usr/bin/env python3
"""Безопасное форматирование ответов агента для Telegram HTML."""
from __future__ import annotations

import html
import re


def _stash(pattern: str, text: str, voids: list[str], repl_fn) -> str:
    def _sub(m: re.Match) -> str:
        voids.append(repl_fn(m))
        return f"\x00V{len(voids) - 1}\x00"

    return re.sub(pattern, _sub, text, flags=re.DOTALL)


def prepare_telegram_html(text: str) -> str:
    """Markdown и валидные HTML-теги → безопасный Telegram HTML."""
    if not text:
        return text

    try:
        from rich_content import enhance_tables_for_telegram

        text = enhance_tables_for_telegram(text)
    except Exception:
        pass

    voids: list[str] = []

    # Сначала inline-код и markdown — иначе `<b>` внутри `...` склеится с чужим </b>.
    text = _stash(r"`([^`\n]+)`", text, voids, lambda m: f"<code>{html.escape(m.group(1))}</code>")
    text = _stash(r"\*\*([^*\n]+?)\*\*", text, voids, lambda m: f"<b>{html.escape(m.group(1))}</b>")
    text = _stash(r"__([^_\n]+?)__", text, voids, lambda m: f"<b>{html.escape(m.group(1))}</b>")
    text = _stash(
        r"(?<!\*)\*([^*\n]+?)\*(?!\*)",
        text,
        voids,
        lambda m: f"<i>{html.escape(m.group(1))}</i>",
    )

    for tag in ("b", "i", "u", "s", "code"):
        text = _stash(
            rf"<{tag}>(.*?)</{tag}>",
            text,
            voids,
            lambda m, t=tag: f"<{t}>{html.escape(m.group(1))}</{t}>",
        )

    text = re.sub(r"\*\*", "", text)
    text = html.escape(text)
    for i, chunk in enumerate(voids):
        text = text.replace(f"\x00V{i}\x00", chunk)

    return text


def strip_formatting(text: str) -> str:
    """Plain text fallback без разметки."""
    text = re.sub(r"</?(?:b|i|u|s|code|pre|a)(?:\s[^>]*)?>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", text)
    return text


_INTERNAL_LINE_RE = re.compile(
    r"^(?:"
    r"Проверяю\s+(?:логи|статус|код|маршрутизацию|настройки)|"
    r"ответ\s+должен\s+уйти\s+реплаем|"
    r"Обнаружил\s+баг|"
    r"Обнаружена\s+ошибка|"
    r"Ищу\s+причину|"
    r"Ищу\s+в\s+проекте|"
    r"Ищу\s+в\s+контексте|"
    r"Ищу\s+контекст|"
    r"🔧\s*Правлю|"
    r"Хозяин\s+разрешил.*ищу|"
    r"Окей,\s*Хозяин\s+разрешил|"
    r"Коротко\s+по\s+бабайке|"
    r"Усиливаю\s+фильтр|"
    r"готовлю\s+(?:короткий\s+)?(?:ответ|реплай)|"
    r"Сначала\s+(?:проверю|посмотрю)|"
    r"сначала\s+посмотрю|"
    r"Сейчас\s+посмотрю|"
    r"Секунду[,.…]?\s*(?:читаю|копаюсь|смотрю)|"
    r"Минутку[,.…]?|"
    r"Копаюсь\s+в\s+коде|"
    r"Скачиваю|"
    r"Смотрю\s+(?:логи|код|аватар)|"
    r"Ищу\s+(?:в\s+коде|обработку|причину)|"
    r"Разбираюсь\s+(?:с|почему)|"
    r"Уточняю|"
    r"Нужна\s+|"
    r"Кизу\s+просит|"
    r"Проверяю\s+обработку|"
    r"Проверяю,\s+как\s+в\s+проекте|"
    r"через\s+Telethon|"
    r"Пофиксила|"
    r"Усиливаю\s+обработку|"
    r"Вношу\s+правки|"
    r"Обнаружена\s+проблема|"
    r"Отвечу\s+.+|"
    r"Проверю\s+.+|"
    r"Причина\s+—|"
    r"Исправляю\s+.+|"
    r"Добавл(?:ю|яю)\s+(?:в\s+)?(?:поддержку|подсказку|генератор|\[\[)|"
    r"Меняю\s+дефолт|"
    r"Кизу\s+жаловался|"
    r"усиливаю\s+«?spicy|"
    r"Исправляю\s+логику|"
    r"Кизу\s+уточняет|"
    r"параллельно\s+нужно\s+обработать|"
    r"MP3\s+готов|"
    r"Маркер.*\[\[audio|"
    r"смотрю\s+логи\s+и\s+код\s+отправки\s+аудио|"
    r"Кизу\s+пишет,\s+что\s+MP3|"
    r"укрепляю\s+доставку\s+аудио|"
    r"проверю\s+отправку\s+аудио|"
    r"Добавляю\s+очистку\s+после\s+отправки|"
    r"🔧|"
    r"Меняю\s+свой\s+код|"
    r"Правлю\s+код|"
    r"Сейчас\s+пишет\s+(?:владелец|хозяин|\w+)|"
    r"Разбираю\s+контекст|"
    r"Вношу\s+правки|"
    r"Похоже,\s+(?:в\s+чат|снова)|"
    r"Похоже,\s+«Секунду|"
    r"Добавлю\s+(?:фильтр|детекцию)|"
    r"подготовлю\s+короткий\s+реплай|"
    r"усил(?:ю|иваю)\s+фильтрацию|"
    r"расширю\s+детекцию|"
    r"найду\s+в\s+коде|"
    r"Поджала\s+фильтр|"
    r"🔍\s*\*\*Разбор|"
    r"Промо\s+.+не\s+(?:скам|требует)|"
    r"ставлю\s+тишину|"
    r"Ответы\s+залипали|"
    r"очередь\s+забилась|"
    r"полный\s+текст\s+не\s+доходил|"
    r"должно\s+отвечать\s+нормально|"
    r"проверяю\s+вызов\s+агента|"
    r"обрезку\s+контекста|"
    r"фильтрацию\s+утечек|"
    r"Пост\s+будет\s+скопирован|"
    r"Статус:\s*Опубликован|"
    r"https?://t\.me/\+|"
    r"ТЗ:\s*Telegram|"
    r"Проверю,\s+что\s+сейчас\s+запущено|"
    r"Отключаю\s+фоновый\s+мониторинг|"
    r"очищаю\s+(?:их|очередь)|"
    r"очередь\s+в\s+твой\s+чат|"
    r"Хозяин\s+(?:спрашивает|просит|жалуется|сообщает|попросил|ждёт)|"
    r"Хозяин,\s*(?:сначала|отвечаю|виновата|поймала|исправляю)"
    r")",
    re.I | re.M,
)

_QUOTA_REPORT_RE = re.compile(
    r"(?:"
    r"Осталось\s+запросов\s*:[^\n]*|"
    r"(?:^|\n)\s*~?\d+\s+из\s+\d+\s*(?:запрос|request)?|"
    r"не\s+должно\s+быть\s+лимитов"
    r")",
    re.I | re.M,
)

_QUOTA_INCOMING_LINE_RE = re.compile(
    r"^(?:"
    r"Осталось\s+запросов\s*:[^\n]*|"
    r".*не\s+должно\s+быть\s+лимитов.*"
    r")$",
    re.I,
)

_CURSOR_SUBSCRIPTION_BLOCK_RE = re.compile(
    r"(?:"
    r"Разбор\s+по\s+(?:cursor-agent|LLM\s+agent)|"
    r"\*\*Подписка\*\*\s*:\s*Pro|"
    r"Cursor\s+Pro\s+(?:всё\s+ещё\s+)?активн|"
    r"Осталось\s+запросов\s*:"
    r")",
    re.I,
)


def sanitize_owner_incoming_text(text: str) -> str:
    """Убирает служебные строки квот из входящего сообщения хозяина."""
    if not text:
        return text
    lines: list[str] = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            lines.append(ln)
            continue
        if _QUOTA_INCOMING_LINE_RE.match(s):
            continue
        if re.search(r"осталось\s+запросов", s, re.I):
            continue
        lines.append(ln)
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def strip_quota_reports(text: str) -> str:
    """Убирает квоты Auto/Cursor из ответов хозяину."""
    if not text:
        return text
    parts = re.split(r"\n{2,}", text)
    kept: list[str] = []
    for p in parts:
        s = p.strip()
        if not s:
            continue
        if _QUOTA_REPORT_RE.search(s) or _CURSOR_SUBSCRIPTION_BLOCK_RE.search(s):
            continue
        s = _QUOTA_REPORT_RE.sub("", s).strip()
        if s:
            kept.append(s)
    out = "\n\n".join(kept).strip()
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out


def strip_cursor_branding(text: str) -> str:
    """Подменяет Cursor на нейтральные формулировки в ответах хозяину."""
    if not text:
        return text
    out = re.sub(r"\bCursor\s+Pro\b", "подписка агента", text, flags=re.I)
    out = re.sub(r"\bCursor\b", "агент", out, flags=re.I)
    out = re.sub(r"\bкурсор\b", "агент", out, flags=re.I)
    return out


_INTERNAL_PARA_RE = re.compile(
    r"(?:"
    r"Подозрение на скам|"
    r"Запрос личных данных|"
    r"Опасный запрос|"
    r"Хозяин\s+(?:спрашивает|просит|жалуется|сообщает|ждёт)|"
    r"Хозяин,\s*(?:отвечаю|сначала|виновата|поймала)|"
    r"cursor-agent|"
    r"Разбор\s+по\s+(?:cursor-agent|LLM)|"
    r"Осталось\s+запросов|"
    r"Cursor\s+Pro|"
    r"прем-эмодзи\s+уже\s+в\s+коде|"
    r"Argument\s+list\s+too\s+long|"
    r"промпт\s+передавался|"
    r"can_write_chats|"
    r"Что было:|"
    r"Что изменил|"
    r"Проверка\s*\(|"
    r"демон выкидывал|"
    r"Ответил отказом|"
    r"Сообщение:\s*https?://t\.me/"
    r")",
    re.I,
)


_PERSONA_BLEED_RE = re.compile(
    r"(?:"
    r"juzo.*(?:юи|yui)|(?:юи|yui).*(?:наша\s+личк|перемешан)|"
    r"наша\s+личк|сух(?:ие|их)\s+шаблон|перемешанных\s+контекст|"
    r"залез\s+чуж(?:ой|ого)\s+шаблон|улетел\s+мой\s+шаблон|"
    r"это\s+я,\s*юи|я\s+здесь,\s*это\s+юи|"
    r"поправила\s+код|привязан\w*\s+только\s+к\s+этому\s+чату|"
    r"прем-эмодзи\s+уже\s+в\s+коде|"
    r"сама\s+от\s+этого\s+короблю|"
    r"разберём\s+спокойно,\s*я\s+рядом|"
    r"без\s+чужих\s+чатов\s+и\s+перемешанных"
    r")",
    re.I,
)


def is_persona_bleed_template(text: str, *, interlocutor: str = "") -> bool:
    """Чужой шаблон персонажа (Юи→Juzo и т.п.) — не реплика собеседника."""
    body = (text or "").strip()
    if not body:
        return False
    peer = (interlocutor or "").strip().lower()
    if has_hoshi_external_prefix(body):
        return False
    if peer in ("лега", "legendaah", "lega") and re.search(
        r"лега|не\s+тво|призрак|не\s+от\s+тебя|шаблон", body, re.I
    ):
        if not re.match(r"^(?:😊\s*)?Juzo", body, re.I):
            return False
    if re.match(r"^😊\s*Juzo", body, re.I):
        return True
    if re.match(r"^Juzo\b", body, re.I) and re.search(
        r"юи|yui|шаблон|личк|перемешан|коробил", low := body.lower(), re.I
    ):
        return True
    if re.search(r"это\s+я,\s*юи|я\s+здесь,\s*это\s+юи", body, re.I):
        return True
    if re.match(
        r"^(?:секунду|минутку|думаю|копаюсь|читаю|"
        r"посмотрю\s+на\s+сервере|готово\s+—\s+прем-эмодзи)",
        body.lower(),
    ):
        return True
    low = body.lower()
    if _PERSONA_BLEED_RE.search(body):
        if body.lstrip().startswith("😊"):
            return True
        if re.search(r"наша\s+личк|перемешанных\s+контекст", low):
            return True
    if re.search(r"\bjuzo\b|джузо", low, re.I) and re.search(
        r"юи|yui|я\s+здесь.*юи|это\s+я.*юи", low, re.I
    ):
        if peer in ("лега", "legendaah", "lega"):
            return True
        if peer and peer not in ("juzo", "джузо", "suzuya", "суzuya"):
            return True
    if has_agent_reply_prefix(body) and re.search(
        r"^(?:😊\s*)?(?:секунду|минутку|думаю|копаюсь|"
        r"посмотрю\s+на\s+сервере|готово\s+—\s+прем-эмодзи)",
        low,
    ):
        return True
    return False


def _is_internal_paragraph(p: str) -> bool:
    s = p.strip()
    if not s:
        return True
    if is_persona_bleed_template(s):
        return True
    if _INTERNAL_PARA_RE.search(s):
        return True
    if s.startswith("⚠️") or s.startswith("🔒") or s.startswith("⛔"):
        return True
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    if len(lines) == 1 and _BOT_SERVICE_LINE_RE.match(lines[0]):
        return True
    if lines and all(_INTERNAL_LINE_RE.match(ln) for ln in lines):
        return True
    return False


def _strip_internal_lead(text: str) -> str:
    """Убирает служебное начало вроде «Проверяю…» перед ответом для чата."""
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    while parts and _INTERNAL_LINE_RE.match(parts[0].strip()):
        parts.pop(0)
    return " ".join(parts).strip()


def _strip_inline_internal(text: str) -> str:
    """Убирает служебные предложения и куски внутри строки."""
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    kept = [p.strip() for p in parts if p.strip() and not _INTERNAL_LINE_RE.match(p.strip())]
    text = " ".join(kept).strip()
    text = re.sub(
        r"[^.!?…]*(?:сначала\s+посмотрю|скачиваю|проверяю|ищу\s+в\s+проекте|"
        r"найду\s+в\s+коде|расширю\s+детекцию|поджала\s+фильтр|"
        r"через\s+telethon|уточняю|нужна\s+\w+)[^.!?…]*[.!?…]?\s*",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(r"\s*—\s*[^.!?…]*(?:сначала|скачива|проверя|ищу)[^.!?…]*", "", text, flags=re.I)
    return text.strip()


_HOSHI_INTERNAL_REPLACEMENTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"data/tg_inbox(?:/\*\.json)?", re.I), "queue/inbox/"),
    (re.compile(r"data/tg_outbox(?:/\*\.json)?", re.I), "queue/outbox/"),
    (re.compile(r"data/agent_sessions(?:/[^\s`]+)?(?:\.json)?", re.I), "sessions/<chat_id>.json"),
    (re.compile(r"hoshi_daemon\.py", re.I), "worker.py"),
    (re.compile(r"tg_bridge\.py", re.I), "bridge.py"),
    (re.compile(r"chat_watcher\.py", re.I), "userbot_listener.py"),
    (re.compile(r"hoshi_ctl\.sh", re.I), "ctl.sh"),
    (re.compile(r"cursor-agent(?:\s+CLI)?", re.I), "LLM agent CLI"),
    (re.compile(r"/root/projects/Hoshi", re.I), "<project_root>"),
    (re.compile(r"account_linker\.py", re.I), "auth_link.py"),
]


def redact_hoshi_internals(text: str) -> str:
    """Подменяет утечки внутренних имён Hoshi — страховка для внешних чатов."""
    if not text:
        return text
    for pat, repl in _HOSHI_INTERNAL_REPLACEMENTS:
        text = pat.sub(repl, text)
    return text


_OWNER_REFUSAL_RE = re.compile(
    r"(?:"
    r"не\s+(?:могу|выйдет|кача|тянет|добавлю|встраиваю|шлёт)|"
    r"бот\s+не\s+тянет|"
    r"rule34|redtube|"
    r"первое\s+правило|"
    r"отдельн\w+\s+обход|"
    r"не\s+будет|"
    r"отказ\w*\s+ни\s+в\s+чём"
    r")",
    re.I,
)

_CANNED_DELETE_REFUSAL_RE = re.compile(
    r"я\s+агент\s+hoshi\s+—\s+не\s+могу\s+удалять\s+чат",
    re.I,
)

_CANNED_TZ_TEMPLATE_RE = re.compile(
    r"(?:"
    r"кратко\s+по\s+тз\s+такого\s+агента|"
    r"telegram-бот\s+для\s+владельца.*очередь\s+задач\s+на\s+диске|"
    r"могу\s+расписать\s+один\s+блок\s*\(\s*безопасность"
    r")",
    re.I | re.S,
)

_CANNED_IRIS_GREETING_RE = re.compile(
    r"(?:"
    r"на\s+связи[^.\n]{0,80}(?:ветк|iris|ирис|black\s*diamond)|"
    r"ветк[аауё]?\s+.{0,24}(?:iris|ирис).{0,40}(?:black\s*diamond|бирж)|"
    r"чем\s+помочь[^.\n]{0,60}(?:бирж|мешок|чек)|"
    r"вижу\s+по\s+чату[^.\n]{0,80}(?:голд|ирис|мешок|продаж|заявк)"
    r")",
    re.I | re.S,
)


def is_canned_tz_template(text: str) -> bool:
    """Заевший шаблон «Кратко по ТЗ такого агента…» — не слать в чат."""
    return bool(_CANNED_TZ_TEMPLATE_RE.search(text or ""))


def is_iris_greeting_template(text: str) -> bool:
    """Заевший шаблон «На связи, ветка iris… Чем помочь — биржа, мешок…»."""
    return bool(_CANNED_IRIS_GREETING_RE.search(text or ""))


def strip_iris_greeting_template(text: str) -> str:
    if not text or not is_iris_greeting_template(text):
        return text
    parts = re.split(r"\n{2,}", text.strip())
    kept = [p.strip() for p in parts if p.strip() and not is_iris_greeting_template(p)]
    if kept:
        return "\n\n".join(kept).strip()
    return ""


def strip_canned_tz_template(text: str) -> str:
    if not text or not is_canned_tz_template(text):
        return text
    parts = re.split(r"\n{2,}", text.strip())
    kept = [p.strip() for p in parts if p.strip() and not is_canned_tz_template(p)]
    if kept:
        return "\n\n".join(kept).strip()
    return ""


def is_canned_delete_refusal(text: str) -> bool:
    """Шаблон «не могу удалять чаты…» — только по прямому запросу удалить."""
    return bool(_CANNED_DELETE_REFUSAL_RE.search(text or ""))


def strip_canned_delete_refusal(text: str) -> str:
    if not text or not is_canned_delete_refusal(text):
        return text
    parts = re.split(r"\n{2,}", text.strip())
    kept = [p.strip() for p in parts if p.strip() and not is_canned_delete_refusal(p)]
    if kept:
        return "\n\n".join(kept).strip()
    return ""


def sanitize_external_caption(text: str, *, allow_delete_refusal: bool = False) -> str:
    """Подпись к медиа во внешний чат — без шаблонов «не могу удалять…» и «Кратко по ТЗ…»."""
    cap = (text or "").strip()
    if not cap or allow_delete_refusal:
        return cap
    cap = strip_canned_delete_refusal(cap)
    if is_canned_delete_refusal(cap):
        return ""
    cap = strip_canned_tz_template(cap)
    if is_canned_tz_template(cap):
        return ""
    return cap.strip()


def strip_owner_refusals(text: str) -> str:
    """Убирает отказы из ответа владельцу — без подмены на заглушку «качаю»."""
    if not text:
        return text
    parts = re.split(r"\n{2,}", text)
    kept = [p.strip() for p in parts if p.strip() and not _OWNER_REFUSAL_RE.search(p)]
    if kept:
        return "\n\n".join(kept).strip()
    return text.strip()


_BOT_SERVICE_LINE_RE = re.compile(
    r"^(?:"
    r"Хозяин\s+(?:спрашивает|просит|жалуется|сообщает|хочет|передаёт)\b|"
    r"Кизу\s+просит|"
    r"🔧|"
    r"Меняю\s+свой\s+код|"
    r"Правлю\s+код|"
    r"Сейчас\s+пишет|"
    r"Проверяю\s+|"
    r"Сначала\s+(?:проверю|посмотрю)|"
    r"сначала\s+посмотрю|"
    r"Сейчас\s+посмотрю|"
    r"Секунду[,.…]?\s*(?:читаю|копаюсь|смотрю)|"
    r"Минутку[,.…]?|"
    r"Копаюсь\s+в\s+коде|"
    r"Скачиваю|"
    r"Ищу\s+(?:в\s+коде|в\s+проекте|причину|обработку)|"
    r"найду\s+в\s+коде|"
    r"расширю\s+детекцию|"
    r"Поджала\s+фильтр|"
    r"Добавлю\s+(?:фильтр|детекцию)|"
    r"подготовлю\s+короткий\s+реплай|"
    r"Уточняю|"
    r"Разбираюсь\s+(?:с|почему)|"
    r"Нужна\s+|"
    r"Нужно\s+найти\s+подходящее\s+изображение"
    r")",
    re.I,
)


def _strip_bot_service_lead(text: str) -> str:
    text = re.sub(
        r"^Хозяин\s+(?:спрашивает|просит|жалуется|сообщает)[^\n]*—\s*[^\n]+(?:\n+|$)",
        "",
        text.strip(),
        flags=re.I,
    )
    return text.strip()


def _strip_bot_service_inline_paragraph(text: str) -> str:
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    kept = [
        p.strip()
        for p in parts
        if p.strip() and not _BOT_SERVICE_LINE_RE.match(p.strip())
    ]
    text = " ".join(kept).strip()
    text = re.sub(
        r"[^.!?…]*(?:сначала\s+посмотрю|скачиваю|проверяю|ищу\s+в\s+проекте|"
        r"через\s+telethon|уточняю)[^.!?…]*[.!?…]?\s*",
        "",
        text,
        flags=re.I,
    )
    return text.strip()


def _strip_bot_service_inline(text: str) -> str:
    paras = re.split(r"\n{2,}", text.strip())
    if len(paras) <= 1:
        return _strip_bot_service_inline_paragraph(text)
    cleaned = [_strip_bot_service_inline_paragraph(p) for p in paras if p.strip()]
    return "\n\n".join(cleaned).strip()


_LEAKED_POLICY_BLOCK_RE = re.compile(
    r"(?:^|\n)\s*\*{0,2}(?:"
    r"Iris-биржа\s*\(фоновый\s+мониторинг\)|"
    r"Аниме-новости\s*\(фон\)|"
    r"Биржи\s+рекламы\s*\(фон\)"
    r")\s*\*{0,2}\s*:",
    re.I | re.M,
)
_LEAKED_USER_MSG_RE = re.compile(
    r"\n\s*\*{0,2}Новое\s+сообщение\s+(?:владельца|во\s+внешнем\s+чате)\s*:\s*\*{0,2}",
    re.I,
)


def _strip_leaked_prompt_blocks(text: str) -> str:
    """Убирает утечки служебных блоков промпта в ответ агента."""
    m = _LEAKED_POLICY_BLOCK_RE.search(text)
    if m:
        text = text[: m.start()]
    m = _LEAKED_USER_MSG_RE.search(text)
    if m:
        text = text[: m.start()]
    return text.strip()


def strip_bot_reply(text: str) -> str:
    """Служебные вставки агента убираем; обращение «Хозяин, …» в ответе оставляем."""
    if not text:
        return text
    text = _strip_leaked_prompt_blocks(text)
    text = re.sub(r"\[\[silent\]\]\s*", "", text, flags=re.I)
    try:
        from voice_delivery import strip_reaction_markers

        text = strip_reaction_markers(text)
    except Exception:
        pass
    m = re.search(r"\n---\s*\n\s*\*{0,2}Для владельца", text, flags=re.I)
    if m:
        text = text[: m.start()]
    for marker in (
        r"\n\s*\*{0,2}Для владельца\s*\(код\)",
        r"\n\s*\*{0,2}Проверка\s*\*{0,2}\s*\(после перезапуска",
    ):
        m = re.search(marker, text, flags=re.I)
        if m:
            text = text[: m.start()]
    lines = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            lines.append(ln)
            continue
        if _BOT_SERVICE_LINE_RE.match(s) or _INTERNAL_LINE_RE.match(s):
            continue
        lines.append(ln)
    text = "\n".join(lines)
    text = _strip_bot_service_lead(text)
    text = _strip_bot_service_inline(text)
    text = strip_quota_reports(text)
    text = strip_cursor_branding(text)
    return strip_owner_refusals(text).strip()


_OWNER_DIRECT_START_RE = re.compile(
    r"^(?:\*{0,2})?(?:Хозяин,\s|Спасибо,\s*Хозяин|Хозяин\s*—)",
    re.I,
)


def is_owner_direct_reply(text: str) -> bool:
    """Ответ адресован владельцу, а не собеседнику во внешнем чате."""
    s = (text or "").strip()
    if not s:
        return False
    first = re.split(r"\n{2,}", s, maxsplit=1)[0].strip()
    first_line = first.splitlines()[0].strip() if first else ""
    return bool(_OWNER_DIRECT_START_RE.match(first_line))


def strip_interlocutor_misaddress(text: str, *names: str) -> str:
    """Убирает обращение к собеседнику в начале ответа, когда в чате пишет хозяин."""
    if not text:
        return text
    uniq: list[str] = []
    seen: set[str] = set()
    for raw in names:
        name = (raw or "").strip().split("(")[0].strip()
        key = name.lower()
        if name and key not in seen:
            seen.add(key)
            uniq.append(name)
    if not uniq:
        return text
    result = text.strip()
    lead = (
        r"^(?:"
        r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0000FE0F\s"
        r"🥳👋🙂😊✨⭐️*#]+"
        r")*"
    )
    for name in uniq:
        pat = re.compile(
            lead
            + rf"(?:\*{{0,2}})?{re.escape(name)}(?:\*{{0,2}})?"
            + r"[,!:—\-\s]+",
            re.I | re.U,
        )
        result = pat.sub("", result, count=1).strip()
    return result


def _external_reply_parts(text: str) -> list[str]:
    """Абзацы ответа для внешнего чата; одиночные \\n не схлопываем в один блок."""
    parts = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    if len(parts) == 1 and "\n" in parts[0]:
        lines = [ln.strip() for ln in parts[0].splitlines() if ln.strip()]
        if len(lines) > 1:
            return lines
    return parts


def extract_owner_direct_reply(text: str) -> str:
    """Только блоки, адресованные владельцу (для бота 1:1)."""
    text = strip_bot_reply(text or "")
    parts = _external_reply_parts(text)
    kept = [p for p in parts if is_owner_direct_reply(p) and not _is_internal_paragraph(p)]
    return "\n\n".join(kept).strip()


def extract_non_owner_chat_reply(text: str, *, interlocutor: str = "") -> str:
    """Абзацы для внешнего чата — без блоков «Хозяин,» и [[silent]]."""
    text = strip_bot_reply(text or "")
    text = re.sub(r"\[\[silent\]\]\s*", "", text, flags=re.I)
    parts = _external_reply_parts(text)
    kept = [
        p
        for p in parts
        if not is_owner_direct_reply(p)
        and not _is_internal_paragraph(p)
        and not is_persona_bleed_template(p, interlocutor=interlocutor)
    ]
    return "\n\n".join(kept).strip()


_AUTOPOST_SPAM_RE = re.compile(
    r"(?:"
    r"https?://t\.me/\+|"
    r"Пост\s+будет\s+скопирован|"
    r"Статус:\s*Опубликован|"
    r"Ссылка:\s*https?://t\.me/|"
    r"мне\s+по\s+скидке|"
    r"📍\s*.+(?:Tr@p|Yaoi|хентай|Comics|TRAPLAND)"
    r")",
    re.I,
)


_LEAKED_TZ_BLOCK_RE = re.compile(
    r"(?:"
    r"ТЗ:\s*Telegram\s+AI|"
    r"Архитектура\s*\(\s*3\s+процесса|"
    r"bridge\.py|userbot_listener\.py|worker\.py"
    r")",
    re.I,
)




_SPEC_FORBIDDEN_RE = re.compile(
    r"(?:"
    r"\bcursor\b|курсор|"
    r"\bhoshi\b|хоши|"
    r"bridge\.py|worker\.py|hoshi_daemon|tg_bridge|"
    r"queue/inbox|queue/outbox|"
    r"\[Hoshi\]|cursor-agent|"
    r"data/incoming_media|/root/projects/"
    r")",
    re.I,
)


def sanitize_external_spec_reply(text: str) -> str:
    if not text:
        return text
    if not _SPEC_FORBIDDEN_RE.search(text) and not _LEAKED_TZ_BLOCK_RE.search(text):
        return text
    cleaned = strip_leaked_spec_blocks(text) or text
    cleaned = redact_hoshi_internals(cleaned)
    if _LEAKED_TZ_BLOCK_RE.search(cleaned):
        parts = re.split(r"\n{2,}", cleaned)
        kept = [p for p in parts if not _LEAKED_TZ_BLOCK_RE.search(p)]
        cleaned = "\n\n".join(kept).strip()
    return cleaned


def strip_leaked_spec_blocks(text: str) -> str:
    """Убирает утечки ТЗ/архитектуры проекта во внешний чат."""
    if not text or not _LEAKED_TZ_BLOCK_RE.search(text):
        return text
    parts = re.split(r"\n{2,}", text)
    kept = [p for p in parts if not _LEAKED_TZ_BLOCK_RE.search(p)]
    return "\n\n".join(kept).strip()


def strip_autopost_spam(text: str) -> str:
    """Убирает утечки автопостинга/кросс-поста каналов в ответ для чата."""
    if not text:
        return text
    lines: list[str] = []
    skip_block = False
    for ln in text.splitlines():
        s = ln.strip()
        if _AUTOPOST_SPAM_RE.search(s):
            skip_block = True
            continue
        if skip_block and s and re.match(r"^\d+\.\s*📍", s):
            continue
        if skip_block and not s:
            continue
        skip_block = False
        lines.append(ln)
    out = "\n".join(lines).strip()
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out


def split_telegram_text(text: str, *, limit: int = 4000) -> list[str]:
    """Делит длинный ответ на части для Telegram без обрезки посередине абзаца."""
    blob = (text or "").strip()
    if not blob:
        return []
    if len(blob) <= limit:
        return [blob]
    parts: list[str] = []
    buf = ""
    for para in re.split(r"(\n{2,})", blob):
        if not para:
            continue
        if len(para) > limit:
            if buf.strip():
                parts.append(buf.strip())
                buf = ""
            for i in range(0, len(para), limit):
                parts.append(para[i : i + limit].strip())
            continue
        candidate = f"{buf}{para}" if buf else para
        if len(candidate) > limit and buf.strip():
            parts.append(buf.strip())
            buf = para
        else:
            buf = candidate
    if buf.strip():
        parts.append(buf.strip())
    return parts or [blob[:limit]]


def strip_external_reply(text: str, *, interlocutor: str = "") -> str:
    """Убирает блоки для владельца/кода — только текст для внешнего чата."""
    raw = text or ""
    has_silent = bool(re.search(r"\[\[silent\]\]", raw, re.I))
    text = strip_bot_reply(raw)
    text = re.sub(r"\[\[silent\]\]\s*", "", text, flags=re.I)
    sep = re.search(r"\n---\s*\n", text)
    if sep:
        head = text[: sep.start()]
        tail = text[sep.end() :]
        if has_silent and re.search(r"^\s*\*{0,2}Хозяин\b", head, re.I | re.M):
            text = ""
        elif re.search(
            r"^\s*\*{0,2}(?:Хозяин|Для владельца|Что было)",
            tail,
            re.I | re.M,
        ):
            text = head
        elif re.search(r"^\s*\*{0,2}Хозяин\b", head, re.I | re.M) or re.search(
            r"отвечаю\s+\w+", head, re.I
        ):
            text = tail
    cut_markers = (
        r"\n---\s*\n\s*\*{0,2}(?:Хозяин|Для владельца)",
        r"\n\s*\*{0,2}Для владельца\s*\(код\)",
        r"\n\s*\*{0,2}Хозяин,\s*что\s+было",
        r"\n\s*\*{0,2}Проверка\s*\*{0,2}\s*\(после перезапуска",
    )
    for marker in cut_markers:
        m = re.search(marker, text, flags=re.IGNORECASE)
        if m:
            text = text[: m.start()]
    parts = _external_reply_parts(text)
    filtered = []
    seen_norm = set()
    for p in parts:
        s = p.strip()
        if (
            not s
            or _is_internal_paragraph(s)
            or is_owner_direct_reply(s)
            or is_persona_bleed_template(s, interlocutor=interlocutor)
            or is_canned_delete_refusal(s)
            or is_canned_tz_template(s)
        ):
            continue
        norm = re.sub(r"\s+", " ", s.lower())[:140]
        if ("бабайк" in norm or "бобр" in norm or "измены нет" in norm) and norm in seen_norm:
            continue
        seen_norm.add(norm)
        filtered.append(s)
    text = "\n\n".join(filtered)
    while is_owner_direct_reply(text):
        chunks = _external_reply_parts(text)
        text = "\n\n".join(chunks[1:]).strip() if len(chunks) > 1 else ""
    m = re.match(r"Хозяин,\s*отвечаю\s+[^.\n]+[.\n]+\s*", text, re.I)
    if m:
        text = text[m.end() :]
    lines = [ln for ln in text.splitlines() if not _INTERNAL_LINE_RE.match(ln.strip())]
    text = "\n".join(lines)
    text = _strip_internal_lead(text)
    text = _strip_inline_internal(text)
    text = sanitize_external_spec_reply(strip_leaked_spec_blocks(strip_autopost_spam(text)))
    text = strip_canned_tz_template(text)
    text = redact_hoshi_internals(strip_formatting(text).strip())
    if has_silent and re.search(
        r"(?:Отправить это в чат|Разрешаешь|Чат на муте|в чат не писала|только тебе)",
        raw,
        re.I,
    ):
        return ""
    return text


_CURSOR_AUTH_REPLY_RE = re.compile(
    r"(?:"
    r"authentication\s+required|"
    r"stored\s+authentication\s+is\s+invalid|"
    r"please\s+run\s+['\"]agent\s+login|"
    r"CURSOR_API_KEY"
    r")",
    re.I,
)


def is_cursor_auth_reply(text: str) -> bool:
    """Служебный текст cursor-agent — не слать во внешние чаты."""
    return bool(_CURSOR_AUTH_REPLY_RE.search(text or ""))


HOSHI_EXTERNAL_PREFIX = "🥳"
# Премиум-эмодзи хозяина (document_id из его сообщений в external-чате).
HOSHI_EXTERNAL_EMOJI_DOCUMENT_ID = 5388749493237744497

# Футер канала @HoshiKojima (document_id с публичной страницы канала).
NEWS_FOOTER_LOOP_EMOJI_ID = 5467405436942561997
NEWS_FOOTER_SPARKLE_EMOJI_ID = 5206461245720379293
NEWS_FOOTER_TORII_EMOJI_ID = 5256226752606280157
NEWS_FOOTER_CHANNEL_URL = "https://t.me/HoshiKojima"
NEWS_FOOTER_VPN_URL = "http://t.me/saovpnbot"
NEWS_FOOTER_CHANNEL_TEXT = "Hoshi Kojima"
NEWS_FOOTER_VPN_TEXT = "Лучший впн"
NEWS_FOOTER_LINE_LOOPS = "➿" * 10
NEWS_FOOTER_LINE_BRAND = (
    f"✨{NEWS_FOOTER_CHANNEL_TEXT} | {NEWS_FOOTER_VPN_TEXT} ⛩️"
)
OWNER_BOT_PREFIX = "✨"


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def hoshi_external_prefix_entities(*, offset: int = 0) -> list:
    from telethon.tl.types import MessageEntityCustomEmoji

    return [
        MessageEntityCustomEmoji(
            offset=offset,
            length=_utf16_len(HOSHI_EXTERNAL_PREFIX),
            document_id=HOSHI_EXTERNAL_EMOJI_DOCUMENT_ID,
        )
    ]


def _external_prefix_applies(chat_id: int) -> bool:
    from config import OWNER_ID

    try:
        from chat_router import is_bot_chat_id, is_stealth_chat

        cid = int(chat_id)
        if cid == OWNER_ID or is_bot_chat_id(cid):
            return False
        if is_stealth_chat(cid):
            return False
        from chat_router import get_chat_preferences

        if get_chat_preferences(cid).get("no_prefix"):
            return False
    except (TypeError, ValueError):
        return False
    return True


AGENT_REPLY_PREFIXES = (HOSHI_EXTERNAL_PREFIX, "😊")


def has_agent_reply_prefix(text: str) -> bool:
    t = (text or "").lstrip()
    return bool(t) and any(t.startswith(p) for p in AGENT_REPLY_PREFIXES)


def has_hoshi_external_prefix(text: str) -> bool:
    t = (text or "").lstrip()
    return bool(t) and t.startswith(HOSHI_EXTERNAL_PREFIX)


def strip_hoshi_external_prefix(text: str) -> str:
    t = (text or "").lstrip()
    if not t.startswith(HOSHI_EXTERNAL_PREFIX):
        return text or ""
    rest = t[len(HOSHI_EXTERNAL_PREFIX) :].lstrip()
    return rest


def prefix_hoshi_external(text: str) -> str:
    """Маркер Hoshi перед ответом во внешний чат (премиум-эмодзи)."""
    t = (text or "").strip()
    if not t or has_hoshi_external_prefix(t):
        return text or ""
    return f"{HOSHI_EXTERNAL_PREFIX} {t}"


def _external_prefix_offset(text: str) -> int:
    """UTF-16-смещение префикса 🥳 в исходящем тексте."""
    t = text or ""
    idx = t.find(HOSHI_EXTERNAL_PREFIX)
    if idx < 0:
        return 0
    return _utf16_len(t[:idx])


def hoshi_outbound_parts(text: str, chat_id: int) -> tuple[str, list | None]:
    """Текст и entities для исходящего ответа Hoshi во внешний чат."""
    if not (text or "").strip():
        return text or "", None
    try:
        from voice_delivery import strip_reaction_markers

        text = strip_reaction_markers(text)
    except Exception:
        pass
    if not _external_prefix_applies(chat_id):
        return strip_hoshi_external_prefix(text), None
    outbound = prefix_hoshi_external(text)
    if not has_hoshi_external_prefix(outbound):
        return outbound, hoshi_external_prefix_entities(offset=0)
    # Премиум-метка нужна всегда, даже если агент уже вставил обычный 🥳 в текст.
    return outbound, hoshi_external_prefix_entities(
        offset=_external_prefix_offset(outbound)
    )


def hoshi_outbound_text(text: str, chat_id: int) -> str:
    """Текст исходящего ответа Hoshi во внешний чат (не бот 1:1)."""
    outbound, _ = hoshi_outbound_parts(text, chat_id)
    return outbound


def news_footer_text() -> str:
    """Текст футера постов канала (премиум-эмодзи подставляются через entities)."""
    return f"#animenews\n{NEWS_FOOTER_LINE_LOOPS}\n{NEWS_FOOTER_LINE_BRAND}"


def news_footer_entities(*, offset: int = 0) -> list:
    """Премиум-эмодзи и текстовые ссылки футера новостного канала."""
    from telethon.tl.types import MessageEntityCustomEmoji, MessageEntityTextUrl

    footer = news_footer_text()
    entities: list = []
    loop_start = footer.find("➿")
    if loop_start >= 0:
        for i in range(10):
            pos = loop_start + i
            entities.append(
                MessageEntityCustomEmoji(
                    offset=offset + _utf16_len(footer[:pos]),
                    length=_utf16_len("➿"),
                    document_id=NEWS_FOOTER_LOOP_EMOJI_ID,
                )
            )
    sparkle_pos = footer.find("✨")
    if sparkle_pos >= 0:
        entities.append(
            MessageEntityCustomEmoji(
                offset=offset + _utf16_len(footer[:sparkle_pos]),
                length=_utf16_len("✨"),
                document_id=NEWS_FOOTER_SPARKLE_EMOJI_ID,
            )
        )
    brand_start = footer.find(NEWS_FOOTER_LINE_BRAND)
    if brand_start >= 0:
        brand = NEWS_FOOTER_LINE_BRAND
        ch_pos = brand.find(NEWS_FOOTER_CHANNEL_TEXT)
        if ch_pos >= 0:
            entities.append(
                MessageEntityTextUrl(
                    offset=offset + _utf16_len(footer[: brand_start + ch_pos]),
                    length=_utf16_len(NEWS_FOOTER_CHANNEL_TEXT),
                    url=NEWS_FOOTER_CHANNEL_URL,
                )
            )
        vpn_pos = brand.find(NEWS_FOOTER_VPN_TEXT)
        if vpn_pos >= 0:
            entities.append(
                MessageEntityTextUrl(
                    offset=offset + _utf16_len(footer[: brand_start + vpn_pos]),
                    length=_utf16_len(NEWS_FOOTER_VPN_TEXT),
                    url=NEWS_FOOTER_VPN_URL,
                )
            )
        torii_pos = brand.find("⛩")
        if torii_pos >= 0:
            entities.append(
                MessageEntityCustomEmoji(
                    offset=offset + _utf16_len(footer[: brand_start + torii_pos]),
                    length=_utf16_len("⛩️"),
                    document_id=NEWS_FOOTER_TORII_EMOJI_ID,
                )
            )
    return entities


def _parse_markdown_bot_entities(text: str) -> tuple[str, list]:
    """**bold**, `code`, *italic* → plain text + Bot API entities."""
    from telegram import MessageEntity
    from telegram.constants import MessageEntityType as T

    plain = text or ""
    entities: list = []
    patterns = (
        (r"\*\*(.+?)\*\*", T.BOLD),
        (r"`([^`\n]+)`", T.CODE),
        (r"(?<!\*)\*([^*\n]+?)\*(?!\*)", T.ITALIC),
    )
    for pat, etype in patterns:
        while True:
            m = re.search(pat, plain)
            if not m:
                break
            before = plain[: m.start()]
            content = m.group(1)
            plain = before + content + plain[m.end() :]
            entities.append(
                MessageEntity(
                    type=etype,
                    offset=_utf16_len(before),
                    length=_utf16_len(content),
                )
            )
    entities.sort(key=lambda e: e.offset)
    return plain, entities


def owner_bot_outbound_parts(text: str) -> tuple[str, list | None]:
    """Префикс ✨ (премиум) + markdown → plain + Bot API entities для чата с хозяином."""
    from telegram import MessageEntity
    from telegram.constants import MessageEntityType as T

    body = (text or "").strip()
    if not body:
        return body, None

    sparkle = MessageEntity(
        type=T.CUSTOM_EMOJI,
        offset=0,
        length=_utf16_len(OWNER_BOT_PREFIX),
        custom_emoji_id=str(NEWS_FOOTER_SPARKLE_EMOJI_ID),
    )

    if body.startswith(OWNER_BOT_PREFIX):
        rest = body[len(OWNER_BOT_PREFIX) :].lstrip()
        plain, entities = _parse_markdown_bot_entities(rest)
        full = f"{OWNER_BOT_PREFIX} {plain}".rstrip() if plain else OWNER_BOT_PREFIX
        base = _utf16_len(f"{OWNER_BOT_PREFIX} ")
        out = [sparkle]
        for ent in entities:
            out.append(
                MessageEntity(
                    type=ent.type,
                    offset=ent.offset + base,
                    length=ent.length,
                )
            )
        return full, out

    plain, entities = _parse_markdown_bot_entities(body)
    full = f"{OWNER_BOT_PREFIX} {plain}"
    base = _utf16_len(f"{OWNER_BOT_PREFIX} ")
    out = [sparkle]
    for ent in entities:
        out.append(
            MessageEntity(
                type=ent.type,
                offset=ent.offset + base,
                length=ent.length,
            )
        )
    return full, out
