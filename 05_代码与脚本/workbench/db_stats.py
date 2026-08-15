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
    """按 symbol 或 name 搜索资产，用于下拉自动补全。
    优先查 core.asset，无结果时从 src_cmc 回退并自动入库。
    """
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.asset_id, a.canonical_symbol, a.canonical_name, a.asset_type,
                       cb.cmc_id
                FROM core.asset a
                LEFT JOIN biz.coin_basic cb ON cb.asset_id = a.asset_id
                WHERE a.canonical_symbol ILIKE %s
                   OR a.canonical_name ILIKE %s
                ORDER BY
                    CASE
                        WHEN a.canonical_symbol = UPPER(%s) THEN 0
                        WHEN a.canonical_symbol ILIKE %s THEN 1
                        ELSE 2
                    END,
                    a.canonical_symbol
                LIMIT %s
                """,
                (f"%{query}%", f"%{query}%", query, f"{query}%", limit),
            )
            rows = cur.fetchall()

            if rows:
                return [
                    {
                        "asset_id": row[0],
                        "symbol": row[1],
                        "name": row[2],
                        "type": row[3],
                        "cmc_id": row[4],
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


def _build_doc_sources(doc_source_entries, research_urls, doc_assets, notebooklm_urls) -> list[dict]:
    """把各来源的文档链接合并去重成统一资料清单。"""
    sources = []
    seen = set()

    def _add(entry_type: str, url, title=None):
        url = (url or "").strip()
        if not url or url in seen:
            return
        seen.add(url)
        sources.append({"type": entry_type, "url": url, "title": title or url})

    for e in doc_source_entries:
        _add(e["entry_type"], e["url"])
    for r in research_urls:
        _add(r.get("category") or "research", r["url"], title=r.get("doc_type") or None)
    for d in doc_assets:
        _add("doc_file", d.get("source_url") or d.get("resolved_url"),
             title=d.get("file_name") or d.get("doc_type"))
        if d.get("resolved_url") and d.get("resolved_url") != d.get("source_url"):
            _add("doc_file", d["resolved_url"], title=d.get("file_name") or d.get("doc_type"))
    for u in notebooklm_urls:
        _add("notebooklm", u)
    return sources


def _collect_asset_snapshot(asset_id: int) -> dict | None:
    """收集一个代币的全部投研资料快照（文档入口/文件/精选/结构化数据/合约）。"""
    with get_db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT canonical_symbol, canonical_name, asset_type FROM core.asset WHERE asset_id = %s",
                (asset_id,),
            )
            asset = cur.fetchone()
            if not asset:
                return None

            cur.execute("""
                SELECT entry_id, source_code, entry_type, entry_url, discovered_from, is_primary
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
                    END, entry_id
            """, (asset_id,))
            doc_source_entries = [
                {
                    "entry_id": r["entry_id"],
                    "source": r["source_code"],
                    "entry_type": r["entry_type"],
                    "url": r["entry_url"],
                    "discovered_from": r["discovered_from"],
                    "is_primary": bool(r["is_primary"]),
                }
                for r in cur.fetchall()
            ]

            cur.execute("""
                SELECT doc_id, doc_type, source_url, resolved_url, file_name, mime_type, parse_status
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
                }
                for r in cur.fetchall()
            ]

            cur.execute("""
                SELECT url_id, url, category, doc_type, relevance_score, ai_reason, is_selected
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

    sources = _build_doc_sources(doc_source_entries, research_urls, doc_assets, notebooklm_urls)

    return {
        "asset_id": asset_id,
        "symbol": asset["canonical_symbol"],
        "name": asset["canonical_name"],
        "type": asset["asset_type"],
        "sources": sources,
        "structured": {
            "tokenomics": tokenomics,
            "onchain": onchain,
            "social": social,
            "unlocks": unlocks,
            "contracts": contracts,
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


def _compute_missing_materials(snapshot: dict) -> list[dict]:
    """按投研清单判断还缺哪些资料。"""
    counts = snapshot.get("counts") or {}
    structured = snapshot.get("structured") or {}
    entry_types = set(counts.get("doc_source_entry_types") or [])
    asset_types = set(counts.get("doc_asset_types") or [])
    research_cats = set(counts.get("research_categories") or [])

    items = []

    def _add(key, label, present, note=""):
        items.append({"key": key, "label": label, "present": bool(present), "note": note})

    _add("official_website", "官网", "official_website" in entry_types)
    has_whitepaper = (
        "whitepaper_page" in entry_types
        or "docs" in entry_types
        or "docs_portal" in entry_types
        or "whitepaper" in asset_types
        or "tokenomics" in asset_types
    )
    _add("whitepaper", "白皮书 / 文档", has_whitepaper)
    _add("github", "GitHub 仓库", "github" in entry_types)
    has_audit = ("audit" in asset_types) or any(
        "audit" in (c or "").lower() or "security" in (c or "").lower() for c in research_cats
    )
    _add("audit", "审计报告", has_audit)
    _add("tokenomics", "代币经济学", bool(structured.get("tokenomics")))
    _add("onchain", "链上持仓数据", bool(structured.get("onchain") and structured["onchain"].get("by_chain")))
    _add("social", "社交热度", bool(structured.get("social")))
    _add("unlocks", "代币解锁数据", bool(structured.get("unlocks")))
    _add("contract", "合约地址", bool(structured.get("contracts")))
    return items


_FETCH_TYPES = {"whitepaper_page", "docs", "docs_portal", "official_website", "github", "medium"}
_MAX_DOC_FETCH = 10
_SNIPPET_LIMIT = 2500


def _fetch_url_text(url: str) -> str:
    """抓取 URL 正文文本，失败或非 HTML 返回空字符串（仅保留链接引用）。"""
    import re
    import requests

    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ResearchBot/1.0)"},
            timeout=8,
            allow_redirects=True,
        )
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        if "html" not in ctype.lower() and "text" not in ctype.lower():
            return ""
        text = resp.text
    except Exception:
        return ""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_SNIPPET_LIMIT]


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
    docs = sorted((snapshot.get("sources") or []), key=lambda d: order.get(d.get("type"), 99))

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
        })
    return sources


def _format_research_context(sources: list[dict]) -> str:
    """把引用来源格式化成 LLM 上下文文本。"""
    lines = []
    for i, s in enumerate(sources, 1):
        if s.get("type") == "structured":
            head = f"[{i}] {s['title']}"
        else:
            head = f"[{i}] {s.get('title') or s.get('url')}（类型: {s.get('type')}）"
        lines.append(head)
        if s.get("url"):
            lines.append(f"    链接: {s['url']}")
        snip = (s.get("snippet") or "").strip()
        if snip:
            lines.append(f"    内容: {snip}")
    return "\n".join(lines)


def get_or_create_research_notebook(asset_id: int) -> dict:
    """打开（不存在则创建）一个代币对应的一键投研笔记本，返回资料快照 + 缺失清单 + 历史对话。"""
    with get_db() as conn:
        _ensure_research_tables(conn)
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("SELECT * FROM biz.research_notebook WHERE asset_id = %s", (asset_id,))
            nb = cur.fetchone()

    snapshot = _collect_asset_snapshot(asset_id)
    if not snapshot:
        return {"ok": False, "error": "资产不存在"}
    missing = _compute_missing_materials(snapshot)
    title = f"{snapshot['symbol']} ({snapshot['name']}) 投研笔记"

    with get_db() as conn:
        _ensure_research_tables(conn)
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            if nb:
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

    return {
        "ok": True,
        "data": {
            "notebook_id": notebook["notebook_id"],
            "asset_id": notebook["asset_id"],
            "title": notebook["title"],
            "missing": missing,
            "sources": snapshot["sources"],
            "structured": snapshot["structured"],
            "counts": snapshot["counts"],
            "messages": messages,
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
    """从 CoinGecko 获取当前价格、市值、FDV。先查直接映射，失败则按 symbol 搜索。支持重试。"""
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
        "include_24hr_vol": "false",
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
    return {"price_usd": error_msg, "market_cap_usd": error_msg, "fdv_usd": error_msg}


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
