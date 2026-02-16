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
        "/help - 查看全部命令\n"
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


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """显示帮助信息"""
    text = (
        "📖 小助理 命令帮助\n\n"
        "【对话】\n"
        "/newchat — 清除对话历史\n"
        "/settings — 查看/切换模型、设定\n"
        "/web_search — 联网搜索开关（私聊）\n\n"
        "【配置】（私聊/群组）\n"
        "/set_model — 切换模型\n"
        "/set_prompt — 设置自定义设定\n"
        "/reset_prompt — 重置设定\n"
        "/reset_model — 重置模型\n"
        "/cancel — 取消当前操作\n\n"
        "【贴纸】\n"
        "/tz — 贴纸管理（添加/删除）\n"
        "/getsticker — 获取贴纸 file_id（私聊）\n\n"
        "【管理】（指定群组）\n"
        "/xhadd — 添加设定\n"
        "/xhdel — 删除设定\n"
        "/xhset — 显示设定列表\n"
        "/warn — 警告用户"
    )
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
