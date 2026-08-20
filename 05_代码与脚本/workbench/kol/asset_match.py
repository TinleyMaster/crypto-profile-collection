"""
币种匹配：将帖子中提取的 symbol 与 core.asset 关联。

策略：
  1. 精确匹配（不区分大小写）
  2. 常见别名映射（如 BTC → Bitcoin, ETH → Ethereum）
  3. 匹配失败返回 None，asset_id 留空
"""
from __future__ import annotations

from .db import find_asset_by_symbol

# 常见别名映射（别名 → 标准 symbol）
_ALIAS_MAP: dict[str, str] = {
    "BITCOIN": "BTC",
    "BTC": "BTC",
    "ETHEREUM": "ETH",
    "ETH": "ETH",
    "SOLANA": "SOL",
    "SOL": "SOL",
    "BINANCECOIN": "BNB",
    "BNB": "BNB",
    "XRP": "XRP",
    "RIPPLE": "XRP",
    "DOGECOIN": "DOGE",
    "DOGE": "DOGE",
    "CARDANO": "ADA",
    "ADA": "ADA",
    "AVALANCHE": "AVAX",
    "AVAX": "AVAX",
    "DOT": "DOT",
    "POLKADOT": "DOT",
    "MATIC": "MATIC",
    "POLYGON": "MATIC",
    "LINK": "LINK",
    "CHAINLINK": "LINK",
    "TRX": "TRX",
    "TRON": "TRX",
    "TON": "TON",
    "TONCOIN": "TON",
    "ATOM": "ATOM",
    "COSMOS": "ATOM",
    "UNI": "UNI",
    "UNISWAP": "UNI",
    "LTC": "LTC",
    "LITECOIN": "LTC",
    "BCH": "BCH",
    "BITCOINCASH": "BCH",
    "APT": "APT",
    "APTOS": "APT",
    "ARB": "ARB",
    "ARBITRUM": "ARB",
    "OP": "OP",
    "OPTIMISM": "OP",
    "NEAR": "NEAR",
    "NEARPROTOCOL": "NEAR",
    "SUI": "SUI",
    "SEI": "SEI",
    "PEPE": "PEPE",
    "SHIB": "SHIB",
    "SHIBA": "SHIB",
    "DOGE": "DOGE",
    "WIF": "WIF",
    "FLOKI": "FLOKI",
    "BONK": "BONK",
    "JUP": "JUP",
    "JUPITER": "JUP",
    "RAY": "RAY",
    "RAYDIUM": "RAY",
    "ORCA": "ORCA",
    "JTO": "JTO",
    "JITO": "JTO",
}


def match_asset(symbol: str | None) -> int | None:
    """
    将 symbol 匹配到 core.asset 的 asset_id。

    Args:
        symbol: 从帖子中提取的币种符号

    Returns:
        asset_id 或 None
    """
    if not symbol:
        return None

    symbol = symbol.strip().upper()
    if not symbol:
        return None

    # 先精确匹配
    asset_id = find_asset_by_symbol(symbol)
    if asset_id:
        return asset_id

    # 别名映射后再匹配
    canonical = _ALIAS_MAP.get(symbol)
    if canonical and canonical != symbol:
        asset_id = find_asset_by_symbol(canonical)
        if asset_id:
            return asset_id

    # 去掉常见后缀再试（如 BTCUSDT → BTC）
    for suffix in ("USDT", "USDC", "BUSD", "TUSD", "USDS", "PERP", "SWAP"):
        if symbol.endswith(suffix) and len(symbol) > len(suffix):
            base = symbol[: -len(suffix)]
            asset_id = find_asset_by_symbol(base)
            if asset_id:
                return asset_id
            # 别名映射
            canonical = _ALIAS_MAP.get(base)
            if canonical:
                asset_id = find_asset_by_symbol(canonical)
                if asset_id:
                    return asset_id
            # 币安永续 1000 前缀（如 1000PEPEUSDT → PEPE）
            if base.startswith("1000") and len(base) > 4:
                base2 = base[4:]
                asset_id = find_asset_by_symbol(base2)
                if asset_id:
                    return asset_id
                canonical2 = _ALIAS_MAP.get(base2)
                if canonical2:
                    asset_id = find_asset_by_symbol(canonical2)
                    if asset_id:
                        return asset_id

    return None
