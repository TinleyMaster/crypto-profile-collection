"""
催化剂统一运行器。

按源调度：依次抓取各数据源 → 统一走 pipeline（去重+关联+落库）。

新增平台只需在 sources/__init__.py 注册，这里自动可用。
"""
from __future__ import annotations

import logging
from datetime import datetime

from .db import get_conn
from .pipeline import upsert_catalyst_item, get_latest_publish_time
from .sources import SOURCE_REGISTRY, get_source
from .binance_news import RateLimitedError

logger = logging.getLogger(__name__)


def run_source(
    source_code: str,
    since_ts: float | None = None,
    max_pages: int | None = None,
    **source_kwargs,
) -> dict:
    """运行单个催化剂源。

    Args:
        source_code: 源编码（需已注册）
        since_ts: 增量起点时间戳，None 表示自动取库中最新
        max_pages: 覆盖默认翻页数
        **source_kwargs: 透传给源构造函数的参数

    Returns:
        统计 dict: {source_code, fetched, inserted, merged, skipped, error}
    """
    source_cls = get_source(source_code)
    if not source_cls:
        return {
            "source_code": source_code,
            "fetched": 0, "inserted": 0, "merged": 0, "skipped": 0,
            "error": f"source not found: {source_code}",
        }

    if max_pages is not None:
        source_kwargs.setdefault("max_pages", max_pages)

    stats = {
        "source_code": source_code,
        "fetched": 0,
        "inserted": 0,
        "merged": 0,
        "skipped": 0,
        "error": "",
    }

    try:
        with get_conn() as conn:
            # 自动增量：since_ts 为 None 时取库中最新
            if since_ts is None:
                since_ts = get_latest_publish_time(source_code, conn)
                if since_ts:
                    # 留 1 小时重叠，防止边界漏
                    since_ts -= 3600
                    logger.info("auto-increment since_ts: %s (%s)",
                                since_ts, datetime.fromtimestamp(since_ts))

            with source_cls(**source_kwargs) as source:
                items = source.fetch(since_ts=since_ts)

            stats["fetched"] = len(items)

            for item in items:
                try:
                    # 先查 hash 判断是新增还是合并
                    existing = conn.execute(
                        "SELECT catalyst_id FROM biz.asset_catalyst WHERE content_hash = %s",
                        (item.content_hash,),
                    ).fetchone()

                    row = upsert_catalyst_item(item, conn)

                    if existing:
                        stats["merged"] += 1
                    else:
                        stats["inserted"] += 1
                except Exception as e:
                    logger.error("upsert failed id=%s: %s", item.source_item_id, e)
                    conn.rollback()
                    stats["skipped"] += 1

    except RateLimitedError as e:
        logger.error("source %s rate limited: %s", source_code, e)
        stats["error"] = f"rate limited (429): {e}"
    except Exception as e:
        logger.error("source %s failed: %s", source_code, e)
        stats["error"] = str(e)

    return stats


def run_all(
    sources: list[str] | None = None,
    since_ts: float | None = None,
    max_pages: int | None = None,
) -> list[dict]:
    """运行多个催化剂源（默认全部已注册的）。

    Args:
        sources: 源编码列表，None 表示全部
        since_ts: 增量起点，None 表示自动
        max_pages: 覆盖默认翻页数

    Returns:
        各源统计列表
    """
    if sources is None:
        sources = sorted(SOURCE_REGISTRY.keys())

    results = []
    for code in sources:
        logger.info("=" * 60)
        logger.info("running source: %s", code)
        stats = run_source(code, since_ts=since_ts, max_pages=max_pages)
        results.append(stats)
        logger.info(
            "source %s done: fetched=%d inserted=%d merged=%d skipped=%d error=%s",
            code, stats["fetched"], stats["inserted"],
            stats["merged"], stats["skipped"], stats["error"],
        )

    return results
