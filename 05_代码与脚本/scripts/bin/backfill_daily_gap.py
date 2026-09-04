"""日价缺口自动检测+回填（RT-BACKTEST-D1-001 改动 2）

自动扫描最近 N 天的 biz.asset_market_daily，检测缺失日期并回填。
回填完成后在 sys.task 生成任务卡，方便日后追溯。

数据源优先级：
1. CMC 历史行情 API（source_code='cmc_historical'）
2. 从已有快照重新 ETL（若 cmc_asset_quote_snapshot 有数据但 asset_market_daily 缺失）

用法：
    python backfill_daily_gap.py                    # 自动检测+回填最近 30 天缺口
    python backfill_daily_gap.py --lookback 60      # 扫描最近 60 天
    python backfill_daily_gap.py --dry-run          # 预览，不写入
    python backfill_daily_gap.py --top 500          # 只回填 top 500 资产
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection


# ── 缺口检测 ──

def detect_missing_dates(conn, lookback_days: int, min_assets: int = 100) -> list[date]:
    """扫描最近 lookback_days 天，返回缺失日期列表。

    判定逻辑（双阈值）：
    1. 绝对阈值：资产数 < min_assets → 直接视为缺失（如 0 条）
    2. 相对阈值：资产数 < 相邻7日中位数 × 0.90 → 视为部分缺失（如6888 vs ~7968）
    排除"今天"（数据尚未产出是正常的）
    """
    today = date.today()
    cutoff = today - timedelta(days=1)  # 排除今天

    with conn.cursor() as cur:
        cur.execute("""
            SELECT market_date, COUNT(DISTINCT asset_id) AS assets
            FROM biz.asset_market_daily
            WHERE market_date >= %s AND market_date <= %s
            GROUP BY market_date
        """, (cutoff - timedelta(days=lookback_days), cutoff))
        coverage = {row[0]: row[1] for row in cur.fetchall()}

    # 完整日期序列
    expected = [cutoff - timedelta(days=i) for i in range(lookback_days)]

    # 计算每个日期的相邻7日中位数作为基准
    def _neighbor_median(d: date) -> int:
        neighbors = [coverage.get(d + timedelta(days=offset), 0) for offset in range(-3, 4)]
        neighbors = [n for n in neighbors if n > 0]
        if not neighbors:
            return 0
        neighbors.sort()
        return neighbors[len(neighbors) // 2]

    missing = []
    for d in expected:
        count = coverage.get(d, 0)
        if count < min_assets:
            # 绝对缺失（如 0 条）
            missing.append(d)
        else:
            # 相对缺失：低于相邻中位数的90%
            median = _neighbor_median(d)
            if median > 0 and count < median * 0.90:
                missing.append(d)

    missing.sort()
    return missing, coverage


# ── 回填逻辑 ──

def ensure_source_platform(conn) -> None:
    """确保 source_code 外键存在。"""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO sys.source_platform (platform_code, platform_name, base_url, description, is_active)
            VALUES 
                ('cmc_historical', 'CoinMarketCap Historical Quotes', 'https://coinmarketcap.com', 'CMC 专业版历史行情 API 回填', TRUE),
                ('binance_klines', 'Binance Klines', 'https://api.binance.com', 'Binance 历史日K线回填', TRUE)
            ON CONFLICT (platform_code) DO NOTHING
        """)


def fetch_assets_for_backfill(conn, top_n: int = 8000) -> list[tuple[int, int]]:
    """获取需要回填的资产列表，返回 [(asset_id, cmc_id), ...]。"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT asm.asset_id, asm.source_asset_key::INT AS cmc_id
            FROM core.asset_source_map asm
            WHERE asm.source_code = 'cmc'
              AND asm.source_asset_key IS NOT NULL
              AND asm.source_asset_key ~ '^[0-9]+$'
            ORDER BY asm.asset_id
            LIMIT %s
        """, (top_n,))
        return [(row[0], row[1]) for row in cur.fetchall()]


def backfill_via_cmc_historical(
    conn,
    target_dates: list[date],
    assets: list[tuple[int, int]],
    batch_size: int = 50,
    dry_run: bool = False,
    log=None,
    max_retries: int = 3,
    base_delay: float = 2.0,
) -> dict:
    """通过 CMC 历史行情 API 回填指定日期，带指数退避重试。"""
    from crypto_research.clients.cmc_client import CMCClient

    settings = get_settings(require_database=True)
    cmc = CMCClient(settings)
    ensure_source_platform(conn)

    min_date = min(target_dates)
    max_date = max(target_dates)
    time_start = min_date.isoformat()
    time_end = (max_date + timedelta(days=1)).isoformat()

    asset_id_map = {cmc_id: aid for aid, cmc_id in assets}
    batch_size = min(batch_size, 100)
    total_batches = (len(assets) + batch_size - 1) // batch_size
    total_rows = 0
    errors = []
    failed_batches = []  # (batch_idx, cmc_ids) 待重试

    def _log(msg):
        print(msg)
        if log:
            log(msg)

    def _fetch_batch(batch_cmc_ids: list[int], attempt: int = 0) -> dict | None:
        """单次 API 调用，失败返回 None。"""
        try:
            resp = cmc.get_quotes_historical(
                ids=batch_cmc_ids,
                time_start=time_start,
                time_end=time_end,
                interval="daily",
            )
            return resp
        except Exception as e:
            if "429" in str(e) and attempt < max_retries:
                delay = base_delay * (2 ** attempt)  # 指数退避: 2s, 4s, 8s
                _log(f"[CMC]   429 限流，等待 {delay:.0f}s 后重试 ({attempt+1}/{max_retries})")
                time.sleep(delay)
                return _fetch_batch(batch_cmc_ids, attempt + 1)
            return None

    # 第一轮：正常遍历
    for batch_idx in range(total_batches):
        batch_start = batch_idx * batch_size
        batch_end = min(batch_start + batch_size, len(assets))
        batch_cmc_ids = [cmc_id for _, cmc_id in assets[batch_start:batch_end]]

        _log(f"[CMC] Batch {batch_idx + 1}/{total_batches}: {len(batch_cmc_ids)} assets")

        resp = _fetch_batch(batch_cmc_ids)
        if resp is None:
            failed_batches.append((batch_idx, batch_cmc_ids))
            _log(f"[CMC]   Batch {batch_idx + 1} 失败，加入重试队列")
            time.sleep(base_delay)
            continue

        data = resp.get("data") or {}
        rows = []
        for cmc_id_str, coin_data in data.items():
            cmc_id = int(cmc_id_str)
            asset_id = asset_id_map.get(cmc_id)
            if asset_id is None:
                continue

            for quote_entry in (coin_data.get("quotes") or []):
                timestamp_str = quote_entry.get("timestamp")
                if not timestamp_str:
                    continue
                try:
                    quote_time = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    continue

                market_date = quote_time.date()
                if market_date not in target_dates:
                    continue

                quote_usd = (quote_entry.get("quote") or {}).get("USD") or {}
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

        if rows and not dry_run:
            sql = """
                INSERT INTO biz.asset_market_daily
                    (asset_id, market_date, source_code, price_usd,
                     market_cap, fdv, circulating_supply, total_supply,
                     volume_24h, change_24h, change_7d, raw_ref)
                VALUES (
                    %(asset_id)s, %(market_date)s, %(source_code)s, %(price_usd)s,
                    %(market_cap)s, %(fdv)s, %(circulating_supply)s, %(total_supply)s,
                    %(volume_24h)s, %(change_24h)s, %(change_7d)s,
                    '{"source": "cmc_historical_backfill"}'::jsonb
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
            conn.commit()
            total_rows += len(rows)
            _log(f"[CMC]   Inserted {len(rows)} rows")
        elif dry_run:
            total_rows += len(rows)
            _log(f"[CMC]   Would insert {len(rows)} rows (dry-run)")
        else:
            _log(f"[CMC]   No rows for target dates")

        time.sleep(base_delay)

    # 第二轮：重试失败的 batch（更长间隔）
    if failed_batches:
        _log(f"\n[CMC] === 重试 {len(failed_batches)} 个失败 batch ===")
        retry_delay = base_delay * 5  # 重试间隔更长
        still_failed = []

        for batch_idx, batch_cmc_ids in failed_batches:
            _log(f"[CMC] Retry Batch {batch_idx + 1}: {len(batch_cmc_ids)} assets")
            time.sleep(retry_delay)

            resp = _fetch_batch(batch_cmc_ids)
            if resp is None:
                still_failed.append(batch_idx)
                _log(f"[CMC]   Batch {batch_idx + 1} 重试仍失败")
                continue

            data = resp.get("data") or {}
            rows = []
            for cmc_id_str, coin_data in data.items():
                cmc_id = int(cmc_id_str)
                asset_id = asset_id_map.get(cmc_id)
                if asset_id is None:
                    continue
                for quote_entry in (coin_data.get("quotes") or []):
                    timestamp_str = quote_entry.get("timestamp")
                    if not timestamp_str:
                        continue
                    try:
                        quote_time = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                    except (ValueError, AttributeError):
                        continue
                    market_date = quote_time.date()
                    if market_date not in target_dates:
                        continue
                    quote_usd = (quote_entry.get("quote") or {}).get("USD") or {}
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

            if rows and not dry_run:
                sql = """
                    INSERT INTO biz.asset_market_daily
                        (asset_id, market_date, source_code, price_usd,
                         market_cap, fdv, circulating_supply, total_supply,
                         volume_24h, change_24h, change_7d, raw_ref)
                    VALUES (
                        %(asset_id)s, %(market_date)s, %(source_code)s, %(price_usd)s,
                        %(market_cap)s, %(fdv)s, %(circulating_supply)s, %(total_supply)s,
                        %(volume_24h)s, %(change_24h)s, %(change_7d)s,
                        '{"source": "cmc_historical_backfill"}'::jsonb
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
                conn.commit()
                total_rows += len(rows)
                _log(f"[CMC]   Retry inserted {len(rows)} rows")
            elif dry_run:
                total_rows += len(rows)

        if still_failed:
            errors.append(f"{len(still_failed)} batches failed after retry")

    return {
        "total_rows": total_rows,
        "errors": errors,
        "batches": total_batches,
        "failed_batches": len(failed_batches),
        "retry_failed": len(still_failed) if failed_batches else 0,
    }


def backfill_via_binance_klines(
    conn,
    target_dates: list[date],
    top_n: int = 2000,
    dry_run: bool = False,
    log=None,
    request_delay: float = 0.2,
) -> dict:
    """通过 Binance klines API 兜底回填（CMC 不可用时）。

    从 core.asset 取有 canonical_symbol 的资产（按市值降序），
    用 /api/v3/klines 拉历史日收盘价写入 asset_market_daily。
    429/限流时指数退避重试；网络异常不中断循环。
    """
    import requests as req

    def _log(msg):
        print(msg)
        if log:
            log(msg)

    # 获取有 symbol 的资产，按最新市值降序（优先覆盖头部主流币）
    with conn.cursor() as cur:
        cur.execute("""
            SELECT a.asset_id, UPPER(a.canonical_symbol) AS symbol
            FROM core.asset a
            LEFT JOIN LATERAL (
                SELECT amd.market_cap
                FROM biz.asset_market_daily amd
                WHERE amd.asset_id = a.asset_id
                  AND amd.market_cap IS NOT NULL
                ORDER BY amd.market_date DESC
                LIMIT 1
            ) m ON TRUE
            WHERE a.canonical_symbol IS NOT NULL
              AND LENGTH(a.canonical_symbol) BETWEEN 2 AND 10
            ORDER BY m.market_cap DESC NULLS LAST
            LIMIT %s
        """, (top_n,))
        assets = [(row[0], row[1]) for row in cur.fetchall()]

    if not assets:
        return {"skipped": True, "reason": "No assets with Binance symbol found"}

    _log(f"[BINANCE] Assets with symbol (ordered by market_cap): {len(assets)}")

    # 准备日期参数
    min_date = min(target_dates)
    max_date = max(target_dates)
    start_ms = int(datetime.combine(min_date, datetime.min.time()).timestamp() * 1000)
    end_ms = int(datetime.combine(max_date + timedelta(days=1), datetime.min.time()).timestamp() * 1000) - 1

    total_rows = 0
    errors = []
    symbol_map = {}  # asset_id -> symbol
    empty_count = 0
    rate_limited_count = 0
    consecutive_failures = 0
    MAX_CONSECUTIVE_FAILURES = 50

    def _fetch_with_retry(pair: str, max_retries: int = 3) -> tuple[list, bool]:
        """拉取 klines，返回 (klines_list, is_rate_limited)。"""
        nonlocal rate_limited_count
        url = (
            f"https://api.binance.com/api/v3/klines"
            f"?symbol={pair}&interval=1d"
            f"&startTime={start_ms}&endTime={end_ms}&limit=10"
        )
        for attempt in range(max_retries):
            try:
                r = req.get(url, timeout=10)
                if r.status_code == 429:
                    rate_limited_count += 1
                    _log(f"[BINANCE] 429 rate limited ({pair})，等待 {10 * (attempt + 1)}s 重试")
                    time.sleep(10 * (attempt + 1))
                    continue
                if r.status_code == 451:  # 地理风控
                    _log(f"[BINANCE] 451 unavailable for {pair}，跳过")
                    return [], False
                if r.status_code == 418:  # IP ban
                    _log(f"[BINANCE] 418 IP banned，暂停 60s")
                    time.sleep(60)
                    continue
                if r.status_code != 200:
                    return [], False
                return r.json(), False
            except req.exceptions.Timeout:
                if attempt < max_retries - 1:
                    time.sleep(2 * (attempt + 1))
                    continue
                return [], False
            except req.exceptions.ConnectionError as e:
                _log(f"[BINANCE] Connection error ({pair}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(5 * (attempt + 1))
                    continue
                return [], False
            except Exception as e:
                _log(f"[BINANCE] Unexpected error ({pair}): {type(e).__name__}: {e}")
                return [], False
        return [], True

    for idx, (asset_id, symbol) in enumerate(assets):
        pair = f"{symbol}USDT"

        try:
            klines, was_rate_limited = _fetch_with_retry(pair)
            if was_rate_limited:
                if rate_limited_count >= 10:
                    _log(f"[BINANCE] 连续限流 {rate_limited_count} 次，提前终止")
                    break
                continue

            if not klines:
                empty_count += 1
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    _log(f"[BINANCE] 连续 {consecutive_failures} 次无数据，提前终止（可能全部无交易对）")
                    break
                if (idx + 1) % 100 == 0:
                    _log(f"[BINANCE] Processed {idx + 1}/{len(assets)}, success={len(symbol_map)}, empty={empty_count}, rate_limited={rate_limited_count}")
                time.sleep(request_delay)
                continue

            consecutive_failures = 0
            symbol_map[asset_id] = symbol
            rows = []
            for k in klines:
                # k = [open_time, open, high, low, close, volume, close_time, ...]
                open_time_ms = k[0]
                try:
                    close_price = float(k[4])
                    volume = float(k[5])
                except (ValueError, TypeError, IndexError):
                    continue
                market_date = datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc).date()

                if market_date not in target_dates:
                    continue
                if close_price <= 0:
                    continue

                rows.append({
                    "asset_id": asset_id,
                    "market_date": market_date,
                    "source_code": "binance_klines",
                    "price_usd": close_price,
                    "market_cap": None,
                    "fdv": None,
                    "circulating_supply": None,
                    "total_supply": None,
                    "volume_24h": volume,
                    "change_24h": None,
                    "change_7d": None,
                })

            if rows and not dry_run:
                sql = """
                    INSERT INTO biz.asset_market_daily
                        (asset_id, market_date, source_code, price_usd,
                         market_cap, fdv, circulating_supply, total_supply,
                         volume_24h, change_24h, change_7d, raw_ref)
                    VALUES (
                        %(asset_id)s, %(market_date)s, %(source_code)s, %(price_usd)s,
                        %(market_cap)s, %(fdv)s, %(circulating_supply)s, %(total_supply)s,
                        %(volume_24h)s, %(change_24h)s, %(change_7d)s,
                        '{"source": "binance_klines_backfill"}'::jsonb
                    )
                    ON CONFLICT (asset_id, market_date, source_code) DO UPDATE SET
                        price_usd = EXCLUDED.price_usd,
                        volume_24h = EXCLUDED.volume_24h,
                        updated_at = NOW()
                """
                with conn.cursor() as cur:
                    cur.executemany(sql, rows)
                conn.commit()
                total_rows += len(rows)
            elif dry_run:
                total_rows += len(rows)

            if (idx + 1) % 100 == 0:
                _log(f"[BINANCE] Processed {idx + 1}/{len(assets)}, success={len(symbol_map)}, empty={empty_count}, rate_limited={rate_limited_count}, rows={total_rows}")

            time.sleep(request_delay)

        except Exception as e:
            # 捕获任何未预期的异常，不中断循环
            errors.append(f"{symbol}: {type(e).__name__}: {str(e)[:100]}")
            _log(f"[BINANCE] Error processing {symbol}: {type(e).__name__}: {str(e)[:100]}")
            consecutive_failures += 1
            time.sleep(request_delay)
            continue

    _log(f"[BINANCE] Total inserted: {total_rows} rows, success symbols={len(symbol_map)}, empty={empty_count}, rate_limited={rate_limited_count}, errors={len(errors)}")
    return {
        "total_rows": total_rows,
        "assets_with_symbol": len(symbol_map),
        "empty_count": empty_count,
        "rate_limited_count": rate_limited_count,
        "errors": errors[:20],
    }


def re_etl_from_snapshots(conn, target_dates: list[date], dry_run: bool = False, log=None) -> dict:
    """从已有快照重新 ETL（兜底）。"""
    def _log(msg):
        print(msg)
        if log:
            log(msg)

    with conn.cursor() as cur:
        placeholders = ",".join(["%s"] * len(target_dates))
        cur.execute(f"""
            SELECT DATE(quote_time AT TIME ZONE 'UTC') AS snap_date, COUNT(*) AS cnt
            FROM src_cmc.cmc_asset_quote_snapshot
            WHERE DATE(quote_time AT TIME ZONE 'UTC') IN ({placeholders})
            GROUP BY snap_date
        """, target_dates)
        snap_dates = {row[0]: row[1] for row in cur.fetchall()}

        if not snap_dates:
            return {"skipped": True, "reason": "No snapshots found"}

        if dry_run:
            return {"dry_run": True, "snap_dates": {k.isoformat(): v for k, v in snap_dates.items()}}

        affected = 0
        for snap_date, snap_count in snap_dates.items():
            cur.execute("""
                INSERT INTO biz.asset_market_daily
                    (asset_id, market_date, source_code, price_usd,
                     market_cap, fdv, circulating_supply, total_supply,
                     volume_24h, change_24h, change_7d, raw_ref)
                WITH ranked AS (
                    SELECT
                        asm.asset_id,
                        DATE(q.quote_time AT TIME ZONE 'UTC') AS market_date,
                        q.price_usd, q.market_cap, q.fdv,
                        q.circulating_supply, q.total_supply,
                        q.volume_24h,
                        q.percent_change_24h AS change_24h,
                        q.percent_change_7d AS change_7d,
                        ROW_NUMBER() OVER (
                            PARTITION BY asm.asset_id, DATE(q.quote_time AT TIME ZONE 'UTC')
                            ORDER BY q.quote_time DESC
                        ) AS rn
                    FROM src_cmc.cmc_asset_quote_snapshot q
                    JOIN core.asset_source_map asm
                        ON asm.source_code = 'cmc'
                        AND asm.source_asset_key = q.cmc_id::text
                    WHERE DATE(q.quote_time AT TIME ZONE 'UTC') = %s
                      AND (q.is_anomaly IS NOT TRUE OR q.is_anomaly IS NULL)
                )
                SELECT asset_id, market_date, 'cmc', price_usd,
                       market_cap, fdv, circulating_supply, total_supply,
                       volume_24h, change_24h, change_7d,
                       '{"source": "re_etl_backfill"}'::jsonb
                FROM ranked
                WHERE rn = 1 AND asset_id IS NOT NULL
                ON CONFLICT (asset_id, market_date, source_code) DO UPDATE SET
                    price_usd = EXCLUDED.price_usd, market_cap = EXCLUDED.market_cap,
                    fdv = EXCLUDED.fdv, circulating_supply = EXCLUDED.circulating_supply,
                    total_supply = EXCLUDED.total_supply, volume_24h = EXCLUDED.volume_24h,
                    change_24h = EXCLUDED.change_24h, change_7d = EXCLUDED.change_7d,
                    updated_at = NOW()
            """, (snap_date,))
            affected += cur.rowcount
            _log(f"[ETL] Re-ETL {snap_date}: {cur.rowcount} rows")

        conn.commit()
        return {"affected": affected, "snap_dates_processed": len(snap_dates)}


def verify_backfill(conn, target_dates: list[date], min_pass: int = 7000) -> dict:
    """校验回填结果。返回 {date_str: asset_count, ..., "pass": bool}。"""
    with conn.cursor() as cur:
        placeholders = ",".join(["%s"] * len(target_dates))
        cur.execute(f"""
            SELECT market_date, COUNT(DISTINCT asset_id) AS assets
            FROM biz.asset_market_daily
            WHERE market_date IN ({placeholders})
            GROUP BY market_date ORDER BY market_date
        """, target_dates)
        stats = {row[0].isoformat(): row[1] for row in cur.fetchall()}

    stats["pass"] = all(v >= min_pass for v in stats.values())
    return stats


# ── 任务卡 ──

def create_task_card(conn, missing_dates: list[date], coverage: dict, result: dict) -> str:
    """在 sys.task 中写入一张任务卡，方便日后追溯。"""
    task_id = uuid.uuid4().hex[:12]
    name = f"[回填] 日价缺口自动回填 {len(missing_dates)}天"
    cmd = ["python", "backfill_daily_gap.py"]

    coverage_before = {d.isoformat(): coverage.get(d, 0) for d in missing_dates}

    # 回填后覆盖
    verify = {}
    if result.get("cmc", {}).get("total_rows", 0) > 0 or result.get("etl", {}).get("affected", 0) > 0:
        placeholders = ",".join(["%s"] * len(missing_dates))
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT market_date, COUNT(DISTINCT asset_id)
                FROM biz.asset_market_daily
                WHERE market_date IN ({placeholders})
                GROUP BY market_date
            """, missing_dates)
            verify = {row[0].isoformat(): row[1] for row in cur.fetchall()}

    stats = {
        "missing_dates": [d.isoformat() for d in missing_dates],
        "coverage_before": coverage_before,
        "coverage_after": verify,
        "cmc_result": result.get("cmc", {}),
        "etl_result": result.get("etl", {}),
        "binance_result": result.get("binance", {}),
    }

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO sys.task (task_id, name, status, cmd, started_at, ended_at, stats, error, category)
            VALUES (%s, %s, %s, %s::text[], NOW(), NOW(), %s::jsonb, %s, 'core')
        """, (
            task_id, name, "done", cmd,
            json.dumps(stats, ensure_ascii=False, default=str),
            None,
        ))

    return task_id


# ── 主流程 ──

def main() -> int:
    parser = argparse.ArgumentParser(description="日价缺口自动检测+回填")
    parser.add_argument("--lookback", type=int, default=30, help="扫描最近 N 天（默认30）")
    parser.add_argument("--top", type=int, default=8000, help="回填资产上限（默认8000）")
    parser.add_argument("--min-assets", type=int, default=100, help="绝对缺失阈值（默认100）；同时会自动检测低于相邻日中位数90%的日期")
    parser.add_argument("--dry-run", action="store_true", help="预览，不写入")
    parser.add_argument("--skip-cmc", action="store_true", help="跳过 CMC 历史 API，仅 re-ETL")
    parser.add_argument("--binance-delay", type=float, default=0.2, help="Binance API 请求间隔秒数（默认0.2，被限流时建议0.5-1.0）")
    parser.add_argument("--min-pass", type=int, default=7000, help="verify通过的最低资产数（默认7000，Binance兜底场景建议500）")
    args = parser.parse_args()

    settings = get_settings(require_database=True)

    with get_connection(settings.database_url) as conn:
        # 1. 自动检测缺失日期
        print(f"[BACKFILL] 扫描最近 {args.lookback} 天...")
        missing_dates, coverage = detect_missing_dates(conn, args.lookback, args.min_assets)

        if not missing_dates:
            print("[BACKFILL] 无缺口，一切正常。")
            return 0

        print(f"[BACKFILL] 检测到 {len(missing_dates)} 天缺口: {[d.isoformat() for d in missing_dates]}")
        for d in missing_dates:
            print(f"  {d.isoformat()}: {coverage.get(d, 0)} 资产")

        if args.dry_run:
            print("[BACKFILL] dry-run 模式，跳过回填。")
            return 0

        # 2. 执行回填
        result = {}

        if not args.skip_cmc:
            assets = fetch_assets_for_backfill(conn, args.top)
            print(f"\n[BACKFILL] CMC 历史 API 回填 ({len(assets)} 资产)...")
            cmc_result = backfill_via_cmc_historical(conn, missing_dates, assets)
            result["cmc"] = cmc_result
            print(f"[BACKFILL] CMC 完成: {cmc_result}")

        print(f"\n[BACKFILL] Re-ETL 兜底...")
        etl_result = re_etl_from_snapshots(conn, missing_dates)
        result["etl"] = etl_result
        print(f"[BACKFILL] Re-ETL 完成: {etl_result}")

        # CMC + re-ETL 后仍有日期 < 7000 资产时，走 Binance klines 兜底
        with conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(missing_dates))
            cur.execute(f"""
                SELECT market_date, COUNT(DISTINCT asset_id)
                FROM biz.asset_market_daily
                WHERE market_date IN ({placeholders})
                GROUP BY market_date
            """, missing_dates)
            coverage_after = {row[0]: row[1] for row in cur.fetchall()}

        gap_days = [d for d in missing_dates if coverage_after.get(d, 0) < 7000]
        if gap_days:
            print(f"\n[BACKFILL] CMC/ETL 后仍有 {len(gap_days)} 天覆盖不足7000: {[d.isoformat() for d in gap_days]}，启动 Binance klines 兜底...")
            ensure_source_platform(conn)
            binance_result = backfill_via_binance_klines(
                conn, gap_days, top_n=args.top, request_delay=args.binance_delay
            )
            result["binance"] = binance_result
            print(f"[BACKFILL] Binance 完成: {binance_result}")

        # 3. 写入任务卡（verify 之前，确保即使失败也有记录）
        task_id = create_task_card(conn, missing_dates, coverage, result)
        print(f"\n[TASK] 任务卡已生成: task_id={task_id}")

        # 4. 校验
        print(f"\n[VERIFY] 校验回填结果...")
        verify = verify_backfill(conn, missing_dates, min_pass=args.min_pass)
        print(f"[VERIFY] {json.dumps(verify, ensure_ascii=False, indent=2, default=str)}")

    return 0 if verify.get("pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
