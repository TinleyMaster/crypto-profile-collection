"""催化剂资产过滤：排除非加密资产（美股 tokenized stock / 商品期货）。

背景：
    core.asset 有 asset_type 字段，但取值范围（stablecoin/meme/token/coin）
    无法可靠区分「美股 tokenized stock」和「商品期货」——这些资产常被
    标成 token/coin。因此用「名称特征黑名单」做过滤，最稳健。

适用范围：
    - 慢汇总/快提醒邮件查询（JOIN core.asset 后按 canonical_name 过滤）
    - 如需彻底杜绝入库，应在摄入层 `map_pairs_to_asset_ids` 同样套用
"""

from __future__ import annotations

# 名称特征黑名单（正则，对 canonical_name 做不区分大小写匹配）
# 命中即视为「非加密资产」，从信号/邮件中剔除。
_NAME_BLOCK_PATTERNS: tuple[str, ...] = (
    # 美股 tokenized stock（PreStocks / bStocks 等币安股票代币）
    r"tokeniz\w*",        # Tokenized / Tokenised / Tokenization
    r"b?stocks",          # bStocks / PreStocks
    r"(^|\W)pre\s*stocks\b",
    # 商品/大宗期货（仅匹配明确的衍生品形态，避免误杀同名 crypto 项目）
    r"\bfutures?\b",      # Futures
    r"\bderivativ\w*\b",  # Derivatives
    r"(crude|brent)\s+oil",
    r"\b([cg]old|silver|brent|crude)\s*\(?\s*(futures|derivativ|oil)?\)?\b",
    r"(crude|brent|heating|natural)\s+gas\b",
)

# 明确的允许豁免（即使名称命中上面某个弱模式，也不过滤）
# 目前互补性豁免，后续若误伤可在此加
_ALLOW_SYMBOLS: set[str] = set()


def is_non_crypto(asset_name: str, asset_symbol: str | None = None) -> bool:
    """判断资产是否非加密资产（应被过滤）。

    Args:
        asset_name: canonical_name（如 "OpenAI Tokenized Stock"）
        asset_symbol: canonical_symbol（如 "OPENAI"），用于豁免判断

    Returns:
        True = 非加密资产，应从信号/邮件中排除
    """
    if asset_symbol and asset_symbol.upper() in _ALLOW_SYMBOLS:
        return False
    if not asset_name:
        return False
    import re
    name = asset_name.lower()
    return any(re.search(p, name) for p in _NAME_BLOCK_PATTERNS)


# 可直接拼进 SQL 的过滤条件（psycopg 可用，只需 a.canonical_name 列存在）
# 用法：AND {ASSET_NAME_FILTER_SQL}  （JOIN core.asset a 之后）
# PostgreSQL !~ 走 PG ARE 正则：不认 \s/\b 简写，用 [[:space:]] 和词边界写法。
ASSET_NAME_FILTER_SQL = """
LOWER(COALESCE(a.canonical_name, '')) !~ 'tokeniz|b[[:space:]]*stocks|pre[[:space:]]*stocks|futures|derivativ|crude[[:space:]]+oil|brent|[[:<:]](gold|silver)[[:>:]][[:space:]]*\(?[[:space:]]*(futures|derivativ|oil)?[[:space:]]*\)?'
"""