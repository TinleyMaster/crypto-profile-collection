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
from crypto_research.db.upsert import load_sql, execute_many  # noqa: E402
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
    parser.add_argument(
        "--time-start",
        type=str,
        default=None,
        help="Explicit backfill window start (YYYY-MM-DD). Overrides --days-based range when set.",
    )
    parser.add_argument(
        "--time-end",
        type=str,
        default=None,
        help="Explicit backfill window end (YYYY-MM-DD, exclusive). Requires --time-start.",
    )
    parser.add_argument(
        "--all-assets",
        action="store_true",
        help="Backfill ALL assets with a cmc_id (ignore rank_num limit) for full snapshot density.",
    )
    parser.add_argument(
        "--snapshot-dates",
        type=str,
        default=None,
        help="Comma-separated dates (YYYY-MM-DD) to ALSO upsert into src_cmc.cmc_asset_quote_snapshot. "
             "If unset, only biz.asset_market_daily is written (backward compatible).",
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


def fetch_target_assets(
    conn, top_n: int | None, asset_id: int | None, all_assets: bool = False
) -> list[tuple[int, int]]:
    """获取需要回填的资产列表，返回 [(asset_id, cmc_id), ...]。

    优先按 CMC 排名取 top N；若指定 asset_id 则只取该资产；all_assets 则取全部。
    """
    with conn.cursor() as cur:
        if asset_id is not None:
            cur.execute("""
                SELECT asset_id, cmc_id
                FROM biz.coin_basic
                WHERE asset_id = %s AND cmc_id IS NOT NULL
                LIMIT 1
            """, (asset_id,))
        elif all_assets:
            # A 补：全量资产，跳过 rank_num 限制，逼近正常快照日 ~8000 密度
            cur.execute("""
                SELECT cb.asset_id, cb.cmc_id
                FROM biz.coin_basic cb
                WHERE cb.cmc_id IS NOT NULL
            """)
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
    snapshot_dates: set[str] | None = None,  # A 补：仅这些日期生成快照行
) -> tuple[list[dict], list[dict]]:
    """返回 (daily_rows, snapshot_rows)。snapshot_rows 为空当 snapshot_dates=None。

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
    daily_rows: list[dict] = []
    snapshot_rows: list[dict] = []

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
            market_date_str = market_date.isoformat()

            # ── 原有 daily 行（不变）──
            daily_rows.append({
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

            # ── A 补：快照行（仅缺口日）──
            if snapshot_dates is not None and market_date_str in snapshot_dates:
                snapshot_rows.append({
                    "cmc_id": cmc_id,
                    "quote_time": quote_time,  # 用历史快照精确 timestamp（00:00Z 量级）
                    "price_usd": quote_usd.get("price"),
                    "market_cap": quote_usd.get("market_cap"),
                    "fdv": quote_usd.get("fully_diluted_market_cap"),
                    "volume_24h": quote_usd.get("volume_24h"),
                    "circulating_supply": quote_usd.get("circulating_supply"),
                    "total_supply": quote_usd.get("total_supply"),
                    "max_supply": quote_usd.get("max_supply"),
                    "percent_change_1h": quote_usd.get("percent_change_1h"),
                    "percent_change_24h": quote_usd.get("percent_change_24h"),
                    "percent_change_7d": quote_usd.get("percent_change_7d"),
                    "percent_change_30d": quote_usd.get("percent_change_30d"),
                    "market_cap_dominance": quote_usd.get("market_cap_dominance"),
                })

    return daily_rows, snapshot_rows


def insert_daily_quotes(conn, rows: list[dict]) -> int:
    """批量写入 asset_market_daily，幂等更新。

    range guard：change_24h / change_7d 超 1e10 的脏值跳过，
    防 NUMERIC(18,8) 溢出（同 0c2a639 修复思路）。
    """
    if not rows:
        return 0

    PCT_LIMIT = 1e10
    clean_rows = []
    skipped = 0
    for r in rows:
        bad = False
        for k in ("change_24h", "change_7d"):
            v = r.get(k)
            if v is not None and abs(float(v)) >= PCT_LIMIT:
                bad = True
                break
        if bad:
            skipped += 1
            continue
        clean_rows.append(r)
    if skipped:
        print(f"[CMC]   daily skipped {skipped} rows (range guard)")
    if not clean_rows:
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
        cur.executemany(sql, clean_rows)
        return cur.rowcount


def upsert_snapshot(conn, rows: list[dict]) -> int:
    """批量 upsert 进 src_cmc.cmc_asset_quote_snapshot。幂等（ON CONFLICT cmc_id,quote_time）。

    复用 sql/src_cmc/upsert_cmc_quote_snapshot.sql 的 16 列顺序。
    range guard：abs(percent_change_*)>=1e10 或 market_cap_dominance>=100 跳过，
    防脏值打爆 NUMERIC(18,8)（同 0c2a639 修复）。
    """
    if not rows:
        return 0

    PCT_LIMIT = 1e10
    DOM_LIMIT = 100.0
    upsert_sql = load_sql("src_cmc/upsert_cmc_quote_snapshot.sql")

    params = []
    skipped = 0
    for r in rows:
        bad = False
        for k in ("percent_change_1h", "percent_change_24h",
                  "percent_change_7d", "percent_change_30d"):
            v = r.get(k)
            if v is not None and abs(float(v)) >= PCT_LIMIT:
                bad = True
                break
        dom = r.get("market_cap_dominance")
        if dom is not None and float(dom) >= DOM_LIMIT:
            bad = True
        if bad:
            skipped += 1
            continue
        params.append((
            r["cmc_id"], r["quote_time"], r["price_usd"], r["market_cap"],
            r["fdv"], r["volume_24h"], r["circulating_supply"],
            r["total_supply"], r["max_supply"],
            r["percent_change_1h"], r["percent_change_24h"],
            r["percent_change_7d"], r["percent_change_30d"],
            r["market_cap_dominance"],
            None,        # raw_response_id：historical 不存 raw，置 NULL
            False,       # is_anomaly：无 median_map，不参与校验
        ))

    if not params:
        return 0
    if skipped:
        print(f"[CMC]   snapshot skipped {skipped} rows (range guard)")
    execute_many(conn, upsert_sql, params)
    return len(params)


def backfill_historical_quotes(
    days: int,
    top_n: int,
    asset_id: int | None,
    batch_size: int,
    dry_run: bool,
    all_assets: bool = False,
    time_start: str | None = None,
    time_end: str | None = None,
    snapshot_dates: set[str] | None = None,
) -> dict:
    """执行历史行情回填，返回统计信息。"""
    settings = get_settings(require_database=True)
    cmc = CMCClient(settings)

    # 时间范围：显式 --time-start/--time-end 优先，否则 --days
    if time_start:
        ts = time_start
        te = time_end or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    else:
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=days)
        ts = start_dt.strftime("%Y-%m-%d")
        te = end_dt.strftime("%Y-%m-%d")

    print(f"[CMC] Historical quotes backfill")
    print(f"[CMC] Date range: {ts} ~ {te}")

    with get_connection(settings.database_url) as conn:
        # 注册 source_code 外键
        ensure_source_platform(conn)

        # 获取目标资产
        assets = fetch_target_assets(conn, top_n, asset_id, all_assets=all_assets)
        if not assets:
            return {"error": "No target assets found"}

        print(f"[CMC] Target assets: {len(assets)}"
              + ("  [ALL-ASSETS]" if all_assets else ""))
        if dry_run:
            return {
                "dry_run": True,
                "assets": len(assets),
                "days": days,
                "date_from": ts,
                "date_to": te,
                "snapshot_dates": sorted(snapshot_dates) if snapshot_dates else None,
            }

        # 构建 cmc_id -> asset_id 映射
        asset_id_map = {cmc_id: aid for aid, cmc_id in assets}

        # 分批调用 CMC API
        batch_size = min(batch_size, 100)  # CMC API 单次最多 100 个
        total_daily = 0
        total_snapshot = 0
        total_batches = (len(assets) + batch_size - 1) // batch_size

        for batch_idx in range(total_batches):
            batch_start = batch_idx * batch_size
            batch_end = min(batch_start + batch_size, len(assets))
            batch_cmc_ids = [cmc_id for _, cmc_id in assets[batch_start:batch_end]]

            print(f"[CMC] Batch {batch_idx + 1}/{total_batches}: {len(batch_cmc_ids)} assets")

            try:
                resp = cmc.get_quotes_historical(
                    ids=batch_cmc_ids,
                    time_start=ts,
                    time_end=te,
                    interval="daily",
                )
            except Exception as e:
                print(f"[CMC] Batch {batch_idx + 1} failed: {e}")
                # 失败不中断，继续下一批
                time.sleep(2)
                continue

            # 解析并写入
            daily_rows, snapshot_rows = parse_historical_quotes(
                resp, asset_id_map, snapshot_dates=snapshot_dates)
            if daily_rows:
                affected = insert_daily_quotes(conn, daily_rows)
                total_daily += affected
            if snapshot_rows:
                aff = upsert_snapshot(conn, snapshot_rows)
                total_snapshot += aff
            conn.commit()
            print(f"[CMC]   daily +{len(daily_rows)} / snapshot +{len(snapshot_rows)} parsed")

            # 限速：避免触发 CMC rate limit
            time.sleep(1.5)

        # 验证统计
        with conn.cursor() as cur:
            cur.execute("""
                SELECT count(*), min(market_date), max(market_date), count(DISTINCT asset_id)
                FROM biz.asset_market_daily
                WHERE source_code = 'cmc_historical'
            """)
            drow = cur.fetchone()
            snap_stat = None
            if snapshot_dates:
                cur.execute("""
                    SELECT count(*), count(DISTINCT cmc_id)
                    FROM src_cmc.cmc_asset_quote_snapshot
                    WHERE DATE(quote_time) = ANY(%s)
                """, (list(snapshot_dates),))
                snap_stat = cur.fetchone()

    result = {
        "total_daily_inserted": total_daily,
        "total_snapshot_upserted": total_snapshot,
        "historical_total": drow[0],
        "historical_date_from": str(drow[1]) if drow[1] else None,
        "historical_date_to": str(drow[2]) if drow[2] else None,
        "historical_assets": drow[3],
    }
    if snap_stat:
        result["snapshot_rows"] = snap_stat[0]
        result["snapshot_assets"] = snap_stat[1]
    return result


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    snapshot_dates = (
        {s.strip() for s in args.snapshot_dates.split(",") if s.strip()}
        if args.snapshot_dates else None
    )

    result = backfill_historical_quotes(
        days=args.days,
        top_n=args.top,
        asset_id=args.asset_id,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
        all_assets=args.all_assets,
        time_start=args.time_start,
        time_end=args.time_end,
        snapshot_dates=snapshot_dates,
    )

    if result.get("error"):
        print(f"[ERROR] {result['error']}")
        return 1

    if result.get("dry_run"):
        print(f"[DRY-RUN] Would backfill {result['assets']} assets for {result['days']} days")
        print(f"[DRY-RUN] Date range: {result['date_from']} ~ {result['date_to']}")
        if result.get("snapshot_dates"):
            print(f"[DRY-RUN] Snapshot dates: {', '.join(result['snapshot_dates'])}")
    else:
        print(f"[DONE] Daily inserted/updated: {result['total_daily_inserted']:,} rows")
        if result.get("total_snapshot_upserted"):
            print(f"[DONE] Snapshot upserted: {result['total_snapshot_upserted']:,} rows")
        print(f"[DONE] Historical total: {result['historical_total']:,} rows ({result['historical_assets']:,} assets)")
        if result.get("historical_date_from"):
            print(f"[DONE] Date range: {result['historical_date_from']} ~ {result['historical_date_to']}")
        if result.get("snapshot_rows"):
            print(f"[DONE] Snapshot gap filled: {result['snapshot_rows']:,} rows, {result['snapshot_assets']:,} assets")

    return 0


if __name__ == "__main__":
    sys.exit(main())
