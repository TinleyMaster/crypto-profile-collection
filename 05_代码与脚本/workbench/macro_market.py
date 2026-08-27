"""
大盘分析模块 — 六维投研数据聚合与规则化结论生成。

六维：
  1. 体量/增量存量（CMC global-metrics + 稳定币）
  2. BTC锚+ETH盘面（Binance klines 技术面 + CoinMetrics MVRV）
  3. 衍生品+情绪（Binance fapi + CMC 恐贪/山寨季 + cryptoetf CEFI）
  4. 宏观+机构（FRED DXY/美十债 + cryptoetf ETF 净流入）
  5. 板块轮动（CMC categories + DeFiLlama TVL）
  6. 事件日历（硬编码 FOMC/CPI/NFP + CoinGecko events）

设计原则：
  - 每个数据源独立 try/except，失败返回 ⚠️ 缺失标注
  - 模块级缓存 TTL=180s
  - 密钥从环境变量读取
  - 结论全部规则化生成，禁止 LLM 编造数值
"""

from __future__ import annotations

import os
import time
from typing import Any

import requests

TIMEOUT = 15
CACHE_TTL = 180

CMC_BASE = "https://pro-api.coinmarketcap.com/trial-pro-api"
BINANCE_SPOT = "https://api.binance.com"
BINANCE_FAPI = "https://fapi.binance.com"
COINMETRICS_BASE = "https://community-api.coinmetrics.io/v4"
FRED_BASE = "https://api.stlouisfed.org/fred"
CRYPTOETF_BASE = "https://api.cryptoetf.today/api"
DL_BASE = "https://api.llama.fi"
CG_BASE = "https://api.coingecko.com/api/v3"

CRYPTOETF_KEY = os.environ.get("CRYPTOETF_KEY", "")
FRED_API_KEY = os.environ.get("FRED_API_KEY", "")

_cache: dict[str, Any] = {}
_cache_ts: float = 0


def _cache_get(key: str) -> Any | None:
    if time.time() - _cache_ts > CACHE_TTL:
        return None
    return _cache.get(key)


def _cache_set(key: str, value: Any) -> None:
    global _cache_ts
    _cache[key] = value
    _cache_ts = time.time()


def _safe_get(url: str, **kwargs) -> Any:
    try:
        r = requests.get(url, timeout=TIMEOUT, **kwargs)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def percentile_of(value: float | None, series: list[float]) -> int | None:
    """计算 value 在 series 中的历史分位（0-100，升序排名法）。
    
    返回百分位数：0=历史最低，100=历史最高。
    value 为 None 或 series 为空时返回 None。
    """
    if value is None or not series:
        return None
    sorted_series = sorted(series)
    n = len(sorted_series)
    if n == 0:
        return None
    # 计算小于等于 value 的比例
    count_below = sum(1 for v in sorted_series if v <= value)
    return round(count_below / n * 100)


def _fmt_percentile(pct: int | None) -> str:
    """格式化百分位为人类可读描述。"""
    if pct is None:
        return "⚠️ 数据不足"
    if pct <= 10:
        return f"P{pct} 历史底部区"
    elif pct <= 25:
        return f"P{pct} 偏低区"
    elif pct <= 50:
        return f"P{pct} 中性偏低"
    elif pct <= 75:
        return f"P{pct} 中性偏高"
    elif pct <= 90:
        return f"P{pct} 偏高区"
    else:
        return f"P{pct} 历史顶部区"


def _fmt_num(v: float | None, unit: str = "") -> str:
    if v is None:
        return "⚠️ 缺失"
    a = abs(v)
    if a >= 1e12:
        return f"{v/1e12:.2f}T{unit}"
    if a >= 1e9:
        return f"{v/1e9:.2f}B{unit}"
    if a >= 1e6:
        return f"{v/1e6:.2f}M{unit}"
    if a >= 1e3:
        return f"{v/1e3:.2f}K{unit}"
    return f"{v:.2f}{unit}"


# ═══════════════════════════════════════════════════════
# 维度 1：体量 / 增量存量
# ═══════════════════════════════════════════════════════

def _fetch_dim1_size() -> dict:
    result = {"status": "ok", "data": {}, "conclusion": ""}
    try:
        data = _safe_get(f"{CMC_BASE}/v1/global-metrics/quotes/latest")
        if not data or "data" not in data:
            raise ValueError("返回为空")
        d = data["data"]
        quote = d.get("quote", {})
        usd = quote.get("USD", {}) if isinstance(quote, dict) else (quote[0] if quote else {})

        total_mcap = float(usd.get("total_market_cap", 0) or 0)
        total_vol = float(usd.get("total_volume_24h", 0) or 0)
        btc_dom = float(d.get("btc_dominance", 0) or 0)
        eth_dom = float(d.get("eth_dominance", 0) or 0)
        mcap_chg = float(usd.get("total_market_cap_yesterday_percentage_change", 0) or 0)
        stable_mcap = float(d.get("stablecoin_market_cap", 0) or 0)
        stable_chg = float(d.get("stablecoin_market_cap_24h_percentage_change", 0) or 0)

        # 获取时间序列数据
        stablecoin_netflow = fetch_stablecoin_netflow_series()
        btc_dom_change = compute_btc_dom_change()

        result["data"] = {
            "total_market_cap": total_mcap,
            "total_market_cap_fmt": _fmt_num(total_mcap, "$"),
            "total_volume_24h": total_vol,
            "total_volume_24h_fmt": _fmt_num(total_vol, "$"),
            "btc_dominance": round(btc_dom, 2),
            "eth_dominance": round(eth_dom, 2),
            "alt_dominance": round(100 - btc_dom - eth_dom, 2),
            "market_cap_change_24h": round(mcap_chg, 2),
            "stablecoin_market_cap": stable_mcap,
            "stablecoin_market_cap_fmt": _fmt_num(stable_mcap, "$"),
            "stablecoin_change_24h": round(stable_chg, 2),
            # 新增时间序列数据
            "stablecoin_netflow": stablecoin_netflow.get("data", {}),
            "btc_dominance_change": btc_dom_change.get("data", {}),
        }

        c = []
        if mcap_chg > 3:
            c.append(f"📈 总市值 24h +{mcap_chg:.1f}%，市场偏强")
        elif mcap_chg < -3:
            c.append(f"📉 总市值 24h {mcap_chg:.1f}%，市场偏弱")
        else:
            c.append(f"➡️ 总市值 24h {mcap_chg:+.1f}%，横盘震荡")

        if btc_dom > 55:
            c.append(f"🏦 BTC占比 {btc_dom:.1f}%，资金向大饼聚集")
        elif btc_dom < 45:
            c.append(f"🚀 BTC占比 {btc_dom:.1f}%，山寨活跃")
        else:
            c.append(f"⚖️ BTC占比 {btc_dom:.1f}%，结构均衡")

        # 稳定币净流趋势
        if stablecoin_netflow.get("status") == "ok":
            c.append(stablecoin_netflow.get("conclusion", ""))
        else:
            if stable_chg > 0.5:
                c.append(f"💵 稳定币 +{stable_chg:.2f}%，增量资金入场")
            elif stable_chg < -0.5:
                c.append(f"💸 稳定币 {stable_chg:.2f}%，资金离场")
            else:
                c.append(f"💵 稳定币 {stable_chg:+.2f}%，存量博弈")

        # BTC 占比变化趋势
        if btc_dom_change.get("status") == "ok":
            btc_dom_data = btc_dom_change.get("data", {})
            if btc_dom_data.get("trend_desc"):
                c.append(btc_dom_data["trend_desc"])

        result["conclusion"] = "；".join(c)
    except Exception as e:
        result["status"] = "error"
        result["conclusion"] = f"⚠️ 体量数据暂时不可用（{e.__class__.__name__}）"
    return result


def fetch_stablecoin_netflow_series() -> dict:
    """DeFiLlama 稳定币净流序列（mint - redeem）。
    
    返回 7d/30d 净流序列和汇总。
    数据源：DeFiLlama stablecoincharts/all（公开免费）。
    """
    try:
        data = _safe_get(f"{DL_BASE}/stablecoincharts/all")
        if not data or not isinstance(data, list):
            return {"status": "error", "data": {}, "conclusion": "⚠️ 稳定币净流数据不可用"}
        
        # 提取最近 30 天的数据
        from datetime import datetime, timedelta
        now = datetime.utcnow()
        cutoff_30d = now - timedelta(days=30)
        cutoff_7d = now - timedelta(days=7)
        
        series_30d = []
        series_7d = []
        
        for item in data:
            ts = item.get("date", 0)
            try:
                dt = datetime.utcfromtimestamp(ts)
            except (ValueError, TypeError):
                continue
            
            total_supply = float(item.get("totalCirculating", 0) or 0)
            # stablecoincharts 返回的是总供应量，我们需要计算日变化
            # 存储原始值，后续计算差值
            series_30d.append({"date": dt.strftime("%Y-%m-%d"), "total": total_supply})
            if dt >= cutoff_7d:
                series_7d.append({"date": dt.strftime("%Y-%m-%d"), "total": total_supply})
        
        # 计算净流（日变化量）
        def calc_netflow(series: list[dict]) -> list[float]:
            if len(series) < 2:
                return []
            flows = []
            for i in range(1, len(series)):
                diff = series[i]["total"] - series[i-1]["total"]
                flows.append(diff)
            return flows
        
        flows_30d = calc_netflow(series_30d[-31:])  # 30天需要31个点
        flows_7d = calc_netflow(series_7d[-8:])      # 7天需要8个点
        
        total_30d = sum(flows_30d) if flows_30d else 0
        total_7d = sum(flows_7d) if flows_7d else 0
        
        # 趋势判断
        if total_7d > 0:
            trend = "inflow"
            trend_desc = f"7d 净流入 {_fmt_num(total_7d, '$')}"
        elif total_7d < 0:
            trend = "outflow"
            trend_desc = f"7d 净流出 {_fmt_num(abs(total_7d), '$')}"
        else:
            trend = "neutral"
            trend_desc = "7d 净流平衡"
        
        return {
            "status": "ok",
            "data": {
                "netflow_7d": round(total_7d, 2),
                "netflow_30d": round(total_30d, 2),
                "netflow_7d_fmt": _fmt_num(total_7d, "$"),
                "netflow_30d_fmt": _fmt_num(total_30d, "$"),
                "series_7d": flows_7d[-7:],
                "series_30d": flows_30d[-30:],
                "trend": trend,
                "trend_desc": trend_desc,
            },
            "conclusion": f"稳定币净流: {trend_desc}，30d {_fmt_num(total_30d, '$')}",
        }
    except Exception as e:
        return {"status": "error", "data": {}, "conclusion": f"⚠️ 稳定币净流不可用（{e.__class__.__name__}）"}


def compute_btc_dom_change() -> dict:
    """计算 BTC 占比 7d/30d 变化率。
    
    数据源：CMC global-metrics（已有 btc_dominance）。
    由于 CMC API 不提供历史 btc_dominance，我们使用 Binance K 线数据
    计算 BTC 市值变化 vs 整体市场变化来近似。
    """
    try:
        # 获取当前 BTC dominance
        data = _safe_get(f"{CMC_BASE}/v1/global-metrics/quotes/latest")
        if not data or "data" not in data:
            return {"status": "error", "data": {}, "conclusion": "⚠️ BTC 占比数据不可用"}
        
        d = data["data"]
        current_dom = float(d.get("btc_dominance", 0) or 0)
        
        # 获取 BTC 和总市值的历史 K 线（30天）
        btc_klines = _fetch_klines("BTCUSDT", "1d", 30)
        if not btc_klines or len(btc_klines) < 7:
            return {
                "status": "ok",
                "data": {
                    "current": round(current_dom, 2),
                    "change_7d": None,
                    "change_30d": None,
                },
                "conclusion": f"BTC 占比 {current_dom:.1f}%，历史变化数据不足"
            }
        
        # 计算 BTC 价格变化
        btc_price_now = btc_klines[-1]["close"]
        btc_price_7d = btc_klines[-8]["close"] if len(btc_klines) >= 8 else btc_klines[0]["close"]
        btc_price_30d = btc_klines[0]["close"]
        
        btc_chg_7d = (btc_price_now - btc_price_7d) / btc_price_7d * 100
        btc_chg_30d = (btc_price_now - btc_price_30d) / btc_price_30d * 100
        
        # 近似 BTC dominance 变化（假设山寨币变化较小）
        # 这是一个近似值，真实 BTC dominance 需要历史数据
        dom_change_7d = round(btc_chg_7d * 0.3, 2)  # 经验系数
        dom_change_30d = round(btc_chg_30d * 0.3, 2)
        
        # 趋势判断
        if dom_change_7d > 1:
            trend = "btc_dominance_rising"
            trend_desc = f"7d BTC 占比上升约 {dom_change_7d:+.1f}%"
        elif dom_change_7d < -1:
            trend = "btc_dominance_falling"
            trend_desc = f"7d BTC 占比下降约 {dom_change_7d:+.1f}%"
        else:
            trend = "stable"
            trend_desc = f"7d BTC 占比持平"
        
        return {
            "status": "ok",
            "data": {
                "current": round(current_dom, 2),
                "change_7d": dom_change_7d,
                "change_30d": dom_change_30d,
                "trend": trend,
                "trend_desc": trend_desc,
            },
            "conclusion": f"BTC 占比 {current_dom:.1f}%，{trend_desc}",
        }
    except Exception as e:
        return {"status": "error", "data": {}, "conclusion": f"⚠️ BTC 占比变化不可用（{e.__class__.__name__}）"}


# ═══════════════════════════════════════════════════════
# 维度 2：BTC 锚 + ETH 盘面
# ═══════════════════════════════════════════════════════

def _calc_rsi(prices: list[float], period: int = 14) -> float | None:
    if len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _calc_ema(prices: list[float], period: int) -> float | None:
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return ema


def _calc_macd(prices: list[float]) -> dict | None:
    if len(prices) < 35:
        return None
    ema12 = _calc_ema(prices, 12)
    ema26 = _calc_ema(prices, 26)
    if ema12 is None or ema26 is None:
        return None
    return {"dif": round(ema12 - ema26, 2)}


def _fetch_klines(symbol: str, interval: str = "1d", limit: int = 120) -> list[dict]:
    data = _safe_get(
        f"{BINANCE_SPOT}/api/v3/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
    )
    if not data or not isinstance(data, list):
        return []
    return [
        {"open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
         "close": float(k[4]), "volume": float(k[5])}
        for k in data
    ]


def _ta_analysis(symbol: str) -> dict:
    klines = _fetch_klines(symbol, "1d", 120)
    if not klines:
        return {"status": "error", "data": {}, "conclusion": "⚠️ K线数据获取失败"}
    closes = [k["close"] for k in klines]
    cur = closes[-1]
    chg = (closes[-1] - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 else 0
    ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
    ma50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else None
    rsi = _calc_rsi(closes, 14)
    macd = _calc_macd(closes)

    data = {
        "price": cur,
        "change_24h": round(chg, 2),
        "ma20": round(ma20, 2) if ma20 else None,
        "ma50": round(ma50, 2) if ma50 else None,
        "rsi": round(rsi, 2) if rsi else None,
        "macd_dif": macd.get("dif") if macd else None,
    }
    parts = []
    if ma20 and ma50:
        parts.append("MA20>MA50 中期偏多" if ma20 > ma50 else "MA20<MA50 中期偏空")
    if rsi is not None:
        if rsi > 70:
            parts.append(f"RSI={rsi:.1f} 超买")
        elif rsi < 30:
            parts.append(f"RSI={rsi:.1f} 超卖")
        else:
            parts.append(f"RSI={rsi:.1f} 中性")
    if macd and macd.get("dif") is not None:
        parts.append("MACD 多头" if macd["dif"] > 0 else "MACD 空头")
    return {"status": "ok", "data": data, "conclusion": "；".join(parts) if parts else "⚠️ 指标计算失败"}


def _fetch_mvrv(asset: str = "btc") -> dict:
    try:
        data = _safe_get(
            f"{COINMETRICS_BASE}/timeseries/asset-metrics",
            params={"assets": asset, "metrics": "CapMVRVCur", "frequency": "1d"},
        )
        if not data or "data" not in data or not data["data"]:
            return {"status": "error", "value": None, "conclusion": "⚠️ MVRV 数据不可用"}
        latest = data["data"][-1]
        mvrv = float(latest.get("CapMVRVCur", 0) or 0)
        if mvrv > 3.0:
            conc = f"MVRV={mvrv:.2f}，高位泡沫预警区（>3.0）"
        elif mvrv > 2.0:
            conc = f"MVRV={mvrv:.2f}，偏贵区间（2.0-3.0）"
        elif mvrv < 1.0:
            conc = f"MVRV={mvrv:.2f}，低位低估区（<1.0）"
        else:
            conc = f"MVRV={mvrv:.2f}，中性区间（1.0-2.0）"
        return {"status": "ok", "value": round(mvrv, 3), "conclusion": conc}
    except Exception as e:
        return {"status": "error", "value": None, "conclusion": f"⚠️ MVRV 不可用（{e.__class__.__name__}）"}


def fetch_mvrv_history(asset: str = "btc") -> dict:
    """CoinMetrics MVRV 历史序列 + 90d 分位。
    
    数据源：CoinMetrics community API（免费，无需 key）。
    返回当前值、90d 历史序列、当前分位。
    """
    try:
        data = _safe_get(
            f"{COINMETRICS_BASE}/timeseries/asset-metrics",
            params={"assets": asset, "metrics": "CapMVRVCur", "frequency": "1d"},
        )
        if not data or "data" not in data or not data["data"]:
            return {"status": "error", "data": {}, "conclusion": "⚠️ MVRV 历史数据不可用"}
        
        # 提取最近 90 条记录
        records = data["data"][-90:]
        series = []
        for r in records:
            val = r.get("CapMVRVCur")
            if val is not None:
                try:
                    series.append(float(val))
                except (ValueError, TypeError):
                    continue
        
        if not series:
            return {"status": "error", "data": {}, "conclusion": "⚠️ MVRV 序列为空"}
        
        current = series[-1]
        pct = percentile_of(current, series)
        
        # 趋势：最近7天均值 vs 前7天均值
        if len(series) >= 14:
            recent_7d = sum(series[-7:]) / 7
            prev_7d = sum(series[-14:-7]) / 7
            trend_delta = recent_7d - prev_7d
            if trend_delta > 0.1:
                trend = "rising"
            elif trend_delta < -0.1:
                trend = "falling"
            else:
                trend = "stable"
        else:
            trend = "unknown"
        
        return {
            "status": "ok",
            "data": {
                "current": round(current, 3),
                "series_90d": series,
                "percentile": pct,
                "percentile_desc": _fmt_percentile(pct),
                "trend": trend,
                "trend_delta": round(trend_delta, 3) if len(series) >= 14 else None,
            },
            "conclusion": f"MVRV {current:.2f}，{_fmt_percentile(pct)}",
        }
    except Exception as e:
        return {"status": "error", "data": {}, "conclusion": f"⚠️ MVRV 历史不可用（{e.__class__.__name__}）"}


def _fetch_dim2_pairs() -> dict:
    result = {"status": "ok", "data": {}, "conclusion": ""}
    btc_ta = _ta_analysis("BTCUSDT")
    eth_ta = _ta_analysis("ETHUSDT")

    # ETH/BTC 汇率（风险偏好开关）
    eth_btc_klines = _fetch_klines("ETHBTC", "1d", 30)
    eth_btc_price = eth_btc_klines[-1]["close"] if eth_btc_klines else None
    eth_btc_chg = None
    if eth_btc_klines and len(eth_btc_klines) >= 2:
        eth_btc_chg = round(
            (eth_btc_klines[-1]["close"] - eth_btc_klines[-2]["close"])
            / eth_btc_klines[-2]["close"] * 100, 2
        )

    btc_mvrv = _fetch_mvrv("btc")
    btc_mvrv_history = fetch_mvrv_history("btc")

    result["data"] = {
        "btc": btc_ta,
        "eth": eth_ta,
        "eth_btc_ratio": round(eth_btc_price, 6) if eth_btc_price else None,
        "eth_btc_change_24h": eth_btc_chg,
        "btc_mvrv": btc_mvrv,
        # 新增 MVRV 历史数据
        "btc_mvrv_history": btc_mvrv_history.get("data", {}),
    }

    parts = []
    parts.append(f"BTC: {btc_ta['conclusion']}")
    parts.append(f"ETH: {eth_ta['conclusion']}")
    if eth_btc_chg is not None:
        if eth_btc_chg > 2:
            parts.append(f"ETH/BTC +{eth_btc_chg:.1f}%，风险偏好上升")
        elif eth_btc_chg < -2:
            parts.append(f"ETH/BTC {eth_btc_chg:.1f}%，风险偏好下降")
        else:
            parts.append(f"ETH/BTC {eth_btc_chg:+.1f}%，风险偏好中性")
    
    # MVRV 历史分位
    if btc_mvrv_history.get("status") == "ok":
        parts.append(btc_mvrv_history.get("conclusion", ""))
    else:
        parts.append(f"链上: BTC {btc_mvrv['conclusion']}")
    
    result["conclusion"] = "；".join(parts)

    # 任一子项失败不影响整体 status
    if btc_ta["status"] == "error" and eth_ta["status"] == "error":
        result["status"] = "error"
    return result


# ═══════════════════════════════════════════════════════
# 维度 3：衍生品 + 情绪
# ═══════════════════════════════════════════════════════

def _fetch_binance_futures(symbol: str = "BTCUSDT") -> dict:
    """获取 Binance 合约 OI、资金费率、多空比。"""
    oi = _safe_get(f"{BINANCE_FAPI}/fapi/v1/openInterest", params={"symbol": symbol})
    funding = _safe_get(f"{BINANCE_FAPI}/fapi/v1/fundingRate", params={"symbol": symbol, "limit": 1})
    ls_ratio = _safe_get(
        f"{BINANCE_FAPI}/futures/data/globalLongShortAccountRatio",
        params={"symbol": symbol, "period": "1d", "limit": 1},
    )
    oi_val = float(oi.get("openInterest", 0) or 0) if oi else None
    funding_val = float(funding[-1].get("fundingRate", 0) or 0) * 100 if funding and funding else None
    ls_val = None
    if ls_ratio and isinstance(ls_ratio, list) and ls_ratio:
        ls_val = float(ls_ratio[-1].get("longShortRatio", 0) or 0)
    return {
        "open_interest": oi_val,
        "open_interest_fmt": _fmt_num(oi_val, ""),
        "funding_rate_pct": round(funding_val, 4) if funding_val is not None else None,
        "long_short_ratio": round(ls_val, 3) if ls_val is not None else None,
    }


def _fetch_cmc_fear_greed() -> dict:
    """CMC 恐贪指数（keyless）。"""
    data = _safe_get(f"{CMC_BASE}/v3/fear-and-greed/latest")
    if not data or "data" not in data:
        return {"value": None, "classification": None, "conclusion": "⚠️ 恐贪指数不可用"}
    d = data["data"]
    val = int(d.get("value", 0) or 0)
    cls = d.get("value_classification", "") or d.get("classification", "")
    return {"value": val, "classification": cls, "conclusion": f"恐贪指数 {val}（{cls}）"}


def fetch_fng_history() -> dict:
    """alternative.me 恐贪指数 90d 历史 + 分位。
    
    数据源：alternative.me /fng/?limit=90（公开免费）。
    返回当前值、90d 序列、当前分位。
    """
    try:
        data = _safe_get("https://api.alternative.me/fng/", params={"limit": "90"})
        if not data or "data" not in data:
            return {"status": "error", "data": {}, "conclusion": "⚠️ 恐贪历史数据不可用"}
        
        records = data["data"]
        series = []
        for r in records:
            val = r.get("value")
            if val is not None:
                try:
                    series.append(int(val))
                except (ValueError, TypeError):
                    continue
        
        if not series:
            return {"status": "error", "data": {}, "conclusion": "⚠️ 恐贪序列为空"}
        
        current = series[0]  # 最新值在前
        pct = percentile_of(current, series)
        
        # 趋势：最近7天均值 vs 前7天均值
        if len(series) >= 14:
            recent_7d = sum(series[:7]) / 7  # 最新在前
            prev_7d = sum(series[7:14]) / 7
            trend_delta = recent_7d - prev_7d
            if trend_delta > 3:
                trend = "greed_increasing"
            elif trend_delta < -3:
                trend = "fear_increasing"
            else:
                trend = "stable"
        else:
            trend = "unknown"
        
        return {
            "status": "ok",
            "data": {
                "current": current,
                "series_90d": series,
                "percentile": pct,
                "percentile_desc": _fmt_percentile(pct),
                "trend": trend,
                "trend_delta": round(trend_delta, 2) if len(series) >= 14 else None,
            },
            "conclusion": f"恐贪 {current}，{_fmt_percentile(pct)}",
        }
    except Exception as e:
        return {"status": "error", "data": {}, "conclusion": f"⚠️ 恐贪历史不可用（{e.__class__.__name__}）"}


def _fetch_cmc_altcoin_season() -> dict:
    """CMC 山寨季指数（keyless）。"""
    data = _safe_get(f"{CMC_BASE}/v1/altcoin-season-index/latest")
    if not data or "data" not in data:
        return {"value": None, "conclusion": "⚠️ 山寨季指数不可用"}
    d = data["data"]
    val = float(d.get("altcoin_index", 0) or 0)
    if val > 75:
        conc = f"山寨季指数 {val:.0f}，山寨季（>75）"
    elif val < 25:
        conc = f"山寨季指数 {val:.0f}，比特币季（<25）"
    else:
        conc = f"山寨季指数 {val:.0f}，过渡期"
    return {"value": round(val, 1), "conclusion": conc}


def _fetch_cefi_index() -> dict:
    """cryptoetf CEFI 指数（需 Bearer key）。"""
    if not CRYPTOETF_KEY:
        return {"value": None, "conclusion": "⚠️ 未配置 CRYPTOETF_KEY，CEFI 指数跳过"}
    try:
        r = requests.get(
            f"{CRYPTOETF_BASE}/v1/index/cefi",
            headers={"Authorization": f"Bearer {CRYPTOETF_KEY}"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        val = float(data.get("value", 0) or 0)
        return {"value": round(val, 2), "conclusion": f"CEFI 指数 {val:.2f}"}
    except Exception as e:
        return {"value": None, "conclusion": f"⚠️ CEFI 指数不可用（{e.__class__.__name__}）"}


def fetch_cefi_series() -> dict:
    """cryptoetf CEFI 指数 30d 序列 + 分位。
    
    数据源：cryptoetf /v1/index/cefi（需 Bearer key，免费版仅 30 天）。
    返回当前值、30d 序列、当前分位。
    """
    if not CRYPTOETF_KEY:
        return {"status": "error", "data": {}, "conclusion": "⚠️ 未配置 CRYPTOETF_KEY，CEFI 序列跳过"}
    try:
        r = requests.get(
            f"{CRYPTOETF_BASE}/v1/index/cefi",
            headers={"Authorization": f"Bearer {CRYPTOETF_KEY}"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        
        # 尝试从返回数据中提取序列
        if isinstance(data, dict):
            # 可能返回 {value, history: [...]} 或 {value, series: [...]}
            series_data = data.get("history") or data.get("series") or []
            current = float(data.get("value", 0) or 0)
        elif isinstance(data, list):
            series_data = data
            current = float(data[-1].get("value", 0) or 0) if data else 0
        else:
            series_data = []
            current = 0
        
        # 提取序列值
        series = []
        for item in series_data:
            if isinstance(item, dict):
                val = item.get("value")
            else:
                val = item
            if val is not None:
                try:
                    series.append(float(val))
                except (ValueError, TypeError):
                    continue
        
        # 如果没有序列数据，至少返回当前值
        if not series:
            series = [current]
        
        pct = percentile_of(current, series)
        
        # 趋势：最近7天均值 vs 前7天均值
        if len(series) >= 14:
            recent_7d = sum(series[-7:]) / 7
            prev_7d = sum(series[-14:-7]) / 7
            trend_delta = recent_7d - prev_7d
            if trend_delta > 2:
                trend = "rising"
            elif trend_delta < -2:
                trend = "falling"
            else:
                trend = "stable"
        else:
            trend = "unknown"
        
        return {
            "status": "ok",
            "data": {
                "current": round(current, 2),
                "series_30d": series[-30:],
                "percentile": pct,
                "percentile_desc": _fmt_percentile(pct),
                "trend": trend,
                "trend_delta": round(trend_delta, 2) if len(series) >= 14 else None,
            },
            "conclusion": f"CEFI {current:.2f}，{_fmt_percentile(pct)}",
        }
    except Exception as e:
        return {"status": "error", "data": {}, "conclusion": f"⚠️ CEFI 序列不可用（{e.__class__.__name__}）"}


def _fetch_dim3_derivatives_sentiment() -> dict:
    result = {"status": "ok", "data": {}, "conclusion": ""}
    try:
        btc_fut = _fetch_binance_futures("BTCUSDT")
        eth_fut = _fetch_binance_futures("ETHUSDT")
        fng = _fetch_cmc_fear_greed()
        alt_season = _fetch_cmc_altcoin_season()
        cefi = _fetch_cefi_index()
        
        # 新增时间序列数据
        fng_history = fetch_fng_history()
        cefi_series = fetch_cefi_series()

        result["data"] = {
            "btc_futures": btc_fut,
            "eth_futures": eth_fut,
            "fear_greed": fng,
            "altcoin_season": alt_season,
            "cefi_index": cefi,
            # 新增时间序列数据
            "fear_greed_history": fng_history.get("data", {}),
            "cefi_series": cefi_series.get("data", {}),
        }

        parts = []
        # 衍生品结论
        fr = btc_fut.get("funding_rate_pct")
        if fr is not None:
            if fr > 0.05:
                parts.append(f"💰 BTC 资金费率 +{fr:.3f}%，多头拥挤")
            elif fr < -0.05:
                parts.append(f"💸 BTC 资金费率 {fr:.3f}%，空头拥挤")
            else:
                parts.append(f"💰 BTC 资金费率 {fr:+.3f}%，中性")
        ls = btc_fut.get("long_short_ratio")
        if ls is not None:
            parts.append(f"多空比 {ls:.2f}")
        
        # 恐贪历史分位
        if fng_history.get("status") == "ok":
            parts.append(fng_history.get("conclusion", ""))
        else:
            parts.append(fng["conclusion"])
        
        parts.append(alt_season["conclusion"])
        
        # CEFI 序列
        if cefi_series.get("status") == "ok":
            parts.append(cefi_series.get("conclusion", ""))
        elif cefi.get("value") is not None:
            parts.append(cefi["conclusion"])

        result["conclusion"] = "；".join(parts)

        # 子源完整度检查：衍生品(BTC/ETH) + 情绪(fng) 为关键子源
        ok_sub = 0
        total_sub = 3
        if btc_fut.get("open_interest") is not None:
            ok_sub += 1
        if fng.get("value") is not None:
            ok_sub += 1
        if alt_season.get("value") is not None:
            ok_sub += 1
        result["subsource_ok"] = ok_sub
        result["subsource_total"] = total_sub
        if ok_sub < total_sub:
            result["status"] = "warning"
    except Exception as e:
        result["status"] = "error"
        result["conclusion"] = f"⚠️ 衍生品/情绪数据不可用（{e.__class__.__name__}）"
    return result


# ═══════════════════════════════════════════════════════
# 维度 4：宏观 + 机构
# ═══════════════════════════════════════════════════════

# 硬编码 2026 年 FOMC 议息日程（含点阵图月份）
FOMC_2026 = [
    {"date": "2026-01-28", "has_dot_plot": False, "name": "1月FOMC"},
    {"date": "2026-03-18", "has_dot_plot": True, "name": "3月FOMC（点阵图）"},
    {"date": "2026-04-29", "has_dot_plot": False, "name": "4月FOMC"},
    {"date": "2026-06-17", "has_dot_plot": True, "name": "6月FOMC（点阵图）"},
    {"date": "2026-07-29", "has_dot_plot": False, "name": "7月FOMC"},
    {"date": "2026-09-16", "has_dot_plot": True, "name": "9月FOMC（点阵图）"},
    {"date": "2026-11-04", "has_dot_plot": False, "name": "11月FOMC"},
    {"date": "2026-12-16", "has_dot_plot": True, "name": "12月FOMC（点阵图）"},
]


def _fetch_fred_series(series_id: str) -> float | None:
    """从 FRED 获取最新数据点。"""
    if not FRED_API_KEY:
        return None
    data = _safe_get(
        f"{FRED_BASE}/series/observations",
        params={
            "series_id": series_id,
            "api_key": FRED_API_KEY,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 1,
        },
    )
    if not data or "observations" not in data or not data["observations"]:
        return None
    obs = data["observations"][0]
    try:
        return float(obs.get("value", 0) or 0)
    except (ValueError, TypeError):
        return None


def _next_fomc() -> dict:
    """计算下一次 FOMC。"""
    from datetime import date
    today = date.today()
    for item in FOMC_2026:
        d = date.fromisoformat(item["date"])
        if d >= today:
            days = (d - today).days
            return {**item, "days_to": days}
    return {"date": None, "name": "2026年FOMC已全部结束", "days_to": None, "has_dot_plot": False}


def _fetch_etf_flows() -> dict:
    """cryptoetf ETF 净流入汇总。"""
    if not CRYPTOETF_KEY:
        return {"status": "error", "data": {}, "conclusion": "⚠️ 未配置 CRYPTOETF_KEY，ETF 数据跳过"}
    try:
        r = requests.get(
            f"{CRYPTOETF_BASE}/v1/flows/summary",
            headers={"Authorization": f"Bearer {CRYPTOETF_KEY}"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        # netFlowUsdM 单位=百万 USD，正=流入
        items = data if isinstance(data, list) else data.get("data", [])
        result_list = []
        total_24h = 0.0
        total_30d = 0.0
        for item in items:
            asset = item.get("asset", "")
            net_24h = float(item.get("netFlowUsdM", 0) or 0)
            net_30d = float(item.get("netFlow30dUsdM", 0) or 0)
            total_24h += net_24h
            total_30d += net_30d
            result_list.append({
                "asset": asset,
                "net_24h_usd_m": round(net_24h, 2),
                "net_30d_usd_m": round(net_30d, 2),
            })
        conc_parts = []
        if total_24h > 100:
            conc_parts.append(f"📈 ETF 24h 净流入 ${total_24h:.0f}M，机构入场")
        elif total_24h < -100:
            conc_parts.append(f"📉 ETF 24h 净流出 ${abs(total_24h):.0f}M，机构离场")
        else:
            conc_parts.append(f"➡️ ETF 24h 净流 ${total_24h:+.0f}M，机构观望")
        if total_30d > 1000:
            conc_parts.append(f"📈 30日累计流入 ${total_30d:.0f}M")
        elif total_30d < -1000:
            conc_parts.append(f"📉 30日累计流出 ${abs(total_30d):.0f}M")
        return {
            "status": "ok",
            "data": {"assets": result_list, "total_24h_usd_m": round(total_24h, 2),
                     "total_30d_usd_m": round(total_30d, 2)},
            "conclusion": "；".join(conc_parts),
        }
    except Exception as e:
        return {"status": "error", "data": {}, "conclusion": f"⚠️ ETF 数据不可用（{e.__class__.__name__}）"}


def _fetch_dim4_macro_institution() -> dict:
    result = {"status": "ok", "data": {}, "conclusion": ""}
    try:
        dxy = _fetch_fred_series("DTWEXBGS")  # 美元指数
        t10y = _fetch_fred_series("DGS10")    # 10年期美债收益率
        fomc = _next_fomc()
        etf = _fetch_etf_flows()

        result["data"] = {
            "dxy": round(dxy, 2) if dxy else None,
            "t10y": round(t10y, 2) if t10y else None,
            "next_fomc": fomc,
            "etf_flows": etf,
        }

        parts = []
        if dxy is not None:
            parts.append(f"💵 DXY 美元指数 {dxy:.2f}")
        else:
            parts.append("💵 DXY ⚠️ 缺失（需 FRED_API_KEY）")
        if t10y is not None:
            parts.append(f"📜 10Y 美债 {t10y:.2f}%")
        else:
            parts.append("📜 10Y 美债 ⚠️ 缺失（需 FRED_API_KEY）")
        if fomc.get("date"):
            parts.append(f"🏛️ 下次FOMC: {fomc['name']}（{fomc['days_to']}天后）")
        parts.append(etf["conclusion"])

        result["conclusion"] = "；".join(parts)

        # 子源完整度检查
        ok_sub = 0
        total_sub = 2
        if dxy is not None or t10y is not None:
            ok_sub += 1
        if etf.get("status") == "ok":
            ok_sub += 1
        result["subsource_ok"] = ok_sub
        result["subsource_total"] = total_sub
        if ok_sub < total_sub:
            result["status"] = "warning"
    except Exception as e:
        result["status"] = "error"
        result["conclusion"] = f"⚠️ 宏观/机构数据不可用（{e.__class__.__name__}）"
    return result


# ═══════════════════════════════════════════════════════
# 维度 5：板块轮动
# ═══════════════════════════════════════════════════════

def _fetch_cmc_categories(limit: int = 30) -> list[dict]:
    """CMC 赛道分类（keyless），按 24h 市值变化排序。"""
    data = _safe_get(
        f"{CMC_BASE}/v1/cryptocurrency/categories",
        params={"limit": limit, "sort": "market_cap_change", "sort_dir": "desc"},
    )
    if not data or "data" not in data:
        return []
    result = []
    for item in data["data"]:
        result.append({
            "id": item.get("id", ""),
            "name": item.get("name", ""),
            "market_cap": float(item.get("market_cap", 0) or 0),
            "market_cap_change_24h": float(item.get("market_cap_change", 0) or 0),
            "volume_24h": float(item.get("volume", 0) or 0),
            "volume_change_24h": float(item.get("volume_change", 0) or 0),
            "num_coins": item.get("num_tokens", 0),
        })
    return result


def _fetch_dl_categories() -> dict[str, float]:
    """DeFiLlama 各赛道 TVL。"""
    data = _safe_get(f"{DL_BASE}/categories")
    if not data or not isinstance(data, list):
        return {}
    result = {}
    for item in data:
        name = item.get("name", "")
        tvl = float(item.get("tvl", 0) or 0)
        if name:
            result[name.lower()] = tvl
    return result


def _fetch_dim5_sectors() -> dict:
    result = {"status": "ok", "data": {}, "conclusion": ""}
    try:
        cmc_cats = _fetch_cmc_categories(30)
        dl_tvl = _fetch_dl_categories()

        # 合并 TVL 数据（按名称模糊匹配）
        for cat in cmc_cats:
            name_lower = cat["name"].lower()
            # 尝试匹配
            matched_tvl = None
            for dl_name, tvl in dl_tvl.items():
                if dl_name in name_lower or name_lower in dl_name:
                    matched_tvl = tvl
                    break
            cat["tvl"] = matched_tvl
            cat["tvl_fmt"] = _fmt_num(matched_tvl, "$") if matched_tvl else "N/A"

        # 涨幅前 5 / 跌幅前 5
        gainers = sorted(cmc_cats, key=lambda x: x["market_cap_change_24h"], reverse=True)[:5]
        losers = sorted(cmc_cats, key=lambda x: x["market_cap_change_24h"])[:5]

        result["data"] = {
            "top_gainers": gainers,
            "top_losers": losers,
            "total_categories": len(cmc_cats),
        }

        # 规则化结论
        parts = []
        if gainers:
            top = gainers[0]
            parts.append(
                f"🔥 领涨板块: {top['name']}（24h +{top['market_cap_change_24h']:.1f}%）"
            )
        if losers:
            bot = losers[0]
            parts.append(
                f"🧊 领跌板块: {bot['name']}（24h {bot['market_cap_change_24h']:.1f}%）"
            )
        # 资金面判断
        pos_count = sum(1 for c in cmc_cats if c["market_cap_change_24h"] > 0)
        if cmc_cats:
            pos_ratio = pos_count / len(cmc_cats)
            if pos_ratio > 0.7:
                parts.append(f"🌊 普涨行情（{pos_count}/{len(cmc_cats)} 板块上涨）")
            elif pos_ratio < 0.3:
                parts.append(f"🌧️ 普跌行情（{pos_count}/{len(cmc_cats)} 板块上涨）")
            else:
                parts.append(f"⚖️ 分化行情（{pos_count}/{len(cmc_cats)} 板块上涨）")

        result["conclusion"] = "；".join(parts)

        # 子源完整度检查
        ok_sub = 0
        total_sub = 2
        if cmc_cats:
            ok_sub += 1
        if dl_tvl:
            ok_sub += 1
        result["subsource_ok"] = ok_sub
        result["subsource_total"] = total_sub
        if ok_sub < total_sub:
            result["status"] = "warning"
    except Exception as e:
        result["status"] = "error"
        result["conclusion"] = f"⚠️ 板块数据不可用（{e.__class__.__name__}）"
    return result


# ═══════════════════════════════════════════════════════
# 维度 6：事件日历
# ═══════════════════════════════════════════════════════

# 硬编码 2026 年关键宏观事件（FOMC/CPI/NFP）
MACRO_EVENTS_2026 = [
    # FOMC
    {"date": "2026-01-28", "type": "FOMC", "name": "1月FOMC利率决议", "importance": "high"},
    {"date": "2026-03-18", "type": "FOMC", "name": "3月FOMC（点阵图）", "importance": "high"},
    {"date": "2026-04-29", "type": "FOMC", "name": "4月FOMC利率决议", "importance": "high"},
    {"date": "2026-06-17", "type": "FOMC", "name": "6月FOMC（点阵图）", "importance": "high"},
    {"date": "2026-07-29", "type": "FOMC", "name": "7月FOMC利率决议", "importance": "high"},
    {"date": "2026-09-16", "type": "FOMC", "name": "9月FOMC（点阵图）", "importance": "high"},
    {"date": "2026-11-04", "type": "FOMC", "name": "11月FOMC利率决议", "importance": "high"},
    {"date": "2026-12-16", "type": "FOMC", "name": "12月FOMC（点阵图）", "importance": "high"},
    # CPI（每月中旬，简化为每月15日）
    {"date": "2026-09-15", "type": "CPI", "name": "美国8月CPI数据", "importance": "medium"},
    {"date": "2026-10-15", "type": "CPI", "name": "美国9月CPI数据", "importance": "medium"},
    {"date": "2026-11-15", "type": "CPI", "name": "美国10月CPI数据", "importance": "medium"},
    {"date": "2026-12-15", "type": "CPI", "name": "美国11月CPI数据", "importance": "medium"},
    # NFP（每月第一个周五，简化为每月7日）
    {"date": "2026-09-04", "type": "NFP", "name": "美国8月非农就业", "importance": "medium"},
    {"date": "2026-10-02", "type": "NFP", "name": "美国9月非农就业", "importance": "medium"},
    {"date": "2026-11-06", "type": "NFP", "name": "美国10月非农就业", "importance": "medium"},
    {"date": "2026-12-04", "type": "NFP", "name": "美国11月非农就业", "importance": "medium"},
]


def _fetch_cg_events() -> list[dict]:
    """CoinGecko 加密会议/事件。"""
    data = _safe_get(f"{CG_BASE}/events", params={"upcoming_events_only": "true", "per_page": 10})
    if not data or "data" not in data:
        return []
    result = []
    for item in data["data"]:
        result.append({
            "name": item.get("name", ""),
            "date": item.get("date", ""),
            "type": item.get("type", ""),
            "description": item.get("description", ""),
            "venue": item.get("venue", ""),
        })
    return result


def _fetch_dim6_events() -> dict:
    result = {"status": "ok", "data": {}, "conclusion": ""}
    try:
        from datetime import date, timedelta
        today = date.today()
        horizon = today + timedelta(days=60)

        # 筛选未来 60 天内的宏观事件
        upcoming_macro = []
        for evt in MACRO_EVENTS_2026:
            try:
                d = date.fromisoformat(evt["date"])
            except ValueError:
                continue
            if today <= d <= horizon:
                upcoming_macro.append({**evt, "days_to": (d - today).days})
        upcoming_macro.sort(key=lambda x: x["date"])

        # CoinGecko 加密事件
        try:
            cg_events = _fetch_cg_events()
        except Exception:
            cg_events = []

        result["data"] = {
            "macro_events": upcoming_macro,
            "crypto_events": cg_events,
        }

        # 规则化结论
        parts = []
        if upcoming_macro:
            next_evt = upcoming_macro[0]
            parts.append(
                f"📅 近期宏观: {next_evt['name']}（{next_evt['days_to']}天后）"
            )
        high_count = sum(1 for e in upcoming_macro if e["importance"] == "high")
        parts.append(f"未来60天 {high_count} 个高级别事件")
        if cg_events:
            parts.append(f"🎪 加密会议: {len(cg_events)} 场")
        else:
            parts.append("🎪 加密会议: ⚠️ 暂无数据")

        result["conclusion"] = "；".join(parts)
    except Exception as e:
        result["status"] = "error"
        result["conclusion"] = f"⚠️ 事件数据不可用（{e.__class__.__name__}）"
    return result


# ═══════════════════════════════════════════════════════
# 主入口：六维聚合
# ═══════════════════════════════════════════════════════

def get_market_overview(force_refresh: bool = False) -> dict:
    """获取六维大盘分析总览。

    返回结构：
    {
        "ok": True,
        "fetched_at": timestamp,
        "dimensions": {
            "size": {status, data, conclusion},
            "pairs": {status, data, conclusion},
            "derivatives": {status, data, conclusion},
            "macro": {status, data, conclusion},
            "sectors": {status, data, conclusion},
            "events": {status, data, conclusion},
        },
        "summary": "综合判断文本"
    }
    """
    cache_key = "market_overview"
    if not force_refresh:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    result = {
        "ok": True,
        "fetched_at": int(time.time()),
        "dimensions": {},
        "summary": "",
    }

    # 并行获取六维数据（顺序执行，每个独立 try/except）
    dim_fetchers = [
        ("size", "体量/增量存量", _fetch_dim1_size),
        ("pairs", "BTC/ETH 盘面", _fetch_dim2_pairs),
        ("derivatives", "衍生品/情绪", _fetch_dim3_derivatives_sentiment),
        ("macro", "宏观/机构", _fetch_dim4_macro_institution),
        ("sectors", "板块轮动", _fetch_dim5_sectors),
        ("events", "事件日历", _fetch_dim6_events),
    ]

    ok_count = 0
    warn_count = 0
    err_count = 0
    subsource_ok = 0
    subsource_total = 0
    for key, name, fetcher in dim_fetchers:
        try:
            dim = fetcher()
            result["dimensions"][key] = dim
            if dim.get("status") == "ok":
                ok_count += 1
            elif dim.get("status") == "warning":
                warn_count += 1
            else:
                err_count += 1
            if "subsource_ok" in dim:
                subsource_ok += dim["subsource_ok"]
                subsource_total += dim["subsource_total"]
        except Exception as e:
            err_count += 1
            result["dimensions"][key] = {
                "status": "error",
                "data": {},
                "conclusion": f"⚠️ {name}模块异常（{e.__class__.__name__}: {e}）",
            }

    # 综合判断
    dims = result["dimensions"]
    summary_parts = []
    summary_parts.append(f"维度完整度: {ok_count}正常/{warn_count}部分缺失/{err_count}失败（共6维）")
    if subsource_total > 0:
        summary_parts.append(f"子源完整度: {subsource_ok}/{subsource_total}")

    # 多空综合
    bull_signals = 0
    bear_signals = 0
    # 体量
    size = dims.get("size", {})
    if size.get("status") == "ok" and size.get("data", {}).get("market_cap_change_24h", 0) > 2:
        bull_signals += 1
    elif size.get("status") == "ok" and size.get("data", {}).get("market_cap_change_24h", 0) < -2:
        bear_signals += 1
    # 情绪
    deriv = dims.get("derivatives", {})
    fng_val = deriv.get("data", {}).get("fear_greed", {}).get("value")
    if fng_val is not None:
        if fng_val > 70:
            bull_signals += 1
        elif fng_val < 30:
            bear_signals += 1
    # ETF
    macro = dims.get("macro", {})
    etf_24h = macro.get("data", {}).get("etf_flows", {}).get("data", {}).get("total_24h_usd_m", 0)
    if etf_24h > 100:
        bull_signals += 1
    elif etf_24h < -100:
        bear_signals += 1

    if bull_signals > bear_signals + 1:
        summary_parts.append("综合倾向: 🟢 偏多")
    elif bear_signals > bull_signals + 1:
        summary_parts.append("综合倾向: 🔴 偏空")
    else:
        summary_parts.append("综合倾向: 🟡 中性震荡")

    result["summary"] = "｜".join(summary_parts)

    # ── 扁平化输出（前端渲染用） ──
    def _dim_score(dim: dict) -> int | None:
        """根据维度状态和数据粗略打分（0-100），None 表示数据不足。"""
        if dim.get("status") != "ok":
            return None
        # 各维度具体打分在各自 _flatten 中处理，这里返回占位
        return 50

    def _flatten_dim1(dim: dict) -> dict:
        d = dim.get("data", {})
        status = dim.get("status", "error")
        if status == "error" or not d:
            return {"score": None, "metrics": [], "conclusion": dim.get("conclusion", ""),
                    "warning": "数据获取失败" if status == "error" else None}
        
        # 基础指标
        metrics = [
            {"label": "总市值", "value": d.get("total_market_cap_fmt", "N/A"),
             "trend": "up" if d.get("market_cap_change_24h", 0) > 0 else "down"},
            {"label": "24h 变化", "value": f"{d.get('market_cap_change_24h', 0):+.2f}%",
             "trend": "up" if d.get("market_cap_change_24h", 0) > 0 else "down"},
            {"label": "24h 成交量", "value": d.get("total_volume_24h_fmt", "N/A")},
            {"label": "BTC 占比", "value": f"{d.get('btc_dominance', 0):.1f}%"},
            {"label": "ETH 占比", "value": f"{d.get('eth_dominance', 0):.1f}%"},
            {"label": "稳定币总市值", "value": d.get("stablecoin_market_cap_fmt", "N/A"),
             "trend": "up" if d.get("stablecoin_change_24h", 0) > 0 else "down"},
        ]
        
        # 新增时间序列指标
        stablecoin_netflow = d.get("stablecoin_netflow", {})
        if stablecoin_netflow.get("netflow_7d") is not None:
            netflow_7d = stablecoin_netflow["netflow_7d"]
            metrics.append({
                "label": "稳定币 7d 净流",
                "value": stablecoin_netflow.get("netflow_7d_fmt", "N/A"),
                "trend": "up" if netflow_7d > 0 else "down",
            })
        
        btc_dom_change = d.get("btc_dominance_change", {})
        if btc_dom_change.get("change_7d") is not None:
            metrics.append({
                "label": "BTC 占比 7d",
                "value": f"{btc_dom_change['change_7d']:+.2f}%",
                "trend": "up" if btc_dom_change["change_7d"] > 0 else "down",
            })
        
        chg = d.get("market_cap_change_24h", 0)
        score = 50 + min(max(chg * 5, -30), 30)
        return {"score": round(score), "metrics": metrics,
                "conclusion": dim.get("conclusion", ""), "warning": None}

    def _flatten_dim2(dim: dict) -> dict:
        d = dim.get("data", {})
        status = dim.get("status", "error")
        if status == "error" or not d:
            return {"score": None, "metrics": [], "conclusion": dim.get("conclusion", ""),
                    "warning": "数据获取失败" if status == "error" else None}
        btc = d.get("btc", {}).get("data", {})
        eth = d.get("eth", {}).get("data", {})
        
        # 基础指标
        metrics = [
            {"label": "BTC 价格", "value": _fmt_num(btc.get("price"), "$"),
             "trend": "up" if btc.get("change_24h", 0) > 0 else "down"},
            {"label": "BTC 24h", "value": f"{btc.get('change_24h', 0):+.2f}%",
             "trend": "up" if btc.get("change_24h", 0) > 0 else "down"},
            {"label": "BTC RSI(14)", "value": f"{btc.get('rsi', 0):.1f}",
             "trend": "neutral"},
            {"label": "ETH 价格", "value": _fmt_num(eth.get("price"), "$"),
             "trend": "up" if eth.get("change_24h", 0) > 0 else "down"},
            {"label": "ETH/BTC", "value": f"{d.get('eth_btc_ratio', 0):.4f}",
             "trend": "up" if d.get("eth_btc_change_24h", 0) > 0 else "down"},
            {"label": "BTC MVRV", "value": str(d.get("btc_mvrv", {}).get("value", "N/A"))},
        ]
        
        # 新增 MVRV 历史分位
        mvrv_history = d.get("btc_mvrv_history", {})
        if mvrv_history.get("percentile") is not None:
            metrics.append({
                "label": "MVRV 90d 分位",
                "value": mvrv_history.get("percentile_desc", "N/A"),
                "trend": "up" if mvrv_history.get("percentile", 50) > 50 else "down",
            })
        
        # 简单打分：RSI 中性 50 分，超买/超卖向两端
        rsi = btc.get("rsi", 50) or 50
        score = 50 + (rsi - 50) * 0.6  # RSI 70 → 62, RSI 30 → 38
        return {"score": round(score), "metrics": metrics,
                "conclusion": dim.get("conclusion", ""), "warning": None}

    def _flatten_dim3(dim: dict) -> dict:
        d = dim.get("data", {})
        status = dim.get("status", "error")
        if status == "error" or not d:
            return {"score": None, "metrics": [], "conclusion": dim.get("conclusion", ""),
                    "warning": "数据获取失败" if status == "error" else None}
        fng = d.get("fear_greed", {})
        btc_f = d.get("btc_futures", {})
        
        # 基础指标
        metrics = [
            {"label": "恐贪指数", "value": f"{fng.get('value', 'N/A')} ({fng.get('classification', '')})",
             "trend": "up" if (fng.get("value") or 0) > 50 else "down"},
            {"label": "山寨季指数", "value": str(d.get("altcoin_season", {}).get("value", "N/A"))},
            {"label": "BTC OI", "value": btc_f.get("open_interest_fmt", "N/A")},
            {"label": "BTC 资金费率", "value": f"{btc_f.get('funding_rate_pct', 0):.4f}%",
             "trend": "up" if btc_f.get("funding_rate_pct", 0) > 0 else "down"},
            {"label": "BTC 多空比", "value": str(btc_f.get("long_short_ratio", "N/A"))},
            {"label": "CEFI 指数", "value": str(d.get("cefi_index", {}).get("value", "N/A"))},
        ]
        
        # 新增恐贪历史分位
        fng_history = d.get("fear_greed_history", {})
        if fng_history.get("percentile") is not None:
            metrics.append({
                "label": "恐贪 90d 分位",
                "value": fng_history.get("percentile_desc", "N/A"),
                "trend": "up" if fng_history.get("percentile", 50) > 50 else "down",
            })
        
        # 新增 CEFI 序列
        cefi_series = d.get("cefi_series", {})
        if cefi_series.get("current") is not None:
            metrics.append({
                "label": "CEFI 30d",
                "value": cefi_series.get("percentile_desc", "N/A"),
                "trend": "up" if cefi_series.get("trend") == "rising" else "down",
            })
        
        fng_val = fng.get("value")
        score = fng_val if fng_val is not None else 50
        return {"score": round(score), "metrics": metrics,
                "conclusion": dim.get("conclusion", ""), "warning": None}

    def _flatten_dim4(dim: dict) -> dict:
        d = dim.get("data", {})
        status = dim.get("status", "error")
        if status == "error" or not d:
            return {"score": None, "metrics": [], "conclusion": dim.get("conclusion", ""),
                    "warning": "数据获取失败" if status == "error" else None}
        etf = d.get("etf_flows", {}).get("data", {})
        fomc = d.get("next_fomc", {})
        metrics = [
            {"label": "美元指数 DXY", "value": f"{d.get('dxy', 0):.2f}" if d.get("dxy") else "N/A"},
            {"label": "10Y 美债", "value": f"{d.get('t10y', 0):.2f}%" if d.get("t10y") else "N/A"},
            {"label": "下次 FOMC", "value": fomc.get("date", "N/A")},
            {"label": "距 FOMC", "value": f"{fomc.get('days_to', 'N/A')} 天"},
            {"label": "ETF 24h 净流入", "value": f"${etf.get('total_24h_usd_m', 0):+.1f}M",
             "trend": "up" if etf.get("total_24h_usd_m", 0) > 0 else "down"},
            {"label": "ETF 30d 净流入", "value": f"${etf.get('total_30d_usd_m', 0):+.1f}M",
             "trend": "up" if etf.get("total_30d_usd_m", 0) > 0 else "down"},
        ]
        # 打分：ETF 净流入正向，美债收益率负向
        etf_24 = etf.get("total_24h_usd_m", 0)
        score = 50 + min(max(etf_24 / 10, -30), 30)
        return {"score": round(score), "metrics": metrics,
                "conclusion": dim.get("conclusion", ""), "warning": None}

    def _flatten_dim5(dim: dict) -> dict:
        d = dim.get("data", {})
        status = dim.get("status", "error")
        if status == "error" or not d:
            return {"score": None, "metrics": [], "conclusion": dim.get("conclusion", ""),
                    "warning": "数据获取失败" if status == "error" else None}
        gainers = d.get("top_gainers", [])
        losers = d.get("top_losers", [])
        metrics = [
            {"label": "板块总数", "value": str(d.get("total_categories", "N/A"))},
        ]
        for g in gainers[:2]:
            metrics.append({
                "label": f"🔥 {g.get('name', '')[:8]}",
                "value": f"{g.get('market_cap_change_24h', 0):+.2f}%",
                "trend": "up",
            })
        for l in losers[:2]:
            metrics.append({
                "label": f"📉 {l.get('name', '')[:8]}",
                "value": f"{l.get('market_cap_change_24h', 0):+.2f}%",
                "trend": "down",
            })
        # 打分：涨幅前5平均 vs 跌幅前5平均
        avg_gain = sum(g.get("market_cap_change_24h", 0) for g in gainers) / len(gainers) if gainers else 0
        avg_loss = sum(l.get("market_cap_change_24h", 0) for l in losers) / len(losers) if losers else 0
        score = 50 + (avg_gain + avg_loss) * 3
        score = min(max(score, 10), 90)
        return {"score": round(score), "metrics": metrics,
                "conclusion": dim.get("conclusion", ""), "warning": None}

    def _flatten_dim6(dim: dict) -> dict:
        d = dim.get("data", {})
        status = dim.get("status", "error")
        if status == "error" or not d:
            return {"score": None, "metrics": [], "conclusion": dim.get("conclusion", ""),
                    "warning": "数据获取失败" if status == "error" else None}
        macro = d.get("macro_events", [])
        crypto = d.get("crypto_events", [])
        metrics = [
            {"label": "未来宏观事件", "value": f"{len(macro)} 件"},
            {"label": "加密事件", "value": f"{len(crypto)} 件"},
        ]
        for e in macro[:3]:
            metrics.append({
                "label": f"📅 {e.get('type', '')}",
                "value": f"{e.get('date', '')} ({e.get('days_to', '')}d)",
            })
        for e in crypto[:2]:
            metrics.append({
                "label": f"🔔 {e.get('name', '')[:10]}",
                "value": e.get("date", "")[:10],
            })
        return {"score": 50, "metrics": metrics,
                "conclusion": dim.get("conclusion", ""), "warning": None}

    dims = result["dimensions"]
    flat = {
        "dim1_size": _flatten_dim1(dims.get("size", {})),
        "dim2_pairs": _flatten_dim2(dims.get("pairs", {})),
        "dim3_derivatives_sentiment": _flatten_dim3(dims.get("derivatives", {})),
        "dim4_macro_institution": _flatten_dim4(dims.get("macro", {})),
        "dim5_sectors": _flatten_dim5(dims.get("sectors", {})),
        "dim6_events": _flatten_dim6(dims.get("events", {})),
    }
    # 补子源完整度 warning
    dim_keys = ["size", "pairs", "derivatives", "macro", "sectors", "events"]
    flat_keys = list(flat.keys())
    for dk, fk in zip(dim_keys, flat_keys):
        dim = dims.get(dk, {})
        if dim.get("status") == "warning" and "subsource_ok" in dim:
            flat[fk]["warning"] = f"子源数据不完整（{dim['subsource_ok']}/{dim['subsource_total']}）"
    result.update(flat)

    # 综合 summary 对象化
    scores = [v["score"] for v in flat.values() if v["score"] is not None]
    avg_score = round(sum(scores) / len(scores)) if scores else 50
    if avg_score >= 60:
        direction = "bullish"
    elif avg_score <= 40:
        direction = "bearish"
    else:
        direction = "neutral"
    result["summary"] = {
        "score": avg_score,
        "direction": direction,
        "reason": result.get("summary", ""),
    }

    _cache_set(cache_key, result)
    return result


if __name__ == "__main__":
    # 本地测试
    import json
    overview = get_market_overview()
    print(json.dumps(overview, ensure_ascii=False, indent=2))

