"""Bot 入口"""
import logging
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from config.settings import AI_PROVIDER, OPENAI_BASE_URL, TELEGRAM_BOT_TOKEN
from telegram.ext import CallbackQueryHandler

from bot.models.database import init_db
from bot.handlers.start import cmd_start, cmd_newchat
from bot.handlers.chat import handle_message, handle_sticker_reply_to_bot
from bot.handlers.admin import (
    cmd_settings,
    cmd_set_model,
    cmd_set_prompt,
    cmd_cancel,
    cmd_reset_prompt,
    cmd_reset_model,
    cmd_getsticker,
    cmd_tiezhi,
    callback_set_model,
)
from bot.handlers.xh import cmd_xhadd, cmd_xhdel, cmd_xhset
from bot.handlers.warn import cmd_warn
from bot.handlers.warm import track_admin_activity
from bot.services.warm_scheduler import run_warm_scheduler
from config.settings import ALLOWED_CHAT_ID

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def main():
    if not TELEGRAM_BOT_TOKEN:
        env_path = Path(__file__).resolve().parent.parent / ".env"
        hint = ""
        if not env_path.exists():
            hint = f"\n提示：请复制 .env.example 为 .env，并填入 TELEGRAM_BOT_TOKEN。\n.env 文件应位于：{env_path}"
        else:
            hint = "\n提示：请检查 .env 中是否有 TELEGRAM_BOT_TOKEN=你的token（等号两边不要有空格）"
        raise ValueError(f"未找到 TELEGRAM_BOT_TOKEN{hint}")

    init_db()

    # job_queue(None) 避免 PTB 与 APScheduler 导致的 ExtBot 初始化错误
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).job_queue(None).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("newchat", cmd_newchat))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("set_model", cmd_set_model))
    app.add_handler(CommandHandler("set_prompt", cmd_set_prompt))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("reset_prompt", cmd_reset_prompt))
    app.add_handler(CommandHandler("reset_model", cmd_reset_model))
    app.add_handler(CommandHandler("xhadd", cmd_xhadd))
    app.add_handler(CommandHandler("xhdel", cmd_xhdel))
    app.add_handler(CommandHandler("xhset", cmd_xhset))
    app.add_handler(CommandHandler("getsticker", cmd_getsticker))
    app.add_handler(CommandHandler("tz", cmd_tiezhi))
    app.add_handler(CommandHandler("warn", cmd_warn))
    app.add_handler(CallbackQueryHandler(callback_set_model, pattern="^model:"))
    # 暖群：group -1 先执行，监听管理员发言
    app.add_handler(
        MessageHandler(
            filters.Chat(chat_id=ALLOWED_CHAT_ID) & filters.ChatType.GROUPS,
            track_admin_activity,
        ),
        group=-1,
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    # 群组中回复机器人并发贴纸：用配置的贴纸回复
    app.add_handler(
        MessageHandler(
            filters.Chat(chat_id=ALLOWED_CHAT_ID) & filters.ChatType.GROUPS & filters.Sticker.ALL,
            handle_sticker_reply_to_bot,
        ),
    )
    # 贴纸：私聊中发送贴纸则返回 file_id（仅所有者）
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.Sticker.ALL,
            cmd_getsticker,
        ),
    )

    run_warm_scheduler()  # 始终启动（延迟删除 + 暖群）

    logger.info("Bot 启动中... (AI: %s, API: %s)", AI_PROVIDER, OPENAI_BASE_URL)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
