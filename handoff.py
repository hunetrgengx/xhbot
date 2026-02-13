# -*- coding: utf-8 -*-
"""
霜刃 ↔ 小助理 双向转交机制
因 Telegram 不向机器人转发其他机器人消息，两 bot 无法直接互通。
改为：通过 handoff 文件请求对方代为发送。
使用 JSONL 队列结构，避免连续多次转交时后者覆盖前者导致丢失。
"""
import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# 使用 xhbot 项目根目录下的 handoff 文件
_XHBOT_ROOT = Path(__file__).resolve().parent
HANDOFF_FILE = _XHBOT_ROOT / "handoff_pending.jsonl"
FROST_REPLY_FILE = _XHBOT_ROOT / "handoff_frost_reply.jsonl"
_lock = threading.Lock()
_frost_lock = threading.Lock()


def put_handoff(chat_id: int, reply_to_message_id: int, question: str) -> bool:
    """
    写入转交请求（追加到队列）。霜刃在 AI 返回「小助理，你来回答」时调用。
    返回是否成功。
    """
    if not question or not question.strip():
        return False
    data = {
        "chat_id": chat_id,
        "reply_to_message_id": reply_to_message_id,
        "question": question.strip(),
    }
    try:
        with _lock:
            with open(HANDOFF_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False) + "\n")
        logger.info("handoff: 已追加 chat_id=%s reply_to=%s", chat_id, reply_to_message_id)
        return True
    except Exception as e:
        logger.warning("handoff: 写入失败 %s", e)
        return False


def take_handoff() -> dict | None:
    """
    取走队列头部的一个转交请求。小助理轮询时调用。
    返回 {"chat_id": int, "reply_to_message_id": int, "question": str} 或 None
    """
    if not HANDOFF_FILE.exists():
        return None
    try:
        with _lock:
            with open(HANDOFF_FILE, "r", encoding="utf-8") as f:
                lines = [ln.strip() for ln in f.readlines() if ln.strip()]
            if not lines:
                return None
            data = json.loads(lines[0])
            remaining = lines[1:]
            chat_id = data.get("chat_id")
            reply_to = data.get("reply_to_message_id")
            question = (data.get("question") or "").strip()
            if remaining:
                with open(HANDOFF_FILE, "w", encoding="utf-8") as f:
                    f.write("\n".join(remaining) + "\n")
            else:
                os.remove(HANDOFF_FILE)
            if chat_id is None or reply_to is None or not question:
                return None
            return {"chat_id": int(chat_id), "reply_to_message_id": int(reply_to), "question": question}
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning("handoff: 读取失败 %s", e)
        return None


def put_frost_reply_handoff(chat_id: int, reply_to_message_id: int) -> bool:
    """
    小助理→霜刃：小助理回复中含「霜刃」时调用，请求霜刃代为发送「......」。
    追加到队列，返回是否成功。
    """
    data = {"chat_id": chat_id, "reply_to_message_id": reply_to_message_id}
    try:
        with _frost_lock:
            with open(FROST_REPLY_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False) + "\n")
        logger.info("handoff_frost: 已追加 chat_id=%s reply_to=%s", chat_id, reply_to_message_id)
        return True
    except Exception as e:
        logger.warning("handoff_frost: 写入失败 %s", e)
        return False


def take_frost_reply_handoff() -> dict | None:
    """
    霜刃轮询调用，取走队列头部的小助理→霜刃转交请求。
    返回 {"chat_id": int, "reply_to_message_id": int} 或 None
    """
    if not FROST_REPLY_FILE.exists():
        return None
    try:
        with _frost_lock:
            with open(FROST_REPLY_FILE, "r", encoding="utf-8") as f:
                lines = [ln.strip() for ln in f.readlines() if ln.strip()]
            if not lines:
                return None
            data = json.loads(lines[0])
            remaining = lines[1:]
            chat_id = data.get("chat_id")
            reply_to = data.get("reply_to_message_id")
            if remaining:
                with open(FROST_REPLY_FILE, "w", encoding="utf-8") as f:
                    f.write("\n".join(remaining) + "\n")
            else:
                os.remove(FROST_REPLY_FILE)
            if chat_id is None or reply_to is None:
                return None
            return {"chat_id": int(chat_id), "reply_to_message_id": int(reply_to)}
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning("handoff_frost: 读取失败 %s", e)
        try:
            os.remove(FROST_REPLY_FILE)
        except Exception:
            pass
        return None
