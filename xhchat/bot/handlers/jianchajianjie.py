"""超管私聊 /jianchajianjie：扫描访客可见人物简介中的 Telegram 消息链接。"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import sqlite3
from html import escape
from typing import Any

from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import ContextTypes

from bot.handlers.jiancha import (
    _BATCH_SIZE,
    _MAX_MSG,
    _RATE_LIMIT_WAIT_SEC,
    _SCAN_INTERVAL_SEC,
    _attr_href,
    _is_npwiki_super_admin,
    _send_html_chunks,
)
from config.settings import NPWIKI_DB_PATH

logger = logging.getLogger(__name__)

_STATUS_CLOSED_PERM = "closed_perm"
_STATUS_PENDING_OPEN = "pending_open"
_HREF_RE = re.compile(r"""<a\s+[^>]*href\s*=\s*["']([^"']+)["']""", re.I)
_LINK_PRIVATE_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/c/(\d+)/(\d+)", re.I)
_LINK_PUBLIC_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/([a-zA-Z0-9_]{4,64})/(\d+)",
    re.I,
)

_ISSUE_DELETED = "deleted"
_ISSUE_NO_ACCESS = "no_access"
_ISSUE_LABELS = {
    _ISSUE_DELETED: "消息已删除",
    _ISSUE_NO_ACCESS: "无权访问",
}


def _list_guest_visible_person_intro_links() -> list[dict]:
    """访客可见人物 + 简介内标准 <a href> 可解析的 Telegram 消息链（扁平为逐链接任务）。"""
    vis_sql = f"""
        (
            EXISTS (
                SELECT 1 FROM kw_profile_shop ps_pub
                INNER JOIN shop s_pub ON s_pub.id = ps_pub.shop_id
                WHERE ps_pub.kw_profile_id = p.id
                  AND COALESCE(ps_pub.listed, 1) = 1
                  AND TRIM(COALESCE(s_pub.status, '')) NOT IN (?, ?)
            )
            OR (
                NOT EXISTS (
                    SELECT 1 FROM kw_profile_shop ps0 WHERE ps0.kw_profile_id = p.id
                )
                AND EXISTS (
                    SELECT 1 FROM record r0
                    INNER JOIN shop s0 ON s0.id = r0.shop_id
                    WHERE r0.kw_profile_id = p.id
                      AND TRIM(COALESCE(s0.status, '')) NOT IN (?, ?)
                )
            )
        )
    """
    conn = sqlite3.connect(NPWIKI_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"""
            SELECT
                p.id AS person_id,
                TRIM(COALESCE(p.name, '')) AS person_name,
                TRIM(COALESCE(p.intro, '')) AS intro,
                COALESCE(
                    (
                        SELECT s.name FROM kw_profile_shop ps
                        INNER JOIN shop s ON s.id = ps.shop_id
                        WHERE ps.kw_profile_id = p.id
                          AND COALESCE(ps.listed, 1) = 1
                          AND TRIM(COALESCE(s.status, '')) NOT IN (?, ?)
                        ORDER BY s.name COLLATE NOCASE
                        LIMIT 1
                    ),
                    (
                        SELECT s.name FROM record r
                        INNER JOIN shop s ON s.id = r.shop_id
                        WHERE r.kw_profile_id = p.id
                          AND TRIM(COALESCE(s.status, '')) NOT IN (?, ?)
                        ORDER BY s.name COLLATE NOCASE
                        LIMIT 1
                    ),
                    '未知店'
                ) AS shop_name
            FROM person p
            WHERE TRIM(COALESCE(p.intro, '')) != ''
              AND {vis_sql}
            ORDER BY shop_name COLLATE NOCASE, person_name COLLATE NOCASE
            """,
            (
                _STATUS_CLOSED_PERM,
                _STATUS_PENDING_OPEN,
                _STATUS_CLOSED_PERM,
                _STATUS_PENDING_OPEN,
                _STATUS_CLOSED_PERM,
                _STATUS_PENDING_OPEN,
                _STATUS_CLOSED_PERM,
                _STATUS_PENDING_OPEN,
            ),
        ).fetchall()
    finally:
        conn.close()

    tasks: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        person_name = (row["person_name"] or "").strip() or "未命名"
        shop_name = (row["shop_name"] or "").strip() or "未知店"
        label = f"{shop_name}-{person_name}"
        for url in _extract_intro_hrefs(row["intro"] or ""):
            if not _is_telegram_message_link(url):
                continue
            key = (label, url, str(row["person_id"]))
            if key in seen:
                continue
            seen.add(key)
            tasks.append(
                {
                    "person_id": row["person_id"],
                    "label": label,
                    "url": url,
                }
            )
    return tasks


def _extract_intro_hrefs(intro_html: str) -> list[str]:
    urls: list[str] = []
    for m in _HREF_RE.finditer(intro_html or ""):
        raw = html.unescape((m.group(1) or "").strip())
        if raw:
            urls.append(raw)
    return urls


def _is_telegram_message_link(url: str) -> bool:
    return _parse_message_link(url) is not None


def _parse_message_link(url: str) -> tuple[Any, int] | None:
    raw = (url or "").strip()
    if not raw:
        return None
    m = _LINK_PRIVATE_RE.search(raw)
    if m:
        try:
            return int(f"-100{m.group(1)}"), int(m.group(2))
        except ValueError:
            return None
    m = _LINK_PUBLIC_RE.search(raw)
    if m and m.group(1).lower() != "c":
        try:
            return f"@{m.group(1)}", int(m.group(2))
        except ValueError:
            return None
    return None


def _issue_from_bad_request(exc: BadRequest) -> str:
    msg = str(exc).lower()
    if any(
        token in msg
        for token in (
            "message to copy not found",
            "message_id_invalid",
            "message not found",
            "message can't be copied",
            "message to forward not found",
        )
    ):
        return _ISSUE_DELETED
    return _ISSUE_NO_ACCESS


async def _resolve_from_chat_id(bot, from_chat: Any) -> int | None:
    if isinstance(from_chat, int):
        return from_chat
    while True:
        try:
            chat = await bot.get_chat(from_chat)
            return int(chat.id)
        except RetryAfter:
            logger.info("jianchajianjie: getChat 限流，等待 %ss 后重试", _RATE_LIMIT_WAIT_SEC)
            await asyncio.sleep(_RATE_LIMIT_WAIT_SEC)
        except TelegramError as e:
            if "too many requests" in str(e).lower():
                logger.info("jianchajianjie: getChat 限流，等待 %ss 后重试", _RATE_LIMIT_WAIT_SEC)
                await asyncio.sleep(_RATE_LIMIT_WAIT_SEC)
                continue
            return None
        except Exception:
            logger.debug("jianchajianjie: resolve chat failed", exc_info=True)
            return None


async def _probe_message_link(bot, probe_chat_id: int, url: str) -> str | None:
    """检测消息链；正常返回 None，异常返回 deleted / no_access。"""
    parsed = _parse_message_link(url)
    if not parsed:
        return None
    from_chat, message_id = parsed
    chat_id = await _resolve_from_chat_id(bot, from_chat)
    if chat_id is None:
        return _ISSUE_NO_ACCESS

    while True:
        try:
            copied = await bot.copy_message(
                chat_id=probe_chat_id,
                from_chat_id=chat_id,
                message_id=message_id,
                disable_notification=True,
            )
            try:
                await bot.delete_message(chat_id=probe_chat_id, message_id=copied.message_id)
            except Exception:
                pass
            return None
        except RetryAfter:
            logger.info("jianchajianjie: copyMessage 限流，等待 %ss 后重试", _RATE_LIMIT_WAIT_SEC)
            await asyncio.sleep(_RATE_LIMIT_WAIT_SEC)
        except Forbidden:
            return _ISSUE_NO_ACCESS
        except BadRequest as e:
            return _issue_from_bad_request(e)
        except TelegramError as e:
            if "too many requests" in str(e).lower():
                logger.info("jianchajianjie: copyMessage 限流，等待 %ss 后重试", _RATE_LIMIT_WAIT_SEC)
                await asyncio.sleep(_RATE_LIMIT_WAIT_SEC)
                continue
            return _ISSUE_NO_ACCESS
        except Exception:
            logger.debug("jianchajianjie: probe failed url=%s", url[:120], exc_info=True)
            return _ISSUE_NO_ACCESS


def _format_issue_line(seq: int, item: dict, issue: str) -> str:
    label = escape(item["label"])
    url = (item["url"] or "").strip()
    issue_label = _ISSUE_LABELS.get(issue, issue)
    return (
        f'{seq}、<a href="{_attr_href(url)}">{label}</a>'
        f"（{escape(url)}）  -{escape(issue_label)}"
    )


async def _send_batch_progress(
    message,
    start_idx: int,
    end_idx: int,
    batch_issues: list[tuple[dict, str]],
) -> None:
    header = f"📋 已扫描链接 {start_idx}–{end_idx}"
    if not batch_issues:
        await message.reply_text(f"{header}，均正常。", parse_mode=ParseMode.HTML)
        return
    body = [header + "，异常链接："]
    body.extend(
        _format_issue_line(i, item, issue)
        for i, (item, issue) in enumerate(batch_issues, start=1)
    )
    await _send_html_chunks(message, body)


async def _send_final_report(
    message,
    issues: list[tuple[dict, str]],
    scanned: int,
) -> None:
    if not issues:
        await message.reply_text(
            f"✅ <b>扫描完成</b>\n\n"
            f"共检测 {scanned} 条简介消息链接，未发现问题。",
            parse_mode=ParseMode.HTML,
        )
        return
    header = (
        f"✅ <b>扫描完成</b>\n\n"
        f"共检测 {scanned} 条简介消息链接，"
        f"累计 {len(issues)} 条异常：\n"
    )
    body = [header]
    body.extend(
        _format_issue_line(i, item, issue)
        for i, (item, issue) in enumerate(issues, start=1)
    )
    await _send_html_chunks(message, body)


async def cmd_jianchajianjie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """扫描访客可见人物简介内 Telegram 消息链：仅超管私聊；不在命令菜单中列出。"""
    if not update.message or update.effective_chat.type != ChatType.PRIVATE:
        return
    uid = update.effective_user.id if update.effective_user else None
    if not uid or not _is_npwiki_super_admin(uid):
        return

    try:
        links = await asyncio.to_thread(_list_guest_visible_person_intro_links)
    except Exception:
        logger.exception("jianchajianjie: 读取 npwiki 人物简介失败")
        await update.message.reply_text("❌ 无法读取 npwiki 数据库，请检查 NPWIKI_DB_PATH 配置。")
        return

    total = len(links)
    if total == 0:
        await update.message.reply_text("🔍 未找到访客可见人物简介中的 Telegram 消息链接。")
        return

    await update.message.reply_text(
        f"🔍 开始扫描简介链接，共 {total} 条（每 {_BATCH_SIZE} 条汇报一次）…",
        parse_mode=ParseMode.HTML,
    )

    probe_chat_id = update.effective_chat.id
    all_issues: list[tuple[dict, str]] = []
    batch_issues: list[tuple[dict, str]] = []

    for i, item in enumerate(links, start=1):
        issue = await _probe_message_link(context.bot, probe_chat_id, item["url"])
        if issue is not None:
            pair = (item, issue)
            all_issues.append(pair)
            batch_issues.append(pair)

        if i % _BATCH_SIZE == 0 or i == total:
            batch_start = ((i - 1) // _BATCH_SIZE) * _BATCH_SIZE + 1
            await _send_batch_progress(update.message, batch_start, i, batch_issues)
            batch_issues = []

        if i < total:
            await asyncio.sleep(_SCAN_INTERVAL_SEC)

    await _send_final_report(update.message, all_issues, total)
