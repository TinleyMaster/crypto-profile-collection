#!/usr/bin/env python3
"""
FEAT-SECTOR-003: 赛道日频市值快照 ETL
从 src_cmc.cmc_asset_quote_snapshot + biz.asset_sector 聚合
计算 12 赛道的日频市值 + 加权变化率，写入 biz.sector_flow_daily
"""
import os
import sys
import argparse
from datetime import date, datetime
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("DB_URL")

# 12 赛道 → 展示名映射（sector code -> label
SECTOR_LABELS = {
    "l1": "Layer 1",
    "l2": "Layer 2",
    "defi": "DeFi",
    "meme": "Memes",
    "ai": "AI & Big Data",
    "rwa": "Real World Assets",
    "gamefi": "Gaming",
    "stablecoin": "Stablecoin",
    "infra": "Infrastructure",
    "depin": "DePIN",
    "derivatives": "Derivatives",
    "cex_token": "CEX Tokens",
}


def get_conn():
    return psycopg2.connect(DATABASE_URL)


def get_available_dates(conn) -> list[date]:
    """取行情快照有数据的所有日期。"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT quote_time::date as dt
            FROM src_cmc.cmc_asset_quote_snapshot
            WHERE market_cap IS NOT NULL
            ORDER BY dt
        """)
        return [r[0] for r in cur.fetchall()]


def calc_sector_daily(conn, metric_date: date) -> list[dict]:
    """
    计算指定日期的 12 赛道日频指标。
    返回列表 [{"sector": ..., "market_cap": ..., "coin_count": ..., ...}
    """
    with conn.cursor() as cur:
        # 每天每个 cmc_id 取最新一条行情，然后按赛道聚合
        cur.execute("""
            WITH daily_quote AS (
                SELECT DISTINCT ON (cmc_id)
                    cmc_id, market_cap,
                    percent_change_1h, percent_change_24h,
                    percent_change_7d, percent_change_30d
                FROM src_cmc.cmc_asset_quote_snapshot
                WHERE market_cap IS NOT NULL
                  AND quote_time::date = %s
                ORDER BY cmc_id, quote_time DESC
            )
            SELECT
                s.sector,
                count(*) as coin_count,
                SUM(dq.market_cap) as total_mcap,
                SUM(dq.market_cap * dq.percent_change_1h) / NULLIF(SUM(dq.market_cap), 0) as w_1h,
                SUM(dq.market_cap * dq.percent_change_24h) / NULLIF(SUM(dq.market_cap), 0) as w_24h,
                SUM(dq.market_cap * dq.percent_change_7d) / NULLIF(SUM(dq.market_cap), 0) as w_7d,
                SUM(dq.market_cap * dq.percent_change_30d) / NULLIF(SUM(dq.market_cap), 0) as w_30d
            FROM biz.asset_sector s
            JOIN core.asset_source_map asm
              ON s.asset_id = asm.asset_id
             AND asm.source_code = 'cmc'
            JOIN daily_quote dq
              ON asm.source_asset_key::bigint = dq.cmc_id
            WHERE s.is_primary = true
            GROUP BY s.sector
            ORDER BY total_mcap DESC
        """, (metric_date,))
        rows = cur.fetchall()

    results = []
    for sector, coin_count, mcap, w1h, w24h, w7d, w30d in rows:
        results.append({
            "sector_type": "sector_12",
            "sector_key": sector,
            "sector_label": SECTOR_LABELS.get(sector, sector),
            "metric_date": metric_date,
            "market_cap": float(mcap) if mcap else None,
            "mcap_change_1d_pct": float(w24h) if w24h is not None else None,
            "mcap_change_7d_pct": float(w7d) if w7d is not None else None,
            "mcap_change_30d_pct": float(w30d) if w30d is not None else None,
            "coin_count": coin_count,
            "mcap_period": "7d",  # 来自 CMC 行情，7d 变化率直接可用
        })
    return results


def etl_date(conn, metric_date: date, dry_run: bool = False) -> int:
    """对指定日期执行 ETL，返回写入条数。"""
    sectors = calc_sector_daily(conn, metric_date)
    if not sectors:
        print(f"  {metric_date}: 无数据")
        return 0

    if dry_run:
        print(f"  {metric_date}: {len(sectors)} 赛道")
        for s in sectors[:5]:
            mcap = s["market_cap"]
            mcap_s = f"${mcap/1e9:.1f}B" if mcap else "N/A"
            chg = s["mcap_change_7d_pct"]
            chg_s = f"{chg:+.2f}%" if chg is not None else "N/A"
            print(f"    {s['sector_label']:20s} {s['coin_count']:4d} 币  {mcap_s:>10s}  7d {chg_s}")
        if len(sectors) > 5:
            print(f"    ... 还有 {len(sectors)-5} 个")
        return len(sectors)

    # 写入：UPSERT 模式
    with conn.cursor() as cur:
        # 先删当天（幂等）
        cur.execute("""
            DELETE FROM biz.sector_flow_daily
            WHERE sector_type = 'sector_12' AND metric_date = %s
        """, (metric_date,))

        # 批量插入
        insert_rows = [
            (
                s["sector_type"], s["sector_key"], s["sector_label"], s["metric_date"],
                s["market_cap"], s["mcap_change_1d_pct"], s["mcap_change_7d_pct"],
                s["mcap_change_30d_pct"], s["coin_count"], s["mcap_period"],
            )
            for s in sectors
        ]
        execute_values(cur, """
            INSERT INTO biz.sector_flow_daily (
                sector_type, sector_key, sector_label, metric_date,
                market_cap, mcap_change_1d_pct, mcap_change_7d_pct,
                mcap_change_30d_pct, coin_count, mcap_period
            ) VALUES %s
        """, insert_rows)

    print(f"  {metric_date}: {len(sectors)} 赛道 [已写入]")
    return len(sectors)


def main():
    parser = argparse.ArgumentParser(description="赛道日频市值快照 ETL")
    parser.add_argument("--date", type=str, help="指定日期 (YYYY-MM-DD)，默认最新有数据的最新一天")
    parser.add_argument("--backfill", action="store_true", help="回填所有有数据的日期")
    parser.add_argument("--dry-run", action="store_true", help="只计算不写入")
    args = parser.parse_args()

    if not DATABASE_URL:
        print("[ERROR] 未找到 DATABASE_URL 环境变量")
        return 1

    conn = get_conn()
    try:
        dates = []
        if args.backfill:
            dates = get_available_dates(conn)
            print(f"[INFO] 回填模式，共 {len(dates)} 天数据")
        elif args.date:
            d = datetime.strptime(args.date, "%Y-%m-%d").date()
            dates = [d]
            print(f"[INFO] 指定日期: {d}")
        else:
            # 默认取最新有数据的一天
            all_dates = get_available_dates(conn)
            if all_dates:
                dates = [all_dates[-1]]
                print(f"[INFO] 使用最新日期: {dates[0]}")
            else:
                print("[WARN] 无可用日期")
                return 0

        total = 0
        for d in dates:
            n = etl_date(conn, d, dry_run=args.dry_run)
            total += n

        if not args.dry_run:
            conn.commit()
            print(f"\n[DONE] 共 {len(dates)} 天, {total} 条记录")
        else:
            print(f"\n[DONE] 共 {len(dates)} 天, {total} 条记录 [DRY-RUN]")
    except Exception as e:
        conn.rollback()
        print(f"[ERROR] {e}")
        import traceback; traceback.print_exc()
        return 1
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
