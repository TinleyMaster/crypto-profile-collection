"""入库脚本：CryptoETF ETF 日频资金流数据。

从 cryptoetf.today API 拉取 13 种资产的历史日频资金流，upsert 到 biz.etf_flow_daily。
幂等：同一天同一资产重复运行会更新而非重复插入。

用法：
    python ingest_cryptoetf_flow.py                        # 全资产增量更新（回补缺失/零值日期）
    python ingest_cryptoetf_flow.py --full                 # 全资产全量回填（拉全部历史）
    python ingest_cryptoetf_flow.py --asset sol            # 只拉指定资产
    python ingest_cryptoetf_flow.py --asset btc,eth,sol    # 多个资产用逗号分隔
    python ingest_cryptoetf_flow.py --dry-run              # 预览，不写入
    python ingest_cryptoetf_flow.py --backfill-days 90     # 回填最近 N 天
    python ingest_cryptoetf_flow.py --prune-zeros --dry-run   # 预览将清理的历史 0 值占位行
    python ingest_cryptoetf_flow.py --prune-zeros --until 2026-09-18   # 实际清理

修复说明（2026-09-18 审计）：
- F1：上游对「数据未发布」的日期返回 netFlowUsdM=0 占位（非真值），归一化时拦截并跳过。
- F2：增量不再只认 MAX(flow_date)，改为回补缺失/零值日期（UPSERT 覆盖），缝隙不再永久固化。
- F4：--prune-zeros 显式清理历史 0 值占位行（带日期窗口保护）。
- F5：跳过零值占位时打印 warning 计数，污染不再静默。
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
"""

CREATE_INDEX_1_SQL = """
CREATE INDEX IF NOT EXISTS ix_etf_flow_daily_date
    ON biz.etf_flow_daily (flow_date DESC);
"""

CREATE_INDEX_2_SQL = """
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

VALID_DATES_SQL = """
SELECT DISTINCT flow_date FROM biz.etf_flow_daily
WHERE symbol = %s AND source_code = 'cryptoetf'
  AND net_flow_usd_m IS NOT NULL AND net_flow_usd_m <> 0
"""

PRUNE_COUNT_SQL = """
SELECT COUNT(*) FROM biz.etf_flow_daily
WHERE source_code = 'cryptoetf'
  AND net_flow_usd_m = 0
  AND flow_date < %s::date
  AND (%s = '' OR symbol = ANY(string_to_array(%s, ',')))
"""

PRUNE_ZEROS_SQL = """
DELETE FROM biz.etf_flow_daily
WHERE source_code = 'cryptoetf'
  AND net_flow_usd_m = 0
  AND flow_date < %s::date
  AND (%s = '' OR symbol = ANY(string_to_array(%s, ',')))
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


def _is_zero_placeholder(net_usd: Any, net_usd_m: Any) -> bool:
    """判断上游记录的净流是否为「占位 0」。

    cryptoetf.today API 对「窗口内但当日数据尚未发布」的日期返回 netFlowUsdM=0
    作为占位（非真实净流，也不省略该日期）。当前 API 语义下无法区分「真 0」与
    「无数据」，故保守把「两个净流字段均为空或 0」视为无数据（2026-09-18 审计 F1）。
    ETF 单日净流精确为 0 的概率极低，误判代价远小于把占位 0 当真值入库。
    """
    def _is_empty(v: Any) -> bool:
        return v is None or (isinstance(v, (int, float)) and float(v) == 0.0)
    return _is_empty(net_usd) and _is_empty(net_usd_m)


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

    # F1（2026-09-18 审计）：占位 0 拦截。上游对未发布的日期返回 0，
    # 若两个净流字段均为空或 0，视为「无数据」→ 返回 None 交由上层跳过，不入库。
    if _is_zero_placeholder(record.get("netFlowUsd"), record.get("netFlowUsdM")):
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
        # cursor 默认返回 tuple（非 dict_row），用下标取值
        return row[0] if row and row[0] else None


def get_valid_dates(conn, symbol: str) -> set[date]:
    """查询该资产已入库的「真实数据」日期（净流非空且非 0）。

    增量模式以它为准：已有真实数据的日期不重拉，其余（缺失或零值占位）
    一律回补，打破「增量只认 MAX、缝隙永不回补」的历史缺陷（2026-09-18 F2）。
    """
    with conn.cursor() as cur:
        cur.execute(VALID_DATES_SQL, (symbol.upper(),))
        return {r[0] for r in cur.fetchall()}


def prune_zero_rows(
    conn,
    symbols: list[str],
    until: date | None,
    dry_run: bool = False,
) -> int:
    """清理历史 0 值占位行（2026-09-18 审计 F4，需显式授权）。

    仅删 `net_flow_usd_m = 0` 且 `flow_date < until`（默认今天）的行；
    保留当日可能的占位。返回将删除/已删除的行数。
    """
    until_date = until or date.today()
    symbol_csv = ",".join(s.upper() for s in symbols)
    with conn.cursor() as cur:
        cur.execute(PRUNE_COUNT_SQL, (until_date, symbol_csv, symbol_csv))
        n = cur.fetchone()[0]
        if dry_run:
            return int(n)
        cur.execute(PRUNE_ZEROS_SQL, (until_date, symbol_csv, symbol_csv))
        conn.commit()
        return int(n)


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

    # 归一化；占位 0 在 _normalize_record 里已拦截（F1），此处额外计数供日志告警（F5）
    records = []
    zero_skipped = 0
    for rec in raw_records:
        if _is_zero_placeholder(rec.get("netFlowUsd"), rec.get("netFlowUsdM")):
            zero_skipped += 1
            continue
        norm = _normalize_record(symbol_upper, rec)
        if norm:
            records.append(norm)

    if zero_skipped:
        print(f"  [warn] 跳过 {zero_skipped} 条零值占位记录（上游数据未发布，非真实净流 0）")

    if not records:
        print("  无有效记录")
        return 0, None, None

    # 按日期排序
    records.sort(key=lambda r: r["flow_date"])
    earliest = records[0]["flow_date"]
    latest = records[-1]["flow_date"]

    # 增量模式：回补「缺失或零值」的日期，而非只认 MAX(flow_date)。
    # 历史缺陷（2026-09-18 审计 F2）：只拉 flow_date > MAX 会让早于 MAX 的
    # 0 值占位缝隙永久固化——即使上游日后补出真实数据也不会被覆盖。
    # 改为：跳过「已有真实（非零）数据」的日期，其余全部回补（UPSERT 覆盖）。
    if not full_backfill and backfill_days is None:
        valid_dates = get_valid_dates(conn, symbol_upper)
        if valid_dates:
            original_count = len(records)
            records = [r for r in records if r["flow_date"] not in valid_dates]
            skipped = original_count - len(records)
            if skipped > 0:
                db_latest = get_latest_date(conn, symbol_upper)
                print(f"  增量模式：跳过 {skipped} 条已有真实数据的日期（最新 {db_latest}），"
                      f"待回补 {len(records)} 条缺失/零值日期")

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
    parser.add_argument("--prune-zeros", action="store_true",
                        help="清理历史 0 值占位行（净流=0 且早于 until；需显式使用）")
    parser.add_argument("--until", type=str, default=None,
                        help="prune-zeros 时删除 flow_date < 该日期（默认今天，YYYY-MM-DD）")
    args = parser.parse_args()

    settings = get_settings(require_database=True)

    if not settings.cryptoetf_api_key:
        print("错误：CRYPTOETF_KEY 未设置，请在 .env 中配置")
        return 1

    # 确定要处理的资产列表
    if args.asset:
        assets = [a.strip().lower() for a in args.asset.split(",") if a.strip()]
    else:
        assets = list(SUPPORTED_ASSETS.keys())

    print(f"=" * 60)
    if args.prune_zeros:
        print("CryptoETF 零值占位清理")
        print(f"资产: {', '.join(assets)} ({len(assets)} 种) | dry-run: {args.dry_run}")
        print(f"=" * 60)

        until_date: date | None = None
        if args.until:
            until_date = _parse_date(args.until)
            if not until_date:
                print(f"错误：无法解析 --until {args.until!r}，应为 YYYY-MM-DD")
                return 1
        symbols = [SUPPORTED_ASSETS.get(a, a.upper()) for a in assets]
        with get_connection(settings.database_url) as conn:
            n = prune_zero_rows(conn, symbols, until_date, dry_run=args.dry_run)
            action = "将删除" if args.dry_run else "已删除"
            print(f"{action} {n} 条零值占位行（flow_date < {until_date or date.today()}）")
            if args.dry_run:
                print("提示：去掉 --dry-run 才会真正删除；删除后建议 --full 或 --backfill-days 45 重拉真实数据")
        return 0

    client = CryptoETFClient(
        api_key=settings.cryptoetf_api_key,
        base_url=settings.cryptoetf_base_url,
        timeout=settings.request_timeout_seconds,
    )

    print(f"CryptoETF 资金流入库")
    print(f"模式: {'全量回填' if args.full else f'backfill {args.backfill_days}天' if args.backfill_days else '增量更新（回补缺失/零值日期）'}")
    print(f"资产: {', '.join(assets)} ({len(assets)} 种)")
    print(f"dry-run: {args.dry_run}")
    print(f"=" * 60)

    with get_connection(settings.database_url) as conn:
        # 确保表存在（psycopg 不支持单 execute 多条 SQL，需分开执行）
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
            cur.execute(CREATE_INDEX_1_SQL)
            cur.execute(CREATE_INDEX_2_SQL)
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
