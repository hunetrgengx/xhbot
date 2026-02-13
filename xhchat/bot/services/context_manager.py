"""对话上下文管理"""
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from bot.models.database import add_message, get_recent_messages
from config.settings import MAX_CONTEXT_MESSAGES, RATE_LIMIT_PER_MINUTE


class RateLimiter:
    """简单的按分钟限流"""

    def __init__(self, max_per_minute: int = 5):
        self.max_per_minute = max_per_minute
        self.requests: dict[tuple[int, int], list[datetime]] = defaultdict(list)

    def check(self, chat_id: int, user_id: int) -> bool:
        """检查是否超限，未超限则记录一次请求"""
        key = (chat_id, user_id)
        now = datetime.now()
        cutoff = now - timedelta(minutes=1)
        self.requests[key] = [t for t in self.requests[key] if t > cutoff]
        if len(self.requests[key]) >= self.max_per_minute:
            return False
        self.requests[key].append(now)
        return True


rate_limiter = RateLimiter(RATE_LIMIT_PER_MINUTE)


def build_messages_for_ai(
    chat_id: int, user_id: int, user_query: str, reply_to_assistant: Optional[str] = None
) -> list[dict]:
    """
    构建发送给 AI 的消息列表
    包含历史上下文 + 当前用户问题
    当用户回复机器人某条消息时，reply_to_assistant 为该条消息内容，会作为上一条 assistant 注入上下文
    """
    history = get_recent_messages(chat_id, user_id, MAX_CONTEXT_MESSAGES)
    messages = [{"role": m["role"], "content": m["content"]} for m in history]
    if reply_to_assistant and reply_to_assistant.strip():
        messages.append({"role": "assistant", "content": reply_to_assistant.strip()})
    messages.append({"role": "user", "content": user_query})
    return messages


def save_exchange(chat_id: int, user_id: int, user_content: str, assistant_content: str):
    """保存一轮对话"""
    add_message(chat_id, user_id, "user", user_content)
    add_message(chat_id, user_id, "assistant", assistant_content)
