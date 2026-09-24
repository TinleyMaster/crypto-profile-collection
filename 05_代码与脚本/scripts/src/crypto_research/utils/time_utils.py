"""展示层时区工具：全仓库「东八区（北京时间）」的唯一定义。

数据存储与判定口径一律用 UTC；仅邮件等展示文案经本模块转换后输出。
禁止在别处重复定义 ``timezone(timedelta(hours=8))``。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# 东八区（北京时间，UTC+8，无夏令时）
BJ_TZ = timezone(timedelta(hours=8))


def to_bj(dt: datetime | str | None) -> datetime | None:
    """把时间转成北京时间（aware）。无法解析时返回 None。

    - aware datetime：直接 astimezone
    - naive datetime：按 UTC 解释（DB 无时区 timestamp 的项目约定）
    - ISO 字符串：先解析（容忍尾部 Z）；无时区者同样按 UTC 解释
    """
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(BJ_TZ)
    except (ValueError, OSError):
        return None


def fmt_bj(dt: datetime | str | None, fmt: str = "%m-%d %H:%M",
           fallback: str = "—") -> str:
    """格式化为北京时间字符串；None 或非法值返回 fallback。"""
    bj = to_bj(dt)
    if bj is None:
        return fallback
    return bj.strftime(fmt)
