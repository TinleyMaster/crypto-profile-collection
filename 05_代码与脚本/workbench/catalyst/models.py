"""
催化剂统一数据模型（平台无关）。

所有催化剂源（Binance CMS / Binance Square / CoinGecko / ...）
都输出 CatalystItem 列表，由核心模块统一做：去重、资产关联、落库。

新增平台只需：
1. 继承 BaseCatalystSource
2. 实现 fetch() 返回 list[CatalystItem]
3. 注册到 SOURCE_REGISTRY
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field


@dataclass
class CatalystItem:
    """一条催化剂事件（平台无关的统一结构）。

    核心字段：
    - source_code: 来源编码（如 binance_news, binance_square_news, coingecko_events）
    - source_item_id: 来源侧的唯一 ID（用于同源去重）
    - title / body_text: 标题和正文（用于 content_hash 跨源去重）
    - published_at: 发布时间（秒级时间戳）
    - related_pairs: 关联交易对列表（用于资产关联）
    """
    source_code: str
    source_item_id: str
    title: str
    body_text: str
    published_at: float  # 秒级时间戳

    # 可选字段
    source_item_code: str = ""          # 来源侧 code（如 CMS 的 articleCode）
    body_html: str = ""
    event_category: str = ""
    event_subcategory: str = ""
    related_pairs: list[str] = field(default_factory=list)
    source_url: str = ""
    seo_keywords: list[str] = field(default_factory=list)
    share_count: int = 0
    raw_json: dict | None = None

    # 计算属性：content_hash（跨源去重键）
    @property
    def content_hash(self) -> str:
        """计算内容哈希（sha256 of 归一化 title + 正文前 200 字）。

        归一化规则：全小写 + 压缩空白，确保同内容不同格式也能匹配。
        """
        normalized = _normalize_text(
            (self.title or "") + "|" + (self.body_text or "")[:200]
        )
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _normalize_text(text: str) -> str:
    """文本归一化：去多余空白、全小写，用于哈希比较。"""
    if not text:
        return ""
    # 压缩空白
    text = re.sub(r"\s+", " ", text)
    # 全小写
    text = text.lower()
    return text.strip()
