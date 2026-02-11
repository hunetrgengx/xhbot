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
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# 路径：Windows 用绝对路径，Ubuntu 用相对路径（以 bytecler 目录为基准）
_BYTECLER_DIR = Path(__file__).resolve().parent if sys.platform == "win32" else None

def _path(name: str) -> Path:
    if sys.platform == "win32" and _BYTECLER_DIR:
        return _BYTECLER_DIR / name
    return Path(name)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault
from telethon.tl.types import PeerChannel
from telethon.tl.types import (
    MessageMediaPhoto,
    MessageMediaDocument,
    MessageMediaContact,
    MessageMediaGeo,
    MessageMediaPoll,
    MessageMediaWebPage,
    MessageMediaDice,
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
VERIFY_TIMEOUT = 60  # 验证码有效期（秒）
VERIFY_MSG_DELETE_AFTER = 30  # 验证相关消息保留多久后自动删除（秒）
VERIFY_FAIL_THRESHOLD = 5  # 验证失败次数阈值，达到则限制
VERIFY_FAILURES_RETENTION_SECONDS = 86400  # 单次验证失败记录保留时间（秒），1 天
VERIFY_RESTRICT_DURATION = 1  # 限制时长（天），0=永久
UNBAN_BOT_USERNAME = os.getenv("UNBAN_BOT_USERNAME", "@XHNPBOT")
VERBOSE = os.getenv("TG_VERBOSE", "").lower() in ("1", "true", "yes")
ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = {int(x.strip()) for x in ADMIN_IDS_STR.split(",") if x.strip().isdigit()}
# 垃圾关键词：三个字段各自独立配置
# {"text": {"exact": [], "match": []}, "name": {...}, "bio": {...}}
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

# 两段式关键词配置：等待用户输入类型和关键词
PENDING_KEYWORD_TIMEOUT = 60
pending_keyword_cmd = {}  # user_id: {"cmd": "add_text", "time": timestamp}

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
            succ_msg = await event.reply(f"【{_get_full_name(event.sender)}】\n\n✓ 验证通过，可以正常发言了")
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


def _load_spam_keywords():
    """加载垃圾关键词配置，三个字段各自独立"""
    global spam_keywords
    if not SPAM_KEYWORDS_PATH.exists():
        return
    try:
        with open(SPAM_KEYWORDS_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        for field in ("text", "name", "bio"):
            field_cfg = cfg.get(field) or {}
            spam_keywords[field]["exact"], spam_keywords[field]["match"] = _parse_field_keywords(field_cfg)
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


def _check_spam(text: str, first_name: str, last_name: str, sender_bio: Optional[str]) -> Optional[str]:
    """
    检查是否命中垃圾关键词。三个字段各自独立配置关键词：
    - text: 消息文本
    - name: first_name + last_name 组合
    - bio: 简介
    每个字段只检查自己的 exact/match，返回命中的关键词。
    """
    msg_text = (text or "").strip()
    full_name = ((first_name or "").strip() + " " + (last_name or "").strip()).strip()
    bio = (sender_bio or "").strip()

    field_values = {"text": msg_text, "name": full_name, "bio": bio}

    for field, value in field_values.items():
        kw_cfg = spam_keywords.get(field) or {}
        exact_list = kw_cfg.get("exact") or []
        match_list = kw_cfg.get("match") or []

        for kw in exact_list:
            if value and value.lower() == kw.lower():
                return kw

        for item in match_list:
            if item[0] == "str":
                if item[1] in (value.lower() or ""):
                    return item[1]
            else:
                if item[1].search(value):
                    return item[1].pattern

    return None


def _check_spam_name_bio(first_name: str, last_name: str, sender_bio: Optional[str]) -> Optional[str]:
    """
    检查 name 或 bio 是否命中垃圾关键词（用于人机验证）。
    返回命中的关键词，如果未命中则返回 None。
    """
    full_name = ((first_name or "").strip() + " " + (last_name or "").strip()).strip()
    bio = (sender_bio or "").strip()

    field_values = {"name": full_name, "bio": bio}

    for field, value in field_values.items():
        kw_cfg = spam_keywords.get(field) or {}
        exact_list = kw_cfg.get("exact") or []
        match_list = kw_cfg.get("match") or []

        for kw in exact_list:
            if value and value.lower() == kw.lower():
                return kw

        for item in match_list:
            if item[0] == "str":
                if item[1] in (value.lower() or ""):
                    return item[1]
            else:
                if item[1].search(value):
                    return item[1].pattern

    return None


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

    # 设置快捷命令（输入框左侧 / 菜单）
    await client(SetBotCommandsRequest(
        scope=BotCommandScopeDefault(),
        lang_code="zh",
        commands=[
            BotCommand(command="add_name", description="添加昵称关键词"),
            BotCommand(command="add_bio", description="添加简介关键词"),
            BotCommand(command="add_text", description="添加消息关键词"),
            BotCommand(command="del_name", description="删除昵称关键词"),
            BotCommand(command="del_bio", description="删除简介关键词"),
            BotCommand(command="del_text", description="删除消息关键词"),
            BotCommand(command="list", description="查看关键词"),
            BotCommand(command="start", description="启动"),
            BotCommand(command="help", description="帮助"),
            BotCommand(command="cancel", description="取消操作"),
            BotCommand(command="reload", description="重载配置"),
            BotCommand(command="verified_stats", description="导出验证用户统计"),
        ],
    ))

    # 机器人入群时自动加入 verified_users；记录所有用户入群时间
    @client.on(events.ChatAction)
    async def on_chat_action(event):
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
        bot_id = me.id
        for uid in (event.user_ids or []):
            join_times[uid] = now_iso
            if uid == bot_id:
                _add_verified_user(
                    bot_id,
                    username=getattr(me, "username", None),
                    full_name=_get_full_name(me),
                )
                if VERBOSE:
                    print(f"[入群] 机器人已加入 verified_users: 群{chat_id}")
                break
        _save_verified_users()

    # 启动时向群发送你好，并将机器人加入 verified_users
    if TARGET_GROUP_IDS:
        print("你好")
        for gid in TARGET_GROUP_IDS:
            try:
                chat = await client.get_entity(int(gid))
                name = getattr(chat, "title", None) or getattr(chat, "name", "") or gid
                print(f"  群: {name} (ID: {gid})")
                _add_verified_user(
                    me.id,
                    username=getattr(me, "username", None),
                    full_name=_get_full_name(me),
                )
                await client.send_message(int(gid), "你好")
            except Exception as e:
                print(f"  群{gid} 发送失败: {e}")
        _save_verified_users()

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

        # 1. 白名单成员（按用户，多群通用）：不再做任何检查，直接通过
        if uid in verified_users:
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

        # 3. 广告判定（网页且文本≤2）→ 进入人机验证
        msg_type = get_message_type(event.message)
        if msg_type == "webpage" and len(text) <= 2:
            await _start_verification(
                event.client, event, chat_id,
                "⚠️ 检测到您的消息中含有疑似广告，请先完成人机验证。",
                "广告",
            )
            return

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
        if bio_matched_kw or bio_has_link:
            await _start_verification(
                event.client, event, chat_id,
                "⚠️ 检测到您简介中含有疑似广告词，请先完成人机验证。",
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
        await event.reply("Bytecler 群消息监控机器人\n发送 /help 查看完整指令")

    @client.on(events.NewMessage(pattern=r"^/help"))
    async def cmd_help(event):
        if not event.is_private:
            return
        admin_hint = "（需配置 ADMIN_IDS 环境变量限制权限）" if ADMIN_IDS else ""
        msg = f"""Bytecler 指令（仅私聊有效）{admin_hint}

• /list — 查看垃圾关键词
• /add_text, /add_name, /add_bio — 添加（两段式）
• /del_text, /del_name, /del_bio — 删除（两段式）
• /cancel — 取消当前操作
• /reload — 从文件重载关键词
• /verified_stats — 导出用户统计

两段式：发送命令后按提示输入关键词
• 子串匹配：直接输入，如 加V
• 精确匹配：/ 前缀，如 / 加微信
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
        lines.append("添加/删除: 发送命令后输入关键词")
        lines.append("子串匹配: 加V  |  精确匹配: / 加微信")
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
            return
        if not _is_admin(event.sender.id):
            await event.reply("无权限")
            return
        
        # 统计信息（按用户，多群通用）
        total = len(verified_users)
        has_join_time = sum(1 for uid in verified_users if (verified_users_details.get(uid) or {}).get("join_time"))
        
        lines = [f"📊 验证通过用户统计（按用户，多群通用）\n"]
        lines.append(f"总用户数: {total}")
        lines.append(f"有入群时间记录: {has_join_time}")
        lines.append(f"\n用户列表（显示前20个）:")
        count = 0
        for uid in sorted(verified_users):
            if count >= 20:
                lines.append(f"\n... 还有 {total - 20} 个用户未显示")
                break
            d = verified_users_details.get(uid) or {}
            user_id = d.get("user_id") or uid
            username = d.get("username") or "无"
            full_name = d.get("full_name") or "用户"
            join_time = d.get("join_time") or "未知"
            verify_time = d.get("verify_time") or "未知"
            
            # 格式化时间（只显示日期和时间，去掉秒）
            if join_time and join_time != "未知":
                try:
                    dt = datetime.fromisoformat(join_time.replace("Z", "+00:00"))
                    join_time = dt.strftime("%Y-%m-%d %H:%M")
                except:
                    pass
            if verify_time and verify_time != "未知":
                try:
                    dt = datetime.fromisoformat(verify_time.replace("Z", "+00:00"))
                    verify_time = dt.strftime("%Y-%m-%d %H:%M")
                except:
                    pass
            
            lines.append(f"{count + 1}. ID:{user_id} | @{username} | {full_name}")
            lines.append(f"   入群: {join_time} | 验证: {verify_time}")
            count += 1
        
        msg = "\n".join(lines)
        
        # Telegram 消息最大 4096 字符，如果超过则截断
        if len(msg) > 4000:
            msg = msg[:4000] + f"\n\n... (消息过长，已截断)"
        
        try:
            await event.reply(msg)
        except Exception as e:
            await event.reply(f"发送失败: {e}")

    @client.on(events.NewMessage(pattern=r"^/cancel"))
    async def cmd_cancel(event):
        if not event.is_private or not event.sender:
            return
        if event.sender.id in pending_keyword_cmd:
            pending_keyword_cmd.pop(event.sender.id, None)
            await event.reply("已取消")
        else:
            await event.reply("当前无待完成的操作")

    def _do_add(field: str, kw_type: str, keyword: str, cmd: str) -> str:
        kw_cfg = spam_keywords[field]
        cmd_text = f" /{cmd} "
        if kw_type == "exact":
            if keyword not in kw_cfg["exact"]:
                kw_cfg["exact"].append(keyword)
                _save_spam_keywords()
                return f"已添加 exact: {keyword}\n\n再次添加: {cmd_text}"
            return f"该关键词已存在\n\n再次添加: {cmd_text}"
        existing = [x[1] if x[0] == "str" else f"/{x[1].pattern}/" for x in kw_cfg["match"]]
        key_str = keyword if (keyword.startswith("/") and keyword.endswith("/") and len(keyword) > 2) else keyword.lower()
        if key_str not in existing:
            if keyword.startswith("/") and keyword.endswith("/") and len(keyword) > 2:
                kw_cfg["match"].append(("regex", re.compile(keyword[1:-1], re.I)))
            else:
                kw_cfg["match"].append(("str", keyword.lower()))
            _save_spam_keywords()
            return f"已添加 match: {keyword}\n\n再次添加: {cmd_text}"
        return f"该关键词已存在\n\n再次添加: {cmd_text}"

    def _do_del(field: str, kw_type: str, keyword: str, cmd: str) -> str:
        kw_cfg = spam_keywords[field]
        cmd_text = f" /{cmd} "
        if kw_type == "exact":
            if keyword in kw_cfg["exact"]:
                kw_cfg["exact"].remove(keyword)
                _save_spam_keywords()
                return f"已删除 exact: {keyword}\n\n再次删除: {cmd_text}"
            return f"未找到该关键词\n\n再次删除: {cmd_text}"
        for i, item in enumerate(kw_cfg["match"]):
            if item[0] == "str" and item[1] == keyword.lower():
                kw_cfg["match"].pop(i)
                _save_spam_keywords()
                return f"已删除 match: {keyword}\n\n再次删除: {cmd_text}"
            if item[0] == "regex" and f"/{item[1].pattern}/" == keyword:
                kw_cfg["match"].pop(i)
                _save_spam_keywords()
                return f"已删除 match: {keyword}\n\n再次删除: {cmd_text}"
        return f"未找到该关键词\n\n再次删除: {cmd_text}"

    async def _handle_add_del_step1(event, cmd: str):
        """第一步：收到命令，等待输入"""
        if not event.is_private or not event.sender:
            return
        if not _is_admin(event.sender.id):
            await event.reply("无权限")
            return
        action = "添加" if cmd.startswith("add_") else "删除"
        field_label = {"text": "消息", "name": "昵称", "bio": "简介"}.get(cmd[4:], "")
        pending_keyword_cmd[event.sender.id] = {"cmd": cmd, "time": time.time()}
        await event.reply(f"【{action}{field_label}】\n请输入关键词（默认子串匹配）\n精确匹配请用 / 前缀，如：/ 加微信\n发送 /cancel 取消")

    @client.on(events.NewMessage)
    async def on_pending_keyword_input(event):
        """第二步：收到用户输入的类型和关键词"""
        if not event.is_private or not event.sender:
            return
        if not _is_admin(event.sender.id):
            return  # 无权限时静默忽略（可能已在 step1 提示过）
        text = (event.message.text or "").strip()
        if text.startswith("/") and (len(text) < 2 or text[1] != " "):  # 命令如 /add_text 由对应 handler 处理
            return
        user_id = event.sender.id
        if user_id not in pending_keyword_cmd:
            return
        now = time.time()
        if now - pending_keyword_cmd[user_id]["time"] > PENDING_KEYWORD_TIMEOUT:
            pending_keyword_cmd.pop(user_id, None)
            await event.reply("操作已超时，请重新发送命令")
            return
        cmd = pending_keyword_cmd.pop(user_id)["cmd"]
        if text.startswith("/ ") and len(text) > 2:
            kw_type, keyword = "exact", text[2:].strip()
        else:
            kw_type, keyword = "match", text
        if not keyword:
            await event.reply("关键词不能为空")
            return
        field = cmd[4:]
        if cmd.startswith("add_"):
            msg = _do_add(field, kw_type, keyword, cmd)
        else:
            msg = _do_del(field, kw_type, keyword, cmd)
        await event.reply(msg)

    for c in ["add_text", "add_name", "add_bio", "del_text", "del_name", "del_bio"]:
        @client.on(events.NewMessage(pattern=rf"^/{re.escape(c)}"))
        async def _add_del_handler(event, cmd=c):
            await _handle_add_del_step1(event, cmd)

    async def periodic_heartbeat():
        n = 0
        while True:
            await asyncio.sleep(300)
            n += 1
            print(f"[ heartbeat ] 运行中 (第{n}次)")

    asyncio.create_task(periodic_heartbeat())
    print("机器人已启动，等待消息...")
    print("提示: 若收不到消息，请在 @BotFather 对机器人执行 /setprivacy 选择 Disable 关闭隐私模式")
    await client.run_until_disconnected()


if __name__ == "__main__":
    if not BOT_TOKEN:
        print("请配置 BOT_TOKEN")
        exit(1)
    asyncio.run(main())
