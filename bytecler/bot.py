#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bytecler 霜刃 - PTB 版（合并 bot_ptb + shared）
实现功能参见 BYTECLER_PTB_ANALYSIS.md
"""
import asyncio
import json
import os
import random
import re
import sqlite3
import sys
import time
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

try:
    import ahocorasick
    AHOCORASICK_AVAILABLE = True
except ImportError:
    AHOCORASICK_AVAILABLE = False

from telegram import Update, ChatMemberBanned, ChatMemberRestricted, ChatMemberLeft, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.constants import MessageEntityType
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ChatMemberHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

# ==================== 共享逻辑 (原 shared.py) ====================
_BASE = Path(__file__).resolve().parent

def _path(name: str) -> Path:
    return _BASE / name

SPAM_KEYWORDS_PATH = _path("spam_keywords.json")
VERIFIED_USERS_PATH = _path("verified_users.json")
VERIFICATION_FAILURES_PATH = _path("verification_failures.json")
VERIFICATION_BLACKLIST_PATH = _path("verification_blacklist.json")
VERIFICATION_RECORDS_PATH = _path("verification_records.json")
SYNC_LOTTERY_CHECKPOINT_PATH = _path("sync_lottery_checkpoint.json")
SETTIME_CONFIG_PATH = _path("settime_config.json")
BGROUP_CONFIG_PATH = _path("bgroup_config.json")  # 每群单独配置 B 群（仅一个）：{ "chat_id": "b_id" }，无全局
LOTTERY_DB_PATH = os.getenv("LOTTERY_DB_PATH", "/tgbot/cjbot/cjdb/lottery.db")

spam_keywords = {"text": {"exact": [], "match": [], "_ac": None, "_regex": []},
                 "name": {"exact": [], "match": [], "_ac": None, "_regex": []},
                 "bio": {"exact": [], "match": [], "_ac": None, "_regex": []},
                 "whitelist": {"name": {"exact": [], "match": [], "_regex": []},
                              "text": {"exact": [], "match": [], "_regex": []}}}
verified_users = set()
verified_users_details = {}
join_times = {}
verification_failures = {}
verification_blacklist = set()
_verification_records = {}
# 缓存用户最近一条消息，供管理员删除+限制/封禁时自动加入关键词，保存一天后自动删除
LAST_MESSAGE_CACHE_TTL_SECONDS = 86400  # 24 小时
_last_message_by_user: dict[tuple[str, int], tuple[str, float]] = {}

VERIFY_FAIL_THRESHOLD = 5
VERIFY_FAILURES_RETENTION_SECONDS = 86400
# 冷却间隔 + 时间窗口：15 秒内重复触发不计入，20 分钟内 5 次即限制
TRIGGER_COOLDOWN_SECONDS = int(os.getenv("TRIGGER_COOLDOWN_SECONDS", "15"))
TRIGGER_WINDOW_SECONDS = int(os.getenv("TRIGGER_WINDOW_SECONDS", "1200"))  # 20 分钟
# 含 emoji 的消息/昵称触发验证，0/false 关闭
ENABLE_EMOJI_CHECK = os.getenv("ENABLE_EMOJI_CHECK", "1").lower() not in ("0", "false", "no")
ENABLE_STICKER_CHECK = os.getenv("ENABLE_STICKER_CHECK", "1").lower() not in ("0", "false", "no")


def _apply_trigger_cooldown_window(timestamps: list, now: float) -> tuple[bool, list, int]:
    """冷却间隔+时间窗口。返回 (本次是否计入, 更新后的时间戳列表, 当前窗口内次数)"""
    cutoff = now - TRIGGER_WINDOW_SECONDS
    ts_list = [t for t in (timestamps or []) if t > cutoff]
    last_ts = ts_list[-1] if ts_list else 0
    if last_ts and (now - last_ts) < TRIGGER_COOLDOWN_SECONDS:
        return False, ts_list, len(ts_list)  # 冷却中，不计入
    ts_list.append(now)
    return True, ts_list, len(ts_list)


def _parse_field_keywords(cfg: dict) -> tuple:
    exact = [s.strip() for s in (cfg.get("exact") or []) if s and s.strip()]
    match_raw = [s.strip() for s in (cfg.get("match") or []) if s and s.strip()]
    match_list = []
    for s in match_raw:
        if s.startswith("/") and s.endswith("/") and len(s) > 2:
            match_list.append(("regex", re.compile(s[1:-1], re.I)))
        else:
            match_list.append(("str", s.lower()))
    return exact, match_list


def _build_ac(match_list: list):
    if not AHOCORASICK_AVAILABLE:
        return None
    str_kw = [item[1] for item in match_list if item[0] == "str" and item[1]]
    if not str_kw:
        return None
    automaton = ahocorasick.Automaton()
    for kw in str_kw:
        automaton.add_word(kw.lower(), kw.lower())
    automaton.make_automaton()
    return automaton


def _parse_whitelist_field(fc: dict) -> tuple:
    """解析白名单字段，返回 (exact, match_list)"""
    exact = [s.strip() for s in (fc.get("exact") or []) if s and s.strip()]
    match_raw = [s.strip() for s in (fc.get("match") or []) if s and s.strip()]
    match_list = []
    for s in match_raw:
        if s.startswith("/") and s.endswith("/") and len(s) > 2:
            try:
                match_list.append(("regex", re.compile(s[1:-1], re.I)))
            except re.error:
                pass
        else:
            match_list.append(("str", s.lower()))
    return exact, match_list


def load_spam_keywords():
    global spam_keywords
    if not SPAM_KEYWORDS_PATH.exists():
        return
    try:
        with open(SPAM_KEYWORDS_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        for field in ("text", "name", "bio"):
            fc = cfg.get(field) or {}
            ex, mt = _parse_field_keywords(fc)
            spam_keywords[field]["exact"] = ex
            spam_keywords[field]["match"] = mt
            spam_keywords[field]["_ac"] = _build_ac(mt)
            spam_keywords[field]["_regex"] = [x[1] for x in mt if x[0] == "regex"]
        wl = cfg.get("whitelist") or {}
        for field in ("name", "text"):
            fc = wl.get(field) or {}
            ex, mt = _parse_whitelist_field(fc)
            spam_keywords["whitelist"][field]["exact"] = ex
            spam_keywords["whitelist"][field]["match"] = mt
            spam_keywords["whitelist"][field]["_regex"] = [x[1] for x in mt if x[0] == "regex"]
    except Exception as e:
        print(f"[shared] 加载关键词失败: {e}")


def save_spam_keywords():
    try:
        cfg = {}
        for field in ("text", "name", "bio"):
            kw = spam_keywords.get(field) or {}
            exact = kw.get("exact") or []
            match = []
            for x in (kw.get("match") or []):
                if x[0] == "str":
                    match.append(x[1])
                else:
                    match.append(f"/{x[1].pattern}/")
            cfg[field] = {"exact": exact, "match": match}
        wl_cfg = {}
        for field in ("name", "text"):
            kw = (spam_keywords.get("whitelist") or {}).get(field) or {}
            exact = kw.get("exact") or []
            match = []
            for x in (kw.get("match") or []):
                if x[0] == "str":
                    match.append(x[1])
                else:
                    match.append(f"/{x[1].pattern}/")
            wl_cfg[field] = {"exact": exact, "match": match}
        cfg["whitelist"] = wl_cfg
        with open(SPAM_KEYWORDS_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[shared] 保存关键词失败: {e}")


def _is_in_keyword_whitelist(field: str, value: str) -> bool:
    """检查 value 是否命中白名单（精确/子串/正则），命中则不应加入垃圾关键词"""
    if field not in ("name", "text") or not (value or "").strip():
        return False
    wl = (spam_keywords.get("whitelist") or {}).get(field) or {}
    vl = (value or "").strip().lower()
    for ex in (wl.get("exact") or []):
        if vl == (ex or "").strip().lower():
            return True
    for x in (wl.get("match") or []):
        if x[0] == "str":
            if (x[1] or "").lower() in vl:
                return True
        elif x[0] == "regex" and x[1].search(value or ""):
            return True
    return False


def _keyword_exists_in_field(field: str, keyword: str, as_exact: bool, is_regex: bool) -> bool:
    """检查关键词是否已存在于 exact 或 match"""
    if field not in ("text", "name", "bio"):
        return False
    kw = spam_keywords.get(field) or {}
    kw_lower = (keyword or "").strip().lower()
    if as_exact:
        return kw_lower in [s.lower() for s in (kw.get("exact") or [])]
    if is_regex:
        try:
            pat = keyword.strip()
            if pat.startswith("/") and pat.endswith("/"):
                pat = pat[1:-1]
            rx = re.compile(pat, re.I)
            for x in (kw.get("match") or []):
                if x[0] == "regex" and x[1].pattern == rx.pattern:
                    return True
        except re.error:
            pass
        return False
    return kw_lower in [s.lower() for s in (x[1] for x in (kw.get("match") or []) if x[0] == "str")]


def _parse_keyword_input(text: str) -> tuple[bool, str, bool]:
    """解析用户输入：(as_exact, normalized_keyword, is_regex)
    - 直接输入如 加V -> match(子串)
    - / 前缀如 /加微信 或 / 加微信 -> exact(精确)
    - /正则/ 如 /加微.*/ -> match(regex)
    """
    t = (text or "").strip()
    if not t:
        return False, "", False
    if t.startswith("/") and t.endswith("/") and len(t) > 2:
        inner = t[1:-1]
        if any(c in inner for c in ".^$*+?{}[]|()"):
            try:
                re.compile(inner)
                return False, t, True  # regex match
            except re.error:
                pass
        return True, inner, False  # /加微信/ 视为 exact
    if t.startswith("/"):
        return True, t.lstrip("/").strip(), False  # exact
    return False, t, False  # match (子串)


def add_spam_keyword(field: str, keyword: str, is_regex: bool = False, as_exact: bool = None) -> bool:
    """as_exact: None=自动(is_regex时match否则exact), True=exact, False=match"""
    if field not in ("text", "name", "bio"):
        return False
    kw = spam_keywords[field]
    kw_lower = (keyword or "").strip().lower()
    use_exact = as_exact if as_exact is not None else (not is_regex)
    if is_regex:
        if not (keyword.startswith("/") and keyword.endswith("/") and len(keyword) > 2):
            return False
        try:
            rx = re.compile(keyword[1:-1], re.I)
        except re.error:
            return False
        for x in (kw.get("match") or []):
            if x[0] == "regex" and x[1].pattern == rx.pattern:
                return True
        kw["match"] = (kw.get("match") or []) + [("regex", rx)]
        kw["_regex"] = (kw.get("_regex") or []) + [rx]
    elif use_exact:
        if kw_lower in [s.lower() for s in (kw.get("exact") or [])]:
            return True
        kw["exact"] = (kw.get("exact") or []) + [keyword.strip()]
    else:
        for x in (kw.get("match") or []):
            if x[0] == "str" and x[1].lower() == kw_lower:
                return True
        kw["match"] = (kw.get("match") or []) + [("str", keyword.strip().lower())]
    kw["_ac"] = _build_ac([x for x in (kw.get("match") or []) if x[0] == "str"])
    return True


def remove_spam_keyword(field: str, keyword: str, is_regex: bool = False, as_exact: bool = None) -> bool:
    if field not in ("text", "name", "bio"):
        return False
    if as_exact is None:
        as_exact = not is_regex
    kw = spam_keywords[field]
    kw_lower = (keyword or "").strip().lower()
    if is_regex:
        pat = (keyword or "").strip()
        if pat.startswith("/") and pat.endswith("/"):
            pat = pat[1:-1]
        try:
            rx = re.compile(pat, re.I)
        except re.error:
            return False
        mt = [x for x in (kw.get("match") or []) if not (x[0] == "regex" and x[1].pattern == rx.pattern)]
        if len(mt) == len(kw.get("match") or []):
            return False
        kw["match"] = mt
        kw["_regex"] = [x[1] for x in mt if x[0] == "regex"]
    elif as_exact:
        ex = [s for s in (kw.get("exact") or []) if s.lower() != kw_lower]
        if len(ex) == len(kw.get("exact") or []):
            return False
        kw["exact"] = ex
    else:
        mt = [x for x in (kw.get("match") or []) if not (x[0] == "str" and x[1].lower() == kw_lower)]
        if len(mt) == len(kw.get("match") or []):
            return False
        kw["match"] = mt
        kw["_regex"] = [x[1] for x in mt if x[0] == "regex"]
    kw["_ac"] = _build_ac([x for x in (kw.get("match") or []) if x[0] == "str"])
    return True


def _check_field(kw_cfg: dict, value: str) -> Optional[str]:
    if not value:
        return None
    vl = value.lower()
    for kw in (kw_cfg.get("exact") or []):
        if vl == kw.lower():
            return kw
    ac = kw_cfg.get("_ac")
    if ac:
        for _, m in ac.iter(vl):
            return m
    for rx in (kw_cfg.get("_regex") or []):
        if rx.search(value):
            return rx.pattern
    return None


def check_spam_text(text: str) -> Optional[str]:
    return _check_field(spam_keywords.get("text") or {}, text or "")


def check_spam_name(first_name: str, last_name: str) -> Optional[str]:
    name = ((first_name or "") + " " + (last_name or "")).strip()
    return _check_field(spam_keywords.get("name") or {}, name)


def load_verified_users():
    global verified_users, verified_users_details, join_times
    if not VERIFIED_USERS_PATH.exists():
        return
    try:
        with open(VERIFIED_USERS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("users") or []
        verified_users.clear()
        for u in raw:
            uid = int(u) if isinstance(u, (int, str)) and str(u).isdigit() else None
            if uid:
                verified_users.add(uid)
        details = data.get("details") or {}
        join_t = data.get("join_times") or {}
        verified_users_details.clear()
        join_times.clear()
        for k, v in details.items():
            uid = int(k) if str(k).isdigit() else None
            if uid and isinstance(v, dict):
                verified_users_details[uid] = v
        for k, t in join_t.items():
            uid = int(k) if str(k).isdigit() else None
            if uid and t:
                join_times[uid] = t
    except Exception as e:
        print(f"[shared] 加载白名单失败: {e}")


def _verification_failures_ent_to_timestamps(ent) -> list:
    """将旧格式 {count, first_ts} 或新格式 {timestamps} 转为时间戳列表"""
    if isinstance(ent, dict) and "timestamps" in ent:
        return list(ent.get("timestamps") or [])
    if isinstance(ent, dict) and "first_ts" in ent:
        first = ent.get("first_ts", 0)
        cnt = int(ent.get("count", 1))
        return [first] * cnt
    return []


def load_verification_failures():
    global verification_failures
    if not VERIFICATION_FAILURES_PATH.exists():
        return
    try:
        with open(VERIFICATION_FAILURES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        verification_failures.clear()
        now = time.time()
        for k, v in (data.get("failures") or {}).items():
            parts = str(k).split(":", 1)
            if len(parts) != 2 or not parts[1].isdigit():
                continue
            key = (parts[0], int(parts[1]))
            ts_list = _verification_failures_ent_to_timestamps(v)
            ts_list = [t for t in ts_list if now - t <= VERIFY_FAILURES_RETENTION_SECONDS]
            if ts_list:
                verification_failures[key] = {"timestamps": ts_list}
    except Exception as e:
        print(f"[shared] 加载失败计数失败: {e}")


def load_verification_blacklist():
    global verification_blacklist
    verification_blacklist = set()
    if not VERIFICATION_BLACKLIST_PATH.exists():
        return
    try:
        with open(VERIFICATION_BLACKLIST_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        for u in data.get("users") or []:
            uid = int(u) if isinstance(u, (int, str)) and str(u).isdigit() else None
            if uid:
                verification_blacklist.add(uid)
    except Exception as e:
        print(f"[shared] 加载黑名单失败: {e}")


def chat_allowed(chat_id: str, target_ids: set) -> bool:
    return bool(target_ids and str(chat_id) in target_ids)


def is_admin(user_id: int, admin_ids: set) -> bool:
    return not admin_ids or user_id in admin_ids


def add_verified_user(user_id: int, username: str = None, full_name: str = None):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    verified_users.add(user_id)
    verified_users_details[user_id] = {
        "user_id": user_id, "username": username, "full_name": full_name or "用户",
        "join_time": join_times.get(user_id), "verify_time": now,
    }


def save_verified_users():
    try:
        data = {
            "users": list(verified_users),
            "details": {str(u): v for u, v in verified_users_details.items()},
            "join_times": {str(u): t for u, t in join_times.items()},
        }
        with open(VERIFIED_USERS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[shared] 保存白名单失败: {e}")


def increment_verification_failures(chat_id: str, user_id: int) -> int:
    """验证码错误计数，冷却+窗口内 5 次即限制。返回当前窗口内次数。"""
    key = (chat_id, user_id)
    now = time.time()
    ent = verification_failures.get(key)
    ts_list = _verification_failures_ent_to_timestamps(ent) if ent else []
    ts_list = [t for t in ts_list if now - t <= VERIFY_FAILURES_RETENTION_SECONDS]
    should_count, new_ts, cnt = _apply_trigger_cooldown_window(ts_list, now)
    if should_count:
        verification_failures[key] = {"timestamps": new_ts}
    return cnt


def save_verification_failures():
    try:
        now = time.time()
        to_save = {}
        for (c, u), v in verification_failures.items():
            ts_list = _verification_failures_ent_to_timestamps(v)
            ts_list = [t for t in ts_list if now - t <= VERIFY_FAILURES_RETENTION_SECONDS]
            if ts_list:
                to_save[f"{c}:{u}"] = {"timestamps": ts_list}
        with open(VERIFICATION_FAILURES_PATH, "w", encoding="utf-8") as f:
            json.dump({"failures": to_save}, f, ensure_ascii=False)
    except Exception as e:
        print(f"[shared] 保存失败计数失败: {e}")


def add_to_blacklist(user_id: int):
    verification_blacklist.add(user_id)
    verified_users.discard(user_id)
    verified_users_details.pop(user_id, None)


def _record_key(chat_id: str, message_id: int) -> str:
    return f"{chat_id}:{message_id}"


def load_verification_records():
    global _verification_records
    _verification_records = {}
    if not VERIFICATION_RECORDS_PATH.exists():
        return
    try:
        with open(VERIFICATION_RECORDS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        _verification_records = data.get("records") or {}
    except Exception as e:
        print(f"[shared] 加载验证记录失败: {e}")


def save_verification_records():
    try:
        items = list(_verification_records.items())
        if len(items) > 10000:
            items = sorted(items, key=lambda x: (x[1].get("started_at") or ""), reverse=True)[:10000]
            _verification_records.clear()
            _verification_records.update(dict(items))
        with open(VERIFICATION_RECORDS_PATH, "w", encoding="utf-8") as f:
            json.dump({"records": _verification_records}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[shared] 保存验证记录失败: {e}")


def _serialize_message_body(msg) -> dict | None:
    """将 PTB Message 序列化为可 JSON 存储的完整消息体"""
    if msg is None:
        return None
    try:
        body = {
            "message_id": getattr(msg, "message_id", None),
            "date": getattr(msg, "date", None),
            "text": (getattr(msg, "text", None) or "")[:20],
            "caption": (getattr(msg, "caption", None) or "")[:20],
        }
        if body.get("date"):
            body["date"] = body["date"].isoformat() if hasattr(body["date"], "isoformat") else str(body["date"])
        chat = getattr(msg, "chat", None)
        if chat:
            body["chat"] = {"id": getattr(chat, "id", None), "type": getattr(chat, "type", None)}
        user = getattr(msg, "from_user", None)
        if user:
            body["from_user"] = {
                "id": getattr(user, "id", None),
                "first_name": getattr(user, "first_name", None),
                "last_name": getattr(user, "last_name", None),
                "username": getattr(user, "username", None),
            }
        entities = getattr(msg, "entities", None) or []
        body["entities"] = [
            {"type": str(getattr(e, "type", None)), "offset": getattr(e, "offset", 0), "length": getattr(e, "length", 0),
             **({"url": getattr(e, "url", None)} if hasattr(e, "url") and getattr(e, "url") else {})}
            for e in entities
        ]
        cap_entities = getattr(msg, "caption_entities", None) or []
        body["caption_entities"] = [
            {"type": str(getattr(e, "type", None)), "offset": getattr(e, "offset", 0), "length": getattr(e, "length", 0),
             **({"url": getattr(e, "url", None)} if hasattr(e, "url") and getattr(e, "url") else {})}
            for e in cap_entities
        ]
        reply = getattr(msg, "reply_to_message", None)
        if reply:
            body["reply_to_message_id"] = getattr(reply, "message_id", None)
        return body
    except Exception as e:
        print(f"[PTB] 序列化消息体失败: {e}")
        return None


def add_verification_record(
    chat_id: str, message_id: int, user_id: int,
    full_name: str, username: str, trigger_reason: str,
    msg_preview: str = "", initial_status: str = "pending",
    hit_keyword: str = "", raw_message_body: dict | None = None,
) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    k = _record_key(chat_id, message_id)
    rec = {
        "chat_id": chat_id, "message_id": message_id,
        "user_id": user_id, "full_name": full_name or "用户", "username": username or "",
        "trigger_reason": trigger_reason, "status": initial_status,
        "started_at": now, "msg_preview": (msg_preview or "")[:200],
    }
    if hit_keyword:
        rec["hit_keyword"] = hit_keyword
    if raw_message_body is not None:
        rec["raw_body"] = raw_message_body
    _verification_records[k] = rec


def update_verification_record(chat_id: str, message_id: int, status: str, fail_count: int = None) -> None:
    k = _record_key(chat_id, message_id)
    if k not in _verification_records:
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _verification_records[k]["status"] = status
    _verification_records[k]["updated_at"] = now
    if fail_count is not None:
        _verification_records[k]["fail_count"] = fail_count


def get_verification_record(chat_id: str, message_id: int) -> dict | None:
    k = _record_key(chat_id, message_id)
    return _verification_records.get(k)


def save_verification_blacklist():
    try:
        with open(VERIFICATION_BLACKLIST_PATH, "w", encoding="utf-8") as f:
            json.dump({"users": list(verification_blacklist)}, f, ensure_ascii=False)
    except Exception as e:
        print(f"[shared] 保存黑名单失败: {e}")


def sync_lottery_winners() -> tuple[int, str]:
    if os.name == "nt":
        return 0, "抽奖同步仅适用于 Linux/Ubuntu，Windows 下已跳过"
    db_path = LOTTERY_DB_PATH
    if not db_path or not Path(db_path).exists():
        return 0, "lottery.db 不存在"
    added = 0
    collected: set[int] = set()
    found_compatible = False
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]

        def add_from_table(tbl: str, col: str):
            nonlocal found_compatible
            found_compatible = True
            cur.execute(f"SELECT DISTINCT {col} FROM {tbl}")
            for row in cur.fetchall():
                if row[0] is not None:
                    try:
                        uid = int(row[0])
                        if uid not in verified_users and uid not in verification_blacklist and uid not in collected:
                            add_verified_user(uid, None, "抽奖中奖")
                            collected.add(uid)
                    except (TypeError, ValueError):
                        pass

        # 1. 兼容表：winners, lottery_winners, lottery_entries, participants
        for t in ("winners", "lottery_winners", "lottery_entries", "participants"):
            if t not in tables:
                continue
            cur.execute(f"PRAGMA table_info({t})")
            cols = [r[1].lower() for r in cur.fetchall()]
            for c in ("user_id", "telegram_id", "uid", "user"):
                if c in cols:
                    add_from_table(t, c)
                    break

        # 2. user_participations 表（参与抽奖用户）
        if "user_participations" in tables:
            cur.execute("PRAGMA table_info(user_participations)")
            cols = [r[1].lower() for r in cur.fetchall()]
            if "user_id" in cols:
                add_from_table("user_participations", "user_id")

        # 3. lotteries.winners 列（JSON 数组，如 [8317097354, 7696931296, ...]）
        if "lotteries" in tables:
            cur.execute("PRAGMA table_info(lotteries)")
            cols = [r[1].lower() for r in cur.fetchall()]
            if "winners" in cols:
                found_compatible = True
                cur.execute("SELECT winners FROM lotteries WHERE winners IS NOT NULL AND winners != ''")
                for row in cur.fetchall():
                    try:
                        uids = json.loads(row[0])
                        for uid in (uids or []):
                            try:
                                uid = int(uid)
                                if uid not in verified_users and uid not in verification_blacklist and uid not in collected:
                                    add_verified_user(uid, None, "抽奖中奖")
                                    collected.add(uid)
                            except (TypeError, ValueError):
                                pass
                    except (json.JSONDecodeError, TypeError):
                        pass

        # 4. group_publish_whitelist 表
        if "group_publish_whitelist" in tables:
            cur.execute("PRAGMA table_info(group_publish_whitelist)")
            cols = [r[1].lower() for r in cur.fetchall()]
            if "user_id" in cols:
                add_from_table("group_publish_whitelist", "user_id")

        conn.close()
        added = len(collected)
        if not found_compatible:
            return 0, "未找到兼容的抽奖表结构"
        if added > 0:
            save_verified_users()
        return added, f"同步 {added} 个中奖用户" if added else "无新中奖用户"
    except sqlite3.OperationalError as e:
        return 0, f"lottery.db 只读打开失败: {e}"
    except Exception as e:
        return 0, f"抽奖同步异常: {e}"


# ==================== PTB 霜刃逻辑 ====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GROUP_ID_STR = os.getenv("GROUP_ID", "")
_ENV_GROUP_IDS = {s.strip() for s in GROUP_ID_STR.split(",") if s.strip()}
# 霜刃可用群 = 环境变量 + target_groups.json 中通过 /add_group 添加的
TARGET_GROUP_IDS: set = set()
TARGET_GROUPS_PATH = _path("target_groups.json")
# 全局 B 群（未配置群级时使用）；群级配置见 bgroup_config.json、/set_bgroup
REQUIRED_GROUP_ID = (os.getenv("REQUIRED_GROUP_ID") or os.getenv("REQUIRED_GROUP_IDS") or "").strip()
ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = {int(x.strip()) for x in ADMIN_IDS_STR.split(",") if x.strip().isdigit()}
RESTRICTED_USERS_LOG_PATH = _BASE / "restricted_users.jsonl"
DELETED_CONTENT_LOG_PATH = _BASE / "bio_calls.jsonl"  # 被删除文案记录：time, user_id, full_name, deleted_content
VERIFY_TIMEOUT = 90
VERIFY_MSG_DELETE_AFTER = 30
REQUIRED_GROUP_MSG_DELETE_AFTER = 90  # 入群验证消息 90 秒后自动删除；可通过 /settime 配置

_settime_config: dict = {}  # {"required_group_msg_delete_after": 90, "verify_msg_delete_after": 30}


def _load_settime_config():
    global _settime_config
    try:
        if SETTIME_CONFIG_PATH.exists():
            with open(SETTIME_CONFIG_PATH, "r", encoding="utf-8") as f:
                _settime_config = json.load(f)
        else:
            _settime_config = {}
    except Exception as e:
        print(f"[PTB] 加载 settime 配置失败: {e}")
        _settime_config = {}


def _get_required_group_msg_delete_after() -> int:
    return int(_settime_config.get("required_group_msg_delete_after", REQUIRED_GROUP_MSG_DELETE_AFTER))


def _get_verify_msg_delete_after() -> int:
    return int(_settime_config.get("verify_msg_delete_after", VERIFY_MSG_DELETE_AFTER))


def _save_settime_config():
    try:
        with open(SETTIME_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(_settime_config, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[PTB] 保存 settime 配置失败: {e}")


_load_settime_config()

_bgroup_config: dict = {}  # chat_id_str -> str | None（仅一个 B 群，None 表示不校验）


def _load_bgroup_config():
    global _bgroup_config
    try:
        if BGROUP_CONFIG_PATH.exists():
            with open(BGROUP_CONFIG_PATH, "r", encoding="utf-8") as f:
                _bgroup_config = json.load(f)
        else:
            _bgroup_config = {}
    except Exception as e:
        print(f"[PTB] 加载 B 群配置失败: {e}")
        _bgroup_config = {}


def _save_bgroup_config():
    try:
        with open(BGROUP_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(_bgroup_config, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[PTB] 保存 B 群配置失败: {e}")


def get_bgroup_ids_for_chat(chat_id: str) -> list:
    """获取某群的 B 群 ID（仅一个）。无全局配置，未设置或设为空则返回 [] 表示不校验。"""
    val = _bgroup_config.get(str(chat_id))
    if val is None:
        return []
    s = str(val).strip()
    if not s or not s.lstrip("-").isdigit():
        return []
    return [s]


def set_bgroup_for_chat(chat_id: str, b_id: str | None) -> bool:
    """设置某群的 B 群。b_id: 群 ID 字符串，None/空 表示不校验（删除配置）。"""
    cid = str(chat_id)
    if b_id is None or (isinstance(b_id, str) and not b_id.strip()):
        if cid in _bgroup_config:
            del _bgroup_config[cid]
            _save_bgroup_config()
            return True
        return False
    s = str(b_id).strip()
    if not s or not s.lstrip("-").isdigit():
        return False
    _bgroup_config[cid] = s
    _save_bgroup_config()
    return True


async def _resolve_bgroup_input(bot, raw: str) -> str | None:
    """解析 B 群输入：@channel、https://t.me/xxx、-1001234567890。返回 chat_id 字符串或 None。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.lstrip("-").isdigit():
        return raw
    if raw.startswith("@"):
        try:
            chat = await bot.get_chat(raw)
            return str(chat.id)
        except Exception:
            return None
    m = re.search(r"t\.me/c/(\d+)", raw, re.I)
    if m:
        num = m.group(1)
        if num.isdigit():
            return str(-1000000000000 - int(num))
    m = re.search(r"t\.me/([a-zA-Z0-9_]+)", raw, re.I)
    if m:
        username = m.group(1)
        if username.lower() != "c":
            try:
                chat = await bot.get_chat(f"@{username}")
                return str(chat.id)
            except Exception:
                pass
    return None


_load_bgroup_config()


def _load_target_groups():
    """加载通过 /add_group 添加的群 ID，合并到 TARGET_GROUP_IDS"""
    global TARGET_GROUP_IDS
    TARGET_GROUP_IDS = set(_ENV_GROUP_IDS)
    try:
        if TARGET_GROUPS_PATH.exists():
            with open(TARGET_GROUPS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            for g in (data.get("groups") or []):
                s = str(g).strip()
                if s and s.lstrip("-").isdigit():
                    TARGET_GROUP_IDS.add(s)
    except Exception as e:
        print(f"[PTB] 加载 target_groups 失败: {e}")


def _save_target_groups():
    """保存 target_groups（仅 /add_group 添加的，不含 env）"""
    try:
        extra = [g for g in TARGET_GROUP_IDS if g not in _ENV_GROUP_IDS]
        with open(TARGET_GROUPS_PATH, "w", encoding="utf-8") as f:
            json.dump({"groups": sorted(extra)}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[PTB] 保存 target_groups 失败: {e}")


def _add_target_group(gid: str) -> bool:
    """添加群到监控列表，返回是否为新添加"""
    gid = str(gid).strip()
    if not gid or not gid.lstrip("-").isdigit():
        return False
    if gid in TARGET_GROUP_IDS:
        return False
    TARGET_GROUP_IDS.add(gid)
    _save_target_groups()
    return True


def _group_id_to_link(gid: str) -> str:
    """将群 ID 转为可点击链接。公开群需通过 get_chat 获取 username。"""
    gid = str(gid).strip()
    if gid.startswith("-100") and len(gid) > 4:
        return f"https://t.me/c/{gid[4:]}"
    return f"https://t.me/c/{gid}"  # fallback


async def _get_group_display_info(bot, gid: str) -> tuple[str, str]:
    """获取群展示信息：(title, link)。失败时返回 (gid, _group_id_to_link(gid))"""
    try:
        chat = await bot.get_chat(chat_id=int(gid))
        title = (getattr(chat, "title", None) or "").strip() or gid
        username = (getattr(chat, "username", None) or "").strip()
        link = f"https://t.me/{username}" if username else _group_id_to_link(gid)
        return (title, link)
    except Exception:
        return (gid, _group_id_to_link(gid))


def _escape_html(s: str) -> str:
    """HTML 转义，用于 Telegram parse_mode=HTML"""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def _resolve_group_input(bot, raw: str) -> str | None:
    """解析群输入：@群、https://t.me/xxx、-1001234567890。返回 chat_id 字符串或 None。仅支持群/超级群。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.lstrip("-").isdigit():
        return raw
    if raw.startswith("@"):
        try:
            chat = await bot.get_chat(raw)
            if getattr(chat, "type", "") in ("group", "supergroup"):
                return str(chat.id)
        except Exception:
            pass
        return None
    m = re.search(r"t\.me/c/(\d+)", raw, re.I)
    if m:
        num = m.group(1)
        if num.isdigit():
            return str(-1000000000000 - int(num))
    m = re.search(r"t\.me/([a-zA-Z0-9_]+)", raw, re.I)
    if m:
        username = m.group(1)
        if username.lower() != "c":
            try:
                chat = await bot.get_chat(f"@{username}")
                if getattr(chat, "type", "") in ("group", "supergroup"):
                    return str(chat.id)
            except Exception:
                pass
    return None


_load_target_groups()

REQUIRED_GROUP_RESTRICT_HOURS = float(os.getenv("REQUIRED_GROUP_RESTRICT_HOURS", "24"))  # 未加入 B 群 5 次后限制时长（小时），默认 24 即一天
VERIFY_RESTRICT_DURATION = 1
UNBAN_BOT_USERNAME = os.getenv("UNBAN_BOT_USERNAME", "@XHNPBOT")
KIMI_API_KEY = os.getenv("OPENAI_API_KEY", "")
KIMI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.moonshot.cn/v1")
KIMI_MODEL = os.getenv("MODEL_NAME", "moonshot-v1-128k")
XHCHAT_BOT_USERNAME = (os.getenv("XHCHAT_BOT_USERNAME") or os.getenv("BOT_USERNAME") or "").strip().lstrip("@")
BOT_NICKNAME = (os.getenv("BOT_NICKNAME") or "").strip()  # 机器人显示昵称，用于消息内容；未配置则用 get_me().first_name

pending_verification = {}
# 未加入 B 群的触发时间戳，(chat_id, uid) -> [t1,t2,...]，冷却+窗口内 5 次即限制
_required_group_warn_count: dict[tuple[str, int], list] = {}
_LINK_RE = re.compile(r"t\.me/c/(\d+)/(\d+)", re.I)
_LINK_PUBLIC_RE = re.compile(r"t\.me/([a-zA-Z0-9_]+)/(\d+)", re.I)
_bot_me_cache = None  # get_me() 结果，ExtBot 不允许动态属性
# B 群信息缓存：b_group_id -> (title, link, ts)，TTL 1 天
_required_group_info_cache: dict[str, tuple[str, str, float]] = {}
_REQUIRED_GROUP_INFO_CACHE_TTL = 86400
# 用户是否在 B 群缓存，(user_id, b_group_id) -> (is_in, ts)
_user_in_required_group_cache: dict[tuple[int, str], tuple[bool, float]] = {}
_USER_IN_GROUP_CACHE_TTL = 86400  # 「在」时缓存 1 天
_USER_IN_GROUP_CACHE_TTL_NOT_IN = 10  # 「不在」时仅缓存 10 秒


async def _is_user_in_required_group(bot, user_id: int, chat_id: str, skip_cache: bool = False) -> bool:
    """判断用户是否在指定群的 B 群中（任一即可）。未配置则返回 True（不限制）。"""
    b_ids = get_bgroup_ids_for_chat(chat_id)
    if not b_ids:
        return True
    for b_id in b_ids:
        key = (user_id, b_id)
        now = time.time()
        if not skip_cache and key in _user_in_required_group_cache:
            cached_val, ts = _user_in_required_group_cache[key]
            ttl = _USER_IN_GROUP_CACHE_TTL if cached_val else _USER_IN_GROUP_CACHE_TTL_NOT_IN
            if now - ts <= ttl:
                if cached_val:
                    return True
                continue
        try:
            member = await bot.get_chat_member(chat_id=int(b_id), user_id=user_id)
            status = getattr(member, "status", "") or ""
            is_in = status not in ("left", "kicked")
            _user_in_required_group_cache[key] = (is_in, now)
            print(f"[PTB] B群检查: uid={user_id} b_id={b_id} status={status!r} is_in={is_in} skip_cache={skip_cache}")
            if is_in:
                return True
        except Exception as e:
            print(f"[PTB] 检查用户 {user_id} 是否在 B 群 {b_id} 失败: {e}")
            return True  # 接口报错或异常时，默认用户在 B 群，不限制
    return False


async def _get_required_group_buttons(bot, chat_id: str) -> list[tuple[str, str]]:
    """获取某群的 B 群按钮列表 [(title, link), ...]，公开群/频道有 username 才有 link。"""
    b_ids = get_bgroup_ids_for_chat(chat_id)
    if not b_ids:
        return []
    result = []
    now = time.time()
    for b_id in b_ids:
        cached = _required_group_info_cache.get(b_id)
        if cached and len(cached) >= 3 and (now - cached[2]) <= _REQUIRED_GROUP_INFO_CACHE_TTL:
            result.append((cached[0], cached[1]))
            continue
        try:
            chat = await bot.get_chat(chat_id=int(b_id))
            title = (getattr(chat, "title", None) or "").strip() or f"群组 {b_id}"
            username = (getattr(chat, "username", None) or "").strip()
            link = f"https://t.me/{username}" if username else ""
            _required_group_info_cache[b_id] = (title, link, now)
            result.append((title, link))
        except Exception as e:
            print(f"[PTB] 获取 B 群 {b_id} 信息失败: {e}")
    return result


def _has_url_entity(msg) -> bool:
    entities = getattr(msg, "entities", None) or getattr(msg, "caption_entities", None) or []
    for e in entities:
        t = getattr(e, "type", None)
        if t in (MessageEntityType.URL, MessageEntityType.TEXT_LINK):
            return True
    return False


def _is_ad_message(msg) -> bool:
    text = (msg.text or msg.caption or "").strip()
    return _has_url_entity(msg) and len(text) <= 10


async def _is_mention_bot(msg, text: str, bot) -> bool:
    global _bot_me_cache
    if _bot_me_cache is None:
        try:
            _bot_me_cache = await bot.get_me()
        except Exception:
            return False
    me = _bot_me_cache
    entities = getattr(msg, "entities", None) or []
    for e in entities:
        if getattr(e, "type", None) == MessageEntityType.MENTION:
            men = (text or "")[e.offset : e.offset + e.length]
            if men and me.username and men.lower() == f"@{me.username}".lower():
                return True
    return False


async def _is_frost_trigger(msg, text: str, bot) -> bool:
    """判断是否触发霜刃 AI：以「霜刃，」开头、@提及霜刃、或回复霜刃的消息"""
    if (text or "").strip().startswith("霜刃，"):
        return True
    if await _is_mention_bot(msg, text or "", bot):
        return True
    reply = getattr(msg, "reply_to_message", None)
    if reply and getattr(reply, "from_user", None):
        if reply.from_user.id == bot.id:
            return True
    return False


# 含 emoji 检测：Unicode 常见 emoji 范围，预编译正则一次
_EMOJI_PATTERN = re.compile(
    r'[\U00002600-\U000027BF\U0001F300-\U0001F5FF\U0001F600-\U0001F64F'
    r'\U0001F680-\U0001F6FF\U0001F900-\U0001F9FF\U0001F1E0-\U0001F1FF]',
    re.UNICODE,
)


def _contains_emoji(s: str) -> bool:
    """检查字符串是否含 Unicode emoji"""
    return bool(s and _EMOJI_PATTERN.search(s))


def _is_reply_to_other_chat(msg, current_chat_id: int) -> bool:
    reply = getattr(msg, "reply_to_message", None)
    if not reply:
        return False
    if getattr(reply, "forward_origin", None) is not None:
        return True
    if getattr(reply, "external_reply", None) is not None:
        return True
    return False


def _parse_message_link(text: str) -> tuple[str, int] | None:
    """解析 t.me/c/123/456 格式（私密群），返回 (chat_id_str, msg_id) 或 None"""
    m = _LINK_RE.search(text or "")
    if not m:
        return None
    short_id, msg_id = m.group(1), m.group(2)
    try:
        chat_id = int(f"-100{short_id}")
        return (str(chat_id), int(msg_id))
    except ValueError:
        return None


async def _parse_message_link_async(text: str, bot) -> tuple[str, int] | None:
    """解析链接，支持 t.me/c/123/456 和 t.me/USERNAME/123（公开群）"""
    # 1. 私密群格式
    r = _parse_message_link(text)
    if r:
        return r
    # 2. 公开群格式 t.me/XHNPD/1956968
    m = _LINK_PUBLIC_RE.search(text or "")
    if not m:
        return None
    username, msg_id_str = m.group(1), m.group(2)
    try:
        chat = await bot.get_chat(f"@{username}")
        return (str(chat.id), int(msg_id_str))
    except Exception:
        return None


def _log_restriction(chat_id: str, user_id: int, full_name: str, action: str, until_date=None):
    try:
        until_str = until_date.isoformat() if isinstance(until_date, datetime) else until_date
        rec = {"time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "chat_id": chat_id,
               "user_id": user_id, "full_name": full_name or "", "action": action, "until_date": until_str}
        with open(RESTRICTED_USERS_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[PTB] 记录封禁日志失败: {e}")


def _log_deleted_content(user_id: int, full_name: str, deleted_content: str):
    """记录被删除的文案到 bio_calls.jsonl：time, user_id, full_name, deleted_content"""
    content = (deleted_content or "").strip()
    if not content:
        return
    try:
        rec = {
            "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "user_id": user_id,
            "full_name": full_name or "",
            "deleted_content": content[:500],
        }
        with open(DELETED_CONTENT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[PTB] 记录被删文案失败: {e}")


def _schedule_sync_background(func, *args, **kwargs):
    """将同步 IO 放入后台执行，不阻塞主流程"""
    def _run():
        try:
            func(*args, **kwargs)
        except Exception as e:
            print(f"[PTB] 后台任务失败: {e}")
    try:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, _run)
    except RuntimeError:
        func(*args, **kwargs)  # 无事件循环时同步执行


def _cleanup_expired_message_cache():
    """删除超过 TTL 的缓存消息"""
    now = time.time()
    expired = [k for k, (_, ts) in _last_message_by_user.items() if now - ts > LAST_MESSAGE_CACHE_TTL_SECONDS]
    for k in expired:
        del _last_message_by_user[k]


def _add_whitelist_keyword(field: str, keyword: str, is_regex: bool = False, as_exact: bool = None) -> bool:
    """白名单添加：管理员限制用户时不录入这些昵称/消息"""
    if field not in ("name", "text"):
        return False
    wl = (spam_keywords.get("whitelist") or {}).get(field) or {}
    kw_lower = (keyword or "").strip().lower()
    use_exact = as_exact if as_exact is not None else (not is_regex)
    if is_regex:
        if not (keyword.startswith("/") and keyword.endswith("/") and len(keyword) > 2):
            return False
        try:
            rx = re.compile(keyword[1:-1], re.I)
        except re.error:
            return False
        for x in (wl.get("match") or []):
            if x[0] == "regex" and x[1].pattern == rx.pattern:
                return True
        wl.setdefault("match", []).append(("regex", rx))
    elif use_exact:
        if kw_lower in [s.lower() for s in (wl.get("exact") or [])]:
            return True
        wl.setdefault("exact", []).append(keyword.strip())
    else:
        for x in (wl.get("match") or []):
            if x[0] == "str" and x[1] == kw_lower:
                return True
        wl.setdefault("match", []).append(("str", kw_lower))
    wl["_regex"] = [x[1] for x in (wl.get("match") or []) if x[0] == "regex"]
    return True


def _remove_whitelist_keyword(field: str, keyword: str, is_regex: bool = False, as_exact: bool = None) -> bool:
    if field not in ("name", "text"):
        return False
    wl = (spam_keywords.get("whitelist") or {}).get(field) or {}
    kw_lower = (keyword or "").strip().lower()
    use_exact = as_exact if as_exact is not None else (not is_regex)
    if is_regex:
        pat = (keyword or "").strip()
        if pat.startswith("/") and pat.endswith("/"):
            pat = pat[1:-1]
        try:
            rx = re.compile(pat, re.I)
        except re.error:
            return False
        mt = [x for x in (wl.get("match") or []) if not (x[0] == "regex" and x[1].pattern == rx.pattern)]
        if len(mt) == len(wl.get("match") or []):
            return False
        wl["match"] = mt
    elif use_exact:
        ex = [s for s in (wl.get("exact") or []) if s.lower() != kw_lower]
        if len(ex) == len(wl.get("exact") or []):
            return False
        wl["exact"] = ex
    else:
        mt = [x for x in (wl.get("match") or []) if not (x[0] == "str" and x[1] == kw_lower)]
        if len(mt) == len(wl.get("match") or []):
            return False
        wl["match"] = mt
    wl["_regex"] = [x[1] for x in (wl.get("match") or []) if x[0] == "regex"]
    return True


def _add_keywords_from_admin_action(chat_id: str, user_id: int, full_name: str):
    """管理员（真人）删除并限制/封禁用户时，将昵称加入 name 关键词、被删消息加入 text 关键词。白名单中的不录入。"""
    name_trimmed = (full_name or "").strip()
    if name_trimmed and not _is_in_keyword_whitelist("name", name_trimmed):
        as_exact_name = len(name_trimmed) <= 2
        if add_spam_keyword("name", name_trimmed, as_exact=as_exact_name):
            save_spam_keywords()
            print(f"[PTB] 管理员操作: 已加入 name 关键词 {name_trimmed!r} (exact={as_exact_name})")
    elif name_trimmed:
        print(f"[PTB] 管理员操作: 昵称 {name_trimmed!r} 在白名单中，跳过")
    key = (chat_id, user_id)
    entry = _last_message_by_user.pop(key, None)
    msg_text = ""
    if entry:
        text_val, ts = entry
        if time.time() - ts <= LAST_MESSAGE_CACHE_TTL_SECONDS:
            msg_text = (text_val or "").strip()
        else:
            print(f"[PTB] 管理员操作: 用户 {user_id} 消息缓存已过期(>{LAST_MESSAGE_CACHE_TTL_SECONDS//3600}h)，未加入 text 关键词")
    else:
        print(f"[PTB] 管理员操作: 用户 {user_id} 无消息缓存(可能仅发过图/贴纸/无文字，或 bot 重启后未收到其新消息)，未加入 text 关键词")
    if msg_text:
        _log_deleted_content(user_id, full_name, msg_text)
        msg_text = msg_text[:200]  # 截断避免过长
        if not _is_in_keyword_whitelist("text", msg_text):
            as_exact_text = len(msg_text) <= 2
            if add_spam_keyword("text", msg_text, as_exact=as_exact_text):
                save_spam_keywords()
                print(f"[PTB] 管理员操作: 已加入 text 关键词 {msg_text[:50]!r}... (exact={as_exact_text})")
        else:
            print(f"[PTB] 管理员操作: 消息 {msg_text[:50]!r}... 在白名单中，跳过")


async def chat_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """接收成员状态变更（限制/封禁/踢出）。注意：Bot 必须是群管理员才能收到此更新。"""
    if not update.chat_member:
        return
    cm = update.chat_member
    chat_id = str(cm.chat.id)
    user = cm.new_chat_member.user
    uid = user.id
    new = cm.new_chat_member
    old = cm.old_chat_member
    # 调试：每次收到 chat_member 都打印，便于确认 Bot 是否收到更新
    print(f"[PTB] chat_member 收到: chat_id={chat_id} uid={uid} old={old.status} new={getattr(new, 'status', type(new).__name__)}")
    if not chat_allowed(chat_id, TARGET_GROUP_IDS):
        print(f"[PTB] chat_member 跳过: 群 {chat_id} 不在监控列表")
        return
    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or (user.username or f"用户{uid}")

    if new.status == "member" and old.status in ("left", "kicked", "restricted"):
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        join_times[uid] = now_iso
        if user.is_bot:
            add_verified_user(uid, user.username, full_name)
            save_verified_users()
        return

    if isinstance(new, ChatMemberBanned):
        print(f"[PTB] 管理员操作: 用户 {uid} 被封禁，加入关键词")
        _log_restriction(chat_id, uid, full_name, "banned", new.until_date)
        add_to_blacklist(uid)
        _add_keywords_from_admin_action(chat_id, uid, full_name)
        save_verified_users()
        save_verification_blacklist()
    elif isinstance(new, ChatMemberRestricted):
        print(f"[PTB] 管理员操作: 用户 {uid} 被限制，加入关键词")
        _log_restriction(chat_id, uid, full_name, "restricted", new.until_date)
        add_to_blacklist(uid)
        _add_keywords_from_admin_action(chat_id, uid, full_name)
        save_verified_users()
        save_verification_blacklist()
    elif isinstance(new, ChatMemberLeft):
        if old.status not in ("left", "kicked"):
            print(f"[PTB] 管理员操作: 用户 {uid} 被踢出，加入关键词")
            _log_restriction(chat_id, uid, full_name, "kicked", None)
            add_to_blacklist(uid)
            _add_keywords_from_admin_action(chat_id, uid, full_name)
            save_verified_users()
            save_verification_blacklist()


async def group_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat:
        return
    msg = update.message
    chat_id = str(msg.chat_id)
    text = (msg.text or msg.caption or "").strip()
    if not chat_allowed(chat_id, TARGET_GROUP_IDS):
        if text and await _is_frost_trigger(msg, text, context.bot):
            print(f"[PTB] 群 {chat_id} 不在目标列表 {TARGET_GROUP_IDS}，忽略霜刃唤醒")
        print(f"[PTB] 群消息跳过(无记录): chat_id={chat_id} 不在监控列表")
        return

    user = msg.from_user
    if not user:
        print(f"[PTB] 群消息跳过(无记录): chat_id={chat_id} msg_id={msg.message_id} 无 from_user")
        return
    uid = user.id
    first_name = user.first_name or ""
    last_name = user.last_name or ""

    # 0. /setlimit 的后续输入
    key = (chat_id, uid)
    if key in pending_setlimit:
        info = pending_setlimit[key]
        if (time.time() - info.get("timestamp", 0)) > PENDING_SETLIMIT_TIMEOUT:
            pending_setlimit.pop(key, None)
            await update.message.reply_text("已超时，请重新发送 /setlimit")
            return
        pending_setlimit.pop(key, None)
        if not text:
            await update.message.reply_text("请输入 B 群/频道，或 /cancel 取消")
            pending_setlimit[key] = {"timestamp": time.time()}
            return
        b_id = await _resolve_bgroup_input(context.bot, text)
        if b_id:
            set_bgroup_for_chat(chat_id, b_id)
            await update.message.reply_text(f"已设置 B 群：{b_id}\n用户需加入才能发言")
        else:
            await update.message.reply_text("解析失败，请发送 @频道、https://t.me/xxx 或 -1001234567890")
            pending_setlimit[key] = {"timestamp": time.time()}
        return

    # 缓存用户最近消息（必须在所有 return 之前），供管理员删除+限制/封禁时自动加入 text 关键词
    if text:
        _last_message_by_user[(chat_id, uid)] = (text[:500], time.time())
        if len(_last_message_by_user) % 100 == 0:  # 每 100 条消息清理一次过期缓存
            _cleanup_expired_message_cache()

    # 未加入 B 群？→ 触发验证（在霜刃唤醒之前）；白名单用户跳过缓存，确保离开 B 群后立即触发
    if get_bgroup_ids_for_chat(chat_id) and not (await _is_user_in_required_group(context.bot, uid, chat_id, skip_cache=(uid in verified_users))):
        print(f"[PTB] 群消息已记录: chat_id={chat_id} msg_id={msg.message_id} 触发验证(not_in_required_group)")
        await _start_required_group_verification(context.bot, msg, chat_id, uid, first_name, last_name)
        return

    if await _is_frost_trigger(msg, text or "", context.bot):
        print(f"[PTB] 收到霜刃唤醒 chat={chat_id} uid={uid} msg_id={msg.message_id} text={text[:50]!r} [霜刃唤醒不建记录]")
        await _maybe_ai_trigger(context.bot, msg, chat_id, uid, text or "", first_name, last_name)
        return

    if uid in verified_users:
        add_verification_record(
            chat_id, msg.message_id, uid,
            f"{first_name} {last_name}".strip(), getattr(user, "username", None) or "",
            "normal", (text or "")[:200], initial_status="verified_pass",
            raw_message_body=_serialize_message_body(msg),
        )
        save_verification_records()
        print(f"[PTB] 群消息已记录: chat_id={chat_id} msg_id={msg.message_id} verified_pass")
        return

    key = (chat_id, uid)
    if key in pending_verification:
        pb = pending_verification[key]
        if time.time() - pb["time"] <= VERIFY_TIMEOUT:
            ok = text == pb["code"] or text == f"验证码{pb['code']}"
            msg_id = pb.get("msg_id")
            if ok:
                if msg_id is not None:
                    update_verification_record(chat_id, msg_id, "passed")
                    save_verification_records()
                else:
                    print(f"[PTB] 警告: 验证通过但 msg_id 为空，跳过记录更新 chat_id={chat_id} uid={uid}")
                add_verified_user(uid, user.username, f"{first_name} {last_name}".strip())
                save_verified_users()
                verification_failures.pop(key, None)
                verification_blacklist.discard(uid)
                pending_verification.pop(key, None)
                print(f"[PTB] 群消息已记录: chat_id={chat_id} msg_id={msg.message_id} 验证通过(更新原记录) | 当前验证码消息msg_id={msg.message_id} [验证码消息不单独建记录]")
                await msg.reply_text(f"【{first_name} {last_name}】\n\n✓ 验证通过\n\n已将您加入白名单，可以正常发言了。")
            else:
                cnt = increment_verification_failures(chat_id, uid)
                save_verification_failures()
                if cnt >= VERIFY_FAIL_THRESHOLD:
                    await _restrict_and_notify(context.bot, chat_id, uid, f"{first_name} {last_name}", msg_id)
                else:
                    left = VERIFY_FAIL_THRESHOLD - cnt
                    print(f"[PTB] 群消息: chat_id={chat_id} 验证码错误 msg_id={msg.message_id} [验证码消息不单独建记录]")
                    try:
                        await msg.delete()
                    except Exception:
                        pass
                    vmsg = await msg.reply_text(f"验证失败，再失败 {left} 次将被限制发言")
                    asyncio.create_task(_delete_after(context.bot, int(chat_id), vmsg.message_id, _get_verify_msg_delete_after(), user_msg_id=msg.message_id))
            return
        # 超时 = 未完成验证，计 1 次失败，杜绝「间隔发违禁词」规避限制
        cnt = increment_verification_failures(chat_id, uid)
        save_verification_failures()
        msg_id = pb.get("msg_id")
        if cnt >= VERIFY_FAIL_THRESHOLD:
            await _restrict_and_notify(context.bot, chat_id, uid, f"{first_name} {last_name}", msg_id)
            pending_verification.pop(key, None)
            return
        pending_verification.pop(key, None)

    if ENABLE_STICKER_CHECK and getattr(msg, "sticker", None):
        print(f"[PTB] 群消息已记录: chat_id={chat_id} msg_id={msg.message_id} 触发验证(sticker)")
        await _start_verification(context.bot, msg, chat_id, uid, first_name, last_name,
                                  "⚠️ 检测到您发送了贴纸，请先完成人机验证。", "sticker")
        return

    if _is_ad_message(msg):
        print(f"[PTB] 群消息已记录: chat_id={chat_id} msg_id={msg.message_id} 触发验证(ad)")
        await _start_verification(context.bot, msg, chat_id, uid, first_name, last_name,
                                  "⚠️ 检测到疑似广告链接，请先完成人机验证。", "ad")
        return
    if ENABLE_EMOJI_CHECK and (_contains_emoji(text) or _contains_emoji(f"{first_name} {last_name}".strip())):
        print(f"[PTB] 群消息已记录: chat_id={chat_id} msg_id={msg.message_id} 触发验证(emoji)")
        await _start_verification(context.bot, msg, chat_id, uid, first_name, last_name,
                                  "⚠️ 检测到您的消息或昵称中含有表情符号，请先完成人机验证。", "emoji")
        return
    if _is_reply_to_other_chat(msg, int(chat_id)):
        print(f"[PTB] 群消息已记录: chat_id={chat_id} msg_id={msg.message_id} 触发验证(reply_other_chat)")
        await _start_verification(context.bot, msg, chat_id, uid, first_name, last_name,
                                  "⚠️ 检测到引用非本群消息，请先完成人机验证。", "reply_other_chat")
        return

    hit_text = check_spam_text(text)
    hit_name = check_spam_name(first_name, last_name)
    if hit_text:
        print(f"[PTB] 群消息已记录: chat_id={chat_id} msg_id={msg.message_id} 触发验证(spam_text hit={hit_text})")
        await _start_verification(context.bot, msg, chat_id, uid, first_name, last_name,
                                  "⚠️ 检测到您的消息中含有疑似广告词，请先完成人机验证。", "spam_text", hit_keyword=hit_text)
        return
    if hit_name:
        print(f"[PTB] 群消息已记录: chat_id={chat_id} msg_id={msg.message_id} 触发验证(spam_name hit={hit_name})")
        await _start_verification(context.bot, msg, chat_id, uid, first_name, last_name,
                                  "⚠️ 检测到您昵称中含有疑似广告词，请先完成人机验证。", "spam_name", hit_keyword=hit_name)
        return
    if uid in verification_blacklist:
        print(f"[PTB] 群消息已记录: chat_id={chat_id} msg_id={msg.message_id} 触发验证(blacklist)")
        await _start_verification(context.bot, msg, chat_id, uid, first_name, last_name,
                                  "⚠️ 检测到您的账号疑似广告账号，请先完成人机验证。", "blacklist")
        return

    add_verified_user(uid, user.username, f"{first_name} {last_name}".strip())
    save_verified_users()
    add_verification_record(
        chat_id, msg.message_id, uid,
        f"{first_name} {last_name}".strip(), getattr(user, "username", None) or "",
        "normal", (msg.text or msg.caption or "")[:200],
        initial_status="whitelist_added",
        raw_message_body=_serialize_message_body(msg),
    )
    save_verification_records()
    print(f"[PTB] 群消息已记录: chat_id={chat_id} msg_id={msg.message_id} whitelist_added")


def _cleanup_required_group_warn_count():
    """清理 _required_group_warn_count 中已过期的 key（窗口外的不再计入）"""
    now = time.time()
    cutoff = now - TRIGGER_WINDOW_SECONDS
    expired = [k for k, ts_list in _required_group_warn_count.items() if not ts_list or max(ts_list) <= cutoff]
    for k in expired:
        _required_group_warn_count.pop(k, None)


async def _start_required_group_verification(bot, msg, chat_id: str, user_id: int, first_name: str, last_name: str):
    """未加入 B 群时：删除消息，发送带按钮的警告，冷却+窗口内 5 次后限制"""
    _cleanup_required_group_warn_count()
    key = (chat_id, user_id)
    ts_list = _required_group_warn_count.get(key, [])
    should_count, new_ts, cnt = _apply_trigger_cooldown_window(ts_list, time.time())
    if should_count:
        _required_group_warn_count[key] = new_ts
    try:
        await msg.delete()
    except Exception:
        pass
    full_name = f"{first_name} {last_name}".strip() or "用户"
    deleted_text = (msg.text or msg.caption or "").strip()
    if deleted_text:
        _schedule_sync_background(_log_deleted_content, user_id, full_name, deleted_text)
    add_verification_record(
        chat_id, msg.message_id, user_id,
        full_name, getattr(msg.from_user, "username", None) or "",
        "not_in_required_group", deleted_text[:200],
        raw_message_body=_serialize_message_body(msg),
    )
    _schedule_sync_background(save_verification_records)
    if not should_count:
        return  # 冷却期内：已删消息，不发重复警告
    if cnt >= VERIFY_FAIL_THRESHOLD:
        add_to_blacklist(user_id)
        save_verified_users()
        save_verification_blacklist()
        await _restrict_and_notify(bot, chat_id, user_id, full_name, msg.message_id, restrict_hours=REQUIRED_GROUP_RESTRICT_HOURS)
        return
    rows = []
    for title, link in await _get_required_group_buttons(bot, chat_id):
        if link:
            rows.append([InlineKeyboardButton(title, url=link)])
    cb_data = f"reqgrp_unr:{chat_id}:{user_id}"
    if len(cb_data) <= 64:
        rows.append([InlineKeyboardButton("自助解禁", callback_data=cb_data)])
    reply_markup = InlineKeyboardMarkup(rows) if rows else None
    vmsg = await bot.send_message(
        chat_id=int(chat_id),
        text=f"【{full_name}】\n\n请先关注如下频道或加入群组后才能发言 。\n\n• 警告({cnt}/{VERIFY_FAIL_THRESHOLD})\n\n本条消息{_get_required_group_msg_delete_after()}秒后自动删除",
        reply_markup=reply_markup,
    )
    asyncio.create_task(_delete_after(bot, int(chat_id), vmsg.message_id, _get_required_group_msg_delete_after(), user_msg_id=msg.message_id))


async def _start_verification(bot, msg, chat_id: str, user_id: int, first_name: str, last_name: str, intro: str, trigger_reason: str = "", hit_keyword: str = ""):
    code = str(random.randint(1000, 9999))
    msg_id = msg.message_id
    msg_preview = (msg.text or msg.caption or "")[:200]
    raw_body = _serialize_message_body(msg)
    full_name = f"{first_name} {last_name}".strip() or "用户"
    if msg_preview.strip():
        _schedule_sync_background(_log_deleted_content, user_id, full_name, msg_preview)
    try:
        await msg.delete()
    except Exception:
        pass
    add_verification_record(
        chat_id, msg_id, user_id,
        full_name, getattr(msg.from_user, "username", None) or "",
        trigger_reason, msg_preview, hit_keyword=hit_keyword, raw_message_body=raw_body,
    )
    vmsg = await bot.send_message(
        chat_id=int(chat_id),
        text=f"【{full_name}】\n\n{intro}\n\n👉 您的验证码是： <code>{code}</code>\n\n"
             f"直接发送上述验证码即可通过（{VERIFY_TIMEOUT}秒内有效）",
        parse_mode="HTML",
    )
    pending_verification[(chat_id, user_id)] = {"code": code, "time": time.time(), "msg_id": msg_id}
    _schedule_sync_background(save_verification_records)
    asyncio.create_task(_delete_after(bot, int(chat_id), vmsg.message_id, _get_verify_msg_delete_after()))


async def _delete_after(bot, chat_id: int, msg_id: int, sec: int, user_msg_id: Optional[int] = None):
    """sec 秒后删除 msg_id；若提供 user_msg_id，先尝试删除用户消息（90s 重试未删掉的触发消息）"""
    await asyncio.sleep(sec)
    if user_msg_id is not None:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=user_msg_id)
        except Exception:
            pass
    try:
        await bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except Exception:
        pass


async def _maybe_ai_trigger(bot, msg, chat_id: str, user_id: int, text: str, first_name: str, last_name: str):
    # 由 _is_frost_trigger 保证已触发，此处仅提取 query
    if text.strip().startswith("霜刃，"):
        query = text[3:].strip() or "你好"
    else:
        query = (text or "").strip() or "继续"
    # 若是通过回复霜刃唤醒，将霜刃被回复的那条消息一并发给 AI 作为上下文
    replied_frost_text = None
    reply_to = getattr(msg, "reply_to_message", None)
    if reply_to and getattr(reply_to, "from_user", None) and reply_to.from_user.id == bot.id:
        replied_frost_text = (reply_to.text or reply_to.caption or "").strip()
    if not KIMI_API_KEY:
        try:
            await bot.send_message(
                chat_id=int(chat_id),
                text="⚠️ 霜刃 AI 未配置：请在 bytecler/.env 中设置 OPENAI_API_KEY",
                reply_to_message_id=msg.message_id,
            )
        except Exception:
            pass
        return
    try:
        from openai import AsyncOpenAI
        print(f"[PTB] 霜刃: 调用 Kimi API model={KIMI_MODEL}")
        client = AsyncOpenAI(api_key=KIMI_API_KEY, base_url=KIMI_BASE_URL)
        messages = [
            {"role": "system", "content": "你是一个冷酷的女杀手，沉默寡言。你的老板是小熊。回答严格控制在15字以内，尽量一句话。复杂或不好回复的问题可以回复：小助理，你来回答"},
        ]
        if replied_frost_text:
            messages.append({"role": "assistant", "content": replied_frost_text})
        messages.append({"role": "user", "content": query})
        resp = await client.chat.completions.create(
            model=KIMI_MODEL,
            messages=messages,
            temperature=0.6,
            max_tokens=1024,
        )
        reply = (resp.choices[0].message.content or "").strip()
        print(f"[PTB] 霜刃: API 返回 len={len(reply)}")
        if not reply:
            return
        # 仅当回复几乎就是「小助理，你来回答」时才转交，避免误判（如回答中顺带提到小助理）
        handoff_phrase = "小助理，你来回答"
        is_handoff = reply.strip() == handoff_phrase or (
            handoff_phrase in reply and len(reply) < 30
        )
        if is_handoff:
            try:
                _xhbot = _BASE.parent
                if str(_xhbot) not in sys.path:
                    sys.path.insert(0, str(_xhbot))
                from handoff import put_handoff
                ok = put_handoff(int(chat_id), msg.message_id, query)
                print(f"[PTB] 霜刃: 已转交小助理")
            except ImportError as e:
                print(f"[PTB] 霜刃: handoff 导入失败，改为直接回复: {e}")
                await bot.send_message(
                    chat_id=int(chat_id),
                    text=reply,
                    reply_to_message_id=msg.message_id,
                )
            return
        await bot.send_message(
            chat_id=int(chat_id),
            text=reply,
            reply_to_message_id=msg.message_id,
        )
        print("[PTB] 霜刃: 已发送回复")
    except Exception as e:
        print(f"[PTB] 霜刃 AI 唤醒异常: {e}")
        import traceback
        traceback.print_exc()


async def _restrict_and_notify(bot, chat_id: str, user_id: int, full_name: str, msg_id: int = None, restrict_hours: float | None = None):
    """限制用户发言。restrict_hours 若指定则按小时，否则按 VERIFY_RESTRICT_DURATION 天（验证码失败用）。"""
    if restrict_hours is not None:
        until = datetime.now(timezone.utc) + timedelta(hours=restrict_hours) if restrict_hours > 0 else None
    else:
        until = datetime.now(timezone.utc) + timedelta(days=VERIFY_RESTRICT_DURATION) if VERIFY_RESTRICT_DURATION > 0 else None
    try:
        await bot.restrict_chat_member(
            chat_id=int(chat_id), user_id=user_id,
            until_date=until,
            permissions={"can_send_messages": False, "can_send_media_messages": False},
        )
    except Exception as e:
        print(f"[PTB] 限制用户失败: {e}")
        return
    if msg_id is not None:
        update_verification_record(chat_id, msg_id, "failed_restricted", fail_count=VERIFY_FAIL_THRESHOLD)
        save_verification_records()
    add_to_blacklist(user_id)
    save_verified_users()
    save_verification_blacklist()
    verification_failures.pop((chat_id, user_id), None)
    for k in list(pending_verification):
        if k[1] == user_id:
            pending_verification.pop(k, None)
    try:
        m = await bot.send_message(
            chat_id=int(chat_id),
            text=f"【{full_name}】\n\n验证失败，如有需要，请联系 {UNBAN_BOT_USERNAME} 进行解封",
        )
        asyncio.create_task(_delete_after(bot, int(chat_id), m.message_id, _get_verify_msg_delete_after()))
    except Exception:
        pass


PENDING_SETLIMIT_TIMEOUT = 120
pending_setlimit: dict[tuple[str, int], dict] = {}  # (chat_id, uid) -> {timestamp}


async def _is_group_admin_can_promote(bot, chat_id: int, user_id: int) -> bool:
    """判断用户是否为群管理员且拥有「可添加管理员」权限（仅此类可配置 B 群）"""
    try:
        admins = await bot.get_chat_administrators(chat_id)
        for a in admins:
            if a.user.id != user_id:
                continue
            if getattr(a, "status", "") == "creator":
                return True
            return getattr(a, "can_promote_members", False) is True
        return False
    except Exception:
        return False


def _format_setlimit_prompt(chat_id: str) -> str:
    """生成 /setlimit 的提示文案"""
    ids = get_bgroup_ids_for_chat(chat_id)
    if ids:
        cur = f"当前 B 群/频道：{ids[0]}"
        clear_hint = "\n/clearlimit — 删除此配置（不校验 B 群）"
    else:
        cur = "当前未配置 B 群（不校验）"
        clear_hint = ""
    return (
        f"{cur}{clear_hint}\n\n"
        "请发送 B 群/频道（支持 @形式、https://t.me/xxx、-1001234567890）\n"
        "120 秒内有效，/cancel 取消"
    )


async def cmd_setlimit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """群内配置 B 群：显示当前并等待输入"""
    if not update.message or not update.effective_chat or not update.effective_user:
        return
    if update.effective_chat.type not in ("group", "supergroup"):
        await update.message.reply_text("请在群内使用此命令")
        return
    chat_id = str(update.effective_chat.id)
    uid = update.effective_user.id
    if not chat_allowed(chat_id, TARGET_GROUP_IDS):
        await update.message.reply_text("本群不在监控列表")
        return
    if not await _is_group_admin_can_promote(context.bot, int(chat_id), uid):
        await update.message.reply_text("仅拥有「可添加管理员」权限的管理员可配置")
        return
    pending_setlimit[(chat_id, uid)] = {"timestamp": time.time()}
    await update.message.reply_text(_format_setlimit_prompt(chat_id))


async def cmd_clearlimit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """删除本群 B 群配置"""
    if not update.message or not update.effective_chat or not update.effective_user:
        return
    if update.effective_chat.type not in ("group", "supergroup"):
        return
    chat_id = str(update.effective_chat.id)
    uid = update.effective_user.id
    if not chat_allowed(chat_id, TARGET_GROUP_IDS):
        return
    if not await _is_group_admin_can_promote(context.bot, int(chat_id), uid):
        await update.message.reply_text("仅拥有「可添加管理员」权限的管理员可配置")
        return
    pending_setlimit.pop((chat_id, uid), None)
    if set_bgroup_for_chat(chat_id, None):
        await update.message.reply_text("已删除 B 群配置，本群不再校验")
    else:
        await update.message.reply_text("本群未配置 B 群")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    await update.message.reply_text("Bytecler 机器人\n发送 /help 查看完整指令")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    await update.message.reply_text(
        "Bytecler 指令（仅私聊有效）\n\n"
        "• /add_text、/add_name — 多轮添加（直接输入=子串，/前缀=精确，已存在则删除，/cancel 结束）\n"
        "• /add_group — 添加霜刃可用群（支持 @群、https://t.me/xxx、-100xxxxxxxxxx）\n"
        "• /cancel — 取消当前操作\n"
        "• /help — 本帮助\n"
        "• /reload — 重载配置\n"
        "• /start — 启动\n"
        "• /settime — 配置关联群验证/人机验证消息自动删除时间\n"
        "• 群内 /setlimit — 配置本群 B 群/频道（需加入才能发言），仅拥有「可添加管理员」权限的管理员可配置；/clearlimit 删除\n"
        "• /kw_text、/kw_name add/remove — 关键词增删\n"
        "• /wl_name、/wl_text — 关键词白名单（管理员限制用户时不录入这些昵称/消息）\n"
        "• 发送群消息链接 — 查看该消息的验证过程\n\n"
        "💡 群内「霜刃你好」无反应？请在 @BotFather 关闭 Group Privacy，或使用 @机器人 唤醒"
    )

async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not update.effective_user:
        return
    if not is_admin(update.effective_user.id, ADMIN_IDS):
        await update.message.reply_text("无权限")
        return
    load_spam_keywords()
    load_verified_users()
    load_verification_blacklist()
    _load_bgroup_config()
    _load_target_groups()
    await update.message.reply_text("已重载 spam_keywords、白名单、黑名单、B群配置、监控群列表")

pending_keyword_cmd = {}
pending_settime_cmd = {}  # uid -> {"type": "required_group"|"verify"}
pending_add_group: dict[int, dict] = {}  # uid -> {timestamp}，/add_group 两段式
PENDING_ADD_GROUP_TIMEOUT = 120


async def cmd_add_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """私聊添加霜刃可用群：@群、https://t.me/xxx、-100xxxxxxxxxx"""
    if update.effective_chat.type != "private" or not update.effective_user:
        return
    if not is_admin(update.effective_user.id, ADMIN_IDS):
        await update.message.reply_text("无权限")
        return
    uid = update.effective_user.id
    pending_add_group[uid] = {"timestamp": time.time()}
    cur = list(TARGET_GROUP_IDS) if TARGET_GROUP_IDS else []
    if cur:
        lines = ["当前监控群："]
        for gid in sorted(cur):
            title, link = await _get_group_display_info(context.bot, gid)
            lines.append(f"• <a href=\"{link}\">{_escape_html(title)}</a> ({gid})")
        hint = "\n".join(lines)
    else:
        hint = "当前无监控群（请先在 .env 配置 GROUP_ID 或通过本命令添加）"
    await update.message.reply_text(
        f"{hint}\n\n"
        "请发送要添加的群（支持 @群、https://t.me/xxx、-100xxxxxxxxxx）\n"
        "120 秒内有效，/cancel 取消",
        parse_mode="HTML",
    )


async def cmd_settime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """配置关联群验证/人机验证消息自动删除时间"""
    if update.effective_chat.type != "private" or not update.effective_user:
        return
    if not is_admin(update.effective_user.id, ADMIN_IDS):
        await update.message.reply_text("无权限")
        return
    req_sec = _get_required_group_msg_delete_after()
    verify_sec = _get_verify_msg_delete_after()
    rows = [
        [InlineKeyboardButton("1、关联群验证", callback_data="settime:required_group")],
        [InlineKeyboardButton("2、人机验证", callback_data="settime:verify")],
    ]
    text = (
        "选择要配置的自动删除时间：\n\n"
        f"• 1、关联群验证 — 「请先关注频道或加入群组」警告消息，当前 {req_sec} 秒后删除\n"
        f"• 2、人机验证 — 5 次命中后展示的提示文案，当前 {verify_sec} 秒后删除"
    )
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    uid = update.effective_user.id
    chat_id = str(update.effective_chat.id) if update.effective_chat else ""
    key = (chat_id, uid)
    if key in pending_setlimit:
        pending_setlimit.pop(key, None)
        await update.message.reply_text("已取消")
        return
    if update.effective_chat.type != "private":
        return
    if uid in pending_add_group:
        pending_add_group.pop(uid, None)
        await update.message.reply_text("已取消")
    elif uid in pending_keyword_cmd:
        pending_keyword_cmd.pop(uid, None)
        await update.message.reply_text("已取消")
    elif uid in pending_settime_cmd:
        pending_settime_cmd.pop(uid, None)
        await update.message.reply_text("已取消")
    else:
        await update.message.reply_text("当前无待取消的操作")

async def cmd_kw_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _cmd_kw(update, context, "text", "消息")
async def cmd_kw_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _cmd_kw(update, context, "name", "昵称")
async def cmd_kw_bio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("bio 简介关键词暂未启用")


async def cmd_wl_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _cmd_wl(update, context, "name", "昵称")
async def cmd_wl_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _cmd_wl(update, context, "text", "消息")


async def _cmd_wl(update: Update, context: ContextTypes.DEFAULT_TYPE, field: str, label: str):
    """白名单管理：add/remove/list。子串=直接输入，精确=/前缀，正则=/正则/"""
    if update.effective_chat.type != "private" or not update.effective_user:
        return
    if not is_admin(update.effective_user.id, ADMIN_IDS):
        await update.message.reply_text("无权限")
        return
    args = (context.args or [])
    if len(args) >= 1 and args[0].lower() == "list":
        wl = (spam_keywords.get("whitelist") or {}).get(field) or {}
        ex = wl.get("exact") or []
        mt = [x[1] if x[0] == "str" else f"/{x[1].pattern}/" for x in (wl.get("match") or [])]
        txt = f"【{label}白名单】\nexact: {ex or '无'}\nmatch: {mt or '无'}"
        await update.message.reply_text(txt)
        return
    if len(args) >= 2:
        op, kw = args[0].lower(), " ".join(args[1:]).strip()
        as_exact, kw_norm, is_regex = _parse_keyword_input(kw)
        kw_for_storage = kw if is_regex else kw_norm  # 正则需保留 /.../ 格式
        if op == "add" and kw:
            if _add_whitelist_keyword(field, kw_for_storage, is_regex=is_regex, as_exact=as_exact):
                save_spam_keywords()
                await update.message.reply_text(f"已添加「{kw}」到{label}白名单")
            else:
                await update.message.reply_text("添加失败（可能已存在或正则无效）")
        elif op == "remove" and kw:
            if _remove_whitelist_keyword(field, kw_for_storage, is_regex=is_regex, as_exact=as_exact):
                save_spam_keywords()
                await update.message.reply_text(f"已从{label}白名单移除「{kw}」")
            else:
                await update.message.reply_text("移除失败（可能不存在）")
        else:
            await update.message.reply_text(f"用法: /wl_{field} add 关键词  或  /wl_{field} remove 关键词  或  /wl_{field} list")
    else:
        await update.message.reply_text(
            f"【{label}白名单】管理员限制用户时不录入这些\n"
            f"add 关键词 — 添加（直接输入=子串，/前缀=精确，/正则/=正则）\n"
            f"remove 关键词 — 移除\n"
            f"list — 查看\n"
            f"例: /wl_{field} add 测试用户"
        )


async def cmd_add_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """两段式多轮：直接输入=子串match，/前缀=精确exact，已存在则删除，/cancel 结束"""
    if update.effective_chat.type != "private" or not update.effective_user:
        return
    if not is_admin(update.effective_user.id, ADMIN_IDS):
        await update.message.reply_text("无权限")
        return
    pending_keyword_cmd[update.effective_user.id] = {"field": "text", "op": "add", "label": "消息", "multi": True, "timestamp": time.time()}
    await update.message.reply_text(
        "【消息】关键词管理（多轮，/cancel 结束）\n"
        "• 直接输入如 加V → 子串匹配\n"
        "• /加微信 或 / 加微信 → 精确匹配"
    )

async def cmd_add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not update.effective_user:
        return
    if not is_admin(update.effective_user.id, ADMIN_IDS):
        await update.message.reply_text("无权限")
        return
    pending_keyword_cmd[update.effective_user.id] = {"field": "name", "op": "add", "label": "昵称", "multi": True, "timestamp": time.time()}
    await update.message.reply_text(
        "【昵称】关键词管理（多轮，/cancel 结束）\n"
        "• 直接输入 → 子串匹配\n"
        "• /关键词 → 精确匹配"
    )

async def cmd_add_bio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("bio 简介关键词暂未启用")

async def _cmd_kw(update: Update, context: ContextTypes.DEFAULT_TYPE, field: str, label: str):
    if update.effective_chat.type != "private" or not update.effective_user:
        return
    if not is_admin(update.effective_user.id, ADMIN_IDS):
        await update.message.reply_text("无权限")
        return
    args = (context.args or [])
    if len(args) >= 2:
        op, kw = args[0].lower(), " ".join(args[1:]).strip()
        if op == "add" and kw:
            is_regex = kw.startswith("/") and kw.endswith("/") and len(kw) > 2
            if add_spam_keyword(field, kw, is_regex=is_regex):
                save_spam_keywords()
                await update.message.reply_text(f"已添加「{kw}」到{label}关键词")
            else:
                await update.message.reply_text("添加失败（可能已存在或正则无效）")
        elif op == "remove" and kw:
            is_regex = kw.startswith("/") and kw.endswith("/") and len(kw) > 2
            if remove_spam_keyword(field, kw, is_regex=is_regex):
                save_spam_keywords()
                await update.message.reply_text(f"已从{label}关键词移除「{kw}」")
            else:
                await update.message.reply_text("移除失败（可能不存在）")
        else:
            await update.message.reply_text(f"用法: /kw_{field} add 关键词  或  /kw_{field} remove 关键词")
    else:
        await update.message.reply_text(
            f"【{label}】关键词管理\nadd 关键词 — 添加\nremove 关键词 — 移除\n例: /kw_{field} add 加微信"
        )


async def callback_required_group_unrestrict(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """自助解禁：点击者若在 B 群，则解除点击者本人的限制"""
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("reqgrp_unr:"):
        return
    clicker_id = query.from_user.id if query.from_user else 0
    if not clicker_id:
        await query.answer("无法识别点击者", show_alert=True)
        return
    try:
        parts = query.data.split(":", 2)
        if len(parts) != 3:
            await query.answer("数据格式错误", show_alert=True)
            return
        _, chat_id_str, _ = parts
    except (ValueError, IndexError):
        await query.answer("解析失败", show_alert=True)
        return
    # 每次点击都实时检查，不读缓存（用户可能刚加入 B 群）
    if not await _is_user_in_required_group(context.bot, clicker_id, chat_id_str, skip_cache=True):
        await query.answer("请先加入指定群组后再点击", show_alert=True)
        return
    try:
        # 解除点击者本人的限制
        try:
            perms = ChatPermissions.all_permissions()
        except (AttributeError, TypeError):
            perms = {
                "can_send_messages": True,
                "can_send_media_messages": True,
                "can_send_other_messages": True,
                "can_send_polls": True,
                "can_add_web_page_previews": True,
            }
        await context.bot.restrict_chat_member(
            chat_id=int(chat_id_str),
            user_id=clicker_id,
            permissions=perms,
        )
    except Exception as e:
        err_msg = str(e).lower() if e else ""
        print(f"[PTB] 自助解禁失败 chat={chat_id_str} uid={clicker_id}: {e}")
        if "rights" in err_msg or "permission" in err_msg or "admin" in err_msg:
            tip = "解禁失败，请确认机器人在该群有禁言权限"
        else:
            tip = f"解禁失败：{str(e)[:80]}"
        await query.answer(tip, show_alert=True)
        return
    add_verified_user(clicker_id, None, None)
    verification_blacklist.discard(clicker_id)
    save_verified_users()
    save_verification_blacklist()
    key = (chat_id_str, clicker_id)
    _required_group_warn_count.pop(key, None)
    for b_id in get_bgroup_ids_for_chat(chat_id_str):
        _user_in_required_group_cache.pop((clicker_id, b_id), None)
    await query.answer("已解禁", show_alert=True)


async def callback_raw_message_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理「查看原始消息」按钮点击，返回完整消息体"""
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("raw_msg:"):
        return
    if query.from_user and not is_admin(query.from_user.id, ADMIN_IDS):
        await query.answer("无权限", show_alert=True)
        return
    try:
        parts = query.data.split(":", 2)
        if len(parts) != 3:
            await query.answer("数据格式错误", show_alert=True)
            return
        _, chat_id_str, msg_id_str = parts
        msg_id = int(msg_id_str)
    except (ValueError, IndexError):
        await query.answer("解析失败", show_alert=True)
        return
    rec = get_verification_record(chat_id_str, msg_id)
    if not rec or "raw_body" not in rec:
        await query.answer("无原始消息体", show_alert=True)
        return
    raw = rec["raw_body"]
    body_str = json.dumps(raw, ensure_ascii=False, indent=2)
    if len(body_str) > 4000:
        body_str = body_str[:4000] + "\n...(已截断)"
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(f"<pre>{body_str}</pre>", parse_mode="HTML")


async def callback_settime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /settime 的选项回调"""
    query = update.callback_query
    if not query:
        return
    data = query.data
    user = query.from_user
    if not user or not is_admin(user.id, ADMIN_IDS):
        await query.answer("无权限", show_alert=True)
        return
    typ = data.split(":", 1)[1]
    if typ == "required_group":
        label = "关联群验证"
        current = _get_required_group_msg_delete_after()
        key = "required_group_msg_delete_after"
    elif typ == "verify":
        label = "人机验证（5 次命中后提示文案）"
        current = _get_verify_msg_delete_after()
        key = "verify_msg_delete_after"
    else:
        await query.answer("未知选项", show_alert=True)
        return
    pending_settime_cmd[user.id] = {"type": typ, "key": key, "label": label, "timestamp": time.time()}
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(f"【{label}】当前 {current} 秒后自动删除。请发送数字设置新值（如 90）：")


async def private_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_chat.type != "private":
        return
    user = update.effective_user
    if not user:
        return
    if not is_admin(user.id, ADMIN_IDS):
        await update.message.reply_text("⚠️ 仅管理员可查询验证记录")
        return
    uid = user.id
    text = (update.message.text or "").strip()

    # 0. /add_group 的后续输入：等待群标识（120 秒超时）
    if uid in pending_add_group:
        info = pending_add_group[uid]
        if (time.time() - info.get("timestamp", 0)) > PENDING_ADD_GROUP_TIMEOUT:
            pending_add_group.pop(uid, None)
            await update.message.reply_text("已超时，请重新发送 /add_group")
            return
        pending_add_group.pop(uid, None)
        if not text:
            await update.message.reply_text("请输入群或频道（@群、https://t.me/xxx 或 -100xxxxxxxxxx），或 /cancel 取消")
            pending_add_group[uid] = {"timestamp": time.time()}
            return
        gid = await _resolve_group_input(context.bot, text)
        if gid:
            title, link = await _get_group_display_info(context.bot, gid)
            if _add_target_group(gid):
                await update.message.reply_text(
                    f"已添加 <a href=\"{link}\">{_escape_html(title)}</a>",
                    parse_mode="HTML",
                )
            else:
                await update.message.reply_text(
                    f"<a href=\"{link}\">{_escape_html(title)}</a> 已在监控列表中",
                    parse_mode="HTML",
                )
        else:
            await update.message.reply_text("解析失败，请发送 @群、https://t.me/xxx 或 -100xxxxxxxxxx")
            pending_add_group[uid] = {"timestamp": time.time()}
        return

    # 1. /settime 的后续输入：等待数字（120 秒超时）
    PENDING_SETTIME_TIMEOUT = 120
    if uid in pending_settime_cmd:
        info = pending_settime_cmd[uid]
        if (time.time() - info.get("timestamp", 0)) > PENDING_SETTIME_TIMEOUT:
            pending_settime_cmd.pop(uid, None)
            await update.message.reply_text("已超时，请重新发送 /settime 选择要设置的项")
            return
        info = pending_settime_cmd.pop(uid, None)
        if not info:
            return
        try:
            val = int(text)
            if val < 5 or val > 86400:
                await update.message.reply_text("请输入 5～86400 之间的数字（秒）")
                pending_settime_cmd[uid] = info  # 恢复，让用户重试
                return
        except ValueError:
            await update.message.reply_text("请输入有效数字（如 90）")
            pending_settime_cmd[uid] = info
            return
        key = info["key"]
        label = info["label"]
        _settime_config[key] = val
        _save_settime_config()
        await update.message.reply_text(f"已设置【{label}】自动删除时间为 {val} 秒")
        return

    # 1. 两段式关键词：add_text / add_name / add_bio 的后续输入（多轮、toggle）
    PENDING_KEYWORD_TIMEOUT = 120  # 等待关键词状态 120 秒后超时
    if uid in pending_keyword_cmd:
        info = pending_keyword_cmd[uid]
        if (time.time() - info.get("timestamp", 0)) > PENDING_KEYWORD_TIMEOUT:
            pending_keyword_cmd.pop(uid, None)
            await update.message.reply_text("已超时，请重新发送 /add_text 或 /add_name")
            return
        field, label, multi = info.get("field"), info.get("label", ""), info.get("multi", False)
        if not field or not text:
            if not multi:
                pending_keyword_cmd.pop(uid, None)
            await update.message.reply_text("已取消" if not text else "请发送关键词")
            return
        as_exact, kw, is_regex = _parse_keyword_input(text)
        exists = _keyword_exists_in_field(field, kw, as_exact, is_regex)
        if exists:
            if remove_spam_keyword(field, kw, is_regex=is_regex, as_exact=as_exact):
                save_spam_keywords()
                await update.message.reply_text(f"已移除「{kw}」")
            else:
                await update.message.reply_text("移除失败")
        else:
            if add_spam_keyword(field, kw, is_regex=is_regex, as_exact=as_exact):
                save_spam_keywords()
                reply = f"已添加「{kw}」（{'精确' if as_exact else '子串'}）"
                if multi:
                    reply += "。继续发送关键词，/cancel 结束"
                await update.message.reply_text(reply)
            else:
                await update.message.reply_text("添加失败（正则无效？）")
        if not multi:
            pending_keyword_cmd.pop(uid, None)
        else:
            info["timestamp"] = time.time()  # 刷新超时时间
        return

    # 2. 群消息链接查询（支持 t.me/c/123/456 和 t.me/USERNAME/123），仅在有链接特征时解析
    if not (text and ("t.me" in text or "telegram" in text.lower())):
        return
    print(f"[PTB] 验证记录查询: 收到链接 text={text[:80]!r}")
    parsed = await _parse_message_link_async(text, context.bot)
    if not parsed:
        if text and ("t.me" in text or "telegram" in text.lower()):
            print(f"[PTB] 验证记录查询: 链接解析失败，无法识别格式")
            await update.message.reply_text(
                "\n".join([
                    "❌ 链接解析失败",
                    "",
                    "📌 收到的内容",
                    f"• {text[:100]}{'...' if len(text) > 100 else ''}",
                    "",
                    "📌 支持的格式",
                    "• 私密群: https://t.me/c/1330784088/123",
                    "• 公开群: https://t.me/XHNPD/123",
                    "",
                    "📌 请检查",
                    "• 是否包含完整 chat 数字或用户名",
                    "• 是否包含 msg_id",
                ])
            )
        return
    chat_id_str, msg_id = parsed
    print(f"[PTB] 验证记录查询: 解析成功 chat_id={chat_id_str} msg_id={msg_id}")
    in_target = chat_allowed(chat_id_str, TARGET_GROUP_IDS)
    if not in_target:
        print(f"[PTB] 验证记录查询: 群 {chat_id_str} 不在监控列表 {TARGET_GROUP_IDS}，该群消息不会被处理")
    rec = get_verification_record(chat_id_str, msg_id)
    if not rec:
        total_records = len(_verification_records)
        reasons = []
        if not in_target:
            reasons.append("群不在监控列表")
        reasons.append("霜刃唤醒消息(无记录)")
        reasons.append("非文本/非caption消息(如纯贴纸)")
        reasons.append("验证码消息(不单独建记录)")
        print(f"[PTB] 验证记录查询: 未找到 chat_id={chat_id_str} msg_id={msg_id} | 当前记录总数={total_records} | 可能原因: {', '.join(reasons)}")
        flow_lines = ["📌 流程判断"]
        if not in_target:
            flow_lines.append("└─ 群不在监控？ → 是 → 忽略（停在此）")
        else:
            flow_lines.extend([
                "├─ 群不在监控？ → 否 ✓",
                "├─ 未加入 B 群？ → 否 ✓（有记录则已通过）",
                "├─ 霜刃唤醒？ → 可能（霜刃不建记录）",
                "├─ 白名单？ → 可能",
                "├─ 验证码回复？ → 可能（不单独建记录）",
                "└─ 其他 → 非文本/纯贴纸等",
            ])
        not_found_lines = [
            "❌ 未找到该消息的验证记录",
            "",
            *flow_lines,
            "",
            "📌 查询信息",
            f"• chat_id: {chat_id_str}",
            f"• msg_id: {msg_id}",
            f"• 当前记录总数: {total_records}",
            "",
            "📌 可能原因（该消息未建记录）",
        ]
        if not in_target:
            not_found_lines.append("• 群不在监控 — 该群未在 GROUP_ID 中，消息不会被处理")
        not_found_lines.extend([
            "• 霜刃唤醒 — 以「霜刃，」开头、@霜刃、或回复霜刃的消息不建记录",
            "• 非文本消息 — 纯贴纸、纯图(无说明)、语音、视频等不建记录",
            "• 验证码消息 — 用户发的验证码(对/错)不单独建记录",
        ])
        if in_target:
            not_found_lines.append("")
            not_found_lines.append("💡 群在监控且链接正确，该消息很可能是：霜刃唤醒 / 纯贴纸或纯图 / 验证码 之一")
        await update.message.reply_text("\n".join(not_found_lines))
        return
    print(f"[PTB] 验证记录查询: 已找到 trigger={rec.get('trigger_reason')} status={rec.get('status')}")
    trigger = rec.get("trigger_reason", "")
    rec_status = rec.get("status", "")
    # 根据 trigger 和 status 重建流程判断
    flow_parts = []
    flow_parts.append(("群不在监控？", False, "否 ✓"))
    if trigger == "not_in_required_group":
        flow_parts.append(("未加入 B 群？", True, "是 → 触发验证（停在此）"))
    else:
        flow_parts.append(("未加入 B 群？", False, "否 ✓"))
        flow_parts.append(("霜刃唤醒？", False, "否 ✓"))
        if trigger == "normal" and rec_status == "verified_pass":
            flow_parts.append(("白名单？", True, "是 → 记录 verified_pass（停在此）"))
        else:
            flow_parts.append(("白名单？", False, "否 ✓"))
            if trigger in ("ad", "emoji", "reply_other_chat", "spam_text", "spam_name", "blacklist"):
                if rec_status == "passed":
                    flow_parts.append(("广告/引用/关键词/黑名单？", True, "是 → 触发验证码，用户已通过（停在此）"))
                elif rec_status == "failed_restricted":
                    flow_parts.append(("广告/引用/关键词/黑名单？", True, "是 → 触发验证码验证，已限制（停在此）"))
                else:
                    flow_parts.append(("广告/引用/关键词/黑名单？", True, "是 → 触发验证码验证（停在此）"))
            elif rec_status == "whitelist_added":
                flow_parts.append(("广告/引用/关键词/黑名单？", False, "否 ✓"))
                flow_parts.append(("直接加白？", True, "是 → whitelist_added（停在此）"))
            else:
                flow_parts.append(("广告/引用/关键词/黑名单？", False, "否 ✓"))
                flow_parts.append(("直接加白？", True, f"是 → {rec_status}（停在此）"))
    flow_lines = ["📌 流程判断"]
    for i, (label, is_stop, result) in enumerate(flow_parts):
        prefix = "└─" if i == len(flow_parts) - 1 else "├─"
        flow_lines.append(f"{prefix} {label} → {result}")
    trigger_map = {
        "spam_text": "消息垃圾关键词", "spam_name": "昵称垃圾关键词",
        "ad": "广告链接", "emoji": "消息/昵称含表情", "sticker": "贴纸", "reply_other_chat": "引用非本群消息", "blacklist": "黑名单账号",
        "not_in_required_group": "未加入指定群组",
        "normal": "正常消息", "ai_trigger": "霜刃 AI 唤醒",
    }
    reason = trigger_map.get(trigger, trigger)
    status_map = {"pending": "待验证", "passed": "✓ 已通过", "failed_restricted": "✗ 验证失败已限制", "whitelist_added": "✓ 直接加白", "verified_pass": "✓ 白名单正常消息", "ai_replied": "已回复"}
    status = status_map.get(rec_status, rec_status)
    uid = rec.get("user_id")
    try:
        uid_int = int(uid) if uid is not None else None
    except (TypeError, ValueError):
        uid_int = None
    in_whitelist = uid_int in verified_users if uid_int is not None else False
    in_blacklist = uid_int in verification_blacklist if uid_int is not None else False
    uname = rec.get("username", "") or ""
    wb = "白名单 ✓" if in_whitelist else ("黑名单 ✓" if in_blacklist else "均否")
    lines = [
        *flow_lines,
        "",
        "📌 用户信息",
        f"• 昵称: {rec.get('full_name', '')}" + (f" (@{uname})" if uname else " (无用户名)"),
        f"• user_id: {uid}",
        f"• 白名单/黑名单: {wb}",
        f"• 触发原因: {reason}",
        f"• 状态: {status}",
    ]
    reply_markup = None
    if rec.get("raw_body") is not None:
        cb_data = f"raw_msg:{chat_id_str}:{msg_id}"
        if len(cb_data) <= 64:
            reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("查看原始消息", callback_data=cb_data)]])
    await update.message.reply_text("\n".join(lines), reply_markup=reply_markup)


async def _job_frost_reply(context: ContextTypes.DEFAULT_TYPE):
    try:
        _xhbot = _BASE.parent
        if str(_xhbot) not in sys.path:
            sys.path.insert(0, str(_xhbot))
        from handoff import take_frost_reply_handoff
        req = take_frost_reply_handoff()
        if not req:
            return
        chat_id = req["chat_id"]
        reply_to_id = req["reply_to_message_id"]
        if chat_id and str(chat_id) not in TARGET_GROUP_IDS:
            return
        await context.bot.send_message(
            chat_id=chat_id, text="......",
            reply_to_message_id=reply_to_id,
        )
    except ImportError:
        pass
    except Exception as e:
        print(f"[PTB] frost_reply 处理失败: {e}")


def _is_sync_success(msg: str) -> bool:
    """判断抽奖同步是否成功"""
    return ("同步" in msg and "个中奖用户" in msg) or "无新中奖用户" in msg


def _get_sync_fail_msg(msg: str) -> str:
    """根据失败原因返回对应文案，便于查找具体原因"""
    if "lottery.db 不存在" in msg:
        return "目标已丢失，任务失败，立即撤退"
    if "未找到兼容的抽奖表结构" in msg:
        return "目标已变，任务更改"
    if "lottery.db 只读打开失败" in msg or "只读打开失败" in msg:
        return "目标无法接近，任务暂停"
    if "Windows 下已跳过" in msg or "仅适用于 Linux" in msg:
        return "任务窗口期已过，立即撤退"
    return "任务失败，立即撤退"


async def _send_sync_result_to_groups(bot, added: int, msg: str):
    """抽奖同步完成后向监控群发送结果文案"""
    if _is_sync_success(msg):
        sync_msg = f"任务执行完毕，已歼灭{added} 人" if added > 0 else "任务执行中"
    else:
        sync_msg = _get_sync_fail_msg(msg)
    for gid in TARGET_GROUP_IDS:
        if gid:
            try:
                await bot.send_message(chat_id=int(gid), text=sync_msg)
            except Exception as e:
                print(f"[PTB] 抽奖同步结果群发失败 chat_id={gid}: {e}")


async def _job_lottery_sync(context: ContextTypes.DEFAULT_TYPE):
    added, msg = sync_lottery_winners()
    print(f"[PTB] 抽奖同步: {msg}")
    await _send_sync_result_to_groups(context.bot, added, msg)


async def _post_init_send_hello(application: Application):
    # 设置 Bot 菜单命令
    try:
        await application.bot.set_my_commands([
            BotCommand("add_text", "添加消息关键词"),
            BotCommand("add_name", "添加昵称关键词"),
            BotCommand("add_group", "添加霜刃可用群"),
            BotCommand("cancel", "取消操作"),
            BotCommand("help", "帮助"),
            BotCommand("reload", "重载配置"),
            BotCommand("start", "启动"),
            BotCommand("settime", "配置自动删除时间"),
            BotCommand("setlimit", "群内配置B群"),
        ])
    except Exception as e:
        print(f"[PTB] 设置菜单命令失败: {e}")
    for gid in TARGET_GROUP_IDS:
        if gid:
            try:
                await application.bot.send_message(chat_id=int(gid), text="你好")
            except Exception as e:
                print(f"[PTB] 群发你好失败 chat_id={gid}: {e}")
    added, msg = sync_lottery_winners()
    print(f"[PTB] 抽奖同步: {msg}")
    await _send_sync_result_to_groups(application.bot, added, msg)
    try:
        me = await application.bot.get_me()
        print(f"[PTB] 霜刃 @{me.username} 已就绪，监控群: {list(TARGET_GROUP_IDS)}")
    except Exception as e:
        print(f"[PTB] 获取 bot 信息失败: {e}")


def _ptb_main():
    """同步的 PTB main（run_polling 阻塞）"""
    if not BOT_TOKEN:
        print("请配置 BOT_TOKEN，编辑 bytecler/.env")
        return
    if "你的" in BOT_TOKEN or ("Token" in BOT_TOKEN and ":" not in BOT_TOKEN) or len(BOT_TOKEN) < 40:
        print("❌ 检测到无效 token，请配置真实 Bot Token")
        return
    load_spam_keywords()
    load_verified_users()
    load_verification_failures()
    load_verification_blacklist()
    load_verification_records()

    app = Application.builder().token(BOT_TOKEN).post_init(_post_init_send_hello).build()
    globals()["_ptb_app"] = app

    async def _error_handler(update, context):
        err = getattr(context, "error", None)
        print(f"[PTB] Handler 异常: {err}")
    app.add_error_handler(_error_handler)

    # 显式指定 CHAT_MEMBER（其他成员状态变更），非 MY_CHAT_MEMBER（Bot 自身状态）
    app.add_handler(ChatMemberHandler(chat_member_handler, ChatMemberHandler.CHAT_MEMBER))
    # 群内 /setlimit、/clearlimit 必须在 group_message_handler 之前注册，否则会被 TEXT 匹配抢先消费
    app.add_handler(CommandHandler("setlimit", cmd_setlimit))
    app.add_handler(CommandHandler("clearlimit", cmd_clearlimit))
    # 分两个 handler 避免 filters.TEXT | filters.CAPTION 在某些 PTB 版本的兼容性问题
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT, group_message_handler))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.CAPTION, group_message_handler))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.Sticker.ALL, group_message_handler))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("reload", cmd_reload))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("kw_text", cmd_kw_text))
    app.add_handler(CommandHandler("kw_name", cmd_kw_name))
    app.add_handler(CommandHandler("kw_bio", cmd_kw_bio))
    app.add_handler(CommandHandler("wl_name", cmd_wl_name))
    app.add_handler(CommandHandler("wl_text", cmd_wl_text))
    app.add_handler(CommandHandler("add_text", cmd_add_text))
    app.add_handler(CommandHandler("add_name", cmd_add_name))
    app.add_handler(CommandHandler("add_bio", cmd_add_bio))
    app.add_handler(CommandHandler("settime", cmd_settime))
    app.add_handler(CommandHandler("add_group", cmd_add_group))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, private_message_handler))
    app.add_handler(CallbackQueryHandler(callback_required_group_unrestrict, pattern="^reqgrp_unr:"))
    app.add_handler(CallbackQueryHandler(callback_settime, pattern="^settime:"))
    app.add_handler(CallbackQueryHandler(callback_raw_message_button, pattern="^raw_msg:"))

    jq = app.job_queue
    if jq:
        jq.run_repeating(_job_frost_reply, interval=2, first=2)
        jq.run_daily(_job_lottery_sync, time=dt_time(20, 0))  # 20:00 UTC = 北京时间凌晨 4 点
        print("[PTB] 定时任务已注册：抽奖同步 每日 20:00 UTC（北京时间 4:00）")
    else:
        print("[PTB] ⚠️ job_queue 为 None，定时任务未注册。请执行: pip install 'python-telegram-bot[job-queue]'")

    # 必须显式包含 chat_member，Telegram 默认不推送此类型
    app.run_polling(allowed_updates=Update.ALL_TYPES)


def stop_bytecler():
    """停止霜刃 PTB（供 main.py Ctrl+C 时优雅退出）"""
    app_ref = globals().get("_ptb_app")
    if app_ref:
        try:
            app_ref.stop()
        except Exception:
            pass


def main():
    """直接在主线程运行，PTB run_polling 的 add_signal_handler 必须在主线程"""
    _ptb_main()


if __name__ == "__main__":
    import signal
    _orig_signal = signal.signal
    def _our_signal(signum, handler):
        if signum == signal.SIGINT:
            return _orig_signal(signum, lambda s, f: os._exit(0))
        return _orig_signal(signum, handler)
    signal.signal = _our_signal
    signal.signal(signal.SIGINT, lambda s, f: None)
    try:
        main()
    except KeyboardInterrupt:
        os._exit(0)
