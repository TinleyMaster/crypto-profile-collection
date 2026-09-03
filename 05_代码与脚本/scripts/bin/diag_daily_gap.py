"""日价缺口诊断脚本（RT-BACKTEST-D1-001 改动 1）

诊断 biz.asset_market_daily 在指定日期的缺口根因，检查：
1. src_cmc.cmc_asset_quote_snapshot 是否有该日快照
2. biz.asset_market_daily 是否有该日记录
3. sys.ingest_run / sys.task 中 cmc 相关任务的运行状态
4. 快照时间分布（是否存在跨日偏移）

用法：
    python diag_daily_gap.py                          # 默认检查最近 7 天
    python diag_daily_gap.py --dates 2026-08-30 2026-08-31  # 指定日期
    python diag_daily_gap.py --days 14               # 检查最近 14 天
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection


def diagnose_gap(target_dates: list[date]) -> dict:
    """诊断指定日期的日价缺口根因。"""
    settings = get_settings(require_database=True)
    results = {}

    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            # 1. 检查 asset_market_daily 缺口
            placeholders = ",".join(["%s"] * len(target_dates))
            cur.execute(f"""
                SELECT market_date, COUNT(DISTINCT asset_id) AS assets, COUNT(*) AS rows
                FROM biz.asset_market_daily
                WHERE market_date IN ({placeholders})
                GROUP BY market_date
                ORDER BY market_date
            """, target_dates)
            daily_stats = {row[0]: {"assets": row[1], "rows": row[2]} for row in cur.fetchall()}

            # 2. 检查 cmc_asset_quote_snapshot 快照覆盖
            cur.execute(f"""
                SELECT DATE(quote_time AT TIME ZONE 'UTC') AS snap_date,
                       COUNT(DISTINCT cmc_id) AS cmc_assets,
                       COUNT(*) AS total_snapshots,
                       MIN(quote_time) AS earliest,
                       MAX(quote_time) AS latest
                FROM src_cmc.cmc_asset_quote_snapshot
                WHERE DATE(quote_time AT TIME ZONE 'UTC') IN ({placeholders})
                   OR DATE(quote_time AT TIME ZONE 'UTC') IN ({placeholders})
                GROUP BY snap_date
                ORDER BY snap_date
            """, target_dates + target_dates)
            snapshot_stats = {}
            for row in cur.fetchall():
                snap_date = row[0]
                if snap_date not in snapshot_stats:
                    snapshot_stats[snap_date] = {
                        "cmc_assets": row[1],
                        "total_snapshots": row[2],
                        "earliest": str(row[3]),
                        "latest": str(row[4]),
                    }

            # 3. 检查 asset_source_map 中 cmc 映射数
            cur.execute("""
                SELECT COUNT(*) FROM core.asset_source_map WHERE source_code = 'cmc'
            """)
            total_cmc_mapped = cur.fetchone()[0]

            # 4. 检查最近 14 天的 cmc 任务运行记录
            cur.execute("""
                SELECT name, status, started_at, ended_at, error
                FROM sys.task
                WHERE name LIKE '%cmc%'
                  AND started_at >= NOW() - INTERVAL '30 days'
                ORDER BY started_at DESC
                LIMIT 50
            """)
            task_runs = []
            for row in cur.fetchall():
                task_runs.append({
                    "name": row[0],
                    "status": row[1],
                    "started_at": str(row[2]) if row[2] else None,
                    "ended_at": str(row[3]) if row[3] else None,
                    "error": row[4][:200] if row[4] else None,
                })

            # 5. 检查 ingest_run 中 cmc_listings_latest 的记录
            cur.execute("""
                SELECT run_id, platform_code, workflow_name, status,
                       total_items, success_items, fail_items, error_message, 
                       started_at, finished_at
                FROM sys.ingest_run
                WHERE platform_code = 'cmc'
                  AND workflow_name = 'WF_CMC_QUOTE_SNAPSHOT'
                  AND started_at >= NOW() - INTERVAL '30 days'
                ORDER BY started_at DESC
                LIMIT 30
            """)
            ingest_runs = []
            for row in cur.fetchall():
                ingest_runs.append({
                    "run_id": row[0],
                    "platform": row[1],
                    "status": row[3],
                    "total_items": row[4],
                    "success_items": row[5],
                    "fail_items": row[6],
                    "error": row[7][:200] if row[7] else None,
                    "started_at": str(row[8]) if row[8] else None,
                    "finished_at": str(row[9]) if row[9] else None,
                })

            # 6. 检查 ETL 相关任务
            cur.execute("""
                SELECT name, status, started_at, ended_at, error
                FROM sys.task
                WHERE name LIKE '%etl%market%daily%' OR name LIKE '%market%daily%etl%'
                  AND started_at >= NOW() - INTERVAL '30 days'
                ORDER BY started_at DESC
                LIMIT 20
            """)
            etl_runs = []
            for row in cur.fetchall():
                etl_runs.append({
                    "name": row[0],
                    "status": row[1],
                    "started_at": str(row[2]) if row[2] else None,
                    "ended_at": str(row[3]) if row[3] else None,
                    "error": row[4][:200] if row[4] else None,
                })

    # 汇总诊断结果
    gap_analysis = {}
    for d in target_dates:
        daily = daily_stats.get(d, {"assets": 0, "rows": 0})
        snap = snapshot_stats.get(d, {"cmc_assets": 0, "total_snapshots": 0, "earliest": None, "latest": None})

        has_gap = daily["assets"] == 0
        has_snapshots = snap["total_snapshots"] > 0

        if has_gap and has_snapshots:
            root_cause = "ETL未执行或失败（有快照但无日价）"
        elif has_gap and not has_snapshots:
            root_cause = "快照采集失败或缺失（无快照也无日价）"
        elif not has_gap and daily["assets"] < 7000:
            root_cause = f"部分缺失（仅{daily['assets']}资产，预期~8000）"
        else:
            root_cause = "正常"

        gap_analysis[d.isoformat()] = {
            "daily_assets": daily["assets"],
            "daily_rows": daily["rows"],
            "snapshot_assets": snap["cmc_assets"],
            "snapshot_count": snap["total_snapshots"],
            "snapshot_earliest": snap["earliest"],
            "snapshot_latest": snap["latest"],
            "total_cmc_mapped": total_cmc_mapped,
            "root_cause_hypothesis": root_cause,
            "has_gap": has_gap,
        }

    return {
        "target_dates": [d.isoformat() for d in target_dates],
        "gap_analysis": gap_analysis,
        "task_runs_sample": task_runs[:20],
        "ingest_runs_sample": ingest_runs[:15],
        "etl_runs_sample": etl_runs[:10],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="日价缺口诊断")
    parser.add_argument("--dates", nargs="+", help="指定检查日期（YYYY-MM-DD）")
    parser.add_argument("--days", type=int, default=7, help="检查最近 N 天（默认7）")
    args = parser.parse_args()

    if args.dates:
        target_dates = [date.fromisoformat(d) for d in args.dates]
    else:
        today = date.today()
        target_dates = [today - timedelta(days=i) for i in range(args.days, 0, -1)]

    print(f"[DIAG] 检查日期: {[d.isoformat() for d in target_dates]}")
    result = diagnose_gap(target_dates)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
