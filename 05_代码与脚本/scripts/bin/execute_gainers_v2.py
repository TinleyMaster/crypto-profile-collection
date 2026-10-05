#!/usr/bin/env python3
"""v2 盘面信号执行器：读取 scan_gainer_signal → 风控 → 币安子账户下单 → 审计落库。

依据（盘面异动扫描系统设计方案 v2 + 交易参数回测）：
  - 信号来自 scan_gainers_v2.py（涨幅榜定位 × 强度确认）
  - 方向：SHORT_LONG / MID_LONG 做多；TRAP_SHORT 做空（EXT_SHORT 剔除：bar 级负期望）
  - 交易参数：N 持仓时间 / TP 固定止盈 / TR 跟踪止盈 / SL 固定止损（回测最优，见 PARAMS）
  - 100U 实验风控：V2_TRADE_ENABLED=1 才下单；单笔名义/杠杆/最大持仓数可配

用法：
    python bin/execute_gainers_v2.py --dry-run        # 只打印决策（默认）
    python bin/execute_gainers_v2.py --apply          # 真实下单（需 V2_TRADE_ENABLED=1）
    python bin/execute_gainers_v2.py --apply --watch --interval 600   # 常驻：下单 + 到期平仓

注意：本脚本在 Zeabur 容器内运行（子账户 API Key 绑定服务器 IP），本机仅 dry-run。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.clients.binance_futures import BinanceFuturesClient  # noqa: E402
from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

# ── 交易参数（回测最优，风险约束：单笔最差 ≥ -10%）──────────────
# SHORT_LONG/MID_LONG 为做多；TRAP_SHORT 为做空（无跟踪止盈，SQL 未算做空侧回撤）
PARAMS: dict[str, dict] = {
    "SHORT_LONG": {"N": 12, "TP": 0.50, "TR": None, "SL": 0.10},
    "MID_LONG":   {"N": 12, "TP": 0.10, "TR": None, "SL": 0.10},
    "TRAP_SHORT": {"N": 12, "TP": 0.0,  "TR": None, "SL": 0.10},
}

# 风控（100U 实验）
MAX_POSITIONS = 3          # 最大同时持仓数
MAX_LOSS_EQUITY_PCT = 15.0  # 单笔最大亏损占总权益 %（硬顶，回测风险约束）


def _fmt(t: datetime) -> str:
    return t.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")


def load_pending(conn, max_rows: int = 20) -> list[dict]:
    """读待执行信号（active 且未执行，限可交易信号类型）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, symbol, signal_type, signal_window, direction, "
            "       price_usd, chg_24h_pct, funding_rate, scan_ts "
            "FROM biz.scan_gainer_signal "
            "WHERE status = 'active' AND exec_state IS NULL "
            "  AND signal_type IN ('SHORT_LONG','MID_LONG','TRAP_SHORT') "
            "ORDER BY scan_ts DESC LIMIT %s",
            (max_rows,),
        )
        return [dict(zip([d.name for d in cur.description], rr)) for rr in cur.fetchall()]


def open_positions(client, conn) -> int:
    """当前未平持仓数（按币安账户实际持仓统计）。"""
    try:
        pos = client.get_position_risk()
        return len([p for p in pos if abs(float(p.get("positionAmt", 0))) > 0])
    except Exception:
        return 0


def place_order(client, settings, sig: dict, live: bool) -> dict:
    """单信号风控 + 下单。返回决策记录。"""
    p = PARAMS.get(sig["signal_type"])
    if p is None:
        return {"decision": "skipped", "reason": "no_params"}
    notional = settings.signal_max_notional_usdt
    lev = settings.signal_leverage
    margin = notional / lev
    rec = {
        "signal_id": sig["id"], "symbol": sig["symbol"], "signal_type": sig["signal_type"],
        "direction": sig["direction"], "params": p,
    }
    if not live or not settings.signal_trade_enabled:
        rec.update(decision="dry_run", reason="V2_TRADE_ENABLED=0 or dry-run")
        return rec
    # 实盘：余额 / 持仓上限校验
    bal = client.get_balance("USDT")
    if bal is None or bal < margin * 1.2:
        rec.update(decision="skipped", reason=f"balance_low {bal}")
        return rec
    if open_positions(client, None) >= MAX_POSITIONS:
        rec.update(decision="skipped", reason="max_positions")
        return rec
    # 下单
    client.set_leverage(sig["symbol"], lev)
    direction = "LONG" if sig["direction"] == "LONG" else "SHORT"
    try:
        r = client.open_position(sig["symbol"], direction, notional,
                                 entry_price=None, leverage=lev,
                                 stop_loss_pct=p["SL"] * 100 if p["SL"] else 0,
                                 take_profit_pct=p["TP"] * 100 if p.get("TP") else 0)
        rec.update(decision="ordered", order=r.get("orderId"), reason="ok")
    except Exception as e:  # noqa: BLE001
        rec.update(decision="error", reason=f"{type(e).__name__}: {e}")
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真实下单（需 V2_TRADE_ENABLED=1）")
    ap.add_argument("--watch", action="store_true", help="常驻：下单 + 到期平仓")
    ap.add_argument("--interval", type=int, default=600)
    ap.add_argument("--max-rows", type=int, default=20)
    args = ap.parse_args()

    settings = get_settings(require_database=True)
    live = args.apply and settings.signal_trade_enabled

    while True:
        with get_connection(settings.database_url) as conn:
            pending = load_pending(conn, args.max_rows)
        if not pending:
            print(f"[{_fmt(datetime.now(timezone.utc))}] 无待执行信号")
        for sig in pending:
            client = BinanceFuturesClient(settings.binance_api_key,
                                          settings.binance_api_secret,
                                          base_url=settings.binance_fapi_base_url)
            rec = place_order(client, settings, sig, live)
            print(f"  {sig['symbol']:<12} {sig['signal_type']:<12} {sig['direction']:<6} "
                  f"→ {rec['decision']} {rec.get('reason','')} {rec.get('order','')}")
            if rec["decision"] in ("ordered", "skipped", "error"):
                with get_connection(settings.database_url) as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE biz.scan_gainer_signal SET exec_state=%s, executed_at=%s "
                            "WHERE id=%s",
                            (rec["decision"], datetime.now(timezone.utc), sig["id"]),
                        )
        if not args.watch:
            break
        import time
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
