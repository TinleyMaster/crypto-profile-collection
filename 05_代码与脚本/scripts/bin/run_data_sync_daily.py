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

子任务隔离（2026-09-28 / 2026-09-29 加固）：
  任一子任务失败**不再中止后续子任务**——14 个子任务全部依次执行。
  原设计对「赛道分类刷新 / 资产同名去重 / 主表 supply 对齐」三个关键任务做
  「失败即终止全调度」（`break`），实测一次瞬时锁竞争（删 core.asset 触发 ~42 张
  子表 FK 级联撞锁，见 dedup_assets 韧性修复）就让其余 11 个子任务全部不跑，
  代价远大于收益。现改为：全部执行；第 4 参含义由 `continue_on_fail` 改为
  `critical`——**只影响整体退出码**（关键任务失败 → 退出码非 0，保持失败可见；
  非关键任务失败只记录、不拖累整体），**不再影响执行**。

  另外（2026-09-29）：**挂死（hang）也算一种失败**。原先 `subprocess.run` 无超时，
  一个网络无响应的子任务会永久阻塞、后续子任务全部不跑（本机/沙盒实测 sync_core_supply
  可挂 >30min）。现给每个子任务加**墙钟上限** `SUBTASK_TIMEOUT_SEC`（默认 1800s，
  远大于正常整条调度 3~5min），超时即终止该子任务并**继续下一个**（隔离），
  外层 task_manager 的 12h 硬超时仍作最终兜底。
"""

import subprocess
import sys
import os
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
BIN_DIR = SCRIPTS_DIR

# 单个子任务的墙钟上限（秒）：任一子任务挂死（网络无响应等）不会拖垮整条调度——
# 超时即终止该子任务并继续下一个（隔离）。默认 1800s（30min），远大于正常耗时
# （整条调度 3~5min）；可用环境变量 DATA_SYNC_SUBTASK_TIMEOUT_SEC 覆盖。
# 超时返回码沿用 coreutils `timeout` 约定 124。
SUBTASK_TIMEOUT_SEC = int(os.getenv("DATA_SYNC_SUBTASK_TIMEOUT_SEC", "1800"))
TIMEOUT_RC = 124

# 执行顺序：(任务名, 脚本名, 参数列表, critical)
#   critical=True  → 失败时整体退出码非 0（关键路径，需关注），但**不中止**其余子任务；
#   critical=False → 失败只记录，不影响整体退出码（多为依赖外部 API 的易抖任务）。
# 所有子任务无论成败都依次执行（隔离）。
TASKS = [
    # ─── 基础层 ───
    ("赛道分类刷新", "run_refresh_sectors.py", [], True),
    # 去重失败**不影响后续子任务**（2026-09-28）：删 core.asset 触发 ~42 张子表 FK
    # 级联/检查，与同窗 derivatives_batch 等并发写子表时可能撞锁超时；脚本已内建
    # 等锁放宽 + 退避重试。仍标为 critical：去重是基础层，长期失败需关注。
    ("资产同名去重", "dedup_assets.py", ["--apply"], True),
    ("官网 primary 裁决", "run_refresh_primary_website.py", [], False),

    # ─── 文档入口层 ───
    ("CMC 文档入口补充", "refresh_doc_source_entries_from_cmc_auto.py", [], False),
    ("CG 文档入口补充", "refresh_doc_source_entries_from_cg_auto.py", [], False),
    ("DL 文档入口补充", "refresh_doc_source_entries_from_dl_auto.py", [], False),
    # 注：双源文档入口补充（supplement_doc_entries_dual_auto.py）已从每日同步移除——
    # 其候选查询对 doc_source_entry 的反连接走全表扫描（1.8GB），导致每日同步卡死
    # （详见 复验结论_data_sync_daily执行情况_2026-08-27.md，task 4221708 卡在 Round 89/200）。

    # ─── 第三方数据层 ───
    ("第三方评级/审计回填", "phase_b2_third_party_auto.py", [], False),
    ("TGE/融资轮次采集", "phase_b2_third_party_raises_auto.py", [], False),

    # ─── 行情/市值层 ───
    ("主表 supply/市值对齐 CMC", "sync_core_supply_from_cmc.py", ["--sync"], True),
    ("CMC 分类聚合", "ingest_cmc_category.py", [], False),

    # ─── 信号层 ───
    ("每日 diff 变化榜", "daily_diff_generator.py", [], False),

    # ─── 链接分类矫正 ───
    ("链接分类重标 (GitHub/白皮书)", "backfill_classify_links.py",
     ["--relabel-entry-types", "--upgrade-whitepaper"], False),

    # ─── 解锁层 ───
    ("解锁事件 JSON→结构化同步", "sync_unlock_events_from_json.py", [], False),

    # ─── KOL 层 ───
    ("KOL 信号回测", "kol_backtest_batch.py", [], False),
]


def run_task(name: str, script: str, args: list[str]) -> int:
    """执行单个子任务，返回退出码。"""
    cmd = [sys.executable, "-u", str(BIN_DIR / script)] + args
    print(f"\n{'='*60}")
    print(f"▶ {name}")
    print(f"  CMD: {' '.join(cmd)}")
    print(f"{'='*60}")
    try:
        result = subprocess.run(cmd, cwd=str(SCRIPTS_DIR.parent), timeout=SUBTASK_TIMEOUT_SEC)
        rc = result.returncode
        if rc == 0:
            print(f"[OK] {name} 完成")
        else:
            print(f"[FAIL] {name} 失败 (exit={rc})")
        return rc
    except subprocess.TimeoutExpired:
        # 挂死 = 一种失败：终止该子任务，继续其余子任务（隔离），不抛出、不阻塞。
        print(f"[TIMEOUT] {name} 超时 {SUBTASK_TIMEOUT_SEC}s，已终止该子任务"
              f"（不影响其余子任务继续执行）")
        return TIMEOUT_RC
    except Exception as e:
        print(f"[FAIL] {name} 异常: {e}")
        return 1


def main() -> int:
    print("=" * 60)
    print("每日数据同步/矫正总调度")
    print(f"共 {len(TASKS)} 个子任务（相互隔离：任一失败/挂死都不中止其余；"
          f"单任务超时上限 {SUBTASK_TIMEOUT_SEC}s）")
    print("=" * 60)

    success = 0
    failed = 0
    failed_names = []
    critical_failed = []

    for name, script, args, critical in TASKS:
        rc = run_task(name, script, args)
        if rc == 0:
            success += 1
            continue
        failed += 1
        failed_names.append(name)
        if critical:
            critical_failed.append(name)
        # 不 break：子任务相互隔离，继续执行其余任务（2026-09-28）

    print(f"\n{'='*60}")
    print(f"全部完成：成功 {success} / 失败 {failed} / 共 {len(TASKS)}")
    if failed_names:
        print(f"失败任务：{', '.join(failed_names)}")
    if critical_failed:
        print(f"⚠️ 关键任务失败（不影响其余子任务已执行，但需关注）：{', '.join(critical_failed)}")
    print(f"{'='*60}")

    # 退出码：仅「关键任务失败」时非 0（保持关键路径失败可见）；非关键任务失败只记录、
    # 不拖累整体状态，避免单个易抖子任务（外部 API 限频等）把整条日同步长期钉成 failed。
    # 注意：全部任务失败时，其中的关键任务必然失败 ⇒ 仍返回 1（系统性故障信号）。
    return 1 if critical_failed else 0


if __name__ == "__main__":
    sys.exit(main())
