"""Vybe Network Solana 钱包标签 API 抓取器。

通过 Vybe API 查询 Solana 钱包标签（CEX、KOL、DEFI、MM、VC、TREASURY 等）。

官方文档：https://docs.vybenetwork.com/docs/labeled-wallets

两种使用模式：
1. 全量拉取（推荐 backfill 用）：一次拉所有标签，本地匹配。
   用法：
       fetcher = VybeLabelFetcher(api_key="xxx")
       label_map = fetcher.fetch_all_labels()  # 返回 {address: label_info}

2. 单地址查询（实时 enrich 用）：按地址逐个查。
   用法：
       fetcher = VybeLabelFetcher(api_key="xxx")
       label_map = fetcher.fetch_batch(["addr1", "addr2"])

标签类型映射（Vybe → 内部类型）：
    CEX       → exchange
    MM        → market_maker
    DEFI      → dex
    KOL       → smart_money
    VC        → smart_money
    TREASURY  → dex（项目金库，归为 dex 类）
    NFT       → （忽略，非高价值标签）
"""
from __future__ import annotations

import time
from typing import Any

try:
    import requests
except ImportError:
    requests = None  # noqa


# Vybe 标签 → 内部标签类型映射
VYBE_LABEL_TO_INTERNAL: dict[str, str] = {
    "CEX": "exchange",
    "MM": "market_maker",
    "DEFI": "dex",
    "KOL": "smart_money",
    "VC": "smart_money",
    "TREASURY": "dex",
    # "NFT":  忽略
}

# 高价值标签（只存这些）
HIGH_VALUE_VYBE_LABELS = set(VYBE_LABEL_TO_INTERNAL.keys())


class VybeLabelFetcher:
    """Vybe Network Solana 钱包标签抓取器。"""

    BASE_URL = "https://api.vybenetwork.xyz/v4/wallets/labeled-accounts"

    def __init__(self, api_key: str, proxy: str | None = None,
                 timeout: int = 15, rate_limit_delay: float = 0.1):
        """
        Args:
            api_key: Vybe API key
            proxy: 代理地址，如 http://127.0.0.1:7890
            timeout: 请求超时秒数
            rate_limit_delay: 每次请求间的延迟秒数（限流保护）
        """
        if requests is None:
            raise ImportError("requests 库未安装")
        self.api_key = api_key
        self.proxy = proxy
        self.timeout = timeout
        self.rate_limit_delay = rate_limit_delay
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/json",
            "X-API-Key": api_key,
        })
        if proxy:
            self._session.proxies.update({"http": proxy, "https": proxy})

    # ── 全量拉取（推荐）────────────────────────────────────

    def fetch_all_labels(self) -> dict[str, dict[str, Any]]:
        """一次拉取全部标签数据，返回 {address: label_info} 映射。

        label_info 格式与 ExplorerLabelFetcher 兼容：
            {
                "label_text": str,      # 原始标签文本（entity name）
                "label_type": str,      # 内部标签类型（exchange / market_maker / ...）
                "display_name": str,    # 显示名
                "normalized_name": str, # 归一化交易所名（仅 exchange 类型）
                "is_exchange": bool,
                "vybe_labels": list,    # 原始 Vybe 标签列表
                "entity_name": str,     # 实体名（Binance / Jump 等）
            }
        """
        label_map: dict[str, dict[str, Any]] = {}
        page = 1
        page_size = 100  # Vybe 默认分页大小

        while True:
            params = {"page": page, "limit": page_size}
            try:
                resp = self._session.get(self.BASE_URL, params=params, timeout=self.timeout)
            except Exception as e:
                raise RuntimeError(f"Vybe API 请求失败 (page={page}): {e}")

            if resp.status_code != 200:
                raise RuntimeError(
                    f"Vybe API 返回 {resp.status_code} (page={page}): {resp.text[:200]}"
                )

            data = resp.json()
            items = data.get("data", [])
            if not items:
                break

            for item in items:
                info = self._parse_item(item)
                if info:
                    addr = item.get("ownerAddress", "").strip()
                    if addr:
                        label_map[addr] = info

            # 翻页：如果返回数量 < page_size，说明到最后一页了
            if len(items) < page_size:
                break

            page += 1
            time.sleep(self.rate_limit_delay)

        return label_map

    # ── 单地址 / 批量查询（实时 enrich 用）──────────────────

    def fetch_batch(self, addresses: list[str]) -> dict[str, dict[str, Any]]:
        """批量查询地址标签。

        注意：Vybe API 每次只能查一个地址，内部串行请求。
        对于 backfill 场景，强烈建议用 fetch_all_labels() 一次拉全量。
        """
        label_map: dict[str, dict[str, Any]] = {}
        for addr in addresses:
            info = self.fetch(addr)
            if info:
                label_map[addr] = info
            time.sleep(self.rate_limit_delay)
        return label_map

    def fetch_batch_with_stats(self, addresses: list[str]) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
        """批量查询 + 状态统计。与 ExplorerLabelFetcher 接口兼容。"""
        stats = {
            "ok": 0,
            "no_label": 0,
            "http_403": 0,
            "http_429": 0,
            "http_other": 0,
            "network_error": 0,
        }
        label_map: dict[str, dict[str, Any]] = {}

        for addr in addresses:
            try:
                resp = self._session.get(
                    self.BASE_URL,
                    params={"accountAddress": addr},
                    timeout=self.timeout,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    items = data.get("data", [])
                    if items:
                        info = self._parse_item(items[0])
                        if info:
                            label_map[addr] = info
                            stats["ok"] += 1
                        else:
                            stats["no_label"] += 1
                    else:
                        stats["no_label"] += 1
                elif resp.status_code == 403:
                    stats["http_403"] += 1
                elif resp.status_code == 429:
                    stats["http_429"] += 1
                else:
                    stats["http_other"] += 1
            except Exception:
                stats["network_error"] += 1

            time.sleep(self.rate_limit_delay)

        return label_map, stats

    def fetch(self, address: str) -> dict[str, Any] | None:
        """查询单个地址的标签。"""
        try:
            resp = self._session.get(
                self.BASE_URL,
                params={"accountAddress": address},
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            items = data.get("data", [])
            if not items:
                return None
            return self._parse_item(items[0])
        except Exception:
            return None

    # ── 内部方法 ────────────────────────────────────────────

    def _parse_item(self, item: dict[str, Any]) -> dict[str, Any] | None:
        """解析 Vybe API 返回的单条数据。

        返回格式与 ExplorerLabelFetcher 兼容，
        以便无缝接入 LabelEnricher。
        """
        labels = item.get("labels", []) or []
        entity_name = item.get("entityName") or ""
        name = item.get("name") or entity_name

        # 过滤出高价值标签，并选一个"主类型"
        valuable = [l for l in labels if l in HIGH_VALUE_VYBE_LABELS]
        if not valuable:
            return None  # 没有高价值标签，跳过

        # 优先级：exchange > market_maker > dex > smart_money
        priority = ["CEX", "MM", "DEFI", "TREASURY", "KOL", "VC"]
        primary_label = None
        for p in priority:
            if p in valuable:
                primary_label = p
                break

        if not primary_label:
            return None

        label_type = VYBE_LABEL_TO_INTERNAL[primary_label]
        is_exchange = label_type == "exchange"

        return {
            "label_text": name,
            "label_type": label_type,
            "display_name": entity_name or name,
            "normalized_name": entity_name if is_exchange else None,
            "is_exchange": is_exchange,
            "vybe_labels": labels,
            "entity_name": entity_name,
        }

    def close(self) -> None:
        """关闭 session。"""
        try:
            self._session.close()
        except Exception:
            pass
