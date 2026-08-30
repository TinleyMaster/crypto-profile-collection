#!/usr/bin/env python3
"""回填 CoinMetrics 链上指标缺口（按币动态取起始日 ~ 最新完整日）。

从 CoinMetrics Community API (REST) 拉取日级指标，UPSERT 到 biz.cm_asset_onchain_daily。
支持一次性回填 + 每日增量模式。
起始日期按币动态取库内 MAX(metric_date) WHERE cap_mvrv_cur IS NOT NULL 次日（兜底 2026-05-24）。

用法：
    python backfill_cm_onchain.py                     # 回填缺口（默认 14 币）
    python backfill_cm_onchain.py --coins btc,eth     # 仅回填指定币种
    python backfill_cm_onchain.py --incremental       # 增量模式：仅拉 T-1 完整日
    python backfill_cm_onchain.py --dry-run            # 预览，不写入
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

# ── CoinMetrics Community API ──
CM_API_BASE = "https://community-api.coinmetrics.io/v4"

# 14 有效币种（matic 排除：8 指标全 NULL）
DEFAULT_COINS = [
    "btc", "eth", "ada", "xrp", "link", "uni", "aave",
    "ltc", "bch", "etc", "xlm", "algo", "icp", "doge",
]

# 仅 btc/eth 有交易所 flow 数据
FLOW_COINS = {"btc", "eth"}

# CoinMetrics API 指标 → 基表列映射
METRIC_MAP = {
    "CapMVRVCur": "cap_mvrv_cur",
    "AdrActCnt": "adr_act_cnt",
    "TxTfrCnt": "tx_tfr_cnt",
    "ROI1yr": "roi_1yr",
    "ROI30d": "roi_30d",
    "PriceUSD": "price_usd",
}

# flow 指标（仅 btc/eth）
FLOW_METRIC_MAP = {
    "FlowInExUSD": "flow_in_ex_usd",
    "FlowOutExUSD": "flow_out_ex_usd",
}

# 起始日期（兜底；实际按币动态取 MAX(metric_date) WHERE cap_mvrv_cur IS NOT NULL 次日）
BACKFILL_START = date(2026, 5, 24)

# 每次 API 请求最多拉 90 天（避免超时）
API_WINDOW_DAYS = 90

# 限流：请求间隔（秒）
REQUEST_INTERVAL = 0.5

# UPSERT SQL
UPSERT_SQL = """
INSERT INTO biz.cm_asset_onchain_daily (
    asset_id, cm_symbol, metric_date, price_usd, cap_mvrv_cur,
    adr_act_cnt, tx_tfr_cnt, flow_in_ex_usd, flow_out_ex_usd,
    roi_30d, roi_1yr, source_cutoff
) VALUES (
    %(asset_id)s, %(cm_symbol)s, %(metric_date)s, %(price_usd)s, %(cap_mvrv_cur)s,
    %(adr_act_cnt)s, %(tx_tfr_cnt)s, %(flow_in_ex_usd)s, %(flow_out_ex_usd)s,
    %(roi_30d)s, %(roi_1yr)s, %(source_cutoff)s
)
ON CONFLICT (asset_id, metric_date) DO UPDATE SET
    price_usd = EXCLUDED.price_usd,
    cap_mvrv_cur = EXCLUDED.cap_mvrv_cur,
    adr_act_cnt = EXCLUDED.adr_act_cnt,
    tx_tfr_cnt = EXCLUDED.tx_tfr_cnt,
    flow_in_ex_usd = EXCLUDED.flow_in_ex_usd,
    flow_out_ex_usd = EXCLUDED.flow_out_ex_usd,
    roi_30d = EXCLUDED.roi_30d,
    roi_1yr = EXCLUDED.roi_1yr
"""

# 查询 asset_id 映射
SELECT_ASSET_ID_SQL = """
SELECT asset_id FROM core.asset_source_map
WHERE source_code = 'cm' AND source_asset_key = %s
"""

# 更新 source_cutoff
UPDATE_CUTOFF_SQL = """
UPDATE biz.cm_asset_onchain_daily
SET source_cutoff = %s
WHERE cm_symbol = %s AND source_cutoff < %s
"""


def safe_float(v) -> float | None:
    if v is None or v == "" or v == "null":
        return None
    try:
        f = float(v)
        return f if f == f else None  # NaN check
    except (ValueError, TypeError):
        return None


def safe_int(v) -> int | None:
    if v is None or v == "" or v == "null":
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


def api_get(url: str, retries: int = 3) -> dict | None:
    """GET 请求，带重试退避。"""
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": "backfill-cm-onchain/1.0"})
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            if e.code == 429:
                wait = 2 ** attempt + 1
                print(f"    429 限流，等待 {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            print(f"    HTTP {e.code}: {e.reason}", file=sys.stderr)
            return None
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            print(f"    请求失败: {e}", file=sys.stderr)
            return None
    return None


def fetch_asset_metrics(
    symbol: str,
    metrics: list[str],
    start_date: date,
    end_date: date,
) -> dict[str, dict]:
    """从 CoinMetrics API 拉取单币多指标日级数据。

    返回 {metric_date_str: {metric_col: value, ...}}
    """
    metrics_param = ",".join(metrics)
    result: dict[str, dict] = {}

    # 分窗口拉取
    current = start_date
    while current <= end_date:
        window_end = min(current + timedelta(days=API_WINDOW_DAYS - 1), end_date)
        url = (
            f"{CM_API_BASE}/timeseries/asset-metrics"
            f"?assets={symbol}"
            f"&metrics={metrics_param}"
            f"&frequency=1d"
            f"&start_time={current.isoformat()}"
            f"&end_time={window_end.isoformat()}"
            f"&page_size=10000"
        )
        data = api_get(url)
        if not data or "data" not in data:
            current = window_end + timedelta(days=1)
            continue

        for row in data["data"]:
            ts = row.get("time", "")
            if not ts or len(ts) < 10:
                continue
            day_str = ts[:10]
            if day_str not in result:
                result[day_str] = {}
            for cm_key, db_col in {**METRIC_MAP, **FLOW_METRIC_MAP}.items():
                if cm_key in row:
                    val = safe_float(row[cm_key])
                    if val is not None:
                        result[day_str][db_col] = val

        current = window_end + timedelta(days=1)
        time.sleep(REQUEST_INTERVAL)

    return result


def resolve_asset_id(conn, cm_symbol: str) -> int | None:
    with conn.cursor() as cur:
        cur.execute(SELECT_ASSET_ID_SQL, (cm_symbol,))
        row = cur.fetchone()
        if row:
            return row[0]
    return None


def get_coin_start_date(conn, cm_symbol: str, fallback: date) -> date:
    """从库内取该币最新有效日期（cap_mvrv_cur IS NOT NULL）的次日。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT MAX(metric_date) FROM biz.cm_asset_onchain_daily
            WHERE cm_symbol = %s AND cap_mvrv_cur IS NOT NULL
            """,
            (cm_symbol,),
        )
        row = cur.fetchone()
        if row and row[0]:
            last_valid = row[0]
            # last_valid 是 date 对象，加 1 天
            from datetime import timedelta
            return last_valid + timedelta(days=1)
    return fallback


def backfill_coin(
    conn,
    symbol: str,
    start_date: date,
    end_date: date,
    dry_run: bool,
) -> dict:
    """回填单币数据。返回统计信息。"""
    stats = {
        "symbol": symbol,
        "rows_fetched": 0,
        "rows_upserted": 0,
        "cutoff_updated": False,
        "error": None,
    }

    # 解析 asset_id
    asset_id = resolve_asset_id(conn, symbol)
    if asset_id is None:
        stats["error"] = f"无 asset_id 映射"
        return stats

    # 确定指标列表
    metrics = list(METRIC_MAP.keys())
    if symbol in FLOW_COINS:
        metrics.extend(FLOW_METRIC_MAP.keys())

    # 拉取数据
    print(f"  拉取 {symbol} ({start_date} ~ {end_date})...", end=" ", flush=True)
    day_data = fetch_asset_metrics(symbol, metrics, start_date, end_date)
    stats["rows_fetched"] = len(day_data)
    print(f"{len(day_data)} 天")

    if not day_data or dry_run:
        return stats

    # UPSERT 入库
    batch = []
    source_cutoff = end_date.isoformat()
    for day_str, vals in sorted(day_data.items()):
        record = {
            "asset_id": asset_id,
            "cm_symbol": symbol,
            "metric_date": day_str,
            "price_usd": vals.get("price_usd"),
            "cap_mvrv_cur": vals.get("cap_mvrv_cur"),
            "adr_act_cnt": safe_int(vals.get("adr_act_cnt")) if vals.get("adr_act_cnt") is not None else None,
            "tx_tfr_cnt": safe_int(vals.get("tx_tfr_cnt")) if vals.get("tx_tfr_cnt") is not None else None,
            "flow_in_ex_usd": vals.get("flow_in_ex_usd"),
            "flow_out_ex_usd": vals.get("flow_out_ex_usd"),
            "roi_30d": vals.get("roi_30d"),
            "roi_1yr": vals.get("roi_1yr"),
            "source_cutoff": source_cutoff,
        }
        batch.append(record)

    if batch:
        with conn.cursor() as cur:
            cur.executemany(UPSERT_SQL, batch)
        stats["rows_upserted"] = len(batch)

        # 更新 source_cutoff
        with conn.cursor() as cur:
            cur.execute(UPDATE_CUTOFF_SQL, (source_cutoff, symbol, source_cutoff))
            stats["cutoff_updated"] = cur.rowcount > 0

        conn.commit()

    return stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="回填 CoinMetrics 链上指标缺口（2026-05-25 ~ 最新完整日）"
    )
    parser.add_argument(
        "--coins",
        type=str,
        default=None,
        help="逗号分隔的币种列表（默认 14 币）",
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="增量模式：仅拉 T-1 完整日",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="预览，不写入数据库",
    )
    parser.add_argument(
        "--start-date",
        type=str,
        default=None,
        help="覆盖起始日期（YYYY-MM-DD），默认按币动态取库内有效末日次日（兜底 2026-05-24）",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    coins = args.coins.split(",") if args.coins else DEFAULT_COINS
    dry_run = args.dry_run

    if args.incremental:
        # 增量模式：仅拉 T-1
        today = date.today()
        end_date = today - timedelta(days=1)
        start_date = end_date
    else:
        # 回填模式
        global_start = date.fromisoformat(args.start_date) if args.start_date else BACKFILL_START
        # T-1 完整日（数据天然滞后，已论证无需 API 探测）
        end_date = date.today() - timedelta(days=1)
        print(f"最新完整日: {end_date}")
        start_date = global_start  # 兜底值，后续按币覆盖

    if start_date > end_date:
        print(f"起始日期 {start_date} > 终止日期 {end_date}，无需回填")
        return 0

    days = (end_date - start_date).days + 1
    print(f"\n回填范围: {start_date} ~ {end_date} ({days} 天)")
    print(f"币种: {', '.join(coins)}")
    if dry_run:
        print("模式: DRY RUN（不写入数据库）")
    print()

    settings = get_settings(require_database=True)
    if not settings.database_url and not dry_run:
        print("❌ DATABASE_URL 未配置")
        return 1

    all_stats = []
    if dry_run:
        # dry-run 不连库，仅预览拉取
        for i, sym in enumerate(coins, 1):
            print(f"[{i}/{len(coins)}] {sym.upper()}")
            metrics = list(METRIC_MAP.keys())
            if sym in FLOW_COINS:
                metrics.extend(FLOW_METRIC_MAP.keys())
            day_data = fetch_asset_metrics(sym, metrics, start_date, end_date)
            all_stats.append({
                "symbol": sym, "rows_fetched": len(day_data),
                "rows_upserted": len(day_data), "cutoff_updated": False, "error": None,
            })
            print(f"  [DRY RUN] {len(day_data)} 天数据")
            time.sleep(REQUEST_INTERVAL)
    else:
        with get_connection(settings.database_url) as conn:
            for i, sym in enumerate(coins, 1):
                print(f"[{i}/{len(coins)}] {sym.upper()}")
                # 按币动态取起始日期（非 incremental 且未手动指定 --start-date 时）
                if not args.incremental and not args.start_date:
                    coin_start = get_coin_start_date(conn, sym, global_start)
                    if coin_start > start_date:
                        print(f"  起始日期: {coin_start}（库内有效末日次日）")
                else:
                    coin_start = start_date

                if coin_start > end_date:
                    print(f"  跳过：起始 {coin_start} > 终止 {end_date}（数据已最新）")
                    continue

                stats = backfill_coin(conn, sym, coin_start, end_date, dry_run)
                all_stats.append(stats)
                time.sleep(REQUEST_INTERVAL)

    # 汇总
    print("\n" + "=" * 60)
    print("回填完成")
    print("=" * 60)
    total_fetched = sum(s["rows_fetched"] for s in all_stats)
    total_upserted = sum(s["rows_upserted"] for s in all_stats)
    errors = [s for s in all_stats if s.get("error")]
    print(f"币种: {len(coins)} | 拉取: {total_fetched} 天 | 写入: {total_upserted} 行")
    if errors:
        print(f"错误: {len(errors)} 币")
        for e in errors:
            print(f"  {e['symbol']}: {e['error']}")

    print(json.dumps({
        "status": "success" if not errors else "partial",
        "coins": len(coins),
        "start_date": str(start_date),
        "end_date": str(end_date),
        "total_fetched": total_fetched,
        "total_upserted": total_upserted,
        "errors": len(errors),
    }, ensure_ascii=False, indent=2))

    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
