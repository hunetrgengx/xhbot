#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bytecler PTB - 使用 python-telegram-bot 监听封禁事件

功能：监听群成员封禁/解封事件，记录封禁日志
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# 路径：Windows 用绝对路径，Ubuntu 用相对路径
_BYTECLER_DIR = Path(__file__).resolve().parent if sys.platform == "win32" else None

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from telegram import Update, ChatMemberBanned, ChatMemberRestricted, ChatMemberLeft
from telegram.ext import Application, ChatMemberHandler, ContextTypes

# 配置
PTB_BOT_TOKEN = os.getenv("PTB_BOT_TOKEN", os.getenv("BOT_TOKEN", ""))  # 如果没配置 PTB_BOT_TOKEN，使用 BOT_TOKEN
GROUP_ID_STR = os.getenv("GROUP_ID", "")
TARGET_GROUP_IDS = {s.strip() for s in GROUP_ID_STR.split(",") if s.strip()}
RESTRICTED_USERS_LOG_PATH = (_BYTECLER_DIR / "restricted_users.jsonl") if _BYTECLER_DIR else Path("restricted_users.jsonl")  # 封禁日志


def _chat_allowed(chat_id: str) -> bool:
    """检查是否在监控的群列表中"""
    return bool(TARGET_GROUP_IDS and str(chat_id) in TARGET_GROUP_IDS)


def _log_restriction(chat_id: str, user_id: int, full_name: str, action: str, until_date=None):
    """记录封禁/解封日志"""
    try:
        record = {
            "time": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "chat_id": chat_id,
            "user_id": user_id,
            "full_name": full_name or "",
            "action": action,  # "banned", "restricted", "unbanned", "kicked"
            "until_date": until_date.strftime("%Y-%m-%dT%H:%M:%SZ") if until_date else None,
        }
        with open(RESTRICTED_USERS_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"[PTB 封禁事件] 群{chat_id} | 用户{user_id} ({full_name}) | {action} | 到期: {until_date}")
    except Exception as e:
        print(f"[记录封禁日志失败] {e}")


async def chat_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理群成员状态变化"""
    if not update.chat_member:
        return
    
    member_update = update.chat_member
    chat_id = str(member_update.chat.id)
    
    # 只处理监控的群
    if not _chat_allowed(chat_id):
        return
    
    old_member = member_update.old_chat_member
    new_member = member_update.new_chat_member
    user = new_member.user
    
    user_id = user.id
    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or (user.username or f"用户{user_id}")
    
    # 检查是否被封禁
    if isinstance(new_member, ChatMemberBanned):
        until_date = new_member.until_date
        _log_restriction(chat_id, user_id, full_name, "banned", until_date)
    
    # 检查是否被限制（restricted）
    elif isinstance(new_member, ChatMemberRestricted):
        until_date = new_member.until_date
        _log_restriction(chat_id, user_id, full_name, "restricted", until_date)
    
    # 检查是否被踢出
    elif isinstance(new_member, ChatMemberLeft):
        # 如果之前不是 left 状态，说明是被踢出的
        if old_member.status not in ('left', 'kicked'):
            _log_restriction(chat_id, user_id, full_name, "kicked", None)
    
    # 检查是否解封（从 banned/restricted 变为 member）
    elif new_member.status == 'member':
        if old_member.status in ('kicked', 'restricted'):
            _log_restriction(chat_id, user_id, full_name, "unbanned", None)


async def main():
    """启动 PTB 机器人"""
    if not PTB_BOT_TOKEN:
        print("请配置 PTB_BOT_TOKEN 或 BOT_TOKEN")
        return
    
    print(f"PTB 机器人启动中...")
    print(f"监控群: {TARGET_GROUP_IDS}")
    
    # 创建应用
    application = Application.builder().token(PTB_BOT_TOKEN).build()
    
    # 注册封禁事件处理器
    application.add_handler(ChatMemberHandler(chat_member_handler))
    
    # 启动机器人
    print("PTB 机器人已启动，监听封禁事件...")
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    
    # 保持运行
    try:
        await asyncio.Event().wait()  # 永久等待
    except KeyboardInterrupt:
        print("\nPTB 机器人正在停止...")
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
