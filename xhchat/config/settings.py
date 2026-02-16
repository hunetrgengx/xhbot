"""配置管理"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# 从项目根目录加载 .env（不依赖当前工作目录）
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=_env_path)

# 路径：Windows 用绝对路径，Ubuntu 用相对路径
_XHCHAT_ROOT = Path(__file__).resolve().parent.parent if sys.platform == "win32" else None

# Bot
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
BOT_USERNAME = os.getenv("BOT_USERNAME", "")
# 管理员 user_id，只有此用户可执行管理命令（/settings /set_model /set_prompt 等）
BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "7171378911"))

# 允许使用机器人的群组 ID，多个用逗号分隔（AI 对话、/xhadd /xhdel /xhset）
def _parse_chat_ids(s: str) -> frozenset:
    ids = set()
    for x in (s or "").split(","):
        x = x.strip()
        if not x:
            continue
        try:
            ids.add(int(x))
        except ValueError:
            pass
    return frozenset(ids)


ALLOWED_CHAT_IDS = _parse_chat_ids(os.getenv("ALLOWED_CHAT_ID", "-1001330784088"))

# AI 提供商: openai | kimi
AI_PROVIDER = os.getenv("AI_PROVIDER", "openai").lower()

# API 配置（Kimi 兼容 OpenAI 格式）
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# 未显式配置时，根据 AI_PROVIDER 使用默认值
_default_base_url = "https://api.moonshot.cn/v1" if AI_PROVIDER == "kimi" else "https://api.openai.com/v1"
_default_model = "moonshot-v1-128k" if AI_PROVIDER == "kimi" else "gpt-4o-mini"

OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", _default_base_url)
MODEL_NAME = os.getenv("MODEL_NAME", _default_model)

# Kimi 联网搜索（天气、实时信息等，每次搜索约 ￥0.03）。可在私聊用 /web_search 覆盖
ENABLE_WEB_SEARCH = os.getenv("ENABLE_WEB_SEARCH", "true").lower() in ("true", "1", "yes")

# 自定义设定（所有人对话都会遵循）
# 方式1：指定设定文件路径，文件内容会追加到 system prompt
CUSTOM_PROMPT_FILE = os.getenv("CUSTOM_PROMPT_FILE", "config/custom_prompt.txt")
# 方式2：直接在 .env 中写 CUSTOM_SYSTEM_PROMPT（用 \n 表示换行）
CUSTOM_SYSTEM_PROMPT = os.getenv("CUSTOM_SYSTEM_PROMPT", "")

# 对话（保留轮数，每轮=用户+助理各1条）
MAX_CONTEXT_MESSAGES = int(os.getenv("MAX_CONTEXT_MESSAGES", "5"))

# 限流
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "5"))

# 暖群：监听管理员发言，超时无管理员说话时主动暖群
WARM_ENABLED = os.getenv("WARM_ENABLED", "true").lower() in ("true", "1", "yes")
WARM_IDLE_MINUTES = int(os.getenv("WARM_IDLE_MINUTES", "120"))  # 无管理员发言多久后触发
WARM_COOLDOWN_MINUTES = int(os.getenv("WARM_COOLDOWN_MINUTES", "60"))  # 两次暖群最小间隔
WARM_CHECK_INTERVAL = int(os.getenv("WARM_CHECK_INTERVAL", "10"))  # 检查间隔（分钟）
WARM_SILENT_START = int(os.getenv("WARM_SILENT_START", "0")) if os.getenv("WARM_SILENT_START") != "" else None  # 静默时段开始（时，0-23）
WARM_SILENT_END = int(os.getenv("WARM_SILENT_END", "8")) if os.getenv("WARM_SILENT_END") != "" else None  # 静默时段结束（时）
# 暖群贴纸 file_id 列表，逗号分隔，如：CAACAgIAAxkB...,CAACAgIAAxkB...
WARM_STICKER_IDS = [s.strip() for s in (os.getenv("WARM_STICKER_IDS", "") or "").split(",") if s.strip()]
# 随机水群间隔（分钟），在此范围内随机
RANDOM_WATER_MIN_MINUTES = int(os.getenv("RANDOM_WATER_MIN_MINUTES", "30"))
RANDOM_WATER_MAX_MINUTES = int(os.getenv("RANDOM_WATER_MAX_MINUTES", "60"))

# 数据库
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/bot.db")

# 确保数据目录存在；Windows 用绝对路径，Ubuntu 用相对路径
DATA_DIR = (_XHCHAT_ROOT / "data") if _XHCHAT_ROOT else Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
