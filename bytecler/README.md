# Bytecler - Telegram 群消息监控机器人

基于 python-telegram-bot (PTB) 的群消息监控与垃圾过滤机器人。

## 功能

1. **广告检测**：网页预览消息且文本 ≤2 字自动删除
2. **垃圾关键词**：消息文本、昵称、简介命中关键词则删除（支持精确匹配/子串匹配/正则）
3. **人机验证**：简介含 tg 链接或 @ 的用户首次发言需验证，失败 5 次或超时则限制发言
4. **私聊管理**：通过私聊命令管理关键词（/list、/add_*、/del_*、/reload 等）

## 前置要求

- Python 3.8+
- **Bot Token**（从 @BotFather 获取）

## 安装

```bash
pip install -r requirements.txt
```

## 配置

1. 复制配置示例并编辑：

   ```bash
   copy config.example.env .env
   ```

2. 填写 `.env` 或环境变量：

   - `BOT_TOKEN`：从 @BotFather 获取
   - `GROUP_ID`：目标群 ID，多个用逗号分隔（超级群格式如 -100xxxxxxxxxx）
   - `ADMIN_IDS`：可修改关键词的管理员 user_id，逗号分隔；不配置则所有人可操作
   - `UNBAN_BOT_USERNAME`：验证失败达 5 次或超时后，用户需联系此机器人解封（默认 @XHNPBOT）
   - `TG_VERBOSE`：设为 1/true/yes 启用详细日志

## 启动

```bash
python bot.py
```

`bot.py` 为霜刃 PTB 实现（已合并原 bot_ptb + shared）。

首次运行会向配置的群发送「你好」。若收不到消息，请在 @BotFather 对机器人执行 `/setprivacy` 选择 Disable 关闭隐私模式。

## 私聊指令

| 指令 | 说明 |
|------|------|
| /start | 启动 |
| /help | 帮助 |
| /list | 查看垃圾关键词 |
| /add_text, /add_name, /add_bio | 添加关键词（两段式：发送命令后输入关键词） |
| /del_text, /del_name, /del_bio | 删除关键词（两段式） |
| /cancel | 取消当前操作 |
| /reload | 从文件重载关键词 |

**两段式**：发送命令后按提示输入关键词

- 子串匹配：直接输入，如 `加V`
- 精确匹配：`/` 前缀，如 `/ 加微信`

## 数据文件

- `spam_keywords.json`：垃圾关键词配置
- `verified_users.json`：已通过人机验证的用户
- `verification_failures.json`：验证失败次数（持久化，重启不丢失）
