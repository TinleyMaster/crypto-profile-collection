"""
Binance CMS 新闻催化剂源（改造版，接入新框架）。

来源：币安 bapi/composite/v1/public/cms/* 公开接口（免认证）
栏目：catalogId=48（上新）、49（综合新闻）等

修复：
- P1-A: body_text 不再被 JSON block 污染，改用 contentJson 提取纯文本
- P1-B: 支持多 pairs → 多资产关联（走 pipeline 的多资产链路）
"""
from __future__ import annotations

import logging
import re

from ..base import BaseCatalystSource
from ..models import CatalystItem
from . import register_source

logger = logging.getLogger(__name__)

# 复用已有 scraper
from ..binance_news import BinanceNewsScraper, KNOWN_CATALOGS, RateLimitedError  # noqa: E402


class BinanceCMSNewsSource(BaseCatalystSource):
    """币安 CMS 公告催化剂源。

    source_code: binance_news（catalog 49）/ binance_listing（catalog 48）等
    一个实例对应一个 catalog。
    """

    source_code = "binance_news"  # 默认，可通过 catalog_id 覆盖

    def __init__(
        self,
        catalog_id: int = 49,
        max_pages: int = 5,
        request_interval: float = 1.2,
    ):
        """
        Args:
            catalog_id: 栏目 ID（48=上新, 49=综合新闻, 51=API更新）
            max_pages: 每次最多翻页数
            request_interval: 请求间隔秒数
        """
        self.catalog_id = catalog_id
        self.max_pages = max_pages
        self.request_interval = request_interval

        # 根据 catalog 确定 source_code
        catalog_info = KNOWN_CATALOGS.get(catalog_id, {})
        self.source_code = catalog_info.get("source_code", f"binance_catalog_{catalog_id}")
        self.event_category = catalog_info.get("name", f"Catalog {catalog_id}")

        self._scraper: BinanceNewsScraper | None = None

    @property
    def scraper(self) -> BinanceNewsScraper:
        if self._scraper is None:
            self._scraper = BinanceNewsScraper(
                request_interval=self.request_interval,
            )
        return self._scraper

    def fetch(self, since_ts: float | None = None) -> list[CatalystItem]:
        """抓取 CMS 栏目文章。

        Args:
            since_ts: 增量起点（秒级时间戳）

        Returns:
            CatalystItem 列表
        """
        logger.info(
            "fetching binance CMS catalog=%s source=%s since=%s",
            self.catalog_id, self.source_code,
            since_ts,
        )

        try:
            articles = self.scraper.fetch_catalog(
                catalog_id=self.catalog_id,
                max_pages=self.max_pages,
                since_release_date=since_ts,
            )
        except RateLimitedError:
            logger.warning("binance CMS rate limited (429) at catalog fetch, source skipped")
            return []

        items: list[CatalystItem] = []
        for art in articles:
            code = art.get("code")
            if not code:
                continue
            try:
                detail = self.scraper.fetch_detail(code)
            except RateLimitedError:
                logger.warning(
                    "binance CMS rate limited (429), stopping detail fetch early (fetched %d items)",
                    len(items),
                )
                break
            if not detail:
                continue

            item = _detail_to_catalyst(detail, self.source_code, self.event_category)
            if item:
                items.append(item)

        logger.info(
            "catalog %s: fetched %d catalyst items",
            self.catalog_id, len(items),
        )
        return items

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._scraper:
            self._scraper.session.close()
            self._scraper = None


def _detail_to_catalyst(
    detail: dict,
    source_code: str,
    event_category: str,
) -> CatalystItem | None:
    """将 CMS 详情转换为 CatalystItem（修复 P1-A + P1-B）。"""
    article_id = str(detail.get("id") or "")
    code = detail.get("code") or ""
    title = detail.get("title") or ""
    body_html = detail.get("body") or ""
    publish_ts = (detail.get("publishDate") or 0) / 1000.0

    # P1-A 修复：优先用 contentJson 提取纯文本，比 HTML 去标签更干净
    body_text = _extract_text_from_content_json(detail.get("contentJson"))
    if not body_text:
        # 兜底：HTML 去标签
        body_text = _strip_html(body_html)

    # P1-B 修复：完整 pairs 列表（多交易对 → 多资产）
    pairs = detail.get("pairs") or []
    if isinstance(pairs, str):
        pairs = [pairs]
    pairs = [str(p).upper() for p in pairs if isinstance(p, str) and p]

    # 兜底：从标题+正文提取交易对
    if not pairs:
        pairs = _extract_pairs_from_text(title + " " + body_text)

    first_cat = detail.get("firstCatalogName") or event_category
    second_cat = detail.get("secondCatalogName") or ""

    seo_keywords = detail.get("seoKeywords") or []
    if isinstance(seo_keywords, str):
        seo_keywords = [k.strip() for k in seo_keywords.split(",") if k.strip()]

    share_count = detail.get("shareCount") or 0

    source_url = f"https://www.binance.com/en/support/announcement/{code}" if code else ""

    return CatalystItem(
        source_code=source_code,
        source_item_id=article_id,
        source_item_code=code,
        title=title,
        body_text=body_text,
        body_html=body_html,
        published_at=publish_ts,
        event_category=first_cat,
        event_subcategory=second_cat,
        related_pairs=pairs,
        source_url=source_url,
        seo_keywords=seo_keywords,
        share_count=int(share_count) if share_count else 0,
        raw_json=detail,
    )


def _extract_text_from_content_json(content_json) -> str:
    """从 CMS 的 contentJson（block tree）提取纯文本。

    contentJson 是一个 JSON 字符串或 dict，结构类似：
    {
      "blocks": [
        {"type": "paragraph", "data": {"text": "..."}},
        {"type": "header", "data": {"text": "..."}},
        ...
      ]
    }
    """
    if not content_json:
        return ""

    import json
    if isinstance(content_json, str):
        try:
            content_json = json.loads(content_json)
        except Exception:
            return ""

    if not isinstance(content_json, dict):
        return ""

    blocks = content_json.get("blocks") or []
    texts: list[str] = []

    for block in blocks:
        if not isinstance(block, dict):
            continue
        data = block.get("data") or {}
        if not isinstance(data, dict):
            continue

        # 常见 block 类型的文本字段
        text = data.get("text") or data.get("caption") or data.get("content")
        if text and isinstance(text, str):
            texts.append(text)

        # 列表类 block
        items = data.get("items")
        if items and isinstance(items, list):
            for item in items:
                if isinstance(item, str):
                    texts.append(f"• {item}")

    return "\n".join(texts)


def _strip_html(html: str) -> str:
    """简单 HTML 去标签（兜底用）。"""
    if not html:
        return ""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_pairs_from_text(text: str) -> list[str]:
    """从文本中提取 USDT 交易对（兜底策略）。"""
    if not text:
        return []
    pattern = r'\b([A-Z0-9]{2,20})USDT\b'
    matches = re.findall(pattern, text)
    seen = set()
    result = []
    for base in matches:
        pair = base + "USDT"
        if pair in seen:
            continue
        if not any(c.isalpha() for c in base):
            continue
        if base in ("USD", "USDC", "BUSD", "TUSD", "USDP", "FDUSD"):
            continue
        seen.add(pair)
        result.append(pair)
    return result


# ── 各 catalog 子类注册（每个 catalog 对应一个独立 source_code）──

@register_source
class BinanceListingSource(BinanceCMSNewsSource):
    """币安上新公告（catalog 48）。"""
    source_code = "binance_listing"

    def __init__(self, **kwargs):
        kwargs.setdefault("catalog_id", 48)
        super().__init__(**kwargs)


@register_source
class BinanceNewsSource(BinanceCMSNewsSource):
    """币安综合新闻（catalog 49）。"""
    source_code = "binance_news"

    def __init__(self, **kwargs):
        kwargs.setdefault("catalog_id", 49)
        super().__init__(**kwargs)


@register_source
class BinanceAPISource(BinanceCMSNewsSource):
    """币安 API 更新（catalog 51）。"""
    source_code = "binance_api"

    def __init__(self, **kwargs):
        kwargs.setdefault("catalog_id", 51)
        super().__init__(**kwargs)
