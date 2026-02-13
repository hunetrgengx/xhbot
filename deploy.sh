#!/bin/bash
# xhbot 双机器人一键部署脚本（小助理 + 霜刃）
# 用法: bash deploy.sh
#
# 目录说明:
#   代码目录: /tgbot/xhbots/xhbot     （请通过 git 拉取代码到此目录）
#   部署目录: /tgbot/xhbots/xhbotsh   （venv、脚本、日志、备份等）

set -e

echo "🤖 xhbot 双机器人部署脚本（小助理 + 霜刃）"
echo "========================================"
echo "📁 目录结构:"
echo "  代码:   /tgbot/xhbots/xhbot"
echo "  部署:   /tgbot/xhbots/xhbotsh"
echo "  日志:   /tgbot/xhbots/xhbotsh/logs/"
echo "========================================"

# 路径常量
CODE_DIR="/tgbot/xhbots/xhbot"
DEPLOY_DIR="/tgbot/xhbots/xhbotsh"

# 检查是否为 root 用户
if [ "$EUID" -ne 0 ]; then
    echo "⚠️  建议使用 root 用户运行此脚本"
    echo "  按 Ctrl+C 取消，或按 Enter 继续..."
    read
fi

# 检查代码目录是否存在
if [ ! -d "$CODE_DIR" ]; then
    echo "❌ 代码目录不存在: $CODE_DIR"
    echo "   请先创建目录并 git clone 拉取代码"
    exit 1
fi

if [ ! -f "$CODE_DIR/main.py" ]; then
    echo "❌ 未找到 main.py: $CODE_DIR/main.py"
    echo "   请确保已通过 git 拉取完整代码"
    exit 1
fi

echo "✅ 代码目录检查通过"
echo "========================================"

# 创建部署目录结构
echo "📁 创建部署目录..."
mkdir -p "$DEPLOY_DIR"
mkdir -p "$DEPLOY_DIR/logs"
mkdir -p "$DEPLOY_DIR/backup"

chmod 755 "$DEPLOY_DIR"
chmod 755 "$DEPLOY_DIR/logs" "$DEPLOY_DIR/backup"

echo "✅ 目录创建完成"
echo "========================================"

# 检查 Python 环境
echo "🔍 检查 Python 环境..."
if ! command -v python3 &> /dev/null; then
    echo "❌ Python3 未安装，正在安装..."
    apt-get update
    apt-get install -y python3 python3-venv python3-pip
fi
python3 --version

# 创建 Python 虚拟环境
echo "🐍 配置 Python 虚拟环境..."
cd "$DEPLOY_DIR"

if [ -d "venv" ]; then
    echo "✅ 虚拟环境已存在"
    echo "  是否重新创建？[y/N]"
    read -p "选择: " recreate_venv
    if [[ $recreate_venv =~ ^[Yy]$ ]]; then
        echo "🔄 重新创建虚拟环境..."
        rm -rf venv
        python3 -m venv venv
    fi
else
    echo "📦 创建虚拟环境..."
    python3 -m venv venv
fi

source venv/bin/activate

# 安装依赖（合并 xhchat + bytecler）
echo "📚 安装依赖包..."
pip install --upgrade pip
pip install telethon>=1.36.0 openai>=1.0.0 python-telegram-bot>=20.0 python-dotenv>=1.0.0 pyahocorasick>=2.0.0 httpx>=0.24.0

echo "✅ 依赖安装完成"
echo "========================================"

# 检查配置文件
echo "⚙️  检查配置文件..."

if [ ! -f "$CODE_DIR/bytecler/.env" ]; then
    echo "⚠️  bytecler/.env 不存在"
    if [ -f "$CODE_DIR/bytecler/config.example.env" ]; then
        echo "   可复制: cp $CODE_DIR/bytecler/config.example.env $CODE_DIR/bytecler/.env"
        echo "   然后编辑填入 BOT_TOKEN、GROUP_ID、ADMIN_IDS 等"
    fi
else
    echo "✅ bytecler/.env 已存在"
fi

if [ ! -f "$CODE_DIR/xhchat/.env" ]; then
    echo "⚠️  xhchat/.env 不存在"
    if [ -f "$CODE_DIR/xhchat/.env.example" ] 2>/dev/null || [ -f "$CODE_DIR/xhchat/config/.env.example" ] 2>/dev/null; then
        echo "   请参考 xhchat 目录下的 .env.example 创建 .env"
    fi
else
    echo "✅ xhchat/.env 已存在"
fi

echo "========================================"

# 创建管理脚本
echo "🛠️  创建管理脚本..."

# 1. 启动脚本
cat > "$DEPLOY_DIR/start.sh" << 'START_EOF'
#!/bin/bash
# xhbot 双机器人启动脚本

echo "🤖 启动 xhbot（小助理 + 霜刃）..."
echo "========================================"

DEPLOY_DIR="/tgbot/xhbots/xhbotsh"
CODE_DIR="/tgbot/xhbots/xhbot"

cd "$DEPLOY_DIR"

if [ ! -d "venv" ]; then
    echo "❌ 虚拟环境不存在，请先运行 deploy.sh"
    exit 1
fi

source venv/bin/activate

if [ ! -f "$CODE_DIR/main.py" ]; then
    echo "❌ 主程序不存在: $CODE_DIR/main.py"
    exit 1
fi

# 停止已运行进程（只匹配 xhbotsh，避免误杀 cjbot）
echo "🛑 停止已运行的 xhbot 进程..."
pkill -f "xhbotsh/venv.*main\.py" 2>/dev/null || true
sleep 2

# 启动机器人（工作目录为代码根目录）
echo "🚀 启动机器人..."
cd "$CODE_DIR"
nohup "$DEPLOY_DIR/venv/bin/python3" main.py >> "$DEPLOY_DIR/logs/bot.log" 2>&1 &
cd "$DEPLOY_DIR"

sleep 3
echo "✅ 机器人已启动"

PID=$(pgrep -f "python3.*main.py" | head -1)
if [ -z "$PID" ]; then
    echo "⚠️  进程可能未正常启动，请检查日志"
else
    echo "📊 进程 PID: $PID"
fi

echo "📝 查看日志: tail -f $DEPLOY_DIR/logs/bot.log"
echo "🛑 停止命令: $DEPLOY_DIR/stop.sh"
echo "========================================"
START_EOF

# 2. 停止脚本
cat > "$DEPLOY_DIR/stop.sh" << STOP_EOF
#!/bin/bash
# xhbot 双机器人停止脚本（会停止所有 xhbot 实例，不误杀 cjbot 等）

CODE_DIR="/tgbot/xhbots/xhbot"
DEPLOY_DIR="/tgbot/xhbots/xhbotsh"

echo "🛑 停止 xhbot..."

# 只匹配 xhbotsh 路径，避免误杀 cjbot 等其他机器人
PIDS=\$(pgrep -f "xhbotsh/venv.*main\.py")
if [ -z "\$PIDS" ]; then
    echo "❌ 未找到运行中的 xhbot 进程"
else
    echo "🔍 找到进程: \$PIDS"
    echo "\$PIDS" | xargs -r kill -15 2>/dev/null
    sleep 3
    REMAIN=\$(pgrep -f "xhbotsh/venv.*main\.py")
    if [ -n "\$REMAIN" ]; then
        echo "⚠️  部分进程仍在运行，强制停止..."
        echo "\$REMAIN" | xargs -r kill -9 2>/dev/null
        sleep 1
    fi
    echo "✅ 已停止所有 xhbot 进程"
fi

echo "========================================"
STOP_EOF

# 3. 重启脚本
cat > "$DEPLOY_DIR/restart.sh" << 'RESTART_EOF'
#!/bin/bash
# xhbot 双机器人重启脚本

DEPLOY_DIR="/tgbot/xhbots/xhbotsh"

echo "🔄 重启 xhbot..."
echo "========================================"

"$DEPLOY_DIR/stop.sh"
sleep 2
"$DEPLOY_DIR/start.sh"

echo "✅ 重启完成"
RESTART_EOF

# 4. 状态检查脚本
cat > "$DEPLOY_DIR/status.sh" << 'STATUS_EOF'
#!/bin/bash
# xhbot 状态检查脚本

CODE_DIR="/tgbot/xhbots/xhbot"
DEPLOY_DIR="/tgbot/xhbots/xhbotsh"

echo "📊 xhbot 状态检查"
echo "========================================"

PID=$(pgrep -f "python3.*main.py" | head -1)
if [ -z "$PID" ]; then
    PID=$(ps aux | grep -E "python3.*main\.py" | grep -v grep | awk '{print $2}' | head -1)
fi

if [ -z "$PID" ]; then
    echo "❌ 状态: 未运行"
else
    echo "✅ 状态: 运行中"
    echo "📈 进程 PID: $PID"
    ps -p $PID -o pid,ppid,cmd,%mem,%cpu,etime --no-headers 2>/dev/null || true
fi

echo "----------------------------------------"

if [ -f "$CODE_DIR/bytecler/.env" ]; then
    echo "✅ bytecler/.env: 存在"
else
    echo "❌ bytecler/.env: 缺失"
fi

if [ -f "$CODE_DIR/xhchat/.env" ]; then
    echo "✅ xhchat/.env: 存在"
else
    echo "❌ xhchat/.env: 缺失"
fi

echo "----------------------------------------"

if [ -f "$DEPLOY_DIR/logs/bot.log" ]; then
    LOG_SIZE=$(du -h "$DEPLOY_DIR/logs/bot.log" 2>/dev/null | awk '{print $1}')
    echo "✅ 日志: $DEPLOY_DIR/logs/bot.log (${LOG_SIZE})"
    echo "📋 最近日志:"
    tail -5 "$DEPLOY_DIR/logs/bot.log" 2>/dev/null | sed 's/^/  /'
else
    echo "📭 日志: 尚未生成"
fi

echo "========================================"
STATUS_EOF

# 5. 日志查看脚本
cat > "$DEPLOY_DIR/logs.sh" << 'LOGS_EOF'
#!/bin/bash

LOG_FILE="/tgbot/xhbots/xhbotsh/logs/bot.log"

echo "📝 xhbot 日志查看"
echo "========================================"

if [ ! -f "$LOG_FILE" ]; then
    echo "❌ 日志文件不存在"
    exit 1
fi

echo "日志文件: $LOG_FILE"
echo "文件大小: $(du -h "$LOG_FILE" | awk '{print $1}')"
echo "========================================"
echo "  1) 实时查看（tail -f）"
echo "  2) 查看最后100行"
echo "  3) 查看错误信息"
echo "  4) 退出"
read -p "请选择 (1-4): " c
case $c in
    1) tail -f "$LOG_FILE" ;;
    2) tail -100 "$LOG_FILE" ;;
    3) grep -i "error\|fail\|exception\|traceback" "$LOG_FILE" | tail -50 ;;
    *) echo "退出" ;;
esac
LOGS_EOF

# 6. 备份脚本
cat > "$DEPLOY_DIR/backup.sh" << 'BACKUP_EOF'
#!/bin/bash
# xhbot 备份脚本

CODE_DIR="/tgbot/xhbots/xhbot"
BACKUP_DIR="/tgbot/xhbots/xhbotsh/backup"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_FILE="$BACKUP_DIR/xhbot_backup_$TIMESTAMP.tar.gz"

mkdir -p "$BACKUP_DIR"

echo "💾 xhbot 备份"
echo "========================================"

tar -czf "$BACKUP_FILE" --ignore-failed-read \
    -C "$CODE_DIR" bytecler/.env bytecler/spam_keywords.json bytecler/verified_users.json bytecler/verification_blacklist.json \
    xhchat/.env xhchat/data 2>/dev/null || true

if [ $? -eq 0 ]; then
    echo "✅ 备份成功: $(basename $BACKUP_FILE)"
else
    echo "❌ 备份失败"
fi

find "$BACKUP_DIR" -name "xhbot_backup_*.tar.gz" -mtime +7 -delete 2>/dev/null
echo "========================================"
BACKUP_EOF

# 设置脚本执行权限
chmod +x "$DEPLOY_DIR"/*.sh

echo "✅ 所有管理脚本创建完成"
echo "========================================"

# 创建 systemd 服务文件（可选）
cat > "$DEPLOY_DIR/xhbot.service" << SERVICE_EOF
[Unit]
Description=xhbot Dual Bot (XhChat + Bytecler)
After=network.target
Wants=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/tgbot/xhbots/xhbot
Environment=PATH=/tgbot/xhbots/xhbotsh/venv/bin:/usr/bin:/bin
ExecStart=/tgbot/xhbots/xhbotsh/venv/bin/python3 /tgbot/xhbots/xhbot/main.py
Restart=always
RestartSec=10
StandardOutput=append:/tgbot/xhbots/xhbotsh/logs/bot.log
StandardError=append:/tgbot/xhbots/xhbotsh/logs/bot.log

[Install]
WantedBy=multi-user.target
SERVICE_EOF

echo "📋 systemd 服务文件已创建: $DEPLOY_DIR/xhbot.service"
echo "  启用方式:"
echo "    cp $DEPLOY_DIR/xhbot.service /etc/systemd/system/"
echo "    systemctl daemon-reload"
echo "    systemctl enable xhbot"
echo "    systemctl start xhbot"
echo "========================================"

# 测试环境
echo "🧪 测试环境..."
cd "$DEPLOY_DIR"
source venv/bin/activate
python3 -c "
import sys
sys.path.insert(0, '$CODE_DIR')
try:
    import telegram
    import telethon
    import openai
    import dotenv
    print('✅ Python 依赖测试通过')
except ImportError as e:
    print(f'❌ 依赖缺失: {e}')
    sys.exit(1)
"

echo "========================================"
echo "✅ 部署完成"
echo ""
echo "📌 使用说明:"
echo "  启动: $DEPLOY_DIR/start.sh"
echo "  停止: $DEPLOY_DIR/stop.sh"
echo "  重启: $DEPLOY_DIR/restart.sh"
echo "  状态: $DEPLOY_DIR/status.sh"
echo "  日志: $DEPLOY_DIR/logs.sh"
echo "  备份: $DEPLOY_DIR/backup.sh"
echo ""
echo "⚠️  请确保 bytecler/.env 和 xhchat/.env 已正确配置"
echo "========================================"
