#!/usr/bin/env python3
"""上线前验证脚本"""
import sys

def main():
    errors = []
    # 1. 导入
    try:
        import bot
    except Exception as e:
        errors.append(f"导入失败: {e}")
        for e in errors:
            print(f"[FAIL] {e}")
        return 1

    # 2. 关键函数存在
    funcs = [
        "group_message_handler", "cmd_help", "cmd_start", "cmd_addcp",
        "add_combined_pair", "_increment_combined_pair_count", "set_combined_pair_count",
        "get_cp_restrict_enabled", "set_cp_restrict_enabled", "_restrict_cp_user",
        "_log_deleted_content", "_process_add_cp_followup", "_combined_pair_exists",
    ]
    for f in funcs:
        if not hasattr(bot, f):
            errors.append(f"缺少函数: {f}")
    if errors:
        for e in errors:
            print(f"[FAIL] {e}")
        return 1
    print("[OK] 关键函数存在")

    # 3. 发言流程顺序
    import inspect
    src = inspect.getsource(bot.group_message_handler)
    if "verified_pass" not in src or "verified_users" not in src:
        errors.append("白名单逻辑异常")
    if "check_combined_pairs" not in src:
        errors.append("组合关键词逻辑缺失")
    idx_vp = src.find("verified_pass")
    idx_cp = src.find("check_combined_pairs")
    if idx_vp >= idx_cp:
        errors.append("发言顺序错误: 白名单应在组合关键词之前")
    idx_ft = max(src.find("check_facetext"), src.find("check_facename"))
    if idx_cp >= idx_ft:
        errors.append("发言顺序错误: 组合关键词应在 facetext/facename 之前")
    if errors:
        for e in errors:
            print(f"[FAIL] {e}")
        return 1
    print("[OK] 发言流程顺序: 白名单 -> 组合关键词 -> facetext/facename")

    # 4. addcp 3 参数支持
    src_cp = inspect.getsource(bot._process_add_cp_followup)
    if 'split(",", 2)' not in src_cp:
        errors.append("addcp 未使用 split(\",\", 2)")
    if "set_combined_pair_count" not in src_cp:
        errors.append("addcp 未调用 set_combined_pair_count")
    if errors:
        for e in errors:
            print(f"[FAIL] {e}")
        return 1
    print("[OK] addcp 支持 3 参数格式")

    # 5. help 文案
    help_src = inspect.getsource(bot.cmd_help)
    if "自助验证" not in help_src:
        errors.append("help 未包含自助验证提示")
    if "霜刃你好" in help_src:
        errors.append("help 仍含旧文案「霜刃你好」")
    if errors:
        for e in errors:
            print(f"[FAIL] {e}")
        return 1
    print("[OK] help 文案已更新")

    print("")
    print("=== 上线前验证通过 ===")
    return 0

if __name__ == "__main__":
    sys.exit(main())
