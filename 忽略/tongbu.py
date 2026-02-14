#!/usr/bin/env python3
"""
数据库同步和下载脚本
功能：1. Excel转DB 2. 上传到Vultr 3. 从Vultr下载彩票数据库
使用方法：双击运行 或 python tongbu.py
"""

import os
import sys
import pandas as pd
import sqlite3
import paramiko
import shutil
from datetime import datetime
import time

# =================== 配置区域 ===================
# 在这里修改你的配置！

# 1. Excel文件路径（你的数据源）
EXCEL_FILE = r'C:\Users\Administrator\Desktop\robot.xlsx'

# 2. 本地数据库路径（转换后的DB文件）
LOCAL_DB_FILE = r'C:\Program Files\DB Browser for SQLite\robot.db'

# 3. Vultr服务器配置
VULTR_IP = "155.138.211.201"
VULTR_USER = "root"
VULTR_PASSWORD = "+Do9z-E{VHuZ+Xtg"
VULTR_DB_PATH = "/opt/botsearch/robot.db"  # Vultr上的robot.db路径

# 4. 下载配置（从Vultr下载彩票数据库）
DOWNLOAD_DB_PATH = "/tgbot/cjbot/cjdb/lottery.db"  # Vultr上的彩票数据库路径
DOWNLOAD_LOCAL_FILE = r'C:\Program Files\DB Browser for SQLite\lottery.db'  # 本地保存路径

# 5. bytecler 文件下载配置（白名单、bio调用、黑名单、关键词）
REMOTE_BYTECLER_DIR = "/tgbot/xhbots/xhbot/bytecler"  # Vultr 上的 bytecler 目录
LOCAL_BYTECLER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bytecler")  # 本地保存目录（xhbot/bytecler）
BYTECLER_FILES = [
    "verified_users.json",      # 白名单
    "bio_calls.jsonl",          # bio 调用
    "verification_blacklist.json",  # 黑名单
    "spam_keywords.json",       # 关键词
]

# 6. 备份配置
BACKUP_DIR = r'C:\Users\Administrator\Documents\Axure\backups'
KEEP_BACKUPS = 5  # 保留最近5个备份

# =================== 主程序 ===================

def excel_to_db():
    """第一步：将Excel转换为SQLite数据库"""
    print("📊 步骤1: 正在转换Excel到数据库...")
    
    # 确保目录存在
    os.makedirs(os.path.dirname(LOCAL_DB_FILE), exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    
    # 检查Excel文件是否存在
    if not os.path.exists(EXCEL_FILE):
        print(f"❌ Excel文件不存在: {EXCEL_FILE}")
        return False
    
    # 读取Excel
    try:
        df = pd.read_excel(EXCEL_FILE, engine='openpyxl')
        print(f"  读取成功！共 {len(df)} 行数据")
    except Exception as e:
        print(f"❌ 读取Excel失败: {e}")
        return False
    
    # 备份旧数据库（如果存在）
    if os.path.exists(LOCAL_DB_FILE):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = os.path.join(BACKUP_DIR, f"robot_backup_{timestamp}.db")
        shutil.copy2(LOCAL_DB_FILE, backup_file)
        print(f"  已备份旧数据库: {backup_file}")
        
        # 清理旧备份
        cleanup_old_backups("robot_backup_")
    
    # 写入数据库
    try:
        conn = sqlite3.connect(LOCAL_DB_FILE)
        df.to_sql('data', conn, if_exists='replace', index=False)
        conn.close()
        
        # 验证数据库
        db_size = os.path.getsize(LOCAL_DB_FILE)
        print(f"✅ 数据库创建成功！大小: {db_size/1024/1024:.2f} MB")
        print(f"  保存位置: {LOCAL_DB_FILE}")
        return True
        
    except Exception as e:
        print(f"❌ 写入数据库失败: {e}")
        return False

def upload_to_vultr():
    """第二步：上传数据库到Vultr"""
    print("\n☁  步骤2: 正在上传到Vultr服务器...")
    
    # 检查数据库文件是否存在
    if not os.path.exists(LOCAL_DB_FILE):
        print("❌ 本地数据库文件不存在，跳过上传步骤")
        return False
    
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        # 连接Vultr
        print(f"  正在连接 {VULTR_IP}...")
        ssh.connect(VULTR_IP, username=VULTR_USER, password=VULTR_PASSWORD, timeout=30)
        
        # 备份Vultr上的旧数据库
        sftp = ssh.open_sftp()
        try:
            sftp.stat(VULTR_DB_PATH)
            
            # 创建备份
            backup_name = f"{VULTR_DB_PATH}.backup_{datetime.now().strftime('%Y%m%d')}"
            ssh.exec_command(f"cp {VULTR_DB_PATH} {backup_name}")
            print(f"  Vultr上的旧数据库已备份: {backup_name}")
        except FileNotFoundError:
            print("  Vultr上未找到旧数据库，直接上传新文件")
        
        # 上传新数据库
        print(f"  正在上传数据库文件...")
        sftp.put(LOCAL_DB_FILE, VULTR_DB_PATH)
        
        # 设置权限
        ssh.exec_command(f"chmod 644 {VULTR_DB_PATH}")
        
        print(f"✅ 上传成功！")
        print(f"  位置: {VULTR_DB_PATH}")
        
        # 检查文件大小
        stdin, stdout, stderr = ssh.exec_command(f"du -h {VULTR_DB_PATH}")
        size_info = stdout.read().decode().strip()
        print(f"  远程文件大小: {size_info}")
        
        return True
        
    except paramiko.AuthenticationException:
        print("❌ 连接失败：用户名或密码错误")
        return False
    except Exception as e:
        print(f"❌ 上传失败: {e}")
        return False
    finally:
        ssh.close()

def download_from_vultr():
    """第三步：从Vultr服务器下载彩票数据库到本地"""
    print("\n⬇️  步骤3: 正在从Vultr下载彩票数据库...")
    
    # 确保本地目录存在
    local_dir = os.path.dirname(DOWNLOAD_LOCAL_FILE)
    if not os.path.exists(local_dir):
        os.makedirs(local_dir)
        print(f"  创建本地目录: {local_dir}")
    
    # 备份本地文件（如果存在）
    if os.path.exists(DOWNLOAD_LOCAL_FILE):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = os.path.join(BACKUP_DIR, f"lottery_backup_{timestamp}.db")
        os.makedirs(os.path.dirname(backup_file), exist_ok=True)
        shutil.copy2(DOWNLOAD_LOCAL_FILE, backup_file)
        print(f"  已备份本地文件: {backup_file}")
        
        # 清理旧备份
        cleanup_old_backups("lottery_backup_")
    
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        # 连接Vultr服务器
        print(f"  正在连接 {VULTR_IP}...")
        ssh.connect(VULTR_IP, username=VULTR_USER, password=VULTR_PASSWORD, timeout=30)
        
        # 检查远程文件是否存在
        sftp = ssh.open_sftp()
        try:
            remote_stat = sftp.stat(DOWNLOAD_DB_PATH)
            print(f"  远程文件存在，大小: {remote_stat.st_size / 1024:.2f} KB")
        except FileNotFoundError:
            print(f"❌ 远程文件不存在: {DOWNLOAD_DB_PATH}")
            return False
        
        # 下载文件
        print(f"  正在下载 {DOWNLOAD_DB_PATH}...")
        sftp.get(DOWNLOAD_DB_PATH, DOWNLOAD_LOCAL_FILE)
        
        # 验证下载的文件
        if os.path.exists(DOWNLOAD_LOCAL_FILE):
            local_size = os.path.getsize(DOWNLOAD_LOCAL_FILE)
            print(f"✅ 下载成功！")
            print(f"  保存到: {DOWNLOAD_LOCAL_FILE}")
            print(f"  文件大小: {local_size / 1024:.2f} KB")
            
            # 验证文件完整性
            if remote_stat.st_size == local_size:
                print("  ✓ 文件完整性验证通过")
            else:
                print(f"  ⚠  文件大小不一致: 远程={remote_stat.st_size} 本地={local_size}")
            
            return True
        else:
            print("❌ 下载失败：本地文件未创建")
            return False
            
    except paramiko.AuthenticationException:
        print("❌ 连接失败：用户名或密码错误")
        return False
    except Exception as e:
        print(f"❌ 下载失败: {e}")
        return False
    finally:
        ssh.close()

def download_bytecler_files():
    """第四步：从Vultr服务器下载 bytecler 四个文件（白名单、bio调用、黑名单、关键词）"""
    print("\n⬇️  步骤4: 正在从Vultr下载 bytecler 文件...")
    
    os.makedirs(LOCAL_BYTECLER_DIR, exist_ok=True)
    
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    success_count = 0
    try:
        print(f"  正在连接 {VULTR_IP}...")
        ssh.connect(VULTR_IP, username=VULTR_USER, password=VULTR_PASSWORD, timeout=30)
        sftp = ssh.open_sftp()
        
        for filename in BYTECLER_FILES:
            remote_path = f"{REMOTE_BYTECLER_DIR}/{filename}"
            local_path = os.path.join(LOCAL_BYTECLER_DIR, filename)
            try:
                sftp.stat(remote_path)
                if os.path.exists(local_path):
                    backup_subdir = os.path.join(BACKUP_DIR, "bytecler")
                    os.makedirs(backup_subdir, exist_ok=True)
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    backup_ext = ".json" if filename.endswith(".json") else ".jsonl"
                    backup_file = os.path.join(backup_subdir, f"{os.path.splitext(filename)[0]}_backup_{timestamp}{backup_ext}")
                    shutil.copy2(local_path, backup_file)
                    print(f"  已备份本地: {filename}")
                sftp.get(remote_path, local_path)
                local_size = os.path.getsize(local_path)
                print(f"  ✅ {filename} 下载成功 ({local_size / 1024:.2f} KB)")
                success_count += 1
            except FileNotFoundError:
                print(f"  ⚠  远程文件不存在，跳过: {filename}")
            except Exception as e:
                print(f"  ❌ {filename} 下载失败: {e}")
        
        sftp.close()
        print(f"✅ bytecler 文件下载完成，成功 {success_count}/{len(BYTECLER_FILES)} 个")
        return success_count == len(BYTECLER_FILES)
        
    except paramiko.AuthenticationException:
        print("❌ 连接失败：用户名或密码错误")
        return False
    except Exception as e:
        print(f"❌ 下载失败: {e}")
        return False
    finally:
        ssh.close()

def cleanup_old_backups(prefix):
    """清理旧的备份文件"""
    try:
        backups = []
        for file in os.listdir(BACKUP_DIR):
            if file.startswith(prefix) and file.endswith(".db"):
                filepath = os.path.join(BACKUP_DIR, file)
                if os.path.exists(filepath):
                    backups.append((filepath, os.path.getmtime(filepath)))
        
        # 按修改时间排序，删除最旧的
        backups.sort(key=lambda x: x[1])
        
        if len(backups) > KEEP_BACKUPS:
            for i in range(len(backups) - KEEP_BACKUPS):
                try:
                    os.remove(backups[i][0])
                    print(f"  清理旧备份: {os.path.basename(backups[i][0])}")
                except Exception as e:
                    print(f"  删除备份文件失败: {e}")
                
    except Exception as e:
        print(f"⚠  清理备份时出错: {e}")

def main():
    """主函数：一键执行所有步骤"""
    print("=" * 60)
    print("🚀 开始执行数据库同步和下载任务")
    print("=" * 60)
    print(f"📂 Excel文件: {EXCEL_FILE}")
    print(f"💾 本地数据库: {LOCAL_DB_FILE}")
    print(f"📥 下载目标: {DOWNLOAD_LOCAL_FILE}")
    print(f"📁 bytecler 下载: {REMOTE_BYTECLER_DIR} → {LOCAL_BYTECLER_DIR}")
    print(f"☁  远程服务器: {VULTR_IP}")
    print("-" * 60)
    
    start_time = datetime.now()
    
    # 执行第一步：Excel转DB
    step1_success = excel_to_db()
    
    # 执行第二步：上传到Vultr
    step2_success = False
    if step1_success:
        step2_success = upload_to_vultr()
    else:
        print("\n⚠  跳过上传步骤，因为Excel转换失败")
    
    # 执行第三步：从Vultr下载彩票数据库
    step3_success = download_from_vultr()
    
    # 执行第四步：从Vultr下载 bytecler 四个文件（白名单、bio调用、黑名单、关键词）
    step4_success = download_bytecler_files()
    
    # 显示执行结果
    print("\n" + "=" * 60)
    print("📊 执行结果汇总")
    print("=" * 60)
    print(f"1. Excel转DB: {'✅ 成功' if step1_success else '❌ 失败'}")
    print(f"2. 上传到Vultr: {'✅ 成功' if step2_success else '⚠  跳过/失败'}")
    print(f"3. 下载彩票数据库: {'✅ 成功' if step3_success else '❌ 失败'}")
    print(f"4. 下载 bytecler 文件: {'✅ 成功' if step4_success else '⚠  部分/失败'}")
    
    # 计算执行时间
    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()
    print(f"\n⏱️  总执行时间: {duration:.2f} 秒")
    print("=" * 60)
    
    print("程序将在10秒后自动退出...")
    time.sleep(10)

if __name__ == "__main__":
    main()
