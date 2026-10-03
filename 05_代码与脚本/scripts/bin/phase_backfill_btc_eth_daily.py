#!/usr/bin/env python3
"""BTC/ETH 日频回测数据回填（2026-10-03）→ 既有表结构。

背景：库内爆仓历史只有 4h×180 天、K线只有 5m/15m/1h×~3.5 个月，不满足
「尽可能多的日频」回测。本脚本补两块，全部落既有表（幂等 UPSERT）：

  1. 币安 USDT 永续 1d K线（免费，无 key）→ biz.asset_klines (interval='1d')
     - BTCUSDT 2019-09-08 ~（~2582 天）；ETHUSDT 2019-11-27 ~（~2502 天）
  2. CoinGlass @1d 爆仓历史（分段增量口径）→ biz.liquidation_history (interval='1d')
     - scope=binance：交易对级，与 K线完全对齐（同合约、同所）
     - scope=all：多所聚合，币种级；早期数据可信度低，仅作 2019-09+ 交叉验证

⚠️ 纪律：
  - liquidation_history 是分段增量口径，与 liquidation_snapshot 滚动窗口**不可换算**；
    仅标定/回测消费，禁止接入实时判定链（沿用表注释的用途限制）。
  - 缺失 ≠ 0：接口没返回的行不补 0。
  - 聚合口径 @1d 可回 2014 年（4500 行上限），但 ETH 2015 年才上线、币安永续 2019 年
    才开，早期行可信度低 ⇒ all scope 仅落库 ≥ 2019-01-01，用于 2019-09+ 交叉验证。

用法：
    python phase_backfill_btc_eth_daily.py            # 全量回填
    python phase_backfill_btc_eth_daily.py --dry-run  # 只打印计划不写库
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.clients.binance_http import fapi_get  # noqa: E402
from crypto_research.clients.coinglass_client import CoinGlassClient  # noqa: E402
from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

FAPI = "https://fapi.binance.com"
KLINES_INTERVAL = "1d"
LIQ_INTERVAL = "1d"
LIQ_SCOPE_BINANCE = "binance"
LIQ_SCOPE_ALL = "all"
ALL_SCOPE_START_MS = int(datetime(2019, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
CG_MIN_GAP = 2.5  # CoinGlass 限频 30 req/min，保守 2.5s
CG_RETRIES = 3
CG_BACKOFF = (1, 2, 4)

# 目标：BTC/ETH
TARGETS = [
    {"contract": "BTCUSDT", "base": "BTC"},
    {"contract": "ETHUSDT", "base": "ETH"},
]

UPSERT_KLINES_SQL = """
    INSERT INTO biz.asset_klines
        (symbol, interval, open_time, open_px, high_px, low_px, close_px,
         base_vol, quote_vol, trade_count, fetched_at)
    VALUES (%s,'1d',%s,%s,%s,%s,%s,%s,%s,%s,NOW())
    ON CONFLICT (symbol, interval, open_time) DO UPDATE SET
        open_px=EXCLUDED.open_px, high_px=EXCLUDED.high_px, low_px=EXCLUDED.low_px,
        close_px=EXCLUDED.close_px, base_vol=EXCLUDED.base_vol,
        quote_vol=EXCLUDED.quote_vol, trade_count=EXCLUDED.trade_count, fetched_at=NOW()
"""

UPSERT_LIQ_SQL = """
    INSERT INTO biz.liquidation_history
        (symbol, interval, exchange_scope, ts, long_liq_usd, short_liq_usd, fetched_at)
    VALUES (%s,'1d',%s,%s,%s,%s,NOW())
    ON CONFLICT (symbol, interval, exchange_scope, ts) DO UPDATE SET
        long_liq_usd=EXCLUDED.long_liq_usd,
        short_liq_usd=EXCLUDED.short_liq_usd,
        fetched_at=NOW()
"""


def fetch_klines_1d(symbol: str) -> list[dict]:
    """分页拉全量 1d K线（binance klines 单次上限 1500，永续 1d 全历史约 3 页）。"""
    out: list[dict] = []
    start = 0
    while True:
        rows = fapi_get(f"{FAPI}/fapi/v1/klines",
                        {"symbol": symbol, "interval": "1d", "limit": 1500, "startTime": start})
        if not rows:
            break
        out.extend(rows)
        if len(rows) < 1500:
            break
        start = rows[-1][0] + 1
        time.sleep(0.3)
    return out


def fetch_cg_liq_1d(client: CoinGlassClient, contract: str, base: str,
                    scope: str) -> list[dict]:
    """拉 CoinGlass @1d 爆仓历史。scope=binance 传合约码；scope=all 传基码。"""
    for attempt in range(CG_RETRIES + 1):
        try:
            if scope == LIQ_SCOPE_BINANCE:
                rows = client.liquidation_history("Binance", contract,
                                                  interval=LIQ_INTERVAL, limit=4500)
            else:
                rows = client.liquidation_aggregated_history(
                    client.supported_exchanges(), base, interval=LIQ_INTERVAL,
                    limit=4500, start_time=ALL_SCOPE_START_MS)
            return rows or []
        except Exception as e:  # noqa: BLE001
            if attempt >= CG_RETRIES:
                raise
            time.sleep(CG_BACKOFF[attempt])
    return []


def main() -> None:
    ap = argparse.ArgumentParser(description="BTC/ETH 日频回测数据回填")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划不写库")
    args = ap.parse_args()

    s = get_settings()
    client = CoinGlassClient(s.coinglass_api_key, s.coinglass_base_url)

    with get_connection(s.database_url) as conn:
        with conn.cursor() as cur:
            for t in TARGETS:
                contract, base = t["contract"], t["base"]

                # ── 1. K线 ──
                if args.dry_run:
                    print(f"[dry-run] {contract}: 拉取币安 1d K线全历史")
                else:
                    kl = fetch_klines_1d(contract)
                    k_rows = [
                        (contract,
                         datetime.fromtimestamp(k[0] / 1000.0, tz=timezone.utc),
                         k[1], k[2], k[3], k[4], k[5], k[7], k[8])
                        for k in kl
                    ]
                    cur.executemany(UPSERT_KLINES_SQL, k_rows)
                    if k_rows:
                        first = k_rows[0][1]
                        last = k_rows[-1][1]
                        print(f"[klines] {contract} 1d: n={len(k_rows)} "
                              f"{first.date()} -> {last.date()}")

                # ── 2. 爆仓 binance 单所 ──
                if args.dry_run:
                    print(f"[dry-run] {contract}: 拉取 CoinGlass @1d 爆仓 (binance)")
                else:
                    rows = fetch_cg_liq_1d(client, contract, base, LIQ_SCOPE_BINANCE)
                    liq_rows = [
                        (contract, LIQ_SCOPE_BINANCE,
                         datetime.fromtimestamp(r["time"] / 1000.0, tz=timezone.utc),
                         r.get("long_liquidation_usd"), r.get("short_liquidation_usd"))
                        for r in rows
                    ]
                    cur.executemany(UPSERT_LIQ_SQL, liq_rows)
                    if liq_rows:
                        first = liq_rows[0][2]
                        last = liq_rows[-1][2]
                        print(f"[liq] {contract} 1d binance: n={len(liq_rows)} "
                              f"{first.date()} -> {last.date()}")
                time.sleep(CG_MIN_GAP)

                # ── 3. 爆仓 all 聚合（2019-01-01 起） ──
                if args.dry_run:
                    print(f"[dry-run] {contract}: 拉取 CoinGlass @1d 爆仓 (all, >=2019-01)")
                else:
                    rows = fetch_cg_liq_1d(client, contract, base, LIQ_SCOPE_ALL)
                    liq_rows = [
                        (contract, LIQ_SCOPE_ALL,
                         datetime.fromtimestamp(r["time"] / 1000.0, tz=timezone.utc),
                         r.get("aggregated_long_liquidation_usd"),
                         r.get("aggregated_short_liquidation_usd"))
                        for r in rows
                    ]
                    cur.executemany(UPSERT_LIQ_SQL, liq_rows)
                    if liq_rows:
                        first = liq_rows[0][2]
                        last = liq_rows[-1][2]
                        print(f"[liq] {contract} 1d all: n={len(liq_rows)} "
                              f"{first.date()} -> {last.date()}")
                time.sleep(CG_MIN_GAP)

    print("完成。")


if __name__ == "__main__":
    main()
