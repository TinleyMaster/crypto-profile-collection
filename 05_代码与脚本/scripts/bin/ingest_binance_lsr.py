#!/usr/bin/env python3
"""Binance 多空比日频采集 → biz.long_short_ratio（period='1d' 每日一点）。

背景：biz.long_short_ratio 原由 scan_daemon 轧空任务「按需」写入（仅跟踪中币），
2026-10-06 后无跟踪币即停更。每日变化榜的「多空比榜」（2026-10-12 新增）需要
**全市场**最新多空比 → 本脚本补齐：对 Binance USDT 永续按 24h 成交额取 Top N，
用免费端点 `/futures/data/*` 拉最新一点，以 period='1d' 落库（不干扰 5m 轧空链路）。

⚠️ 端点限速：/futures/data/* 有 ~30 req/min IP 限速，脚本内 2.2s 节流（≈27/min）。
复用 binance_http.fapi_get（全局限频 + 429/418 指数退避 + 全局封禁闸门）。

用法：
    python ingest_binance_lsr.py                 # 默认 Top 150，vol >= $10M
    python ingest_binance_lsr.py --limit 50      # 只采前 50（测试用）
    python ingest_binance_lsr.py --min-vol 5e6   # 提高成交量下限
    python ingest_binance_lsr.py --dry-run       # 只打印计划，不写库
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
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.clients.binance_http import fapi_get  # noqa: E402
from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

FAPI_BASE = "https://fapi.binance.com"
DEFAULT_LIMIT = 150
DEFAULT_MIN_VOL = 10_000_000
# /futures/data/* ~30 req/min IP 限速 → 2.2s ≈ 27/min（留余量给 scan_daemon 等并发）
REQ_GAP_S = 2.2

# 只采 3 类：大户持仓(信号) / 大户账户 / 全体账户（taker 噪声大、与 squeeze 链路语义重叠，不采）
LSR_ENDPOINTS = {
    "top_position_ratio": "/futures/data/topLongShortPositionRatio",
    "top_account_ratio": "/futures/data/topLongShortAccountRatio",
    "global_ratio": "/futures/data/globalLongShortAccountRatio",
}

UPSERT_SQL = """
INSERT INTO biz.long_short_ratio
    (symbol, period, ts, top_position_ratio, top_account_ratio, global_ratio, fetched_at)
VALUES (%s, '1d', %s, %s, %s, %s, NOW())
ON CONFLICT (symbol, period, ts) DO UPDATE SET
    top_position_ratio = EXCLUDED.top_position_ratio,
    top_account_ratio  = EXCLUDED.top_account_ratio,
    global_ratio       = EXCLUDED.global_ratio,
    fetched_at         = NOW()
"""


def top_symbols(limit: int, min_vol: float) -> list[str]:
    """USDT 永续按 24h 成交额 Top N（成交额降序）。"""
    data = fapi_get(f"{FAPI_BASE}/fapi/v1/ticker/24hr")
    rows = [
        (r["symbol"], float(r.get("quoteVolume") or 0))
        for r in data
        if str(r.get("symbol") or "").endswith("USDT")
        and float(r.get("quoteVolume") or 0) >= min_vol
    ]
    rows.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in rows[:limit]]


def fetch_latest(symbol: str) -> tuple[datetime | None, dict[str, float]]:
    """拉单币三类多空比的最新一点。返回 (ts, {key: ratio})；端点失败/空则跳过该 key。"""
    values: dict[str, float] = {}
    max_ts: datetime | None = None
    for key, path in LSR_ENDPOINTS.items():
        try:
            data = fapi_get(f"{FAPI_BASE}{path}",
                            {"symbol": symbol, "period": "5m", "limit": 1})
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] {symbol} {key} 拉取失败: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        if not data:
            continue
        row = data[-1]
        ratio = row.get("longShortRatio")
        ts_ms = row.get("timestamp")
        if ratio is None:
            continue
        try:
            values[key] = float(ratio)
            ts = datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=timezone.utc)
            if max_ts is None or ts > max_ts:
                max_ts = ts
        except (TypeError, ValueError):
            continue
        time.sleep(REQ_GAP_S)  # 每端点一次请求后节流
    return max_ts, values


def main() -> int:
    ap = argparse.ArgumentParser(description="Binance 多空比日频采集（period=1d）")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help=f"按 24h 成交额取 Top N（默认 {DEFAULT_LIMIT}）")
    ap.add_argument("--min-vol", type=float, default=DEFAULT_MIN_VOL,
                    help=f"成交量下限 USD（默认 {DEFAULT_MIN_VOL:,.0f}）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不写库")
    ap.add_argument("--symbols", default="", help="指定合约码（逗号分隔），覆盖 Top N 选择")
    args = ap.parse_args()

    settings = get_settings(require_database=True)

    # ── 1) 定宇宙（HTTP 先行，不持 DB 连接）──
    symbols = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
               if args.symbols else top_symbols(args.limit, args.min_vol))
    n_req = len(symbols) * len(LSR_ENDPOINTS)
    print(f"[lsr] 宇宙 {len(symbols)} 币，{len(LSR_ENDPOINTS)} 端点 × {REQ_GAP_S}s 节流 "
          f"≈ {n_req * REQ_GAP_S / 60:.1f} 分钟"
          f"{'（DRY RUN，不写库）' if args.dry_run else ''}")

    # ── 2) 逐币取数 ──
    rows: list[tuple] = []
    t0 = time.time()
    for i, sym in enumerate(symbols, 1):
        ts, values = fetch_latest(sym)
        if ts is not None and values:
            rows.append((sym, ts, values.get("top_position_ratio"),
                         values.get("top_account_ratio"), values.get("global_ratio")))
        if i % 25 == 0 or i == len(symbols):
            el = time.time() - t0
            print(f"[lsr] {i}/{len(symbols)} 币，{len(rows)} 行已取，用时 {el/60:.1f} min")

    print(f"[lsr] 取数完成：{len(rows)}/{len(symbols)} 币有数据")

    if args.dry_run:
        for sym, ts, tp, ta, gl in rows[:5]:
            print(f"  {sym}: ts={ts} top_pos={tp} top_acc={ta} global={gl}")
        return 0

    # ── 3) 写库 ──
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.executemany(UPSERT_SQL, rows)
            n = cur.rowcount
        conn.commit()
    print(f"[lsr] 完成：写入 {n} 行 → biz.long_short_ratio (period='1d')")
    return 0


if __name__ == "__main__":
    sys.exit(main())
