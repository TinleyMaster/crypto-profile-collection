"""
Bitget 上新公告催化剂源。

来源：`api.bitget.com/api/v2/public/annoucements`（公开接口，免认证、免 key）
    注意端点拼写就是 `annoucements`（Bitget 官方拼写，非笔误）。

实测结构（2026-10-16）：
    data[] = {annId, annDesc, annTitle, annType, annSubType(spot|futures),
              language, annUrl, cTime(ms 字符串)}

有 `annId` 作唯一 ID。
"""
from __future__ import annotations

import logging

from ..models import CatalystItem
from . import register_source
from ._exchange_announce import ExchangeAnnouncementSource, build_item

logger = logging.getLogger(__name__)

# `coin_listings` 栏目实测混入与上新无关的子类（如
# 「Bitget futures copy trading now supports 15 perpetual futures」= annSubType copy_trading）。
# 按**源字段**黑名单过滤，比猜标题更准；黑名单保持最小，避免误杀新子类。
_SUBTYPE_BLOCKLIST = frozenset({"copy_trading", "trading_bots", "api_maintenance"})


@register_source
class BitgetAnnouncementSource(ExchangeAnnouncementSource):
    """Bitget 上币公告（一手、分钟级；annSubType 区分现货/合约）。"""

    source_code = "bitget_announcement"
    _endpoint = "https://api.bitget.com/api/v2/public/annoucements"

    def _build_params(self, page: int) -> dict:
        # Bitget 该端点无分页，返回近 N 条
        return {"language": "en_US", "annType": "coin_listings"}

    def _parse(self, payload: dict) -> list[CatalystItem]:
        items: list[CatalystItem] = []
        for row in payload.get("data") or []:
            ann_id = row.get("annId")
            title = row.get("annTitle") or ""
            sub_type = (row.get("annSubType") or "").strip().lower()
            if not ann_id or not title:
                continue
            if sub_type in _SUBTYPE_BLOCKLIST:
                logger.debug("bitget: 跳过非上新子类 %s: %s", sub_type, title[:60])
                continue
            item = build_item(
                source_code=self.source_code,
                item_id=ann_id,
                title=title,
                published_ms=row.get("cTime"),
                source_url=row.get("annUrl") or "",
                event_subcategory=row.get("annSubType") or row.get("annType") or "",
                raw=row,
            )
            if item:
                items.append(item)
        return items
