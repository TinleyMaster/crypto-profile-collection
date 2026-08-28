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
COINMETRICS_BASE = "https://community-api.coinmetrics.io"
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
DL_BASE = "https://api.llama.fi"
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
NARRATIVE_TOP_N = 15      # 最多拉取 detail 聚合的叙事数（受 CMC 限流约束）
TVL_LEG_MIN = 50_000_000  # TVL 腿阈值：低于此视为无有效 TVL，仅用市值变化
CHAIN_TOP_N = 30          # 链榜扫描的 top 链数（按当前 TVL）


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


# ══════════════════════════════════════════════════════════════
# P1-1 板块/链资金净流入（7d 视角）
# ══════════════════════════════════════════════════════════════

def _cmc_category_7d_flow(category_id: str) -> tuple[float | None, int, int]:
    """
    通过 CMC category detail 聚合成分币的 7d 市值变化%（真实 7d 视角）。
    返回 (change_pct, used, total)，change_pct 为 None 表示聚合失败/数据不足。
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
        if prev > 0 and used >= max(1, len(coins) // 2):
            return (cur - prev) / prev * 100, used, len(coins)
        return None, used, len(coins)
    except Exception:
        return None, 0, 0


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
        change, used, total = _cmc_category_7d_flow(c["id"])
        period = "7d" if change is not None else "24h_fallback"
        if change is None:
            change = c.get("market_cap_change")  # 24h 兜底，避免整条丢失
        return {
            "narrative": w,
            "cmc_category": c.get("name"),
            "market_cap": _safe_float(c.get("market_cap")),
            "mcap_change_7d_pct": round(change, 2) if change is not None else None,
            "mcap_period": period,
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
    合成叙事榜：有 TVL 腿（映射命中且 TVL≥阈值）= 市值变化 0.5 + TVL 变化 0.5；
    无 TVL 腿（Meme/L1 等）= 仅市值变化。按合成值降序取前 10。
    返回 {status, ranked, degraded}。
    """
    tvl_cats = tvl_flow.get("categories", {}) if tvl_flow.get("status") == "ok" else {}
    ranked: list[dict] = []
    for item in cat_flow.get("ranked", []):
        mcap7 = item.get("mcap_change_7d_pct")
        if mcap7 is None:
            continue
        dl_cat = NARRATIVE_TVL_MAP.get(item["narrative"])
        tvl_info = tvl_cats.get(dl_cat) if dl_cat else None
        if tvl_info and tvl_info.get("tvl", 0) >= TVL_LEG_MIN:
            composite = 0.5 * mcap7 + 0.5 * tvl_info["tvl_change_7d_pct"]
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
        })

    ranked.sort(key=lambda x: -x["composite_score"])
    degraded = list(cat_flow.get("degraded", []))
    status = "ok"
    if not ranked:
        status = "error"
    elif degraded or tvl_flow.get("status") != "ok":
        status = "partial"
    return {"ranked": ranked[:10], "status": status, "degraded": degraded}


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


def fetch_chain_flow() -> dict:
    """
    DeFiLlama 链净流入榜 TOP5。/v2/chains 无 tvlPrevWeek 字段（实测），
    改为对 top 链逐一拉 /v2/historicalChainTvl 差分 7d 净流入。
    返回 {status, ranked: [{chain, tvl, flow_7d, flow_7d_pct}], degraded_count, scanned}。
    """
    try:
        r = requests.get(f"{DL_BASE}/v2/chains", timeout=TIMEOUT)
        r.raise_for_status()
        chains = r.json()
    except Exception as e:
        return {"status": "error", "error": str(e), "ranked": [], "degraded_count": 0, "scanned": 0}

    top = sorted(chains, key=lambda x: -(x.get("tvl") or 0))[:CHAIN_TOP_N]

    def work(c: dict) -> dict | None:
        # 历史端点用链显示名（实测 BSC/Polygon/Avalanche 的 gecko_id 不可用，name 可用）
        ident = c.get("name") or c.get("gecko_id")
        if not ident:
            return None
        info = _chain_7d_flow(ident)
        if info is None:
            return {
                "chain": c.get("name"),
                "tvl": _safe_float(c.get("tvl")),
                "flow_7d": None,
                "flow_7d_pct": None,
                "degraded": True,
            }
        return {
            "chain": c.get("name"),
            "tvl": info["tvl"],
            "tvl_prev_week": info["tvl_prev_week"],
            "flow_7d": info["flow_7d"],
            "flow_7d_pct": info["flow_7d_pct"],
            "degraded": False,
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

    # ── P1-1 板块/链资金净流入（7d 视角） ──
    cat_flow = fetch_category_flow()
    tvl_flow = fetch_category_tvl_flow()
    chain_flow = fetch_chain_flow()
    narrative_flow = build_narrative_flow_ranking(cat_flow, tvl_flow)

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
                },
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
