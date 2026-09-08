#!/usr/bin/env python3
"""
FEAT-SECTOR-003: 赛道日频快照 ETL（市值 + TVL + 净流入）

数据源：
  - 市值：src_cmc.cmc_asset_quote_snapshot + biz.asset_sector
  - TVL：biz.protocol_metric_daily + biz.asset_sector
  - 净流入：TVL × 加权变化率（截尾 5% 避免异常值干扰）

写入：biz.sector_flow_daily（sector_type = 'sector_12'）
"""
import sys
import argparse
from datetime import date, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

# 12 赛道 → 展示名映射
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

# 截尾比例（去掉最高最低各 TRIM_PCT 的变化率异常值）
TRIM_PCT = 0.05


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


def calc_sector_mcap(conn, metric_date: date) -> dict[str, dict]:
    """
    计算指定日期 12 赛道的市值 + 加权变化率。
    返回 {sector_key: {market_cap, coin_count, mcap_change_1d/7d/30d_pct}}
    """
    with conn.cursor() as cur:
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
        """, (metric_date,))
        rows = cur.fetchall()

    result = {}
    for sector, coin_count, mcap, w24h, w7d, w30d in rows:
        result[sector] = {
            "market_cap": float(mcap) if mcap else None,
            "coin_count": coin_count,
            "mcap_change_1d_pct": float(w24h) if w24h is not None else None,
            "mcap_change_7d_pct": float(w7d) if w7d is not None else None,
            "mcap_change_30d_pct": float(w30d) if w30d is not None else None,
            "mcap_period": "7d",
        }
    return result


def calc_sector_tvl(conn, metric_date: date) -> dict[str, dict]:
    """
    计算指定日期 12 赛道的 TVL + 截尾加权变化率 + 净流入估算。
    返回 {sector_key: {tvl, protocol_count, tvl_change_1d/7d/30d_pct, flow_7d_usd, flow_7d_pct}}
    """
    # protocol_metric_daily 可能是用的 metric_date 也可能是最新有数据的一天
    with conn.cursor() as cur:
        # 先确认指定日期有没有 TVL 数据，没有就用最近一天
        cur.execute("""
            SELECT MAX(metric_date) FROM biz.protocol_metric_daily
            WHERE metric_date <= %s
        """, (metric_date,))
        tvl_date = cur.fetchone()[0]
        if not tvl_date:
            return {}

        # 截尾 5% 加权变化率（去掉最高最低各 5% 的变化率异常值）
        cur.execute(f"""
            WITH ranked AS (
                SELECT
                    s.sector,
                    p.tvl,
                    p.tvl_change_1d,
                    p.tvl_change_7d,
                    PERCENT_RANK() OVER (
                        PARTITION BY s.sector ORDER BY COALESCE(p.tvl_change_7d, 0)
                    ) as pct_rank_7d,
                    PERCENT_RANK() OVER (
                        PARTITION BY s.sector ORDER BY COALESCE(p.tvl_change_1d, 0)
                    ) as pct_rank_1d
                FROM biz.protocol_metric_daily p
                JOIN biz.asset_sector s
                  ON p.asset_id = s.asset_id
                 AND s.is_primary = true
                WHERE p.metric_date = %s
                  AND p.tvl > 0
            )
            SELECT
                sector,
                COUNT(*) as protocol_count,
                SUM(tvl) as tvl,
                -- 1d 截尾加权
                SUM(CASE WHEN pct_rank_1d BETWEEN {TRIM_PCT} AND {1 - TRIM_PCT}
                         THEN tvl * tvl_change_1d ELSE 0 END)
                  / NULLIF(SUM(CASE WHEN pct_rank_1d BETWEEN {TRIM_PCT} AND {1 - TRIM_PCT}
                                    THEN tvl ELSE 0 END), 0) as tvl_change_1d,
                -- 7d 截尾加权
                SUM(CASE WHEN pct_rank_7d BETWEEN {TRIM_PCT} AND {1 - TRIM_PCT}
                         THEN tvl * tvl_change_7d ELSE 0 END)
                  / NULLIF(SUM(CASE WHEN pct_rank_7d BETWEEN {TRIM_PCT} AND {1 - TRIM_PCT}
                                    THEN tvl ELSE 0 END), 0) as tvl_change_7d
            FROM ranked
            GROUP BY sector
        """, (tvl_date,))
        rows = cur.fetchall()

    result = {}
    for sector, proto_count, tvl, chg_1d, chg_7d in rows:
        tvl_val = float(tvl) if tvl else None
        chg_7d_val = float(chg_7d) if chg_7d is not None else None
        chg_1d_val = float(chg_1d) if chg_1d is not None else None

        # 净流入估算：TVL × 变化率 / (1 + 变化率)
        # 原理：TVL_now = TVL_prev × (1 + chg) → flow = TVL_now - TVL_prev = TVL_now × chg / (1 + chg)
        flow_7d = None
        flow_7d_pct = None
        if tvl_val is not None and chg_7d_val is not None:
            # 变化率是百分比，转成小数
            chg_ratio = chg_7d_val / 100.0
            if abs(chg_ratio + 1.0) > 1e-9:
                flow_7d = tvl_val * chg_ratio / (1.0 + chg_ratio)
                flow_7d_pct = chg_7d_val

        result[sector] = {
            "tvl": tvl_val,
            "protocol_count": proto_count,
            "tvl_change_1d_pct": chg_1d_val,
            "tvl_change_7d_pct": chg_7d_val,
            "tvl_change_30d_pct": None,  # protocol_metric_daily 只有 1d/7d
            "flow_7d_usd": flow_7d,
            "flow_7d_pct": flow_7d_pct,
        }
    return result


def calc_composite_score(mcap_data: dict, tvl_data: dict) -> float | None:
    """
    综合评分：市值动量 + TVL 动量 加权。
    简单版：7d 市值变化率标准化 + 7d TVL 变化率标准化，各 50% 权重。
    """
    scores = []
    if mcap_data.get("mcap_change_7d_pct") is not None:
        # 市值变化率：±30% 映射到 ±100 分
        v = mcap_data["mcap_change_7d_pct"]
        scores.append(max(-100, min(100, v / 30.0 * 100)) * 0.5)
    if tvl_data.get("tvl_change_7d_pct") is not None:
        v = tvl_data["tvl_change_7d_pct"]
        scores.append(max(-100, min(100, v / 30.0 * 100)) * 0.5)
    if not scores:
        return None
    return sum(scores)


def calc_sector_daily(conn, metric_date: date) -> list[dict]:
    """
    计算指定日期的 12 赛道完整日频指标（市值 + TVL + 净流入）。
    """
    mcap_map = calc_sector_mcap(conn, metric_date)
    tvl_map = calc_sector_tvl(conn, metric_date)

    # 合并：取两个集合的并集，以市值为主排序
    all_sectors = set(mcap_map.keys()) | set(tvl_map.keys())
    results = []
    for sector in all_sectors:
        mcap_data = mcap_map.get(sector, {})
        tvl_data = tvl_map.get(sector, {})

        row = {
            "sector_type": "sector_12",
            "sector_key": sector,
            "sector_label": SECTOR_LABELS.get(sector, sector),
            "metric_date": metric_date,
            # 市值相关
            "market_cap": mcap_data.get("market_cap"),
            "mcap_change_1d_pct": mcap_data.get("mcap_change_1d_pct"),
            "mcap_change_7d_pct": mcap_data.get("mcap_change_7d_pct"),
            "mcap_change_30d_pct": mcap_data.get("mcap_change_30d_pct"),
            "coin_count": mcap_data.get("coin_count", 0),
            "mcap_period": mcap_data.get("mcap_period", "7d"),
            # TVL 相关
            "tvl": tvl_data.get("tvl"),
            "tvl_change_1d_pct": tvl_data.get("tvl_change_1d_pct"),
            "tvl_change_7d_pct": tvl_data.get("tvl_change_7d_pct"),
            "tvl_change_30d_pct": tvl_data.get("tvl_change_30d_pct"),
            "protocol_count": tvl_data.get("protocol_count", 0) or 0,
            # 净流入
            "flow_7d_usd": tvl_data.get("flow_7d_usd"),
            "flow_7d_pct": tvl_data.get("flow_7d_pct"),
            # 综合评分
            "composite_score": calc_composite_score(mcap_data, tvl_data),
            "mode": "full" if mcap_data and tvl_data else ("mcap_only" if mcap_data else "tvl_only"),
        }
        results.append(row)

    # 按市值降序，无市值的按 TVL 降序
    results.sort(key=lambda x: (x["market_cap"] or 0, x["tvl"] or 0), reverse=True)
    return results


def etl_date(conn, metric_date: date, dry_run: bool = False) -> int:
    """对指定日期执行 ETL，返回写入条数。"""
    sectors = calc_sector_daily(conn, metric_date)
    if not sectors:
        print(f"  {metric_date}: 无数据")
        return 0

    if dry_run:
        print(f"  {metric_date}: {len(sectors)} 赛道")
        for s in sectors:
            mcap = s["market_cap"]
            mcap_s = f"${mcap/1e9:.1f}B" if mcap else "     N/A"
            tvl = s["tvl"]
            tvl_s = f"${tvl/1e9:.1f}B" if tvl else "     N/A"
            mc7 = s["mcap_change_7d_pct"]
            mc7_s = f"{mc7:+.2f}%" if mc7 is not None else "   N/A"
            tv7 = s["tvl_change_7d_pct"]
            tv7_s = f"{tv7:+.2f}%" if tv7 is not None else "   N/A"
            flow = s["flow_7d_usd"]
            flow_s = f"${flow/1e9:+.2f}B" if flow is not None else "     N/A"
            print(f"    {s['sector_label']:20s} mcap={mcap_s:>10s} tvl={tvl_s:>10s} "
                  f"mcap7d={mc7_s:>9s} tvl7d={tv7_s:>9s} flow7d={flow_s:>11s} "
                  f"[{s['mode']}]")
        return len(sectors)

    # 写入：UPSERT 模式（先 DELETE 再 INSERT，幂等）
    with conn.cursor() as cur:
        cur.execute("""
            DELETE FROM biz.sector_flow_daily
            WHERE sector_type = 'sector_12' AND metric_date = %s
        """, (metric_date,))

        INSERT_SQL = """
            INSERT INTO biz.sector_flow_daily (
                sector_type, sector_key, sector_label, metric_date,
                market_cap, mcap_change_1d_pct, mcap_change_7d_pct,
                mcap_change_30d_pct, coin_count, mcap_period,
                tvl, tvl_change_1d_pct, tvl_change_7d_pct,
                tvl_change_30d_pct, protocol_count,
                flow_7d_usd, flow_7d_pct,
                composite_score, mode
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s,
                %s, %s,
                %s, %s
            )
        """
        for s in sectors:
            cur.execute(INSERT_SQL, (
                s["sector_type"], s["sector_key"], s["sector_label"], s["metric_date"],
                s["market_cap"], s["mcap_change_1d_pct"], s["mcap_change_7d_pct"],
                s["mcap_change_30d_pct"], s["coin_count"], s["mcap_period"],
                s["tvl"], s["tvl_change_1d_pct"], s["tvl_change_7d_pct"],
                s["tvl_change_30d_pct"], s["protocol_count"],
                s["flow_7d_usd"], s["flow_7d_pct"],
                s["composite_score"], s["mode"],
            ))

    print(f"  {metric_date}: {len(sectors)} 赛道 [已写入]")
    return len(sectors)


def main():
    parser = argparse.ArgumentParser(description="赛道日频快照 ETL（市值 + TVL + 净流入）")
    parser.add_argument("--date", type=str, help="指定日期 (YYYY-MM-DD)，默认最新有数据的最新一天")
    parser.add_argument("--backfill", action="store_true", help="回填所有有数据的日期")
    parser.add_argument("--dry-run", action="store_true", help="只计算不写入")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
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

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
