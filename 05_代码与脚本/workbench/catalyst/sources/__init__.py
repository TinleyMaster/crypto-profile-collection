"""
催化剂数据源插件注册中心。

新增平台时：
1. 在本目录下新建 {platform}.py，实现 BaseCatalystSource
2. 在下方 SOURCE_REGISTRY 中注册
"""
from __future__ import annotations

from typing import Type

from ..base import BaseCatalystSource

# 源编码 → 源类 的注册表
SOURCE_REGISTRY: dict[str, Type[BaseCatalystSource]] = {}


def register_source(source_cls: Type[BaseCatalystSource]) -> Type[BaseCatalystSource]:
    """装饰器：注册催化剂源。"""
    code = source_cls.source_code
    if not code:
        raise ValueError(f"Source class {source_cls.__name__} has no source_code")
    SOURCE_REGISTRY[code] = source_cls
    return source_cls


def get_source(source_code: str) -> Type[BaseCatalystSource] | None:
    """根据 source_code 获取源类。"""
    return SOURCE_REGISTRY.get(source_code)


def list_sources() -> list[str]:
    """列出所有已注册的源编码。"""
    return sorted(SOURCE_REGISTRY.keys())


# 导入各源以触发注册
from . import binance_square_news  # noqa: E402,F401
from . import binance_cms          # noqa: E402,F401
