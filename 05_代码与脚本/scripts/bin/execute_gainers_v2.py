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
    python bin/execute_gainers_v2.py --apply --watch --interval 600   # 常驻：下单 + 软件跟踪止盈 + 到期平仓

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
# TRAP_SHORT = 诱多做空（涨幅<5%，FIX 止盈 50% / 止损 15%，持仓 12h）
# 实盘硬止损 -15%（2026-10-19 从 10% 上调：回测 act=0+SL=15% PF=2~24 vs SL=10% PF=1.12，
#   82k 样本中 SL=15% 出场占比<0.1%，与 SL=0 等价；DOSUSDT 级闪崩-65% 属极端长尾，
#   168h 窗口内未出现过）
PARAMS: dict[str, dict] = {
    "SHORT_LONG": {"N": 24,  "TP": None, "TR": 0.03, "SL": 0.15},
    "MID_LONG":   {"N": 168, "TP": None, "TR": 0.03, "SL": 0.15},
    "TRAP_SHORT": {"N": 12,  "TP": 0.50, "TR": None, "SL": 0.15},
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

# 浮盈保本止损（2026-10-12）：持仓浮盈 ≥ BREAKEVEN_TRIGGER_PCT% 时，把硬止损
# （STOP_MARKET）从原 -10% 上移到入场价附近（BREAKEVEN_COST_COVER_PCT），锁定盈利，
# 防跳空/闪崩把浮盈打回亏损。与跟踪止盈 TR 配合：正常回撤由 TR（峰值-3%）先触发，
# 本止损只在价格跌破保本线时兜底。
# ⚠️ 浮盈口径 = **入场后最高价**（峰值），不是轮询时刻的瞬时价（2026-10-12 修正）：
#   执行器 5min 轮询一次，瞬时价口径会漏掉插针。实盘复现：WUSDT id=5 入场 07:06 后
#   07:25 插针至 +5.32%（5m bar high）旋即回落到 +3~4%，此后各轮询点价格均 <+5%，
#   保本损始终未挂——用户看到「价格已超入场价 5% 却没有保本损」。
#   峰值取 biz.asset_klines 中 5m/15m/1h（任一可用）自入场起（open_time ≥ 入场时刻，
#   不含入场所在那根 bar，避免把入场前的高点算进来）的 MAX(high)，空单取 MIN(low)。
#   仍要求「当前价在保本线盈利侧」才上移（否则会立刻触发市价止损，见 _ensure_protective）。
# 阈值取 5% 的数学依据：浮盈 X% 时 TR 线 = 入场×(1+X)×0.97。要求 TR 线高于保本线
# （入场价上方 0.4%）→ (1+X)×0.97 > 1.004 → X > 3.51%。5% 时 TR 线 = 入场×1.0185，
# 比保本线（1.004）高 1.45%，SL/TR 不抢触发。5% 也高于放量大阳币的日间噪声。
# 保本线不是 0%（纯保本）：STOP_MARKET 触发为市价单有滑点 + 双边 taker 手续费，
# 停在入场价实际是亏损离场。0.4% = 0.1% 双边 taker（0.05%×2）+ 0.3% 滑点预算
# （与项目回测 COST=0.003 口径一致；放量大阳币急跌时盘口瞬时薄，保守给足）。
BREAKEVEN_TRIGGER_PCT = 5.0        # 浮盈 ≥5% 触发止损上移
BREAKEVEN_COST_COVER_PCT = 0.4      # 保本线：入场价 +0.4%（多单）/ −0.4%（空单），覆盖手续费+滑点


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


# ───────────────────────── 防裸奔：保护单失败 → 回滚 + 告警 ─────────────────────────
# 2026-10-12 实盘教训（SUSDT/WUSDT）：子账户条件单（STOP_MARKET/TRAILING）在普通
# /fapi/v1/order 被 -4120/-4136 拒绝，而 open_position 把失败静默吞进 protective 字段
# → 开仓成功但无止损/追踪止盈，实盘裸奔。本组函数：保护单任一失败 → 回滚开仓 +
# 邮件告警，禁止裸奔落地。

def _prot_ok(v: dict | None) -> bool:
    """保护单是否**确实挂上了**：须为非空 dict、无 error 键、且有交易所标识。

    algo 条件单只有 algoId/clientAlgoId（没有 orderId）；经典挂单有 orderId。
    开仓后校验与运行期自愈共用本判定（2026-10-12 DOSUSDT 事故：DB 里留着
    已失效记录，看起来"有止损"实则裸奔，故判定必须落到标识的存在性上）。
    """
    if not v or "error" in v:
        return False
    return bool(v.get("orderId") or v.get("algoId") or v.get("clientAlgoId"))


def _cancel_protective_orders(client, symbol: str, prot: dict | None) -> None:
    """撤某单的保护单：先按普通接口撤（openOrders 里可见的），再按 clientAlgoId 撤残余
    Algo 条件单（子账户条件单可能不进 openOrders，须走 DELETE /fapi/v1/algo/order）。"""
    try:
        for o in client.get_open_orders(symbol):
            try:
                client.cancel_order(symbol, o["orderId"])
                print(f"    撤保护单 {symbol} {o.get('type')} id={o.get('orderId')}")
            except Exception as e:  # noqa: BLE001
                print(f"    撤单失败 {symbol} id={o.get('orderId')}: {type(e).__name__}: {e}")
    except Exception as e:  # noqa: BLE001
        print(f"    查询挂单失败 {symbol}: {type(e).__name__}: {e}")
    for key in ("stop_loss", "trailing_stop", "take_profit"):
        po = (prot or {}).get(key) or {}
        cid = po.get("clientAlgoId") or po.get("newClientOrderId")
        if cid:
            try:
                client.cancel_algo_order(client_algo_id=cid)
                print(f"    撤 Algo 条件单 {symbol} {key} clientAlgoId={cid}")
            except Exception as e:  # noqa: BLE001
                print(f"    撤 Algo 条件单失败 {symbol} {key}: {type(e).__name__}: {e}")


def _close_all_market(client, symbol: str, reason: str) -> tuple[bool, str]:
    """按当前持仓方向市价 closePosition 全平（回滚用）。"""
    try:
        amt = client.get_position_amt(symbol)
    except Exception as e:  # noqa: BLE001
        return False, f"持仓查询失败: {type(e).__name__}: {e}"
    if abs(amt) <= 0:
        return True, f"持仓为 0，无需平仓（{reason}）"
    pos_side = "LONG" if amt > 0 else "SHORT"
    close_side = "SELL" if amt > 0 else "BUY"
    try:
        r = client.place_order(symbol=symbol, side=close_side, order_type="MARKET",
                               close_position=True, position_side=pos_side)
        return True, f"市价全平成功（orderId={r.get('orderId')}，{reason}）"
    except Exception as e:  # noqa: BLE001
        return False, f"市价全平失败: {type(e).__name__}: {e}（{reason}）"


def _rollback_entry(client, symbol: str, order_id) -> tuple[bool, str]:
    """保护单失败后的开仓回滚：未成交撤单，已成交市价全平。

    返回 (是否成功, 说明)。MARKET 单 POST ACK 时多为 NEW，但实际几乎瞬间成交，
    故先查单状态：NEW → 撤单；FILLED/其他 → 市价 closePosition 全平。
    """
    if not order_id:
        return False, "无开仓单 orderId 可回滚"
    try:
        o = client.get_order(symbol, order_id)
        status = str(o.get("status", ""))
    except Exception as e:  # noqa: BLE001
        # 查单失败（订单可能已不存在）→ 直接按当前持仓市价全平
        return _close_all_market(client, symbol, f"查单失败({type(e).__name__}: {e})")
    if status == "NEW":
        try:
            client.cancel_order(symbol, order_id)
            return True, f"开仓单未成交已撤（orderId={order_id}）"
        except Exception as e:  # noqa: BLE001
            return _close_all_market(client, symbol, f"撤单失败({type(e).__name__}: {e})")
    # 已成交（FILLED/PARTIALLY_FILLED）或状态不明 → 市价全平
    return _close_all_market(client, symbol, f"开仓单状态={status}")


def _alert_protective_failed(settings, sig: dict, p_err: str, rollback_msg: str) -> None:
    """保护单挂单失败 → 邮件告警（实盘防裸奔：绝不静默）。"""
    try:
        from crypto_research.clients.notifier import EmailNotifier
        nf = EmailNotifier(settings)
        if not nf.configured:
            print(f"  [ALERT] SMTP 未配置，无法邮件告警: {sig['symbol']} {p_err}")
            return
        subject = f"🚨 实盘保护单挂单失败已回滚 {sig['symbol']} {sig['signal_type']}"
        rows = "".join(
            f"<tr><td style='padding:6px;border:1px solid #ddd'>{k}</td>"
            f"<td style='padding:6px;border:1px solid #ddd'>{v}</td></tr>"
            for k, v in [("币种", sig["symbol"]), ("信号类型", sig["signal_type"]),
                         ("方向", sig["direction"]), ("开仓价", sig.get("price_usd")),
                         ("保护单错误", p_err), ("回滚结果", rollback_msg)]
        )
        body = (
            "<div style='font-family:sans-serif;font-size:14px;color:#171717'>"
            "<h3 style='margin:0 0 8px'>🚨 实盘保护单挂单失败，已自动回滚开仓</h3>"
            "<p>该实盘开仓的保护单（止损/追踪止盈）挂单失败，为避免裸奔已自动回滚"
            "（未成交撤单 / 已成交市价平仓）。请检查币安账户确认无残留持仓。</p>"
            f"<table style='border-collapse:collapse'>{rows}</table></div>"
        )
        ok, msg = nf.send(subject=subject, body_html=body, from_name="实盘保护单告警",
                          to=settings.admin_email or settings.smtp_to)
        print(f"  [ALERT] 保护单失败告警邮件 {'OK' if ok else 'FAIL'}: {msg}")
    except Exception as e:  # noqa: BLE001
        print(f"  [ALERT] 告警邮件发送异常: {type(e).__name__}: {e}")


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
            # 原生 TRAILING_STOP_MARKET（交易所侧即时兜底）：
            #   callbackRate=TR%，**入场即激活**（act=0，回测 PF 7.93 远优于 act=5% 的 0.88），
            #   从持仓期最高价回调 TR% 由**交易所瞬间接住**（轮询式软件 TR 对 5 分钟级崩盘
            #   无能为力，DOSUSDT 21:45 一根 5m 崩 −65% 教训）。
            # 软件 _trail_check 仍保留负责回测对齐的慢速回撤出场；两者竞争先触发者胜。
            trailing_stop_pct=p["TR"] * 100 if p.get("TR") else 0,
            trailing_activation_pct=0,   # 回测结论：act=0 入场即激活（PF 7.93）远优于 act=5%（PF 0.88）
                                          # 涨过 5% 才激活的设计让"冲不到 5% 就掉头"的动量币全部亏损
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
        # ⚠️ 开仓后保护单校验（2026-10-12 教训：SUSDT/WUSDT 静默裸奔、DOSUSDT −45%）：
        #   ① 校验硬止损是否**真的挂上了**（响应须含 algoId/clientAlgoId 且无 error）；
        #   ② 没挂上 → 补挂一次；
        #   ③ 补挂仍失败 → 回滚：未成交撤单 / 已成交市价全平 + 邮件告警。
        #      **止损是必选保护**，绝不留下「开仓成功但无止损」的裸奔持仓。
        #   原生 TR / TP 是可选增强：失败仅降级/告警，不阻断（SL 仍在，软件 _trail_check 兜底）。
        need_sl = bool(p.get("SL"))
        if need_sl and not _prot_ok(protective.get("stop_loss")):
            print(f"  [verify] {sig['symbol']} 止损未挂上"
                  f"（{protective.get('stop_loss') or '响应缺失'}）→ 补挂一次")
            try:
                sl2 = client.place_sltp(sig["symbol"], direction, avg,
                                        p["SL"] * 100, is_stop=True)
                protective["stop_loss"] = sl2
                print(f"  [verify] {sig['symbol']} 补挂止损 "
                      f"{'成功 algoId=' + str(sl2.get('algoId')) if _prot_ok(sl2) else f'仍失败: {sl2}'}")
            except Exception as e:  # noqa: BLE001
                print(f"  [verify] {sig['symbol']} 补挂止损异常: {type(e).__name__}: {e}")
                protective["stop_loss"] = {"error": f"retry_failed: {type(e).__name__}: {e}"}
        if need_sl and not _prot_ok(protective.get("stop_loss")):
            p_err = r.get("_protective_error") or "止损两次未挂上"
            rb_ok, rb_msg = _rollback_entry(client, sig["symbol"], order_id)
            rec.update(decision="aborted", order=order_id,
                       reason=f"protective_failed: {p_err} | rollback={rb_msg}",
                       rollback_ok=rb_ok, open_price=avg, open_qty=qty,
                       margin=(notional / lev) if lev else None,
                       entry_resp=entry_resp, protective=protective)
            print(f"  [ROLLBACK] {sig['symbol']} 止损两次未挂上→回滚开仓: {p_err} | {rb_msg}")
            _alert_protective_failed(settings, sig, p_err, rb_msg)
            return rec
        # 可选保护降级：原生 TR 失败 → 标注软件 TR 接管（_trail_check）；TP 失败 → 仅告警
        trl = protective.get("trailing_stop")
        if isinstance(trl, dict) and "error" in trl:
            print(f"  [verify] {sig['symbol']} 原生 TR 挂单失败，降级软件 TR: {trl['error']}")
            protective["trailing_stop"] = {"mode": "software", "tr_pct": p.get("TR") or 0}
        tp = protective.get("take_profit")
        if isinstance(tp, dict) and "error" in tp:
            print(f"  [verify] {sig['symbol']} TP 挂单失败（SL/TR 仍在，继续）: {tp['error']}")
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


def _attribution_from_trades(client, t: dict, prot: dict) -> tuple[dict | None, str]:
    """algo 保护单触发归因（2026-10-14 BTWUSDT 复盘补充）：SL/TR/TP 现走 Algo Order
    API，记录里只有 algoId 没有 orderId，_filled_order 恒查不到 → 平仓全部落
    manual_or_unknown、close_price=None。改为取 open_ts 后该 symbol 的**平仓方向**
    成交，补 close_price，并按「成交均价 vs 保护单触发价」启发式判定 exit_reason：
      - 成交价 ≈ SL triggerPrice（±10% 容差，覆盖闪崩滑点）→ sl
      - 成交价 ≈ TP triggerPrice（±2%）→ tp（当前策略无 TP，预留）
      - 成交价 > 入场价×0.97 → trail（TR 激活价=入场价×1.05，峰值≥激活价，
        触发价=峰值×0.97 下界≈入场价×1.0185；激活价错位的存量数据放宽到 0.97）
      - 其余 → manual_or_unknown（保留 close_price，reason 如实标注未知）
    启发式无法区分「手动盈利平仓」与 TR，属推断口径（比全 unknown 有用，且 close_price
    必补）。返回 (出场成交记录 dict | None, exit_reason)。
    """
    try:
        start_ms = int(t["open_ts"].replace(tzinfo=timezone.utc).timestamp() * 1000)
        trades = client.get_user_trades(t["symbol"], start_ms=start_ms)
    except Exception as e:  # noqa: BLE001
        print(f"  [reconcile] {t['symbol']} 归因拉成交失败: {type(e).__name__}: {e}")
        return None, "manual_or_unknown"
    is_long = str(t["direction"]).upper() == "LONG"
    close_side = "SELL" if is_long else "BUY"
    closes = [x for x in trades if str(x.get("side")) == close_side]
    if not closes:
        return None, "manual_or_unknown"
    tot_q = sum(float(x.get("qty") or 0) for x in closes)
    if tot_q <= 0:
        return None, "manual_or_unknown"
    avg = sum(float(x.get("price") or 0) * float(x.get("qty") or 0) for x in closes) / tot_q
    last = max(closes, key=lambda x: float(x.get("time") or 0))
    last["_close_avg_price"] = avg  # 加权均价给 close_price（防拆单）
    entry = float(t["open_price"] or 0)
    sl = prot.get("stop_loss") or {}
    tp = prot.get("take_profit") or {}
    reason = "manual_or_unknown"
    sl_trig = float(sl.get("triggerPrice") or sl.get("stopPrice") or 0)
    if sl_trig > 0 and avg > 0 and abs(avg - sl_trig) / sl_trig <= 0.10:
        reason = "sl"
    else:
        tp_trig = float(tp.get("triggerPrice") or 0) if isinstance(tp, dict) else 0
        if tp_trig > 0 and avg > 0 and abs(avg - tp_trig) / tp_trig <= 0.02:
            reason = "tp"
        elif entry > 0 and ((is_long and avg > entry * 0.97)
                            or (not is_long and avg < entry * 1.03)):
            # 多单 TR 触发价 ≥ 峰值×0.97 通常 > 入场价（深幅盈利币可远超）；
            # 空单对称：触发价 ≤ 谷值×1.03 通常 < 入场价。按方向分别放宽边界。
            reason = "trail"
    return last, reason


def _trail_check(client, conn) -> int:
    """软件跟踪止盈降级兜底（仅处理 Binance 原生 TR 挂单失败的裸奔仓，--watch 每轮调用）。

    与 Binance 原生 TRAILING_STOP_MARKET 让道：
      Binance 原生 TR（algoId 存在）用 tick 级实时峰值追踪、act=0 入场即激活，比
      软件轮询（5min 间隔 + bar high 累积峰值）**更能锁住利润**。正常有 Binance TR
      保护单的仓位由交易所侧独管，本函数只处理 place_trailing_stop 调用失败、
      protective_orders.trailing_stop.algoId 缺失的裸奔仓（降级 fallback）。

    回测（backtest_trade_params.py）TR 判定：
        ph = 入场后各已收盘 bar 的滚动最高 high（不含入场 bar、不含当前判定 bar）
        触发 = low <= ph × (1-tr)，出场价 = ph×(1-tr)
    实盘对齐：峰值 = biz.asset_klines 中入场后**已收盘** 5m/15m/1h bar（任一可用）
    的 MAX(high_px)（未收盘 bar 不计入，等同回测「1 PRECEDING」语义；空单用
    MIN(low_px)），每轮轮询最新价，触及 峰值×(1-tr)（多单）/ 谷值×(1+tr)（空单）
    → 市价平仓。

    ⚠️ 峰值周期（2026-10-13 DOSUSDT 教训）：原实现只取**已收盘 1h bar** —— 入场后
    第一根符合条件的 1h bar 可能在一小时后才收盘，期间价格暴涨暴跌对 TR 完全盲区
    （DOSUSDT 21:08 入场、21:40 冲到 +102%、21:45 一根 5m 崩 −65%，TR 峰值恒为 0，
    全程无感）。改为 5m/15m/1h **任一已收盘 bar** 取峰：活跃币 5m bar 每 5 分钟收盘，
    入场后 5 分钟即有峰值，盲区从「~1 小时」缩到「~5 分钟」，且仍保持「仅已收盘
    bar、峰值 bar 自身不触发」的回测语义。

    与原生 TRAILING_STOP_MARKET 的关键差异（2026-10-12 对齐原因）：
    原生 TR 从「挂单时最新价（入场价）」激活，入场后回调 ≥3% 立即触发；回测峰值
    从「入场后 bar 的 high」累积（回调 bar 的 high 低于入场价时触发线更低），
    「入场即回调≥3%再突破」的币回测能吃到突破、原生 TR 却在回调中被打掉。
    本函数复刻回测判定，消除该乐观偏差。

    ⚠️ 近似：触发判定用每轮轮询时刻价格（默认 5min），回测用 bar 的 low——
    轮询间隔内的瞬时触达可能漏检（价格快速跌破触发线又拉回）。回调 3% 多为趋势性
    回撤，5min 采样大概率能捕捉；可调小 --interval 提高灵敏度。

    返回本轮平仓笔数。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT t.id, t.symbol, t.direction, t.tr_pct, t.open_ts, t.protective_orders, "
            "       (SELECT COALESCE(MAX(k.high_px), 0) FROM biz.asset_klines k "
            "         WHERE k.symbol = t.symbol AND k.open_time >= t.open_ts "
            "           AND ((k.interval = '5m'  AND k.open_time + interval '5 minutes'  <= NOW())"
            "             OR (k.interval = '15m' AND k.open_time + interval '15 minutes' <= NOW())"
            "             OR (k.interval = '1h'  AND k.open_time + interval '1 hour'     <= NOW()))) "
            "       AS peak, "
            "       (SELECT COALESCE(MIN(k.low_px), 1e18) FROM biz.asset_klines k "
            "         WHERE k.symbol = t.symbol AND k.open_time >= t.open_ts "
            "           AND ((k.interval = '5m'  AND k.open_time + interval '5 minutes'  <= NOW())"
            "             OR (k.interval = '15m' AND k.open_time + interval '15 minutes' <= NOW())"
            "             OR (k.interval = '1h'  AND k.open_time + interval '1 hour'     <= NOW()))) "
            "       AS trough "
            "FROM biz.v2_trade_log t "
            "WHERE t.close_ts IS NULL AND t.exit_order_id IS NULL "
            "  AND t.origin = 'bot' AND COALESCE(t.tr_pct, 0) > 0 "
            "  AND COALESCE(t.protective_orders->'trailing_stop'->>'algoId', 'NULL') = 'NULL' "
            "ORDER BY t.open_ts")
        rows = [dict(zip([d.name for d in cur.description], r)) for r in cur.fetchall()]
    n = 0
    for t in rows:
        try:
            tr = float(t["tr_pct"])  # tr_pct 落库即小数（0.03 = 3%）
            is_long = str(t["direction"]).upper() == "LONG"
            if is_long:
                peak = float(t["peak"] or 0)
                if peak <= 0:
                    continue  # 入场后尚无已收盘 bar，等待
                trigger_px = peak * (1 - tr)
                ref_px = peak
            else:
                trough = float(t["trough"] or 0)
                if trough <= 0 or trough >= 1e17:
                    continue
                trigger_px = trough * (1 + tr)
                ref_px = trough
            cur_px = client.get_price(t["symbol"])
            hit = (cur_px <= trigger_px) if is_long else (cur_px >= trigger_px)
            if not hit:
                continue
            # 触发 → 市价全平（复用 close_expired 流程；exit_reason='trail'）
            amt = client.get_position_amt(t["symbol"])
            if amt == 0:
                continue
            pos_side = "LONG" if amt > 0 else "SHORT"
            close_side = "SELL" if amt > 0 else "BUY"
            _cancel_protective_orders(client, t["symbol"], t.get("protective_orders"))
            r = client.place_order(symbol=t["symbol"], side=close_side, order_type="MARKET",
                                   close_position=True, position_side=pos_side)
            oid = r.get("orderId")
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE biz.v2_trade_log SET exit_order_id=%s, exit_reason='trail', "
                    "close_resp=%s::jsonb, updated_at=NOW() WHERE id=%s",
                    (oid, json.dumps(r), t["id"]))
            print(f"  [trail] {t['symbol']} 峰值{ref_px:.6g} 回撤{tr*100:.0f}% 触发，"
                  f"市价平仓 orderId={oid}（触发线={trigger_px:.6g}）")
            n += 1
        except Exception as e:  # noqa: BLE001
            print(f"  [trail] {t['symbol']} 异常: {type(e).__name__}: {e}")
    return n


def close_expired(client, conn) -> int:
    """到期平仓：持有 ≥ planned_hold_h 且仍持仓的机器人单，市价了结（对齐回测）。

    回测出场规则：持有 N 小时未触发 TRAIL/SL/TP → 按第 N 根收盘平仓（见
    backtest_trade_params.py）。实盘此前只挂 TRAIL3/SL10%，不会到期了结，导致
    未触发保护单的持仓无限期持有 —— 出场分布与回测不一致。本函数补齐该支：
    撤保护单 → 市价全平 → 写 exit_order_id/exit_reason='expiry'，盈亏由
    _reconcile_closed 下一轮按该 orderId 精确回填（close_ts 待成交明细齐备后置）。

    仅处理 origin='bot'（手动单不入本表、不受影响）。返回本轮平仓笔数。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, symbol, open_ts, planned_hold_h, protective_orders FROM biz.v2_trade_log "
            "WHERE close_ts IS NULL AND exit_order_id IS NULL AND origin = 'bot' "
            "  AND planned_hold_h IS NOT NULL AND planned_hold_h > 0 "
            "  AND NOW() >= open_ts + (planned_hold_h * INTERVAL '1 hour') "
            "ORDER BY open_ts")
        rows = [dict(zip([d.name for d in cur.description], r)) for r in cur.fetchall()]
    n = 0
    for t in rows:
        try:
            amt = client.get_position_amt(t["symbol"])
            if amt == 0:
                continue  # 已被 TRAIL/SL 平掉，交给 _reconcile_closed 回填
            pos_side = "LONG" if amt > 0 else "SHORT"
            close_side = "SELL" if amt > 0 else "BUY"
            # 撤保护单（closePosition 单与全平市价单冲突 -4045；Algo 条件单一并撤）
            _cancel_protective_orders(client, t["symbol"], t.get("protective_orders"))
            r = client.place_order(symbol=t["symbol"], side=close_side, order_type="MARKET",
                                   close_position=True, position_side=pos_side)
            oid = r.get("orderId")
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE biz.v2_trade_log SET exit_order_id=%s, exit_reason='expiry', "
                    "close_resp=%s::jsonb, updated_at=NOW() WHERE id=%s",
                    (oid, json.dumps(r), t["id"]))
            print(f"  [expiry] {t['symbol']} 持有≥{t['planned_hold_h']}h 市价平仓 orderId={oid}")
            n += 1
        except Exception as e:  # noqa: BLE001
            print(f"  [expiry] {t['symbol']} 平仓失败: {type(e).__name__}: {e}")
    return n


def _reconcile_closed(client, settings, conn) -> int:
    """对账：查 v2_trade_log 中未平仓记录，回填出场明细与盈亏。

    出场判定（全量留痕）：
    - 到期平仓（close_expired 已写入 exit_order_id）：出场单已知，直接用其 orderId 取成交；
    - TRAIL/SL/TP 触发：先按记录里的 orderId 查（普通单路径）；algo 条件单没有
      orderId（只有 algoId），退回 _attribution_from_trades 用平仓成交启发式归因
      （2026-10-14 BTWUSDT 复盘：algo 单触发后原逻辑恒落 manual_or_unknown、
      close_price=None）。
    再按出场单 orderId 拉 userTrades，**把盈亏精确定位到本笔订单**（realizedPnl，
    手续费取负），从而剔除同币手动单对 income 的污染。无保护单成交（运维手动平仓）
    才回退按 symbol 时间窗 income，并标 exit_reason='manual_or_unknown' 以区分口径。
    返回本次回填笔数。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, symbol, direction, open_ts, open_price, protective_orders, "
            "       order_id, exit_order_id, exit_reason "
            "FROM biz.v2_trade_log WHERE close_ts IS NULL ORDER BY open_ts")
        rows = [dict(zip([d.name for d in cur.description], r)) for r in cur.fetchall()]
    n = 0
    for t in rows:
        try:
            exit_order_id = t.get("exit_order_id")
            exit_order = None
            if exit_order_id:
                # 到期平仓路径：出场单是本进程下的市价平仓单，orderId 已知，直接用其取成交，
                # 不再重复判定保护单。
                exit_reason = t.get("exit_reason") or "expiry"
                exit_order = _filled_order(client, t["symbol"], exit_order_id)
                if exit_order is None:
                    continue  # 尚未成交，下轮再试
            else:
                if client.get_position_amt(t["symbol"]) != 0:
                    continue  # 仍持仓
                # 平仓后清理遗留的保护单：SL 与 TR 都是 closePosition 单，只会触发其中一个，
                # 另一个在 API 侧不会随持仓归零自动撤销；若不撤，同 symbol 下次开仓会被这张
                # 旧单立即平掉（币安 API 不提供 UI 那种「成交自动撤销同类单」的行为）。
                # 子账户的 Algo 条件单（可能不进 openOrders）按 clientAlgoId 一并撤销。
                prot = t.get("protective_orders") or {}
                _cancel_protective_orders(client, t["symbol"], prot)
                exit_reason = "unknown"
                for key, reason in (("stop_loss", "sl"), ("trailing_stop", "trail"),
                                    ("take_profit", "tp")):
                    filled = _filled_order(client, t["symbol"], (prot.get(key) or {}).get("orderId"))
                    if filled:
                        exit_order, exit_reason = filled, reason
                        break
                if exit_order is None:
                    # algo 保护单（SL/TR/TP）只有 algoId 没有 orderId，_filled_order 查不到
                    # → 用 open_ts 后平仓成交启发式归因（close_price 必补，reason 尽力）
                    exit_order, exit_reason = _attribution_from_trades(client, t, prot)

            close_price = None
            close_resp = None
            income_detail = None
            if exit_order is not None:
                exit_order_id = exit_order.get("orderId") or exit_order_id
                close_resp = exit_order
                close_price = float(exit_order.get("_close_avg_price")
                                    or exit_order.get("avgPrice") or 0) or None
                trades = client.get_user_trades(t["symbol"], order_id=exit_order_id)
                if not trades:
                    continue  # 成交明细尚未就绪，下轮再回填（避免误写 0 盈亏）
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


def _save_prot(conn, tid, prot: dict) -> None:
    """把保护单快照写回 v2_trade_log（唯一落点，避免散落的 UPDATE 写错字段）。"""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE biz.v2_trade_log SET protective_orders=%s::jsonb, "
            "updated_at=NOW() WHERE id=%s",
            (json.dumps(prot), tid))


def _restore_or_alert(client, conn, settings, t, prot, direction, entry, reason) -> bool:
    """「撤旧止损成功、挂新止损失败」的兜底：立刻按原 sl_pct 恢复硬止损。

    2026-10-12 DOSUSDT 事故根因（id=8，−45%）：持仓浮盈 +100% 触发保本上移 →
    旧止损（0.3799）撤掉、新止损挂单失败被外层 except 吞掉 → DB 里仍留着**旧的
    已失效记录**，看起来"有止损"，实则从 21:1x 一路裸奔到 21:45 崩盘。
    本函数杜绝该假象：
      - 恢复成功 → 写回原单，仓位重新有 -sl_pct 硬止损；
      - 恢复失败 → stop_loss 写 `{"error": ...}` 标记（下轮自愈分支会重挂）+
        邮件告警，绝不静默。
    返回是否恢复成功。
    """
    sl_pct = float(t.get("sl_pct") or 0)
    if sl_pct > 0:
        try:
            orig = client.place_sltp(t["symbol"], direction, entry, sl_pct * 100, is_stop=True)
            orig["restored"] = True
            prot["stop_loss"] = orig
            _save_prot(conn, t["id"], prot)
            print(f"  [ensure] 已恢复原硬止损 {t['symbol']} -{sl_pct*100:.0f}% "
                  f"id={t['id']}（{reason}）")
            return True
        except Exception as e2:  # noqa: BLE001
            err = f"{reason}；恢复原止损亦失败: {type(e2).__name__}: {e2}"
    else:
        err = f"{reason}；无 sl_pct 可恢复"
    prot["stop_loss"] = {"error": err}
    _save_prot(conn, t["id"], prot)
    print(f"  [ensure] ⚠️ 裸奔告警 {t['symbol']} id={t['id']}: {err}")
    _alert_protective_failed(
        settings,
        {"symbol": t["symbol"], "signal_type": t.get("signal_type") or "-",
         "direction": t.get("direction"), "price_usd": entry},
        err, "止损已撤且新挂/恢复均失败 → 该仓位当前无止损，下轮自愈分支将重挂")
    return False


def _ensure_protective(client, conn, settings) -> int:
    """保护单自愈 + 浮盈保本止损（--watch 每轮调用）。

    两类动作（幂等；单笔异常只打印不阻断整体；仅处理未平仓 bot 单）：

    1. 自愈补挂：**硬止损（STOP_MARKET）** 缺失或挂单失败（protective_orders 里
       stop_loss 是 error 或缺 orderId，如子账户 -4120 历史裸奔单）→ 按 sl_pct 重新
       补挂（binance_futures 已走 Algo Order API）。部署后首轮即给存量裸奔仓补挂 SL。
       存量「按数量」SL（closePosition=false，仓位页不显示止损价）一并重挂为 Close-All。
       **跟踪止盈由软件 TR 接管（_trail_check，峰值=入场后已收盘 5m/15m/1h bar 的
       max high，回调 tr_pct 触发），不再挂原生 TRAILING_STOP_MARKET** —— 2026-10-12 对齐回测：
       回测峰值从「入场后 bar 的 high」累积、实盘原生 TR 从「入场价」激活，导致
       「入场即回调≥3%再突破」的币实盘被 TR 打掉而回测吃到突破，故改用软件复刻回测。

    2. 浮盈保本：硬止损已就绪且**入场后峰值浮盈** ≥ BREAKEVEN_TRIGGER_PCT% → 撤旧止损、
       把硬止损上移到入场价 ± BREAKEVEN_COST_COVER_PCT（覆盖手续费+滑点的净保本线），
       锁盈利防跳空。浮盈用「入场后 5m/15m/1h bar 的最高价」（不是轮询时刻瞬时价，
       瞬时价会漏掉插针）；且仅当当前价仍在保本线盈利侧才上移（否则挂出即触发）。
       幂等：止损触发价已不低于保本线则跳过。正常回撤由软件 TR
       （峰值-3%）先兑现，保本止损只在跳空/闪崩跌破保本线时兜底。
       ⚠️ 「撤旧→挂新」不是原子操作：币安同一持仓侧只允许一张 closePosition 单，
       必须先撤后挂。故撤单失败即终止本次上移（旧单仍在）；**新单挂单失败则立刻恢复
       原硬止损**，恢复失败写 error 标记 + 邮件告警（`_restore_or_alert`）——绝不
       留下「DB 有止损记录、实际裸奔」的假象（2026-10-12 DOSUSDT −45% 事故）。

    返回处理笔数。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, symbol, direction, signal_type, open_ts, open_price, "
            "       sl_pct, tr_pct, protective_orders, "
            "       (SELECT COALESCE(MAX(k.high_px), 0) FROM biz.asset_klines k "
            "         WHERE k.symbol = t.symbol AND k.interval IN ('5m','15m','1h') "
            "           AND k.open_time >= t.open_ts) AS peak_intra, "
            "       (SELECT COALESCE(MIN(k.low_px), 1e18) FROM biz.asset_klines k "
            "         WHERE k.symbol = t.symbol AND k.interval IN ('5m','15m','1h') "
            "           AND k.open_time >= t.open_ts) AS trough_intra "
            "FROM biz.v2_trade_log t "
            "WHERE close_ts IS NULL AND origin='bot' "
            "ORDER BY open_ts")
        rows = [dict(zip([d.name for d in cur.description], r)) for r in cur.fetchall()]
    n = 0
    for t in rows:
        try:
            amt = client.get_position_amt(t["symbol"])
            if abs(amt) <= 0:
                continue  # 已平仓（TR/SL/到期），交给 _reconcile_closed 回填
            entry = float(t["open_price"] or 0)
            if entry <= 0:
                continue
            direction = "long" if str(t["direction"]).upper() == "LONG" else "short"
            prot = t.get("protective_orders") or {}
            sl = prot.get("stop_loss") or {}
            trl = prot.get("trailing_stop") or {}
            # 保护单是否有效（模块级 _prot_ok：algo 单只有 algoId/clientAlgoId）
            sl_missing = not _prot_ok(sl)
            # 存量迁移：历史挂的 SL 若是「按数量」条件单（closePosition=false），
            # 币安仓位页不显示止损价（只出现在「当前委托→条件委托」），易被误判为裸奔
            # （WUSDT id=5 实盘复现）→ 重挂为 Close-All，一次性收敛。
            sl_not_close_all = _prot_ok(sl) and sl.get("closePosition") is False

            # ── 1) 自愈补挂缺失硬止损（TR 由 _trail_check 软件接管，不补原生单）──
            if sl_missing or sl_not_close_all:
                # 清残留挂单（避免同侧多张 closePosition 单 -4045 冲突）
                _cancel_protective_orders(client, t["symbol"], prot)
                new_prot = prot.copy()
                # 保留已生效的原生 TR 记录（algoId 等）；仅当无有效原生 TR 时才标注软件 TR 接管
                if not _prot_ok(prot.get("trailing_stop") or {}):
                    new_prot["trailing_stop"] = {"mode": "software",
                                                 "tr_pct": float(t.get("tr_pct") or 0)}
                if (t.get("sl_pct") or 0) > 0:
                    try:
                        r = client.place_sltp(t["symbol"], direction, entry,
                                              float(t["sl_pct"]) * 100, is_stop=True)
                    except Exception as e:  # noqa: BLE001
                        # 补挂失败：写 error 标记（下轮本分支重试），**绝不保留
                        # "看起来有止损"的旧记录**（否则下一轮会被判为有效而永不重挂）。
                        new_prot["stop_loss"] = {
                            "error": f"recover_place_failed: {type(e).__name__}: {e}"}
                        _save_prot(conn, t["id"], new_prot)
                        print(f"  [ensure] 补挂硬止损失败 {t['symbol']} id={t['id']}: "
                              f"{type(e).__name__}: {e}")
                        continue  # 留待下轮重试
                    new_prot["stop_loss"] = r
                    _save_prot(conn, t["id"], new_prot)
                    why = "缺失" if sl_missing else "改 Close-All"
                    print(f"  [ensure] 补挂硬止损 {t['symbol']} id={t['id']} "
                          f"原因={why} sl=ok（TR=软件接管）")
                    n += 1
                continue  # 本轮先补挂，浮盈保本下轮评估

            # ── 2) 浮盈保本止损 ──
            # 浮盈口径 = 入场后已记录的峰值价（5m/15m/1h bar 的 high），不是轮询时刻
            # 瞬时价——瞬时价会漏掉插针（WUSDT id=5：07:25 插针 +5.32% 后回落至 +3~4%，
            # 保本损始终不触发）。空单用最低价。
            cur_px = client.get_price(t["symbol"])
            peak_intra = float(t.get("peak_intra") or 0)
            trough_intra = float(t.get("trough_intra") or 0)
            if direction == "long":
                ref_px = peak_intra if peak_intra > 0 else cur_px
                pnl = ref_px / entry - 1
            else:
                ref_px = trough_intra if 0 < trough_intra < 1e17 else cur_px
                pnl = 1 - ref_px / entry
            if pnl < BREAKEVEN_TRIGGER_PCT / 100:
                continue
            # 保本线 = 入场价 ± 成本覆盖（0.4%：0.1% 双边 taker + 0.3% 滑点预算），
            # 不是 0%（纯保本）——市价止损触发有滑点+手续费，停在入场价实际是亏。
            target_stop = (entry * (1 + BREAKEVEN_COST_COVER_PCT / 100)) if direction == "long" \
                else (entry * (1 - BREAKEVEN_COST_COVER_PCT / 100))
            # 仅在当前价处于保本线盈利侧才上移止损：否则挂出即触发市价止损（追保平仓）。
            if direction == "long" and cur_px <= target_stop:
                continue
            if direction == "short" and cur_px >= target_stop:
                continue
            # Algo 条件单的触发价字段是 triggerPrice（closePosition 单的 stopPrice 恒为 0），
            # 只看 stopPrice 会导致幂等判断永远落空 → 每轮撤单重挂止损（churn）。
            cur_stop = float(sl.get("triggerPrice") or sl.get("stopPrice") or 0)
            if cur_stop and ((direction == "long" and cur_stop >= target_stop) or
                             (direction == "short" and cur_stop <= target_stop)):
                continue  # 已到保本线，幂等跳过
            # 撤旧止损（algo 单按 algoId/clientAlgoId 撤，普通单按 orderId 撤）。
            # ⚠️ 撤单失败 → 旧止损仍在挂，**本轮放弃上移**：继续挂新单会与旧
            # closePosition 单 -4130 冲突，且会把「旧单仍有效」的真实状态搅乱。
            try:
                if sl.get("algoId"):
                    client.cancel_algo_order(algo_id=sl["algoId"])
                elif sl.get("clientAlgoId"):
                    client.cancel_algo_order(client_algo_id=sl["clientAlgoId"])
                elif sl.get("orderId"):
                    client.cancel_order(t["symbol"], sl["orderId"])
            except Exception as e:  # noqa: BLE001
                print(f"  [ensure] 撤旧止损失败 {t['symbol']}，跳过本次保本上移: "
                      f"{type(e).__name__}: {e}")
                continue
            # 挂保本止损（保留 TR 不动）；stop_price 直传目标触发价。
            # 不传 quantity → Close-All（仓位页可见 + 自动跟随仓位数量）。
            # ⚠️ 此处**旧止损已被撤掉**：挂单失败即裸奔，必须立刻恢复原硬止损
            # （DOSUSDT 事故就是这一步失败被吞掉、裸奔到崩盘 −45%）。
            try:
                new_sl = client.place_sltp(t["symbol"], direction, entry, 0.0,
                                           is_stop=True, stop_price=target_stop)
            except Exception as e:  # noqa: BLE001
                print(f"  [ensure] 保本止损挂单失败 {t['symbol']} id={t['id']}: "
                      f"{type(e).__name__}: {e}")
                _restore_or_alert(client, conn, settings, t, prot, direction, entry,
                                  f"保本上移({cur_stop}→{target_stop:.6g})挂单失败: "
                                  f"{type(e).__name__}: {e}")
                continue
            new_sl["breakeven"] = True
            new_sl["breakeven_trigger_pct"] = BREAKEVEN_TRIGGER_PCT
            new_sl["breakeven_ref"] = "peak_intra"
            new_sl["peak_price"] = ref_px
            new_sl["moved_from_pct"] = t.get("sl_pct")
            prot["stop_loss"] = new_sl
            _save_prot(conn, t["id"], prot)
            print(f"  [ensure] 浮盈保本 {t['symbol']} 峰值浮盈={pnl*100:+.1f}% "
                  f"(峰值价 {ref_px:.6g} 现价 {cur_px:.6g}) "
                  f"止损 {cur_stop}→{target_stop:.6g} id={t['id']}")
            n += 1
        except Exception as e:  # noqa: BLE001
            print(f"  [ensure] {t['symbol']} 异常: {type(e).__name__}: {e}")
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
                if rec["decision"] in ("ordered", "aborted", "skipped", "error"):
                    with get_connection(settings.database_url) as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                "UPDATE biz.scan_gainer_signal SET exec_state=%s, executed_at=%s "
                                "WHERE id=%s",
                                (rec["decision"], datetime.now(timezone.utc), sig["id"]),
                            )
                        # 实盘开仓 → 写 v2_trade_log（全量留痕，供追溯复验 + 完善策略）
                        # 只记机器人单（origin='bot'）；手动单不经过本脚本、不入本表。
                        # aborted = 保护单失败已回滚，同样留痕（exit_reason='aborted_protective'）。
                        if rec["decision"] in ("ordered", "aborted"):
                            sp = sig.get("params") or {}
                            eo = sp.get("event_bar_ts")
                            eo_dt = None
                            if eo:
                                try:
                                    eo_dt = datetime.fromisoformat(eo)
                                except (TypeError, ValueError):
                                    eo_dt = None
                            p = rec.get("params") or {}
                            aborted = rec["decision"] == "aborted"
                            # aborted（保护单失败已回滚）：不落成交价/量，close_ts=now，
                            # exit_reason='aborted_protective' 标记即时了结，留痕可追溯。
                            v2_open_price = None if aborted else rec.get("open_price")
                            v2_open_qty = None if aborted else rec.get("open_qty")
                            v2_close_ts = datetime.now(timezone.utc) if aborted else None
                            v2_exit_reason = "aborted_protective" if aborted else None
                            with conn.cursor() as cur:
                                cur.execute(
                                    "INSERT INTO biz.v2_trade_log ("
                                    " signal_id, symbol, signal_type, direction, open_ts,"
                                    " open_price, open_qty, notional_usdt, leverage, order_id,"
                                    " origin, signal_window, scan_ts, event_bar_ts, ref_price_usd,"
                                    " chg_24h_pct, chg1h_pct, vr_1h, funding_rate, vol_24h_usd,"
                                    " margin_usdt, planned_hold_h, sl_pct, tr_pct, tp_pct,"
                                    " entry_client_order_id, entry_resp, protective_orders,"
                                    " close_ts, exit_reason, base_url, dry_run, updated_at"
                                    ") VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                                    " %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,"
                                    " %s,%s,%s,%s,NOW()) "
                                    "ON CONFLICT (signal_id) DO NOTHING",
                                    (sig["id"], sig["symbol"], sig["signal_type"], sig["direction"],
                                     datetime.now(timezone.utc), v2_open_price,
                                     v2_open_qty, rec.get("notional"), rec.get("leverage"),
                                     rec.get("order_id"), "bot", sig.get("signal_window"),
                                     sig.get("scan_ts"), eo_dt, sig.get("price_usd"),
                                     sig.get("chg_24h_pct"), sp.get("chg1h_pct"), sp.get("vr_1h"),
                                     sig.get("funding_rate"), sig.get("vol_24h_usd"),
                                     rec.get("margin"), p.get("N"), p.get("SL"), p.get("TR"),
                                     p.get("TP"), rec.get("client_order_id"),
                                     json.dumps(rec.get("entry_resp")) if rec.get("entry_resp") else None,
                                     json.dumps(rec.get("protective")) if rec.get("protective") else None,
                                     v2_close_ts, v2_exit_reason,
                                     settings.binance_fapi_base_url, not live),
                                )
            if args.watch:
                # 保护单自愈（补挂缺失/失败硬止损，部署后即给存量裸奔仓补挂）+ 浮盈保本止损
                with get_connection(settings.database_url) as conn:
                    _ensure_protective(client, conn, settings)
                # 软件跟踪止盈（对齐回测 1h bar 峰值回撤：峰值=入场后已收盘 bar max high）
                with get_connection(settings.database_url) as conn:
                    _trail_check(client, conn)
                # 到期平仓（对齐回测：持有 N 小时未触发保护单 → 市价了结）
                with get_connection(settings.database_url) as conn:
                    close_expired(client, conn)
                # 对账已平仓持仓（TRAIL/SL/TP/到期 触发后补录盈亏）
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
