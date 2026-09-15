"""催化剂资产分类：把资产分为「加密货币」与「美股/商品（tokenized stock & 期货）」。

背景：
    core.asset 有 asset_type 字段，但取值范围（stablecoin/meme/token/coin）
    无法可靠区分「美股 tokenized stock」和「商品期货」——这些资产常被
    标成 token/coin。因此用「名称特征」做分类，最稳健。

用途：
    - 邮件提醒分两类：加密货币一封、美股/商品一封（各自独立去重）
    - 慢汇总/快提醒查询：crypto 节用 CRYPTO_FILTER_SQL，美股节用 IS_STOCK_SQL
"""

from __future__ import annotations

# 美股 tokenized stock / 商品期货 的名称特征（命中即视为「非加密」）
# 正则按大小写不敏感匹配 canonical_name。
_STOCK_PATTERNS: tuple[str, ...] = (
    # 美股 tokenized stock（bStocks / PreStocks 等币安股票代币）
    r"tokeniz\w*",        # Tokenized / Tokenised / Tokenization
    r"b?stocks",          # bStocks / PreStocks
    r"pre\s*stocks\b",
    # tokenized stock 发行方（线上审计 2026-09-15：NVIDIA • Robinhood Token /
    # SpaceX (Backpack Securities) / Boyd Gaming (Dinari Tokenized Stock) 等）
    r"robinhood\s+token",
    r"backpack\s+securities",
    r"\bdinari\b",
    r"\bxstock\b",
    # 商品/大宗期货（仅匹配明确的衍生品形态，避免误杀同名 crypto）
    r"\bfutures?\b",      # Futures
    r"\bderivativ\w*\b",  # Derivatives
    r"(crude|brent)\s+oil",
    r"\bgold\s*(futures|derivativ)\b",
    r"\bsilver\s*(futures|derivativ)\b",
    r"(crude|brent|heating|natural)\s+gas\b",
)

# 明确的豁免（即使名称命中上面某个弱模式，仍视为「加密货币」）
_CRYPTO_ALLOW_SYMBOLS: set[str] = set()
_CRYPTO_ALLOW_NAMES: tuple[str, ...] = ()


def is_stock(asset_name: str, asset_symbol: str | None = None) -> bool:
    """判断资产是否为「美股/商品」（非加密货币）。

    攻击同名 crypto 项目误伤：
    - `$Copper`、`$Gold`、`$Silver` 这类 meme 币不会命中（需带 futures/derivativ 限定）
    - 可通过白名单豁免
    """
    if not asset_name:
        return False
    name = asset_name.lower()
    if asset_symbol and asset_symbol.upper() in _CRYPTO_ALLOW_SYMBOLS:
        return False
    if name in _CRYPTO_ALLOW_NAMES:
        return False
    import re
    return any(re.search(p, name) for p in _STOCK_PATTERNS)


def is_crypto(asset_name: str, asset_symbol: str | None = None) -> bool:
    """判断资产是否为加密货币（= 非美股/商品）。"""
    return not is_stock(asset_name, asset_symbol)


def is_non_crypto(asset_name: str, asset_symbol: str | None = None) -> bool:
    """兼容旧名：True = 非加密资产（美股/商品）。等价于 is_stock。"""
    return is_stock(asset_name, asset_symbol)


# ---------------------------------------------------------------------
# SQL 片段（JOIN core.asset a 之后使用）。
# PostgreSQL ARE 正则：不认 \\s/\\b 简写，用 [[:space:]] 与词边界写法。
# ---------------------------------------------------------------------

# 命中即「美股/商品」（非加密）
IS_STOCK_SQL = """
LOWER(COALESCE(a.canonical_name, '')) ~ 'tokeniz|b[[:space:]]*stocks|pre[[:space:]]*stocks|futures|derivativ|crude[[:space:]]+oil|brent|heating[[:space:]]+gas|natural[[:space:]]+gas|robinhood[[:space:]]+token|backpack[[:space:]]+securities|\\mdinari\\M|\\mxstock\\M'
"""

# 命中即「非美股/商品 = 加密货币」（crypto 邮件用）
CRYPTO_FILTER_SQL = f"""
LOWER(COALESCE(a.canonical_name, '')) !~ 'tokeniz|b[[:space:]]*stocks|pre[[:space:]]*stocks|futures|derivativ|crude[[:space:]]+oil|brent|robinhood[[:space:]]+token|backpack[[:space:]]+securities|\\mdinari\\M|\\mxstock\\M'
"""

# 兼容旧名（= crypto 过滤，等价 CRYPTO_FILTER_SQL）
ASSET_NAME_FILTER_SQL = CRYPTO_FILTER_SQL