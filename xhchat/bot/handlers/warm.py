# -*- coding: utf-8 -*-
"""暖群机制：监听管理员发言，超时无管理员说话时主动暖群；3-5 人连续重复则跟发"""
import logging
import random
from collections import defaultdict
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import ContextTypes

from config.settings import ALLOWED_CHAT_IDS
from bot.handlers.warn import _update_username_cache
from bot.models.database import (
    get_group_activity,
    update_admin_activity,
    update_warm_at,
)

logger = logging.getLogger(__name__)

# 多人重复：最近消息缓存，随机 3-5 人触发
_recent_messages: dict[int, list[tuple[int, str]]] = defaultdict(list)
MAX_RECENT = 15
REPEAT_MIN_LEN = 2
REPEAT_COOLDOWN_SECONDS = 30
REPEAT_MIN_PEOPLE = 3
REPEAT_MAX_PEOPLE = 5
_last_repeat_at: dict[int, datetime] = {}

# 启动暖群：首次 check_and_warm 时发送（此时 bot 已就绪，避免 ExtBot 初始化错误）
_startup_warm_done = False

# 暖群文案
WARM_MESSAGES = [
    "大家好久没聊天啦～来唠唠呗～",
    "有人在吗～随便聊聊呀～",
    "气氛有点安静呢，有人想唠唠吗～",
    "嘿嘿，群里有点冷清，谁来开个头～",
    "大家好～今天有什么新鲜事吗～",
    "Oi！～有人想唠唠吗～",
    "路过一下～大家最近都在忙啥呀～",
    "发现一个安静的群，有人想聊聊吗～",
    "嗨～来个人说说话呗～",
    "感觉可以开个茶话会了～有人参加吗～",
    "好久不见大家啦～想你们了～",
    "偷偷冒个泡～有人想唠嗑吗～",
    "今天天气不错呀～有人想唠唠吗～",
    "发现群里有空位，我来占一下～",
    "各位～有什么好玩的事分享一下呗～",
    "滴滴～有人在线吗～",
    "新春愉快呀～来唠唠呗～",
    "搬个小板凳坐下，等大家来聊天～",
    "群里的朋友们～出来晒太阳啦～",
    "敲敲门～有人在家吗～",
]


async def track_admin_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """监听消息：① 管理员发言则更新 last_admin_message_at；② 3-5 人连续重复则跟发"""
    if not update.message or not update.effective_chat:
        return
    chat_id = update.effective_chat.id
    if chat_id not in ALLOWED_CHAT_IDS:
        return
    if update.effective_chat.type not in ("group", "supergroup"):
        return
    user = update.effective_user
    if not user:
        return

    # 机器人自身消息不参与统计
    if user.id == context.bot.id:
        return

    # 更新用户名缓存，供 /warn @用户名 解析
    _update_username_cache(chat_id, user)
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        _update_username_cache(chat_id, update.message.reply_to_message.from_user)

    # ① 管理员发言记录
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        admin_ids = {a.user.id for a in admins}
        if user.id in admin_ids:
            update_admin_activity(chat_id)
    except Exception as e:
        logger.warning("暖群: 获取管理员列表失败 %s", e)

    # ② 多人连续重复检测：随机 3-5 人，仅统计文本
    text = (update.message.text or "").strip()
    if len(text) < REPEAT_MIN_LEN:
        return
    recent = _recent_messages[chat_id]
    recent.append((user.id, text))
    if len(recent) > MAX_RECENT:
        _recent_messages[chat_id] = recent[-MAX_RECENT:]

    # 随机确定本次需要的人数 N（3-5）
    required = random.randint(REPEAT_MIN_PEOPLE, REPEAT_MAX_PEOPLE)
    if len(recent) >= required:
        last_n = recent[-required:]
        users = {u for u, _ in last_n}
        contents = {c for _, c in last_n}
        if len(users) == required and len(contents) == 1:
            now = datetime.now()
            last = _last_repeat_at.get(chat_id)
            if last is None or (now - last).total_seconds() >= REPEAT_COOLDOWN_SECONDS:
                try:
                    await context.bot.send_message(chat_id=chat_id, text=last_n[0][1])
                    _last_repeat_at[chat_id] = now
                    _recent_messages[chat_id] = []  # 清空避免连续触发
                    logger.info("暖群: chat_id=%s %d人重复跟发", chat_id, required)
                except Exception as e:
                    logger.warning("暖群: 重复跟发失败 %s", e)


async def check_and_warm(context: ContextTypes.DEFAULT_TYPE):
    """定时检查：若距上次管理员发言超过阈值则暖群；首次运行时发送启动暖群"""
    global _startup_warm_done
    from config.settings import (
        WARM_ENABLED,
        WARM_IDLE_MINUTES,
        WARM_COOLDOWN_MINUTES,
        WARM_SILENT_START,
        WARM_SILENT_END,
    )
    from bot.services.sticker_service import get_sticker_ids

    sticker_ids = get_sticker_ids()

    # 首次运行：发送启动暖群（60 秒后 bot 已就绪），向每个群发送
    if not _startup_warm_done:
        _startup_warm_done = True
        for cid in ALLOWED_CHAT_IDS:
            try:
                use_sticker = sticker_ids and random.random() < 0.5
                if use_sticker:
                    sticker_id = random.choice(sticker_ids)
                    await context.bot.send_sticker(chat_id=cid, sticker=sticker_id)
                else:
                    msg = random.choice(WARM_MESSAGES)
                    await context.bot.send_message(chat_id=cid, text=msg)
                logger.info("启动暖群: chat_id=%s 已发送%s", cid, "贴纸" if use_sticker else "文案")
            except Exception as e:
                logger.warning("启动暖群 chat_id=%s 失败: %s", cid, e)

    if not WARM_ENABLED:
        return

    for chat_id in ALLOWED_CHAT_IDS:
        now = datetime.utcnow()

        # 静默时段（如 0-8 点不暖群，使用本地时间）
        if WARM_SILENT_START is not None and WARM_SILENT_END is not None:
            hour = datetime.now().hour
            if WARM_SILENT_START <= WARM_SILENT_END:
                if WARM_SILENT_START <= hour < WARM_SILENT_END:
                    continue
            else:
                if hour >= WARM_SILENT_START or hour < WARM_SILENT_END:
                    continue

        try:
            activity = get_group_activity(chat_id)
        except Exception as e:
            logger.warning("暖群: chat_id=%s 读取活动记录失败 %s", chat_id, e)
            continue

        last_admin = activity.get("last_admin_message_at") if activity else None
        last_warm = activity.get("last_warm_at") if activity else None

        def parse_ts(s):
            if not s:
                return None
            try:
                return datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S")
            except Exception:
                return None

        last_admin_dt = parse_ts(last_admin)
        last_warm_dt = parse_ts(last_warm)

        idle_minutes = WARM_IDLE_MINUTES
        cooldown_minutes = WARM_COOLDOWN_MINUTES

        # 从未有管理员发言过：不暖群（避免群刚加机器人就发）
        if last_admin_dt is None:
            continue

        idle_ok = (now - last_admin_dt) >= timedelta(minutes=idle_minutes)
        cooldown_ok = last_warm_dt is None or (now - last_warm_dt) >= timedelta(minutes=cooldown_minutes)

        if idle_ok and cooldown_ok:
            try:
                use_sticker = sticker_ids and random.random() < 0.5
                if use_sticker:
                    sticker_id = random.choice(sticker_ids)
                    await context.bot.send_sticker(chat_id=chat_id, sticker=sticker_id)
                else:
                    msg = random.choice(WARM_MESSAGES)
                    await context.bot.send_message(chat_id=chat_id, text=msg)
                update_warm_at(chat_id)
                logger.info("暖群: chat_id=%s 已发送%s", chat_id, "贴纸" if use_sticker else "文案")
            except Exception as e:
                logger.warning("暖群 chat_id=%s 发送失败: %s", chat_id, e)
