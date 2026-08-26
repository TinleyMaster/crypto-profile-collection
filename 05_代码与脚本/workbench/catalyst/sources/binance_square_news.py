"""
Binance Square News 催化剂源。

来源：币安广场「Binance News」官方账号帖子
特点：免登录、bodyTextOnly 干净文本、tradingPairsV2 结构化交易对
复用：workbench.kol.scraper.BinanceSquareScraper（8/20 已逆向跑通）

账号信息：
- 展示 handle: Binance_News
- squareUid: 需通过 user/client 接口解析（已在 scraper 中实现）
"""
from __future__ import annotations

import os
import sys
import logging
from datetime import datetime

from ..base import BaseCatalystSource
from ..models import CatalystItem
from . import register_source

logger = logging.getLogger(__name__)

# 复用 KOL scraper
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "kol"))
from scraper import BinanceSquareScraper, ScrapedPost  # noqa: E402


# Binance News 账号配置
# username 是 user/client 接口需要的参数（不是展示 handle）
# 实测：展示 handle 为 Binance_News，对应 username 需探测
# 这里用展示 handle 作为查找键，scraper 内部会自动解析 squareUid
BINANCE_NEWS_USERNAME = "Binance_News"


@register_source
class BinanceSquareNewsSource(BaseCatalystSource):
    """币安广场 Binance News 账号催化剂源。

    source_code: binance_square_news
    """

    source_code = "binance_square_news"

    def __init__(
        self,
        username: str = BINANCE_NEWS_USERNAME,
        max_pages: int = 5,
        request_interval: float = 1.2,
    ):
        """
        Args:
            username: 账号 username（user/client 接口参数）
            max_pages: 每次最多翻页数
            request_interval: 请求间隔秒数
        """
        self.username = username
        self.max_pages = max_pages
        self.request_interval = request_interval
        self._scraper: BinanceSquareScraper | None = None

    @property
    def scraper(self) -> BinanceSquareScraper:
        if self._scraper is None:
            self._scraper = BinanceSquareScraper(
                request_interval=self.request_interval,
            )
        return self._scraper

    def fetch(self, since_ts: float | None = None) -> list[CatalystItem]:
        """抓取 Binance News 账号的帖子。

        Args:
            since_ts: 增量起点（秒级时间戳），None 表示抓最近 max_pages 页

        Returns:
            CatalystItem 列表
        """
        # 解析 squareUid
        square_uid = self.scraper._get_square_uid(self.username)
        if not square_uid:
            logger.error("failed to resolve squareUid for username=%s", self.username)
            return []

        logger.info(
            "fetching binance square news: username=%s uid=%s since=%s",
            self.username, square_uid,
            datetime.fromtimestamp(since_ts) if since_ts else "full",
        )

        # since_post_id: scraper 支持按 post_id 增量，但我们按时间更方便
        # 这里用 max_pages + 时间过滤
        result = self.scraper.fetch_posts(
            platform_user_id=square_uid,
            since_post_id=None,
            max_pages=self.max_pages,
        )

        if result.page_status != "ok":
            logger.warning(
                "binance square fetch status=%s reason=%s",
                result.page_status, result.error_reason,
            )
            return []

        items: list[CatalystItem] = []
        for post in result.posts:
            # 时间过滤
            post_ts = _parse_posted_at(post.posted_at)
            if since_ts and post_ts and post_ts < since_ts:
                continue

            item = _post_to_catalyst(post, post_ts)
            if item:
                items.append(item)

        logger.info("fetched %d catalyst items from binance square news", len(items))
        return items

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._scraper:
            self._scraper.session.close()
            self._scraper = None


def _parse_posted_at(posted_at: str) -> float | None:
    """解析 ISO 8601 时间字符串为秒级时间戳。"""
    if not posted_at:
        return None
    try:
        # 处理带 Z 的 UTC 时间
        if posted_at.endswith("Z"):
            posted_at = posted_at[:-1] + "+00:00"
        dt = datetime.fromisoformat(posted_at)
        return dt.timestamp()
    except Exception:
        return None


def _post_to_catalyst(post: ScrapedPost, post_ts: float | None) -> CatalystItem | None:
    """将 ScrapedPost 转换为 CatalystItem。"""
    if not post.content_text:
        return None

    # Square 帖子没有独立 title，取正文第一行或前 80 字
    title = _extract_title(post.content_text)
    body_text = post.content_text

    # 交易对：优先 trading_pairs（结构化字段），兜底从正文提取
    pairs = list(post.trading_pairs) if post.trading_pairs else []

    return CatalystItem(
        source_code=BinanceSquareNewsSource.source_code,
        source_item_id=post.platform_post_id,
        source_item_code=post.platform_post_id,  # Square 无 code 概念，用 id
        title=title,
        body_text=body_text,
        body_html="",  # Square 无 HTML
        published_at=post_ts or 0.0,
        event_category="Binance News",
        event_subcategory="",
        related_pairs=pairs,
        source_url=post.post_url,
        seo_keywords=[],
        share_count=0,  # Square scraper 暂未提取分享数
        raw_json=post.raw_json,
    )


def _extract_title(text: str, max_len: int = 80) -> str:
    """从正文提取标题（第一行或前 max_len 字）。"""
    if not text:
        return ""
    # 取第一行
    first_line = text.strip().split("\n")[0].strip()
    if first_line:
        # 截断到 max_len
        if len(first_line) > max_len:
            return first_line[:max_len] + "..."
        return first_line
    # 兜底：取前 max_len 字
    return text[:max_len] + ("..." if len(text) > max_len else "")
