#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
消息删除模块（可复用）
保证所有需删消息最终都能删除：立即删除 + 重试 → 入队 → 同群触发重试 + 定时兜底
依赖：python-telegram-bot
"""
import asyncio
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

# 可配置参数（支持环境变量）
PENDING_DELETE_RETRY_TTL = int(os.getenv("PENDING_DELETE_RETRY_TTL", "3600"))
PENDING_DELETE_RETRY_MAX = int(os.getenv("PENDING_DELETE_RETRY_MAX", "100"))
PENDING_DELETE_RETRY_PER_MSG = int(os.getenv("PENDING_DELETE_RETRY_PER_MSG", "3"))
PENDING_DELETE_RETRY_JOB_BATCH = int(os.getenv("PENDING_DELETE_RETRY_JOB_BATCH", "15"))

_pending_delete_retry: List[Tuple[str, int, int, float]] = []  # [(chat_id, msg_id, user_id, ts), ...]
_delete_stats: Dict[str, int] = {
    "immediate_success": 0, "immediate_fail": 0,
    "retry_success": 0, "retry_fail": 0,
    "evict_retry_success": 0, "evict_retry_fail": 0,
}


def _log_failure(chat_id: Any, msg_id: int, label: str, e: Exception, prefix: str = "PTB"):
    """删除失败时输出日志"""
    print(f"[{prefix}] 删除消息失败 chat_id={chat_id} msg_id={msg_id} {label}: {type(e).__name__}: {e}")


async def _add_pending_retry(bot: Any, chat_id: str, msg_id: int, user_id: int, log_prefix: str = "PTB"):
    """删除失败时加入待重试队列。队列满时先尝试删除被驱逐的那条，再打警告日志。"""
    global _pending_delete_retry, _delete_stats
    if any((c, m) == (chat_id, msg_id) for (c, m, u, t) in _pending_delete_retry):
        return
    now = time.time()
    cutoff = now - PENDING_DELETE_RETRY_TTL
    _pending_delete_retry[:] = [(c, m, u, t) for (c, m, u, t) in _pending_delete_retry if t > cutoff]
    while len(_pending_delete_retry) >= PENDING_DELETE_RETRY_MAX:
        evicted = _pending_delete_retry.pop(0)
        cid, mid, _, _ = evicted
        print(f"[{log_prefix}] 待删队列已满，驱逐 chat_id={cid} msg_id={mid} 给新任务腾位")
        try:
            await bot.delete_message(chat_id=int(cid), message_id=mid)
            _delete_stats["evict_retry_success"] += 1
        except Exception as e:
            if "not found" in str(e).lower():
                _delete_stats["evict_retry_success"] += 1
            else:
                _delete_stats["evict_retry_fail"] += 1
                print(f"[{log_prefix}] 驱逐时删除失败 chat_id={cid} msg_id={mid}: {type(e).__name__}: {e}")
    _pending_delete_retry.append((chat_id, msg_id, user_id, now))


async def delete_message_with_retry(
    bot: Any,
    chat_id: int,
    msg_id: int,
    label: str,
    retries: int = 3,
    cache_dict: Optional[dict] = None,
    clear_cache_key: Optional[Tuple[str, int]] = None,
    on_success: Optional[Callable[[], None]] = None,
    log_prefix: str = "PTB",
) -> bool:
    """带重试的删除。成功时若提供 cache_dict+clear_cache_key 或 on_success 则执行清理。
    失败时入待重试队列。clear_cache_key 的 [1] 为 user_id（0 表示 bot 消息）。"""
    global _delete_stats
    for attempt in range(retries):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            _delete_stats["immediate_success"] += 1
            if cache_dict is not None and clear_cache_key is not None:
                cache_dict.pop(clear_cache_key, None)
            if on_success is not None:
                on_success()
            return True
        except Exception as e:
            if attempt < retries - 1:
                await asyncio.sleep(2)
            else:
                _log_failure(chat_id, msg_id, label, e, log_prefix)
                _delete_stats["immediate_fail"] += 1
                if "not found" in str(e).lower():
                    return False
    user_id = clear_cache_key[1] if clear_cache_key is not None else 0
    await _add_pending_retry(bot, str(chat_id), msg_id, user_id, log_prefix)
    return False


async def retry_pending_deletes_for_chat(bot: Any, chat_id: str, log_prefix: str = "PTB"):
    """同群触发：顺带重试该群待删除队列。单次最多重试 N 条。"""
    global _pending_delete_retry, _delete_stats
    now = time.time()
    cutoff = now - PENDING_DELETE_RETRY_TTL
    candidates = [(c, m, u, t) for (c, m, u, t) in _pending_delete_retry if c == chat_id and t > cutoff]
    to_retry = candidates[:PENDING_DELETE_RETRY_PER_MSG]
    remaining_this_chat = candidates[PENDING_DELETE_RETRY_PER_MSG:]
    other_items = [(c, m, u, t) for (c, m, u, t) in _pending_delete_retry if c != chat_id or t <= cutoff]
    for item in to_retry:
        cid, mid, _, _ = item
        try:
            await bot.delete_message(chat_id=int(cid), message_id=mid)
            _delete_stats["retry_success"] += 1
        except Exception as e:
            if "not found" in str(e).lower():
                _delete_stats["retry_success"] += 1
            else:
                remaining_this_chat.append(item)
                _delete_stats["retry_fail"] += 1
    _pending_delete_retry[:] = other_items + remaining_this_chat


async def delete_after(
    bot: Any,
    chat_id: int,
    msg_id: int,
    sec: int,
    user_msg_id: Optional[int] = None,
    cache_dict: Optional[dict] = None,
    user_cache_key: Optional[Tuple[str, int]] = None,
    log_prefix: str = "PTB",
):
    """sec 秒后删除 msg_id；若提供 user_msg_id，先尝试删除用户消息。"""
    await asyncio.sleep(sec)
    if user_msg_id is not None:
        await delete_message_with_retry(
            bot, chat_id, user_msg_id, "user_msg",
            cache_dict=cache_dict, clear_cache_key=user_cache_key, log_prefix=log_prefix,
        )
    await delete_message_with_retry(bot, chat_id, msg_id, "bot_msg", log_prefix=log_prefix)


async def job_retry_pending_deletes(context: Any) -> None:
    """兜底：定时扫描待重试队列。供 PTB job_queue 注册。"""
    global _pending_delete_retry, _delete_stats
    bot = context.bot
    now = time.time()
    cutoff = now - PENDING_DELETE_RETRY_TTL
    valid = [(c, m, u, t) for (c, m, u, t) in _pending_delete_retry if t > cutoff]
    to_retry = sorted(valid, key=lambda x: x[3])[:PENDING_DELETE_RETRY_JOB_BATCH]
    retried_ids = {(c, m) for c, m, _, _ in to_retry}
    remaining = [(c, m, u, t) for (c, m, u, t) in valid if (c, m) not in retried_ids]
    other = [(c, m, u, t) for (c, m, u, t) in _pending_delete_retry if t <= cutoff]
    for item in to_retry:
        cid, mid, _, _ = item
        try:
            await bot.delete_message(chat_id=int(cid), message_id=mid)
            _delete_stats["retry_success"] += 1
        except Exception as e:
            if "not found" in str(e).lower():
                _delete_stats["retry_success"] += 1
            else:
                remaining.append(item)
                _delete_stats["retry_fail"] += 1
        await asyncio.sleep(1)
    _pending_delete_retry[:] = other + remaining


def get_stats() -> Dict[str, int]:
    """获取删除统计"""
    return dict(_delete_stats)


def get_pending_queue_len() -> int:
    """获取待重试队列长度"""
    return len(_pending_delete_retry)
