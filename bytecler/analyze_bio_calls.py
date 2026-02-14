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
    
    print("=" * 60)
    print("被删除文案记录分析 (bio_calls.jsonl)")
    print("=" * 60)
    print(f"\n【总体统计】")
    print(f"  总调用次数: {total}")
    print(f"  时间范围: {start} ~ {end}")
    print(f"  跨度: {span_hours:.1f} 小时")
    print(f"  平均每小时: {total/max(span_hours, 0.01):.1f} 次")
    
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
    
    # 简要说明
    print(f"\n【说明】")
    print("  - 本文件记录被删除的文案（bot 触发验证时删除、管理员限制时缓存的消息）")
    print("  - 格式: time, user_id, full_name, deleted_content")
    print("=" * 60)

if __name__ == "__main__":
    analyze()
