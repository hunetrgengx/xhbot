# 霜刃 PTB 改造分析

## 概述

本文档记录霜刃（bytecler）各功能使用 PTB（python-telegram-bot）的实现情况，以及 PTB 无法实现、必须保留 Telethon 的部分。

---

## 一、PTB 已实现 / 可实现的功能

| 功能 | 实现方式 | 说明 |
|------|----------|------|
| **封禁/限制事件监听** | `ChatMemberHandler` | 监听 `ChatMemberUpdated`，检测 banned/restricted/kicked/unbanned |
| **群消息接收** | `MessageHandler` | 接收群内新消息 |
| **私聊命令** | `CommandHandler` | /start, /help, /list, /reload, /cancel, /kw_* 等 |
| **Callback 按钮** | `CallbackQueryHandler` | 内联键盘回调（如「查看原始 JSON」） |
| **删除消息** | `bot.delete_message()` | 与 Telethon `delete_messages` 等效 |
| **限制用户** | `bot.restrict_chat_member()` | 与 Telethon `edit_permissions` 等效 |
| **发送消息** | `bot.send_message()` | 完全支持 |
| **设置 typing** | `bot.send_chat_action(chat_action='typing')` | 等效 `SetTypingRequest` |
| **设置 Bot 命令** | `bot.set_my_commands()` | 等效 `SetBotCommandsRequest` |
| **内联键盘** | `InlineKeyboardButton` | 等效 Telethon `Button.inline` |
| **垃圾关键词过滤（text）** | 纯逻辑 | 不依赖 API，PTB 可直接用 |
| **垃圾关键词过滤（name）** | 纯逻辑 | 消息中 sender 有 first_name/last_name |
| **关键词管理命令** | 纯逻辑 | /list, /kw_*, /reload 等 |
| **白名单/黑名单/验证失败** | JSON 文件读写 | 不依赖 API |
| **抽奖白名单同步** | SQLite + JSON | 不依赖 API |
| **AI 唤醒回复（KIMI）** | OpenAI SDK | 不依赖 API |
| **handoff 小助理转接** | 本地文件 | 不依赖 API |
| **新成员入群（user_added）** | `ChatMemberHandler` | 可检测 `new_chat_member.status == 'member'` 且 `old_chat_member.status` 为 left/kicked |

---

## 二、PTB 无法实现、必须 Telethon 的功能

| 功能 | 依赖 | 原因 |
|------|------|------|
| **获取用户 bio（简介）** | `GetFullUserRequest` | Telegram Bot API 不提供获取任意用户 bio 的接口，仅 MTProto 可调用 `users.getFullUser` |
| **人机验证：简介含链接/关键词触发** | 用户 bio | 需先获取 bio 才能判断是否含 tg 链接、http、@，PTB 无法获取 |
| **人机验证：简介关键词命中** | 用户 bio | 同上 |
| **私聊消息链接解析** | `client.get_messages(entity, ids=msg_id)` | 用户发送 `t.me/xxx/123` 时，Bot API 无法根据 chat_id + msg_id 拉取消息内容，需 MTProto |
| **「查看原始 JSON」按钮** | 同上 | 依赖根据链接获取消息对象并序列化 |
| **Raw 事件：管理员限制用户自动加黑名单** | `events.Raw(UpdateChannelParticipant)` | PTB 的 `ChatMemberHandler` 可检测 banned/restricted，但需区分「管理员操作」vs「用户离开」— PTB 的 `ChatMemberUpdated` 可做到 |
| **引用非本群消息判定** | `reply_to.reply_to_peer_id` | PTB 的 `Message.reply_to_message` 结构不同，跨群引用场景需核实 PTB 是否有等效字段 |

> **核心结论**：人机验证中「简介含链接/关键词」的触发逻辑、以及「私聊发送群消息链接返回验证过程」两个功能，**必须使用 Telethon**，Bot API 无替代方案。

---

## 三、霜刃功能清单与 PTB 对应关系

| 序号 | 功能 | PTB | Telethon |
|------|------|-----|----------|
| 1 | 群消息文本/昵称垃圾关键词 → 人机验证 | ✅ | - |
| 2 | 群消息 bio 垃圾关键词 / 简介含链接 → 人机验证 | ❌ | ✅ |
| 3 | 广告判定（网页且文本≤10）→ 人机验证 | ✅ | - |
| 4 | 引用非本群消息 → 人机验证 | ⚠️ 待核实 | ✅ |
| 5 | 验证码通过/失败、失败 5 次限制 | ✅ | - |
| 6 | 白名单用户 AI 唤醒（霜刃，/@bot） | ✅ | - |
| 7 | handoff 小助理转接 | ✅ | - |
| 8 | 抽奖白名单同步（启动 + 定时） | ✅ | - |
| 9 | 新成员入群、机器人大致加入白名单 | ✅ | - |
| 10 | 管理员限制/封禁用户 → 自动加黑名单 | ✅ | - |
| 11 | 私聊命令：/list, /kw_*, /reload, /verified_stats 等 | ✅ | - |
| 12 | 私聊发送群消息链接 → 返回验证过程 + 查看原始 JSON | ❌ | ✅ |
| 13 | 启动时群发「你好」、同步结果通知 | ✅ | - |

---

## 四、改造建议

1. **PTB 优先**：除「获取 bio」和「私聊链接拉取消息」外，其余逻辑均可迁移到 PTB。
2. **混合方案**：主流程用 PTB，仅当需要 bio 或私聊链接时调用 Telethon 子进程/服务。实现复杂度较高。
3. **双进程方案**：`bot.py`（Telethon）保留完整功能；`bot_ptb.py` 实现 PTB 可做部分，作为轻量或备用实例。
4. **当前实现**：`bot.py` 全功能（Telethon），`bot_ptb.py` 仅封禁事件日志（PTB）。

---

## 五、文件说明

| 文件 | 框架 | 功能 |
|------|------|------|
| `bot.py` | 入口 | 转发到 bot_ptb.py |
| `bot_ptb.py` | PTB | 霜刃主程序：封禁事件、群消息、关键词(text/name)、人机验证(无 bio)、命令 |
| `shared.py` | 无 | 共享逻辑（关键词、白名单、黑名单等），供 bot_ptb.py 使用 |
