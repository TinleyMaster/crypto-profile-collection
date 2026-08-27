"""
催化剂源基类（插件式）。

新增平台只需：
1. 继承 BaseCatalystSource
2. 实现 source_code 属性和 fetch() 方法
3. 在 sources/__init__.py 中注册

核心流程：
    source.fetch(since_ts) → list[CatalystItem]
        → dedup（content_hash 跨源去重）
        → link_assets（交易对 → 多资产关联）
        → persist（落库，合并而非重复插入）
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import ClassVar

from .models import CatalystItem

logger = logging.getLogger(__name__)


class BaseCatalystSource(ABC):
    """催化剂数据源基类。

    子类必须实现：
    - source_code: str  来源编码（唯一标识，如 binance_news）
    - fetch(since_ts) -> list[CatalystItem]  抓取 since_ts 之后的新事件
    """

    source_code: ClassVar[str] = ""

    @abstractmethod
    def fetch(self, since_ts: float | None = None) -> list[CatalystItem]:
        """抓取催化剂事件。

        Args:
            since_ts: 增量起点（秒级时间戳），None 表示全量/默认。

        Returns:
            CatalystItem 列表（应按发布时间倒序，但不强制）。
        """
        ...

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """资源清理（如关闭 session）。子类可覆盖。"""
        pass
