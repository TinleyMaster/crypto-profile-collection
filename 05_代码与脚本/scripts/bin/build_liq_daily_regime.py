#!/usr/bin/env python3
"""爆仓极值日窗口覆盖层 · 每日计算 → biz.liq_daily_regime（2026-10-03）。

投研结论落地（日频，只读展示）：
  - 空爆主导日（爆仓/成交额近 90 日 p90+ 且空单占比 ≥70%）→ 后 7-14 日显著上涨 ⇒ long_window
  - 多空双爆日 → 偏涨（恐慌顶点）⇒ capitulation_window
  - 多爆主导日 → 无方向（不产生信号）
口径与 workbench/backtest_liq_cascade_btc_eth.py 逐行一致。

本脚本自给自足：先刷新 BTC/ETH 日频输入（币安 1d K线 + CoinGlass @1d 爆仓，
复用 phase_backfill_btc_eth_daily 的函数），再算 regime 并 UPSERT。

⚠️ 纪律：
  - 本表仅供标定/回测/展示消费；scan_daemon._build_regime 只读 bucket/窗口标注追加 tags，
    **不改 long_fav/short_fav**（粒度不匹配，禁止接入实时判定）。
  - 缺失 ≠ 0：1d 爆仓早期无数据不补 0（沿用回测口径，样本自 2020-12 起）。
  - 窗口口径：T 为极值日 ⇒ 窗口覆盖 T+1 .. T+7（交易日），当日 T 本身不算。

用法：
    python build_liq_daily_regime.py                    # 刷新输入 + 全量重算并 UPSERT
    python build_liq_daily_regime.py --no-refresh       # 只重算（输入已新）
    python build_liq_daily_regime.py --dry-run          # 只打印不写库
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = _SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import phase_backfill_btc_eth_daily as daily  # noqa: E402 复用日频刷新
from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

PCT_WINDOW = 90
PCT_THR = 0.90
LONG_SHARE_THR = 0.70
WINDOW_DAYS = 7          # 极值日后的可行动窗口长度（交易日）
SYMBOLS = ("BTCUSDT", "ETHUSDT")

UPSERT_SQL = """
    INSERT INTO biz.liq_daily_regime
        (symbol, ts, liq_total, liq_ratio, pct, long_share, bucket, is_extreme,
         long_window, capitulation_window, fwd7, fwd14, fetched_at)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
    ON CONFLICT (symbol, ts) DO UPDATE SET
        liq_total=EXCLUDED.liq_total, liq_ratio=EXCLUDED.liq_ratio, pct=EXCLUDED.pct,
        long_share=EXCLUDED.long_share, bucket=EXCLUDED.bucket,
        is_extreme=EXCLUDED.is_extreme, long_window=EXCLUDED.long_window,
        capitulation_window=EXCLUDED.capitulation_window,
        fwd7=EXCLUDED.fwd7, fwd14=EXCLUDED.fwd14, fetched_at=NOW()
"""

DDL = """
CREATE TABLE IF NOT EXISTS biz.liq_daily_regime (
    symbol text NOT NULL, ts date NOT NULL,
    liq_total numeric(24,2), liq_ratio numeric(16,8), pct numeric(8,4),
    long_share numeric(8,4), bucket text, is_extreme boolean NOT NULL DEFAULT false,
    long_window boolean NOT NULL DEFAULT false,
    capitulation_window boolean NOT NULL DEFAULT false,
    fwd7 numeric(12,6), fwd14 numeric(12,6),
    fetched_at timestamptz NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, ts)
)
"""


def load_daily(conn, symbol: str) -> dict:
    """加载 1d K线 + 1d 爆仓（binance 单所）→ {date: {...}}。"""
    out: dict = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT open_time, close_px, quote_vol FROM biz.asset_klines "
            "WHERE symbol=%s AND interval='1d' ORDER BY open_time", (symbol,))
        for ot, close, vol in cur.fetchall():
            d = ot.replace(tzinfo=None).date()
            out[d] = {"close": float(close), "vol": float(vol or 0)}
        cur.execute(
            "SELECT ts, long_liq_usd, short_liq_usd FROM biz.liquidation_history "
            "WHERE symbol=%s AND interval='1d' AND exchange_scope='binance' "
            "ORDER BY ts", (symbol,))
        for ts, lon, sho in cur.fetchall():
            d = ts.replace(tzinfo=None).date()
            if d in out:
                out[d]["long_liq"] = float(lon or 0)
                out[d]["short_liq"] = float(sho or 0)
    return out


def compute(day_map: dict) -> list[dict]:
    """按回测口径逐日算 bucket/窗口/fwd（无前视）。"""
    days = sorted(day_map)
    closes = {d: day_map[d]["close"] for d in days}
    n = len(days)
    rows: list[dict] = []
    # 预扫：每天是否极值 + bucket
    daily = {}  # date -> {bucket, is_extreme, long_share, pct, ratio, total}
    for i, d in enumerate(days):
        rec = day_map[d]
        if "long_liq" not in rec:
            continue
        total = rec["long_liq"] + rec["short_liq"]
        vol = rec["vol"]
        if total <= 0 or vol <= 0:
            continue
        ratio = total / vol
        win = []
        for j in range(max(0, i - PCT_WINDOW + 1), i + 1):
            r = day_map[days[j]]
            if "long_liq" in r and r["vol"] > 0:
                win.append((r["long_liq"] + r["short_liq"]) / r["vol"])
        pct = sum(1 for v in win if v <= ratio) / len(win) if win else 0.5
        long_share = rec["long_liq"] / total
        bucket = None
        if pct >= PCT_THR:
            if long_share >= LONG_SHARE_THR:
                bucket = "EXT-LONG"
            elif (1 - long_share) >= LONG_SHARE_THR:
                bucket = "EXT-SHORT"
            else:
                bucket = "EXT-MIX"
        daily[d] = {"total": total, "ratio": ratio, "pct": pct,
                    "long_share": long_share, "bucket": bucket,
                    "is_extreme": bucket is not None}
    # 窗口：极值日 T ⇒ T+1..T+7（交易日）
    ext_dates = sorted(d for d, v in daily.items() if v["is_extreme"])
    for i, d in enumerate(days):
        if d not in daily:
            continue
        long_win = cap_win = False
        for e in ext_dates:
            gap = days.index(d) - days.index(e)
            if 1 <= gap <= WINDOW_DAYS:
                if daily[e]["bucket"] == "EXT-SHORT":
                    long_win = True
                elif daily[e]["bucket"] == "EXT-MIX":
                    cap_win = True
        f7 = closes[days[min(i + 7, n - 1)]] / closes[d] - 1 if i + 7 < n else None
        f14 = closes[days[min(i + 14, n - 1)]] / closes[d] - 1 if i + 14 < n else None
        rows.append({
            "ts": d, **daily[d], "long_window": long_win,
            "capitulation_window": cap_win, "fwd7": f7, "fwd14": f14,
        })
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="爆仓极值日窗口覆盖层每日计算")
    ap.add_argument("--no-refresh", action="store_true", help="跳过日频输入刷新")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    s = get_settings()
    with get_connection(s.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()

        for symbol in SYMBOLS:
            if not args.no_refresh:
                print(f"[refresh] {symbol}: 刷新 1d K线 + CoinGlass @1d 爆仓")
                if not args.dry_run:
                    client = daily.CoinGlassClient(s.coinglass_api_key, s.coinglass_base_url)
                    base = symbol[:-4]
                    kl = daily.fetch_klines_1d(symbol)
                    with conn.cursor() as cur:
                        cur.executemany(daily.UPSERT_KLINES_SQL, [
                            (symbol, datetime.fromtimestamp(k[0] / 1000.0, tz=timezone.utc),
                             k[1], k[2], k[3], k[4], k[5], k[7], k[8]) for k in kl])
                    for scope in ("binance", "all"):
                        rows = daily.fetch_cg_liq_1d(client, symbol, base, scope)
                        with conn.cursor() as cur:
                            cur.executemany(daily.UPSERT_LIQ_SQL, [
                                (symbol, scope, datetime.fromtimestamp(
                                    r["time"] / 1000.0, tz=timezone.utc),
                                 r.get("long_liquidation_usd"), r.get("short_liquidation_usd"))
                                for r in rows])
                    print(f"[refresh] {symbol}: 完成")
            else:
                print(f"[refresh] {symbol}: 跳过（--no-refresh）")

            day_map = load_daily(conn, symbol)
            rows = compute(day_map)
            if not rows:
                print(f"[{symbol}] 无数据")
                continue
            ext = sum(1 for r in rows if r["is_extreme"])
            lw = sum(1 for r in rows if r["long_window"])
            cw = sum(1 for r in rows if r["capitulation_window"])
            print(f"[{symbol}] {rows[0]['ts']} ~ {rows[-1]['ts']} 共 {len(rows)} 天 | "
                  f"极值 {ext} 天 | 空爆窗口 {lw} 天 | 双爆窗口 {cw} 天")
            if args.dry_run:
                last = rows[-1]
                print(f"  [dry] 最近一天 {last['ts']}: bucket={last['bucket']} "
                      f"pct={last['pct']:.2f} long_win={last['long_window']} "
                      f"cap_win={last['capitulation_window']}")
                continue
            with conn.cursor() as cur:
                cur.executemany(UPSERT_SQL, [
                    (symbol, r["ts"], r["total"], r["ratio"], r["pct"],
                     r["long_share"], r["bucket"], r["is_extreme"],
                     r["long_window"], r["capitulation_window"], r["fwd7"], r["fwd14"])
                    for r in rows])
            print(f"[{symbol}] 已 UPSERT {len(rows)} 行")

    print("完成。")


if __name__ == "__main__":
    main()
