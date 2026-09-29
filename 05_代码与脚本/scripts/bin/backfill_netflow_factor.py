"""资产级交易所净流因子 — 小时聚合回填（读侧，幂等，DB 端单语句）。

从 biz.onchain_transfer_log 聚合出「资产 × 小时」的交易所净流因子，
写入 biz.onchain_netflow_hourly（DDL: migrations/fix_080_onchain_netflow_hourly.sql）。

语义（对齐 CoinGlass，详见 DDL 头注释）：
    inflow  = 转入交易所（充值，潜在抛压）
    outflow = 提币离场（吸筹/自托管）
    netflow = inflow - outflow（正=抛压偏多，负=吸筹偏多）
归因口径 = 读侧 union 两张地址表（与 workbench/onchain_alert.py 完全一致）；
同所家族互转、is_suspect、0xtest% 全部剔除。

实现要点：
    写入 = 单条 INSERT INTO ... SELECT ... ON CONFLICT，聚合与 upsert 全程 DB 端完成，
    数据不离开数据库（跨公网逐行 upsert 的 round-trip 版本已废弃，31 天窗口 1h+ → 秒级）。
    桶按 UTC 截断（SET TIME ZONE 'UTC'），不含进行中的当前小时（边界稳定可重放）。
    幂等：ON CONFLICT DO UPDATE，窗口可任意重叠重跑。

用法：
    # 预览（不写库，打印 top5 聚合行）
    python backfill_netflow_factor.py --hours 48 --dry-run

    # 实际回填（调度默认每小时跑，回看 6h）
    python backfill_netflow_factor.py --hours 6

    # 首次播种 30 天历史
    python backfill_netflow_factor.py --hours 744
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

# 与 workbench/onchain_alert.py 完全一致的归因 CTE + 聚合体
# 家族判定：先按冒号拆（Binance: Hot Wallet 20 → Binance），再按空格拆（Binance 14 → Binance）
_AGG_BODY = """
WITH exch AS (
    SELECT address, chain, exchange_name FROM biz.onchain_exchange_wallet WHERE confidence = 'high'
    UNION
    SELECT address, chain, label_name FROM biz.onchain_address_label
    WHERE label_type = 'exchange' AND confidence IN ('high', 'medium')
),
tl AS (
    SELECT asset_id, block_timestamp, value_usd, from_address, to_address, chain
    FROM biz.onchain_transfer_log
    WHERE block_timestamp >= NOW() - (%s * INTERVAL '1 hour')
      AND block_timestamp <  date_trunc('hour', NOW())
      AND asset_id IS NOT NULL
      AND value_usd IS NOT NULL AND value_usd > 0
      AND (is_suspect IS NOT TRUE)
      AND tx_hash NOT LIKE '0xtest%%'
),
j AS (
    SELECT tl.asset_id,
           date_trunc('hour', tl.block_timestamp) AS bucket_hour,
           f.exchange_name AS f_ex,
           t.exchange_name AS t_ex,
           tl.value_usd
    FROM tl
    LEFT JOIN exch f ON f.address = tl.from_address AND f.chain = tl.chain
    LEFT JOIN exch t ON t.address = tl.to_address   AND t.chain = tl.chain
),
clean AS (
    SELECT * FROM j
    WHERE f_ex IS NULL OR t_ex IS NULL
       OR split_part(split_part(f_ex, ':', 1), ' ', 1)
       <> split_part(split_part(t_ex, ':', 1), ' ', 1)
),
agg AS (
    SELECT bucket_hour,
           asset_id,
           COALESCE(sum(value_usd) FILTER (WHERE t_ex IS NOT NULL), 0) AS inflow_usd,
           COALESCE(count(*)     FILTER (WHERE t_ex IS NOT NULL), 0)   AS inflow_cnt,
           COALESCE(sum(value_usd) FILTER (WHERE f_ex IS NOT NULL), 0) AS outflow_usd,
           COALESCE(count(*)     FILTER (WHERE f_ex IS NOT NULL), 0)   AS outflow_cnt
    FROM clean
    GROUP BY 1, 2
    HAVING count(*) FILTER (WHERE t_ex IS NOT NULL OR f_ex IS NOT NULL) > 0
)
"""

SELECT_SQL = _AGG_BODY + "SELECT * FROM agg ORDER BY bucket_hour DESC, asset_id"

INSERT_SQL = _AGG_BODY + """
INSERT INTO biz.onchain_netflow_hourly
    (bucket_hour, asset_id, inflow_usd, outflow_usd, netflow_usd, inflow_cnt, outflow_cnt, updated_at)
SELECT bucket_hour, asset_id,
       inflow_usd, outflow_usd, inflow_usd - outflow_usd,
       inflow_cnt, outflow_cnt, NOW()
FROM agg
ON CONFLICT (bucket_hour, asset_id) DO UPDATE SET
    inflow_usd  = EXCLUDED.inflow_usd,
    outflow_usd = EXCLUDED.outflow_usd,
    netflow_usd = EXCLUDED.netflow_usd,
    inflow_cnt  = EXCLUDED.inflow_cnt,
    outflow_cnt = EXCLUDED.outflow_cnt,
    updated_at  = NOW()
"""


def _symbol_of(conn, asset_id: int) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT canonical_symbol FROM core.asset WHERE asset_id = %s", (asset_id,))
        row = cur.fetchone()
        return str(row[0]) if row else f"#{asset_id}"


def main() -> None:
    parser = argparse.ArgumentParser(description="资产净流因子小时聚合回填（幂等，DB 端单语句）")
    parser.add_argument("--hours", type=int, default=6,
                        help="回看窗口（小时），默认 6；首播 30 天用 744")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印聚合结果 top5，不写库")
    parser.add_argument("--db-url", type=str, default=None,
                        help="数据库连接串（默认从 settings 读）")
    args = parser.parse_args()

    if args.db_url:
        conn_cm = get_connection(args.db_url)
    else:
        settings = get_settings(require_database=True)
        conn_cm = get_connection(settings.database_url)

    with conn_cm as conn:
        # 桶边界必须按 UTC 截断，否则 date_trunc 跟随会话时区会漂移
        with conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")

        with conn.cursor() as cur:
            cur.execute(SELECT_SQL, (args.hours,))
            rows = cur.fetchall()

        total_in = sum(float(r[2]) for r in rows)
        total_out = sum(float(r[4]) for r in rows)
        print(f"窗口 {args.hours}h（不含进行中的当前小时）: "
              f"聚合行={len(rows)}  总流入=${total_in:,.0f}  总流出=${total_out:,.0f}")

        if args.dry_run:
            for r in rows[:5]:
                net = float(r[2]) - float(r[4])
                print(f"  {r[0]:%m-%d %H:%M} {_symbol_of(conn, r[1]):<10} "
                      f"in=${float(r[2]):>14,.0f} out=${float(r[4]):>14,.0f} "
                      f"net=${net:>14,.0f} (i{r[3]}/o{r[5]})")
            print("[dry-run] 未写库")
            return

        with conn.cursor() as cur:
            cur.execute(INSERT_SQL, (args.hours,))
            print(f"✅ DB 端 upsert 完成，影响行={cur.rowcount} → biz.onchain_netflow_hourly")


if __name__ == "__main__":
    main()
