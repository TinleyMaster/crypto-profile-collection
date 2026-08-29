"""入库脚本：Coin Metrics Community 档链上日频指标。

从 github.com/coinmetrics/data 下载 raw CSV，解析后 upsert 到 biz.cm_asset_onchain_daily。

用法：
    python ingest_cm_onchain_daily.py                     # 入库所有达标币种
    python ingest_cm_onchain_daily.py --coins btc,eth     # 仅入库指定币种
    python ingest_cm_onchain_daily.py --dry-run            # 预览，不写入
    python ingest_cm_onchain_daily.py --list cm_major_coins.json  # 从清单文件读取币种
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from datetime import date
from pathlib import Path
from urllib.request import urlopen, Request

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

# Coin Metrics CSV 基础 URL
CM_CSV_BASE = "https://raw.githubusercontent.com/coinmetrics/data/master/csv"

# 数据截止日期
SOURCE_CUTOFF = date(2026, 5, 24)

# CSV 列名 → 数据库字段映射
COL_MAP = {
    "PriceUSD": "price_usd",
    "CapMVRVCur": "cap_mvrv_cur",
    "AdrActCnt": "adr_act_cnt",
    "TxTfrCnt": "tx_tfr_cnt",
    "FlowInExUSD": "flow_in_ex_usd",
    "FlowOutExUSD": "flow_out_ex_usd",
    "ROI30d": "roi_30d",
    "ROI1yr": "roi_1yr",
    "volume_reported_spot_usd_1d": "volume_reported_spot_usd_1d",
}

# UPSERT SQL
UPSERT_SQL = """
INSERT INTO biz.cm_asset_onchain_daily (
    asset_id, cm_symbol, metric_date, price_usd, cap_mvrv_cur,
    adr_act_cnt, tx_tfr_cnt, flow_in_ex_usd, flow_out_ex_usd,
    roi_30d, roi_1yr, volume_reported_spot_usd_1d, source_cutoff
) VALUES (
    %(asset_id)s, %(cm_symbol)s, %(metric_date)s, %(price_usd)s, %(cap_mvrv_cur)s,
    %(adr_act_cnt)s, %(tx_tfr_cnt)s, %(flow_in_ex_usd)s, %(flow_out_ex_usd)s,
    %(roi_30d)s, %(roi_1yr)s, %(volume_reported_spot_usd_1d)s, %(source_cutoff)s
)
ON CONFLICT (asset_id, metric_date) DO UPDATE SET
    price_usd = EXCLUDED.price_usd,
    cap_mvrv_cur = EXCLUDED.cap_mvrv_cur,
    adr_act_cnt = EXCLUDED.adr_act_cnt,
    tx_tfr_cnt = EXCLUDED.tx_tfr_cnt,
    flow_in_ex_usd = EXCLUDED.flow_in_ex_usd,
    flow_out_ex_usd = EXCLUDED.flow_out_ex_usd,
    roi_30d = EXCLUDED.roi_30d,
    roi_1yr = EXCLUDED.roi_1yr,
    volume_reported_spot_usd_1d = EXCLUDED.volume_reported_spot_usd_1d
"""

# 查询 asset_id 映射
SELECT_ASSET_ID_SQL = """
SELECT asset_id FROM core.asset_source_map
WHERE source_code = 'cm' AND source_asset_key = %s
"""

# 插入 asset_source_map（CM 映射）
INSERT_SOURCE_MAP_SQL = """
INSERT INTO core.asset_source_map (
    asset_id, source_code, source_asset_key, match_status, match_method, is_primary
) VALUES (
    %s, 'cm', %s, 'confirmed', 'symbol_match', false
)
ON CONFLICT (source_code, source_asset_key) DO UPDATE SET
    asset_id = EXCLUDED.asset_id,
    match_status = 'confirmed',
    updated_at = NOW()
"""


def safe_float(v: str) -> float | None:
    """安全转换为 float，空值/NaN 返回 None。"""
    if v in ("", "null", "NaN", "None"):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def safe_int(v: str) -> int | None:
    """安全转换为 int，空值/NaN 返回 None。"""
    if v in ("", "null", "NaN", "None"):
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


def fetch_csv_content(symbol: str) -> str | None:
    """下载指定币种的 CSV 内容。失败返回 None。"""
    url = f"{CM_CSV_BASE}/{symbol}.csv"
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8")
    except Exception as e:
        print(f"  [WARN] {symbol}: 下载失败 - {e}", file=sys.stderr)
        return None


def resolve_asset_id(conn, cm_symbol: str) -> int | None:
    """解析 CM ticker → core.asset.asset_id。"""
    # 先查 asset_source_map
    with conn.cursor() as cur:
        cur.execute(SELECT_ASSET_ID_SQL, (cm_symbol,))
        row = cur.fetchone()
        if row:
            return row[0]

    # 未找到映射，尝试通过 canonical_symbol 匹配
    with conn.cursor() as cur:
        cur.execute(
            "SELECT asset_id FROM core.asset WHERE LOWER(canonical_symbol) = %s",
            (cm_symbol,),
        )
        row = cur.fetchone()
        if row:
            asset_id = row[0]
            # 建立映射
            conn.execute(INSERT_SOURCE_MAP_SQL, (asset_id, cm_symbol))
            return asset_id

    return None


def parse_and_upsert(conn, symbol: str, csv_content: str, dry_run: bool) -> dict:
    """解析 CSV 并 upsert 到数据库。返回统计信息。"""
    reader = csv.DictReader(io.StringIO(csv_content))
    rows = list(reader)

    stats = {"symbol": symbol, "total_rows": len(rows), "inserted": 0, "skipped_no_asset": 0, "skipped_no_date": 0}

    if not rows:
        return stats

    # 解析 asset_id
    asset_id = resolve_asset_id(conn, symbol)
    if asset_id is None:
        stats["skipped_no_asset"] = len(rows)
        print(f"  [WARN] {symbol}: 无 asset_id 映射，跳过", file=sys.stderr)
        return stats

    # 批量处理
    batch = []
    for row in rows:
        time_str = row.get("time", "")
        if not time_str or len(time_str) < 10:
            stats["skipped_no_date"] += 1
            continue

        metric_date = time_str[:10]  # 取 YYYY-MM-DD

        record = {
            "asset_id": asset_id,
            "cm_symbol": symbol,
            "metric_date": metric_date,
            "source_cutoff": SOURCE_CUTOFF,
        }

        # 映射列
        for csv_col, db_col in COL_MAP.items():
            record[db_col] = safe_float(row.get(csv_col, ""))

        # 特殊处理整数列
        record["adr_act_cnt"] = safe_int(row.get("AdrActCnt", ""))
        record["tx_tfr_cnt"] = safe_int(row.get("TxTfrCnt", ""))

        batch.append(record)

    if dry_run:
        stats["inserted"] = len(batch)
        return stats

    # 批量 upsert
    if batch:
        with conn.cursor() as cur:
            cur.executemany(UPSERT_SQL, batch)
        stats["inserted"] = len(batch)

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="入库 CM Community 档链上日频指标")
    parser.add_argument("--coins", type=str, default=None, help="逗号分隔的币种列表，如 btc,eth")
    parser.add_argument("--list", type=str, default=None, help="从 JSON 清单文件读取币种")
    parser.add_argument("--dry-run", action="store_true", help="预览，不写入数据库")
    args = parser.parse_args()

    # 确定币种列表
    if args.coins:
        coins = [c.strip().lower() for c in args.coins.split(",")]
    elif args.list:
        list_path = Path(args.list)
        if not list_path.exists():
            print(f"错误：清单文件不存在 {list_path}", file=sys.stderr)
            sys.exit(1)
        data = json.loads(list_path.read_text(encoding="utf-8"))
        coins = [c["symbol"] for c in data.get("coins", [])]
    else:
        # 默认使用候选列表
        coins = [
            "btc", "eth", "ada", "xrp", "sol", "dot", "avax", "matic", "link",
            "uni", "aave", "atom", "ltc", "bch", "etc", "xlm", "algo", "near",
            "apt", "arb", "op", "fil", "icp",
        ]

    print(f"准备入库 {len(coins)} 个币种：{', '.join(coins)}")
    if args.dry_run:
        print("[DRY-RUN] 模式，不写入数据库")

    settings = get_settings(require_database=True)
    total_stats = {"coins": 0, "total_rows": 0, "inserted": 0, "errors": 0}

    with get_connection(settings.database_url) as conn:
        for i, symbol in enumerate(coins, 1):
            print(f"\n[{i}/{len(coins)}] {symbol}...")
            csv_content = fetch_csv_content(symbol)
            if csv_content is None:
                total_stats["errors"] += 1
                continue

            try:
                stats = parse_and_upsert(conn, symbol, csv_content, args.dry_run)
                total_stats["coins"] += 1
                total_stats["total_rows"] += stats["total_rows"]
                total_stats["inserted"] += stats["inserted"]
                print(f"  {stats['total_rows']} 行 → {stats['inserted']} 行入库")
            except Exception as e:
                print(f"  [ERROR] {symbol}: {e}", file=sys.stderr)
                total_stats["errors"] += 1

    print(f"\n{'='*50}")
    print(f"入库完成：{total_stats['coins']} 个币种")
    print(f"总行数：{total_stats['total_rows']}")
    print(f"已入库：{total_stats['inserted']}")
    print(f"错误：{total_stats['errors']}")


if __name__ == "__main__":
    main()
