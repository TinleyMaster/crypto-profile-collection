"""信号盘面快照装配器：组装喂给 AI 的实时盘面数据。

数据来源（全部 best-effort，任一失败只缺该项，绝不阻塞）：
1. 项目 DB：
   - biz.asset_derivatives（资金费率/OI/OI 24h 变化/CVD 24h）
   - biz.fear_greed_daily（恐贪指数）
2. Binance 公开 klines（信号到达时单次调用）：
   - 现价 / 1h ATR(14)（机械止损底线依据）/ 24h 高低 / 24h 成交量

产物：
- snapshot dict（结构化，供日志/落库）
- to_prompt_text(snapshot) -> 紧凑文本（喂 AI）
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests

from crypto_research.clients.binance_futures import FAPI_BASE

KLINES_INTERVAL = "1h"
KLINES_LIMIT = 25  # 覆盖 24h 高低/量 + ATR(14)
TIMEOUT = 15


def _num(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def fetch_market_snapshot(settings, futures_symbol: str) -> dict[str, Any]:
    """拉取信号标的的盘面快照。返回 {symbol, price, atr_pct, derivatives, fear_greed, ...}。"""
    base_symbol = futures_symbol[:-4] if futures_symbol.upper().endswith("USDT") else futures_symbol
    out: dict[str, Any] = {"symbol": futures_symbol, "price": None, "atr_pct": None}

    # 1. 项目 DB（衍生品 + 恐贪）
    if settings.database_url:
        try:
            from crypto_research.db.conn import get_connection
            with get_connection(settings.database_url) as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT funding_rate_pct, funding_rate_7d_avg, total_oi_usd,
                           oi_change_24h_pct, cvd_24h_usd
                    FROM biz.asset_derivatives
                    WHERE symbol = %s ORDER BY fetched_at DESC LIMIT 1
                """, (base_symbol,))
                row = cur.fetchone()
                if row and row[0] is not None:
                    out["derivatives"] = {
                        "funding_pct": _num(row[0]),
                        "funding_7d_avg_pct": _num(row[1]),
                        "oi_usd": _num(row[2]),
                        "oi_change_24h_pct": _num(row[3]),
                        "cvd_24h_usd": _num(row[4]),
                    }
                cur.execute("""
                    SELECT metric_date, value, value_classification
                    FROM biz.fear_greed_daily ORDER BY metric_date DESC LIMIT 1
                """)
                fg = cur.fetchone()
                if fg:
                    out["fear_greed"] = {"date": str(fg[0]), "value": int(fg[1]), "label": fg[2]}
        except Exception as e:
            out["_db_error"] = str(e)[:120]

    # 2. Binance klines（现价 / ATR / 24h 高低 / 成交量）
    try:
        r = requests.get(
            f"{FAPI_BASE}/fapi/v1/klines",
            params={"symbol": futures_symbol, "interval": KLINES_INTERVAL, "limit": KLINES_LIMIT},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        candles = r.json()
        if candles:
            highs = [_num(c[2]) or 0.0 for c in candles]
            lows = [_num(c[3]) or 0.0 for c in candles]
            closes = [_num(c[4]) or 0.0 for c in candles]
            vols = [_num(c[5]) or 0.0 for c in candles]
            price = closes[-1]
            # ATR(14)：近 14 根 1h K 线的平均真实波幅
            trs = []
            for i in range(1, len(candles)):
                h, l, pc = highs[i], lows[i], closes[i - 1]
                trs.append(max(h - l, abs(h - pc), abs(l - pc)))
            atr = sum(trs[-14:]) / max(1, len(trs[-14:])) if trs else 0.0
            out["price"] = price
            out["atr_pct"] = atr / price * 100 if price else 0.0
            out["high_24h"] = max(highs[-24:])
            out["low_24h"] = min(lows[-24:])
            out["volume_24h"] = sum(vols[-24:])
            out["atr_1h"] = atr
            out["snapshot_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    except Exception as e:
        out["_klines_error"] = str(e)[:120]

    return out


def to_prompt_text(snap: dict[str, Any]) -> str:
    """把快照拼成喂给 AI 的紧凑文本（只含有数据的项）。"""
    lines = []
    if snap.get("price"):
        lines.append(f"当前价：{snap['price']}")
    if snap.get("atr_pct") is not None:
        lines.append(f"1小时ATR(14)：{snap['atr_pct']:.3f}%（近14小时平均真实波幅，止损不宜明显小于其2倍）")
    if snap.get("high_24h") and snap.get("low_24h"):
        lines.append(f"24h高/低：{snap['high_24h']} / {snap['low_24h']}")
    if snap.get("volume_24h"):
        lines.append(f"24h成交量：{snap['volume_24h']:.0f}（合约口径）")
    d = snap.get("derivatives")
    if d:
        sym = snap.get("symbol", "该币")
        lines.append(f"{sym} 情绪代理（合约资金面）：")
        if d.get("funding_pct") is not None:
            avg7 = d.get("funding_7d_avg_pct")
            avg7_text = f"{avg7:.4f}" if avg7 is not None else "—"
            lines.append(f"- 资金费率：{d['funding_pct']:.4f}%（7日均 {avg7_text}%）")
        if d.get("oi_usd") is not None:
            lines.append(f"- 未平仓合约OI：{d['oi_usd'] / 1e8:.1f}亿 USDT（24h {d.get('oi_change_24h_pct') or 0}%）")
        if d.get("cvd_24h_usd") is not None:
            lines.append(f"- CVD 24h：{d['cvd_24h_usd'] / 1e8:.1f}亿（正=主动买盘占优，负=主动卖盘占优）")
    fg = snap.get("fear_greed")
    if fg:
        lines.append(f"全市场恐贪（参考）：{fg['value']}（{fg['label']}，{fg['date']}）")
    if not lines:
        return "（无盘面数据）"
    return "\n".join(lines)
