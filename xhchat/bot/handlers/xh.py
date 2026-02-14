"""设定管理命令：/xhadd /xhdel /xhset，仅允许群 ALLOWED_CHAT_IDS 使用"""
from telegram import Update
from telegram.ext import ContextTypes

from config.settings import ALLOWED_CHAT_IDS, BOT_OWNER_ID

XHSET_DELETE_DELAY = 3  # 秒

from bot.models.database import get_group_settings, set_group_settings
from bot.services.warm_scheduler import schedule_delete_message
from bot.services.group_config import get_custom_prompt


def _is_owner(update: Update) -> bool:
    user_id = update.effective_user.id if update.effective_user else 0
    return user_id == BOT_OWNER_ID


def _is_allowed_chat(update: Update) -> bool:
    return update.effective_chat.id in ALLOWED_CHAT_IDS


def _get_args_after_command(text: str, cmd: str) -> str:
    """提取命令后的内容，支持 /cmd 与 /cmd@botname 形式"""
    text = (text or "").strip()
    lowered = text.lower()
    prefix = f"/{cmd}"
    if not lowered.startswith(prefix):
        return ""
    rest = text[len(prefix):].lstrip()
    if rest.startswith("@"):
        idx = rest.find(" ")
        rest = rest[idx + 1:].strip() if idx >= 0 else ""
    return rest.strip()


async def cmd_xhadd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """添加一条设定（仅所有者），用法：/xhadd <设定内容>"""
    if not _is_allowed_chat(update):
        await update.message.reply_text("本命令仅在指定群组中可用。")
        return
    if not _is_owner(update):
        await update.message.reply_text("❌ 权限不足。")
        return
    content = _get_args_after_command(update.message.text or "", "xhadd")
    if not content:
        await update.message.reply_text("用法：/xhadd <设定内容>\n例如：/xhadd 星华的老板是小熊")
        return
    chat_id = update.effective_chat.id
    current = get_custom_prompt(chat_id) or ""
    lines = [ln.strip() for ln in current.split("\n") if ln.strip()]
    lines.append(content)
    new_prompt = "\n".join(lines)
    set_group_settings(chat_id, custom_prompt=new_prompt)
    await update.message.reply_text(f"✅ 已添加设定（共 {len(lines)} 条）。")


async def cmd_xhdel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """删除一条设定（仅所有者），用法：/xhdel <行号> 或 /xhdel <设定内容>"""
    if not _is_allowed_chat(update):
        await update.message.reply_text("本命令仅在指定群组中可用。")
        return
    if not _is_owner(update):
        await update.message.reply_text("❌ 权限不足。")
        return
    content = _get_args_after_command(update.message.text or "", "xhdel")
    chat_id = update.effective_chat.id
    current = get_custom_prompt(chat_id) or ""
    lines = [ln.strip() for ln in current.split("\n") if ln.strip()]
    if not lines:
        await update.message.reply_text("当前没有设定可删，请先用 /xhadd 添加。")
        return
    if not content:
        text = "当前设定（按行号或内容删除）：\n\n"
        for i, ln in enumerate(lines, 1):
            text += f"{i}. {ln}\n"
        text += "\n用法：/xhdel <行号> 或 /xhdel <设定内容>"
        await update.message.reply_text(text)
        return
    # 尝试按行号删除
    try:
        idx = int(content.strip())
        if 1 <= idx <= len(lines):
            lines.pop(idx - 1)
            new_prompt = "\n".join(lines) if lines else ""
            set_group_settings(chat_id, custom_prompt=new_prompt if new_prompt else "")
            await update.message.reply_text(f"✅ 已删除第 {idx} 条设定（剩余 {len(lines)} 条）。")
            return
    except ValueError:
        pass
    # 按内容删除：匹配包含该内容的行
    orig_len = len(lines)
    lines = [ln for ln in lines if content not in ln]
    if len(lines) < orig_len:
        new_prompt = "\n".join(lines) if lines else ""
        set_group_settings(chat_id, custom_prompt=new_prompt if new_prompt else "")
        await update.message.reply_text(f"✅ 已删除包含「{content}」的设定（剩余 {len(lines)} 条）。")
    else:
        await update.message.reply_text("未找到匹配的设定，可用 /xhset 查看当前设定。")


async def cmd_xhset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看当前设定（所有人可用）"""
    if not _is_allowed_chat(update):
        await update.message.reply_text("本命令仅在指定群组中可用。")
        return
    chat_id = update.effective_chat.id
    current = get_custom_prompt(chat_id) or ""
    lines = [ln.strip() for ln in current.split("\n") if ln.strip()]
    if not lines:
        gs = get_group_settings(chat_id)
        if gs and gs.get("custom_prompt") is not None:
            msg = await update.message.reply_text("当前设定为空（已清空本群自定义）。")
        else:
            msg = await update.message.reply_text("当前使用全局设定，本群暂无自定义设定。可用 /xhadd 添加（仅所有者）。")
        schedule_delete_message(chat_id, msg.message_id, XHSET_DELETE_DELAY)
        return
    text = "当前设定：\n\n"
    for i, ln in enumerate(lines, 1):
        text += f"{i}. {ln}\n"
    msg = await update.message.reply_text(text)
    schedule_delete_message(chat_id, msg.message_id, XHSET_DELETE_DELAY)
