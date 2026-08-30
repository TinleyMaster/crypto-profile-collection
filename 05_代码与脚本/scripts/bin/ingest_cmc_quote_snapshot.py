from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ingest CMC latest listings quote snapshot into src_cmc.cmc_asset_quote_snapshot."
    )
    parser.add_argument(
        "--top",
        type=int,
        default=1000,
        help="Number of top coins by market cap to ingest. Default: 1000",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=5000,
        help="Page size per API call. CMC max is 5000. Default: 5000",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and parse only, do not write database.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    from crypto_research.clients.cmc_client import CMCClient
    from crypto_research.config import get_settings
    from crypto_research.db.upsert import load_sql
    from crypto_research.parsers.cmc_quote_snapshot import parse_cmc_quote_snapshot_payload
    from crypto_research.utils.hash_utils import md5_text
    from crypto_research.utils.json_utils import stable_json_dumps

    settings = get_settings(require_database=not args.dry_run)
    client = CMCClient(settings)

    all_rows: list[dict] = []
    all_raw_responses: list[tuple[str, str, str]] = []  # (page_key, payload_text, payload_hash)
    fetched_at = datetime.now(timezone.utc).isoformat()

    # Paginate through listings
    start = 1
    remaining = args.top
    page_size = min(args.page_size, 5000)

    while remaining > 0:
        limit = min(remaining, page_size)
        payload = client.get_listings_latest(start=start, limit=limit)
        payload_text = stable_json_dumps(payload)
        payload_hash = md5_text(payload_text)
        page_key = f"start={start},limit={limit}"
        all_raw_responses.append((page_key, payload_text, payload_hash))

        data = payload.get("data") or []
        if not data:
            break

        parsed = parse_cmc_quote_snapshot_payload(payload, raw_response_id=None)
        all_rows.extend(parsed)

        remaining -= len(data)
        start += len(data)

        if len(data) < limit:
            break

    if args.dry_run:
        print(
            json.dumps(
                {
                    "mode": "dry-run",
                    "top": args.top,
                    "row_count": len(all_rows),
                    "fetched_at": fetched_at,
                    "first_row": all_rows[0] if all_rows else None,
                    "last_row": all_rows[-1] if all_rows else None,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0

    load_sql("sys/insert_ingest_run.sql")
    insert_ingest_run_sql = load_sql("sys/insert_ingest_run.sql")
    finish_ingest_run_sql = load_sql("sys/finish_ingest_run.sql")
    insert_raw_sql = load_sql("raw/insert_api_response.sql")
    upsert_quote_sql = load_sql("src_cmc/upsert_cmc_quote_snapshot.sql")

    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required unless --dry-run is used")

    from crypto_research.db.conn import get_connection
    from crypto_research.db.upsert import execute_many, fetch_one

    with get_connection(settings.database_url) as conn:
        run_row = fetch_one(
            conn,
            insert_ingest_run_sql,
            (
                "cmc",
                "cmc_listings_latest",
                "WF_CMC_QUOTE_SNAPSHOT",
                json.dumps(
                    {
                        "top": args.top,
                    },
                    ensure_ascii=False,
                ),
                f"{settings.cmc_base_url}/v1/cryptocurrency/listings/latest",
            ),
        )
        run_id = run_row["run_id"]

        try:
            # Insert raw responses (one per page) and collect raw_response_ids
            raw_response_ids: list[int] = []
            for page_key, payload_text, payload_hash in all_raw_responses:
                raw_row = fetch_one(
                    conn,
                    insert_raw_sql,
                    (
                        run_id,
                        "cmc",
                        "cmc_listings_latest",
                        page_key,
                        None,
                        "page:listings_latest",
                        payload_text,
                        payload_hash,
                        fetched_at,
                    ),
                )
                raw_response_ids.append(raw_row["response_id"])

            # Assign raw_response_id to each row (first page = first response_id, etc.)
            # Since we parse per-page, re-parse with correct raw_response_id
            # Simpler: just use the first raw_response_id for all rows
            primary_raw_id = raw_response_ids[0] if raw_response_ids else None

            # 过滤掉不在 cmc_asset_map 中的新币，避免外键失败导致整批回滚
            with conn.cursor() as cur:
                all_cmc_ids = [row["cmc_id"] for row in all_rows]
                cur.execute(
                    "SELECT cmc_id FROM src_cmc.cmc_asset_map WHERE cmc_id = ANY(%s)",
                    (all_cmc_ids,),
                )
                valid_ids = {r[0] for r in cur.fetchall()}

            skipped_new = 0
            filtered_rows = []
            for row in all_rows:
                if row["cmc_id"] in valid_ids:
                    filtered_rows.append(row)
                else:
                    skipped_new += 1

            # ═══ RC-1: 价格尖刺校验 ═══
            # 为每个 cmc_id 获取参考价（迭代稳健中位数），检测 >10× 偏离的异常
            spike_threshold = 10.0  # 偏离倍数阈值
            anomaly_count = 0
            
            with conn.cursor() as cur:
                # 批量获取近 90 天的迭代稳健中位数作为参考
                # 第一遍：全量中位数；第二遍：剔除 [0.2*m0, 5*m0] 范围外的点后再取中位数
                cur.execute("""
                    WITH full_median AS (
                        SELECT 
                            asset_id,
                            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY price_usd) AS m0
                        FROM biz.asset_market_daily
                        WHERE source_code = 'cmc'
                          AND market_date >= CURRENT_DATE - INTERVAL '90 days'
                          AND price_usd > 0
                          AND (is_anomaly IS NOT TRUE OR is_anomaly IS NULL)
                        GROUP BY asset_id
                        HAVING COUNT(*) >= 5
                    ),
                    trimmed_median AS (
                        SELECT 
                            f.asset_id,
                            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY d.price_usd) AS m1
                        FROM full_median f
                        JOIN biz.asset_market_daily d 
                            ON d.asset_id = f.asset_id 
                            AND d.source_code = 'cmc'
                            AND d.market_date >= CURRENT_DATE - INTERVAL '90 days'
                            AND d.price_usd > 0
                            AND (d.is_anomaly IS NOT TRUE OR d.is_anomaly IS NULL)
                            AND d.price_usd >= f.m0 * 0.2
                            AND d.price_usd <= f.m0 * 5
                        GROUP BY f.asset_id
                        HAVING COUNT(*) >= 3
                    )
                    SELECT asset_id, m1 AS median_price
                    FROM trimmed_median
                """)
                median_map = {r['asset_id']: float(r['median_price']) for r in cur.fetchall() if r['median_price']}
                
                # 获取 asset_id 到 cmc_id 的映射
                if median_map:
                    cur.execute("""
                        SELECT asm.source_asset_key::INT AS cmc_id, asm.asset_id
                        FROM core.asset_source_map asm
                        WHERE asm.source_code = 'cmc'
                          AND asm.asset_id = ANY(%s)
                    """, (list(median_map.keys()),))
                    cmc_to_asset = {r['cmc_id']: r['asset_id'] for r in cur.fetchall()}
                else:
                    cmc_to_asset = {}

            # 标记异常价格
            for row in filtered_rows:
                cmc_id = row["cmc_id"]
                price = row.get("price_usd")
                pct_24h = row.get("percent_change_24h")
                
                if price is None or price <= 0:
                    row["is_anomaly"] = False
                    continue
                
                asset_id = cmc_to_asset.get(cmc_id)
                ref_price = median_map.get(asset_id) if asset_id else None
                
                # 如果没有历史中位数，用 24h 涨跌幅反推昨收价（仅作兜底）
                if ref_price is None and pct_24h is not None:
                    if pct_24h != -100:
                        ref_price = price / (1 + pct_24h / 100)
                
                is_anomaly = False
                if ref_price and ref_price > 0:
                    ratio = price / ref_price
                    # 偏离 >10× → 价格异常（不再用 pct_24h 卡门槛）
                    if ratio > spike_threshold:
                        is_anomaly = True
                        anomaly_count += 1
                        print(f"[spike] cmc_id={cmc_id} price={price:.6f} ref={ref_price:.6f} ratio={ratio:.1f}x → 标记异常", file=sys.stderr)
                    elif ratio < 1/spike_threshold:
                        is_anomaly = True
                        anomaly_count += 1
                        print(f"[spike] cmc_id={cmc_id} price={price:.6f} ref={ref_price:.6f} ratio={ratio:.4f}x → 标记异常", file=sys.stderr)
                
                row["is_anomaly"] = is_anomaly

            if anomaly_count > 0:
                print(f"[spike] 本轮检测到 {anomaly_count} 条异常价格，已标记 is_anomaly", file=sys.stderr)

            row_params = [
                (
                    row["cmc_id"],
                    row["quote_time"],
                    row["price_usd"],
                    row["market_cap"],
                    row["fdv"],
                    row["volume_24h"],
                    row["circulating_supply"],
                    row["total_supply"],
                    row["max_supply"],
                    row["percent_change_1h"],
                    row["percent_change_24h"],
                    row["percent_change_7d"],
                    row["percent_change_30d"],
                    row["market_cap_dominance"],
                    primary_raw_id,
                    row.get("is_anomaly", False),
                )
                for row in filtered_rows
            ]
            execute_many(conn, upsert_quote_sql, row_params)

            fetch_one(
                conn,
                finish_ingest_run_sql,
                (
                    "success",
                    200,
                    len(all_rows),
                    len(all_rows),
                    0,
                    None,
                    run_id,
                ),
            )

            print(
                json.dumps(
                    {
                        "status": "success",
                        "run_id": run_id,
                        "row_count": len(filtered_rows),
                        "skipped_new_coins": skipped_new,
                        "top": args.top,
                        "fetched_at": fetched_at,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        except Exception as exc:
            conn.rollback()
            fetch_one(
                conn,
                finish_ingest_run_sql,
                (
                    "failed",
                    None,
                    None,
                    None,
                    None,
                    str(exc),
                    run_id,
                ),
            )
            raise


if __name__ == "__main__":
    raise SystemExit(main())
