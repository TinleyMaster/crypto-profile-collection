#!/usr/bin/env python3
"""KOL 实时喊单信号 → 币安子账户自动开单（DB 驱动）。

流程（读库，不走邮件）：
    爬虫抓帖 → AI 分类(biz.kol_signal.post_type='prediction' 实时喊单) →
    本脚本轮询 kol_signal → 去重(is_auto_traded) + 同币冷却 →
    风控校验（单笔名义上限 / 现价偏离 / 余额）→ 盘面快照 + AI 止损止盈 →
    币安合约挂单 → 落库审计。

为何读库而非邮件：邮件是项目 KOL 模块自己发的（workbench/kol/notifier.py），
且发邮件时已含 analysis（行情分析）类型；只有 AI 分类器确认的 post_type='prediction'
才允许自动开单，避免把事后晒单/行情分析当实时喊单。

安全设计：
- 默认 dry-run，只打印「将开仓」，不真正下单、不标记 is_auto_traded
- 真正下单需要 `--live` 且 .env 里 SIGNAL_TRADE_ENABLED=1（双保险）
- 只处理 post_type='prediction'（实时喊单）+ direction long/short + entry_price 非空
- 子账户 API Key 只开「合约交易」权限、不开提现，隔离主账户资金
- 每笔信号记录审计日志（DB + 本地 JSON 状态），可追溯

用法：
    python auto_trade_signal.py                      # 一次：扫待处理信号（dry-run）
    python auto_trade_signal.py --watch --interval 60   # 常驻轮询（dry-run）
    python auto_trade_signal.py --live                # 真正下单（需开总开关）
    python auto_trade_signal.py --test-connection     # 自检币安连通性
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

# 路径：复用 scripts/bin 下其它脚本的做法
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.clients.binance_futures import (  # noqa: E402
    BinanceFuturesClient, BinanceFuturesError,
    _round_down_to_step, _round_price_to_tick,
)
from crypto_research.clients.signal_sltp import SignalSltpAdvisor  # noqa: E402
from crypto_research.clients.signal_market_data import (  # noqa: E402
    fetch_market_snapshot, to_prompt_text,
)

# 币种 → 合约代码映射（其余一律补 USDT 后缀）
SYMBOL_SUFFIX_OVERRIDE = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
                          "BNB": "BNBUSDT", "XRP": "XRPUSDT", "DOGE": "DOGEUSDT",
                          "ADA": "ADAUSDT", "AVAX": "AVAXUSDT", "LINK": "LINKUSDT"}

STATE_DIR = SCRIPT_DIR.parent / "data"
STATE_FILE = STATE_DIR / "signal_trade_state.json"
LOG_FILE = STATE_DIR / "auto_trade_signal.log"

logger = logging.getLogger("auto_trade_signal")


# ───────────────────────── 日志 ─────────────────────────
def setup_logging() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.setLevel(logging.INFO)
    logger.addHandler(fh)
    logger.addHandler(sh)


# ───────────────────────── 状态（同币冷却）─────────────────────────
def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def in_cooldown(state: dict[str, Any], symbol: str, minutes: int) -> bool:
    if minutes <= 0:
        return False
    last = state.get("last_symbol_trade", {}).get(symbol)
    if not last:
        return False
    try:
        last_dt = datetime.fromisoformat(last)
    except Exception:
        return False
    return datetime.now(timezone.utc) - last_dt < timedelta(minutes=minutes)


# ───────────────────────── 币种映射 ─────────────────────────
def _to_futures_symbol(coin: str) -> str:
    coin = coin.upper().strip()
    if coin in SYMBOL_SUFFIX_OVERRIDE:
        return SYMBOL_SUFFIX_OVERRIDE[coin]
    if coin.endswith("USDT"):
        return coin
    return coin + "USDT"


# ───────────────────────── 数据库：待处理信号 ─────────────────────────
def ensure_schema(settings) -> None:
    """幂等建表/加列：kol_signal.is_auto_traded + 审计表 kol_signal_id。"""
    if not settings.database_url:
        return
    try:
        from crypto_research.db.conn import get_connection
        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    ALTER TABLE biz.kol_signal
                        ADD COLUMN IF NOT EXISTS is_auto_traded BOOLEAN NOT NULL DEFAULT FALSE
                """)
                cur.execute("""
                    ALTER TABLE biz.kol_auto_trade_log
                        ADD COLUMN IF NOT EXISTS kol_signal_id BIGINT
                """)
    except Exception as e:
        logger.warning("[schema] 幂等加列失败（不影响运行）: %s", e)


def fetch_pending_signals(settings) -> list[dict[str, Any]]:
    """拉取待自动开单的实时喊单信号（post_type='prediction'）。"""
    whitelist = settings.signal_kol_whitelist
    params: dict[str, Any] = {
        "min_conf": settings.signal_min_confidence,
        "max_age_hours": str(settings.signal_max_age_hours),
    }
    whitelist_sql = ""
    if whitelist:
        whitelist_sql = "AND pr.nickname = ANY(%(whitelist)s::text[])"
        params["whitelist"] = whitelist

    sql = f"""
        SELECT s.signal_id, s.direction, s.symbol, s.entry_price, s.stop_loss,
               s.take_profit, s.leverage, s.confidence, s.entry_condition,
               s.resistance_level, s.support_level,
               p.content_text, p.posted_at, p.post_url,
               pr.nickname, pr.win_rate
        FROM biz.kol_signal s
        JOIN biz.kol_post p ON s.post_id = p.post_id
        JOIN biz.kol_profile pr ON s.profile_id = pr.profile_id
        WHERE s.post_type = 'prediction'
          AND s.direction IN ('long', 'short')
          AND s.entry_price IS NOT NULL
          AND s.already_entered = FALSE
          AND COALESCE(s.signal_category, 'trading') <> 'onchain'
          AND s.confidence >= %(min_conf)s
          AND s.is_auto_traded = FALSE
          AND pr.is_active = TRUE
          AND pr.kol_type = 'kol'
          AND p.posted_at >= NOW() - (%(max_age_hours)s || ' hours')::interval
          {whitelist_sql}
        ORDER BY p.posted_at ASC
    """
    try:
        from crypto_research.db.conn import get_connection
        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                cols = [d.name for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        logger.error("[db] 拉取待处理信号失败: %s", e)
        return []


def mark_signal_auto_traded(settings, signal_id: int) -> None:
    """标记信号已处理（live 模式调用；dry-run 不标记，供 live 后续处理）。"""
    try:
        from crypto_research.db.conn import get_connection
        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE biz.kol_signal
                    SET is_auto_traded = TRUE, updated_at = NOW()
                    WHERE signal_id = %s
                """, (int(signal_id),))
    except Exception as e:
        logger.warning("[db] 标记 is_auto_traded 失败 signal_id=%s: %s", signal_id, e)


def _row_to_signal(row: dict[str, Any]) -> dict[str, Any]:
    """DB 行 → trade_signal 需要的信号 dict。"""
    return {
        "kol_signal_id": int(row["signal_id"]),
        "kol_name": row["nickname"],
        "direction": row["direction"],  # long / short
        "symbol": _to_futures_symbol(row["symbol"] or ""),
        "entry_price": float(row["entry_price"]),
        "stop_loss": float(row["stop_loss"]) if row.get("stop_loss") else None,
        "take_profit": float(row["take_profit"]) if row.get("take_profit") else None,
        "leverage_signal": float(row["leverage"]) if row.get("leverage") else None,
        "win_rate": float(row["win_rate"]) if row.get("win_rate") else None,
        "confidence": float(row["confidence"]) if row.get("confidence") else None,
        "raw_text": (row["content_text"] or "")[:2000],
        "publish_time": row["posted_at"],
        "post_url": row["post_url"],
        "entry_condition": row["entry_condition"] or "",
        "resistance_level": float(row["resistance_level"]) if row.get("resistance_level") else None,
        "support_level": float(row["support_level"]) if row.get("support_level") else None,
    }


# ───────────────────────── 审计（DB + 本地）─────────────────────────
def log_audit_db(settings, rec: dict[str, Any]) -> None:
    """写入 biz.kol_auto_trade_log；失败不影响交易，仅告警。"""
    if not settings.database_url:
        return
    try:
        from crypto_research.db.conn import get_connection
        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS biz.kol_auto_trade_log (
                        id BIGSERIAL PRIMARY KEY,
                        kol_signal_id BIGINT,
                        email_message_id TEXT,
                        received_at TIMESTAMPTZ,
                        signal_time TIMESTAMPTZ,
                        kol_name TEXT,
                        symbol TEXT,
                        direction TEXT,
                        entry_price NUMERIC(20,8),
                        win_rate NUMERIC(5,2),
                        raw_text TEXT,
                        decision TEXT,
                        skip_reason TEXT,
                        binance_order_id TEXT,
                        order_side TEXT,
                        order_type TEXT,
                        order_qty NUMERIC(20,8),
                        order_price NUMERIC(20,8),
                        sl_pct NUMERIC(8,2),
                        tp_pct NUMERIC(8,2),
                        sl_source TEXT,
                        ai_reason TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                """)
                cur.execute("""
                    ALTER TABLE biz.kol_auto_trade_log
                        ADD COLUMN IF NOT EXISTS kol_signal_id BIGINT,
                        ADD COLUMN IF NOT EXISTS sl_pct NUMERIC(8,2),
                        ADD COLUMN IF NOT EXISTS tp_pct NUMERIC(8,2),
                        ADD COLUMN IF NOT EXISTS sl_source TEXT,
                        ADD COLUMN IF NOT EXISTS ai_reason TEXT
                """)
                cur.execute("""
                    INSERT INTO biz.kol_auto_trade_log
                        (kol_signal_id, email_message_id, received_at, signal_time, kol_name,
                         symbol, direction, entry_price, win_rate, raw_text, decision,
                         skip_reason, binance_order_id, order_side, order_type,
                         order_qty, order_price, sl_pct, tp_pct, sl_source, ai_reason)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    rec.get("kol_signal_id"), rec.get("message_id"), rec.get("received_at"),
                    rec.get("signal_time"), rec.get("kol_name"),
                    rec.get("symbol"), rec.get("direction"), rec.get("entry_price"),
                    rec.get("win_rate"), rec.get("raw_text"), rec.get("decision"),
                    rec.get("skip_reason"), rec.get("binance_order_id"), rec.get("order_side"),
                    rec.get("order_type"), rec.get("order_qty"), rec.get("order_price"),
                    rec.get("sl_pct"), rec.get("tp_pct"), rec.get("sl_source"),
                    rec.get("ai_reason"),
                ))
    except Exception as e:
        logger.warning("[audit] DB 审计失败（不影响交易）: %s", e)


# ───────────────────────── 交易执行 ─────────────────────────
def build_market_context(settings, signal: dict[str, Any]) -> tuple[str, float | None]:
    """构造 AI 参考的盘面快照 + 信号自带信息。返回 (上下文文本, ATR%)。"""
    snap = fetch_market_snapshot(settings, signal["symbol"])
    text = to_prompt_text(snap)
    atr_pct = snap.get("atr_pct")
    logger.info("[盘面快照] %s ATR=%.3f%% | %s", signal["symbol"], atr_pct or 0,
                text.replace("\n", "；"))

    # 信号自带的关键位/止损止盈（KOL 给的，AI 可参考）
    extra = []
    if signal.get("entry_condition"):
        extra.append(f"信号入场条件：{signal['entry_condition']}")
    if signal.get("resistance_level"):
        extra.append(f"信号压力位：{signal['resistance_level']}")
    if signal.get("support_level"):
        extra.append(f"信号支撑位：{signal['support_level']}")
    if signal.get("stop_loss"):
        extra.append(f"信号自带止损：{signal['stop_loss']}")
    if signal.get("take_profit"):
        extra.append(f"信号自带止盈：{signal['take_profit']}")
    if extra:
        text = "\n".join(extra) + "\n" + text
    return text, atr_pct


def trade_signal(settings, signal: dict[str, Any], live: bool) -> dict[str, Any]:
    """对一个实时喊单信号执行风控 + 下单。返回决策结果。

    止损止盈（AI + 2% 硬顶 + 兜底）时序：
    入场单先行（抢进场位）→ 兜底止损立即挂（防裸奔）→ AI 建议 →
    撤兜底换 AI 止损（clamp 到硬顶）→ 挂止盈。
    """
    symbol = signal["symbol"]
    direction = signal["direction"]
    entry = signal["entry_price"]
    side = "BUY" if direction == "long" else "SELL"
    notional = settings.signal_max_notional_usdt

    rec = {
        "kol_signal_id": signal["kol_signal_id"],
        "message_id": f"kol_signal:{signal['kol_signal_id']}",
        "received_at": signal["publish_time"],
        "signal_time": signal["publish_time"],
        "kol_name": signal["kol_name"],
        "symbol": symbol,
        "direction": direction,
        "entry_price": entry,
        "win_rate": signal["win_rate"],
        "raw_text": signal["raw_text"][:2000],
        "decision": "skipped",
        "skip_reason": "",
        "sl_pct": None, "tp_pct": None, "sl_source": None, "ai_reason": None,
    }

    # 1. 构造交易客户端
    if not (settings.binance_api_key and settings.binance_api_secret):
        rec["skip_reason"] = "未配置 BINANCE_API_KEY/SECRET"
        return rec
    client = BinanceFuturesClient(
        settings.binance_api_key, settings.binance_api_secret,
        base_url=settings.binance_fapi_base_url,
    )

    # 2. 现价 + 偏离校验
    try:
        cur = client.get_price(symbol)
    except BinanceFuturesError as e:
        rec["skip_reason"] = f"查价失败: {e}"
        return rec
    dev_pct = abs(cur - entry) / entry * 100 if entry else 0.0

    if settings.signal_order_type == "market":
        if dev_pct > settings.signal_max_price_deviation_pct:
            rec["skip_reason"] = (f"市价单偏离进场价 {dev_pct:.2f}% > "
                                  f"{settings.signal_max_price_deviation_pct}%，跳过")
            return rec
    else:
        # 限价单：只拦截「已突破进场位」方向
        if direction == "short" and cur > entry * (1 + settings.signal_max_price_deviation_pct / 100):
            rec["skip_reason"] = (f"现价 {cur} 已突破进场位 {entry} 上方 "
                                  f"{settings.signal_max_price_deviation_pct}%，不再追空")
            return rec
        if direction == "long" and cur < entry * (1 - settings.signal_max_price_deviation_pct / 100):
            rec["skip_reason"] = (f"现价 {cur} 已跌破进场位 {entry} 下方 "
                                  f"{settings.signal_max_price_deviation_pct}%，不再追多")
            return rec

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

    # 4. AI 止损止盈顾问 + 盘面快照
    advisor = SignalSltpAdvisor(settings)
    context_text, atr_pct = build_market_context(settings, signal)

    # 5. dry-run：只算 AI 建议并打印意图
    if not live:
        sugg = advisor.suggest(signal, cur, equity, notional,
                               context_text=context_text, atr_pct=atr_pct)
        rec["decision"] = "dry_run"
        rec["order_side"] = side
        rec["order_type"] = settings.signal_order_type
        rec["order_qty"] = round(notional / entry, 6)
        rec["order_price"] = entry
        rec["sl_pct"] = sugg["stop_loss_pct"]
        rec["tp_pct"] = sugg["take_profit_pct"]
        rec["sl_source"] = sugg["source"]
        rec["ai_reason"] = sugg["reason"][:300]
        logger.info(
            "[DRY-RUN] 将开仓 %s %s 方向=%s 进场=%s 现价=%s(偏离%.2f%%) 名义=%.2fUSDT 杠杆=%s | "
            "止损=%.2f%%(来源=%s,底线=%.2f%%,硬顶=%.2f%%) 止盈=%.2f%% 理由=%s",
            symbol, settings.signal_order_type, direction, entry, cur, dev_pct,
            notional, settings.signal_leverage,
            sugg["stop_loss_pct"] or 0, sugg["source"], sugg["floor_sl_pct"],
            sugg["cap_sl_pct"],
            sugg["take_profit_pct"] or 0, sugg["reason"][:150],
        )
        return rec

    # 6. 真正下单：入场单先行 → 兜底止损立即挂 → AI 建议 → 替换止损 + 挂止盈
    try:
        client.set_leverage(symbol, settings.signal_leverage)
        info = client.get_exchange_info(symbol)
        if settings.signal_order_type == "limit":
            order_price = _round_price_to_tick(entry, info["tick_size"])
            order_qty = _round_down_to_step(notional / order_price, info["step_size"])
            result = client.place_order(
                symbol, side, "LIMIT", quantity=order_qty, price=order_price,
                position_side="LONG" if direction == "long" else "SHORT",
            )
            entry_used = order_price
        else:
            order_qty = _round_down_to_step(notional / cur, info["step_size"])
            result = client.place_order(
                symbol, side, "MARKET", quantity=order_qty,
                position_side="LONG" if direction == "long" else "SHORT",
            )
            entry_used = cur
        rec["decision"] = "ordered"
        rec["order_side"] = side
        rec["order_type"] = result.get("type") or settings.signal_order_type
        rec["order_qty"] = order_qty
        rec["order_price"] = result.get("price") or entry_used
        rec["binance_order_id"] = result.get("orderId")

        # 6.1 兜底止损立即挂（防止入场成交后、AI 返回前出现裸奔）
        fallback_pct = settings.signal_fallback_stop_loss_pct
        fallback_sl_order: dict | None = None
        if fallback_pct > 0:
            fallback_sl_order = client.place_sltp(symbol, direction, entry_used, fallback_pct, is_stop=True)
            rec["sl_pct"] = fallback_pct
            rec["sl_source"] = "fallback"

        # 6.2 AI 建议（此时兜底止损已生效，AI 推理不阻塞开仓）
        sugg = advisor.suggest(signal, cur, equity, notional,
                               context_text=context_text, atr_pct=atr_pct)
        sl_pct = sugg["stop_loss_pct"]
        tp_pct = sugg["take_profit_pct"]
        rec["sl_pct"] = sl_pct or rec.get("sl_pct")
        rec["tp_pct"] = tp_pct
        rec["sl_source"] = sugg["source"]
        rec["ai_reason"] = sugg["reason"][:300]

        # 6.3 撤兜底止损、换成 AI 止损（不同才换，保持连贯性）
        if sl_pct and fallback_sl_order and abs(sl_pct - fallback_pct) > 0.01:
            try:
                client.cancel_order(symbol, fallback_sl_order.get("orderId"))
            except BinanceFuturesError as e:
                logger.warning("[LIVE] 撤兜底止损失败（保留兜底）: %s", e)
                sl_pct = fallback_pct
            else:
                client.place_sltp(symbol, direction, entry_used, sl_pct, is_stop=True)
        elif sl_pct and not fallback_sl_order:
            client.place_sltp(symbol, direction, entry_used, sl_pct, is_stop=True)
        # 6.4 止盈（AI 给才挂；AI 失败无 TP，属正常降级）
        if tp_pct:
            client.place_sltp(symbol, direction, entry_used, tp_pct, is_stop=False)

        logger.info(
            "[LIVE] 已开仓 %s %s 订单号=%s 数量=%s 价格=%s | 止损=%.2f%%(来源=%s) 止盈=%.2f%% 理由=%s",
            symbol, side, result.get("orderId"), order_qty, rec["order_price"],
            rec["sl_pct"] or 0, rec["sl_source"], tp_pct or 0, sugg["reason"][:150],
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
def process_db_signal(settings, row: dict[str, Any], state: dict[str, Any],
                      live: bool) -> dict[str, Any] | None:
    """处理一条 DB 信号：冷却 → 风控 → 下单 → 审计 → 标记已处理。"""
    signal = _row_to_signal(row)
    signal_id = signal["kol_signal_id"]

    if not signal["symbol"] or not signal["entry_price"]:
        logger.warning("[db] 信号缺 symbol/entry_price，跳过 signal_id=%s", signal_id)
        if live:
            mark_signal_auto_traded(settings, signal_id)
        return None

    # 同币冷却
    if in_cooldown(state, signal["symbol"], settings.signal_cooldown_minutes):
        last = state.get("last_symbol_trade", {}).get(signal["symbol"])
        logger.warning("[cooldown] %s 冷却期内（上次 %s），跳过 signal_id=%s",
                       signal["symbol"], last, signal_id)
        rec = {
            "kol_signal_id": signal_id,
            "message_id": f"kol_signal:{signal_id}",
            "received_at": signal["publish_time"],
            "signal_time": signal["publish_time"],
            "kol_name": signal["kol_name"],
            "symbol": signal["symbol"],
            "direction": signal["direction"],
            "entry_price": signal["entry_price"],
            "win_rate": signal["win_rate"],
            "raw_text": signal["raw_text"][:2000],
            "decision": "skipped",
            "skip_reason": "同币种冷却期内",
        }
        log_audit_db(settings, rec)
        if live:
            mark_signal_auto_traded(settings, signal_id)
        return rec

    rec = trade_signal(settings, signal, live=live)

    # 记录冷却（无论 dry-run/live，进入过交易逻辑就记，防重复处理）
    if rec["decision"] in ("ordered", "dry_run"):
        state.setdefault("last_symbol_trade", {})
        state["last_symbol_trade"][signal["symbol"]] = datetime.now(timezone.utc).isoformat()

    log_audit_db(settings, rec)

    # live 才标记已处理（dry-run 不消费信号，供后续 live 处理）
    if live and rec["decision"] != "error":
        mark_signal_auto_traded(settings, signal_id)

    if rec["decision"] == "dry_run" or rec["decision"] == "ordered":
        logger.info("→ %s: %s", signal["symbol"], rec["decision"])
    elif rec["decision"] == "skipped":
        logger.info("→ %s: 跳过（%s）", signal["symbol"], rec["skip_reason"])
    elif rec["decision"] == "error":
        logger.info("→ %s: 失败（%s）", signal["symbol"], rec["skip_reason"])

    return rec


def run_once_db(settings, live: bool) -> int:
    """跑一轮：查库 → 处理所有待开单信号。返回处理条数。"""
    ensure_schema(settings)
    rows = fetch_pending_signals(settings)
    if not rows:
        logger.info("暂无待处理的实时喊单信号")
        return 0
    logger.info("[db] 待处理实时喊单信号：%s 条", len(rows))
    for row in rows:
        logger.info("[signal] #%s %s %s %s@%s 置信度=%s%% 原文=%s",
                    row["signal_id"],
                    row["direction"], row["symbol"],
                    row["entry_price"],
                    row["posted_at"].astimezone(timezone(timedelta(hours=8))).strftime("%m-%d %H:%M")
                    if row["posted_at"] else "?",
                    round(float(row["confidence"] or 0) * 100),
                    (row["content_text"] or "")[:80])
    state = load_state()
    processed = 0
    for row in rows:
        try:
            rec = process_db_signal(settings, row, state, live=live)
        except Exception as e:
            logger.error("处理信号异常 signal_id=%s: %s", row.get("signal_id"), e)
            continue
        if rec is not None:
            processed += 1
    save_state(state)
    return processed


def test_connection(settings) -> int:
    """全面自检：币安连通 + 账户明细 + 持仓模式 + 风控配置一览。

    返回 0 = 全部正常，1 = 有异常（异常项会以 ❌ 标出）。
    """
    code = 0

    # ── 1. 风控配置快照（不管币安连不连得上都打）──
    whitelist = "、".join(settings.signal_kol_whitelist) if settings.signal_kol_whitelist else "（空=全部）"
    print()
    print("=" * 64)
    print("  📋 自动交易配置总览")
    print("=" * 64)
    print(f"  总开关 SIGNAL_TRADE_ENABLED     : {settings.signal_trade_enabled} "
          f"{'（仅dry-run）' if not settings.signal_trade_enabled else '（LIVE 可下单）'}")
    print(f"  AI 止损止盈 AI_SLTP_ENABLED     : {settings.signal_ai_sltp_enabled}")
    print(f"  单笔名义上限                    : {settings.signal_max_notional_usdt:.0f} USDT")
    print(f"  默认杠杆                        : {settings.signal_leverage}x")
    print(f"  下单方式                        : {settings.signal_order_type} "
          f"（偏离上限 {settings.signal_max_price_deviation_pct}%）")
    print(f"  同币冷却                        : {settings.signal_cooldown_minutes} min")
    print(f"  信号最低置信度                  : ≥ {settings.signal_min_confidence:.0%}")
    print(f"  信号最大时效                    : < {settings.signal_max_age_hours} h")
    print(f"  KOL 白名单                      : {whitelist}")
    print()
    print("  🔒 止损止盈三层结构")
    print(f"    ① 机械底线  (ATR×{settings.signal_atr_multiplier} 或 0.2% 取大)")
    print(f"    ② AI 建议   (SIGNAL_AI_SLTP_ENABLED=1 时启用)")
    print(f"    ③ 硬顶      : 单笔亏损 ≤ 总权益 × {settings.signal_max_loss_pct_of_equity}%")
    print(f"    兜底止损    : {settings.signal_fallback_stop_loss_pct}% (AI 失败时立即挂)")
    print("=" * 64)
    print()

    if not (settings.binance_api_key and settings.binance_api_secret):
        print("⚠️  未配置 BINANCE_API_KEY/SECRET，跳过币安连接测试")
        return 1

    client = BinanceFuturesClient(
        settings.binance_api_key, settings.binance_api_secret,
        base_url=settings.binance_fapi_base_url,
    )

    # ── 2. 连通性 ──
    print("🌐  连通性测试")
    try:
        ok = client.ping()
        print(f"   ✅ API ping          : 正常")
    except Exception as e:
        print(f"   ❌ API ping          : 失败 - {e}")
        code = 1
        return code

    # ── 3. 账户信息 ──
    print()
    print("💰  账户信息 (USDT-M 合约账户)")
    try:
        account = client.get_account()
        assets = account.get("assets", [])
        usdt_asset = next((a for a in assets if a["asset"] == "USDT"), None)
        if usdt_asset:
            wallet_balance = float(usdt_asset.get("walletBalance", 0))
            available_balance = float(usdt_asset.get("availableBalance", 0))
            unrealized_pnl = float(usdt_asset.get("unrealizedProfit", 0))
            margin_balance = float(usdt_asset.get("marginBalance", 0))
            print(f"   钱包余额 (walletBalance)     : {wallet_balance:,.2f} USDT")
            print(f"   可用余额 (availableBalance)  : {available_balance:,.2f} USDT")
            print(f"   保证金余额 (marginBalance)   : {margin_balance:,.2f} USDT")
            print(f"   未实现盈亏                    : {unrealized_pnl:+,.2f} USDT")
        else:
            print("   ⚠️  未找到 USDT 资产记录")

        total_initial_margin = float(account.get("totalInitialMargin", 0))
        print(f"   已用初始保证金                : {total_initial_margin:,.2f} USDT")

        # 单笔 2% 硬顶对应的金额
        equity = float(usdt_asset.get("marginBalance", 0)) if usdt_asset else 0
        max_loss_usdt = equity * settings.signal_max_loss_pct_of_equity / 100
        print()
        print(f"   💡 按当前权益计算单笔风控上限")
        print(f"      权益 × {settings.signal_max_loss_pct_of_equity}% 硬顶 = "
              f"{max_loss_usdt:,.2f} USDT / 笔")
        nominal = settings.signal_max_notional_usdt
        margin_needed = nominal / max(settings.signal_leverage, 1)
        print(f"      单笔名义 {nominal:.0f} USDT @ {settings.signal_leverage}x "
              f"= 保证金 {margin_needed:.2f} USDT")
        if margin_needed > available_balance:
            print(f"      ⚠️  可用余额不足以开单笔（差 {margin_needed - available_balance:.2f} USDT）")
            code = 1
        else:
            print(f"      ✅ 可用余额充足（可开 {available_balance / margin_needed:.1f} 笔）")
    except Exception as e:
        print(f"   ❌ 账户查询失败: {e}")
        code = 1

    # ── 4. 持仓模式（关键！决定下单参数）──
    print()
    print("📊  持仓模式")
    try:
        mode = client.get_position_mode()
        mode_cn = "双向持仓 (dual)" if mode == "dual" else "单向持仓 (oneway)"
        mode_warn = (" ⚠️ 当前代码按双向持仓写的，若实际是单向可能需调整 positionSide 逻辑"
                     if mode == "oneway" else "")
        print(f"   {mode_cn}{mode_warn}")
    except Exception as e:
        print(f"   ❌ 查询失败: {e}")
        code = 1

    # ── 5. 当前持仓 ──
    print()
    print("📈  当前持仓")
    try:
        positions = client.get_position_risk()
        active = [p for p in positions if float(p.get("positionAmt", 0)) != 0]
        if not active:
            print("   （空仓）")
        else:
            for p in active:
                amt = float(p["positionAmt"])
                side = "多" if amt > 0 else "空"
                print(f"   {p['symbol']:>12}  {side} {abs(amt):g}  "
                      f"入场={float(p.get('entryPrice', 0)):g}  "
                      f"未实现盈亏={float(p.get('unRealizedProfit', 0)):+,.2f} USDT  "
                      f"杠杆={p.get('leverage', '?')}x")
    except Exception as e:
        print(f"   ❌ 查询失败: {e}")
        code = 1

    # ── 6. API 权限探测（用结果反推权限，不直接问）──
    print()
    print("🔑  API 权限探测")
    # 读取权限 = 已经通过上面 account 查询验证了
    print("   ✅ 读取权限        : 正常")

    # 合约交易权限 = 能查到 positionRisk 已经说明有合约读权限
    # 真正的下单权限要实际下单才知道，但我们不下，只做推断
    print("   ✅ 合约读取权限    : 正常")
    print("   ℹ️  合约交易权限    : 需实际下单验证（建议先 dry-run 观察）")
    print("   ℹ️  提现权限        : 建议保持关闭（API Key 安全红线）")

    # ── 7. 汇总 ──
    print()
    print("=" * 64)
    if code == 0:
        print("  ✅ 全部自检通过")
        print(f"     → 下一步：python auto_trade_signal.py  (dry-run 扫信号)")
        print(f"     → 确认无误后： SIGNAL_TRADE_ENABLED=1 + --live")
    else:
        print("  ⚠️  自检发现异常，请检查上面 ❌ 标记的项目")
    print("=" * 64)
    print()

    return code


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="KOL 实时喊单 → 币安子账户自动开单（DB 驱动）")
    parser.add_argument("--watch", action="store_true", help="常驻轮询模式")
    parser.add_argument("--interval", type=int, default=60, help="轮询间隔秒数（默认60）")
    parser.add_argument("--live", action="store_true", help="真正下单（需 SIGNAL_TRADE_ENABLED=1）")
    parser.add_argument("--test-connection", action="store_true", help="自检币安连通性")
    args = parser.parse_args()

    settings = get_settings(require_database=False)

    if args.test_connection:
        sys.exit(test_connection(settings))

    live = args.live and settings.signal_trade_enabled
    if args.live and not settings.signal_trade_enabled:
        logger.warning("--live 已指定，但 SIGNAL_TRADE_ENABLED 未设为 1，仍按 dry-run 执行")

    whitelist = "、".join(settings.signal_kol_whitelist) if settings.signal_kol_whitelist else "全部 kol 博主"
    logger.info("模式=%s 总开关=%s 单笔名义=%.0fUSDT 杠杆=%s 下单方式=%s 冷却=%smin "
                "置信度≥%.2f 时效<%sh KOL白名单=%s 止损兜底=%s%% 硬顶=%s%%",
                "LIVE" if live else "DRY-RUN",
                settings.signal_trade_enabled, settings.signal_max_notional_usdt,
                settings.signal_leverage, settings.signal_order_type,
                settings.signal_cooldown_minutes,
                settings.signal_min_confidence, settings.signal_max_age_hours,
                whitelist, settings.signal_fallback_stop_loss_pct,
                settings.signal_max_loss_pct_of_equity)

    if args.watch:
        logger.info("常驻轮询开始，间隔 %ss（Ctrl+C 停止）", args.interval)
        while True:
            try:
                run_once_db(settings, live=live)
            except KeyboardInterrupt:
                logger.info("收到退出信号，停止")
                break
            except Exception as e:
                logger.error("轮询异常（稍后重试）: %s", e)
            time.sleep(args.interval)
    else:
        run_once_db(settings, live=live)


if __name__ == "__main__":
    main()
