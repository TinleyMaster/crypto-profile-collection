"""
数据库查询工具：给工作台提供统计数据。
复用 scripts/src/crypto_research 的数据库连接。
"""
from __future__ import annotations

import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_SRC = WORKSPACE_ROOT / "05_代码与脚本" / "scripts" / "src"
if str(SCRIPTS_SRC) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_SRC))


def get_db():
    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    settings = get_settings(require_database=True)
    return get_connection(settings.database_url)


def get_dashboard_stats() -> dict:
    """返回仪表盘需要的全部统计数据。"""
    result = {}
    with get_db() as conn:
        with conn.cursor() as cur:
            # doc_source_entry 总数与分类
            cur.execute(
                """
                SELECT entry_type, count(*) FROM biz.doc_source_entry
                GROUP BY entry_type ORDER BY count(*) DESC
                """
            )
            result["entry_types"] = {row[0]: row[1] for row in cur.fetchall()}
            result["total_entries"] = sum(result["entry_types"].values())

            # 深度爬取进度
            cur.execute(
                """
                SELECT entry_type,
                       count(*) AS total,
                       count(deep_crawled_at) AS crawled,
                       count(*) - count(deep_crawled_at) AS pending
                FROM biz.doc_source_entry
                WHERE entry_type IN ('official_website', 'docs', 'docs_portal')
                GROUP BY entry_type
                ORDER BY total DESC
                """
            )
            crawl_progress = {}
            for row in cur.fetchall():
                crawl_progress[row[0]] = {
                    "total": row[1],
                    "crawled": row[2],
                    "pending": row[3],
                    "pct": round(row[2] / row[1] * 100, 1) if row[1] > 0 else 0,
                }
            result["crawl_progress"] = crawl_progress

            # doc_asset 统计
            cur.execute(
                """
                SELECT
                    count(*) AS total,
                    count(storage_path) AS downloaded,
                    count(content_hash) AS hashed,
                    count(*) FILTER (WHERE parse_status = '已解析') AS parsed
                FROM biz.doc_asset
                """
            )
            row = cur.fetchone()
            result["doc_asset"] = {
                "total": row[0],
                "downloaded": row[1],
                "hashed": row[2],
                "parsed": row[3],
            }

            # 资产总数
            cur.execute("SELECT count(*) FROM core.asset")
            result["total_assets"] = cur.fetchone()[0]

            # 数据源分布
            cur.execute(
                """
                SELECT source_code, count(*) FROM core.asset_source_map
                GROUP BY source_code ORDER BY count(*) DESC
                """
            )
            result["source_distribution"] = {row[0]: row[1] for row in cur.fetchall()}

    return result


def get_pending_b2() -> dict:
    """B2 剩余待爬数量。"""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT entry_type, count(*)
                FROM biz.doc_source_entry
                WHERE entry_type IN ('official_website', 'docs', 'docs_portal')
                  AND deep_crawled_at IS NULL
                GROUP BY entry_type
                ORDER BY count(*) DESC
                """
            )
            rows = cur.fetchall()
            result = {r[0]: r[1] for r in rows}
            result["total"] = sum(result.values())
    return result
