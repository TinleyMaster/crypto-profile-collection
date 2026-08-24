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
        m.asset_id,
        CURRENT_DATE,
        p.tvl,
        p.change_1d,
        p.change_7d,
        'dl'
    FROM src_dl.protocol_list p
    JOIN core.asset_source_map m
        ON m.source_code = 'dl' AND m.source_asset_key = p.protocol_id
    WHERE p.tvl IS NOT NULL AND p.tvl > 0
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
            # 确保表存在（若已有旧表，用 ADD COLUMN IF NOT EXISTS 补齐列）
            cur.execute("""
                CREATE TABLE IF NOT EXISTS biz.protocol_metric_daily (
                    asset_id      INTEGER NOT NULL,
                    metric_date   DATE NOT NULL DEFAULT CURRENT_DATE,
                    source_code   TEXT NOT NULL DEFAULT 'dl',
                    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (asset_id, metric_date, source_code)
                )
            """)

            cur.execute("""
                ALTER TABLE biz.protocol_metric_daily
                ADD COLUMN IF NOT EXISTS tvl NUMERIC(20, 2),
                ADD COLUMN IF NOT EXISTS tvl_change_1d NUMERIC,
                ADD COLUMN IF NOT EXISTS tvl_change_7d NUMERIC,
                ADD COLUMN IF NOT EXISTS fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            """)

            # 确保外键（已有数据可能冲突，这里仅添加约束；若失败则打印警告继续）
            try:
                cur.execute("""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_constraint
                            WHERE conname = 'protocol_metric_daily_asset_id_fkey'
                        ) THEN
                            ALTER TABLE biz.protocol_metric_daily
                            ADD CONSTRAINT protocol_metric_daily_asset_id_fkey
                            FOREIGN KEY (asset_id) REFERENCES core.asset(asset_id) ON DELETE CASCADE;
                        END IF;
                    END $$;
                """)
            except Exception as e:
                print(f"[WARN] 添加外键约束失败（可能已有脏数据）: {e}", file=sys.stderr)

            if args.dry_run:
                cur.execute("""
                    SELECT COUNT(*) AS candidates, SUM(p.tvl) AS total_tvl
                    FROM src_dl.protocol_list p
                    JOIN core.asset_source_map m
                        ON m.source_code = 'dl' AND m.source_asset_key = p.protocol_id
                    WHERE p.tvl IS NOT NULL AND p.tvl > 0
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
