"""
数据库查询工具：给工作台提供统计数据。
复用 scripts/src/crypto_research 的数据库连接。
"""

from __future__ import annotations

import sys
import os
import urllib.request
import urllib.error
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import psycopg
import psycopg.rows
import psycopg_pool

# Docker 环境下直接用 /app/scripts/src，本地则相对路径计算
if os.path.exists("/app/scripts/src"):
    SCRIPTS_SRC = Path("/app/scripts/src")
else:
    WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
    SCRIPTS_SRC = WORKSPACE_ROOT / "05_代码与脚本" / "scripts" / "src"

if str(SCRIPTS_SRC) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_SRC))

try:
    from crypto_research.mapping.sector import (
        SECTOR_LABELS,
        get_sector_visible_material_keys,
        topic_priority_rank,
    )
except ImportError:  # pragma: no cover - 独立运行场景
    SECTOR_LABELS = {}
    get_sector_visible_material_keys = None
    topic_priority_rank = None


_pool: psycopg_pool.ConnectionPool | None = None


def _get_pool() -> psycopg_pool.ConnectionPool:
    """惰性创建连接池（常驻进程复用连接，避免每次请求重新握手远程数据库）。"""
    global _pool
    if _pool is None:
        from crypto_research.config import get_settings

        settings = get_settings(require_database=True)
        _pool = psycopg_pool.ConnectionPool(
            settings.database_url,
            min_size=1,
            max_size=5,
            open=True,
            timeout=30,
            kwargs={"connect_timeout": 30},
        )
    return _pool


@contextmanager
def get_db():
    """从连接池取连接，保留原 get_connection 的 commit/rollback 语义。"""
    with _get_pool().connection() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


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
                WHERE entry_type IN ('official_website', 'docs')
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

            # doc_source_entry 按数据源分布
            cur.execute(
                """
                SELECT source_code, COUNT(*) AS cnt
                FROM biz.doc_source_entry
                WHERE entity_type = 'asset'
                GROUP BY source_code
                ORDER BY cnt DESC
                """
            )
            source_stats = {row[0]: row[1] for row in cur.fetchall()}
            result["doc_source_stats"] = {
                "total": sum(source_stats.values()),
                "cmc": source_stats.get("cmc", 0),
                "cg": source_stats.get("cg", 0),
                "dl": source_stats.get("dl", 0),
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
                WHERE entry_type IN ('official_website', 'docs')
                  AND deep_crawled_at IS NULL
                GROUP BY entry_type
                ORDER BY count(*) DESC
                """
            )
            rows = cur.fetchall()
            result = {r[0]: r[1] for r in rows}
            result["total"] = sum(result.values())
    return result


def get_task_progress() -> list[dict]:
    """返回各自动循环任务的进度：已处理/总量/百分比。"""
    result = []
    with get_db() as conn:
        with conn.cursor() as cur:
            # 1. CG 拉取币种详情
            #    候选集: src_cg.coin_list 中所有 coin
            #    done: 已拉取 coin_info 的
            cur.execute("SELECT COUNT(*) FROM src_cg.coin_list")
            total = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM src_cg.coin_info")
            done = cur.fetchone()[0]
            remaining = total - done
            result.append({
                "task": "CG 拉取币种详情",
                "done": done,
                "total": total,
                "remaining": remaining,
                "pct": round(done / total * 100, 1) if total > 0 else 0,
            })

            # 2. CG 补充文档入口
            #    候选集: 有 CG coin_info (homepage 或 links) + asset 映射的资产
            #    done: 候选集中已有 CG doc_source_entry 的
            cur.execute(
                """
                SELECT COUNT(DISTINCT asm.asset_id)
                FROM src_cg.coin_info i
                INNER JOIN core.asset_source_map asm ON asm.source_code = 'cg' AND asm.source_asset_key = i.coin_id
                WHERE i.homepage_url IS NOT NULL OR i.links IS NOT NULL
                """
            )
            total = cur.fetchone()[0]
            cur.execute(
                """
                SELECT COUNT(DISTINCT asm.asset_id)
                FROM src_cg.coin_info i
                INNER JOIN core.asset_source_map asm ON asm.source_code = 'cg' AND asm.source_asset_key = i.coin_id
                INNER JOIN biz.doc_source_entry dse ON dse.asset_id = asm.asset_id
                    AND dse.source_code = 'cg' AND dse.entity_type = 'asset'
                WHERE i.homepage_url IS NOT NULL OR i.links IS NOT NULL
                """
            )
            done = cur.fetchone()[0]
            remaining = total - done
            result.append({
                "task": "CG 补充文档入口",
                "done": done,
                "total": total,
                "remaining": remaining,
                "pct": round(done / total * 100, 1) if total > 0 else 0,
            })

            # 3. CMC 补充文档入口
            #    候选集: 有 CMC info (urls 非空数组) + asset 映射的资产
            #    done: 候选集中已有 CMC doc_source_entry 的
            cur.execute(
                """
                SELECT COUNT(DISTINCT asm.asset_id)
                FROM src_cmc.cmc_asset_info i
                INNER JOIN core.asset_source_map asm ON asm.source_code = 'cmc' AND asm.source_asset_key = i.cmc_id::text
                WHERE i.urls IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM jsonb_each(i.urls) AS kv
                      WHERE jsonb_typeof(kv.value) = 'array' AND jsonb_array_length(kv.value) > 0
                  )
                """
            )
            total = cur.fetchone()[0]
            cur.execute(
                """
                SELECT COUNT(DISTINCT asm.asset_id)
                FROM src_cmc.cmc_asset_info i
                INNER JOIN core.asset_source_map asm ON asm.source_code = 'cmc' AND asm.source_asset_key = i.cmc_id::text
                INNER JOIN biz.doc_source_entry dse ON dse.asset_id = asm.asset_id
                    AND dse.source_code = 'cmc' AND dse.entity_type = 'asset'
                WHERE i.urls IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM jsonb_each(i.urls) AS kv
                      WHERE jsonb_typeof(kv.value) = 'array' AND jsonb_array_length(kv.value) > 0
                  )
                """
            )
            done = cur.fetchone()[0]
            remaining = total - done
            result.append({
                "task": "CMC 补充文档入口",
                "done": done,
                "total": total,
                "remaining": remaining,
                "pct": round(done / total * 100, 1) if total > 0 else 0,
            })

            # 3.5. DL 补充文档入口
            cur.execute(
                """
                SELECT COUNT(DISTINCT asm.asset_id)
                FROM src_dl.protocol_list p
                INNER JOIN core.asset_source_map asm ON asm.source_code = 'dl' AND asm.source_asset_key = p.protocol_id
                WHERE (p.url IS NOT NULL AND TRIM(p.url) != '') OR (p.twitter IS NOT NULL AND TRIM(p.twitter) != '')
                """
            )
            total = cur.fetchone()[0]
            cur.execute(
                """
                SELECT COUNT(DISTINCT asm.asset_id)
                FROM src_dl.protocol_list p
                INNER JOIN core.asset_source_map asm ON asm.source_code = 'dl' AND asm.source_asset_key = p.protocol_id
                INNER JOIN biz.doc_source_entry dse ON dse.asset_id = asm.asset_id
                    AND dse.source_code = 'dl' AND dse.entity_type = 'asset'
                WHERE (p.url IS NOT NULL AND TRIM(p.url) != '') OR (p.twitter IS NOT NULL AND TRIM(p.twitter) != '')
                """
            )
            done = cur.fetchone()[0]
            remaining = total - done
            result.append({
                "task": "DL 补充文档入口",
                "done": done,
                "total": total,
                "remaining": remaining,
                "pct": round(done / total * 100, 1) if total > 0 else 0,
            })

            # 3.6. 双源补充文档入口（DexScreener + Binance）
            #    候选集: 无任何 doc_source_entry 的活跃资产（排除衍生品）
            #    done: 已有 dexscreener 或 binance 来源的 doc_source_entry
            cur.execute(
                """
                SELECT COUNT(DISTINCT a.asset_id)
                FROM core.asset a
                LEFT JOIN biz.doc_source_entry dse ON dse.entity_type = 'asset' AND dse.asset_id = a.asset_id
                WHERE dse.entry_id IS NULL
                  AND a.status = 'active'
                  AND a.canonical_symbol IS NOT NULL
                  AND a.canonical_symbol != ''
                  AND a.asset_type NOT IN ('derivative', 'synthetic', 'iou')
                """
            )
            total = cur.fetchone()[0]
            cur.execute(
                """
                SELECT COUNT(DISTINCT a.asset_id)
                FROM core.asset a
                INNER JOIN biz.doc_source_entry dse ON dse.entity_type = 'asset' AND dse.asset_id = a.asset_id
                    AND dse.source_code IN ('dexscreener', 'binance')
                WHERE a.status = 'active'
            """
            )
            done = cur.fetchone()[0]
            remaining = total - done
            result.append({
                "task": "双源补充文档入口",
                "done": done,
                "total": total,
                "remaining": remaining,
                "pct": round(done / total * 100, 1) if total > 0 else 0,
            })

            # 3.7. SPA 无头浏览器爬取
            #    pending: needs_browser=TRUE
            #    done: spa_crawled_at IS NOT NULL（已处理）
            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE needs_browser = TRUE) AS pending,
                    COUNT(*) FILTER (WHERE spa_crawled_at IS NOT NULL) AS done
                FROM biz.doc_source_entry
                WHERE entry_type IN ('official_website', 'docs')
                  AND (needs_browser = TRUE OR spa_crawled_at IS NOT NULL)
                """
            )
            row = cur.fetchone()
            remaining = row[0]
            done = row[1]
            total = remaining + done
            result.append({
                "task": "B3 SPA 无头浏览器爬取",
                "done": done,
                "total": total,
                "remaining": remaining,
                "pct": round(done / total * 100, 1) if total > 0 else 0,
            })

            # 4. B2 深度文档发现（只统计可爬的 entry_type）
            cur.execute(
                """
                SELECT COUNT(*) FROM biz.doc_source_entry
                WHERE discovered_from NOT LIKE 'deep_crawl:%'
                  AND entry_type IN ('official_website', 'docs')
                """
            )
            total = cur.fetchone()[0]
            cur.execute(
                """
                SELECT COUNT(*) FROM biz.doc_source_entry
                WHERE discovered_from NOT LIKE 'deep_crawl:%'
                  AND entry_type IN ('official_website', 'docs')
                  AND deep_crawled_at IS NOT NULL
                """
            )
            done = cur.fetchone()[0]
            remaining = total - done
            result.append({
                "task": "B2 深度文档发现",
                "done": done,
                "total": total,
                "remaining": remaining,
                "pct": round(done / total * 100, 1) if total > 0 else 0,
            })

            # 5. B2 AI 噪声清理
            cur.execute(
                """
                SELECT COUNT(*) FROM biz.doc_source_entry
                WHERE discovered_from LIKE 'deep_crawl:%'
                """
            )
            total = cur.fetchone()[0]
            cur.execute(
                """
                SELECT COUNT(*) FROM biz.doc_source_entry
                WHERE discovered_from LIKE 'deep_crawl:%' AND ai_noise_checked_at IS NOT NULL
                """
            )
            done = cur.fetchone()[0]
            remaining = total - done
            result.append({
                "task": "B4 AI 噪声清理",
                "done": done,
                "total": total,
                "remaining": remaining,
                "pct": round(done / total * 100, 1) if total > 0 else 0,
            })

            # 6. 链上持仓快照
            #    候选集: 有合约地址的活跃资产
            #    done: 已有今日持仓快照的
            cur.execute(
                """
                SELECT COUNT(DISTINCT a.asset_id)
                FROM core.asset a
                INNER JOIN core.asset_contract_map m ON m.asset_id = a.asset_id
                WHERE a.status = 'active'
                """
            )
            total = cur.fetchone()[0]
            cur.execute(
                """
                SELECT COUNT(DISTINCT hs.asset_id)
                FROM biz.onchain_holder_snapshot hs
                WHERE hs.snapshot_date = CURRENT_DATE
                """
            )
            done = cur.fetchone()[0]
            remaining = total - done
            result.append({
                "task": "链上持仓快照采集",
                "done": done,
                "total": total,
                "remaining": remaining,
                "pct": round(done / total * 100, 1) if total > 0 else 0,
            })

            # 7. 大额转账监控告警（24h）
            cur.execute(
                "SELECT COUNT(*) FROM biz.onchain_transfer_log WHERE is_to_exchange = TRUE"
            )
            total_alerts = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM biz.onchain_transfer_log WHERE is_to_exchange = TRUE AND block_timestamp >= NOW() - INTERVAL '24 hours'"
            )
            alerts_24h = cur.fetchone()[0]
            result.append({
                "task": "大额转入交易所告警(24h)",
                "done": alerts_24h,
                "total": total_alerts,
                "remaining": total_alerts - alerts_24h,
                "pct": 0,  # 累计型告警，不展示百分比
            })

    return result


def search_assets(query: str, limit: int = 20) -> list[dict]:
    """按 symbol / name / 合约地址搜索资产，用于下拉自动补全。
    优先查 core.asset，无结果时从 src_cmc 回退并自动入库。

    合约地址匹配：EVM（0x 开头）大小写不敏感（含部分匹配）；非 EVM（如 Solana
    base58）精确匹配（大小写敏感）。
    """
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.asset_id, a.canonical_symbol, a.canonical_name, a.asset_type,
                       cb.cmc_id, a.primary_sector
                FROM core.asset a
                LEFT JOIN biz.coin_basic cb ON cb.asset_id = a.asset_id
                WHERE a.canonical_symbol ILIKE %s
                   OR a.canonical_name ILIKE %s
                   OR EXISTS (
                       SELECT 1 FROM core.asset_contract ac
                       WHERE ac.asset_id = a.asset_id
                         AND (
                             (LEFT(ac.contract_address, 2) = '0x' AND LOWER(ac.contract_address) LIKE LOWER(%s))
                             OR (LEFT(ac.contract_address, 2) <> '0x' AND ac.contract_address = %s)
                         )
                   )
                ORDER BY
                    CASE
                        WHEN EXISTS (
                            SELECT 1 FROM core.asset_contract ac
                            WHERE ac.asset_id = a.asset_id
                              AND (
                                  (LEFT(ac.contract_address, 2) = '0x' AND LOWER(ac.contract_address) = LOWER(%s))
                                  OR (LEFT(ac.contract_address, 2) <> '0x' AND ac.contract_address = %s)
                              )
                        ) THEN 0
                        WHEN a.canonical_symbol = UPPER(%s) THEN 1
                        WHEN a.canonical_symbol ILIKE %s THEN 2
                        ELSE 3
                    END,
                    a.canonical_symbol
                LIMIT %s
                """,
                (f"%{query}%", f"%{query}%", f"%{query}%", query, query, query, query, f"{query}%", limit),
            )
            rows = cur.fetchall()

            if rows:
                # 补充链/合约信息（每资产优先 primary 合约），用于区分同名币
                asset_ids = [row[0] for row in rows]
                cur.execute(
                    """
                    SELECT asset_id, chain, contract_address
                    FROM core.asset_contract
                    WHERE asset_id = ANY(%s)
                    ORDER BY asset_id, is_primary DESC, contract_id
                    """,
                    (asset_ids,),
                )
                contract_map = {}
                for aid, chain, addr in cur.fetchall():
                    if aid not in contract_map:
                        contract_map[aid] = (chain, addr)

                return [
                    {
                        "asset_id": row[0],
                        "symbol": row[1],
                        "name": row[2],
                        "type": row[3],
                        "cmc_id": row[4],
                        "sector": row[5] or "other",
                        "sector_label": SECTOR_LABELS.get(row[5] or "other", row[5] or "other"),
                        "chain": contract_map.get(row[0], (None, None))[0],
                        "contract": contract_map.get(row[0], (None, None))[1],
                    }
                    for row in rows
                ]

        # ── 回退：从 src_cmc 搜索并自动入库 ──
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.cmc_id, m.symbol, m.name, m.slug, m.platform_name,
                       i.category_hint
                FROM src_cmc.cmc_asset_map m
                LEFT JOIN src_cmc.cmc_asset_info i ON i.cmc_id = m.cmc_id
                WHERE (UPPER(m.symbol) = UPPER(%s) OR m.name ILIKE %s)
                  AND NOT EXISTS (
                      SELECT 1 FROM core.asset_source_map asm
                      WHERE asm.source_code = 'cmc'
                        AND asm.source_asset_key = m.cmc_id::text
                  )
                ORDER BY
                    CASE WHEN UPPER(m.symbol) = UPPER(%s) THEN 0 ELSE 1 END,
                    m.cmc_id
                LIMIT %s
                """,
                (query, f"%{query}%", query, limit),
            )
            cmc_rows = cur.fetchall()

            if not cmc_rows:
                return []

        # 自动入库
        results = []
        with conn.cursor() as cur:
            for row in cmc_rows:
                cmc_id = row[0]
                symbol = row[1]
                name = row[2]
                slug = row[3] or ""
                platform_name = row[4]
                category_hint = row[5]

                # 判断 asset_type：有 platform 是 token，否则 coin
                asset_type = "token" if platform_name else "coin"
                # meme 检测
                hint = (category_hint or "").strip().lower()
                if "meme" in hint:
                    asset_type = "meme"
                if "stablecoin" in hint:
                    asset_type = "stablecoin"

                try:
                    # 先查是否已有同 symbol 的资产
                    cur.execute(
                        "SELECT asset_id FROM core.asset WHERE canonical_symbol = %s",
                        (symbol,),
                    )
                    existing = cur.fetchone()
                    if existing:
                        asset_id = existing[0]
                    else:
                        # 插入 core.asset
                        cur.execute(
                            """INSERT INTO core.asset (canonical_symbol, canonical_name, asset_type, status)
                               VALUES (%s, %s, %s, 'active')
                               RETURNING asset_id""",
                            (symbol, name, asset_type),
                        )
                        asset_id = cur.fetchone()[0]

                    # 插入 source_map
                    cur.execute(
                        """INSERT INTO core.asset_source_map
                           (asset_id, source_code, source_asset_key, match_status,
                            match_method, match_confidence, is_primary, verified_by, updated_at)
                           VALUES (%s, 'cmc', %s, 'confirmed', 'search_onboard', 1.0, false, 'workbench', NOW())
                           ON CONFLICT (source_code, source_asset_key) DO NOTHING""",
                        (asset_id, str(cmc_id)),
                    )
                    conn.commit()

                    results.append({
                        "asset_id": asset_id,
                        "symbol": symbol,
                        "name": name,
                        "type": asset_type,
                        "cmc_id": cmc_id,
                        "sector": None,
                        "sector_label": None,
                        "chain": None,
                        "contract": None,
                    })
                except Exception:
                    conn.rollback()
                    continue

        return results


def get_asset_materials(asset_id: int) -> dict:
    """获取某个币种的全部投研资料，按类型分组。"""
    with get_db() as conn:
        with conn.cursor() as cur:
            # 基础信息
            cur.execute(
                "SELECT canonical_symbol, canonical_name, asset_type FROM core.asset WHERE asset_id = %s",
                (asset_id,),
            )
            asset_row = cur.fetchone()
            if not asset_row:
                return {}

            result = {
                "asset_id": asset_id,
                "symbol": asset_row[0],
                "name": asset_row[1],
                "type": asset_row[2],
                "doc_source_entries": [],
                "doc_assets": [],
                "research_urls": [],
                "github_activity": [],
            }

            # 文档入口
            cur.execute(
                """
                SELECT entry_id, source_code, entry_type, entry_url, discovered_from,
                       is_primary, deep_crawled_at
                FROM biz.doc_source_entry
                WHERE asset_id = %s AND entity_type = 'asset'
                ORDER BY
                    CASE entry_type
                        WHEN 'official_website' THEN 1
                        WHEN 'docs' THEN 2
                        WHEN 'docs_portal' THEN 3
                        WHEN 'github' THEN 4
                        WHEN 'medium' THEN 5
                        ELSE 6
                    END, entry_id
                """,
                (asset_id,),
            )
            result["doc_source_entries"] = [
                {
                    "entry_id": r[0],
                    "source": r[1],
                    "entry_type": r[2],
                    "url": r[3],
                    "discovered_from": r[4],
                    "is_primary": r[5],
                    "deep_crawled": r[6] is not None,
                }
                for r in cur.fetchall()
            ]

            # 文档文件
            cur.execute(
                """
                SELECT doc_id, doc_type, source_url, resolved_url, file_name,
                       mime_type, file_size_bytes, storage_path,
                       parse_status, last_seen_at
                FROM biz.doc_asset
                WHERE asset_id = %s
                ORDER BY
                    CASE doc_type
                        WHEN 'whitepaper' THEN 1
                        WHEN 'tokenomics' THEN 2
                        WHEN 'audit' THEN 3
                        WHEN 'docs' THEN 4
                        ELSE 5
                    END, doc_id
                """,
                (asset_id,),
            )
            result["doc_assets"] = [
                {
                    "doc_id": r[0],
                    "doc_type": r[1],
                    "source_url": r[2],
                    "resolved_url": r[3],
                    "file_name": r[4],
                    "mime_type": r[5],
                    "file_size_bytes": r[6],
                    "storage_path": r[7],
                    "parse_status": r[8],
                    "last_seen_at": str(r[9]) if r[9] else None,
                }
                for r in cur.fetchall()
            ]

            # 投研链接精选
            cur.execute(
                """
                SELECT url_id, url, category, doc_type, file_name, mime_type,
                       health_status, relevance_score, ai_reason, is_selected
                FROM biz.research_url
                WHERE asset_id = %s
                ORDER BY is_selected DESC, relevance_score DESC NULLS LAST, url_id
                """,
                (asset_id,),
            )
            result["research_urls"] = [
                {
                    "url_id": r[0],
                    "url": r[1],
                    "category": r[2],
                    "doc_type": r[3],
                    "file_name": r[4],
                    "mime_type": r[5],
                    "health_status": r[6],
                    "relevance_score": float(r[7]) if r[7] is not None else None,
                    "ai_reason": r[8],
                    "is_selected": r[9],
                }
                for r in cur.fetchall()
            ]

            # GitHub 开发活跃度
            cur.execute(
                """
                SELECT owner_login, repo_name, stars_count, forks_count,
                       open_issues_count, language, topics, license_name,
                       archived, total_commits_52w, contributor_count_52w,
                       pushed_at, fetched_at
                FROM biz.github_repo_activity
                WHERE owner_login || '/' || repo_name IN (
                    SELECT DISTINCT
                        SUBSTRING(entry_url FROM 'github\\.com/([^/]+/[^/\\s#?]+)')
                    FROM biz.doc_source_entry
                    WHERE asset_id = %s
                      AND entry_type = 'github'
                      AND entry_url LIKE '%%github.com%%'
                )
                ORDER BY stars_count DESC NULLS LAST
                """,
                (asset_id,),
            )
            result["github_activity"] = [
                {
                    "owner": r[0],
                    "repo": r[1],
                    "stars": r[2],
                    "forks": r[3],
                    "open_issues": r[4],
                    "language": r[5],
                    "topics": r[6],
                    "license": r[7],
                    "archived": r[8],
                    "commits_52w": r[9],
                    "contributors_52w": r[10],
                    "pushed_at": str(r[11]) if r[11] else None,
                    "fetched_at": str(r[12]) if r[12] else None,
                }
                for r in cur.fetchall()
            ]

            # 统计汇总
            result["stats"] = {
                "total_entries": len(result["doc_source_entries"]),
                "total_docs": len(result["doc_assets"]),
                "total_research_urls": len(result["research_urls"]),
                "total_github": len(result["github_activity"]),
                "by_entry_type": {},
                "by_doc_type": {},
            }
            for e in result["doc_source_entries"]:
                t = e["entry_type"]
                result["stats"]["by_entry_type"][t] = result["stats"]["by_entry_type"].get(t, 0) + 1
            for d in result["doc_assets"]:
                t = d["doc_type"] or "other"
                result["stats"]["by_doc_type"][t] = result["stats"]["by_doc_type"].get(t, 0) + 1

    return result


def get_asset_tokenomics(asset_id: int) -> dict | None:
    """获取资产的代币经济学结构化数据（含 tokenomics.com 的收入/估值子板块）。"""
    with get_db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """
                SELECT t.total_supply, t.max_supply, t.circulating_supply,
                       t.buy_tax_pct, t.sell_tax_pct, t.tax_info,
                       t.contract_renounced, t.lp_locked, t.lp_lock_info,
                       t.allocation_json, t.burn_info, t.emission_schedule,
                       t.inflation_info, t.governance_info, t.utility_info,
                       t.confidence, t.extraction_notes,
                       t.source_urls, t.chart_images, t.created_at, t.updated_at,
                       u.revenue_json, u.valuation_json, u.overview_json
                FROM biz.asset_tokenomics t
                LEFT JOIN biz.asset_token_unlocks u ON u.asset_id = t.asset_id
                WHERE t.asset_id = %s
                """,
                (asset_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "total_supply": row["total_supply"],
                "max_supply": row["max_supply"],
                "circulating_supply": row["circulating_supply"],
                "buy_tax_pct": float(row["buy_tax_pct"]) if row["buy_tax_pct"] is not None else None,
                "sell_tax_pct": float(row["sell_tax_pct"]) if row["sell_tax_pct"] is not None else None,
                "tax_info": row["tax_info"],
                "contract_renounced": row["contract_renounced"],
                "lp_locked": row["lp_locked"],
                "lp_lock_info": row["lp_lock_info"],
                "allocation": row["allocation_json"],
                "burn_info": row["burn_info"],
                "emission_schedule": row["emission_schedule"],
                "inflation_info": row["inflation_info"],
                "governance_info": row["governance_info"],
                "utility_info": row["utility_info"],
                "confidence": float(row["confidence"]) if row["confidence"] is not None else None,
                "extraction_notes": row["extraction_notes"],
                "source_urls": row["source_urls"],
                "chart_images": row["chart_images"],
                "created_at": str(row["created_at"]) if row["created_at"] else None,
                "updated_at": str(row["updated_at"]) if row["updated_at"] else None,
                "revenue": row["revenue_json"] or {},
                "valuation": row["valuation_json"] or {},
                "overview": row["overview_json"] or {},
            }


def query_tokenomics(asset_id: int, force: bool = False, log=None) -> dict:
    """按需提取代币经济学数据。

    优先 tokenomics.com 结构化数据；未命中时返回 needs_url，由前端询问用户
    提供网址或改用 AI 测算。
    """
    def _emit(msg: str) -> None:
        if log:
            log(msg)

    scripts_bin = _get_scripts_bin()
    script = str(scripts_bin / "phase_c_extract_tokenomics.py")
    # 单币调用始终强制覆盖：用户点「提取代币经济学」即为手动重提，
    # 否则脚本会在「已有数据」时提前 return，不输出 not_found/ok JSON，
    # 导致 _extract_json_output 解析失败。
    cmd = [sys.executable, "-u", script, "--asset-id", str(asset_id), "--force"]

    _emit("开始提取代币经济学数据（tokenomics.com 优先）...")
    stdout, returncode = _run_with_log(cmd, str(scripts_bin), 300, log=log)

    if returncode == -1:
        return {"ok": False, "error": "代币经济学提取超时（300秒），请稍后重试"}

    data = _extract_json_output(stdout)
    if data is None:
        return {"ok": False, "error": (stdout.strip() or "无输出")[-500:]}

    if data.get("status") == "ok":
        _emit("代币经济学提取成功")
        return {"ok": True, "data": get_asset_tokenomics(asset_id) or {}}

    if data.get("status") == "not_found":
        _emit("tokenomics.com 未收录，等待用户提供网址或改用 AI 测算")
        return {
            "ok": False,
            "needs_url": True,
            "error": data.get("message", "tokenomics.com 未收录该代币"),
            "symbol": data.get("symbol"),
            "name": data.get("name"),
        }

    return {"ok": False, "error": data.get("message", "提取失败")}


def query_tokenomics_by_url(asset_id: int, url: str, log=None) -> dict:
    """按用户提供的网址抓取 tokenomics 数据（LLM 提取）。"""
    def _emit(msg: str) -> None:
        if log:
            log(msg)

    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "网址必须以 http:// 或 https:// 开头"}

    scripts_bin = _get_scripts_bin()
    script = str(scripts_bin / "phase_c_extract_tokenomics.py")
    cmd = [sys.executable, "-u", script, "--asset-id", str(asset_id),
           "--url", url, "--force"]

    _emit(f"按用户提供的网址抓取 tokenomics: {url}")
    stdout, returncode = _run_with_log(cmd, str(scripts_bin), 300, log=log)

    if returncode == -1:
        return {"ok": False, "error": "网址抓取超时（300秒）"}

    data = _extract_json_output(stdout)
    if data and data.get("status") == "ok":
        _emit("网址抓取 tokenomics 成功")
        return {"ok": True, "data": get_asset_tokenomics(asset_id) or {}}

    err = (data or {}).get("message") if data else (stdout.strip() or "无输出")
    return {"ok": False, "error": err[-500:]}


def query_tokenomics_ai(asset_id: int, log=None) -> dict:
    """用户未提供网址时，直接触发 AI 测算（文档 + LLM）。"""
    def _emit(msg: str) -> None:
        if log:
            log(msg)

    _emit("用户未提供网址，改用 AI 测算代币经济学（文档 + LLM）")
    scripts_bin = _get_scripts_bin()
    script = str(scripts_bin / "phase_c_extract_tokenomics.py")
    cmd = [sys.executable, "-u", script, "--asset-id", str(asset_id),
           "--ai", "--force"]

    stdout, returncode = _run_with_log(cmd, str(scripts_bin), 600, log=log)

    if returncode == -1:
        return {"ok": False, "error": "AI 测算超时（600秒）"}

    data = _extract_json_output(stdout)
    if data and data.get("status") == "ok":
        _emit("AI 测算代币经济学成功")
        return {"ok": True, "data": get_asset_tokenomics(asset_id) or {}}

    err = (data or {}).get("message") if data else (stdout.strip() or "无输出")
    return {"ok": False, "error": err[-500:]}


def reset_deep_crawl(asset_id: int) -> dict:
    """重置指定资产的 deep_crawled_at，允许 B2 重新爬取。"""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE biz.doc_source_entry SET deep_crawled_at = NULL WHERE asset_id = %s AND deep_crawled_at IS NOT NULL",
                (asset_id,),
            )
            affected = cur.rowcount
    return {"affected": affected}


def reset_full_crawl(asset_id: int) -> dict:
    """完整重新爬取前置清理：
    1. 删除该资产所有爬取来源链接（deep_crawl / spa_browser_crawl），
       仅保留 API 来源的种子链接（cmc/cg/dl/dexscreener/binance/manual）。
    2. 重置剩余种子链接的 deep_crawled_at / needs_browser / spa_retry_count，
       允许 B2 从第一层重新爬取，并让 B3 重新尝试渲染 SPA 页面。
    """
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM biz.doc_source_entry WHERE asset_id = %s "
                "AND (discovered_from LIKE %s OR discovered_from LIKE %s)",
                (asset_id, "deep_crawl:%", "spa_browser_crawl:%"),
            )
            deleted = cur.rowcount

            cur.execute(
                "UPDATE biz.doc_source_entry SET deep_crawled_at = NULL, needs_browser = FALSE, "
                "spa_retry_count = 0, spa_crawled_at = NULL "
                "WHERE asset_id = %s AND deep_crawled_at IS NOT NULL",
                (asset_id,),
            )
            reset = cur.rowcount
        conn.commit()
    return {"deleted_crawl_links": deleted, "reset_seed_links": reset}


def add_manual_entry(asset_id: int, entry_url: str) -> dict:
    """手动为资产添加一个文档入口（官网链接）。"""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO biz.doc_source_entry (
                    entity_type, asset_id, source_code, entry_type, entry_url,
                    discovered_from, is_primary, updated_at
                ) VALUES (
                    'asset', %s, 'manual', 'official_website', %s,
                    'manual_input', TRUE, NOW()
                )
                ON CONFLICT (entity_type, COALESCE(asset_id, -1), COALESCE(protocol_id, -1), entry_url) DO NOTHING
                RETURNING entry_id
                """,
                (asset_id, entry_url),
            )
            row = cur.fetchone()
        conn.commit()
    return {"entry_id": row[0] if row else None, "url": entry_url}


# doc_source_entry 允许的 entry_type（对齐 taxonomy.SOURCE_TYPES 与数据库 CHECK 约束）
VALID_ENTRY_TYPES = {
    "official_website", "docs", "docs_portal", "whitepaper_page",
    "github", "medium", "announcement", "twitter", "telegram",
    "reddit", "facebook", "explorer", "social", "other",
}


def update_entry_type(entry_id: int, entry_type: str) -> dict:
    """修改某条 doc_source_entry 的来源类型（entry_type）。"""
    if entry_type not in VALID_ENTRY_TYPES:
        return {"ok": False, "error": f"非法 entry_type: {entry_type}"}
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE biz.doc_source_entry SET entry_type = %s, updated_at = NOW() WHERE entry_id = %s",
                (entry_type, entry_id),
            )
            affected = cur.rowcount
    if affected == 0:
        return {"ok": False, "error": "条目不存在"}
    return {"ok": True, "affected": affected, "entry_type": entry_type}


def _extract_url_title(url: str) -> str:
    """从 URL 路径最后一段提取粗略标题（供 AI 分类参考）。"""
    from urllib.parse import urlparse, unquote

    path = unquote(urlparse(url).path)
    name = path.rsplit("/", 1)[-1] if "/" in path else path
    if "." in name:
        name = name.rsplit(".", 1)[0]
    return name.replace("-", " ").replace("_", " ").strip()[:200]


def ai_classify_asset(asset_id: int, log=None) -> dict:
    """对单个资产的「未精确分类」链接做 AI 内容主题分类。

    范围：classify_method='default'（content_topics=['other']，规则/元数据未判出）的
    doc_source_entry。抓正文后用 LLM 判 content_topics，回写 classify_method='ai_content'。
    """

    def _emit(msg: str) -> None:
        if log:
            try:
                log(msg)
            except Exception:
                pass

    from crypto_research.config import get_settings
    from crypto_research.clients.llm_client import LLMClient

    settings = get_settings(require_database=True)
    llm = LLMClient(settings, rpm=30)
    if not llm.is_available():
        return {"ok": False, "error": "未配置 LLM（OPENAI_API_KEY / ARK_*），无法做 AI 精确分类"}

    with get_db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """
                SELECT entry_id, entry_url
                FROM biz.doc_source_entry
                WHERE asset_id = %s AND entity_type = 'asset'
                  AND classify_method = 'default'
                ORDER BY entry_id
                """,
                (asset_id,),
            )
            rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        _emit("[AI精确分类] 该币无待分类链接（classify_method=default）")
        return {"ok": True, "data": {"total": 0, "classified": 0, "failed": 0}}

    _emit(f"[AI精确分类] 待分类 {len(rows)} 条")

    items = []
    texts = []
    for r in rows:
        text = _fetch_url_text(r["entry_url"])
        texts.append(text)
        items.append({
            "entry_id": str(r["entry_id"]),
            "url": r["entry_url"],
            "title": _extract_url_title(r["entry_url"]),
            "text": text,
        })
        _emit(f"[AI精确分类] 抓正文 {len(text)} 字: {r['entry_url'][:80]}")

    results = llm.batch_classify_content_topics(items)

    classified = 0
    failed = 0
    no_content = 0
    for r, t, res in zip(rows, texts, results):
        if not t:
            # 无正文（JS 渲染/反爬）：不靠 URL 硬猜，标 needs_browser 交 SPA 爬取重抓
            no_content += 1
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE biz.doc_source_entry
                        SET classify_method = 'ai_failed', classify_error = '无正文（JS渲染/反爬）',
                            classify_reason = NULL, needs_browser = TRUE
                        WHERE entry_id = %s
                        """,
                        (r["entry_id"],),
                    )
            _emit(f"[AI精确分类] 无正文: {r['entry_url'][:60]} (标 needs_browser)")
            continue
        topics = res.get("content_topics") or []
        conf = float(res.get("confidence") or 0.0)
        reason = (res.get("reason") or "").strip()
        if topics and conf > 0:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE biz.doc_source_entry
                        SET content_topics = %s, classify_method = 'ai_content',
                            classify_confidence = %s, classify_reason = %s, classify_error = NULL
                        WHERE entry_id = %s
                        """,
                        (topics, conf, reason, r["entry_id"]),
                    )
            classified += 1
            _emit(f"[AI精确分类] {r['entry_url'][:60]} -> {topics} (conf={conf:.2f})")
        else:
            failed += 1
            _emit(f"[AI精确分类] 失败: {r['entry_url'][:60]} ({reason or '无结果'})")

    return {"ok": True, "data": {"total": len(rows), "classified": classified, "failed": failed, "no_content": no_content}}


# ── DexScreener 辅助添加 ──


def search_dexscreener(query: str) -> list[dict]:
    """搜索 DexScreener，返回去重后的代币列表。"""
    url = f"https://api.dexscreener.com/latest/dex/search?q={urllib.parse.quote(query)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        return []

    pairs = data.get("pairs") or []
    if not pairs:
        return []

    # 按 baseToken.address 去重，保留流动性最高的 pair
    seen = {}
    for p in pairs:
        bt = p.get("baseToken") or {}
        addr = (bt.get("address") or "").lower()
        if not addr:
            continue
        liq = float(p.get("liquidity", {}).get("usd", 0) or 0)
        if addr not in seen or liq > seen[addr]["liquidity_usd"]:
            info = p.get("info") or {}
            websites = []
            socials = []
            for w in info.get("websites") or []:
                if w.get("url"):
                    websites.append({"label": w.get("label", ""), "url": w["url"]})
            for s in info.get("socials") or []:
                if s.get("url"):
                    socials.append({"type": s.get("type", ""), "url": s["url"]})

            seen[addr] = {
                "name": bt.get("name", ""),
                "symbol": bt.get("symbol", ""),
                "address": addr,
                "chain_id": p.get("chainId", ""),
                "dex_id": p.get("dexId", ""),
                "price_usd": p.get("priceUsd", ""),
                "liquidity_usd": liq,
                "fdv": p.get("fdv", 0),
                "volume_24h": float(p.get("volume", {}).get("h24", 0) or 0),
                "dex_url": p.get("url", ""),
                "websites": websites,
                "socials": socials,
            }

    # 按流动性降序排列
    results = sorted(seen.values(), key=lambda x: x["liquidity_usd"], reverse=True)
    return results[:10]


def create_asset_with_links(
    symbol: str,
    name: str,
    asset_type: str = "token",
    links: list[dict] | None = None,
) -> dict:
    """创建资产并批量添加文档链接。

    links 格式: [{"entry_type": "official_website", "entry_url": "https://..."}, ...]
    """
    with get_db() as conn:
        with conn.cursor() as cur:
            # 检查是否已存在同名代币
            cur.execute(
                "SELECT asset_id FROM core.asset WHERE canonical_symbol = %s AND canonical_name = %s",
                (symbol.upper(), name),
            )
            existing = cur.fetchone()
            if existing:
                return {"asset_id": existing[0], "created": False, "message": "资产已存在"}

            cur.execute(
                """INSERT INTO core.asset (canonical_symbol, canonical_name, asset_type, status)
                   VALUES (%s, %s, %s, 'active')
                   RETURNING asset_id""",
                (symbol.upper(), name, asset_type),
            )
            asset_id = cur.fetchone()[0]

            # 添加文档链接
            entry_count = 0
            if links:
                for link in links:
                    entry_type = link.get("entry_type", "official_website")
                    entry_url = link.get("entry_url", "").strip()
                    if not entry_url or not entry_url.startswith("http"):
                        continue
                    if entry_type not in (
                        "official_website", "docs", "github", "medium",
                        "docs_portal", "whitepaper_page", "other",
                    ):
                        entry_type = "other"
                    cur.execute(
                        """INSERT INTO biz.doc_source_entry (
                               entity_type, asset_id, source_code, entry_type, entry_url,
                               discovered_from, is_primary, updated_at
                           ) VALUES (
                               'asset', %s, 'manual', %s, %s,
                               'dexscreener', FALSE, NOW()
                           )
                           ON CONFLICT (entity_type, COALESCE(asset_id, -1), COALESCE(protocol_id, -1), entry_url)
                           DO NOTHING""",
                        (asset_id, entry_type, entry_url),
                    )
                    if cur.rowcount:
                        entry_count += 1

        conn.commit()
    return {
        "asset_id": asset_id,
        "created": True,
        "entry_count": entry_count,
        "symbol": symbol.upper(),
        "name": name,
    }


# ── NotebookLM 投研精选 ──


def get_notebooklm_links(asset_id: int) -> dict:
    """获取已缓存的 NotebookLM 精选链接。"""
    with get_db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT entry_url FROM biz.doc_source_notebooklm WHERE asset_id = %s ORDER BY ai_rank",
                (asset_id,),
            )
            urls = [r["entry_url"] for r in cur.fetchall()]
    return {"ok": True, "asset_id": asset_id, "count": len(urls), "urls": urls}


def curate_notebooklm(asset_id: int, force: bool = False, log=None) -> dict:
    """触发 NotebookLM 精选生成（配额粗筛 + AI 排序）。"""

    def _emit(msg: str) -> None:
        if log:
            try:
                log(msg)
            except Exception:
                pass

    scripts_bin = _get_scripts_bin()
    script = str(scripts_bin / "curate_notebooklm.py")
    script_dir = str(scripts_bin)
    cmd = [
        sys.executable, "-u", script,
        "--asset-id", str(asset_id),
        "--top-n", "50",
    ]
    if force:
        cmd.append("--force")

    _emit(f"[NotebookLM] 开始精选: asset {asset_id}")
    _emit(f"[NotebookLM] 执行: {' '.join(cmd)}")
    output, returncode = _run_with_log(cmd, script_dir, 180, log=log)

    if returncode == -1:
        return {"ok": False, "error": "NotebookLM 精选超时（180秒）"}

    json_line = _extract_json_output(output)
    if json_line:
        urls = json_line.get("urls") or []
        count = json_line.get("count") if json_line.get("count") is not None else len(urls)
        _emit(f"[NotebookLM] 精选完成: status={json_line.get('status')}, 链接数={count}")
        return {"ok": True, "data": json_line}

    err = _extract_chain_error(output, "", returncode) or f"exit code {returncode}"
    return {"ok": False, "error": err[:500]}


# ── 一键投研（NotebookLM 风格） ──


def _ensure_research_tables(conn) -> None:
    """确保一键投研笔记本相关表存在（新环境自动建表）。"""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS biz.research_notebook (
                notebook_id    SERIAL PRIMARY KEY,
                asset_id       INTEGER NOT NULL,
                title          TEXT NOT NULL DEFAULT '',
                snapshot_json  JSONB NOT NULL DEFAULT '{}'::jsonb,
                missing_json   JSONB NOT NULL DEFAULT '[]'::jsonb,
                created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT uq_research_notebook_asset UNIQUE (asset_id),
                CONSTRAINT fk_research_notebook_asset
                    FOREIGN KEY (asset_id) REFERENCES core.asset(asset_id)
                    ON DELETE CASCADE
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS biz.research_message (
                message_id     SERIAL PRIMARY KEY,
                notebook_id    INTEGER NOT NULL,
                role           TEXT NOT NULL,
                content        TEXT NOT NULL,
                citations_json JSONB,
                created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT fk_research_message_notebook
                    FOREIGN KEY (notebook_id) REFERENCES biz.research_notebook(notebook_id)
                    ON DELETE CASCADE
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_research_message_notebook
                ON biz.research_message (notebook_id, created_at)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS biz.research_thesis (
                thesis_id         SERIAL PRIMARY KEY,
                asset_id          INTEGER NOT NULL,
                stance            TEXT NOT NULL,            -- bullish / bearish / neutral
                conviction        TEXT NOT NULL DEFAULT 'medium',  -- high / medium / low
                thesis_json       JSONB NOT NULL DEFAULT '[]'::jsonb,  -- 核心论点列表（带引用）
                key_metrics_json  JSONB,                    -- 关键指标快照
                risks_json        JSONB,                    -- 风险点列表
                catalysts_json    JSONB,                    -- 催化剂 + 时间点
                source_notebook_id INTEGER REFERENCES biz.research_notebook(notebook_id) ON DELETE SET NULL,
                created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT fk_research_thesis_asset
                    FOREIGN KEY (asset_id) REFERENCES core.asset(asset_id)
                    ON DELETE CASCADE
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_research_thesis_asset
                ON biz.research_thesis (asset_id, updated_at DESC)
        """)


def _build_doc_sources(doc_source_entries, research_urls, doc_assets, notebooklm_urls) -> list[dict]:
    """把各来源的文档链接合并去重成统一资料清单。"""
    sources = []
    seen = set()

    def _add(entry_type: str, url, title=None, topics=None):
        url = (url or "").strip()
        if not url or url in seen:
            return
        seen.add(url)
        sources.append({"type": entry_type, "url": url, "title": title or url,
                        "topics": list(topics or [])})

    for e in doc_source_entries:
        _add(e["entry_type"], e["url"], topics=e.get("content_topics"))
    for r in research_urls:
        _add(r.get("category") or "research", r["url"], title=r.get("doc_type") or None,
             topics=r.get("content_topics"))
    for d in doc_assets:
        _add("doc_file", d.get("source_url") or d.get("resolved_url"),
             title=d.get("file_name") or d.get("doc_type"), topics=d.get("content_topics"))
        if d.get("resolved_url") and d.get("resolved_url") != d.get("source_url"):
            _add("doc_file", d["resolved_url"], title=d.get("file_name") or d.get("doc_type"),
                 topics=d.get("content_topics"))
    for u in notebooklm_urls:
        _add("notebooklm", u)
    return sources


def get_asset_raises(asset_id: int) -> list[dict]:
    """读取已落库的融资轮次（团队/VC 投资人结构化数据，只读）。"""
    try:
        with get_db() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute(
                    """
                    SELECT protocol_name, round, raise_date, amount,
                           lead_investors, other_investors, valuation
                    FROM biz.asset_raises
                    WHERE asset_id = %s
                    ORDER BY raise_date
                    """,
                    (asset_id,),
                )
                return [dict(r) for r in cur.fetchall()]
    except psycopg.errors.UndefinedTable:
        return []


def get_asset_exchanges(asset_id: int) -> list[dict]:
    """读取 CMC 交易对快照，作为交易所上线信息的结构化来源（只读）。"""
    try:
        with get_db() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute(
                    """
                    SELECT DISTINCT mp.exchange_name, mp.market_pair,
                           mp.pair_base_symbol, mp.pair_quote_symbol
                    FROM src_cmc.cmc_market_pair_snapshot mp
                    INNER JOIN core.asset_source_map asm
                      ON asm.source_code = 'cmc' AND asm.source_asset_key = mp.cmc_id::text
                    WHERE asm.asset_id = %s
                    ORDER BY mp.exchange_name
                    """,
                    (asset_id,),
                )
                return [dict(r) for r in cur.fetchall()]
    except psycopg.errors.UndefinedTable:
        return []


def _collect_asset_snapshot(asset_id: int) -> dict | None:
    """收集一个代币的全部投研资料快照（文档入口/文件/精选/结构化数据/合约）。"""
    with get_db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT canonical_symbol, canonical_name, asset_type, primary_sector FROM core.asset WHERE asset_id = %s",
                (asset_id,),
            )
            asset = cur.fetchone()
            if not asset:
                return None

            cur.execute("""
                SELECT entry_id, source_code, entry_type, entry_url, discovered_from, is_primary, content_topics, published_at
                FROM biz.doc_source_entry
                WHERE asset_id = %s AND entity_type = 'asset'
                ORDER BY
                    CASE entry_type
                        WHEN 'whitepaper_page' THEN 1
                        WHEN 'official_website' THEN 2
                        WHEN 'docs' THEN 3
                        WHEN 'docs_portal' THEN 4
                        WHEN 'github' THEN 5
                        WHEN 'medium' THEN 6
                        ELSE 7
                    END, published_at DESC NULLS LAST, entry_id
            """, (asset_id,))
            doc_source_entries = [
                {
                    "entry_id": r["entry_id"],
                    "source": r["source_code"],
                    "entry_type": r["entry_type"],
                    "url": r["entry_url"],
                    "discovered_from": r["discovered_from"],
                    "is_primary": bool(r["is_primary"]),
                    "content_topics": r["content_topics"] or [],
                    "published_at": str(r["published_at"]) if r["published_at"] else None,
                }
                for r in cur.fetchall()
            ]

            cur.execute("""
                SELECT doc_id, doc_type, source_url, resolved_url, file_name, mime_type, parse_status, content_topics
                FROM biz.doc_asset WHERE asset_id = %s
                ORDER BY CASE doc_type WHEN 'whitepaper' THEN 1 WHEN 'tokenomics' THEN 2 WHEN 'audit' THEN 3 ELSE 4 END, doc_id
            """, (asset_id,))
            doc_assets = [
                {
                    "doc_id": r["doc_id"],
                    "doc_type": r["doc_type"],
                    "source_url": r["source_url"],
                    "resolved_url": r["resolved_url"],
                    "file_name": r["file_name"],
                    "mime_type": r["mime_type"],
                    "parse_status": r["parse_status"],
                    "content_topics": r["content_topics"] or [],
                }
                for r in cur.fetchall()
            ]

            cur.execute("""
                SELECT url_id, url, category, doc_type, relevance_score, ai_reason, is_selected, content_topics
                FROM biz.research_url WHERE asset_id = %s
                ORDER BY is_selected DESC, relevance_score DESC NULLS LAST, url_id
            """, (asset_id,))
            research_urls = [
                {
                    "url_id": r["url_id"],
                    "url": r["url"],
                    "category": r["category"],
                    "doc_type": r["doc_type"],
                    "relevance_score": float(r["relevance_score"]) if r["relevance_score"] is not None else None,
                    "ai_reason": r["ai_reason"],
                    "is_selected": bool(r["is_selected"]),
                    "content_topics": r["content_topics"] or [],
                }
                for r in cur.fetchall()
            ]

            cur.execute(
                "SELECT entry_url FROM biz.doc_source_notebooklm WHERE asset_id = %s ORDER BY ai_rank",
                (asset_id,),
            )
            notebooklm_urls = [r["entry_url"] for r in cur.fetchall()]

            cur.execute("""
                SELECT chain, contract_address, decimals, is_primary
                FROM core.asset_contract WHERE asset_id = %s
                ORDER BY is_primary DESC, chain
            """, (asset_id,))
            contracts = [
                {
                    "chain": r["chain"],
                    "contract_address": r["contract_address"],
                    "decimals": r["decimals"],
                    "is_primary": bool(r["is_primary"]),
                }
                for r in cur.fetchall()
            ]

    # 结构化数据（复用现有只读查询；缺失返回 None / 空结构）
    tokenomics = get_asset_tokenomics(asset_id)
    onchain = get_onchain_holder_snapshot(asset_id)
    social = get_asset_social_heat(asset_id)
    unlocks = get_asset_unlocks(asset_id)
    raises = get_asset_raises(asset_id)
    exchanges = get_asset_exchanges(asset_id)

    sources = _build_doc_sources(doc_source_entries, research_urls, doc_assets, notebooklm_urls)

    return {
        "asset_id": asset_id,
        "symbol": asset["canonical_symbol"],
        "name": asset["canonical_name"],
        "type": asset["asset_type"],
        "sector": asset["primary_sector"] or "other",
        "sources": sources,
        "structured": {
            "tokenomics": tokenomics,
            "onchain": onchain,
            "social": social,
            "unlocks": unlocks,
            "contracts": contracts,
            "raises": raises,
            "exchanges": exchanges,
        },
        "counts": {
            "doc_source_entries": len(doc_source_entries),
            "doc_assets": len(doc_assets),
            "research_urls": len(research_urls),
            "contracts": len(contracts),
            "doc_source_entry_types": [e["entry_type"] for e in doc_source_entries],
            "doc_asset_types": [d["doc_type"] for d in doc_assets],
            "research_categories": [r["category"] for r in research_urls],
        },
    }


# 完整投研资料类型清单（key 为后端唯一标识，label 为 UI 显示名，description 为说明）。
RESEARCH_MATERIAL_TYPES = [
    {"key": "official_website", "label": "官网", "description": "项目官方网站"},
    {"key": "whitepaper_docs", "label": "白皮书 / 文档", "description": "白皮书、官方技术文档、Gitbook"},
    {"key": "github_repo", "label": "GitHub仓库", "description": "合约、前端、SDK开源代码仓库"},
    {"key": "audit_report", "label": "审计报告", "description": "第三方安全审计PDF/链接，包含二次审计"},
    {"key": "tokenomics", "label": "代币经济学", "description": "代币分配、总供应量、通胀模型文档"},
    {"key": "onchain_holder_data", "label": "链上持仓数据", "description": "大户持仓、持有者分布、Top Holder"},
    {"key": "social_heat", "label": "社交热度", "description": "X、Discord、TG粉丝数量、活跃度指标"},
    {"key": "token_unlock_data", "label": "代币解锁数据", "description": "解锁时间表、TGE后释放、vesting资料"},
    {"key": "contract_address", "label": "合约地址", "description": "代币合约、代理合约，区分多链地址"},
    {"key": "tge_ido_info", "label": "TGE & IDO信息", "description": "TGE日期，IDO平台，公募/私募价格，轮次信息"},
    {"key": "lp_liquidity_info", "label": "LP流动性信息", "description": "LP合约地址、流动性锁仓、DEX交易对、深度"},
    {"key": "treasury_multisig", "label": "国库&多签钱包", "description": "国库地址、多签配置、国库资产与历史转账"},
    {"key": "team_vc", "label": "团队 & VC投资人资料", "description": "核心团队背景、投资机构、融资轮次记录"},
    {"key": "roadmap", "label": "Roadmap路线图", "description": "官方路线图，已完成/待完成里程碑"},
    {"key": "dao_governance", "label": "治理DAO资料", "description": "治理页面、提案记录、投票权重规则"},
    {"key": "bug_bounty", "label": "漏洞披露 & BugBounty", "description": "赏金计划，历史漏洞披露记录"},
    {"key": "exchange_listing", "label": "交易所上线信息", "description": "CEX、DEX上线交易对列表"},
    {"key": "competitor_material", "label": "竞品对比资料", "description": "同赛道竞品项目链接，用于横向投研对比"},
    {"key": "major_event_announcement", "label": "重大公告&事件", "description": "合约迁移、升级、品牌更名、风险公告"},
    {"key": "third_party_rating", "label": "第三方评级资料", "description": "DefiLlama、Tokenomist等第三方页面链接"},
    {"key": "onchain_abnormal_event", "label": "链上异常事件记录", "description": "大额异常转账、攻击事件、链上风险事件资料"},
]

# 后 12 类资料类型 → content_topics 内容主题的精确映射（取代早期基于 URL/标题的关键词猜测）。
_MATERIAL_TOPIC_MAP = {
    "tge_ido_info": ("tge_ido",),
    "lp_liquidity_info": ("lp_liquidity",),
    "treasury_multisig": ("treasury_multisig",),
    "team_vc": ("team_vc",),
    "roadmap": ("roadmap",),
    "dao_governance": ("dao_governance",),
    "bug_bounty": ("bug_bounty",),
    "exchange_listing": ("exchange_listing",),
    "competitor_material": ("competitor",),
    "major_event_announcement": ("major_event", "announcement"),
    "third_party_rating": ("third_party_rating",),
    "onchain_abnormal_event": ("onchain_abnormal",),
}


def _collect_material_links(sources: list[dict]) -> dict[str, list[dict]]:
    """把引用来源按投研资料类型分组，供投研页「资料完整性」清单点击展开查看链接。

    前 5 类（官网/文档/GitHub/审计/代币经济学）用来源类型 + 内容主题匹配；
    后 12 类复用 _MATERIAL_TOPIC_MAP 的内容主题映射。无 url 的（结构化数据）不纳入。
    """
    links: dict[str, list[dict]] = {}

    def _add(key: str, s: dict):
        url = s.get("url")
        if not url:
            return
        links.setdefault(key, []).append({
            "title": s.get("title") or url,
            "url": url,
        })

    for s in sources:
        topics = set(s.get("topics") or [])
        stype = s.get("type") or ""

        if stype == "official_website":
            _add("official_website", s)
        if stype in {"whitepaper_page", "docs", "docs_portal"} or topics & {"whitepaper", "docs", "tokenomics"}:
            _add("whitepaper_docs", s)
        if stype == "github":
            _add("github_repo", s)
        if "audit" in topics:
            _add("audit_report", s)
        if "tokenomics" in topics:
            _add("tokenomics", s)
        for key, wanted in _MATERIAL_TOPIC_MAP.items():
            if set(wanted) & topics:
                _add(key, s)

    return links


def _compute_missing_materials(snapshot: dict) -> list[dict]:
    """按完整投研清单判断每类资料的收集状态。

    前 9 类用结构化数据/来源类型精确判定；后 12 类用 content_topics
    内容主题精确判定（不再依赖 URL/标题关键词猜测）。
    """
    counts = snapshot.get("counts") or {}
    structured = snapshot.get("structured") or {}
    entry_types = set(counts.get("doc_source_entry_types") or [])
    asset_types = set(counts.get("doc_asset_types") or [])
    sources = snapshot.get("sources") or []
    tokenomics = structured.get("tokenomics") or {}
    onchain = structured.get("onchain") or {}
    unlocks = structured.get("unlocks")

    topics: set[str] = set()
    for s in sources:
        for t in (s.get("topics") or []):
            topics.add(t)

    present: dict[str, bool] = {
        "official_website": "official_website" in entry_types,
        "whitepaper_docs": bool({"whitepaper_page", "docs", "docs_portal"} & entry_types)
        or bool({"whitepaper", "tokenomics", "docs"} & asset_types)
        or bool({"whitepaper", "docs"} & topics),
        "github_repo": "github" in entry_types,
        "audit_report": "audit" in asset_types or "audit" in topics,
        "tokenomics": bool(tokenomics) or "tokenomics" in asset_types,
        "onchain_holder_data": bool(onchain and onchain.get("by_chain")),
        "social_heat": bool(structured.get("social")),
        "token_unlock_data": bool(unlocks),
        "contract_address": bool(structured.get("contracts")),
    }
    for key, wanted in _MATERIAL_TOPIC_MAP.items():
        present[key] = bool(set(wanted) & topics)

    # 结构化数据补齐：融资轮次（团队/VC）、CMC 交易对（交易所上线）
    present["team_vc"] = present["team_vc"] or bool(structured.get("raises"))
    present["exchange_listing"] = present["exchange_listing"] or bool(structured.get("exchanges"))

    material_links = _collect_material_links(sources)
    items = []
    for spec in RESEARCH_MATERIAL_TYPES:
        key = spec["key"]
        items.append({
            "key": key,
            "label": spec["label"],
            "description": spec["description"],
            "present": bool(present.get(key)),
            "note": "",
            "links": material_links.get(key, []),
        })

    # 分赛道展示：只保留该赛道需要的资料类型，无关类型隐藏。
    sector = snapshot.get("sector") or "other"
    if get_sector_visible_material_keys is not None:
        visible = get_sector_visible_material_keys(sector)
        items = [it for it in items if it["key"] in visible]

    # 分赛道排序：缺失项优先；缺失项内部按赛道主题优先级排序，
    # 让该赛道更看重的资料（如 DeFi 的审计、Meme 的交易所上线）排最前。
    if topic_priority_rank is not None:
        # 资料类型 key → 用于赛道优先级排序的代表主题（结构化数据无对应主题）
        priority_topic = {
            "official_website": None,
            "whitepaper_docs": "whitepaper",
            "github_repo": "docs",
            "audit_report": "audit",
            "tokenomics": "tokenomics",
        }
        for _k, _v in _MATERIAL_TOPIC_MAP.items():
            priority_topic[_k] = _v[0]

        def _sector_rank(item: dict) -> int:
            if item["present"]:
                return 1  # 已收集排后（保持原顺序）
            topic = priority_topic.get(item["key"])
            if topic is None:
                # 官网是一切投研基础，缺失最优先；结构化数据保持原顺序
                return -1 if item["key"] == "official_website" else 500
            return topic_priority_rank(sector, topic)

        # 稳定排序：主键 present（缺失优先），次键赛道优先级
        items = sorted(items, key=lambda it: (0 if not it["present"] else 1,
                                              _sector_rank(it)))

    return items


# ── 单币缺失补齐流水线 ──
# 缺失资料类型 key → 可自动补齐的动作集合。动作在执行时去重，按 _FILL_ACTION_ORDER 顺序串行。
_CONTENT_TOPIC_FILL = ["deep", "spa", "ai_classify"]  # 文档深爬 + SPA 兜底 + AI 正文分类

_MISSING_FILL_ACTIONS = {
    "official_website": [],                                   # 官网入口无可靠自动补，需手动
    "whitepaper_docs": ["deep", "spa", "ai_classify"],
    "github_repo": ["deep"],
    "audit_report": ["third_party", "deep", "ai_classify"],
    "tokenomics": ["tokenomics", "deep", "ai_classify"],
    "onchain_holder_data": ["holders"],
    "social_heat": ["social"],
    "token_unlock_data": ["unlocks", "deep", "ai_classify"],
    "contract_address": [],                                   # 合约地址无自动补，需手动
    "tge_ido_info": ["raises", "ai_classify"],
    "lp_liquidity_info": _CONTENT_TOPIC_FILL,
    "treasury_multisig": _CONTENT_TOPIC_FILL,
    "team_vc": ["raises", "deep", "ai_classify"],
    "roadmap": _CONTENT_TOPIC_FILL,
    "dao_governance": _CONTENT_TOPIC_FILL,
    "bug_bounty": ["third_party", "deep", "ai_classify"],
    "exchange_listing": _CONTENT_TOPIC_FILL,
    "competitor_material": ["ai_classify"],
    "major_event_announcement": _CONTENT_TOPIC_FILL,
    "third_party_rating": ["third_party"],
    "onchain_abnormal_event": ["hacks"],
}

# 动作执行顺序（依赖关系：先爬文档，再第三方/TGE/异常，最后 AI 分类；结构化数据独立置后）
_FILL_ACTION_ORDER = [
    "deep", "spa", "third_party", "raises", "hacks", "ai_classify",
    "tokenomics", "holders", "social", "unlocks",
]

# 脚本动作：key -> (脚本名, 额外参数, 超时秒)
_FILL_SCRIPT_ACTIONS = {
    "deep": ("phase_b2_deep_doc_discovery.py", ["--limit", "50", "--workers", "1", "--timeout", "15"], 300),
    "spa": ("phase_b2_spa_browser_crawl.py", ["--limit", "20", "--concurrency", "1"], 300),
    "third_party": ("phase_b2_third_party.py", ["--timeout", "20"], 180),
    "raises": ("phase_b2_third_party_raises.py", ["--timeout", "20"], 180),
    "hacks": ("phase_b2_third_party_hacks.py", ["--timeout", "20"], 180),
}


def _fill_result_ok(result) -> bool:
    """宽松判断结构化补齐函数返回是否成功（兼容 ok / status 两种字段约定）。"""
    if not isinstance(result, dict):
        return False
    if "ok" in result:
        return bool(result["ok"])
    return result.get("status") == "ok"


def _run_fill_action(action: str, asset_id: int, log=None) -> tuple[bool, str]:
    """执行单个补齐动作，返回 (是否成功, 说明)。"""
    if action in _FILL_SCRIPT_ACTIONS:
        script, extra_args, timeout = _FILL_SCRIPT_ACTIONS[action]
        bin_dir = _get_scripts_bin()
        cmd = [sys.executable, "-u", str(bin_dir / script), "--asset-id", str(asset_id)] + extra_args
        if log:
            log("$ " + " ".join(cmd))
        _, returncode = _run_with_log(cmd, str(bin_dir), timeout, log=log)
        return returncode == 0, ("ok" if returncode == 0 else f"exit {returncode}")

    if action == "ai_classify":
        result = ai_classify_asset(asset_id, log=log)
        return _fill_result_ok(result), (result.get("error") or "ok") if isinstance(result, dict) else "ok"

    if action == "tokenomics":
        result = query_tokenomics_ai(asset_id, log=log)
        return _fill_result_ok(result), (result.get("error") or "ok") if isinstance(result, dict) else "ok"

    if action == "holders":
        result = query_onchain_data(asset_id, force=True, log=log)
        return _fill_result_ok(result), (result.get("error") or "ok") if isinstance(result, dict) else "ok"

    if action == "social":
        result = query_social_heat(asset_id, force=True, log=log)
        return _fill_result_ok(result), (result.get("error") or "ok") if isinstance(result, dict) else "ok"

    if action == "unlocks":
        result = query_unlocks_ai(asset_id, log=log)
        return _fill_result_ok(result), (result.get("error") or "ok") if isinstance(result, dict) else "ok"

    return False, f"unknown action: {action}"


def fill_missing_materials(asset_id: int, log=None) -> dict:
    """一键补齐单个代币缺失的投研资料。

    流程：采集当前快照 → 计算缺失清单 → 按缺失项映射动作 → 串行执行 →
    重新采集快照对比，返回补齐前后缺失变化。单个动作失败不中断整体。
    """

    def _emit(msg: str) -> None:
        if log:
            try:
                log(msg)
            except Exception:
                pass

    snapshot = _collect_asset_snapshot(asset_id)
    if not snapshot:
        return {"ok": False, "error": "资产不存在"}

    materials = _compute_missing_materials(snapshot)
    before_missing = [{"key": m["key"], "label": m["label"]} for m in materials if not m["present"]]

    if not before_missing:
        return {
            "ok": True,
            "data": {
                "message": "该代币投研资料已完整，无需补齐",
                "before_missing": [],
                "filled": [],
                "still_missing": [],
                "actions": [],
            },
        }

    actions: list[str] = []
    for m in materials:
        if m["present"]:
            continue
        for act in _MISSING_FILL_ACTIONS.get(m["key"], []):
            if act not in actions:
                actions.append(act)

    _emit(f"检测到 {len(before_missing)} 项缺失，计划执行动作: {actions}")

    action_results = []
    for act in _FILL_ACTION_ORDER:
        if act not in actions:
            continue
        _emit(f"\n{'=' * 40}\n[补齐] {act}\n{'=' * 40}")
        try:
            ok, note = _run_fill_action(act, asset_id, log=log)
        except Exception as e:
            ok, note = False, str(e)
        action_results.append({"action": act, "ok": ok, "note": note})
        _emit(f"[补齐] {act} => {'成功' if ok else '失败'}: {note}")

    snapshot2 = _collect_asset_snapshot(asset_id)
    materials2 = _compute_missing_materials(snapshot2)
    still_missing = [{"key": m["key"], "label": m["label"]} for m in materials2 if not m["present"]]
    still_keys = {x["key"] for x in still_missing}
    filled = [m for m in before_missing if m["key"] not in still_keys]

    return {
        "ok": True,
        "data": {
            "message": f"补齐完成：缺失 {len(before_missing)} 项 → {len(still_missing)} 项（本次补齐 {len(filled)} 项）",
            "before_missing": before_missing,
            "filled": filled,
            "still_missing": still_missing,
            "actions": action_results,
        },
    }


_FETCH_TYPES = {"whitepaper_page", "docs", "docs_portal", "official_website", "github", "medium", "doc_file"}
_MAX_DOC_FETCH = 10
_SNIPPET_LIMIT = 2500
_PDF_MAX_PAGES = 30


def _fetch_url_text(url: str) -> str:
    """抓取 URL 正文文本：HTML 用正则去标签，PDF 用 PyPDF2 抽取。
    失败或非文本返回空字符串（仅保留链接引用）。"""
    import io
    import re
    import requests

    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ResearchBot/1.0)"},
            timeout=20,
            allow_redirects=True,
        )
        resp.raise_for_status()
        ctype = (resp.headers.get("content-type") or "").lower()
        if "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf"):
            from PyPDF2 import PdfReader

            reader = PdfReader(io.BytesIO(resp.content))
            parts = []
            for page in reader.pages[:_PDF_MAX_PAGES]:
                t = page.extract_text()
                if t:
                    parts.append(t)
            text = "\n\n".join(parts)
            text = re.sub(r"\s+", " ", text).strip()
            return text[:_SNIPPET_LIMIT]

        if "html" not in ctype and "text" not in ctype:
            return ""
        text = resp.text
    except Exception:
        return ""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_SNIPPET_LIMIT]


def fetch_research_source_content(url: str) -> str:
    """抓取 URL 正文文本（HTML 去标签 / PDF 抽取），供投研页按需查看资料内容。"""
    return _fetch_url_text(url)


def _build_research_sources(snapshot: dict) -> list[dict]:
    """把快照组装成带编号的引用来源（结构化数据 + 文档正文）。"""
    sources = []
    structured = snapshot.get("structured") or {}

    def _add_structured(key, label):
        val = structured.get(key)
        if not val:
            return
        sources.append({
            "type": "structured",
            "title": label,
            "url": None,
            "snippet": json.dumps(val, ensure_ascii=False, default=str),
        })

    _add_structured("tokenomics", "代币经济学数据")
    _add_structured("onchain", "链上持仓数据")
    _add_structured("social", "社交热度数据")
    _add_structured("unlocks", "代币解锁数据")
    _add_structured("contracts", "合约地址")

    order = {
        "whitepaper_page": 0, "docs": 1, "docs_portal": 2, "official_website": 3,
        "github": 4, "medium": 5, "doc_file": 6, "audit": 7,
    }
    # 按类型升序 + 发布时间倒序（新的在前，时效性更高），无日期排最后
    docs = sorted(
        (snapshot.get("sources") or []),
        key=lambda d: (
            order.get(d.get("type"), 99),
            _date_sort_key(d.get("published_at")),
        ),
    )

    fetched = 0
    for d in docs:
        snippet = None
        if d.get("type") in _FETCH_TYPES and d.get("url") and fetched < _MAX_DOC_FETCH:
            snippet = _fetch_url_text(d["url"])
            fetched += 1
        sources.append({
            "type": d.get("type"),
            "title": d.get("title") or d.get("url"),
            "url": d.get("url"),
            "snippet": snippet,
            "published_at": d.get("published_at"),
        })
    return sources


def _date_sort_key(date_str: str | None) -> tuple[int, str]:
    """日期排序 key：(有日期=0, 日期倒序字符串) 或 (无日期=1, '')。

    升序排序时：有日期的排前面（0 < 1），且越新越靠前（'9999-2025' < '9999-2024'）。
    """
    if not date_str:
        return (1, "")
    # 用一个大数减年份，实现日期倒序：越新的日期 key 越小
    # 例如 2025-08-15 -> '7974-91-84'，2024-08-15 -> '7975-91-84'，升序时 2025 在前
    try:
        y, m, d = date_str.split("-")
        inv_y = 9999 - int(y)
        inv_m = 99 - int(m)
        inv_d = 99 - int(d)
        return (0, f"{inv_y:04d}-{inv_m:02d}-{inv_d:02d}")
    except (ValueError, AttributeError):
        return (1, "")


def _format_research_context(sources: list[dict]) -> str:
    """把引用来源格式化成 LLM 上下文文本。"""
    lines = []
    for i, s in enumerate(sources, 1):
        if s.get("type") == "structured":
            head = f"[{i}] {s['title']}"
        else:
            pub = s.get("published_at")
            pub_tag = f"（发布: {pub}）" if pub else ""
            head = f"[{i}] {s.get('title') or s.get('url')}（类型: {s.get('type')}）{pub_tag}"
        lines.append(head)
        if s.get("url"):
            lines.append(f"    链接: {s['url']}")
        snip = (s.get("snippet") or "").strip()
        if snip:
            lines.append(f"    内容: {snip}")
    return "\n".join(lines)


_SNAPSHOT_TTL_SECONDS = 3600  # 一键投研快照缓存有效期（1 小时）


def _snapshot_cache_valid(snapshot: dict, updated_at) -> bool:
    """判断缓存的快照是否可复用：结构为最新（sources 均带 topics）且未过期。"""
    if not snapshot or not updated_at:
        return False
    sources = snapshot.get("sources") or []
    # 阶段3 之后快照的 sources 均带 topics 字段；旧结构快照视为失效，强制重采。
    if sources and not all("topics" in s for s in sources):
        return False
    # 引入结构化 raises/exchanges 后，旧快照缺这些键也视为失效，保证新判定立即生效。
    structured = snapshot.get("structured") or {}
    if "raises" not in structured or "exchanges" not in structured:
        return False
    # 引入分赛道排序后，旧快照缺 sector 键视为失效，保证新排序立即生效。
    if "sector" not in snapshot:
        return False
    try:
        updated = updated_at if updated_at.tzinfo else updated_at.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - updated).total_seconds()
    except Exception:
        return False
    return 0 <= age < _SNAPSHOT_TTL_SECONDS


def get_or_create_research_notebook(asset_id: int, force_refresh: bool = False) -> dict:
    """打开（不存在则创建）一个代币对应的一键投研笔记本，返回资料快照 + 缺失清单 + 历史对话。

    快照缓存：先读 snapshot_json，未过期且结构一致时直接复用，避免每次打开都重采
    结构化数据；通过 force_refresh（或 ?refresh=1）可强制重采。
    """
    with get_db() as conn:
        _ensure_research_tables(conn)
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("SELECT * FROM biz.research_notebook WHERE asset_id = %s", (asset_id,))
            nb = cur.fetchone()

    # 命中缓存：已有快照且未强制刷新且未过期且结构为最新（sources 均带 topics）
    snapshot = None
    if nb and not force_refresh:
        cached = nb.get("snapshot_json") or {}
        if _snapshot_cache_valid(cached, nb.get("updated_at")):
            snapshot = cached

    recollected = snapshot is None
    if recollected:
        snapshot = _collect_asset_snapshot(asset_id)
        if not snapshot:
            return {"ok": False, "error": "资产不存在"}

    missing = _compute_missing_materials(snapshot)
    title = f"{snapshot['symbol']} ({snapshot['name']}) 投研笔记"

    with get_db() as conn:
        _ensure_research_tables(conn)
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            if nb:
                if recollected:
                    cur.execute("""
                        UPDATE biz.research_notebook
                        SET snapshot_json = %s, missing_json = %s, title = %s, updated_at = NOW()
                        WHERE notebook_id = %s
                        RETURNING notebook_id, asset_id, title, snapshot_json, missing_json, created_at, updated_at
                    """, (
                        json.dumps(snapshot, ensure_ascii=False, default=str),
                        json.dumps(missing, ensure_ascii=False),
                        title,
                        nb["notebook_id"],
                    ))
                else:
                    # 命中缓存：不重写快照，直接复用行数据（updated_at 保持原值）
                    cur.execute("""
                        SELECT notebook_id, asset_id, title, snapshot_json, missing_json, created_at, updated_at
                        FROM biz.research_notebook WHERE notebook_id = %s
                    """, (nb["notebook_id"],))
            else:
                cur.execute("""
                    INSERT INTO biz.research_notebook (asset_id, title, snapshot_json, missing_json)
                    VALUES (%s, %s, %s, %s)
                    RETURNING notebook_id, asset_id, title, snapshot_json, missing_json, created_at, updated_at
                """, (
                    asset_id, title,
                    json.dumps(snapshot, ensure_ascii=False, default=str),
                    json.dumps(missing, ensure_ascii=False),
                ))
            notebook = cur.fetchone()

            cur.execute("""
                SELECT message_id, role, content, citations_json, created_at
                FROM biz.research_message WHERE notebook_id = %s ORDER BY created_at, message_id
            """, (notebook["notebook_id"],))
            messages = [
                {
                    "message_id": r["message_id"],
                    "role": r["role"],
                    "content": r["content"],
                    "citations": r["citations_json"] or [],
                    "created_at": str(r["created_at"]),
                }
                for r in cur.fetchall()
            ]
        conn.commit()

    sector = snapshot.get("sector") or "other"
    return {
        "ok": True,
        "data": {
            "notebook_id": notebook["notebook_id"],
            "asset_id": notebook["asset_id"],
            "title": notebook["title"],
            "sector": sector,
            "sector_label": SECTOR_LABELS.get(sector, sector),
            "missing": missing,
            "sources": snapshot["sources"],
            "structured": snapshot["structured"],
            "counts": snapshot["counts"],
            "messages": messages,
            "thesis": get_latest_research_thesis(asset_id),
            "created_at": str(notebook["created_at"]),
            "updated_at": str(notebook["updated_at"]),
        },
    }


def ask_research_notebook(notebook_id: int, question: str, log=None) -> dict:
    """基于笔记本资料库进行 AI 问答（所有回答来自资料库并标注引用）。"""
    def _emit(msg: str) -> None:
        if log:
            try:
                log(msg)
            except Exception:
                pass

    question = (question or "").strip()
    if not question:
        return {"ok": False, "error": "问题为空"}

    from crypto_research.config import get_settings
    from crypto_research.clients.llm_client import LLMClient, extract_json_from_llm_response

    settings = get_settings(require_database=True)
    llm = LLMClient(settings, rpm=30)
    if not llm.is_available():
        return {"ok": False, "error": "LLM 未配置，无法问答"}

    with get_db() as conn:
        _ensure_research_tables(conn)
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("SELECT * FROM biz.research_notebook WHERE notebook_id = %s", (notebook_id,))
            nb = cur.fetchone()
        if not nb:
            return {"ok": False, "error": "笔记本不存在"}

        snapshot = nb.get("snapshot_json") or {}
        sources = _build_research_sources(snapshot)
        if not sources:
            return {"ok": False, "error": "该代币暂无投研资料，无法问答"}

        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "INSERT INTO biz.research_message (notebook_id, role, content) VALUES (%s, 'user', %s) "
                "RETURNING message_id, role, content, citations_json, created_at",
                (notebook_id, question),
            )
            user_msg = cur.fetchone()
        conn.commit()

    _emit("构建资料上下文并调用 LLM...")
    context = _format_research_context(sources)
    if len(context) > 40000:
        context = context[:40000]

    system_prompt = (
        "你是一个加密货币投研助手。请严格只依据下面「资料库」中的内容回答用户问题，"
        "不要使用资料库之外的任何知识或猜测。\n"
        "引用资料时，在相应句子末尾用 [编号] 标注来源，例如 [1] 或 [2][3]。\n"
        "如果资料库中没有相关信息，请直接说明「资料库中未找到相关信息」，不要编造。\n"
        "只输出 JSON，不要输出其他内容。JSON 格式："
        '{"answer": "你的回答（Markdown 文本，含 [编号] 引用）", '
        '"citations": [{"index": 1, "quote": "被引用的原文片段（不超过40字）"}]}'
    )
    user_prompt = (
        f"资料库如下：\n\n{context}\n\n"
        f"用户问题：{question}\n\n请回答。"
    )

    try:
        raw = llm.chat(system_prompt, user_prompt, temperature=0.2, max_tokens=4096)
    except Exception as e:
        return {"ok": False, "error": f"LLM 调用失败: {e}"}

    try:
        est = extract_json_from_llm_response(raw)
    except Exception as e:
        return {"ok": False, "error": f"AI 返回解析失败: {e}"}

    answer = (est.get("answer") or "").strip()
    if not answer:
        answer = "（AI 未返回有效回答）"

    citations = []
    seen_idx = set()
    for c in est.get("citations") or []:
        try:
            idx = int(c.get("index", 0))
        except (TypeError, ValueError):
            continue
        if idx < 1 or idx > len(sources) or idx in seen_idx:
            continue
        seen_idx.add(idx)
        s = sources[idx - 1]
        citations.append({
            "index": idx,
            "title": s.get("title") or s.get("url") or "",
            "url": s.get("url"),
            "quote": str(c.get("quote", ""))[:200],
        })

    with get_db() as conn:
        _ensure_research_tables(conn)
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("""
                INSERT INTO biz.research_message (notebook_id, role, content, citations_json)
                VALUES (%s, 'assistant', %s, %s)
                RETURNING message_id, role, content, citations_json, created_at
            """, (notebook_id, answer, json.dumps(citations, ensure_ascii=False)))
            assistant_msg = cur.fetchone()
            cur.execute("UPDATE biz.research_notebook SET updated_at = NOW() WHERE notebook_id = %s", (notebook_id,))
        conn.commit()

    _emit("问答完成")
    return {
        "ok": True,
        "data": {
            "message": {
                "message_id": assistant_msg["message_id"],
                "role": assistant_msg["role"],
                "content": assistant_msg["content"],
                "citations": citations,
                "created_at": str(assistant_msg["created_at"]),
            },
            "user_message_id": user_msg["message_id"],
        },
    }


# ── 研究结论 / 评级（结构化沉淀） ──


def _thesis_row_to_dict(row) -> dict:
    """把 research_thesis 行转成可序列化 dict。"""
    if not row:
        return None
    return {
        "thesis_id": row["thesis_id"],
        "asset_id": row["asset_id"],
        "stance": row["stance"],
        "conviction": row["conviction"],
        "thesis": row["thesis_json"] or [],
        "key_metrics": row["key_metrics_json"] or {},
        "risks": row["risks_json"] or [],
        "catalysts": row["catalysts_json"] or [],
        "source_notebook_id": row["source_notebook_id"],
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def get_sector_competitors(asset_id: int, limit: int = 8) -> dict:
    """同赛道竞品结构化对比：按 primary_sector 找同赛道币，聚合关键指标横向对比。

    对比维度：
      - 基础：市值 / FDV / 价格
      - 代币经济学：总供应量 / 流通量 / 通胀率
      - 链上：Top10 集中度 / 持有者数
      - 社交：热度分 / X 粉丝
      - 解锁：未来 30 天解锁 %
      - 融资：融资轮数 / 总金额
    """
    with get_db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            # 1. 取当前币的赛道
            cur.execute(
                "SELECT asset_id, canonical_symbol, canonical_name, primary_sector, asset_type "
                "FROM core.asset WHERE asset_id = %s",
                (asset_id,),
            )
            target = cur.fetchone()
            if not target:
                return {"ok": False, "error": "资产不存在"}
            sector = target["primary_sector"] or "other"

            # 2. 找同赛道币（排除自己），按市值/FDV 降序取前 N 个
            #    市值从 biz.coin_basic 或 input_snapshot 里取（降级兼容）
            cur.execute("""
                WITH sector_assets AS (
                    SELECT a.asset_id, a.canonical_symbol, a.canonical_name, a.asset_type,
                           a.primary_sector
                    FROM core.asset a
                    WHERE a.primary_sector = %s
                      AND a.asset_id <> %s
                ),
                asset_mcap AS (
                    SELECT asset_id,
                           COALESCE(
                               NULLIF((cb.input_snapshot_json->>'market_cap')::NUMERIC, 0),
                               NULLIF((cb.input_snapshot_json->>'fdv')::NUMERIC, 0),
                               0
                           ) AS mcap
                    FROM biz.asset_token_unlocks cb
                    WHERE cb.asset_id IN (SELECT asset_id FROM sector_assets)
                )
                SELECT sa.asset_id, sa.canonical_symbol, sa.canonical_name, sa.asset_type,
                       COALESCE(am.mcap, 0) AS mcap
                FROM sector_assets sa
                LEFT JOIN asset_mcap am ON am.asset_id = sa.asset_id
                ORDER BY am.mcap DESC NULLS LAST, sa.canonical_symbol
                LIMIT %s
            """, (sector, asset_id, limit))
            comp_rows = cur.fetchall()

            if not comp_rows:
                return {
                    "ok": True,
                    "sector": sector,
                    "sector_label": SECTOR_LABELS.get(sector, sector),
                    "target": {
                        "asset_id": target["asset_id"],
                        "symbol": target["canonical_symbol"],
                        "name": target["canonical_name"],
                    },
                    "competitors": [],
                    "metrics": [],
                }

            comp_ids = [r["asset_id"] for r in comp_rows]
            all_ids = [asset_id] + comp_ids

            # 3. 批量取各维度数据
            # 3a. 代币经济学
            cur.execute("""
                SELECT asset_id, total_supply, circulating_supply, max_supply,
                       buy_tax_pct, sell_tax_pct, contract_renounced, lp_locked
                FROM biz.asset_tokenomics WHERE asset_id = ANY(%s)
            """, (all_ids,))
            tokenomics_map = {r["asset_id"]: dict(r) for r in cur.fetchall()}

            # 3b. 链上持仓（最新一条）
            cur.execute("""
                SELECT DISTINCT ON (asset_id)
                       asset_id, top10_concentration, total_holders,
                       holder_change_30d, whale_balance_change_30d_pct
                FROM biz.onchain_holder_snapshot
                WHERE asset_id = ANY(%s)
                ORDER BY asset_id, snapshot_date DESC
            """, (all_ids,))
            onchain_map = {r["asset_id"]: dict(r) for r in cur.fetchall()}

            # 3c. 社交热度
            cur.execute("""
                SELECT asset_id, score, community_json
                FROM biz.asset_social_heat WHERE asset_id = ANY(%s)
            """, (all_ids,))
            social_map = {r["asset_id"]: dict(r) for r in cur.fetchall()}

            # 3d. 解锁（未来 30 天解锁 %）
            cur.execute("""
                SELECT asset_id, unlock_events_json, input_snapshot_json
                FROM biz.asset_token_unlocks WHERE asset_id = ANY(%s)
            """, (all_ids,))
            unlock_map = {r["asset_id"]: dict(r) for r in cur.fetchall()}

            # 3e. 融资
            try:
                cur.execute("""
                    SELECT asset_id, COUNT(*) AS raise_count,
                           SUM(amount) AS total_raised
                    FROM biz.asset_raises
                    WHERE asset_id = ANY(%s)
                    GROUP BY asset_id
                """, (all_ids,))
                raise_map = {r["asset_id"]: dict(r) for r in cur.fetchall()}
            except psycopg.errors.UndefinedTable:
                raise_map = {}

            # 3f. 市值/价格（从 unlock input_snapshot 降级取）
            def _get_mcap_price(aid):
                row = unlock_map.get(aid)
                if not row:
                    return (None, None)
                snap = row.get("input_snapshot_json") or {}
                mcap = snap.get("market_cap") or snap.get("market_cap_usd")
                price = snap.get("price") or snap.get("price_usd")
                fdv = snap.get("fdv") or snap.get("fdv_usd")
                return (mcap or fdv, price)

            # 4. 组装竞品列表
            def _build_coin(aid, symbol, name, atype):
                mcap, price = _get_mcap_price(aid)
                t = tokenomics_map.get(aid) or {}
                o = onchain_map.get(aid) or {}
                s = social_map.get(aid) or {}
                u = unlock_map.get(aid) or {}
                r = raise_map.get(aid) or {}

                # 计算未来 30 天解锁 %
                unlock_30d_pct = None
                events = u.get("unlock_events_json") or []
                if events:
                    from datetime import datetime, timedelta
                    now = datetime.utcnow().date()
                    thirty = now + timedelta(days=30)
                    total_pct = 0.0
                    for ev in events:
                        if not ev.get("is_upcoming"):
                            continue
                        d = ev.get("date")
                        if not d:
                            continue
                        try:
                            ev_date = datetime.strptime(d[:10], "%Y-%m-%d").date()
                        except (ValueError, TypeError):
                            continue
                        if ev_date <= thirty:
                            pct = ev.get("pct") or 0
                            try:
                                total_pct += float(pct)
                            except (ValueError, TypeError):
                                pass
                    unlock_30d_pct = round(total_pct, 2) if total_pct else 0

                # X 粉丝数
                community = s.get("community_json") or {}
                x_followers = None
                for plat in ("x", "twitter", "X"):
                    if plat in community:
                        x_followers = community[plat].get("followers")
                        break

                circ_supply = t.get("circulating_supply")
                total_supply = t.get("total_supply")
                inflation_pct = None
                if circ_supply and total_supply and float(total_supply) > 0:
                    try:
                        inflation_pct = round(
                            (1 - float(circ_supply) / float(total_supply)) * 100, 2
                        )
                    except (ValueError, TypeError, ZeroDivisionError):
                        pass

                return {
                    "asset_id": aid,
                    "symbol": symbol,
                    "name": name,
                    "type": atype,
                    "is_target": aid == asset_id,
                    "market_cap": mcap,
                    "price": price,
                    "total_supply": total_supply,
                    "circulating_supply": circ_supply,
                    "inflation_pct": inflation_pct,
                    "buy_tax_pct": t.get("buy_tax_pct"),
                    "sell_tax_pct": t.get("sell_tax_pct"),
                    "contract_renounced": t.get("contract_renounced"),
                    "lp_locked": t.get("lp_locked"),
                    "top10_concentration": o.get("top10_concentration"),
                    "total_holders": o.get("total_holders"),
                    "holder_change_30d": o.get("holder_change_30d"),
                    "social_score": s.get("score"),
                    "x_followers": x_followers,
                    "unlock_30d_pct": unlock_30d_pct,
                    "raise_count": r.get("raise_count"),
                    "total_raised": r.get("total_raised"),
                }

            competitors = []
            # 目标币放第一个
            competitors.append(_build_coin(
                target["asset_id"], target["canonical_symbol"],
                target["canonical_name"], target["asset_type"],
            ))
            for r in comp_rows:
                competitors.append(_build_coin(
                    r["asset_id"], r["canonical_symbol"],
                    r["canonical_name"], r["asset_type"],
                ))

            # 5. 指标定义（前端表格列）
            metrics = [
                {"key": "market_cap", "label": "市值", "format": "usd_big"},
                {"key": "price", "label": "价格", "format": "usd_price"},
                {"key": "total_supply", "label": "总供应量", "format": "number_big"},
                {"key": "circulating_supply", "label": "流通量", "format": "number_big"},
                {"key": "inflation_pct", "label": "未流通占比", "format": "pct"},
                {"key": "unlock_30d_pct", "label": "30天解锁%", "format": "pct"},
                {"key": "top10_concentration", "label": "Top10集中度", "format": "pct"},
                {"key": "total_holders", "label": "持有者数", "format": "number_big"},
                {"key": "social_score", "label": "社交热度分", "format": "score"},
                {"key": "x_followers", "label": "X粉丝", "format": "number_big"},
                {"key": "raise_count", "label": "融资轮数", "format": "number"},
                {"key": "total_raised", "label": "融资总额", "format": "usd_big"},
            ]

            return {
                "ok": True,
                "sector": sector,
                "sector_label": SECTOR_LABELS.get(sector, sector),
                "target": {
                    "asset_id": target["asset_id"],
                    "symbol": target["canonical_symbol"],
                    "name": target["canonical_name"],
                },
                "competitors": competitors,
                "metrics": metrics,
            }


def get_latest_research_thesis(asset_id: int) -> dict | None:
    """读取某代币最新一条研究结论（不含历史版本）。"""
    with get_db() as conn:
        _ensure_research_tables(conn)
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("""
                SELECT thesis_id, asset_id, stance, conviction, thesis_json,
                       key_metrics_json, risks_json, catalysts_json,
                       source_notebook_id, created_at, updated_at
                FROM biz.research_thesis WHERE asset_id = %s
                ORDER BY updated_at DESC, thesis_id DESC LIMIT 1
            """, (asset_id,))
            row = cur.fetchone()
    return _thesis_row_to_dict(row)


def generate_research_thesis(asset_id: int, log=None) -> dict:
    """基于资料库生成结构化研究结论（stance/conviction/thesis/risks/catalysts/key_metrics）。

    结论严格依据笔记本资料库，并附带抛压评分作为量化辅助；每次生成追加一条新记录，
    保留历史版本用于「当时判断 vs 后续走势」回溯。
    """
    def _emit(msg: str) -> None:
        if log:
            try:
                log(msg)
            except Exception:
                pass

    from crypto_research.config import get_settings
    from crypto_research.clients.llm_client import LLMClient, extract_json_from_llm_response

    settings = get_settings(require_database=True)
    llm = LLMClient(settings, rpm=30)
    if not llm.is_available():
        return {"ok": False, "error": "LLM 未配置，无法生成结论"}

    # 1. 读取笔记本快照（不存在则先创建）
    with get_db() as conn:
        _ensure_research_tables(conn)
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("SELECT notebook_id, snapshot_json FROM biz.research_notebook WHERE asset_id = %s", (asset_id,))
            nb = cur.fetchone()
    if not nb:
        created = get_or_create_research_notebook(asset_id)
        if not created.get("ok"):
            return {"ok": False, "error": created.get("error", "无法创建笔记本")}
        with get_db() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute("SELECT notebook_id, snapshot_json FROM biz.research_notebook WHERE asset_id = %s", (asset_id,))
                nb = cur.fetchone()

    notebook_id = nb["notebook_id"]
    snapshot = nb.get("snapshot_json") or {}
    sources = _build_research_sources(snapshot)
    if not sources:
        return {"ok": False, "error": "该代币暂无投研资料，无法生成结论"}

    context = _format_research_context(sources)
    if len(context) > 40000:
        context = context[:40000]

    # 2. 附加抛压评分作为量化辅助
    pressure = compute_unlock_pressure(asset_id)
    pressure_txt = ""
    if pressure:
        pressure_txt = (
            f"\n\n[抛压评分（量化参考）]\n"
            f"风险等级: {pressure.get('risk_level')}, 评分: {pressure.get('pressure_score')}分, "
            f"未来30天解锁占比: {pressure.get('unlock_pct_30d')}%, "
            f"Top10持仓集中度: {pressure.get('top10_concentration')}%"
        )

    _emit("调用 LLM 生成研究结论...")
    system_prompt = (
        "你是一名资深加密货币投研分析师。请严格只依据下面「资料库」中的内容，"
        "输出一个结构化的研究结论 JSON，不要使用资料库之外的知识或猜测。\n"
        "论点必须基于资料库事实，并在 citations 中用 [编号] 标注依据（编号对应资料库条目）。\n"
        "只输出 JSON，不要输出其他内容。JSON 格式：\n"
        '{"stance": "bullish|bearish|neutral", '
        '"conviction": "high|medium|low", '
        '"thesis": [{"point": "核心论点（一句话）", "citations": [1, 2]}], '
        '"risks": [{"risk": "风险点", "citations": [3]}], '
        '"catalysts": [{"catalyst": "催化剂/事件", "timing": "预期时间"}], '
        '"key_metrics": {"价格": "...", "市值": "...", "FDV": "...", "其他关键指标": "..."}}'
    )
    user_prompt = f"资料库如下：\n\n{context}{pressure_txt}\n\n请给出该代币的研究结论。"

    try:
        raw = llm.chat(system_prompt, user_prompt, temperature=0.3, max_tokens=4096)
    except Exception as e:
        return {"ok": False, "error": f"LLM 调用失败: {e}"}

    try:
        est = extract_json_from_llm_response(raw)
    except Exception as e:
        return {"ok": False, "error": f"AI 返回解析失败: {e}"}

    stance = (est.get("stance") or "neutral").lower()
    if stance not in ("bullish", "bearish", "neutral"):
        stance = "neutral"
    conviction = (est.get("conviction") or "medium").lower()
    if conviction not in ("high", "medium", "low"):
        conviction = "medium"

    thesis = est.get("thesis") or []
    risks = est.get("risks") or []
    catalysts = est.get("catalysts") or []
    key_metrics = est.get("key_metrics") or {}

    with get_db() as conn:
        _ensure_research_tables(conn)
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("""
                INSERT INTO biz.research_thesis
                    (asset_id, stance, conviction, thesis_json, key_metrics_json,
                     risks_json, catalysts_json, source_notebook_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING thesis_id, asset_id, stance, conviction, thesis_json,
                          key_metrics_json, risks_json, catalysts_json,
                          source_notebook_id, created_at, updated_at
            """, (
                asset_id, stance, conviction,
                json.dumps(thesis, ensure_ascii=False),
                json.dumps(key_metrics, ensure_ascii=False, default=str),
                json.dumps(risks, ensure_ascii=False),
                json.dumps(catalysts, ensure_ascii=False),
                notebook_id,
            ))
            row = cur.fetchone()
        conn.commit()

    _emit("研究结论已生成")
    return {"ok": True, "data": _thesis_row_to_dict(row)}


# ── 链上数据监控 ──


def get_onchain_holder_snapshot(asset_id: int) -> dict:
    """获取指定资产的最新持仓快照。"""
    with get_db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("""
                SELECT hs.*, a.canonical_symbol, a.canonical_name
                FROM biz.onchain_holder_snapshot hs
                INNER JOIN core.asset a ON a.asset_id = hs.asset_id
                WHERE hs.asset_id = %s
                ORDER BY hs.snapshot_date DESC, hs.chain
            """, (asset_id,))
            rows = [dict(r) for r in cur.fetchall()]

            # 按链分组
            by_chain = {}
            for r in rows:
                chain = r["chain"]
                if chain not in by_chain:
                    by_chain[chain] = []
                by_chain[chain].append({
                    "snapshot_date": str(r["snapshot_date"]),
                    "chain": r["chain"],
                    "top10_concentration": float(r["top10_concentration"]) if r["top10_concentration"] else None,
                    "top50_concentration": float(r["top50_concentration"]) if r["top50_concentration"] else None,
                    "top100_concentration": float(r["top100_concentration"]) if r["top100_concentration"] else None,
                    "total_holders": r["total_holders"],
                    "holder_change_7d": r["holder_change_7d"],
                    "holder_change_30d": r["holder_change_30d"],
                    "whale_balance_change_7d_pct": float(r["whale_balance_change_7d_pct"]) if r["whale_balance_change_7d_pct"] else None,
                    "whale_balance_change_30d_pct": float(r["whale_balance_change_30d_pct"]) if r["whale_balance_change_30d_pct"] else None,
                    "exchange_wallet_pct": float(r["exchange_wallet_pct"]) if r["exchange_wallet_pct"] else None,
                    "fetched_at": str(r["fetched_at"]) if r["fetched_at"] else None,
                })
    return {
        "ok": True,
        "asset_id": asset_id,
        "symbol": rows[0]["canonical_symbol"] if rows else "",
        "name": rows[0]["canonical_name"] if rows else "",
        "by_chain": by_chain,
    }


def get_onchain_transfers(
    asset_id: int | None = None,
    is_to_exchange: bool | None = None,
    limit: int = 50,
) -> list[dict]:
    """获取大额转账记录，可按资产和转入交易所过滤。"""
    with get_db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            conditions = []
            params = []

            if asset_id:
                conditions.append("tl.asset_id = %s")
                params.append(asset_id)
            if is_to_exchange is not None:
                conditions.append("tl.is_to_exchange = %s")
                params.append(is_to_exchange)

            where = "WHERE " + " AND ".join(conditions) if conditions else ""

            cur.execute(f"""
                SELECT tl.*, a.canonical_symbol, a.canonical_name
                FROM biz.onchain_transfer_log tl
                LEFT JOIN core.asset a ON a.asset_id = tl.asset_id
                {where}
                ORDER BY tl.block_timestamp DESC
                LIMIT %s
            """, params + [limit])

            return [
                {
                    "log_id": r["log_id"],
                    "asset_id": r["asset_id"],
                    "symbol": r["canonical_symbol"],
                    "name": r["canonical_name"],
                    "chain": r["chain"],
                    "tx_hash": r["tx_hash"],
                    "from_address": r["from_address"],
                    "to_address": r["to_address"],
                    "value": float(r["value"]),
                    "value_usd": float(r["value_usd"]) if r["value_usd"] else None,
                    "from_label": r["from_label"],
                    "to_label": r["to_label"],
                    "from_exchange": r["from_exchange"],
                    "to_exchange": r["to_exchange"],
                    "block_number": r["block_number"],
                    "block_timestamp": str(r["block_timestamp"]) if r["block_timestamp"] else None,
                    "is_to_exchange": r["is_to_exchange"],
                }
                for r in cur.fetchall()
            ]


def get_onchain_alert_summary() -> dict:
    """获取链上告警摘要：最近 24h 转入交易所的大额转账统计。"""
    with get_db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            # 24h 转入交易所汇总
            cur.execute("""
                SELECT a.canonical_symbol, tl.chain,
                       COUNT(*) AS tx_count,
                       SUM(tl.value_usd) AS total_value_usd,
                       MAX(tl.value_usd) AS max_value_usd,
                       MAX(tl.block_timestamp) AS latest_at
                FROM biz.onchain_transfer_log tl
                LEFT JOIN core.asset a ON a.asset_id = tl.asset_id
                WHERE tl.is_to_exchange = TRUE
                  AND tl.block_timestamp >= NOW() - INTERVAL '24 hours'
                GROUP BY a.canonical_symbol, tl.chain
                ORDER BY total_value_usd DESC NULLS LAST
                LIMIT 20
            """)
            alerts_24h = [
                {
                    "symbol": r["canonical_symbol"] or "?",
                    "chain": r["chain"],
                    "tx_count": r["tx_count"],
                    "total_value_usd": float(r["total_value_usd"]) if r["total_value_usd"] else 0,
                    "max_value_usd": float(r["max_value_usd"]) if r["max_value_usd"] else 0,
                    "latest_at": str(r["latest_at"]) if r["latest_at"] else None,
                }
                for r in cur.fetchall()
            ]

            # 总览
            cur.execute("""
                SELECT
                    COUNT(*) AS total_transfers,
                    COUNT(*) FILTER (WHERE is_to_exchange) AS to_exchange_count,
                    COALESCE(SUM(value_usd), 0) AS total_value_usd
                FROM biz.onchain_transfer_log
            """)
            totals = dict(cur.fetchone()) if cur.rowcount else {}

    return {
        "ok": True,
        "alerts_24h": alerts_24h,
        "totals": {
            "total_transfers": totals.get("total_transfers", 0),
            "to_exchange_count": totals.get("to_exchange_count", 0),
            "total_value_usd": float(totals.get("total_value_usd", 0)),
        },
    }


def _extract_chain_error(stdout: str, stderr: str, returncode: int) -> str:
    """从 subprocess 输出提取关键错误信息（脚本错误用 print 输出到 stdout）。"""
    lines = [l.strip() for l in (stderr or "").split("\n") + (stdout or "").split("\n") if l.strip()]
    warn_lines = [l for l in lines if "[WARN]" in l or "[ERROR]" in l or "ERROR" in l]
    if warn_lines:
        return warn_lines[-1]
    if returncode != 0:
        return lines[-1] if lines else f"退出码 {returncode}"
    return ""  # 退出码 0 且无错误行：视为无数据，由调用方提示


def _run_with_log(cmd: list, cwd: str, timeout: int, log=None) -> tuple[str, int]:
    """运行子进程，实时把 stdout/stderr 逐行传给 log，返回 (合并输出, 退出码)。

    退出码为 -1 表示超时（已被 kill），供调用方判断。
    """
    import subprocess
    import threading

    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, env=env,
    )
    out_lines: list[str] = []

    def _reader() -> None:
        assert proc.stdout
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            if line:
                if log:
                    try:
                        log(line)
                    except Exception:
                        pass
                out_lines.append(line)

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        t.join(timeout=5)
        return "\n".join(out_lines), -1
    t.join(timeout=5)
    return "\n".join(out_lines), proc.returncode


def _extract_json_output(output: str) -> dict | None:
    """从子进程合并输出中提取脚本最后的 JSON 结果。

    脚本约定：最终结果用 print(json.dumps(...)) 单行输出到 stdout，
    进度日志走 stderr（被 _run_with_log 合并进同一输出）。因此从后往前找
    第一个能解析且含 status 键的 JSON 行。
    """
    for line in reversed(output.strip().split("\n")):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "status" in obj:
            return obj
    return None


def _ensure_onchain_snapshot_table(conn) -> None:
    """确保 biz.onchain_holder_snapshot 存在（新环境自动建表，与 DDL 迁移保持一致）。"""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS biz.onchain_holder_snapshot (
                snapshot_id      SERIAL PRIMARY KEY,
                asset_id         INTEGER NOT NULL,
                chain            TEXT NOT NULL,
                contract_address TEXT NOT NULL,
                snapshot_date    DATE NOT NULL,
                top10_concentration  NUMERIC(5,2),
                top50_concentration  NUMERIC(5,2),
                top100_concentration NUMERIC(5,2),
                total_holders        INTEGER,
                holder_change_7d     INTEGER,
                holder_change_30d    INTEGER,
                whale_balance_change_7d_pct  NUMERIC(6,2),
                whale_balance_change_30d_pct NUMERIC(6,2),
                exchange_wallet_pct   NUMERIC(5,2),
                vc_wallet_pct         NUMERIC(5,2),
                smart_money_pct       NUMERIC(5,2),
                retail_pct            NUMERIC(5,2),
                contract_pct          NUMERIC(5,2),
                fetched_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT fk_holder_snapshot_asset
                    FOREIGN KEY (asset_id) REFERENCES core.asset(asset_id)
                    ON DELETE CASCADE
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_holder_snapshot_asset_date
            ON biz.onchain_holder_snapshot (asset_id, snapshot_date DESC)
        """)


def _save_onchain_holder_snapshot(asset_id: int, chain: str, contract_address: str, data: dict) -> None:
    """把「拉取链上数据」爬到的持仓分布写入投研页判定所用的 onchain_holder_snapshot 表。

    同日同链重复拉取时先删旧快照再写新快照，避免重复累积。
    """
    with get_db() as conn:
        _ensure_onchain_snapshot_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM biz.onchain_holder_snapshot
                WHERE asset_id = %s AND chain = %s AND snapshot_date = CURRENT_DATE
                """,
                (asset_id, chain),
            )
            cur.execute(
                """
                INSERT INTO biz.onchain_holder_snapshot
                    (asset_id, chain, contract_address, snapshot_date,
                     top10_concentration, top50_concentration, top100_concentration,
                     total_holders, fetched_at)
                VALUES (%s, %s, %s, CURRENT_DATE, %s, %s, %s, %s, NOW())
                """,
                (
                    asset_id, chain, contract_address,
                    data.get("top_10_pct"), data.get("top_50_pct"), data.get("top_100_pct"),
                    data.get("total_holders"),
                ),
            )


def query_onchain_data(asset_id: int, force: bool = False, log=None) -> dict:
    """按需查询链上持仓分布（BSC/ETH 优先 Binplorer API，其余链从区块浏览器 HTML 爬取）。

    支持多链：依次爬取资产在各链上的合约数据。
    大额转账暂不支持（需 API），仅返回持仓分布。
    """
    import time

    def _emit(msg: str) -> None:
        if log:
            log(msg)

    t0 = time.time()
    scripts_bin = _get_scripts_bin()
    script = str(scripts_bin / "phase_chain_holder_scrape.py")

    # 1. 获取资产的所有链的合约地址
    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection
    settings = get_settings(require_database=True)

    result = {
        "status": "ok",
        "asset_id": asset_id,
        "from_cache": False,
        "chains": {},
        "transfers": [],
    }

    chains_info = []
    with get_connection(settings.database_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """SELECT a.canonical_symbol, a.canonical_name,
                          m.chain, m.contract_address
                   FROM core.asset a
                   INNER JOIN core.asset_contract m ON m.asset_id = a.asset_id
                   WHERE a.asset_id = %s AND a.status = 'active'
                   ORDER BY CASE m.chain WHEN 'bsc' THEN 0 WHEN 'eth' THEN 1 ELSE 2 END""",
                (asset_id,),
            )
            chains_info = [dict(r) for r in cur.fetchall()]

    if not chains_info:
        return {"ok": False, "error": "资产无合约地址，无法查询链上数据"}

    result["symbol"] = chains_info[0]["canonical_symbol"]
    result["name"] = chains_info[0]["canonical_name"]
    _emit(f"开始查询链上数据: {result['name']} ({result['symbol']})，共 {len(chains_info)} 条链")

    # 2. 逐链爬取
    holder_fetched = False
    for info in chains_info:
        chain = info["chain"]
        contract = info["contract_address"]
        _emit(f"→ 爬取 {chain} 链持仓: {contract}")
        cmd = [
            sys.executable, "-u", script,
            "--contract", contract,
            "--chain", chain,
        ]
        if force:
            cmd.append("--force")

        try:
            stdout, returncode = _run_with_log(cmd, str(scripts_bin), 120, log=log)
        except Exception as e:
            result["_errors"] = result.get("_errors", [])
            result["_errors"].append(f"{chain}: {e}")
            continue

        if returncode == -1:
            result["_errors"] = result.get("_errors", [])
            result["_errors"].append(f"{chain}: 爬取超时（120秒）")
            continue

        stdout = stdout.strip()
        if returncode != 0 or not stdout:
            err = _extract_chain_error(stdout, "", returncode)
            result["_errors"] = result.get("_errors", [])
            result["_errors"].append(f"{chain}: {err or '无输出'}")
            continue

        # 提取最后一行 JSON 输出
        for line in reversed(stdout.split("\n")):
            line = line.strip()
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                    if data.get("status") == "ok" and data.get("total_holders", 0) > 0:
                        result["chains"][chain] = {
                            "top10_concentration": data.get("top_10_pct"),
                            "top50_concentration": data.get("top_50_pct"),
                            "top100_concentration": data.get("top_100_pct"),
                            "total_holders": data.get("total_holders", 0),
                            "top_holders": [
                                {
                                    "address": h.get("address", ""),
                                    "share_pct": h.get("pct"),
                                    "label": h.get("label", ""),
                                    "rank": h.get("rank"),
                                }
                                for h in data.get("top_holders", [])
                            ],
                            "tier_distribution": data.get("tier_distribution", []),
                        }
                        holder_fetched = True
                        try:
                            _save_onchain_holder_snapshot(asset_id, chain, contract, data)
                        except Exception as e:
                            _emit(f"写入链上持仓快照失败({chain}): {e}")
                    else:
                        err = _extract_chain_error(stdout, "", 0)
                        result["_errors"] = result.get("_errors", [])
                        result["_errors"].append(f"{chain}: {err or '无持币数据'}")
                except json.JSONDecodeError:
                    pass
                break

    result["elapsed_ms"] = int((time.time() - t0) * 1000)
    if not holder_fetched:
        detail = ""
        if result.get("_errors"):
            detail = " | ".join(result["_errors"])
        result["_note"] = "持仓数据爬取失败（可能合约无持币记录或区块浏览器访问受限）"
        if detail:
            result["_note"] += f"【{detail}】"
        result.pop("_errors", None)
    else:
        _emit(f"链上数据查询完成，耗时 {result['elapsed_ms']}ms")

    return {"ok": True, "data": result}


def _get_scripts_bin() -> Path:
    """获取 scripts/bin 目录路径（兼容 Docker 和本地）。"""
    if os.path.exists("/app/scripts/bin"):
        return Path("/app/scripts/bin")
    return Path(__file__).resolve().parents[2] / "05_代码与脚本" / "scripts" / "bin"


def get_asset_unlocks(asset_id: int) -> dict | None:
    """读取已缓存的解锁数据（只读，不触发爬取）。"""
    with get_db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """SELECT overview_json, unlock_events_json, revenue_json, valuation_json,
                          source_name, slug,
                          methodology_json, input_snapshot_json, updated_at
                   FROM biz.asset_token_unlocks WHERE asset_id = %s""",
                (asset_id,),
            )
            row = cur.fetchone()
    if not row:
        return None
    overview = row.get("overview_json") or {}
    events = row.get("unlock_events_json") or []
    revenue = row.get("revenue_json") or {}
    valuation = row.get("valuation_json") or {}
    methodology = row.get("methodology_json") or {}
    input_snapshot = row.get("input_snapshot_json") or {}
    note = ""
    if isinstance(overview, dict):
        note = overview.pop("_note", "") or ""
    return {
        "source_name": row.get("source_name", "缓存"),
        "slug": row.get("slug"),
        "overview": overview,
        "unlock_events": events,
        "revenue": revenue,
        "valuation": valuation,
        "note": note,
        "methodology": methodology,
        "input_snapshot": input_snapshot,
        "updated_at": str(row.get("updated_at", "")),
        "pressure": compute_unlock_pressure(asset_id),
    }


# ── 解锁 × 持仓 抛压评分 ─────────────────────────────────────

_UNLOCK_PRESSURE_TTL_SECONDS = 6 * 3600  # 压力评分缓存 6 小时

_ENSURE_UNLOCK_PRESSURE_SQL = """
CREATE TABLE IF NOT EXISTS biz.asset_unlock_pressure (
    asset_id            INTEGER PRIMARY KEY REFERENCES core.asset(asset_id),
    unlock_pct_7d       NUMERIC(6,2),   -- 未来 7 天解锁占 MCAP 百分比
    unlock_pct_30d      NUMERIC(6,2),   -- 未来 30 天解锁占 MCAP 百分比
    next_unlock_date    DATE,           -- 最近一次解锁日期
    top10_concentration NUMERIC(5,2),   -- Top10 持仓集中度（快照）
    turnover_24h        NUMERIC(8,4),   -- 24h 换手率（volume/mcap）
    pressure_score      NUMERIC(5,2),   -- 抛压评分 0-100
    risk_level          TEXT,           -- low / medium / high
    detail_json         JSONB,          -- 各分量明细
    calculated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


def _pressure_float(v):
    """Decimal / None / 字符串 → float 或 None。"""
    from decimal import Decimal
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_unlock_event_date(date_str: str):
    """解析解锁事件日期（如 'Feb 15, 2026'），失败返回 None。"""
    if not date_str:
        return None
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(date_str).strip(), fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def _compute_pressure_score(unlock_pct_30d, top10_concentration, turnover_24h):
    """抛压评分 0-100（越高越危险）。

    unlock_score：未来 30 天解锁占比 × 6，60 分封顶；
    concentration_score：Top10 集中度映射 0-25 分（越集中解锁抛压越集中）；
    liquidity_discount：换手率越高承接力越强，减免最多 15 分。
    """
    unlock_score = min(60.0, (unlock_pct_30d or 0.0) * 6.0)
    concentration_score = ((top10_concentration or 0.0) / 100.0) * 25.0
    liquidity_discount = min(15.0, (turnover_24h or 0.0) * 150.0)
    score = max(0.0, min(100.0, unlock_score + concentration_score - liquidity_discount))
    if score >= 60:
        risk = "high"
    elif score >= 30:
        risk = "medium"
    else:
        risk = "low"
    return round(score, 2), risk


def compute_unlock_pressure(asset_id: int, force: bool = False) -> dict | None:
    """计算「未来解锁 × 持仓集中度 × 流动性」交叉抛压评分。

    数据来源：biz.asset_token_unlocks（未来 7/30 天解锁占比）+ biz.asset_token_holders
    （Top10 集中度）+ CoinGecko（市值/24h 交易量算换手率）。结果缓存到
    biz.asset_unlock_pressure，6 小时内命中缓存秒级返回。
    """
    from crypto_research.config import get_settings

    settings = get_settings(require_database=True)

    # 0. 读缓存（未过期且非 force）
    if not force:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(_ENSURE_UNLOCK_PRESSURE_SQL)
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute(
                    """SELECT unlock_pct_7d, unlock_pct_30d, next_unlock_date,
                              top10_concentration, turnover_24h, pressure_score,
                              risk_level, detail_json, calculated_at
                       FROM biz.asset_unlock_pressure WHERE asset_id = %s""",
                    (asset_id,),
                )
                row = cur.fetchone()
        if row:
            age = (datetime.now(timezone.utc) - row["calculated_at"]).total_seconds()
            if age < _UNLOCK_PRESSURE_TTL_SECONDS:
                return {
                    "unlock_pct_7d": _pressure_float(row["unlock_pct_7d"]),
                    "unlock_pct_30d": _pressure_float(row["unlock_pct_30d"]),
                    "next_unlock_date": str(row["next_unlock_date"]) if row["next_unlock_date"] else None,
                    "top10_concentration": _pressure_float(row["top10_concentration"]),
                    "turnover_24h": _pressure_float(row["turnover_24h"]),
                    "pressure_score": _pressure_float(row["pressure_score"]),
                    "risk_level": row["risk_level"],
                    "detail": row["detail_json"] or {},
                    "cached": True,
                }

    # 1. 读解锁事件 + 持仓集中度
    with get_db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT unlock_events_json FROM biz.asset_token_unlocks WHERE asset_id = %s",
                (asset_id,),
            )
            urow = cur.fetchone()
            cur.execute(
                "SELECT top_10_pct FROM biz.asset_token_holders WHERE asset_id = %s",
                (asset_id,),
            )
            hrow = cur.fetchone()

    if not urow:
        return None

    events = urow.get("unlock_events_json") or []
    today = datetime.now(timezone.utc).date()
    unlock_pct_7d = 0.0
    unlock_pct_30d = 0.0
    next_unlock_date = None
    for e in events:
        if not e.get("is_upcoming"):
            continue
        d = _parse_unlock_event_date(e.get("date"))
        if d is None or d < today:
            continue
        pct = _pressure_float(e.get("pct")) or 0.0
        delta_days = (d - today).days
        if delta_days <= 7:
            unlock_pct_7d += pct
        if delta_days <= 30:
            unlock_pct_30d += pct
        if next_unlock_date is None or d < next_unlock_date:
            next_unlock_date = d

    top10_concentration = _pressure_float(hrow.get("top_10_pct")) if hrow else None

    # 2. 价格 / 市值 / 24h 交易量 → 换手率
    price_info = _fetch_cg_price(asset_id, settings)
    market_cap_f = _pressure_float(price_info.get("market_cap_usd"))
    volume_f = _pressure_float(price_info.get("volume_24h_usd"))
    turnover_24h = None
    if market_cap_f and volume_f and market_cap_f > 0:
        turnover_24h = round(volume_f / market_cap_f, 4)

    score, risk = _compute_pressure_score(unlock_pct_30d, top10_concentration, turnover_24h)

    detail = {
        "unlock_score": round(min(60.0, unlock_pct_30d * 6.0), 2),
        "concentration_score": round(((top10_concentration or 0.0) / 100.0) * 25.0, 2),
        "liquidity_discount": round(min(15.0, (turnover_24h or 0.0) * 150.0), 2),
        "price_usd": price_info.get("price_usd"),
        "market_cap_usd": price_info.get("market_cap_usd"),
        "volume_24h_usd": price_info.get("volume_24h_usd"),
        "upcoming_events_count": sum(1 for e in events if e.get("is_upcoming")),
    }

    # 3. 写缓存
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(_ENSURE_UNLOCK_PRESSURE_SQL)
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO biz.asset_unlock_pressure
                       (asset_id, unlock_pct_7d, unlock_pct_30d, next_unlock_date,
                        top10_concentration, turnover_24h, pressure_score, risk_level,
                        detail_json, calculated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                   ON CONFLICT (asset_id) DO UPDATE SET
                       unlock_pct_7d = EXCLUDED.unlock_pct_7d,
                       unlock_pct_30d = EXCLUDED.unlock_pct_30d,
                       next_unlock_date = EXCLUDED.next_unlock_date,
                       top10_concentration = EXCLUDED.top10_concentration,
                       turnover_24h = EXCLUDED.turnover_24h,
                       pressure_score = EXCLUDED.pressure_score,
                       risk_level = EXCLUDED.risk_level,
                       detail_json = EXCLUDED.detail_json,
                       calculated_at = NOW()""",
                (asset_id,
                 round(unlock_pct_7d, 2),
                 round(unlock_pct_30d, 2),
                 next_unlock_date,
                 top10_concentration,
                 turnover_24h,
                 score,
                 risk,
                 json.dumps(detail, ensure_ascii=False, default=str)),
            )

    return {
        "unlock_pct_7d": round(unlock_pct_7d, 2),
        "unlock_pct_30d": round(unlock_pct_30d, 2),
        "next_unlock_date": str(next_unlock_date) if next_unlock_date else None,
        "top10_concentration": top10_concentration,
        "turnover_24h": turnover_24h,
        "pressure_score": score,
        "risk_level": risk,
        "detail": detail,
        "cached": False,
    }


def query_token_unlocks(asset_id: int, force: bool = False, log=None) -> dict:
    """按需拉取代币解锁数据（先查缓存，未命中则从 tokenomist 爬取，失败则 AI 测算）。"""
    def _emit(msg: str) -> None:
        if log:
            log(msg)

    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    settings = get_settings(require_database=True)

    # 0. 先查缓存（非 force 模式）
    if not force:
        try:
            with get_connection(settings.database_url) as conn:
                with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                    cur.execute(
                        """SELECT overview_json, unlock_events_json, source_name, slug,
                                  methodology_json, input_snapshot_json, updated_at
                           FROM biz.asset_token_unlocks WHERE asset_id = %s""",
                        (asset_id,),
                    )
                    cached = cur.fetchone()
            if cached:
                _emit("命中缓存，直接返回已缓存的解锁数据")
                overview = cached.get("overview_json") or {}
                events = cached.get("unlock_events_json") or []
                methodology = cached.get("methodology_json") or {}
                input_snapshot = cached.get("input_snapshot_json") or {}
                note = ""
                if isinstance(overview, dict):
                    note = overview.pop("_note", "") or ""
                return {
                    "ok": True,
                    "data": {
                        "status": "ok",
                        "source_name": cached.get("source_name", "缓存"),
                        "slug": cached.get("slug"),
                        "overview": overview,
                        "unlock_events": events,
                        "note": note,
                        "methodology": methodology,
                        "input_snapshot": input_snapshot,
                        "from_cache": True,
                        "cached_at": str(cached.get("updated_at", "")),
                    },
                }
        except Exception as e:
            pass  # 缓存查询失败，继续走实时拉取

    scripts_bin = _get_scripts_bin()
    script = str(scripts_bin / "phase_chain_token_unlocks.py")
    cmd = [
        sys.executable, "-u", script,
        "--asset-id", str(asset_id),
        "--save",
    ]

    _emit("开始拉取代币解锁数据（tokenomist）...")
    stdout, returncode = _run_with_log(cmd, str(scripts_bin), 180, log=log)

    if returncode == -1:
        return {"ok": False, "error": "Tokenomist 爬取超时（180秒），请稍后重试或检查网络"}

    output = stdout.strip()

    if returncode != 0:
        err_msg = output or f"exit code {returncode}"
        # 如果 stderr 中有 Playwright/浏览器相关错误，给出友好提示
        if "Executable doesn't exist" in err_msg or "BrowserType.launch" in err_msg:
            return {"ok": False, "error": "Playwright 浏览器未安装，请运行: playwright install chromium"}
        return {"ok": False, "error": err_msg[:500]}

    if not output:
        return {"ok": False, "error": "无输出"}

    try:
        data = _extract_json_output(output)
        if data is None:
            return {"ok": False, "error": (output or "无输出")[:500]}
        if data.get("status") == "ok":
            _emit("tokenomist 解锁数据拉取成功")
            return {"ok": True, "data": data}
        # tokenomist 没收录 → 让前端询问用户是否提供网址，未提供再走 AI 测算
        if data.get("status") == "not_found":
            _emit(f"tokenomist 未收录: {data.get('message', '')}，等待用户提供网址或改用 AI 测算")
            return {
                "ok": False,
                "needs_url": True,
                "error": data.get("message", "该代币未被 tokenomist 收录"),
                "symbol": data.get("symbol"),
                "name": data.get("name"),
            }
        # 其他错误
        return {"ok": False, "error": data.get("message", "失败")}
    except Exception:
        return {"ok": False, "error": (output or "无输出")[:500]}


def query_unlocks_by_url(asset_id: int, url: str, log=None) -> dict:
    """按用户提供的 tokenomics 网址抓取解锁数据，失败则回退 AI 测算。"""
    def _emit(msg: str) -> None:
        if log:
            log(msg)

    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "网址必须以 http:// 或 https:// 开头"}

    scripts_bin = _get_scripts_bin()
    script = str(scripts_bin / "phase_chain_token_unlocks.py")
    cmd = [
        sys.executable, "-u", script,
        "--asset-id", str(asset_id),
        "--url", url,
        "--save",
    ]

    _emit(f"按用户提供的网址抓取解锁数据: {url}")
    stdout, returncode = _run_with_log(cmd, str(scripts_bin), 180, log=log)

    if returncode == -1:
        return {"ok": False, "error": "网址抓取超时（180秒）"}

    output = stdout.strip()
    if returncode != 0 or not output:
        _emit(f"网址抓取失败（exit {returncode}），回退 AI 测算")
        return _ai_estimate_unlocks(asset_id, "网址抓取失败，tokenomist 未收录", log=log)

    try:
        data = _extract_json_output(output)
        if data is None:
            _emit("网址抓取输出解析失败，回退 AI 测算")
            return _ai_estimate_unlocks(asset_id, "网址抓取输出解析失败", log=log)
        if data.get("status") == "ok":
            _emit("网址抓取解锁数据成功")
            return {"ok": True, "data": data}
        msg = data.get("message", "网址抓取失败")
        _emit(f"网址抓取未获取到数据（{msg}），回退 AI 测算")
        return _ai_estimate_unlocks(asset_id, msg, log=log)
    except Exception:
        _emit("网址抓取输出解析失败，回退 AI 测算")
        return _ai_estimate_unlocks(asset_id, "网址抓取输出解析失败", log=log)


def query_unlocks_ai(asset_id: int, log=None) -> dict:
    """用户未提供网址时，直接触发 AI 测算解锁数据。"""
    def _emit(msg: str) -> None:
        if log:
            log(msg)

    _emit("用户未提供网址，改用 AI 测算解锁数据")
    return _ai_estimate_unlocks(asset_id, "tokenomist 未收录，用户未提供网址", log=log)


def _social_float(v):
    """将 Postgres NUMERIC/其它类型安全转为 float。"""
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return v


def get_asset_social_heat(asset_id: int) -> dict | None:
    """读取已缓存的社交热度数据（只读，不触发拉取）。"""
    with get_db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """SELECT score, confidence, community_json, sentiment_json,
                          trend_json, market_json, score_detail_json,
                          methodology_json, input_snapshot_json, fetched_at
                   FROM biz.asset_social_heat WHERE asset_id = %s""",
                (asset_id,),
            )
            row = cur.fetchone()
    if not row:
        return None
    return {
        "score": _social_float(row.get("score")),
        "confidence": row.get("confidence"),
        "community": row.get("community_json") or {},
        "sentiment": row.get("sentiment_json") or {},
        "trend": row.get("trend_json") or {},
        "market": row.get("market_json") or {},
        "score_detail": row.get("score_detail_json") or {},
        "methodology": row.get("methodology_json") or {},
        "input_snapshot": row.get("input_snapshot_json") or {},
        "fetched_at": str(row.get("fetched_at", "")),
    }


def query_social_heat(asset_id: int, force: bool = False, log=None) -> dict:
    """按需拉取社交热度数据（先查缓存，未命中或强制则运行脚本）。"""
    def _emit(msg: str) -> None:
        if log:
            log(msg)

    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    settings = get_settings(require_database=True)

    # 0. 先查缓存（非 force 模式）
    if not force:
        cached = None
        try:
            with get_connection(settings.database_url) as conn:
                with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                    cur.execute(
                        """SELECT score, confidence, community_json, sentiment_json,
                                  trend_json, market_json, score_detail_json,
                                  methodology_json, input_snapshot_json, fetched_at
                           FROM biz.asset_social_heat WHERE asset_id = %s""",
                        (asset_id,),
                    )
                    cached = cur.fetchone()
        except Exception:
            cached = None

        if cached:
            _emit("命中缓存，直接返回已缓存的社交热度数据")
            return {
                "ok": True,
                "data": {
                    "status": "ok",
                    "score": _social_float(cached.get("score")),
                    "confidence": cached.get("confidence"),
                    "community": cached.get("community_json") or {},
                    "sentiment": cached.get("sentiment_json") or {},
                    "trend": cached.get("trend_json") or {},
                    "market": cached.get("market_json") or {},
                    "score_detail": cached.get("score_detail_json") or {},
                    "methodology": cached.get("methodology_json") or {},
                    "input_snapshot": cached.get("input_snapshot_json") or {},
                    "from_cache": True,
                    "fetched_at": str(cached.get("fetched_at", "")),
                },
            }

    scripts_bin = _get_scripts_bin()
    script = str(scripts_bin / "phase_c_social_heat.py")
    cmd = [sys.executable, "-u", script, "--asset-id", str(asset_id), "--save"]

    _emit("开始拉取社交热度数据（社区规模 + 舆情 + 趋势 + 市场）...")
    stdout, returncode = _run_with_log(cmd, str(scripts_bin), 180, log=log)

    if returncode == -1:
        return {"ok": False, "error": "社交热度拉取超时（180秒），请稍后重试"}

    output = stdout.strip()
    if returncode != 0:
        return {"ok": False, "error": (output or f"exit code {returncode}")[-500:]}

    if not output:
        return {"ok": False, "error": "无输出"}

    try:
        data = _extract_json_output(output)
        if data is None:
            return {"ok": False, "error": (output or "无输出")[-500:]}
        if data.get("status") == "ok":
            _emit("社交热度拉取成功")
            return {"ok": True, "data": data}
        if data.get("status") == "not_found":
            return {"ok": False, "error": data.get("message", "未获取到社交热度数据")}
        return {"ok": False, "error": data.get("message", "失败")}
    except Exception:
        return {"ok": False, "error": (output or "无输出")[-500:]}


AI_UNLOCK_PROMPT = """你是一个加密货币解锁时间表分析专家。根据以下代币经济学数据，估算该代币的解锁时间表。

请返回一个 JSON，格式如下：
{
  "overview": {
    "released_pct": 已释放百分比(数字),
    "total_amount_str": "总供应量",
    "released_amount_str": "已释放量",
    "next_unlock_date": "下一次解锁日期",
    "next_unlock_amount_str": "下一次解锁数量",
    "next_unlock_pct": 下一次解锁占总量百分比(数字),
    "next_unlock_value_str": "下一次解锁估值",
    "market_cap_str": "市值",
    "fdv_str": "完全稀释估值",
    "float_pct": 流通率(数字),
    "allocation": [{"group": "分组名", "pct": 百分比(数字), "cliff_months": 锁定期月数, "vesting_months": 线性释放月数, "tge_unlock_pct": TGE解锁百分比(数字)}]
  },
  "unlock_events": [
    {"date": "2026-08-15", "amount_str": "10M", "pct_of_total": 1.0, "value_str": "$1M", "label": "Team vesting"}
  ],
  "note": "估算方法一句话概述",
  "methodology": {
    "data_sources": ["使用的数据来源"],
    "key_assumptions": {
      "tge_date": "推测的TGE日期及依据",
      "cliff_vesting_rules": {
        "Team": "cliff X月 + vesting Y月，依据...",
        "Investor": "cliff X月 + vesting Y月，依据...",
        "Ecosystem": "释放规则，依据..."
      },
      "release_curve": "线性/阶梯/自定义，依据..."
    },
    "calculation_steps": [
      "步骤1: 根据流通率XX%倒推已释放量 = ...",
      "步骤2: 按月分配释放量...",
      "步骤3: ..."
    ],
    "confidence": "high/medium/low，说明确定性"
  }
}

规则：
1. 如果有明确的解锁时间表（cliff + vesting），按时间线逐月生成 unlock_events（未来12个月的关键事件）
2. 如果没有明确时间表，给出合理的行业常规估计（如 Team 1年cliff + 3年vesting，Investor 1年cliff + 2年vesting）
3. methodology 必须详细记录你是如何得出每个数字的，包括假设依据和计算过程
4. overview 中 released_pct 估算当前已流通比例
5. 所有解锁日期必须以给定的 TGE/上线日期为基准计算（TGE + cliff + vesting）；若 TGE/上线日期未知，必须在 methodology.key_assumptions.tge_date 中说明推测依据，并将 confidence 降为 low
6. 所有解锁事件（unlock_events 及 overview.next_unlock_date）必须严格晚于"当前日期"；由 TGE + cliff + vesting 推导出的解锁日若已早于当前日期，应跳到之后的下一个解锁日，禁止输出历史解锁事件
7. 代币经济学数据:"""


def _fetch_cg_price(asset_id: int, settings) -> dict:
    """从 CoinGecko 获取当前价格、市值、FDV。先查直接映射，失败则按 symbol 搜索。支持重试。

    降级策略：CG 不可达时，从 biz.coin_basic 或 biz.asset_token_unlocks.input_snapshot_json
    里取缓存的市值/FDV/价格，保证抛压评分等下游逻辑在离线环境也能算出有意义的结果。
    """
    import requests
    import time as time_mod
    from crypto_research.db.conn import get_connection

    coin_id = None
    symbol = None

    try:
        with get_connection(settings.database_url) as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                # 1. 先查 CG 直接映射
                cur.execute(
                    """SELECT source_asset_key FROM core.asset_source_map
                       WHERE asset_id = %s AND source_code = 'cg'""",
                    (asset_id,),
                )
                row = cur.fetchone()
                if row:
                    coin_id = row["source_asset_key"]

                # 2. 同时查 symbol（供回退搜索用）
                cur.execute(
                    "SELECT canonical_symbol FROM core.asset WHERE asset_id = %s",
                    (asset_id,),
                )
                asset_row = cur.fetchone()
                if asset_row:
                    symbol = asset_row["canonical_symbol"]
    except Exception as e:
        return {"price_usd": f"DB查询失败: {e}", "market_cap_usd": f"DB查询失败: {e}", "fdv_usd": f"DB查询失败: {e}"}

    # 3. 无直接映射 → 按 symbol 搜索 CG（无 key 优先，限流/失败时回退 key）
    if not coin_id and symbol:
        search_headers_candidates = [{"Accept": "application/json"}]
        for _key in settings.get_coingecko_keys():
            search_headers_candidates.append({
                "Accept": "application/json",
                "x-cg-demo-api-key": _key,
            })
        for headers in search_headers_candidates:
            try:
                search_url = f"{settings.coingecko_base_url}/search"
                params = {"query": symbol.lower()}
                resp = requests.get(search_url, params=params, headers=headers, timeout=10)
                resp.raise_for_status()
                search_data = resp.json()
                coins = search_data.get("coins", [])
                if coins:
                    # 优先精确匹配 symbol
                    exact = [c for c in coins if c.get("symbol", "").lower() == symbol.lower()]
                    best = exact[0] if exact else coins[0]
                    coin_id = best.get("id")
                if coin_id:
                    break
            except Exception:
                continue

    if not coin_id:
        return {"price_usd": "无CG映射", "market_cap_usd": "无CG映射", "fdv_usd": "无CG映射"}

    # 4. 获取价格（无 key 优先，限流/失败时回退 key；带重试）
    url = f"{settings.coingecko_base_url}/simple/price"
    params = {
        "ids": coin_id,
        "vs_currencies": "usd",
        "include_market_cap": "true",
        "include_24hr_vol": "true",
        "include_24hr_change": "false",
        "include_last_updated_at": "false",
    }

    # 请求头候选：无 key 优先（不消耗配额），限流/失败时依次轮替多个 key
    header_candidates = [{"Accept": "application/json"}]
    for _key in settings.get_coingecko_keys():
        header_candidates.append({
            "Accept": "application/json",
            "x-cg-demo-api-key": _key,
        })

    last_error = None
    for headers in header_candidates:
        for attempt in range(3):
            try:
                resp = requests.get(url, params=params, headers=headers, timeout=15)
                resp.raise_for_status()
                data = resp.json()
                coin_data = data.get(coin_id, {})
                return {
                    "price_usd": coin_data.get("usd"),
                    "market_cap_usd": coin_data.get("usd_market_cap"),
                    "fdv_usd": coin_data.get("usd_fully_diluted_valuation"),
                    "volume_24h_usd": coin_data.get("usd_24h_vol"),
                }
            except requests.exceptions.ConnectionError as e:
                last_error = f"连接失败: {e}"
                if attempt < 2:
                    time_mod.sleep(1 * (attempt + 1))
                continue
            except requests.exceptions.Timeout as e:
                last_error = f"超时: {e}"
                if attempt < 2:
                    time_mod.sleep(1 * (attempt + 1))
                continue
            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response is not None else None
                last_error = f"HTTP错误: {status}"
                # 401/403 = key 无效，429 = 配额超限 → 换无 key 公共接口重试
                break
            except Exception as e:
                last_error = f"未知错误: {e}"
                break

    error_msg = last_error or "获取失败"

    # ── 降级：从本地缓存表取市值/FDV/价格（离线环境也能算抛压评分） ──
    try:
        with get_connection(settings.database_url) as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                # 1) 优先从 coin_basic 取（如果有缓存的市值/价格字段）
                cur.execute(
                    """SELECT column_name FROM information_schema.columns
                       WHERE table_schema='biz' AND table_name='coin_basic'
                         AND column_name IN ('price_usd', 'market_cap_usd', 'fdv_usd', 'volume_24h_usd')"""
                )
                cols = [r["column_name"] for r in cur.fetchall()]
                if cols:
                    sel = ", ".join(cols)
                    cur.execute(f"SELECT {sel} FROM biz.coin_basic WHERE asset_id = %s", (asset_id,))
                    cb = cur.fetchone()
                    if cb and any(cb.get(c) for c in cols):
                        result = {
                            "price_usd": cb.get("price_usd"),
                            "market_cap_usd": cb.get("market_cap_usd"),
                            "fdv_usd": cb.get("fdv_usd"),
                            "volume_24h_usd": cb.get("volume_24h_usd"),
                            "_source": "coin_basic_cache",
                            "_error": error_msg,
                        }
                        return result

                # 2) 再从 asset_token_unlocks.input_snapshot_json 里挖
                cur.execute(
                    "SELECT input_snapshot_json FROM biz.asset_token_unlocks WHERE asset_id = %s",
                    (asset_id,),
                )
                urow = cur.fetchone()
                if urow and urow.get("input_snapshot_json"):
                    snap = urow["input_snapshot_json"] or {}
                    ov = snap.get("overview") or {}
                    price = ov.get("price") or ov.get("price_usd")
                    mcap = ov.get("market_cap") or ov.get("market_cap_usd")
                    fdv = ov.get("fdv") or ov.get("fdv_usd")
                    vol = ov.get("volume_24h") or ov.get("volume_24h_usd")
                    if mcap or fdv or price:
                        return {
                            "price_usd": price,
                            "market_cap_usd": mcap,
                            "fdv_usd": fdv,
                            "volume_24h_usd": vol,
                            "_source": "unlock_snapshot_cache",
                            "_error": error_msg,
                        }
    except Exception:
        pass  # 降级失败就返回原始错误

    return {"price_usd": error_msg, "market_cap_usd": error_msg, "fdv_usd": error_msg,
            "volume_24h_usd": error_msg}


def _ai_estimate_unlocks(asset_id: int, tokenomist_error: str, log=None) -> dict:
    """AI 根据代币经济学数据测算解锁信息，保存并返回。"""
    def _emit(msg: str) -> None:
        if log:
            log(msg)

    import datetime
    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection
    from crypto_research.clients.llm_client import LLMClient, extract_json_from_llm_response

    settings = get_settings(require_database=True)
    llm = LLMClient(settings, rpm=30)
    if not llm.is_available():
        return {"ok": False, "error": "LLM 未配置，无法 AI 测算", "tokenomist_error": tokenomist_error}

    _emit("开始 AI 测算解锁数据...")

    # 1. 获取代币经济学数据
    with get_connection(settings.database_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """SELECT total_supply, max_supply, circulating_supply,
                          allocation_json, burn_info, emission_schedule,
                          governance_info, utility_info, confidence, source_urls
                   FROM biz.asset_tokenomics WHERE asset_id = %s""",
                (asset_id,),
            )
            tkn = cur.fetchone()

        if not tkn:
            return {"ok": False, "error": "无代币经济学数据，无法 AI 测算",
                    "tokenomist_error": tokenomist_error}

        # 获取 symbol/name/launch_date（launch_date 作为 TGE/上线日期基准）
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT canonical_symbol, canonical_name, launch_date FROM core.asset WHERE asset_id = %s",
                (asset_id,),
            )
            asset = cur.fetchone()

    _emit("已获取代币经济学数据，正在获取当前价格/市值/FDV...")

    # 1.5 获取 CG 价格/市值/FDV（供 AI 估算解锁价值）
    price_info = _fetch_cg_price(asset_id, settings)

    # 价格数据校验：无有效价格则无法测算，直接报错
    price_usd = price_info.get("price_usd")
    if price_usd is None or isinstance(price_usd, str):
        return {
            "ok": False,
            "error": f"无法获取价格数据，跳过 AI 测算: {price_usd}",
            "tokenomist_error": tokenomist_error,
        }

    _emit(f"价格: {price_info.get('price_usd')} USD, 市值: {price_info.get('market_cap_usd')} USD, FDV: {price_info.get('fdv_usd')} USD")

    # 2. 构建 prompt
    tge_date = asset.get("launch_date") if asset else None
    if hasattr(tge_date, "strftime"):
        tge_date_str = tge_date.strftime("%Y-%m-%d")
    elif tge_date:
        tge_date_str = str(tge_date)
    else:
        tge_date_str = "未知"

    tokenomics_text = f"""
    当前日期: {datetime.date.today().isoformat()}
    代币: {asset['canonical_name']} ({asset['canonical_symbol']})
    TGE/上线日期: {tge_date_str}
    总供应: {tkn.get('total_supply')}
    最大供应: {tkn.get('max_supply')}
    流通供应: {tkn.get('circulating_supply')}
    当前价格: {price_info.get('price_usd', '未获取')} USD
    市值: {price_info.get('market_cap_usd', '未获取')} USD
    完全稀释估值(FDV): {price_info.get('fdv_usd', '未获取')} USD
    分配: {json.dumps(tkn.get('allocation_json'), ensure_ascii=False) if tkn.get('allocation_json') else '无'}
    销毁: {tkn.get('burn_info', '无')}
    排放: {tkn.get('emission_schedule', '无')}
    治理: {tkn.get('governance_info', '无')}
    用途: {tkn.get('utility_info', '无')}
    数据来源: {json.dumps(tkn.get('source_urls', []), ensure_ascii=False)}
    置信度: {tkn.get('confidence')}
    """

    prompt = AI_UNLOCK_PROMPT + "\n" + tokenomics_text

    _emit("调用 LLM 测算解锁时间表...")
    try:
        raw = llm.chat(
            "你是一个加密货币解锁时间表分析专家。只输出 JSON。",
            prompt, temperature=0.1, max_tokens=8192,
        )
    except Exception as e:
        return {"ok": False, "error": f"LLM 调用失败: {e}",
                "tokenomist_error": tokenomist_error}

    _emit("LLM 返回成功，正在解析结果...")

    # 3. 解析 JSON（增强提取：处理 LLM 返回 JSON 前后附带文字的情况）
    try:
        est = extract_json_from_llm_response(raw)
    except Exception as e:
        return {"ok": False, "error": f"AI 返回 JSON 解析失败: {e}",
                "raw": raw[:500], "tokenomist_error": tokenomist_error}

    overview = est.get("overview", {})
    unlock_events = est.get("unlock_events", [])
    note = est.get("note", "")
    methodology = est.get("methodology", {})

    # 规范化字段：AI 返回 pct_of_total，前端统一读 pct
    for e in unlock_events:
        if "pct_of_total" in e and "pct" not in e:
            e["pct"] = e.pop("pct_of_total")

    # 输入数据快照（供后续核验）
    def _safe_float(v):
        """将 Decimal 等类型转为 float，确保 JSON 可序列化。"""
        from decimal import Decimal
        if isinstance(v, Decimal):
            return float(v)
        return v

    input_snapshot = {
        "total_supply": _safe_float(tkn.get("total_supply")),
        "max_supply": _safe_float(tkn.get("max_supply")),
        "circulating_supply": _safe_float(tkn.get("circulating_supply")),
        "tge_date": tge_date_str,
        "price_usd": _safe_float(price_info.get("price_usd")),
        "market_cap_usd": _safe_float(price_info.get("market_cap_usd")),
        "fdv_usd": _safe_float(price_info.get("fdv_usd")),
        "allocation": tkn.get("allocation_json"),
        "burn_info": tkn.get("burn_info"),
        "emission_schedule": tkn.get("emission_schedule"),
        "confidence": _safe_float(tkn.get("confidence")),
        "source_urls": tkn.get("source_urls", []),
    }

    # 4. 保存到数据库
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            # 确保表存在（含新增列）
            cur.execute("""
                CREATE TABLE IF NOT EXISTS biz.asset_token_unlocks (
                    asset_id INTEGER PRIMARY KEY REFERENCES core.asset(asset_id),
                    source_url TEXT,
                    source_name TEXT DEFAULT 'tokenomist',
                    slug TEXT,
                    overview_json JSONB,
                    unlock_events_json JSONB,
                    methodology_json JSONB,
                    input_snapshot_json JSONB,
                    scraped_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            # 兼容旧表：添加可能缺失的列
            for col in ("methodology_json", "input_snapshot_json"):
                try:
                    cur.execute(f"""
                        ALTER TABLE biz.asset_token_unlocks
                        ADD COLUMN IF NOT EXISTS {col} JSONB
                    """)
                except Exception:
                    pass  # 列已存在或表刚创建
            cur.execute("""
                INSERT INTO biz.asset_token_unlocks (
                    asset_id, source_url, source_name, slug,
                    overview_json, unlock_events_json,
                    methodology_json, input_snapshot_json,
                    scraped_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (asset_id) DO UPDATE SET
                    source_url = EXCLUDED.source_url,
                    source_name = EXCLUDED.source_name,
                    slug = EXCLUDED.slug,
                    overview_json = EXCLUDED.overview_json,
                    unlock_events_json = EXCLUDED.unlock_events_json,
                    methodology_json = EXCLUDED.methodology_json,
                    input_snapshot_json = EXCLUDED.input_snapshot_json,
                    updated_at = NOW()
            """, (
                asset_id,
                f"AI估算基于tokenomics数据 asset_id={asset_id}",
                "AI测算",
                f"ai_estimate_{asset_id}",
                json.dumps(overview, ensure_ascii=False),
                json.dumps(unlock_events, ensure_ascii=False),
                json.dumps(methodology, ensure_ascii=False),
                json.dumps(input_snapshot, ensure_ascii=False),
            ))
        conn.commit()

    _emit("AI 测算完成并已保存")
    return {
        "ok": True,
        "data": {
            "source_name": "AI测算",
            "overview": overview,
            "unlock_events": unlock_events,
            "note": note,
            "methodology": methodology,
            "input_snapshot": input_snapshot,
            "asset_id": asset_id,
            "symbol": asset["canonical_symbol"],
            "name": asset["canonical_name"],
        },
    }


def query_holder_snapshot(asset_id: int, chain: str = "bsc", save: bool = True) -> dict:
    """拉取代币持仓分布快照（从区块浏览器爬取）。"""
    import subprocess

    scripts_bin = _get_scripts_bin()
    script = str(scripts_bin / "phase_chain_holder_scrape.py")
    cmd = [
        sys.executable, "-u", script,
        "--asset-id", str(asset_id),
        "--chain", chain,
    ]
    if save:
        cmd.append("--save")

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(scripts_bin),
    )

    output = result.stdout.strip()
    stderr_output = result.stderr.strip() if result.stderr else ""

    if result.returncode != 0:
        err_msg = stderr_output or output or f"exit code {result.returncode}"
        return {"ok": False, "error": err_msg[:500]}

    if not output:
        return {"ok": False, "error": "无输出"}

    try:
        lines = output.strip().split("\n")
        json_lines = [l for l in lines if l.strip().startswith("{")]
        if json_lines:
            data = json.loads(json_lines[-1])
            if data.get("status") == "ok":
                return {"ok": True, "data": data, "stderr": stderr_output[:500]}
            return {"ok": False, "error": str(data.get("message", "失败")), "stderr": stderr_output[:500]}
        return {"ok": False, "error": "无 JSON 输出", "stderr": stderr_output[:500]}
    except json.JSONDecodeError:
        return {"ok": False, "error": (stderr_output or output)[:500]}


def get_token_holders(asset_id: int) -> dict:
    """读取 biz.asset_token_holders 中的持仓分布数据。"""
    with get_db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """SELECT h.*, a.canonical_symbol AS symbol, a.canonical_name AS name
                   FROM biz.asset_token_holders h
                   JOIN core.asset a ON a.asset_id = h.asset_id
                   WHERE h.asset_id = %s""",
                (asset_id,),
            )
            row = cur.fetchone()
    if not row:
        return {"ok": False, "error": "无持仓数据"}

    return {
        "ok": True,
        "data": {
            "asset_id": row["asset_id"],
            "symbol": row["symbol"],
            "name": row["name"],
            "chain": row["chain"],
            "contract_address": row["contract_address"],
            "total_holders": row["total_holders"],
            "top_5_pct": float(row["top_5_pct"]) if row["top_5_pct"] else None,
            "top_10_pct": float(row["top_10_pct"]) if row["top_10_pct"] else None,
            "top_50_pct": float(row["top_50_pct"]) if row["top_50_pct"] else None,
            "top_100_pct": float(row["top_100_pct"]) if row["top_100_pct"] else None,
            "top_holders": json.loads(row["top_holders_json"]) if isinstance(row["top_holders_json"], str) else row["top_holders_json"],
            "tier_distribution": json.loads(row["tier_distribution_json"]) if isinstance(row["tier_distribution_json"], str) else row["tier_distribution_json"],
            "scraped_at": str(row["scraped_at"]) if row["scraped_at"] else None,
        },
    }


# ── 解锁追踪列表（watchlist） ──

def _ensure_watchlist_table(conn) -> None:
    """确保 biz.unlock_watchlist 表存在（新环境自动建表）。"""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS biz.unlock_watchlist (
                watch_id            SERIAL PRIMARY KEY,
                asset_id            INTEGER NOT NULL,
                symbol              TEXT,
                short_plan_note     TEXT,
                target_unlock_date  DATE,
                target_unlock_pct   NUMERIC(8,2),
                entry_price         NUMERIC(24,8),
                last_price          NUMERIC(24,8),
                last_price_at       TIMESTAMPTZ,
                unlock_alert_sent_at TIMESTAMPTZ,
                trend_alert_sent_at  TIMESTAMPTZ,
                created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT uq_watchlist_asset UNIQUE (asset_id),
                CONSTRAINT fk_watchlist_asset
                    FOREIGN KEY (asset_id) REFERENCES core.asset(asset_id)
                    ON DELETE CASCADE
            )
        """)


def add_watchlist(asset_id: int, short_plan_note: str = "",
                  target_unlock_date: str = None,
                  target_unlock_pct: float = None) -> dict:
    """加入解锁追踪列表（记录加入时价格）。"""
    from datetime import date as date_cls
    from crypto_research.config import get_settings

    settings = get_settings(require_database=True)

    unlock_date = None
    if target_unlock_date:
        try:
            unlock_date = date_cls.fromisoformat(target_unlock_date)
        except ValueError:
            return {"ok": False, "error": f"日期格式错误: {target_unlock_date}（应为 YYYY-MM-DD）"}

    with get_db() as conn:
        _ensure_watchlist_table(conn)
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT canonical_symbol AS symbol, canonical_name AS name "
                "FROM core.asset WHERE asset_id = %s",
                (asset_id,),
            )
            asset = cur.fetchone()
        if not asset:
            return {"ok": False, "error": "资产不存在"}

        # 获取加入时价格（失败则留空，由监控脚本后续补）
        entry_price = None
        try:
            price_info = _fetch_cg_price(asset_id, settings)
            p = price_info.get("price_usd")
            if isinstance(p, (int, float)):
                entry_price = p
        except Exception:
            entry_price = None

        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO biz.unlock_watchlist
                    (asset_id, symbol, short_plan_note, target_unlock_date,
                     target_unlock_pct, entry_price, last_price, last_price_at,
                     created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), NOW(), NOW())
                ON CONFLICT (asset_id) DO UPDATE SET
                    short_plan_note = EXCLUDED.short_plan_note,
                    target_unlock_date = EXCLUDED.target_unlock_date,
                    target_unlock_pct = EXCLUDED.target_unlock_pct,
                    entry_price = COALESCE(biz.unlock_watchlist.entry_price, EXCLUDED.entry_price),
                    last_price = EXCLUDED.last_price,
                    last_price_at = NOW(),
                    updated_at = NOW()
            """, (
                asset_id, asset["symbol"], short_plan_note, unlock_date,
                target_unlock_pct, entry_price, entry_price,
            ))
        conn.commit()

    return {"ok": True, "entry_price": entry_price,
            "symbol": asset["symbol"], "name": asset["name"]}


def list_watchlist() -> dict:
    """返回解锁追踪列表（含跌幅、到期天数、临近/逾期标记）。"""
    from datetime import date as date_cls

    today = date_cls.today()
    rows = []
    with get_db() as conn:
        _ensure_watchlist_table(conn)
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("""
                SELECT w.*, a.canonical_name AS name
                FROM biz.unlock_watchlist w
                JOIN core.asset a ON a.asset_id = w.asset_id
                ORDER BY w.target_unlock_date ASC NULLS LAST, w.created_at DESC
            """)
            for r in cur.fetchall():
                entry = r.get("entry_price")
                last = r.get("last_price")
                change_pct = None
                if entry and last:
                    try:
                        change_pct = round((float(last) - float(entry)) / float(entry) * 100, 2)
                    except ZeroDivisionError:
                        change_pct = None
                days_left = None
                if r.get("target_unlock_date"):
                    days_left = (r["target_unlock_date"] - today).days
                rows.append({
                    "watch_id": r["watch_id"],
                    "asset_id": r["asset_id"],
                    "symbol": r["symbol"],
                    "name": r["name"],
                    "short_plan_note": r["short_plan_note"],
                    "target_unlock_date": str(r["target_unlock_date"]) if r["target_unlock_date"] else None,
                    "target_unlock_pct": float(r["target_unlock_pct"]) if r["target_unlock_pct"] is not None else None,
                    "entry_price": float(entry) if entry is not None else None,
                    "last_price": float(last) if last is not None else None,
                    "change_pct": change_pct,
                    "days_left": days_left,
                    "approaching": days_left is not None and 0 <= days_left <= 14,
                    "overdue": days_left is not None and days_left < 0,
                    "created_at": str(r["created_at"]),
                })

    return {"ok": True, "data": rows}


def remove_watchlist(watch_id: int) -> dict:
    """从追踪列表移除。"""
    with get_db() as conn:
        _ensure_watchlist_table(conn)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM biz.unlock_watchlist WHERE watch_id = %s", (watch_id,))
        conn.commit()
    return {"ok": True}
