"""
大盘宏观分析模块。
聚合六维数据，输出 emotion_subscore + structure_subscore 两项子分。
事件日历仅作为独立信息栏展示，不参与任何子分计算。
"""

from __future__ import annotations

import os
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
    """稳定币净流入历史序列（日频）。返回 {status, series: [netflow_usd, ...], rolling_7d: [...]}。

    rolling_7d：7d 滚动累计净流入，与前端 7d 视角对齐（分位计算用此口径）。
    """
    try:
        r = requests.get("https://stablecoins.llama.fi/stablecoincharts/All", timeout=TIMEOUT)
        r.raise_for_status()
        rows = r.json()
        series = []
        for row in rows:
            usd = (row.get("totalCirculating") or {}).get("peggedUSD")
            if usd is not None:
                series.append(_safe_float(usd))
        if len(series) < 2:
            return {"status": "error", "error": "insufficient", "series": [], "rolling_7d": []}
        # 计算日净流入
        netflows = [series[i] - series[i - 1] for i in range(1, len(series))]
        # 7d 滚动累计，与前端 7d 视角对齐
        rolling_7d = [sum(netflows[max(0, i - 6):i + 1]) for i in range(len(netflows))]
        return {
            "status": "ok",
            "series": netflows[-days:],
            "rolling_7d": rolling_7d[-days:],
        }
    except Exception as e:
        return {"status": "error", "error": str(e), "series": [], "rolling_7d": []}


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
        payload = r.json()
        assets = payload.get("assets") or []
        if not assets:
            return {"status": "partial", "error": "empty assets", "net_flow_usd_m": None}
        total = 0.0
        btc_flow = None
        for a in assets:
            v = _safe_float(a.get("netFlowUsdM"))
            if v is not None:
                total += v
            if a.get("symbol") == "BTC":
                btc_flow = v
        return {
            "net_flow_usd_m": round(total, 2),
            "btc_net_flow_usd_m": btc_flow,
            "as_of_date": assets[0].get("date"),
            "status": "ok",
        }
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

def _cmc_category_7d_flow(category_id: str) -> tuple[float | None, int, int, list]:
    """
    通过 CMC category detail 聚合成分币的 7d 市值变化%（真实 7d 视角）。
    返回 (change_pct, used, total, top_coins)，change_pct 为 None 表示聚合失败/数据不足。
    top_coins 为按市值降序取前 5 的成分币摘要列表。
    """
    try:
        r = requests.get(
            f"{CMC_BASE}/trial-pro-api/v1/cryptocurrency/category",
            params={"id": category_id},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json().get("data", {})
        coins = data.get("coins", []) or []
        cur = 0.0
        prev = 0.0
        used = 0
        for c in coins:
            q = (c.get("quote") or {}).get("USD") or {}
            mcap = q.get("market_cap")
            p7 = q.get("percent_change_7d")
            if mcap is None or p7 is None:
                continue
            p7f = _safe_float(p7)
            if p7f <= -100:
                continue  # 价格归零，避免除零
            mcapf = _safe_float(mcap)
            cur += mcapf
            prev += mcapf / (1 + p7f / 100)
            used += 1
        # 构造 top_coins：按市值降序取 Top 5
        enriched = []
        for c in coins:
            q = (c.get("quote") or {}).get("USD") or {}
            mcap = _safe_float(q.get("market_cap"))
            price = _safe_float(q.get("price"))
            p7 = q.get("percent_change_7d")
            p7f = _safe_float(p7) if p7 is not None else None
            enriched.append({
                "name": c.get("name", ""),
                "symbol": c.get("symbol", ""),
                "market_cap": mcap,
                "price": price,
                "percent_change_7d": round(p7f, 2) if p7f is not None else None,
            })
        enriched.sort(key=lambda x: -x["market_cap"])
        top_coins = enriched[:5]
        if prev > 0 and used >= max(1, len(coins) // 2):
            return (cur - prev) / prev * 100, used, len(coins), top_coins
        return None, used, len(coins), top_coins
    except Exception:
        return None, 0, 0, []


def fetch_category_flow() -> dict:
    """
    CMC categories 7d 市值变化%（叙事榜 mcap 腿）。
    返回 {status, ranked: [{narrative, cmc_category, market_cap, mcap_change_7d_pct, mcap_period}], degraded}。
    detail 聚合失败时降级用 list 的 market_cap_change（24h）并标记 mcap_period='24h_fallback'。
    """
    try:
        r = requests.get(
            f"{CMC_BASE}/trial-pro-api/v1/cryptocurrency/categories",
            params={"limit": 500},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        cats = r.json().get("data", [])
    except Exception as e:
        return {"status": "error", "error": str(e), "ranked": [], "degraded": []}

    wanted: dict[str, dict] = {}
    # 两遍匹配：先精确、后前缀，避免前缀误配（如 Gaming 匹配到 Gaming Guild）
    for c in cats:
        name = (c.get("name") or "").strip().lower()
        for w in NARRATIVE_WATCHLIST:
            if name == w.lower() and w not in wanted:
                wanted[w] = c
    for c in cats:
        name = (c.get("name") or "").strip().lower()
        for w in NARRATIVE_WATCHLIST:
            if w not in wanted and name.startswith(w.lower()):
                wanted[w] = c
    if not wanted:
        return {"status": "ok", "ranked": [], "degraded": [], "note": "no watchlist category matched"}

    # 按市值取 top N，优先计算大盘叙事
    selected = sorted(wanted.items(), key=lambda x: -_safe_float(x[1].get("market_cap")))[:NARRATIVE_TOP_N]

    def work(item: tuple[str, dict]) -> dict:
        w, c = item
        change, used, total, top_coins = _cmc_category_7d_flow(c["id"])
        period = "7d" if change is not None else "24h_fallback"
        if change is None:
            change = c.get("market_cap_change")  # 24h 兜底，避免整条丢失
        return {
            "narrative": w,
            "cmc_category": c.get("name"),
            "market_cap": _safe_float(c.get("market_cap")),
            "mcap_change_7d_pct": round(change, 2) if change is not None else None,
            "mcap_period": period,
            "top_coins": top_coins,
        }

    with ThreadPoolExecutor(max_workers=6) as ex:
        ranked = list(ex.map(work, selected))

    degraded = [item["narrative"] for item in ranked if item["mcap_period"] == "24h_fallback"]

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
        e = agg.setdefault(cat, {"tvl": 0.0, "wsum": 0.0, "n": 0})
        e["tvl"] += tvl
        if ch7 is not None:
            e["wsum"] += _safe_float(ch7) * tvl
            e["n"] += 1

    categories: dict[str, dict] = {}
    for cat, e in agg.items():
        tvl = e["tvl"]
        change = (e["wsum"] / tvl) if tvl > 0 else 0.0
        categories[cat] = {
            "tvl": tvl,
            "tvl_change_7d_pct": round(change, 2),
            "protocols": e["n"],
        }
    return {"status": "ok", "categories": categories}


def build_narrative_flow_ranking(cat_flow: dict, tvl_flow: dict) -> dict:
    """
    合成叙事榜：有 TVL 腿（映射命中且 TVL≥阈值）= 市值变化 mcap_weight + TVL 变化 tvl_weight；
    无 TVL 腿（Meme/L1 等）= 仅市值变化。按合成值降序取前 rank_limit。
    返回 {status, ranked, degraded}。
    """
    tvl_cats = tvl_flow.get("categories", {}) if tvl_flow.get("status") == "ok" else {}
    mc_w = NARRATIVE_CHAIN["mcap_weight"]
    tvl_w = NARRATIVE_CHAIN["tvl_weight"]
    ranked: list[dict] = []
    for item in cat_flow.get("ranked", []):
        mcap7 = item.get("mcap_change_7d_pct")
        if mcap7 is None:
            continue
        dl_cat = NARRATIVE_TVL_MAP.get(item["narrative"])
        tvl_info = tvl_cats.get(dl_cat) if dl_cat else None
        if tvl_info and tvl_info.get("tvl", 0) >= TVL_LEG_MIN:
            composite = mc_w * mcap7 + tvl_w * tvl_info["tvl_change_7d_pct"]
            mode = "blended"
        else:
            composite = mcap7
            mode = "mcap_only"
        ranked.append({
            "narrative": item["narrative"],
            "composite_score": round(composite, 2),
            "mode": mode,
            "mcap_change_7d_pct": mcap7,
            "mcap_period": item.get("mcap_period"),
            "tvl_change_7d_pct": tvl_info["tvl_change_7d_pct"] if tvl_info else None,
            "tvl_usd": tvl_info["tvl"] if tvl_info else None,
            "market_cap": item.get("market_cap"),
            "top_coins": item.get("top_coins", []),
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


def fetch_chain_flow() -> dict:
    """
    DeFiLlama 链净流入榜 TOP5。/v2/chains 无 tvlPrevWeek 字段（实测），
    改为对 top 链逐一拉 /v2/historicalChainTvl 差分 7d 净流入。
    每条链同时附带该链 Top 5 协议（按 TVL 排序）供下钻。
    返回 {status, ranked: [{chain, tvl, flow_7d, flow_7d_pct, protocols}], degraded_count, scanned}。
    """
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
    degraded_count = sum(1 for x in results if x.get("degraded"))
    return {
        "status": "ok",
        "ranked": valid[:5],
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
    "push_confidence_threshold": "medium",   # 默认只推 高+中（low 剔除，语义由 conviction 承接）
    "conviction_high_min": 75,               # conviction ≥75 → HIGH 置顶
    "conviction_med_min": 55,                # conviction ≥55 → MED（观察池），<55 → LOW 剔除
    "protocol_top_n": 3,                     # P1-3 新协议 TVL 异动取前 N
    "exchange_netflow_min_usd": 100_000_000, # 交易所净流出触发 long 信号最小阈值
    # P0-1 MVRV 估值回归
    "mvrv_deep_undervalued_pct": 15,         # MVRV 百分位 ≤ 此值 → 深度低估
    "mvrv_undervalued_pct": 30,              # MVRV 百分位 ≤ 此值 → 低估
    "mvrv_overvalued_pct": 85,               # MVRV 百分位 ≥ 此值 → 高估
    # P0-3 conviction 各轴权重（总和 = 1.0）
    "conviction_weight_mvrv": 0.25,          # MVRV 估值权重
    "conviction_weight_cycle": 0.20,         # 周期相位权重
    "conviction_weight_funding": 0.15,       # funding 极性权重
    "conviction_weight_netflow": 0.15,       # 交易所净流权重
    "conviction_weight_stable": 0.10,        # 稳定币流向权重
    "conviction_weight_roi": 0.15,           # ROI 动量权重
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


def _push_opportunity(opp: dict, opportunities: list[dict], excluded: list[dict], t: dict) -> None:
    """写入 conviction_tier 并按 tier 过滤：LOW 进 excluded（可追溯），保留 confidence 向后兼容。

    tier 语义（P1-4 第一刀）：conviction_score ≥ conviction_high_min → HIGH 置顶；
    ≥ conviction_med_min → MED（观察池）；否则 LOW 剔除。旧 push_confidence_threshold 语义由 tier 承接。
    """
    score = opp.get("conviction_score")
    if score is None:
        score = 45
        opp["conviction_score"] = score
    high_min = int(t.get("conviction_high_min", 75))
    med_min = int(t.get("conviction_med_min", 55))
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
    cycle_phase: str,
    funding: float | None,
    exchange_netflow: float | None,
    stablecoin_flow: float | None,
    roi_1yr: float | None = None,
    t: dict,
) -> int:
    """P0-3: 多轴加权合成 conviction 分 (0-100)。

    各轴归一化到 0~100 后加权求和，cycle phase 作为调制系数乘入。
    """
    # ── MVRV 轴：低估=高分（均值回归逻辑），高估=低分 ──
    if mvrv_pct is not None:
        mvrv_score = max(0, min(100, 100 - mvrv_pct))
    else:
        mvrv_score = 50  # 无数据时中性

    # ── Cycle 轴：phase → score ──
    cycle_scores = {
        "early_top": 25, "late_top": 15, "top": 20,
        "early_bottom": 85, "late_bottom": 75, "bottom": 80,
        "mid_cycle": 50, "unknown": 50,
    }
    cycle_score = cycle_scores.get(cycle_phase, 50)

    # ── Funding 轴：正=过热（做空机会/风险），负=恐慌（做多机会） ──
    if funding is not None:
        funding_score = max(0, min(100, 50 - funding * 10000))
    else:
        funding_score = 50

    # ── 交易所净流轴：正=净流出(积累)=做多信号 ──
    if exchange_netflow is not None and exchange_netflow != 0:
        netflow_score = max(0, min(100, 50 + (exchange_netflow / 1e9) * 20))
    else:
        netflow_score = 50

    # ── 稳定币流轴：正=场外弹药充裕=做多信号 ──
    if stablecoin_flow is not None and stablecoin_flow != 0:
        stable_score = max(0, min(100, 50 + (stablecoin_flow / 1e10) * 30))
    else:
        stable_score = 50

    # ── ROI 轴：深度负(超跌)=高做多分，深度正(过热)=高做空分 ──
    if roi_1yr is not None:
        # ROI1yr -50% → 80 (超跌做多)；+100% → 20 (过热风险)
        roi_score = max(0, min(100, 50 - roi_1yr * 30))
    else:
        roi_score = 50

    w_mvrv = t.get("conviction_weight_mvrv", 0.25)
    w_cycle = t.get("conviction_weight_cycle", 0.20)
    w_funding = t.get("conviction_weight_funding", 0.15)
    w_netflow = t.get("conviction_weight_netflow", 0.15)
    w_stable = t.get("conviction_weight_stable", 0.10)
    w_roi = t.get("conviction_weight_roi", 0.15)

    raw = (
        mvrv_score * w_mvrv
        + cycle_score * w_cycle
        + funding_score * w_funding
        + netflow_score * w_netflow
        + stable_score * w_stable
        + roi_score * w_roi
    )
    return max(0, min(100, round(raw)))


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
    t = OPPORTUNITY_THRESHOLDS
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

    # ── P0-1: MVRV 估值回归（long，低估值深度低估币） ──
    deep_undervalued_pct = t.get("mvrv_deep_undervalued_pct", 15)
    undervalued_pct = t.get("mvrv_undervalued_pct", 30)
    for coin in mvrv_coins:
        pct = coin.get("pct_full")
        symbol = coin.get("symbol", "?")
        if pct is None:
            continue
        if pct <= deep_undervalued_pct:
            # 深度低估 → 高确定性均值回归机会
            mvrv_score_val = max(0, min(100, 100 - pct))
            conviction = _compute_conviction_score(
                mvrv_pct=pct, cycle_phase=cycle_phase,
                funding=funding_latest, exchange_netflow=ex_netflow,
                stablecoin_flow=stable_7d, roi_1yr=btc_roi_1yr, t=t,
            )
            trigger = (
                f"MVRV 百分位 {pct:.1f}%（深度低估 ≤{deep_undervalued_pct}%）→ "
                f"历史均值回归信号"
            )
            _push_opportunity(
                {"target": symbol, "direction": "long", "confidence": "high",
                 "conviction_score": conviction,
                 "trigger_logic": trigger,
                 "related_dims": ["mvrv_universe", "P0-1 估值回归"]},
                opportunities, excluded, t,
            )
        elif pct <= undervalued_pct:
            # 低估 → 中置信
            conviction = _compute_conviction_score(
                mvrv_pct=pct, cycle_phase=cycle_phase,
                funding=funding_latest, exchange_netflow=ex_netflow,
                stablecoin_flow=stable_7d, roi_1yr=btc_roi_1yr, t=t,
            )
            trigger = (
                f"MVRV 百分位 {pct:.1f}%（低估 ≤{undervalued_pct}%）→ "
                f"估值偏低，关注回归"
            )
            _push_opportunity(
                {"target": symbol, "direction": "long", "confidence": "medium",
                 "conviction_score": conviction,
                 "trigger_logic": trigger,
                 "related_dims": ["mvrv_universe", "P0-1 估值回归"]},
                opportunities, excluded, t,
            )
    if not mvrv_coins:
        degraded.append("mvrv_universe 数据缺失")

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
        conviction = _compute_conviction_score(
            mvrv_pct=_mvrv_pct_for("BTC", mvrv_map),
            cycle_phase=cycle_phase,
            funding=funding_latest, exchange_netflow=ex_netflow,
            stablecoin_flow=stable_7d, roi_1yr=btc_roi_1yr, t=t,
        )
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
             "trigger_logic": trigger, "related_dims": related},
            opportunities, excluded, t,
        )

    # ── 1b) 多资产采用背离 / 网络健康（CM 第三刀） ──
    cm_act = ((overview.get("dimensions") or {}).get("6a网络健康") or {}).get("data") or {}
    if cm_act.get("status") == "ok":
        for act_coin in (cm_act.get("coins") or []):
            sig = act_coin.get("signal")
            if sig == "accumulation":
                conviction = _compute_conviction_score(
                    mvrv_pct=_mvrv_pct_for(act_coin.get("symbol"), mvrv_map),
                    cycle_phase=cycle_phase,
                    funding=funding_latest, exchange_netflow=ex_netflow,
                    stablecoin_flow=stable_7d, roi_1yr=btc_roi_1yr, t=t,
                )
                _push_opportunity(
                    {"target": act_coin["symbol"], "direction": "long", "confidence": "medium",
                     "conviction_score": conviction,
                     "trigger_logic": (
                         f"{act_coin['symbol']} 活跃地址 {act_coin.get('adr_pct')}% 分位 + "
                         f"30d 收益 {act_coin.get('roi_30d')}% → 采用增长但价格未跟上（静默积累）"
                     ),
                     "related_dims": ["6a网络健康 CM 采用背离"]},
                    opportunities, excluded, t,
                )
            elif sig == "decline":
                _push_opportunity(
                    {"target": act_coin["symbol"], "direction": "watch", "confidence": "low",
                     "conviction_score": 0,
                     "trigger_logic": (
                         f"{act_coin['symbol']} 活跃地址 {act_coin.get('adr_pct')}% 分位 → 网络衰退警示"
                     ),
                     "related_dims": ["6a网络健康 CM 网络衰退"]},
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
            cycle_phase=cycle_phase,
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
            cycle_phase=cycle_phase,
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
            cycle_phase=cycle_phase,
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
            cycle_phase=cycle_phase,
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
                cycle_phase=cycle_phase,
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

    # 按 conviction_score 降序排列
    opportunities.sort(key=lambda x: x.get("conviction_score", 0), reverse=True)

    status = "ok"
    if not opportunities:
        status = "error" if not degraded else "partial"
    elif degraded:
        status = "partial"
    return {
        "status": status,
        "opportunities": opportunities,
        "excluded": excluded,
        "degraded": degraded,
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


def _build_tldr(today: dict, opps: list) -> str:
    cycle = today.get("btc_cycle") or {}
    parts = [f"BTC 周期：{cycle.get('phase_label') or cycle.get('phase') or '未知'}"]
    high = [o for o in opps if o.get("conviction_tier") == "HIGH"]
    if high:
        parts.append(f"高确定性机会 {len(high)} 条（{', '.join(str(o.get('target')) for o in high[:3])}）")
    else:
        parts.append("无 HIGH 机会（观察 MED）")
    return "；".join(parts)


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


def generate_morning_brief(today: dict, yesterday: dict | None) -> dict:
    """
    早报结构化骨架（设计文档第七节）。消费 overview 已有字段，不改 API 层。
    返回 {M0~M6, DIFF}；首跑 yesterday=None → DIFF=None 不报错。
    """
    cycle = today.get("btc_cycle") or {}
    diff = diff_overview(yesterday, today) if yesterday else None
    stab = fetch_stablecoin_supply_trend()
    opps = (today.get("opportunity_list") or {}).get("opportunities") or []
    divs = (today.get("divergence_signals") or {}).get("signals") or []

    return {
        "M0_tldr": _build_tldr(today, opps),
        "M1_cycle": cycle,
        "M2_flow": _build_flow(today, diff, stab),
        "M3_divergence": [
            d for d in divs
            if d.get("label") in ("DANGEROUS", "DIVERGENT")
        ],
        "M4_opportunities": [
            o for o in opps if o.get("conviction_tier") == "HIGH"
        ],
        "M4_watchlist": [
            o for o in opps if o.get("conviction_tier") != "HIGH"
        ],
        "M5_catalyst": today.get("event_calendar") or {},
        "M6_degraded": _collect_degraded(today),
        "DIFF": diff,
    }


def fetch_event_calendar() -> dict:
    """事件日历：宏观硬日程（FOMC/CPI/NFP）+ 已知重大解锁峰。仅展示，不参与子分。

    原 CoinGecko events 兜底已废弃（端点实测 404/返空），显式置空避免误导。
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
    # ③ （follow-up）CoinMarketCal：需 CMCAL_API_KEY，返回 crypto 专属事件（mainnet/解锁/空投）
    # cmcal = fetch_coinmarketcal() if os.getenv("CMCAL_API_KEY") else []

    events = hardcoded_events + unlock_events
    return {
        "status": "ok" if events else "partial",
        "hardcoded": events,
        "gecko": [],  # 已废弃，显式空（不再静默失败）
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


def _build_mvrv_universe() -> dict:
    """构建多币 MVRV 极值汇总：max/min/median + 各币分位。"""
    try:
        from crypto_research.config import get_settings
        from crypto_research.db.conn import get_connection

        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    WITH latest AS (
                        SELECT d.cm_symbol, d.cap_mvrv_cur, p.mvrv_pct_full,
                               CASE
                                   WHEN p.mvrv_pct_full > 90 THEN 'HIGH'
                                   WHEN p.mvrv_pct_full < 10 THEN 'LOW'
                                   ELSE 'NONE'
                               END AS extreme
                        FROM biz.cm_onchain_percentile_full p
                        JOIN biz.cm_asset_onchain_daily d
                            ON p.asset_id = d.asset_id AND p.metric_date = d.metric_date
                        WHERE p.metric_date = (SELECT MAX(metric_date) FROM biz.cm_asset_onchain_daily)
                          AND d.cap_mvrv_cur IS NOT NULL
                    )
                    SELECT cm_symbol, cap_mvrv_cur, mvrv_pct_full, extreme
                    FROM latest
                    ORDER BY mvrv_pct_full DESC NULLS LAST
                """)
                rows = cur.fetchall()

        if not rows:
            return {"status": "error", "coins": []}

        pcts = [float(r[2]) for r in rows if r[2] is not None]
        coins = []
        for r in rows:
            coins.append({
                "symbol": r[0],
                "value": float(r[1]) if r[1] is not None else None,
                "pct_full": float(r[2]) if r[2] is not None else None,
                "extreme": r[3] or "NONE",
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
    onchain = fetch_onchain_anomaly_signals()
    btc_onchain = fetch_btc_onchain_signals()  # CM Community BTC 链上指标
    cm_activity = fetch_cm_activity_signals()  # CM 多资产活跃/采用信号

    # 若链上 CEX 净流量可用，用其归一化分值覆盖 cryptoetf cefi（优先级更高）
    if onchain.get("status") == "ok" and onchain.get("cefi_score") is not None:
        cefi = {
            "value": onchain["cefi_score"],
            "status": "ok",
            "source": "onchain_cex_netflow",
        }

    # ── P2-1 历史分位：拉取历史序列 ──
    fear_greed_hist = fetch_fear_greed_history(90)
    mvrv_hist = fetch_mvrv_history("btc")
    stablecoin_flow_hist = fetch_stablecoin_netflow_history(30)
    cefi_hist = fetch_cefi_history(30)
    btc_dom_hist = fetch_btc_dominance_history(30)

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

    # ── P1-1 板块/链资金净流入（7d 视角） ──
    cat_flow = fetch_category_flow()
    tvl_flow = fetch_category_tvl_flow()
    chain_flow = fetch_chain_flow()
    narrative_flow = build_narrative_flow_ranking(cat_flow, tvl_flow)

    # ── P1-2 背离检测（价格/OI、价格/funding、价格/稳定币、BTC/纳指） ──
    divergence = build_divergence_signals()

    # ── BTC 周期定位（OBM 链上指标） ──
    try:
        from db_stats import get_btc_cycle_position
        btc_cycle = get_btc_cycle_position()
    except Exception:
        btc_cycle = {"status": "error", "phase": "unknown", "phase_label": "数据不可用", "signals": []}

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

    _cache = result
    _cache_ts = now

    return result


if __name__ == "__main__":
    import json
    result = get_market_overview()
    print(json.dumps(result, indent=2, ensure_ascii=False))
