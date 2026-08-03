from __future__ import annotations

from typing import Any


def classify_asset_type(
    symbol: str | None,
    category_hint: str | None,
    urls: dict[str, Any],
    has_platform: bool,
) -> str:
    hint = (category_hint or "").strip().lower()
    symbol_norm = (symbol or "").strip().upper()

    if "stablecoin" in hint or symbol_norm in {
        "USDT",
        "USDC",
        "DAI",
        "FDUSD",
        "TUSD",
        "USDE",
    }:
        return "stablecoin"
    if "meme" in hint:
        return "meme"
    if has_platform:
        return "token"
    return "coin"


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
