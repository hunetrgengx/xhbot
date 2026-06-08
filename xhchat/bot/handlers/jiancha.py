"""超管私聊 /jiancha：扫描 npwiki 访客可见门店交流群链接。"""
from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from html import escape
from typing import Any

from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import ContextTypes

from config.settings import NPWIKI_DB_PATH, NPWIKI_SUPER_ADMIN_IDS

logger = logging.getLogger(__name__)

_INTERNAL_BOT_UPLOAD_SHOP_NAME = "bot上传"
_STATUS_CLOSED_PERM = "closed_perm"
_STATUS_PENDING_OPEN = "pending_open"
_SCAN_INTERVAL_SEC = 2.0
_BATCH_SIZE = 20
_RATE_LIMIT_WAIT_SEC = 60
_MAX_MSG = 4000

_TYPE_LABELS = {
    1: "失效",
    2: "已改名",
    3: "频道",
    4: "无权访问",
}


def _attr_href(url: str) -> str:
    return (url or "").replace("&", "&amp;")


def _is_npwiki_super_admin(user_id: int) -> bool:
    if user_id in NPWIKI_SUPER_ADMIN_IDS:
        return True
    try:
        conn = sqlite3.connect(NPWIKI_DB_PATH, timeout=10)
        try:
            row = conn.execute(
                "SELECT 1 FROM admin WHERE user_id = ? AND is_super = 1",
                (user_id,),
            ).fetchone()
            return bool(row)
        finally:
            conn.close()
    except Exception:
        logger.warning("jiancha: 无法读取 npwiki 超管表", exc_info=True)
        return False


def _list_guest_visible_shops_with_group_binding() -> list[dict]:
    """访客公开池：营业中、歇业（排除永久闭店、待营业）且有交流群信息的门店。"""
    conn = sqlite3.connect(NPWIKI_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, name, link, channel_id
            FROM shop
            WHERE TRIM(COALESCE(name, '')) != ?
              AND TRIM(COALESCE(status, '')) NOT IN (?, ?)
              AND (
                    TRIM(COALESCE(link, '')) != ''
                 OR TRIM(COALESCE(channel_id, '')) != ''
              )
            ORDER BY name COLLATE NOCASE
            """,
            (_INTERNAL_BOT_UPLOAD_SHOP_NAME, _STATUS_CLOSED_PERM, _STATUS_PENDING_OPEN),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _is_public_username_ref(ref: str) -> bool:
    raw = (ref or "").strip()
    if not raw:
        return False
    if re.match(r"^@([a-zA-Z0-9_]{5,32})$", raw):
        return True
    return bool(
        re.match(
            r"^https?://(t\.me|telegram\.me)/([a-zA-Z0-9_]{5,32})(?:/|\?.*)?\s*$",
            raw,
            re.I,
        )
    )


def _chat_target_from_ref(ref: str) -> str | int | None:
    raw = (ref or "").strip()
    if not raw:
        return None
    m = re.match(r"^@([a-zA-Z0-9_]{5,32})$", raw)
    if m:
        return f"@{m.group(1)}"
    if re.match(r"^-100\d{8,}$", raw):
        return int(raw)
    m = re.match(
        r"^https?://(t\.me|telegram\.me)/([a-zA-Z0-9_]{5,32})(?:/|\?.*)?\s*$",
        raw,
        re.I,
    )
    if m:
        return f"@{m.group(2)}"
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    if re.match(r"^(t\.me|telegram\.me)/", raw, re.I):
        return raw if raw.startswith("http") else f"https://{raw}"
    return None


def _fail_kind_from_error(ref: str, exc: Exception) -> str:
    if isinstance(exc, Forbidden):
        return "no_access"
    if isinstance(exc, BadRequest):
        msg = str(exc).lower()
        if _is_public_username_ref(ref) and (
            "chat not found" in msg
            or "username not found" in msg
            or "username invalid" in msg
        ):
            return "not_found"
        return "no_access"
    return "no_access"


async def _try_get_chat(bot, ref: str) -> tuple[Any | None, str | None]:
    """返回 (chat, fail_kind)。fail_kind 为 not_found / no_access。"""
    target = _chat_target_from_ref(ref)
    if target is None:
        return None, "not_found"
    while True:
        try:
            return await bot.get_chat(target), None
        except RetryAfter:
            logger.info("jiancha: getChat 限流，等待 %ss 后重试", _RATE_LIMIT_WAIT_SEC)
            await asyncio.sleep(_RATE_LIMIT_WAIT_SEC)
        except TelegramError as e:
            if "too many requests" in str(e).lower():
                logger.info("jiancha: getChat 限流，等待 %ss 后重试", _RATE_LIMIT_WAIT_SEC)
                await asyncio.sleep(_RATE_LIMIT_WAIT_SEC)
                continue
            return None, _fail_kind_from_error(ref, e)
        except Exception:
            logger.debug("jiancha: get_chat failed for ref=%s", str(ref)[:80], exc_info=True)
            return None, "no_access"


async def _resolve_shop_chat(bot, shop: dict) -> tuple[Any | None, str | None]:
    link = (shop.get("link") or "").strip()
    channel_id = (shop.get("channel_id") or "").strip()
    last_fail: str | None = "not_found"

    if link:
        chat, fail = await _try_get_chat(bot, link)
        if chat is not None:
            return chat, None
        if fail:
            last_fail = fail

    if channel_id:
        chat, fail = await _try_get_chat(bot, channel_id)
        if chat is not None:
            return chat, None
        if fail == "no_access":
            last_fail = "no_access"
        elif fail == "not_found" and last_fail != "no_access":
            last_fail = "not_found"

    return None, last_fail


def _classify_shop_issue(shop_name: str, chat: Any | None, fail_kind: str | None) -> int | None:
    if chat is None:
        if fail_kind == "no_access":
            return 4
        return 1
    ctype = getattr(chat, "type", None) or ""
    if ctype == "channel":
        return 3
    title = (getattr(chat, "title", None) or "").strip()
    name = (shop_name or "").strip()
    if name and title and name.lower() not in title.lower():
        return 2
    return None


def _shop_display_link(shop: dict) -> str:
    link = (shop.get("link") or "").strip()
    if link:
        return link
    return (shop.get("channel_id") or "").strip()


def _format_issue_line(seq: int, shop: dict, issue_type: int) -> str:
    name = (shop.get("name") or "").strip() or "未命名"
    href = _shop_display_link(shop)
    label = _TYPE_LABELS.get(issue_type, str(issue_type))
    if href:
        body = f'<a href="{_attr_href(href)}">{escape(name)}</a>'
    else:
        body = escape(name)
    return f"{seq}. {body} — {label}"


def _split_message_lines(lines: list[str]) -> list[str]:
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in lines:
        add = len(line) + (1 if buf else 0)
        if buf and size + add > _MAX_MSG:
            chunks.append("\n".join(buf))
            buf = [line]
            size = len(line)
        else:
            buf.append(line)
            size += add
    if buf:
        chunks.append("\n".join(buf))
    return chunks


async def _send_html_chunks(message, lines: list[str]) -> None:
    for i, text in enumerate(_split_message_lines(lines)):
        if i > 0:
            text = f"（续 {i + 1}）\n\n{text}"
        await message.reply_text(text, parse_mode=ParseMode.HTML)


async def _send_batch_progress(
    message,
    start_idx: int,
    end_idx: int,
    batch_issues: list[tuple[dict, int]],
) -> None:
    header = f"📋 已扫描 {start_idx}–{end_idx}"
    if not batch_issues:
        await message.reply_text(f"{header}，均正常。", parse_mode=ParseMode.HTML)
        return
    body = [header + "，不成功的有："]
    body.extend(
        _format_issue_line(i, shop, t)
        for i, (shop, t) in enumerate(batch_issues, start=1)
    )
    await _send_html_chunks(message, body)


async def _send_final_report(
    message,
    issues: list[tuple[dict, int]],
    scanned: int,
) -> None:
    if not issues:
        await message.reply_text(
            f"✅ <b>扫描完成</b>\n\n"
            f"共扫描 {scanned} 个访客可见门店，未发现问题。",
            parse_mode=ParseMode.HTML,
        )
        return
    header = (
        f"✅ <b>扫描完成</b>\n\n"
        f"共扫描 {scanned} 个访客可见门店，"
        f"累计 {len(issues)} 个问题：\n"
    )
    body = [header]
    body.extend(
        _format_issue_line(i, shop, t)
        for i, (shop, t) in enumerate(issues, start=1)
    )
    await _send_html_chunks(message, body)


async def cmd_jiancha(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """扫描 npwiki 访客可见门店交流群：仅超管私聊；不在命令菜单中列出。"""
    if not update.message or update.effective_chat.type != ChatType.PRIVATE:
        return
    uid = update.effective_user.id if update.effective_user else None
    if not uid or not _is_npwiki_super_admin(uid):
        return

    try:
        shops = await asyncio.to_thread(_list_guest_visible_shops_with_group_binding)
    except Exception:
        logger.exception("jiancha: 读取 npwiki 门店失败")
        await update.message.reply_text("❌ 无法读取 npwiki 门店数据库，请检查 NPWIKI_DB_PATH 配置。")
        return

    total = len(shops)
    await update.message.reply_text(
        f"🔍 开始扫描，共 {total} 个访客可见门店（每 {_BATCH_SIZE} 家汇报一次）…",
        parse_mode=ParseMode.HTML,
    )

    all_issues: list[tuple[dict, int]] = []
    batch_issues: list[tuple[dict, int]] = []

    for i, shop in enumerate(shops, start=1):
        chat, fail_kind = await _resolve_shop_chat(context.bot, shop)
        issue_type = _classify_shop_issue(shop.get("name") or "", chat, fail_kind)
        if issue_type is not None:
            item = (shop, issue_type)
            all_issues.append(item)
            batch_issues.append(item)

        if i % _BATCH_SIZE == 0 or i == total:
            batch_start = ((i - 1) // _BATCH_SIZE) * _BATCH_SIZE + 1
            await _send_batch_progress(update.message, batch_start, i, batch_issues)
            batch_issues = []

        if i < total:
            await asyncio.sleep(_SCAN_INTERVAL_SEC)

    await _send_final_report(update.message, all_issues, total)
