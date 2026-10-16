"""
OKX 上新公告催化剂源。

来源：`okx.com/api/v5/support/announcements`（公开接口，免认证、免 key）
栏目：`annType=announcements-new-listings`（上新）；另有 `announcements-delistings`（下架）。

实测结构（2026-10-16）：
    data[].details[] = {annType, title, url, pTime(ms 字符串), businessPTime(ms)}
    data[].totalPage = 52

无条目级唯一 ID → 用公告 url 尾段（slug）作 source_item_id。
"""
from __future__ import annotations

import logging

from ..models import CatalystItem
from . import register_source
from ._exchange_announce import ExchangeAnnouncementSource, build_item

logger = logging.getLogger(__name__)


@register_source
class OKXAnnouncementSource(ExchangeAnnouncementSource):
    """OKX 上新公告（一手、分钟级、标题自带币名）。"""

    source_code = "okx_announcement"
    _endpoint = "https://www.okx.com/api/v5/support/announcements"

    def _build_params(self, page: int) -> dict:
        # OKX 不支持 language 参数时默认英文；显式带 page 翻页
        return {"annType": "announcements-new-listings", "page": page}

    def _parse(self, payload: dict) -> list[CatalystItem]:
        items: list[CatalystItem] = []
        for block in payload.get("data") or []:
            for det in block.get("details") or []:
                url = det.get("url") or ""
                slug = url.rstrip("/").split("/")[-1] if url else ""
                title = det.get("title") or ""
                if not slug or not title:
                    continue
                item = build_item(
                    source_code=self.source_code,
                    item_id=slug,
                    title=title,
                    published_ms=det.get("pTime") or det.get("businessPTime"),
                    source_url=url,
                    event_subcategory=det.get("annType") or "new-listing",
                    raw=det,
                )
                if item:
                    items.append(item)
        return items


@register_source
class OKXDelistingSource(OKXAnnouncementSource):
    """OKX 下架公告（利空向；与上新分开 source_code 便于分别取证）。"""

    source_code = "okx_delisting"

    def _build_params(self, page: int) -> dict:
        return {"annType": "announcements-delistings", "page": page}
