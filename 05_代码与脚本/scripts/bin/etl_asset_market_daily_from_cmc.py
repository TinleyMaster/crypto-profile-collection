"""ETL: 从 src_cmc.cmc_asset_quote_snapshot 聚合成日级，写入 biz.asset_market_daily。

聚合规则：
- 按 (asset_id, date) 分组，取当日 quote_time 最晚的一条快照作为日收盘价
- source_code = 'cmc'
- 幂等：ON CONFLICT 更新所有数值字段
- 纯 SQL 批量执行（性能远优于逐行 INSERT）

用法：
    python etl_asset_market_daily_from_cmc.py              # 全量回填（所有已有快照）
    python etl_asset_market_daily_from_cmc.py --days 7     # 只回填最近 7 天
    python etl_asset_market_daily_from_cmc.py --dry-run    # 预览，不写入
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402


# ── FDV 口径修复（审计 P0-1，2026-09-26）──────────────────────────────
#
# 背景：biz.asset_market_daily.fdv 直接照抄 src_cmc.cmc_asset_quote_snapshot.fdv
# （即 CMC 的 fully_diluted_market_cap）。CMC 会在不通知的情况下**下调** max_supply，
# 一旦下调到「等于流通量」，fully_diluted_market_cap 就退化成 == market_cap，
# 稀释风险在估值维度整体隐身。实况（PONS / asset_id=11114）：2026-09-06 起
# CMC 把 max_supply 由 1e9 改为 ≈7.02e8（== 流通量），fdv 随之变成 == 市值，
# 页面估值从 6.18 亿掉到 4.23 亿；另有 08-30 / 08-31 / 09-03 三天 CMC 直接返回 fdv=NULL。
#
# 修复原则（不推翻 CMC，只在 CMC 自相矛盾时纠正）：
#   1. 源 fdv 有效且**未退化**（与市值差异 >1%）→ 一律采用，不动；
#   2. 源 fdv 缺失、或退化为 ≈ 市值 → 若存在**被 CMC 历史印证过**的最大供应量
#      （tokenomist 的 max_supply，且 CMC 历史快照曾报过 ≥ 该值的 max_supply），
#      且（源 fdv 为 NULL，或）该值明显大于流通量（>1.02x）→ 用 price × max_supply 重建 fdv；
#      NULL 行不再要求「max_supply > 流通量」：此时填 price × max 只是补上 FDV 的定义，
#      不会改写任何已有值（CMC 历史行情行的 circulating 常被写成 == max_supply）。
#   3. 其余情况保持原值（含 NULL）。
#
# 「CMC 历史印证」（hist.cmc_hist_max >= tok.max_supply * 0.98）是关键守卫：
# 若不加，会把大量代币化股票（MRVLon / CMGon 等）误伤——它们的 tokenomics
# max_supply 由 LLM 从底层股票股本抽取，量级完全失真，而 CMC 从未报过该值。
# 实测：无守卫时 09-01 起 2,569 个资产被改写；加守卫后 597 个，且其中 99%
# 属于「补 NULL」（原本无值），仅 PONS 这类「CMC 自我下调」的才被纠偏。
_FDV_REPAIR_CTE = """
    WITH tok AS (
        SELECT DISTINCT ON (asset_id) asset_id, max_supply
        FROM biz.asset_tokenomics
        WHERE max_supply IS NOT NULL AND max_supply > 0
        ORDER BY asset_id, updated_at DESC NULLS LAST
    ),
    hist AS (
        SELECT asm.asset_id, MAX(q.max_supply) AS cmc_hist_max
        FROM src_cmc.cmc_asset_quote_snapshot q
        JOIN core.asset_source_map asm
          ON asm.source_code = 'cmc'
         AND asm.source_asset_key = q.cmc_id::text
        GROUP BY asm.asset_id
    ),
    cand AS (
        SELECT m.asset_id, m.market_date, m.source_code, m.fdv AS old_fdv,
               CASE
                 WHEN m.price_usd IS NULL OR m.price_usd <= 0 THEN NULL
                 WHEN m.fdv IS NOT NULL
                      AND (m.market_cap IS NULL OR m.market_cap <= 0
                           OR ABS(m.fdv - m.market_cap) > 0.01 * m.market_cap)
                   THEN m.fdv
                 WHEN t.max_supply IS NOT NULL
                      AND (m.fdv IS NULL
                           OR t.max_supply > COALESCE(m.circulating_supply, 0) * 1.02)
                      AND h.cmc_hist_max IS NOT NULL
                      AND h.cmc_hist_max >= t.max_supply * 0.98
                   THEN ROUND(m.price_usd * t.max_supply, 2)
                 ELSE m.fdv
               END AS new_fdv
        FROM biz.asset_market_daily m
        LEFT JOIN tok t ON t.asset_id = m.asset_id
        LEFT JOIN hist h ON h.asset_id = m.asset_id
        {date_filter}
    )
    UPDATE biz.asset_market_daily m
    SET fdv = c.new_fdv,
        raw_ref = COALESCE(m.raw_ref, '{{}}'::jsonb)
                  || '{{"fdv_basis": "price_x_corroborated_max_supply"}}'::jsonb,
        updated_at = NOW()
    FROM cand c
    WHERE m.asset_id = c.asset_id
      AND m.market_date = c.market_date
      AND m.source_code = c.source_code
      AND c.new_fdv IS DISTINCT FROM c.old_fdv
"""


def repair_degenerate_fdv(conn, days: int | None = None) -> int:
    """重建退化/缺失的 FDV，返回被修正的行数（幂等）。"""
    date_filter = ""
    params: tuple = ()
    if days is not None:
        date_filter = "WHERE m.market_date >= %s"
        params = ((datetime.now(timezone.utc) - timedelta(days=days)).date(),)
    sql = _FDV_REPAIR_CTE.format(date_filter=date_filter)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        n = cur.rowcount
    conn.commit()
    return n


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ETL CMC quote snapshots into biz.asset_market_daily (daily close)."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Only backfill last N days. Default: all available data.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count rows only, do not insert.",
    )
    return parser


def etl_cmc_to_daily(days: int | None, dry_run: bool) -> dict:
    """执行 ETL，返回统计信息。"""
    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            date_filter = ""
            params: tuple = ()
            if days is not None:
                start_date = (datetime.now(timezone.utc) - timedelta(days=days)).date()
                date_filter = "WHERE q.quote_time >= %s"
                params = (start_date,)

            if dry_run:
                sql = f"""
                    WITH ranked AS (
                        SELECT
                            asm.asset_id,
                            DATE(q.quote_time AT TIME ZONE 'UTC') AS market_date,
                            ROW_NUMBER() OVER (
                                PARTITION BY asm.asset_id, DATE(q.quote_time AT TIME ZONE 'UTC')
                                ORDER BY q.quote_time DESC
                            ) AS rn
                        FROM src_cmc.cmc_asset_quote_snapshot q
                        JOIN core.asset_source_map asm
                            ON asm.source_code = 'cmc'
                            AND asm.source_asset_key = q.cmc_id::text
                        {date_filter}
                    )
                    SELECT count(*), min(market_date), max(market_date), count(DISTINCT asset_id)
                    FROM ranked
                    WHERE rn = 1 AND asset_id IS NOT NULL
                """
                cur.execute(sql, params)
                row = cur.fetchone()
                return {
                    "total": row[0],
                    "date_from": str(row[1]) if row[1] else None,
                    "date_to": str(row[2]) if row[2] else None,
                    "assets": row[3],
                    "dry_run": True,
                }

            # 批量 INSERT ... ON CONFLICT
            sql = f"""
                INSERT INTO biz.asset_market_daily
                    (asset_id, market_date, source_code, price_usd,
                     market_cap, fdv, circulating_supply, total_supply,
                     volume_24h, change_24h, change_7d, raw_ref)
                WITH ranked AS (
                    SELECT
                        asm.asset_id,
                        DATE(q.quote_time AT TIME ZONE 'UTC') AS market_date,
                        CASE WHEN q.price_usd IS NULL OR q.price_usd <= 0
                             THEN NULL ELSE q.price_usd END AS price_usd,
                        CASE WHEN q.price_usd IS NULL OR q.price_usd <= 0
                             THEN NULL ELSE NULLIF(q.market_cap, 0) END AS market_cap,
                        CASE WHEN q.price_usd IS NULL OR q.price_usd <= 0
                             THEN NULL ELSE NULLIF(q.fdv, 0) END AS fdv,
                        q.circulating_supply,
                        q.total_supply,
                        CASE WHEN q.price_usd IS NULL OR q.price_usd <= 0
                             THEN NULL ELSE NULLIF(q.volume_24h, 0) END AS volume_24h,
                        q.percent_change_24h AS change_24h,
                        q.percent_change_7d AS change_7d,
                        q.is_anomaly,
                        ROW_NUMBER() OVER (
                            PARTITION BY asm.asset_id, DATE(q.quote_time AT TIME ZONE 'UTC')
                            ORDER BY q.quote_time DESC
                        ) AS rn
                    FROM src_cmc.cmc_asset_quote_snapshot q
                    JOIN core.asset_source_map asm
                        ON asm.source_code = 'cmc'
                        AND asm.source_asset_key = q.cmc_id::text
                    WHERE (q.is_anomaly IS NOT TRUE OR q.is_anomaly IS NULL)
                    {date_filter}
                )
                SELECT
                    asset_id, market_date, 'cmc', price_usd,
                    market_cap, fdv, circulating_supply, total_supply,
                    volume_24h, change_24h, change_7d,
                    '{{"source": "cmc_quote_snapshot"}}'::jsonb
                FROM ranked
                WHERE rn = 1 AND asset_id IS NOT NULL
                ON CONFLICT (asset_id, market_date, source_code) DO UPDATE SET
                    price_usd = EXCLUDED.price_usd,
                    market_cap = EXCLUDED.market_cap,
                    fdv = EXCLUDED.fdv,
                    circulating_supply = EXCLUDED.circulating_supply,
                    total_supply = EXCLUDED.total_supply,
                    volume_24h = EXCLUDED.volume_24h,
                    change_24h = EXCLUDED.change_24h,
                    change_7d = EXCLUDED.change_7d,
                    updated_at = NOW()
            """
            cur.execute(sql, params)
            affected = cur.rowcount
            conn.commit()

        # FDV 口径修复：必须在写入之后、同一时间窗口内执行（审计 P0-1）
        fdv_repaired = repair_degenerate_fdv(conn, days)

        with conn.cursor() as cur:
            # 验证
            cur.execute("""
                SELECT count(*), min(market_date), max(market_date), count(DISTINCT asset_id)
                FROM biz.asset_market_daily
                WHERE source_code = 'cmc'
            """)
            row = cur.fetchone()
            return {
                "affected": affected,
                "fdv_repaired": fdv_repaired,
                "total": row[0],
                "date_from": str(row[1]) if row[1] else None,
                "date_to": str(row[2]) if row[2] else None,
                "assets": row[3],
            }


def check_daily_continuity() -> dict:
    """日价连续性自检：检测昨日是否有缺失，若有则告警+自动重试一次。

    RT-BACKTEST-D1-001 改动 3：在 ETL 末尾执行，确保单日失败可重试而非静默缺日。
    """
    from datetime import date, timedelta

    settings = get_settings(require_database=True)
    yesterday = date.today() - timedelta(days=1)

    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            # 检查昨日 asset_market_daily 覆盖
            cur.execute("""
                SELECT COUNT(DISTINCT asset_id)
                FROM biz.asset_market_daily
                WHERE market_date = %s
            """, (yesterday,))
            row = cur.fetchone()
            asset_count = row[0] if row else 0

            # 对比前日作为基准
            day_before = yesterday - timedelta(days=1)
            cur.execute("""
                SELECT COUNT(DISTINCT asset_id)
                FROM biz.asset_market_daily
                WHERE market_date = %s
            """, (day_before,))
            row2 = cur.fetchone()
            baseline_count = row2[0] if row2 else 0

    result = {
        "check_date": yesterday.isoformat(),
        "asset_count": asset_count,
        "baseline_count": baseline_count,
        "has_gap": asset_count < 100,
        "retry_triggered": False,
    }

    if asset_count < 100 and baseline_count > 100:
        # 昨日缺失，尝试重试 ETL
        print(f"[CONTINUITY] 昨日 {yesterday} 仅 {asset_count} 条记录（基准 {baseline_count}），触发自动重试",
              file=sys.stderr)
        try:
            retry_result = etl_cmc_to_daily(days=2, dry_run=False)
            result["retry_triggered"] = True
            result["retry_result"] = retry_result
            print(f"[CONTINUITY] 重试完成: affected={retry_result.get('affected', 0)}", file=sys.stderr)
        except Exception as e:
            result["retry_error"] = str(e)
            print(f"[CONTINUITY] 重试失败: {e}", file=sys.stderr)
    elif asset_count >= 100:
        print(f"[CONTINUITY] 昨日 {yesterday} 正常: {asset_count} 条记录")
    else:
        print(f"[CONTINUITY] 昨日 {yesterday} 缺失但无基准可对比（前日也缺失）", file=sys.stderr)

    return result


def main() -> int:
    parser = build_parser()
    parser.add_argument(
        "--check-continuity",
        action="store_true",
        help="仅执行昨日连续性自检（不执行主 ETL）",
    )
    parser.add_argument(
        "--repair-only",
        action="store_true",
        help="仅执行 FDV 口径修复（不重跑 ETL），配合 --days 限定窗口",
    )
    args = parser.parse_args()

    if args.check_continuity:
        result = check_daily_continuity()
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0 if not result.get("has_gap") or result.get("retry_triggered") else 1

    if args.repair_only:
        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            n = repair_degenerate_fdv(conn, args.days)
        print(f"[FDV] 修复退化/缺失 FDV: {n:,} 行")
        return 0

    print(f"[ETL] asset_market_daily from CMC snapshots")
    if args.days:
        print(f"[ETL] Range: last {args.days} days")
    else:
        print(f"[ETL] Range: all available data")

    result = etl_cmc_to_daily(days=args.days, dry_run=args.dry_run)

    if result.get("dry_run"):
        print(f"[DRY-RUN] Would process {result['total']} rows ({result['assets']} assets)")
        if result.get("date_from"):
            print(f"[DRY-RUN] Date range: {result['date_from']} ~ {result['date_to']}")
    else:
        print(f"[ETL] Affected: {result['affected']:,} rows")
        if result.get("fdv_repaired"):
            print(f"[ETL] FDV 修复: {result['fdv_repaired']:,} rows")
        print(f"[ETL] Total now: {result['total']:,} rows ({result['assets']:,} assets)")
        if result.get("date_from"):
            print(f"[ETL] Date range: {result['date_from']} ~ {result['date_to']}")

    # RT-BACKTEST-D1-001 改动 3：日价连续性自检
    if not args.dry_run:
        print(f"\n[ETL] === 日价连续性自检 ===")
        continuity = check_daily_continuity()
        if continuity.get("has_gap") and not continuity.get("retry_triggered"):
            print(f"[ETL] WARNING: 连续性自检发现缺口且重试未触发", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
