"""
KOL 帖子抓取器。

支持的平台：
  - binance_square  币安广场（Playwright 无头浏览器，调用内部 bapi 接口）

架构：每个平台一个 Scraper 类，统一接口 fetch_posts(profile, since_post_id)。
新增平台时只需新增 Scraper 类，其他逻辑（AI 分类、邮件、存档）全部复用。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Iterator

from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext


@dataclass
class ScrapedPost:
    """抓取到的原始帖子数据（平台无关结构）。"""
    platform_post_id: str
    content_text: str
    image_urls: list[str] = field(default_factory=list)
    post_url: str = ""
    posted_at: str = ""  # ISO 8601 字符串
    raw_json: dict = field(default_factory=dict)


class BaseScraper:
    """抓取器基类。子类实现 fetch_posts。"""

    platform_code: str = ""

    def fetch_posts(
        self,
        platform_user_id: str,
        since_post_id: str | None = None,
        max_pages: int = 3,
    ) -> list[ScrapedPost]:
        """
        抓取指定博主的帖子。

        Args:
            platform_user_id: 平台内用户 ID
            since_post_id: 增量游标，只返回比此 ID 更新的帖子
            max_pages: 最大翻页数

        Returns:
            按时间从新到旧排序的帖子列表
        """
        raise NotImplementedError


# ============================================================
# 币安广场抓取器
# ============================================================

class BinanceSquareScraper(BaseScraper):
    """
    币安广场帖子抓取器。

    使用 Playwright 无头浏览器访问博主主页，
    拦截 /bapi/composite/* 接口获取帖子列表 JSON。

    币安广场博主主页 URL 格式：
      https://www.binance.com/zh-CN/square/profile/{user_id}
    """

    platform_code = "binance_square"

    BASE_URL = "https://www.binance.com/zh-CN/square/profile"

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    def __enter__(self):
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=self.headless,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        self._context = self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
        )
        # 拦截静态资源加速
        self._context.route(
            "**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,eot,css,mp4,webm}",
            lambda route: route.abort(),
        )
        self._page = self._context.new_page()
        return self

    def __exit__(self, *args):
        try:
            if self._context:
                self._context.close()
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass

    def fetch_posts(
        self,
        platform_user_id: str,
        since_post_id: str | None = None,
        max_pages: int = 3,
    ) -> list[ScrapedPost]:
        """
        抓取币安广场博主的帖子列表。

        策略：
          1. 打开博主主页，等待帖子列表接口响应
          2. 解析接口返回的 JSON
          3. 如果需要翻页，模拟滚动加载更多
        """
        if not self._page:
            raise RuntimeError("Scraper not initialized. Use 'with' statement.")

        url = f"{self.BASE_URL}/{platform_user_id}"
        posts: list[ScrapedPost] = []
        seen_ids: set[str] = set()

        # 收集 API 响应
        api_responses: list[dict] = []

        def handle_response(response):
            url = response.url
            # 币安广场帖子列表接口（bapi composite）
            if "/bapi/composite/" in url and "feed" in url.lower():
                try:
                    data = response.json()
                    api_responses.append(data)
                except Exception:
                    pass
            # 另一种可能的接口路径
            if "/bapi/web/" in url and "post" in url.lower():
                try:
                    data = response.json()
                    api_responses.append(data)
                except Exception:
                    pass

        self._page.on("response", handle_response)

        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            try:
                self._page.goto(url, wait_until="commit", timeout=30000)
            except Exception as e:
                print(f"[KOL][binance_square] 页面加载失败 {platform_user_id}: {e}")
                return []

        # 等待帖子渲染
        time.sleep(2)

        # 尝试从页面中提取帖子数据（兜底方案）
        if not api_responses:
            # 从页面 HTML 中提取 __NEXT_DATA__ 或 window 数据
            page_content = self._page.content()
            extracted = self._extract_from_html(page_content)
            for item in extracted:
                post = self._parse_post_item(item)
                if post and post.platform_post_id not in seen_ids:
                    seen_ids.add(post.platform_post_id)
                    posts.append(post)

        # 从 API 响应中解析
        for resp in api_responses:
            items = self._extract_posts_from_api(resp)
            for item in items:
                post = self._parse_post_item(item)
                if post and post.platform_post_id not in seen_ids:
                    seen_ids.add(post.platform_post_id)
                    posts.append(post)

        # 如果第一页没抓到足够数据，尝试滚动加载更多
        page_count = 1
        while page_count < max_pages and len(posts) < 20:
            # 滚动到底部触发加载
            self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(2)

            before_count = len(posts)
            for resp in api_responses:
                items = self._extract_posts_from_api(resp)
                for item in items:
                    post = self._parse_post_item(item)
                    if post and post.platform_post_id not in seen_ids:
                        seen_ids.add(post.platform_post_id)
                        posts.append(post)

            if len(posts) == before_count:
                break  # 没有新内容
            page_count += 1

        # 按时间倒序
        posts.sort(key=lambda p: p.posted_at, reverse=True)

        # 增量过滤：只返回比 since_post_id 更新的
        if since_post_id and posts:
            idx = None
            for i, p in enumerate(posts):
                if p.platform_post_id == since_post_id:
                    idx = i
                    break
            if idx is not None:
                posts = posts[:idx]

        return posts

    def _extract_posts_from_api(self, resp: dict) -> list[dict]:
        """从 API 响应中递归提取帖子列表。"""
        results: list[dict] = []

        def walk(obj):
            if isinstance(obj, dict):
                # 常见的帖子列表字段
                for key in ("posts", "feedList", "list", "items", "data"):
                    if key in obj and isinstance(obj[key], list):
                        for item in obj[key]:
                            if isinstance(item, dict):
                                # 判断是否是帖子对象（有 id + content 类字段）
                                if any(k in item for k in ("id", "postId", "post_id")):
                                    results.append(item)
                                else:
                                    walk(item)
                for v in obj.values():
                    walk(v)
            elif isinstance(obj, list):
                for item in obj:
                    walk(item)

        walk(resp)
        return results

    def _extract_from_html(self, html: str) -> list[dict]:
        """从 HTML 中提取 __NEXT_DATA__ 等内嵌 JSON。"""
        results: list[dict] = []

        # 匹配 <script id="__NEXT_DATA__" ...>...</script>
        m = re.search(
            r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            html, re.DOTALL,
        )
        if m:
            try:
                data = json.loads(m.group(1))
                results.extend(self._extract_posts_from_api(data))
            except Exception:
                pass

        # 匹配 window.__INITIAL_STATE__ 等
        for pattern in [
            r'window\.__INITIAL_STATE__\s*=\s*({.*?});',
            r'window\.__APP_DATA__\s*=\s*({.*?});',
        ]:
            m = re.search(pattern, html, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group(1))
                    results.extend(self._extract_posts_from_api(data))
                except Exception:
                    pass

        return results

    def _parse_post_item(self, item: dict) -> ScrapedPost | None:
        """将 API 返回的单条帖子解析为 ScrapedPost。"""
        # 尝试多种字段名
        post_id = (
            item.get("id")
            or item.get("postId")
            or item.get("post_id")
            or item.get("postCode")
        )
        if not post_id:
            return None
        post_id = str(post_id)

        # 正文内容
        content = (
            item.get("content")
            or item.get("text")
            or item.get("body")
            or item.get("description")
            or item.get("title", "")
        )
        if isinstance(content, dict):
            content = content.get("text", "") or str(content)
        content = str(content or "").strip()

        # 图片
        images: list[str] = []
        for key in ("images", "imageList", "image_urls", "medias", "mediaList"):
            val = item.get(key)
            if isinstance(val, list):
                for img in val:
                    if isinstance(img, str):
                        images.append(img)
                    elif isinstance(img, dict):
                        url = (
                            img.get("url")
                            or img.get("imageUrl")
                            or img.get("src")
                        )
                        if url:
                            images.append(url)

        # 发帖时间
        posted_at = ""
        for key in ("createTime", "createdAt", "postTime", "publishTime", "time", "date"):
            val = item.get(key)
            if val:
                if isinstance(val, (int, float)):
                    # 可能是毫秒时间戳
                    if val > 1e12:
                        val = val / 1000
                    from datetime import datetime, timezone
                    posted_at = datetime.fromtimestamp(val, tz=timezone.utc).isoformat()
                else:
                    posted_at = str(val)
                break

        # 帖子 URL
        post_url = ""
        url = item.get("url") or item.get("postUrl") or item.get("shareUrl")
        if url:
            post_url = url if url.startswith("http") else f"https://www.binance.com{url}"
        else:
            post_url = f"https://www.binance.com/zh-CN/square/post/{post_id}"

        return ScrapedPost(
            platform_post_id=post_id,
            content_text=content,
            image_urls=images,
            post_url=post_url,
            posted_at=posted_at,
            raw_json=item,
        )


# ============================================================
# 抓取器工厂
# ============================================================

_SCRAPER_REGISTRY: dict[str, type[BaseScraper]] = {
    "binance_square": BinanceSquareScraper,
}


def get_scraper_class(platform_code: str) -> type[BaseScraper] | None:
    """根据平台编码获取抓取器类。"""
    return _SCRAPER_REGISTRY.get(platform_code)


def register_scraper(platform_code: str, scraper_cls: type[BaseScraper]) -> None:
    """注册新平台抓取器（扩展用）。"""
    _SCRAPER_REGISTRY[platform_code] = scraper_cls
