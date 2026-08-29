"""
ETH/BTC 汇率风险偏好开关模块。
从 Binance 公开 API 获取实时汇率 + 7d/30d 变化率，
作为风险偏好判断依据（上行=风险偏好扩张，下行=避险回流 BTC）。
"""

from __future__ import annotations

import os
import time
import requests
from typing import Any

BINANCE_BASE = "https://api.binance.com"
TIMEOUT = 10

# ── P0-2 风险偏好阈值默认值（P2-4 外置 market_rules.yaml） ──
ETHBTC_RISK_DEFAULT = {
    "expansion_threshold": 5.0,    # > +5% → 风险偏好扩张
    "flight_threshold": -5.0,      # < -5% → 避险回流 BTC
}


def _load_ethbtc_risk() -> dict:
    """从 market_rules.yaml 加载 ETH/BTC 风险偏好阈值（缺失/解析失败回退默认值）。"""
    rules = dict(ETHBTC_RISK_DEFAULT)
    try:
        import yaml
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market_rules.yaml")
        if not os.path.exists(path):
            return rules
        data = yaml.safe_load(open(path, encoding="utf-8")) or {}
        overrides = data.get("ethbtc_risk") or {}
        for k, v in overrides.items():
            if k in rules:
                try:
                    rules[k] = float(v)
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass
    return rules


_ETHBTC_RISK = _load_ethbtc_risk()
RISK_EXPANSION_THRESHOLD = _ETHBTC_RISK["expansion_threshold"]
RISK_FLIGHT_THRESHOLD = _ETHBTC_RISK["flight_threshold"]

# 缓存
_cache: dict[str, Any] = {}
_cache_ts: float = 0
CACHE_TTL = 60  # 1 分钟缓存


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def fetch_ethbtc_price() -> dict:
    """获取 ETHBTC 实时价格。返回 {price, source, fetched_at} 或 {error, source}。"""
    try:
        r = requests.get(
            f"{BINANCE_BASE}/api/v3/ticker/price",
            params={"symbol": "ETHBTC"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        return {
            "price": _safe_float(data.get("price")),
            "source": "binance",
            "fetched_at": int(time.time()),
        }
    except Exception as e:
        return {"error": str(e), "source": "binance"}


def fetch_ethbtc_klines() -> dict:
    """获取 ETHBTC 日线 K 线（31 天），计算 7d/30d 变化率。
    返回 {change_7d, change_30d, source, fetched_at} 或 {error, source}。
    """
    try:
        r = requests.get(
            f"{BINANCE_BASE}/api/v3/klines",
            params={"symbol": "ETHBTC", "interval": "1d", "limit": 31},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()

        if not data or len(data) < 2:
            return {"error": "insufficient klines data", "source": "binance"}

        # klines 格式: [open_time, open, high, low, close, volume, ...]
        closes = [_safe_float(k[4]) for k in data]
        latest = closes[-1]

        # 7d 变化 = (今天 - 7天前) / 7天前 * 100
        change_7d = None
        if len(closes) >= 8:
            price_7d_ago = closes[-8]
            if price_7d_ago > 0:
                change_7d = round((latest - price_7d_ago) / price_7d_ago * 100, 2)

        # 30d 变化 = (今天 - 30天前) / 30天前 * 100
        change_30d = None
        if len(closes) >= 31:
            price_30d_ago = closes[-31]
            if price_30d_ago > 0:
                change_30d = round((latest - price_30d_ago) / price_30d_ago * 100, 2)

        return {
            "change_7d": change_7d,
            "change_30d": change_30d,
            "source": "binance",
            "fetched_at": int(time.time()),
        }
    except Exception as e:
        return {"error": str(e), "source": "binance"}


def judge_risk_appetite(change_30d: float | None) -> str:
    """根据 30d 变化率判定风险偏好状态。"""
    if change_30d is None:
        return "数据缺失"
    if change_30d > RISK_EXPANSION_THRESHOLD:
        return "风险偏好扩张"
    elif change_30d < RISK_FLIGHT_THRESHOLD:
        return "避险回流 BTC"
    else:
        return "中性"


def get_ethbtc_overview() -> dict:
    """汇总 ETHBTC 汇率信息（带缓存）。返回结构：
    {
        price: float,
        change_7d: float | None,
        change_30d: float | None,
        risk_appetite: str,
        source: str,
        fetched_at: int
    }
    """
    global _cache, _cache_ts

    now = time.time()
    if _cache and (now - _cache_ts) < CACHE_TTL:
        return _cache

    price_info = fetch_ethbtc_price()
    klines_info = fetch_ethbtc_klines()

    # 容错：任一失败不中断
    price = price_info.get("price")
    error = price_info.get("error") or klines_info.get("error")
    source = "binance" if not error else "binance (partial)"

    result = {
        "price": price,
        "change_7d": klines_info.get("change_7d"),
        "change_30d": klines_info.get("change_30d"),
        "risk_appetite": judge_risk_appetite(klines_info.get("change_30d")),
        "source": source,
        "fetched_at": int(now),
    }

    if error:
        result["warning"] = f"⚠️ Binance 数据源异常: {error}"

    _cache = result
    _cache_ts = now

    return result
