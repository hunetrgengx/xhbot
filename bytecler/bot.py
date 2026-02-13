#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bytecler - Telegram 群消息监控与垃圾过滤机器人

功能：广告检测、垃圾关键词过滤、人机验证（简介含 tg/@ 的用户）、私聊管理关键词
"""

import asyncio
import json
import os
import random
import re
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

try:
    import ahocorasick
    AHOCORASICK_AVAILABLE = True
except ImportError:
    AHOCORASICK_AVAILABLE = False

# 路径：Windows 用绝对路径，Ubuntu 用相对路径（以 bytecler 目录为基准）
_BYTECLER_DIR = Path(__file__).resolve().parent if sys.platform == "win32" else None

def _path(name: str) -> Path:
    if sys.platform == "win32" and _BYTECLER_DIR:
        return _BYTECLER_DIR / name
    return Path(name)

try:
    from dotenv import load_dotenv
    load_dotenv()
    # 尝试加载 xhchat 的 .env 以获取 OPENAI_API_KEY（bytecler 未配置时使用）
    _xhchat_env = Path(__file__).resolve().parent.parent / "xhchat" / ".env"
    if _xhchat_env.exists():
        load_dotenv(dotenv_path=_xhchat_env, override=False)
except ImportError:
    pass

# AI (KIMI) - 使用 xhchat 的 token
KIMI_API_KEY = os.getenv("OPENAI_API_KEY", "")
KIMI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.moonshot.cn/v1")
KIMI_MODEL = os.getenv("MODEL_NAME", "moonshot-v1-128k")

# 小助理 bot 的 @用户名（用于检测小助理提到霜刃时回复）
XHCHAT_BOT_USERNAME = (os.getenv("XHCHAT_BOT_USERNAME") or os.getenv("BOT_USERNAME") or "").strip().lstrip("@")

from telethon import TelegramClient, events, Button
from telethon.errors import FloodWaitError
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.functions.messages import SetTypingRequest
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import SendMessageTypingAction
from telethon.tl.types import BotCommand, BotCommandScopeDefault
from telethon.tl.types import PeerChannel, PeerChat
from telethon.tl.types import (
    MessageMediaPhoto,
    MessageMediaDocument,
    MessageMediaContact,
    MessageMediaGeo,
    MessageMediaPoll,
    MessageMediaWebPage,
    MessageMediaDice,
    UpdateChannelParticipant,
    ChannelParticipantBanned,
)

# 配置
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GROUP_ID_STR = os.getenv("GROUP_ID", "")
TARGET_GROUP_IDS = {s.strip() for s in GROUP_ID_STR.split(",") if s.strip()}
SPAM_KEYWORDS_PATH = _path("spam_keywords.json")
VERIFIED_USERS_PATH = _path("verified_users.json")
VERIFICATION_FAILURES_PATH = _path("verification_failures.json")
VERIFICATION_BLACKLIST_PATH = _path("verification_blacklist.json")  # 曾 5 次失败或验证超时被限制的用户
BIO_CALLS_LOG_PATH = _path("bio_calls.jsonl")  # 每次调用 bio 接口后追加一条记录
VERIFY_TIMEOUT = 90  # 验证码有效期（秒）
VERIFY_MSG_DELETE_AFTER = 30  # 验证相关消息保留多久后自动删除（秒）
VERIFY_FAIL_THRESHOLD = 5  # 验证失败次数阈值，达到则限制
VERIFY_FAILURES_RETENTION_SECONDS = 86400  # 单次验证失败记录保留时间（秒），1 天
VERIFY_RESTRICT_DURATION = 1  # 限制时长（天），0=永久
UNBAN_BOT_USERNAME = os.getenv("UNBAN_BOT_USERNAME", "@XHNPBOT")
VERBOSE = os.getenv("TG_VERBOSE", "").lower() in ("1", "true", "yes")
ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = {int(x.strip()) for x in ADMIN_IDS_STR.split(",") if x.strip().isdigit()}
# 抽奖白名单同步（服务器路径，通过 LOTTERY_DB_PATH 环境变量配置）
LOTTERY_DB_PATH = os.getenv("LOTTERY_DB_PATH", "/tgbot/cjbot/cjdb/lottery.db")
SYNC_LOTTERY_CHECKPOINT_PATH = _path("sync_lottery_checkpoint.json")
SYNC_LOTTERY_HOUR = int(os.getenv("SYNC_LOTTERY_HOUR", "20"))  # UTC 20:00 = 北京凌晨 4:00
# 垃圾关键词：三个字段各自独立配置
# {"text": {"exact": [], "match": [], "_ac": automaton, "_regex": []}, "name": {...}, "bio": {...}}
# _ac: Aho-Corasick 自动机（用于 match 子串）；_regex: 预编译正则列表
spam_keywords = {"text": {"exact": [], "match": []}, "name": {"exact": [], "match": []}, "bio": {"exact": [], "match": []}}

# 人机验证：简介含 tg 链接、http 链接或 @ 的用户需验证
TG_LINK_PATTERN = re.compile(r"(?:t\.me|telegram\.me|telegram\.dog)/[\w]+", re.I)
BIO_HTTP_PATTERN = re.compile(r"https?://\S+", re.I)  # http:// 或 https://
BIO_AT_PATTERN = re.compile(r"@\w+")  # @用户名

verified_users = set()  # {user_id} 白名单，按用户不按群
verified_users_details = {}  # {user_id: {user_id, username, full_name, join_time, verify_time}}
join_times = {}  # {user_id: "ISO8601"} 入群时间（任一配置群）
# 验证失败记录：{(chat_id, user_id): {"count": int, "first_ts": float}}，按群记录，超过保留期视为 0
verification_failures = {}
verification_blacklist = set()  # {user_id} 黑名单，按用户不按群
pending_by_user = {}  # {(chat_id, user_id): {"code": ..., "time": ...}} 待验证（仍按群+用户，因验证发生在某群）

API_ID = 6
API_HASH = "eb06d4abfb49dc3eeb1aeb98ae0f581e"
SESSION_NAME = "bytecler_bot"

# 关键词管理模式：连续输入切换添加/删除，直到 /cancel 或 /start
PENDING_KEYWORD_TIMEOUT = 300
pending_keyword_cmd = {}  # user_id: {"field": "text"|"name"|"bio", "time": timestamp}

# sender_bio 缓存，减少 GetFullUserRequest 调用
BIO_CACHE_TTL = 86400  # 秒（24小时）
bio_cache = {}  # user_id: (bio, expire_time)
# bio 接口限流：调用一次后 60s 内不再调用，多请求排队等待
BIO_CALL_INTERVAL = 60  # 秒
_last_bio_call_time = 0.0
bio_call_lock = asyncio.Lock()

def get_message_type(msg) -> str:
    """获取消息类型"""
    if not msg or not msg.media:
        return "text" if (msg and (msg.text or msg.message)) else "unknown"
    if isinstance(msg.media, MessageMediaPhoto):
        return "photo"
    if isinstance(msg.media, MessageMediaWebPage):
        return "webpage"
    if isinstance(msg.media, MessageMediaDocument):
        doc = msg.media.document
        if doc and doc.attributes:
            for attr in doc.attributes:
                k = type(attr).__name__
                if "Video" in k:
                    return "video"
                if "Audio" in k or "Voice" in k:
                    return "audio"
                if "Sticker" in k:
                    return "sticker"
                if "Animated" in k:
                    return "gif"
        return "document"
    if isinstance(msg.media, MessageMediaContact):
        return "contact"
    if isinstance(msg.media, MessageMediaGeo):
        return "location"
    if isinstance(msg.media, MessageMediaPoll):
        return "poll"
    if isinstance(msg.media, MessageMediaDice):
        return "dice"
    return "media"


def _chat_allowed(chat_id: str) -> bool:
    return bool(TARGET_GROUP_IDS and str(chat_id) in TARGET_GROUP_IDS)


def _get_full_name(sender) -> str:
    """从 sender 提取完整昵称"""
    if not sender:
        return "用户"
    fn = (getattr(sender, "first_name", None) or "").strip()
    ln = (getattr(sender, "last_name", None) or "").strip()
    return (fn + " " + ln).strip() or "用户"


def _get_sender_display(sender) -> str:
    """从 sender 提取用于日志的显示标识（username 或 id），sender 可为 None"""
    if not sender:
        return "?"
    return getattr(sender, "username", None) or getattr(sender, "id", "?")


def _is_admin(user_id: int) -> bool:
    """检查是否为管理员（可修改关键词）。未配置 ADMIN_IDS 时所有人可操作"""
    if not ADMIN_IDS:
        return True
    return user_id in ADMIN_IDS


def _bio_needs_verification(bio: Optional[str]) -> bool:
    """简介是否含 tg 链接、http 链接或 @"""
    if not bio:
        return False
    return bool(
        TG_LINK_PATTERN.search(bio) or BIO_HTTP_PATTERN.search(bio) or BIO_AT_PATTERN.search(bio)
    )


def _load_verified_users():
    """加载已通过验证用户及详情（按用户不按群，多群通用）"""
    global verified_users, verified_users_details, join_times
    if not VERIFIED_USERS_PATH.exists():
        return
    try:
        with open(VERIFIED_USERS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        raw_users = data.get("users") or []
        raw_details = dict(data.get("details", {}) or {})
        raw_join_times = dict(data.get("join_times", {}) or {})
        verified_users = set()
        for u in raw_users:
            if isinstance(u, int):
                verified_users.add(u)
            elif isinstance(u, str):
                if u.isdigit():
                    verified_users.add(int(u))
                else:
                    parts = u.split(":", 1)
                    if len(parts) == 2 and parts[1].isdigit():
                        verified_users.add(int(parts[1]))
        verified_users_details = {}
        join_times = {}
        for k, v in raw_details.items():
            uid = int(k) if (isinstance(k, str) and k.isdigit()) else (int(k.split(":", 1)[1]) if ":" in str(k) else None)
            if uid is not None and isinstance(v, dict):
                verified_users_details[uid] = {
                    "user_id": uid,
                    "username": v.get("username"),
                    "full_name": v.get("full_name") or "用户",
                    "join_time": raw_join_times.get(k) or raw_join_times.get(str(uid)),
                    "verify_time": v.get("verify_time"),
                }
        for k, t in raw_join_times.items():
            uid = int(k) if (isinstance(k, str) and k.isdigit()) else (int(k.split(":", 1)[1]) if ":" in str(k) else None)
            if uid is not None and (uid not in join_times or (t and (not join_times[uid] or t > join_times[uid]))):
                join_times[uid] = t
        for uid in verified_users:
            if uid not in verified_users_details:
                verified_users_details[uid] = {
                    "user_id": uid,
                    "username": None,
                    "full_name": "用户",
                    "join_time": join_times.get(uid),
                    "verify_time": None,
                }
    except Exception as e:
        print(f"加载已验证用户失败: {e}")


def _get_verification_failures_count(chat_id: str, user_id: int) -> int:
    """获取当前有效失败次数（按群+用户），超过保留期视为 0 并清理"""
    key = (chat_id, user_id)
    if key not in verification_failures:
        return 0
    ent = verification_failures[key]
    now = time.time()
    if now - ent["first_ts"] > VERIFY_FAILURES_RETENTION_SECONDS:
        verification_failures.pop(key, None)
        return 0
    return ent["count"]


def _increment_verification_failures(chat_id: str, user_id: int) -> int:
    """失败次数 +1（按群+用户），若为新 key 或已过期则从 1 开始；返回当前次数"""
    key = (chat_id, user_id)
    now = time.time()
    if key not in verification_failures:
        verification_failures[key] = {"count": 1, "first_ts": now}
        return 1
    ent = verification_failures[key]
    if now - ent["first_ts"] > VERIFY_FAILURES_RETENTION_SECONDS:
        verification_failures[key] = {"count": 1, "first_ts": now}
        return 1
    ent["count"] += 1
    return ent["count"]


def _load_verification_failures():
    """加载验证失败计数（仅加载未过期的，超过保留期不加载）"""
    global verification_failures
    if not VERIFICATION_FAILURES_PATH.exists():
        return
    try:
        with open(VERIFICATION_FAILURES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"加载验证失败计数失败: {e}")
        return
    now = time.time()
    failures = data.get("failures") or {}
    for k, v in failures.items():
        parts = k.split(":", 1) if isinstance(k, str) else []
        if len(parts) != 2 or not parts[1].isdigit():
            continue
        key = (parts[0], int(parts[1]))
        if isinstance(v, dict) and "count" in v and "first_ts" in v:
            first_ts = v["first_ts"]
            if now - first_ts <= VERIFY_FAILURES_RETENTION_SECONDS:
                verification_failures[key] = {"count": int(v["count"]), "first_ts": first_ts}
        else:
            verification_failures[key] = {"count": int(v), "first_ts": now}


def _save_verification_failures():
    """保存验证失败计数（按群+用户，仅保存未过期的）"""
    try:
        now = time.time()
        to_save = {}
        for (cid, uid), ent in verification_failures.items():
            if now - ent["first_ts"] <= VERIFY_FAILURES_RETENTION_SECONDS:
                to_save[f"{cid}:{uid}"] = {"count": ent["count"], "first_ts": ent["first_ts"]}
        data = {"failures": to_save}
        with open(VERIFICATION_FAILURES_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        print(f"保存验证失败计数失败: {e}")


def _load_verification_blacklist():
    """加载验证黑名单（按用户，曾 5 次失败或验证超时被限制的用户）"""
    global verification_blacklist
    if not VERIFICATION_BLACKLIST_PATH.exists():
        return
    try:
        with open(VERIFICATION_BLACKLIST_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("users") or []
        verification_blacklist = set()
        for u in raw:
            if isinstance(u, int):
                verification_blacklist.add(u)
            elif isinstance(u, str):
                if u.isdigit():
                    verification_blacklist.add(int(u))
                elif ":" in u:
                    parts = u.split(":", 1)
                    if parts[1].isdigit():
                        verification_blacklist.add(int(parts[1]))
    except Exception as e:
        print(f"加载验证黑名单失败: {e}")


def _save_verification_blacklist():
    """保存验证黑名单（按用户）"""
    try:
        data = {"users": list(verification_blacklist)}
        with open(VERIFICATION_BLACKLIST_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        print(f"保存验证黑名单失败: {e}")


def _save_verified_users():
    """保存已通过验证用户及详情（按用户，JSON 的 key 用 str(user_id)）"""
    try:
        details_out = {str(uid): v for uid, v in verified_users_details.items()}
        join_times_out = {str(uid): t for uid, t in join_times.items()}
        data = {
            "users": list(verified_users),
            "details": details_out,
            "join_times": join_times_out,
        }
        with open(VERIFIED_USERS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存已验证用户失败: {e}")


def _sync_lottery_to_verified(full: bool = False) -> tuple[bool, int, str]:
    """
    从抽奖数据库同步到霜刃白名单。
    full=True 时全量对比数据库并增量写入白名单；full=False 时按 checkpoint 增量同步。
    只读打开源数据库，不写入、不阻塞、不损坏源库；当日失败可接受，漏数据问题不大。
    返回 (success, new_count, error_msg)
    """
    last_sync = "1970-01-01T00:00:00Z"
    if not full and SYNC_LOTTERY_CHECKPOINT_PATH.exists():
        try:
            with open(SYNC_LOTTERY_CHECKPOINT_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            last_sync = str(cfg.get("last_sync_time", last_sync))
        except Exception:
            pass
    try:
        uri = Path(LOTTERY_DB_PATH).resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        cur = conn.execute(
            "SELECT user_id, username, full_name, join_time FROM user_participations WHERE join_time > ? ORDER BY join_time",
            (last_sync,),
        )
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        import traceback
        traceback.print_exc()
        return False, 0, str(e)
    if not rows:
        print(f"[抽奖同步] 数据库查询到 0 条记录 (last_sync={last_sync})，请确认表 user_participations 及 join_time 字段")
    users_set = set()
    details = {}
    join_times_out = {}
    if VERIFIED_USERS_PATH.exists():
        try:
            with open(VERIFIED_USERS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            for u in data.get("users") or []:
                uid = int(u) if isinstance(u, (int, str)) and str(u).isdigit() else None
                if uid is not None:
                    users_set.add(uid)
            raw_details = dict(data.get("details", {}) or {})
            raw_join = dict(data.get("join_times", {}) or {})
            for k, v in raw_details.items():
                uid = int(k) if (isinstance(k, str) and k.isdigit()) else (int(k.split(":", 1)[1]) if ":" in str(k) else None)
                if uid is not None and isinstance(v, dict):
                    details[uid] = {
                        "user_id": uid,
                        "username": v.get("username"),
                        "full_name": v.get("full_name") or "用户",
                        "join_time": raw_join.get(k) or raw_join.get(str(uid)),
                        "verify_time": v.get("verify_time"),
                    }
            for k, t in raw_join.items():
                uid = int(k) if (isinstance(k, str) and k.isdigit()) else (int(k.split(":", 1)[1]) if ":" in str(k) else None)
                if uid is not None and t:
                    join_times_out[uid] = t
        except Exception as e:
            return False, 0, str(e)
    new_count = 0
    max_join_time = last_sync
    for row in rows:
        uid = int(row[0]) if row[0] is not None else None
        if uid is None:
            continue
        username = row[1] if row[1] else None
        full_name = (row[2] or "").strip() or "用户"
        jt = (row[3] or "").strip() if row[3] else None
        if uid not in users_set:
            users_set.add(uid)
            details[uid] = {
                "user_id": uid,
                "username": username,
                "full_name": full_name,
                "join_time": jt,
                "verify_time": None,
            }
            if jt:
                join_times_out[uid] = jt
            new_count += 1
        if jt and jt > max_join_time:
            max_join_time = jt
    # 写入前再次读取文件，合并期间新增的用户（避免覆盖验证通过的用户）
    if VERIFIED_USERS_PATH.exists():
        try:
            with open(VERIFIED_USERS_PATH, "r", encoding="utf-8") as f:
                fresh = json.load(f)
            for u in fresh.get("users") or []:
                uid = int(u) if isinstance(u, (int, str)) and str(u).isdigit() else None
                if uid is not None and uid not in users_set:
                    users_set.add(uid)
                    raw_d = dict(fresh.get("details", {}) or {})
                    raw_j = dict(fresh.get("join_times", {}) or {})
                    k = str(uid)
                    v = raw_d.get(k) or raw_d.get(str(uid))
                    if isinstance(v, dict):
                        details[uid] = {
                            "user_id": uid,
                            "username": v.get("username"),
                            "full_name": v.get("full_name") or "用户",
                            "join_time": raw_j.get(k) or raw_j.get(str(uid)),
                            "verify_time": v.get("verify_time"),
                        }
                    if raw_j.get(k) or raw_j.get(str(uid)):
                        join_times_out[uid] = raw_j.get(k) or raw_j.get(str(uid))
        except Exception:
            pass
    data_out = {
        "users": list(users_set),
        "details": {str(uid): v for uid, v in details.items()},
        "join_times": {str(uid): t for uid, t in join_times_out.items()},
    }
    try:
        write_dir = Path(VERIFIED_USERS_PATH).resolve().parent
        fd, tmp = tempfile.mkstemp(dir=write_dir, prefix="verified_users.", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data_out, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(Path(VERIFIED_USERS_PATH).resolve()))
    except Exception as e:
        import traceback
        print(f"[抽奖同步] 写入白名单失败 path={Path(VERIFIED_USERS_PATH).resolve()}")
        traceback.print_exc()
        return False, 0, str(e)
    try:
        with open(SYNC_LOTTERY_CHECKPOINT_PATH, "w", encoding="utf-8") as f:
            json.dump({"last_sync_time": max_join_time}, f, ensure_ascii=False)
    except Exception:
        pass
    return True, new_count, ""


def _add_verified_user(
    user_id: int,
    username: Optional[str] = None,
    full_name: Optional[str] = None,
    verify_time: Optional[str] = None,
):
    """添加已验证用户并记录详情（按用户，多群通用）"""
    now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    verified_users.add(user_id)
    verified_users_details[user_id] = {
        "user_id": user_id,
        "username": username or None,
        "full_name": full_name or "用户",
        "join_time": join_times.get(user_id),
        "verify_time": verify_time or now_iso,
    }


def _clean_expired_verifications():
    """清理过期的待验证，返回 [(chat_id, user_id), ...] 供限制处理"""
    now = time.time()
    expired = []
    for (cid, uid), v in list(pending_by_user.items()):
        if now - v["time"] > VERIFY_TIMEOUT:
            expired.append((cid, uid))
            pending_by_user.pop((cid, uid), None)
    return expired


async def _delete_msg_after(client, chat_id: int, msg_ids, seconds: int = VERIFY_MSG_DELETE_AFTER):
    """延时删除消息（用于验证相关消息），msg_ids 可为 int 或 list"""
    await asyncio.sleep(seconds)
    try:
        ids = [msg_ids] if isinstance(msg_ids, int) else msg_ids
        await client.delete_messages(chat_id, ids)
    except Exception:
        pass


async def _restrict_user_and_notify(client, chat_id: str, user_id: int, full_name: Optional[str] = None):
    """限制用户发送消息和媒体权限，并发送解封指引（30s后删除）

    注意：Telethon 的 edit_permissions 中，False=限制，True=不限制。
    仅适用于超级群（supergroup），普通群会抛出 ValueError。
    """
    until = datetime.utcnow() + timedelta(days=VERIFY_RESTRICT_DURATION) if VERIFY_RESTRICT_DURATION > 0 else None
    if full_name is None:
        try:
            user = await client.get_entity(user_id)
            full_name = _get_full_name(user)
        except Exception:
            full_name = "用户"
    try:
        await client.edit_permissions(
            int(chat_id),
            user_id,
            until_date=until,
            send_messages=False,  # False = 限制发送消息
            send_media=False,     # False = 限制发送媒体
        )
        verification_failures.pop((chat_id, user_id), None)  # 仅清理当前群失败计数
        for k in list(pending_by_user):
            if k[1] == user_id:
                pending_by_user.pop(k, None)
        verification_blacklist.add(user_id)  # 进入黑名单（按用户，多群通用）
        _save_verification_failures()
        _save_verification_blacklist()
        if VERBOSE:
            print(f"[限制用户成功] 群 {chat_id} 用户 {user_id} 已被限制发言")
    except ValueError as e:
        if "channel or a supergroup" in str(e):
            print(f"[限制用户失败] 群 {chat_id} 不是超级群，请将群升级为超级群后重试: {e}")
        else:
            print(f"[限制用户失败] {chat_id} {user_id}: {e}")
        return
    except Exception as e:
        print(f"[限制用户失败] {chat_id} {user_id}: {e}")
        return
    try:
        msg = await client.send_message(
            int(chat_id),
            f"【{full_name}】\n\n验证失败，如有需要，请联系 {UNBAN_BOT_USERNAME} 进行解封",
        )
        asyncio.create_task(_delete_msg_after(client, int(chat_id), msg.id))
    except Exception as e:
        print(f"[发送限制说明失败] {e}")


async def _start_verification(client, event, chat_id: str, intro_line: str, label: str) -> None:
    """统一人机验证入口：删消息、发验证码、加入待验证、定时删验证消息。intro_line 为提示首段（含原因）。"""
    code = str(random.randint(1000, 9999))
    try:
        await event.delete()
        full_name = _get_full_name(event.sender)
        vmsg = await event.respond(
            f"【{full_name}】\n\n{intro_line}\n\n"
            f"👉 您的验证码是： <code>{code}</code>\n\n"
            f"直接发送上述验证码即可通过（{VERIFY_TIMEOUT}秒内有效）",
            parse_mode="html",
        )
        now = time.time()
        pending_by_user[(chat_id, event.sender.id)] = {"code": code, "time": now}
        asyncio.create_task(_delete_msg_after(client, int(chat_id), vmsg.id))
        print(f"[人机验证] 群{chat_id} | {_get_sender_display(event.sender)} | 待验证({label})")
    except Exception as e:
        print(f"[人机验证失败] {e}")


async def _handle_verification_result(client, event, chat_id: str, user_id: int, code: str, ok: bool):
    """处理验证结果（通过或失败）。调用方需确保 event.sender.id == user_id"""
    if ok:
        username = getattr(event.sender, "username", None) if event.sender else None
        full_name = _get_full_name(event.sender)
        _add_verified_user(user_id, username=username, full_name=full_name)
        verification_failures.pop((chat_id, user_id), None)  # 清理当前群失败计数
        verification_blacklist.discard(user_id)  # 验证通过则移出黑名单
        _save_verified_users()
        _save_verification_failures()
        _save_verification_blacklist()
        pending_by_user.pop((chat_id, user_id), None)
        try:
            succ_msg = await event.reply(
                f"【{full_name}】\n\n"
                "✓ 验证通过\n\n"
                "已将您加入白名单，可以正常发言了。"
            )
            asyncio.create_task(_delete_msg_after(client, int(chat_id), [event.message.id, succ_msg.id]))
        except Exception:
            pass
    else:
        count = _increment_verification_failures(chat_id, user_id)
        _save_verification_failures()
        try:
            await event.delete()
            if count >= VERIFY_FAIL_THRESHOLD:
                await _restrict_user_and_notify(client, chat_id, user_id, _get_full_name(event.sender))
            else:
                left = VERIFY_FAIL_THRESHOLD - count
                fail_msg = await event.respond(
                    f"【{_get_full_name(event.sender)}】\n\n验证失败，正确验证码为 <code>{code}</code>。再失败 {left} 次将被限制发言",
                    parse_mode="html",
                )
                asyncio.create_task(_delete_msg_after(client, int(chat_id), [fail_msg.id]))
        except Exception:
            pass


def _parse_field_keywords(cfg: dict) -> tuple:
    """解析单个字段的 exact/match 配置，返回 (exact_list, match_list)"""
    exact = [s.strip() for s in (cfg.get("exact") or []) if s and s.strip()]
    match_raw = [s.strip() for s in (cfg.get("match") or []) if s and s.strip()]
    match_list = []
    for s in match_raw:
        if s.startswith("/") and s.endswith("/") and len(s) > 2:
            match_list.append(("regex", re.compile(s[1:-1], re.I)))
        else:
            match_list.append(("str", s.lower()))
    return exact, match_list


def _build_ahocorasick_automaton(match_list: list) -> "ahocorasick.Automaton|None":
    """从 match 列表中的子串关键词构建 Aho-Corasick 自动机，正则类型跳过"""
    if not AHOCORASICK_AVAILABLE:
        return None
    str_keywords = [item[1] for item in match_list if item[0] == "str" and item[1]]
    if not str_keywords:
        return None
    automaton = ahocorasick.Automaton()
    for kw in str_keywords:
        kw_lower = kw.lower()
        automaton.add_word(kw_lower, kw_lower)
    automaton.make_automaton()
    return automaton


def _load_spam_keywords():
    """加载垃圾关键词配置，三个字段各自独立，并为 match 子串构建 Aho-Corasick 自动机"""
    global spam_keywords
    if not SPAM_KEYWORDS_PATH.exists():
        return
    try:
        with open(SPAM_KEYWORDS_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        for field in ("text", "name", "bio"):
            field_cfg = cfg.get(field) or {}
            exact_list, match_list = _parse_field_keywords(field_cfg)
            spam_keywords[field]["exact"] = exact_list
            spam_keywords[field]["match"] = match_list
            spam_keywords[field]["_ac"] = _build_ahocorasick_automaton(match_list)
            spam_keywords[field]["_regex"] = [item[1] for item in match_list if item[0] == "regex"]
    except Exception as e:
        print(f"加载垃圾关键词失败: {e}")


def _save_spam_keywords():
    """保存垃圾关键词到文件"""
    cfg = {}
    for field in ("text", "name", "bio"):
        kw = spam_keywords.get(field) or {}
        exact_list = kw.get("exact") or []
        match_list = kw.get("match") or []
        match_str = []
        for item in match_list:
            if item[0] == "str":
                match_str.append(item[1])
            else:
                match_str.append(f"/{item[1].pattern}/")
        cfg[field] = {"exact": exact_list, "match": match_str}
    try:
        with open(SPAM_KEYWORDS_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存垃圾关键词失败: {e}")


def _log_bio_call(user_id: int, full_name: str, bio: Optional[str]) -> None:
    """将一次 bio 接口调用记录追加到 bio_calls.jsonl"""
    try:
        record = {
            "time": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "user_id": user_id,
            "full_name": full_name or "",
            "bio": bio if bio is not None else "",
        }
        with open(BIO_CALLS_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[bio 调用记录失败] {e}")


async def _get_sender_bio_cached(client, user_id: int) -> Optional[str]:
    """获取用户简介，带 24 小时缓存；未命中时排队，两次调用间隔至少 60 秒；遇 FloodWait 按 retry_after 重试"""
    global _last_bio_call_time
    now = time.time()
    if user_id in bio_cache:
        cached_bio, expire = bio_cache[user_id]
        if now < expire:
            return cached_bio
        bio_cache.pop(user_id, None)
    try:
        async with bio_call_lock:
            # 等待期间可能已有其他协程为该用户填了缓存，入锁后再次检查
            now = time.time()
            if user_id in bio_cache:
                cached_bio, expire = bio_cache[user_id]
                if now < expire:
                    return cached_bio
                bio_cache.pop(user_id, None)
            # 距上次调用至少 60 秒，否则等待
            elapsed = now - _last_bio_call_time
            if elapsed < BIO_CALL_INTERVAL:
                wait = BIO_CALL_INTERVAL - elapsed
                if VERBOSE:
                    print(f"[bio 限流] 等待 {wait:.1f}s 后调用")
                await asyncio.sleep(wait)
            # 等待后再次检查缓存（前一个排队者可能已为该用户写入）
            now = time.time()
            if user_id in bio_cache:
                cached_bio, expire = bio_cache[user_id]
                if now < expire:
                    return cached_bio
                bio_cache.pop(user_id, None)
            # get_entity + GetFullUserRequest，遇 FloodWait (429) 按 retry_after 等待后重试
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    user_entity = await client.get_entity(user_id)
                    full = await client(GetFullUserRequest(user_entity))
                    break
                except FloodWaitError as e:
                    wait_sec = getattr(e, "seconds", 60) or 60
                    if attempt < max_retries - 1:
                        if VERBOSE:
                            print(f"[bio FloodWait] 等待 {wait_sec}s 后重试 ({attempt + 1}/{max_retries})")
                        await asyncio.sleep(wait_sec)
                    else:
                        if VERBOSE:
                            print(f"[bio FloodWait] 重试 {max_retries} 次后仍限流: {e}")
                        return None
            else:
                return None
            if full and getattr(full, "full_user", None):
                bio = (getattr(full.full_user, "about", None) or "").strip() or None
            else:
                bio = None
            # 更新上次调用时间（在 lock 内，保证下一个排队者看到）
            _last_bio_call_time = time.time()
            full_name = (
                (getattr(user_entity, "first_name", None) or "").strip()
                + " "
                + (getattr(user_entity, "last_name", None) or "").strip()
            ).strip() or ""
            _log_bio_call(user_id, full_name, bio)
        bio_cache[user_id] = (bio, time.time() + BIO_CACHE_TTL)
        return bio
    except Exception as e:
        if VERBOSE:
            print(f"[获取简介失败] {e}")
        return None


def _check_field_spam(kw_cfg: dict, value: str) -> Optional[str]:
    """检查单个字段是否命中关键词（exact / Aho-Corasick 子串 / 正则）"""
    if not value:
        return None
    value_lower = value.lower()
    exact_list = kw_cfg.get("exact") or []
    for kw in exact_list:
        if value_lower == kw.lower():
            return kw
    ac = kw_cfg.get("_ac")
    if ac is not None:
        for _end, matched in ac.iter(value_lower):
            return matched
    else:
        # pyahocorasick 未安装时回退到子串遍历
        for item in kw_cfg.get("match") or []:
            if item[0] == "str" and item[1] in value_lower:
                return item[1]
    for regex in kw_cfg.get("_regex") or []:
        if regex.search(value):
            return regex.pattern
    return None


def _check_spam(text: str, first_name: str, last_name: str, sender_bio: Optional[str]) -> Optional[str]:
    """
    检查是否命中垃圾关键词。三个字段各自独立配置关键词：
    - text: 消息文本
    - name: first_name + last_name 组合
    - bio: 简介
    每个字段只检查自己的 exact/match，返回命中的关键词。
    子串匹配使用 Aho-Corasick 算法，关键词数量增加时性能稳定。
    """
    msg_text = (text or "").strip()
    full_name = ((first_name or "").strip() + " " + (last_name or "").strip()).strip()
    bio = (sender_bio or "").strip()
    field_values = {"text": msg_text, "name": full_name, "bio": bio}
    for field, value in field_values.items():
        kw_cfg = spam_keywords.get(field) or {}
        hit = _check_field_spam(kw_cfg, value)
        if hit:
            return hit
    return None


def _check_spam_name_bio(first_name: str, last_name: str, sender_bio: Optional[str]) -> Optional[str]:
    """
    检查 name 或 bio 是否命中垃圾关键词（用于人机验证）。
    返回命中的关键词，如果未命中则返回 None。使用 Aho-Corasick 做子串匹配。
    """
    full_name = ((first_name or "").strip() + " " + (last_name or "").strip()).strip()
    bio = (sender_bio or "").strip()
    for field, value in (("name", full_name), ("bio", bio)):
        kw_cfg = spam_keywords.get(field) or {}
        hit = _check_field_spam(kw_cfg, value)
        if hit:
            return hit
    return None


def _check_ai_trigger(
    text: str,
    reply_to_msg,
    bot_id: int,
    bot_username: Optional[str],
) -> tuple[bool, str, Optional[str]]:
    """
    检查是否触发 AI 回复。
    唤醒：霜刃，/验证官，/@机器人/回复机器人
    返回 (是否触发, 用户问题, 被回复的机器人消息文本或 None)
    """
    t = (text or "").strip()
    if not t:
        return False, "", None
    query = ""
    replied_bot_text: Optional[str] = None
    # 霜刃，
    if t.startswith("霜刃，"):
        query = t[3:].strip()
    # 验证官，
    elif t.startswith("验证官，"):
        query = t[4:].strip()
    # @提及
    elif bot_username and f"@{bot_username}".lower() in t.lower():
        query = re.sub(rf"@{re.escape(bot_username)}\s*", "", t, flags=re.IGNORECASE).strip()
    # 回复机器人：将被回复的机器人消息内容一并送入 KIMI
    elif reply_to_msg and getattr(reply_to_msg, "sender_id", None) == bot_id:
        query = t
        replied_bot_text = (reply_to_msg.text or reply_to_msg.message or "").strip() or None
    else:
        return False, "", None
    if not query:
        query = "你好，有什么可以帮你的？"
    return True, query, replied_bot_text


async def _call_kimi_single_turn(user_message: str, replied_bot_text: Optional[str] = None) -> str:
    """单轮调用 KIMI，无历史。若 replied_bot_text 存在则一并送入"""
    if not KIMI_API_KEY:
        return "未配置 AI（OPENAI_API_KEY）"
    try:
        from openai import OpenAI
        client = OpenAI(api_key=KIMI_API_KEY, base_url=KIMI_BASE_URL)
        content = user_message
        if replied_bot_text:
            content = f"【用户回复的机器人上一条消息】\n{replied_bot_text}\n\n【用户本次说的话】\n{user_message}"
        messages = [
            {"role": "system", "content": "你是一个冷酷的女杀手，沉默寡言。你的老板是小熊。回答控制在15字以内，尽量一句话。复杂问题时可回复\"不知道\"，\"小助理，你来回答\"，\"......\"，\"无可奉告\""},
            {"role": "user", "content": content},
        ]
        resp = client.chat.completions.create(
            model=KIMI_MODEL,
            messages=messages,
            max_tokens=1024,
            temperature=0.7,
        )
        out = (resp.choices[0].message.content or "").strip()
        return out or "（无回复）"
    except Exception as e:
        return f"AI 调用异常：{e}"


async def main():
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)

    me = await client.get_me()
    print(f"已登录: {me.first_name} (ID: {me.id})")
    if not TARGET_GROUP_IDS:
        print("警告: 未配置 GROUP_ID")
    else:
        print(f"仅作用于群: {TARGET_GROUP_IDS}")

    _load_spam_keywords()
    _load_verified_users()
    _load_verification_failures()
    _load_verification_blacklist()

    # 启动时全量对比抽奖数据库，增量写入白名单，并在群中发通知
    try:
        db_path = Path(LOTTERY_DB_PATH)
        db_exists = db_path.exists() if db_path.is_absolute() else (Path.cwd() / db_path).exists()
        print(f"[抽奖同步] LOTTERY_DB_PATH={LOTTERY_DB_PATH} exists={db_exists} cwd={Path.cwd()}")
        if not db_exists:
            print(f"[抽奖同步] 数据库文件不存在，跳过同步。可设置环境变量 LOTTERY_DB_PATH 指定路径")
            _sync_startup_msg = "任务失败，立即撤退"
        else:
            success, new_count, err = await asyncio.to_thread(_sync_lottery_to_verified, True)
            if success:
                _load_verified_users()
                print(f"[抽奖同步] 启动全量同步完成，新增 {new_count} 人")
                _sync_startup_msg = "任务执行完毕"
            else:
                print(f"[抽奖同步] 启动全量同步失败: {err}")
                _sync_startup_msg = "任务失败，立即撤退"
        for gid in TARGET_GROUP_IDS:
            try:
                await client.send_message(int(gid), _sync_startup_msg)
            except Exception as e:
                print(f"[抽奖同步] 群{gid} 发送失败: {e}")
    except Exception as e:
        import traceback
        print(f"[抽奖同步] 启动全量同步异常: {e}")
        traceback.print_exc()
        for gid in TARGET_GROUP_IDS:
            try:
                await client.send_message(int(gid), "任务失败，立即撤退")
            except Exception:
                pass

    # 设置快捷命令（输入框左侧 / 菜单）
    await client(SetBotCommandsRequest(
        scope=BotCommandScopeDefault(),
        lang_code="zh",
        commands=[
            BotCommand(command="list", description="查看关键词"),
            BotCommand(command="kw_text", description="消息关键词管理"),
            BotCommand(command="kw_name", description="昵称关键词管理"),
            BotCommand(command="kw_bio", description="简介关键词管理"),
            BotCommand(command="start", description="启动"),
            BotCommand(command="help", description="帮助"),
            BotCommand(command="cancel", description="取消操作"),
            BotCommand(command="reload", description="重载配置"),
            BotCommand(command="verified_stats", description="导出验证用户统计"),
        ],
    ))

    def _add_to_blacklist_and_save(user_id: int):
        """将用户加入黑名单并保存，同时从白名单移除（避免黑白名单同时存在）"""
        verification_blacklist.add(user_id)
        _save_verification_blacklist()
        verified_users.discard(user_id)
        verified_users_details.pop(user_id, None)
        _save_verified_users()

    # 监控：管理员限制或封禁用户时，将用户加入黑名单
    @client.on(events.Raw)
    async def on_raw_update(update):
        if isinstance(update, UpdateChannelParticipant) and isinstance(
            getattr(update, "new_participant", None), ChannelParticipantBanned
        ):
            chat_id = str(-1000000000000 - update.channel_id)
            if _chat_allowed(chat_id):
                _add_to_blacklist_and_save(update.user_id)
                if VERBOSE:
                    print(f"[黑名单] 用户 {update.user_id} 被限制/封禁，已加入黑名单: 群{chat_id}")

    # 有机器人入群时，自动加入白名单（仅入群事件触发，保证所有机器人都在白名单里）
    @client.on(events.ChatAction)
    async def on_chat_action(event):
        if event.user_kicked:
            chat_peer = getattr(event, "chat_peer", None)
            if isinstance(chat_peer, PeerChannel):
                chat_id = str(-1000000000000 - chat_peer.channel_id)
            else:
                chat_id = str(getattr(event, "chat_id", None) or "")
            if chat_id and _chat_allowed(chat_id):
                for uid in (event.user_ids or []):
                    _add_to_blacklist_and_save(uid)
                    if VERBOSE:
                        print(f"[黑名单] 用户 {uid} 被踢出，已加入黑名单: 群{chat_id}")
            return
        if not (event.user_added or event.user_joined):
            return
        chat_peer = getattr(event, "chat_peer", None)
        if isinstance(chat_peer, PeerChannel):
            chat_id = str(-1000000000000 - chat_peer.channel_id)
        else:
            chat_id = str(getattr(event, "chat_id", None) or "")
        if not chat_id or not _chat_allowed(chat_id):
            return
        now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        for uid in (event.user_ids or []):
            join_times[uid] = now_iso
            try:
                user = await event.client.get_entity(uid)
                if getattr(user, "bot", False):
                    _add_verified_user(
                        uid,
                        username=getattr(user, "username", None),
                        full_name=_get_full_name(user),
                    )
                    if VERBOSE:
                        print(f"[入群] 机器人 {uid} 已加入 verified_users: 群{chat_id}")
            except Exception:
                pass
        _save_verified_users()

    # 启动时向群发送你好
    if TARGET_GROUP_IDS:
        print("你好")
        for gid in TARGET_GROUP_IDS:
            try:
                chat = await client.get_entity(int(gid))
                name = getattr(chat, "title", None) or getattr(chat, "name", "") or gid
                print(f"  群: {name} (ID: {gid})")
                await client.send_message(int(gid), "你好")
            except Exception as e:
                print(f"  群{gid} 发送失败: {e}")

    @client.on(events.NewMessage)
    async def on_message(event):
        chat = await event.get_chat()
        if not chat:
            return
        chat_id = str(getattr(event, "chat_id", None) or getattr(chat, "id", chat))
        if not _chat_allowed(chat_id):
            return

        text = (event.message.text or event.message.message or "").strip()

        # 过期验证清理
        for cid, uid in _clean_expired_verifications():
            count = _increment_verification_failures(cid, uid)
            _save_verification_failures()
            if count >= VERIFY_FAIL_THRESHOLD:
                await _restrict_user_and_notify(event.client, cid, uid)
        if event.sender and (chat_id, event.sender.id) in pending_by_user:
            pb = pending_by_user[(chat_id, event.sender.id)]
            if time.time() - pb["time"] <= VERIFY_TIMEOUT:
                ok = text == pb["code"] or text == f"验证码{pb['code']}"
                await _handle_verification_result(event.client, event, chat_id, event.sender.id, pb["code"], ok)
                return

        if not event.sender:
            return
        uid = event.sender.id

        # 1. 白名单成员（按用户，多群通用）：检查 AI 触发，否则直接通过
        if uid in verified_users:
            # AI 唤醒：霜刃，/验证官，/@机器人/回复机器人
            if text:
                reply_msg = await event.get_reply_message() if event.message.reply_to else None
                triggered, query, replied_bot_text = _check_ai_trigger(
                    text, reply_msg, me.id, getattr(me, "username", None)
                )
                if triggered and KIMI_API_KEY:
                    try:
                        reply_text = await _call_kimi_single_turn(query, replied_bot_text)
                        await event.reply(reply_text)
                        # 霜刃说「小助理，你来回答」时，小助理收不到（Telegram 不转发 bot→bot）
                        # 写入 handoff，由小助理轮询代为回复
                        rt = (reply_text or "").strip().rstrip("。.！？!? ")
                        if "小助理" in rt and "你来回答" in rt:
                            try:
                                from handoff import put_handoff
                                if reply_msg and getattr(reply_msg, "sender_id", None) != me.id:
                                    q_text = (reply_msg.text or reply_msg.message or "").strip()
                                    if q_text:
                                        reply_to_id = reply_msg.id
                                        put_handoff(int(chat_id), reply_to_id, q_text)
                                    else:
                                        put_handoff(int(chat_id), event.message.id, query)
                                else:
                                    put_handoff(int(chat_id), event.message.id, query)
                            except Exception as he:
                                print(f"[handoff] 写入失败: {he}")
                        if VERBOSE:
                            print(f"[AI] 群{chat_id} | {_get_sender_display(event.sender)} | 已回复")
                    except Exception as e:
                        print(f"[AI 失败] {e}")
                        try:
                            await event.reply(f"AI 调用失败：{e}")
                        except Exception:
                            pass
                    return
            msg_type = get_message_type(event.message)
            if VERBOSE:
                print(f"[消息] 群{chat_id} | {_get_sender_display(event.sender)} | {msg_type} | {text[:40]}...")
            else:
                print(f"[消息] 群{chat_id} | {_get_sender_display(event.sender)} | {msg_type}")
            return

        # 2. 非白名单：等待 2s，若消息在 2s 内已被删除则结束流程，否则进入广告判定
        await asyncio.sleep(2)
        try:
            still = await event.client.get_messages(event.chat_id, ids=event.message.id)
            if not still:
                return  # 消息已不存在（如已被删除），流程结束
            msg = still[0] if isinstance(still, list) else still
            if msg is None or getattr(msg, "deleted", False):
                return  # 消息已被删除，流程结束
        except Exception:
            return  # 无法获取消息（如已删除），流程结束

        # 3. 广告判定（网页且文本≤10）→ 进入人机验证
        msg_type = get_message_type(event.message)
        if msg_type == "webpage" and len(text) <= 10:
            await _start_verification(
                event.client, event, chat_id,
                "⚠️ 检测到您的消息中含有疑似广告，请先完成人机验证。",
                "广告",
            )
            return

        # 3.5 引用非本群消息判定 → 进入人机验证
        reply_to = getattr(event.message, "reply_to", None)
        if reply_to:
            reply_peer = getattr(reply_to, "reply_to_peer_id", None)
            if reply_peer is not None:
                try:
                    if isinstance(reply_peer, PeerChannel):
                        reply_chat_id = str(-1000000000000 - reply_peer.channel_id)
                    elif isinstance(reply_peer, PeerChat):
                        reply_chat_id = str(-reply_peer.chat_id)
                    else:
                        reply_chat_id = None  # PeerUser 等其它类型暂不处理
                    if reply_chat_id and reply_chat_id != str(chat_id):
                        await _start_verification(
                            event.client, event, chat_id,
                            "⚠️ 检测到您引用了非本群消息，疑似广告，请先完成人机验证。",
                            "引用",
                        )
                        return
                except Exception:
                    pass

        # 4. 发言 text 关键词判定 → 进入人机验证
        text_matched_kw = _check_spam(text, "", "", None)
        if text_matched_kw:
            await _start_verification(
                event.client, event, chat_id,
                "⚠️ 检测到您的消息中含有疑似广告词，请先完成人机验证。",
                "文本",
            )
            return

        # 5. 人机验证：先仅检查昵称关键词（不调 bio）
        first_name = getattr(event.sender, "first_name", None) or ""
        last_name = getattr(event.sender, "last_name", None) or ""
        name_matched_kw = _check_spam_name_bio(first_name, last_name, None)
        if name_matched_kw:
            await _start_verification(
                event.client, event, chat_id,
                "⚠️ 检测到您昵称中含有疑似广告词，请先完成人机验证。",
                "昵称",
            )
            return

        # 6. 调取 bio 前：若在黑名单中（按用户），不调用 bio，直接进入验证码流程
        if uid in verification_blacklist:
            await _start_verification(
                event.client, event, chat_id,
                "⚠️ 检测到您的账号疑似广告账号，请先完成人机验证。",
                "黑名单",
            )
            return

        # 7. 未在黑名单：调取 bio，进行简介关键词 / 简介链接验证
        sender_bio = await _get_sender_bio_cached(event.client, event.sender.id)
        bio_matched_kw = _check_spam_name_bio("", "", sender_bio)
        bio_has_link = _bio_needs_verification(sender_bio)
        if bio_matched_kw:
            await _start_verification(
                event.client, event, chat_id,
                "⚠️ 检测到您简介中含有疑似广告词，请先完成人机验证。",
                "简介",
            )
            return
        if bio_has_link:
            await _start_verification(
                event.client, event, chat_id,
                "⚠️ 简介中有链接，疑似广告，请先完成人机验证。",
                "简介",
            )
            return

        # 8. 简介也无问题：加入白名单，后续发言不再重复检验
        username = getattr(event.sender, "username", None)
        full_name = _get_full_name(event.sender)
        _add_verified_user(uid, username=username, full_name=full_name)
        _save_verified_users()

        msg_type = get_message_type(event.message)
        if VERBOSE:
            print(f"[消息] 群{chat_id} | {_get_sender_display(event.sender)} | {msg_type} | {text[:40]}...")
        else:
            print(f"[消息] 群{chat_id} | {_get_sender_display(event.sender)} | {msg_type}")

    @client.on(events.NewMessage(pattern=r"^/start"))
    async def cmd_start(event):
        if not event.is_private:
            return
        if event.sender and event.sender.id in pending_keyword_cmd:
            pending_keyword_cmd.pop(event.sender.id, None)
            await event.reply("已退出关键词管理。\n\nBytecler 群消息监控机器人\n发送 /help 查看完整指令")
        else:
            await event.reply("Bytecler 群消息监控机器人\n发送 /help 查看完整指令")

    _MSG_LINK_RE = re.compile(
        r"https?://t\.me/c/(\d+)/(\d+)|https?://t\.me/([a-zA-Z0-9_]+)/(\d+)"
    )

    async def _simulate_verification(client, msg, chat_id: str) -> str:
        """根据消息模拟霜刃的验证流程，返回验证过程描述"""
        lines = []
        sender = getattr(msg, "sender", None)
        if not sender:
            return "无法获取发送者"
        uid = getattr(sender, "id", None)
        if uid is None:
            return "无法获取用户 ID"
        text = (msg.text or msg.message or "").strip()
        first_name = getattr(sender, "first_name", None) or ""
        last_name = getattr(sender, "last_name", None) or ""
        full_name = (first_name + " " + last_name).strip() or "用户"
        username = getattr(sender, "username", None)

        lines.append(f"【消息链接验证】")
        lines.append(f"用户: {full_name} (@{username or '无'}) ID:{uid}")
        lines.append(f"群: {chat_id}")
        lines.append(f"消息: {text[:80]}{'...' if len(text) > 80 else ''}")
        lines.append("")

        if not _chat_allowed(chat_id):
            lines.append("→ 该群不在监控范围内，跳过")
            return "\n".join(lines)

        if uid in verified_users:
            lines.append("→ 白名单用户，直接通过")
            return "\n".join(lines)

        lines.append("→ 非白名单，进入验证流程：")
        msg_type = get_message_type(msg)
        lines.append(f"  1. 消息类型: {msg_type}")

        if msg_type == "webpage" and len(text) <= 10:
            lines.append("  2. 判定: 网页+短文本 → 人机验证（广告）")
            return "\n".join(lines)

        reply_to = getattr(msg, "reply_to", None)
        if reply_to:
            reply_peer = getattr(reply_to, "reply_to_peer_id", None)
            if reply_peer is not None:
                try:
                    if isinstance(reply_peer, PeerChannel):
                        reply_chat_id = str(-1000000000000 - reply_peer.channel_id)
                    elif isinstance(reply_peer, PeerChat):
                        reply_chat_id = str(-reply_peer.chat_id)
                    else:
                        reply_chat_id = None
                    if reply_chat_id and reply_chat_id != str(chat_id):
                        lines.append("  2. 判定: 引用非本群消息 → 人机验证（引用）")
                        return "\n".join(lines)
                except Exception:
                    pass

        text_matched = _check_spam(text, "", "", None)
        if text_matched:
            lines.append(f"  2. 判定: 文本关键词命中「{text_matched}」→ 人机验证（文本）")
            return "\n".join(lines)

        name_matched = _check_spam_name_bio(first_name, last_name, None)
        if name_matched:
            lines.append(f"  2. 判定: 昵称关键词命中「{name_matched}」→ 人机验证（昵称）")
            return "\n".join(lines)

        if uid in verification_blacklist:
            lines.append("  2. 判定: 黑名单用户 → 人机验证（黑名单）")
            return "\n".join(lines)

        bio = await _get_sender_bio_cached(client, uid)
        bio_matched = _check_spam_name_bio("", "", bio)
        if bio_matched:
            lines.append(f"  2. 判定: 简介关键词命中「{bio_matched}」→ 人机验证（简介）")
            return "\n".join(lines)
        if _bio_needs_verification(bio):
            lines.append("  2. 判定: 简介含链接 → 人机验证（简介）")
            return "\n".join(lines)

        lines.append("  2. 全部通过 → 加入白名单")
        return "\n".join(lines)

    @client.on(events.NewMessage)
    async def on_private_msg_link(event):
        """私聊中输入群消息链接（如 https://t.me/xxx/123 或 t.me/c/ channelid/123），返回霜刃的验证过程"""
        if not event.is_private or not event.sender:
            return
        text = (event.message.text or event.message.message or "").strip()
        if not text:
            return
        m = _MSG_LINK_RE.search(text)
        if not m:
            return
        try:
            if m.group(1) is not None:
                channel_id = int(m.group(1))
                msg_id = int(m.group(2))
                entity = -1000000000000 - channel_id
            else:
                entity = m.group(3)
                msg_id = int(m.group(4))
            msg = await event.client.get_messages(entity, ids=msg_id)
            if not msg:
                await event.reply("无法获取该消息（可能已删除或无权访问）")
                return
            msg = msg[0] if isinstance(msg, list) else msg
            chat_id = str(getattr(msg, "chat_id", None) or entity)
            out = await _simulate_verification(event.client, msg, chat_id)
            cb_data = f"vjson:{getattr(msg, 'chat_id', entity)}:{msg.id}"[:64]
            await event.reply(out, buttons=[[Button.inline("查看原始 JSON", cb_data.encode())]])
        except Exception as e:
            await event.reply(f"解析失败: {e}")

    @client.on(events.CallbackQuery)
    async def on_verify_json_callback(event):
        """点击「查看原始 JSON」按钮，返回消息的 JSON"""
        data = event.data
        if not isinstance(data, bytes) or not data.startswith(b"vjson:"):
            return
        try:
            parts = data.decode().split(":", 2)
            if len(parts) != 3:
                return
            _, chat_id_str, msg_id_str = parts
            entity = int(chat_id_str)
            msg_id = int(msg_id_str)
            msg = await event.client.get_messages(entity, ids=msg_id)
            if not msg:
                await event.answer("无法获取该消息", alert=True)
                return
            msg = msg[0] if isinstance(msg, list) else msg
            msg_dict = msg.to_dict()
            for key in ("message", "text"):
                if key in msg_dict and isinstance(msg_dict[key], str) and len(msg_dict[key]) > 100:
                    msg_dict[key] = msg_dict[key][:100] + "..."
            def _drop_none(obj):
                if obj is None or isinstance(obj, bytes):
                    return None
                if isinstance(obj, dict):
                    return {k: v for k, v in ((k, _drop_none(v)) for k, v in obj.items()) if v is not None}
                if isinstance(obj, list):
                    return [x for x in (_drop_none(item) for item in obj) if x is not None]
                return obj
            def _json_default(o):
                if isinstance(o, datetime):
                    return o.isoformat()
                if isinstance(o, bytes):
                    return f"<bytes len={len(o)}>"
                raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")
            msg_dict = _drop_none(msg_dict)
            out = json.dumps(msg_dict, ensure_ascii=False, indent=2, default=_json_default)
            if len(out) > 4000:
                out = out[:4000] + "\n\n... (已截断)"
            await event.answer()
            peer = getattr(event, "chat_id", None) or getattr(event, "sender_id", None)
            if peer is not None:
                await event.client.send_message(peer, out)
        except Exception as e:
            await event.answer(f"解析失败: {e}", alert=True)

    @client.on(events.NewMessage(pattern=r"^/help"))
    async def cmd_help(event):
        if not event.is_private:
            return
        admin_hint = "（需配置 ADMIN_IDS 环境变量限制权限）" if ADMIN_IDS else ""
        msg = f"""Bytecler 指令（仅私聊有效）{admin_hint}

• /list — 查看垃圾关键词
• /kw_text, /kw_name, /kw_bio — 关键词管理（发送则切换添加/删除，/cancel 或 /start 退出）
• /cancel — 取消当前操作
• /reload — 从文件重载关键词
• /verified_stats — 导出用户统计
• 发送群消息链接 — 返回该消息的验证过程（如 https://t.me/xxx/123）

关键词格式：子串匹配直接输入；精确匹配用 / 前缀，如 / 加微信
"""
        await event.reply(msg)

    @client.on(events.NewMessage(pattern=r"^/list"))
    async def cmd_list(event):
        if not event.is_private or not event.sender:
            return
        if not _is_admin(event.sender.id):
            await event.reply("无权限")
            return
        lines = []
        for field, label in [("text", "消息"), ("name", "昵称"), ("bio", "简介")]:
            kw = spam_keywords.get(field) or {}
            ex = kw.get("exact") or []
            mt = [x[1] if x[0] == "str" else f"/{x[1].pattern}/" for x in (kw.get("match") or [])]
            lines.append(f"【{label}】exact: {ex or '无'} | match: {mt or '无'}")
        lines.append("")
        lines.append("管理: /kw_text /kw_name /kw_bio 进入模式后输入关键词切换")
        await event.reply("\n".join(lines))

    @client.on(events.NewMessage(pattern=r"^/reload"))
    async def cmd_reload(event):
        if not event.is_private or not event.sender:
            return
        if not _is_admin(event.sender.id):
            await event.reply("无权限")
            return
        pending_keyword_cmd.pop(event.sender.id, None)
        _load_spam_keywords()
        await event.reply("已重载 spam_keywords.json")

    @client.on(events.NewMessage(pattern=r"^/verified_stats"))
    async def cmd_verified_stats(event):
        """显示验证通过用户统计：user_id, username, full_name, 入群时间, 验证通过时间"""
        if not event.is_private or not event.sender:
            if event.is_private is False and event.sender:
                await event.reply("该命令仅在私聊中有效。")
            return
        if not _is_admin(event.sender.id):
            await event.reply("无权限")
            return
        try:
            total = len(verified_users)
            has_join_time = sum(1 for uid in verified_users if (verified_users_details.get(uid) or {}).get("join_time"))

            lines = [f"📊 验证通过用户统计（按用户，多群通用）\n"]
            lines.append(f"总用户数: {total}")
            lines.append(f"有入群时间记录: {has_join_time}")
            lines.append(f"\n用户列表（按验证时间倒序，显示前20个）:")
            count = 0
            def _sort_key(uid):
                d = verified_users_details.get(uid) or {}
                return d.get("verify_time") or "0000-00-00"
            for uid in sorted(verified_users, key=_sort_key, reverse=True):
                if count >= 20:
                    lines.append(f"\n... 还有 {total - 20} 个用户未显示")
                    break
                d = verified_users_details.get(uid) or {}
                user_id = d.get("user_id") or uid
                username = d.get("username") or "无"
                full_name = d.get("full_name") or "用户"
                join_time = d.get("join_time") or "未知"
                verify_time = d.get("verify_time") or "未知"

                if join_time and join_time != "未知":
                    try:
                        dt = datetime.fromisoformat(join_time.replace("Z", "+00:00"))
                        join_time = dt.strftime("%Y-%m-%d %H:%M")
                    except Exception:
                        pass
                if verify_time and verify_time != "未知":
                    try:
                        dt = datetime.fromisoformat(verify_time.replace("Z", "+00:00"))
                        verify_time = dt.strftime("%Y-%m-%d %H:%M")
                    except Exception:
                        pass

                lines.append(f"{count + 1}. ID:{user_id} | @{username} | {full_name}")
                lines.append(f"   入群: {join_time} | 验证: {verify_time}")
                count += 1

            msg = "\n".join(lines)
            if len(msg) > 4000:
                msg = msg[:4000] + "\n\n... (消息过长，已截断)"

            await event.reply(msg)
        except Exception as e:
            await event.reply(f"统计失败: {e}")

    @client.on(events.NewMessage(pattern=r"^/cancel"))
    async def cmd_cancel(event):
        if not event.is_private or not event.sender:
            return
        if event.sender.id in pending_keyword_cmd:
            pending_keyword_cmd.pop(event.sender.id, None)
            await event.reply("已取消")
        else:
            await event.reply("当前无待完成的操作")

    def _do_toggle(field: str, kw_type: str, keyword: str) -> str:
        """存在则删，不存在则添"""
        kw_cfg = spam_keywords[field]
        if kw_type == "exact":
            if keyword in kw_cfg["exact"]:
                kw_cfg["exact"].remove(keyword)
                _save_spam_keywords()
                return f"❌ 已删除 exact: {keyword}"
            kw_cfg["exact"].append(keyword)
            _save_spam_keywords()
            return f"✅ 已添加 exact: {keyword}"
        # match
        existing = [(x[0], x[1] if x[0] == "str" else f"/{x[1].pattern}/") for x in kw_cfg["match"]]
        key_str = keyword if (keyword.startswith("/") and keyword.endswith("/") and len(keyword) > 2) else keyword.lower()
        for i, item in enumerate(kw_cfg["match"]):
            cmp = item[1] if item[0] == "str" else f"/{item[1].pattern}/"
            if cmp == key_str:
                kw_cfg["match"].pop(i)
                _save_spam_keywords()
                return f"❌ 已删除 match: {keyword}"
        if keyword.startswith("/") and keyword.endswith("/") and len(keyword) > 2:
            kw_cfg["match"].append(("regex", re.compile(keyword[1:-1], re.I)))
        else:
            kw_cfg["match"].append(("str", keyword.lower()))
        _save_spam_keywords()
        return f"✅ 已添加 match: {keyword}"

    async def _handle_kw_mode(event, field: str):
        """进入关键词管理模式"""
        if not event.is_private or not event.sender:
            return
        if not _is_admin(event.sender.id):
            await event.reply("无权限")
            return
        label = {"text": "消息", "name": "昵称", "bio": "简介"}[field]
        pending_keyword_cmd[event.sender.id] = {"field": field, "time": time.time()}
        await event.reply(
            f"【{label}关键词】管理模式\n"
            "发送关键词：已存在则删除，不存在则添加。\n"
            "子串匹配直接输入，精确匹配用 / 前缀，如：/ 加微信\n"
            "输入 /cancel 或 /start 退出"
        )

    @client.on(events.NewMessage)
    async def on_pending_keyword_input(event):
        """关键词管理模式：收到用户输入则切换添加/删除，保持模式直到 /cancel 或 /start"""
        if not event.is_private or not event.sender:
            return
        if not _is_admin(event.sender.id):
            return
        text = (event.message.text or "").strip()
        if text.startswith("/") and (len(text) < 2 or text[1] != " "):
            return
        user_id = event.sender.id
        if user_id not in pending_keyword_cmd:
            return
        now = time.time()
        if now - pending_keyword_cmd[user_id]["time"] > PENDING_KEYWORD_TIMEOUT:
            pending_keyword_cmd.pop(user_id, None)
            await event.reply("操作已超时，请重新发送命令")
            return
        field = pending_keyword_cmd[user_id]["field"]
        pending_keyword_cmd[user_id]["time"] = now
        if text.startswith("/ ") and len(text) > 2:
            kw_type, keyword = "exact", text[2:].strip()
        else:
            kw_type, keyword = "match", text
        if not keyword:
            await event.reply("关键词不能为空")
            return
        msg = _do_toggle(field, kw_type, keyword)
        await event.reply(msg)

    for f in ["text", "name", "bio"]:
        @client.on(events.NewMessage(pattern=rf"^/kw_{re.escape(f)}"))
        async def _kw_handler(event, field=f):
            await _handle_kw_mode(event, field)

    async def periodic_heartbeat():
        n = 0
        while True:
            await asyncio.sleep(300)
            n += 1
            print(f"[ heartbeat ] 运行中 (第{n}次)")

    async def frost_reply_poller():
        """小助理回复含「霜刃」时收不到，轮询 handoff 代为发送「......」"""
        while True:
            await asyncio.sleep(2)
            try:
                from handoff import take_frost_reply_handoff
                while True:
                    req = take_frost_reply_handoff()
                    if not req:
                        break
                    try:
                        await client(SetTypingRequest(peer=req["chat_id"], action=SendMessageTypingAction()))
                    except Exception:
                        pass
                    await asyncio.sleep(10)
                    try:
                        await client(SetTypingRequest(peer=req["chat_id"], action=SendMessageTypingAction()))
                    except Exception:
                        pass
                    await asyncio.sleep(5)
                    try:
                        await client.send_message(
                            req["chat_id"],
                            "......",
                            reply_to=req["reply_to_message_id"],
                        )
                    except Exception as send_err:
                        await client.send_message(req["chat_id"], "......")
                    print(f"[霜刃代为回复] 群{req['chat_id']} reply_to={req['reply_to_message_id']}")
            except Exception as e:
                print(f"[frost_reply_poller] {e}")

    _sync_last_run_date = [None]  # [date_str] 避免同一天多次执行

    async def sync_lottery_scheduler():
        """每日凌晨 SYNC_LOTTERY_HOUR 点执行抽奖白名单同步，并在群中发通知"""
        while True:
            await asyncio.sleep(60)
            now = datetime.now()
            if now.hour != SYNC_LOTTERY_HOUR:
                continue
            today = now.strftime("%Y-%m-%d")
            if _sync_last_run_date[0] == today:
                continue
            _sync_last_run_date[0] = today
            try:
                success, new_count, err = await asyncio.to_thread(_sync_lottery_to_verified)
                if success:
                    _load_verified_users()
                    msg = "任务执行完毕"
                else:
                    msg = "任务失败，立即撤退"
                print(f"[抽奖同步] success={success} new={new_count} err={err}")
                for gid in TARGET_GROUP_IDS:
                    try:
                        await client.send_message(int(gid), msg)
                    except Exception as e:
                        print(f"[抽奖同步] 群{gid} 发送失败: {e}")
            except Exception as e:
                print(f"[抽奖同步] 异常: {e}")
                for gid in TARGET_GROUP_IDS:
                    try:
                        await client.send_message(int(gid), "任务失败，立即撤退")
                    except Exception:
                        pass

    asyncio.create_task(periodic_heartbeat())
    asyncio.create_task(frost_reply_poller())
    asyncio.create_task(sync_lottery_scheduler())
    print("机器人已启动，等待消息...")
    print("提示: 若收不到消息，请在 @BotFather 对机器人执行 /setprivacy 选择 Disable 关闭隐私模式")
    await client.run_until_disconnected()


if __name__ == "__main__":
    if not BOT_TOKEN:
        print("请配置 BOT_TOKEN")
        exit(1)
    asyncio.run(main())
