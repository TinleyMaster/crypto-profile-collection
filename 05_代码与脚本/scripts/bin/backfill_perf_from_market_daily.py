#!/usr/bin/env python3
"""用免费行情数据自算 ATH/ATL/距高点回撤，回填 biz.asset_perf_daily。

数据来源：biz.asset_market_daily（CMC 免费 listings 快照聚合 + 历史回填，无需付费 key）。
替代 CMC 付费 /v2/cryptocurrency/price-performance-stats/latest 的免费方案：
用已有日频历史计算每资产 all_time 的 ATH/ATL 及当前距 ATH 回撤。

用法：
    python backfill_perf_from_market_daily.py                 # 全量（有历史的资产）
    python backfill_perf_from_market_daily.py --min-days 30   # 至少 30 天历史才算
    python backfill_perf_from_market_daily.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402
from crypto_research.db.upsert import execute_many, load_sql  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从 asset_market_daily 免费行情自算 ATH/ATL/回撤，回填 biz.asset_perf_daily。"
    )
    parser.add_argument(
        "--min-days",
        type=int,
        default=15,
        help="资产最少需要 N 天历史才算 ATH（默认 15，太短无意义）。",
    )
    parser.add_argument(
        "--asset-id",
        type=int,
        default=None,
        help="只处理单个资产（调试用）。",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="最多处理资产数（0=全量）。分批跑时用。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只统计，不写入。",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    settings = get_settings(require_database=True)
    upsert_sql = load_sql("biz/upsert_asset_perf_daily.sql")

    today = date.today()

    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            if args.asset_id:
                cur.execute(
                    """
                    SELECT asset_id, COUNT(*) AS n, MIN(market_date) AS min_d,
                           MAX(market_date) AS max_d
                    FROM biz.asset_market_daily
                    WHERE asset_id = %s
                      AND source_code IN ('cmc', 'cmc_historical')
                      AND price_usd > 0
                    GROUP BY asset_id
                    """,
                    (args.asset_id,),
                )
                rows = cur.fetchall()
            else:
                # 全市场：有足够历史的资产，且当日还没算过（增量续跑，幂等）
                cur.execute(
                    """
                    SELECT m.asset_id, COUNT(*) AS n, MIN(m.market_date) AS min_d,
                           MAX(m.market_date) AS max_d
                    FROM biz.asset_market_daily m
                    LEFT JOIN biz.asset_perf_daily p
                        ON p.asset_id = m.asset_id
                       AND p.perf_date = %s
                       AND p.source_code = 'cmc'
                    WHERE m.source_code IN ('cmc', 'cmc_historical')
                      AND m.price_usd > 0
                      AND p.asset_id IS NULL   -- 当日未算过
                    GROUP BY m.asset_id
                    HAVING COUNT(*) >= %s
                    ORDER BY m.asset_id
                    """,
                    (today, args.min_days),
                )
                rows = cur.fetchall()
                if args.limit > 0:
                    rows = rows[: args.limit]

            print(f"[perf] 候选资产: {len(rows)}")

            written = 0
            skipped = 0
            CHUNK = 500  # 每块处理的资产数，避免单次大查询/连接超时

            # 按资产分块：每块查价格序列 + 批量写入，断连可从最后一块续跑
            for chunk_start in range(0, len(rows), CHUNK):
                chunk = rows[chunk_start: chunk_start + CHUNK]
                chunk_ids = [r[0] for r in chunk]
                by_asset: dict[int, list[tuple]] = {}
                try:
                    with get_connection(settings.database_url) as c2:
                        with c2.cursor() as cur2:
                            cur2.execute(
                                """
                                SELECT asset_id, market_date, price_usd
                                FROM biz.asset_market_daily
                                WHERE source_code IN ('cmc', 'cmc_historical')
                                  AND price_usd > 0
                                  AND asset_id = ANY(%s)
                                ORDER BY asset_id, market_date ASC
                                """,
                                (chunk_ids,),
                            )
                            for asset_id, market_date, price in cur2.fetchall():
                                by_asset.setdefault(asset_id, []).append((market_date, price))
                except Exception as e:
                    print(f"[perf] 分块 {chunk_start} 查询失败（跳过，可重跑续）：{e}", file=sys.stderr)
                    skipped += len(chunk)
                    continue

                batch: list[tuple] = []
                for asset_id, n, min_d, max_d in chunk:
                    prices = by_asset.get(asset_id, [])
                    if len(prices) < args.min_days:
                        skipped += 1
                        continue

                    ath_price = None
                    ath_date = None
                    atl_price = None
                    atl_date = None
                    for d, p in prices:
                        if ath_price is None or p > ath_price:
                            ath_price = p
                            ath_date = d
                        if atl_price is None or p < atl_price:
                            atl_price = p
                            atl_date = d

                    # 当前价 = 最新一天
                    latest_price = prices[-1][1]
                    drawdown = None
                    if ath_price and ath_price > 0 and latest_price is not None:
                        drawdown = round((latest_price - ath_price) / ath_price * 100, 4)

                    if args.dry_run:
                        if written < 5:
                            print(f"  asset_id={asset_id} n={n} "
                                  f"ATH={ath_price:.4f}({ath_date}) ATL={atl_price:.4f}({atl_date}) "
                                  f"latest={latest_price:.4f} drawdown={drawdown}%")
                        written += 1
                        continue

                    batch.append(
                        (
                            asset_id,
                            today,
                            "cmc",
                            ath_price,
                            ath_date,
                            atl_price,
                            atl_date,
                            drawdown,
                            json.dumps(
                                {
                                    "computed_from": "asset_market_daily",
                                    "min_date": str(min_d),
                                    "max_date": str(max_d),
                                    "data_points": n,
                                },
                                ensure_ascii=False,
                            ),
                        )
                    )
                    written += 1

                if batch:
                    try:
                        with get_connection(settings.database_url) as c3:
                            execute_many(c3, upsert_sql, batch)
                            c3.commit()
                    except Exception as e:
                        print(f"[perf] 分块 {chunk_start} 写入失败: {e}", file=sys.stderr)
                        continue
                if written and written % 500 == 0:
                    print(f"[perf] 已写入 {written} 条...", flush=True)

    if args.dry_run:
        print(f"[perf] DRY RUN: 将写入 {written} 条（跳过 {skipped}）")
    else:
        print(f"[perf] 完成：写入 {written} 条（跳过 {skipped}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())