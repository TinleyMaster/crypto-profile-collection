#!/usr/bin/env python3
"""
批量重算解锁抛压评分（asset_unlock_pressure）。

P0-3：原 asset_unlock_pressure 仅 54 资产、且依赖单资产按需触发才回填，
覆盖度极低导致解锁抛压榜选不出标的。本脚本把覆盖范围扩展到
biz.asset_token_unlocks 中所有存在未过期解锁事件（is_upcoming=True 且日期 >= 今天）
的资产，逐个调用 compute_unlock_pressure(asset_id, force=True) 重算并写缓存。

用法：
    python recompute_unlock_pressure.py                 # 全量重算
    python recompute_unlock_pressure.py --limit 100     # 只重算前 100 个
    python recompute_unlock_pressure.py --no-force      # 命中 6h 缓存则跳过
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
# prod 结构: /app/scripts/bin/ → /app/（workbench 文件直接在 /app/ 下）
# 本地结构: .../scripts/bin/ → .../workbench/
_candidate = SCRIPT_DIR.parent.parent / "workbench"
WORKBENCH_DIR = _candidate if _candidate.exists() else SCRIPT_DIR.parent.parent
if str(WORKBENCH_DIR) not in sys.path:
    sys.path.insert(0, str(WORKBENCH_DIR))
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))


def main() -> int:
    parser = argparse.ArgumentParser(description="批量重算解锁抛压评分")
    parser.add_argument("--limit", type=int, default=0,
                        help="最多重算数量 (0=不限，全量)")
    parser.add_argument("--no-force", action="store_true",
                        help="命中 6h 缓存则跳过（默认强制重算）")
    args = parser.parse_args()

    from db_stats import recompute_unlock_pressure_batch  # noqa: E402

    result = recompute_unlock_pressure_batch(
        limit=args.limit,
        force=not args.no_force,
        log=print,
    )
    print("=" * 60)
    print(f"候选资产: {result['total']}")
    print(f"重算成功: {result['computed']}")
    print(f"重算失败: {result['failed']}")
    print(f"清理旧行: {result.get('stale_deleted', 0)}")
    if result["failed_ids"]:
        print(f"失败资产: {result['failed_ids']}")
    print("=" * 60)
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())