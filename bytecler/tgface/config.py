"""配置加载模块"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Telegram 配置
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
TIMEZONE = os.getenv("TIMEZONE", "Asia/Shanghai")
BOT_USERNAME = os.getenv("BOT_USERNAME", "")

# 存储路径
STORAGE_PATH = Path(os.getenv("STORAGE_PATH", "./storage"))
STORAGE_PATH.mkdir(parents=True, exist_ok=True)

# 日志路径（异常日志保存目录）
LOG_PATH = Path(os.getenv("LOG_PATH", "./logs"))
LOG_PATH.mkdir(parents=True, exist_ok=True)
