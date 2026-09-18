#!/usr/bin/env python3
"""Таблицы, длинные статьи (Telegraph) и форматирование для Telegram."""
from __future__ import annotations

import html
import re
from typing import Any

TELEGRAPH_DIRECTIVE_RE = re.compile(
    r"\[\[telegraph:(?P<title>[^\]|]+)(?:\|(?P<body>[^\]]+))?\]\]",
    re.I,
)

TABLE_BLOCK_RE = re.compile(
    r"(?:^|\n)"
    r"((?:\|[^\n]+\|\s*\n)+)"
    r"(?=\n\n|\n[^|]|$)",
    re.M,
)


def _is_table_row(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.endswith("|") and s.count("|") >= 2


def _is_separator_row(line: str) -> bool:
    s = line.strip().strip("|").strip()
    if not s:
        return False
    return bool(re.fullmatch(r"[\s:\-|]+", s))


def markdown_table_to_pre(block: str) -> str:
    """Markdown-таблица → <pre> для Telegram HTML."""
    lines = [ln.rstrip() for ln in block.strip().splitlines() if _is_table_row(ln)]
    if len(lines) < 2:
        return block
    if not any(_is_separator_row(ln) for ln in lines[1:3]):
        return block
    body = "\n".join(lines)
    return f"<pre>{html.escape(body)}</pre>"


def enhance_tables_for_telegram(text: str) -> str:
    """Конвертирует markdown-таблицы в <pre>-блоки (если ещё не HTML)."""
    if not text or "<pre>" in text:
        return text

    def _sub(m: re.Match) -> str:
        block = m.group(1)
        if not any(_is_separator_row(ln) for ln in block.splitlines()):
            return m.group(0)
        return "\n" + markdown_table_to_pre(block) + "\n"

    return TABLE_BLOCK_RE.sub(_sub, text)


def strip_telegraph_directives(text: str) -> tuple[str, list[dict[str, str]]]:
    found: list[dict[str, str]] = []

    def _sub(m: re.Match) -> str:
        found.append(
            {
                "title": (m.group("title") or "Статья").strip(),
                "body": (m.group("body") or "").strip(),
            }
        )
        return ""

    cleaned = TELEGRAPH_DIRECTIVE_RE.sub(_sub, text or "")
    return cleaned.strip(), found


def publish_telegraph(title: str, content: str) -> str | None:
    """Публикует статью на Telegraph, возвращает URL или None."""
    title = (title or "Статья").strip()[:256]
    body = (content or "").strip()
    if not body:
        return None
    try:
        from telegraph import Telegraph

        tg = Telegraph()
        tg.create_account(short_name="Hoshi")
        html_body = _markdown_to_telegraph_html(body)
        page = tg.create_page(title=title, html_content=html_body)
        return page.get("url")
    except Exception:
        return None


def _markdown_to_telegraph_html(text: str) -> str:
    """Простой markdown → HTML для Telegraph."""
    lines = text.splitlines()
    out: list[str] = []
    in_pre = False
    for raw in lines:
        line = raw.rstrip()
        if _is_table_row(line):
            if not in_pre:
                out.append("<pre>")
                in_pre = True
            out.append(html.escape(line))
            continue
        if in_pre:
            out.append("</pre>")
            in_pre = False
        if line.startswith("### "):
            out.append(f"<h4>{html.escape(line[4:].strip())}</h4>")
        elif line.startswith("## "):
            out.append(f"<h3>{html.escape(line[3:].strip())}</h3>")
        elif line.startswith("# "):
            out.append(f"<h3>{html.escape(line[2:].strip())}</h3>")
        elif re.match(r"^[-*]\s+", line):
            out.append(f"<p>• {html.escape(re.sub(r'^[-*]\s+', '', line))}</p>")
        elif line.strip():
            chunk = line
            chunk = re.sub(
                r"\*\*([^*]+)\*\*",
                lambda m: f"<strong>{html.escape(m.group(1))}</strong>",
                chunk,
            )
            chunk = re.sub(
                r"`([^`]+)`",
                lambda m: f"<code>{html.escape(m.group(1))}</code>",
                chunk,
            )
            if "<" not in chunk:
                chunk = html.escape(chunk)
            out.append(f"<p>{chunk}</p>")
        else:
            out.append("<p></p>")
    if in_pre:
        out.append("</pre>")
    return "".join(out) if out else f"<p>{html.escape(text)}</p>"


def process_rich_content(text: str) -> str:
    """Таблицы + опциональные Telegraph-директивы в ответе агента."""
    if not text:
        return text
    cleaned, directives = strip_telegraph_directives(text)
    result = enhance_tables_for_telegram(cleaned)
    extras: list[str] = []
    for spec in directives:
        title = spec.get("title") or "Статья"
        body = spec.get("body") or cleaned
        url = publish_telegraph(title, body)
        if url:
            extras.append(f"📄 **{title}:** {url}")
    if extras:
        result = (result.rstrip() + "\n\n" + "\n".join(extras)).strip()
    return result
