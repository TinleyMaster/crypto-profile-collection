"""入库脚本：CryptoETF ETF 日频资金流数据。

从 cryptoetf.today API 拉取 13 种资产的历史日频资金流，upsert 到 biz.etf_flow_daily。
幂等：同一天同一资产重复运行会更新而非重复插入。

用法：
    python ingest_cryptoetf_flow.py                        # 全资产增量更新（只拉最新缺的天数）
    python ingest_cryptoetf_flow.py --full                 # 全资产全量回填（拉全部历史）
    python ingest_cryptoetf_flow.py --asset sol            # 只拉指定资产
    python ingest_cryptoetf_flow.py --asset btc,eth,sol    # 多个资产用逗号分隔
    python ingest_cryptoetf_flow.py --dry-run              # 预览，不写入
    python ingest_cryptoetf_flow.py --backfill-days 90     # 回填最近 N 天
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(line_buffering=True)

from crypto_research.clients.cryptoetf_client import (  # noqa: E402
    SUPPORTED_ASSETS,
    CryptoETFClient,
)
from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402


# ── SQL ──────────────────────────────────────────────────────

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS biz.etf_flow_daily (
    symbol            TEXT        NOT NULL,
    flow_date         DATE        NOT NULL,
    net_flow_usd      NUMERIC(20,2),
    net_flow_usd_m    NUMERIC(12,2),
    aum_usd           NUMERIC(20,2),
    total_inflow_usd  NUMERIC(20,2),
    total_outflow_usd NUMERIC(20,2),
    source_code       TEXT        NOT NULL DEFAULT 'cryptoetf',
    fetched_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, flow_date, source_code)
);
CREATE INDEX IF NOT EXISTS ix_etf_flow_daily_date
    ON biz.etf_flow_daily (flow_date DESC);
CREATE INDEX IF NOT EXISTS ix_etf_flow_daily_symbol_date
    ON biz.etf_flow_daily (symbol, flow_date DESC);
"""

UPSERT_SQL = """
INSERT INTO biz.etf_flow_daily (
    symbol, flow_date, net_flow_usd, net_flow_usd_m, aum_usd,
    total_inflow_usd, total_outflow_usd, source_code, fetched_at, updated_at
) VALUES (
    %(symbol)s, %(flow_date)s, %(net_flow_usd)s, %(net_flow_usd_m)s, %(aum_usd)s,
    %(total_inflow_usd)s, %(total_outflow_usd)s, %(source_code)s,
    NOW(), NOW()
)
ON CONFLICT (symbol, flow_date, source_code) DO UPDATE SET
    net_flow_usd = EXCLUDED.net_flow_usd,
    net_flow_usd_m = EXCLUDED.net_flow_usd_m,
    aum_usd = EXCLUDED.aum_usd,
    total_inflow_usd = EXCLUDED.total_inflow_usd,
    total_outflow_usd = EXCLUDED.total_outflow_usd,
    updated_at = NOW()
"""

LATEST_DATE_SQL = """
SELECT MAX(flow_date) AS latest FROM biz.etf_flow_daily
WHERE symbol = %s AND source_code = 'cryptoetf'
"""


# ── 工具函数 ─────────────────────────────────────────────────

def _safe_float(v: Any) -> float | None:
    """安全转换为 float，空值返回 None。"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _parse_date(s: str) -> date | None:
    """解析日期字符串，支持 YYYY-MM-DD 格式。"""
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _normalize_record(symbol: str, record: dict) -> dict | None:
    """将 API 返回的单条记录归一化为数据库字段。

    API 返回字段可能因版本不同而有差异，这里做兼容处理。
    常见字段名：date, netFlowUsd, netFlowUsdM, aum, inflow, outflow
    """
    flow_date = (
        _parse_date(record.get("date", ""))
        or _parse_date(record.get("flowDate", ""))
        or _parse_date(record.get("day", ""))
    )
    if not flow_date:
        return None

    # 净流入：优先用 netFlowUsd（完整美元），其次 netFlowUsdM（百万美元）
    net_flow_usd = _safe_float(record.get("netFlowUsd"))
    net_flow_usd_m = _safe_float(record.get("netFlowUsdM"))

    if net_flow_usd is not None and net_flow_usd_m is None:
        net_flow_usd_m = round(net_flow_usd / 1_000_000, 2)
    elif net_flow_usd_m is not None and net_flow_usd is None:
        net_flow_usd = round(net_flow_usd_m * 1_000_000, 2)

    aum_usd = _safe_float(record.get("aum")) or _safe_float(record.get("aumUsd"))
    total_inflow = _safe_float(record.get("inflow")) or _safe_float(record.get("totalInflowUsd"))
    total_outflow = _safe_float(record.get("outflow")) or _safe_float(record.get("totalOutflowUsd"))

    return {
        "symbol": symbol.upper(),
        "flow_date": flow_date,
        "net_flow_usd": net_flow_usd,
        "net_flow_usd_m": net_flow_usd_m,
        "aum_usd": aum_usd,
        "total_inflow_usd": total_inflow,
        "total_outflow_usd": total_outflow,
        "source_code": "cryptoetf",
    }


# ── 主逻辑 ───────────────────────────────────────────────────

def get_latest_date(conn, symbol: str) -> date | None:
    """查询数据库中该资产的最新日期。"""
    with conn.cursor() as cur:
        cur.execute(LATEST_DATE_SQL, (symbol.upper(),))
        row = cur.fetchone()
        return row["latest"] if row and row["latest"] else None


def fetch_and_ingest(
    client: CryptoETFClient,
    conn,
    asset: str,
    full_backfill: bool = False,
    backfill_days: int | None = None,
    dry_run: bool = False,
) -> tuple[int, date | None, date | None]:
    """拉取并入库单个资产的 ETF 资金流数据。

    Returns:
        (inserted_count, earliest_date, latest_date)
    """
    symbol = asset.lower().strip()
    symbol_upper = SUPPORTED_ASSETS.get(symbol, symbol.upper())

    print(f"  [{symbol_upper}] 拉取历史数据...", end=" ")

    try:
        raw_records = client.get_asset_flows(symbol, days=3650)
    except Exception as e:
        print(f"失败: {e}")
        return 0, None, None

    if not raw_records:
        print("无数据")
        return 0, None, None

    print(f"获取 {len(raw_records)} 条原始记录")

    # 归一化
    records = []
    for rec in raw_records:
        norm = _normalize_record(symbol_upper, rec)
        if norm:
            records.append(norm)

    if not records:
        print("  无有效记录")
        return 0, None, None

    # 按日期排序
    records.sort(key=lambda r: r["flow_date"])
    earliest = records[0]["flow_date"]
    latest = records[-1]["flow_date"]

    # 增量模式：只入库比数据库最新日期新的记录
    if not full_backfill and backfill_days is None:
        db_latest = get_latest_date(conn, symbol_upper)
        if db_latest:
            original_count = len(records)
            records = [r for r in records if r["flow_date"] > db_latest]
            skipped = original_count - len(records)
            if skipped > 0:
                print(f"  增量模式：跳过 {skipped} 条已存在记录（最新 {db_latest}），待入库 {len(records)} 条")

    # backfill_days 模式：只入库最近 N 天
    elif backfill_days is not None and backfill_days > 0:
        cutoff = date.today() - timedelta(days=backfill_days)
        original_count = len(records)
        records = [r for r in records if r["flow_date"] >= cutoff]
        skipped = original_count - len(records)
        if skipped > 0:
            print(f"  backfill {backfill_days} 天：跳过 {skipped} 条早于 {cutoff} 的记录，待入库 {len(records)} 条")

    if not records:
        print(f"  无需更新（区间：{earliest} ~ {latest}）")
        return 0, earliest, latest

    # 写入
    if dry_run:
        print(f"  [dry-run] 将写入 {len(records)} 条（{records[0]['flow_date']} ~ {records[-1]['flow_date']}）")
        return len(records), earliest, latest

    with conn.cursor() as cur:
        cur.executemany(UPSERT_SQL, records)
    conn.commit()

    print(f"  已写入 {len(records)} 条（{records[0]['flow_date']} ~ {records[-1]['flow_date']}）")
    return len(records), earliest, latest


def main() -> int:
    parser = argparse.ArgumentParser(description="CryptoETF 日频资金流入库")
    parser.add_argument("--asset", type=str, default="",
                        help="指定资产（如 btc,eth,sol），默认全量 13 种")
    parser.add_argument("--full", action="store_true",
                        help="全量回填（默认增量，只拉最新缺的）")
    parser.add_argument("--backfill-days", type=int, default=None,
                        help="只回填最近 N 天的数据")
    parser.add_argument("--dry-run", action="store_true",
                        help="预览，不写入数据库")
    args = parser.parse_args()

    settings = get_settings(require_database=True)

    if not settings.cryptoetf_api_key:
        print("错误：CRYPTOETF_KEY 未设置，请在 .env 中配置")
        return 1

    client = CryptoETFClient(
        api_key=settings.cryptoetf_api_key,
        base_url=settings.cryptoetf_base_url,
        timeout=settings.request_timeout_seconds,
    )

    # 确定要拉取的资产列表
    if args.asset:
        assets = [a.strip().lower() for a in args.asset.split(",") if a.strip()]
    else:
        assets = list(SUPPORTED_ASSETS.keys())

    print(f"=" * 60)
    print(f"CryptoETF 资金流入库")
    print(f"模式: {'全量回填' if args.full else f'backfill {args.backfill_days}天' if args.backfill_days else '增量更新'}")
    print(f"资产: {', '.join(assets)} ({len(assets)} 种)")
    print(f"dry-run: {args.dry_run}")
    print(f"=" * 60)

    with get_connection(settings.database_url) as conn:
        # 确保表存在
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
        conn.commit()

        total_inserted = 0
        summary = []

        for i, asset in enumerate(assets, 1):
            print(f"\n[{i}/{len(assets)}] 处理 {asset.upper()}...")
            count, earliest, latest = fetch_and_ingest(
                client, conn, asset,
                full_backfill=args.full,
                backfill_days=args.backfill_days,
                dry_run=args.dry_run,
            )
            total_inserted += count
            summary.append({
                "symbol": SUPPORTED_ASSETS.get(asset, asset.upper()),
                "inserted": count,
                "earliest": earliest,
                "latest": latest,
            })

        # 汇总
        print(f"\n{'=' * 60}")
        print(f"完成！共写入 {total_inserted} 条记录")
        print(f"\n{'资产':<8} {'入库数':>6} {'最早日期':>12} {'最新日期':>12}")
        print("-" * 45)
        for s in summary:
            earliest_s = str(s["earliest"]) if s["earliest"] else "N/A"
            latest_s = str(s["latest"]) if s["latest"] else "N/A"
            print(f"{s['symbol']:<8} {s['inserted']:>6} {earliest_s:>12} {latest_s:>12}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
