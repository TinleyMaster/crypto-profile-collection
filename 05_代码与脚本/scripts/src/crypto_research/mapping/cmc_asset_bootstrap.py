from __future__ import annotations

from typing import Any


# 稳定币误判陷阱类别（描述“发行方/生态”而非资产本身是稳定币）
STABLE_FALSE_CATS = {"stable ecosystem", "stablecoin issuer"}

# 强稳定币标签集（排除 stablecoin-protocol 等模糊标签）
STRONG_STABLE_TAGS = {
    "stablecoin",
    "usd-stablecoin",
    "asset-backed-stablecoin",
    "fiat-stablecoin",
    "fiat-backed-stablecoin",
    "algorithmic-stablecoin",
    "crypto-backed-stablecoin",
    "yield-bearing-stablecoin",
    "eur-stablecoin",
    "krw-stablecoin",
}

# 硬编码稳定币 symbol 白名单（无 CMC 信号时的兜底）
STABLE_SYMBOLS = {"USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE"}


def classify_asset_type(
    symbol: str | None,
    category_hint: str | None,
    urls: dict[str, Any],
    has_platform: bool,
    tags: list[str] | None = None,
    categories: list[str] | None = None,
) -> str:
    """多信号交叉判定 asset_type，稳定币优先。

    旧逻辑只用 category_hint 单值做子串匹配，过窄，导致真实 meme / 稳定币
    大量被漏标为 token/coin（P0-3 数据污染根因）。现改为：

      1) 稳定币优先：categories 含 'stablecoin'（排除 'stable ecosystem' /
         'stablecoin issuer' 两个误判陷阱）或 category_hint == 'stablecoin'
         或 tags ∈ 强稳定币标签集 或 symbol 在稳定币白名单 → stablecoin
      2) meme：categories / tags / category_hint 任意含 'meme' 子串 → meme
      3) 其余：有 platform（合约/平台）为 token，否则 coin
    """
    cats = [c.lower() for c in (categories or [])]
    tagset = set(t.lower() for t in (tags or []))
    hint = (category_hint or "").strip().lower()
    symbol_norm = (symbol or "").strip().upper()

    # 1) 稳定币优先
    for c in cats:
        if "stablecoin" in c and c not in STABLE_FALSE_CATS:
            return "stablecoin"
    if hint == "stablecoin" or symbol_norm in STABLE_SYMBOLS:
        return "stablecoin"
    if tagset & STRONG_STABLE_TAGS:
        return "stablecoin"

    # 2) meme
    blob = " ".join(cats + list(tagset) + ([hint] if hint else []))
    if "meme" in blob:
        return "meme"

    # 3) coin / token
    return "token" if has_platform else "coin"


def build_description_short(
    description: str | None, max_length: int = 500
) -> str | None:
    if not description:
        return None
    text = " ".join(description.split()).strip()
    if not text:
        return None
    if len(text) <= max_length:
        return text
    return text[: max_length - 3].rstrip() + "..."
