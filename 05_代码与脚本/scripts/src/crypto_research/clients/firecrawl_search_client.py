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

# 进程级共享客户端缓存（按 api_key 缓存，避免每次新建实例绕过限流）
_shared_clients: dict[str, "FirecrawlSearchClient"] = {}


def get_shared_client(settings: Settings, timeout: int = 30) -> FirecrawlSearchClient | None:
    """获取进程级共享的 Firecrawl 客户端实例（按 api_key 缓存）。

    未配置 API Key 时返回 None。
    使用共享实例可确保多调用点之间共享限流计数器，避免绕过限流导致 429。
    """
    api_key = settings.firecrawl_api_key
    if not api_key:
        return None
    key = f"{api_key}:{settings.firecrawl_base_url}:{timeout}"
    if key not in _shared_clients:
        _shared_clients[key] = FirecrawlSearchClient(settings, timeout=timeout)
    return _shared_clients[key]


class FirecrawlSearchClient:
    """Firecrawl Search API 轻量封装。"""

    def __init__(self, settings: Settings, timeout: int = 30):
        self.api_key = settings.firecrawl_api_key
        self.base_url = settings.firecrawl_base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        # 简易限流：默认 Firecrawl 免费档 100 req/min，这里保守 30 req/min（2s 间隔）
        self._min_interval = 2.0  # 秒
        self._last_call = 0.0
        # 429 退避参数
        self._max_retries = 3
        self._base_backoff = 5.0  # 首次退避秒数，后续指数增长

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

        遇到 429 时自动指数退避重试（最多 _max_retries 次）。
        """
        if not self.is_available():
            raise RuntimeError("Firecrawl API Key 未配置")

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

        # 带 429 退避的重试循环
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            self._wait_rate_limit()
            try:
                resp = self._session.post(url, json=payload, headers=headers, timeout=self.timeout)
                if resp.status_code == 429:
                    # 限流：指数退避
                    retry_after = float(resp.headers.get("Retry-After", 0) or 0)
                    backoff = max(retry_after, self._base_backoff * (2 ** attempt))
                    if attempt < self._max_retries:
                        print(f"[firecrawl] 429 限流，{backoff:.1f}s 后重试（第 {attempt+1}/{self._max_retries} 次）")
                        time.sleep(backoff)
                        continue
                    resp.raise_for_status()
                resp.raise_for_status()
                break
            except requests.HTTPError as e:
                last_exc = e
                # 非 429 的 HTTP 错误直接抛出
                if e.response is None or e.response.status_code != 429:
                    raise
                # 429 但已用完重试
                if attempt >= self._max_retries:
                    raise
            except requests.RequestException:
                raise

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
