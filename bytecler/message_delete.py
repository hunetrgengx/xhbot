#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
消息删除模块（可复用）
保证所有需删消息最终都能删除：立即删除 + 重试 → 入队 → 持久化溢出 → 永不放弃重试
依赖：python-telegram-bot
"""
import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# 可配置参数（支持环境变量）
PENDING_DELETE_RETRY_TTL = int(os.getenv("PENDING_DELETE_RETRY_TTL", "3600"))
PENDING_DELETE_RETRY_MAX = int(os.getenv("PENDING_DELETE_RETRY_MAX", "100"))
PENDING_DELETE_RETRY_PER_MSG = int(os.getenv("PENDING_DELETE_RETRY_PER_MSG", "3"))
PENDING_DELETE_RETRY_JOB_BATCH = int(os.getenv("PENDING_DELETE_RETRY_JOB_BATCH", "15"))
PENDING_DELETE_PERSIST_BATCH = int(os.getenv("PENDING_DELETE_PERSIST_BATCH", "30"))

_BASE = Path(__file__).resolve().parent
PENDING_DELETE_PERSIST_PATH = Path(os.getenv("PENDING_DELETE_PERSIST_PATH", str(_BASE / "pending_delete_persist.jsonl")))

# 删除事件埋点配置
DELETE_EVENTS_ENABLED = os.getenv("DELETE_EVENTS_ENABLED", "1") == "1"
DELETE_EVENTS_PATH = Path(os.getenv("DELETE_EVENTS_PATH", str(_BASE / "debug" / "delete_events.jsonl")))
DELETE_EVENTS_ROTATE_MB = int(os.getenv("DELETE_EVENTS_ROTATE_MB", "50"))
DELETE_EVENTS_RETAIN_DAYS = int(os.getenv("DELETE_EVENTS_RETAIN_DAYS", "7"))

_pending_delete_retry: List[Tuple[str, int, int, float]] = []  # [(chat_id, msg_id, user_id, ts), ...]
_attempt_count: Dict[Tuple[str, int], int] = {}  # (chat_id, msg_id) -> 累计尝试次数，用于 attempt_no
_delete_stats: Dict[str, int] = {
    "immediate_success": 0, "immediate_fail": 0,
    "retry_success": 0, "retry_fail": 0,
    "evict_retry_success": 0, "evict_retry_fail": 0,
    "persist_retry_success": 0, "persist_retry_fail": 0,
}


def _log_failure(chat_id: Any, msg_id: int, label: str, e: Exception, prefix: str = "PTB"):
    """删除失败时输出日志"""
    print(f"[{prefix}] 删除消息失败 chat_id={chat_id} msg_id={msg_id} {label}: {type(e).__name__}: {e}")


def _ts_iso() -> str:
    """UTC ISO 时间戳"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _emit_event(evt: str, **kwargs: Any) -> None:
    """写入埋点事件到 delete_events.jsonl，支持按大小轮转"""
    if not DELETE_EVENTS_ENABLED:
        return
    try:
        DELETE_EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {"evt": evt, "ts": _ts_iso(), **kwargs}
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        # 轮转检查：写入前若文件超限则轮转
        if DELETE_EVENTS_PATH.exists():
            size_mb = DELETE_EVENTS_PATH.stat().st_size / (1024 * 1024)
            if size_mb >= DELETE_EVENTS_ROTATE_MB:
                rot_path = DELETE_EVENTS_PATH.with_suffix(".jsonl.1")
                if rot_path.exists():
                    rot_path.unlink()
                DELETE_EVENTS_PATH.rename(rot_path)
        with open(DELETE_EVENTS_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        print(f"[PTB] 埋点写入失败: {e}")


def _cleanup_old_event_files() -> None:
    """清理超期归档文件"""
    if DELETE_EVENTS_RETAIN_DAYS <= 0:
        return
    try:
        base = DELETE_EVENTS_PATH.parent
        cutoff = time.time() - DELETE_EVENTS_RETAIN_DAYS * 86400
        for p in base.glob("delete_events.jsonl.*"):
            if p.stat().st_mtime < cutoff:
                p.unlink()
    except Exception as e:
        print(f"[PTB] 埋点归档清理失败: {e}")


def _next_attempt_no(chat_id: str, msg_id: int) -> int:
    """返回并递增 (chat_id, msg_id) 的尝试次数"""
    key = (chat_id, msg_id)
    _attempt_count[key] = _attempt_count.get(key, 0) + 1
    return _attempt_count[key]


def _clear_attempt_no(chat_id: str, msg_id: int) -> None:
    """删除成功后清理计数，避免内存泄漏"""
    _attempt_count.pop((chat_id, msg_id), None)


def _persist_append(chat_id: str, msg_id: int, user_id: int, ts: float):
    """将待删项追加到持久化文件"""
    try:
        with open(PENDING_DELETE_PERSIST_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({"chat_id": chat_id, "msg_id": msg_id, "user_id": user_id, "ts": ts}, ensure_ascii=False) + "\n")
        _emit_event("persist_append", chat_id=chat_id, msg_id=msg_id, user_id=user_id)
    except Exception as e:
        print(f"[PTB] 持久化待删队列失败: {e}")


def _persist_load() -> List[Tuple[str, int, int, float]]:
    """从持久化文件加载待删项"""
    if not PENDING_DELETE_PERSIST_PATH.exists():
        return []
    try:
        items = []
        with open(PENDING_DELETE_PERSIST_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    items.append((d["chat_id"], d["msg_id"], d.get("user_id", 0), d.get("ts", 0)))
                except (json.JSONDecodeError, KeyError):
                    pass
        return items
    except Exception as e:
        print(f"[PTB] 加载持久化待删队列失败: {e}")
        return []


def _persist_remove(chat_id: str, msg_id: int):
    """从持久化文件移除指定项"""
    try:
        items = []
        with open(PENDING_DELETE_PERSIST_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    if d.get("chat_id") != chat_id or d.get("msg_id") != msg_id:
                        items.append(line)
                except (json.JSONDecodeError, KeyError):
                    pass
        with open(PENDING_DELETE_PERSIST_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(items) + ("\n" if items else ""))
        _emit_event("persist_remove", chat_id=chat_id, msg_id=msg_id)
    except Exception as e:
        print(f"[PTB] 从持久化移除失败: {e}")


async def _add_pending_retry(bot: Any, chat_id: str, msg_id: int, user_id: int, log_prefix: str = "PTB"):
    """删除失败时加入待重试队列。队列满时溢出到持久化文件，永不放弃。"""
    global _pending_delete_retry, _delete_stats
    if any((c, m) == (chat_id, msg_id) for (c, m, u, t) in _pending_delete_retry):
        return
    now = time.time()
    cutoff = now - PENDING_DELETE_RETRY_TTL
    expired = [(c, m, u, t) for (c, m, u, t) in _pending_delete_retry if t <= cutoff]
    _pending_delete_retry[:] = [(c, m, u, t) for (c, m, u, t) in _pending_delete_retry if t > cutoff]
    for item in expired:
        _emit_event("queue_expire", chat_id=item[0], msg_id=item[1], user_id=item[2], reason="expire", queue_len=len(_pending_delete_retry))
        _persist_append(item[0], item[1], item[2], item[3])
    while len(_pending_delete_retry) >= PENDING_DELETE_RETRY_MAX:
        evicted = _pending_delete_retry.pop(0)
        _emit_event("queue_evict", chat_id=evicted[0], msg_id=evicted[1], user_id=evicted[2], reason="evict", queue_len=len(_pending_delete_retry))
        _persist_append(evicted[0], evicted[1], evicted[2], evicted[3])
        print(f"[{log_prefix}] 待删队列已满，溢出到持久化 chat_id={evicted[0]} msg_id={evicted[1]}")
    _emit_event("queue_enqueue", chat_id=chat_id, msg_id=msg_id, user_id=user_id, queue_len=len(_pending_delete_retry) + 1)
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
    cid_str = str(chat_id)
    for attempt in range(retries):
        attempt_no = _next_attempt_no(cid_str, msg_id)
        result = "fail"
        error_type = ""
        error_msg = ""
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            _delete_stats["immediate_success"] += 1
            result = "success"
            _emit_event(
                "delete_attempt",
                chat_id=cid_str,
                msg_id=msg_id,
                label=label,
                attempt_no=attempt_no,
                phase="immediate",
                result=result,
            )
            _clear_attempt_no(cid_str, msg_id)
            if cache_dict is not None and clear_cache_key is not None:
                cache_dict.pop(clear_cache_key, None)
            if on_success is not None:
                on_success()
            return True
        except Exception as e:
            error_type = type(e).__name__
            error_msg = str(e)[:200]
            not_found = "not found" in error_msg.lower()
            result = "not_found" if not_found else "fail"
            _emit_event(
                "delete_attempt",
                chat_id=cid_str,
                msg_id=msg_id,
                label=label,
                attempt_no=attempt_no,
                phase="immediate",
                result=result,
                error_type=error_type,
                error_msg=error_msg,
            )
            if not_found:
                _clear_attempt_no(cid_str, msg_id)
                return False
            if attempt < retries - 1:
                await asyncio.sleep(2)
            else:
                _log_failure(chat_id, msg_id, label, e, log_prefix)
                _delete_stats["immediate_fail"] += 1
    user_id = clear_cache_key[1] if clear_cache_key is not None else 0
    await _add_pending_retry(bot, cid_str, msg_id, user_id, log_prefix)
    return False


async def retry_pending_deletes_for_chat(bot: Any, chat_id: str, log_prefix: str = "PTB"):
    """同群触发：顺带重试该群待删除队列。单次最多重试 N 条。不限 TTL，永不放弃。"""
    global _pending_delete_retry, _delete_stats
    candidates = [(c, m, u, t) for (c, m, u, t) in _pending_delete_retry if c == chat_id]
    to_retry = candidates[:PENDING_DELETE_RETRY_PER_MSG]
    remaining_this_chat = candidates[PENDING_DELETE_RETRY_PER_MSG:]
    other_items = [(c, m, u, t) for (c, m, u, t) in _pending_delete_retry if c != chat_id]
    num_dequeued = 0
    for item in to_retry:
        cid, mid, _, _ = item
        attempt_no = _next_attempt_no(cid, mid)
        result = "fail"
        error_type = ""
        error_msg = ""
        try:
            await bot.delete_message(chat_id=int(cid), message_id=mid)
            _delete_stats["retry_success"] += 1
            result = "success"
            num_dequeued += 1
            _emit_event("delete_attempt", chat_id=cid, msg_id=mid, label="", attempt_no=attempt_no, phase="retry_mem", result=result)
            _emit_event("queue_dequeue", chat_id=cid, msg_id=mid, source="retry_chat", queue_len=len(_pending_delete_retry) - num_dequeued)
            _clear_attempt_no(cid, mid)
        except Exception as e:
            error_type = type(e).__name__
            error_msg = str(e)[:200]
            if "not found" in error_msg.lower():
                _delete_stats["retry_success"] += 1
                result = "not_found"
                num_dequeued += 1
                _emit_event("delete_attempt", chat_id=cid, msg_id=mid, label="", attempt_no=attempt_no, phase="retry_mem", result=result)
                _emit_event("queue_dequeue", chat_id=cid, msg_id=mid, source="retry_chat", queue_len=len(_pending_delete_retry) - num_dequeued)
                _clear_attempt_no(cid, mid)
            else:
                remaining_this_chat.append(item)
                _delete_stats["retry_fail"] += 1
                _emit_event("delete_attempt", chat_id=cid, msg_id=mid, label="", attempt_no=attempt_no, phase="retry_mem", result=result, error_type=error_type, error_msg=error_msg)
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
    """兜底：定时扫描内存队列 + 持久化队列，永不放弃。供 PTB job_queue 注册。"""
    global _pending_delete_retry, _delete_stats
    bot = context.bot
    batch_half = max(1, PENDING_DELETE_RETRY_JOB_BATCH // 2)
    to_retry = sorted(_pending_delete_retry, key=lambda x: x[3])[:batch_half]
    retried_ids = {(c, m) for c, m, _, _ in to_retry}
    remaining_mem = [(c, m, u, t) for (c, m, u, t) in _pending_delete_retry if (c, m) not in retried_ids]
    num_dequeued = 0
    for item in to_retry:
        cid, mid, _, _ = item
        attempt_no = _next_attempt_no(cid, mid)
        result = "fail"
        error_type = ""
        error_msg = ""
        try:
            await bot.delete_message(chat_id=int(cid), message_id=mid)
            _delete_stats["retry_success"] += 1
            result = "success"
            num_dequeued += 1
            _emit_event("delete_attempt", chat_id=cid, msg_id=mid, label="", attempt_no=attempt_no, phase="retry_mem", result=result)
            _emit_event("queue_dequeue", chat_id=cid, msg_id=mid, source="retry_mem", queue_len=len(_pending_delete_retry) - num_dequeued)
            _clear_attempt_no(cid, mid)
        except Exception as e:
            error_type = type(e).__name__
            error_msg = str(e)[:200]
            if "not found" in error_msg.lower():
                _delete_stats["retry_success"] += 1
                result = "not_found"
                num_dequeued += 1
                _emit_event("delete_attempt", chat_id=cid, msg_id=mid, label="", attempt_no=attempt_no, phase="retry_mem", result=result)
                _emit_event("queue_dequeue", chat_id=cid, msg_id=mid, source="retry_mem", queue_len=len(_pending_delete_retry) - num_dequeued)
                _clear_attempt_no(cid, mid)
            else:
                remaining_mem.append(item)
                _delete_stats["retry_fail"] += 1
                _emit_event("delete_attempt", chat_id=cid, msg_id=mid, label="", attempt_no=attempt_no, phase="retry_mem", result=result, error_type=error_type, error_msg=error_msg)
        await asyncio.sleep(1)
    _pending_delete_retry[:] = remaining_mem

    persist_items = _persist_load()
    _emit_event("persist_load", count=len(persist_items))
    to_retry_persist = persist_items[:PENDING_DELETE_PERSIST_BATCH]
    for item in to_retry_persist:
        cid, mid, _, _ = item
        attempt_no = _next_attempt_no(cid, mid)
        result = "fail"
        error_type = ""
        error_msg = ""
        try:
            await bot.delete_message(chat_id=int(cid), message_id=mid)
            _delete_stats["persist_retry_success"] += 1
            result = "success"
            _emit_event("delete_attempt", chat_id=cid, msg_id=mid, label="", attempt_no=attempt_no, phase="retry_persist", result=result)
            _persist_remove(cid, mid)
            _clear_attempt_no(cid, mid)
        except Exception as e:
            error_type = type(e).__name__
            error_msg = str(e)[:200]
            if "not found" in error_msg.lower():
                _delete_stats["persist_retry_success"] += 1
                result = "not_found"
                _emit_event("delete_attempt", chat_id=cid, msg_id=mid, label="", attempt_no=attempt_no, phase="retry_persist", result=result)
                _persist_remove(cid, mid)
                _clear_attempt_no(cid, mid)
            else:
                _delete_stats["persist_retry_fail"] += 1
                _emit_event("delete_attempt", chat_id=cid, msg_id=mid, label="", attempt_no=attempt_no, phase="retry_persist", result=result, error_type=error_type, error_msg=error_msg)
        await asyncio.sleep(1)
    _cleanup_old_event_files()


def get_stats() -> Dict[str, int]:
    """获取删除统计"""
    return dict(_delete_stats)


def get_pending_queue_len() -> int:
    """获取待重试队列长度（仅内存）"""
    return len(_pending_delete_retry)


def get_persist_queue_len() -> int:
    """获取持久化待删队列长度"""
    return len(_persist_load())
