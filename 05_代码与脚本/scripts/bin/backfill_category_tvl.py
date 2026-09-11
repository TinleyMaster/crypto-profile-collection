#!/usr/bin/env python3
"""
从 src_dl.protocol_list 历史快照回填 biz.category_tvl_daily。

DeFi Llama /protocols 接口拉不动时，用已有的 protocol_list 历史快照
按天聚合 category TVL，回填到 category_tvl_daily 表。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection


def ensure_table(conn) -> None:
    """幂等建表（和 ingest_category_tvl.py 一致）"""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS biz.category_tvl_daily (
                snapshot_date      DATE           NOT NULL,
                category           VARCHAR(100)   NOT NULL,
                tvl_usd            NUMERIC(24,2)  NOT NULL DEFAULT 0,
                tvl_change_7d_pct  NUMERIC(12,4),
                protocol_count     INT            NOT NULL DEFAULT 0,
                source_code        VARCHAR(20)    NOT NULL DEFAULT 'defillama',
                fetched_at         TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
                updated_at         TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
                PRIMARY KEY (snapshot_date, category)
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_category_tvl_daily_date
            ON biz.category_tvl_daily(snapshot_date DESC);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_category_tvl_daily_cat
            ON biz.category_tvl_daily(category);
        """)
        conn.commit()


def backfill(dry_run: bool = False) -> dict:
    """从 src_dl.protocol_list 回填 category_tvl_daily。"""
    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        ensure_table(conn)

        with conn.cursor() as cur:
            # 1. 找出 protocol_list 有哪些天的快照
            cur.execute("""
                SELECT DISTINCT fetched_at::date as snap_date
                FROM src_dl.protocol_list
                ORDER BY snap_date
            """)
            dates = [r[0] for r in cur.fetchall()]
            print(f"[backfill] protocol_list 共 {len(dates)} 个快照日: {dates[0]} ~ {dates[-1]}")

            # 2. 按天 + category 聚合 TVL
            #    取每天的最新 fetched_at 时刻的快照（避免一天内多次抓取重复）
            cur.execute("""
                WITH daily_latest AS (
                    SELECT fetched_at::date as snap_date,
                           MAX(fetched_at) as latest_ts
                    FROM src_dl.protocol_list
                    GROUP BY 1
                )
                SELECT
                    dl.snap_date,
                    COALESCE(p.category, 'Unknown') as category,
                    SUM(p.tvl) as total_tvl,
                    COUNT(*) as proto_count
                FROM daily_latest dl
                JOIN src_dl.protocol_list p
                  ON p.fetched_at = dl.latest_ts
                WHERE p.tvl IS NOT NULL AND p.tvl > 0
                GROUP BY 1, 2
                ORDER BY 1, 3 DESC
            """)
            rows = cur.fetchall()
            print(f"[backfill] 聚合得到 {len(rows)} 条 (日期 x category) 记录")

            if dry_run:
                # 预览：打印最新一天的 top 10
                latest_date = dates[-1]
                top = [r for r in rows if r[0] == latest_date][:10]
                print(f"\n[backfill] DRY RUN: {latest_date} top 10 categories:")
                for r in top:
                    print(f"  {r[1]:30s}  TVL=${float(r[2])/1e9:>8.2f}B  protos={r[3]}")
                return {"dates": len(dates), "rows": len(rows), "dry_run": True}

            # 3. 计算 7d 变化率（和 7 天前比）
            #    先把数据加载到内存，计算后写入
            from collections import defaultdict
            cat_by_date: dict[str, dict[str, dict]] = defaultdict(dict)
            for snap_date, category, total_tvl, proto_count in rows:
                date_str = str(snap_date)
                cat_by_date[date_str][category] = {
                    "tvl": float(total_tvl),
                    "count": proto_count,
                }

            sorted_dates = sorted(cat_by_date.keys())
            insert_rows = []
            for i, d in enumerate(sorted_dates):
                # 找 7 天前的日期
                from datetime import datetime, timedelta
                d_dt = datetime.strptime(d, "%Y-%m-%d").date()
                d7 = str(d_dt - timedelta(days=7))
                prev_data = cat_by_date.get(d7, {})

                for cat, info in cat_by_date[d].items():
                    tvl_now = info["tvl"]
                    tvl_prev = prev_data.get(cat, {}).get("tvl")
                    chg_7d = None
                    if tvl_prev and tvl_prev > 0:
                        chg_7d = round((tvl_now - tvl_prev) / tvl_prev * 100, 4)
                    insert_rows.append((d, cat, tvl_now, chg_7d, info["count"]))

            # 4. 幂等 upsert
            sql = """
                INSERT INTO biz.category_tvl_daily
                    (snapshot_date, category, tvl_usd, tvl_change_7d_pct, protocol_count, source_code)
                VALUES (%s, %s, %s, %s, %s, 'defillama')
                ON CONFLICT (snapshot_date, category) DO UPDATE SET
                    tvl_usd = EXCLUDED.tvl_usd,
                    tvl_change_7d_pct = EXCLUDED.tvl_change_7d_pct,
                    protocol_count = EXCLUDED.protocol_count,
                    updated_at = NOW()
            """
            with conn.cursor() as cur2:
                cur2.executemany(sql, insert_rows)
            conn.commit()

            print(f"[backfill] 已写入 {len(insert_rows)} 条记录，覆盖 {len(sorted_dates)} 天")
            return {"dates": len(sorted_dates), "rows": len(insert_rows)}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="从 protocol_list 回填 category_tvl_daily")
    parser.add_argument("--dry-run", action="store_true", help="只预览不写入")
    args = parser.parse_args()
    backfill(dry_run=args.dry_run)
