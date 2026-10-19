"""
Bybit 上新公告催化剂源。

来源：`api.bybit.com/v5/announcements/index`（公开接口，免认证、免 key）
    参数 `type=new_crypto`（新品上架）/ `delistings`（下架）。

实测结构（2026-10-16）：
    result.list[] = {title, description, type{key,title}, tags[],
                     url, dateTimestamp, startDateTimestamp,
                     endDateTimestamp, publishTime}

发布时间取 `publishTime`（毫秒）；无条目级 ID → 用 url 尾段。
"""
from __future__ import annotations

import logging

from ..models import CatalystItem
from . import register_source
from ._exchange_announce import ExchangeAnnouncementSource, build_item

logger = logging.getLogger(__name__)


@register_source
class BybitAnnouncementSource(ExchangeAnnouncementSource):
    """Bybit 上新公告（一手、分钟级）。"""

    source_code = "bybit_announcement"
    _endpoint = "https://api.bybit.com/v5/announcements/index"

    def _build_params(self, page: int) -> dict:
        return {"locale": "en-US", "limit": 50, "type": "new_crypto", "page": page}

    def _parse(self, payload: dict) -> list[CatalystItem]:
        items: list[CatalystItem] = []
        rows = (payload.get("result") or {}).get("list") or []
        for row in rows:
            url = row.get("url") or ""
            slug = url.rstrip("/").split("/")[-1] if url else ""
            title = row.get("title") or ""
            if not slug or not title:
                continue
            type_info = row.get("type") or {}
            item = build_item(
                source_code=self.source_code,
                item_id=slug,
                title=title,
                published_ms=row.get("publishTime") or row.get("dateTimestamp"),
                source_url=url,
                event_subcategory=type_info.get("title") or type_info.get("key") or "",
                raw=row,
                sink=self.rejected,
            )
            if item:
                items.append(item)
        return items


@register_source
class BybitDelistingSource(BybitAnnouncementSource):
    """Bybit 下架公告（利空向）。"""

    source_code = "bybit_delisting"

    def _build_params(self, page: int) -> dict:
        return {"locale": "en-US", "limit": 50, "type": "delistings", "page": page}
