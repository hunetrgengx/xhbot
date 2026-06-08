"""超管私聊 /jiancha：扫描 npwiki 库内门店交流群链接，检出失效、已改名、频道。"""
from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from html import escape

from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from config.settings import NPWIKI_DB_PATH, NPWIKI_SUPER_ADMIN_IDS

logger = logging.getLogger(__name__)

_INTERNAL_BOT_UPLOAD_SHOP_NAME = "bot上传"
_TYPE_LABELS = {1: "失效", 2: "已改名", 3: "频道"}
_MAX_MSG = 4000


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


def _list_shops_with_group_binding() -> list[dict]:
    conn = sqlite3.connect(NPWIKI_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, name, link, channel_id
            FROM shop
            WHERE TRIM(COALESCE(name, '')) != ?
              AND (
                    TRIM(COALESCE(link, '')) != ''
                 OR TRIM(COALESCE(channel_id, '')) != ''
              )
            ORDER BY name COLLATE NOCASE
            """,
            (_INTERNAL_BOT_UPLOAD_SHOP_NAME,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


async def _try_get_chat(bot, ref: str):
    raw = (ref or "").strip()
    if not raw:
        return None
    try:
        m = re.match(r"^@([a-zA-Z0-9_]{5,32})$", raw)
        if m:
            return await bot.get_chat(f"@{m.group(1)}")
        if re.match(r"^-100\d{8,}$", raw):
            return await bot.get_chat(int(raw))
        m = re.match(
            r"^https?://(t\.me|telegram\.me)/([a-zA-Z0-9_]{5,32})(?:/|\?.*)?\s*$",
            raw,
            re.I,
        )
        if m:
            return await bot.get_chat(f"@{m.group(2)}")
        if raw.startswith("http://") or raw.startswith("https://"):
            return await bot.get_chat(raw)
        if re.match(r"^(t\.me|telegram\.me)/", raw, re.I):
            return await bot.get_chat(f"https://{raw}" if not raw.startswith("http") else raw)
    except TelegramError:
        return None
    except Exception:
        logger.debug("jiancha: get_chat failed for ref=%s", raw[:80], exc_info=True)
        return None
    return None


async def _resolve_shop_chat(bot, shop: dict):
    link = (shop.get("link") or "").strip()
    channel_id = (shop.get("channel_id") or "").strip()
    if link:
        chat = await _try_get_chat(bot, link)
        if chat is not None:
            return chat
    if channel_id:
        return await _try_get_chat(bot, channel_id)
    return None


def _classify_shop_issue(shop_name: str, chat) -> int | None:
    if chat is None:
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


async def _reply_jiancha_report(message, issues: list[tuple[dict, int]], scanned: int) -> None:
    if not issues:
        await message.reply_text(
            f"🔍 <b>门店链接检查完成</b>\n\n"
            f"共扫描 {scanned} 个门店，未发现问题。",
            parse_mode=ParseMode.HTML,
        )
        return

    header = (
        f"🔍 <b>门店链接检查结果</b>\n\n"
        f"共扫描 {scanned} 个门店，发现 {len(issues)} 个问题：\n"
    )
    body_lines = [_format_issue_line(i, shop, t) for i, (shop, t) in enumerate(issues, start=1)]
    chunks = _split_message_lines([header, *body_lines])
    for i, text in enumerate(chunks):
        if i > 0:
            text = f"（续 {i + 1}/{len(chunks)}）\n\n{text}"
        await message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_jiancha(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """扫描 npwiki 门店交流群链接：仅超管私聊；不在命令菜单中列出。"""
    if not update.message or update.effective_chat.type != ChatType.PRIVATE:
        return
    uid = update.effective_user.id if update.effective_user else None
    if not uid or not _is_npwiki_super_admin(uid):
        return

    status_msg = await update.message.reply_text("🔍 正在扫描门店链接，请稍候…")
    try:
        shops = await asyncio.to_thread(_list_shops_with_group_binding)
    except Exception:
        logger.exception("jiancha: 读取 npwiki 门店失败")
        try:
            await status_msg.delete()
        except Exception:
            pass
        await update.message.reply_text("❌ 无法读取 npwiki 门店数据库，请检查 NPWIKI_DB_PATH 配置。")
        return

    issues: list[tuple[dict, int]] = []
    for shop in shops:
        chat = await _resolve_shop_chat(context.bot, shop)
        issue_type = _classify_shop_issue(shop.get("name") or "", chat)
        if issue_type is not None:
            issues.append((shop, issue_type))
        await asyncio.sleep(0.05)

    try:
        await status_msg.delete()
    except Exception:
        pass
    await _reply_jiancha_report(update.message, issues, len(shops))
