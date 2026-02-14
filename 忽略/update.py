#!/usr/bin/env python3
"""
上传垃圾关键词配置到服务器
用法：python update.py
"""
import os
import paramiko

# 服务器配置
VULTR_IP = "155.138.211.201"
VULTR_USER = "root"
VULTR_PASSWORD = "+Do9z-E{VHuZ+Xtg"
REMOTE_BYTECLER_DIR = "/tgbot/xhbots/xhbot/bytecler"
LOCAL_BYTECLER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bytecler")
FILE_NAME = "spam_keywords.json"

def main():
    local_path = os.path.join(LOCAL_BYTECLER_DIR, FILE_NAME)
    remote_path = f"{REMOTE_BYTECLER_DIR}/{FILE_NAME}"
    
    if not os.path.exists(local_path):
        print(f"❌ 本地文件不存在: {local_path}")
        return 1
    
    print(f"⬆️  上传 {FILE_NAME} 到服务器...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        ssh.connect(VULTR_IP, username=VULTR_USER, password=VULTR_PASSWORD, timeout=30)
        sftp = ssh.open_sftp()
        sftp.put(local_path, remote_path)
        sftp.chmod(remote_path, 0o644)
        sftp.close()
        size_kb = os.path.getsize(local_path) / 1024
        print(f"✅ {FILE_NAME} 上传成功 ({size_kb:.2f} KB)")
        return 0
    except paramiko.AuthenticationException:
        print("❌ 连接失败：用户名或密码错误")
        return 1
    except Exception as e:
        print(f"❌ 上传失败: {e}")
        return 1
    finally:
        ssh.close()

if __name__ == "__main__":
    exit(main())
