"""
币安 CMS 新闻/公告抓取器
来源：币安 bapi/composite/v1/public/cms/* 公开接口（免认证）
栏目：catalogId=48（上新）、49（综合新闻）等
详情：含 body / pairs / publishDate 等，pairs 可直接关联交易对
"""
from __future__ import annotations

import time
import logging
from dataclasses import dataclass, field
from typing import Any

import requests

logger = logging.getLogger(__name__)


class RateLimitedError(Exception):
    """Binance API 限流（HTTP 429）信号，用于快速短路整个源抓取。"""


LIST_URL = "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
DETAIL_URL = "https://www.binance.com/bapi/composite/v1/public/cms/article/detail/query"

# 已知栏目（catalogId -> 名称/来源编码）
KNOWN_CATALOGS = {
    48: {"name": "New Cryptocurrency Listing", "source_code": "binance_listing"},
    49: {"name": "Latest Binance News", "source_code": "binance_news"},
    51: {"name": "API Updates", "source_code": "binance_api"},
}

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.binance.com",
    "Referer": "https://www.binance.com/en/support/announcement",
}


@dataclass
class CatalystArticle:
    """一篇催化剂文章（结构化后的数据）"""
    source_code: str
    source_article_id: str
    source_article_code: str
    title: str
    body_html: str = ""
    body_text: str = ""
    published_at: float = 0.0          # 秒级时间戳
    event_category: str = ""
    event_subcategory: str = ""
    related_pairs: list[str] = field(default_factory=list)
    source_url: str = ""
    seo_keywords: list[str] = field(default_factory=list)
    share_count: int = 0
    raw_json: dict | None = None


class BinanceNewsScraper:
    """币安 CMS 新闻抓取器（免认证公开接口）"""

    def __init__(self, request_interval: float = 1.2, timeout: int = 15):
        """
        Args:
            request_interval: 每次请求间隔秒数（防限流）
            timeout: HTTP 超时秒数
        """
        self.request_interval = request_interval
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self._last_request_ts = 0.0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.session.close()

    def _throttle(self):
        """请求节流"""
        elapsed = time.time() - self._last_request_ts
        if elapsed < self.request_interval:
            time.sleep(self.request_interval - elapsed)
        self._last_request_ts = time.time()

    def _get(self, url: str, params: dict) -> dict | None:
        """统一 GET 请求封装

        Returns:
            dict 或 None；429 限流抛 RateLimitedError 由上层短路
        """
        self._throttle()
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
            if resp.status_code == 429:
                logger.warning("GET %s status=429 (rate limited)", url)
                raise RateLimitedError(url)
            if resp.status_code != 200:
                logger.warning("GET %s status=%s", url, resp.status_code)
                return None
            data = resp.json()
            if data.get("code") != "000000":
                logger.warning("GET %s code=%s msg=%s", url, data.get("code"), data.get("message"))
                return None
            return data.get("data")
        except RateLimitedError:
            raise
        except Exception as e:
            logger.error("GET %s error: %s", url, e)
            return None

    def fetch_catalog_page(
        self,
        catalog_id: int,
        page_no: int = 1,
        page_size: int = 20,
    ) -> list[dict]:
        """抓取某栏目一页文章列表

        Returns:
            文章列表（每项含 id/code/title/releaseDate 等）
        """
        params = {
            "type": 1,
            "catalogId": catalog_id,
            "pageNo": page_no,
            "pageSize": page_size,
        }
        data = self._get(LIST_URL, params)
        if not data:
            return []
        catalogs = data.get("catalogs") or []
        if not catalogs:
            return []
        return catalogs[0].get("articles") or []

    def fetch_catalog(
        self,
        catalog_id: int,
        max_pages: int = 5,
        since_release_date: float | None = None,
    ) -> list[dict]:
        """抓取某栏目多页文章列表，遇到早于 since_release_date 的停止

        Args:
            catalog_id: 栏目 ID
            max_pages: 最大翻页数
            since_release_date: 截止时间（秒级时间戳），早于此时间的文章跳过且停止翻页

        Returns:
            文章列表（按发布时间倒序）
        """
        all_articles = []
        for page in range(1, max_pages + 1):
            articles = self.fetch_catalog_page(catalog_id, page_no=page)
            if not articles:
                break

            stopped = False
            for art in articles:
                release_ts = (art.get("releaseDate") or 0) / 1000.0
                if since_release_date and release_ts < since_release_date:
                    stopped = True
                    break
                all_articles.append(art)

            if stopped:
                break

            # 不足一页说明到末尾了
            if len(articles) < 20:
                break

        logger.info(
            "catalog %s: fetched %d articles (pages=%d)",
            catalog_id, len(all_articles), min(page, max_pages),
        )
        return all_articles

    def fetch_detail(self, article_code: str) -> dict | None:
        """抓取文章详情（含 body / pairs / publishDate）

        Args:
            article_code: 文章 code（不是 id！）

        Returns:
            详情 dict，失败返回 None
        """
        params = {"articleCode": article_code}
        data = self._get(DETAIL_URL, params)
        return data

    def parse_detail(self, detail: dict, source_code: str) -> CatalystArticle:
        """将详情 API 返回解析为 CatalystArticle"""
        article_id = str(detail.get("id") or "")
        code = detail.get("code") or ""
        title = detail.get("title") or ""
        body_html = detail.get("body") or ""
        publish_ts = (detail.get("publishDate") or 0) / 1000.0

        # 清洗 HTML 为纯文本（简单去标签，后续 AI 处理用 body_html 更准）
        body_text = _strip_html(body_html)

        pairs = detail.get("pairs") or []
        if isinstance(pairs, str):
            pairs = [pairs]
        pairs = [p for p in pairs if isinstance(p, str) and p]

        # 兜底：API 的 pairs 字段经常为空，从标题+正文提取交易对
        if not pairs:
            extracted = _extract_pairs_from_text(title + " " + body_text)
            pairs = extracted

        first_cat = detail.get("firstCatalogName") or ""
        second_cat = detail.get("secondCatalogName") or ""

        seo_keywords = detail.get("seoKeywords") or []
        if isinstance(seo_keywords, str):
            seo_keywords = [k.strip() for k in seo_keywords.split(",") if k.strip()]

        share_count = detail.get("shareCount") or 0

        # 构造原文链接
        source_url = f"https://www.binance.com/en/support/announcement/{code}" if code else ""

        return CatalystArticle(
            source_code=source_code,
            source_article_id=article_id,
            source_article_code=code,
            title=title,
            body_html=body_html,
            body_text=body_text,
            published_at=publish_ts,
            event_category=first_cat,
            event_subcategory=second_cat,
            related_pairs=pairs,
            source_url=source_url,
            seo_keywords=seo_keywords,
            share_count=int(share_count) if share_count else 0,
            raw_json=detail,
        )

    def fetch_and_parse(
        self,
        catalog_id: int,
        max_pages: int = 5,
        since_release_date: float | None = None,
    ) -> list[CatalystArticle]:
        """抓取栏目 + 逐条拉详情，返回结构化文章列表

        增量策略：since_release_date 之前的文章不抓详情
        """
        source_code = KNOWN_CATALOGS.get(catalog_id, {}).get(
            "source_code", f"binance_catalog_{catalog_id}"
        )

        articles = self.fetch_catalog(catalog_id, max_pages, since_release_date)
        results = []
        for art in articles:
            code = art.get("code")
            if not code:
                continue
            detail = self.fetch_detail(code)
            if not detail:
                continue
            results.append(self.parse_detail(detail, source_code))

        logger.info(
            "catalog %s: parsed %d articles with details",
            catalog_id, len(results),
        )
        return results


def _strip_html(html: str) -> str:
    """简单 HTML 去标签，保留纯文本"""
    if not html:
        return ""
    import re
    # 去 script/style
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # 去标签
    text = re.sub(r"<[^>]+>", " ", text)
    # 压缩空白
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_pairs_from_text(text: str) -> list[str]:
    """从文本中提取 USDT 交易对（兜底策略）

    匹配规则：大写字母+数字组成的币种名 + USDT，如 BTCUSDT、ETHUSDT
    限制：币种名 2-20 字符，避免误匹配长单词
    """
    if not text:
        return []
    import re
    # 匹配 XXXXUSDT，其中 X 是大写字母或数字，长度 2-20
    pattern = r'\b([A-Z0-9]{2,20})USDT\b'
    matches = re.findall(pattern, text)
    # 去重并保持顺序，过滤掉明显不是币种的（纯数字、太短等）
    seen = set()
    result = []
    for base in matches:
        pair = base + "USDT"
        if pair in seen:
            continue
        # 过滤：不能全是数字，至少有一个字母
        if not any(c.isalpha() for c in base):
            continue
        # 过滤常见误匹配
        if base in ("USD", "USDC", "BUSD", "TUSD", "USDP"):
            continue
        seen.add(pair)
        result.append(pair)
    return result
