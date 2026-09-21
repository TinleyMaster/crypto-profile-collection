from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


# 上游 CMC 对低流动性/未上所/已下架资产把「无数据」编码成 0，与真值 0 不可分辨。
# 按项目铁律（0 与缺失不可分辨时一律按无数据）统一归一化为 NULL，
# 避免伪 0 污染市值/成交量统计（审计 P0-2，2026-09-18）。
#
# 2026-09-21 复验补丁（D+3 新-1 / F1+F2）：判零必须按「落库列精度」而非 Python 原值。
# 例：CMC 原始 market_cap=0.0031 在 Python 侧 float(x)==0 为 False 被放过，
# 但列 numeric(38,2) 落库舍入为 0.00 → 伪 0 漏网。故统一量化到列 scale 后再判零；
# 同时把 price_usd 纳入归一化范围（此前完全未覆盖，导致 price 伪 0 入库）。
_FIELD_SCALE = {
    "price_usd": 18,   # numeric(38,18)
    "market_cap": 2,   # numeric(38,2)
    "fdv": 2,          # numeric(38,2)
    "volume_24h": 2,   # numeric(38,2)
}
# 无可信价格时，基于价格的市值/成交量字段一并置 NULL
_PRICE_DEPENDENT_FIELDS = ("market_cap", "fdv", "volume_24h")


def _rounds_to_zero(value: Any, scale: int) -> bool:
    """按列精度（scale 位小数）量化后是否为 0（无法解析视为缺失，同样返回 True）。

    只做判零，不改写原值，避免改变下游依赖的数值类型。
    """
    if value is None:
        return False
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return True
    if not dec.is_finite():
        return True
    try:
        return dec.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP) == 0
    except InvalidOperation:
        # 超出该列可量化范围（如 1e40），不当作缺失，交下游 range guard / DB 处理
        return False


def normalize_zero_as_null(row: dict[str, Any]) -> dict[str, Any]:
    """把 CMC 返回的占位 0 归一化为 NULL（原地修改并返回）。

    - ``price_usd`` / ``market_cap`` / ``fdv`` / ``volume_24h``
      按各自列精度量化后 == 0 → NULL
    - ``price_usd`` 缺失/为 0 时，市值/成交量字段同样置 NULL
      （没有可信价格时，基于价格的市值/成交量无意义）
    """
    for field, scale in _FIELD_SCALE.items():
        if field in row and _rounds_to_zero(row.get(field), scale):
            row[field] = None

    price = row.get("price_usd")
    price_missing = True
    if price is not None:
        try:
            price_missing = Decimal(str(price)) <= 0
        except (InvalidOperation, ValueError, TypeError):
            price_missing = True

    if price_missing:
        row["price_usd"] = None
        for field in _PRICE_DEPENDENT_FIELDS:
            row[field] = None

    return row


def parse_cmc_quote_snapshot_payload(
    payload: dict[str, Any],
    raw_response_id: int | None = None,
) -> list[dict[str, Any]]:
    """Parse CMC /v1/cryptocurrency/listings/latest response into quote snapshot rows.

    Returns list of dicts with keys matching src_cmc.cmc_asset_quote_snapshot columns.
    """
    data = payload.get("data") or []
    status = payload.get("status") or {}
    # Use server timestamp from response status if available
    quote_time_str = status.get("timestamp")
    if quote_time_str:
        quote_time = datetime.fromisoformat(quote_time_str.replace("Z", "+00:00"))
    else:
        quote_time = datetime.now(timezone.utc)

    rows: list[dict[str, Any]] = []
    for coin in data:
        cmc_id = coin.get("id")
        if cmc_id is None:
            continue

        # v1: quote 为 { "USD": {...} } 对象；v3: quote 为 [ {...} ] 数组（默认计价币在首位）
        quote = coin.get("quote") or {}
        if isinstance(quote, list):
            quote_usd = quote[0] if quote else {}
        else:
            quote_usd = quote.get("USD") or {}

        rows.append(
            normalize_zero_as_null(
                {
                    "cmc_id": cmc_id,
                    "quote_time": quote_time,
                    "price_usd": quote_usd.get("price"),
                    "market_cap": quote_usd.get("market_cap"),
                    "fdv": quote_usd.get("fully_diluted_market_cap"),
                    "volume_24h": quote_usd.get("volume_24h"),
                    "circulating_supply": coin.get("circulating_supply"),
                    "total_supply": coin.get("total_supply"),
                    "max_supply": coin.get("max_supply"),
                    "percent_change_1h": quote_usd.get("percent_change_1h"),
                    "percent_change_24h": quote_usd.get("percent_change_24h"),
                    "percent_change_7d": quote_usd.get("percent_change_7d"),
                    "percent_change_30d": quote_usd.get("percent_change_30d"),
                    "market_cap_dominance": quote_usd.get("market_cap_dominance"),
                    "raw_response_id": raw_response_id,
                }
            )
        )

    return rows
