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
import signal
import subprocess
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
    force=True  # 强制重新配置，避免重复配置1
)

# 创建日志记录器
logger = logging.getLogger('main')

# 将 xhbot 根目录加入 path，便于 bytecler/xhchat 导入 handoff 模块
_xhbot_root = Path(__file__).resolve().parent
if str(_xhbot_root) not in sys.path:
    sys.path.insert(0, str(_xhbot_root))


# 线程名 -> logger 名称映射，用于 stderr 输出时带上正确前缀
_THREAD_TO_LOGGER = {
    "ByteclerThread": "bytecler.print",
    "MainThread": "xhchat.print",  # 主线程运行 xhchat（run_polling 需主线程）
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


# Ctrl+C / kill 时终止 bytecler 子进程并退出
def _signal_handler(signum, frame):
    global _bytecler_proc
    try:
        if _bytecler_proc and _bytecler_proc.poll() is None:
            _bytecler_proc.terminate()
            _bytecler_proc.wait(timeout=3)
    except Exception:
        pass
    os._exit(0)


_orig_signal = signal.signal
def _our_signal(signum, handler):
    if signum in (signal.SIGINT, signal.SIGTERM):
        return _orig_signal(signum, _signal_handler)
    return _orig_signal(signum, handler)
signal.signal = _our_signal
signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)

# 应用彩色格式化器
for handler in logging.root.handlers:
    handler.setFormatter(ColoredFormatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))


_bytecler_proc = None  # 子进程句柄，用于 Ctrl+C 时终止


def run_bytecler_subprocess():
    """以子进程运行 bytecler，避免 PTB run_polling 的 add_signal_handler 必须在主线程的限制"""
    global _bytecler_proc
    xhbot_root = Path(__file__).resolve().parent
    bytecler_path = xhbot_root / 'bytecler'
    bot_py = bytecler_path / 'bot.py'
    if not bot_py.exists():
        logger.error(f"未找到 bytecler/bot.py: {bot_py}")
        return False
    logger.info("正在启动 Bytecler 机器人（子进程）...")
    try:
        # 子进程继承 stdout/stderr，nohup 重定向时一并写入 bot.log
        _bytecler_proc = subprocess.Popen(
            [sys.executable, str(bot_py)],
            cwd=str(bytecler_path),
            stdin=subprocess.DEVNULL,
            stdout=sys.stdout,
            stderr=sys.stderr,
            env=os.environ.copy(),
        )
        return True
    except Exception as e:
        logger.error(f"Bytecler 子进程启动失败: {e}", exc_info=True)
        return False


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
    """主函数：同时启动两个机器人

    PTB 的 run_polling 必须在主线程运行（add_signal_handler 限制），
    因此 bytecler 以子进程运行（自有主线程），xhchat 跑主进程主线程。
    """
    global _bytecler_proc
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

    # bytecler 以子进程运行，避免 PTB run_polling 的 set_wakeup_fd 主线程限制
    if not run_bytecler_subprocess():
        sys.exit(1)

    logger.info("等待 Bytecler 机器人初始化...")
    time.sleep(2)

    if _bytecler_proc and _bytecler_proc.poll() is not None:
        logger.error("Bytecler 机器人启动失败，进程已退出")
        sys.exit(1)

    logger.info("Bytecler 已启动，现在启动 XhChat 机器人（主线程）...")

    # xhchat 的 run_polling 必须在主线程
    try:
        run_xhchat()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error(f"启动过程中发生错误: {e}", exc_info=True)
        os._exit(1)

    # 终止 bytecler 子进程
    try:
        if _bytecler_proc and _bytecler_proc.poll() is None:
            _bytecler_proc.terminate()
            _bytecler_proc.wait(timeout=5)
    except Exception:
        try:
            _bytecler_proc.kill()
        except Exception:
            pass
    os._exit(0)


if __name__ == "__main__":
    main()




