#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
启动霜刃 PTB 版（Telethon 已移除）

使用方法：python start_both.py  或  python bot.py
"""
import subprocess
import sys
from pathlib import Path

def main():
    bytecler_dir = Path(__file__).resolve().parent
    print("=" * 50)
    print("启动霜刃 PTB 版")
    print("按 Ctrl+C 停止")
    print("=" * 50)
    p = subprocess.Popen([sys.executable, "bot.py"], cwd=str(bytecler_dir))
    p.wait()

if __name__ == "__main__":
    main()
