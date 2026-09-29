"""只读验证：链上异动告警页依赖的 prod 数据现状（三交叉检查）。
仅 SELECT，不写不删。

重点回答「地址库够不够全」：
  - ① 地址表分布（union 归因覆盖范围）
  - ② transfer_log 规模 + 近 N 天两端都不命中地址库的「漏掉率」
  - ③ chain 维度缺口：transfer_log 实际发生转账的链 vs 地址库覆盖的链（差集=必全漏）
  - ④ 抽样 union 归因后最新 5 条（字段语义核对）

用法（容器内）：
  DATABASE_URL=$DATABASE_URL python 05_代码与脚本/onchain_verify.py [--days 7]
"""
import argparse
import os
import psycopg2
import psycopg.rows

URL = os.environ.get("DATABASE_URL")
if not URL:
    raise SystemExit("DATABASE_URL 未设置")

ap = argparse.ArgumentParser()
ap.add_argument("--days", type=int, default=7, help="统计窗口（天），默认 7")
args = ap.parse_args()
DAYS = args.days


def main():
    with psycopg2.connect(URL, connect_timeout=30) as conn:
        conn.read_only = True
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:

            print(f"=== ① 地址表分布（union 归因覆盖范围，窗口 {DAYS}d）===")
            cur.execute("""
                SELECT 'exchange_wallet(high)' AS src, count(*) AS n
                FROM biz.onchain_exchange_wallet WHERE confidence='high'
                UNION ALL
                SELECT 'address_label(exchange,high/medium)', count(*)
                FROM biz.onchain_address_label
                WHERE label_type='exchange' AND confidence IN ('high','medium')
            """)
            for r in cur.fetchall():
                print(f"  {r['src']:<36} {r['n']:>10,}")

            print(f"\n=== ② transfer_log 规模 + 漏掉率（近 {DAYS} 天）===")
            cur.execute(f"""
                SELECT count(*) AS total,
                       count(*) FILTER (
                           WHERE block_timestamp >= now() - interval '{DAYS} days'
                       ) AS in_window
                FROM biz.onchain_transfer_log
                WHERE tx_hash NOT LIKE '0xtest%'
                  AND (is_suspect IS NOT TRUE OR is_suspect IS NULL)
            """)
            r = cur.fetchone()
            print(f"  全量有效行: {r['total']}    近{DAYS}天窗口: {r['in_window']}")

            cur.execute(f"""
                WITH exch AS (
                    SELECT address, chain FROM biz.onchain_exchange_wallet WHERE confidence='high'
                    UNION
                    SELECT address, chain FROM biz.onchain_address_label
                    WHERE label_type='exchange' AND confidence IN ('high','medium')
                )
                SELECT
                    count(*) AS total,
                    count(*) FILTER (
                        WHERE f.address IS NOT NULL OR t.address IS NOT NULL
                    ) AS hit,
                    count(*) FILTER (
                        WHERE f.address IS NULL AND t.address IS NULL
                    ) AS miss
                FROM biz.onchain_transfer_log tl
                LEFT JOIN exch f
                    ON f.address = tl.from_address
                   AND f.chain  = tl.chain
                LEFT JOIN exch t
                    ON t.address = tl.to_address
                   AND t.chain  = tl.chain
                WHERE tl.block_timestamp >= now() - interval '{DAYS} days'
                  AND tl.tx_hash NOT LIKE '0xtest%'
                  AND (tl.is_suspect IS NOT TRUE OR tl.is_suspect IS NULL)
            """)
            r = cur.fetchone()
            total = r["total"] or 0
            hit = r["hit"] or 0
            miss = r["miss"] or 0
            miss_rate = (miss / total * 100) if total else 0.0
            print(f"  窗口内转账: {total}")
            if total:
                print(f"  至少一端命中交易所: {hit}  ({hit/total*100:.1f}%)")
            print(f"  ⚠️ 两端都不命中（漏掉/无法归因）: {miss}  (漏掉率 {miss_rate:.1f}%)")

            print(f"\n=== ③ chain 维度缺口（近 {DAYS} 天）===")
            cur.execute(f"""
                SELECT tl.chain, count(*) AS n
                FROM biz.onchain_transfer_log tl
                WHERE tl.block_timestamp >= now() - interval '{DAYS} days'
                  AND tl.tx_hash NOT LIKE '0xtest%'
                  AND (tl.is_suspect IS NOT TRUE OR tl.is_suspect IS NULL)
                GROUP BY tl.chain ORDER BY n DESC
            """)
            chains_actual = {r["chain"]: r["n"] for r in cur.fetchall()}
            cur.execute("""
                SELECT chain, count(*) AS n FROM biz.onchain_exchange_wallet WHERE confidence='high'
                GROUP BY chain
                UNION ALL
                SELECT chain, count(*) FROM biz.onchain_address_label
                WHERE label_type='exchange' AND confidence IN ('high','medium')
                GROUP BY chain
            """)
            chains_cov = {}
            for r in cur.fetchall():
                chains_cov[r["chain"]] = chains_cov.get(r["chain"], 0) + r["n"]
            print("  链            转账数(实际)   地址库覆盖数   状态")
            for ch in sorted(chains_actual, key=lambda c: -chains_actual[c]):
                cov = chains_cov.get(ch, 0)
                status = "OK" if cov > 0 else "❌ 完全无标签(此链必全漏)"
                print(f"  {ch:<12} {chains_actual[ch]:>12} {cov:>12}   {status}")
            uncovered = [c for c in chains_actual if c not in chains_cov]
            if uncovered:
                tot_unc = sum(chains_actual[c] for c in uncovered)
                print(f"  → 完全无标签的链: {uncovered} 共 {tot_unc} 笔转账（占窗口 {tot_unc/total*100:.1f}%）")

            print(f"\n=== ④ 抽样 union 归因后最新 5 条（字段语义核对）===")
            cur.execute(f"""
                WITH exch AS (
                    SELECT address, chain, exchange_name FROM biz.onchain_exchange_wallet WHERE confidence='high'
                    UNION
                    SELECT address, chain, label_name FROM biz.onchain_address_label
                    WHERE label_type='exchange' AND confidence IN ('high','medium')
                )
                SELECT tl.chain, tl.value_usd, a.canonical_symbol,
                       f.exchange_name AS from_exch, t.exchange_name AS to_exch
                FROM biz.onchain_transfer_log tl
                LEFT JOIN core.asset a ON a.asset_id=tl.asset_id
                LEFT JOIN exch f ON f.address=tl.from_address AND f.chain=tl.chain
                LEFT JOIN exch t ON t.address=tl.to_address   AND t.chain=tl.chain
                WHERE tl.block_timestamp >= now() - interval '{DAYS} days'
                  AND tl.tx_hash NOT LIKE '0xtest%'
                  AND (tl.is_suspect IS NOT TRUE OR tl.is_suspect IS NULL)
                  AND (f.exchange_name IS NOT NULL OR t.exchange_name IS NOT NULL)
                ORDER BY tl.block_timestamp DESC LIMIT 5
            """)
            for r in cur.fetchall():
                print(f"  {r['chain']:<9} {str(r['canonical_symbol']):<8} "
                      f"from={r['from_exch']} to={r['to_exch']} usd={r['value_usd']}")

    print(f"\nVERIFY_DONE (只读, 窗口={DAYS}d)")


if __name__ == "__main__":
    main()
