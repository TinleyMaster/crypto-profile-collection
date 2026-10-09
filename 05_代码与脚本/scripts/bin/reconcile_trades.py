#!/usr/bin/env python3
"""实盘胜率复验：从 v2_trade_log 统计已平仓交易，与回测预期对比。

用法：
    python bin/reconcile_trades.py               # 统计已平仓记录
    python bin/reconcile_trades.py --reconcile   # 先对账未平仓（币安 income 回填）再统计
    python bin/reconcile_trades.py --all         # 附每笔明细
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.clients.binance_futures import BinanceFuturesClient  # noqa: E402
from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

# 回测预期（含 0.3% 成本，2026-10-07 定稿）
BACKTEST = {
    "SHORT_LONG": {"win": 0.711, "mean": 0.056, "label": "A(≥50%做多)"},
    "MID_LONG": {"win": 0.662, "mean": 0.043, "label": "B右上角(30~50%+vr≥5.6)"},
}


def _fmt(t) -> str:
    if t is None:
        return "—"
    if isinstance(t, datetime):
        t = t.astimezone(timezone(timedelta_hours(8)))
        return t.strftime("%Y-%m-%d %H:%M")
    return str(t)


def timedelta_hours(h: int = 8):
    from datetime import timedelta
    return timedelta(hours=h)


def reconcile_pending(client, settings, conn) -> int:
    """对账：v2_trade_log 未平仓记录 → 币安已无持仓则用 income 回填。"""
    with conn.cursor() as cur:
        cur.execute("SELECT id, symbol, open_ts FROM biz.v2_trade_log "
                    "WHERE close_ts IS NULL ORDER BY open_ts")
        rows = [dict(zip([d.name for d in cur.description], r)) for r in cur.fetchall()]
    n = 0
    for t in rows:
        try:
            if client.get_position_amt(t["symbol"]) != 0:
                continue
            start_ms = int(t["open_ts"].replace(tzinfo=timezone.utc).timestamp() * 1000)
            incomes = client.get_income(t["symbol"], start_ms=start_ms)
            pnl = sum(float(i.get("income", 0)) for i in incomes
                      if i.get("incomeType") == "REALIZED_PNL")
            comm = sum(float(i.get("income", 0)) for i in incomes
                       if i.get("incomeType") == "COMMISSION")
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE biz.v2_trade_log SET close_ts=NOW(), realized_pnl_usdt=%s, "
                    "commission_usdt=%s, win=%s, exit_reason='unknown' WHERE id=%s",
                    (pnl, comm, pnl + comm > 0, t["id"]))
            print(f"  [reconcile] {t['symbol']} 已平仓 pnl={pnl:+.4f} comm={comm:+.4f}")
            n += 1
        except Exception as e:  # noqa: BLE001
            print(f"  [reconcile] {t['symbol']} 失败: {type(e).__name__}: {e}")
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reconcile", action="store_true", help="先对账未平仓再统计")
    ap.add_argument("--all", action="store_true", help="附每笔明细")
    args = ap.parse_args()

    settings = get_settings(require_database=True)
    if args.reconcile:
        print("[reconcile] 对账未平仓记录...")
        client = BinanceFuturesClient(settings.binance_api_key,
                                      settings.binance_api_secret,
                                      base_url=settings.binance_fapi_base_url)
        with get_connection(settings.database_url) as conn:
            n = reconcile_pending(client, settings, conn)
        print(f"  → 回填 {n} 笔")

    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT signal_id, symbol, signal_type, direction, open_ts, open_price, "
                "       notional_usdt, leverage, close_ts, realized_pnl_usdt, "
                "       commission_usdt, win "
                "FROM biz.v2_trade_log WHERE close_ts IS NOT NULL ORDER BY open_ts")
            rows = [dict(zip([d.name for d in cur.description], r)) for r in cur.fetchall()]

    if not rows:
        print("尚无已平仓实盘记录（v2_trade_log 为空或有未平仓）")
        return 0

    print(f"\n{'='*96}\n实盘胜率复验（已平仓 {len(rows)} 笔，2026-10-07 起）\n{'='*96}")
    print(f"{'信号':<14}{'n':>5}{'胜率':>8}{'均净盈亏U':>11}{'均亏U':>9}{'均盈U':>9}"
          f"{'回测胜率':>9}{'偏差':>8}")
    for sig_type in sorted({r["signal_type"] for r in rows}):
        sub = [r for r in rows if r["signal_type"] == sig_type]
        nets = [(r["realized_pnl_usdt"] or 0) + (r["commission_usdt"] or 0) for r in sub]
        wins = [x for x in nets if x > 0]
        losses = [x for x in nets if x <= 0]
        win_rate = len(wins) / len(sub)
        bt = BACKTEST.get(sig_type, {})
        dev = (win_rate - bt.get("win", 0)) * 100 if bt else float("nan")
        print(f"{sig_type:<14}{len(sub):>5}{win_rate*100:>7.1f}%{sum(nets)/len(nets):>11.2f}"
              f"{sum(losses)/max(len(losses),1):>9.2f}{sum(wins)/max(len(wins),1):>9.2f}"
              f"{bt.get('win',0)*100:>8.1f}%{dev:>+7.1f}pp")

    print(f"\n合计: n={len(rows)} 胜率={sum(1 for r in rows if r['win'])/len(rows)*100:.1f}% "
          f"净盈亏={sum((r['realized_pnl_usdt'] or 0)+(r['commission_usdt'] or 0) for r in rows):+.2f} U")

    if args.all:
        print(f"\n{'开仓时间':<18}{'代币':<12}{'信号':<12}{'方向':<6}{'开仓价':>12}"
              f"{'名义U':>8}{'净盈亏U':>10}")
        for r in rows:
            net = (r["realized_pnl_usdt"] or 0) + (r["commission_usdt"] or 0)
            print(f"{_fmt(r['open_ts']):<18}{r['symbol']:<12}{r['signal_type']:<12}"
                  f"{r['direction']:<6}{float(r['open_price'] or 0):>12.6g}"
                  f"{float(r['notional_usdt'] or 0):>8.1f}{net:>+10.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
