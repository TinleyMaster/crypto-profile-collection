#!/usr/bin/env python3
"""v2 盘面信号执行器：读取 scan_gainer_signal → 风控 → 币安子账户下单 → 审计落库。

依据（盘面异动扫描系统设计方案 v2 + 交易参数回测）：
  - 信号来自 scan_gainers_v2.py（涨幅榜定位 × 强度确认）
  - 方向：SHORT_LONG 做多；MID_LONG 仅 B 右上角（chg30~50% + vr≥5.6）；TRAP_SHORT 不实盘
  - 交易参数：N 持仓时间 / TP 固定止盈 / TR 跟踪止盈 / SL 固定止损（回测最优，见 PARAMS）
  - 100U 实验风控：V2_TRADE_ENABLED=1 才下单；分层杠杆/名义（A=3x/60U，其他 1x/20U）

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

# ── 交易参数（2026-10-06 细分档回测定稿，含 0.2% 成本）──────────────
# SHORT_LONG = 短线做多（涨幅≥50%，TRAIL 3%，持仓 24h）
# MID_LONG   = 中线做多（涨幅 20~50%，TRAIL 3%，持仓 168h）
# TRAP_SHORT = 诱多做空（涨幅<5%，FIX 止盈 50% / 止损 10%，持仓 12h）
# 实盘硬止损 -10% 作为黑天鹅保护（跟踪止盈 3% 先触发，硬止损兜底）
PARAMS: dict[str, dict] = {
    "SHORT_LONG": {"N": 24,  "TP": None, "TR": 0.03, "SL": 0.10},
    "MID_LONG":   {"N": 168, "TP": None, "TR": 0.03, "SL": 0.10},
    "TRAP_SHORT": {"N": 12,  "TP": 0.50, "TR": None, "SL": 0.10},
}

# 风控（100U 实验）
MAX_POSITIONS = 3          # 最大同时持仓数
MAX_LOSS_EQUITY_PCT = 15.0  # 单笔最大亏损占总权益 %（硬顶，回测风险约束）

# 分层杠杆/名义（2026-10-07 决策：纯 A 高胜率高赔率 → 加杠杆重仓；其他信号 1x 不加杠杆）
# - SHORT_LONG（A）：胜率 71.1% / 期望 +5.6% / PF 14.2 → 3x、60U（SL10%×3x=本金 -30%/笔 兜底）
# - MID_LONG（B）/TRAP_SHORT（C）：待扩容验证，1x、20U 小仓试错（不加杠杆）
# 未列出的信号类型回退到 env 兜底值。
LEVERAGE_BY_SIGNAL: dict[str, int] = {"SHORT_LONG": 3, "MID_LONG": 1, "TRAP_SHORT": 1}
NOTIONAL_BY_SIGNAL: dict[str, float] = {"SHORT_LONG": 60.0, "MID_LONG": 20.0, "TRAP_SHORT": 20.0}

# 实盘信号白名单（2026-10-07 扩容决策 C3：A + B右上角并行）
# - SHORT_LONG（≥50% 做多）：胜率 71.1% / 期望 +5.6% / PF 14.2（含 0.3% 成本）→ 3x / 60U
# - MID_LONG 仅 B 右上角子集（chg30~50% + vr≥5.6，扫描器已过滤）：
#   胜率 66.2% / 期望 +4.3% / PF 8.4 → 1x / 20U 不加杠杆
# - TRAP_SHORT（<5% 做空）：滚动 24h 口径全周期负期望，不实盘
LIVE_SIGNALS = ("SHORT_LONG", "MID_LONG")


def _fmt(t: datetime) -> str:
    return t.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")


def load_pending(conn, max_rows: int = 20) -> list[dict]:
    """读待执行信号（active 且未执行，仅限实盘白名单信号类型）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, symbol, signal_type, signal_window, direction, "
            "       price_usd, chg_24h_pct, funding_rate, scan_ts "
            "FROM biz.scan_gainer_signal "
            "WHERE status = 'active' AND exec_state IS NULL "
            "  AND signal_type IN %s "
            "ORDER BY scan_ts DESC LIMIT %s",
            (LIVE_SIGNALS, max_rows),
        )
        return [dict(zip([d.name for d in cur.description], rr)) for rr in cur.fetchall()]


def account_positions(client) -> list[str]:
    """当前未平持仓的 symbol 列表（按币安账户实际持仓统计）。

    一次调用同时服务「总持仓数」与「同币不叠仓」两类校验。
    """
    try:
        pos = client.get_position_risk()
        return [p.get("symbol") for p in pos if abs(float(p.get("positionAmt", 0))) > 0]
    except Exception:
        return []


def place_order(client, settings, sig: dict, live: bool) -> dict:
    """单信号风控 + 下单。返回决策记录。"""
    p = PARAMS.get(sig["signal_type"])
    if p is None:
        return {"decision": "skipped", "reason": "no_params"}
    # 分层杠杆/名义：按信号类型取配置，未列出的回退 env 兜底值
    notional = NOTIONAL_BY_SIGNAL.get(sig["signal_type"], settings.signal_max_notional_usdt)
    lev = LEVERAGE_BY_SIGNAL.get(sig["signal_type"], settings.signal_leverage)
    margin = notional / lev
    rec = {
        "signal_id": sig["id"], "symbol": sig["symbol"], "signal_type": sig["signal_type"],
        "direction": sig["direction"], "params": p,
    }
    if not live or not settings.signal_trade_enabled:
        rec.update(decision="dry_run", reason="V2_TRADE_ENABLED=0 or dry-run")
        return rec
    # 实盘：余额 / 同币不叠仓 / 持仓上限校验
    bal = client.get_balance("USDT")
    if bal is None or bal < margin * 1.2:
        rec.update(decision="skipped", reason=f"balance_low {bal}")
        return rec
    held = account_positions(client)
    if sig["symbol"] in held:
        # 同币已有未平持仓（含跨类型升级，如 MID_LONG→SHORT_LONG）→ 禁止叠仓
        rec.update(decision="skipped", reason="already_holding")
        return rec
    if len(held) >= MAX_POSITIONS:
        rec.update(decision="skipped", reason="max_positions")
        return rec
    # 下单
    client.set_leverage(sig["symbol"], lev)
    direction = "LONG" if sig["direction"] == "LONG" else "SHORT"
    try:
        r = client.open_position(
            sig["symbol"], direction, notional,
            entry_price=None, leverage=lev,
            stop_loss_pct=p["SL"] * 100 if p.get("SL") else 0,
            take_profit_pct=p["TP"] * 100 if p.get("TP") else 0,
            trailing_stop_pct=p["TR"] * 100 if p.get("TR") else 0,
        )
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
