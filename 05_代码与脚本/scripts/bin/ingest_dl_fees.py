#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""DeFiLlama 费用/收入入库（L5-B 收入估值层）。

分层：
    /overview/fees?dataType=daily{Fees|Revenue|HoldersRevenue}
        → src_dl.protocol_fees           （快照层，一协议×一口径一行）
        → biz.asset_revenue_snapshot     （业务层，一资产×一天一行，多子协议求和）

为什么需要这个（2026-10-27 实测）：
    L5「贵不贵」此前只有 18 行人工录入数据，等于没有收入分母。
    本脚本一次抓全市场：fees 3084 / revenue 2720 / holders_revenue 1250 个协议，
    落到本地资产池（市值≥10M 有排名 684 只）可覆盖 122 只有 revenue。
    ⚠️ 天花板 29%，补不齐——BTC/DOGE 这类没有「协议收入」口径，ARKM 这类
       DeFiLlama 不收录。NULL 必须解释为「无法判断」，不得解释为「便宜」。

三个口径不可互换（混用会把手续费当成收入，差一个量级）：
    fees            用户支付的总手续费
    revenue         协议留存的那部分
    holders_revenue 真正分配给代币持有者的那部分（判断价值捕获优先用它）

用法：
    python bin/ingest_dl_fees.py                      # 抓取今日并入库
    python bin/ingest_dl_fees.py --dry-run            # 只抓不写，打印覆盖统计
    python bin/ingest_dl_fees.py --date 2026-10-27    # 指定 as_of 回填

幂等：同一 (defillama_id, data_type) / (asset_id, as_of, source_code) 重复运行只更新。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(line_buffering=True)

UPSERT_SRC_SQL = """
INSERT INTO src_dl.protocol_fees
    (defillama_id, data_type, slug, name, category, total_24h, total_7d, total_30d, total_1y, fetched_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (defillama_id, data_type) DO UPDATE SET
    slug       = EXCLUDED.slug,
    name       = EXCLUDED.name,
    category   = EXCLUDED.category,
    total_24h  = EXCLUDED.total_24h,
    total_7d   = EXCLUDED.total_7d,
    total_30d  = EXCLUDED.total_30d,
    total_1y   = EXCLUDED.total_1y,
    fetched_at = EXCLUDED.fetched_at
"""

# 一个资产可能映射多个 DL 子协议（aave-v2 / aave-v3）→ 求和。
# 全 NULL 的资产不写行：写 0 会让读取方误判「收入为 0」。
AGGREGATE_SQL = """
INSERT INTO biz.asset_revenue_snapshot
    (asset_id, as_of, fees_24h, fees_30d, fees_1y,
     revenue_24h, revenue_30d, revenue_1y,
     holders_rev_30d, holders_rev_1y,
     protocol_count, source_code, fetched_at)
SELECT m.asset_id,
       %(as_of)s::date,
       sum(f.total_24h) FILTER (WHERE f.data_type = 'fees'),
       sum(f.total_30d) FILTER (WHERE f.data_type = 'fees'),
       sum(f.total_1y)  FILTER (WHERE f.data_type = 'fees'),
       sum(f.total_24h) FILTER (WHERE f.data_type = 'revenue'),
       sum(f.total_30d) FILTER (WHERE f.data_type = 'revenue'),
       sum(f.total_1y)  FILTER (WHERE f.data_type = 'revenue'),
       sum(f.total_30d) FILTER (WHERE f.data_type = 'holders_revenue'),
       sum(f.total_1y)  FILTER (WHERE f.data_type = 'holders_revenue'),
       count(DISTINCT f.defillama_id)::smallint,
       'dl',
       now()
FROM src_dl.protocol_fees f
JOIN core.asset_source_map m
  ON m.source_code = 'dl' AND m.source_asset_key = f.defillama_id
GROUP BY m.asset_id
HAVING count(f.total_30d) > 0 OR count(f.total_1y) > 0
ON CONFLICT (asset_id, as_of, source_code) DO UPDATE SET
    fees_24h        = EXCLUDED.fees_24h,
    fees_30d        = EXCLUDED.fees_30d,
    fees_1y         = EXCLUDED.fees_1y,
    revenue_24h     = EXCLUDED.revenue_24h,
    revenue_30d     = EXCLUDED.revenue_30d,
    revenue_1y      = EXCLUDED.revenue_1y,
    holders_rev_30d = EXCLUDED.holders_rev_30d,
    holders_rev_1y  = EXCLUDED.holders_rev_1y,
    protocol_count  = EXCLUDED.protocol_count,
    fetched_at      = now()
"""

COVERAGE_SQL = """
SELECT
    (SELECT count(*) FROM biz.asset_revenue_snapshot WHERE as_of = %(as_of)s)          AS rows_today,
    (SELECT count(*) FROM biz.asset_revenue_snapshot
      WHERE as_of = %(as_of)s AND revenue_30d > 0)                                     AS with_revenue,
    (SELECT count(*) FROM biz.asset_revenue_snapshot
      WHERE as_of = %(as_of)s AND fees_30d > 0 AND COALESCE(revenue_30d,0) = 0)        AS fee_no_revenue,
    (SELECT count(*) FROM biz.asset_revenue_snapshot
      WHERE as_of = %(as_of)s AND holders_rev_30d > 0)                                 AS with_holders
"""


def num(v):
    """DeFiLlama 可能返回 int / float / None / 字符串。统一成 float 或 None。"""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="DeFiLlama 费用/收入入库（L5-B）")
    ap.add_argument("--dry-run", action="store_true", help="只抓取并打印统计，不写库")
    ap.add_argument("--date", default=None, help="as_of 日期（YYYY-MM-DD），默认今日")
    args = ap.parse_args()

    from crypto_research.clients.defillama_client import DefiLlamaClient
    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    settings = get_settings(require_database=True)
    client = DefiLlamaClient(settings)
    fetched_at = datetime.now(timezone.utc)
    as_of = date.fromisoformat(args.date) if args.date else date.today()

    t0 = time.time()
    rows: list[tuple] = []
    per_type: dict[str, int] = {}
    for data_type in client.FEES_DATA_TYPES:
        payload = client.get_fees_overview(data_type)
        protocols = payload.get("protocols") or []
        kept = 0
        for p in protocols:
            did = p.get("defillamaId")
            if did is None:
                continue
            rows.append((
                str(did), data_type,
                p.get("slug"), p.get("name") or p.get("displayName"), p.get("category"),
                num(p.get("total24h")), num(p.get("total7d")),
                num(p.get("total30d")), num(p.get("total1y")),
                fetched_at,
            ))
            kept += 1
        per_type[data_type] = kept
        print(f"[ingest_dl_fees] {data_type:16s} 协议 {len(protocols):5d} → 入库候选 {kept:5d}")

    print(f"[ingest_dl_fees] 合计 {len(rows)} 行，耗时 {time.time()-t0:.1f}s")

    if args.dry_run:
        print("[ingest_dl_fees] DRY-RUN：不写库")
        print(json.dumps({"status": "dry-run", "as_of": str(as_of),
                          "fetched_rows": len(rows), "per_type": per_type},
                         ensure_ascii=False, indent=2))
        return 0

    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            t1 = time.time()
            cur.executemany(UPSERT_SRC_SQL, rows)
            print(f"[ingest_dl_fees] src_dl.protocol_fees 写入 {len(rows)} 行，耗时 {time.time()-t1:.1f}s")

            t2 = time.time()
            cur.execute(AGGREGATE_SQL, {"as_of": as_of})
            agg = cur.rowcount
            print(f"[ingest_dl_fees] biz.asset_revenue_snapshot 聚合 {agg} 个资产，耗时 {time.time()-t2:.1f}s")

            cur.execute(COVERAGE_SQL, {"as_of": as_of})
            cov = cur.fetchone()
        conn.commit()

    stats = {
        "status": "ok",
        "as_of": str(as_of),
        "source_rows": len(rows),
        "per_type": per_type,
        "assets_aggregated": agg,
        "rows_today": cov[0],
        "assets_with_revenue": cov[1],
        "assets_fee_but_no_revenue": cov[2],
        "assets_with_holders_revenue": cov[3],
        "total_time_s": round(time.time() - t0, 1),
    }
    print(json.dumps(stats, ensure_ascii=False, indent=2))

    if cov[1] == 0:
        print("[warn] 没有任何资产的 revenue 落库——检查 core.asset_source_map 的 dl 映射是否为空",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
