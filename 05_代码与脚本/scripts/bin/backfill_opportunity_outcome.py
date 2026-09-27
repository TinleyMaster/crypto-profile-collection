#!/usr/bin/env python3
"""机会清单前向收益回填（biz.opportunity_snapshot）——W-13。

早报机会清单此前只存在于 payload JSON，无独立持久化，「W-03 改了排序后指导意义是否
真的提升」没有任何数据能回答。build_daily_brief 现已把每日清单落表，本脚本每天把已到期
的行按 T+1 / T+7 回填相对 `ref_price` 的收益，积累样本后即可按 `calibration_gate` 分组
比较收益，作为「改动是否有效」的最终判据：

    SELECT calibration_gate, avg(outcome_7d), count(*)
    FROM biz.opportunity_snapshot
    WHERE outcome_7d IS NOT NULL GROUP BY 1;

复用 db_stats 的前向收益框架（`_forward_return_pct`），不新建回测体系：
  · 只填「已到期 且 该期次为空 且 ref_price/asset_id 非空」的格，已填不重算（幂等）；
  · 到期价取 `market_date >= snapshot_date + N` 的首个可得收盘价（cmc 优先）；
  · ref_price 为空 或 无 asset_id → 该行跳过（无基准价算不出收益，不写假数）。

用法：
    python backfill_opportunity_outcome.py            # 增量：处理所有未回填 T+7 的到期行
    python backfill_opportunity_outcome.py --limit 500
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
# prod 结构: /app/scripts/bin/ → /app（workbench 文件直接在 /app/ 下）
# 本地结构: .../scripts/bin/ → .../workbench/
_candidate = SCRIPT_DIR.parent.parent / "workbench"
WORKBENCH_DIR = _candidate if _candidate.exists() else SCRIPT_DIR.parent.parent
if str(WORKBENCH_DIR) not in sys.path:
    sys.path.insert(0, str(WORKBENCH_DIR))
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

# 前向窗口（天 → 列名）
OUTCOME_HORIZONS: tuple[tuple[int, str], ...] = ((1, "outcome_1d"), (7, "outcome_7d"))


def backfill_opportunity_outcome(limit: int | None = None, log=print) -> dict:
    """对 biz.opportunity_snapshot 未回填的到期行回填 T+1 / T+7 前向收益（幂等）。"""
    import psycopg.rows

    from db_stats import _forward_return_pct, _to_float, get_db

    stats = {"scanned": 0, "filled_cells": 0, "rows_completed": 0, "skipped_no_ref": 0}
    with get_db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            sql = """
                SELECT snapshot_date, target, signal_type, asset_id, ref_price,
                       outcome_1d, outcome_7d,
                       ((snapshot_date + 1) <= (CURRENT_DATE AT TIME ZONE 'Asia/Shanghai')::date) AS due_1,
                       ((snapshot_date + 7) <= (CURRENT_DATE AT TIME ZONE 'Asia/Shanghai')::date) AS due_7
                FROM biz.opportunity_snapshot
                WHERE outcome_7d IS NULL
                ORDER BY snapshot_date ASC
            """
            if limit:
                sql += " LIMIT %s"
                cur.execute(sql, (limit,))
            else:
                cur.execute(sql)
            rows = cur.fetchall()
            stats["scanned"] = len(rows)

            for r in rows:
                ref = _to_float(r["ref_price"])
                if not ref or ref <= 0 or r["asset_id"] is None:
                    stats["skipped_no_ref"] += 1
                    continue
                upd = {}
                for days, col in OUTCOME_HORIZONS:
                    if not r[f"due_{days}"] or r[col] is not None:
                        continue
                    # 到期日（snapshot_date+days）当日或之后的首个可得收盘价
                    cur.execute(
                        """
                        SELECT price_usd FROM biz.asset_market_daily
                        WHERE asset_id = %s
                          AND source_code IN ('cmc', 'cmc_historical')
                          AND market_date >= (%s::date + %s)
                          AND price_usd IS NOT NULL AND price_usd > 0
                        ORDER BY market_date ASC,
                                 CASE source_code WHEN 'cmc' THEN 0 ELSE 1 END
                        LIMIT 1
                        """,
                        (r["asset_id"], r["snapshot_date"], days),
                    )
                    prow = cur.fetchone()
                    ret = _forward_return_pct(ref, prow["price_usd"] if prow else None)
                    if ret is not None:
                        upd[col] = ret
                if not upd:
                    continue
                sets = ", ".join(f"{c} = %s" for c in upd)
                cur.execute(
                    f"UPDATE biz.opportunity_snapshot SET {sets}, updated_at = NOW() "
                    f"WHERE snapshot_date = %s AND target = %s AND signal_type = %s",
                    (*upd.values(), r["snapshot_date"], r["target"], r["signal_type"]),
                )
                stats["filled_cells"] += len(upd)
                merged = {c: (upd.get(c) if c in upd else r[c]) for _d, c in OUTCOME_HORIZONS}
                if all(v is not None for v in merged.values()):
                    stats["rows_completed"] += 1
        conn.commit()
    log(
        f"机会清单回填：扫描 {stats['scanned']} 行，填入 {stats['filled_cells']} 格，"
        f"完成 {stats['rows_completed']} 行，无基准价/无 asset_id 跳过 {stats['skipped_no_ref']} 行"
    )
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="机会清单 T+1/T+7 前向收益回填（W-13）")
    ap.add_argument("--limit", type=int, default=None, help="最多处理 N 行（默认全部）")
    args = ap.parse_args()

    stats = backfill_opportunity_outcome(limit=args.limit)
    print("=" * 60)
    print(f"扫描未回填行:     {stats['scanned']}")
    print(f"填入收益格数:     {stats['filled_cells']}")
    print(f"两期完成行数:     {stats['rows_completed']}")
    print(f"无基准价跳过行数: {stats['skipped_no_ref']}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())