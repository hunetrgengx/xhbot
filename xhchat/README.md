# XHChat - Telegram 群聊 AI 机器人

基于 Python 的 Telegram AI 聊天机器人，支持群聊 @提及 和私聊。

## 功能

- **群聊**：@提及 机器人、以「小助理，」开头、或回复机器人的消息继续对话
- **私聊**：直接发送消息即可
- **上下文**：按用户+群组维护对话历史
- **限流**：每用户每分钟请求数限制
- **新对话**：/newchat 清除历史，开始新对话
- **自定义设定**：可配置 AI 的人设和规则，所有人对话都会遵循

## 快速开始

### 1. 创建 Telegram 机器人

1. 在 Telegram 中搜索 [@BotFather](https://t.me/BotFather)
2. 发送 `/newbot`，按提示创建
3. 复制得到的 Token

### 2. 配置

```bash
# 复制环境变量模板
cp .env.example .env

# 编辑 .env，填入：
# TELEGRAM_BOT_TOKEN=你的机器人Token
# OPENAI_API_KEY=你的API Key (OpenAI 或 Kimi)
# 使用 Kimi 时设置：AI_PROVIDER=kimi
```

### 3. 安装依赖

```bash
pip install -r requirements.txt
```

### 4. 运行

```bash
python run.py
```

## 群聊使用

1. 将机器人加入群组
2. 在群内发送：`@你的机器人 你好` 或 `小助理，介绍一下自己`
3. 或回复机器人的某条消息进行追问
4. 使用「小助理，」时需关闭隐私模式（BotFather 中设置），否则机器人收不到该消息

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| TELEGRAM_BOT_TOKEN | Bot Token | 必填 |
| AI_PROVIDER | AI 提供商 | openai / kimi |
| OPENAI_API_KEY | API Key（OpenAI/Kimi 通用） | 必填 |
| OPENAI_BASE_URL | API 地址 | 按 provider 自动 |
| MODEL_NAME | 模型名称 | 按 provider 自动 |
| MAX_CONTEXT_MESSAGES | 上下文轮数 | 10 |
| RATE_LIMIT_PER_MINUTE | 每用户每分钟限制 | 5 |
| ENABLE_CONTEXT_CACHE | Kimi 上下文缓存（省钱） | true |

### Kimi 上下文缓存（省钱）

Kimi 的 `custom_prompt` 等固定内容每次请求都会重复发送。开启 `ENABLE_CONTEXT_CACHE=true` 后：
- 固定部分只向 Kimi 推送一次并缓存
- `custom_prompt.txt` 变更时会自动刷新缓存
- 可降低 token 消耗、加快响应

### 自定义设定（所有人对话遵循）

编辑 `config/custom_prompt.txt`，写入你的人设、规则等，例如：

```
你的名字叫小助理。
用简洁、亲切的语气回复。
拒绝回答违法、暴力相关的内容。
```

或通过 `.env` 设置 `CUSTOM_SYSTEM_PROMPT=你的设定`（用 `\n` 表示换行）。

### 使用 Kimi 月之暗面

1. 登录 [Kimi 开放平台](https://platform.moonshot.cn/) 创建 API Key
2. 在 `.env` 中配置：

```env
AI_PROVIDER=kimi
OPENAI_API_KEY=你的Kimi_API_Key
# 可选模型：moonshot-v1-8k | moonshot-v1-128k | moonshot-v1-1m | kimi-k2
MODEL_NAME=moonshot-v1-128k
```

## 项目结构

```
xhchat/
├── bot/
│   ├── handlers/      # 消息处理
│   ├── models/        # 数据库
│   ├── services/      # AI、上下文
│   └── main.py
├── config/
│   ├── settings.py
│   └── custom_prompt.txt   # 自定义设定（可选）
├── run.py
└── requirements.txt
```
