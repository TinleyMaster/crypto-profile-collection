"""
Firecrawl Search 客户端封装。

用于 AI 信号分析时补全数据库中缺失的数据维度（估值、链上、社交热度等）。
只做搜索 + 结果摘要，不做深度爬取，控制成本和耗时。

用法：
    client = FirecrawlSearchClient(settings)
    results = client.search("Hyperliquid HYPE MVRV ratio 2024", limit=3)
    for r in results:
        print(r["title"], r["url"], r["description"])
"""
from __future__ import annotations

import time
from typing import Any

import requests

from crypto_research.config import Settings


class FirecrawlSearchClient:
    """Firecrawl Search API 轻量封装。"""

    def __init__(self, settings: Settings, timeout: int = 30):
        self.api_key = settings.firecrawl_api_key
        self.base_url = settings.firecrawl_base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        # 简易限流：默认 Firecrawl 免费档 100 req/min，这里保守 30 req/min
        self._min_interval = 2.0  # 秒
        self._last_call = 0.0

    def is_available(self) -> bool:
        return bool(self.api_key)

    def _wait_rate_limit(self) -> None:
        elapsed = time.time() - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.time()

    def search(
        self,
        query: str,
        limit: int = 5,
        lang: str | None = None,
        country: str | None = None,
        scrape: bool = False,
    ) -> list[dict[str, Any]]:
        """
        执行 Web 搜索，返回结构化结果列表。

        每个结果包含：
            - title: str
            - url: str
            - description: str
            - markdown: str（仅当 scrape=True 时有内容）
            - published_date: str | None
        """
        if not self.is_available():
            raise RuntimeError("Firecrawl API Key 未配置")

        self._wait_rate_limit()

        url = f"{self.base_url}/v1/search"
        payload: dict[str, Any] = {
            "query": query,
            "limit": limit,
            "scrapeOptions": {"formats": ["markdown"]} if scrape else None,
        }
        if lang:
            payload["lang"] = lang
        if country:
            payload["country"] = country
        # 去掉 None 字段
        payload = {k: v for k, v in payload.items() if v is not None}

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        resp = self._session.post(url, json=payload, headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()

        # Firecrawl v1 search 返回结构：{ success: true, data: [...] }
        results = data.get("data", []) or []

        # 统一字段名，方便上层使用
        normalized = []
        for r in results:
            item = {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "description": r.get("description", ""),
                "published_date": r.get("published_date"),
                "markdown": r.get("markdown", ""),
            }
            normalized.append(item)

        return normalized

    def search_summary(self, query: str, limit: int = 3, max_chars: int = 1500) -> str:
        """
        搜索并返回一个紧凑的文本摘要（带来源标注），可直接拼进 prompt。

        返回格式：
            标题1 - 摘要1 [来源: url1]
            标题2 - 摘要2 [来源: url2]
            ...
        """
        results = self.search(query, limit=limit)
        if not results:
            return ""

        lines = []
        total = 0
        for r in results:
            title = r["title"] or ""
            desc = r["description"] or ""
            url = r["url"] or ""
            line = f"- {title}: {desc} [来源: {url}]"
            if total + len(line) > max_chars:
                # 截断最后一条
                remain = max_chars - total
                if remain > 50:
                    lines.append(line[:remain] + "... [来源: " + url + "]")
                break
            lines.append(line)
            total += len(line)

        return "\n".join(lines)
