#!/usr/bin/env python3
"""v2 盘面信号执行器：读取 scan_gainer_signal → 风控 → 币安子账户下单 → 审计落库。

依据（盘面异动扫描系统设计方案 v2 + 交易参数回测）：
  - 信号来自 scan_gainers_v2.py（涨幅榜定位 × 强度确认）
  - 方向：SHORT_LONG 做多；MID_LONG 仅 B 右上角（chg30~50% + vr≥5.6）；TRAP_SHORT 不实盘
  - 交易参数：N 持仓时间 / TP 固定止盈 / TR 跟踪止盈 / SL 固定止损（回测最优，见 PARAMS）
  - 100U 实验风控：SIGNAL_TRADE_ENABLED=1 才下单；分层杠杆/名义（A=3x/60U，其他 1x/20U）

用法：
    python bin/execute_gainers_v2.py --dry-run        # 只打印决策（默认）
    python bin/execute_gainers_v2.py --apply          # 真实下单（需 SIGNAL_TRADE_ENABLED=1）
    python bin/execute_gainers_v2.py --apply --watch --interval 600   # 常驻：下单 + 到期平仓

注意：本脚本在 Zeabur 容器内运行（子账户 API Key 绑定服务器 IP），本机仅 dry-run。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
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

# 信号时效上限（小时）：常驻执行器停机/重建后重启时，仅执行该窗口内扫描到的新鲜信号，
# 避免把早已错过入场时点（scan_ts 陈旧）的信号当作真单打下。扫描器每 30min 一轮，
# 2h 窗口有 ~4 轮余量，正常抖动不会漏单。
MAX_SIGNAL_AGE_H = 2


def _fmt(t: datetime) -> str:
    return t.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")


def load_pending(conn, max_rows: int = 20) -> list[dict]:
    """读待执行信号（active 且未执行，仅限实盘白名单信号类型 + 时效窗口内）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, symbol, signal_type, signal_window, direction, "
            "       price_usd, chg_24h_pct, funding_rate, scan_ts, vol_24h_usd, params "
            "FROM biz.scan_gainer_signal "
            "WHERE status = 'active' AND exec_state IS NULL "
            "  AND signal_type = ANY(%s::text[]) "
            "  AND scan_ts >= NOW() - (%s * INTERVAL '1 hour') "
            "ORDER BY scan_ts DESC LIMIT %s",
            (list(LIVE_SIGNALS), MAX_SIGNAL_AGE_H, max_rows),
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
        "notional": notional, "leverage": lev,
    }
    if not live or not settings.signal_trade_enabled:
        rec.update(decision="dry_run", reason="SIGNAL_TRADE_ENABLED=0 or dry-run")
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
    # ⚠️ open_position/place_trailing_stop 的 direction 契约是**小写** long/short；
    #    传大写 "LONG" 会落入其 else 分支 → 实际下 SELL/SHORT，多空完全反向
    #    （2026-10-11 实盘复现：SHORT_LONG 信号开出了 -539.1 的空头持仓）。
    direction = "long" if sig["direction"] == "LONG" else "short"
    try:
        r = client.open_position(
            sig["symbol"], direction, notional,
            entry_price=None, leverage=lev,
            stop_loss_pct=p["SL"] * 100 if p.get("SL") else 0,
            take_profit_pct=p["TP"] * 100 if p.get("TP") else 0,
            trailing_stop_pct=p["TR"] * 100 if p.get("TR") else 0,
        )
        order_id = r.get("orderId")
        # 全量留痕：开仓单原始响应（剔除内部下划线字段）+ 各保护单原始响应（含 orderId）
        entry_resp = {k: v for k, v in r.items() if not k.startswith("_")}
        protective = {
            "stop_loss": r.get("_stop_loss"),
            "trailing_stop": r.get("_trailing_stop"),
            "take_profit": r.get("_take_profit"),
        }
        qty = float(r.get("_qty") or 0)
        avg = float(r.get("avgPrice") or 0) or float(r.get("_price") or 0)
        # MARKET 单 POST ACK 的 avgPrice 常为 0 → 回查真实成交均价/量（best-effort，失联不阻断）
        if order_id and float(r.get("avgPrice") or 0) <= 0:
            try:
                o = client.get_order(sig["symbol"], order_id)
                entry_resp["_fetched_order"] = o
                avg = float(o.get("avgPrice") or 0) or avg
                qty = float(o.get("executedQty") or 0) or qty
            except Exception as e:  # noqa: BLE001
                print(f"  [warn] {sig['symbol']} 回查成交失败: {type(e).__name__}: {e}")
        rec.update(decision="ordered", order=order_id, reason="ok", order_id=order_id,
                   client_order_id=r.get("clientOrderId"),
                   open_price=avg, open_qty=qty,
                   margin=(notional / lev) if lev else None,
                   entry_resp=entry_resp, protective=protective)
    except Exception as e:  # noqa: BLE001
        rec.update(decision="error", reason=f"{type(e).__name__}: {e}")
    return rec


def _filled_order(client, symbol: str, order_id) -> dict | None:
    """按 orderId 查单，返回 FILLED 的订单（否则 None）。用于判定哪张保护单触发了出场。"""
    if not order_id:
        return None
    try:
        o = client.get_order(symbol, order_id)
        return o if str(o.get("status")) == "FILLED" else None
    except Exception:  # noqa: BLE001
        return None


def _reconcile_closed(client, settings, conn) -> int:
    """对账：查 v2_trade_log 中未平仓记录，若币安已无持仓 → 回填出场明细与盈亏。

    出场判定（全量留痕）：从记录的保护单（SL/TR/TP）里找哪张 status=FILLED → 即出场单，
    再按该出场单 orderId 拉 userTrades，**把盈亏精确定位到本笔订单**（realizedPnl，
    手续费取负），从而剔除同币手动单对 income 的污染。无保护单成交（运维/到期平仓）
    才回退按 symbol 时间窗 income，并标 exit_reason='manual_or_unknown' 以区分口径。
    返回本次回填笔数。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, symbol, open_ts, protective_orders, order_id "
            "FROM biz.v2_trade_log WHERE close_ts IS NULL ORDER BY open_ts")
        rows = [dict(zip([d.name for d in cur.description], r)) for r in cur.fetchall()]
    n = 0
    for t in rows:
        try:
            if client.get_position_amt(t["symbol"]) != 0:
                continue  # 仍持仓
            # 平仓后清理遗留的保护单：SL 与 TR 都是 closePosition 单，只会触发其中一个，
            # 另一个在 API 侧不会随持仓归零自动撤销；若不撤，同 symbol 下次开仓会被这张
            # 旧单立即平掉（币安 API 不提供 UI 那种「成交自动撤销同类单」的行为）。
            for o in client.get_open_orders(t["symbol"]):
                try:
                    client.cancel_order(t["symbol"], o["orderId"])
                    print(f"  [reconcile] 撤销遗留挂单 {t['symbol']} {o.get('type')} id={o.get('orderId')}")
                except Exception as e:  # noqa: BLE001
                    print(f"  [reconcile] 撤单失败 {t['symbol']} id={o.get('orderId')}: {type(e).__name__}: {e}")

            prot = t.get("protective_orders") or {}
            exit_order = None
            exit_reason = "unknown"
            for key, reason in (("stop_loss", "sl"), ("trailing_stop", "trail"),
                                ("take_profit", "tp")):
                filled = _filled_order(client, t["symbol"], (prot.get(key) or {}).get("orderId"))
                if filled:
                    exit_order, exit_reason = filled, reason
                    break

            exit_order_id = None
            close_price = None
            close_resp = None
            income_detail = None
            if exit_order is not None:
                exit_order_id = exit_order.get("orderId")
                close_resp = exit_order
                close_price = float(exit_order.get("avgPrice") or 0) or None
                trades = client.get_user_trades(t["symbol"], order_id=exit_order_id)
                income_detail = trades
                pnl = sum(float(x.get("realizedPnl", 0)) for x in trades)
                # userTrades 的 commission 为正数（实付手续费）→ 取负，与 income 口径一致
                comm = -sum(float(x.get("commission", 0)) for x in trades)
            else:
                exit_reason = "manual_or_unknown"
                start_ms = int(t["open_ts"].replace(tzinfo=timezone.utc).timestamp() * 1000)
                incomes = client.get_income(t["symbol"], start_ms=start_ms)
                income_detail = incomes
                pnl = sum(float(i.get("income", 0)) for i in incomes
                          if i.get("incomeType") == "REALIZED_PNL")
                comm = sum(float(i.get("income", 0)) for i in incomes
                           if i.get("incomeType") == "COMMISSION")

            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE biz.v2_trade_log SET close_ts=NOW(), close_price=%s, "
                    "realized_pnl_usdt=%s, commission_usdt=%s, win=%s, exit_reason=%s, "
                    "exit_order_id=%s, close_resp=%s::jsonb, income_detail=%s::jsonb, "
                    "updated_at=NOW() WHERE id=%s",
                    (close_price, pnl, comm, pnl + comm > 0, exit_reason, exit_order_id,
                     json.dumps(close_resp) if close_resp else None,
                     json.dumps(income_detail) if income_detail else None, t["id"]),
                )
            print(f"  [reconcile] {t['symbol']} 已平仓 exit={exit_reason} "
                  f"pnl={pnl:+.4f} comm={comm:+.4f} win={pnl+comm>0}")
            n += 1
        except Exception as e:  # noqa: BLE001
            print(f"  [reconcile] {t['symbol']} 对账失败: {type(e).__name__}: {e}")
    return n


def close_symbol(client, symbol: str) -> int:
    """运维：市价平掉某 symbol 的全部持仓。

    先撤该 symbol 的挂单（含 closePosition 的 TRAIL/SL/TP —— 币安同一持仓侧只允许
    一张 closePosition 单，不撤会 -4045 冲突），再对该侧下 closePosition 全平单。
    双向持仓模式下用 positionSide 表达方向，不传 reduceOnly（hedge 模式禁用）。
    """
    amt = client.get_position_amt(symbol)
    if amt == 0:
        print(f"  {symbol} 无持仓，跳过")
        return 0
    pos_side = "LONG" if amt > 0 else "SHORT"
    close_side = "SELL" if amt > 0 else "BUY"
    for o in client.get_open_orders(symbol):
        try:
            client.cancel_order(symbol, o["orderId"])
            print(f"  撤单 {symbol} {o.get('type')} id={o.get('orderId')}")
        except Exception as e:  # noqa: BLE001
            print(f"  撤单失败 id={o.get('orderId')}: {type(e).__name__}: {e}")
    try:
        r = client.place_order(symbol=symbol, side=close_side, order_type="MARKET",
                               close_position=True, position_side=pos_side)
        print(f"  平仓 {symbol} {pos_side} amt={amt} → orderId={r.get('orderId')}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"  平仓失败 {symbol}: {type(e).__name__}: {e}")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真实下单（需 SIGNAL_TRADE_ENABLED=1）")
    ap.add_argument("--close-symbol", metavar="SYM",
                    help="运维：市价平掉该 symbol 全部持仓后退出（不下新单）")
    ap.add_argument("--watch", action="store_true", help="常驻：下单 + 到期平仓")
    ap.add_argument("--interval", type=int, default=600)
    ap.add_argument("--max-rows", type=int, default=20)
    args = ap.parse_args()

    settings = get_settings(require_database=True)
    live = args.apply and settings.signal_trade_enabled
    # 单一客户端实例：既服务下单，也服务 --watch 的平仓对账。
    # 必须建在 while 之外——对账与本轮「有无待执行信号」无关，若建在 for 循环内，
    # 无信号轮次（常态）会把 client 漏成未定义 → NameError 崩溃。
    client = BinanceFuturesClient(settings.binance_api_key,
                                  settings.binance_api_secret,
                                  base_url=settings.binance_fapi_base_url)

    # 运维平仓分支：只平指定 symbol 的持仓后退出，不开新单、不进常驻循环。
    if args.close_symbol:
        close_symbol(client, args.close_symbol.upper())
        return 0

    while True:
        try:
            with get_connection(settings.database_url) as conn:
                pending = load_pending(conn, args.max_rows)
            if not pending:
                print(f"[{_fmt(datetime.now(timezone.utc))}] 无待执行信号")
            for sig in pending:
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
                        # 实盘开仓 → 写 v2_trade_log（全量留痕，供追溯复验 + 完善策略）
                        # 只记机器人下单（origin='bot'）；手动单不经过本脚本、不入本表。
                        if rec["decision"] == "ordered":
                            sp = sig.get("params") or {}
                            eo = sp.get("event_bar_ts")
                            eo_dt = None
                            if eo:
                                try:
                                    eo_dt = datetime.fromisoformat(eo)
                                except (TypeError, ValueError):
                                    eo_dt = None
                            p = rec.get("params") or {}
                            with conn.cursor() as cur:
                                cur.execute(
                                    "INSERT INTO biz.v2_trade_log ("
                                    " signal_id, symbol, signal_type, direction, open_ts,"
                                    " open_price, open_qty, notional_usdt, leverage, order_id,"
                                    " origin, signal_window, scan_ts, event_bar_ts, ref_price_usd,"
                                    " chg_24h_pct, chg1h_pct, vr_1h, funding_rate, vol_24h_usd,"
                                    " margin_usdt, planned_hold_h, sl_pct, tr_pct, tp_pct,"
                                    " entry_client_order_id, entry_resp, protective_orders,"
                                    " base_url, dry_run, updated_at"
                                    ") VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                                    " %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,NOW()) "
                                    "ON CONFLICT (signal_id) DO NOTHING",
                                    (sig["id"], sig["symbol"], sig["signal_type"], sig["direction"],
                                     datetime.now(timezone.utc), rec.get("open_price"),
                                     rec.get("open_qty"), rec.get("notional"), rec.get("leverage"),
                                     rec.get("order_id"), "bot", sig.get("signal_window"),
                                     sig.get("scan_ts"), eo_dt, sig.get("price_usd"),
                                     sig.get("chg_24h_pct"), sp.get("chg1h_pct"), sp.get("vr_1h"),
                                     sig.get("funding_rate"), sig.get("vol_24h_usd"),
                                     rec.get("margin"), p.get("N"), p.get("SL"), p.get("TR"),
                                     p.get("TP"), rec.get("client_order_id"),
                                     json.dumps(rec.get("entry_resp")) if rec.get("entry_resp") else None,
                                     json.dumps(rec.get("protective")) if rec.get("protective") else None,
                                     settings.binance_fapi_base_url, not live),
                                )
            if args.watch:
                # 对账已平仓持仓（TRAIL/SL/TP 触发后补录盈亏）
                with get_connection(settings.database_url) as conn:
                    _reconcile_closed(client, settings, conn)
            if not args.watch:
                break
        except Exception as e:  # noqa: BLE001
            # 常驻进程：单轮内的 DB 抖动 / 币安 5xx / 网络闪断不得整进程退出，
            # 打印后交给下一轮重试（supervisord 的 autorestart 只兜进程级死亡）。
            print(f"[{_fmt(datetime.now(timezone.utc))}] 本轮异常（跳过，下轮重试）: "
                  f"{type(e).__name__}: {e}")
            if not args.watch:
                break
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
