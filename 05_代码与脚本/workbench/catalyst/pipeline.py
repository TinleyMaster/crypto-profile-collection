"""
催化剂核心落库模块：跨源去重 + 多资产关联 + 合并写入。

核心函数：
- upsert_catalyst_item(item, conn) -> dict
    1. 计算 content_hash
    2. 查是否已有同 hash 记录
    3. 有 → 合并（追加 source_code、合并 pairs、补全字段）
    4. 无 → 新插入
    5. 更新多资产关联表 catalyst_asset_link

设计原则：
- content_hash 是跨源去重的唯一真相
- source_codes 数组记录所有来源
- 字段合并策略：先入为主，后入补空（先到的源填了的字段保留，后到的只补空）
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from .models import CatalystItem
from .linker import map_pairs_to_asset_ids, extract_pairs_from_text

logger = logging.getLogger(__name__)


def upsert_catalyst_item(
    item: CatalystItem,
    conn,
    link_source: str = "trading_pairs",
) -> dict | None:
    """upsert 一条催化剂（带跨源去重 + 多资产关联）。

    Args:
        item: 催化剂条目
        conn: 数据库连接
        link_source: 关联来源标记（trading_pairs / cashtag / manual）

    Returns:
        落库后的 catalyst 行 dict
    """
    content_hash = item.content_hash

    # 1. 查是否已有同 hash 记录
    existing = conn.execute(
        "SELECT * FROM biz.asset_catalyst WHERE content_hash = %s",
        (content_hash,),
    ).fetchone()

    if existing:
        # 2. 合并模式：已有同内容，追加来源 + 补空字段
        return _merge_catalyst(existing, item, conn, link_source)
    else:
        # 3. 新增模式
        return _insert_catalyst(item, content_hash, conn, link_source)


def _insert_catalyst(
    item: CatalystItem,
    content_hash: str,
    conn,
    link_source: str,
) -> dict:
    """新增一条催化剂记录 + 多资产关联。"""
    # 主资产 = 第一个关联到的资产（兼容旧查询）
    asset_ids = _resolve_asset_ids(item, conn, link_source)
    primary_asset_id = asset_ids[0] if asset_ids else None

    row = conn.execute(
        """
        INSERT INTO biz.asset_catalyst (
            source_code, source_article_id, source_article_code,
            asset_id, title, body_text, body_html,
            published_at, event_category, event_subcategory,
            related_pairs, source_url, seo_keywords, share_count,
            raw_json, content_hash, source_codes
        ) VALUES (
            %(source_code)s, %(source_item_id)s, %(source_item_code)s,
            %(asset_id)s, %(title)s, %(body_text)s, %(body_html)s,
            %(published_at)s, %(event_category)s, %(event_subcategory)s,
            %(related_pairs)s, %(source_url)s, %(seo_keywords)s, %(share_count)s,
            %(raw_json)s, %(content_hash)s, %(source_codes)s
        )
        ON CONFLICT (content_hash) DO UPDATE SET
            updated_at = NOW()
        RETURNING *
        """,
        {
            "source_code": item.source_code,
            "source_item_id": item.source_item_id,
            "source_item_code": item.source_item_code or item.source_item_id,
            "asset_id": primary_asset_id,
            "title": item.title,
            "body_text": item.body_text,
            "body_html": item.body_html,
            "published_at": _to_dt(item.published_at),
            "event_category": item.event_category,
            "event_subcategory": item.event_subcategory,
            "related_pairs": item.related_pairs or None,
            "source_url": item.source_url,
            "seo_keywords": item.seo_keywords or None,
            "share_count": item.share_count or 0,
            "raw_json": json.dumps(item.raw_json) if item.raw_json else None,
            "content_hash": content_hash,
            "source_codes": [item.source_code],
        },
    ).fetchone()

    # 写多资产关联表
    _update_asset_links(row["catalyst_id"], asset_ids, link_source, conn)
    return row


def _merge_catalyst(
    existing: dict,
    item: CatalystItem,
    conn,
    link_source: str,
) -> dict:
    """合并到已有记录：追加来源 + 补空字段 + 合并资产关联。"""
    catalyst_id = existing["catalyst_id"]

    # 已有 source_codes
    existing_sources = list(existing.get("source_codes") or [])
    if item.source_code not in existing_sources:
        existing_sources.append(item.source_code)

    # 合并 related_pairs
    existing_pairs = list(existing.get("related_pairs") or [])
    for p in item.related_pairs:
        if p not in existing_pairs:
            existing_pairs.append(p)

    # 补空字段策略：先入为主，后入只补空
    updates = {}
    for field in ("title", "body_text", "body_html", "event_category", "event_subcategory", "source_url"):
        if not existing.get(field) and getattr(item, field, ""):
            updates[field] = getattr(item, field)

    # share_count 取大
    new_share = max(existing.get("share_count") or 0, item.share_count or 0)
    if new_share != existing.get("share_count"):
        updates["share_count"] = new_share

    # 合并 seo_keywords
    existing_keywords = list(existing.get("seo_keywords") or [])
    for kw in item.seo_keywords:
        if kw not in existing_keywords:
            existing_keywords.append(kw)
    if len(existing_keywords) != len(existing.get("seo_keywords") or []):
        updates["seo_keywords"] = existing_keywords

    # 构造更新 SQL
    if updates or existing_sources != list(existing.get("source_codes") or []):
        set_clauses = ["source_codes = %s"]
        params = [existing_sources]
        for k, v in updates.items():
            set_clauses.append(f"{k} = %s")
            params.append(v)
        set_clauses.append("updated_at = NOW()")
        params.append(catalyst_id)

        conn.execute(
            f"""
            UPDATE biz.asset_catalyst
            SET {', '.join(set_clauses)}
            WHERE catalyst_id = %s
            """,
            params,
        )

    # 合并资产关联（新 pairs 可能映射出新资产）
    all_pairs = list(set(existing_pairs + item.related_pairs))
    asset_ids = map_pairs_to_asset_ids(all_pairs, conn)
    _update_asset_links(catalyst_id, asset_ids, link_source, conn)

    # 返回最新行
    return conn.execute(
        "SELECT * FROM biz.asset_catalyst WHERE catalyst_id = %s",
        (catalyst_id,),
    ).fetchone()


def _resolve_asset_ids(
    item: CatalystItem,
    conn,
    link_source: str,
) -> list[int]:
    """解析 item 的资产关联（先 trading_pairs，兜底 cashtag 提取）。"""
    pairs = list(item.related_pairs) if item.related_pairs else []

    # 兜底：从正文提取 cashtag
    if not pairs:
        pairs = extract_pairs_from_text(item.title + " " + item.body_text)
        if pairs:
            link_source = "cashtag"

    if not pairs:
        return []

    return map_pairs_to_asset_ids(pairs, conn)


def _update_asset_links(
    catalyst_id: int,
    asset_ids: list[int],
    link_source: str,
    conn,
):
    """更新 catalyst_asset_link 表（增量插入，已存在的跳过）。"""
    if not asset_ids:
        return

    for aid in asset_ids:
        conn.execute(
            """
            INSERT INTO biz.catalyst_asset_link (catalyst_id, asset_id, link_source, confidence)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (catalyst_id, asset_id) DO NOTHING
            """,
            (catalyst_id, aid, link_source, 0.9 if link_source == "trading_pairs" else 0.6),
        )


def _to_dt(ts: float | None) -> datetime | None:
    """秒级时间戳 → datetime，None 保持 None。"""
    if not ts:
        return None
    return datetime.fromtimestamp(ts)


def get_latest_publish_time(source_code: str, conn) -> float | None:
    """获取某来源最新发布时间（秒级时间戳），用于增量抓取。"""
    row = conn.execute(
        """
        SELECT MAX(published_at) as latest
        FROM biz.asset_catalyst
        WHERE source_code = %s
           OR %s = ANY(source_codes)
        """,
        (source_code, source_code),
    ).fetchone()
    if not row or not row["latest"]:
        return None
    if isinstance(row["latest"], datetime):
        return row["latest"].timestamp()
    return float(row["latest"])
