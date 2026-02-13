#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一启动入口 - 同时启动两个机器人
- bytecler/bot.py: Telegram 群消息监控与垃圾过滤机器人
- xhchat/run.py: AI 聊天机器人
"""

import asyncio
import importlib.util
import logging
import os
import sys
import threading
import time
from pathlib import Path
from io import StringIO

# 配置日志格式，统一两个机器人的日志输出
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(name)s] - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
    force=True  # 强制重新配置，避免重复配置
)

# 创建日志记录器
logger = logging.getLogger('main')

# 将 xhbot 根目录加入 path，便于 bytecler/xhchat 导入 handoff 模块
_xhbot_root = Path(__file__).resolve().parent
if str(_xhbot_root) not in sys.path:
    sys.path.insert(0, str(_xhbot_root))


# 线程名 -> logger 名称映射，用于 stderr 输出时带上正确前缀
_THREAD_TO_LOGGER = {
    "XhChatThread": "xhchat.print",
    "MainThread": "bytecler.print",  # 主线程运行 bytecler 时
}


class ThreadAwareStderrWrapper:
    """线程感知的 stderr 包装器：根据当前线程名使用正确前缀"""
    def __init__(self, original_stderr):
        self._original = original_stderr
    
    def write(self, message):
        if not message.strip():
            return
        thread_name = threading.current_thread().name
        logger_name = _THREAD_TO_LOGGER.get(thread_name, "main")
        log = logging.getLogger(logger_name)
        # 多行内容（如 traceback）逐行输出
        for line in message.rstrip().split("\n"):
            if line.strip():
                log.error(line)
    
    def flush(self):
        if hasattr(self._original, "flush"):
            self._original.flush()
    
    def __getattr__(self, name):
        return getattr(self._original, name)


class PrintToLogger:
    """将 print 输出重定向到日志系统（仅用于 bytecler 主线程的 stdout）"""
    def __init__(self, bot_name=''):
        self.logger = logging.getLogger(f'{bot_name}.print' if bot_name else 'print')
        self.bot_name = bot_name
    
    def write(self, message):
        if message.strip():
            self.logger.info(message.strip())
    
    def flush(self):
        pass


class ColoredFormatter(logging.Formatter):
    """彩色日志格式化器，区分不同机器人"""
    COLORS = {
        'bytecler': '\033[94m',  # 蓝色
        'xhchat': '\033[92m',    # 绿色
        'main': '\033[93m',      # 黄色
        'RESET': '\033[0m',
    }
    
    def format(self, record):
        # 根据日志名称和路径添加颜色前缀
        name = record.name.lower()
        pathname = str(record.pathname).lower() if hasattr(record, 'pathname') else ''
        
        # 检查是否来自 bytecler（通过 logger 名称或文件路径）
        if 'bytecler' in name or 'bytecler' in pathname or '\\bytecler\\' in pathname or '/bytecler/' in pathname:
            prefix = self.COLORS['bytecler'] + '[BYTECLER]' + self.COLORS['RESET']
        # 检查是否来自 xhchat（通过 logger 名称或文件路径）
        elif 'xhchat' in name or 'xhchat' in pathname or '\\xhchat\\' in pathname or '/xhchat/' in pathname:
            prefix = self.COLORS['xhchat'] + '[XHCHAT]' + self.COLORS['RESET']
        else:
            prefix = self.COLORS['main'] + '[MAIN]' + self.COLORS['RESET']
        
        # 格式化日志消息
        log_msg = super().format(record)
        return f"{prefix} {log_msg}"


# 应用彩色格式化器
for handler in logging.root.handlers:
    handler.setFormatter(ColoredFormatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))


def run_bytecler():
    """运行 bytecler 机器人"""
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    original_cwd = os.getcwd()
    
    try:
        logger.info("正在启动 Bytecler 机器人...")
        bytecler_path = Path(__file__).parent / 'bytecler'
        
        # bytecler 使用相对路径（如 spam_keywords.json），需切换工作目录
        os.chdir(bytecler_path)
        
        # 重定向输出到日志；stderr 使用线程感知包装器，确保各线程错误带上正确前缀
        sys.stdout = PrintToLogger(bot_name='bytecler')
        sys.stderr = ThreadAwareStderrWrapper(original_stderr)
        
        try:
            # 显式加载 bytecler/bot.py，避免与 xhchat 的 bot 包冲突
            bot_file = bytecler_path / "bot.py"
            spec = importlib.util.spec_from_file_location("bytecler_bot", bot_file)
            bytecler_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(bytecler_module)
            asyncio.run(bytecler_module.main())
        finally:
            os.chdir(original_cwd)
            sys.stdout = original_stdout
            sys.stderr = original_stderr
    except KeyboardInterrupt:
        logger.info("Bytecler 机器人已停止")
    except Exception as e:
        os.chdir(original_cwd)
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        logger.error(f"Bytecler 机器人启动失败: {e}", exc_info=True)
        raise


def run_xhchat():
    """运行 xhchat 机器人（在单独线程中）"""
    try:
        logger.info("正在启动 XhChat 机器人...")
        # 切换到 xhchat 目录
        xhchat_path = Path(__file__).parent / 'xhchat'
        original_path = sys.path.copy()
        sys.path.insert(0, str(xhchat_path))
        
        try:
            # 显式加载 xhchat/run.py，避免 "run" 无法解析的静态分析警告
            run_file = xhchat_path / "run.py"
            spec = importlib.util.spec_from_file_location("xhchat_run", run_file)
            xhchat_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(xhchat_module)
            xhchat_module.main()
        finally:
            # 恢复原始路径
            sys.path[:] = original_path
    except KeyboardInterrupt:
        logger.info("XhChat 机器人已停止")
    except Exception as e:
        logger.error(f"XhChat 机器人启动失败: {e}", exc_info=True)
        raise


def main():
    """主函数：同时启动两个机器人"""
    logger.info("=" * 60)
    logger.info("正在启动双机器人系统...")
    logger.info("=" * 60)
    
    # 检查必要的文件是否存在
    bytecler_bot = Path(__file__).parent / 'bytecler' / 'bot.py'
    xhchat_run = Path(__file__).parent / 'xhchat' / 'run.py'
    
    if not bytecler_bot.exists():
        logger.error(f"未找到 bytecler/bot.py: {bytecler_bot}")
        sys.exit(1)
    
    if not xhchat_run.exists():
        logger.error(f"未找到 xhchat/run.py: {xhchat_run}")
        sys.exit(1)
    
    logger.info("文件检查通过，开始启动机器人...")
    
    # 在单独线程中运行 xhchat（因为它使用同步的 run_polling）
    xhchat_thread = threading.Thread(
        target=run_xhchat,
        name="XhChatThread",
        daemon=True
    )
    xhchat_thread.start()
    
    # 等待一下，让 xhchat 先启动
    logger.info("等待 XhChat 机器人初始化...")
    time.sleep(2)
    
    # 检查 xhchat 线程是否还在运行
    if not xhchat_thread.is_alive():
        logger.error("XhChat 机器人启动失败，线程已退出")
        sys.exit(1)
    
    logger.info("XhChat 机器人已启动，现在启动 Bytecler 机器人...")
    
    # 在主线程中运行 bytecler（因为它使用 asyncio）
    try:
        run_bytecler()
    except KeyboardInterrupt:
        logger.info("收到停止信号 (Ctrl+C)，正在关闭...")
    except Exception as e:
        logger.error(f"启动过程中发生错误: {e}", exc_info=True)
        sys.exit(1)
    finally:
        logger.info("=" * 60)
        logger.info("双机器人系统已关闭")
        logger.info("=" * 60)


if __name__ == "__main__":
    main()




