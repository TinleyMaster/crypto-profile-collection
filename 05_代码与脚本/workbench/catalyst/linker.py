"""
资产关联模块：将催化剂的交易对 → core.asset.asset_id（支持多资产）。

策略：
1. 从 related_pairs 提取每个交易对的 base symbol
2. 逐个查 asset_source_map（binance 源优先）→ asset_id
3. 去重后返回所有关联的 asset_id 列表

带缓存，避免重复查库。
"""
from __future__ import annotations

import re
import logging

logger = logging.getLogger(__name__)

# symbol(大写) -> asset_id 缓存
_symbol_asset_cache: dict[str, int | None] = {}

# 常见 quote 币种，用于拆分交易对
_QUOTE_ASSETS = (
    "USDT", "USDC", "BUSD", "TUSD", "USDP", "FDUSD",
    "BTC", "ETH", "BNB", "SOL", "XRP",
)


def extract_base_symbol(pair: str) -> str | None:
    """从交易对中提取 base symbol（大写）。

    优先匹配已知 quote 后缀，匹配不到返回 None。
    """
    if not pair:
        return None
    pair = pair.upper().strip()
    for quote in _QUOTE_ASSETS:
        if pair.endswith(quote) and len(pair) > len(quote):
            base = pair[: -len(quote)]
            # 至少 2 个字符，且不全是数字
            if len(base) >= 2 and any(c.isalpha() for c in base):
                return base
    return None


def extract_pairs_from_text(text: str) -> list[str]:
    """从正文中提取交易对（cashtag 兜底）。

    匹配 $BTC / BTCUSDT / $ETHUSDT 等形式。
    """
    if not text:
        return []
    pairs: list[str] = []
    seen: set[str] = set()

    # 模式 1：$XXXUSDT 或 $XXX
    for m in re.finditer(r"\$([A-Z0-9]{2,20})(USDT|USDC|BTC|ETH|BNB)?\b", text):
        base = m.group(1)
        quote = m.group(2) or "USDT"
        pair = base + quote
        if pair not in seen and any(c.isalpha() for c in base):
            seen.add(pair)
            pairs.append(pair)

    # 模式 2：直接 XXXUSDT（大写字母+数字 2-20 位 + USDT）
    for m in re.finditer(r"\b([A-Z0-9]{2,20})USDT\b", text):
        base = m.group(1)
        # 过滤明显不是币种的
        if not any(c.isalpha() for c in base):
            continue
        if base in ("USD", "USDC", "BUSD", "TUSD", "USDP", "FDUSD"):
            continue
        pair = base + "USDT"
        if pair not in seen:
            seen.add(pair)
            pairs.append(pair)

    return pairs


def map_pairs_to_asset_ids(
    pairs: list[str],
    conn,
    source_hint: str = "binance",
) -> list[int]:
    """将交易对列表映射为 asset_id 列表（多资产）。

    Args:
        pairs: 交易对列表（如 ["BTCUSDT", "ETHUSDT"]）
        conn: 数据库连接
        source_hint: 优先查的数据源（默认 binance，因为交易对来自币安）

    Returns:
        asset_id 列表（去重，顺序按 pairs 出现顺序）
    """
    if not pairs:
        return []

    asset_ids: list[int] = []
    seen: set[int] = set()

    for pair in pairs:
        base = extract_base_symbol(pair)
        if not base:
            continue

        # 查缓存
        if base in _symbol_asset_cache:
            aid = _symbol_asset_cache[base]
            if aid is not None and aid not in seen:
                seen.add(aid)
                asset_ids.append(aid)
            continue

        # 查库：优先 source_hint 来源的 source_asset_key
        row = conn.execute(
            """
            SELECT a.asset_id
            FROM core.asset a
            JOIN core.asset_source_map m ON a.asset_id = m.asset_id
            WHERE m.source_code = %s
              AND UPPER(m.source_asset_key) = %s
            LIMIT 1
            """,
            (source_hint, base),
        ).fetchone()
        if row:
            aid = row["asset_id"]
            _symbol_asset_cache[base] = aid
            if aid not in seen:
                seen.add(aid)
                asset_ids.append(aid)
            continue

        # 退一步：asset 表的 canonical_symbol
        row = conn.execute(
            """
            SELECT asset_id
            FROM core.asset
            WHERE UPPER(canonical_symbol) = %s
            LIMIT 1
            """,
            (base,),
        ).fetchone()
        if row:
            aid = row["asset_id"]
            _symbol_asset_cache[base] = aid
            if aid not in seen:
                seen.add(aid)
                asset_ids.append(aid)
            continue

        # 没找到，缓存 None
        _symbol_asset_cache[base] = None

    return asset_ids
