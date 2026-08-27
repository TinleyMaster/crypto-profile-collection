#!/usr/bin/env python3
"""
每日数据同步/矫正总调度
按依赖顺序串起所有"同步/对齐/去重/兜底"类任务，避免散点调度互相打架。

执行顺序（按依赖关系排列）：
  1. 赛道分类刷新（sector 是很多下游的基础）
  2. 资产同名去重（清理脏数据，避免下游按 asset_id 操作时命中重复）
  3. 官网 primary 裁决（文档入口规范化）
  4. CMC/CG/DL 文档入口补充（刷新各源文档链接）
  5. 双源补充（DexScreener + Binance）
  6. 第三方数据回填（评级/审计/融资/黑客事件）
  7. 主表 supply/市值对齐 CMC（行情数据对齐）
  8. 每日 diff 变化榜（基于最新行情生成信号）
  9. GitHub 链接重标 + 白皮书升级（链接分类矫正）
  10. 解锁事件 JSON→结构化同步
  11. KOL 信号回测
"""

import subprocess
import sys
import os
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
BIN_DIR = SCRIPTS_DIR

# 执行顺序：(任务名, 脚本名, 参数列表, 失败是否继续)
# 关键路径任务失败则终止；非关键任务失败继续往下走
TASKS = [
    # ─── 基础层 ───
    ("赛道分类刷新", "run_refresh_sectors.py", [], False),
    ("资产同名去重", "dedup_assets.py", ["--apply"], False),
    ("官网 primary 裁决", "run_refresh_primary_website.py", [], True),

    # ─── 文档入口层 ───
    ("CMC 文档入口补充", "refresh_doc_source_entries_from_cmc_auto.py", [], True),
    ("CG 文档入口补充", "refresh_doc_source_entries_from_cg_auto.py", [], True),
    ("DL 文档入口补充", "refresh_doc_source_entries_from_dl_auto.py", [], True),
    # 注：双源文档入口补充（supplement_doc_entries_dual_auto.py）已从每日同步移除——
    # 其候选查询对 doc_source_entry 的反连接走全表扫描（1.8GB），导致每日同步卡死
    # （详见 复验结论_data_sync_daily执行情况_2026-08-27.md，task 4221708 卡在 Round 89/200）。

    # ─── 第三方数据层 ───
    ("第三方评级/审计回填", "phase_b2_third_party_auto.py", [], True),
    ("TGE/融资轮次采集", "phase_b2_third_party_raises_auto.py", [], True),

    # ─── 行情/市值层 ───
    ("主表 supply/市值对齐 CMC", "sync_core_supply_from_cmc.py", ["--sync"], False),
    ("CMC 分类聚合", "ingest_cmc_category.py", [], True),

    # ─── 信号层 ───
    ("每日 diff 变化榜", "daily_diff_generator.py", [], True),

    # ─── 链接分类矫正 ───
    ("链接分类重标 (GitHub/白皮书)", "backfill_classify_links.py",
     ["--relabel-entry-types", "--upgrade-whitepaper"], True),

    # ─── 解锁层 ───
    ("解锁事件 JSON→结构化同步", "sync_unlock_events_from_json.py", [], True),

    # ─── KOL 层 ───
    ("KOL 信号回测", "kol_backtest_batch.py", [], True),
]


def run_task(name: str, script: str, args: list[str]) -> int:
    """执行单个子任务，返回退出码。"""
    cmd = [sys.executable, "-u", str(BIN_DIR / script)] + args
    print(f"\n{'='*60}")
    print(f"▶ {name}")
    print(f"  CMD: {' '.join(cmd)}")
    print(f"{'='*60}")
    try:
        result = subprocess.run(cmd, cwd=str(SCRIPTS_DIR.parent))
        rc = result.returncode
        if rc == 0:
            print(f"✅ {name} 完成")
        else:
            print(f"❌ {name} 失败 (exit={rc})")
        return rc
    except Exception as e:
        print(f"❌ {name} 异常: {e}")
        return 1


def main() -> int:
    print("=" * 60)
    print("每日数据同步/矫正总调度")
    print(f"共 {len(TASKS)} 个子任务")
    print("=" * 60)

    success = 0
    failed = 0
    failed_names = []

    for name, script, args, continue_on_fail in TASKS:
        rc = run_task(name, script, args)
        if rc == 0:
            success += 1
        else:
            failed += 1
            failed_names.append(name)
            if not continue_on_fail:
                print(f"\n⛔ 关键任务 [{name}] 失败，终止后续任务")
                break

    print(f"\n{'='*60}")
    print(f"全部完成：成功 {success} / 失败 {failed} / 共 {len(TASKS)}")
    if failed_names:
        print(f"失败任务：{', '.join(failed_names)}")
    print(f"{'='*60}")

    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
