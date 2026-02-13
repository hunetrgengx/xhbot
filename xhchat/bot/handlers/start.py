"""命令处理：/start, /newchat 等"""
from telegram import Update
from telegram.ext import ContextTypes

from bot.models.database import clear_context


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /start 命令"""
    if context.chat_data.pop("awaiting_tiezhi", None):
        prefix = "已退出贴纸管理。\n\n"
    else:
        prefix = ""
    text = prefix + (
        "👋 小助理\n\n"
        "【对话】\n"
        "• @提及我 或 以「小助理，」开头 提问\n"
        "• 回复我的消息继续对话\n"
        "• 回复我并发送贴纸，我会用贴纸回复\n\n"
        "【命令】\n"
        "/newchat - 清除对话历史\n"
        "/settings - 查看/切换模型、设定\n\n"
        "【贴纸池】（私聊，仅管理员）\n"
        "/tz - 发送贴纸切换添加/删除，/cancel 或 /start 退出"
    )
    if update.effective_chat.type == "private":
        await update.message.reply_text(
            "本机器人仅在群组中使用。请将机器人加入群组后：\n\n" + text
        )
    else:
        await update.message.reply_text(text)


async def cmd_newchat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """清除当前对话的历史上下文"""
    if update.effective_chat.type == "private":
        await update.message.reply_text("本机器人仅在群组中使用。")
        return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    clear_context(chat_id, user_id)
    await update.message.reply_text("✅ 已开始新对话，之前的聊天记录已清除。")
