"""资产级交易所净流因子 — 小时聚合回填（读侧，幂等，DB 端单语句）。

从 biz.onchain_transfer_log 聚合出「资产 × 小时」的交易所净流因子，
写入 biz.onchain_netflow_hourly（DDL: migrations/fix_080_onchain_netflow_hourly.sql）。

语义（对齐 CoinGlass，详见 DDL 头注释）：
    inflow  = 转入交易所（充值，潜在抛压）
    outflow = 提币离场（吸筹/自托管）
    netflow = inflow - outflow（正=抛压偏多，负=吸筹偏多）
归因口径 = 读侧 union 两张地址表（与 workbench/onchain_alert.py 完全一致）；
同所家族互转、is_suspect、0xtest% 全部剔除。

时间与窗口：
    两种模式互斥，--since/--until 优先于 --hours：
      --hours N        相对窗口： [NOW()-N小时, 当前UTC整点)      —— 调度增量用（默认 6）
      --since T        绝对窗口： [T, --until 或 当前UTC整点)      —— 历史回填用
    上限仍排除「进行中的当前小时」，保证桶边界稳定、可任意重放。

迟到写入与回看窗（2026-10-01 修复）：
    biz.onchain_transfer_log 是「迟到写入」常态源——实测 14d 内 15% 的行在区块时间
    6h 后才入库（延迟分布：6~24h 11.8% / 24~72h 2.4% / >72h 2.1%）。回看窗若只有
    6h，迟到行就永远落在后续任何一次窗口之外，留下永久低覆盖小时（修复前实测
    336 小时里 202 个覆盖 <90%，整体仅 76.8% 成交量）。故调度改为每小时 --hours 168
    ＋每日 --hours 720 深修复。聚合幂等，放宽窗口只是多算几行、不会重复计数。

实现要点：
    写入 = 单条 INSERT INTO ... SELECT ... ON CONFLICT，聚合与 upsert 全程 DB 端完成，
    数据不离开数据库（跨公网逐行 upsert 的 round-trip 版本已废弃，31 天窗口 1h+ → 秒级）。
    桶按 UTC 截断（SET TIME ZONE 'UTC'），不含进行中的当前小时（边界稳定可重放）。
    幂等：ON CONFLICT DO UPDATE，窗口可任意重叠重跑。
    历史回填按 --chunk-days 切块（默认 7 天），避免单个长事务锁表过久、失败时便于断点续跑。

用法：
    # 预览（不写库，打印 top5 聚合行）
    python backfill_netflow_factor.py --hours 48 --dry-run

    # 实际回填（调度每小时跑，回看 7d；每日 04:40 深修复回看 30d）
    python backfill_netflow_factor.py --hours 168

    # 历史回填：从 8 月 1 日补到当前 UTC 整点（分块，幂等）
    python backfill_netflow_factor.py --since 2026-08-01

    # 指定终止时间（左闭右开）
    python backfill_netflow_factor.py --since 2026-08-01 --until 2026-09-01
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
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
    WHERE block_timestamp >= %s::timestamptz
      AND block_timestamp <  %s::timestamptz
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


def _parse_ts(s: str) -> datetime:
    """解析 --since/--until；无时区信息一律按 UTC 处理（与桶口径一致）。"""
    d = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def main() -> None:
    parser = argparse.ArgumentParser(description="资产净流因子小时聚合回填（幂等，DB 端单语句）")
    parser.add_argument("--hours", type=int, default=6,
                        help="相对回看窗口（小时），默认 6；调度用 168（每小时）/ 720（每日深修复）")
    parser.add_argument("--since", type=str, default=None,
                        help="绝对起始时间（UTC，如 2026-08-01），历史回填用；优先于 --hours")
    parser.add_argument("--until", type=str, default=None,
                        help="绝对终止时间（左闭右开），默认当前 UTC 整点")
    parser.add_argument("--chunk-days", type=int, default=7,
                        help="历史回填分块天数，默认 7")
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
            cur.execute("SELECT date_trunc('hour', NOW())")
            now_hour = cur.fetchone()[0]

        # 窗口解析：--since/--until 优先，否则按 --hours 相对回看
        end = _parse_ts(args.until) if args.until else now_hour
        start = (_parse_ts(args.since) if args.since
                 else end - timedelta(hours=args.hours))
        if start >= end:
            print(f"⚠️ 空窗口 [{start} → {end})，无事可做")
            return

        chunks: list[tuple] = []
        cur_start = start
        step = timedelta(days=max(1, args.chunk_days))
        while cur_start < end:
            cur_end = min(cur_start + step, end)
            chunks.append((cur_start, cur_end))
            cur_start = cur_end

        print(f"窗口 [{start:%Y-%m-%d %H:%M}Z → {end:%Y-%m-%d %H:%M}Z)  "
              f"分 {len(chunks)} 块（每块 ≤{args.chunk_days}天）"
              f"{'  [dry-run 不写库]' if args.dry_run else ''}")

        total_rows = 0
        for i, (cs, ce) in enumerate(chunks, 1):
            with conn.cursor() as cur:
                cur.execute(SELECT_SQL, (cs, ce))
                rows = cur.fetchall()

            total_in = sum(float(r[2]) for r in rows)
            total_out = sum(float(r[4]) for r in rows)
            total_rows += len(rows)
            print(f"  [{i}/{len(chunks)}] {cs:%m-%d %H:%M} → {ce:%m-%d %H:%M}: "
                  f"聚合行={len(rows):>6}  流入=${total_in:>16,.0f}  流出=${total_out:>16,.0f}")

            if args.dry_run:
                for r in rows[:5]:
                    net = float(r[2]) - float(r[4])
                    print(f"      {r[0]:%m-%d %H:%M} {_symbol_of(conn, r[1]):<10} "
                          f"in=${float(r[2]):>14,.0f} out=${float(r[4]):>14,.0f} "
                          f"net=${net:>14,.0f} (i{r[3]}/o{r[5]})")
                continue

            with conn.cursor() as cur:
                cur.execute(INSERT_SQL, (cs, ce))
                print(f"      ✅ upsert 影响行={cur.rowcount}")

        if args.dry_run:
            print(f"[dry-run] 未写库；窗口聚合行合计={total_rows}")
        else:
            print(f"✅ 完成：{len(chunks)} 块，聚合行合计={total_rows} → biz.onchain_netflow_hourly")


if __name__ == "__main__":
    main()
