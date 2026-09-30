#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2: 大额未标地址挖掘（whale / 聪明钱候选）。

从 onchain_transfer_log 反向挖：哪些**没有任何标签**的地址在大额、频繁地
活动，尤其是与已标交易所地址往来密切的——这是「未标记聪明钱在吸筹什么」
的内容源，也是富化爬取的优先级队列（按价值排序，而不是按频次）。

口径（2026-09-30 实测数据分布定版）：
- 排除四集合：onchain_address_label（任意类型）、onchain_exchange_wallet、
  core.asset_contract（代币合约——它们因 transfer 事件大量出现在流水里，
  不是 whale）、attempt 表 status='contract'（RPC 判定的合约）
- 排除零地址 / dead 地址
- value_usd 污染防御：库里有十亿美元级量纲污染（实测 2 笔 $142B），
  逐笔 cap 到 PER_TX_CAP（默认 $500M），n_capped 计数供分析师识别
- 交易所往来：对手方命中「address_label exchange high/medium ∪
  exchange_wallet high」（与净流归因同口径）

产物表 biz.onchain_whale_candidate（PK: chain, address, window_days），
幂等：按 (chain, window_days) 先删后插。

score = 2*ln(1+exch_usd) + ln(1+total_usd) + ln(1+median_usd) + ln(1+assets)
        —— 交易所往来加权最高（净流相关性），中位数压粉尘攻击地址，
           多资产奖励真·聪明钱（单资产刷量降权）。

用法：
  python bin/mine_whale_candidates.py                      # 全 8 链 30 天窗
  python bin/mine_whale_candidates.py --chain eth --days 7 --min-usd 50000
  python bin/mine_whale_candidates.py --top 30             # 打印 top 榜
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import psycopg
from dotenv import load_dotenv

CANDIDATE_TABLE = "biz.onchain_whale_candidate"
# 逐笔价值上限：防御 value_usd 量纲污染（实测存在 2 笔 $142B 的脏数据，
# 合法单笔超 $500M 极罕见）。被 cap 的笔数记入 n_capped。
PER_TX_CAP = 500_000_000
ZERO_ADDRS = (
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
)
CHAINS_DEFAULT = ["eth", "bsc", "base", "solana", "optimism", "polygon",
                  "avalanche", "arbitrum"]


def ensure_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {CANDIDATE_TABLE} (
                chain          TEXT NOT NULL,
                address        TEXT NOT NULL,
                window_days    INT  NOT NULL,
                tx_count       INT  NOT NULL,
                in_usd         NUMERIC NOT NULL DEFAULT 0,
                out_usd        NUMERIC NOT NULL DEFAULT 0,
                total_usd      NUMERIC NOT NULL DEFAULT 0,   -- cap 后
                median_usd     NUMERIC NOT NULL DEFAULT 0,
                max_usd        NUMERIC NOT NULL DEFAULT 0,
                n_capped       INT  NOT NULL DEFAULT 0,
                distinct_assets   INT NOT NULL DEFAULT 0,
                distinct_cp       INT NOT NULL DEFAULT 0,   -- 对手方数量
                exch_tx        INT  NOT NULL DEFAULT 0,      -- 与交易所往来笔数
                exch_usd       NUMERIC NOT NULL DEFAULT 0,   -- 与交易所往来金额(cap 后)
                score          NUMERIC NOT NULL DEFAULT 0,
                computed_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                PRIMARY KEY (chain, address, window_days)
            )
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_whale_cand_score
            ON {CANDIDATE_TABLE} (window_days, score DESC)
        """)
    conn.commit()


def mine_chain(conn, chain: str, days: int, min_usd: float, per_tx_cap: float) -> int:
    """挖一条链，返回写入候选数。"""
    t0 = time.time()
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = 300000")

        # 1. 窗口内有效转账（cap 逐笔价值防污染）
        cur.execute("DROP TABLE IF EXISTS tl_mine")
        cur.execute("""
            CREATE TEMP TABLE tl_mine AS
            SELECT chain, lower(from_address) fa, lower(to_address) ta,
                   LEAST(value_usd, %(cap)s) v,
                   (value_usd > %(cap)s) AS capped,
                   asset_id
            FROM biz.onchain_transfer_log
            WHERE chain = %(ch)s
              AND block_timestamp >= now() - (%(days)s || ' days')::interval
              AND value_usd IS NOT NULL AND value_usd > 0
              AND is_suspect IS NOT TRUE
              AND tx_hash NOT LIKE '0xtest%%'
        """, {"cap": per_tx_cap, "ch": chain, "days": days})
        cur.execute("CREATE INDEX ON tl_mine (fa)")
        cur.execute("CREATE INDEX ON tl_mine (ta)")

        # 2. 排除四集合 + 已标交易所集合（后者单独取，用于往来计算）
        cur.execute("DROP TABLE IF EXISTS excluded_mine")
        cur.execute("""
            CREATE TEMP TABLE excluded_mine AS
            SELECT lower(address) a FROM biz.onchain_address_label WHERE chain = %(ch)s
            UNION SELECT lower(address) FROM biz.onchain_exchange_wallet WHERE chain = %(ch)s
            UNION SELECT lower(contract_address) FROM core.asset_contract
                  WHERE CASE chain WHEN 'ethereum' THEN 'eth' ELSE chain END = %(ch)s
            UNION SELECT lower(address) FROM biz.onchain_label_fetch_attempt
                  WHERE chain = %(ch)s AND status = 'contract'
        """, {"ch": chain})
        cur.execute("CREATE INDEX ON excluded_mine (a)")

        cur.execute("DROP TABLE IF EXISTS exch_mine")
        cur.execute("""
            CREATE TEMP TABLE exch_mine AS
            SELECT lower(address) a FROM biz.onchain_address_label
            WHERE chain = %(ch)s AND label_type = 'exchange'
              AND confidence IN ('high', 'medium')
            UNION SELECT lower(address) FROM biz.onchain_exchange_wallet
                  WHERE chain = %(ch)s AND confidence = 'high'
        """, {"ch": chain})
        cur.execute("CREATE INDEX ON exch_mine (a)")

        # 3. 地址双边聚合（排除集合之外的地址才算候选）
        #    对手方是否交易所：fa/ta 命中 exch_mine
        cur.execute("DROP TABLE IF EXISTS agg_mine")
        cur.execute("""
            CREATE TEMP TABLE agg_mine AS
            WITH sides AS (
                -- 未标侧地址 + 金额 + 方向 + 对手方是否交易所
                -- 候选在 from 侧 = 流出；在 to 侧 = 流入
                SELECT t.fa AS a, t.v, t.capped, t.asset_id,
                       (e2.a IS NOT NULL) AS cp_is_exch,
                       t.ta AS cp,
                       t.v AS out_v, 0.0::numeric AS in_v
                FROM tl_mine t LEFT JOIN exch_mine e2 ON e2.a = t.ta
                WHERE t.fa IS NOT NULL AND t.fa <> ''
                  AND NOT EXISTS (SELECT 1 FROM excluded_mine e WHERE e.a = t.fa)
                UNION ALL
                SELECT t.ta, t.v, t.capped, t.asset_id,
                       (e3.a IS NOT NULL),
                       t.fa,
                       0.0::numeric, t.v
                FROM tl_mine t LEFT JOIN exch_mine e3 ON e3.a = t.fa
                WHERE t.ta IS NOT NULL AND t.ta <> ''
                  AND NOT EXISTS (SELECT 1 FROM excluded_mine e WHERE e.a = t.ta)
            )
            SELECT a,
                   count(*)                                  AS tx_count,
                   sum(out_v) FILTER (WHERE NOT capped)      AS out_usd,
                   sum(in_v)  FILTER (WHERE NOT capped)      AS in_usd,
                   sum(v) FILTER (WHERE NOT capped)          AS total_usd,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY v) AS median_usd,
                   max(v)                                    AS max_usd,
                   count(*) FILTER (WHERE capped)            AS n_capped,
                   count(distinct asset_id)                  AS distinct_assets,
                   count(distinct cp)                        AS distinct_cp,
                   count(*) FILTER (WHERE cp_is_exch)        AS exch_tx,
                   sum(v) FILTER (WHERE cp_is_exch AND NOT capped) AS exch_usd
            FROM sides
            WHERE NOT (a = ANY(%(zero)s))
            GROUP BY a
            HAVING sum(v) FILTER (WHERE NOT capped) >= %(min_usd)s
        """, {"ch": chain, "min_usd": min_usd, "zero": list(ZERO_ADDRS)})

        # 4. 分数 + 幂等写库（先删本 chain+window 分区再插）
        cur.execute(f"""
            DELETE FROM {CANDIDATE_TABLE} WHERE chain = %(ch)s AND window_days = %(days)s
        """, {"ch": chain, "days": days})
        cur.execute(f"""
            INSERT INTO {CANDIDATE_TABLE}
                (chain, address, window_days, tx_count, in_usd, out_usd, total_usd,
                 median_usd, max_usd, n_capped, distinct_assets, distinct_cp,
                 exch_tx, exch_usd, score)
            SELECT %(ch)s, a, %(days)s, tx_count,
                   coalesce(in_usd, 0), coalesce(out_usd, 0), total_usd,
                   median_usd, max_usd, n_capped, distinct_assets, distinct_cp,
                   exch_tx, coalesce(exch_usd, 0),
                   round( (2 * ln(1 + coalesce(exch_usd, 0))
                           + ln(1 + total_usd)
                           + ln(1 + median_usd)
                           + ln(1 + distinct_assets))::numeric, 3)
            FROM agg_mine
        """, {"ch": chain, "days": days})
        n = cur.rowcount
    conn.commit()
    print(f"  {chain}: 候选 {n} 个（{time.time()-t0:.0f}s）")
    return n


def print_top(conn, days: int, top: int, chain: str | None = None) -> None:
    sql = f"""
        SELECT chain, address, tx_count,
               round(total_usd/1e6, 2)  AS total_m,
               round(median_usd, 0)     AS med,
               round(exch_usd/1e6, 2)   AS exch_m,
               exch_tx, distinct_assets, n_capped, score
        FROM {CANDIDATE_TABLE}
        WHERE window_days = %s
    """
    params: list = [days]
    if chain:
        sql += " AND chain = %s"
        params.append(chain)
    sql += " ORDER BY score DESC LIMIT %s"
    params.append(top)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    print(f"\n── TOP {len(rows)} 候选（window={days}d）──")
    print(f"{'chain':9} {'address':14} {'tx':>6} {'$M':>9} {'med$':>9} "
          f"{'exch$M':>8} {'exTx':>5} {'ast':>4} {'cap':>4} {'score':>8}")
    for r in rows:
        print(f"{r[0]:9} {r[1][:12]+'…':14} {r[2]:>6} {r[3]:>9} {r[4]:>9} "
              f"{r[5]:>8} {r[6]:>5} {r[7]:>4} {r[8]:>4} {r[9]:>8}")


def main() -> None:
    parser = argparse.ArgumentParser(description="大额未标地址挖掘（whale 候选）")
    parser.add_argument("--chain", default=",".join(CHAINS_DEFAULT),
                        help=f"逗号分隔，默认 {','.join(CHAINS_DEFAULT)}")
    parser.add_argument("--days", type=int, default=30, help="回看窗口天数（默认 30）")
    parser.add_argument("--min-usd", type=float, default=100_000,
                        help="候选门槛：cap 后总额（默认 100000）")
    parser.add_argument("--per-tx-cap", type=float, default=PER_TX_CAP,
                        help=f"逐笔价值上限防污染（默认 {PER_TX_CAP:.0f}）")
    parser.add_argument("--top", type=int, default=20, help="打印 top N（默认 20）")
    parser.add_argument("--no-mine", action="store_true",
                        help="不重新挖掘，只打印已有结果的 top 榜")
    args = parser.parse_args()

    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("❌ 缺少 DATABASE_URL"); sys.exit(1)

    chains = [c.strip() for c in args.chain.split(",") if c.strip()]
    conn = psycopg.connect(db_url, connect_timeout=30)
    ensure_table(conn)

    if not args.no_mine:
        total = 0
        for ch in chains:
            try:
                total += mine_chain(conn, ch, args.days, args.min_usd, args.per_tx_cap)
            except Exception as e:
                print(f"  ⚠️  {ch} 挖掘失败: {e}")
                conn.rollback()
        print(f"\n合计候选 {total} 个（window={args.days}d, min_usd=${args.min_usd:,.0f}）")

    print_top(conn, args.days, args.top,
              chain=chains[0] if len(chains) == 1 else None)
    conn.close()


if __name__ == "__main__":
    main()
