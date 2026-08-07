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
from pathlib import Path
import psycopg
import psycopg.rows

# Docker 环境下直接用 /app/scripts/src，本地则相对路径计算
if os.path.exists("/app/scripts/src"):
    SCRIPTS_SRC = Path("/app/scripts/src")
else:
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

            # 3.6. DexScreener 补充文档入口
            #    候选集: 无任何 doc_source_entry 的活跃资产（排除衍生品）
            #    done: 已有 dexscreener 来源的 doc_source_entry
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
                    AND dse.source_code = 'dexscreener'
                WHERE a.status = 'active'
            """
            )
            done = cur.fetchone()[0]
            remaining = total - done
            result.append({
                "task": "DexScreener 补充文档入口",
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
                "task": "B2 AI 噪声清理",
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
    """按 symbol 或 name 搜索资产，用于下拉自动补全。"""
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
            return [
                {
                    "asset_id": row[0],
                    "symbol": row[1],
                    "name": row[2],
                    "type": row[3],
                    "cmc_id": row[4],
                }
                for row in cur.fetchall()
            ]


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


def curate_notebooklm(asset_id: int, force: bool = False) -> dict:
    """触发 NotebookLM 精选生成（配额粗筛 + AI 排序）。"""
    import subprocess

    script = str(Path(__file__).resolve().parents[2] / "05_代码与脚本" / "scripts" / "bin" / "curate_notebooklm.py")
    cmd = [
        sys.executable, "-u", script,
        "--asset-id", str(asset_id),
        "--top-n", "50",
    ]
    if force:
        cmd.append("--force")

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(Path(script).parent),
    )

    output = result.stdout.strip()
    stderr = result.stderr.strip() if result.stderr else ""

    json_line = None
    for line in output.splitlines():
        try:
            parsed = json.loads(line)
            if "status" in parsed:
                json_line = parsed
        except json.JSONDecodeError:
            continue

    if json_line:
        return {"ok": True, "data": json_line}
    if stderr:
        return {"ok": False, "error": stderr[:500]}
    return {"ok": False, "error": f"exit code {result.returncode}"}


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


def query_onchain_data(asset_id: int, force: bool = False) -> dict:
    """按需查询链上数据（持仓 + 大额转账），先查缓存，缓存未命中则实时拉取。"""
    import subprocess

    script = str(Path(__file__).resolve().parents[2] / "05_代码与脚本" / "scripts" / "bin" / "phase_chain_onchain_query.py")
    cmd = [
        sys.executable, "-u", script,
        "--asset-id", str(asset_id),
    ]
    if force:
        cmd.append("--force")

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(Path(script).parent),
    )

    output = result.stdout.strip()
    if not output:
        return {"ok": False, "error": "无输出"}

    try:
        data = json.loads(output)
        if data.get("status") == "ok":
            return {"ok": True, "data": data}
        return {"ok": False, "error": data.get("message", "查询失败")}
    except json.JSONDecodeError:
        return {"ok": False, "error": output[:200]}
