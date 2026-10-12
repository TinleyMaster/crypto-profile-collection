#!/usr/bin/env python3
"""定向回填每日变化榜「新增 6 类榜单」的历史数据（不动旧榜单）。

背景（2026-10-12）：whale_mover / exchange_flow_24h / derivatives_oi /
funding_rate_extreme / liquidation_24h / long_short_ratio 六个榜单随生成器新增，
但只对「新增当天」生效。为让「候选→交叉验证」组合筛选器（research_diff_combo_screen.py）
有历史样本，需要对既有 diff_date 逐日回填这 6 类。

⚠️ 刻意**不**用 daily_diff_generator.py --all：
  --all 会整日删除后重插「全部类别」，volume_surge_24h 于 2026-09-08 由 top-20 扩到
  top-40，重跑会把 09-08 之前的日期也从 20 行改成 40 行，破坏既有历史与连板口径。
  本脚本只回填新 6 类，旧榜单一行不碰。

语义（与生成器一致，按 diff_date 快照）：
  - whale_mover         取 snapshot_date <= diff_date 的最近持仓快照
  - exchange_flow_24h   按 bucket_hour::date = diff_date 聚合当日充/提币
  - derivatives_oi / funding_rate_extreme / liquidation_24h  取 ts::date <= diff_date 最新
  - long_short_ratio    取 ts::date <= diff_date 最新

用法：
    python backfill_diff_new_categories.py               # 回填所有既有 diff_date
    python backfill_diff_new_categories.py --since 2026-10-01
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

# 复用生成器的 SQL 常量（模块有 __main__ 守卫，可安全导入）
from daily_diff_generator import (  # noqa: E402
    WHALE_MOVER_SQL,
    EXCHANGE_FLOW_SQL,
    DERIVATIVES_OI_SQL,
    FUNDING_EXTREME_SQL,
    LIQUIDATION_24H_SQL,
    LONG_SHORT_RATIO_SQL,
)

# 只回填这 6 类（新榜单）
NEW_CATEGORIES = (
    "whale_mover",
    "exchange_flow_24h",
    "derivatives_oi",
    "funding_rate_extreme",
    "liquidation_24h",
    "long_short_ratio",
)

# category → (SQL, 参数个数)
TASKS = [
    ("whale_mover", WHALE_MOVER_SQL, 2),
    ("exchange_flow_24h", EXCHANGE_FLOW_SQL, 2),
    ("derivatives_oi", DERIVATIVES_OI_SQL, 2),
    ("funding_rate_extreme", FUNDING_EXTREME_SQL, 3),
    ("liquidation_24h", LIQUIDATION_24H_SQL, 2),
    ("long_short_ratio", LONG_SHORT_RATIO_SQL, 3),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="定向回填新增 6 类榜单历史")
    ap.add_argument("--since", type=str, default=None, help="只回填 >= 该日期的 diff_date")
    args = ap.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT diff_date FROM biz.daily_diff_summary ORDER BY diff_date")
            dates = [r[0] for r in cur.fetchall()]
            if args.since:
                dates = [d for d in dates if str(d) >= args.since]
            if not dates:
                print("[backfill-diff] 无日期可回填")
                return 0
            print(f"[backfill-diff] 回填 {len(dates)} 天 × {len(TASKS)} 类新榜单")

            total = 0
            for d in dates:
                ds = str(d)
                # 幂等：先删当日该 6 类旧行再插入
                cur.execute(
                    "DELETE FROM biz.daily_diff_summary WHERE diff_date = %s AND category = ANY(%s)",
                    (ds, list(NEW_CATEGORIES)),
                )
                day = 0
                for cat, sql, nparam in TASKS:
                    cur.execute(sql, (ds,) * nparam)
                    day += cur.rowcount
                total += day
                print(f"  {ds}: +{day} 行")
            conn.commit()
            print(f"[backfill-diff] 完成，合计新增 {total} 行（新 6 类历史）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
