"""
Crawl4AI 自托管抓取客户端封装（本地浏览器渲染，无需 API Key）。

用于投研 SOP 的 P2/P5 外部资料抓取：把 URL 渲染成结构化 Markdown，
替代 firecrawl 的 scrape 功能（firecrawl 仍负责搜索发现 URL）。

与 FirecrawlSearchClient 的关系：
    - FirecrawlSearchClient.search(query)  → 发现 URL（保留）
    - Crawl4AIClient.crawl(url)            → URL 渲染为 markdown（本类）

设计：
    - 懒加载 + 进程级单例：AsyncWebCrawler 启动浏览器较慢（~1s），
      且异步事件循环不能跨线程复用，因此单例仅在同一事件循环内复用。
    - 失败快速返回 None：投研链路不因单个 URL 抓取失败阻塞。
    - 可选内存/磁盘缓存，避免同一 URL 重复渲染。

依赖：pip install crawl4ai，并已执行 `python -m patchright install chromium`。
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any
from urllib.parse import urlparse

from crypto_research.config import Settings

try:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
    _CRAWL4AI_OK = True
except Exception as _e:  # pragma: no cover - 依赖缺失时降级
    _CRAWL4AI_OK = False
    _CRAWL4AI_IMPORT_ERR = str(_e)


class Crawl4AIClient:
    """本地 Crawl4AI 抓取客户端。

    用法（同步入口，内部自动管理事件循环）：
        client = Crawl4AIClient(settings)
        md = client.crawl("https://docs.uniswap.org/", max_chars=30000)
        if md:
            print(md[:500])
    """

    def __init__(
        self,
        settings: Settings,
        timeout_seconds: int = 45,
        cache_dir: str | None = None,
        enabled: bool = True,
    ):
        self.settings = settings
        self.timeout_seconds = timeout_seconds
        # 默认缓存到 scripts/data/crawl4ai_cache（可按 URL hash 存 .md）
        # __file__ = scripts/src/crypto_research/clients/crawl4ai_client.py
        _pkg_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        # 到 scripts/src 后再上一级到 scripts，然后 data/
        self.cache_dir = cache_dir or os.path.join(
            os.path.dirname(_pkg_root), "data", "crawl4ai_cache",
        )
        self.enabled = enabled and _CRAWL4AI_OK
        if not _CRAWL4AI_OK:
            self._import_err = _CRAWL4AI_IMPORT_ERR
        self._crawler: Any | None = None  # 当前事件循环内的单例

    def is_available(self) -> bool:
        return self.enabled

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.new_event_loop()

    def crawl(
        self,
        url: str,
        max_chars: int = 30000,
        use_cache: bool = True,
        wait_for: str | None = None,
    ) -> str | None:
        """抓取 URL → 结构化 Markdown。失败返回 None（不抛异常阻塞投研链路）。

        参数：
            url: 目标 URL
            max_chars: 返回内容上限（截断）
            use_cache: 是否启用本地缓存（默认开，同 URL 不重复渲染）
            wait_for: CSS 选择器，等待其出现后再取内容（SPA 场景）
        """
        if not self.is_available():
            return None
        if not url or not url.startswith(("http://", "https://")):
            return None

        # 缓存命中
        cache_file = self._cache_path(url)
        if use_cache and cache_file and os.path.exists(cache_file):
            try:
                with open(cache_file, encoding="utf-8") as f:
                    return f.read()[:max_chars]
            except OSError:
                pass

        try:
            md = self._run_sync(url, wait_for)
        except Exception:
            return None

        if md and use_cache and cache_file:
            try:
                os.makedirs(os.path.dirname(cache_file), exist_ok=True)
                with open(cache_file, "w", encoding="utf-8") as f:
                    f.write(md)
            except OSError:
                pass
        return (md or "")[:max_chars] or None

    def _cache_path(self, url: str) -> str | None:
        if not self.cache_dir:
            return None
        import hashlib

        h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        host = urlparse(url).netloc.replace(".", "_")
        return os.path.join(self.cache_dir, f"{host}_{h}.md")

    def _run_sync(self, url: str, wait_for: str | None) -> str | None:
        """在当前（或新建）事件循环中同步执行抓取。

        每次调用在所属事件循环内新建并关闭 crawler：避免 crawler 与
        上一个 asyncio.run() 的事件循环绑定后无法复用的问题。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        async def _go() -> str | None:
            browser_cfg = BrowserConfig(headless=True)
            async with AsyncWebCrawler(config=browser_cfg) as crawler:
                cfg = CrawlerRunConfig(
                    cache_mode=CacheMode.BYPASS,
                    page_timeout=int(self.timeout_seconds * 1000),
                )
                if wait_for:
                    cfg.wait_for = wait_for
                    cfg.wait_for_timeout = int(self.timeout_seconds * 1000)
                result = await crawler.arun(url=url, config=cfg)
                if not result or not getattr(result, "success", False):
                    return None
                return getattr(result, "markdown", None) or None

        if loop and loop.is_running():
            # 已在运行中的事件循环内（如被 async 代码调用）→ 线程桥接
            future = asyncio.run_coroutine_threadsafe(_go(), loop)
            return future.result(timeout=self.timeout_seconds + 20)
        # 无运行循环（同步调用，最常见）→ asyncio.run 全新建循环
        return asyncio.run(_go())


def get_shared_client(
    settings: Settings, enabled: bool = True
) -> Crawl4AIClient | None:
    """进程级共享客户端（懒加载，避免重复初始化）。

    与 FirecrawlSearchClient.get_shared_client 对齐的调用方式。
    未启用（依赖缺失）时返回 None。
    """
    global _shared
    if not enabled or not _CRAWL4AI_OK:
        return None
    if _shared is None:
        _shared = Crawl4AIClient(settings, enabled=True)
    return _shared


_shared: Crawl4AIClient | None = None


def main() -> int:
    """CLI 验证：抓取单个 URL 打印 markdown 前 N 字符。"""
    import argparse
    import sys

    from crypto_research.config import get_settings

    parser = argparse.ArgumentParser(description="Crawl4AI 抓取验证")
    parser.add_argument("url", help="目标 URL")
    parser.add_argument("--chars", type=int, default=2000)
    args = parser.parse_args()

    settings = get_settings(require_database=False)
    client = get_shared_client(settings)
    if client is None:
        print("[crawl4ai] 未启用（依赖缺失）:", getattr(_crawl4ai_client_mod := sys.modules.get(__name__), "_CRAWL4AI_IMPORT_ERR", "?"))
        return 1

    t0 = time.time()
    md = client.crawl(args.url, max_chars=args.chars, use_cache=False)
    dt = time.time() - t0
    if not md:
        print(f"[crawl4ai] 抓取失败: {args.url}")
        return 1
    print(f"[crawl4ai] OK ({dt:.1f}s, {len(md)} chars) {args.url}")
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
