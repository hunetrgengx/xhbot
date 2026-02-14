# -*- coding: utf-8 -*-
"""暖群调度器 + 延迟删除：独立线程，统一处理暖群与 /xhset 自动删除"""
import asyncio
import logging
import random
import threading
import time
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

from telegram import Bot
from config.settings import (
    TELEGRAM_BOT_TOKEN,
    ALLOWED_CHAT_IDS,
    WARM_ENABLED,
    WARM_IDLE_MINUTES,
    WARM_COOLDOWN_MINUTES,
    WARM_CHECK_INTERVAL,
    WARM_SILENT_START,
    WARM_SILENT_END,
    RANDOM_WATER_MIN_MINUTES,
    RANDOM_WATER_MAX_MINUTES,
)
from bot.services.sticker_service import get_sticker_ids
from bot.services.context_manager import build_messages_for_ai, save_exchange
from bot.services.ai_service import chat_completion
from bot.models.database import get_group_activity, update_warm_at
from bot.handlers.warm import WARM_MESSAGES

logger = logging.getLogger(__name__)

_startup_warm_done = False
_stop_event = threading.Event()
# 延迟删除队列：(run_at, chat_id, message_id)，线程安全
_delete_queue: list[tuple[float, int, int]] = []
_delete_lock = threading.Lock()
# 循环检查最大睡眠（秒），两个下次任务都较远时减少唤醒
LOOP_MAX_SLEEP_SEC = 300
HANDOFF_CHECK_INTERVAL_SEC = 2


def _beijing_hour() -> int:
    """获取当前北京时间（0-23）"""
    if ZoneInfo:
        return datetime.now(ZoneInfo("Asia/Shanghai")).hour
    return datetime.now().hour


def _in_silent_period() -> bool:
    """是否处于静默时段（0-8 点北京时间不暖群、不水群）"""
    if WARM_SILENT_START is None or WARM_SILENT_END is None:
        return False
    hour = _beijing_hour()
    if WARM_SILENT_START <= WARM_SILENT_END:
        return WARM_SILENT_START <= hour < WARM_SILENT_END
    return hour >= WARM_SILENT_START or hour < WARM_SILENT_END


async def _delete_message_async(bot: Bot, chat_id: int, message_id: int) -> None:
    """删除消息"""
    await bot.delete_message(chat_id=chat_id, message_id=message_id)


async def _do_random_water_async(bot: Bot) -> None:
    """随机水群：仅发送贴纸，向每个允许的群发送"""
    sticker_ids = get_sticker_ids()
    if not sticker_ids:
        return
    if _in_silent_period():
        return
    sticker_id = random.choice(sticker_ids)
    for chat_id in ALLOWED_CHAT_IDS:
        try:
            await bot.send_sticker(chat_id=chat_id, sticker=sticker_id)
            logger.info("随机水群: chat_id=%s 已发送贴纸", chat_id)
        except Exception as e:
            logger.warning("随机水群 chat_id=%s 失败: %s", chat_id, e)


def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


async def _do_warm_tick_async(bot: Bot) -> None:
    """执行一次暖群检查，对每个允许的群独立检查"""
    global _startup_warm_done
    # 首次运行：启动暖群，向每个群发送
    if not _startup_warm_done:
        _startup_warm_done = True
        sticker_ids = get_sticker_ids()
        for chat_id in ALLOWED_CHAT_IDS:
            try:
                use_sticker = sticker_ids and random.random() < 0.5
                if use_sticker:
                    sticker_id = random.choice(sticker_ids)
                    await bot.send_sticker(chat_id=chat_id, sticker=sticker_id)
                else:
                    msg = random.choice(WARM_MESSAGES)
                    await bot.send_message(chat_id=chat_id, text=msg)
                logger.info("启动暖群: chat_id=%s 已发送%s", chat_id, "贴纸" if use_sticker else "文案")
            except Exception as e:
                logger.warning("启动暖群 chat_id=%s 失败: %s", chat_id, e)

    if not WARM_ENABLED:
        return

    now = datetime.utcnow()
    # 静默时段（北京时间 0-8 点不暖群）
    if WARM_SILENT_START is not None and WARM_SILENT_END is not None:
        hour = _beijing_hour()
        if WARM_SILENT_START <= WARM_SILENT_END:
            if WARM_SILENT_START <= hour < WARM_SILENT_END:
                return
        else:
            if hour >= WARM_SILENT_START or hour < WARM_SILENT_END:
                return

    for chat_id in ALLOWED_CHAT_IDS:
        try:
            activity = get_group_activity(chat_id)
        except Exception as e:
            logger.warning("暖群: chat_id=%s 读取活动记录失败 %s", chat_id, e)
            continue

        last_admin = activity.get("last_admin_message_at") if activity else None
        last_warm = activity.get("last_warm_at") if activity else None
        last_admin_dt = _parse_ts(last_admin)
        last_warm_dt = _parse_ts(last_warm)
        if last_admin_dt is None:
            continue

        idle_ok = (now - last_admin_dt) >= timedelta(minutes=WARM_IDLE_MINUTES)
        cooldown_ok = last_warm_dt is None or (now - last_warm_dt) >= timedelta(minutes=WARM_COOLDOWN_MINUTES)
        if idle_ok and cooldown_ok:
            try:
                sticker_ids = get_sticker_ids()
                use_sticker = sticker_ids and random.random() < 0.5
                if use_sticker:
                    sticker_id = random.choice(sticker_ids)
                    await bot.send_sticker(chat_id=chat_id, sticker=sticker_id)
                else:
                    msg = random.choice(WARM_MESSAGES)
                    await bot.send_message(chat_id=chat_id, text=msg)
                update_warm_at(chat_id)
                logger.info("暖群: chat_id=%s 已发送%s", chat_id, "贴纸" if use_sticker else "文案")
            except Exception as e:
                logger.warning("暖群 chat_id=%s 发送失败: %s", chat_id, e)


async def _process_handoff_async(bot: Bot) -> None:
    """霜刃转交：轮询 handoff 文件，代为回复"""
    try:
        from handoff import take_handoff
        req = take_handoff()
        if not req:
            return
        chat_id = req["chat_id"]
        reply_to_id = req["reply_to_message_id"]
        question = req["question"]
        if chat_id not in ALLOWED_CHAT_IDS:
            logger.warning("handoff: 跳过非允许群 chat_id=%s", chat_id)
            return
        from bot.services.text_utils import replace_emoji_digits
        messages = build_messages_for_ai(chat_id, 0, question)
        reply = chat_completion(messages, chat_id=chat_id, user_full_name="用户", user_message=question)
        reply = replace_emoji_digits(reply or "")
        save_exchange(chat_id, 0, question, reply)
        await bot.send_message(
            chat_id=chat_id,
            text=reply,
            reply_to_message_id=reply_to_id,
        )
        logger.info("handoff: 已代为回复 chat_id=%s reply_to=%s", chat_id, reply_to_id)
    except ImportError:
        pass
    except Exception as e:
        logger.warning("handoff 处理失败: %s", e)


def run_warm_scheduler() -> None:
    """在独立线程中运行：延迟删除 + 暖群（启动/空闲/随机水群）"""
    if not TELEGRAM_BOT_TOKEN:
        return

    def _worker():
        time.sleep(60)  # 等待主 bot 完全启动
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            bot = Bot(token=TELEGRAM_BOT_TOKEN)
            idle_interval_sec = WARM_CHECK_INTERVAL * 60 if WARM_ENABLED else 3600
            next_random_water = time.time() + random.randint(
                RANDOM_WATER_MIN_MINUTES * 60, RANDOM_WATER_MAX_MINUTES * 60
            ) if WARM_ENABLED else float("inf")
            next_idle = time.time() if WARM_ENABLED else float("inf")
            next_handoff = time.time()
            while not _stop_event.is_set():
                now = time.time()
                # 0. 霜刃转交（每 2 秒检查）
                if now >= next_handoff:
                    try:
                        loop.run_until_complete(_process_handoff_async(bot))
                    except Exception:
                        pass
                    next_handoff = now + HANDOFF_CHECK_INTERVAL_SEC
                # 1. 处理延迟删除
                with _delete_lock:
                    due = [(t, c, m) for t, c, m in _delete_queue if t <= now]
                    _delete_queue[:] = [(t, c, m) for t, c, m in _delete_queue if t > now]
                    next_delete = min((t for t, _, _ in _delete_queue), default=float("inf"))
                for _, chat_id, msg_id in due:
                    try:
                        loop.run_until_complete(_delete_message_async(bot, chat_id, msg_id))
                    except Exception:
                        pass
                # 2. 暖群（仅 WARM_ENABLED 时）
                if WARM_ENABLED:
                    if now >= next_random_water:
                        loop.run_until_complete(_do_random_water_async(bot))
                        next_random_water = now + random.randint(
                            RANDOM_WATER_MIN_MINUTES * 60, RANDOM_WATER_MAX_MINUTES * 60
                        )
                    if now >= next_idle:
                        loop.run_until_complete(_do_warm_tick_async(bot))
                        next_idle = now + idle_interval_sec
                # 3. 下次唤醒时间
                candidates = [next_random_water - now, next_idle - now, next_handoff - now]
                if next_delete != float("inf"):
                    candidates.append(next_delete - now)
                sleep_sec = min((c for c in candidates if 0 < c < float("inf")), default=LOOP_MAX_SLEEP_SEC)
                sleep_sec = min(max(1, int(sleep_sec)), LOOP_MAX_SLEEP_SEC)
                _stop_event.wait(sleep_sec)
        finally:
            loop.close()

    t = threading.Thread(target=_worker, daemon=True, name="warm_scheduler")
    t.start()
    if WARM_ENABLED:
        logger.info(
            "调度器已启动（延迟删除 + 空闲检查每 %s 分钟，随机水群 %d-%d 分钟）",
            WARM_CHECK_INTERVAL, RANDOM_WATER_MIN_MINUTES, RANDOM_WATER_MAX_MINUTES,
        )
    else:
        logger.info("调度器已启动（仅延迟删除）")


def stop_warm_scheduler() -> None:
    _stop_event.set()


def schedule_delete_message(chat_id: int, message_id: int, delay_sec: float = 3) -> None:
    """安排延迟删除消息（供 /xhset 等调用，合并到调度线程）"""
    with _delete_lock:
        _delete_queue.append((time.time() + delay_sec, chat_id, message_id))
