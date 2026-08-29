"""DefiLlama TVL 每日聚合：src_dl.protocol_list → biz.protocol_metric_daily。

在 ingest_dl_protocols.py 之后运行，将源层 TVL 快照聚合到业务层。
幂等写入：同一天同一资产重复运行会更新而非重复插入。

用法：
    python ingest_dl_tvl_daily.py              # 聚合今日 TVL
    python ingest_dl_tvl_daily.py --dry-run    # 预览，不写入
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(line_buffering=True)

SQL_AGGREGATE = """
    INSERT INTO biz.protocol_metric_daily (asset_id, metric_date, tvl, tvl_change_1d, tvl_change_7d, source_code)
    SELECT
        asset_id,
        CURRENT_DATE,
        MAX(tvl) AS tvl,
        MAX(tvl_change_1d) AS tvl_change_1d,
        MAX(tvl_change_7d) AS tvl_change_7d,
        'dl'
    FROM (
        SELECT
            m.asset_id,
            p.tvl,
            p.change_1d AS tvl_change_1d,
            p.change_7d AS tvl_change_7d
        FROM src_dl.protocol_list p
        JOIN core.asset_source_map m
            ON m.source_code = 'dl' AND m.source_asset_key = p.protocol_id
        WHERE p.tvl IS NOT NULL AND p.tvl > 0
    ) t
    GROUP BY asset_id
    ON CONFLICT (asset_id, metric_date, source_code) DO UPDATE SET
        tvl = EXCLUDED.tvl,
        tvl_change_1d = EXCLUDED.tvl_change_1d,
        tvl_change_7d = EXCLUDED.tvl_change_7d,
        updated_at = NOW()
"""

SQL_STATS = """
    SELECT COUNT(*) AS total_rows, COUNT(DISTINCT asset_id) AS assets,
           SUM(tvl) AS total_tvl
    FROM biz.protocol_metric_daily
    WHERE metric_date = CURRENT_DATE
"""


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="DefiLlama TVL 每日聚合")
    parser.add_argument("--dry-run", action="store_true", help="预览，不写入")
    args = parser.parse_args()

    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    settings = get_settings(require_database=True)

    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            # 累积式写入：IF NOT EXISTS 保留历史，不再 DROP 重建
            cur.execute("""
                CREATE TABLE IF NOT EXISTS biz.protocol_metric_daily (
                    asset_id      INTEGER NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,
                    metric_date   DATE NOT NULL DEFAULT CURRENT_DATE,
                    tvl           NUMERIC(20, 2),
                    tvl_change_1d NUMERIC,
                    tvl_change_7d NUMERIC,
                    source_code   TEXT NOT NULL DEFAULT 'dl',
                    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (asset_id, metric_date, source_code)
                )
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_protocol_metric_daily_date
                    ON biz.protocol_metric_daily (metric_date DESC)
            """)

            if args.dry_run:
                cur.execute("""
                    SELECT COUNT(DISTINCT asset_id) AS candidates, SUM(tvl) AS total_tvl
                    FROM (
                        SELECT m.asset_id, p.tvl
                        FROM src_dl.protocol_list p
                        JOIN core.asset_source_map m
                            ON m.source_code = 'dl' AND m.source_asset_key = p.protocol_id
                        WHERE p.tvl IS NOT NULL AND p.tvl > 0
                    ) t
                """)
                row = cur.fetchone()
                print(f"[DRY-RUN] 候选资产: {row[0]}, TVL 总计: ${float(row[1] or 0):,.0f}")
                return 0

            cur.execute(SQL_AGGREGATE)
            affected = cur.rowcount

            cur.execute(SQL_STATS)
            stats = cur.fetchone()

        conn.commit()

    print(json.dumps({
        "status": "ok",
        "upserted": affected,
        "total_rows": stats[0],
        "assets": stats[1],
        "total_tvl": float(stats[2] or 0),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
