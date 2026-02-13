"""管理命令：/settings /set_model /set_prompt /reset_* /addsticker /liststickers /delsticker"""
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config.settings import BOT_OWNER_ID
from bot.models.database import (
    set_group_settings,
    add_sticker,
    remove_sticker_by_index,
    remove_sticker_by_file_id,
    has_sticker,
)
from bot.models.database import get_sticker_ids as db_get_sticker_ids
from bot.services.group_config import get_ai_config, get_custom_prompt, get_preset_list, PRESET_MODELS


def _is_owner(update: Update) -> bool:
    """是否为管理员（仅 BOT_OWNER_ID 可管理）"""
    user_id = update.effective_user.id if update.effective_user else 0
    return user_id == BOT_OWNER_ID


async def _check_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """检查权限，无权限时回复并返回 False"""
    if not _is_owner(update):
        msg = update.message or (update.callback_query.message if update.callback_query else None)
        if msg:
            await msg.reply_text("❌ 权限不足。")
        elif update.callback_query:
            await update.callback_query.answer("❌ 权限不足。", show_alert=True)
        return False
    return True


async def _ensure_group(update: Update) -> bool:
    """确保在群组中，私聊则提示"""
    if update.effective_chat.type not in ("group", "supergroup"):
        if update.message:
            await update.message.reply_text("请在群组中使用此命令。")
        return False
    return True


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看当前群配置"""
    if not await _check_owner(update, context):
        return
    chat_id = update.effective_chat.id
    if chat_id > 0:
        # 私聊用全局
        cfg = get_ai_config(0)
        custom = get_custom_prompt(0)
        text = f"📋 当前为私聊，使用全局配置\n\n模型：{cfg['ai_provider']} / {cfg['model_name']}\n自定义设定：{'已设置' if custom else '未设置'}"
        await update.message.reply_text(text)
        return

    cfg = get_ai_config(chat_id)
    custom = get_custom_prompt(chat_id)
    len_custom = len(custom) if custom else 0

    text = (
        f"📋 本群配置\n\n"
        f"模型：{cfg['ai_provider']} / {cfg['model_name']}\n"
        f"自定义设定：{'已设置 (' + str(len_custom) + ' 字)' if custom else '使用全局'}\n\n"
        f"命令：\n"
        f"/set_model - 切换模型\n"
        f"/set_prompt - 设置本群设定\n"
        f"/reset_prompt - 恢复用全局设定\n"
        f"/reset_model - 恢复用全局模型"
    )
    await update.message.reply_text(text)


async def cmd_set_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """切换模型 - 显示预置方案按钮"""
    if not await _check_owner(update, context):
        return
    if not await _ensure_group(update):
        return
    chat_id = update.effective_chat.id

    presets = get_preset_list()
    keyboard = []
    row = []
    for i, (sid, name) in enumerate(presets):
        row.append(InlineKeyboardButton(name, callback_data=f"model:{sid}"))
        if len(row) >= 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    await update.message.reply_text(
        "选择要切换的模型：",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def callback_set_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理模型切换的回调"""
    if not _is_owner(update):
        await update.callback_query.answer("❌ 权限不足。", show_alert=True)
        return
    query = update.callback_query
    await query.answer()
    if not query.data or not query.data.startswith("model:"):
        return
    preset_id = query.data[6:]
    if preset_id not in PRESET_MODELS:
        await query.edit_message_text("❌ 未知方案")
        return

    chat_id = query.message.chat_id
    preset = PRESET_MODELS[preset_id]
    set_group_settings(
        chat_id,
        ai_provider=preset["ai_provider"],
        model_name=preset["model_name"],
        openai_base_url=preset["base_url"],
        openai_api_key=preset.get("api_key") or "",
    )
    await query.edit_message_text(f"✅ 已切换为：{preset_id} ({preset['model_name']})")


async def cmd_set_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """设置本群 custom_prompt - 进入等待状态"""
    if not await _check_owner(update, context):
        return
    if not await _ensure_group(update):
        return
    context.chat_data["awaiting_prompt"] = True
    await update.message.reply_text("请直接发送下一条消息作为本群的自定义设定，或发送 /cancel 取消。")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """取消等待"""
    if context.chat_data.pop("awaiting_prompt", None):
        await update.message.reply_text("已取消。")
    elif context.chat_data.pop("awaiting_sticker", None):
        await update.message.reply_text("已取消添加贴纸。")
    elif context.chat_data.pop("awaiting_tiezhi", None):
        await update.message.reply_text("已退出贴纸管理。")
    else:
        await update.message.reply_text("没有进行中的操作。")


async def cmd_reset_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """恢复用全局 custom_prompt"""
    if not await _check_owner(update, context):
        return
    if not await _ensure_group(update):
        return
    chat_id = update.effective_chat.id
    set_group_settings(chat_id, custom_prompt="")
    await update.message.reply_text("✅ 已恢复使用全局设定。")


async def cmd_tiezhi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """贴纸管理：发送贴纸则存在则删、不存在则添，连续操作直到 /cancel"""
    if not await _check_owner(update, context):
        return
    if update.effective_chat.type != "private":
        await update.message.reply_text("贴纸管理请在私聊中使用。")
        return
    context.chat_data["awaiting_tiezhi"] = True
    ids = db_get_sticker_ids()
    count = len(ids)
    await update.message.reply_text(
        f"贴纸管理模式（当前 {count} 张）\n"
        "发送贴纸：已存在则删除，不存在则添加。\n"
        "输入 /cancel 或 /start 退出。"
    )


async def cmd_getsticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """获取贴纸 file_id 或处理 /tiezhi 模式下的贴纸切换（仅所有者，私聊）"""
    if not _is_owner(update):
        return
    if update.effective_chat.type != "private":
        return
    sticker = update.message.sticker if update.message and update.message.sticker else None
    if not sticker:
        await update.message.reply_text(
            "请直接发送一个贴纸以获取 file_id，或使用 /tiezhi 管理贴纸池。"
        )
        return
    fid = sticker.file_id
    # /tiezhi 模式：存在则删，不存在则添，保持模式
    if context.chat_data.get("awaiting_tiezhi"):
        if has_sticker(fid):
            remove_sticker_by_file_id(fid)
            await update.message.reply_text("❌ 已从贴纸池删除。")
        else:
            add_sticker(fid)
            await update.message.reply_text("✅ 已添加到贴纸池。")
        return
    await update.message.reply_text(
        f"贴纸 file_id：\n{fid}\n\n使用 /tiezhi 可管理贴纸池。",
    )


async def cmd_reset_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """恢复用全局模型"""
    if not await _check_owner(update, context):
        return
    if not await _ensure_group(update):
        return
    chat_id = update.effective_chat.id
    from bot.models.database import clear_group_model
    clear_group_model(chat_id)
    await update.message.reply_text("✅ 已恢复使用全局模型。")
