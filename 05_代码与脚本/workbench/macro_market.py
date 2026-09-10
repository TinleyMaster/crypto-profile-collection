"""
大盘宏观分析模块。
聚合六维数据，输出 emotion_subscore + structure_subscore 两项子分。
事件日历仅作为独立信息栏展示，不参与任何子分计算。
"""

from __future__ import annotations

import os
import re
import time
import requests
from concurrent.futures import ThreadPoolExecutor
from typing import Any

# ── 数据源配置 ──
CMC_BASE = "https://pro-api.coinmarketcap.com"
CRYPTOETF_BASE = "https://api.cryptoetf.today/api"
BINANCE_BASE = "https://api.binance.com"
BINANCE_FAPI = "https://fapi.binance.com"
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
DL_BASE = "https://api.llama.fi"
TIMEOUT = 15

# ── P0-3 权重默认值（P2-4 外置 market_rules.yaml，启动时优先读 yaml） ──
EMOTION_WEIGHTS_DEFAULT = {
    "fear_greed": 0.40,      # 恐贪指数
    "altcoin_season": 0.20,  # 山寨季指数
    "cefi": 0.20,            # CEFI 指数
    "derivative": 0.20,      # 衍生品极值
}

STRUCTURE_WEIGHTS_DEFAULT = {
    "market_cap": 0.25,      # 体量（总市值）
    "price_action": 0.25,    # 盘面（BTC/ETH 技术面）
    "institution": 0.25,     # 机构（ETF 等）
    "sector": 0.25,          # 板块轮动
}

# ── P2-1 极端区阈值默认值（P2-4 外置 market_rules.yaml） ──
EXTREME_ZONE_DEFAULT = {
    "high_pct": 90,          # 百分位 > 90% → HIGH
    "low_pct": 10,           # 百分位 < 10% → LOW
}

# ── P1-1 叙事榜/链榜配置默认值（P2-4 外置 market_rules.yaml） ──
NARRATIVE_CHAIN_DEFAULT = {
    "narrative_top_n": 15,   # 最多拉取 detail 聚合的叙事数
    "tvl_leg_min": 50_000_000,  # TVL 腿阈值
    "chain_top_n": 30,       # 链榜扫描的 top 链数
    "mcap_weight": 0.5,      # 合成叙事榜：市值变化权重
    "tvl_weight": 0.5,       # 合成叙事榜：TVL 变化权重
    "rank_limit": 10,        # 合成叙事榜最终取前 N
}

# ── 评分微调参数默认值（P2-4 外置 market_rules.yaml） ──
SCORING_TUNING_DEFAULT = {
    "funding_extreme_abs": 0.001,
    "funding_high_abs": 0.0005,
    "funding_extreme_bull": 80,
    "funding_extreme_bear": 20,
    "funding_high_bull": 65,
    "funding_high_bear": 35,
    "funding_neutral": 50,
    "mcap_step": 40_000_000_000,
    "rsi_base": 50,
    "rsi_slope": 0.8,
    "ma20_bonus": 5,
    "ma50_bonus": 5,
    "etf_flow_high": 100,
    "etf_flow_low": -100,
    "etf_score_inflow_high": 80,
    "etf_score_inflow": 60,
    "etf_score_outflow": 40,
    "etf_score_outflow_high": 20,
    "sector_base": 50,
    "sector_slope": 5,
    "min_available_weight": 0.4,
}

# ── 缓存 ──
_cache: dict[str, Any] = {}
_cache_ts: float = 0
CACHE_TTL = 180  # 3 分钟缓存

# ── P1-1 资金净流入：叙事榜配置 ──
# 关注的 CMC 叙事分类（无 TVL 赛道（Meme/L1 等）仅用市值变化，有 TVL 赛道与 DeFiLlama 加权合成）
NARRATIVE_WATCHLIST = [
    "Layer 1", "Layer 2", "DeFi", "Memes", "AI & Big Data", "Real World Assets",
    "Gaming", "DePIN", "Liquid Staking", "Lending", "Derivatives", "Yield Farming",
    "Restaking", "Bridges", "Stablecoin", "NFTs & Collectibles", "Metaverse",
    "Privacy", "Oracles", "File Storage", "Zero Knowledge", "SocialFi",
]
# CMC 叙事分类名 → DeFiLlama category（有 TVL 腿才合成）
NARRATIVE_TVL_MAP = {
    "Lending": "Lending",
    "Dexes": "Dexs",
    "DEX": "Dexs",
    "Derivatives": "Derivatives",
    "Liquid Staking": "Liquid Staking",
    "Liquid Staking Derivatives": "Liquid Staking",
    "Restaking": "Restaking",
    "Bridges": "Bridge",
    "Bridging": "Bridge",
    "Real World Assets": "RWA",
    "RWA": "RWA",
    "Yield Farming": "Yield",
    "Staking": "Staking Pool",
    "CDP": "CDP",
    "Yield Aggregator": "Yield Aggregator",
}
# P1-1 叙事/链榜配置（启动时从 yaml 加载，见 _load_market_rules）

# 协议级 change_7d 异常阈值（%）：超过视为低基数/历史回填导致的数据异常，加权时剔除
TVL_CHANGE_CAP = 1000.0


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


def percentile_of(value: float, series: list[float]) -> float | None:
    """
    计算 value 在 series 中的历史百分位（0~100）。
    series 不足 2 个有效数据点时返回 None。
    """
    if value is None:
        return None
    # 过滤掉 None 值
    valid_series = [x for x in series if x is not None]
    if len(valid_series) < 2:
        return None
    sorted_s = sorted(valid_series)
    count_below = sum(1 for x in sorted_s if x < value)
    count_equal = sum(1 for x in sorted_s if x == value)
    # 百分位 = (低于 + 0.5*等于) / 总数 * 100
    percentile = (count_below + 0.5 * count_equal) / len(sorted_s) * 100
    return round(percentile, 1)


def flag_extreme(percentile: float | None) -> str:
    """
    根据百分位标记极端区：>high_pct → HIGH，<low_pct → LOW，否则 NONE。
    """
    if percentile is None:
        return "NONE"
    if percentile > EXTREME_ZONE["high_pct"]:
        return "HIGH"
    if percentile < EXTREME_ZONE["low_pct"]:
        return "LOW"
    return "NONE"


# ══════════════════════════════════════════════════════════════
# 数据获取函数
# ══════════════════════════════════════════════════════════════

def fetch_cmc_global_metrics() -> dict:
    """获取全球市值数据。优先 CMC，失败时降级 CoinGecko。返回 {total_market_cap, volume_24h, btc_dominance, ...}。"""
    # 1. 尝试 CMC
    try:
        r = requests.get(
            f"{CMC_BASE}/trial-pro-api/v1/global-metrics/quotes/latest",
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json().get("data", {})
        quote = (data.get("quote") or {}).get("USD", {})
        total_mcap = _safe_float(quote.get("total_market_cap"))
        if total_mcap > 0:
            return {
                "total_market_cap": total_mcap,
                "total_volume_24h": _safe_float(quote.get("total_volume_24h")),
                "btc_dominance": _safe_float(data.get("btc_dominance")),
                "eth_dominance": _safe_float(data.get("eth_dominance")),
                "stablecoin_market_cap": _safe_float(data.get("stablecoin_market_cap")),
                "total_cryptocurrencies": _safe_int(data.get("total_cryptocurrencies")),
                "status": "ok",
            }
    except Exception:
        pass
    
    # 2. 降级 CoinGecko
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/global",
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json().get("data", {})
        mcap = data.get("total_market_cap", {})
        return {
            "total_market_cap": _safe_float(mcap.get("usd")),
            "total_volume_24h": _safe_float(data.get("total_volume", {}).get("usd")),
            "btc_dominance": _safe_float(data.get("market_cap_percentage", {}).get("btc")),
            "eth_dominance": _safe_float(data.get("market_cap_percentage", {}).get("eth")),
            "stablecoin_market_cap": 0,
            "total_cryptocurrencies": _safe_int(data.get("active_cryptocurrencies")),
            "status": "ok",
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def fetch_cmc_fear_greed() -> dict:
    """获取恐贪指数。优先 CMC，失败时降级 alternative.me。返回 {value, value_classification, ...}。"""
    # 1. 尝试 CMC
    try:
        r = requests.get(
            f"{CMC_BASE}/trial-pro-api/v3/fear-and-greed",
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json().get("data", {})
        value = _safe_float(data.get("value"))
        if value > 0:
            return {
                "value": value,
                "value_classification": data.get("value_classification", ""),
                "status": "ok",
            }
    except Exception:
        pass
    
    # 2. 降级 alternative.me
    try:
        r = requests.get(
            "https://api.alternative.me/fng/?limit=1",
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json().get("data", [{}])[0]
        return {
            "value": _safe_float(data.get("value")),
            "value_classification": data.get("value_classification", ""),
            "status": "ok",
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def fetch_cmc_altcoin_season() -> dict:
    """获取山寨季指数。优先 CMC，失败时降级 blockchaincenter.net。返回 {value, status}。"""
    # 1. 尝试 CMC
    try:
        r = requests.get(
            f"{CMC_BASE}/trial-pro-api/v1/altcoin-season-index",
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json().get("data", {})
        value = _safe_float(data.get("value"))
        if value > 0:
            return {
                "value": value,
                "status": "ok",
            }
    except Exception:
        pass
    
    # 2. 降级 blockchaincenter.net（提取页面中的指数值）
    try:
        r = requests.get(
            "https://www.blockchaincenter.net/altcoin-season-index/",
            timeout=TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        r.raise_for_status()
        import re
        match = re.search(r'Altcoin Season Index.*?(\d+)', r.text[:5000])
        if match:
            return {
                "value": _safe_float(match.group(1)),
                "status": "ok",
            }
    except Exception:
        pass
    
    return {"status": "error", "error": "All sources failed"}


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


# ══════════════════════════════════════════════════════════════
# P2-1 历史分位：拉取各指标历史序列
# ══════════════════════════════════════════════════════════════

def fetch_fear_greed_history(days: int = 90) -> dict:
    """CMC 恐贪指数历史序列（日频）。返回 {status, series: [value, ...]}。"""
    try:
        r = requests.get(
            f"{CMC_BASE}/trial-pro-api/v3/fear-and-greed",
            params={"limit": days},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json().get("data", [])
        series = [_safe_float(item.get("value")) for item in data if item.get("value") is not None]
        if not series:
            return {"status": "error", "error": "empty", "series": []}
        return {"status": "ok", "series": series}
    except Exception as e:
        return {"status": "error", "error": str(e), "series": []}


def fetch_mvrv_history(asset: str = "btc") -> dict:
    """MVRV 最新值 + 全历史分位。从 biz.cm_asset_onchain_daily 读取。
    返回 {status, value, pct_full, extreme, asset}。"""
    try:
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection

        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
                # 查最新 MVRV 值 + 全历史分位
                cur.execute("""
                    SELECT d.cap_mvrv_cur, p.mvrv_pct_full,
                           CASE
                               WHEN p.mvrv_pct_full > 90 THEN 'HIGH'
                               WHEN p.mvrv_pct_full < 10 THEN 'LOW'
                               ELSE 'NONE'
                           END AS extreme
                    FROM biz.cm_asset_onchain_daily d
                    JOIN biz.cm_onchain_percentile_full p
                        ON d.asset_id = p.asset_id AND d.metric_date = p.metric_date
                    JOIN core.asset_source_map asm
                        ON asm.source_code = 'cm' AND asm.source_asset_key = d.cm_symbol
                    WHERE d.cm_symbol = %s
                    ORDER BY d.metric_date DESC
                    LIMIT 1
                """, (asset,))
                row = cur.fetchone()

                if not row or row[0] is None:
                    return {"status": "error", "error": "no data", "asset": asset}

                mvrv_value = _safe_float(row[0])
                pct_full = _safe_float(row[1]) if row[1] is not None else None
                extreme = row[2] or "NONE"

                return {
                    "status": "ok",
                    "value": mvrv_value,
                    "pct_full": pct_full,
                    "extreme": extreme,
                    "asset": asset,
                }
    except Exception as e:
        return {"status": "error", "error": str(e), "asset": asset}


def fetch_stablecoin_netflow_history(days: int = 30) -> dict:
    """稳定币净流入历史序列（日频）。返回 {status, series, rolling_7d, dates, total_supply, anomaly}。

    series/rolling_7d：日净流入 / 7d 滚动累计净流入。
    dates：对应日期（YYYY-MM-DD）。
    total_supply：总供给序列（用于画分位折线）。
    anomaly：异动信号 {type, strength, message}。
    """
    try:
        r = requests.get("https://stablecoins.llama.fi/stablecoincharts/All", timeout=TIMEOUT)
        r.raise_for_status()
        rows = r.json()
        dates = []
        supplies = []
        for row in rows:
            usd = (row.get("totalCirculating") or {}).get("peggedUSD")
            ts = row.get("date")
            if usd is not None and ts is not None:
                supplies.append(_safe_float(usd))
                # ts 是 unix timestamp（秒）
                from datetime import datetime, timezone
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                dates.append(dt.strftime("%Y-%m-%d"))
        if len(supplies) < 2:
            return {"status": "error", "error": "insufficient", "series": [], "rolling_7d": [],
                    "dates": [], "total_supply": [], "anomaly": None}
        # 计算日净流入
        netflows = [supplies[i] - supplies[i - 1] for i in range(1, len(supplies))]
        flow_dates = dates[1:]  # 净流对应后一天
        # 7d 滚动累计，与前端 7d 视角对齐
        rolling_7d = [sum(netflows[max(0, i - 6):i + 1]) for i in range(len(netflows))]
        # 异动检测
        anomaly = _detect_stablecoin_anomaly(netflows, rolling_7d, supplies)
        return {
            "status": "ok",
            "series": netflows[-days:],
            "rolling_7d": rolling_7d[-days:],
            "dates": flow_dates[-days:],
            "total_supply": supplies[-days:],
            "supply_dates": dates[-days:],
            "anomaly": anomaly,
        }
    except Exception as e:
        return {"status": "error", "error": str(e), "series": [], "rolling_7d": [],
                "dates": [], "total_supply": [], "anomaly": None}


def _detect_stablecoin_anomaly(netflows: list[float], rolling_7d: list[float],
                                supplies: list[float]) -> dict | None:
    """
    稳定币异动检测：
    - 单日巨量流入/流出（> 30d 均值的 3σ 或 > 50 亿美金）
    - 7d 滚动趋势反转（连续 3 天方向变化）
    - 供给突破 N 日新高/新低
    返回 {type, direction, strength, message, metric} 或 None。
    """
    if len(netflows) < 14 or len(rolling_7d) < 7:
        return None
    import statistics
    # ── 1. 单日巨量异动 ──
    latest = netflows[-1]
    abs_latest = abs(latest)
    recent_30d = netflows[-30:] if len(netflows) >= 30 else netflows
    mean_abs = statistics.mean(abs(x) for x in recent_30d)
    stdev_abs = statistics.stdev(abs(x) for x in recent_30d) if len(recent_30d) >= 2 else 0
    z_score = (abs_latest - mean_abs) / stdev_abs if stdev_abs > 0 else 0

    if abs_latest > 5_000_000_000 or z_score > 2.5:
        direction = "inflow" if latest > 0 else "outflow"
        strength = "high" if abs_latest > 10_000_000_000 or z_score > 3 else "medium"
        label = "巨量流入" if latest > 0 else "巨量流出"
        return {
            "type": "daily_surge",
            "direction": direction,
            "strength": strength,
            "label": label,
            "message": f"单日净{'流入' if latest > 0 else '流出'} ${latest / 1e9:+.1f}B，{'超过阈值' if z_score > 2.5 else '规模显著'}",
            "metric": round(latest, 0),
            "z_score": round(z_score, 2),
        }

    # ── 2. 7d 趋势反转 ──
    if len(rolling_7d) >= 10:
        # 前 7 天 vs 后 3 天的方向是否反转
        prev_avg = sum(rolling_7d[-10:-3]) / 7
        last_3_avg = sum(rolling_7d[-3:]) / 3
        if prev_avg != 0 and last_3_avg != 0:
            if prev_avg > 0 and last_3_avg < 0 and abs(last_3_avg) > abs(prev_avg) * 0.5:
                return {
                    "type": "trend_reversal",
                    "direction": "outflow",
                    "strength": "medium",
                    "label": "趋势转流出",
                    "message": f"7d 滚动净流由 ${prev_avg/1e9:+.1f}B 转负 ${last_3_avg/1e9:+.1f}B",
                    "metric": round(last_3_avg, 0),
                }
            if prev_avg < 0 and last_3_avg > 0 and abs(last_3_avg) > abs(prev_avg) * 0.5:
                return {
                    "type": "trend_reversal",
                    "direction": "inflow",
                    "strength": "medium",
                    "label": "趋势转流入",
                    "message": f"7d 滚动净流由 ${prev_avg/1e9:+.1f}B 转正 ${last_3_avg/1e9:+.1f}B",
                    "metric": round(last_3_avg, 0),
                }

    # ── 3. 供给创 30d 新高/新低 ──
    if len(supplies) >= 30:
        latest_supply = supplies[-1]
        prev_30d = supplies[-30:-1]
        if latest_supply > max(prev_30d):
            return {
                "type": "supply_extreme",
                "direction": "inflow",
                "strength": "low",
                "label": "供给创新高",
                "message": f"稳定币总供给 ${latest_supply/1e9:.0f}B，创 30 日新高",
                "metric": round(latest_supply, 0),
            }
        if latest_supply < min(prev_30d):
            return {
                "type": "supply_extreme",
                "direction": "outflow",
                "strength": "low",
                "label": "供给创新低",
                "message": f"稳定币总供给 ${latest_supply/1e9:.0f}B，创 30 日新低",
                "metric": round(latest_supply, 0),
            }

    return None


def fetch_cefi_history(days: int = 30) -> dict:
    """CEFI 指数历史序列（日频）。返回 {status, series: [value, ...]}。"""
    api_key = os.environ.get("CRYPTOETF_KEY", "")
    if not api_key:
        return {"status": "skipped", "error": "CRYPTOETF_KEY 未设置", "series": []}
    try:
        r = requests.get(
            f"{CRYPTOETF_BASE}/v1/index/cefi/history",
            headers={"Authorization": f"Bearer {api_key}"},
            params={"days": days},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json().get("data", [])
        series = [_safe_float(item.get("value")) for item in data if item.get("value") is not None]
        if not series:
            return {"status": "error", "error": "empty", "series": []}
        return {"status": "ok", "series": series}
    except Exception as e:
        return {"status": "error", "error": str(e), "series": []}


def fetch_btc_dominance_history(days: int = 30) -> dict:
    """BTC 占比历史序列（日频）。返回 {status, series: [btc_dominance, ...]}。
    注意：CMC trial API 不提供 dominance 历史端点，此函数返回 error 状态。
    需要 CoinMetrics CapBTC.DOM 或其他历史源才能计算百分位。"""
    return {"status": "error", "error": "CMC trial API 无 dominance 历史端点", "series": []}


def fetch_binance_btc_klines() -> dict:
    """获取 BTC 日线 K 线（90 天），计算技术指标。返回 {rsi, ma20, ma50, price, closes, ...}。"""
    try:
        r = requests.get(
            f"{BINANCE_BASE}/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "1d", "limit": 90},
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
            "closes": closes,
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


def fetch_onchain_anomaly_signals() -> dict:
    """链上异动信号：从 db_stats.get_global_cex_netflow 拉取全局 CEX 净流量。

    返回 {exchange_netflow: {status, netflow_7d_usd}, cefi_score, status}。
    - exchange_netflow: 供 score_opportunities 消费（P1-3 交易所净流）
    - cefi_score: 0-100 归一化分值，供 compute_emotion_subscore 消费
    - 归一化算法：30d 滚动 Z-score → 映射到 0-100
    """
    try:
        from db_stats import get_global_cex_netflow, get_db
        import psycopg
        import psycopg.rows
        import statistics

        # 7d 净流量（主力窗口）
        net_7d = get_global_cex_netflow(hours=7 * 24)
        if not net_7d.get("ok") or not net_7d.get("has_data"):
            return {
                "status": "error",
                "error": net_7d.get("error", "no onchain data"),
                "exchange_netflow": {"status": "error"},
                "cefi_score": None,
            }

        netflow_7d = net_7d["netflow_usd"]

        # 30d 历史净流量序列（用于 Z-score 归一化）
        with get_db() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute("""
                    SELECT
                        date_trunc('day', block_timestamp)::date AS day,
                        SUM(CASE WHEN from_label = 'exchange' THEN value_usd ELSE 0 END)
                          - SUM(CASE WHEN to_label = 'exchange' THEN value_usd ELSE 0 END) AS netflow
                    FROM biz.onchain_transfer_log
                    WHERE block_timestamp >= NOW() - INTERVAL '30 days'
                      AND value_usd IS NOT NULL
                      AND (is_suspect IS NOT TRUE OR is_suspect IS NULL)
                      AND (from_label = 'exchange' OR to_label = 'exchange')
                    GROUP BY 1
                    ORDER BY 1
                """)
                daily_rows = cur.fetchall()

        daily_netflows = [float(r["netflow"]) for r in daily_rows if r["netflow"] is not None]

        # 归一化：Z-score → 0-100
        cefi_score = None
        if len(daily_netflows) >= 5:
            mu = statistics.mean(daily_netflows)
            sigma = statistics.stdev(daily_netflows)
            if sigma > 0:
                z = ((netflow_7d / 7) - mu) / sigma  # 7d 日均对齐单日量纲
                # Z-score → 0-100：Z=0 → 50；Z=+2 → ~84；Z=-2 → ~16
                cefi_score = max(0, min(100, round(50 + z * 17, 1)))
            else:
                cefi_score = 50  # 无波动 → 中性

        return {
            "status": "ok",
            "exchange_netflow": {
                "status": "ok",
                "netflow_7d_usd": round(netflow_7d, 2),
                "inflow_7d_usd": net_7d["inflow_usd"],
                "outflow_7d_usd": net_7d["outflow_usd"],
                "covered_transfers": net_7d["covered_transfers"],
                "by_exchange": net_7d.get("by_exchange", []),
                "cm_benchmark": net_7d.get("cm_benchmark", {}),
            },
            "cefi_score": cefi_score,
            "daily_netflows_30d": daily_netflows,
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "exchange_netflow": {"status": "error"},
            "cefi_score": None,
        }


def fetch_btc_onchain_signals() -> dict:
    """BTC 链上积累/分配信号：从 biz.cm_asset_onchain_daily 读取 CM Community 免费档指标。

    返回 {exchange_netflow, exchange_balance, hashrate, active_addresses,
           roi_1yr, roi_30d, status}。
    - exchange_netflow: FlowOutExUSD - FlowInExUSD（正=净流出=积累）
    - exchange_balance: SplyExUSD 趋势（30d 斜率）
    - hashrate: HashRate 趋势（30d 变化率）
    - active_addresses: AdrActCnt 趋势（30d 变化率）
    - roi_1yr/roi_30d: 收益率（用于 conviction 调制）
    """
    try:
        from db_stats import get_db

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    WITH btc_daily AS (
                        SELECT metric_date,
                               flow_in_ex_usd, flow_out_ex_usd,
                               sply_ex_usd, hash_rate, adr_act_cnt,
                               roi_1yr, roi_30d, price_usd
                        FROM biz.cm_asset_onchain_daily
                        WHERE cm_symbol = 'btc'
                          AND metric_date >= (SELECT MAX(metric_date) FROM biz.cm_asset_onchain_daily) - INTERVAL '35 days'
                        ORDER BY metric_date
                    )
                    SELECT
                        -- 最新日交易所净流（FlowOut - FlowIn；正=净流出=积累）
                        (SELECT flow_out_ex_usd - flow_in_ex_usd FROM btc_daily
                         WHERE flow_in_ex_usd IS NOT NULL AND flow_out_ex_usd IS NOT NULL
                         ORDER BY metric_date DESC LIMIT 1) AS exchange_netflow_1d,
                        -- 7d 累计净流（子查询包裹避免 PG GROUP BY 问题）
                        (SELECT SUM(net) FROM (
                            SELECT flow_out_ex_usd - flow_in_ex_usd AS net FROM btc_daily
                            WHERE flow_in_ex_usd IS NOT NULL AND flow_out_ex_usd IS NOT NULL
                            ORDER BY metric_date DESC LIMIT 7
                        ) _s) AS exchange_netflow_7d,
                        -- 交易所余额趋势：最新 vs 30d 前
                        (SELECT sply_ex_usd FROM btc_daily
                         WHERE sply_ex_usd IS NOT NULL
                         ORDER BY metric_date DESC LIMIT 1) AS exchange_balance_latest,
                        (SELECT sply_ex_usd FROM btc_daily
                         WHERE sply_ex_usd IS NOT NULL
                         ORDER BY metric_date DESC LIMIT 1 OFFSET 29) AS exchange_balance_30d_ago,
                        -- HashRate 趋势：最新 vs 30d 前
                        (SELECT hash_rate FROM btc_daily
                         WHERE hash_rate IS NOT NULL
                         ORDER BY metric_date DESC LIMIT 1) AS hashrate_latest,
                        (SELECT hash_rate FROM btc_daily
                         WHERE hash_rate IS NOT NULL
                         ORDER BY metric_date DESC LIMIT 1 OFFSET 29) AS hashrate_30d_ago,
                        -- 活跃地址趋势：最新 vs 30d 前
                        (SELECT adr_act_cnt FROM btc_daily
                         WHERE adr_act_cnt IS NOT NULL
                         ORDER BY metric_date DESC LIMIT 1) AS adr_latest,
                        (SELECT adr_act_cnt FROM btc_daily
                         WHERE adr_act_cnt IS NOT NULL
                         ORDER BY metric_date DESC LIMIT 1 OFFSET 29) AS adr_30d_ago,
                        -- ROI
                        (SELECT roi_1yr FROM btc_daily
                         WHERE roi_1yr IS NOT NULL
                         ORDER BY metric_date DESC LIMIT 1) AS roi_1yr,
                        (SELECT roi_30d FROM btc_daily
                         WHERE roi_30d IS NOT NULL
                         ORDER BY metric_date DESC LIMIT 1) AS roi_30d
                """)
                row = cur.fetchone()

        if not row:
            return {"status": "error", "error": "no BTC onchain data"}

        # 解包（psycopg TupleCursor）
        (netflow_1d, netflow_7d, ex_bal_latest, ex_bal_30d,
         hr_latest, hr_30d, adr_latest, adr_30d,
         roi_1yr, roi_30d) = row

        # 趋势计算
        ex_bal_trend = None
        if ex_bal_latest is not None and ex_bal_30d and ex_bal_30d > 0:
            ex_bal_trend = (ex_bal_latest - ex_bal_30d) / ex_bal_30d * 100

        hr_trend = None
        if hr_latest is not None and hr_30d and hr_30d > 0:
            hr_trend = (hr_latest - hr_30d) / hr_30d * 100

        adr_trend = None
        if adr_latest is not None and adr_30d and adr_30d > 0:
            adr_trend = (adr_latest - adr_30d) / adr_30d * 100

        return {
            "status": "ok",
            "exchange_netflow_1d": float(netflow_1d) if netflow_1d is not None else None,
            "exchange_netflow_7d": float(netflow_7d) if netflow_7d is not None else None,
            "exchange_balance_latest": float(ex_bal_latest) if ex_bal_latest is not None else None,
            "exchange_balance_trend_pct": round(ex_bal_trend, 2) if ex_bal_trend is not None else None,
            "hashrate_latest": float(hr_latest) if hr_latest is not None else None,
            "hashrate_trend_pct": round(hr_trend, 2) if hr_trend is not None else None,
            "active_addresses_latest": int(adr_latest) if adr_latest is not None else None,
            "active_addresses_trend_pct": round(adr_trend, 2) if adr_trend is not None else None,
            "roi_1yr": float(roi_1yr) if roi_1yr is not None else None,
            "roi_30d": float(roi_30d) if roi_30d is not None else None,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def fetch_cm_activity_signals() -> dict:
    """CM 多资产链上活跃/采用信号（网络健康/采用背离）。

    读取 biz.cm_asset_onchain_daily + cm_onchain_percentile_full 的活跃地址分位，
    用于机会引擎「静默积累/采用增长」与「网络衰退」判定。

    - SOL 免费档 403，排除。
    - adr_pct（活跃地址历史分位）+ roi_30d（近30日收益）构造背离：
      活跃地址高位 + 价格未跟上（roi30d 低/负）= 静默积累/采用增长（long）
      活跃地址崩盘（adr_pct < 10）= 网络衰退（watch）

    返回 {status, coins: [{symbol, adr_pct, tx_pct, roi_30d, roi_1yr, signal}], metric_date}。
    """
    try:
        from db_stats import get_cm_activity_dashboard

        data = get_cm_activity_dashboard()
        if not data.get("ok"):
            return {"status": "error", "error": "no cm activity data", "coins": []}

        coins = []
        for a in data.get("cm_activity") or []:
            symbol = (a.get("symbol") or "").upper()
            # SOL 免费档 403，明确排除
            if symbol == "SOL":
                continue
            adr_pct = a.get("adr_pct")
            tx_pct = a.get("tx_pct")
            roi_30d = a.get("roi_30d")
            roi_1yr = a.get("roi_1yr")
            roi_30d_pct = a.get("roi_30d_pct")

            signal = "neutral"
            if adr_pct is not None:
                # 活跃地址高位 + 30d 收益分位偏低 → 采用增长但价格未跟上（静默积累）
                if adr_pct >= 70 and roi_30d_pct is not None and roi_30d_pct <= 45:
                    signal = "accumulation"  # 静默积累 / 采用增长
                elif adr_pct < 10:
                    signal = "decline"       # 网络衰退
                elif roi_30d is not None and roi_30d < 0:
                    signal = "weak"          # 活跃尚可但短期承压

            coins.append({
                "symbol": symbol,
                "adr_pct": round(adr_pct, 1) if adr_pct is not None else None,
                "tx_pct": round(tx_pct, 1) if tx_pct is not None else None,
                "roi_30d": round(roi_30d, 2) if roi_30d is not None else None,
                "roi_1yr": round(roi_1yr, 2) if roi_1yr is not None else None,
                "signal": signal,
            })

        return {
            "status": "ok",
            "coins": coins,
            "metric_date": data.get("metric_date"),
        }
    except Exception as e:
        return {"status": "error", "error": str(e), "coins": []}


def fetch_btc_netflow_7d() -> float | None:
    """从 CM 落库表取 BTC 近 7d 交易所净流(USD)。None=缺失(降级)。

    净流 = FlowOutExUSD - FlowInExUSD（正=净流出=积累，负=净流入=抛压）。
    以库内最新完整日为基准回看 7 天，避免 CURRENT_DATE 与数据滞后错位。
    """
    try:
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection

        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT SUM(flow_out_ex_usd - flow_in_ex_usd)
                    FROM biz.cm_asset_onchain_daily
                    WHERE cm_symbol = 'btc'
                      AND flow_in_ex_usd IS NOT NULL
                      AND flow_out_ex_usd IS NOT NULL
                      AND metric_date > (
                          SELECT MAX(metric_date) FROM biz.cm_asset_onchain_daily
                          WHERE cm_symbol = 'btc'
                      ) - INTERVAL '7 days'
                """)
                row = cur.fetchone()
        if row and row[0] is not None:
            return float(row[0])
        return None
    except Exception:
        return None


def fetch_binance_etf_flows() -> dict:
    """获取 cryptoetf ETF 资金流数据。返回 {net_flow_usd_m, ...}。

    优先从数据库 biz.etf_flow_daily 读取（更快、可离线）；
    数据库无数据或数据过旧时，回退到 API 拉取并回写数据库。
    """
    api_key = os.environ.get("CRYPTOETF_KEY", "")

    # ── 1. 先查数据库 ──────────────────────────────────────
    try:
        import psycopg.rows
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection
        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            conn.row_factory = psycopg.rows.dict_row
            with conn.cursor() as cur:
                # 每个币种取最近一个净流入非 0 的交易日（跳过休市日）
                cur.execute("""
                    SELECT DISTINCT ON (symbol)
                           symbol, flow_date, net_flow_usd_m, net_flow_usd, aum_usd
                    FROM biz.etf_flow_daily
                    WHERE source_code = 'cryptoetf'
                      AND net_flow_usd_m IS NOT NULL
                      AND net_flow_usd_m != 0
                    ORDER BY symbol, flow_date DESC
                """)
                rows = cur.fetchall()
                if rows:
                    total = 0.0
                    btc_flow = None
                    assets_detail = []
                    # 取有数据的币种中最新的那个日期作为 as_of_date
                    latest_date = max(r["flow_date"] for r in rows)
                    for r in rows:
                        v = float(r["net_flow_usd_m"]) if r["net_flow_usd_m"] is not None else None
                        if v is not None:
                            total += v
                        sym = r["symbol"]
                        if sym == "BTC":
                            btc_flow = v
                        assets_detail.append({
                            "symbol": sym,
                            "asset": sym.lower(),
                            "net_flow_usd_m": v,
                            "date": str(r["flow_date"]),
                        })
                    # 按净流入绝对值降序排列，与 API 返回顺序一致
                    assets_detail.sort(key=lambda x: abs(x.get("net_flow_usd_m") or 0), reverse=True)
                    return {
                        "net_flow_usd_m": round(total, 2),
                        "btc_net_flow_usd_m": btc_flow,
                        "as_of_date": str(latest_date),
                        "assets_detail": assets_detail,
                        "status": "ok",
                        "source": "db",
                    }
    except Exception:
        # 数据库不可用或表不存在，静默回退到 API
        pass

    # ── 2. 回退到 API 拉取 ─────────────────────────────────
    if not api_key:
        return {"status": "skipped", "error": "CRYPTOETF_KEY 未设置"}
    try:
        r = requests.get(
            f"{CRYPTOETF_BASE}/v1/flows/summary",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        payload = r.json()
        assets = payload.get("assets") or []
        if not assets:
            return {"status": "partial", "error": "empty assets", "net_flow_usd_m": None}
        total = 0.0
        btc_flow = None
        assets_detail = []  # 全币种明细，按净流入绝对值降序
        for a in assets:
            v = _safe_float(a.get("netFlowUsdM"))
            if v is not None:
                total += v
            sym = a.get("symbol", "")
            if sym == "BTC":
                btc_flow = v
            assets_detail.append({
                "symbol": sym,
                "asset": a.get("asset", ""),
                "net_flow_usd_m": v,
                "date": a.get("date", ""),
            })
        # 按净流入绝对值降序排列，方便展示
        assets_detail.sort(key=lambda x: abs(x.get("net_flow_usd_m") or 0), reverse=True)
        result = {
            "net_flow_usd_m": round(total, 2),
            "btc_net_flow_usd_m": btc_flow,
            "as_of_date": assets[0].get("date"),
            "assets_detail": assets_detail,
            "status": "ok",
            "source": "api",
        }

        # ── 3. 回写数据库 ──────────────────────────────────
        try:
            import psycopg.rows
            from crypto_research.config import get_settings
            from crypto_research.db.conn import get_connection
            settings = get_settings(require_database=True)
            with get_connection(settings.database_url) as conn:
                with conn.cursor() as cur:
                    # 确保表存在
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS biz.etf_flow_daily (
                            symbol            TEXT        NOT NULL,
                            flow_date         DATE        NOT NULL,
                            net_flow_usd      NUMERIC(20,2),
                            net_flow_usd_m    NUMERIC(12,2),
                            aum_usd           NUMERIC(20,2),
                            total_inflow_usd  NUMERIC(20,2),
                            total_outflow_usd NUMERIC(20,2),
                            source_code       TEXT        NOT NULL DEFAULT 'cryptoetf',
                            fetched_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            PRIMARY KEY (symbol, flow_date, source_code)
                        )
                    """)
                    # 回写每条数据
                    for a in assets_detail:
                        if not a["date"] or not a["symbol"]:
                            continue
                        net_usd = round(a["net_flow_usd_m"] * 1_000_000, 2) if a["net_flow_usd_m"] is not None else None
                        cur.execute("""
                            INSERT INTO biz.etf_flow_daily
                                (symbol, flow_date, net_flow_usd, net_flow_usd_m,
                                 source_code, fetched_at, updated_at)
                            VALUES (%s, %s, %s, %s, 'cryptoetf', NOW(), NOW())
                            ON CONFLICT (symbol, flow_date, source_code) DO UPDATE SET
                                net_flow_usd = EXCLUDED.net_flow_usd,
                                net_flow_usd_m = EXCLUDED.net_flow_usd_m,
                                updated_at = NOW()
                        """, (a["symbol"], a["date"], net_usd, a["net_flow_usd_m"]))
                conn.commit()
        except Exception:
            # 回写失败不影响主流程
            pass

        return result
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response else None
        return {"status": "error", "error": f"HTTP {code}"}
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
            quote = (cat.get("quote") or {}).get("USD", {})
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


# ══════════════════════════════════════════════════════════════
# P1-1 板块/链资金净流入（7d 视角）
# ══════════════════════════════════════════════════════════════

def _fetch_narratives_from_db() -> tuple[dict[str, dict], list[str]]:
    """
    从 biz.sector_narrative_asset 读最新一天的叙事成分币全量数据。
    返回 (narrative_data, matched_narratives)
      narrative_data: {narrative: {"market_cap": float, "top_coins_all": [...]}}
      top_coins_all 元素: {asset_id, symbol, name, market_cap, percent_change_24h, weight_pct, rank_in_category}
    """
    result: dict[str, dict] = {}
    matched: list[str] = []
    try:
        from db_stats import get_db
        with get_db() as conn:
            with conn.cursor() as cur:
                # 取最新有数据的日期
                cur.execute("SELECT MAX(as_of_date) FROM biz.sector_narrative_asset")
                row = cur.fetchone()
                if not row or not row[0]:
                    return {}, []
                latest_date = row[0]

                # 一次性拉取所有叙事的成分币
                cur.execute("""
                    SELECT
                        narrative,
                        cmc_category_name,
                        asset_id,
                        symbol,
                        name,
                        market_cap,
                        percent_change_24h,
                        weight_pct,
                        rank_in_category
                    FROM biz.sector_narrative_asset
                    WHERE as_of_date = %s
                    ORDER BY narrative, market_cap DESC NULLS LAST
                """, (latest_date,))
                rows = cur.fetchall()

        cur_narr = None
        cur_mcap = 0.0
        cur_coins: list[dict] = []
        cur_cat_name = ""

        for r in rows:
            narr = r[0]
            if narr != cur_narr:
                if cur_narr is not None:
                    result[cur_narr] = {
                        "cmc_category": cur_cat_name,
                        "market_cap": cur_mcap,
                        "top_coins_all": cur_coins,
                    }
                cur_narr = narr
                cur_cat_name = r[1]
                cur_mcap = 0.0
                cur_coins = []
            mcap = float(r[5]) if r[5] else 0.0
            cur_mcap += mcap
            cur_coins.append({
                "asset_id": int(r[2]) if r[2] else None,
                "symbol": r[3] or "",
                "name": r[4] or "",
                "market_cap": mcap,
                "percent_change_24h": float(r[6]) if r[6] is not None else None,
                "weight_pct": float(r[7]) if r[7] is not None else None,
                "rank_in_category": int(r[8]) if r[8] else None,
            })
        if cur_narr is not None:
            result[cur_narr] = {
                "cmc_category": cur_cat_name,
                "market_cap": cur_mcap,
                "top_coins_all": cur_coins,
            }
        matched = list(result.keys())
        return result, matched
    except Exception:
        return {}, []


def _cmc_category_multi_window(category_id: str) -> dict:
    """
    通过 CMC category detail 聚合成分币的多窗口市值变化%。
    返回 {
        "change_1d": float|None, "change_7d": float|None, "change_30d": float|None,
        "used": int, "total": int, "top_coins": list
    }
    """
    result = {"change_1d": None, "change_7d": None, "change_30d": None,
              "used": 0, "total": 0, "top_coins": []}
    try:
        r = requests.get(
            f"{CMC_BASE}/trial-pro-api/v1/cryptocurrency/category",
            params={"id": category_id},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json().get("data", {})
        coins = data.get("coins", []) or []
        result["total"] = len(coins)

        # 三个窗口：1d/7d/30d，每个窗口独立聚合
        cur = {"1d": 0.0, "7d": 0.0, "30d": 0.0}
        prev = {"1d": 0.0, "7d": 0.0, "30d": 0.0}
        used = {"1d": 0, "7d": 0, "30d": 0}
        min_used_ratio = 0.5  # 至少一半成分币有数据才信任

        for c in coins:
            q = (c.get("quote") or {}).get("USD") or {}
            mcap = q.get("market_cap")
            if mcap is None:
                continue
            mcapf = _safe_float(mcap)
            if mcapf <= 0:
                continue

            for window, key in [("1d", "percent_change_24h"), ("7d", "percent_change_7d"), ("30d", "percent_change_30d")]:
                p = q.get(key)
                if p is None:
                    continue
                pf = _safe_float(p)
                if pf <= -100:
                    continue  # 价格归零
                cur[window] += mcapf
                prev[window] += mcapf / (1 + pf / 100)
                used[window] += 1

        for w in ["1d", "7d", "30d"]:
            if prev[w] > 0 and used[w] >= max(1, int(len(coins) * min_used_ratio)):
                change = (cur[w] - prev[w]) / prev[w] * 100
                result[f"change_{w}"] = round(change, 2)
                result["used"] = max(result["used"], used[w])

        # 构造 top_coins：按市值降序取 Top 5（保留多周期变化率）
        enriched = []
        for c in coins:
            q = (c.get("quote") or {}).get("USD") or {}
            mcap = _safe_float(q.get("market_cap"))
            price = _safe_float(q.get("price"))
            p1 = q.get("percent_change_24h")
            p7 = q.get("percent_change_7d")
            p30 = q.get("percent_change_30d")
            enriched.append({
                "name": c.get("name", ""),
                "symbol": c.get("symbol", ""),
                "market_cap": mcap,
                "price": price,
                "percent_change_24h": round(_safe_float(p1), 2) if p1 is not None else None,
                "percent_change_7d": round(_safe_float(p7), 2) if p7 is not None else None,
                "percent_change_30d": round(_safe_float(p30), 2) if p30 is not None else None,
            })
        enriched.sort(key=lambda x: -x["market_cap"])
        result["top_coins"] = enriched[:5]

        return result
    except Exception:
        return result


def _calc_narrative_momentum(chg_1d: float | None, chg_7d: float | None, chg_30d: float | None,
                             weights: dict | None = None) -> float | None:
    """
    三窗动量合成评分：加权求和。
    weights: {"1d": w1, "7d": w7, "30d": w30}，默认 1d=0.2, 7d=0.5, 30d=0.3
    """
    if weights is None:
        weights = {"1d": 0.2, "7d": 0.5, "30d": 0.3}
    total_w = 0.0
    score = 0.0
    for w, val in [("1d", chg_1d), ("7d", chg_7d), ("30d", chg_30d)]:
        if val is not None:
            score += weights.get(w, 0) * val
            total_w += weights.get(w, 0)
    if total_w == 0:
        return None
    return round(score / total_w, 2)


def _calc_trend_label(chg_1d: float | None, chg_7d: float | None, chg_30d: float | None) -> str:
    """
    趋势标签：根据三窗变化率的方向和加速度判断。
    返回: '加速上涨' | '减速上涨' | '震荡上涨' | '加速下跌' | '减速下跌' | '震荡下跌' | '横盘'
    """
    vals = {k: v for k, v in [("1d", chg_1d), ("7d", chg_7d), ("30d", chg_30d)] if v is not None}
    if not vals:
        return "横盘"

    # 7d 为主方向判断
    main = vals.get("7d", vals.get("30d", vals.get("1d", 0)))
    if abs(main) < 1.0:  # 主方向变化<1% 视为横盘
        return "横盘"

    direction = "up" if main > 0 else "down"

    # 加速度：比较 1d 变化 vs 7d 日均变化
    if "1d" in vals and "7d" in vals:
        daily_1d = vals["1d"]
        daily_7d_avg = vals["7d"] / 7
        ratio = abs(daily_1d) / abs(daily_7d_avg) if daily_7d_avg != 0 else 0
        if direction == "up":
            if daily_1d > 0 and ratio > 1.5:
                return "加速上涨"
            elif daily_1d < 0 and abs(daily_1d) > 0.5:
                return "减速上涨"
            else:
                return "震荡上涨"
        else:
            if daily_1d < 0 and ratio > 1.5:
                return "加速下跌"
            elif daily_1d > 0 and abs(daily_1d) > 0.5:
                return "减速下跌"
            else:
                return "震荡下跌"

    # 只有 7d 和 30d
    if "7d" in vals and "30d" in vals:
        weekly_7d = vals["7d"]
        weekly_30d = vals["30d"] / (30 / 7)
        if direction == "up":
            return "加速上涨" if weekly_7d > weekly_30d * 1.2 else "震荡上涨"
        else:
            return "加速下跌" if weekly_7d < weekly_30d * 1.2 else "震荡下跌"

    return "上涨" if direction == "up" else "下跌"


def fetch_category_flow() -> dict:
    """
    CMC categories 7d 市值变化%（叙事榜 mcap 腿）。
    成分币列表优先从 biz.sector_narrative_asset 读（全量 + asset_id），
    7d 变化通过 CMC category detail API 聚合。
    返回 {status, ranked: [{narrative, cmc_category, market_cap, mcap_change_7d_pct,
             mcap_period, top_coins, top_coins_all, total_coins, from_db}], degraded}。
    """
    # 先从数据库取叙事成分币（全量 + asset_id）
    db_data, db_matched = _fetch_narratives_from_db()

    # 拉取 CMC 分类列表（用于 7d 变化计算 + 数据库没有的叙事兜底）
    try:
        r = requests.get(
            f"{CMC_BASE}/trial-pro-api/v1/cryptocurrency/categories",
            params={"limit": 500},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        cats = r.json().get("data", [])
    except Exception as e:
        # API 失败但有数据库数据：降级返回 24h 视角
        if db_data:
            ranked = []
            for narr, info in db_data.items():
                coins = info["top_coins_all"]
                top5 = coins[:5] if coins else []
                ranked.append({
                    "narrative": narr,
                    "cmc_category": info["cmc_category"],
                    "market_cap": info["market_cap"],
                    "mcap_change_7d_pct": None,
                    "mcap_period": "db_only_24h",
                    "top_coins": top5,
                    "top_coins_all": coins,
                    "total_coins": len(coins),
                    "from_db": True,
                })
            ranked.sort(key=lambda x: -x["market_cap"])
            return {"status": "ok", "ranked": ranked, "degraded": [x["narrative"] for x in ranked], "note": "cmc api failed, db-only mode"}
        return {"status": "error", "error": str(e), "ranked": [], "degraded": []}

    # 构建叙事 → CMC 分类映射
    cat_map: dict[str, dict] = {}  # narrative -> cmc category info
    cat_name_map: dict[str, str] = {}  # narrative -> cmc name
    # 第一遍：精确匹配
    for c in cats:
        name = (c.get("name") or "").strip().lower()
        for w in NARRATIVE_WATCHLIST:
            if name == w.lower() and w not in cat_map:
                cat_map[w] = c
    # 第二遍：前缀匹配兜底
    for c in cats:
        name = (c.get("name") or "").strip().lower()
        for w in NARRATIVE_WATCHLIST:
            if w not in cat_map and name.startswith(w.lower()):
                cat_map[w] = c
    # 第三遍：用数据库里的 cmc_category_name 反查（兜底名称不匹配的）
    for narr, info in db_data.items():
        if narr not in cat_map:
            db_name = info["cmc_category"]
            for c in cats:
                if (c.get("name") or "").strip() == db_name:
                    cat_map[narr] = c
                    break

    if not cat_map:
        return {"status": "ok", "ranked": [], "degraded": [], "note": "no watchlist category matched"}

    # 按市值取 top N
    selected = sorted(cat_map.items(), key=lambda x: -_safe_float(x[1].get("market_cap")))[:NARRATIVE_TOP_N]

    def work(item: tuple[str, dict]) -> dict:
        w, c = item
        # 多窗口变化率用 CMC detail API 计算
        mw = _cmc_category_multi_window(c["id"])
        change7d = mw["change_7d"]
        change1d = mw["change_1d"]
        change30d = mw["change_30d"]
        period = "7d" if change7d is not None else "24h_fallback"
        if change7d is None:
            change7d = c.get("market_cap_change")

        # 三窗动量评分 + 趋势标签
        momentum = _calc_narrative_momentum(change1d, change7d, change30d)
        trend_label = _calc_trend_label(change1d, change7d, change30d)

        # 成分币：优先数据库全量（带 asset_id），兜底 API top5
        db_info = db_data.get(w)
        if db_info and db_info.get("top_coins_all"):
            all_coins = db_info["top_coins_all"]
            top5 = all_coins[:5]
            mcap = db_info["market_cap"] or _safe_float(c.get("market_cap"))
            from_db = True
        else:
            all_coins = mw["top_coins"]
            top5 = mw["top_coins"]
            mcap = _safe_float(c.get("market_cap"))
            from_db = False

        return {
            "narrative": w,
            "cmc_category": c.get("name"),
            "market_cap": mcap,
            "mcap_change_1d_pct": round(change1d, 2) if change1d is not None else None,
            "mcap_change_7d_pct": round(change7d, 2) if change7d is not None else None,
            "mcap_change_30d_pct": round(change30d, 2) if change30d is not None else None,
            "momentum_score": momentum,
            "trend_label": trend_label,
            "mcap_period": period,
            "top_coins": top5,
            "top_coins_all": all_coins,
            "total_coins": len(all_coins),
            "from_db": from_db,
        }

    with ThreadPoolExecutor(max_workers=6) as ex:
        ranked = list(ex.map(work, selected))

    degraded = [item["narrative"] for item in ranked if item["mcap_period"] != "7d"]

    return {"status": "ok", "ranked": ranked, "degraded": degraded}


def fetch_category_tvl_flow() -> dict:
    """
    DeFiLlama /protocols 按 category 聚合 7d TVL 变化%（叙事榜 TVL 腿）。
    /categories 已 402 付费墙，改聚合免费 /protocols（每条含 category/tvl/change_7d）。
    返回 {status, categories: {cat: {tvl, tvl_change_7d_pct, protocols}}}。
    """
    try:
        r = requests.get(f"{DL_BASE}/protocols", timeout=TIMEOUT)
        r.raise_for_status()
        prots = r.json()
    except Exception as e:
        return {"status": "error", "error": str(e), "categories": {}}

    agg: dict[str, dict] = {}
    for p in prots:
        cat = p.get("category") or "Unknown"
        tvl = _safe_float(p.get("tvl"))
        ch7 = p.get("change_7d")
        e = agg.setdefault(cat, {"tvl": 0.0, "wtvl": 0.0, "wsum": 0.0, "n": 0})
        e["tvl"] += tvl  # 赛道总 TVL（展示用，含全部协议）
        if ch7 is not None and abs(_safe_float(ch7)) <= TVL_CHANGE_CAP:
            # 加权分母仅含正常协议：剔除 |change_7d| 天文值（低基数/历史回填），防撑爆赛道均值
            e["wtvl"] += tvl
            e["wsum"] += _safe_float(ch7) * tvl
            e["n"] += 1

    categories: dict[str, dict] = {}
    for cat, e in agg.items():
        tvl = e["tvl"]
        change = (e["wsum"] / e["wtvl"]) if e["wtvl"] > 0 else 0.0
        categories[cat] = {
            "tvl": tvl,
            "tvl_change_7d_pct": round(change, 2),
            "protocols": e["n"],
        }
    return {"status": "ok", "categories": categories}


def build_narrative_flow_ranking(cat_flow: dict, tvl_flow: dict) -> dict:
    """
    合成叙事榜：三窗动量评分（市值腿）+ TVL 变化（TVL 腿）。
    有 TVL 腿（映射命中且 TVL≥阈值）= momentum_score * mcap_weight + tvl_change * tvl_weight；
    无 TVL 腿（Meme/L1 等）= momentum_score。
    按合成值降序取前 rank_limit。返回 {status, ranked, degraded}。
    """
    tvl_cats = tvl_flow.get("categories", {}) if tvl_flow.get("status") == "ok" else {}
    mc_w = NARRATIVE_CHAIN["mcap_weight"]
    tvl_w = NARRATIVE_CHAIN["tvl_weight"]
    ranked: list[dict] = []
    for item in cat_flow.get("ranked", []):
        momentum = item.get("momentum_score")
        mcap7 = item.get("mcap_change_7d_pct")
        # 用 momentum 做主评分，没有的话兜底用 7d
        score = momentum if momentum is not None else mcap7
        if score is None:
            continue
        dl_cat = NARRATIVE_TVL_MAP.get(item["narrative"])
        tvl_info = tvl_cats.get(dl_cat) if dl_cat else None
        if tvl_info and tvl_info.get("tvl", 0) >= TVL_LEG_MIN:
            composite = mc_w * score + tvl_w * tvl_info["tvl_change_7d_pct"]
            mode = "blended"
        else:
            composite = score
            mode = "mcap_only"
        ranked.append({
            "narrative": item["narrative"],
            "composite_score": round(composite, 2),
            "mode": mode,
            "momentum_score": momentum,
            "trend_label": item.get("trend_label", "横盘"),
            "mcap_change_1d_pct": item.get("mcap_change_1d_pct"),
            "mcap_change_7d_pct": mcap7,
            "mcap_change_30d_pct": item.get("mcap_change_30d_pct"),
            "mcap_period": item.get("mcap_period"),
            "tvl_change_7d_pct": tvl_info["tvl_change_7d_pct"] if tvl_info else None,
            "tvl_usd": tvl_info["tvl"] if tvl_info else None,
            "market_cap": item.get("market_cap"),
            "top_coins": item.get("top_coins", []),
            "top_coins_all": item.get("top_coins_all", []),
            "total_coins": item.get("total_coins", 0),
            "from_db": item.get("from_db", False),
        })

    ranked.sort(key=lambda x: -x["composite_score"])
    degraded = list(cat_flow.get("degraded", []))
    status = "ok"
    if not ranked:
        status = "error"
    elif degraded or tvl_flow.get("status") != "ok":
        status = "partial"
    return {"ranked": ranked[:int(NARRATIVE_CHAIN["rank_limit"])], "status": status, "degraded": degraded}


def _chain_7d_flow(chain_ident: str) -> dict | None:
    """拉取单链日频 TVL 历史，差分最新 vs 约 7 天前，返回 {tvl, tvl_prev_week, flow_7d, flow_7d_pct}。"""
    try:
        r = requests.get(f"{DL_BASE}/v2/historicalChainTvl/{chain_ident}", timeout=TIMEOUT)
        r.raise_for_status()
        hist = r.json()
        if not isinstance(hist, list):
            return None
        pts = [p for p in hist if isinstance(p, dict) and p.get("tvl") is not None]
        if len(pts) < 2:
            return None
        latest_date = max(p["date"] for p in pts)
        latest = max(p for p in pts if p["date"] == latest_date)
        target = latest_date - 7 * 86400
        candidates = [p for p in pts if p["date"] <= target]
        prev = min(candidates, key=lambda p: target - p["date"]) if candidates else pts[0]
        tvl_now = _safe_float(latest["tvl"])
        tvl_prev = _safe_float(prev["tvl"])
        flow = tvl_now - tvl_prev
        flow_pct = (flow / tvl_prev * 100) if tvl_prev > 0 else 0.0
        return {"tvl": tvl_now, "tvl_prev_week": tvl_prev, "flow_7d": flow, "flow_7d_pct": round(flow_pct, 2)}
    except Exception:
        return None


# DeFiLlama /v2/chains name → /protocols chain 字段别名映射
# 实测 BSC/Binance、OP Mainnet/Optimism 等两侧命名不一致
DL_CHAIN_ALIAS: dict[str, str] = {
    "BSC": "Binance",
    "OP Mainnet": "Optimism",
}


def _enrich_protocols_with_asset(protocols: list[dict]) -> list[dict]:
    """
    为协议列表批量补充 asset_id / canonical_symbol，支持前端点击跳转资产详情。
    匹配策略：symbol 大写精确匹配 core.asset.canonical_symbol（验证覆盖率 99.9% @ TVL>10M）。
    返回新列表（不修改输入），每个协议新增 asset_id / canonical_symbol 字段（无匹配则为 null）。
    """
    if not protocols:
        return protocols

    symbols = [
        (p.get("symbol") or "").strip().upper()
        for p in protocols
        if p.get("symbol") and str(p.get("symbol")).strip()
    ]
    if not symbols:
        return protocols

    try:
        from db_stats import get_db
        import psycopg.rows
    except Exception:
        return protocols

    symbol_to_asset: dict[str, dict] = {}
    try:
        with get_db() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                placeholders = ",".join(["%s"] * len(symbols))
                cur.execute(
                    f"""
                    SELECT asset_id, canonical_symbol
                    FROM core.asset
                    WHERE UPPER(canonical_symbol) IN ({placeholders})
                      AND status = 'active'
                    """,
                    symbols,
                )
                for row in cur.fetchall():
                    sym = (row["canonical_symbol"] or "").upper()
                    if sym and sym not in symbol_to_asset:
                        symbol_to_asset[sym] = {
                            "asset_id": str(row["asset_id"]),
                            "canonical_symbol": row["canonical_symbol"],
                        }
    except Exception:
        # 数据库不可用时静默降级，不影响链榜展示
        return protocols

    enriched = []
    for p in protocols:
        p2 = dict(p)
        sym = (p.get("symbol") or "").strip().upper()
        hit = symbol_to_asset.get(sym)
        if hit:
            p2["asset_id"] = hit["asset_id"]
            p2["canonical_symbol"] = hit["canonical_symbol"]
        else:
            p2["asset_id"] = None
            p2["canonical_symbol"] = None
        enriched.append(p2)
    return enriched


def _fetch_narrative_flow_from_db() -> dict | None:
    """
    从 biz.sector_flow_daily 读 12 赛道数据，转成叙事榜同构格式。
    用于 API 失败时的二级兜底。返回 None 表示无数据。
    """
    try:
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection
        import psycopg.rows

        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute("""
                    SELECT sector_key, sector_label, metric_date,
                           market_cap, mcap_change_1d_pct, mcap_change_7d_pct, mcap_change_30d_pct,
                           coin_count, tvl, tvl_change_7d_pct, flow_7d_usd,
                           composite_score, mode
                    FROM biz.sector_flow_daily
                    WHERE metric_date = (
                        SELECT MAX(metric_date) FROM biz.sector_flow_daily
                    )
                      AND sector_type = 'sector_12'
                      AND market_cap IS NOT NULL
                    ORDER BY composite_score DESC NULLS LAST, mcap_change_7d_pct DESC NULLS LAST
                """)
                rows = [dict(r) for r in cur.fetchall()]
                if not rows:
                    return None

                # 取每条赛道的领涨币 TOP 3
                for s in rows:
                    cur.execute("""
                        SELECT a.canonical_symbol AS symbol, a.canonical_name AS name,
                               q.market_cap, q.percent_change_7d
                        FROM biz.asset_sector s
                        JOIN core.asset a ON s.asset_id = a.asset_id
                        JOIN core.asset_source_map asm
                          ON a.asset_id = asm.asset_id AND asm.source_code = 'cmc'
                        JOIN (
                            SELECT DISTINCT ON (cmc_id) cmc_id, market_cap,
                                   percent_change_7d
                            FROM src_cmc.cmc_asset_quote_snapshot
                            WHERE market_cap IS NOT NULL
                              AND quote_time::date = (
                                SELECT MAX(quote_time)::date FROM src_cmc.cmc_asset_quote_snapshot
                              )
                            ORDER BY cmc_id, quote_time DESC
                        ) q ON asm.source_asset_key::bigint = q.cmc_id
                        WHERE s.sector = %s AND s.is_primary = TRUE
                          AND q.market_cap >= 10000000
                        ORDER BY q.percent_change_7d DESC NULLS LAST
                        LIMIT 3
                    """, (s["sector_key"],))
                    leaders = [dict(r) for r in cur.fetchall()]
                    s["top_coins"] = [
                        {
                            "symbol": l["symbol"],
                            "name": l["name"],
                            "market_cap": float(l["market_cap"]) if l["market_cap"] else None,
                            "change_7d_pct": float(l["percent_change_7d"]) if l["percent_change_7d"] is not None else None,
                        }
                        for l in leaders
                    ]

                # 转成叙事榜同构格式
                ranked = []
                for r in rows:
                    comp = r.get("composite_score")
                    if comp is None:
                        comp = r.get("mcap_change_7d_pct") or 0

                    # 趋势标签
                    m7 = r.get("mcap_change_7d_pct") or 0
                    if m7 > 10:
                        trend = "强势上涨"
                    elif m7 > 3:
                        trend = "上涨"
                    elif m7 > -3:
                        trend = "横盘"
                    elif m7 > -10:
                        trend = "下跌"
                    else:
                        trend = "强势下跌"

                    ranked.append({
                        "narrative": r["sector_label"],
                        "narrative_key": r["sector_key"],
                        "composite_score": round(float(comp), 2) if comp is not None else 0,
                        "mode": r["mode"] or "mcap_only",
                        "momentum_score": round(float(r["mcap_change_7d_pct"]), 2) if r["mcap_change_7d_pct"] is not None else None,
                        "trend_label": trend,
                        "mcap_change_1d_pct": float(r["mcap_change_1d_pct"]) if r["mcap_change_1d_pct"] is not None else None,
                        "mcap_change_7d_pct": float(r["mcap_change_7d_pct"]) if r["mcap_change_7d_pct"] is not None else None,
                        "mcap_change_30d_pct": float(r["mcap_change_30d_pct"]) if r["mcap_change_30d_pct"] is not None else None,
                        "mcap_period": "7d",
                        "tvl_change_7d_pct": float(r["tvl_change_7d_pct"]) if r["tvl_change_7d_pct"] is not None else None,
                        "tvl_usd": float(r["tvl"]) if r["tvl"] else None,
                        "market_cap": float(r["market_cap"]) if r["market_cap"] else None,
                        "top_coins": r.get("top_coins", []),
                        "top_coins_all": r.get("top_coins", []),
                        "total_coins": r["coin_count"],
                        "flow_7d_usd": float(r["flow_7d_usd"]) if r["flow_7d_usd"] is not None else None,
                        "from_db": True,
                        "source": "db_12sector",
                    })

                return {
                    "status": "ok",
                    "ranked": ranked,
                    "degraded": [],
                    "source": "db_12sector",
                    "metric_date": str(rows[0]["metric_date"]),
                }
    except Exception:
        return None


def _fetch_chain_flow_from_db() -> dict | None:
    """
    从 src_dl.chain_tvl_snapshot 读链净流入 TOP5。
    有数据返回与 fetch_chain_flow 同结构的 dict，无数据返回 None。
    若链数不足 MIN_CHAINS 条，返回 None 触发降级到实时 API。
    """
    MIN_CHAINS = 5
    try:
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection
        import psycopg.rows

        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute("""
                    SELECT chain_key, chain_name, tvl_usd,
                           flow_7d_usd, tvl_change_7d as flow_7d_pct,
                           snapshot_date
                    FROM src_dl.chain_tvl_snapshot
                    WHERE snapshot_date = (
                        SELECT MAX(snapshot_date) FROM src_dl.chain_tvl_snapshot
                    )
                      AND flow_7d_usd IS NOT NULL
                    ORDER BY flow_7d_usd DESC
                    LIMIT 20
                """)
                rows = [dict(r) for r in cur.fetchall()]
                if not rows or len(rows) < MIN_CHAINS:
                    return None

                # 取净流入 TOP 5 + 净流出 TOP 5（共 10 条，前端按需求取）
                top_by_flow = sorted(rows, key=lambda x: -(x.get("flow_7d_usd") or 0))
                top5 = top_by_flow[:5]

                # 批量查询每条链的 TOP 5 协议（从 src_dl.protocol_list）
                chain_proto_map: dict[str, list[dict]] = {}
                chain_names = [r["chain_name"] for r in top5]
                if chain_names:
                    # 构造要查询的链名列表（包含别名映射后的名字）
                    # chain_tvl_snapshot.chain_name -> protocol_list.chain
                    name_to_proto = {
                        "BSC": "Binance",
                        "OP Mainnet": "Optimism",
                    }
                    query_chains = set()
                    for cn in chain_names:
                        query_chains.add(cn)
                        if cn in name_to_proto:
                            query_chains.add(name_to_proto[cn])
                    query_chains = list(query_chains)
                    placeholders = ",".join(["%s"] * len(query_chains))

                    cur.execute(f"""
                        SELECT name, symbol, slug, tvl, change_7d, category, chain
                        FROM src_dl.protocol_list
                        WHERE chain IN ({placeholders})
                        ORDER BY tvl DESC NULLS LAST
                    """, query_chains)
                    all_protos = [dict(r) for r in cur.fetchall()]

                    # 按 chain 分组，每条链取 top 5
                    temp_map: dict[str, list[dict]] = {}
                    for p in all_protos:
                        ch = p.get("chain") or ""
                        if ch not in temp_map or len(temp_map[ch]) < 5:
                            temp_map.setdefault(ch, []).append(p)

                    # 映射回 chain_name（处理别名：protocol_list 用 Binance，我们用 BSC）
                    proto_to_name = {"Binance": "BSC", "Optimism": "OP Mainnet"}
                    for r in top5:
                        cname = r["chain_name"]
                        protos = temp_map.get(cname, [])
                        # 试试反向别名（Binance -> BSC）
                        if not protos:
                            for proto_ch, our_ch in proto_to_name.items():
                                if our_ch == cname:
                                    protos = temp_map.get(proto_ch, [])
                                    break
                        chain_proto_map[cname] = protos

                # 转成和 fetch_chain_flow 一样的结构
                ranked = []
                for r in top5:
                    cname = r["chain_name"]
                    protos_raw = chain_proto_map.get(cname, [])
                    protocols = [
                        {
                            "name": p.get("name", ""),
                            "slug": p.get("slug", ""),
                            "symbol": p.get("symbol", ""),
                            "tvl": float(p["tvl"]) if p.get("tvl") is not None else 0.0,
                            "category": p.get("category", ""),
                            "change_7d": float(p["change_7d"]) if p.get("change_7d") is not None else None,
                        }
                        for p in protos_raw
                    ]
                    # 用 _enrich_protocols_with_asset 补充 asset_id（支持前端跳转）
                    try:
                        protocols = _enrich_protocols_with_asset(protocols)
                    except Exception:
                        pass

                    ranked.append({
                        "chain": cname,
                        "chain_key": r["chain_key"],
                        "tvl": float(r["tvl_usd"]) if r["tvl_usd"] else None,
                        "flow_7d": float(r["flow_7d_usd"]) if r["flow_7d_usd"] is not None else None,
                        "flow_7d_pct": float(r["flow_7d_pct"]) if r["flow_7d_pct"] is not None else None,
                        "degraded": False,
                        "protocols": protocols,
                        "source": "db",
                    })

                return {
                    "status": "ok",
                    "ranked": ranked,
                    "degraded_count": 0,
                    "scanned": len(rows),
                    "snapshot_date": str(rows[0]["snapshot_date"]),
                    "source": "db",
                }
    except Exception:
        return None


def fetch_chain_flow() -> dict:
    """
    链净流入榜 TOP5。优先读数据库（src_dl.chain_tvl_snapshot），
    无数据兜底实时调 DeFiLlama API。
    """
    # 优先读数据库
    db_result = _fetch_chain_flow_from_db()
    if db_result and db_result.get("ranked"):
        return db_result

    # 兜底：实时 API
    try:
        r = requests.get(f"{DL_BASE}/v2/chains", timeout=TIMEOUT)
        r.raise_for_status()
        chains = r.json()
    except Exception as e:
        return {"status": "error", "error": str(e), "ranked": [], "degraded_count": 0, "scanned": 0}

    # 预拉全量协议列表，按 chain 过滤（一次请求替代 N 次）
    all_protocols = []
    try:
        rp = requests.get(f"{DL_BASE}/protocols", timeout=TIMEOUT)
        rp.raise_for_status()
        all_protocols = rp.json() if isinstance(rp.json(), list) else []
    except Exception:
        pass

    chain_protos: dict[str, list] = {}
    for p in all_protocols:
        ch = p.get("chain") or ""
        if not ch:
            continue
        chain_protos.setdefault(ch, []).append(p)
    for ch in chain_protos:
        chain_protos[ch].sort(key=lambda x: -(_safe_float(x.get("tvl"))))
        chain_protos[ch] = chain_protos[ch][:5]

    top = sorted(chains, key=lambda x: -(x.get("tvl") or 0))[:CHAIN_TOP_N]

    def work(c: dict) -> dict | None:
        # 历史端点用链显示名（实测 BSC/Polygon/Avalanche 的 gecko_id 不可用，name 可用）
        ident = c.get("name") or c.get("gecko_id")
        if not ident:
            return None
        info = _chain_7d_flow(ident)
        # 从预拉数据中取该链 Top 5 协议（支持别名映射：BSC→Binance, OP Mainnet→Optimism）
        proto_key = DL_CHAIN_ALIAS.get(ident, ident)
        protos = chain_protos.get(proto_key, chain_protos.get(ident, []))
        top_protos = [
            {
                "name": p.get("name", ""),
                "slug": p.get("slug", ""),
                "symbol": p.get("symbol", ""),
                "tvl": _safe_float(p.get("tvl")),
                "category": p.get("category", ""),
                "change_7d": _safe_float(p.get("change_7d")) if p.get("change_7d") is not None else None,
            }
            for p in protos[:5]
        ]
        if info is None:
            return {
                "chain": c.get("name"),
                "tvl": _safe_float(c.get("tvl")),
                "flow_7d": None,
                "flow_7d_pct": None,
                "degraded": True,
                "protocols": top_protos,
            }
        return {
            "chain": c.get("name"),
            "tvl": info["tvl"],
            "tvl_prev_week": info["tvl_prev_week"],
            "flow_7d": info["flow_7d"],
            "flow_7d_pct": info["flow_7d_pct"],
            "degraded": False,
            "protocols": top_protos,
        }

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = [x for x in ex.map(work, top) if x]

    valid = [x for x in results if x.get("flow_7d") is not None]
    valid.sort(key=lambda x: -x["flow_7d"])
    top5 = valid[:5]

    # FEAT-SECTOR-001: 批量为协议补 asset_id / canonical_symbol，支持前端点击跳转
    all_protos: list[dict] = []
    for c in top5:
        all_protos.extend(c.get("protocols") or [])
    enriched_protos = _enrich_protocols_with_asset(all_protos)
    if enriched_protos is not all_protos:
        idx = 0
        for c in top5:
            protos = c.get("protocols") or []
            for i in range(len(protos)):
                protos[i] = enriched_protos[idx]
                idx += 1

    degraded_count = sum(1 for x in results if x.get("degraded"))
    return {
        "status": "ok",
        "ranked": top5,
        "degraded_count": degraded_count,
        "scanned": len(results),
    }


# ══════════════════════════════════════════════════════════════
# P1-2 背离检测引擎（健康 vs 危险 vs 背离）
# ══════════════════════════════════════════════════════════════

# 背离标签阈值默认值（P2-4 外置 market_rules.yaml，启动时优先读 yaml）
DIVERGENCE_THRESHOLDS_DEFAULT = {
    "oi_surge_pct": 30.0,           # OI 7d 变化 > +30% = 暴增（杠杆过热）
    "oi_flat_pct": 10.0,            # OI 7d 变化 < +10% = 平稳（现货推动）
    "oi_drop_pct": -10.0,           # OI 7d 变化 < -10% = 明显收缩（去杠杆）
    "price_up_pct": 1.0,            # 7d 价涨判定
    "price_down_pct": -1.0,         # 7d 价跌判定
    "funding_extreme": 0.0005,      # 单期 funding(8h) 绝对值 > 0.05% = 极端
    "price_stagnation_pct": 5.0,    # 7d 价变化 < 5% 视为滞涨
    "stablecoin_flow_min_usd": 5_000_000_000,  # 稳定币 7d 净流显著阈值（50 亿美元）
    "corr_strong": 0.6,             # 30d Pearson |r| > 0.6 强耦合
    "corr_decouple": 0.2,           # 30d Pearson r < 0.2 脱钩
    "corr_prior": 0.5,              # 60d 历史相关性基线（曾强相关）
}

# P1-4 机会评分阈值默认值（P2-4 外置 market_rules.yaml）
OPPORTUNITY_THRESHOLDS_DEFAULT = {
    "narrative_min_composite": 8.0,          # 叙事入榜最小合成分
    "narrative_top_n": 3,                    # 叙事机会最多取前 N
    "chain_min_flow_usd": 200_000_000,       # 链 7d 净流入阈值（2 亿美元）
    "chain_min_flow_pct": 3.0,               # 链 7d 净流入百分比阈值
    "chain_top_n": 3,                        # 链机会最多取前 N
    "stablecoin_flow_min_usd": 5_000_000_000,  # 稳定币净流入显著阈值（50 亿美元）
    "emotion_fear_max": 50,                  # 恐贪 < 50 视为恐惧（左侧信号）
    "resonance_high_min_sources": 2,         # 高置信最少独立源类型数
    "resonance_min_source_count": 2,         # 共识动量最少独立数据源数
    "resonance_consensus_top_n": 50,         # 扫描共识榜前 N
    "resonance_max_results": 10,             # 共振榜最多返回条数
    "push_confidence_threshold": "medium",   # 默认只推 高+中（low 剔除，语义由 conviction 承接）
    "conviction_high_min": 70,               # conviction ≥70 → HIGH 置顶（从 75 降至 70）
    "conviction_med_min": 55,                # conviction ≥55 → MED（观察池），<55 → LOW 剔除
    # P0-2 修复：低覆盖资产放宽 MED 阈值，避免数据饿死导致 0 机会
    "conviction_med_min_low_coverage": 50,   # 可用轴权重 < min_available_weight 时 MED 阈值
    "min_available_weight": 0.5,             # 可用轴权重和低于此值视为覆盖度不足
    "protocol_top_n": 3,                     # P1-3 新协议 TVL 异动取前 N
    "exchange_netflow_min_usd": 100_000_000, # 交易所净流出触发 long 信号最小阈值
    # P0-1 MVRV 估值回归
    "mvrv_deep_undervalued_pct": 15,         # MVRV 百分位 ≤ 此值 → 深度低估
    "mvrv_undervalued_pct": 30,              # MVRV 百分位 ≤ 此值 → 低估
    "mvrv_overvalued_pct": 85,               # MVRV 百分位 ≥ 此值 → 高估
    # P0-3 conviction 各轴权重（总和 = 1.0，7 轴含 catalyst）
    "conviction_weight_mvrv": 0.23,          # MVRV 估值权重
    "conviction_weight_cycle": 0.18,         # 周期相位权重（保留，已由 _finalize_conviction 用 regime_mult 替代加性轴）
    "conviction_weight_funding": 0.14,       # funding 极性权重
    "conviction_weight_netflow": 0.14,       # 交易所净流权重
    "conviction_weight_stable": 0.09,        # 稳定币流向权重
    "conviction_weight_roi": 0.14,           # ROI 动量权重
    "conviction_weight_catalyst": 0.08,      # 催化剂因子权重（保守初值，未校准，待 FEAT-CATALYST-002 回测校准）
    # P0-B 催化剂因子
    "catalyst_window_days": 14,              # 催化剂回溯窗口（天）
    "catalyst_min_score": 50,                # 独立强事件机会类入场门槛
    # FEAT-HIGHLIGHT-003 第三刀：评分重构
    "conviction_resonance_step": 6,          # 每多 1 个独立确认源 +6 分
    "conviction_resonance_cap": 18,          # 共振加分上限
    "mvrv_deep_strength_base": 60,
    "mvrv_deep_strength_per_pct": 2.0,
    "mvrv_deep_strength_per_coin": 3,
    "mvrv_deep_strength_cap": 12,
    "fng_strength_base": 50,
    "fng_strength_span": 50,
    "leverage_strength_base": 70,
    "leverage_strength_k": 20000,
    "stablecoin_strength_base": 58,
    "stablecoin_strength_per_1b": 3,
    "catalyst_top_n": 5,                     # 催化剂机会最多取前 N（去重后）
    # 高亮信号板块接入（2026-09-01 工单集合）
    # 工单1: 链上巨鲸异常
    "whale_window_hours": 24,                # 巨鲸回看窗口（小时）
    "whale_usd_min": 1000000,                # 单笔最低美元（默认 100 万刀）
    "whale_top_n": 5,                        # 最多展示巨鲸卡数
    # 工单3: GitHub dev 活跃异动
    "github_window_days": 60,                # 拉取新鲜度窗口（天）
    "github_burst_ratio": 1.5,               # last4 > 1.5*prev4 → 爆发
    "github_decline_ratio": 0.5,             # last4 < 0.5*prev4 → 骤降
    "github_top_n": 5,
    # 工单4: 融资落地
    "raise_window_days": 90,                 # 融资回看窗口（天，低频用 90d 避免空板）
    "raise_top_n": 5,
    # 工单5: 代币解锁抛压
    "unlock_window_days": 14,                # 未来解锁窗口（天）
    "unlock_ratio_min": 1.0,                 # unlock_ratio_mcap 最小阈值（%）
    "unlock_top_n": 5,
    # 工单6: KOL onchain 情报
    "kol_window_days": 7,                    # KOL 信号回看窗口（天）
    "kol_top_n": 5,
    # FEAT-HORIZON-001: 信号周期 & 有效期配置
    # horizon: short(1-7天) / medium(1-3月) / long(6月+)
    # expire_days: 信号有效期（线性衰减到 0 的天数）
    # weight_override: 可选，替代默认 conviction 权重（None=用默认）
    "signal_horizon_map": {
        # ── 估值类（长期，慢变量，作为过滤器）──
        "mvrv_deep_under":      {"horizon": "long",    "expire_days": 90,  "is_valuation": True},
        "mvrv_under_watch":     {"horizon": "long",    "expire_days": 60,  "is_valuation": True},
        "mvrv_deep_over":       {"horizon": "long",    "expire_days": 90,  "is_valuation": True},
        "mvrv_over_watch":      {"horizon": "long",    "expire_days": 60,  "is_valuation": True},
        # ── 价格/量能类（短期，战术信号）──
        "price_surge":          {"horizon": "short",   "expire_days": 5},
        "price_crash":          {"horizon": "short",   "expire_days": 5},
        "price_volume_surge":   {"horizon": "short",   "expire_days": 7},
        "volume_surge":         {"horizon": "short",   "expire_days": 5},
        # ── 赛道/板块类（中期，趋势信号）──
        "sector_inflow":        {"horizon": "medium",  "expire_days": 14},
        "sector_outflow":       {"horizon": "medium",  "expire_days": 14},
        "narrative_surge":      {"horizon": "medium",  "expire_days": 21},
        "chain_inflow":         {"horizon": "medium",  "expire_days": 14},
        # ── 资金流类（中期，核心趋势）──
        "stablecoin_inflow":    {"horizon": "medium",  "expire_days": 21},
        "stablecoin_outflow":   {"horizon": "medium",  "expire_days": 21},
        "etf_flow":             {"horizon": "short",   "expire_days": 5},
        "btc_left_accum":       {"horizon": "medium",  "expire_days": 14},
        "cm_adoption_divergence": {"horizon": "medium","expire_days": 21},
        # ── 链上/情绪类（短期，战术信号）──
        "leverage_extreme":     {"horizon": "short",   "expire_days": 3},
        "fng_extreme":          {"horizon": "short",   "expire_days": 5},
        "whale_flow":           {"horizon": "short",   "expire_days": 5},
        "funding":              {"horizon": "short",   "expire_days": 3},
        # ── KOL/事件类（短期，脉冲信号）──
        "kol_onchain":          {"horizon": "short",   "expire_days": 7},
        "catalyst":             {"horizon": "short",   "expire_days": 10},
        # ── 基本面类（长期）──
        "github_activity":      {"horizon": "long",    "expire_days": 60},
        "funding_raise":        {"horizon": "long",    "expire_days": 45},
        "token_unlock":         {"horizon": "short",   "expire_days": 14},
    },
    # FEAT-HORIZON-001: 估值过滤器阈值
    "valuation_filter_high_pct": 85,   # MVRV ≥ 此值 → 多头信号打折扣
    "valuation_filter_low_pct": 15,    # MVRV ≤ 此值 → 空头信号打折扣
    "valuation_filter_mult": 0.5,      # 过滤区信号强度乘数（音量旋钮）
    # FEAT-HORIZON-001: 周期权重（加权总分用）
    "horizon_weight_short": 0.25,
    "horizon_weight_medium": 0.50,
    "horizon_weight_long": 0.25,
}


def _load_market_rules() -> dict:
    """从 market_rules.yaml 加载规则（缺失/解析失败回退默认值，不影响运行）。"""
    rules = {
        "divergence_thresholds": dict(DIVERGENCE_THRESHOLDS_DEFAULT),
        "opportunity_thresholds": dict(OPPORTUNITY_THRESHOLDS_DEFAULT),
        "emotion_weights": dict(EMOTION_WEIGHTS_DEFAULT),
        "structure_weights": dict(STRUCTURE_WEIGHTS_DEFAULT),
        "extreme_zone": dict(EXTREME_ZONE_DEFAULT),
        "narrative_chain": dict(NARRATIVE_CHAIN_DEFAULT),
        "scoring_tuning": dict(SCORING_TUNING_DEFAULT),
    }
    try:
        import yaml

        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market_rules.yaml")
        if not os.path.exists(path):
            return rules
        data = yaml.safe_load(open(path, encoding="utf-8")) or {}
        # 标量覆盖（divergence / opportunity / scoring_tuning 等 flat dict）
        for section, target in (
            ("divergence_thresholds", rules["divergence_thresholds"]),
            ("opportunity_rules", rules["opportunity_thresholds"]),
            ("emotion_weights", rules["emotion_weights"]),
            ("structure_weights", rules["structure_weights"]),
            ("extreme_zone", rules["extreme_zone"]),
            ("narrative_chain", rules["narrative_chain"]),
            ("scoring_tuning", rules["scoring_tuning"]),
        ):
            overrides = data.get(section) or {}
            for k, v in overrides.items():
                if k in target:
                    try:
                        target[k] = type(target[k])(v) if not isinstance(target[k], bool) else v
                    except (TypeError, ValueError):
                        pass
    except Exception:
        pass
    return rules


_MARKET_RULES = _load_market_rules()
DIVERGENCE_THRESHOLDS = _MARKET_RULES["divergence_thresholds"]
OPPORTUNITY_THRESHOLDS = _MARKET_RULES["opportunity_thresholds"]
EMOTION_WEIGHTS = _MARKET_RULES["emotion_weights"]
STRUCTURE_WEIGHTS = _MARKET_RULES["structure_weights"]
EXTREME_ZONE = _MARKET_RULES["extreme_zone"]
NARRATIVE_CHAIN = _MARKET_RULES["narrative_chain"]
SCORING_TUNING = _MARKET_RULES["scoring_tuning"]

# P1-1 叙事/链榜配置（从 yaml 覆盖）
NARRATIVE_TOP_N = int(NARRATIVE_CHAIN["narrative_top_n"])
TVL_LEG_MIN = NARRATIVE_CHAIN["tvl_leg_min"]
CHAIN_TOP_N = int(NARRATIVE_CHAIN["chain_top_n"])

DIVERGENCE_META = {
    "price_oi": {"label": "价格 vs OI", "icon": "⚖️"},
    "price_funding": {"label": "价格 vs funding", "icon": "🔥"},
    "price_stablecoin": {"label": "价格 vs 稳定币净流", "icon": "💰"},
    "btc_nasdaq": {"label": "BTC vs 纳指", "icon": "🌐"},
}


def _pct_change(first: float, last: float) -> float | None:
    """计算百分比变化（%），first<=0 返回 None。"""
    if first is None or last is None:
        return None
    if first == 0:
        return None
    return (last - first) / first * 100


def _pearson(a: list[float], b: list[float]) -> float | None:
    """两序列同尾对齐后的 Pearson 相关系数（纯 Python 实现）。"""
    n = min(len(a), len(b))
    if n < 5:
        return None
    a = a[-n:]
    b = b[-n:]
    ma = sum(a) / n
    mb = sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va == 0 or vb == 0:
        return None
    return cov / ((va * vb) ** 0.5)


def _daily_returns(closes: list[float]) -> list[float]:
    return [(closes[i] / closes[i - 1] - 1) for i in range(1, len(closes)) if closes[i - 1]]


def _fetch_binance_oi_history(days: int = 30) -> dict:
    """Binance fapi 日频未平仓合约历史。返回 {status, series: [{date, oi}]}。"""
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(
                f"{BINANCE_FAPI}/futures/data/openInterestHist",
                params={"symbol": "BTCUSDT", "period": "1d", "limit": days},
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            rows = r.json()
            series = sorted(
                (
                    {"date": int(row["timestamp"] // 1000), "oi": _safe_float(row.get("sumOpenInterest"))}
                    for row in rows
                    if row.get("sumOpenInterest") is not None
                ),
                key=lambda x: x["date"],
            )
            if not series:
                return {"status": "error", "error": "empty", "series": []}
            return {"status": "ok", "series": series}
        except Exception as e:
            last_err = e
            time.sleep(1.5)
    return {"status": "error", "error": str(last_err), "series": []}


def _fetch_binance_funding_history(limit: int = 100) -> dict:
    """Binance fapi 历史 funding rate（8h 一期）。返回 {status, rates: [...]}。"""
    try:
        r = requests.get(
            f"{BINANCE_FAPI}/fapi/v1/fundingRate",
            params={"symbol": "BTCUSDT", "limit": limit},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        rows = r.json()
        rates = [
            _safe_float(row.get("fundingRate"))
            for row in rows
            if row.get("fundingRate") is not None
        ]
        if not rates:
            return {"status": "error", "error": "empty", "rates": []}
        return {"status": "ok", "rates": rates}
    except Exception as e:
        return {"status": "error", "error": str(e), "rates": []}


def _fetch_stablecoin_supply_history(days: int = 35) -> dict:
    """DeFiLlama 稳定币总流通量日频序列。返回 {status, series: [{date, supply}]}。"""
    try:
        r = requests.get("https://stablecoins.llama.fi/stablecoincharts/All", timeout=TIMEOUT)
        r.raise_for_status()
        rows = r.json()
        series = []
        for row in rows:
            usd = (row.get("totalCirculating") or {}).get("peggedUSD")
            if usd is None:
                continue
            series.append({"date": int(row["date"]), "supply": _safe_float(usd)})
        if not series:
            return {"status": "error", "error": "empty", "series": []}
        return {"status": "ok", "series": series[-days:]}
    except Exception as e:
        return {"status": "error", "error": str(e), "series": []}


def _yf_closes(symbol: str, days: int = 90) -> list[float] | None:
    """yfinance 拉日频收盘序列（主源，Zeabur 美区直连稳定）。失败返回 None。"""
    try:
        import yfinance as yf

        for attempt in range(3):
            try:
                df = yf.download(
                    symbol,
                    period=f"{days}d",
                    interval="1d",
                    progress=False,
                    auto_adjust=True,
                    threads=False,
                )
                if df is not None and not df.empty:
                    closes = [float(v) for v in df["Close"].dropna().tolist()]
                    if len(closes) >= 2:
                        return closes
            except Exception:
                pass
            time.sleep(1.5)
        return None
    except Exception:
        return None


def _stooq_closes(symbol: str) -> list[float] | None:
    """stooq 日频 CSV 兜底（yfinance 限流时使用）。失败返回 None。"""
    try:
        r = requests.get(f"https://stooq.com/q/d/l/?s={symbol}&i=d", timeout=TIMEOUT)
        r.raise_for_status()
        lines = r.text.strip().splitlines()
        if len(lines) < 2:
            return None
        closes: list[float] = []
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) >= 5 and parts[4] not in ("", "null"):
                try:
                    closes.append(float(parts[4]))
                except ValueError:
                    pass
        return closes[-90:] if closes else None
    except Exception:
        return None


def _yahoo_chart_closes(symbol: str, rng: str = "3mo") -> list[float] | None:
    """直接调 Yahoo Finance chart API（免 crumb，比 yfinance 更稳），失败返回 None。"""
    sym = symbol.replace("^", "%5E")
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            r = requests.get(
                f"https://{host}/v8/finance/chart/{sym}",
                params={"range": rng, "interval": "1d"},
                timeout=TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            r.raise_for_status()
            res = r.json().get("chart", {}).get("result")
            if not res or not res[0].get("timestamp"):
                continue
            closes = [
                c for c in (res[0].get("indicators", {}).get("quote", [{}])[0].get("close") or [])
                if c is not None
            ]
            if len(closes) >= 2:
                return closes
        except Exception:
            continue
    return None


def _fetch_nasdaq_gold() -> dict:
    """纳指 + 黄金日频序列。返回 {status, nasdaq, gold}。yfinance 主源，Yahoo chart API 兜底。"""
    nasdaq = _yf_closes("^IXIC") or _yahoo_chart_closes("^IXIC") or _stooq_closes("^ndx")
    gold = _yf_closes("GC=F") or _yahoo_chart_closes("GC=F") or _stooq_closes("gc.f")
    if not nasdaq and not gold:
        return {"status": "error", "error": "no cross-asset data", "nasdaq": [], "gold": []}
    status = "ok" if nasdaq and gold else "partial"
    return {"status": status, "nasdaq": nasdaq or [], "gold": gold or []}


def detect_price_oi_divergence(btc_closes: list[float], oi_series: list[dict], t: dict | None = None) -> dict:
    """价格 vs OI 背离：价涨 OI 不涨=现货推动健康；价涨 OI 暴增=杠杆过热危险。"""
    t = t or DIVERGENCE_THRESHOLDS
    if not btc_closes or len(btc_closes) < 8 or not oi_series or len(oi_series) < 8:
        return {"signal": "price_oi", "label": "DEGRADED", "status": "error",
                "interpretation": "价格/OI 数据不足，无法检测", "metrics": {}}
    price_pct = _pct_change(btc_closes[-8], btc_closes[-1])
    oi_now = oi_series[-1]["oi"]
    oi_prev = oi_series[-8]["oi"]
    oi_pct = _pct_change(oi_prev, oi_now)
    metrics = {"price_7d_pct": round(price_pct or 0, 2), "oi_7d_pct": round(oi_pct or 0, 2)}

    if price_pct is None or oi_pct is None:
        return {"signal": "price_oi", "label": "DEGRADED", "status": "error",
                "interpretation": "价格/OI 变化无法计算", "metrics": metrics}

    if price_pct >= t["price_up_pct"]:
        if oi_pct >= t["oi_surge_pct"]:
            label = "DANGEROUS"
            interp = f"价涨 {price_pct:.1f}% 但 OI 暴增 {oi_pct:.1f}%：杠杆过热，回撤风险高"
        elif oi_pct >= t["oi_flat_pct"]:
            label = "HEALTHY"
            interp = f"价涨 {price_pct:.1f}% + OI 温和增 {oi_pct:.1f}%：资金推动健康"
        else:
            label = "HEALTHY"
            interp = f"价涨 {price_pct:.1f}% + OI 未跟涨 {oi_pct:.1f}%：现货推动，杠杆不拥挤"
    elif price_pct <= t["price_down_pct"]:
        if oi_pct <= t["oi_drop_pct"]:
            label = "HEALTHY"
            interp = f"价跌 {price_pct:.1f}% + OI 收缩 {oi_pct:.1f}%：去杠杆释放风险"
        elif oi_pct >= t["oi_surge_pct"]:
            label = "DANGEROUS"
            interp = f"价跌 {price_pct:.1f}% 但 OI 暴增 {oi_pct:.1f}%：杠杆堆积未出清，下跌风险延续"
        else:
            label = "DIVERGENT"
            interp = f"价跌 {price_pct:.1f}% 但 OI 基本未动 {oi_pct:.1f}%：空头被动持仓，警惕急拉"
    else:
        if oi_pct >= t["oi_surge_pct"]:
            label = "DANGEROUS"
            interp = f"价横盘 {price_pct:.1f}% + OI 暴增 {oi_pct:.1f}%：杠杆潜伏，随时放大波动"
        else:
            label = "HEALTHY"
            interp = f"价横盘 {price_pct:.1f}% + OI {oi_pct:.1f}%：中性"
    return {"signal": "price_oi", "label": label, "status": "ok", "interpretation": interp, "metrics": metrics}


def detect_price_funding_divergence(btc_closes: list[float], rates: list[float], t: dict | None = None) -> dict:
    """价格 vs funding：极端正 funding + 价滞涨 = 挤仓风险；负 funding + 价滞涨 = 逼空风险。"""
    t = t or DIVERGENCE_THRESHOLDS
    if not btc_closes or len(btc_closes) < 8 or not rates:
        return {"signal": "price_funding", "label": "DEGRADED", "status": "error",
                "interpretation": "价格/funding 数据不足，无法检测", "metrics": {}}
    price_pct = _pct_change(btc_closes[-8], btc_closes[-1])
    funding = rates[-1]
    funding_7d_avg = sum(rates[-21:]) / len(rates[-21:]) if len(rates) >= 21 else funding
    metrics = {
        "price_7d_pct": round(price_pct or 0, 2),
        "funding_latest": funding,
        "funding_7d_avg": round(funding_7d_avg, 6),
    }
    if price_pct is None:
        return {"signal": "price_funding", "label": "DEGRADED", "status": "error",
                "interpretation": "价格变化无法计算", "metrics": metrics}

    if funding > t["funding_extreme"]:
        if abs(price_pct) < t["price_stagnation_pct"]:
            label = "DANGEROUS"
            interp = f"funding {funding * 100:.3f}%/期 极端多头 + 价滞涨 {price_pct:.1f}%：多头拥挤，挤压/回撤风险"
        else:
            label = "DIVERGENT"
            interp = f"funding {funding * 100:.3f}%/期 极端正 + 价仍涨 {price_pct:.1f}%：过热但趋势未破，警惕顶部"
    elif funding < -t["funding_extreme"]:
        if abs(price_pct) < t["price_stagnation_pct"]:
            label = "DIVERGENT"
            interp = f"funding {funding * 100:.3f}%/期 极端负 + 价滞涨 {price_pct:.1f}%：空头拥挤，逼空风险"
        else:
            label = "HEALTHY"
            interp = f"funding {funding * 100:.3f}%/期 极端负 + 价跌 {price_pct:.1f}%：空头主导出清"
    else:
        label = "HEALTHY"
        interp = f"funding {funding * 100:.3f}%/期 正常区间：无极端仓位拥挤"
    return {"signal": "price_funding", "label": label, "status": "ok", "interpretation": interp, "metrics": metrics}


def detect_price_stablecoin_divergence(btc_closes: list[float], sc_series: list[dict], t: dict | None = None) -> dict:
    """价格 vs 稳定币净流：价涨但稳定币净流出=存量博弈；价稳但稳定币净流入=场外弹药积累。"""
    t = t or DIVERGENCE_THRESHOLDS
    if not btc_closes or len(btc_closes) < 8 or not sc_series or len(sc_series) < 8:
        return {"signal": "price_stablecoin", "label": "DEGRADED", "status": "error",
                "interpretation": "价格/稳定币数据不足，无法检测", "metrics": {}}
    price_pct = _pct_change(btc_closes[-8], btc_closes[-1])
    sc_now = sc_series[-1]["supply"]
    sc_prev = sc_series[-8]["supply"]
    netflow = sc_now - sc_prev
    flow_pct = _pct_change(sc_prev, sc_now)
    metrics = {
        "price_7d_pct": round(price_pct or 0, 2),
        "stablecoin_supply": round(sc_now, 0),
        "stablecoin_7d_netflow_usd": round(netflow, 0),
        "stablecoin_7d_pct": round(flow_pct or 0, 3),
    }
    if price_pct is None or sc_prev == 0:
        return {"signal": "price_stablecoin", "label": "DEGRADED", "status": "error",
                "interpretation": "价格/稳定币变化无法计算", "metrics": metrics}

    flow_b = netflow / 1e9
    if price_pct >= t["price_up_pct"]:
        if netflow < -t["stablecoin_flow_min_usd"]:
            label = "DIVERGENT"
            interp = f"价涨 {price_pct:.1f}% 但稳定币净流出 {flow_b:+.1f}B：存量博弈，上涨或虚"
        else:
            label = "HEALTHY"
            interp = f"价涨 {price_pct:.1f}% + 稳定币净流 {flow_b:+.1f}B：场外弹药充足"
    elif price_pct <= t["price_down_pct"]:
        if netflow > t["stablecoin_flow_min_usd"]:
            label = "HEALTHY"
            interp = f"价跌 {price_pct:.1f}% 但稳定币净流入 {flow_b:+.1f}B：场外弹药积累，左侧信号"
        else:
            label = "DIVERGENT"
            interp = f"价跌 {price_pct:.1f}% + 稳定币净流出 {flow_b:+.1f}B：抛压延续"
    else:
        if netflow > t["stablecoin_flow_min_usd"]:
            label = "HEALTHY"
            interp = f"价横盘 {price_pct:.1f}% + 稳定币净流入 {flow_b:+.1f}B：蓄势待发"
        else:
            label = "HEALTHY"
            interp = f"价横盘 {price_pct:.1f}%，稳定币净流 {flow_b:+.1f}B 中性"
    return {"signal": "price_stablecoin", "label": label, "status": "ok", "interpretation": interp, "metrics": metrics}


def detect_btc_nasdaq_divergence(
    btc_closes: list[float], nasdaq_closes: list[float], gold_closes: list[float], t: dict | None = None
) -> dict:
    """BTC vs 纳指 30d 相关性/背离（yfinance 主源）。曾强相关后脱钩=宏观脱钩独立行情。"""
    t = t or DIVERGENCE_THRESHOLDS
    if not btc_closes or len(btc_closes) < 10 or not nasdaq_closes or len(nasdaq_closes) < 10:
        return {"signal": "btc_nasdaq", "label": "DEGRADED", "status": "error",
                "interpretation": "纳指/黄金序列缺失（yfinance 不可达），无法检测", "metrics": {}}
    btc_ret = _daily_returns(btc_closes)
    ndx_ret = _daily_returns(nasdaq_closes)
    r30 = _pearson(btc_ret[-30:], ndx_ret[-30:])
    r60 = _pearson(btc_ret[-60:], ndx_ret[-60:]) if len(btc_ret) >= 60 and len(ndx_ret) >= 60 else r30
    gold_ret = _daily_returns(gold_closes) if len(gold_closes) >= 10 else []
    r30_gold = _pearson(btc_ret[-30:], gold_ret[-30:]) if len(gold_ret) >= 30 else None
    metrics = {
        "nasdaq_30d_corr": round(r30, 3) if r30 is not None else None,
        "nasdaq_60d_corr": round(r60, 3) if r60 is not None else None,
        "gold_30d_corr": round(r30_gold, 3) if r30_gold is not None else None,
    }
    if r30 is None:
        return {"signal": "btc_nasdaq", "label": "DEGRADED", "status": "error",
                "interpretation": "相关性序列不足", "metrics": metrics}

    if r60 is not None and r60 >= t["corr_prior"] and r30 < t["corr_decouple"]:
        label = "DIVERGENT"
        interp = f"BTC-纳指 30d 相关 {r30:.2f}（60d 曾 {r60:.2f}）：宏观脱钩，独立行情"
    elif abs(r30) >= t["corr_strong"]:
        label = "HEALTHY"
        interp = f"BTC-纳指 30d 相关 {r30:.2f}：强耦合，受宏观风险偏好驱动"
    elif r30 < t["corr_decouple"]:
        label = "DIVERGENT"
        interp = f"BTC-纳指 30d 相关 {r30:.2f}：低相关，加密独立行情"
    else:
        label = "HEALTHY"
        interp = f"BTC-纳指 30d 相关 {r30:.2f}：中性耦合"
    return {"signal": "btc_nasdaq", "label": label, "status": "ok", "interpretation": interp, "metrics": metrics}


def build_divergence_signals() -> dict:
    """编排四对背离检测，输出 {status, degraded, signals}。单源失败降级标注不中断。"""
    btc = fetch_binance_btc_klines()
    btc_closes = btc.get("closes") or []
    oi = _fetch_binance_oi_history()
    funding = _fetch_binance_funding_history()
    sc = _fetch_stablecoin_supply_history()
    cross = _fetch_nasdaq_gold()

    detectors = [
        detect_price_oi_divergence(btc_closes, oi.get("series") or []),
        detect_price_funding_divergence(btc_closes, funding.get("rates") or []),
        detect_price_stablecoin_divergence(btc_closes, sc.get("series") or []),
        detect_btc_nasdaq_divergence(btc_closes, cross.get("nasdaq") or [], cross.get("gold") or []),
    ]
    # 补充分类元信息
    for s in detectors:
        meta = DIVERGENCE_META.get(s["signal"], {})
        s["name"] = meta.get("label", s["signal"])
        s["icon"] = meta.get("icon", "📡")

    ok_count = sum(1 for s in detectors if s["status"] == "ok")
    degraded = [s["signal"] for s in detectors if s["status"] != "ok"]
    status = "ok" if ok_count == len(detectors) else ("partial" if ok_count > 0 else "error")
    return {"status": status, "degraded": degraded, "signals": detectors}


# ══════════════════════════════════════════════════════════════
# P1-4 机会评分清单（Layer 3 最终交付物）
# ══════════════════════════════════════════════════════════════

def _fmt_billions(value: float | None) -> str:
    """格式化美元金额为 +x.xB / -x.xB。"""
    if value is None:
        return "N/A"
    return f"{value / 1e9:+.1f}B"


def _resolve_confidence(sources: list[tuple[str, str]], t: dict) -> tuple[str, str]:
    """
    共振置信判定（PD-1）：sources = [(source_type, direction), ...]。
    返回 (confidence, direction)；方向冲突 → (low, neutral)。
    高 = 独立源类型数 ≥ high_min_sources 且同向；中 = 单强信号。
    """
    dirs = {d for _, d in sources}
    types = {typ for typ, _ in sources}
    if len(dirs) > 1:
        return "low", "neutral"
    direction = next(iter(dirs))
    n_types = len(types)
    if n_types >= int(t.get("resonance_high_min_sources", 2)):
        return "high", direction
    return "medium", direction


def _push_opportunity(opp: dict, opportunities: list[dict], excluded: list[dict], t: dict,
                      cycle_phase: str = "unknown", n_confirm: int = 1) -> None:
    """FEAT-HIGHLIGHT-003：写入 conviction_tier 并按 tier 过滤。

    score = _finalize_conviction(raw, cycle_phase, n_confirm, t)
    raw_strength / regime_mult / resonance_bonus 写入 opp 供前端解释。

    P0-2 修复：当可用轴权重和不足（coverage_weight < min_available_weight）时，
    降低 MED 阈值，避免低覆盖资产被统一剔除。

    去重：按 (signal_type, target) 去重，已存在的同类型同标的信号不再重复添加。
    """
    # 去重检查：同 signal_type + 同 target 不重复添加
    st = opp.get("signal_type") or ""
    tgt = (opp.get("target") or "").strip()
    for existing in opportunities + excluded:
        if (existing.get("signal_type") or "") == st \
           and (existing.get("target") or "").strip() == tgt:
            return  # 已存在，跳过
    raw = opp.get("conviction_score")
    if raw is None:
        raw = 45
    score, extra = _finalize_conviction(raw, cycle_phase, n_confirm, t)
    opp["conviction_score"] = score
    opp["conviction_strength"] = extra["raw_strength"]
    opp["regime_mult"] = extra["regime_mult"]
    opp["resonance_bonus"] = extra["resonance_bonus"]
    high_min = int(t.get("conviction_high_min", 75))
    med_min = int(t.get("conviction_med_min", 55))

    # P0-2：覆盖度不足时放宽 MED 阈值，避免数据饿死导致 0 机会
    breakdown = opp.get("conviction_breakdown")
    if breakdown:
        coverage_weight = breakdown.get("coverage_weight", 1.0)
        min_available_weight = float(t.get("min_available_weight", 0.4))
        if coverage_weight < min_available_weight:
            med_min = int(t.get("conviction_med_min_low_coverage", 50))
            opp["coverage_note"] = f"覆盖度不足({coverage_weight:.2f})，MED 阈值降至 {med_min}"

    tier = "HIGH" if score >= high_min else ("MED" if score >= med_min else "LOW")
    opp["conviction_tier"] = tier
    if tier == "LOW":
        excluded.append(opp)
    else:
        opportunities.append(opp)


def _normalize_symbol(target: str) -> str:
    """机会 target → MVRV map 的 symbol 键（小写、去「链」后缀、别名归一）。匹配不到返回原小写。"""
    if not target:
        return ""
    s = target.lower().strip()
    for suf in (" 链", "链"):
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    aliases = {
        "bitcoin": "btc", "ethereum": "eth", "solana": "sol",
    }
    return aliases.get(s, s)


def _mvrv_pct_for(target: str, mvrv_map: dict) -> float | None:
    """取机会对应币种的 MVRV 百分位；匹配不到回退 BTC 市场锚；map 空返回 None（不惩罚）。"""
    if not mvrv_map:
        return None
    coin = mvrv_map.get(_normalize_symbol(target))
    if coin is None:
        coin = mvrv_map.get("btc")
    return coin.get("pct_full") if coin else None


def _compute_conviction_score(
    *,
    mvrv_pct: float | None,
    funding: float | None,
    exchange_netflow: float | None,
    stablecoin_flow: float | None,
    roi_1yr: float | None = None,
    catalyst_score: float | None = None,
    t: dict,
) -> int:
    """返回 raw_strength (不含周期调制)。cycle 由 _finalize_conviction 乘入。"""
    return _conviction_breakdown(
        mvrv_pct=mvrv_pct,
        funding=funding, exchange_netflow=exchange_netflow,
        stablecoin_flow=stablecoin_flow, roi_1yr=roi_1yr,
        catalyst_score=catalyst_score, t=t,
    )["raw_strength"]


def _conviction_breakdown(
    *,
    mvrv_pct: float | None,
    funding: float | None,
    exchange_netflow: float | None,
    stablecoin_flow: float | None,
    roi_1yr: float | None = None,
    catalyst_score: float | None = None,
    t: dict,
) -> dict:
    """P0-3 + FEAT-HIGHLIGHT-003：返回各轴子分与加权贡献，供前端解释。

    返回 {axes: {name: {score, weight, contribution}}, raw_strength}。
    raw_strength 不含周期调制（由 _finalize_conviction 乘入 regime_mult）。
    """
    # ── MVRV 轴：低估=高分（均值回归逻辑），高估=低分 ──
    mvrv_score = max(0, min(100, 100 - mvrv_pct)) if mvrv_pct is not None else 50

    # ── Funding 轴：正=过热（做空机会/风险），负=恐慌（做多机会） ──
    funding_score = max(0, min(100, 50 - funding * 10000)) if funding is not None else 50

    # ── 交易所净流轴：正=净流出(积累)=做多信号 ──
    netflow_score = max(0, min(100, 50 + (exchange_netflow / 1e9) * 20)) if exchange_netflow else 50

    # ── 稳定币流轴：正=场外弹药充裕=做多信号 ──
    stable_score = max(0, min(100, 50 + (stablecoin_flow / 1e10) * 30)) if stablecoin_flow else 50

    # ── ROI 轴：深度负(超跌)=高做多分，深度正(过热)=高做空分 ──
    roi_score = max(0, min(100, 50 - roi_1yr * 30)) if roi_1yr is not None else 50

    # ── Catalyst 轴：净情绪分数（0~100），None 时取中性 50 ──
    cat_norm = max(0, min(100, catalyst_score)) if catalyst_score is not None else 50

    def _w(k: str) -> float:
        return t.get(k, 0.0)

    axes = {
        "mvrv": (mvrv_score, _w("conviction_weight_mvrv"), mvrv_pct is not None),
        "funding": (funding_score, _w("conviction_weight_funding"), funding is not None),
        "netflow": (netflow_score, _w("conviction_weight_netflow"), exchange_netflow is not None),
        "stable": (stable_score, _w("conviction_weight_stable"), stablecoin_flow is not None),
        "roi": (roi_score, _w("conviction_weight_roi"), roi_1yr is not None),
        "catalyst": (cat_norm, _w("conviction_weight_catalyst"), catalyst_score is not None),
    }
    breakdown: dict = {}
    total = 0.0
    coverage_weight = 0.0
    for name, (sc, wt, available) in axes.items():
        contrib = round(sc * wt, 1)
        total += contrib
        if available:
            coverage_weight += wt
        breakdown[name] = {"score": sc, "weight": wt, "contribution": contrib, "available": available}
    return {"axes": breakdown, "raw_strength": max(0, min(100, round(total))), "coverage_weight": round(coverage_weight, 2)}


# ── FEAT-HIGHLIGHT-003：周期调制乘子 ──
_CYCLE_REGIME_MULT = {
    "early_bottom": 1.15, "late_bottom": 1.10, "bottom": 1.12,
    "mid_cycle": 1.00, "early_top": 0.85, "late_top": 0.80,
    "top": 0.82, "unknown": 1.00,
}

def _cycle_regime_mult(cycle_phase: str) -> float:
    """周期相位 → 调制乘子（积/底部放大多头信号，顶/顶收敛空头信号）。"""
    return _CYCLE_REGIME_MULT.get(cycle_phase, 1.00)


def _finalize_conviction(raw_strength: float, cycle_phase: str, n_confirm: int, t: dict) -> tuple[int, dict]:
    """FEAT-HIGHLIGHT-003：raw_strength × regime_mult + 多源共振加分 → 最终 conviction。"""
    regime_mult = _cycle_regime_mult(cycle_phase)
    step = int(t.get("conviction_resonance_step", 6))
    cap = int(t.get("conviction_resonance_cap", 18))
    bonus = min(cap, max(0, int(n_confirm) - 1) * step)
    score = max(0, min(100, round(raw_strength * regime_mult + bonus)))
    return score, {"raw_strength": raw_strength, "regime_mult": regime_mult, "resonance_bonus": bonus}


# ══════════════════════════════════════════════════════════════
# FEAT-HORIZON-001: 信号周期 & 时间衰减 & 估值过滤器
# ══════════════════════════════════════════════════════════════

def _signal_horizon_info(signal_type: str, t: dict) -> dict:
    """根据 signal_type 返回周期配置，默认 medium / 14 天。"""
    hmap = t.get("signal_horizon_map", {}) or {}
    info = hmap.get(signal_type) or {}
    return {
        "horizon": info.get("horizon", "medium"),
        "expire_days": int(info.get("expire_days", 14)),
        "is_valuation": bool(info.get("is_valuation", False)),
    }


def _decay_strength(strength: float, days_old: float, expire_days: int) -> float:
    """信号强度随时间线性衰减，超过有效期后为 0。"""
    if days_old <= 0:
        return strength
    if days_old >= expire_days:
        return 0.0
    decay = 1.0 - (days_old / expire_days)
    return max(0.0, strength * decay)


def _apply_valuation_filter(
    score: float,
    direction: str,
    mvrv_pct: float | None,
    t: dict,
) -> tuple[float, str]:
    """FEAT-HORIZON-001：估值过滤器（音量旋钮）。

    - MVRV 极度高估（≥high_pct）：多头信号打 valuation_filter_mult 折
    - MVRV 极度低估（≤low_pct）：空头信号打 valuation_filter_mult 折
    - 中间区间：不影响

    返回 (调整后分数, 过滤说明)
    """
    if mvrv_pct is None:
        return score, ""
    high_pct = float(t.get("valuation_filter_high_pct", 85))
    low_pct = float(t.get("valuation_filter_low_pct", 15))
    mult = float(t.get("valuation_filter_mult", 0.5))

    if mvrv_pct >= high_pct and direction == "long":
        return round(score * mult, 1), f"估值过热，多头信号衰减 ×{mult}"
    if mvrv_pct <= low_pct and direction == "short":
        return round(score * mult, 1), f"估值过冷，空头信号衰减 ×{mult}"
    return score, ""


def _annotate_horizon(opp: dict, t: dict, signal_date: str | None = None) -> dict:
    """为单个机会/信号标注周期信息。

    新增字段：horizon, expire_days, is_valuation, signal_date,
             decayed_score, days_old, valuation_filter_note
    直接修改原 dict 并返回。
    """
    from datetime import date, datetime

    stype = opp.get("signal_type", "")
    info = _signal_horizon_info(stype, t)

    opp["horizon"] = info["horizon"]
    opp["expire_days"] = info["expire_days"]
    opp["is_valuation"] = info["is_valuation"]

    # 信号日期（默认今天）
    if signal_date:
        opp["signal_date"] = signal_date
    else:
        opp["signal_date"] = date.today().isoformat()

    # 计算衰减后分数
    try:
        sd = datetime.strptime(opp["signal_date"], "%Y-%m-%d").date()
        days_old = (date.today() - sd).days
    except Exception:
        days_old = 0
    opp["days_old"] = days_old

    base_score = float(opp.get("conviction_score", 0) or 0)
    decayed = _decay_strength(base_score, days_old, info["expire_days"])
    opp["decayed_score"] = round(decayed, 1)

    # 估值过滤器（用 BTC 的 MVRV 作为大盘估值参考）
    opp["valuation_filter_note"] = ""

    return opp


def apply_horizon_to_opportunities(
    opportunities: list[dict],
    t: dict,
    btc_mvrv_pct: float | None = None,
) -> list[dict]:
    """为一组机会批量标注周期 + 应用估值过滤器。"""
    for opp in opportunities:
        _annotate_horizon(opp, t)

        # 应用估值过滤器（仅对非估值类信号）
        if not opp.get("is_valuation") and btc_mvrv_pct is not None:
            direction = opp.get("direction", "long") or "long"
            score = float(opp.get("decayed_score", opp.get("conviction_score", 0)) or 0)
            filtered, note = _apply_valuation_filter(score, direction, btc_mvrv_pct, t)
            opp["decayed_score"] = round(filtered, 1)
            opp["valuation_filter_note"] = note

    return opportunities


def _recent_catalyst_targets(window_days: int = 14) -> list[tuple[int, str, float]]:
    """查询近 N 天内有催化剂的资产，返回 [(asset_id, symbol, score), ...]。

    score = 净加权情绪分（0-100）：bullish=+1, bearish=-1, neutral=0；
    权重：strong=1.0, medium=0.6, weak=0.3。归一化到 0-100。
    """
    try:
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection
        from datetime import timedelta

        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT ci.asset_id,
                           a.canonical_symbol,
                           SUM(
                               CASE ci.impact_direction
                                   WHEN 'bullish' THEN 1
                                   WHEN 'bearish' THEN -1
                                   ELSE 0
                               END
                               * CASE ci.impact_strength
                                   WHEN 'strong' THEN 1.0
                                   WHEN 'medium' THEN 0.6
                                   ELSE 0.3
                               END
                           ) AS raw_score,
                           COUNT(*) AS event_count
                    FROM biz.catalyst_impact ci
                    JOIN core.asset a ON a.asset_id = ci.asset_id
                    JOIN biz.asset_catalyst ac ON ac.catalyst_id = ci.catalyst_id
                    WHERE ac.published_at >= NOW() - make_interval(days => %s)
                    GROUP BY ci.asset_id, a.canonical_symbol
                    HAVING SUM(
                               CASE ci.impact_direction
                                   WHEN 'bullish' THEN 1
                                   WHEN 'bearish' THEN -1
                                   ELSE 0
                               END
                               * CASE ci.impact_strength
                                   WHEN 'strong' THEN 1.0
                                   WHEN 'medium' THEN 0.6
                                   ELSE 0.3
                               END
                           ) > 0
                    ORDER BY raw_score DESC
                    LIMIT 20
                """, (window_days,))
                rows = cur.fetchall()
                results = []
                for aid, symbol, raw_score, cnt in rows:
                    # 归一化到 0-100：raw_score(Decimal) 先转 float，clip 后映射
                    try:
                        raw_f = float(raw_score)
                    except (TypeError, ValueError):
                        raw_f = 0.0
                    clipped = max(-100.0, min(100.0, raw_f * 10))
                    score = (clipped + 100.0) / 2.0
                    results.append((aid, symbol or "", round(score, 1)))
                return results
    except Exception:
        return []


def _fetch_catalyst_events(asset_id: int, window_days: int = 14, limit: int = 3) -> list[dict]:
    """获取指定资产近 N 天的催化剂事件列表，按重要性+时间排序。

    返回 [{catalyst_id, title, event_date, direction, strength, category, source, source_url, body_text}, ...]。
    同一条 catalyst 只取 impact 最强的一条（DISTINCT 去重）。
    """
    try:
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection

        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
                # 先用子查询按 catalyst_id 取 impact 最强的一条，再整体排序取 TOP N
                # （解决同一条 catalyst 因多个 horizon_days 或多条 impact 导致重复的问题）
                cur.execute("""
                    WITH ranked AS (
                        SELECT ac.catalyst_id, ac.title, ac.published_at, ac.body_text,
                               ac.event_category, ac.source_code, ac.source_url,
                               ci.impact_direction, ci.impact_strength,
                               ROW_NUMBER() OVER (
                                   PARTITION BY ac.catalyst_id
                                   ORDER BY
                                       CASE ci.impact_strength
                                           WHEN 'strong' THEN 3
                                           WHEN 'medium' THEN 2
                                           ELSE 1
                                       END DESC,
                                       ci.impact_direction DESC
                               ) AS rn
                        FROM biz.catalyst_impact ci
                        JOIN biz.asset_catalyst ac ON ac.catalyst_id = ci.catalyst_id
                        WHERE ci.asset_id = %s
                          AND ac.published_at >= NOW() - make_interval(days => %s)
                    )
                    SELECT catalyst_id, title, published_at, body_text, event_category,
                           source_code, source_url, impact_direction, impact_strength
                    FROM ranked
                    WHERE rn = 1
                    ORDER BY
                        CASE impact_strength
                            WHEN 'strong' THEN 3
                            WHEN 'medium' THEN 2
                            ELSE 1
                        END DESC,
                        published_at DESC
                    LIMIT %s
                """, (asset_id, window_days, limit))
                rows = cur.fetchall()
                return [
                    {
                        "catalyst_id": cid,
                        "title": title or "",
                        "event_date": str(published_at.date()) if published_at else "",
                        "direction": direction or "neutral",
                        "strength": strength or "weak",
                        "category": category or "",
                        "source": source_code or "",
                        "source_url": source_url or "",
                        "body_text": body_text or "",
                    }
                    for cid, title, published_at, body_text, category, source_code, source_url, direction, strength in rows
                ]
    except Exception:
        return []


def get_market_catalysts(window_days: int = 14, limit: int = 50) -> dict:
    """获取大盘级催化剂事件列表（按事件聚合，关联多个代币）。

    排序逻辑：按事件时间倒序（最新的排最上面），同分再按影响分绝对值排序。

    返回:
        {
            "catalyst_events": [
                {
                    "catalyst_id": int,
                    "title": str,
                    "event_date": str,          # YYYY-MM-DD
                    "direction": str,           # bullish / bearish / neutral
                    "strength": str,            # strong / medium / weak
                    "category": str,            # ai_event_type 或 event_category
                    "source": str,
                    "source_url": str,
                    "summary": str,             # ai_summary，可能为空
                    "related_assets": [
                        {"symbol": str, "asset_id": int, "impact_strength": str},
                        ...
                    ],
                    "n_assets": int,
                    "impact_score": float,      # 综合影响分，用于排序
                },
                ...
            ],
            "total": int,
            "window_days": int,
        }
    """
    try:
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection

        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
                # 第一步：按事件聚合，计算综合影响分，取 TOP N
                # 影响分 = strength权重 × direction符号 × 资产数因子
                # strength: strong=3, medium=2, weak=1
                # direction: bullish=+1, bearish=-1, neutral=0.3
                cur.execute("""
                    WITH event_stats AS (
                        SELECT
                            ac.catalyst_id,
                            ac.title,
                            ac.published_at,
                            COALESCE(ac.ai_event_type, ac.event_category, '') AS category,
                            ac.source_code,
                            ac.source_url,
                            COALESCE(ac.ai_summary, '') AS summary,
                            -- 主方向：取资产数量最多的那个方向
                            (
                                SELECT ci2.impact_direction
                                FROM biz.catalyst_impact ci2
                                WHERE ci2.catalyst_id = ac.catalyst_id
                                GROUP BY ci2.impact_direction
                                ORDER BY COUNT(*) DESC
                                LIMIT 1
                            ) AS main_direction,
                            -- 主强度：取最强的那个强度
                            (
                                SELECT ci2.impact_strength
                                FROM biz.catalyst_impact ci2
                                WHERE ci2.catalyst_id = ac.catalyst_id
                                ORDER BY
                                    CASE ci2.impact_strength
                                        WHEN 'strong' THEN 3
                                        WHEN 'medium' THEN 2
                                        ELSE 1
                                    END DESC
                                LIMIT 1
                            ) AS main_strength,
                            COUNT(DISTINCT ci.asset_id) AS n_assets,
                            -- 综合影响分（绝对值越大越重要）
                            SUM(
                                CASE ci.impact_strength
                                    WHEN 'strong' THEN 3.0
                                    WHEN 'medium' THEN 2.0
                                    ELSE 1.0
                                END
                                * CASE ci.impact_direction
                                    WHEN 'bullish' THEN 1.0
                                    WHEN 'bearish' THEN -1.0
                                    ELSE 0.3
                                END
                            ) AS raw_score
                        FROM biz.asset_catalyst ac
                        JOIN biz.catalyst_impact ci ON ac.catalyst_id = ci.catalyst_id
                        WHERE ac.published_at >= NOW() - INTERVAL '1 day' * %s
                          AND ac.title IS NOT NULL
                          AND ac.title <> ''
                        GROUP BY ac.catalyst_id, ac.title, ac.published_at,
                                 ac.ai_event_type, ac.event_category,
                                 ac.source_code, ac.source_url, ac.ai_summary
                    )
                    SELECT
                        catalyst_id, title, published_at, category,
                        source_code, source_url, summary,
                        main_direction, main_strength, n_assets,
                        raw_score
                    FROM event_stats
                    ORDER BY
                        -- 主排序：时间倒序，最新的事件排最上面
                        published_at DESC,
                        -- 同分：影响分绝对值越大越靠前（事件越重要）
                        ABS(raw_score) DESC
                    LIMIT %s
                """, (window_days, limit))
                rows = cur.fetchall()

                if not rows:
                    return {"catalyst_events": [], "total": 0, "window_days": window_days}

                # 第二步：批量获取每个事件的关联资产列表
                cat_ids = [r[0] for r in rows]
                placeholders = ",".join(["%s"] * len(cat_ids))
                cur.execute(f"""
                    SELECT ci.catalyst_id, ci.asset_id, a.canonical_symbol,
                           ci.impact_strength, ci.impact_direction
                    FROM biz.catalyst_impact ci
                    JOIN core.asset a ON a.asset_id = ci.asset_id
                    WHERE ci.catalyst_id IN ({placeholders})
                      AND a.canonical_name NOT LIKE '%%Bridged%%'
                      AND a.canonical_name NOT LIKE '%%Wrapped%%'
                      AND a.canonical_name NOT LIKE 'Peg %%'
                    ORDER BY
                        ci.catalyst_id,
                        CASE ci.impact_strength
                            WHEN 'strong' THEN 1
                            WHEN 'medium' THEN 2
                            ELSE 3
                        END,
                        a.canonical_symbol
                """, tuple(cat_ids))
                asset_rows = cur.fetchall()

                # 组装 {catalyst_id: [assets]}
                assets_by_cat: dict[int, list[dict]] = {}
                for cid, aid, symbol, istr, idir in asset_rows:
                    if cid not in assets_by_cat:
                        assets_by_cat[cid] = []
                    assets_by_cat[cid].append({
                        "symbol": symbol or "",
                        "asset_id": int(aid),
                        "impact_strength": istr or "weak",
                        "impact_direction": idir or "neutral",
                    })

                # 组装最终结果
                events = []
                for (cid, title, pub_at, category, src, src_url, summary,
                     mdir, mstr, n_assets, raw_score) in rows:
                    events.append({
                        "catalyst_id": int(cid),
                        "title": title or "",
                        "event_date": str(pub_at.date()) if pub_at else "",
                        "direction": mdir or "neutral",
                        "strength": mstr or "weak",
                        "category": category or "",
                        "source": src or "",
                        "source_url": src_url or "",
                        "summary": summary or "",
                        "related_assets": assets_by_cat.get(int(cid), []),
                        "n_assets": int(n_assets),
                        "impact_score": float(raw_score or 0),
                    })

                return {
                    "catalyst_events": events,
                    "total": len(events),
                    "window_days": window_days,
                }
    except Exception as e:
        return {"catalyst_events": [], "total": 0, "window_days": window_days,
                "error": str(e)}


def get_onchain_anomalies(hours: int = 24, limit: int = 20) -> dict:
    """获取链上异动信号（KOL 分析博主发现的链上大额转账/爆仓/吸筹等）。

    排序：按 event_usd_value 倒序（金额越大越靠前），其次按时间倒序。
    筛选：只取 signal_category = 'onchain' 的链上异动（排除普通交易信号）。

    返回:
        {
            "anomalies": [
                {
                    "signal_id": int,
                    "symbol": str,
                    "direction": str,           # long / short / neutral / outflow / inflow ...
                    "event_type": str,          # exchange_flow / whale_move / smart_money / liquidation / ...
                    "event_usd_value": float,
                    "event_amount": float,
                    "event_token": str,
                    "event_exchange": str,
                    "address_label": str,
                    "event_direction": str,     # inflow / outflow / accumulation / distribution ...
                    "kol_name": str,
                    "content_text": str,
                    "post_url": str,
                    "created_at": str,          # ISO datetime
                },
                ...
            ],
            "total": int,
            "hours": int,
            "n_kols": int,
        }
    """
    try:
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection

        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        ks.signal_id,
                        ks.symbol,
                        COALESCE(ks.direction, '') as direction,
                        COALESCE(ks.signal_subtype, ks.signal_category, '') as event_type,
                        ks.event_usd_value,
                        ks.event_amount,
                        ks.event_token,
                        COALESCE(ks.event_exchange, '') as event_exchange,
                        COALESCE(ks.address_label, '') as address_label,
                        COALESCE(ks.event_direction, '') as event_direction,
                        kp.nickname as kol_name,
                        COALESCE(kp2.content_text, '') as content_text,
                        COALESCE(kp2.post_url, '') as post_url,
                        ks.created_at,
                        kp.win_rate,
                        kp.total_signals
                    FROM biz.kol_signal ks
                    JOIN biz.kol_profile kp ON ks.profile_id = kp.profile_id
                    JOIN biz.kol_post kp2 ON ks.post_id = kp2.post_id
                    WHERE ks.created_at >= NOW() - (%s || ' hours')::interval
                      AND ks.signal_category = 'onchain'
                      AND ks.symbol IS NOT NULL
                    ORDER BY
                        ks.event_usd_value DESC NULLS LAST,
                        ks.created_at DESC
                    LIMIT %s
                """, (hours, limit))
                rows = cur.fetchall()

                anomalies = []
                kol_set = set()
                for (sid, symbol, direction, event_type, usd_val, amt, token,
                     exch, addr_label, evt_dir, kol_name, content, post_url,
                     created_at, win_rate, total_sigs) in rows:
                    kol_set.add(kol_name)
                    anomalies.append({
                        "signal_id": int(sid),
                        "symbol": symbol or "",
                        "direction": direction or "",
                        "event_type": event_type or "",
                        "event_usd_value": float(usd_val) if usd_val is not None else 0.0,
                        "event_amount": float(amt) if amt is not None else 0.0,
                        "event_token": token or "",
                        "event_exchange": exch or "",
                        "address_label": addr_label or "",
                        "event_direction": evt_dir or "",
                        "kol_name": kol_name or "",
                        "kol_win_rate": float(win_rate) if win_rate is not None else None,
                        "kol_total_signals": int(total_sigs) if total_sigs is not None else 0,
                        "content_text": content or "",
                        "post_url": post_url or "",
                        "created_at": str(created_at) if created_at else "",
                    })

                return {
                    "anomalies": anomalies,
                    "total": len(anomalies),
                    "hours": hours,
                    "n_kols": len(kol_set),
                }
    except Exception as e:
        return {"anomalies": [], "total": 0, "hours": hours, "n_kols": 0,
                "error": str(e)}


def _recent_whale_flow_targets(
    hours: float = 24, usd_min: float = 1_000_000, limit: int = 5,
) -> list[tuple]:
    """近 N 小时链上巨鲸大额转账（onchain_transfer_log），按 asset 聚合。

    返回 [(asset_id, symbol, usd_total, n_tx, max_value_usd, is_to_exchange, chain), ...]。
    失败返回 []（降级不崩）。
    """
    try:
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection

        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT t.asset_id, a.canonical_symbol,
                           SUM(t.value_usd) AS usd_total,
                           COUNT(*) AS n_tx,
                           MAX(t.value_usd) AS max_val,
                           BOOL_OR(t.is_to_exchange) AS any_to_ex,
                           MAX(t.chain) AS chain
                    FROM biz.onchain_transfer_log t
                    JOIN core.asset a ON a.asset_id = t.asset_id
                    WHERE (t.is_to_exchange OR t.from_exchange IS NOT NULL)
                      AND (t.is_suspect IS NOT TRUE OR t.is_suspect IS NULL)
                      AND t.value_usd >= %s
                      AND t.block_timestamp >= NOW() - make_interval(hours => %s)
                    GROUP BY t.asset_id, a.canonical_symbol
                    ORDER BY usd_total DESC
                    LIMIT %s
                """, (usd_min, int(hours), limit))
                rows = cur.fetchall()
                return [
                    (
                        r[0], r[1] or "?", float(r[2] or 0), int(r[3] or 0),
                        float(r[4] or 0), bool(r[5]), r[6] or "",
                    )
                    for r in rows
                ]
    except Exception:
        return []


def _github_activity_targets(
    window_days: int = 60, burst_ratio: float = 1.5,
    decline_ratio: float = 0.5, limit: int = 5,
) -> list[tuple]:
    """GitHub dev 活跃异动：weekly_commit_counts 末 4 周 vs 前 4 周。

    返回 [(asset_id, symbol, last4, prev4, ratio, direction), ...]。
    direction: 'burst'(last4 > burst_ratio*prev4) / 'decline'(last4 < decline_ratio*prev4)。
    失败返回 []。
    """
    try:
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection
        from datetime import datetime, timezone

        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT g.asset_id, a.canonical_symbol,
                           r.weekly_commit_counts
                    FROM biz.github_repo_activity r
                    JOIN biz.asset_github_repo g
                      ON g.owner_login = r.owner_login AND g.repo_name = r.repo_name
                    JOIN core.asset a ON a.asset_id = g.asset_id
                    WHERE r.fetched_at >= NOW() - make_interval(days => %s)
                    ORDER BY r.fetched_at DESC
                """, (window_days,))
                rows = cur.fetchall()

        results: list[tuple] = []
        for asset_id, symbol, weekly in rows:
            if not weekly or not isinstance(weekly, list) or len(weekly) < 8:
                continue
            vals = []
            for v in weekly[-8:]:
                try:
                    vals.append(int(float(v)))
                except (TypeError, ValueError):
                    vals.append(0)
            last4 = sum(vals[-4:])
            prev4 = sum(vals[:4])
            if prev4 <= 0:
                continue
            ratio = last4 / prev4
            if last4 > burst_ratio * prev4:
                direction = "burst"
            elif last4 < decline_ratio * prev4:
                direction = "decline"
            else:
                continue
            results.append((asset_id, symbol or "?", last4, prev4, round(ratio, 2), direction))
            if len(results) >= limit:
                break
        return results
    except Exception:
        return []


def _recent_raises(
    window_days: int = 90, limit: int = 5,
) -> list[tuple]:
    """近期融资落地（asset_raises）。返回 [(asset_id, symbol, round, amount_m, lead, raise_date, protocol_name), ...]。

    amount 单位为百万美元（实测：Crypto.com 400 = $400M）；协议名用于 symbol 缺失兜底。
    """
    try:
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection

        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT r.asset_id, a.canonical_symbol,
                           r.round, r.amount, r.lead_investors, r.raise_date, r.protocol_name
                    FROM biz.asset_raises r
                    JOIN core.asset a ON a.asset_id = r.asset_id
                    WHERE r.raise_date >= NOW() - make_interval(days => %s)
                    ORDER BY r.raise_date DESC
                    LIMIT %s
                """, (window_days, limit))
                rows = cur.fetchall()
        results = []
        for r in rows:
            lead = r[4]
            if isinstance(lead, (list, tuple)):
                lead = ", ".join(str(x) for x in lead[:2])
            amount_m = float(r[3]) if r[3] is not None else None
            results.append((r[0], r[1] or "", r[2] or "", amount_m, lead or "", str(r[5] or ""), r[6] or ""))
        return results
    except Exception:
        return []


def _upcoming_unlocks(
    window_days: int = 14, ratio_min: float = 1.0, limit: int = 5,
) -> list[tuple]:
    """未来 N 天代币解锁（asset_unlock_event，含解锁价值与市值占比）。

    返回 [(asset_id, symbol, unlock_value_usd, unlock_date, ratio_mcap), ...]。
    失败返回 []。
    """
    try:
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection

        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT u.asset_id, a.canonical_symbol,
                           u.unlock_value_usd, u.unlock_date, u.unlock_ratio_mcap
                    FROM biz.asset_unlock_event u
                    JOIN core.asset a ON a.asset_id = u.asset_id
                    WHERE u.unlock_date BETWEEN NOW() AND NOW() + make_interval(days => %s)
                      AND u.unlock_value_usd IS NOT NULL
                    ORDER BY u.unlock_value_usd DESC
                    LIMIT %s
                """, (window_days, limit))
                rows = cur.fetchall()
        results = []
        for r in rows:
            ratio_mcap = float(r[4]) if r[4] is not None else 0.0
            if ratio_mcap <= 0:
                continue
            results.append((r[0], r[1] or "?", float(r[2] or 0), str(r[3] or ""), ratio_mcap))
        return results
    except Exception:
        return []


def _kol_onchain_signals(
    days: int = 7, limit: int = 10,
) -> list[dict]:
    """KOL 链上异动情报（kol_signal，onchain 类）。

    返回 [dict, ...]，dict 包含：
        asset_id, symbol, subtype, usd_value, exchange, profile_id,
        kol_name, direction, address_label, event_direction, content_text, post_url
    """
    try:
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection

        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT k.asset_id, COALESCE(k.symbol, a.canonical_symbol),
                           k.signal_subtype, k.event_usd_value, k.event_exchange,
                           k.profile_id, p.nickname,
                           k.direction, k.address_label, k.event_direction,
                           COALESCE(post.content_text, ''), COALESCE(post.post_url, '')
                    FROM biz.kol_signal k
                    LEFT JOIN biz.kol_profile p ON p.profile_id = k.profile_id
                    LEFT JOIN biz.kol_post post ON post.post_id = k.post_id
                    LEFT JOIN core.asset a ON a.asset_id = k.asset_id
                    WHERE k.signal_category = 'onchain'
                      AND k.symbol IS NOT NULL
                      AND k.created_at >= NOW() - (%s || ' days')::interval
                    ORDER BY COALESCE(k.event_usd_value, 0) DESC
                    LIMIT %s
                """, (days, limit))
                rows = cur.fetchall()
        results = []
        for r in rows:
            if r[0] is None:
                continue
            usd_val = float(r[3]) if r[3] is not None else 0.0
            results.append({
                "asset_id": r[0],
                "symbol": r[1] or "?",
                "subtype": r[2] or "",
                "usd_value": usd_val,
                "exchange": r[4] or "",
                "profile_id": r[5] or "",
                "kol_name": r[6] or "",
                "direction": r[7] or "",
                "address_label": r[8] or "",
                "event_direction": r[9] or "",
                "content_text": r[10] or "",
                "post_url": r[11] or "",
            })
        return results
    except Exception:
        return []


# ══════════════════════════════════════════════════════════════
# 批④ 数据驱动层：Meme 五维标签 / 4 烟囱 / 深加工 / 聪明钱背离
# ══════════════════════════════════════════════════════════════

def fetch_meme_risk_summary(limit_per_bucket: int = 5) -> dict:
    """
    P0-4：读取 biz.asset_risk_labels，按风险等级聚合 Meme 五维标签池。
    返回 {status, count, buckets, summary}。
    """
    try:
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection
        import psycopg.rows

        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute("""
                    SELECT a.canonical_symbol AS symbol,
                           a.canonical_name AS name,
                           a.primary_sector AS sector,
                           r.total_score, r.risk_label, r.axes_computed,
                           r.contract_label, r.liquidity_label, r.holder_label,
                           r.lifecycle_label, r.social_label,
                           r.flags, r.computed_at
                    FROM biz.asset_risk_labels r
                    JOIN core.asset a ON a.asset_id = r.asset_id
                    WHERE a.status = 'active'
                      AND r.risk_label IS NOT NULL
                    ORDER BY r.total_score DESC
                    LIMIT 500
                """)
                rows = cur.fetchall()

        if not rows:
            return {"status": "empty", "count": 0, "buckets": {}, "summary": {}}

        buckets: dict[str, list[dict]] = {
            "block": [], "high": [], "medium": [], "low": [], "unknown": []
        }
        for r in rows:
            label = r.get("risk_label") or "unknown"
            buckets.setdefault(label, []).append({
                "symbol": r.get("symbol"),
                "name": r.get("name"),
                "sector": r.get("sector"),
                "total_score": float(r["total_score"]) if r.get("total_score") is not None else None,
                "axes_computed": r.get("axes_computed"),
                "contract_label": r.get("contract_label"),
                "liquidity_label": r.get("liquidity_label"),
                "holder_label": r.get("holder_label"),
                "lifecycle_label": r.get("lifecycle_label"),
                "social_label": r.get("social_label"),
                "flags": r.get("flags") or [],
            })

        return {
            "status": "ok",
            "count": len(rows),
            "buckets": {k: v[:limit_per_bucket] for k, v in buckets.items()},
            "summary": {k: len(v) for k, v in buckets.items()},
        }
    except Exception as e:
        return {"status": "error", "error": str(e), "count": 0, "buckets": {}, "summary": {}}


def _recent_hacks(window_days: int = 14, limit: int = 5) -> list[tuple]:
    """P1-3：近期黑客/安全事件（biz.asset_hacks）。返回 [(asset_id, symbol, name, amount, hack_date, technique), ...]。"""
    try:
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection

        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT h.asset_id, a.canonical_symbol,
                           h.name, h.amount, h.hack_date, h.technique
                    FROM biz.asset_hacks h
                    LEFT JOIN core.asset a ON a.asset_id = h.asset_id
                    WHERE h.hack_date >= NOW() - make_interval(days => %s)
                      AND h.amount IS NOT NULL
                    ORDER BY h.amount DESC
                    LIMIT %s
                """, (window_days, limit))
                rows = cur.fetchall()
        return [
            (r[0], r[1] or "?", r[2] or "?", float(r[3]) if r[3] is not None else 0.0,
             str(r[4]) if r[4] else "", r[5] or "")
            for r in rows
        ]
    except Exception:
        return []


def build_chimney_signals(overview: dict) -> dict:
    """
    P1-3：四烟囱信号消费层（TVL / GitHub / 融资 / 黑客）。
    TVL 取自 overview 已计算的 narrative_tvl_flow / chain_flow；
    GitHub / 融资 / 黑客直接读库。
    返回 {status, tvl, github, funding, hacks}。
    """
    status_parts: list[str] = []

    # ── TVL 异动 ──
    tvl_signals: list[dict] = []
    try:
        d5 = (overview.get("dimensions") or {}).get("5板块") or {}
        d5data = d5.get("data") or {}
        tvl_cats = (d5data.get("narrative_tvl_flow") or {}).get("categories") or {}
        chain_flow = (d5data.get("chain_flow_ranking") or {}).get("ranked") or []

        cat_items = []
        for cat, info in (tvl_cats or {}).items():
            chg = info.get("tvl_change_7d_pct")
            if chg is None:
                continue
            cat_items.append({
                "category": cat,
                "tvl_change_7d_pct": chg,
                "tvl_usd": info.get("tvl"),
                "protocols": info.get("protocols"),
            })
        cat_items.sort(key=lambda x: abs(x["tvl_change_7d_pct"]), reverse=True)

        chain_items = []
        for row in chain_flow:
            flow_pct = row.get("flow_7d_pct")
            if flow_pct is None:
                continue
            chain_items.append({
                "chain": row.get("chain"),
                "flow_7d_pct": flow_pct,
                "flow_7d_usd": row.get("flow_7d"),
                "tvl": row.get("tvl"),
            })
        chain_items.sort(key=lambda x: abs(x["flow_7d_pct"]), reverse=True)

        if cat_items or chain_items:
            tvl_signals = [
                {"type": "category", "items": cat_items[:3]},
                {"type": "chain", "items": chain_items[:3]},
            ]
    except Exception as e:
        tvl_signals = [{"type": "error", "error": str(e)}]

    if tvl_signals:
        status_parts.append("tvl")

    # ── GitHub dev 活跃 ──
    github_signals: list[dict] = []
    try:
        for gt in _github_activity_targets(window_days=60, burst_ratio=1.5, decline_ratio=0.5, limit=5):
            aid, symbol, last4, prev4, ratio, direction = gt
            github_signals.append({
                "asset_id": aid,
                "symbol": symbol,
                "direction": direction,
                "last4_commits": last4,
                "prev4_commits": prev4,
                "ratio": ratio,
            })
    except Exception:
        pass
    if github_signals:
        status_parts.append("github")

    # ── 融资落地 ──
    funding_signals: list[dict] = []
    try:
        for rt in _recent_raises(window_days=90, limit=5):
            aid, symbol, rnd, amount_m, lead, rdate, proto = rt
            funding_signals.append({
                "asset_id": aid,
                "symbol": symbol,
                "round": rnd,
                "amount_m": amount_m,
                "lead": lead,
                "date": rdate,
                "protocol": proto,
            })
    except Exception:
        pass
    if funding_signals:
        status_parts.append("funding")

    # ── 黑客事件 ──
    hack_signals: list[dict] = []
    try:
        for ht in _recent_hacks(window_days=14, limit=5):
            aid, symbol, name, amount, hdate, technique = ht
            hack_signals.append({
                "asset_id": aid,
                "symbol": symbol,
                "name": name,
                "amount_usd": amount,
                "date": hdate,
                "technique": technique,
            })
    except Exception:
        pass
    if hack_signals:
        status_parts.append("hacks")

    status = "ok" if status_parts else "empty"
    if any(isinstance(s, dict) and s.get("type") == "error" for s in tvl_signals):
        status = "partial"

    return {
        "status": status,
        "available": status_parts,
        "tvl": tvl_signals,
        "github": github_signals,
        "funding": funding_signals,
        "hacks": hack_signals,
    }


def build_institutional_mvrv_summary(overview: dict) -> dict:
    """
    P2：深加工——机构净流结构 + MVRV 分层 + 可操作建议。
    """
    etf_flows = ((overview.get("dimensions") or {}).get("4机构") or {}).get("data") or {}
    onchain = overview.get("onchain_anomaly_signals") or {}
    ex_net = (onchain.get("exchange_netflow") or {}) if isinstance(onchain, dict) else {}

    etf_net = etf_flows.get("net_flow_usd_m")
    cex_netflow_7d = ex_net.get("netflow_7d_usd") if isinstance(ex_net, dict) else None

    # 机构净流定性
    inst_bias = "neutral"
    if etf_net is not None and cex_netflow_7d is not None:
        if etf_net > 100 and cex_netflow_7d > 200_000_000:
            inst_bias = "accumulation"
        elif etf_net < -100 and cex_netflow_7d < -200_000_000:
            inst_bias = "distribution"
    elif etf_net is not None:
        inst_bias = "accumulation" if etf_net > 100 else "distribution" if etf_net < -100 else "neutral"
    elif cex_netflow_7d is not None:
        inst_bias = "accumulation" if cex_netflow_7d > 200_000_000 else "distribution" if cex_netflow_7d < -200_000_000 else "neutral"

    # MVRV 分层
    mvrv = ((overview.get("dimensions") or {}).get("mvrv_universe") or {}).get("data") or {}
    coins = mvrv.get("coins") or []

    deep_under = [c for c in coins if c.get("pct_full") is not None and c.get("pct_full", 100) <= 15]
    under = [c for c in coins if 15 < c.get("pct_full", 100) <= 30]
    fair = [c for c in coins if 30 < c.get("pct_full", 100) < 85]
    over = [c for c in coins if c.get("pct_full", 100) >= 85]

    def _coin_summary(c):
        return {
            "symbol": c.get("symbol"),
            "pct_full": c.get("pct_full"),
            "value": c.get("value"),
        }

    mvrv_layers = {
        "deep_under": {"count": len(deep_under), "coins": [_coin_summary(c) for c in deep_under[:3]]},
        "under": {"count": len(under), "coins": [_coin_summary(c) for c in under[:3]]},
        "fair": {"count": len(fair)},
        "overvalued": {"count": len(over), "coins": [_coin_summary(c) for c in over[:3]]},
    }

    # 可操作建议
    actions: list[str] = []
    if inst_bias == "accumulation":
        actions.append("机构+CEX 同步净流入 → 中线偏多，关注回调加仓")
    elif inst_bias == "distribution":
        actions.append("机构+CEX 同步净流出 → 降低敞口，警惕高位回落")
    if deep_under:
        actions.append(f"{len(deep_under)} 个标的 MVRV 深度低估 → 左侧分批关注")
    if over:
        actions.append(f"{len(over)} 个标的 MVRV 高估 → 不追高中线止盈")

    return {
        "status": "ok" if coins else "partial",
        "institutional": {
            "etf_net_flow_usd_m": etf_net,
            "cex_netflow_7d_usd": cex_netflow_7d,
            "bias": inst_bias,
        },
        "mvrv_layers": mvrv_layers,
        "actionable_hints": actions,
    }


def build_smart_money_divergence(overview: dict, max_assets: int = 20) -> dict:
    """
    P1-1：聪明钱背离段。遍历 opportunity_list 中含 asset_id 的标的，
    调用 db_stats.get_divergence_signals，聚合 bullish/bearish 信号。
    硬依赖：MEME-01 修复后 onchain_holder_snapshot 4 列非空。
    """
    opps = (overview.get("opportunity_list") or {}).get("opportunities") or []
    asset_ids: list[int] = []
    seen: set[int] = set()
    for o in opps:
        aid = o.get("asset_id")
        if isinstance(aid, int) and aid not in seen:
            asset_ids.append(aid)
            seen.add(aid)
    asset_ids = asset_ids[:max_assets]

    if not asset_ids:
        return {"status": "empty", "bullish": [], "bearish": [], "count": 0}

    try:
        from db_stats import get_divergence_signals
    except Exception as e:
        return {"status": "error", "error": str(e), "bullish": [], "bearish": [], "count": 0}

    bullish: list[dict] = []
    bearish: list[dict] = []
    for aid in asset_ids:
        try:
            res = get_divergence_signals(aid)
            if not res.get("ok"):
                continue
            symbol = res.get("symbol") or "?"
            for sig in res.get("signals") or []:
                entry = {
                    "asset_id": aid,
                    "symbol": symbol,
                    "type": sig.get("type"),
                    "label": sig.get("label"),
                    "severity": sig.get("severity"),
                    "confidence": sig.get("confidence"),
                    "description": sig.get("description"),
                }
                if sig.get("type") == "bullish_divergence":
                    bullish.append(entry)
                elif sig.get("type") == "bearish_divergence":
                    bearish.append(entry)
        except Exception:
            continue

    status = "ok" if (bullish or bearish) else "empty"
    return {
        "status": status,
        "bullish": bullish,
        "bearish": bearish,
        "count": len(bullish) + len(bearish),
    }


# ══════════════════════════════════════════════════════════════
# FEAT-HIGHLIGHT-002：高亮信号精选
# ══════════════════════════════════════════════════════════════

def select_highlight_signals(opportunities: list[dict], max_total: int = 10,
                              min_resonance: int = 2) -> list[dict]:
    """从全部机会中精选高亮信号：HIGH 优先 + 共振筛选 + 类型配额（FEAT-HIGHLIGHT-002）。

    处理流程：
    1. 全量按分数排序取候选池
    2. 同标的合并：同一 target 的多条信号合并为一张卡片，主卡为分数最高的
    3. 共振筛选：币种级 target（如 BTC/ETH）需 ≥ min_resonance 种不同 signal_type 才入选
       宏观/聚合类 target 不受共振限制（如"恐贪指数极度恐惧"）
    4. 按类型配额限制每种卡片数量（以主卡 signal_type 为准）
    5. 按分数降序，限制 max_total
    """
    import re
    _symbol_re = re.compile(r'^[A-Z0-9]{2,10}$')

    def _is_symbol_target(tgt: str) -> bool:
        """判断 target 是否为具体币种 symbol（如 BTC、ETH、SOL）。"""
        t = (tgt or "").strip()
        if not t:
            return False
        return bool(_symbol_re.match(t))

    # V2：配额适度放宽（给 AI 更多候选，最终排序由 AI 评分决定）
    # 旧配额已废弃，新配额约为原来的 1.5 倍
    quotas = {
        "mvrv_deep_under": 3, "mvrv_under_watch": 2,
        "catalyst": 3, "whale_flow": 3, "github_activity": 2,
        "funding": 2, "token_unlock": 2, "kol_onchain": 2,
        "fng_extreme": 2, "leverage_extreme": 2, "stablecoin_inflow": 2,
        "etf_flow": 2,
        "price_surge": 3, "price_volume_surge": 3, "volume_surge": 2,
        "sector_inflow": 2,
        "__default__": 2,
    }

    # ── 1. 筛选多头方向信号（高亮信号 = 机会/看多） ──
    # FEAT-HIGHLIGHT-FIX: 严格过滤 direction，避免空头信号混进机会区
    long_opps = [
        o for o in opportunities
        if o.get("direction") in ("long", None, "", "bullish")
        and o.get("signal_type") not in ("mvrv_deep_over", "mvrv_over_watch")
    ]

    if not long_opps:
        return []

    # ── 2. 全量按排序取候选池 ──
    def _sort_key(o):
        is_high = 1 if o.get("conviction_tier") == "HIGH" else 0
        # FEAT-HORIZON-001: 优先用衰减后分数（含估值过滤器），否则用原始分
        score = o.get("decayed_score")
        if score is None:
            score = o.get("conviction_score", 0) or 0
        resonance = len(o.get("related_dims", []) or [])
        is_new = 1 if o.get("is_new_today") else 0
        return (is_high, is_new, resonance, score)

    # 取足够多的候选，确保合并和筛选后还有量
    candidates = sorted(long_opps, key=_sort_key, reverse=True)[:max_total * 10]

    # ── 2. 同标的合并：同一 target 聚合成一张卡片 ──
    merged_map: dict[str, dict] = {}  # target_lower -> merged_opp
    merged_order: list[str] = []
    for o in candidates:
        tgt = (o.get("target") or "").strip()
        tgt_key = tgt.lower()
        if not tgt_key:
            continue
        if tgt_key not in merged_map:
            merged = dict(o)  # 浅拷贝主卡（分数最高的）
            merged["all_signals"] = [o]
            merged_map[tgt_key] = merged
            merged_order.append(tgt_key)
        else:
            merged = merged_map[tgt_key]
            merged["all_signals"].append(o)
            # 合并相关维度（去重）
            all_dims = list(dict.fromkeys(
                (merged.get("related_dims") or []) + (o.get("related_dims") or [])
            ))
            merged["related_dims"] = all_dims
            # 主卡分数取最高的
            if o.get("conviction_score", 0) > merged.get("conviction_score", 0):
                merged["conviction_score"] = o["conviction_score"]
                merged["conviction_tier"] = o.get("conviction_tier", merged.get("conviction_tier"))

    # 计算每张合并卡的共振维度数（不同 signal_type 数量）
    for k, merged in merged_map.items():
        sig_types = list(dict.fromkeys(
            s.get("signal_type") for s in merged.get("all_signals", [])
            if s.get("signal_type")
        ))
        merged["resonance_count"] = len(sig_types)
        merged["signal_types"] = sig_types

    # ── 3. 共振筛选：币种级 target 需满足 min_resonance ──
    after_resonance: list[dict] = []
    for k in merged_order:
        merged = merged_map[k]
        tgt = merged.get("target", "")
        res_count = merged.get("resonance_count", 1)
        if _is_symbol_target(tgt) and res_count < min_resonance:
            continue  # 币种级信号不满足共振条件，跳过
        after_resonance.append(merged)

    # 按主卡分数降序
    after_resonance.sort(key=lambda x: x.get("conviction_score", 0), reverse=True)

    # ── 4. 按类型配额限制（配额是合并后的卡片数，不是信号数） ──
    type_counts: dict[str, int] = {}
    result: list[dict] = []
    for merged in after_resonance:
        st = merged.get("signal_type") or "__default__"
        q = quotas.get(st, quotas["__default__"])
        if type_counts.get(st, 0) >= q:
            continue
        type_counts[st] = type_counts.get(st, 0) + 1
        result.append(merged)
        if len(result) >= max_total:
            break

    return result


# FEAT-RISK-001：高危信号精选
# ══════════════════════════════════════════════════════════════

def select_risk_signals(opportunities: list[dict], max_total: int = 8,
                         min_resonance: int = 1) -> list[dict]:
    """从全部机会中精选高危（看空）信号：HIGH 优先 + 共振筛选 + 类型配额。

    对称于 select_highlight_signals，但聚焦 direction=short / 风险类信号。

    处理流程：
    1. 筛选 direction == 'short' 或风险类 signal_type
    2. 全量按风险强度排序（解锁金额/巨鲸额/MVRV百分位/置信分数）
    3. 同标的合并：同一 target 的多条风险合并为一张卡片
    4. 共振筛选：币种级 target 需 ≥ min_resonance 种不同 signal_type 才入选
       宏观/聚合类 target 不受共振限制
    5. 按类型配额限制
    6. 限制 max_total
    """
    import re
    _symbol_re = re.compile(r'^[A-Z0-9]{2,10}$')

    def _is_symbol_target(tgt: str) -> bool:
        t = (tgt or "").strip()
        if not t:
            return False
        return bool(_symbol_re.match(t))

    # 风险信号类型配额（合并后的卡片数）
    # V2：配额适度放宽（给 AI 更多候选，最终排序由 AI 评分决定）
    quotas = {
        "token_unlock": 3,
        "whale_flow": 3,
        "mvrv_deep_over": 3,
        "mvrv_over_watch": 2,
        "fng_extreme": 2,
        "leverage_extreme": 2,
        "github_activity": 2,
        "catalyst": 2,
        "price_crash": 3,
        "sector_outflow": 2,
        "__default__": 2,
    }

    # ── 1. 筛选风险类信号 ──
    risk_types = {
        "token_unlock", "whale_flow", "mvrv_deep_over", "mvrv_over_watch",
        "fng_extreme", "leverage_extreme",
        # FEAT-SIGNAL-SRC 新增
        "price_crash", "sector_outflow",
    }
    risk_opps = [
        o for o in opportunities
        if o.get("direction") == "short" or o.get("signal_type") in risk_types
    ]

    if not risk_opps:
        return []

    # ── 2. 按风险强度排序 ──
    def _risk_sort_key(o):
        is_high = 1 if o.get("conviction_tier") == "HIGH" else 0
        # FEAT-HORIZON-001: 优先用衰减后分数（含估值过滤器）
        score = o.get("decayed_score")
        if score is None:
            score = o.get("conviction_score", 0) or 0
        resonance = len(o.get("related_dims", []) or [])
        is_new = 1 if o.get("is_new_today") else 0
        # 解锁优先（确定性高）
        is_unlock = 1 if o.get("signal_type") == "token_unlock" else 0
        return (is_unlock, is_high, is_new, resonance, score)

    candidates = sorted(risk_opps, key=_risk_sort_key, reverse=True)[:max_total * 10]

    # ── 3. 同标的合并 ──
    merged_map: dict[str, dict] = {}
    merged_order: list[str] = []
    for o in candidates:
        tgt = (o.get("target") or "").strip()
        tgt_key = tgt.lower()
        if not tgt_key:
            continue
        if tgt_key not in merged_map:
            merged = dict(o)
            merged["all_signals"] = [o]
            merged_map[tgt_key] = merged
            merged_order.append(tgt_key)
        else:
            merged = merged_map[tgt_key]
            merged["all_signals"].append(o)
            all_dims = list(dict.fromkeys(
                (merged.get("related_dims") or []) + (o.get("related_dims") or [])
            ))
            merged["related_dims"] = all_dims
            if o.get("conviction_score", 0) > merged.get("conviction_score", 0):
                merged["conviction_score"] = o["conviction_score"]
                merged["conviction_tier"] = o.get("conviction_tier", merged.get("conviction_tier"))

    for k, merged in merged_map.items():
        sig_types = list(dict.fromkeys(
            s.get("signal_type") for s in merged.get("all_signals", [])
            if s.get("signal_type")
        ))
        merged["resonance_count"] = len(sig_types)
        merged["signal_types"] = sig_types

    # ── 4. 共振筛选 ──
    after_resonance: list[dict] = []
    for k in merged_order:
        merged = merged_map[k]
        tgt = merged.get("target", "")
        res_count = merged.get("resonance_count", 1)
        if _is_symbol_target(tgt) and res_count < min_resonance:
            continue
        after_resonance.append(merged)

    after_resonance.sort(key=lambda x: x.get("conviction_score", 0), reverse=True)

    # ── 5. 类型配额 ──
    type_counts: dict[str, int] = {}
    result: list[dict] = []
    for merged in after_resonance:
        st = merged.get("signal_type") or "__default__"
        q = quotas.get(st, quotas["__default__"])
        if type_counts.get(st, 0) >= q:
            continue
        type_counts[st] = type_counts.get(st, 0) + 1
        result.append(merged)
        if len(result) >= max_total:
            break

    return result


def _enrich_highlights_with_ai_v2(signals: list[dict], all_opps: list[dict],
                                  direction: str = "long", max_ai: int = 6) -> list[dict]:
    """
    给高亮/风险信号列表补上 asset_id 并做 V2 AI 增强。
    与 score_opportunities 中的 AI 增强逻辑保持一致。
    失败静默，返回原列表。
    """
    if not signals:
        return signals

    try:
        from ai_signal_analyzer import ai_enrich_signals_v2

        # 1. 先补 asset_id（从 opps 里带 asset_id 的信号里找）
        symbol_to_asset: dict[str, int] = {}
        for o in all_opps:
            aid = o.get("asset_id")
            tgt = (o.get("target") or "").upper().strip()
            if aid and tgt:
                symbol_to_asset[tgt] = int(aid)

        for sig in signals:
            if not sig.get("asset_id") and sig.get("target"):
                sym = str(sig["target"]).upper().strip()
                if sym in symbol_to_asset:
                    sig["asset_id"] = symbol_to_asset[sym]

        # 2. V2 AI 增强
        signals = ai_enrich_signals_v2(signals, direction=direction, max_ai_review=max_ai)
    except Exception:
        pass  # AI 增强失败不影响主流程

    return signals


def score_opportunities(overview: dict) -> dict:
    """
    聚合 P1-1~P1-3 + P0-3 真实字段合成机会清单。
    返回 {status, opportunities: [{target, direction, confidence, conviction_score,
          conviction_tier, trigger_logic, related_dims}], excluded, degraded}。
    任一上游信号缺失 → 该机会剔除/降级，不崩溃。

    P0 改进：
    - P0-1: MVRV 极值 → 估值回归机会类（按标的 symbol 匹配 MVRV 百分位）
    - P0-2: BTC 周期相位 → conviction 调制器
    - P0-3: 复合 conviction 分 (0-100) + tier (HIGH/MED/LOW，LOW 剔除)
    """
    # 第四刀：从 yaml 规则读阈值（而非 in-code 常量），yaml 失败兜底默认值
    t = _load_market_rules().get("opportunity_thresholds", dict(OPPORTUNITY_THRESHOLDS_DEFAULT))
    d5 = (overview.get("dimensions") or {}).get("5板块") or {}
    d5data = d5.get("data") or {}
    narrative = d5data.get("narrative_flow_ranking", {}).get("ranked", []) or []
    chains = d5data.get("chain_flow_ranking", {}).get("ranked", []) or []
    divergence = (overview.get("divergence_signals") or {}).get("signals", []) or []
    emotion = ((overview.get("summary") or {}).get("emotion_subscore") or {})
    onchain = overview.get("onchain_anomaly_signals")  # P1-3（可能未接入）
    # CM Community BTC 链上指标（替代死链路 CoinGlass）
    d6 = (overview.get("dimensions") or {}).get("6链上") or {}
    btc_onchain = d6.get("data") or {}
    by_sig = {s.get("signal"): s for s in divergence}

    # P0-1: MVRV 多币极值数据
    mvrv_dim = (overview.get("dimensions") or {}).get("mvrv_universe") or {}
    mvrv_data = mvrv_dim.get("data") or {}
    mvrv_coins = mvrv_data.get("coins") or []
    mvrv_map = {str(c.get("symbol", "")).lower(): c for c in mvrv_coins if c.get("symbol")}

    # P0-2: BTC 周期相位
    btc_cycle = overview.get("btc_cycle") or {}
    cycle_phase = btc_cycle.get("phase", "unknown")

    # ── 预提取各轴信号（供 conviction 计算） ──
    sc = by_sig.get("price_stablecoin") or {}
    scm = sc.get("metrics") or {}
    stable_7d = scm.get("stablecoin_7d_netflow_usd")

    oi_sig = by_sig.get("price_oi") or {}
    funding_sig = by_sig.get("price_funding") or {}
    fm = funding_sig.get("metrics") or {}
    funding_latest = fm.get("funding_latest")

    onchain_ex = (onchain or {}).get("exchange_netflow") or {}
    ex_netflow = onchain_ex.get("netflow_7d_usd") if isinstance(onchain_ex, dict) else None

    # 优先用 CM 原生 BTC 链上净流（替代死链路 CoinGlass）
    cm_netflow_7d = btc_onchain.get("exchange_netflow_7d")
    if cm_netflow_7d is not None:
        ex_netflow = cm_netflow_7d
    # 第三刀：BTC per-asset 净流优先取 CM 落库表（可算 7d 斜率，最稳）
    btc_db_netflow = fetch_btc_netflow_7d()
    if btc_db_netflow is not None:
        ex_netflow = btc_db_netflow

    btc_roi_1yr = btc_onchain.get("roi_1yr")

    opportunities: list[dict] = []
    excluded: list[dict] = []
    degraded: list[str] = []

    # ── FEAT-SIGNAL-SRC: 预加载 daily_diff_summary（变化榜信号用） ──
    # 如果 overview 里没有，从 DB 补拉
    if not overview.get("daily_diff_summary"):
        try:
            from crypto_research.db.conn import get_connection
            from crypto_research.config import get_settings
            from crypto_research.db import db_stats

            settings = get_settings(require_database=True)
            with get_connection(settings.database_url) as conn:
                diff_data = db_stats.get_daily_diff_summary(conn)
                if diff_data and diff_data.get("ok"):
                    overview["daily_diff_summary"] = diff_data
        except Exception:
            pass

    # ── P0-1: MVRV 估值回归（聚合展示，避免「资产清单」当「高亮信号」） ──
    deep_undervalued_pct = t.get("mvrv_deep_undervalued_pct", 15)
    undervalued_pct = t.get("mvrv_undervalued_pct", 30)
    # 深度低估：最多取 3 个代表性币种单独聚合展示
    deep_under = sorted(
        [c for c in mvrv_coins if (c.get("pct_full") or 100) <= deep_undervalued_pct],
        key=lambda c: c.get("pct_full", 100),
    )[:3]
    if deep_under:
        symbols = ", ".join(str(c.get("symbol", "?")) for c in deep_under)
        avg_pct = sum(c.get("pct_full", 0) for c in deep_under) / len(deep_under)
        n_coins = len(deep_under)
        # FEAT-HIGHLIGHT-003: 实例强度公式
        strength_base = int(t.get("mvrv_deep_strength_base", 60))
        per_pct = float(t.get("mvrv_deep_strength_per_pct", 2.0))
        per_coin = int(t.get("mvrv_deep_strength_per_coin", 3))
        strength_cap = int(t.get("mvrv_deep_strength_cap", 12))
        strength = max(40, min(100, strength_base + int((deep_undervalued_pct - avg_pct) * per_pct) + min(strength_cap, n_coins * per_coin)))
        _push_opportunity(
            {"target": f"{n_coins} 币 MVRV 深度低估",
             "direction": "long", "confidence": "high",
             "conviction_score": strength,
             "signal_type": "mvrv_deep_under",
             "key_metric": f"MVRV {avg_pct:.0f}% · {n_coins}币",
             "trigger_logic": (
                 f"{symbols} 等 {n_coins} 个代币 MVRV 百分位 ≤{deep_undervalued_pct}%"
                 f"（平均 {avg_pct:.1f}%），处于历史极低区间"
             ),
             "action_hint": "左侧分批，中线持有",
             "invalidation": "MVRV 回升 >30% 或 BTC 周期进入 late_top",
             "related_dims": ["mvrv_universe", "P0-1 估值回归"],
             "involved_symbols": [c.get("symbol") for c in deep_under]},
            opportunities, excluded, t,
            cycle_phase=cycle_phase, n_confirm=2,
        )

    # 轻度低估：聚合成 1 条观察池
    under = [
        c for c in mvrv_coins
        if deep_undervalued_pct < (c.get("pct_full") or 0) <= undervalued_pct
    ]
    if under:
        symbols = ", ".join(str(c.get("symbol", "?")) for c in under[:5])
        avg_pct = sum(c.get("pct_full", 0) for c in under) / len(under)
        strength = max(40, min(70, 55 - int(avg_pct - deep_undervalued_pct)))
        _push_opportunity(
            {"target": f"{len(under)} 币 MVRV 低估观察池",
             "direction": "watch", "confidence": "medium",
             "conviction_score": strength,
             "signal_type": "mvrv_under_watch",
             "key_metric": f"MVRV {avg_pct:.0f}% · {len(under)}币",
             "trigger_logic": (
                 f"{symbols}{' 等' if len(under) > 5 else ''} {len(under)} 个代币 "
                 f"MVRV 百分位 {deep_undervalued_pct}-{undervalued_pct}%（平均 {avg_pct:.1f}%）"
             ),
             "action_hint": "仅作观察池，不单独下注",
             "invalidation": f"若 MVRV 继续下破 ≤{deep_undervalued_pct}% 或 BTC 周期恶化",
             "related_dims": ["mvrv_universe", "P0-1 估值回归"],
             "involved_symbols": [c.get("symbol") for c in under]},
            opportunities, excluded, t,
            cycle_phase=cycle_phase, n_confirm=1,
        )

    # ── MVRV 高估/极度高估（short 风险信号） ──
    # 对称于深度低估/低估观察池，用于高危信号面板
    overvalued_pct = t.get("mvrv_overvalued_pct", 75)
    deep_over_pct = t.get("mvrv_deep_overvalued_pct", 85)
    deep_over = sorted(
        [c for c in mvrv_coins if (c.get("pct_full") or 0) >= deep_over_pct],
        key=lambda c: c.get("pct_full", 0), reverse=True,
    )
    if deep_over:
        symbols = ", ".join(str(c.get("symbol", "?")) for c in deep_over)
        avg_pct = sum(c.get("pct_full", 0) for c in deep_over) / len(deep_over)
        n_coins = len(deep_over)
        strength_base = 60
        per_pct = 1.5
        per_coin = 5
        strength_cap = 40
        strength = max(55, min(95, strength_base + int((avg_pct - deep_over_pct) * per_pct) + min(strength_cap, n_coins * per_coin)))
        _push_opportunity(
            {"target": f"{len(deep_over)} 币 MVRV 极度高估",
             "direction": "short", "confidence": "high",
             "conviction_score": strength,
             "signal_type": "mvrv_deep_over",
             "key_metric": f"MVRV ≥{deep_over_pct}%",
             "trigger_logic": (
                 f"{symbols} 等 {n_coins} 个代币 MVRV 百分位 ≥{deep_over_pct}%"
             ),
             "action_hint": "估值极度偏高，注意回调风险",
             "invalidation": f"若 MVRV 回落至 <{deep_over_pct}% 或出现新催化剂",
             "related_dims": ["mvrv_universe", "P0-1 估值回归"],
             "involved_symbols": [c.get("symbol") for c in deep_over]},
            opportunities, excluded, t,
            cycle_phase=cycle_phase, n_confirm=1,
        )

    over_watch = [
        c for c in mvrv_coins
        if overvalued_pct <= (c.get("pct_full") or 0) < deep_over_pct
    ]
    if over_watch and len(over_watch) >= 3:
        avg_pct = sum(c.get("pct_full", 0) for c in over_watch) / len(over_watch)
        strength = max(35, min(60, 50 + int(avg_pct - overvalued_pct)))
        _push_opportunity(
            {"target": f"{len(over_watch)} 币 MVRV 高估观察池",
             "direction": "short", "confidence": "medium",
             "conviction_score": strength,
             "signal_type": "mvrv_over_watch",
             "key_metric": f"MVRV {overvalued_pct}-{deep_over_pct}%",
             "trigger_logic": (
                 f"MVRV 百分位 {overvalued_pct}-{deep_over_pct}%（平均 {avg_pct:.1f}%）"
             ),
             "action_hint": "估值偏高区域，追高需谨慎",
             "invalidation": f"若 MVRV 回落 <{overvalued_pct}%",
             "related_dims": ["mvrv_universe", "P0-1 估值回归"],
             "involved_symbols": [c.get("symbol") for c in over_watch]},
            opportunities, excluded, t,
            cycle_phase=cycle_phase, n_confirm=1,
        )

    if not mvrv_coins:
        degraded.append("mvrv_universe 数据缺失")

    # ── FEAT-SIGNAL-SRC-001：每日变化榜信号（从 daily_diff_summary 挖掘） ──
    # 从价格异动、成交量异动、量价齐升中提取机会/风险信号
    diff_cats = (overview.get("daily_diff_summary") or {}).get("categories") or {}
    if diff_cats:
        # 价格涨幅 Top（机会：强势突破）
        price_up = (diff_cats.get("price_change_24h") or {}).get("up") or []
        if price_up:
            strong = [it for it in price_up if (it.get("metric_value") or 0) >= t.get("diff_price_surge_pct", 15)]
            if strong:
                syms = [str(it.get("symbol", "?")) for it in strong[:5]]
                top_pct = strong[0].get("metric_value", 0)
                avg_pct = sum(it.get("metric_value", 0) for it in strong) / len(strong)
                strength = min(90, 45 + int(top_pct * 0.8) + len(strong) * 3)
                _push_opportunity(
                    {"target": f"{len(strong)} 币 24h 暴涨",
                     "direction": "long", "confidence": "medium",
                     "conviction_score": strength,
                     "signal_type": "price_surge",
                     "key_metric": f"24h 涨幅 ≥{t.get('diff_price_surge_pct', 15)}%",
                     "trigger_logic": (
                         f"{', '.join(syms)} 24h 涨幅超 {t.get('diff_price_surge_pct', 15)}%，"
                         f"最高 {top_pct:.1f}%，平均 {avg_pct:.1f}%"
                     ),
                     "action_hint": "关注强势突破，追高需谨慎",
                     "invalidation": "若 48h 内回落至涨幅 50% 以下",
                     "related_dims": ["P1-3 价格异动", "price_surge"],
                     "involved_symbols": syms},
                    opportunities, excluded, t,
                    cycle_phase=cycle_phase, n_confirm=1,
                )

        # 价格跌幅 Top（风险：暴跌/崩盘风险）
        price_down = (diff_cats.get("price_change_24h") or {}).get("down") or []
        if price_down:
            crash = [it for it in price_down if abs(it.get("metric_value") or 0) >= t.get("diff_price_crash_pct", 12)]
            if crash:
                syms = [str(it.get("symbol", "?")) for it in crash[:5]]
                max_drop = abs(crash[0].get("metric_value", 0))
                avg_drop = sum(abs(it.get("metric_value", 0)) for it in crash) / len(crash)
                strength = min(92, 50 + int(max_drop * 0.6) + len(crash) * 4)
                _push_opportunity(
                    {"target": f"{len(crash)} 币 24h 暴跌",
                     "direction": "short", "confidence": "medium",
                     "conviction_score": strength,
                     "signal_type": "price_crash",
                     "key_metric": f"24h 跌幅 ≥{t.get('diff_price_crash_pct', 12)}%",
                     "trigger_logic": (
                         f"{', '.join(syms)} 24h 跌幅超 {t.get('diff_price_crash_pct', 12)}%，"
                         f"最大 {max_drop:.1f}%，平均 {avg_drop:.1f}%"
                     ),
                     "action_hint": "警惕继续下行，抄底需等待企稳",
                     "invalidation": "若 48h 内收复跌幅 50% 以上",
                     "related_dims": ["P1-3 价格异动", "price_crash"],
                     "involved_symbols": syms},
                    opportunities, excluded, t,
                    cycle_phase=cycle_phase, n_confirm=1,
                )

        # 量价齐升（机会：资金入场 + 价格突破共振）
        pvs = (diff_cats.get("price_volume_surge") or {}).get("up") or []
        if pvs:
            top_pvs = pvs[:5]
            syms = [str(it.get("symbol", "?")) for it in top_pvs]
            top_price_change = top_pvs[0].get("metric_value", 0)
            strength = min(88, 55 + int(top_price_change * 0.5) + len(top_pvs) * 2)
            _push_opportunity(
                {"target": f"{len(top_pvs)} 币 量价齐升",
                 "direction": "long", "confidence": "high",
                 "conviction_score": strength,
                 "signal_type": "price_volume_surge",
                 "key_metric": "价格 + 成交量双升",
                 "trigger_logic": (
                     f"{', '.join(syms)} 量价齐升，价格涨幅最高 {top_price_change:.1f}%"
                 ),
                 "action_hint": "量价共振，关注趋势延续性",
                 "invalidation": "若成交量快速萎缩或价格跌破支撑",
                 "related_dims": ["P1-3 价格异动", "volume_surge"],
                 "involved_symbols": syms},
                opportunities, excluded, t,
                cycle_phase=cycle_phase, n_confirm=1,
            )

        # 成交量异动（机会/中性：关注度激增）
        vol_surge = (diff_cats.get("volume_surge_24h") or {}).get("up") or []
        if vol_surge:
            top_vol = vol_surge[:5]
            syms = [str(it.get("symbol", "?")) for it in top_vol]
            top_vol_pct = top_vol[0].get("metric_value", 0)
            strength = min(80, 40 + min(40, top_vol_pct * 0.3) + len(top_vol) * 2)
            _push_opportunity(
                {"target": f"{len(top_vol)} 币 成交量异动",
                 "direction": "long", "confidence": "medium",
                 "conviction_score": strength,
                 "signal_type": "volume_surge",
                 "key_metric": f"24h 成交量放大",
                 "trigger_logic": (
                     f"{', '.join(syms)} 24h 成交量显著放大，最高 +{top_vol_pct:.0f}%"
                 ),
                 "action_hint": "关注度激增，关注方向选择",
                 "invalidation": "若成交量迅速回落至正常水平",
                 "related_dims": ["P1-3 成交量异动", "volume_surge"],
                 "involved_symbols": syms},
                opportunities, excluded, t,
                cycle_phase=cycle_phase, n_confirm=1,
            )

    # ── FEAT-SIGNAL-SRC-002：赛道资金流信号（从 sector_flow_daily 挖掘） ──
    sector_flow_data = (overview.get("sector_flow") or {}).get("sectors") or []
    if not sector_flow_data:
        # 降级：从 overview.dimensions.5板块 读叙事榜
        if narrative:
            top_inflow = narrative[:3]
            if top_inflow and top_inflow[0].get("net_flow_7d_usd", 0) > 0:
                names = [str(x.get("narrative", x.get("sector", "?"))) for x in top_inflow]
                top_flow = top_inflow[0].get("net_flow_7d_usd", 0)
                strength = min(82, 50 + min(30, top_flow / 1e7) * 2)
                _push_opportunity(
                    {"target": f"{len(top_inflow)} 赛道 资金持续流入",
                     "direction": "long", "confidence": "medium",
                     "conviction_score": strength,
                     "signal_type": "sector_inflow",
                     "key_metric": "7d 资金净流入",
                     "trigger_logic": (
                         f"{', '.join(names)} 赛道 7 天资金持续净流入，"
                         f"榜首流入 ${top_flow/1e6:.0f}M"
                     ),
                     "action_hint": "关注领涨赛道龙头币",
                     "invalidation": "若连续 3 天转为净流出",
                     "related_dims": ["5板块", "P0-1 叙事轮动"],
                     "involved_symbols": []},
                    opportunities, excluded, t,
                    cycle_phase=cycle_phase, n_confirm=1,
                )
            bottom_outflow = narrative[-3:] if len(narrative) >= 3 else []
            outflow_neg = [x for x in bottom_outflow if x.get("net_flow_7d_usd", 0) < 0]
            if outflow_neg:
                names = [str(x.get("narrative", x.get("sector", "?"))) for x in outflow_neg]
                bottom_flow = abs(outflow_neg[-1].get("net_flow_7d_usd", 0))
                strength = min(85, 52 + min(30, bottom_flow / 1e7) * 2)
                _push_opportunity(
                    {"target": f"{len(outflow_neg)} 赛道 资金持续流出",
                     "direction": "short", "confidence": "medium",
                     "conviction_score": strength,
                     "signal_type": "sector_outflow",
                     "key_metric": "7d 资金净流出",
                     "trigger_logic": (
                         f"{', '.join(names)} 赛道 7 天资金持续净流出，"
                         f"榜尾流出 ${bottom_flow/1e6:.0f}M"
                     ),
                     "action_hint": "规避弱势赛道，警惕进一步下跌",
                     "invalidation": "若连续 3 天转为净流入",
                     "related_dims": ["5板块", "P0-1 叙事轮动"],
                     "involved_symbols": []},
                    opportunities, excluded, t,
                    cycle_phase=cycle_phase, n_confirm=1,
                )

    # ── 1) BTC 左侧积累 / 场外弹药（long） ──
    btc_left_sources: list[tuple[str, str]] = []
    left_metrics: list[str] = []
    if sc.get("status") == "ok" and (scm.get("stablecoin_7d_netflow_usd") or 0) >= t["stablecoin_flow_min_usd"]:
        if (scm.get("price_7d_pct") or 0) < DIVERGENCE_THRESHOLDS["price_stagnation_pct"]:
            btc_left_sources.append(("stablecoin_flow", "long"))
            left_metrics.append(f"稳定币 7d 净流入 {_fmt_billions(scm.get('stablecoin_7d_netflow_usd'))}")
    emo_score = emotion.get("score")
    if emo_score is not None and emo_score < t.get("emotion_fear_max", 50):
        btc_left_sources.append(("emotion", "long"))
        left_metrics.append(f"恐贪 {emo_score:.0f}（恐惧区）")
    if onchain:
        ex = onchain.get("exchange_netflow") or {}
        if isinstance(ex, dict) and ex.get("status") == "ok":
            net = ex.get("netflow_7d_usd")
            if net is not None and net > t.get("exchange_netflow_min_usd", 100_000_000):
                btc_left_sources.append(("exchange_netflow", "long"))
                left_metrics.append(f"交易所 7d 净流出 {_fmt_billions(net)}")
    # CM BTC 链上积累信号（CM Community 原生，比 CoinGlass 可靠）
    if btc_onchain.get("status") == "ok":
        cm_net = btc_onchain.get("exchange_netflow_7d")
        if cm_net is not None and cm_net > t.get("exchange_netflow_min_usd", 100_000_000):
            if ("exchange_netflow", "long") not in btc_left_sources:
                btc_left_sources.append(("exchange_netflow_cm", "long"))
                left_metrics.append(f"CM BTC 链上 7d 净流出 {_fmt_billions(cm_net)}")
    if btc_left_sources:
        conf, direction = _resolve_confidence(btc_left_sources, t)
        btc_breakdown = _conviction_breakdown(
            mvrv_pct=_mvrv_pct_for("BTC", mvrv_map),
            funding=funding_latest, exchange_netflow=ex_netflow,
            stablecoin_flow=stable_7d, roi_1yr=btc_roi_1yr, t=t,
        )
        conviction = btc_breakdown["raw_strength"]
        related = ["P1-2 价格vs稳定币"]
        if "emotion" in {typ for typ, _ in btc_left_sources}:
            related.append("P0-3 情绪")
        if "exchange_netflow" in {typ for typ, _ in btc_left_sources}:
            related.append("P1-3 交易所净流")
        if "exchange_netflow_cm" in {typ for typ, _ in btc_left_sources}:
            related.append("6链上 CM BTC 链上净流")
        trigger = f"{' / '.join(left_metrics)} → 场外弹药积累，左侧布局窗口"
        _push_opportunity(
            {"target": "BTC", "direction": direction, "confidence": conf,
             "conviction_score": conviction,
             "conviction_breakdown": btc_breakdown,
             "signal_type": "btc_left_accum",
             "key_metric": "左侧积累窗口",
             "action_hint": "左侧布局窗口，分批建仓",
             "invalidation": "若交易所转为净流入或稳定币流向转负，信号失效",
             "trigger_logic": trigger, "related_dims": related},
            opportunities, excluded, t,
            cycle_phase=cycle_phase, n_confirm=len(btc_left_sources),
        )

    # ── 1b) 多资产采用背离 / 网络健康（CM 第三刀） ──
    cm_act = ((overview.get("dimensions") or {}).get("6a网络健康") or {}).get("data") or {}
    if cm_act.get("status") == "ok":
        for act_coin in (cm_act.get("coins") or []):
            sig = act_coin.get("signal")
            if sig == "accumulation":
                conviction = _compute_conviction_score(
                    mvrv_pct=_mvrv_pct_for(act_coin.get("symbol"), mvrv_map),
                    funding=funding_latest, exchange_netflow=ex_netflow,
                    stablecoin_flow=stable_7d, roi_1yr=btc_roi_1yr, t=t,
                )
                _push_opportunity(
                    {"target": act_coin["symbol"], "direction": "long", "confidence": "medium",
                     "conviction_score": conviction,
                     "signal_type": "cm_adoption_divergence",
                     "key_metric": f"活跃 {act_coin.get('adr_pct', '?')}%分位",
                     "trigger_logic": (
                         f"{act_coin['symbol']} 活跃地址 {act_coin.get('adr_pct')}% 分位 + "
                         f"30d 收益 {act_coin.get('roi_30d')}% → 采用增长但价格未跟上（静默积累）"
                     ),
                     "related_dims": ["6a网络健康 CM 采用背离"]},
                    opportunities, excluded, t,
                    cycle_phase=cycle_phase, n_confirm=2,
                )
            elif sig == "decline":
                _push_opportunity(
                    {"target": act_coin["symbol"], "direction": "watch", "confidence": "low",
                     "conviction_score": 0,
                     "signal_type": "cm_adoption_divergence",
                     "trigger_logic": (
                         f"{act_coin['symbol']} 活跃地址 {act_coin.get('adr_pct')}% 分位 → 网络衰退警示"
                     ),
                     "related_dims": ["6a网络健康 CM 网络衰退"]},
                    opportunities, excluded, t,
                )

    # ── 1c) 催化剂驱动机会 (P0-B) ──
    # P1-2: 币安广场宏观噪音过滤（金十/SpaceX 等非加密直接关联噪音）
    _CATALYST_NOISE_KEYWORDS = {"金十", "SpaceX", "马斯克", "特斯拉", "SEC", "美联储", "CPI", "GDP"}
    cat_window = t.get("catalyst_window_days", 14)
    cat_min_score = t.get("catalyst_min_score", 50)
    cat_top_n = int(t.get("catalyst_top_n", 5))
    cat_targets = _recent_catalyst_targets(cat_window)
    _cat_seen: set[str] = set()
    _cat_pushed = 0
    for aid, symbol, cscore in cat_targets:
        if cscore < cat_min_score:
            continue
        # P1-2: 过滤宏观噪音（金十/SpaceX 等非加密直接关联）
        if any(kw in symbol.upper() for kw in _CATALYST_NOISE_KEYWORDS):
            continue
        # 同 symbol 去重（不同 asset_id 可能映射同一标的），保留分高者
        if symbol in _cat_seen:
            continue
        if _cat_pushed >= cat_top_n:
            break
        _cat_seen.add(symbol)
        _cat_pushed += 1
        breakdown = _conviction_breakdown(
            mvrv_pct=_mvrv_pct_for(symbol, mvrv_map),
            funding=funding_latest, exchange_netflow=ex_netflow,
            stablecoin_flow=stable_7d, roi_1yr=btc_roi_1yr,
            catalyst_score=cscore, t=t,
        )
        strength = breakdown["raw_strength"]
        # 取具体催化剂事件列表（TOP 3），供前端展示
        cat_events = _fetch_catalyst_events(aid, cat_window, limit=3)
        _push_opportunity(
            {"target": symbol, "direction": "long",
             "confidence": "high" if cscore >= 70 else "medium",
             "conviction_score": strength,
             "conviction_breakdown": breakdown,
             "signal_type": "catalyst",
             "key_metric": f"催化剂 {cscore:.0f}分",
             "trigger_logic": f"近{cat_window}d 催化剂净情绪 {cscore:.0f}（事件驱动）",
             "catalyst_events": cat_events,
             "catalyst_event_count": len(cat_events),
             "action_hint": "事件驱动，窗口内跟进",
             "invalidation": "催化剂事件兑现/热度消退后信号失效",
             "related_dims": ["catalyst_events", "P0-B 催化剂驱动"]},
            opportunities, excluded, t,
            cycle_phase=cycle_phase, n_confirm=1,
        )

    # ── 1d) 链上巨鲸异常卡（工单1：onchain_transfer_log 24h） ──
    _whale_targets = _recent_whale_flow_targets(
        hours=float(t.get("whale_window_hours", 24)),
        usd_min=float(t.get("whale_usd_min", 1_000_000)),
        limit=int(t.get("whale_top_n", 5)),
    )
    for wt in _whale_targets:
        # wt: (asset_id, symbol, usd_total, n_tx, max_value_usd, is_to_exchange, chain)
        aid, symbol, usd_total, n_tx, max_val, is_to_ex, chain = wt
        if is_to_ex:
            direction = "short"
            action = "抛压预警，观察 T+3 是否跌破阈值"
            invalid = "若 T+3 内未跌破或出现等量回补，信号失效"
        else:
            direction = "long"
            action = "吸筹观察，左侧关注"
            invalid = "若 T+3 内出现同额反手转出，信号失效"
        conviction = _compute_conviction_score(
            mvrv_pct=_mvrv_pct_for(symbol, mvrv_map),
            funding=funding_latest, exchange_netflow=ex_netflow,
            stablecoin_flow=stable_7d, roi_1yr=btc_roi_1yr, t=t,
        )
        _push_opportunity(
            {"target": symbol, "direction": direction, "confidence": "medium",
             "conviction_score": conviction,
             "signal_type": "whale_flow",
             "key_metric": f"巨鲸 ${usd_total / 1e6:.1f}M",
             "asset_id": aid,
             "trigger_logic": (
                 f"{symbol} 近{t.get('whale_window_hours', 24)}h {n_tx} 笔大额"
                 f"（单笔最大 ${max_val / 1e6:.1f}M，合计 ${usd_total / 1e6:.1f}M）"
                 f"{'转入交易所（抛压）' if is_to_ex else '从交易所转出（吸筹）'}"
             ),
             "action_hint": action,
             "invalidation": invalid,
             "related_dims": ["onchain_transfer_log", "P1-3 链上巨鲸"]},
            opportunities, excluded, t,
        )

    # ── 1e) GitHub 开发者活跃异动卡（工单3） ──
    _gh_targets = _github_activity_targets(
        window_days=int(t.get("github_window_days", 60)),
        burst_ratio=float(t.get("github_burst_ratio", 1.5)),
        decline_ratio=float(t.get("github_decline_ratio", 0.5)),
        limit=int(t.get("github_top_n", 5)),
    )
    for gt in _gh_targets:
        # gt: (asset_id, symbol, last4, prev4, ratio, direction)
        aid, symbol, last4, prev4, ratio, gdir = gt
        if gdir == "burst":
            direction, action, invalid = "long", "dev 活跃爆发，关注主网上线/交付", "若后续 4 周回落至均值以下，信号失效"
            label = "dev 活跃爆发"
        else:
            direction, action, invalid = "watch", "开发停滞风险，配合解锁抛压=双杀", "若后续 4 周恢复活跃，信号失效"
            label = "dev 活跃骤降"
        conviction = _compute_conviction_score(
            mvrv_pct=_mvrv_pct_for(symbol, mvrv_map),
            funding=funding_latest, exchange_netflow=ex_netflow,
            stablecoin_flow=stable_7d, roi_1yr=btc_roi_1yr, t=t,
        )
        _push_opportunity(
            {"target": symbol, "direction": direction, "confidence": "medium",
             "conviction_score": conviction,
             "signal_type": "github_activity",
             "key_metric": f"Dev {ratio:.1f}x",
             "asset_id": aid,
             "trigger_logic": (
                 f"{symbol} {label}：近 4 周 {last4} commits vs 前 4 周 {prev4}（{ratio:.1f}x）"
             ),
             "action_hint": action,
             "invalidation": invalid,
             "related_dims": ["github_repo_activity", "P1 GitHub 开发活跃"]},
            opportunities, excluded, t,
        )

    # ── 1f) 融资近期落地卡（工单4） ──
    _raise_targets = _recent_raises(
        window_days=int(t.get("raise_window_days", 90)),
        limit=int(t.get("raise_top_n", 5)),
    )
    for rt in _raise_targets:
        # rt: (asset_id, symbol, round, amount_m, lead, raise_date, protocol_name)
        aid, symbol, rnd, amount_m, lead, rdate, proto = rt
        amount_str = f"${amount_m:.0f}M" if amount_m else "N/A"
        lead_str = lead or "未披露"
        target = symbol if symbol not in ("", "-") else (proto or "?")
        conviction = _compute_conviction_score(
            mvrv_pct=_mvrv_pct_for(symbol, mvrv_map),
            funding=funding_latest, exchange_netflow=ex_netflow,
            stablecoin_flow=stable_7d, roi_1yr=btc_roi_1yr, t=t,
        )
        _push_opportunity(
            {"target": target, "direction": "long", "confidence": "medium",
             "conviction_score": conviction,
             "signal_type": "funding",
             "key_metric": f"融资 {amount_str}",
             "asset_id": aid,
             "trigger_logic": (
                 f"{target} {rnd} 轮融资落地 {amount_str}（领投 {lead_str}，{rdate}）"
             ),
             "action_hint": "机构入场信心信号，关注后续解锁抛压",
             "invalidation": "若大额 vesting 解锁临近，潜在抛压对冲",
             "related_dims": ["asset_raises", "P1 融资落地"]},
            opportunities, excluded, t,
        )

    # ── 1g) 代币解锁抛压卡（工单5） ──
    _unlock_targets = _upcoming_unlocks(
        window_days=int(t.get("unlock_window_days", 14)),
        ratio_min=float(t.get("unlock_ratio_min", 1.0)),
        limit=int(t.get("unlock_top_n", 5)),
    )
    for ut in _unlock_targets:
        # ut: (asset_id, symbol, unlock_value_usd, unlock_date, ratio_mcap)
        aid, symbol, uval, udate, ratio_mcap = ut
        conviction = _compute_conviction_score(
            mvrv_pct=_mvrv_pct_for(symbol, mvrv_map),
            funding=funding_latest, exchange_netflow=ex_netflow,
            stablecoin_flow=stable_7d, roi_1yr=btc_roi_1yr, t=t,
        )
        _push_opportunity(
            {"target": symbol, "direction": "short", "confidence": "medium",
             "conviction_score": conviction,
             "signal_type": "token_unlock",
             "key_metric": f"解锁 ${uval / 1e6:.1f}M ({ratio_mcap:.1f}%)",
             "asset_id": aid,
             "trigger_logic": (
                 f"{symbol} 解锁临近 {udate}（价值 ${uval / 1e6:.1f}M，占市值 "
                 f"{ratio_mcap:.1f}%）→ 供给侧抛压"
             ),
             "action_hint": "cliff 前减仓或对冲，配合 dev 停滞=双杀",
             "invalidation": "解锁后若流通未抛售，抛压证伪",
             "related_dims": ["asset_unlock_event", "P1 解锁抛压"]},
            opportunities, excluded, t,
        )

    # ── 1h) KOL onchain 链上异动信号 ──
    _kol_targets = _kol_onchain_signals(
        days=int(t.get("kol_window_days", 3)),
        limit=int(t.get("kol_top_n", 10)),
    )
    for ks in _kol_targets:
        # ks 是 dict: asset_id, symbol, subtype, usd_value, exchange,
        #           kol_name, direction, address_label, event_direction, content_text, post_url
        symbol = ks["symbol"]
        usd_val = ks["usd_value"]
        subtype = ks["subtype"]
        exchange = ks["exchange"]
        addr_label = ks["address_label"]
        kol_name = ks["kol_name"]
        evt_dir = (ks["event_direction"] or ks["direction"] or "").lower()

        # ── 方向判定 ──
        # 流出/吸筹 → 看多（抛压减小/大资金抄底）
        # 流入/派发 → 看空（抛压增大/大资金出货）
        # 爆仓 → 中性（偏短期波动）
        if evt_dir in ("outflow", "out") or subtype in ("accumulation",):
            sig_direction = "long"
        elif evt_dir in ("inflow", "in") or subtype in ("distribution",):
            sig_direction = "short"
        elif "liquidat" in evt_dir or subtype == "liquidation":
            sig_direction = "watch"
        elif ks["direction"] == "long":
            sig_direction = "long"
        elif ks["direction"] == "short":
            sig_direction = "short"
        else:
            sig_direction = "watch"

        # ── 置信度判定 ──
        # HIGH: 金额 >$10M 且有巨鲸/聪明钱/项目方标签
        # MED:  金额 >$3M 或 有名地址标签
        # LOW:  其他
        is_big_amount = usd_val >= 10_000_000
        is_mid_amount = usd_val >= 3_000_000
        has_smart_label = any(kw in addr_label for kw in [
            "巨鲸", "聪明钱", "项目方", "创始人", "巨鳄",
            "whale", "smart", "founder", "insider",
        ])

        if is_big_amount and has_smart_label and sig_direction != "watch":
            confidence = "high"
        elif is_mid_amount or has_smart_label:
            confidence = "medium"
        else:
            confidence = "low"

        # ── 关键指标文案 ──
        type_cn = {
            "exchange_flow": "交易所流",
            "whale_move": "巨鲸转账",
            "smart_money": "聪明钱",
            "liquidation": "爆仓清算",
            "accumulation": "吸筹",
            "distribution": "派发",
        }.get(subtype, subtype or "链上异动")

        usd_str = (
            f"${usd_val / 1e6:.1f}M" if usd_val >= 1e6
            else f"${usd_val / 1e3:.0f}K" if usd_val >= 1e3
            else f"${usd_val:.0f}"
        )

        trigger_parts = [f"KOL链上异动：{type_cn}"]
        if usd_val > 0:
            trigger_parts.append(usd_str)
        if exchange:
            trigger_parts.append(f"@{exchange}")
        if addr_label:
            trigger_parts.append(f"[{addr_label}]")
        if kol_name:
            trigger_parts.append(f"by {kol_name}")

        conviction = _compute_conviction_score(
            mvrv_pct=_mvrv_pct_for(symbol, mvrv_map),
            funding=funding_latest, exchange_netflow=ex_netflow,
            stablecoin_flow=stable_7d, roi_1yr=btc_roi_1yr, t=t,
        )

        action_hint_map = {
            "long": "链上资金流出 + 大资金异动，关注后续上行动力",
            "short": "链上资金流入 + 大资金异动，警惕短期抛压",
            "watch": "情报参考，结合其他维度确认后跟进",
        }

        _push_opportunity(
            {"target": symbol, "direction": sig_direction,
             "confidence": confidence,
             "conviction_score": conviction,
             "signal_type": "kol_onchain",
             "key_metric": f"{type_cn} {usd_str}" if usd_val > 0 else type_cn,
             "asset_id": ks["asset_id"],
             "trigger_logic": " ".join(trigger_parts),
             "action_hint": action_hint_map.get(sig_direction, "情报参考"),
             "invalidation": "KOL信号时效性强，24h内未验证需重估",
             "related_dims": ["kol_signal", "P1 KOL onchain"]},
            opportunities, excluded, t,
        )

    # ── 2) 杠杆过热 / 风险规避（short） ──
    risk_sources: list[tuple[str, str]] = []
    risk_metrics: list[str] = []
    oim = oi_sig.get("metrics") or {}
    if oi_sig.get("status") == "ok" and oi_sig.get("label") == "DANGEROUS":
        risk_sources.append(("oi", "short"))
        risk_metrics.append(f"OI 7d {oim.get('oi_7d_pct', 0):+.1f}%")
    if funding_sig.get("status") == "ok" and funding_sig.get("label") in ("DANGEROUS", "DIVERGENT"):
        risk_sources.append(("funding", "short"))
        risk_metrics.append(f"funding {fm.get('funding_latest', 0) * 100:.3f}%/期")
    if risk_sources:
        conf, direction = _resolve_confidence(risk_sources, t)
        conviction = _compute_conviction_score(
            mvrv_pct=_mvrv_pct_for("BTC", mvrv_map),
            funding=funding_latest, exchange_netflow=ex_netflow,
            stablecoin_flow=stable_7d, roi_1yr=btc_roi_1yr, t=t,
        )
        trigger = f"{' / '.join(risk_metrics) if risk_metrics else '杠杆信号'} → 杠杆过热，防回撤"
        _push_opportunity(
            {"target": "BTC", "direction": direction, "confidence": conf,
             "conviction_score": conviction,
             "trigger_logic": trigger, "related_dims": ["P1-2 价格vs OI", "P1-2 价格vs funding"]},
            opportunities, excluded, t,
        )

    # ── 3) 叙事流入（long，TOP N） ──
    n_top = int(t.get("narrative_top_n", 3))
    for row in narrative[:n_top]:
        composite = row.get("composite_score")
        if composite is None or composite < t.get("narrative_min_composite", 8.0):
            continue
        mode = row.get("mode")
        if mode == "blended":
            conf, direction = "high", "long"
            related = ["P1-1 叙事榜（市值+TVL）"]
            trigger = (
                f"{row.get('narrative')} 7d 市值 {row.get('mcap_change_7d_pct', 0):+.1f}%"
                f" + TVL {row.get('tvl_change_7d_pct', 0):+.1f}% → 双源资金净流入"
            )
        else:
            conf, direction = "medium", "long"
            related = ["P1-1 叙事榜（市值）"]
            trigger = f"{row.get('narrative')} 7d 市值 {row.get('mcap_change_7d_pct', 0):+.1f}% → 资金净流入"
        conviction = _compute_conviction_score(
            mvrv_pct=_mvrv_pct_for(row.get("narrative"), mvrv_map),
            funding=funding_latest, exchange_netflow=ex_netflow,
            stablecoin_flow=stable_7d, roi_1yr=btc_roi_1yr, t=t,
        )
        _push_opportunity(
            {"target": row.get("narrative"), "direction": direction, "confidence": conf,
             "conviction_score": conviction,
             "trigger_logic": trigger, "related_dims": related},
            opportunities, excluded, t,
        )

    # ── 4) 链净流入（long，TOP N） ──
    c_top = int(t.get("chain_top_n", 3))
    for row in chains[:c_top]:
        flow = row.get("flow_7d")
        flow_pct = row.get("flow_7d_pct")
        if flow is None or flow_pct is None:
            continue
        if flow < t.get("chain_min_flow_usd", 200_000_000) or flow_pct < t.get("chain_min_flow_pct", 3.0):
            continue
        conviction = _compute_conviction_score(
            mvrv_pct=_mvrv_pct_for(f"{row.get('chain')} 链", mvrv_map),
            funding=funding_latest, exchange_netflow=ex_netflow,
            stablecoin_flow=stable_7d, roi_1yr=btc_roi_1yr, t=t,
        )
        _push_opportunity(
            {"target": f"{row.get('chain')} 链", "direction": "long", "confidence": "medium",
             "conviction_score": conviction,
             "trigger_logic": f"{row.get('chain')} 链 7d TVL {_fmt_billions(flow)}（{flow_pct:+.1f}%）→ 资金净流入",
             "related_dims": ["P1-1 链净流入榜"]},
            opportunities, excluded, t,
        )

    # ── 5) 宏观脱钩 / 独立行情（neutral） ──
    ndx_sig = by_sig.get("btc_nasdaq") or {}
    if ndx_sig.get("status") == "ok" and ndx_sig.get("label") == "DIVERGENT":
        interp = ndx_sig.get("interpretation", "宏观脱钩")
        conviction = _compute_conviction_score(
            mvrv_pct=_mvrv_pct_for("BTC", mvrv_map),
            funding=funding_latest, exchange_netflow=ex_netflow,
            stablecoin_flow=stable_7d, roi_1yr=btc_roi_1yr, t=t,
        )
        _push_opportunity(
            {"target": "BTC", "direction": "neutral", "confidence": "medium",
             "conviction_score": conviction,
             "trigger_logic": interp, "related_dims": ["P1-2 BTC vs 纳指"]},
            opportunities, excluded, t,
        )

    # ── 6) P1-3 链上异动（若已接入） ──
    if onchain:
        protos = (onchain.get("new_protocol_tvl") or {}).get("ranked", []) or []
        p_top = int(t.get("protocol_top_n", 3))
        for p in protos[:p_top]:
            if not isinstance(p, dict):
                continue
            conviction = _compute_conviction_score(
                mvrv_pct=_mvrv_pct_for(p.get("name"), mvrv_map),
                funding=funding_latest, exchange_netflow=ex_netflow,
                stablecoin_flow=stable_7d, roi_1yr=btc_roi_1yr, t=t,
            )
            _push_opportunity(
                {"target": p.get("name") or "新协议", "direction": "long", "confidence": "medium",
                 "conviction_score": conviction,
                 "trigger_logic": f"{p.get('name')} 7d TVL {p.get('change_7d_pct') if p.get('change_7d_pct') is not None else '?'}% 异动增长",
                 "related_dims": ["P1-3 新协议 TVL"]},
                opportunities, excluded, t,
            )
    else:
        degraded.append("P1-3 链上异动未接入（跳过）")

    if not narrative:
        degraded.append("P1-1 叙事榜缺失")
    if not chains:
        degraded.append("P1-1 链榜缺失")
    if not divergence:
        degraded.append("P1-2 背离信号缺失")

    # ════════ 第二刀独立高亮维度（FEAT-HIGHLIGHT-002）══════
    # 事件驱动、间歇触发，高信息熵可执行信号，与"状态类"信号互补
    _fear_max = t.get("fng_fear_max", 25)
    _greed_min = t.get("fng_greed_min", 75)

    # A. 恐贪极值
    _fg = (overview.get("dimensions") or {}).get("3情绪") or {}
    _fg_val = (((_fg.get("data") or {}).get("fear_greed") or {}).get("value"))
    if _fg_val is None:
        _emo = (overview.get("summary") or {}).get("emotion_subscore") or {}
        _fg_val = ((_emo.get("components") or {}).get("fear_greed") or {}).get("value")
    if _fg_val is not None:
        if _fg_val <= _fear_max:
            strength = max(70, min(95, 50 + 50 * ((50 - _fg_val) / 50)))
            _push_opportunity(
                {"target": "恐贪指数极度恐惧", "direction": "long", "confidence": "high",
                 "conviction_score": strength,
                 "signal_type": "fng_extreme",
                 "key_metric": f"恐贪 {_fg_val:.0f}",
                 "trigger_logic": f"恐贪指数 {_fg_val:.0f} ≤ {_fear_max}：市场极度恐惧，历史级积累区",
                 "action_hint": "情绪极端悲观时分批建仓，止损设宽",
                 "invalidation": "恐贪回升 >40 或 BTC 破位下行",
                 "related_dims": ["emotion_subscore", "3情绪"], "involved_symbols": ["BTC"]},
                opportunities, excluded, t,
                cycle_phase=cycle_phase, n_confirm=2)
        elif _fg_val >= _greed_min:
            strength = max(70, min(95, 50 + 50 * ((_fg_val - 50) / 50)))
            _push_opportunity(
                {"target": "恐贪指数极度贪婪", "direction": "short", "confidence": "high",
                 "conviction_score": strength,
                 "signal_type": "fng_extreme",
                 "key_metric": f"恐贪 {_fg_val:.0f}",
                 "trigger_logic": f"恐贪指数 {_fg_val:.0f} ≥ {_greed_min}：市场极度贪婪，防回撤",
                 "action_hint": "减仓/对冲，警惕顶部",
                 "invalidation": "恐贪回落 <65 或 BTC 突破新高放量",
                 "related_dims": ["emotion_subscore", "3情绪"], "involved_symbols": ["BTC"]},
                opportunities, excluded, t,
                cycle_phase=cycle_phase, n_confirm=2)

    # B. 杠杆极值
    _funding_sig = by_sig.get("price_funding") or {}
    _funding_label = _funding_sig.get("label")
    if _funding_label in ("DANGEROUS", "DIVERGENT"):
        _fm = _funding_sig.get("metrics") or {}
        _fl = _fm.get("funding_latest") or 0
        _lev_dir = "short" if _fl > 0 else "long"
        lev_base = int(t.get("leverage_strength_base", 70))
        lev_k = float(t.get("leverage_strength_k", 20000))
        strength = max(70, min(95, lev_base + min(20, int(abs(_fl) * lev_k))))
        _push_opportunity(
            {"target": "衍生品杠杆极值", "direction": _lev_dir, "confidence": "high",
             "conviction_score": strength,
             "signal_type": "leverage_extreme",
             "key_metric": f"资金费率 {_fl*100:.2f}%",
             "trigger_logic": f"funding {_fl*100:.3f}%/期 极端 + 价滞涨：{'多头拥挤挤仓风险' if _lev_dir=='short' else '空头拥挤逼空风险'}",
             "action_hint": "警惕杠杆踩踏，降低合约敞口" if _lev_dir == "short" else "关注逼空反弹窗口",
             "invalidation": "funding 回归正常区间 或 价格放量突破",
             "related_dims": ["derivatives", "price_funding"], "involved_symbols": ["BTC"]},
            opportunities, excluded, t,
            cycle_phase=cycle_phase, n_confirm=2)

    # C. 稳定币大幅净流入
    _sc_min = t.get("stablecoin_inflow_min", 5_000_000_000)
    if stable_7d is not None and stable_7d >= _sc_min:
        sc_base = int(t.get("stablecoin_strength_base", 58))
        sc_per_1b = float(t.get("stablecoin_strength_per_1b", 3))
        strength = max(58, min(92, sc_base + int((stable_7d / 1e9 - 5) * sc_per_1b)))
        _push_opportunity(
            {"target": "稳定币大幅净流入", "direction": "long", "confidence": "medium",
             "conviction_score": strength,
             "signal_type": "stablecoin_inflow",
             "key_metric": f"净流入 +${stable_7d/1e9:.1f}B",
             "trigger_logic": f"稳定币 7d 净流 ${stable_7d/1e9:.1f}B ≥ 阈值：场外弹药积累，潜在买盘",
             "action_hint": "关注 BTC/大盘承接与突破",
             "invalidation": "净流转负 或 BTC 放量下跌",
             "related_dims": ["stablecoin_flow"], "involved_symbols": ["BTC"]},
            opportunities, excluded, t,
            cycle_phase=cycle_phase, n_confirm=2)

    # D. ETF 资金流异动（机构资金信号）
    _etf_data = (((overview.get("dimensions") or {}).get("4机构") or {}).get("data") or {})
    if _etf_data.get("status") == "ok":
        etf_net_total = _etf_data.get("net_flow_usd_m")
        etf_btc = _etf_data.get("btc_net_flow_usd_m")
        etf_date = _etf_data.get("as_of_date", "")
        etf_flow_high = float(t.get("etf_flow_high_threshold", 100))   # 单日净流入 > 1亿美金
        etf_flow_low = float(t.get("etf_flow_low_threshold", -100))    # 单日净流出 < -1亿美金

        # D1) BTC ETF 资金流（单独信号，target = BTC）
        if etf_btc is not None:
            if etf_btc >= etf_flow_high:
                direction = "long"
                conf = "high" if etf_btc >= etf_flow_high * 2 else "medium"
                base_str = 72 if etf_btc >= etf_flow_high * 2 else 60
                strength = min(92, base_str + int(min(20, (etf_btc - etf_flow_high) / 50)))
                _push_opportunity(
                    {"target": "BTC", "direction": direction, "confidence": conf,
                     "conviction_score": strength,
                     "signal_type": "etf_flow",
                     "key_metric": f"ETF 净流入 +${etf_btc:.0f}M",
                     "trigger_logic": f"BTC ETF 单日净流入 ${etf_btc:.0f}M（{etf_date}）→ 机构资金持续加仓",
                     "action_hint": "机构入场确认，回踩可加仓",
                     "invalidation": "ETF 连续 3 日净流出 或 BTC 破位",
                     "related_dims": ["机构ETF资金流（cryptoetf.today）", "P1 机构行为"],
                     "involved_symbols": ["BTC"],
                     "etf_detail": {"total_net_usd_m": etf_net_total, "btc_net_usd_m": etf_btc, "date": etf_date}},
                    opportunities, excluded, t,
                    cycle_phase=cycle_phase, n_confirm=2)
            elif etf_btc <= etf_flow_low:
                direction = "short"
                conf = "high" if etf_btc <= etf_flow_low * 2 else "medium"
                base_str = 72 if etf_btc <= etf_flow_low * 2 else 60
                strength = min(92, base_str + int(min(20, (etf_flow_low - etf_btc) / 50)))
                _push_opportunity(
                    {"target": "BTC", "direction": direction, "confidence": conf,
                     "conviction_score": strength,
                     "signal_type": "etf_flow",
                     "key_metric": f"ETF 净流出 ${etf_btc:.0f}M",
                     "trigger_logic": f"BTC ETF 单日净流出 ${etf_btc:.0f}M（{etf_date}）→ 机构资金离场",
                     "action_hint": "机构抛压显现，降低仓位",
                     "invalidation": "ETF 连续 3 日净流入 或 BTC 放量上攻",
                     "related_dims": ["机构ETF资金流（cryptoetf.today）", "P1 机构行为"],
                     "involved_symbols": ["BTC"],
                     "etf_detail": {"total_net_usd_m": etf_net_total, "btc_net_usd_m": etf_btc, "date": etf_date}},
                    opportunities, excluded, t,
                    cycle_phase=cycle_phase, n_confirm=2)

        # D2) 非 BTC 币种 ETF 资金流（按市值分层阈值，最多触发 top 5 异动币种）
        assets_detail = _etf_data.get("assets_detail") or []
        # 按市值规模分层设定阈值（单位：百万美元）
        # L1 龙头：BTC（已单独处理）
        # L2 主流：ETH
        # L3 次主流：SOL, XRP
        # L4 小众：其他
        _etf_tiers = {
            "ETH":   {"high": 30,  "medium": 15,  "base_str_high": 68, "base_str_med": 56, "step": 15},
            "SOL":   {"high": 10,  "medium": 5,   "base_str_high": 65, "base_str_med": 54, "step": 5},
            "XRP":   {"high": 10,  "medium": 5,   "base_str_high": 65, "base_str_med": 54, "step": 5},
            "__default__": {"high": 3, "medium": 1.5, "base_str_high": 62, "base_str_med": 52, "step": 2},
        }
        etf_signals_emitted = 0
        for item in assets_detail:
            sym = item.get("symbol", "")
            if not sym or sym == "BTC":
                continue
            flow = item.get("net_flow_usd_m")
            if flow is None:
                continue
            tier = _etf_tiers.get(sym, _etf_tiers["__default__"])
            abs_flow = abs(flow)
            # 判断是否触发（至少 medium 阈值）
            if flow >= tier["medium"]:
                direction = "long"
                is_high = flow >= tier["high"]
            elif flow <= -tier["medium"]:
                direction = "short"
                is_high = flow <= -tier["high"]
            else:
                continue  # 未达阈值

            conf = "high" if is_high else "medium"
            base_str = tier["base_str_high"] if is_high else tier["base_str_med"]
            exceed = abs_flow - (tier["high"] if is_high else tier["medium"])
            strength = min(90, base_str + int(min(18, exceed / tier["step"])))

            flow_label = f"+${flow:.1f}M" if flow > 0 else f"${flow:.1f}M"
            action = "机构资金加仓，关注联动机会" if direction == "long" else "机构资金离场，短期规避"
            invalid = "ETF 连续 3 日净流出" if direction == "long" else "ETF 连续 3 日净流入"

            _push_opportunity(
                {"target": sym, "direction": direction, "confidence": conf,
                 "conviction_score": strength,
                 "signal_type": "etf_flow",
                 "key_metric": f"ETF 净流入 {flow_label}",
                 "trigger_logic": f"{sym} ETF 单日净流入 {flow_label}（{item.get('date', etf_date)}）→ 机构资金{'加仓' if direction=='long' else '减仓'}",
                 "action_hint": action,
                 "invalidation": invalid,
                 "related_dims": ["机构ETF资金流（cryptoetf.today）", "P1 机构行为"],
                 "involved_symbols": [sym],
                 "etf_detail": {"net_usd_m": flow, "date": item.get("date", etf_date),
                                "tier": "L2主流" if sym == "ETH" else ("L3次主流" if sym in ("SOL", "XRP") else "L4小众")}},
                opportunities, excluded, t,
                cycle_phase=cycle_phase, n_confirm=2)
            etf_signals_emitted += 1
            if etf_signals_emitted >= 5:
                break  # 非 BTC 币种最多 5 个 ETF 信号

    # 按 conviction_score 降序排列
    opportunities.sort(key=lambda x: x.get("conviction_score", 0), reverse=True)

    # P1-2: 多空冲突合并 — 同标的 long+short 合并为"博弈/观望"卡
    _symbol_dir_map: dict[str, dict] = {}  # {symbol: {directions: set, opps: list}}
    for opp in opportunities:
        sym = opp.get("target", "").upper().strip()
        if not sym:
            continue
        d = opp.get("direction", "long")
        entry = _symbol_dir_map.setdefault(sym, {"directions": set(), "opps": [], "tier": opp.get("conviction_tier", "?")})
        entry["directions"].add(d)
        entry["opps"].append(opp)
    # 找出有冲突的方向（同时有 long 和 short）
    conflict_symbols = {sym for sym, info in _symbol_dir_map.items()
                        if len(info["directions"]) > 1}
    if conflict_symbols:
        # 从机会池移除冲突标的
        opportunities = [o for o in opportunities
                         if o.get("target", "").upper().strip() not in conflict_symbols]
        # 合并为博弈卡
        for sym, info in _symbol_dir_map.items():
            if sym not in conflict_symbols:
                continue
            opps_in_conflict = info["opps"]
            max_score = max(o.get("conviction_score", 0) for o in opps_in_conflict)
            trigger_parts = [o.get("trigger_logic", "") for o in opps_in_conflict if o.get("trigger_logic")]
            _push_opportunity(
                {"target": sym, "direction": "watch",
                 "confidence": "medium",
                 "conviction_score": max_score,
                 "signal_type": "catalyst",
                 "key_metric": "多空博弈",
                 "trigger_logic": f"多空信号交织：{' / '.join(trigger_parts[:3])}",
                 "action_hint": "观望，等待多空博弈明朗",
                 "invalidation": "单一方向信号消失后可追",
                 "related_dims": ["catalyst_events", "P1-2 多空博弈"]},
                opportunities, excluded, t,
                cycle_phase=cycle_phase, n_confirm=2,
            )

    # FEAT-HORIZON-001: 为所有信号标注周期 + 时间衰减 + 估值过滤器
    # 获取 BTC MVRV 百分位作为大盘估值参考
    btc_mvrv_pct = None
    btc_info = mvrv_map.get("btc") or mvrv_map.get("BTC")
    if btc_info:
        btc_mvrv_pct = btc_info.get("pct_full")
    apply_horizon_to_opportunities(opportunities, t, btc_mvrv_pct=btc_mvrv_pct)
    # 同步给 excluded 也标上（前端可能展示）
    for o in excluded:
        _annotate_horizon(o, t)

    # 精选高亮信号（FEAT-HIGHLIGHT-001）：与完整机会池分离
    highlight_max_total = int(t.get("highlight_max_total", 10))
    # V2 模式下降级初筛共振门槛（1种即可入池），把质量判断权交给 AI
    ai_v2_enabled_for_init = str(t.get("ai_v2_enabled", "1")) == "1"
    hl_min_resonance = 1 if ai_v2_enabled_for_init else 2
    highlights = select_highlight_signals(
        opportunities, max_total=highlight_max_total,
        min_resonance=hl_min_resonance,
    )

    # 精选高危信号（FEAT-RISK-001）：对称于高亮信号，聚焦看空/风险
    risk_max_total = int(t.get("risk_max_total", 8))
    risk_min_resonance = 1 if ai_v2_enabled_for_init else 1
    risk_signals = select_risk_signals(
        opportunities, max_total=risk_max_total,
        min_resonance=risk_min_resonance,
    )

    # P1：为所有机会/excluded 解析 asset_id，供前端跳转 /research/<asset_id>
    all_symbols: set[str] = set()
    for o in opportunities + excluded:
        all_symbols.update(_symbols_from_opportunity(o))
    symbol_to_asset = _resolve_symbols_to_asset_ids(all_symbols)
    for o in opportunities + excluded:
        syms = _symbols_from_opportunity(o)
        # 优先用 involved_symbols 中第一个能解析到的 asset_id
        asset_id = None
        involved = o.get("involved_symbols") or []
        if isinstance(involved, list):
            for s in involved:
                if s and symbol_to_asset.get(str(s).upper().strip()):
                    asset_id = symbol_to_asset[str(s).upper().strip()]
                    break
        # 否则用 target
        if asset_id is None and o.get("target"):
            asset_id = symbol_to_asset.get(str(o["target"]).upper().strip())
        if asset_id is not None:
            o["asset_id"] = asset_id

    # FEAT-AI-HIGHLIGHT: AI 增强精选（在 asset_id 解析之后，确保信号有 asset_id）
    ai_enabled = str(t.get("ai_highlight_enabled", "1")) == "1"
    ai_v2_enabled = str(t.get("ai_v2_enabled", "1")) == "1"  # V2 开关：全量画像+信号分类过滤
    if ai_enabled and highlights:
        try:
            if ai_v2_enabled:
                # V2：信号分类过滤 + 全量画像 + 六维评分卡
                from ai_signal_analyzer import ai_enrich_signals_v2
                ai_max = int(t.get("ai_highlight_max", 15))
                highlights = ai_enrich_signals_v2(highlights, direction="long", max_ai_review=ai_max)
            else:
                # V1（旧版保留作 fallback）
                from ai_signal_analyzer import ai_enrich_highlight_signals
                ai_max = int(t.get("ai_highlight_max", 10))
                highlights = ai_enrich_highlight_signals(highlights, max_ai_analyze=ai_max)
        except Exception:
            pass  # AI 增强失败不影响主流程
    if ai_enabled and risk_signals:
        try:
            if ai_v2_enabled:
                # V2：高危信号对称改造
                from ai_signal_analyzer import ai_enrich_signals_v2
                ai_max_risk = int(t.get("ai_risk_max", 12))
                risk_signals = ai_enrich_signals_v2(risk_signals, direction="short", max_ai_review=ai_max_risk)
            else:
                # V1（旧版保留作 fallback）
                from ai_signal_analyzer import ai_enrich_risk_signals
                ai_max_risk = int(t.get("ai_risk_max", 8))
                risk_signals = ai_enrich_risk_signals(risk_signals, max_ai_analyze=ai_max_risk)
        except Exception:
            pass  # AI 增强失败不影响主流程

    # 为 highlights 和 risk_signals 也补上 asset_id（AI 增强后可能有新信号或丢失的情况）
    for sig_list in [highlights, risk_signals]:
        for o in sig_list:
            if not o.get("asset_id") and o.get("target"):
                o["asset_id"] = symbol_to_asset.get(str(o["target"]).upper().strip())

    status = "ok"
    if not opportunities:
        # 0 机会但上游数据齐全（平静日）= empty，非故障
        status = "empty" if not degraded else "partial"
    elif degraded:
        status = "partial"

    # P1：检查观察列表告警（轻量，失败不影响主返回）
    watch_alerts = {"ok": True, "alerts": [], "count": 0}
    try:
        from db_stats import check_opportunity_watchlist_alerts
        watch_alerts = check_opportunity_watchlist_alerts(opportunities)
    except Exception:
        pass

    # P1：催化剂事件板块（大盘级事件，按影响权重排序）
    catalyst_events = {"catalyst_events": [], "total": 0, "window_days": 14}
    try:
        cat_window = int(t.get("catalyst_window_days", 14))
        cat_limit = int(t.get("catalyst_market_limit", 50))
        catalyst_events = get_market_catalysts(window_days=cat_window, limit=cat_limit)
    except Exception:
        pass

    # 链上异动板块（KOL 分析博主发现的链上大额异动）
    onchain_anomalies = {"anomalies": [], "total": 0, "hours": 24, "n_kols": 0}
    try:
        oa_hours = int(t.get("onchain_anomaly_hours", 24))
        oa_limit = int(t.get("onchain_anomaly_limit", 20))
        onchain_anomalies = get_onchain_anomalies(hours=oa_hours, limit=oa_limit)
    except Exception:
        pass

    return {
        "status": status,
        "opportunities": opportunities,
        "highlight_signals": highlights,
        "risk_signals": risk_signals,
        "excluded": excluded,
        "degraded": degraded,
        "watchlist_alerts": watch_alerts,
        "catalyst_events": catalyst_events,
        "onchain_anomalies": onchain_anomalies,
    }


# ══════════════════════════════════════════════════════════════
# P0-3 共振榜：共识动量 ∩ 宏观 conviction
# ══════════════════════════════════════════════════════════════

def _symbols_from_opportunity(opp: dict) -> set[str]:
    """从机会条目中提取可交叉的 symbol 集合。"""
    involved = opp.get("involved_symbols") or []
    if isinstance(involved, list) and involved:
        syms = {str(s).upper().strip() for s in involved if s is not None}
        if syms:
            return syms
    target = opp.get("target")
    if isinstance(target, str):
        t = target.upper().strip()
        # 单一代币代码（如 BTC / ETH / 1INCH），排除中文描述性 target
        if re.match(r"^[A-Z0-9]{2,10}$", t):
            return {t}
    return set()


def _resolve_symbols_to_asset_ids(symbols: set[str]) -> dict[str, int]:
    """批量 symbol → asset_id 映射。返回 {symbol_upper: asset_id}。"""
    if not symbols:
        return {}
    try:
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection

        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                placeholders = ",".join(["%s"] * len(symbols))
                cur.execute(f"""
                    SELECT DISTINCT ON (UPPER(canonical_symbol))
                           UPPER(canonical_symbol) AS symbol,
                           asset_id
                    FROM core.asset
                    WHERE UPPER(canonical_symbol) IN ({placeholders})
                      AND canonical_name NOT LIKE '%Bridged%'
                      AND canonical_name NOT LIKE '%Wrapped%'
                      AND canonical_name NOT LIKE '%Peg %'
                    ORDER BY UPPER(canonical_symbol), asset_id
                """, tuple(s.upper().strip() for s in symbols))
                rows = cur.fetchall()
        mapping = {r["symbol"]: int(r["asset_id"]) for r in rows if r.get("symbol") and r.get("asset_id") is not None}

        # 对未命中的 symbol 回退（不排除桥接币）
        unresolved = symbols - set(mapping.keys())
        if unresolved:
            with get_connection(settings.database_url) as conn:
                with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                    placeholders = ",".join(["%s"] * len(unresolved))
                    cur.execute(f"""
                        SELECT DISTINCT ON (UPPER(canonical_symbol))
                               UPPER(canonical_symbol) AS symbol,
                               asset_id
                        FROM core.asset
                        WHERE UPPER(canonical_symbol) IN ({placeholders})
                        ORDER BY UPPER(canonical_symbol), asset_id
                    """, tuple(s.upper().strip() for s in unresolved))
                    rows = cur.fetchall()
            for r in rows:
                if r.get("symbol") and r.get("asset_id") is not None:
                    mapping[r["symbol"]] = int(r["asset_id"])
        return mapping
    except Exception:
        return {}


def _fetch_daily_recommendation_latest() -> dict[str, dict]:
    """读取 biz.daily_recommendation 最新日全量推荐，返回 {symbol_upper: row_dict}。"""
    try:
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection

        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute("""
                    SELECT rec_date, rank, symbol, name, chain, contract, sector,
                           source_count, composite_score, change_24h, volume_24h,
                           price_usd, market_cap_usd
                    FROM biz.daily_recommendation
                    WHERE rec_date = (SELECT MAX(rec_date) FROM biz.daily_recommendation)
                    ORDER BY rank ASC
                """)
                rows = cur.fetchall()
        result: dict[str, dict] = {}
        for r in rows:
            sym = str(r.get("symbol") or "").upper().strip()
            if sym and sym not in result:
                result[sym] = dict(r)
        return result
    except Exception:
        return {}


def build_resonance_signals(overview: dict) -> dict:
    """
    共振榜：每日共识推荐（daily_recommendation）与宏观 conviction（HIGH/MED 机会）的交集。

    P1 修复：
      - 共识来源从 cross_market 改为 biz.daily_recommendation（与工单对齐）；
      - conviction 范围从仅 HIGH 扩展为 HIGH + MED；
      - 交集为空时保留原 cross_market 逻辑作为 fallback，避免空态。

    返回 {status, definition, signals, count, source}。
    """
    t = _load_market_rules().get("opportunity_thresholds", dict(OPPORTUNITY_THRESHOLDS_DEFAULT))
    min_sources = int(t.get("resonance_min_source_count", 2))
    consensus_top_n = int(t.get("resonance_consensus_top_n", 50))
    max_results = int(t.get("resonance_max_results", 10))
    definition = "每日共识推荐（daily_recommendation）与宏观 conviction（HIGH/MED）的交集"

    # 优先从 daily_recommendation 读共识
    consensus_map = _fetch_daily_recommendation_latest()
    source = "daily_recommendation"

    # fallback：daily_recommendation 为空时回退 cross_market
    if not consensus_map:
        try:
            import cross_market

            consensus_data = cross_market.get_cross_validated(limit=consensus_top_n)
            consensus_results = (consensus_data or {}).get("results") or []
            consensus_results = [
                r for r in consensus_results
                if isinstance(r, dict) and r.get("source_count", 0) >= min_sources
            ]
            consensus_results.sort(
                key=lambda x: (x.get("source_count", 0), x.get("composite_score", 0)),
                reverse=True,
            )
            for r in consensus_results:
                sym = str(r.get("symbol") or "").upper().strip()
                if sym and sym not in consensus_map:
                    consensus_map[sym] = r
            source = "cross_market"
        except Exception:
            pass

    opps = (overview.get("opportunity_list") or {}).get("opportunities") or []
    # P1：HIGH + MED 均参与共振
    valid_opps = [o for o in opps if o.get("conviction_tier") in ("HIGH", "MED")]

    signals: list[dict] = []
    seen: set[str] = set()
    for opp in valid_opps:
        opp_syms = _symbols_from_opportunity(opp)
        if not opp_syms:
            continue
        for sym in opp_syms:
            if sym in seen or sym not in consensus_map:
                continue
            con = consensus_map[sym]
            seen.add(sym)
            signals.append({
                "symbol": sym,
                "direction": opp.get("direction", "long"),
                "conviction_score": opp.get("conviction_score"),
                "conviction_tier": opp.get("conviction_tier"),
                "consensus_score": con.get("composite_score"),
                "source_count": con.get("source_count"),
                "consensus": con.get("consensus"),
                "change_24h": con.get("change_24h"),
                "volume_24h": con.get("volume_24h"),
                "sector": con.get("sector"),
                "trigger_logic": opp.get("trigger_logic", ""),
                "action_hint": opp.get("action_hint", ""),
                "invalidation": opp.get("invalidation", ""),
                "related_dims": opp.get("related_dims", []),
            })
            if len(signals) >= max_results:
                break
        if len(signals) >= max_results:
            break

    # 最终排序：conviction 优先，其次 consensus 综合分
    signals.sort(
        key=lambda x: ((x.get("conviction_score") or 0), (x.get("consensus_score") or 0)),
        reverse=True,
    )

    status = "ok" if signals else "empty"
    return {
        "status": status,
        "definition": definition,
        "signals": signals,
        "count": len(signals),
        "source": source if consensus_map else None,
    }


# ══════════════════════════════════════════════════════════════
# P1-4 第二刀 · 早报趋势层（走势感 + 流动性 regime）
# ══════════════════════════════════════════════════════════════

def _ensure_snapshot_table(conn) -> None:
    """确保快照表存在（Zeabur FS ephemeral，必须落 DB）。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS biz.market_overview_snapshot (
                snap_date  DATE PRIMARY KEY,
                payload    JSONB NOT NULL,
                created_at TIMESTAMPTZ DEFAULT now()
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_snap_date ON biz.market_overview_snapshot (snap_date DESC)"
        )


def save_snapshot(snap_date: str, overview: dict) -> None:
    """落库当日 overview 快照，供次日 diff。"""
    import json

    from db_stats import get_db

    with get_db() as conn:
        _ensure_snapshot_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO biz.market_overview_snapshot (snap_date, payload)
                VALUES (%s, %s::jsonb)
                ON CONFLICT (snap_date) DO UPDATE SET
                    payload = EXCLUDED.payload, created_at = now()
                """,
                (snap_date, json.dumps(overview, default=str)),
            )


def load_snapshot(snap_date: str) -> dict | None:
    """读某日 overview 快照；无则返回 None（表未建/无行均返回 None，不报错）。"""
    from db_stats import get_db

    with get_db() as conn:
        _ensure_snapshot_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT payload FROM biz.market_overview_snapshot WHERE snap_date = %s",
                (snap_date,),
            )
            row = cur.fetchone()
            return dict(row[0]) if row and row[0] else None


def _pct(cur, prev):
    """百分比变化（%）；prev 缺失/0 返回 None。"""
    if prev in (None, 0):
        return None
    return round((cur - prev) / abs(prev) * 100, 2)


def _fg_value(sub: dict) -> float | None:
    """从 emotion_subscore 取恐贪原始值（components.fear_greed.value），缺失回退 composite score。"""
    comp = ((sub or {}).get("components") or {}).get("fear_greed") or {}
    v = comp.get("value")
    if v is None:
        v = (sub or {}).get("score")
    return v


def _flow_dir_change(y_flow, t_flow):
    """机构 ETF 净流方向变化：in/out/flat × 前后 → 'in→out' 等；缺失 None。"""
    def _sign(v):
        if v is None:
            return None
        if v > 100:
            return "in"
        if v < -100:
            return "out"
        return "flat"

    ys, ts = _sign(y_flow), _sign(t_flow)
    if ys is None or ts is None:
        return None
    if ys == ts:
        return "stable"
    return f"{ys}→{ts}"


def _netflow_slope(t: dict) -> float | None:
    """链上净流 7d 斜率（近 7d 均值 vs 前 7d 均值 %）。字段缺失 → None（降级铁律）。"""
    oc = (t.get("onchain_anomaly_signals") or {}).get("daily_netflows_30d")
    if not isinstance(oc, list):
        return None
    pts = [
        float(x.get("netflow"))
        for x in oc
        if isinstance(x, dict) and x.get("netflow") is not None
    ]
    if len(pts) < 14:
        return None
    recent = sum(pts[-7:]) / 7
    prior = sum(pts[-14:-7]) / 7
    if prior == 0:
        return None
    return round((recent - prior) / abs(prior) * 100, 2)


def diff_overview(y: dict, t: dict) -> dict:
    """字段级 diff（走势感主干）。y=昨日，t=今日；缺失项显式 None，不插值不补 0。"""
    yg = ((y.get("dimensions") or {}).get("1体量") or {}).get("data") or {}
    tg = ((t.get("dimensions") or {}).get("1体量") or {}).get("data") or {}
    ye = (y.get("summary") or {}).get("emotion_subscore") or {}
    te = (t.get("summary") or {}).get("emotion_subscore") or {}
    yf = ((y.get("dimensions") or {}).get("4机构") or {}).get("data") or {}
    tf = ((t.get("dimensions") or {}).get("4机构") or {}).get("data") or {}

    y_opps = {o.get("target"): o for o in ((y.get("opportunity_list") or {}).get("opportunities") or [])}
    t_opps = {o.get("target"): o for o in ((t.get("opportunity_list") or {}).get("opportunities") or [])}
    new_high = [
        k for k, o in t_opps.items()
        if o.get("conviction_tier") == "HIGH" and k not in y_opps
    ]
    gone = [k for k in y_opps if k not in t_opps]

    return {
        "total_mcap_pct": _pct(tg.get("total_market_cap"), yg.get("total_market_cap")),
        "btc_dom_pct_chg": round(
            (tg.get("btc_dominance") or 0) - (yg.get("btc_dominance") or 0), 2
        ),
        "fear_greed_chg": round((_fg_value(te) or 0) - (_fg_value(ye) or 0), 2),
        "inst_netflow_dir": _flow_dir_change(
            yf.get("net_flow_usd_m"), tf.get("net_flow_usd_m")
        ),
        "onchain_7d_slope": _netflow_slope(t),
        "new_high_opps": new_high,
        "gone_opps": gone,
    }


def fetch_stablecoin_supply_trend() -> dict:
    """DeFiLlama 免费：稳定币总供给 + 1d/7d 环比（stablecoincharts/All 末点差分）。
    返回 {total_usd, change_1d_pct, change_7d_pct, status}；失败 status=error（降级不显 0）。"""
    try:
        r = requests.get(
            "https://stablecoins.llama.fi/stablecoincharts/All",
            timeout=30,  # 响应 ~1.2MB，给足读超时
        )
        r.raise_for_status()
        rows = r.json()
        if not isinstance(rows, list) or not rows:
            return {"total_usd": None, "change_1d_pct": None, "change_7d_pct": None, "status": "error"}
        def _val(row):
            return ((row or {}).get("totalCirculating") or {}).get("peggedUSD")
        pts = [v for v in (_val(x) for x in rows) if v is not None]
        if not pts:
            return {"total_usd": None, "change_1d_pct": None, "change_7d_pct": None, "status": "error"}
        total = _safe_float(pts[-1])
        return {
            "total_usd": total,
            "change_1d_pct": _pct(pts[-1], pts[-2]) if len(pts) >= 2 else None,
            "change_7d_pct": _pct(pts[-1], pts[-8]) if len(pts) >= 8 else None,
            "status": "ok",
        }
    except Exception as e:
        return {"total_usd": None, "change_1d_pct": None, "change_7d_pct": None, "status": "error", "error": str(e)}


def _build_tldr(today: dict, opps: list, highlights: list | None = None,
                 risk_signals: list | None = None) -> dict:
    """M0 头部：从 overview 抽取关键指标 + 一句话摘要 + 多空倾向。

    升级（BRIEF-OPT-001）：
    - 加入高亮信号/高危信号数量
    - 根据机会-风险比值给出多空倾向（偏多/偏空/震荡）
    - 给出操作建议（进攻/防守/观望）
    """
    dims = today.get("dimensions") or {}
    btc_data = ((dims.get("2盘面") or {}).get("data") or {}).get("btc") or {}
    fg_data = ((dims.get("3情绪") or {}).get("data") or {}).get("fear_greed") or {}
    mvrv_dim = (dims.get("mvrv_universe") or {}).get("data") or {}
    mvrv_coins = mvrv_dim.get("coins") or []
    btc_mvrv = next((c for c in mvrv_coins if c.get("symbol") == "BTC"), {})
    cycle = today.get("btc_cycle") or {}

    highlights = highlights or []
    risk_signals = risk_signals or []
    n_high = len(highlights)
    n_risk = len(risk_signals)

    # 多空倾向：机会数 vs 风险数的比值 + 周期阶段调整
    if n_risk == 0 and n_high > 0:
        bias = "偏多"
    elif n_high == 0 and n_risk > 0:
        bias = "偏空"
    elif n_high == 0 and n_risk == 0:
        bias = "震荡"
    else:
        ratio = n_high / max(n_risk, 1)
        if ratio >= 1.5:
            bias = "偏多"
        elif ratio <= 0.6:
            bias = "偏空"
        else:
            bias = "震荡"

    # 操作建议：结合周期阶段 + 恐贪 + 多空比
    fg_val = fg_data.get("value")
    cycle_phase = cycle.get("phase") or ""
    if bias == "偏多" and cycle_phase in ("accumulation", "recovery"):
        action = "进攻"
    elif bias == "偏空":
        action = "防守"
    elif fg_val is not None and fg_val > 85:
        action = "谨慎"
    elif fg_val is not None and fg_val < 20:
        action = "布局"
    else:
        action = "观望"

    high_summary = f"{n_high} 条（{', '.join(str(o.get('target')) for o in highlights[:3])}）" if highlights else "无"
    risk_summary = f"{n_risk} 条（{', '.join(str(o.get('target')) for o in risk_signals[:3])}）" if risk_signals else "无"

    return {
        "btc_price": btc_data.get("price"),
        "btc_change_24h": btc_data.get("change_24h"),
        "fear_greed": fg_val,
        "fear_greed_label": fg_data.get("value_classification"),
        "btc_cycle_phase": cycle.get("phase_label") or cycle.get("phase"),
        "btc_mvrv_pct": btc_mvrv.get("pct_full"),
        "total_market_cap": ((dims.get("1体量") or {}).get("data") or {}).get("total_market_cap"),
        "btc_dominance": ((dims.get("1体量") or {}).get("data") or {}).get("btc_dominance"),
        "n_highlights": n_high,
        "n_risks": n_risk,
        "bias": bias,
        "action": action,
        "highlights_summary": high_summary,
        "risks_summary": risk_summary,
        "summary": (
            f"BTC 周期：{cycle.get('phase_label') or cycle.get('phase') or '未知'}；"
            f"今日倾向：{bias}（{action}）；"
            f"机会 {high_summary}；风险 {risk_summary}"
        ),
    }


def _build_flow(today: dict, diff: dict | None, stab: dict) -> dict:
    g = ((today.get("dimensions") or {}).get("1体量") or {}).get("data") or {}
    return {
        "total_market_cap": g.get("total_market_cap"),
        "btc_dominance": g.get("btc_dominance"),
        "total_mcap_pct_chg": (diff or {}).get("total_mcap_pct"),
        "stablecoin_total_usd": stab.get("total_usd"),
        "stablecoin_change_1d_pct": stab.get("change_1d_pct"),
        "stablecoin_change_7d_pct": stab.get("change_7d_pct"),
        "stablecoin_status": stab.get("status"),
    }


def _collect_degraded(today: dict) -> list[str]:
    out: list[str] = []
    for k, v in (today.get("dimensions") or {}).items():
        if isinstance(v, dict) and v.get("status") in ("error", "degraded"):
            out.append(f"{k}: {v.get('status')}")
    for block in ("divergence_signals", "opportunity_list"):
        b = today.get(block) or {}
        for d in (b.get("degraded") or []):
            out.append(f"{block}: {d}")
    return out


def fetch_sector_flow_with_leaders() -> dict:
    """12 赛道资金流向 + 各赛道领涨币（用于早报）。

    数据口径说明：
    - 资金流用「市值变化率」近似（真实净流入 ETL 待实现，flow_7d_usd 暂为 0）
    - 24h 成交量从 CMC 快照聚合
    - 领涨币按 7d 涨幅排序，过滤掉市值过小的（< 10M）
    """
    try:
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection
        import psycopg.rows

        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                # 1) 12 赛道市值变化 + 基础信息
                cur.execute("""
                    SELECT sector_type, sector_key, sector_label, metric_date,
                           market_cap, mcap_change_1d_pct, mcap_change_7d_pct, mcap_change_30d_pct,
                           coin_count, composite_score, mode
                    FROM biz.sector_flow_daily
                    WHERE metric_date = (SELECT MAX(metric_date) FROM biz.sector_flow_daily)
                      AND sector_type = 'sector_12'
                    ORDER BY mcap_change_7d_pct DESC NULLS LAST
                """)
                sectors = [dict(r) for r in cur.fetchall()]

                if not sectors:
                    return {"status": "empty", "sectors": []}

                # 2) 每个赛道取领涨币 TOP3（按 7d 涨幅，市值 > 10M 过滤小币）
                for s in sectors:
                    cur.execute("""
                        SELECT a.canonical_symbol AS symbol, a.canonical_name AS name,
                               q.market_cap, q.percent_change_24h, q.percent_change_7d,
                               q.volume_24h
                        FROM biz.asset_sector s
                        JOIN core.asset a ON s.asset_id = a.asset_id
                        JOIN core.asset_source_map asm
                          ON a.asset_id = asm.asset_id AND asm.source_code = 'cmc'
                        JOIN (
                            SELECT DISTINCT ON (cmc_id) cmc_id, market_cap,
                                   percent_change_24h, percent_change_7d, volume_24h, quote_time
                            FROM src_cmc.cmc_asset_quote_snapshot
                            WHERE market_cap IS NOT NULL
                              AND quote_time::date = (
                                SELECT MAX(quote_time)::date FROM src_cmc.cmc_asset_quote_snapshot
                              )
                            ORDER BY cmc_id, quote_time DESC
                        ) q ON asm.source_asset_key::bigint = q.cmc_id
                        WHERE s.sector = %s AND s.is_primary = TRUE
                          AND q.market_cap >= 10000000
                        ORDER BY q.percent_change_7d DESC NULLS LAST
                        LIMIT 3
                    """, (s["sector_key"],))
                    s["leaders"] = [dict(r) for r in cur.fetchall()]

                # 3) 全市场 24h 总成交量
                cur.execute("""
                    SELECT SUM(volume_24h) as total_volume
                    FROM (
                        SELECT DISTINCT ON (cmc_id) cmc_id, volume_24h
                        FROM src_cmc.cmc_asset_quote_snapshot
                        WHERE quote_time::date = (
                            SELECT MAX(quote_time)::date FROM src_cmc.cmc_asset_quote_snapshot
                        )
                        ORDER BY cmc_id, quote_time DESC
                    ) q
                """)
                total_vol = cur.fetchone()
                total_volume = float(total_vol["total_volume"]) if total_vol and total_vol["total_volume"] else 0

                return {
                    "status": "ok",
                    "sectors": sectors,
                    "total_volume_24h": total_volume,
                    "metric_date": sectors[0].get("metric_date"),
                }
    except Exception as e:
        return {"status": "error", "sectors": [], "error": str(e)}


def fetch_kol_onchain_signals(hours: int = 24, limit: int = 10) -> dict:
    """KOL 链上分析师近 N 小时信号（用于早报链上异动板块）。

    只取 onchain 类信号（exchange_flow / smart_money / accumulation /
    whale_move / distribution / liquidation）。
    """
    try:
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection
        import psycopg.rows

        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute("""
                    SELECT s.signal_id, s.created_at, s.signal_category, s.signal_subtype,
                           s.symbol, s.event_direction, s.event_amount, s.event_token,
                           s.event_usd_value, s.tx_hash, s.address_label, s.event_exchange,
                           s.event_time, s.confidence, s.address_label as reason,
                           p.nickname as kol_name
                    FROM biz.kol_signal s
                    JOIN biz.kol_profile p ON s.profile_id = p.profile_id
                    WHERE s.signal_category = 'onchain'
                      AND s.created_at >= NOW() - (%s || ' hours')::INTERVAL
                    ORDER BY s.created_at DESC
                    LIMIT %s
                """, (hours, limit))
                signals = [dict(r) for r in cur.fetchall()]

                # 统计各子类型数量
                cur.execute("""
                    SELECT signal_subtype, count(*) as cnt
                    FROM biz.kol_signal
                    WHERE signal_category = 'onchain'
                      AND created_at >= NOW() - (%s || ' hours')::INTERVAL
                    GROUP BY signal_subtype
                    ORDER BY cnt DESC
                """, (hours,))
                stats = [dict(r) for r in cur.fetchall()]

                # 有哪些 KOL
                cur.execute("""
                    SELECT DISTINCT p.nickname
                    FROM biz.kol_signal s
                    JOIN biz.kol_profile p ON s.profile_id = p.profile_id
                    WHERE s.signal_category = 'onchain'
                      AND s.created_at >= NOW() - (%s || ' hours')::INTERVAL
                """, (hours,))
                kols = [r["nickname"] for r in cur.fetchall()]

                return {
                    "status": "ok",
                    "signals": signals,
                    "stats": stats,
                    "kols": kols,
                    "hours": hours,
                }
    except Exception as e:
        return {"status": "error", "signals": [], "error": str(e)}


def fetch_etf_flow_trend(days: int = 7) -> dict:
    """ETF 资金流趋势（用于早报）。

    从 biz.etf_flow_daily 读取，返回各资产近 N 日净流入趋势 + 当日数据。
    """
    try:
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection
        import psycopg.rows

        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                # 最新日期
                cur.execute("SELECT MAX(flow_date) AS latest FROM biz.etf_flow_daily")
                latest_row = cur.fetchone()
                if not latest_row or not latest_row["latest"]:
                    return {"status": "empty", "assets": []}
                latest_date = latest_row["latest"]

                # 近 N 日各资产净流入 + AUM
                cur.execute("""
                    SELECT symbol, flow_date, net_flow_usd, net_flow_usd_m, aum_usd,
                           total_inflow_usd, total_outflow_usd
                    FROM biz.etf_flow_daily
                    WHERE flow_date >= %s::DATE - (%s || ' days')::INTERVAL
                      AND net_flow_usd IS NOT NULL
                    ORDER BY symbol, flow_date DESC
                """, (latest_date, days))
                rows = [dict(r) for r in cur.fetchall()]

                # 按 symbol 聚合
                assets: dict[str, dict] = {}
                for r in rows:
                    sym = r["symbol"]
                    if sym not in assets:
                        assets[sym] = {
                            "symbol": sym,
                            "latest_flow_usd": None,
                            "latest_flow_date": None,
                            "flow_7d_usd": 0,
                            "aum_usd": None,
                            "daily_data": [],
                        }
                    a = assets[sym]
                    if a["latest_flow_date"] is None:
                        a["latest_flow_usd"] = float(r["net_flow_usd"] or 0)
                        a["latest_flow_date"] = str(r["flow_date"])
                        a["aum_usd"] = float(r["aum_usd"]) if r["aum_usd"] else None
                    a["flow_7d_usd"] += float(r["net_flow_usd"] or 0)
                    a["daily_data"].append({
                        "date": str(r["flow_date"]),
                        "net_flow_usd": float(r["net_flow_usd"] or 0),
                    })

                asset_list = sorted(
                    assets.values(),
                    key=lambda x: abs(x["flow_7d_usd"] or 0),
                    reverse=True,
                )

                return {
                    "status": "ok" if asset_list else "empty",
                    "latest_date": str(latest_date),
                    "assets": asset_list,
                    "days": days,
                }
    except Exception as e:
        return {"status": "error", "assets": [], "error": str(e)}


def fetch_onchain_whale_moves(hours: int = 24, limit: int = 10) -> dict:
    """链上大额转账异动（用于早报链上异动板块）。

    从 biz.onchain_transfer_log 读取近 N 小时大额转账，按金额排序。
    区分：交易所充值/提现、巨鲸地址间转账。
    """
    try:
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection
        import psycopg.rows

        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute("""
                    SELECT t.log_id, t.asset_id, t.chain, t.value_usd,
                           t.from_address, t.to_address, t.from_label, t.to_label,
                           t.from_exchange, t.to_exchange, t.tx_hash,
                           t.block_timestamp,
                           a.canonical_symbol AS symbol, a.canonical_name AS name
                    FROM biz.onchain_transfer_log t
                    JOIN core.asset a ON a.asset_id = t.asset_id
                    WHERE t.block_timestamp >= NOW() - (%s || ' hours')::INTERVAL
                      AND t.value_usd >= 1000000
                    ORDER BY t.value_usd DESC
                    LIMIT %s
                """, (hours, limit))
                transfers = [dict(r) for r in cur.fetchall()]

                # 按类型分类
                exchange_in = [t for t in transfers if t.get("to_exchange")]
                exchange_out = [t for t in transfers if t.get("from_exchange") and not t.get("to_exchange")]
                whale_to_whale = [t for t in transfers if not t.get("from_exchange") and not t.get("to_exchange")]

                # 统计维度
                total_usd = sum(float(t["value_usd"] or 0) for t in transfers)
                exchange_in_usd = sum(float(t["value_usd"] or 0) for t in exchange_in)
                exchange_out_usd = sum(float(t["value_usd"] or 0) for t in exchange_out)
                net_exchange_usd = exchange_in_usd - exchange_out_usd

                return {
                    "status": "ok" if transfers else "empty",
                    "transfers": transfers,
                    "total_count": len(transfers),
                    "total_usd": total_usd,
                    "exchange_in_count": len(exchange_in),
                    "exchange_in_usd": exchange_in_usd,
                    "exchange_out_count": len(exchange_out),
                    "exchange_out_usd": exchange_out_usd,
                    "net_exchange_usd": net_exchange_usd,
                    "whale_to_whale_count": len(whale_to_whale),
                    "hours": hours,
                }
    except Exception as e:
        return {"status": "error", "transfers": [], "error": str(e)}


def fetch_holder_concentration_summary(top_n: int = 20) -> dict:
    """持仓集中度摘要（用于早报）。

    从 biz.onchain_holder_snapshot 读取最新快照，
    返回 Top10 集中度最高/最低的资产 + 巨鲸余额变化趋势。
    """
    try:
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection
        import psycopg.rows

        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                # 最新快照日期
                cur.execute("SELECT MAX(snapshot_date) AS latest FROM biz.onchain_holder_snapshot")
                latest_row = cur.fetchone()
                if not latest_row or not latest_row["latest"]:
                    return {"status": "empty", "assets": []}
                latest_date = latest_row["latest"]

                # 集中度 Top N（最集中）- 过滤市值前500
                cur.execute("""
                    SELECT h.asset_id, h.chain, h.top10_concentration, h.top50_concentration,
                           h.total_holders, h.whale_balance_change_7d_pct,
                           h.exchange_wallet_pct, h.smart_money_pct,
                           a.canonical_symbol AS symbol, a.canonical_name AS name,
                           a.market_cap_rank
                    FROM biz.onchain_holder_snapshot h
                    JOIN core.asset a ON a.asset_id = h.asset_id
                    WHERE h.snapshot_date = %s
                      AND a.market_cap_rank <= 500
                      AND h.top10_concentration IS NOT NULL
                    ORDER BY h.top10_concentration DESC
                    LIMIT %s
                """, (latest_date, top_n))
                most_concentrated = [dict(r) for r in cur.fetchall()]

                # 巨鲸 7 日增仓 Top N
                cur.execute("""
                    SELECT h.asset_id, h.chain, h.top10_concentration,
                           h.whale_balance_change_7d_pct,
                           a.canonical_symbol AS symbol, a.canonical_name AS name,
                           a.market_cap_rank
                    FROM biz.onchain_holder_snapshot h
                    JOIN core.asset a ON a.asset_id = h.asset_id
                    WHERE h.snapshot_date = %s
                      AND a.market_cap_rank <= 500
                      AND h.whale_balance_change_7d_pct IS NOT NULL
                      AND h.whale_balance_change_7d_pct != 0
                    ORDER BY h.whale_balance_change_7d_pct DESC
                    LIMIT %s
                """, (latest_date, top_n))
                whale_buying = [dict(r) for r in cur.fetchall()]

                # 巨鲸 7 日减仓 Top N
                cur.execute("""
                    SELECT h.asset_id, h.chain, h.top10_concentration,
                           h.whale_balance_change_7d_pct,
                           a.canonical_symbol AS symbol, a.canonical_name AS name,
                           a.market_cap_rank
                    FROM biz.onchain_holder_snapshot h
                    JOIN core.asset a ON a.asset_id = h.asset_id
                    WHERE h.snapshot_date = %s
                      AND a.market_cap_rank <= 500
                      AND h.whale_balance_change_7d_pct IS NOT NULL
                      AND h.whale_balance_change_7d_pct != 0
                    ORDER BY h.whale_balance_change_7d_pct ASC
                    LIMIT %s
                """, (latest_date, top_n))
                whale_selling = [dict(r) for r in cur.fetchall()]

                return {
                    "status": "ok",
                    "snapshot_date": str(latest_date),
                    "most_concentrated": most_concentrated,
                    "whale_buying": whale_buying,
                    "whale_selling": whale_selling,
                }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def fetch_exchange_net_flow_summary(days: int = 7) -> dict:
    """交易所净流量摘要（从 CM 链上数据）。

    从 biz.cm_asset_onchain_daily 读取主流币的交易所净流入/流出。
    """
    try:
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection
        import psycopg.rows

        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                # 最新日期
                cur.execute("SELECT MAX(metric_date) AS latest FROM biz.cm_asset_onchain_daily")
                latest_row = cur.fetchone()
                if not latest_row or not latest_row["latest"]:
                    return {"status": "empty", "assets": []}
                latest_date = latest_row["latest"]

                # 近 N 日各资产交易所净流量
                cur.execute("""
                    SELECT a.canonical_symbol AS symbol, a.canonical_name AS name,
                           d.asset_id,
                           SUM(d.flow_in_ex_usd) AS total_in_usd,
                           SUM(d.flow_out_ex_usd) AS total_out_usd,
                           SUM(d.flow_in_ex_usd - d.flow_out_ex_usd) AS net_flow_usd,
                           (SELECT net_1d.flow_in_ex_usd - net_1d.flow_out_ex_usd
                            FROM biz.cm_asset_onchain_daily net_1d
                            WHERE net_1d.asset_id = d.asset_id
                              AND net_1d.metric_date = MAX(d.metric_date)
                            LIMIT 1) AS latest_net_usd
                    FROM biz.cm_asset_onchain_daily d
                    JOIN core.asset a ON a.asset_id = d.asset_id
                    WHERE d.metric_date >= %s::DATE - (%s || ' days')::INTERVAL
                      AND d.flow_in_ex_usd IS NOT NULL
                      AND d.flow_out_ex_usd IS NOT NULL
                      AND a.market_cap_rank <= 100
                    GROUP BY a.canonical_symbol, a.canonical_name, d.asset_id
                    ORDER BY ABS(SUM(d.flow_in_ex_usd - d.flow_out_ex_usd)) DESC
                    LIMIT 20
                """, (latest_date, days))
                assets = [dict(r) for r in cur.fetchall()]

                return {
                    "status": "ok" if assets else "empty",
                    "latest_date": str(latest_date),
                    "assets": assets,
                    "days": days,
                }
    except Exception as e:
        return {"status": "error", "assets": [], "error": str(e)}


def fetch_upcoming_unlocks(days: int = 14) -> dict:
    """即将解锁事件（用于早报催化剂板块）。

    从 biz.asset_unlock_event 读取未来 N 天解锁事件，按解锁金额排序。
    """
    try:
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection
        import psycopg.rows

        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute("""
                    SELECT e.asset_id, e.unlock_date, e.unlock_type, e.unlock_amount,
                           e.unlock_ratio_total, e.unlock_ratio_circulating,
                           e.unlock_ratio_mcap, e.unlock_value_usd, e.risk_level,
                           e.beneficiary_type, e.source_code,
                           a.canonical_symbol AS symbol, a.canonical_name AS name,
                           a.market_cap_rank
                    FROM biz.asset_unlock_event e
                    JOIN core.asset a ON a.asset_id = e.asset_id
                    WHERE e.unlock_date >= CURRENT_DATE
                      AND e.unlock_date <= CURRENT_DATE + (%s || ' days')::INTERVAL
                      AND e.unlock_value_usd >= 1000000
                    ORDER BY e.unlock_value_usd DESC NULLS LAST
                    LIMIT 20
                """, (days,))
                unlocks = [dict(r) for r in cur.fetchall()]

                return {
                    "status": "ok" if unlocks else "empty",
                    "unlocks": unlocks,
                    "days": days,
                }
    except Exception as e:
        return {"status": "error", "unlocks": [], "error": str(e)}


def generate_morning_brief_ai_summary(brief: dict) -> dict:
    """用 LLM 生成早报今日定调 + 交易方向建议。

    输入：brief 完整数据
    输出：{headline, market_regime, bias, conviction, key_drivers, trade_suggestions, risk_warnings}
    """
    try:
        from crypto_research.config import get_settings
        from crypto_research.clients.llm_client import LLMClient, extract_json_from_llm_response

        settings = get_settings(require_database=False)
        llm = LLMClient(settings, rpm=30, timeout=120)

        if not llm.is_available():
            return {"status": "error", "error": "LLM 不可用（未配置 API Key）"}

        # 提取核心数据，压缩后传给 LLM
        m0 = brief.get("M0_tldr", {})
        m1 = brief.get("M1_cycle", {})
        m2 = brief.get("M2_flow", {})
        sector_flow = brief.get("M2_sector_flow") or {}
        etf_flow = brief.get("M2_etf_flow") or {}
        whale_moves = brief.get("M2_whale_moves") or {}
        exchange_flow = brief.get("M2_exchange_flow") or {}
        holder_conc = brief.get("M2_holder_concentration") or {}
        highlights = brief.get("M3_highlights", [])
        risks = brief.get("M4_risks", [])
        catalyst = brief.get("M6_catalyst", {})
        unlocks = brief.get("M6_upcoming_unlocks", {})

        # 赛道数据精简
        sectors_top = []
        for s in (sector_flow.get("sectors") or [])[:8]:
            sectors_top.append({
                "赛道": s.get("sector_label") or s.get("sector_key"),
                "7日涨幅": round(float(s.get("mcap_change_7d_pct") or 0), 2),
                "市值": round(float(s.get("market_cap") or 0) / 1e9, 2),
            })

        # ETF 数据精简
        etf_top = []
        for a in (etf_flow.get("assets") or [])[:8]:
            etf_top.append({
                "币种": a.get("symbol"),
                "当日净流入_M": round(float(a.get("latest_flow_usd") or 0) / 1e6, 2),
                "7日净流入_M": round(float(a.get("flow_7d_usd") or 0) / 1e6, 2),
            })

        # 巨鲸转账精简
        whale_top = []
        for t in (whale_moves.get("transfers") or [])[:5]:
            whale_top.append({
                "币种": t.get("symbol"),
                "金额_万USD": round(float(t.get("value_usd") or 0) / 10000, 0),
                "方向": "入交易所" if t.get("to_exchange") else ("出交易所" if t.get("from_exchange") else "巨鲸间转账"),
                "链": t.get("chain"),
            })

        # 交易所净流精简
        exch_top = []
        for a in (exchange_flow.get("assets") or [])[:8]:
            exch_top.append({
                "币种": a.get("symbol"),
                "7日净流_M": round(float(a.get("net_flow_usd") or 0) / 1e6, 2),
            })

        # 巨鲸增减持精简
        whale_buy_top = []
        for a in (holder_conc.get("whale_buying") or [])[:5]:
            whale_buy_top.append({
                "币种": a.get("symbol"),
                "7日增仓_pct": round(float(a.get("whale_balance_change_7d_pct") or 0), 2),
            })
        whale_sell_top = []
        for a in (holder_conc.get("whale_selling") or [])[:5]:
            whale_sell_top.append({
                "币种": a.get("symbol"),
                "7日减仓_pct": round(float(a.get("whale_balance_change_7d_pct") or 0), 2),
            })

        # 高亮/风险信号精简
        hl_symbols = list(set(
            (h.get("target") or "").upper()
            for h in (highlights or [])[:5]
            if h.get("target")
        ))
        risk_symbols = list(set(
            (r.get("target") or "").upper()
            for r in (risks or [])[:5]
            if r.get("target")
        ))

        # 即将解锁精简
        unlock_top = []
        for u in (unlocks.get("unlocks") or [])[:8]:
            unlock_top.append({
                "币种": u.get("symbol"),
                "日期": str(u.get("unlock_date")),
                "金额_M": round(float(u.get("unlock_value_usd") or 0) / 1e6, 2),
                "占流通_pct": round(float(u.get("unlock_ratio_circulating") or 0), 2),
                "风险等级": u.get("risk_level"),
            })

        system_prompt = """你是一位经验丰富的加密货币宏观分析师，每天早上根据多维度链上和市场数据生成简明的市场定调和交易建议。

你的输出必须是严格的 JSON 格式，包含以下字段：
{
  "headline": "一句话市场定调，25字以内，比如 'BTC 高位震荡，资金从 L2 转向 AI 赛道'",
  "market_regime": "市场状态：牛市/熊市/震荡/趋势不明 四选一",
  "bias": "整体方向偏多/偏空/中性 三选一",
  "conviction": "信心等级：high/medium/low",
  "key_drivers": ["3-5个核心驱动因素，每条一句话"],
  "sector_rotation": "赛道轮动结论，一句话，比如 '资金从 L2 流出，AI + DePIN 承接'",
  "trade_suggestions": [
    {
      "direction": "做多/做空/观望",
      "asset": "标的，如 BTC/ETH/SOL/AI板块",
      "horizon": "持有周期：短线(1-3天)/波段(1-2周)/中期(1月+)",
      "reason": "一句话理由",
      "confidence": "high/medium/low"
    }
  ],
  "risk_warnings": ["2-3个主要风险点，每条一句话"],
  "watchlist": ["3-5个值得重点关注的币种或赛道"]
}

要求：
1. 结论要明确，不要模棱两可，必须给出具体的方向判断
2. 交易建议要具体到标的和方向，不能只说"关注"
3. 基于数据说话，不要凭空编造信息
4. 中文输出，简洁专业
"""

        user_prompt = f"""以下是今日加密市场的多维度数据，请综合分析生成今日早报定调和交易建议。

【大盘概况】
- BTC 周期阶段：{m1.get('phase', '未知')}
- BTC 趋势：{m1.get('trend', '未知')}
- 恐贪指数：{m2.get('fear_greed', {}).get('value', '未知')}（{m2.get('fear_greed', {}).get('label', '')}）
- 24h 涨跌幅：{m2.get('btc_change', {}).get('change_24h', '未知')}%
- 总市值：{m2.get('total_market_cap', '未知')}
- 24h 成交量：{m2.get('total_volume_24h', '未知')}

【赛道表现（按7日涨幅排序）】
{chr(10).join(f'- {s["赛道"]}: {s["7日涨幅"]}%，市值 {s["市值"]}B' for s in sectors_top)}

【ETF 资金流】
{chr(10).join(f'- {e["币种"]}: 当日 {e["当日净流入_M"]}M，7日 {e["7日净流入_M"]}M' for e in etf_top)}

【交易所净流量（7日，正值=净流入）】
{chr(10).join(f'- {e["币种"]}: {e["7日净流_M"]}M' for e in exch_top)}

【大额链上转账（24h）】
- 总笔数：{whale_moves.get('total_count', 0)} 笔
- 总金额：约 {round(whale_moves.get('total_usd', 0) / 1e6, 1)}M USD
- 交易所净流入：{round(whale_moves.get('net_exchange_usd', 0) / 1e6, 1)}M USD
{chr(10).join(f'- {w["币种"]} {w["方向"]} {w["金额_万USD"]:.0f}万USD ({w["链"]})' for w in whale_top)}

【巨鲸持仓变化（7日）】
增仓 Top5：
{chr(10).join(f'- {w["币种"]}: +{w["7日增仓_pct"]}%' for w in whale_buy_top)}
减仓 Top5：
{chr(10).join(f'- {w["币种"]}: {w["7日减仓_pct"]}%' for w in whale_sell_top)}

【信号】
- 看多信号（{len(hl_symbols)}个）：{', '.join(hl_symbols) if hl_symbols else '无'}
- 看空风险（{len(risk_symbols)}个）：{', '.join(risk_symbols) if risk_symbols else '无'}

【即将解锁（未来14天，按金额排序）】
{chr(10).join(f'- {u["币种"]}: {u["日期"]} 解锁 {u["金额_M"]}M USD，占流通 {u["占流通_pct"]}%，风险{u["风险等级"]}' for u in unlock_top)}

请基于以上数据，生成今日的市场定调和交易建议。"""

        raw = llm.chat(
            system_prompt, user_prompt,
            temperature=0.3,
            max_tokens=4096,
            response_format={"type": "json_object"},
            use_cache=False,
        )

        data = extract_json_from_llm_response(raw)

        return {
            "status": "ok",
            "headline": str(data.get("headline", ""))[:80],
            "market_regime": str(data.get("market_regime", "震荡")),
            "bias": str(data.get("bias", "中性")),
            "conviction": str(data.get("conviction", "medium")).lower(),
            "key_drivers": [str(x)[:200] for x in (data.get("key_drivers") or [])][:5],
            "sector_rotation": str(data.get("sector_rotation", ""))[:200],
            "trade_suggestions": [
                {
                    "direction": str(s.get("direction", "")),
                    "asset": str(s.get("asset", "")),
                    "horizon": str(s.get("horizon", "")),
                    "reason": str(s.get("reason", ""))[:200],
                    "confidence": str(s.get("confidence", "medium")).lower(),
                }
                for s in (data.get("trade_suggestions") or [])[:5]
            ],
            "risk_warnings": [str(x)[:200] for x in (data.get("risk_warnings") or [])][:5],
            "watchlist": [str(x)[:30] for x in (data.get("watchlist") or [])][:8],
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _fetch_fallback_recommendations(limit: int = 8) -> list[dict]:
    """机会清单兜底：赛道领涨 × 链上信号 交叉筛选。

    当 score_opportunities() 因模块降级/数据缺失返回空时使用。
    数据源：sector_flow（赛道领涨币） + kol_onchain（链上信号）。

    分级逻辑：
    - HIGH：赛道领涨 + 链上信号共振，或强势赛道龙头（TOP3赛道TOP1）
    - MED：赛道领涨币（非龙头），或大额链上异动(>$5M)
    - LOW：其他
    """
    try:
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection
        import psycopg.rows

        sector_data = fetch_sector_flow_with_leaders()
        kol_data = fetch_kol_onchain_signals(hours=24, limit=20)

        sectors = sector_data.get("sectors", []) if sector_data.get("status") == "ok" else []
        signals = kol_data.get("signals", []) if kol_data.get("status") == "ok" else []

        if not sectors and not signals:
            return []

        # 1) 收集赛道领涨币（symbol → 完整信息）
        leader_map = {}
        for s in sectors:
            for rank, l in enumerate(s.get("leaders", [])):
                sym = l.get("symbol")
                if not sym:
                    continue
                entry = {
                    **l,
                    "sector": s.get("sector_label") or s.get("sector_key") or "",
                    "sector_key": s.get("sector_key") or "",
                    "sector_rank": rank,
                    "sector_7d": float(s.get("mcap_change_7d_pct") or 0),
                    "sector_mcap": float(s.get("market_cap") or 0),
                }
                # 同一个币出现在多个赛道，取排名更靠前的
                if sym not in leader_map or rank < leader_map[sym]["sector_rank"]:
                    leader_map[sym] = entry

        # 2) 收集链上信号（symbol → [signals]）
        sig_map = {}
        for sig in signals:
            sym = sig.get("symbol") or sig.get("event_token")
            if not sym:
                continue
            sig_map.setdefault(sym, []).append(sig)

        # 3) 分级打分
        result = []

        # 3a. 赛道领涨 + 链上信号共振 → HIGH（最有价值）
        for sym, lc in leader_map.items():
            if sym in sig_map:
                sigs = sig_map[sym]
                big_sigs = [s for s in sigs if s.get("event_usd_value") and float(s["event_usd_value"]) > 1000000]
                score = 78 + min(3, len(big_sigs)) * 4 - lc["sector_rank"] * 2
                # 方向：24h涨+看涨信号=long，否则混合判断
                p24h = float(lc.get("percent_change_24h") or 0)
                is_bullish = any(
                    "in" in str(s.get("event_direction", "")).lower()
                    or "accum" in str(s.get("signal_subtype", "")).lower()
                    for s in sigs
                )
                direction = "long" if (p24h >= 0 and is_bullish) else "short" if p24h < 0 else "neutral"
                subtypes = list(set(s.get("signal_subtype", "") for s in sigs if s.get("signal_subtype")))
                # 信号源列表（赛道领涨 + 各链上信号类型
                src_list = ["sector_leader"] + subtypes
                result.append({
                    "target": sym,
                    "symbol": sym,
                    "name": lc.get("name") or sym,
                    "conviction_score": score,
                    "conviction_tier": "HIGH",
                    "direction": direction,
                    "sector": lc["sector"],
                    "source_count": 1 + len(sigs),
                    "signal_sources": src_list,
                    "trigger_logic": f"{lc['sector']}赛道领涨 + {len(sigs)}个链上信号共振（{', '.join(subtypes[:3])}），7日 +{float(lc.get('percent_change_7d') or 0):.1f}% / 24日 {p24h:+.1f}%",
                    "change_24h": p24h,
                    "market_cap_usd": float(lc.get("market_cap") or 0),
                    "is_fallback": True,
                })

        # 3b. 强势赛道龙头（7d涨幅>10%的赛道TOP1） → HIGH
        for sym, lc in leader_map.items():
            if any(o["target"] == sym for o in result):
                continue
            if lc["sector_rank"] == 0 and lc["sector_7d"] > 10:
                p24h = float(lc.get("percent_change_24h") or 0)
                score = 72 + min(10, lc["sector_7d"] / 10)
                result.append({
                    "target": sym,
                    "symbol": sym,
                    "name": lc.get("name") or sym,
                    "conviction_score": score,
                    "conviction_tier": "HIGH",
                    "direction": "long" if p24h >= 0 else "short",
                    "sector": lc["sector"],
                    "source_count": 1,
                    "signal_sources": ["sector_leader"],
                    "trigger_logic": f"{lc['sector']}赛道龙头，赛道7日涨幅 +{lc['sector_7d']:.1f}%，币价7日 +{float(lc.get('percent_change_7d') or 0):.1f}%",
                    "change_24h": p24h,
                    "market_cap_usd": float(lc.get("market_cap") or 0),
                    "is_fallback": True,
                })

        # 3c. 其他赛道领涨币 → MED
        for sym, lc in leader_map.items():
            if any(o["target"] == sym for o in result):
                continue
            p24h = float(lc.get("percent_change_24h") or 0)
            p7d = float(lc.get("percent_change_7d") or 0)
            score = 55 + min(20, p7d / 3) - lc["sector_rank"] * 3
            result.append({
                "target": sym,
                "symbol": sym,
                "name": lc.get("name") or sym,
                "conviction_score": score,
                "conviction_tier": "MED",
                "direction": "long" if p24h >= 0 else "short",
                "sector": lc["sector"],
                "source_count": 1,
                "signal_sources": ["sector_leader"],
                "trigger_logic": f"{lc['sector']}赛道领涨榜第{lc['sector_rank']+1}，7日 +{p7d:.1f}% / 24h {p24h:+.1f}%",
                "change_24h": p24h,
                "market_cap_usd": float(lc.get("market_cap") or 0),
                "is_fallback": True,
            })

        # 3d. 大额链上异动(>$3M)但不在赛道领涨里 → MED
        for sym, sigs in sig_map.items():
            if any(o["target"] == sym for o in result):
                continue
            big_sigs = [s for s in sigs if s.get("event_usd_value") and float(s["event_usd_value"]) > 3000000]
            if not big_sigs:
                continue
            total_usd = sum(float(s["event_usd_value"]) for s in big_sigs)
            is_bullish = any(
                "in" in str(s.get("event_direction", "")).lower()
                or "accum" in str(s.get("signal_subtype", "")).lower()
                for s in big_sigs
            )
            score = 58 + min(12, total_usd / 1000000)
            subtypes = list(set(s.get("signal_subtype", "") for s in big_sigs if s.get("signal_subtype")))
            result.append({
                "target": sym,
                "symbol": sym,
                "name": sym,
                "conviction_score": score,
                "conviction_tier": "MED",
                "direction": "long" if is_bullish else "short",
                "sector": "链上异动",
                "source_count": len(big_sigs),
                "signal_sources": subtypes,
                "trigger_logic": f"{len(big_sigs)}笔大额链上异动（合计约 ${total_usd/1e6:.1f}M），类型：{', '.join(subtypes[:3])}",
                "change_24h": None,
                "market_cap_usd": 0,
                "is_fallback": True,
            })

        # 4) 补全合约地址 + 链信息（通过 symbol → core.asset → core.asset_contract）
        if result:
            try:
                syms = [o["target"].upper() for o in result]
                placeholders = ",".join(["%s"] * len(syms))
                settings = get_settings(require_database=True)  # noqa: F821
                with get_connection(settings.database_url) as conn2:  # noqa: F821
                    with conn2.cursor(row_factory=psycopg.rows.dict_row) as cur2:  # noqa: F821
                        # 取 is_primary=TRUE 的合约，没有的话取任意一条
                        cur2.execute(f"""
                            SELECT DISTINCT ON (UPPER(a.canonical_symbol))
                                   UPPER(a.canonical_symbol) AS symbol,
                                   ac.chain,
                                   ac.contract_address
                            FROM core.asset a
                            LEFT JOIN core.asset_contract ac
                              ON ac.asset_id = a.asset_id
                             AND ac.is_primary = TRUE
                            WHERE UPPER(a.canonical_symbol) IN ({placeholders})
                              AND a.canonical_name NOT LIKE '%Bridged%'
                              AND a.canonical_name NOT LIKE '%Wrapped%'
                              AND a.canonical_name NOT LIKE '%Peg %'
                            ORDER BY UPPER(a.canonical_symbol),
                                     CASE WHEN ac.contract_address IS NOT NULL THEN 0 ELSE 1 END,
                                     ac.is_primary DESC, a.asset_id
                        """, tuple(syms))
                        contract_map = {
                            r["symbol"]: {
                                "chain": r.get("chain"),
                                "contract_address": r.get("contract_address"),
                            }
                            for r in cur2.fetchall()
                            if r.get("symbol")
                        }
                        # 对没有 is_primary 的，再找任意一条合约地址兜底
                        missing = [s for s in syms if s not in contract_map or not contract_map[s].get("contract_address")]
                        if missing:
                            placeholders2 = ",".join(["%s"] * len(missing))
                            cur2.execute(f"""
                                SELECT DISTINCT ON (UPPER(a.canonical_symbol))
                                       UPPER(a.canonical_symbol) AS symbol,
                                       ac.chain,
                                       ac.contract_address
                                FROM core.asset a
                                JOIN core.asset_contract ac ON ac.asset_id = a.asset_id
                                WHERE UPPER(a.canonical_symbol) IN ({placeholders2})
                                  AND a.canonical_name NOT LIKE '%Bridged%'
                                  AND a.canonical_name NOT LIKE '%Wrapped%'
                                ORDER BY UPPER(a.canonical_symbol), ac.is_primary DESC
                            """, tuple(missing))
                            for r in cur2.fetchall():
                                sym = r.get("symbol")
                                if sym and r.get("contract_address"):
                                    contract_map[sym] = {
                                        "chain": r.get("chain"),
                                        "contract_address": r.get("contract_address"),
                                    }
                # 回填到 result
                for o in result:
                    key = o["target"].upper()
                    if key in contract_map:
                        o["chain"] = contract_map[key].get("chain")
                        o["contract_address"] = contract_map[key].get("contract_address")
            except Exception:
                # 合约地址补全失败不影响主流程
                pass

        # 排序 + 截断
        result.sort(key=lambda o: o["conviction_score"], reverse=True)
        return result[:limit]
    except Exception:
        return []


def _build_daily_diff_brief(today: dict, highlights: list,
                            risk_signals: list) -> dict:
    """M5 每日变化榜早报精简版（BRIEF-OPT-002）。

    从 overview 的 daily_diff_summary 里取各分类 Top5，
    并给每条打上 ⭐ 高亮信号 / ⚠️ 高危信号 标记。
    如果 overview 里没有变化榜数据（降级场景），从 DB 直接取。
    """
    diff_data = today.get("daily_diff_summary") or {}
    if not diff_data:
        try:
            from crypto_research.db.conn import get_connection
            from crypto_research.config import get_settings

            settings = get_settings(require_database=True)
            with get_connection(settings.database_url) as conn:
                from crypto_research.db import db_stats
                diff_data = db_stats.get_daily_diff_summary(conn)
        except Exception:
            diff_data = {}

    highlight_syms: set[str] = set()
    risk_syms: set[str] = set()
    for o in highlights:
        for s in (o.get("involved_symbols") or []):
            if s:
                highlight_syms.add(str(s).upper())
        tgt = (o.get("target") or "").strip().upper()
        if tgt and len(tgt) <= 10 and tgt.isalpha():
            highlight_syms.add(tgt)
    for o in risk_signals:
        for s in (o.get("involved_symbols") or []):
            if s:
                risk_syms.add(str(s).upper())
        tgt = (o.get("target") or "").strip().upper()
        if tgt and len(tgt) <= 10 and tgt.isalpha():
            risk_syms.add(tgt)

    CATEGORY_LIMIT = 5
    CATEGORY_LABELS = {
        "price_change_24h": "价格涨幅榜",
        "price_change_24h_down": "价格跌幅榜",
        "volume_surge_24h": "成交量异动",
        "price_volume_surge": "量价齐升",
        "sector_rotation": "赛道轮动",
        "unlock_7d": "即将解锁",
        "market_cap_mover": "市值变化榜",
    }

    result: dict[str, list[dict]] = {}
    for cat, items in (diff_data or {}).items():
        if not items or not isinstance(items, list):
            continue
        top = items[:CATEGORY_LIMIT]
        tagged = []
        for it in top:
            sym = str(it.get("symbol") or "").upper()
            entry = dict(it)
            entry["is_highlight"] = sym in highlight_syms
            entry["is_risk"] = sym in risk_syms
            tagged.append(entry)
        label = CATEGORY_LABELS.get(cat, cat)
        result[label] = tagged

    return result


def generate_morning_brief(today: dict, yesterday: dict | None, use_ai: bool = True) -> dict:
    """
    早报结构化骨架 V2（重新设计版）。
    6 大模块 + AI 定调 + 链上深度数据。
    """
    from concurrent.futures import ThreadPoolExecutor

    cycle = today.get("btc_cycle") or {}
    diff = diff_overview(yesterday, today) if yesterday else None
    opps = (today.get("opportunity_list") or {}).get("opportunities") or []
    divs = (today.get("divergence_signals") or {}).get("signals") or []

    # ── 并行拉取所有独立数据 ──
    def _fetch_parallel():
        with ThreadPoolExecutor(max_workers=12) as pool:
            futures = {
                "stablecoin": pool.submit(fetch_stablecoin_supply_trend),
                "sector_flow": pool.submit(fetch_sector_flow_with_leaders),
                "kol_onchain": pool.submit(fetch_kol_onchain_signals),
                "etf_flow": pool.submit(fetch_etf_flow_trend, 7),
                "whale_moves": pool.submit(fetch_onchain_whale_moves, 24, 10),
                "holder_concentration": pool.submit(fetch_holder_concentration_summary, 10),
                "exchange_flow": pool.submit(fetch_exchange_net_flow_summary, 7),
                "upcoming_unlocks": pool.submit(fetch_upcoming_unlocks, 14),
            }
            results = {}
            for k, fut in futures.items():
                try:
                    results[k] = fut.result(timeout=20)
                except Exception as e:
                    print(f"[morning_brief] {k} fetch failed: {e}")
                    results[k] = {"status": "error", "error": str(e)}
            return results

    p = _fetch_parallel()
    stab = p["stablecoin"]
    sector_flow = p["sector_flow"]
    kol_onchain = p["kol_onchain"]
    etf_flow = p["etf_flow"]
    whale_moves = p["whale_moves"]
    holder_concentration = p["holder_concentration"]
    exchange_flow = p["exchange_flow"]
    upcoming_unlocks = p["upcoming_unlocks"]

    # 从 overview 取叙事榜 + 链净流入（首页同一套数据源）
    d5data = (today.get("dimensions") or {}).get("5板块") or {}
    narrative_flow = d5data.get("narrative_flow_ranking") or {}
    chain_flow = d5data.get("chain_flow_ranking") or {}

    # 兜底：如果 overview 里没有（降级场景），直接调 API 补
    if not narrative_flow.get("ranked"):
        try:
            cat_flow = fetch_category_flow()
            tvl_flow = fetch_category_tvl_flow()
            narrative_flow = build_narrative_flow_ranking(cat_flow, tvl_flow)
        except Exception:
            narrative_flow = {"status": "error", "ranked": []}
    # 二级兜底：API 也失败的话，从 biz.sector_flow_daily 读 12 赛道数据
    if not narrative_flow.get("ranked"):
        db_narrative = _fetch_narrative_flow_from_db()
        if db_narrative:
            narrative_flow = db_narrative
    if not chain_flow.get("ranked"):
        try:
            chain_flow = fetch_chain_flow()
        except Exception:
            chain_flow = {"status": "error", "ranked": []}

    # 兜底：如果评分系统返回空机会，从 daily_recommendation 表取热门币种
    if not opps:
        fallback = _fetch_fallback_recommendations()
        if fallback:
            opps = fallback

    # ── 高亮信号 + 高危信号（直接复用大盘分析同一套） ──
    opp_list = today.get("opportunity_list") or {}
    highlights = opp_list.get("highlight_signals") or []
    risk_signals = opp_list.get("risk_signals") or []
    # 兜底：如果 overview 里没有精选信号（降级场景），手动生成 + V2 AI 增强
    if not highlights and opps:
        highlights = select_highlight_signals(opps, max_total=6)
        # 与 score_opportunities 对齐：补上 asset_id + V2 AI 增强
        if highlights:
            _enrich_highlights_with_ai_v2(highlights, opps, direction="long", max_ai=6)
    if not risk_signals and opps:
        risk_signals = select_risk_signals(opps, max_total=6)
        if risk_signals:
            _enrich_highlights_with_ai_v2(risk_signals, opps, direction="short", max_ai=6)

    # ── 每日变化榜精简版 ──
    daily_diff_brief = _build_daily_diff_brief(today, highlights, risk_signals)

    # 组装基础 brief
    brief = {
        "M0_tldr": _build_tldr(today, opps, highlights, risk_signals),
        "M1_cycle": cycle,
        "M2_flow": _build_flow(today, diff, stab),
        "M2_institutional": today.get("institutional_mvrv") or {},
        "M2_sector_flow": sector_flow,
        "M2_etf_flow": etf_flow,
        "M2_whale_moves": whale_moves,
        "M2_exchange_flow": exchange_flow,
        "M2_holder_concentration": holder_concentration,
        "M2_stablecoin": stab,
        "M3_highlights": highlights,
        "M4_risks": risk_signals,
        "M5_daily_diff": daily_diff_brief,
        "M6_catalyst": today.get("event_calendar") or {},
        "M6_upcoming_unlocks": upcoming_unlocks,
        "M7_divergence": [
            d for d in divs
            if d.get("label") in ("DANGEROUS", "DIVERGENT")
        ],
        "M8_opportunities": [
            o for o in opps if o.get("conviction_tier") == "HIGH"
        ],
        "M8_watchlist": [
            o for o in opps if o.get("conviction_tier") != "HIGH"
        ],
        "M8_resonance": today.get("resonance") or {},
        "M8_meme": today.get("meme_risk") or {},
        "M8_chimney": today.get("chimney_signals") or {},
        "M8_smart_money": today.get("smart_money_divergence") or {},
        "M9_degraded": _collect_degraded(today),
        "narrative_flow": narrative_flow,
        "chain_flow": chain_flow,
        "kol_onchain": kol_onchain,
        "DIFF": diff,
    }

    # ── AI 今日定调 + 交易建议（可选） ──
    if use_ai:
        try:
            ai_summary = generate_morning_brief_ai_summary(brief)
            brief["M0_ai_summary"] = ai_summary
        except Exception as e:
            print(f"[morning_brief] AI summary failed: {e}")
            brief["M0_ai_summary"] = {"status": "error", "error": str(e)}

    return brief


def fetch_event_calendar() -> dict:
    """事件日历：宏观硬日程 + 代币级催化剂事件。仅展示，不参与子分。

    P1-followup 修复：原实现误用 biz.asset_catalyst（新闻文章表），
    其 published_at 是文章发布时间而非未来事件日期，导致 token_events 恒为 0。
    改为读 biz.asset_unlock_event（真实未来解锁日程），配合 biz.asset_catalyst
    的 published_at>=NOW() 兜底（极少数新闻有明确未来事件日期）。
    宏观日程为公开固定节奏，由 dev 按当年官方日程维护（每季度更新）。
    """
    # ① 宏观硬日程（手动维护近 3 个月，来源 FRED/FOMC 官网公开日程；零依赖）
    hardcoded_events = [
        {"date": "2026-09-16", "event": "FOMC 议息会议", "type": "macro", "source": "hardcoded"},
        {"date": "2026-09-22", "event": "CPI 公布", "type": "macro", "source": "hardcoded"},
        {"date": "2026-10-02", "event": "NFP 非农", "type": "macro", "source": "hardcoded"},
        {"date": "2026-10-28", "event": "FOMC 议息会议", "type": "macro", "source": "hardcoded"},
        {"date": "2026-11-13", "event": "CPI 公布", "type": "macro", "source": "hardcoded"},
        {"date": "2026-12-09", "event": "FOMC 议息会议", "type": "macro", "source": "hardcoded"},
    ]
    # ② 已知重大解锁峰（手动维护；TokenUnlocks 网页可查，本期不抓）
    unlock_events: list[dict] = [
        # {"date": "2026-10-XX", "event": "XXX 解锁峰", "type": "unlock", "source": "hardcoded"},
    ]

    # ③ P1-followup：代币级未来事件日程（主源：biz.asset_unlock_event；兜底：biz.asset_catalyst）
    token_events: list[dict] = []
    try:
        import logging
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection

        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                # 主源：真实未来解锁日程（unlock_date > NOW）
                cur.execute("""
                    SELECT u.asset_id, a.canonical_symbol AS symbol,
                           u.unlock_date, u.unlock_value_usd,
                           u.unlock_amount, u.unlock_ratio_mcap,
                           u.unlock_type,
                           'unlock' AS event_type
                    FROM biz.asset_unlock_event u
                    JOIN core.asset a ON a.asset_id = u.asset_id
                    WHERE u.unlock_date > CURRENT_TIMESTAMP
                      AND u.unlock_date < CURRENT_TIMESTAMP + INTERVAL '33 days'
                    ORDER BY u.unlock_date ASC
                    LIMIT 50
                """)
                for r in cur.fetchall():
                    event_date = r["unlock_date"]
                    ratio_mcap = r.get("unlock_ratio_mcap")
                    token_events.append({
                        "date": str(event_date.date()) if event_date else None,
                        "event": f"{r['symbol']} 解锁"
                                 + (f" {ratio_mcap:.2f}%" if ratio_mcap else ""),
                        "type": r.get("event_type") or "unlock",
                        "sub_type": r.get("unlock_type"),
                        "source": "asset_unlock_event",
                        "asset_id": r.get("asset_id"),
                        "symbol": r.get("symbol"),
                        "unlock_value_usd": float(r["unlock_value_usd"]) if r.get("unlock_value_usd") else None,
                        "unlock_pct": float(ratio_mcap) if ratio_mcap else None,
                    })

                # 兜底：biz.asset_catalyst 中 published_at 在未来的极少数条目
                if not token_events:
                    cur.execute("""
                        SELECT ac.catalyst_id, ac.asset_id, a.canonical_symbol AS symbol,
                               ac.title, ac.event_category, ac.published_at,
                               ac.source_code
                        FROM biz.asset_catalyst ac
                        LEFT JOIN core.asset a ON a.asset_id = ac.asset_id
                        WHERE ac.published_at > CURRENT_TIMESTAMP
                          AND ac.published_at < CURRENT_TIMESTAMP + INTERVAL '33 days'
                        ORDER BY ac.published_at ASC
                        LIMIT 20
                    """)
                    for r in cur.fetchall():
                        pub_at = r["published_at"]
                        token_events.append({
                            "date": str(pub_at.date()) if pub_at else None,
                            "event": r["title"] or f"{r.get('event_category')} 事件",
                            "type": r.get("event_category") or "catalyst",
                            "source": r.get("source_code") or "asset_catalyst",
                            "asset_id": r.get("asset_id"),
                            "symbol": r.get("symbol"),
                             "catalyst_id": r.get("catalyst_id"),
                        })
    except Exception as e:
        logging.getLogger(__name__).warning("fetch_event_calendar token_events query failed: %s", e)

    events = hardcoded_events + unlock_events + token_events
    return {
        "status": "ok" if events else "partial",
        "hardcoded": hardcoded_events,
        "unlock": unlock_events,
        "token_events": token_events,
        "gecko": [],  # CoinGecko /events 已废弃，显式空
    }


# ══════════════════════════════════════════════════════════════
# 子分计算函数
# ══════════════════════════════════════════════════════════════

def compute_emotion_subscore(
    fear_greed: dict,
    altcoin_season: dict,
    cefi: dict,
    derivatives: dict,
    *,
    fear_greed_percentile: float | None = None,
    fear_greed_extreme: str = "NONE",
    cefi_percentile: float | None = None,
    cefi_extreme: str = "NONE",
    mvrv_percentile: float | None = None,
    mvrv_extreme: str = "NONE",
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
        components["fear_greed"] = {
            "value": fg_value, "score": fg_score, "weight": EMOTION_WEIGHTS["fear_greed"],
            "percentile": fear_greed_percentile, "extreme": fear_greed_extreme,
        }
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
        components["cefi"] = {
            "value": cefi_value, "score": cefi_score, "weight": EMOTION_WEIGHTS["cefi"],
            "percentile": cefi_percentile, "extreme": cefi_extreme,
        }
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
        _ft = SCORING_TUNING
        if funding_abs > _ft["funding_extreme_abs"]:  # 极端
            deriv_score = _ft["funding_extreme_bull"] if funding > 0 else _ft["funding_extreme_bear"]
        elif funding_abs > _ft["funding_high_abs"]:
            deriv_score = _ft["funding_high_bull"] if funding > 0 else _ft["funding_high_bear"]
        else:
            deriv_score = _ft["funding_neutral"]

        components["derivative"] = {
            "funding_rate": funding,
            "open_interest": oi,
            "score": deriv_score,
            "weight": EMOTION_WEIGHTS["derivative"],
            "mvrv_percentile": mvrv_percentile,
            "mvrv_extreme": mvrv_extreme,
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

    overall_status = "ok" if available_weights >= SCORING_TUNING["min_available_weight"] else ("warning" if available_weights > 0 else "error")

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
    *,
    btc_dominance_percentile: float | None = None,
    btc_dominance_extreme: str = "NONE",
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
        if total_mcap > 0:
            mcap_score = min(100, max(0, total_mcap / SCORING_TUNING["mcap_step"]))
        else:
            mcap_score = 50
        components["market_cap"] = {
            "total_market_cap": total_mcap,
            "btc_dominance": global_metrics.get("btc_dominance"),
            "score": mcap_score,
            "weight": STRUCTURE_WEIGHTS["market_cap"],
            "percentile": btc_dominance_percentile,
            "extreme": btc_dominance_extreme,
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
        rsi_score = SCORING_TUNING["rsi_base"] + (rsi - SCORING_TUNING["rsi_base"]) * SCORING_TUNING["rsi_slope"]

        # MA 位置加分
        if ma20 and price > ma20:
            rsi_score += SCORING_TUNING["ma20_bonus"]
        if ma50 and price > ma50:
            rsi_score += SCORING_TUNING["ma50_bonus"]

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
        if net_flow > SCORING_TUNING["etf_flow_high"]:
            inst_score = SCORING_TUNING["etf_score_inflow_high"]
        elif net_flow > 0:
            inst_score = SCORING_TUNING["etf_score_inflow"]
        elif net_flow > SCORING_TUNING["etf_flow_low"]:
            inst_score = SCORING_TUNING["etf_score_outflow"]
        else:
            inst_score = SCORING_TUNING["etf_score_outflow_high"]

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
            sector_score = min(100, max(0, SCORING_TUNING["sector_base"] + avg_change * SCORING_TUNING["sector_slope"]))
        else:
            sector_score = SCORING_TUNING["sector_base"]

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

    overall_status = "ok" if available_weights >= SCORING_TUNING["min_available_weight"] else ("warning" if available_weights > 0 else "error")

    return {
        "score": final_score,
        "components": components,
        "available_weight": round(available_weights, 2),
        "status": overall_status,
    }


def _build_mvrv_universe(top_n: int = 100) -> dict:
    """构建多币 MVRV 极值汇总：max/min/median + 各币分位。

    P0-2 修复：从仅 15 币扩展到 top 100（按 CM 市值）。
    优先使用 cm_onchain_percentile_full 的全历史分位；无分位的币用 90 天自身历史分位近似；
    历史不足时回退 MVRV 绝对值，不惩罚、不拉入极端标记。
    """
    try:
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection

        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
                # 1) 取最新日 top N 有 MVRV 的币（CM 市值排序）
                cur.execute("""
                    WITH latest_date AS (
                        SELECT MAX(metric_date) AS d FROM biz.cm_asset_onchain_daily
                    ),
                    top_coins AS (
                        SELECT asset_id, cm_symbol, cap_mvrv_cur, cap_mrkt_cur_usd
                        FROM biz.cm_asset_onchain_daily
                        WHERE metric_date = (SELECT d FROM latest_date)
                          AND cap_mvrv_cur IS NOT NULL
                          AND cap_mrkt_cur_usd IS NOT NULL
                        ORDER BY cap_mrkt_cur_usd DESC NULLS LAST
                        LIMIT %s
                    )
                    SELECT tc.asset_id, tc.cm_symbol, tc.cap_mvrv_cur,
                           p.mvrv_pct_full,
                           CASE
                               WHEN p.mvrv_pct_full > 90 THEN 'HIGH'
                               WHEN p.mvrv_pct_full < 10 THEN 'LOW'
                               ELSE 'NONE'
                           END AS extreme
                    FROM top_coins tc
                    LEFT JOIN biz.cm_onchain_percentile_full p
                        ON p.asset_id = tc.asset_id
                       AND p.metric_date = (SELECT d FROM latest_date)
                    ORDER BY tc.cap_mrkt_cur_usd DESC NULLS LAST
                """, (top_n,))
                latest_rows = cur.fetchall()

                if not latest_rows:
                    return {"status": "error", "coins": []}

                # 2) 对无全历史分位的币，计算 90 天自身历史分位
                asset_ids = [r[0] for r in latest_rows if r[3] is None]
                hist_pct_map: dict[int, float] = {}
                if asset_ids:
                    placeholders = ",".join(["%s"] * len(asset_ids))
                    cur.execute(f"""
                        WITH latest_date AS (
                            SELECT MAX(metric_date) AS d FROM biz.cm_asset_onchain_daily
                        ),
                        hist AS (
                            SELECT asset_id, cap_mvrv_cur,
                                   PERCENT_RANK() OVER (
                                       PARTITION BY asset_id ORDER BY cap_mvrv_cur
                                   ) AS pct_rank,
                                   ROW_NUMBER() OVER (
                                       PARTITION BY asset_id ORDER BY metric_date DESC
                                   ) AS rn
                            FROM biz.cm_asset_onchain_daily
                            WHERE asset_id IN ({placeholders})
                              AND metric_date >= (SELECT d FROM latest_date) - INTERVAL '90 days'
                              AND cap_mvrv_cur IS NOT NULL
                        )
                        SELECT asset_id, pct_rank * 100 AS hist_pct
                        FROM hist
                        WHERE rn = 1
                    """, tuple(asset_ids))
                    for r in cur.fetchall():
                        hist_pct_map[r[0]] = float(r[1]) if r[1] is not None else None

        coins = []
        pcts = []
        for r in latest_rows:
            asset_id, symbol, mvrv_value, pct_full, extreme = r
            # 优先用全历史分位，没有则用 90 天自身历史分位
            pct = float(pct_full) if pct_full is not None else hist_pct_map.get(asset_id)
            if pct is not None:
                pcts.append(pct)
                # 若用 90 天近似分位，也计算 extreme 标记
                if extreme is None:
                    if pct > 90:
                        extreme = "HIGH"
                    elif pct < 10:
                        extreme = "LOW"
                    else:
                        extreme = "NONE"
            coins.append({
                "symbol": symbol,
                "value": float(mvrv_value) if mvrv_value is not None else None,
                "pct_full": round(pct, 2) if pct is not None else None,
                "extreme": extreme or "NONE",
                "has_full_history": pct_full is not None,
            })

        return {
            "status": "ok",
            "count": len(coins),
            "max_pct": max(pcts) if pcts else None,
            "min_pct": min(pcts) if pcts else None,
            "median_pct": sorted(pcts)[len(pcts) // 2] if pcts else None,
            "coins": coins,
        }
    except Exception as e:
        return {"status": "error", "error": str(e), "coins": []}


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

    # ── 并行获取所有数据（第一批：实时/快照数据）──
    def _fetch_all():
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {
                "global_metrics": pool.submit(fetch_cmc_global_metrics),
                "fear_greed": pool.submit(fetch_cmc_fear_greed),
                "altcoin_season": pool.submit(fetch_cmc_altcoin_season),
                "cefi": pool.submit(fetch_cryptoetf_cefi),
                "btc_klines": pool.submit(fetch_binance_btc_klines),
                "eth_klines": pool.submit(fetch_binance_eth_klines),
                "derivatives": pool.submit(fetch_binance_derivatives),
                "etf_flows": pool.submit(fetch_binance_etf_flows),
                "categories": pool.submit(fetch_cmc_categories),
                "event_calendar": pool.submit(fetch_event_calendar),
                "onchain": pool.submit(fetch_onchain_anomaly_signals),
                "btc_onchain": pool.submit(fetch_btc_onchain_signals),
                "cm_activity": pool.submit(fetch_cm_activity_signals),
            }
            results = {}
            for k, fut in futures.items():
                try:
                    results[k] = fut.result(timeout=30)
                except Exception as e:
                    print(f"[overview] {k} fetch failed: {e}")
                    results[k] = {"status": "error", "error": str(e)}
        return results

    r = _fetch_all()
    global_metrics = r["global_metrics"]
    fear_greed = r["fear_greed"]
    altcoin_season = r["altcoin_season"]
    cefi = r["cefi"]
    btc_klines = r["btc_klines"]
    eth_klines = r["eth_klines"]
    derivatives = r["derivatives"]
    etf_flows = r["etf_flows"]
    categories = r["categories"]
    event_calendar = r["event_calendar"]
    onchain = r["onchain"]
    btc_onchain = r["btc_onchain"]
    cm_activity = r["cm_activity"]

    # 若链上 CEX 净流量可用，用其归一化分值覆盖 cryptoetf cefi（优先级更高）
    if onchain.get("status") == "ok" and onchain.get("cefi_score") is not None:
        cefi = {
            "value": onchain["cefi_score"],
            "status": "ok",
            "source": "onchain_cex_netflow",
        }

    # ── P2-1 历史分位：并行拉取历史序列 ──
    with ThreadPoolExecutor(max_workers=5) as pool:
        fg_fut = pool.submit(fetch_fear_greed_history, 90)
        mvrv_fut = pool.submit(fetch_mvrv_history, "btc")
        sc_fut = pool.submit(fetch_stablecoin_netflow_history, 30)
        cefi_hist_fut = pool.submit(fetch_cefi_history, 30)
        btc_dom_fut = pool.submit(fetch_btc_dominance_history, 30)
        try:
            fear_greed_hist = fg_fut.result(timeout=20)
        except Exception as e:
            print(f"[overview] fear_greed_hist failed: {e}")
            fear_greed_hist = {"status": "error"}
        try:
            mvrv_hist = mvrv_fut.result(timeout=20)
        except Exception as e:
            print(f"[overview] mvrv_hist failed: {e}")
            mvrv_hist = {"status": "error"}
        try:
            stablecoin_flow_hist = sc_fut.result(timeout=20)
        except Exception as e:
            print(f"[overview] stablecoin_flow_hist failed: {e}")
            stablecoin_flow_hist = {"status": "error"}
        try:
            cefi_hist = cefi_hist_fut.result(timeout=20)
        except Exception as e:
            print(f"[overview] cefi_hist failed: {e}")
            cefi_hist = {"status": "error"}
        try:
            btc_dom_hist = btc_dom_fut.result(timeout=20)
        except Exception as e:
            print(f"[overview] btc_dom_hist failed: {e}")
            btc_dom_hist = {"status": "error"}

    # ── P2-1: 计算各核心指标的百分位和极端标记 ──
    fg_value = fear_greed.get("value")
    fg_percentile = percentile_of(fg_value, fear_greed_hist.get("series") or []) if fear_greed_hist.get("status") == "ok" else None
    fg_extreme = flag_extreme(fg_percentile)

    # MVRV：优先用库内全历史分位（cm_onchain_percentile_full），兜底用 percentile_of
    mvrv_value = mvrv_hist.get("value") or derivatives.get("mvrv_z_score")
    mvrv_percentile = mvrv_hist.get("pct_full") if mvrv_hist.get("status") == "ok" else None
    mvrv_extreme = mvrv_hist.get("extreme", "NONE") if mvrv_hist.get("status") == "ok" else flag_extreme(mvrv_percentile)

    # 稳定币分位：优先用 7d 滚动累计口径（与前端 7d 视角对齐），回退单日
    sc_rolling_7d = stablecoin_flow_hist.get("rolling_7d") or []
    sc_flow_value = sc_rolling_7d[-1] if sc_rolling_7d else (
        stablecoin_flow_hist.get("series", [])[-1] if stablecoin_flow_hist.get("series") else None
    )
    sc_flow_ref = sc_rolling_7d if len(sc_rolling_7d) >= 2 else (stablecoin_flow_hist.get("series") or [])
    sc_flow_percentile = percentile_of(sc_flow_value, sc_flow_ref) if stablecoin_flow_hist.get("status") == "ok" else None
    sc_flow_extreme = flag_extreme(sc_flow_percentile)

    cefi_value = cefi.get("value")
    # onchain 覆盖时用自身 30d 序列算 percentile（避免跨源量纲混合）
    if cefi.get("source") == "onchain_cex_netflow" and onchain.get("daily_netflows_30d"):
        cefi_percentile = percentile_of(cefi_value, onchain["daily_netflows_30d"])
    else:
        cefi_percentile = percentile_of(cefi_value, cefi_hist.get("series") or []) if cefi_hist.get("status") == "ok" else None
    cefi_extreme = flag_extreme(cefi_percentile)

    btc_dom_value = global_metrics.get("btc_dominance")
    btc_dom_percentile = percentile_of(btc_dom_value, btc_dom_hist.get("series") or []) if btc_dom_hist.get("status") == "ok" else None
    btc_dom_extreme = flag_extreme(btc_dom_percentile)

    # ── P1-1 板块/链资金净流入 + P1-2 背离 + BTC周期（并行） ──
    def _fetch_sector_and_more():
        with ThreadPoolExecutor(max_workers=5) as pool:
            fut_cat = pool.submit(fetch_category_flow)
            fut_tvl = pool.submit(fetch_category_tvl_flow)
            fut_chain = pool.submit(fetch_chain_flow)
            fut_div = pool.submit(build_divergence_signals)

            def _btc_cycle():
                try:
                    from db_stats import get_btc_cycle_position
                    return get_btc_cycle_position()
                except Exception:
                    return {"status": "error", "phase": "unknown",
                            "phase_label": "数据不可用", "signals": []}

            fut_cycle = pool.submit(_btc_cycle)

            cat_flow = fut_cat.result(timeout=20)
            tvl_flow = fut_tvl.result(timeout=20)
            chain_flow = fut_chain.result(timeout=20)
            divergence = fut_div.result(timeout=20)
            btc_cycle = fut_cycle.result(timeout=20)
        return cat_flow, tvl_flow, chain_flow, divergence, btc_cycle

    cat_flow, tvl_flow, chain_flow, divergence, btc_cycle = _fetch_sector_and_more()
    narrative_flow = build_narrative_flow_ranking(cat_flow, tvl_flow)

    # ── 计算子分 ──
    emotion_subscore = compute_emotion_subscore(
        fear_greed=fear_greed,
        altcoin_season=altcoin_season,
        cefi=cefi,
        derivatives=derivatives,
        fear_greed_percentile=fg_percentile,
        fear_greed_extreme=fg_extreme,
        cefi_percentile=cefi_percentile,
        cefi_extreme=cefi_extreme,
        mvrv_percentile=mvrv_percentile,
        mvrv_extreme=mvrv_extreme,
    )

    structure_subscore = compute_structure_subscore(
        global_metrics=global_metrics,
        btc_klines=btc_klines,
        eth_klines=eth_klines,
        etf_flows=etf_flows,
        categories=categories,
        btc_dominance_percentile=btc_dom_percentile,
        btc_dominance_extreme=btc_dom_extreme,
    )

    # ── B3: 多币 MVRV 极值（提前计算，避免重复调用） ──
    mvrv_data = _build_mvrv_universe()

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
                "status": "ok" if all(x.get("status") == "ok" for x in [fear_greed, altcoin_season, cefi]) else "partial",
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
                "status": (
                    "ok"
                    if narrative_flow.get("ranked") or chain_flow.get("ranked")
                    else ("partial" if cat_flow.get("status") == "ok" or chain_flow.get("status") == "ok" else "error")
                ),
                "data": {
                    "narrative_flow_ranking": narrative_flow,
                    "chain_flow_ranking": chain_flow,
                    "narrative_tvl_flow": tvl_flow,
                    "category_flow": cat_flow,
                    "stablecoin_flow_percentile": sc_flow_percentile,
                    "stablecoin_flow_extreme": sc_flow_extreme,
                    "stablecoin_flow_history": stablecoin_flow_hist.get("series", []),
                    "stablecoin_flow_rolling_7d": stablecoin_flow_hist.get("rolling_7d", []),
                    "stablecoin_flow_dates": stablecoin_flow_hist.get("dates", []),
                    "stablecoin_supply_history": stablecoin_flow_hist.get("total_supply", []),
                    "stablecoin_supply_dates": stablecoin_flow_hist.get("supply_dates", []),
                    "stablecoin_anomaly": stablecoin_flow_hist.get("anomaly"),
                },
            },
            # B3: 多币 MVRV 极值汇总（统一包裹 data+status）
            "mvrv_universe": {
                "data": mvrv_data,
                "status": mvrv_data.get("status", "error"),
            },
            # 6链上: BTC 链上积累/分配信号（CM Community 免费档）
            "6链上": {
                "data": btc_onchain,
                "status": btc_onchain.get("status", "error"),
            },
            # 6a网络健康: CM 多资产活跃/采用信号（第三刀）
            "6a网络健康": {
                "data": cm_activity,
                "status": cm_activity.get("status", "error"),
            },
        },
        "event_calendar": event_calendar,
        "divergence_signals": divergence,
        "onchain_anomaly_signals": onchain,
        "btc_cycle": btc_cycle,
        "fetched_at": int(now),
    }

    # ── P1-4 机会清单（消费 P1-1~P1-3 + P0-3 真实字段） ──
    result["opportunity_list"] = score_opportunities(result)

    # ── P0-3 共振榜（共识动量 ∩ 宏观 conviction） ──
    result["resonance"] = build_resonance_signals(result)

    # ── 批④ 数据驱动层 ──
    result["meme_risk"] = fetch_meme_risk_summary()
    result["chimney_signals"] = build_chimney_signals(result)
    result["institutional_mvrv"] = build_institutional_mvrv_summary(result)
    result["smart_money_divergence"] = build_smart_money_divergence(result)

    _cache = result
    _cache_ts = now

    return result


if __name__ == "__main__":
    import json
    result = get_market_overview()
    print(json.dumps(result, indent=2, ensure_ascii=False))
