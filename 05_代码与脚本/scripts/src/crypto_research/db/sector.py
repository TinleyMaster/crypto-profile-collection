"""资产赛道（sector）增量更新工具。

用于资产入库时实时写入该来源的赛道标签，并重新裁决主赛道。
与 refresh_sectors_multi_source.sql（全量批量）规则一致，
映射规则统一维护在 crypto_research.mapping.sector。
"""
from __future__ import annotations

from typing import Iterable

import psycopg

from crypto_research.mapping.sector import (
    classify_cmc_sectors,
    classify_cg_sectors,
    classify_dl_sectors,
)


def upsert_asset_sectors(
    conn: psycopg.Connection,
    asset_id: int,
    source: str,
    sectors: list[tuple[str, float]],
) -> None:
    """增量更新单个资产某来源的赛道标签，并重新裁决该资产的主赛道。

    Args:
        conn: 数据库连接（调用方负责事务）
        asset_id: 资产 ID
        source: 来源代码（cmc / cg / dl）
        sectors: 该来源命中的赛道列表 [(sector, confidence), ...]
    """
    if source not in ("cmc", "cg", "dl"):
        raise ValueError(f"未知来源: {source}")

    with conn.cursor() as cur:
        # 1. 清除该资产该来源的旧标签
        cur.execute(
            "DELETE FROM biz.asset_sector WHERE asset_id = %s AND source = %s",
            (asset_id, source),
        )

        # 2. 写入新标签
        if sectors:
            cur.executemany(
                """
                INSERT INTO biz.asset_sector (asset_id, sector, source, confidence, is_primary)
                VALUES (%s, %s, %s, %s, false)
                """,
                [(asset_id, sector, source, conf) for sector, conf in sectors],
            )

        # 3. 重新裁决该资产的 is_primary
        cur.execute(
            """
            WITH ranked AS (
                SELECT asset_id, sector, source,
                    ROW_NUMBER() OVER (
                        PARTITION BY asset_id
                        ORDER BY confidence DESC,
                            CASE source
                                WHEN 'cmc' THEN 3
                                WHEN 'cg' THEN 2
                                WHEN 'dl' THEN 1
                                ELSE 0
                            END DESC
                    ) as rn
                FROM biz.asset_sector
                WHERE asset_id = %s
            )
            UPDATE biz.asset_sector s
            SET is_primary = (r.rn = 1),
                updated_at = NOW()
            FROM ranked r
            WHERE s.asset_id = r.asset_id
              AND s.sector = r.sector
              AND s.source = r.source
            """,
            (asset_id,),
        )

        # 4. 更新 core.asset.primary_sector
        cur.execute(
            """
            UPDATE core.asset a
            SET primary_sector = COALESCE(
                (SELECT sector FROM biz.asset_sector
                 WHERE asset_id = a.asset_id AND is_primary = true
                 ORDER BY confidence DESC LIMIT 1),
                'other'
            ),
                updated_at = NOW()
            WHERE a.asset_id = %s
            """,
            (asset_id,),
        )


def upsert_asset_sectors_batch(
    conn: psycopg.Connection,
    asset_ids: Iterable[int],
    source: str,
    sectors_by_asset: dict[int, list[tuple[str, float]]],
) -> None:
    """批量更新多个资产某来源的赛道标签，并重新裁决主赛道。

    Args:
        conn: 数据库连接（调用方负责事务）
        asset_ids: 资产 ID 列表
        source: 来源代码（cmc / cg / dl）
        sectors_by_asset: {asset_id: [(sector, confidence), ...]}
    """
    if source not in ("cmc", "cg", "dl"):
        raise ValueError(f"未知来源: {source}")

    asset_list = list(asset_ids)
    if not asset_list:
        return

    with conn.cursor() as cur:
        # 1. 清除这些资产该来源的旧标签
        cur.execute(
            f"DELETE FROM biz.asset_sector WHERE source = %s AND asset_id = ANY(%s)",
            (source, asset_list),
        )

        # 2. 写入新标签
        tag_rows = []
        for aid in asset_list:
            for sector, conf in sectors_by_asset.get(aid, []):
                tag_rows.append((aid, sector, source, conf))
        if tag_rows:
            cur.executemany(
                """
                INSERT INTO biz.asset_sector (asset_id, sector, source, confidence, is_primary)
                VALUES (%s, %s, %s, %s, false)
                """,
                tag_rows,
            )

        # 3. 重新裁决这些资产的 is_primary
        cur.execute(
            """
            WITH ranked AS (
                SELECT asset_id, sector, source,
                    ROW_NUMBER() OVER (
                        PARTITION BY asset_id
                        ORDER BY confidence DESC,
                            CASE source
                                WHEN 'cmc' THEN 3
                                WHEN 'cg' THEN 2
                                WHEN 'dl' THEN 1
                                ELSE 0
                            END DESC
                    ) as rn
                FROM biz.asset_sector
                WHERE asset_id = ANY(%s)
            )
            UPDATE biz.asset_sector s
            SET is_primary = (r.rn = 1),
                updated_at = NOW()
            FROM ranked r
            WHERE s.asset_id = r.asset_id
              AND s.sector = r.sector
              AND s.source = r.source
            """,
            (asset_list,),
        )

        # 4. 更新 core.asset.primary_sector
        cur.execute(
            """
            UPDATE core.asset a
            SET primary_sector = COALESCE(
                (SELECT sector FROM biz.asset_sector
                 WHERE asset_id = a.asset_id AND is_primary = true
                 ORDER BY confidence DESC LIMIT 1),
                'other'
            ),
                updated_at = NOW()
            WHERE a.asset_id = ANY(%s)
            """,
            (asset_list,),
        )


# ── 便捷函数：从原始信号直接分类并写入 ──────────────────────────────

def classify_and_upsert_cmc(
    conn: psycopg.Connection,
    asset_id: int,
    tags: list[str] | None,
    category_hint: str | None,
) -> list[tuple[str, float]]:
    """根据 CMC 信号分类并写入赛道标签。返回命中的赛道列表。"""
    sectors = classify_cmc_sectors(tags, category_hint)
    upsert_asset_sectors(conn, asset_id, "cmc", sectors)
    return sectors


def classify_and_upsert_cg(
    conn: psycopg.Connection,
    asset_id: int,
    categories: list[str] | None,
) -> list[tuple[str, float]]:
    """根据 CG 信号分类并写入赛道标签。返回命中的赛道列表。"""
    sectors = classify_cg_sectors(categories)
    upsert_asset_sectors(conn, asset_id, "cg", sectors)
    return sectors


def classify_and_upsert_dl(
    conn: psycopg.Connection,
    asset_id: int,
    category: str | None,
) -> list[tuple[str, float]]:
    """根据 DL 信号分类并写入赛道标签。返回命中的赛道列表。"""
    sectors = classify_dl_sectors(category)
    upsert_asset_sectors(conn, asset_id, "dl", sectors)
    return sectors
