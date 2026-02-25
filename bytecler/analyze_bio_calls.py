#!/usr/bin/env python3
"""分析 bio_calls.jsonl（被删除文案记录）的统计信息"""

import json
from collections import defaultdict
from datetime import datetime

def analyze():
    hourly = defaultdict(int)
    by_minute = defaultdict(int)
    by_second = defaultdict(int)
    timestamps = []
    
    with open("bio_calls.jsonl", "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                tstr = (data.get("time") or "").replace("Z", "+00:00")
                if not tstr:
                    continue
                ts = datetime.fromisoformat(tstr)
                hourly[ts.strftime("%Y-%m-%d %H:00")] += 1
                by_minute[ts.strftime("%Y-%m-%d %H:%M")] += 1
                by_second[ts.strftime("%Y-%m-%d %H:%M:%S")] += 1
                timestamps.append(ts)
            except (json.JSONDecodeError, KeyError) as e:
                print(f"解析错误: {e}")
    
    total = len(timestamps)
    if not timestamps:
        print("无数据")
        return
    
    timestamps.sort()
    start, end = timestamps[0], timestamps[-1]
    span_hours = (end - start).total_seconds() / 3600
    
    # 每条记录 = 1 次用户消息删除 + 1 次机器人提醒删除，实际删除量为 2x
    DELETES_PER_RECORD = 2
    total_deletes = total * DELETES_PER_RECORD

    print("=" * 60)
    print("被删除文案记录分析 (bio_calls.jsonl)")
    print("=" * 60)
    print(f"\n【总体统计】")
    print(f"  记录条数: {total} (每条=用户消息+机器人提醒)")
    print(f"  实际删除量: {total_deletes} 次 (用户+bot 各 {total} 次)")
    print(f"  时间范围: {start} ~ {end}")
    print(f"  跨度: {span_hours:.1f} 小时")
    print(f"  平均每小时: {total_deletes/max(span_hours, 0.01):.1f} 次删除")
    
    # 按小时统计
    print(f"\n【按小时分布】")
    sorted_hours = sorted(hourly.items())
    max_per_hour = max(hourly.values()) if hourly else 0
    for h, cnt in sorted_hours:
        bar = "#" * min(cnt // 2, 50) + "-" * (50 - min(cnt // 2, 50))
        print(f"  {h} UTC: {cnt:4d} {bar}")
    
    # 高峰时段
    print(f"\n【高峰时段 (Top 10 小时)】")
    top_hours = sorted(hourly.items(), key=lambda x: -x[1])[:10]
    for h, cnt in top_hours:
        print(f"  {h}: {cnt} 次")
    
    # 每分钟峰值
    max_per_min = max(by_minute.values()) if by_minute else 0
    peak_minutes = [(m, c) for m, c in by_minute.items() if c == max_per_min]
    
    print(f"\n【每分钟峰值】")
    print(f"  最高: {max_per_min} 次/分钟")
    for m, c in sorted(peak_minutes)[:5]:
        print(f"    发生在: {m}")
    
    # 每秒峰值
    max_per_sec = max(by_second.values()) if by_second else 0
    peak_seconds = [(s, c) for s, c in by_second.items() if c == max_per_sec]
    
    print(f"\n【每秒峰值】")
    print(f"  最高: {max_per_sec} 次/秒")
    for s, c in sorted(peak_seconds)[:5]:
        print(f"    发生在: {s}")
    
    # 删除设计覆盖评估（对照 bot.py 删除模块参数）
    # 背景：每条 bio_calls 记录对应 2 次删除（用户消息 + 机器人提醒）
    PENDING_DELETE_RETRY_MAX = 100
    PENDING_DELETE_RETRY_TTL = 3600
    PENDING_DELETE_RETRY_JOB_BATCH = 15
    drain_per_min = PENDING_DELETE_RETRY_JOB_BATCH / 2  # 每 2 分钟 15 条

    max_hour_records = max(hourly.values()) if hourly else 0
    max_hour_deletes = max_hour_records * DELETES_PER_RECORD
    max_min_deletes = max_per_min * DELETES_PER_RECORD

    print(f"\n【删除设计覆盖评估】(考虑每条记录=2次删除)")
    print(f"  设计参数: 队列最大 {PENDING_DELETE_RETRY_MAX} 条, TTL {PENDING_DELETE_RETRY_TTL}s, 兜底消化约 {drain_per_min:.1f} 条/min")
    print(f"  峰值小时: {max_hour_records} 条记录 -> {max_hour_deletes} 次实际删除")
    for fail_rate in (0.1, 0.2, 0.5):
        est_queue = int(max_hour_deletes * fail_rate)
        status = "OK" if est_queue < PENDING_DELETE_RETRY_MAX else "可能满"
        print(f"    失败率 {fail_rate*100:.0f}% -> 入队约 {est_queue} 条/小时 [{status}]")
    print(f"  峰值分钟: {max_per_min} 条记录 -> {max_min_deletes} 次删除, 10% 失败 -> 入队约 {int(max_min_deletes*0.1)}/min")
    est_min_fail_10 = int(max_min_deletes * 0.1)
    if est_min_fail_10 <= drain_per_min + 3:
        print(f"  结论: 当前负载下设计可覆盖（失败率<20% 时队列不会满）")
    elif est_min_fail_10 <= drain_per_min + 10:
        print(f"  结论: 峰值时若失败率>20% 可能接近队列上限，建议监控 evict_retry_fail")
    else:
        print(f"  结论: 峰值时队列可能积压，建议增大 PENDING_DELETE_RETRY_MAX 或降低失败率")
    print("=" * 60)

if __name__ == "__main__":
    analyze()
