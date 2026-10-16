#!/usr/bin/env python3
"""刷新资产多维筛选物化视图 biz.mv_asset_screen（投研 SOP P1.9）。

视图定义见 migrations/fix_105_asset_screen_mv.sql（弹性×解锁四象限 + 弹性×资金流双确认）。
本脚本只负责「刷新」，不改定义；定义改动请改迁移文件后重新 apply。

用法
----
    python bin/refresh_asset_screen_mv.py                 # CONCURRENTLY 刷新（默认，不阻塞读）
    python bin/refresh_asset_screen_mv.py --no-concurrent  # 普通 REFRESH（唯一索引缺失/首建时用）
    python bin/refresh_asset_screen_mv.py --json

调度建议：排在三条 --batch 指标落库之后（见 workbench/scheduler.py 的 asset_screen_mv_refresh）。

实现注记：REFRESH MATERIALIZED VIEW CONCURRENTLY 不能在事务块内执行，故本脚本用
独立连接 + autocommit，不复用全局连接池（避免把 autocommit 状态毒化进池）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg  # noqa: E402
import psycopg.rows  # noqa: E402

from crypto_research.config import get_settings  # noqa: E402

MV = "biz.mv_asset_screen"

STATS_SQL = f"""
SELECT count(*)                                          AS rows_total,
       count(*) FILTER (WHERE quad_key = 'ideal')        AS n_ideal,
       count(*) FILTER (WHERE quad_key = 'elastic_dump') AS n_elastic_dump,
       count(*) FILTER (WHERE quad_key = 'defensive')    AS n_defensive,
       count(*) FILTER (WHERE quad_key = 'avoid')        AS n_avoid,
       count(*) FILTER (WHERE is_double_confirm)         AS n_double_confirm,
       count(*) FILTER (WHERE is_divergence)             AS n_divergence,
       max(latest_as_of)                                 AS latest_as_of
FROM {MV}
"""


def _stats(conn) -> dict:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(STATS_SQL)
        return dict(cur.fetchone())


def main() -> int:
    ap = argparse.ArgumentParser(description="刷新 biz.mv_asset_screen")
    ap.add_argument("--no-concurrent", action="store_true",
                    help="用普通 REFRESH（会阻塞读；仅在唯一索引缺失/首建时使用）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    mode = "REFRESH MATERIALIZED VIEW" + ("" if args.no_concurrent else " CONCURRENTLY")
    sql = f"{mode} {MV}"
    url = get_settings(require_database=True).database_url

    t0 = time.time()
    with psycopg.connect(url, autocommit=True, connect_timeout=30) as conn:
        try:
            before = _stats(conn)
        except Exception as e:
            print(f"[ERR] 视图 {MV} 不可读（可能尚未建，请先 apply fix_105）：{e}")
            return 1
        print(f"[刷新] {sql} ...")
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
        except Exception as e:
            print(f"[ERR] 刷新失败：{e}")
            if "CONCURRENTLY" in sql:
                print("      提示：唯一索引可能缺失，可先跑 --no-concurrent 重建")
            return 1
        after = _stats(conn)

    elapsed = time.time() - t0
    diff = {k: (after[k] or 0) - (before[k] or 0) for k in after if k != "latest_as_of"}
    out = {
        "mv": MV, "mode": mode, "elapsed_sec": round(elapsed, 2),
        "before": {k: (str(v) if k == "latest_as_of" else v) for k, v in before.items()},
        "after": {k: (str(v) if k == "latest_as_of" else v) for k, v in after.items()},
        "delta": diff,
    }
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"[OK] 刷新完成 用时 {elapsed:.1f}s")
        print(f"     行数 {before['rows_total']} -> {after['rows_total']}"
              f"（Δ{diff['rows_total']:+d}）  最新基准日 {after['latest_as_of']}")
        print(f"     四象限 ideal={after['n_ideal']} elastic_dump={after['n_elastic_dump']}"
              f" defensive={after['n_defensive']} avoid={after['n_avoid']}")
        print(f"     双确认 {after['n_double_confirm']}（Δ{diff['n_double_confirm']:+d}）"
              f"  背离 {after['n_divergence']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
