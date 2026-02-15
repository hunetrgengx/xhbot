"""AI 服务 - 按 chat_id 读取配置，支持多模型"""
import json
import random
from datetime import datetime
from typing import Optional

from openai import OpenAI

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

from config.settings import ENABLE_WEB_SEARCH


def _get_client(base_url: str, api_key: str) -> OpenAI:
    return OpenAI(api_key=api_key or "sk-none", base_url=base_url)


def _get_current_time_prompt() -> str:
    if ZoneInfo:
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
    else:
        now = datetime.now()
    return now.strftime("当前日期时间：%Y年%m月%d日 %H:%M:%S（北京时间）")


SYSTEM_PROMPT_BASE = """你是一个友好的 AI 助手，在 Telegram 群聊中回答用户问题。
回复要简洁、有用，适合群聊场景，不超过1000字。适当使用 emoji，如果问题过于复杂，可以建议用户私聊进一步讨论。
当用户询问天气、实时新闻等需要最新信息的问题时，请使用联网搜索获取准确数据。采用UTC+8时区"""

WEB_SEARCH_TOOLS = [{"type": "builtin_function", "function": {"name": "$web_search"}}]


def _web_search_impl(arguments: dict) -> dict:
    return arguments


def _chat_with_tools(client: OpenAI, messages: list[dict], model: str):
    response = client.chat.completions.create(
        model=model, messages=messages, temperature=0.6, max_tokens=4096, tools=WEB_SEARCH_TOOLS
    )
    return response.choices[0]


def _select_prompts_for_message(custom_prompt: str, user_message: str) -> str:
    """
    根据用户消息选择 prompt：
    - 有匹配时：全部采用匹配的 prompt；若不足 1/3 则从非匹配中随机补齐到 1/3；若超过 1/3 则全部采用
    - 无匹配时：不采用
    """
    lines = [ln.strip() for ln in custom_prompt.split("\n") if ln.strip()]
    if not lines:
        return ""

    def has_match(line: str, msg: str) -> bool:
        """检查用户消息是否有 2+ 字符的子串出现在该行"""
        if len(msg) < 2:
            return False
        for i in range(len(msg) - 1):
            sub = msg[i : i + 2]  # 两字匹配
            if sub in line:
                return True
        return False

    matches = [ln for ln in lines if has_match(ln, user_message)]
    target = max(1, (len(lines) + 2) // 3)  # 1/3，至少 1 条

    if matches:
        selected = list(matches)
        if len(selected) < target:
            rest = [ln for ln in lines if ln not in matches]
            need = target - len(selected)
            if rest:
                selected.extend(random.sample(rest, min(need, len(rest))))
        return "\n".join(selected)
    return ""


def _build_full_system_prompt(custom_prompt: str, user_full_name: Optional[str] = None) -> str:
    parts = [SYSTEM_PROMPT_BASE, _get_current_time_prompt()]
    if user_full_name:
        parts.append(f"当前与你对话的用户名叫「{user_full_name}」，在合适的时候可以用名字称呼对方。")
    if custom_prompt:
        parts.append(f"\n\n【你的设定，请严格遵守】\n{custom_prompt}")
    return "\n\n".join(parts)


def chat_completion(
    messages: list[dict],
    chat_id: int = 0,
    user_full_name: Optional[str] = None,
    user_message: Optional[str] = None,
) -> str:
    """
    调用 AI 生成回复
    chat_id: 群组/私聊 ID，用于读取该会话的配置（模型、custom_prompt）
    user_message: 当前用户消息，用于 prompt 匹配；未传则从 messages 最后一条提取
    """
    from bot.services.group_config import get_ai_config, get_global_custom_prompt, get_group_custom_prompt

    cfg = get_ai_config(chat_id)
    # 提取当前用户消息（用于 prompt 匹配）
    if user_message is None and messages:
        for m in reversed(messages):
            if m.get("role") == "user" and m.get("content"):
                user_message = (m["content"] or "").strip()
                break
    user_msg = user_message or ""
    group_prompt = get_group_custom_prompt(chat_id)
    global_prompt = get_global_custom_prompt()
    # 群组有设定：按匹配选择；无匹配则取全局
    # 群组无设定：直接使用完整全局设定
    if group_prompt:
        selected = _select_prompts_for_message(group_prompt, user_msg)
        custom_prompt = selected if selected else global_prompt
    else:
        custom_prompt = global_prompt or ""

    base_url = cfg["base_url"]
    api_key = cfg["api_key"]
    model = cfg["model_name"]
    use_web_search = cfg["use_web_search"] and ENABLE_WEB_SEARCH

    # Ollama 本地可接受任意 api_key
    if cfg["ai_provider"] == "ollama":
        api_key = api_key or "ollama"

    client = _get_client(base_url, api_key)

    # Kimi 且联网搜索时用 kimi-k2
    if use_web_search:
        model = "kimi-k2-turbo-preview"

    full_messages = [{"role": "system", "content": _build_full_system_prompt(custom_prompt, user_full_name)}] + messages

    if use_web_search:
        finish_reason = None
        while finish_reason is None or finish_reason == "tool_calls":
            choice = _chat_with_tools(client, full_messages, model)
            finish_reason = choice.finish_reason
            if finish_reason == "tool_calls" and choice.message.tool_calls:
                msg = choice.message
                full_messages.append({
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}}
                        for tc in msg.tool_calls
                    ],
                })
                for tc in choice.message.tool_calls:
                    name = tc.function.name
                    args = json.loads(tc.function.arguments or "{}")
                    result = _web_search_impl(args) if name == "$web_search" else {}
                    full_messages.append({"role": "tool", "tool_call_id": tc.id, "name": name, "content": json.dumps(result)})
            else:
                return (choice.message.content or "").strip()
        return ""
    else:
        response = client.chat.completions.create(
            model=model, messages=full_messages, max_tokens=1024, temperature=0.7
        )
        return (response.choices[0].message.content or "").strip()
