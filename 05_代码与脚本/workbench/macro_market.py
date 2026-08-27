"""
大盘宏观分析模块。
聚合六维数据，输出 emotion_subscore + structure_subscore 两项子分。
事件日历仅作为独立信息栏展示，不参与任何子分计算。
"""

from __future__ import annotations

import os
import time
import requests
from typing import Any

# ── 数据源配置 ──
CMC_BASE = "https://pro-api.coinmarketcap.com"
CRYPTOETF_BASE = "https://api.cryptoetf.today/api"
BINANCE_BASE = "https://api.binance.com"
BINANCE_FAPI = "https://fapi.binance.com"
COINMETRICS_BASE = "https://community-api.coinmetrics.io"
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
TIMEOUT = 15

# ── 权重配置（P2-4 接 yaml 外置） ──
EMOTION_WEIGHTS = {
    "fear_greed": 0.40,      # 恐贪指数
    "altcoin_season": 0.20,  # 山寨季指数
    "cefi": 0.20,            # CEFI 指数
    "derivative": 0.20,      # 衍生品极值
}

STRUCTURE_WEIGHTS = {
    "market_cap": 0.25,      # 体量（总市值）
    "price_action": 0.25,    # 盘面（BTC/ETH 技术面）
    "institution": 0.25,     # 机构（ETF 等）
    "sector": 0.25,          # 板块轮动
}

# ── 缓存 ──
_cache: dict[str, Any] = {}
_cache_ts: float = 0
CACHE_TTL = 180  # 3 分钟缓存


def _safe_float(v: Any, default: float = 0.0) -> float:
    """安全转换为 float。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    """安全转换为 int。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ══════════════════════════════════════════════════════════════
# 数据获取函数
# ══════════════════════════════════════════════════════════════

def fetch_cmc_global_metrics() -> dict:
    """获取 CMC 全球市值数据。返回 {total_market_cap, volume_24h, btc_dominance, stablecoin_market_cap, ...}。"""
    try:
        r = requests.get(
            f"{CMC_BASE}/trial-pro-api/v1/global-metrics/quotes/latest",
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json().get("data", {})
        quote = data.get("quote", [{}])[0] if data.get("quote") else {}
        return {
            "total_market_cap": _safe_float(quote.get("total_market_cap")),
            "total_volume_24h": _safe_float(quote.get("total_volume_24h")),
            "btc_dominance": _safe_float(data.get("btc_dominance")),
            "eth_dominance": _safe_float(data.get("eth_dominance")),
            "stablecoin_market_cap": _safe_float(data.get("stablecoin_market_cap")),
            "total_cryptocurrencies": _safe_int(data.get("total_cryptocurrencies")),
            "status": "ok",
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def fetch_cmc_fear_greed() -> dict:
    """获取 CMC 恐贪指数。返回 {value, value_classification, ...}。"""
    try:
        r = requests.get(
            f"{CMC_BASE}/trial-pro-api/v3/fear-and-greed",
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json().get("data", {})
        return {
            "value": _safe_float(data.get("value")),
            "value_classification": data.get("value_classification", ""),
            "status": "ok",
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def fetch_cmc_altcoin_season() -> dict:
    """获取 CMC 山寨季指数。返回 {value, status}。"""
    try:
        r = requests.get(
            f"{CMC_BASE}/trial-pro-api/v1/altcoin-season-index",
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json().get("data", {})
        return {
            "value": _safe_float(data.get("value")),
            "status": "ok",
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def fetch_cryptoetf_cefi() -> dict:
    """获取 cryptoetf CEFI 指数。返回 {value, status}。"""
    api_key = os.environ.get("CRYPTOETF_KEY", "")
    if not api_key:
        return {"status": "skipped", "error": "CRYPTOETF_KEY 未设置"}
    try:
        r = requests.get(
            f"{CRYPTOETF_BASE}/v1/index/cefi",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json().get("data", {})
        return {
            "value": _safe_float(data.get("value")),
            "status": "ok",
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def fetch_binance_btc_klines() -> dict:
    """获取 BTC 日线 K 线（30 天），计算技术指标。返回 {rsi, ma20, ma50, price, ...}。"""
    try:
        r = requests.get(
            f"{BINANCE_BASE}/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "1d", "limit": 60},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        if not data or len(data) < 2:
            return {"status": "error", "error": "数据不足"}

        closes = [_safe_float(k[4]) for k in data]
        latest = closes[-1]

        # 简单 RSI 计算（14 天）
        rsi = 50.0  # 默认中性
        if len(closes) >= 15:
            gains = []
            losses = []
            for i in range(-14, 0):
                diff = closes[i] - closes[i - 1]
                if diff > 0:
                    gains.append(diff)
                    losses.append(0)
                else:
                    gains.append(0)
                    losses.append(abs(diff))
            avg_gain = sum(gains) / 14 if gains else 0
            avg_loss = sum(losses) / 14 if losses else 0
            if avg_loss > 0:
                rs = avg_gain / avg_loss
                rsi = round(100 - (100 / (1 + rs)), 1)
            else:
                rsi = 100.0

        # MA20, MA50
        ma20 = round(sum(closes[-20:]) / 20, 2) if len(closes) >= 20 else None
        ma50 = round(sum(closes[-50:]) / 50, 2) if len(closes) >= 50 else None

        return {
            "price": latest,
            "rsi": rsi,
            "ma20": ma20,
            "ma50": ma50,
            "high_24h": _safe_float(data[-1][2]),
            "low_24h": _safe_float(data[-1][3]),
            "status": "ok",
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def fetch_binance_eth_klines() -> dict:
    """获取 ETH 日线 K 线（30 天），计算技术指标。"""
    try:
        r = requests.get(
            f"{BINANCE_BASE}/api/v3/klines",
            params={"symbol": "ETHUSDT", "interval": "1d", "limit": 60},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        if not data or len(data) < 2:
            return {"status": "error", "error": "数据不足"}

        closes = [_safe_float(k[4]) for k in data]
        latest = closes[-1]

        # 简单 RSI
        rsi = 50.0
        if len(closes) >= 15:
            gains = []
            losses = []
            for i in range(-14, 0):
                diff = closes[i] - closes[i - 1]
                if diff > 0:
                    gains.append(diff)
                    losses.append(0)
                else:
                    gains.append(0)
                    losses.append(abs(diff))
            avg_gain = sum(gains) / 14 if gains else 0
            avg_loss = sum(losses) / 14 if losses else 0
            if avg_loss > 0:
                rs = avg_gain / avg_loss
                rsi = round(100 - (100 / (1 + rs)), 1)
            else:
                rsi = 100.0

        ma20 = round(sum(closes[-20:]) / 20, 2) if len(closes) >= 20 else None

        return {
            "price": latest,
            "rsi": rsi,
            "ma20": ma20,
            "status": "ok",
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def fetch_binance_derivatives() -> dict:
    """获取 Binance 衍生品数据：funding rate + OI。返回 {funding_rate, open_interest, ...}。"""
    try:
        # Funding rate
        r_funding = requests.get(
            f"{BINANCE_FAPI}/fapi/v1/fundingRate",
            params={"symbol": "BTCUSDT", "limit": 1},
            timeout=TIMEOUT,
        )
        r_funding.raise_for_status()
        funding_data = r_funding.json()
        funding_rate = _safe_float(funding_data[0].get("fundingRate")) if funding_data else 0

        # Open Interest
        r_oi = requests.get(
            f"{BINANCE_FAPI}/fapi/v1/openInterest",
            params={"symbol": "BTCUSDT"},
            timeout=TIMEOUT,
        )
        r_oi.raise_for_status()
        oi_data = r_oi.json()
        open_interest = _safe_float(oi_data.get("openInterest"))

        return {
            "funding_rate": funding_rate,
            "open_interest": open_interest,
            "status": "ok",
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def fetch_binance_etf_flows() -> dict:
    """获取 cryptoetf ETF 资金流数据。返回 {net_flow_usd_m, ...}。"""
    api_key = os.environ.get("CRYPTOETF_KEY", "")
    if not api_key:
        return {"status": "skipped", "error": "CRYPTOETF_KEY 未设置"}
    try:
        r = requests.get(
            f"{CRYPTOETF_BASE}/v1/flows/summary",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json().get("data", {})
        return {
            "net_flow_usd_m": _safe_float(data.get("netFlowUsdM")),
            "status": "ok",
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def fetch_cmc_categories() -> dict:
    """获取 CMC 板块数据。返回 {categories: [...]}。"""
    try:
        r = requests.get(
            f"{CMC_BASE}/trial-pro-api/v1/cryptocurrency/categories",
            params={"limit": 10},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json().get("data", [])
        categories = []
        for cat in data[:10]:
            quote = cat.get("quote", [{}])[0] if cat.get("quote") else {}
            categories.append({
                "name": cat.get("name", ""),
                "market_cap_change_24h": _safe_float(quote.get("market_cap_change_24h")),
                "volume_change_24h": _safe_float(quote.get("volume_change_24h")),
            })
        return {
            "categories": categories,
            "status": "ok",
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def fetch_event_calendar() -> dict:
    """获取事件日历（硬编码 FOMC + CoinGecko events）。仅展示，不参与子分。"""
    # 硬编码重要事件
    hardcoded_events = [
        {"date": "2026-09-16", "event": "FOMC 议息会议", "type": "macro"},
        {"date": "2026-10-28", "event": "FOMC 议息会议", "type": "macro"},
        {"date": "2026-12-16", "event": "FOMC 议息会议", "type": "macro"},
    ]

    # CoinGecko events 兜底
    gecko_events = []
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/events",
            params={"limit": 5},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json().get("data", [])
        for evt in data[:5]:
            gecko_events.append({
                "date": evt.get("date", ""),
                "event": evt.get("title", ""),
                "type": "crypto",
            })
    except Exception:
        pass

    return {
        "hardcoded": hardcoded_events,
        "gecko": gecko_events,
        "status": "ok",
    }


# ══════════════════════════════════════════════════════════════
# 子分计算函数
# ══════════════════════════════════════════════════════════════

def compute_emotion_subscore(
    fear_greed: dict,
    altcoin_season: dict,
    cefi: dict,
    derivatives: dict,
) -> dict:
    """
    计算情绪子分 = 恐贪（权重）+ 山寨季 + CEFI + 衍生品极值。
    返回 {score, components: {...}, status}。
    """
    components = {}
    available_weights = 0.0
    weighted_sum = 0.0

    # 恐贪指数 (0-100)
    fg_value = fear_greed.get("value")
    if fear_greed.get("status") == "ok" and fg_value is not None:
        fg_score = _safe_float(fg_value)
        components["fear_greed"] = {"value": fg_value, "score": fg_score, "weight": EMOTION_WEIGHTS["fear_greed"]}
        weighted_sum += fg_score * EMOTION_WEIGHTS["fear_greed"]
        available_weights += EMOTION_WEIGHTS["fear_greed"]
    else:
        components["fear_greed"] = {"status": fear_greed.get("status", "error"), "weight": EMOTION_WEIGHTS["fear_greed"]}

    # 山寨季指数 (0-100)
    alt_value = altcoin_season.get("value")
    if altcoin_season.get("status") == "ok" and alt_value is not None:
        alt_score = _safe_float(alt_value)
        components["altcoin_season"] = {"value": alt_value, "score": alt_score, "weight": EMOTION_WEIGHTS["altcoin_season"]}
        weighted_sum += alt_score * EMOTION_WEIGHTS["altcoin_season"]
        available_weights += EMOTION_WEIGHTS["altcoin_season"]
    else:
        components["altcoin_season"] = {"status": altcoin_season.get("status", "error"), "weight": EMOTION_WEIGHTS["altcoin_season"]}

    # CEFI 指数 (0-100)
    cefi_value = cefi.get("value")
    if cefi.get("status") == "ok" and cefi_value is not None:
        cefi_score = _safe_float(cefi_value)
        components["cefi"] = {"value": cefi_value, "score": cefi_score, "weight": EMOTION_WEIGHTS["cefi"]}
        weighted_sum += cefi_score * EMOTION_WEIGHTS["cefi"]
        available_weights += EMOTION_WEIGHTS["cefi"]
    else:
        components["cefi"] = {"status": cefi.get("status", "error"), "weight": EMOTION_WEIGHTS["cefi"]}

    # 衍生品极值（funding rate + OI 增速合成）
    if derivatives.get("status") == "ok":
        funding = derivatives.get("funding_rate", 0)
        oi = derivatives.get("open_interest", 0)

        # Funding rate 极值评分：绝对值越大越极端
        # 正 funding 表示多头付费（看涨过热），负 funding 表示空头付费（看跌过热）
        funding_abs = abs(funding)
        if funding_abs > 0.001:  # 极端
            deriv_score = 80 if funding > 0 else 20  # 多头过热=80，空头过热=20
        elif funding_abs > 0.0005:
            deriv_score = 65 if funding > 0 else 35
        else:
            deriv_score = 50  # 中性

        components["derivative"] = {
            "funding_rate": funding,
            "open_interest": oi,
            "score": deriv_score,
            "weight": EMOTION_WEIGHTS["derivative"],
        }
        weighted_sum += deriv_score * EMOTION_WEIGHTS["derivative"]
        available_weights += EMOTION_WEIGHTS["derivative"]
    else:
        components["derivative"] = {"status": derivatives.get("status", "error"), "weight": EMOTION_WEIGHTS["derivative"]}

    # 归一化（按可用权重）
    if available_weights > 0:
        final_score = round(weighted_sum / available_weights, 1)
    else:
        final_score = None

    overall_status = "ok" if available_weights >= 0.4 else ("warning" if available_weights > 0 else "error")

    return {
        "score": final_score,
        "components": components,
        "available_weight": round(available_weights, 2),
        "status": overall_status,
    }


def compute_structure_subscore(
    global_metrics: dict,
    btc_klines: dict,
    eth_klines: dict,
    etf_flows: dict,
    categories: dict,
) -> dict:
    """
    计算结构子分 = 体量 / 盘面 / 机构 / 板块。
    返回 {score, components: {...}, status}。
    """
    components = {}
    available_weights = 0.0
    weighted_sum = 0.0

    # 体量（总市值）- 用 2.5T 为基准，归一化到 0-100
    if global_metrics.get("status") == "ok":
        total_mcap = global_metrics.get("total_market_cap", 0)
        # 基准：2.5T = 50分，5T = 75分，1T = 25分
        if total_mcap > 0:
            mcap_score = min(100, max(0, total_mcap / 40_000_000_000))  # 40B 步进
        else:
            mcap_score = 50
        components["market_cap"] = {
            "total_market_cap": total_mcap,
            "btc_dominance": global_metrics.get("btc_dominance"),
            "score": mcap_score,
            "weight": STRUCTURE_WEIGHTS["market_cap"],
        }
        weighted_sum += mcap_score * STRUCTURE_WEIGHTS["market_cap"]
        available_weights += STRUCTURE_WEIGHTS["market_cap"]
    else:
        components["market_cap"] = {"status": global_metrics.get("status", "error"), "weight": STRUCTURE_WEIGHTS["market_cap"]}

    # 盘面（BTC RSI + MA 位置）
    if btc_klines.get("status") == "ok":
        rsi = btc_klines.get("rsi", 50)
        price = btc_klines.get("price", 0)
        ma20 = btc_klines.get("ma20")
        ma50 = btc_klines.get("ma50")

        # RSI 评分：50 中性，>70 过热，<30 超卖
        rsi_score = 50 + (rsi - 50) * 0.8  # 线性映射

        # MA 位置加分
        if ma20 and price > ma20:
            rsi_score += 5
        if ma50 and price > ma50:
            rsi_score += 5

        rsi_score = min(100, max(0, rsi_score))

        components["price_action"] = {
            "btc_price": price,
            "btc_rsi": rsi,
            "btc_ma20": ma20,
            "btc_ma50": ma50,
            "eth_price": eth_klines.get("price"),
            "eth_rsi": eth_klines.get("rsi"),
            "score": round(rsi_score, 1),
            "weight": STRUCTURE_WEIGHTS["price_action"],
        }
        weighted_sum += rsi_score * STRUCTURE_WEIGHTS["price_action"]
        available_weights += STRUCTURE_WEIGHTS["price_action"]
    else:
        components["price_action"] = {"status": btc_klines.get("status", "error"), "weight": STRUCTURE_WEIGHTS["price_action"]}

    # 机构（ETF 资金流）
    if etf_flows.get("status") == "ok":
        net_flow = etf_flows.get("net_flow_usd_m", 0)
        # 资金流评分：正流入加分，流出减分
        if net_flow > 100:
            inst_score = 80
        elif net_flow > 0:
            inst_score = 60
        elif net_flow > -100:
            inst_score = 40
        else:
            inst_score = 20

        components["institution"] = {
            "net_flow_usd_m": net_flow,
            "score": inst_score,
            "weight": STRUCTURE_WEIGHTS["institution"],
        }
        weighted_sum += inst_score * STRUCTURE_WEIGHTS["institution"]
        available_weights += STRUCTURE_WEIGHTS["institution"]
    else:
        components["institution"] = {"status": etf_flows.get("status", "error"), "weight": STRUCTURE_WEIGHTS["institution"]}

    # 板块（市值变化平均）
    if categories.get("status") == "ok":
        cats = categories.get("categories", [])
        if cats:
            avg_change = sum(c.get("market_cap_change_24h", 0) for c in cats) / len(cats)
            # 板块评分：平均涨幅
            sector_score = min(100, max(0, 50 + avg_change * 5))
        else:
            sector_score = 50

        components["sector"] = {
            "avg_market_cap_change_24h": avg_change if cats else 0,
            "category_count": len(cats),
            "score": round(sector_score, 1),
            "weight": STRUCTURE_WEIGHTS["sector"],
        }
        weighted_sum += sector_score * STRUCTURE_WEIGHTS["sector"]
        available_weights += STRUCTURE_WEIGHTS["sector"]
    else:
        components["sector"] = {"status": categories.get("status", "error"), "weight": STRUCTURE_WEIGHTS["sector"]}

    # 归一化
    if available_weights > 0:
        final_score = round(weighted_sum / available_weights, 1)
    else:
        final_score = None

    overall_status = "ok" if available_weights >= 0.4 else ("warning" if available_weights > 0 else "error")

    return {
        "score": final_score,
        "components": components,
        "available_weight": round(available_weights, 2),
        "status": overall_status,
    }


# ══════════════════════════════════════════════════════════════
# 主入口函数
# ══════════════════════════════════════════════════════════════

def get_market_overview(force_refresh: str = "0") -> dict:
    """
    大盘宏观分析总览。
    返回 {summary: {emotion_subscore, structure_subscore}, dimensions: {...}, event_calendar: {...}}。
    """
    global _cache, _cache_ts

    now = time.time()
    if force_refresh != "1" and _cache and (now - _cache_ts) < CACHE_TTL:
        return _cache

    # ── 并行获取所有数据 ──
    global_metrics = fetch_cmc_global_metrics()
    fear_greed = fetch_cmc_fear_greed()
    altcoin_season = fetch_cmc_altcoin_season()
    cefi = fetch_cryptoetf_cefi()
    btc_klines = fetch_binance_btc_klines()
    eth_klines = fetch_binance_eth_klines()
    derivatives = fetch_binance_derivatives()
    etf_flows = fetch_binance_etf_flows()
    categories = fetch_cmc_categories()
    event_calendar = fetch_event_calendar()

    # ── 计算子分 ──
    emotion_subscore = compute_emotion_subscore(
        fear_greed=fear_greed,
        altcoin_season=altcoin_season,
        cefi=cefi,
        derivatives=derivatives,
    )

    structure_subscore = compute_structure_subscore(
        global_metrics=global_metrics,
        btc_klines=btc_klines,
        eth_klines=eth_klines,
        etf_flows=etf_flows,
        categories=categories,
    )

    # ── 组装结果 ──
    result = {
        "summary": {
            "emotion_subscore": emotion_subscore,
            "structure_subscore": structure_subscore,
        },
        "dimensions": {
            "1体量": {
                "status": global_metrics.get("status", "error"),
                "data": global_metrics,
            },
            "2盘面": {
                "status": btc_klines.get("status", "error"),
                "data": {
                    "btc": btc_klines,
                    "eth": eth_klines,
                },
            },
            "3衍生品": {
                "status": derivatives.get("status", "error"),
                "data": derivatives,
            },
            "3情绪": {
                "status": "ok" if all(x.get("status") == "ok" for x in [fear_greed, altcoin_season]) else "partial",
                "data": {
                    "fear_greed": fear_greed,
                    "altcoin_season": altcoin_season,
                    "cefi": cefi,
                },
            },
            "4机构": {
                "status": etf_flows.get("status", "error"),
                "data": etf_flows,
            },
            "5板块": {
                "status": categories.get("status", "error"),
                "data": categories,
            },
        },
        "event_calendar": event_calendar,
        "fetched_at": int(now),
    }

    _cache = result
    _cache_ts = now

    return result


if __name__ == "__main__":
    import json
    result = get_market_overview()
    print(json.dumps(result, indent=2, ensure_ascii=False))
