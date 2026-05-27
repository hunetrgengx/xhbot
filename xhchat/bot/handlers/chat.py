"""群聊/私聊消息处理 - @提及触发"""
import logging
import random
import re
from telegram import Update

logger = logging.getLogger(__name__)
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

from config.settings import AI_PROVIDER, ALLOWED_CHAT_IDS
from bot.services.sticker_service import get_sticker_ids
from bot.services.ai_service import chat_completion
from bot.services.context_manager import (
    build_messages_for_ai,
    save_exchange,
    rate_limiter,
)


from bot.services.text_utils import replace_emoji_digits


def should_respond(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[bool, str]:
    """
    判断是否应该回复，以及提取用户的实际问题
    返回 (是否回复, 提取后的文本)
    """
    message = update.message
    if not message or not message.text:
        return False, ""

    text = message.text.strip()
    if not text:
        return False, ""

    bot_username = context.bot.username
    chat_type = update.effective_chat.type

    # 私聊不支持，仅在群组中可用
    if chat_type == "private":
        return False, ""

    # 仅允许在指定群组使用
    if update.effective_chat.id not in ALLOWED_CHAT_IDS:
        return False, ""

    # 触发方式1：以「小助理，」开头
    if text.startswith("小助理，"):
        query = text[4:].strip()  # 移除「小助理，」4 个字符
        return True, query or "你好，有什么可以帮你的？"

    # 群聊/超级群组：需要 @提及 或 回复机器人的消息
    if chat_type in ("group", "supergroup"):
        # 检查是否 @提及 了机器人
        if bot_username and f"@{bot_username}".lower() in text.lower():
            # 移除 @机器人 部分（不区分大小写）
            query = re.sub(rf"@{re.escape(bot_username)}\s*", "", text, flags=re.IGNORECASE).strip()
            return True, query or "你好，有什么可以帮你的？"

        # 检查是否是回复机器人的消息
        if message.reply_to_message and message.reply_to_message.from_user:
            if message.reply_to_message.from_user.id == context.bot.id:
                return True, text

    return False, ""


async def handle_sticker_reply_to_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """群组中回复机器人消息并发送贴纸时，用配置的贴纸回复"""
    if not update.message or not update.message.sticker:
        return
    if update.effective_chat.id not in ALLOWED_CHAT_IDS:
        return
    reply_to = update.message.reply_to_message
    if not reply_to or not reply_to.from_user:
        return
    if reply_to.from_user.id != context.bot.id:
        return
    sticker_ids = get_sticker_ids()
    if not sticker_ids:
        return
    try:
        sticker_id = random.choice(sticker_ids)
        await update.message.reply_sticker(sticker=sticker_id)
    except Exception as e:
        logger.warning("贴纸回复失败: %s", e)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理文本消息，调用 AI 并回复"""
    # 检查是否在等待 /set_prompt 的输入（仅管理员可设置）
    if context.chat_data.get("awaiting_prompt") and update.message and update.message.text:
        from config.settings import BOT_OWNER_ID
        if update.effective_user.id != BOT_OWNER_ID:
            await update.message.reply_text("❌ 权限不足。")
            return
        context.chat_data["awaiting_prompt"] = False
        if update.message.text.strip().lower() == "/cancel":
            await update.message.reply_text("已取消。")
            return
        from bot.models.database import set_group_settings
        set_group_settings(update.effective_chat.id, custom_prompt=update.message.text.strip())
        await update.message.reply_text("✅ 已更新本群自定义设定。")
        return

    ok, query = should_respond(update, context)
    if not ok:
        if update.effective_chat.type == "private":
            await update.message.reply_text("本机器人仅在群组中使用，请将机器人加入群组后 @提及 或 以「小助理，」开头 提问。")
        elif update.effective_chat.id not in ALLOWED_CHAT_IDS:
            await update.message.reply_text("本机器人仅在指定群组中可用，如有需要请联系 @XHNVPU 并注明来意。")
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    message = update.message
    user = update.effective_user
    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or "用户"

    # 回复机器人时：将被回复的那条机器人消息注入上下文
    reply_to_assistant = None
    if message.reply_to_message and message.reply_to_message.from_user:
        if message.reply_to_message.from_user.id == context.bot.id:
            reply_to_assistant = (message.reply_to_message.text or message.reply_to_message.caption or "").strip()

    # 特殊：霜刃（或他人）发「小助理，你来回答」并回复某条消息时，将该消息内容作为待回答问题
    q = query.strip().rstrip("。.！？!? ").strip()
    if q == "你来回答" and message.reply_to_message:
        replied = message.reply_to_message
        if replied.from_user and replied.from_user.id != context.bot.id:
            replied_text = (replied.text or replied.caption or "").strip()
            if replied_text:
                query = f"【以下是待回答的问题】\n{replied_text}"

    # 限流
    if not rate_limiter.check(chat_id, user_id):
        await message.reply_text(
            "⚠️ 请求太频繁，请稍后再试。",
            reply_to_message_id=message.message_id,
        )
        return

    # 显示「正在输入」
    await context.bot.send_chat_action(
        chat_id=chat_id,
        action=ChatAction.TYPING,
    )

    try:
        messages = build_messages_for_ai(chat_id, user_id, query, reply_to_assistant=reply_to_assistant)
        reply = chat_completion(messages, chat_id=chat_id, user_full_name=full_name, user_message=query)
        reply = replace_emoji_digits(reply or "")
        save_exchange(chat_id, user_id, query, reply)
        sent_msg = await message.reply_text(
            reply,
            reply_to_message_id=message.message_id,
        )
        # 小助理回复含「霜刃」时，霜刃收不到（bot→bot 限制），通过 handoff 代为发送「......」
        if sent_msg and "霜刃" in (reply or ""):
            try:
                from handoff import put_frost_reply_handoff
                ok = put_frost_reply_handoff(chat_id, sent_msg.message_id)
                if ok:
                    logger.info("handoff_frost: 已写入 chat_id=%s msg_id=%s", chat_id, sent_msg.message_id)
                else:
                    logger.warning("handoff_frost: 写入失败")
            except Exception as e:
                logger.warning("handoff_frost: 异常 %s", e, exc_info=True)
    except Exception as e:
        err_msg = str(e)
        logger.warning("AI 调用异常: %s", e, exc_info=True)
        # 400：提取 API 返回的详细错误信息
        if hasattr(e, "response") and e.response is not None:
            try:
                body = e.response.json()
                if "error" in body and isinstance(body["error"], dict):
                    em = body["error"].get("message", "") or body["error"].get("msg", "")
                    if em:
                        err_msg = f"{err_msg}\n\nAPI 详情: {em}"
            except Exception:
                pass
        # 超时：网络或 AI 服务响应慢
        if "timeout" in err_msg.lower() or "timed out" in err_msg.lower():
            err_msg = "哦吼，我没听清，请再说一遍"
        # 401 通常是 API Key 问题，给出排查建议
        elif "404" in err_msg or "not found" in err_msg.lower():
            from bot.services.group_config import get_ai_config
            provider = get_ai_config(chat_id).get("ai_provider", AI_PROVIDER)
            if provider == "ollama":
                err_msg = "模型未找到，请确认已执行 ollama pull <模型名> 并检查模型名称是否正确。"
            else:
                err_msg = (
                    "模型未找到或已下线，请用 /set_model 切换到可用模型（如 kimi-k2 / kimi）。"
                    "详情见 https://platform.moonshot.cn/docs"
                )
        elif "400" in err_msg or "bad request" in err_msg.lower():
            if "content_filter" in err_msg.lower():
                err_msg = "内容被安全策略拒绝，请换种方式提问。"
            elif "token" in err_msg.lower() and ("long" in err_msg.lower() or "exceed" in err_msg.lower() or "limit" in err_msg.lower()):
                err_msg = "输入或输出超出模型长度限制，可尝试 /newchat 清空对话历史后再试。"
            elif "invalid" in err_msg.lower() or "request" in err_msg.lower():
                err_msg = err_msg  # 保留 API 返回的详情
        elif "401" in err_msg or "invalid_api_key" in err_msg or "Incorrect API key" in err_msg:
            tip = (
                f"API Key 无效或与当前提供商不匹配。\n"
                f"当前配置：AI_PROVIDER={AI_PROVIDER}，请确认 .env 中：\n"
                f"• 使用 Kimi 时：AI_PROVIDER=kimi，且 OPENAI_API_KEY 为 Kimi 平台的 Key\n"
                f"• 使用 OpenAI 时：AI_PROVIDER=openai，且 Key 来自 platform.openai.com"
            )
            err_msg = f"我突然有点发高烧，要说胡话了\n\n💡 {tip}"
        else:
            err_msg = f"呀，我被外星人劫持了，它控制了我的大脑：{err_msg}"
        await message.reply_text(
            err_msg,
            reply_to_message_id=message.message_id,
        )
