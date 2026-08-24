"""从 CMC 历史行情 API 回填日级行情数据到 biz.asset_market_daily。

解决 P1-3：market-history 仅 3 天问题。
通过 CMC 专业版 /v3/cryptocurrency/quotes/historical 接口批量拉取历史日频数据，
直接写入 biz.asset_market_daily（source_code = 'cmc_historical'）。

特点：
- 批量拉取：每次最多 100 个币种（CMC API 限制）
- 幂等写入：ON CONFLICT 更新数值字段
- 只回填已有资产：从 biz.coin_basic 取有 cmc_id 的资产
- 支持按市值排名过滤：默认 top 500，避免浪费 API 额度

用法：
    python ingest_cmc_historical_quotes.py --days 90          # 回填最近 90 天，top 500
    python ingest_cmc_historical_quotes.py --days 365 --top 200  # 回填最近 1 年，top 200
    python ingest_cmc_historical_quotes.py --dry-run          # 预览，不写入
    python ingest_cmc_historical_quotes.py --asset-id 1       # 只回填某个资产（asset_id）
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402
from crypto_research.clients.cmc_client import CMCClient  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill historical daily quotes from CMC API into biz.asset_market_daily."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=90,
        help="Number of days to backfill. Default: 90.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=500,
        help="Only backfill top N assets by CMC rank. Default: 500.",
    )
    parser.add_argument(
        "--asset-id",
        type=int,
        default=None,
        help="Backfill a single asset by asset_id (overrides --top).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Number of CMC IDs per API call (max 100). Default: 50.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview only, do not write to DB.",
    )
    return parser


def ensure_source_platform(conn) -> None:
    """确保 asset_market_daily.source_code 外键中存在 cmc_historical。"""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO sys.source_platform (platform_code, platform_name, base_url, description, is_active)
            VALUES ('cmc_historical', 'CoinMarketCap Historical Quotes', 'https://coinmarketcap.com', 'CMC 专业版历史行情 API 回填', TRUE)
            ON CONFLICT (platform_code) DO NOTHING
        """)


def fetch_target_assets(conn, top_n: int | None, asset_id: int | None) -> list[tuple[int, int]]:
    """获取需要回填的资产列表，返回 [(asset_id, cmc_id), ...]。

    优先按 CMC 排名取 top N；若指定 asset_id 则只取该资产。
    """
    with conn.cursor() as cur:
        if asset_id is not None:
            cur.execute("""
                SELECT asset_id, cmc_id
                FROM biz.coin_basic
                WHERE asset_id = %s AND cmc_id IS NOT NULL
                LIMIT 1
            """, (asset_id,))
        else:
            cur.execute("""
                SELECT cb.asset_id, cb.cmc_id
                FROM biz.coin_basic cb
                JOIN src_cmc.cmc_asset_map cam ON cam.cmc_id = cb.cmc_id
                WHERE cb.cmc_id IS NOT NULL
                  AND cam.rank_num IS NOT NULL
                ORDER BY cam.rank_num ASC
                LIMIT %s
            """, (top_n,))
        rows = cur.fetchall()
    return [(r[0], r[1]) for r in rows]


def parse_historical_quotes(
    payload: dict,
    asset_id_map: dict[int, int],  # cmc_id -> asset_id
) -> list[dict]:
    """解析 CMC 历史行情响应，返回 asset_market_daily 行列表。

    CMC v3 quotes/historical 返回结构：
    {
      "data": {
        "1": {  // cmc_id
          "id": 1,
          "name": "Bitcoin",
          "symbol": "BTC",
          "quotes": [
            {
              "timestamp": "2026-01-01T00:00:00.000Z",
              "quote": {
                "USD": {
                  "price": 42000,
                  "market_cap": 820000000000,
                  "volume_24h": 28000000000,
                  "circulating_supply": 19500000,
                  "total_supply": 21000000,
                  "percent_change_24h": 2.5,
                  "percent_change_7d": 5.3,
                  ...
                }
              }
            },
            ...
          ]
        },
        ...
      }
    }
    """
    data = payload.get("data") or {}
    rows: list[dict] = []

    for cmc_id_str, coin_data in data.items():
        cmc_id = int(cmc_id_str)
        asset_id = asset_id_map.get(cmc_id)
        if asset_id is None:
            continue

        quotes = coin_data.get("quotes") or []
        for quote_entry in quotes:
            timestamp_str = quote_entry.get("timestamp")
            if not timestamp_str:
                continue
            try:
                quote_time = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue

            quote_usd = (quote_entry.get("quote") or {}).get("USD") or {}
            market_date = quote_time.date()

            rows.append({
                "asset_id": asset_id,
                "market_date": market_date,
                "source_code": "cmc_historical",
                "price_usd": quote_usd.get("price"),
                "market_cap": quote_usd.get("market_cap"),
                "fdv": quote_usd.get("fully_diluted_market_cap"),
                "circulating_supply": quote_usd.get("circulating_supply"),
                "total_supply": quote_usd.get("total_supply"),
                "volume_24h": quote_usd.get("volume_24h"),
                "change_24h": quote_usd.get("percent_change_24h"),
                "change_7d": quote_usd.get("percent_change_7d"),
            })

    return rows


def insert_daily_quotes(conn, rows: list[dict]) -> int:
    """批量写入 asset_market_daily，幂等更新。"""
    if not rows:
        return 0

    sql = """
        INSERT INTO biz.asset_market_daily
            (asset_id, market_date, source_code, price_usd,
             market_cap, fdv, circulating_supply, total_supply,
             volume_24h, change_24h, change_7d, raw_ref)
        VALUES (
            %(asset_id)s, %(market_date)s, %(source_code)s, %(price_usd)s,
            %(market_cap)s, %(fdv)s, %(circulating_supply)s, %(total_supply)s,
            %(volume_24h)s, %(change_24h)s, %(change_7d)s,
            '{"source": "cmc_historical_api"}'::jsonb
        )
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
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
        return cur.rowcount


def backfill_historical_quotes(
    days: int,
    top_n: int,
    asset_id: int | None,
    batch_size: int,
    dry_run: bool,
) -> dict:
    """执行历史行情回填，返回统计信息。"""
    settings = get_settings(require_database=True)
    cmc = CMCClient(settings)

    # 计算时间范围
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days)
    time_start = start_dt.strftime("%Y-%m-%d")
    time_end = end_dt.strftime("%Y-%m-%d")

    print(f"[CMC] Historical quotes backfill")
    print(f"[CMC] Date range: {time_start} ~ {time_end} ({days} days)")

    with get_connection(settings.database_url) as conn:
        # 注册 source_code 外键
        ensure_source_platform(conn)

        # 获取目标资产
        assets = fetch_target_assets(conn, top_n, asset_id)
        if not assets:
            return {"error": "No target assets found"}

        print(f"[CMC] Target assets: {len(assets)}")
        if dry_run:
            return {
                "dry_run": True,
                "assets": len(assets),
                "days": days,
                "date_from": time_start,
                "date_to": time_end,
            }

        # 构建 cmc_id -> asset_id 映射
        asset_id_map = {cmc_id: aid for aid, cmc_id in assets}

        # 分批调用 CMC API
        batch_size = min(batch_size, 100)  # CMC API 单次最多 100 个
        total_rows = 0
        total_batches = (len(assets) + batch_size - 1) // batch_size

        for batch_idx in range(total_batches):
            batch_start = batch_idx * batch_size
            batch_end = min(batch_start + batch_size, len(assets))
            batch_cmc_ids = [cmc_id for _, cmc_id in assets[batch_start:batch_end]]

            print(f"[CMC] Batch {batch_idx + 1}/{total_batches}: {len(batch_cmc_ids)} assets")

            try:
                resp = cmc.get_quotes_historical(
                    ids=batch_cmc_ids,
                    time_start=time_start,
                    time_end=time_end,
                    interval="daily",
                )
            except Exception as e:
                print(f"[CMC] Batch {batch_idx + 1} failed: {e}")
                # 失败不中断，继续下一批
                time.sleep(2)
                continue

            # 解析并写入
            rows = parse_historical_quotes(resp, asset_id_map)
            if rows:
                affected = insert_daily_quotes(conn, rows)
                conn.commit()
                total_rows += affected
                print(f"[CMC]   Inserted/updated {affected} rows ({len(rows)} quotes parsed)")
            else:
                print(f"[CMC]   No quotes parsed")

            # 限速：避免触发 CMC rate limit
            time.sleep(1.5)

        # 验证统计
        with conn.cursor() as cur:
            cur.execute("""
                SELECT count(*), min(market_date), max(market_date), count(DISTINCT asset_id)
                FROM biz.asset_market_daily
                WHERE source_code = 'cmc_historical'
            """)
            row = cur.fetchone()

    return {
        "total_rows_inserted": total_rows,
        "historical_total": row[0],
        "historical_date_from": str(row[1]) if row[1] else None,
        "historical_date_to": str(row[2]) if row[2] else None,
        "historical_assets": row[3],
    }


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    result = backfill_historical_quotes(
        days=args.days,
        top_n=args.top,
        asset_id=args.asset_id,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
    )

    if result.get("error"):
        print(f"[ERROR] {result['error']}")
        return 1

    if result.get("dry_run"):
        print(f"[DRY-RUN] Would backfill {result['assets']} assets for {result['days']} days")
        print(f"[DRY-RUN] Date range: {result['date_from']} ~ {result['date_to']}")
    else:
        print(f"[DONE] Inserted/updated: {result['total_rows_inserted']:,} rows")
        print(f"[DONE] Historical total: {result['historical_total']:,} rows ({result['historical_assets']:,} assets)")
        if result.get("historical_date_from"):
            print(f"[DONE] Date range: {result['historical_date_from']} ~ {result['historical_date_to']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
