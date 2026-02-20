# 霜刃系统长期运行推演

## 一、内存结构概览

| 结构 | 类型 | 持久化 | 清理/上限 | 长期趋势 |
|------|------|--------|-----------|----------|
| `spam_keywords` | dict | ✅ json | 无 | 有界，管理员配置 |
| `verified_users` / `verified_users_details` / `join_times` | set/dict | ✅ json | 无 | **持续增长** |
| `verification_blacklist` | set | ✅ json | 无 | **持续增长** |
| `verification_failures` | dict | ✅ json | 1 天 TTL | 有界 |
| `_verification_records` | dict | ✅ json | 保存时截断 10000 | **有界** |
| `_last_message_by_user` | dict | ❌ | 24h TTL + 每 100 条清理 | 有界 |
| `_required_group_warn_count` | dict | ❌ | 每次 B 群验证前清理 | 有界 |
| `_required_group_info_cache` | dict | ❌ | 1 天 TTL（读时跳过） | **不删除过期 key** |
| `_user_in_required_group_cache` | dict | ❌ | 读时 TTL 判断 | **不删除过期 key** |
| `pending_*` | dict | ❌ | 超时 pop | 有界 |

---

## 二、长期运行推演

### 2.1 内存无界增长风险

#### 1. `_user_in_required_group_cache`

- **Key**：`(user_id, b_group_id)`
- **行为**：每次 B 群检查写入，读时若过期则重新请求，但**从不删除**过期条目
- **推演**：每触发一次 B 群校验就新增一条。活跃群、新用户多时，数月可积累数万条
- **影响**：内存缓慢增长，单条体积小，但长期可能到数十 MB

#### 2. `_required_group_info_cache`

- **Key**：`b_group_id`
- **行为**：每个群的 B 群最多一个，key 数量 ≈ 监控群数量
- **推演**：规模有界，过期条目不删影响很小

#### 3. `verified_users` / `verified_users_details` / `join_times`

- **行为**：通过验证即加入，无移除逻辑（除 `/reload` 重载）
- **推演**：随时间单调增长，群越活跃增长越快
- **影响**：`uid in verified_users` 为 O(1)，但 `verified_users_details`、`join_times` 会持续变大，影响内存和 `save_verified_users` 耗时

#### 4. `verification_blacklist`

- **行为**：验证失败 5 次或 B 群 5 次即加入，无自动移除
- **推演**：单调增长，速度通常低于白名单
- **影响**：规模通常较小，风险较低

---

### 2.2 文件无界增长风险

#### 1. `bio_calls.jsonl`

- **行为**：每次删除消息/触发验证即追加一行，**无轮转、无截断**
- **推演**：每条约 100–500 字节，日删 1000 条 ≈ 0.5MB/天，一年约 180MB
- **影响**：磁盘占用持续增加，读取全文件会变慢（若将来需要）

#### 2. `restricted_users.jsonl`

- **行为**：每次限制/封禁追加一行，**无轮转**
- **推演**：频率低，增长慢，但长期仍会变大

#### 3. `verification_records.json`

- **行为**：`save` 时若超过 10000 条，保留最新 10000 条
- **推演**：有界，单文件约数 MB 级别

---

### 2.3 定时任务与并发

- **抽奖同步**：每日 20:00 UTC，读 lottery.db，写入 `verified_users`
- **frost_reply**：每 2 秒轮询 handoff
- **推演**：无长时间阻塞，正常负载下问题不大

---

### 2.4 边界与异常场景

| 场景 | 可能问题 |
|------|----------|
| 监控群数量大增 | `TARGET_GROUP_IDS` 变大，消息量上升，`_verification_records` 写入更频繁 |
| 大量新用户涌入 | B 群校验、验证码流程触发增多，`_user_in_required_group_cache`、`verified_users` 增长加快 |
| 长时间断网后恢复 | 大量堆积更新，可能短时高负载 |
| 磁盘满 | 所有 `save_*`、`_log_*` 写文件可能失败，需监控 |

---

## 三、建议优化（按优先级）

### 高优先级

1. **`_user_in_required_group_cache` 定期清理**
   - 在读或定时任务中，删除 `now - ts > max(TTL_in, TTL_not_in)` 的条目
   - 或使用 LRU 结构限制最大条目数

2. **`bio_calls.jsonl` 轮转**
   - 按大小或行数轮转（如单文件 50MB 或 10 万行）
   - 或按日期分文件，定期归档/删除旧文件

### 中优先级

3. **`verified_users_details` / `join_times` 裁剪**
   - 仅保留最近 N 天或最近 M 个用户
   - 或改为懒加载，按需从文件读取

4. **`_required_group_info_cache` 过期删除**
   - 读时若过期，删除该 key 再重新拉取，避免 dict 残留过期数据

### 低优先级

5. **`restricted_users.jsonl` 轮转**
   - 与 `bio_calls` 类似，按大小或时间轮转

6. **监控与告警**
   - 监控 `bio_calls.jsonl`、`verification_records.json` 大小
   - 监控进程内存
   - 磁盘空间不足时告警

---

## 四、当前已做的防护

- `_verification_records`：保存时截断至 10000 条
- `verification_failures`：1 天 TTL，加载/保存时过滤
- `_last_message_by_user`：24h TTL + 每 100 条消息触发清理
- `_required_group_warn_count`：每次 B 群验证前清理过期 key
- `_required_group_info_cache`：1 天 TTL（读时跳过过期）
- `pending_*`：均有超时清理

---

## 五、结论

- **短期（数周）**：无明显风险
- **中期（数月）**：`_user_in_required_group_cache`、`verified_users` 系列、`bio_calls.jsonl` 会持续增长
- **长期（一年以上）**：需对上述结构做清理/轮转，否则存在内存与磁盘压力

建议优先实现：`_user_in_required_group_cache` 过期清理、`bio_calls.jsonl` 轮转。
