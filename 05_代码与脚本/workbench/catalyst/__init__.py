"""
催化剂模块：多平台官方事件源抓取与落库。

架构：插件式多源框架，新增平台只需继承 BaseCatalystSource 并注册。

已接入：
- binance_news / binance_listing  — 币安 CMS 公告（全量历史）
- binance_square_news             — 币安广场 Binance News 账号（实时前门）

核心 API：
    from catalyst import run_source, run_all, list_sources
    run_all()  # 跑所有源
    run_source("binance_square_news")  # 跑单个源
"""

from .models import CatalystItem
from .base import BaseCatalystSource
from .pipeline import upsert_catalyst_item
from .runner import run_source, run_all
from .sources import list_sources, SOURCE_REGISTRY

__all__ = [
    "CatalystItem",
    "BaseCatalystSource",
    "upsert_catalyst_item",
    "run_source",
    "run_all",
    "list_sources",
    "SOURCE_REGISTRY",
]
