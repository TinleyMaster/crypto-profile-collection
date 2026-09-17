#!/usr/bin/env python3
"""盘面异动扫描 P4 · 执行层：主池高置信多头信号 → 风控 → dry-run/真实下单。

消费 biz.scan_signal（已告警、未执行、P↑OI↑ 多头），复用 auto_trade_signal.py
的风控/审计链路（BinanceFuturesClient + SignalSltpAdvisor + 盘面快照）：

流程：
    已告警主池 high 信号（exec_state IS NULL）→ 同币 12h 冷却 →
    现价 + 偏离校验 → 子账户权益 → AI 止损止盈（ATR 底线 + 2% 硬顶 + 兜底）→
    dry-run 打印意图 / live 真正下单 → 审计落库 + 回填 exec_state。

安全设计（与 auto_trade_signal.py 一致）：
  - 默认 dry-run：只打印「将开仓」，不真正下单、不标记 exec_state
  - 真正下单需要 --live 且 .env 里 SIGNAL_TRADE_ENABLED=1（双保险）
  - 只做多（P↑OI↑ 为唯一稳定正期望组合，空头侧回测全负，见设计方案 §8）
  - 子账户 API Key 只开「合约交易」权限、不开提现
  - 每笔信号写 biz.scan_auto_trade_log 审计（可追溯）

用法：
    python phase_execute_scan_signal.py                      # dry-run 扫待执行信号
    python phase_execute_scan_signal.py --window-hours 6     # 只看最近 6h 内信号
    python phase_execute_scan_signal.py --live               # 真正下单（需总开关）
    python phase_execute_scan_signal.py --test-connection    # 自检币安连通性
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg.rows  # noqa: E402

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.clients.binance_futures import (  # noqa: E402
    BinanceFuturesClient, BinanceFuturesError,
    _round_down_to_step, _round_price_to_tick,
)
from crypto_research.clients.signal_sltp import SignalSltpAdvisor  # noqa: E402
from crypto_research.clients.signal_market_data import (  # noqa: E402
    fetch_market_snapshot, to_prompt_text,
)

LOG_FILE = SCRIPT_DIR.parent / "data" / "phase_execute_scan_signal.log"
DEFAULT_WINDOW_HOURS = 6     # 只处理最近 N 小时内产生的信号（防陈旧）
COOLDOWN_H = 12              # 同币执行冷却（小时）
NOTIONAL_TIER = {"high": 1.0, "medium": 0.5}  # 置信度 → 名义价值档位系数

logger = logging.getLogger("phase_execute_scan_signal")


# ───────────────────────── 日志 ─────────────────────────
def setup_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.setLevel(logging.INFO)
    logger.addHandler(fh)
    logger.addHandler(sh)


# ───────────────────────── 候选信号 ─────────────────────────
def load_candidates(conn, window_hours: int) -> list[dict]:
    """拉取待执行信号：已告警 + 主池 high + P↑OI↑ 多头 + 未执行 + 未过期。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT id, signal_ts, symbol, scenario, p_dir, price_chg_pct,
                   vol_ratio, oi_dir, oi_chg_pct, cvd_dir, funding_rate,
                   confidence, context_tags, status, expired_at
            FROM biz.scan_signal
            WHERE pool = 'main'
              AND confidence = 'high'
              AND p_dir = 'up'
              AND scenario IN ('S1', 'S2')
              AND alerted_at IS NOT NULL
              AND exec_state IS NULL
              AND status = 'active'
              AND (expired_at IS NULL OR expired_at > NOW())
              AND signal_ts > NOW() - make_interval(hours => %s)
            ORDER BY signal_ts DESC
            """,
            (window_hours,),
        )
        return cur.fetchall()


def in_cooldown(conn, symbol: str) -> bool:
    """同币 12h 内是否已执行（exec_state 非空且 executed_at 在冷却窗口内）。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM biz.scan_signal
            WHERE symbol = %s AND exec_state IS NOT NULL
              AND executed_at > NOW() - make_interval(hours => %s)
            LIMIT 1
            """,
            (symbol, COOLDOWN_H),
        )
        return cur.fetchone() is not None


def _row_to_signal(row: dict[str, Any]) -> dict[str, Any]:
    """DB 行 → 交易信号 dict（供 SignalSltpAdvisor / 风控链路使用）。"""
    return {
        "scan_signal_id": int(row["id"]),
        "direction": "long",   # P↑OI↑ 只做多（回测唯一正期望）
        "symbol": row["symbol"],
        "scenario": row["scenario"],
        "p_dir": row["p_dir"],
        "price_chg_pct": float(row["price_chg_pct"] or 0),
        "vol_ratio": float(row["vol_ratio"] or 0),
        "oi_dir": row["oi_dir"],
        "oi_chg_pct": float(row["oi_chg_pct"] or 0),
        "cvd_dir": row["cvd_dir"],
        "funding_rate": float(row["funding_rate"]) if row.get("funding_rate") else None,
        "confidence": row["confidence"],
        "context_tags": row["context_tags"] or [],
        "signal_ts": row["signal_ts"],
        "entry_price": None,   # 无信号自带进场价 → 以现价为基准（下方查价）
        "raw_text": " / ".join([f"{s}=" + str(t) for s, t in zip(
            ["P", "VOL", "OI", "CVD"],
            [f"{row['p_dir']}{row['price_chg_pct']}%",
             f"{row['vol_ratio']}x", f"{row['oi_dir']}{row['oi_chg_pct']}%",
             row["cvd_dir"] or "-"])]) or "",
    }


# ───────────────────────── 审计 ─────────────────────────
def log_audit(conn, rec: dict[str, Any]) -> None:
    """写 biz.scan_auto_trade_log 并回填 scan_signal 执行状态。失败不阻塞交易。"""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO biz.scan_auto_trade_log
                    (signal_id, signal_ts, symbol, scenario, confidence, p_dir,
                     price_chg_pct, oi_chg_pct, trigger_price, stop_loss_pct,
                     take_profit_pct, sl_source, ai_reason, decision, skip_reason,
                     binance_order_id, order_side, order_type, order_qty, order_price)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (rec.get("signal_id"), rec.get("signal_ts"), rec.get("symbol"),
                 rec.get("scenario"), rec.get("confidence"), rec.get("p_dir"),
                 rec.get("price_chg_pct"), rec.get("oi_chg_pct"), rec.get("trigger_price"),
                 rec.get("stop_loss_pct"), rec.get("take_profit_pct"), rec.get("sl_source"),
                 rec.get("ai_reason"), rec.get("decision"), rec.get("skip_reason"),
                 rec.get("binance_order_id"), rec.get("order_side"), rec.get("order_type"),
                 rec.get("order_qty"), rec.get("order_price")),
            )
            # live 才回填执行状态（dry-run 不消费信号，供 live 后续处理）
            if rec.get("_mark_exec"):
                cur.execute(
                    """
                    UPDATE biz.scan_signal
                    SET exec_state = %s, executed_at = NOW()
                    WHERE id = %s
                    """,
                    (rec["decision"], rec["signal_id"]),
                )
    except Exception as e:
        logger.warning("[audit] 审计/回填失败（不影响交易）: %s", e)


# ───────────────────────── 交易执行 ─────────────────────────
def trade_signal(settings, signal: dict[str, Any], live: bool) -> dict[str, Any]:
    """对一个主池高置信信号执行风控 + dry-run/下单。返回决策结果。"""
    symbol = signal["symbol"]
    direction = signal["direction"]
    side = "BUY" if direction == "long" else "SELL"
    tier = NOTIONAL_TIER.get(signal["confidence"], 1.0)
    notional = settings.signal_max_notional_usdt * tier

    rec = {
        "signal_id": signal["scan_signal_id"],
        "signal_ts": signal["signal_ts"],
        "symbol": symbol,
        "scenario": signal["scenario"],
        "confidence": signal["confidence"],
        "p_dir": signal["p_dir"],
        "price_chg_pct": signal["price_chg_pct"],
        "oi_chg_pct": signal["oi_chg_pct"],
        "trigger_price": None, "stop_loss_pct": None, "take_profit_pct": None,
        "sl_source": None, "ai_reason": None,
        "decision": "skipped", "skip_reason": "",
        "binance_order_id": None, "order_side": side, "order_type": None,
        "order_qty": None, "order_price": None,
        "_mark_exec": False,
    }

    # 1. 构造交易客户端
    if not (settings.binance_api_key and settings.binance_api_secret):
        rec["skip_reason"] = "未配置 BINANCE_API_KEY/SECRET"
        return rec
    client = BinanceFuturesClient(
        settings.binance_api_key, settings.binance_api_secret,
        base_url=settings.binance_fapi_base_url,
    )

    # 2. 现价（触发价基准 = 现价，盘面信号无自带进场价）
    try:
        cur_price = client.get_price(symbol)
    except BinanceFuturesError as e:
        rec["skip_reason"] = f"查价失败: {e}"
        return rec
    rec["trigger_price"] = cur_price

    # 3. 子账户权益（2% 硬顶换算需要）+ 余额校验（live 强校验）
    equity: float | None = None
    try:
        equity = client.get_balance("USDT")
    except BinanceFuturesError as e:
        if live:
            rec["skip_reason"] = f"余额查询失败: {e}"
            return rec
        logger.warning("余额查询失败（dry-run 继续）: %s", e)
    if live and equity is not None:
        need_margin = notional / max(settings.signal_leverage, 1)
        if equity < need_margin:
            rec["skip_reason"] = (f"子账户可用余额 {equity:.2f} USDT < 所需保证金 "
                                  f"{need_margin:.2f}，跳过")
            return rec

    # 4. 盘面快照 + AI 止损止盈顾问
    snap = fetch_market_snapshot(settings, symbol)
    context_text = to_prompt_text(snap)
    atr_pct = snap.get("atr_pct")
    advisor = SignalSltpAdvisor(settings)
    sugg = advisor.suggest(signal, cur_price, equity, notional,
                           context_text=context_text, atr_pct=atr_pct)

    if not live:
        rec["decision"] = "dry_run"
        rec["order_type"] = settings.signal_order_type
        rec["order_qty"] = round(notional / cur_price, 6)
        rec["order_price"] = cur_price
        rec["stop_loss_pct"] = sugg["stop_loss_pct"]
        rec["take_profit_pct"] = sugg["take_profit_pct"]
        rec["sl_source"] = sugg["source"]
        rec["ai_reason"] = sugg["reason"][:300]
        logger.info(
            "[DRY-RUN] 将开多 %s [%s] 现价=%s 名义=%.2fUSDT 杠杆=%sx | "
            "止损=%.2f%%(来源=%s,底线=%.2f%%,硬顶=%.2f%%) 止盈=%.2f%% 理由=%s",
            symbol, signal["scenario"], cur_price, notional, settings.signal_leverage,
            sugg["stop_loss_pct"] or 0, sugg["source"], sugg["floor_sl_pct"],
            sugg["cap_sl_pct"], sugg["take_profit_pct"] or 0, sugg["reason"][:150],
        )
        return rec

    # 5. 真正下单（复用 auto_trade_signal 顺序：入场 → 兜底止损 → AI → 替换 + 止盈）
    try:
        client.set_leverage(symbol, settings.signal_leverage)
        info = client.get_exchange_info(symbol)
        if settings.signal_order_type == "limit":
            order_price = _round_price_to_tick(cur_price, info["tick_size"])
            order_qty = _round_down_to_step(notional / order_price, info["step_size"])
            result = client.place_order(
                symbol, side, "LIMIT", quantity=order_qty, price=order_price,
                position_side="LONG",
            )
            entry_used = order_price
        else:
            order_qty = _round_down_to_step(notional / cur_price, info["step_size"])
            result = client.place_order(
                symbol, side, "MARKET", quantity=order_qty, position_side="LONG",
            )
            entry_used = cur_price
        rec["decision"] = "ordered"
        rec["order_type"] = result.get("type") or settings.signal_order_type
        rec["order_qty"] = order_qty
        rec["order_price"] = result.get("price") or entry_used
        rec["binance_order_id"] = result.get("orderId")
        rec["_mark_exec"] = True

        # 5.1 兜底止损立即挂（防 AI 返回前裸奔）
        fallback_pct = settings.signal_fallback_stop_loss_pct
        fallback_sl_order: dict | None = None
        if fallback_pct > 0:
            fallback_sl_order = client.place_sltp(symbol, direction, entry_used, fallback_pct, is_stop=True)
            rec["stop_loss_pct"] = fallback_pct
            rec["sl_source"] = "fallback"

        # 5.2 AI 止损止盈
        sl_pct = sugg["stop_loss_pct"]
        tp_pct = sugg["take_profit_pct"]
        rec["stop_loss_pct"] = sl_pct or rec.get("stop_loss_pct")
        rec["take_profit_pct"] = tp_pct
        rec["sl_source"] = sugg["source"]
        rec["ai_reason"] = sugg["reason"][:300]

        # 5.3 撤兜底换 AI 止损（不同才换）
        if sl_pct and fallback_sl_order and abs(sl_pct - fallback_pct) > 0.01:
            try:
                client.cancel_order(symbol, fallback_sl_order.get("orderId"))
            except BinanceFuturesError as e:
                logger.warning("[LIVE] 撤兜底止损失败（保留兜底）: %s", e)
                rec["stop_loss_pct"] = fallback_pct
            else:
                client.place_sltp(symbol, direction, entry_used, sl_pct, is_stop=True)
        elif sl_pct and not fallback_sl_order:
            client.place_sltp(symbol, direction, entry_used, sl_pct, is_stop=True)
        # 5.4 止盈
        if tp_pct:
            client.place_sltp(symbol, direction, entry_used, tp_pct, is_stop=False)

        logger.info(
            "[LIVE] 已开多 %s [%s] 订单号=%s 数量=%s 价格=%s | 止损=%.2f%%(来源=%s) 止盈=%.2f%% 理由=%s",
            symbol, signal["scenario"], result.get("orderId"), order_qty, rec["order_price"],
            rec["stop_loss_pct"] or 0, rec["sl_source"], tp_pct or 0, sugg["reason"][:150],
        )
    except BinanceFuturesError as e:
        rec["decision"] = "error"
        rec["skip_reason"] = f"下单失败: {e}"
        logger.error("[LIVE] 下单失败 %s: %s", symbol, e)
    except Exception as e:
        rec["decision"] = "error"
        rec["skip_reason"] = f"下单异常: {e}"
        logger.error("[LIVE] 下单异常 %s: %s", symbol, e)
    return rec


# ───────────────────────── 主流程 ─────────────────────────
def run_once(settings, window_hours: int, live: bool) -> int:
    from crypto_research.db.conn import get_connection
    with get_connection(settings.database_url) as conn:
        candidates = load_candidates(conn, window_hours)
        to_do = [c for c in candidates if not in_cooldown(conn, c["symbol"])]
        print(f"[exec] 候选 {len(candidates)} 条 → 冷却后 {len(to_do)} 条（仅主池 high P↑OI↑ 多头）")
        for c in to_do:
            print(f"  #{c['id']} {c['symbol']} {c['scenario']} P={c['p_dir']}{c['price_chg_pct']}% "
                  f"OI={c['oi_dir']}({c['oi_chg_pct']}%) CVD={c['cvd_dir']} conf={c['confidence']}")

        processed = 0
        for c in to_do:
            signal = _row_to_signal(c)
            rec = trade_signal(settings, signal, live=live)
            log_audit(conn, rec)
            if rec["decision"] in ("ordered", "dry_run"):
                processed += 1
        return processed


def test_connection(settings) -> int:
    """复用 auto_trade_signal 的自检（连通 + 账户 + 持仓模式 + 风控配置）。"""
    import auto_trade_signal
    code = auto_trade_signal.test_connection(settings)
    # 追加 P4 专属说明
    print()
    print("  [P4 执行层] 消费主池 high P↑OI↑ 多头信号（已告警、未执行）")
    print("  [P4 执行层] dry-run 不标记；--live + SIGNAL_TRADE_ENABLED=1 才下单并回填 exec_state")
    return code


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="盘面主池高置信信号 → 币安子账户执行（dry-run 默认）")
    parser.add_argument("--watch", action="store_true", help="常驻轮询模式")
    parser.add_argument("--interval", type=int, default=300, help="轮询间隔秒数（默认300）")
    parser.add_argument("--window-hours", type=int, default=DEFAULT_WINDOW_HOURS,
                        help=f"只处理最近 N 小时内信号（默认 {DEFAULT_WINDOW_HOURS}）")
    parser.add_argument("--live", action="store_true", help="真正下单（需 SIGNAL_TRADE_ENABLED=1）")
    parser.add_argument("--test-connection", action="store_true", help="自检币安连通性")
    args = parser.parse_args()

    settings = get_settings(require_database=False)

    if args.test_connection:
        sys.exit(test_connection(settings))

    live = args.live and settings.signal_trade_enabled
    if args.live and not settings.signal_trade_enabled:
        logger.warning("--live 已指定，但 SIGNAL_TRADE_ENABLED 未设为 1，仍按 dry-run 执行")

    logger.info("模式=%s 窗口=%sh 单笔名义=%.0fUSDT 杠杆=%s 下单方式=%s 兜底止损=%s%% 硬顶=%s%%",
                "LIVE" if live else "DRY-RUN", args.window_hours,
                settings.signal_max_notional_usdt, settings.signal_leverage,
                settings.signal_order_type, settings.signal_fallback_stop_loss_pct,
                settings.signal_max_loss_pct_of_equity)

    if args.watch:
        logger.info("常驻轮询开始，间隔 %ss（Ctrl+C 停止）", args.interval)
        while True:
            try:
                run_once(settings, args.window_hours, live=live)
            except KeyboardInterrupt:
                logger.info("收到退出信号，停止")
                break
            except Exception as e:
                logger.error("轮询异常（稍后重试）: %s", e)
            time.sleep(args.interval)
    else:
        run_once(settings, args.window_hours, live=live)


if __name__ == "__main__":
    main()
