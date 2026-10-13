#!/usr/bin/env python3
"""入库脚本：宏观与跨资产日频数据（Yahoo Finance 免费接口，无 key）。

拉取 4 个序列近 2 年日收盘，按日期对齐合并后 upsert 到 biz.macro_market_daily：
  - DX-Y.NYB  美元指数 DXY
  - ^TNX      美债 10Y 收益率（%）
  - ^IXIC     纳斯达克综合指数
  - GC=F      黄金期货

用途：daily_btc_trend 的「宏观与跨资产联动」段（DXY/美债 + BTC 与纳指/黄金滚动相关性）。

⚠️ 口径：
  - Yahoo 数据是**免费非官方**源，与官方结算值可能有细微出入（趋势参考够用）；
  - 各资产交易日历不同（美股/商品节假日），缺失日期存 NULL，不做前向填充；
  - 美债 ^TNX 单位已是百分比（如 5.24 = 5.24%）。

用法：
    python ingest_macro_market.py              # 增量（当日行 upsert）
    python ingest_macro_market.py --full       # 全量回填 2 年
    python ingest_macro_market.py --dry-run    # 预览，不写入
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart"
TIMEOUT = 30
RANGE = "2y"
INTERVAL = "1d"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# (symbol, 列名, 中文名)
SYMBOLS = [
    ("DX-Y.NYB", "dxy", "美元指数DXY"),
    ("^TNX", "us10y", "美债10Y"),
    ("^IXIC", "nasdaq", "纳斯达克"),
    ("GC=F", "gold", "黄金"),
]


def ensure_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS biz.macro_market_daily (
                metric_date    DATE           NOT NULL PRIMARY KEY,
                dxy            NUMERIC(12,4),
                us10y          NUMERIC(10,4),
                nasdaq         NUMERIC(14,2),
                gold           NUMERIC(12,2),
                source_code    VARCHAR(20)    NOT NULL DEFAULT 'yahoo',
                fetched_at     TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
                updated_at     TIMESTAMPTZ    NOT NULL DEFAULT NOW()
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_macro_market_daily_date
                ON biz.macro_market_daily(metric_date DESC);
        """)
    conn.commit()


def fetch_symbol(symbol: str) -> dict[datetime, float]:
    """拉取单个符号的日收盘序列，返回 {日期(date) -> close}。"""
    r = requests.get(f"{YAHOO}/{symbol}",
                     params={"range": RANGE, "interval": INTERVAL},
                     headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    res = (r.json().get("chart") or {}).get("result")
    if not res:
        raise RuntimeError(f"Yahoo {symbol} 无数据")
    ts = res[0].get("timestamp") or []
    closes = (res[0].get("indicators", {}).get("quote") or [{}])[0].get("close") or []
    out: dict[datetime, float] = {}
    for t, c in zip(ts, closes):
        if c is None:
            continue
        dt = datetime.fromtimestamp(int(t), tz=timezone.utc).date()
        out[dt] = float(c)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="宏观/跨资产日频数据采集（Yahoo 免费接口）")
    ap.add_argument("--full", action="store_true", help="全量回填（默认只 upsert 最近 30 天）")
    ap.add_argument("--dry-run", action="store_true", help="预览，不写入数据库")
    args = ap.parse_args()

    series: dict[str, dict[datetime, float]] = {}
    for sym, col, cn in SYMBOLS:
        try:
            data = fetch_symbol(sym)
            series[col] = data
            dates = sorted(data)
            print(f"[macro] {cn}({sym}): {len(data)} 天, {dates[0]} ~ {dates[-1]}")
        except Exception as e:
            print(f"[macro] {cn}({sym}) 拉取失败: {e}", file=sys.stderr)
    if not series:
        print("[macro] 全部拉取失败")
        return 1

    # 按日期合并
    all_dates = sorted(set().union(*[set(v) for v in series.values()]))
    rows = []
    for d in all_dates:
        rows.append((d, series.get("dxy", {}).get(d),
                     series.get("us10y", {}).get(d),
                     series.get("nasdaq", {}).get(d),
                     series.get("gold", {}).get(d)))

    if args.dry_run:
        print(f"[dry-run] 共 {len(rows)} 天：最近 5 天")
        for r in rows[-5:]:
            print("  ", r)
        return 0

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        ensure_table(conn)
        target = rows if args.full else rows[-30:]
        with conn.cursor() as cur:
            for d, dxy, us10y, nasdaq, gold in target:
                cur.execute("""
                    INSERT INTO biz.macro_market_daily
                        (metric_date, dxy, us10y, nasdaq, gold, source_code, fetched_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, 'yahoo', NOW(), NOW())
                    ON CONFLICT (metric_date) DO UPDATE SET
                        dxy = COALESCE(EXCLUDED.dxy, biz.macro_market_daily.dxy),
                        us10y = COALESCE(EXCLUDED.us10y, biz.macro_market_daily.us10y),
                        nasdaq = COALESCE(EXCLUDED.nasdaq, biz.macro_market_daily.nasdaq),
                        gold = COALESCE(EXCLUDED.gold, biz.macro_market_daily.gold),
                        updated_at = NOW()
                """, (d, dxy, us10y, nasdaq, gold))
        conn.commit()
        print(f"[macro] upserted {len(target)} 行（{'全量' if args.full else '近30天'}）✓")


if __name__ == "__main__":
    sys.exit(main())
