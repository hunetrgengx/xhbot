#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
同时启动 Telethon 和 PTB 两个机器人

使用方法：
1. 直接运行：python start_both.py
2. 或分别运行：python bot.py 和 python bot_ptb.py（两个终端）
"""

import asyncio
import subprocess
import sys
import signal
from pathlib import Path

processes = []

def signal_handler(sig, frame):
    """处理 Ctrl+C，停止所有进程"""
    print("\n正在停止所有机器人...")
    for p in processes:
        if p.poll() is None:  # 进程还在运行
            p.terminate()
    sys.exit(0)

def run_telethon():
    """运行 Telethon 机器人"""
    print("[Telethon] 启动中...")
    p = subprocess.Popen([sys.executable, "bot.py"])
    processes.append(p)
    return p

def run_ptb():
    """运行 PTB 机器人"""
    print("[PTB] 启动中...")
    p = subprocess.Popen([sys.executable, "bot_ptb.py"])
    processes.append(p)
    return p

def main():
    """同时运行两个机器人"""
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    print("=" * 50)
    print("同时启动 Telethon 和 PTB 机器人")
    print("按 Ctrl+C 停止所有机器人")
    print("=" * 50)
    
    # 启动两个进程
    p1 = run_telethon()
    p2 = run_ptb()
    
    # 等待进程结束
    try:
        p1.wait()
        p2.wait()
    except KeyboardInterrupt:
        signal_handler(None, None)

if __name__ == "__main__":
    main()
