# -*- coding: utf-8 -*-
"""警告命令：/warn @用户 或 回复某人消息 /warn"""
from telegram import Update, User
from telegram.ext import ContextTypes
from telegram.constants import MessageEntityType, ParseMode

from config.settings import ALLOWED_CHAT_ID

WARN_MSG_TEMPLATE = "{target_name}，经【{reporter_name}】举报，现核实您已违反群规，即将被移出群聊 {target_mention}"

# 用户名缓存：(chat_id, username_lower) -> user_id，用于解析 @username
_username_cache: dict[tuple[int, str], int] = {}


def _update_username_cache(chat_id: int, user: User) -> None:
    """从消息中更新用户名缓存，供解析 @username 使用"""
    if user and user.username:
        key = (chat_id, user.username.lower())
        _username_cache[key] = user.id


def _user_full_name(user) -> str:
    """获取用户全名"""
    if not user:
        return "用户"
    name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    return name or "用户"


async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """警告某人：/warn @用户名 或 回复某人消息后 /warn"""
    if not update.message or not update.effective_chat:
        return
    chat_id = update.effective_chat.id
    if chat_id != ALLOWED_CHAT_ID:
        return
    if update.effective_chat.type not in ("group", "supergroup"):
        await update.message.reply_text("请在群组中使用此命令。")
        return

    reporter = update.effective_user
    reporter_name = _user_full_name(reporter)
    target_user = None

    # 方式1：回复某人的消息
    reply_to = update.message.reply_to_message
    if reply_to and reply_to.from_user:
        target_user = reply_to.from_user

    # 方式2：@提及
    if target_user is None and update.message.entities:
        for entity in update.message.entities:
            if entity.type == MessageEntityType.TEXT_MENTION and entity.user:
                target_user = entity.user
                break
            if entity.type == MessageEntityType.MENTION and update.message.text:
                # @username 格式：提取用户名，从缓存或管理员列表查找
                username = update.message.text[entity.offset : entity.offset + entity.length].lstrip("@")
                if username:
                    username_lower = username.lower()
                    # 1. 从缓存查找（该用户曾在群内发言过）
                    user_id = _username_cache.get((chat_id, username_lower))
                    if user_id:
                        try:
                            cm = await context.bot.get_chat_member(chat_id, user_id)
                            target_user = cm.user
                        except Exception:
                            pass
                    # 2. 从管理员列表查找
                    if target_user is None:
                        try:
                            admins = await context.bot.get_chat_administrators(chat_id)
                            for cm in admins:
                                u = cm.user
                                if u and u.username and u.username.lower() == username_lower:
                                    target_user = u
                                    break
                        except Exception:
                            pass
                break

    if target_user is None:
        await update.message.reply_text("用法：/warn @用户名  或  回复该用户的消息后发送 /warn")
        return

    if target_user.id == context.bot.id:
        await update.message.reply_text("无法警告机器人。")
        return

    target_name = _user_full_name(target_user)
    target_mention = target_user.mention_html(target_name)
    text = WARN_MSG_TEMPLATE.format(
        target_name=target_name, reporter_name=reporter_name, target_mention=target_mention
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)
