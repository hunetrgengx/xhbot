"""群组配置 - 按 chat_id 获取 custom_prompt 和模型配置"""
import sys
from pathlib import Path
from typing import Optional

from bot.models.database import get_group_settings
from config.settings import (
    AI_PROVIDER,
    CUSTOM_PROMPT_FILE,
    CUSTOM_SYSTEM_PROMPT,
    MODEL_NAME,
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
)

# 预置模型方案
PRESET_MODELS = {
    "kimi": {
        "ai_provider": "kimi",
        "model_name": "moonshot-v1-128k",
        "base_url": "https://api.moonshot.cn/v1",
        "api_key": None,  # 用全局
    },
    "kimi-k2": {
        "ai_provider": "kimi",
        "model_name": "kimi-k2-turbo-preview",
        "base_url": "https://api.moonshot.cn/v1",
        "api_key": None,
    },
    "ollama-qwen": {
        "ai_provider": "ollama",
        "model_name": "qwen2.5",  # 需先执行 ollama pull qwen2.5
        "base_url": "http://localhost:11434/v1",
        "api_key": "ollama",
    },
    "ollama-qwen3-vl": {
        "ai_provider": "ollama",
        "model_name": "qwen3-vl:8b",
        "base_url": "http://localhost:11434/v1",
        "api_key": "ollama",
    },
    "ollama-gemma3": {
        "ai_provider": "ollama",
        "model_name": "gemma3:4b",
        "base_url": "http://localhost:11434/v1",
        "api_key": "ollama",
    },
    "ollama-llama": {
        "ai_provider": "ollama",
        "model_name": "llama3.2",  # 需先执行 ollama pull llama3.2
        "base_url": "http://localhost:11434/v1",
        "api_key": "ollama",
    },
    "openai": {
        "ai_provider": "openai",
        "model_name": "gpt-4o-mini",
        "base_url": "https://api.openai.com/v1",
        "api_key": None,
    },
    "deepseek": {
        "ai_provider": "deepseek",
        "model_name": "deepseek-chat",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": None,
    },
}


def _load_global_custom_prompt() -> str:
    """加载全局 custom_prompt。Windows 用绝对路径，Ubuntu 用相对路径（以当前工作目录为基准）"""
    if CUSTOM_SYSTEM_PROMPT.strip():
        return CUSTOM_SYSTEM_PROMPT.strip().replace("\\n", "\n")
    prompt_path = Path(CUSTOM_PROMPT_FILE)
    if sys.platform == "win32" and not prompt_path.is_absolute():
        project_root = Path(__file__).resolve().parent.parent.parent
        prompt_path = project_root / CUSTOM_PROMPT_FILE
    if prompt_path.exists():
        return prompt_path.read_text(encoding="utf-8").strip()
    return ""


def get_group_custom_prompt(chat_id: int) -> str:
    """获取群组数据库中的 custom_prompt，无则返回空"""
    gs = get_group_settings(chat_id)
    if gs and gs.get("custom_prompt"):
        return gs["custom_prompt"]
    return ""


def get_global_custom_prompt() -> str:
    """获取全局 custom_prompt（.env 或文件）"""
    return _load_global_custom_prompt()


def get_custom_prompt(chat_id: int) -> str:
    """获取 custom_prompt：群组优先，否则全局"""
    gs = get_group_settings(chat_id)
    if gs and gs.get("custom_prompt"):
        return gs["custom_prompt"]
    return _load_global_custom_prompt()


def get_ai_config(chat_id: int) -> dict:
    """
    获取 AI 配置：群组优先，否则全局
    返回: ai_provider, model_name, base_url, api_key, use_web_search
    """
    gs = get_group_settings(chat_id)
    if gs and gs.get("ai_provider"):
        provider = gs["ai_provider"].lower()
        model = gs.get("model_name")
        base_url = gs.get("openai_base_url")
        api_key = gs.get("openai_api_key")
        preset = PRESET_MODELS.get(provider)
        if preset:
            model = model or preset["model_name"]
            base_url = base_url or preset["base_url"]
            if api_key is None and preset.get("api_key") == "ollama":
                api_key = "ollama"
        if base_url is None:
            base_url = OPENAI_BASE_URL
        if api_key is None:
            api_key = OPENAI_API_KEY if provider != "ollama" else "ollama"
        return {
            "ai_provider": provider,
            "model_name": model or MODEL_NAME,
            "base_url": base_url,
            "api_key": api_key,
            "use_web_search": provider == "kimi",
        }
    return {
        "ai_provider": AI_PROVIDER,
        "model_name": MODEL_NAME,
        "base_url": OPENAI_BASE_URL,
        "api_key": OPENAI_API_KEY,
        "use_web_search": AI_PROVIDER == "kimi",
    }


def get_preset_list() -> list[tuple[str, str]]:
    """返回 [(方案id, 显示名), ...]"""
    names = {
        "kimi": "Kimi 128K",
        "kimi-k2": "Kimi K2（联网）",
        "ollama-qwen": "Ollama Qwen2.5",
        "ollama-qwen3-vl": "Ollama Qwen3-VL 8B",
        "ollama-gemma3": "Ollama Gemma3 4B",
        "ollama-llama": "Ollama Llama",
        "openai": "OpenAI",
        "deepseek": "DeepSeek",
    }
    return [(k, names.get(k, k)) for k in PRESET_MODELS]
