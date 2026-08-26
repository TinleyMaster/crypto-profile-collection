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
            max_idle=300,  # 空闲 5 分钟后回收，避免 SSL 连接被中间设备掐断
            check=psycopg_pool.ConnectionPool.check_connection,  # 取连接时健康检查，自动重连
            kwargs={"connect_timeout": 30},
        )
    return _pool


# 市值分层定义（基于 CMC 排名）
MARKET_TIERS = {
    "top100": {"label": "TOP 100", "min_rank": 1, "max_rank": 100},
    "top500": {"label": "TOP 500", "min_rank": 1, "max_rank": 500},
    "top1000": {"label": "TOP 1000", "min_rank": 1, "max_rank": 1000},
    "other": {"label": "长尾", "min_rank": 1001, "max_rank": 999999},
}
MARKET_TIER_ORDER = ["top100", "top500", "top1000", "other"]


def get_market_tier(cmc_rank: int | None, cg_rank: int | None = None) -> str:
    """根据 CMC 排名（或 CG 排名 fallback）计算市值分层。

    优先用 CMC 排名，没有则用 CG 排名兜底。无排名返回 'other'。
    """
    rank = cmc_rank or cg_rank
    if not rank:
        return "other"
    try:
        r = int(rank)
    except (TypeError, ValueError):
        return "other"
    if r <= 100:
        return "top100"
    if r <= 500:
        return "top500"
    if r <= 1000:
        return "top1000"
    return "other"


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


_dashboard_cache = None
_dashboard_cache_ts = 0
_DASHBOARD_CACHE_TTL = 60  # 秒


def get_dashboard_stats() -> dict:
    """返回仪表盘需要的全部统计数据（60秒缓存）。"""
    global _dashboard_cache, _dashboard_cache_ts
    import time
    now = time.time()
    if _dashboard_cache and (now - _dashboard_cache_ts) < _DASHBOARD_CACHE_TTL:
        return _dashboard_cache

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

    _dashboard_cache = result
    _dashboard_cache_ts = now
    return result


def get_coverage_by_tier() -> dict:
    """按市值分层统计各维度数据覆盖率。

    统计维度：白皮书/文档、代币经济、社交热度、解锁数据、衍生品、链上持仓。

    Returns:
        {
            "ok": bool,
            "tiers": {
                "top100": {
                    "total": int,
                    "whitepaper": {"count": int, "pct": float},
                    "tokenomics": {"count": int, "pct": float},
                    "social_heat": {"count": int, "pct": float},
                    "unlocks": {"count": int, "pct": float},
                    "derivatives": {"count": int, "pct": float},
                    "onchain_holders": {"count": int, "pct": float},
                },
                ...
            }
        }
    """
    tiers = [
        ("top100", "COALESCE(cm.rank_num, ci.market_cap_rank, 999999) <= 100"),
        ("top500", "COALESCE(cm.rank_num, ci.market_cap_rank, 999999) <= 500"),
        ("top1000", "COALESCE(cm.rank_num, ci.market_cap_rank, 999999) <= 1000"),
        ("other", "COALESCE(cm.rank_num, ci.market_cap_rank, 999999) > 1000"),
        ("all", "1=1"),
    ]

    result = {}
    with get_db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            for tier_name, tier_cond in tiers:
                cur.execute(
                    f"""
                    SELECT
                        COUNT(*) AS total,
                        -- 白皮书/文档
                        COUNT(CASE WHEN wp.asset_id IS NOT NULL THEN 1 END) AS whitepaper,
                        -- 代币经济
                        COUNT(CASE WHEN tk.asset_id IS NOT NULL THEN 1 END) AS tokenomics,
                        -- 社交热度
                        COUNT(CASE WHEN sh.asset_id IS NOT NULL THEN 1 END) AS social_heat,
                        -- 解锁数据
                        COUNT(CASE WHEN ul.asset_id IS NOT NULL THEN 1 END) AS unlocks,
                        -- 衍生品
                        COUNT(CASE WHEN dv.asset_id IS NOT NULL THEN 1 END) AS derivatives,
                        -- 链上持仓
                        COUNT(CASE WHEN oh.asset_id IS NOT NULL THEN 1 END) AS onchain_holders
                    FROM biz.coin_basic cb
                    LEFT JOIN src_cmc.cmc_asset_map cm ON cm.cmc_id = cb.cmc_id
                    LEFT JOIN core.asset_source_map asm ON asm.asset_id = cb.asset_id
                        AND asm.source_code = 'cg' AND asm.is_primary = TRUE
                    LEFT JOIN src_cg.coin_info ci ON ci.coin_id = asm.source_asset_key
                    LEFT JOIN (
                        SELECT DISTINCT asset_id FROM biz.doc_source_entry
                         WHERE entry_type IN ('whitepaper', 'whitepaper_page')
                    ) wp ON wp.asset_id = cb.asset_id
                    LEFT JOIN (
                        SELECT DISTINCT asset_id FROM biz.asset_tokenomics
                    ) tk ON tk.asset_id = cb.asset_id
                    LEFT JOIN biz.asset_social_heat sh ON sh.asset_id = cb.asset_id
                    LEFT JOIN biz.asset_unlock_event ul ON ul.asset_id = cb.asset_id
                    LEFT JOIN biz.asset_derivatives dv ON dv.asset_id = cb.asset_id
                    LEFT JOIN biz.onchain_holder_snapshot oh ON oh.asset_id = cb.asset_id
                    WHERE {tier_cond}
                    """
                )
                row = cur.fetchone()
                total = row["total"] or 0

                def _pct(cnt):
                    return round(cnt / total * 100, 1) if total > 0 else 0.0

                result[tier_name] = {
                    "total": total,
                    "whitepaper": {"count": row["whitepaper"] or 0, "pct": _pct(row["whitepaper"])},
                    "tokenomics": {"count": row["tokenomics"] or 0, "pct": _pct(row["tokenomics"])},
                    "social_heat": {"count": row["social_heat"] or 0, "pct": _pct(row["social_heat"])},
                    "unlocks": {"count": row["unlocks"] or 0, "pct": _pct(row["unlocks"])},
                    "derivatives": {"count": row["derivatives"] or 0, "pct": _pct(row["derivatives"])},
                    "onchain_holders": {"count": row["onchain_holders"] or 0, "pct": _pct(row["onchain_holders"])},
                }

    return {"ok": True, "tiers": result}


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
                -- 按北京时间计"今日"，与调度时区对齐
                WHERE hs.snapshot_date = (CURRENT_DATE AT TIME ZONE 'Asia/Shanghai')::date
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


def search_assets(query: str, limit: int = 20, tier: str | None = None) -> list[dict]:
    """按 symbol / name / 合约地址搜索资产，用于下拉自动补全。
    优先查 core.asset，无结果时从 src_cmc 回退并自动入库。

    合约地址匹配：EVM（0x 开头）大小写不敏感（含部分匹配）；非 EVM（如 Solana
    base58）精确匹配（大小写敏感）。

    tier: 市值分层过滤（top100/top500/top1000/other），None 表示不过滤。
    """
    try:
        return _search_assets_inner(query, limit, tier)
    except Exception as e:
        return [{"error": str(e), "notice": "搜索失败，请检查数据库连接"}]


def _search_assets_inner(query: str, limit: int = 20, tier: str | None = None) -> list[dict]:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH latest_cmc AS (
                    SELECT cmc_id, market_cap
                    FROM src_cmc.cmc_asset_quote_snapshot
                    WHERE quote_time = (
                        SELECT MAX(quote_time) FROM src_cmc.cmc_asset_quote_snapshot
                    )
                )
                SELECT a.asset_id, a.canonical_symbol, a.canonical_name, a.asset_type,
                       cb.cmc_id, a.primary_sector,
                       ci.market_cap_rank,
                       cam.rank_num AS cmc_rank,
                       COALESCE(cqs.market_cap, a.market_cap) AS market_cap
                FROM core.asset a
                LEFT JOIN biz.coin_basic cb ON cb.asset_id = a.asset_id
                LEFT JOIN src_cmc.cmc_asset_map cam ON cam.cmc_id = cb.cmc_id
                LEFT JOIN core.asset_source_map asm ON asm.asset_id = a.asset_id
                    AND asm.source_code = 'cg' AND asm.is_primary = TRUE
                LEFT JOIN src_cg.coin_info ci ON ci.coin_id = asm.source_asset_key
                LEFT JOIN latest_cmc cqs ON cqs.cmc_id = cb.cmc_id
                WHERE (
                    a.canonical_symbol ILIKE %s
                    OR a.canonical_name ILIKE %s
                    OR EXISTS (
                        SELECT 1 FROM core.asset_contract ac
                        WHERE ac.asset_id = a.asset_id
                          AND (
                              (LEFT(ac.contract_address, 2) = '0x' AND LOWER(ac.contract_address) LIKE LOWER(%s))
                              OR (LEFT(ac.contract_address, 2) <> '0x' AND ac.contract_address = %s)
                          )
                    )
                )
                AND CASE %s
                        WHEN 'top100' THEN COALESCE(cam.rank_num, ci.market_cap_rank, 999999) <= 100
                        WHEN 'top500' THEN COALESCE(cam.rank_num, ci.market_cap_rank, 999999) <= 500
                        WHEN 'top1000' THEN COALESCE(cam.rank_num, ci.market_cap_rank, 999999) <= 1000
                        WHEN 'other' THEN COALESCE(cam.rank_num, ci.market_cap_rank, 999999) > 1000
                        ELSE TRUE
                    END
                ORDER BY
                    -- 市值权重优先：排名越小越靠前。修复 P1-1：避免 meme 币（symbol=BITCOIN）的
                    -- 精确符号匹配压过真币（symbol=BTC, rank=1）。搜索结果整体按市值排名排序，
                    -- 精确符号/名称匹配仅作为同排名区间内的次级加权。
                    COALESCE(cam.rank_num, ci.market_cap_rank, 999999),
                    CASE
                        WHEN EXISTS (
                            SELECT 1 FROM core.asset_contract ac
                            WHERE ac.asset_id = a.asset_id
                              AND (
                                  (LEFT(ac.contract_address, 2) = '0x' AND LOWER(ac.contract_address) = LOWER(%s))
                                  OR (LEFT(ac.contract_address, 2) <> '0x' AND ac.contract_address = %s)
                              )
                        ) THEN 0
                        -- P1-1 (2.3) 防御性加固：同 rank（如 999999 蹭名币）区间，精确符号匹配
                        -- 先于精确名称匹配前置，便于“真币”在撞名组中排在最前。
                        WHEN LOWER(a.canonical_symbol) = LOWER(%s) THEN 1
                        WHEN LOWER(a.canonical_name) = LOWER(%s) THEN 2
                        WHEN a.canonical_symbol ILIKE %s THEN 3
                        WHEN a.canonical_name ILIKE %s THEN 4
                        ELSE 5
                    END,
                    -- 市值权重：优先 CMC 排名（越小越靠前），其次 CG 排名，无排名用市值降序
                    COALESCE(cam.rank_num, ci.market_cap_rank, 999999),
                    COALESCE(cqs.market_cap, a.market_cap, 0) DESC,
                    a.canonical_symbol
                LIMIT %s
                """,
                (f"%{query}%", f"%{query}%", f"%{query}%", query,
                 tier or "",
                 query, query, query, query, f"%{query}%", f"%{query}%", limit),
            )
            rows = cur.fetchall()

            # P1-1 (2.2) 消歧标注：同符号多个匹配时，仅 rank 最小者标 is_canonical=True，
            # 供前端下拉区分“真币”与“蹭名币”，无需盲取第一个。
            canonical_ids: set = set()
            sym_groups: dict = {}
            for row in rows:
                rk = row[7] if row[7] is not None else (row[6] if row[6] is not None else 999999)
                mc = float(row[8]) if row[8] is not None else 0.0
                sym_groups.setdefault((row[1] or "").lower(), []).append((rk, mc, row[0]))
            for members in sym_groups.values():
                # rank 升序 -> 市值降序 -> asset_id 升序，取首个为 canonical
                members.sort(key=lambda m: (m[0], -m[1], m[2]))
                canonical_ids.add(members[0][2])

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
                        "cmc_rank": row[7],
                        "market_cap": float(row[8]) if row[8] else None,
                        "market_tier": get_market_tier(row[7], row[6]),
                        "market_tier_label": MARKET_TIERS[get_market_tier(row[7], row[6])]["label"],
                        "is_canonical": row[0] in canonical_ids,
                    }
                    for row in rows
                ]

        # ── 回退：从 src_cmc 搜索并自动入库 ──
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.cmc_id, m.symbol, m.name, m.slug, m.platform_name,
                       i.category_hint, i.tags
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
                tags = row[6]

                # 多信号交叉判定 asset_type（稳定币优先），与 cmc_asset_bootstrap.classify_asset_type 保持一致
                _hint = (category_hint or "").strip().lower()
                _tagset = set(t.lower() for t in (tags or []))
                _STABLE_FALSE = {"stable ecosystem", "stablecoin issuer"}
                _STRONG_STABLE = {
                    "stablecoin", "usd-stablecoin", "asset-backed-stablecoin",
                    "fiat-stablecoin", "fiat-backed-stablecoin", "algorithmic-stablecoin",
                    "crypto-backed-stablecoin", "yield-bearing-stablecoin",
                    "eur-stablecoin", "krw-stablecoin",
                }
                _SYMBOL_STABLE = {"USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE"}
                if (
                    _hint == "stablecoin"
                    or (symbol or "").strip().upper() in _SYMBOL_STABLE
                    or (_tagset & _STRONG_STABLE)
                ):
                    asset_type = "stablecoin"
                elif "meme" in _hint or (_tagset & {"memes", "meme", "memecoin"}):
                    asset_type = "meme"
                else:
                    asset_type = "token" if platform_name else "coin"

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
                    "local_url": f"/api/docs/{r[7]}" if r[7] else None,
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
    """获取资产的代币经济学结构化数据（含 tokenomics.com 的收入/估值子板块）。

    supply 安全校验：total_supply / circulating_supply / max_supply 会与
    CMC 权威快照对比，偏离 >10 倍时自动用权威值覆盖，防止单位错误传导。
    """
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

            # supply 安全校验：从 CMC 权威快照取基准值
            auth_supply = {}
            try:
                cur.execute(
                    """
                    SELECT q.total_supply, q.circulating_supply, q.max_supply
                    FROM biz.coin_basic cb
                    JOIN src_cmc.cmc_asset_quote_snapshot q ON q.cmc_id = cb.cmc_id
                    WHERE cb.asset_id = %s
                      AND q.quote_time = (SELECT MAX(quote_time) FROM src_cmc.cmc_asset_quote_snapshot)
                    """,
                    (asset_id,),
                )
                auth_row = cur.fetchone()
                if auth_row:
                    auth_supply = {
                        k: auth_row[k]
                        for k in ("total_supply", "circulating_supply", "max_supply")
                        if auth_row[k] is not None
                    }
            except (psycopg.errors.UndefinedTable, Exception):
                pass

            def _safe_supply(tok_key: str):
                """supply 字段安全取值：偏离权威值 >10 倍则用权威值覆盖。"""
                tok_val = row[tok_key]
                auth_val = auth_supply.get(tok_key)
                if auth_val is None:
                    return tok_val
                if tok_val is None:
                    return auth_val
                try:
                    tv = float(tok_val)
                    av = float(auth_val)
                    if av > 0 and (tv / av > 10 or tv / av < 0.1):
                        return auth_val  # 单位疑似错误，用权威值覆盖
                except (ValueError, TypeError, ZeroDivisionError):
                    pass
                return tok_val

            return {
                "total_supply": _safe_supply("total_supply"),
                "max_supply": _safe_supply("max_supply"),
                "circulating_supply": _safe_supply("circulating_supply"),
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


def get_whitepaper_summary(asset_id: int) -> list[dict]:
    """获取资产的白皮书结构化摘要列表。"""
    with get_db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """
                SELECT s.id, s.doc_id, s.one_liner, s.summary_short, s.summary_long,
                       s.problem_statement, s.solution, s.core_mechanism,
                       s.key_innovations, s.tech_stack, s.token_utility,
                       s.tokenomics_notes, s.team_info, s.investors, s.funding_info,
                       s.roadmap, s.key_milestones, s.risks, s.challenges,
                       s.confidence, s.extraction_notes,
                       d.file_name, d.storage_path
                FROM biz.doc_whitepaper_summary s
                JOIN biz.doc_asset d ON d.doc_id = s.doc_id
                WHERE s.asset_id = %s
                ORDER BY s.confidence DESC NULLS LAST, s.id
                """,
                (asset_id,),
            )
            rows = cur.fetchall()
            return [
                {
                    "id": r["id"],
                    "doc_id": r["doc_id"],
                    "file_name": r["file_name"],
                    "local_url": f"/api/docs/{r['storage_path']}" if r["storage_path"] else None,
                    "one_liner": r["one_liner"],
                    "summary_short": r["summary_short"],
                    "summary_long": r["summary_long"],
                    "problem_statement": r["problem_statement"],
                    "solution": r["solution"],
                    "core_mechanism": r["core_mechanism"],
                    "key_innovations": r["key_innovations"] or [],
                    "tech_stack": r["tech_stack"] or [],
                    "token_utility": r["token_utility"],
                    "tokenomics_notes": r["tokenomics_notes"],
                    "team_info": r["team_info"],
                    "investors": r["investors"] or [],
                    "funding_info": r["funding_info"],
                    "roadmap": r["roadmap"],
                    "key_milestones": r["key_milestones"] or [],
                    "risks": r["risks"] or [],
                    "challenges": r["challenges"],
                    "confidence": float(r["confidence"]) if r["confidence"] is not None else None,
                    "extraction_notes": r["extraction_notes"],
                }
                for r in rows
            ]


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
                    ON DELETE CASCADE,
                CONSTRAINT uq_research_thesis_asset_notebook
                    UNIQUE (asset_id, source_notebook_id)
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
                SELECT doc_id, doc_type, source_url, resolved_url, file_name, mime_type,
                       storage_path, parse_status, content_topics
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
                    "storage_path": r["storage_path"],
                    "local_url": f"/api/docs/{r['storage_path']}" if r["storage_path"] else None,
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


def _augment_structured_links(items: list[dict], structured: dict) -> None:
    """为结构化数据类型补充摘要条目（无 doc_source_entry 链接时）。

    合约地址 → 生成区块浏览器链接
    链上持仓 → 展示持有者数/Top10 集中度 + 区块浏览器 holder 页
    社交热度 → 展示评分 + X/twitter 链接（如有）
    代币经济学 → 展示流通量/总量/燃烧机制摘要
    解锁数据 → 展示未来 30 天解锁比例/事件数

    全局 try/except 兜底：单类异常不影响 notebook 整体返回。
    """
    try:
        _augment_structured_links_inner(items, structured)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            f"_augment_structured_links failed: {e}"
        )


def _augment_structured_links_inner(items: list[dict], structured: dict) -> None:
    if not structured:
        return

    item_map = {it["key"]: it for it in items}

    # 1. 合约地址
    it = item_map.get("contract_address")
    if it and it["present"] and not it["links"]:
        contracts = structured.get("contracts") or []
        for c in contracts:
            chain = c.get("chain") or ""
            addr = c.get("contract_address") or c.get("address") or ""
            if not addr:
                continue
            explorer_url = _chain_explorer_token_url(chain, addr)
            label = f"{chain}: {addr[:8]}…{addr[-6:]}"
            if c.get("is_primary"):
                label += "（主合约）"
            it["links"].append({
                "title": label,
                "url": explorer_url,
                "is_structured": True,
            })

    # 2. 链上持仓数据
    it = item_map.get("onchain_holder_data")
    if it and it["present"] and not it["links"]:
        onchain = structured.get("onchain") or {}
        by_chain = onchain.get("by_chain") or {}
        for chain, data in by_chain.items():
            if isinstance(data, list) and data:
                latest = data[-1] if isinstance(data[-1], dict) else {}
            elif isinstance(data, dict):
                latest = data
            else:
                latest = {}
            holders = _to_float(latest.get("total_holders"))
            top10 = _to_float(latest.get("top10_concentration"))
            parts = []
            if holders is not None:
                parts.append(f"持有者 {holders:,.0f}")
            if top10 is not None:
                parts.append(f"Top10 集中度 {top10}%")
            label = f"{chain}: " + (" / ".join(parts) if parts else "已采集")
            # 尝试生成区块浏览器 holder 页链接
            contracts = structured.get("contracts") or []
            primary = next((c for c in contracts if c.get("chain") == chain and c.get("is_primary")), None)
            url = ""
            if primary:
                url = _chain_explorer_holder_url(chain, primary.get("address", ""))
            it["links"].append({
                "title": label,
                "url": url,
                "is_structured": True,
            })

    # 3. 社交热度
    it = item_map.get("social_heat")
    if it and it["present"] and not it["links"]:
        social = structured.get("social") or {}
        score = social.get("score")
        sentiment = social.get("sentiment_score")
        parts = []
        if score is not None:
            parts.append(f"社交热度分 {score}")
        if sentiment is not None:
            parts.append(f"情绪分 {sentiment}")
        cj = social.get("community_json") or {}
        x_url = ""
        for plat in ("x", "twitter", "X"):
            if plat in cj and cj[plat].get("url"):
                x_url = cj[plat]["url"]
                break
            if plat in cj and cj[plat].get("username"):
                x_url = f"https://x.com/{cj[plat]['username']}"
                break
        label = " / ".join(parts) if parts else "已采集"
        it["links"].append({
            "title": label,
            "url": x_url,
            "is_structured": True,
        })

    # 4. 代币经济学
    it = item_map.get("tokenomics")
    if it and it["present"] and not it["links"]:
        tok = structured.get("tokenomics") or {}
        parts = []
        circ = _to_float(tok.get("circulating_supply"))
        if circ is not None:
            parts.append(f"流通量 {circ:,.0f}")
        total = _to_float(tok.get("total_supply"))
        if total is not None:
            parts.append(f"总量 {total:,.0f}")
        if tok.get("burn_info"):
            bi = tok["burn_info"]
            if isinstance(bi, dict) and bi.get("burned_pct") is not None:
                parts.append(f"已燃烧 {bi['burned_pct']}%")
            elif isinstance(bi, str):
                parts.append(f"燃烧: {bi[:30]}")
        label = " / ".join(parts) if parts else "已采集"
        it["links"].append({
            "title": label,
            "url": "",
            "is_structured": True,
        })

    # 5. 解锁数据（补充摘要，同时解决 P1-4「0事件误判为缺失」）
    it = item_map.get("token_unlock_data")
    if it and it["present"] and not it["links"]:
        unlocks = structured.get("unlocks")
        if isinstance(unlocks, dict):
            events = unlocks.get("events") or unlocks.get("unlock_events_json") or []
            upcoming = [e for e in events if e.get("is_upcoming")]
            if upcoming:
                next_ev = upcoming[0]
                label = f"未来 30 天 {len(upcoming)} 次解锁 · 下一次: {next_ev.get('date', '?')}"
                if next_ev.get("pct") is not None:
                    label += f"（{next_ev['pct']}%）"
            else:
                label = "已采集 · 近期无解锁事件"
                # 0 事件也是有效数据，标记 note 让前端知道不是缺失
                it["note"] = "no_upcoming_events"
            it["links"].append({
                "title": label,
                "url": "",
                "is_structured": True,
            })


def _chain_explorer_token_url(chain: str, address: str) -> str:
    """根据链名生成代币区块浏览器链接。"""
    if not address:
        return ""
    chain_l = (chain or "").lower()
    if chain_l == "solana":
        return f"https://solscan.io/token/{address}"
    if chain_l == "ethereum":
        return f"https://etherscan.io/token/{address}"
    if chain_l == "bsc" or chain_l == "bnb":
        return f"https://bscscan.com/token/{address}"
    if chain_l == "base":
        return f"https://basescan.org/token/{address}"
    if chain_l == "arbitrum":
        return f"https://arbiscan.io/token/{address}"
    if chain_l == "polygon":
        return f"https://polygonscan.com/token/{address}"
    if chain_l == "avalanche":
        return f"https://snowtrace.io/token/{address}"
    if chain_l == "optimism":
        return f"https://optimistic.etherscan.io/token/{address}"
    # 默认返回地址（无链接）
    return ""


def _chain_explorer_holder_url(chain: str, address: str) -> str:
    """根据链名生成代币持有者页面链接。"""
    if not address:
        return ""
    chain_l = (chain or "").lower()
    if chain_l == "solana":
        return f"https://solscan.io/token/{address}#holders"
    if chain_l in ("ethereum", "bsc", "bnb", "base", "arbitrum", "polygon", "avalanche", "optimism"):
        # Etherscan 系列
        base = _chain_explorer_token_url(chain, address)
        return base + "#balances" if base else ""
    return ""


def _compute_missing_materials(snapshot: dict) -> list[dict]:
    """按完整投研清单判断每类资料的收集状态。

    前 9 类用结构化数据/来源类型精确判定；后 12 类用 content_topics
    内容主题精确判定（不再依赖 URL/标题关键词猜测）。

    全局 try/except 兜底：异常时返回空列表，不让 notebook 500。
    """
    try:
        return _compute_missing_materials_inner(snapshot)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            f"_compute_missing_materials failed: {e}"
        )
        return []


def _compute_missing_materials_inner(snapshot: dict) -> list[dict]:
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
    # 结构化数据类型的"已收集数量"（不是文档链接数，而是数据条目数/链数等）
    structured_counts = {}
    if tokenomics:
        structured_counts["tokenomics"] = 1
    if onchain and onchain.get("by_chain"):
        structured_counts["onchain_holder_data"] = len(onchain["by_chain"])
    if structured.get("social"):
        structured_counts["social_heat"] = 1
    if unlocks:
        events = unlocks.get("events") or unlocks.get("unlock_events") or unlocks.get("unlock_events_json") or []
        structured_counts["token_unlock_data"] = len(events) if isinstance(unlocks, dict) else 1
    if structured.get("contracts"):
        structured_counts["contract_address"] = len(structured["contracts"])
    if structured.get("raises"):
        structured_counts["team_vc"] = len(structured["raises"])
    if structured.get("exchanges"):
        structured_counts["exchange_listing"] = len(structured["exchanges"])

    items = []
    for spec in RESEARCH_MATERIAL_TYPES:
        key = spec["key"]
        links = material_links.get(key, [])
        count = structured_counts.get(key, len(links))
        items.append({
            "key": key,
            "label": spec["label"],
            "description": spec["description"],
            "present": bool(present.get(key)),
            "count": count,
            "note": "",
            "links": links,
        })

    # ── 结构化数据补充摘要链接 ──
    # 合约/链上/社交/代币经济 等类型只有结构化数据、没有 doc_source_entry 链接，
    # 前端会显示「已收集(N份) 暂无链接」造成矛盾。这里从 structured 生成
    # 摘要条目（带外链或纯文本摘要），让用户点开能看到实际数据。
    _augment_structured_links(items, structured)

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


def _to_float(v) -> float | None:
    """安全地将任意类型转为 float，失败返回 None。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        v = v.strip()
        if not v:
            return None
        try:
            return float(v)
        except (ValueError, TypeError):
            return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _build_structured_metrics_from_snapshot(snapshot: dict, asset_id: int) -> dict:
    """从 snapshot.structured 实时拼装结构化指标，与 competitors 接口同源。

    用于 notebook 读取时覆盖 thesis 里的旧快照市场数据，
    确保页面顶部关键指标与竞品对比表数字一致。

    所有数值统一转为 float，避免 JSON 字段取回为字符串导致前端格式化崩溃。
    全局 try/except 兜底：单字段异常不影响 notebook 整体返回。
    """
    try:
        return _build_structured_metrics_inner(snapshot, asset_id)
    except Exception as e:
        # 任何异常都返回空结构，不让 notebook 500
        import logging
        logging.getLogger(__name__).warning(
            f"_build_structured_metrics_from_snapshot failed for asset {asset_id}: {e}"
        )
        return {
            "market": {},
            "tokenomics": {},
            "unlock": {},
            "onchain": {},
            "social": {},
            "pressure": {},
        }


def _build_structured_metrics_inner(snapshot: dict, asset_id: int) -> dict:
    structured = snapshot.get("structured") or {}
    tokenomics = structured.get("tokenomics")
    unlocks = structured.get("unlocks")
    social = structured.get("social")
    onchain = structured.get("onchain")

    result = {
        "market": {},
        "tokenomics": {},
        "unlock": {},
        "onchain": {},
        "social": {},
        "derivatives": {},
        "pressure": {},
    }

    # ── 市场数据（多源 fallback：unlock > social > CMC快照 > tokenomics推算）──
    # 优先级与 get_sector_competitors 完全对齐
    market_price = None
    market_mcap = None
    market_fdv = None
    market_snapshot_time = None

    def _extract_market(snap: dict) -> tuple:
        """从快照 dict 提取价格/市值/FDV/时间，统一转 float。"""
        p = _to_float(snap.get("price") or snap.get("price_usd"))
        m = _to_float(snap.get("market_cap") or snap.get("market_cap_usd"))
        f = _to_float(snap.get("fdv") or snap.get("fdv_usd") or snap.get("fully_diluted_valuation"))
        t = snap.get("snapshot_time") or snap.get("updated_at") or snap.get("quote_time")
        return p, m, f, t

    # 1. unlock input_snapshot
    if isinstance(unlocks, dict):
        snap = unlocks.get("input_snapshot_json") or {}
        p, m, f, t = _extract_market(snap)
        if p is not None or m is not None or f is not None:
            market_price, market_mcap, market_fdv, market_snapshot_time = p, m, f, t

    # 2. social_heat market_json
    if (market_price is None and market_mcap is None) and isinstance(social, dict):
        mj = social.get("market_json") or {}
        p, m, f, t = _extract_market(mj)
        if p is not None or m is not None or f is not None:
            market_price, market_mcap, market_fdv, market_snapshot_time = p, m, f, t

    # 3. CMC 最新报价快照（psycopg 返回的是数值类型，无需转）
    if market_price is None and market_mcap is None:
        try:
            with get_db() as conn:
                with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                    cur.execute("""
                        SELECT cb.asset_id, q.price_usd, q.market_cap, q.fdv, q.quote_time
                        FROM biz.coin_basic cb
                        JOIN src_cmc.cmc_asset_quote_snapshot q ON q.cmc_id = cb.cmc_id
                        WHERE cb.asset_id = %s
                          AND q.quote_time = (SELECT MAX(quote_time) FROM src_cmc.cmc_asset_quote_snapshot)
                    """, (asset_id,))
                    row = cur.fetchone()
                    if row:
                        market_price = _to_float(row.get("price_usd"))
                        market_mcap = _to_float(row.get("market_cap"))
                        market_fdv = _to_float(row.get("fdv"))
                        if row.get("quote_time"):
                            market_snapshot_time = str(row.get("quote_time"))
        except (psycopg.errors.UndefinedTable, Exception):
            pass

    # 4. tokenomics 推算
    if market_mcap is None and tokenomics:
        price = _to_float(tokenomics.get("price_usd"))
        circ = _to_float(tokenomics.get("circulating_supply"))
        total = _to_float(tokenomics.get("total_supply"))
        if price is not None and circ is not None:
            market_price = price
            market_mcap = price * circ
        if market_fdv is None and price is not None and total is not None:
            market_fdv = price * total

    if market_price is not None:
        result["market"]["price_usd"] = market_price
    if market_mcap is not None:
        result["market"]["market_cap_usd"] = market_mcap
    if market_fdv is not None:
        result["market"]["fdv_usd"] = market_fdv
    if market_snapshot_time is not None:
        result["market"]["snapshot_time"] = market_snapshot_time

    # ── 代币经济学 ──
    if tokenomics:
        for key in ("total_supply", "circulating_supply", "max_supply"):
            v = _to_float(tokenomics.get(key))
            if v is not None:
                result["tokenomics"][key] = v
        for key in ("buy_tax_pct", "sell_tax_pct"):
            v = _to_float(tokenomics.get(key))
            if v is not None:
                result["tokenomics"][key] = v

    # ── 解锁 ──
    if isinstance(unlocks, dict):
        events = unlocks.get("events") or unlocks.get("unlock_events") or unlocks.get("unlock_events_json") or []
        upcoming = [e for e in events if e.get("is_upcoming")]
        result["unlock"]["upcoming_events_count"] = len(upcoming)
        if upcoming:
            next_ev = upcoming[0]
            if next_ev.get("date"):
                result["unlock"]["next_unlock_date"] = next_ev["date"]
            pct = _to_float(next_ev.get("pct"))
            if pct is not None:
                result["unlock"]["next_unlock_pct"] = pct
            # 30天解锁比例
            from datetime import datetime, timezone, timedelta
            try:
                now = datetime.now(timezone.utc)
                thirty_days = now + timedelta(days=30)
                pct_30d = 0.0
                for e in upcoming:
                    try:
                        ed = datetime.fromisoformat(str(e["date"]).replace("Z", "+00:00"))
                        if ed <= thirty_days:
                            pct_30d += _to_float(e.get("pct")) or 0
                    except (ValueError, TypeError, KeyError):
                        pass
                result["unlock"]["unlock_pct_30d"] = round(pct_30d, 4)
            except Exception:
                pass

    # ── 链上持仓 ──
    if isinstance(onchain, dict):
        by_chain = onchain.get("by_chain") or {}
        chains = []
        total_holders = None
        top10 = None
        for chain, data in by_chain.items():
            chains.append(chain)
            latest = None
            if isinstance(data, list) and data:
                latest = data[-1] if isinstance(data[-1], dict) else None
            elif isinstance(data, dict):
                latest = data
            if latest:
                th = _to_float(latest.get("total_holders"))
                if th is not None:
                    total_holders = th
                t10 = _to_float(latest.get("top10_concentration"))
                if t10 is not None:
                    top10 = t10
        if chains:
            result["onchain"]["chains"] = chains
        if total_holders is not None:
            result["onchain"]["total_holders"] = total_holders
        if top10 is not None:
            result["onchain"]["top10_concentration_pct"] = top10

    # ── 社交热度 ──
    if isinstance(social, dict):
        score = _to_float(social.get("score"))
        if score is not None:
            result["social"]["social_score"] = score
        sent = _to_float(social.get("sentiment_score"))
        if sent is not None:
            result["social"]["sentiment_score"] = sent
        cj = social.get("community_json") or {}
        x_followers = None
        for plat in ("x", "twitter", "X"):
            if plat in cj and cj[plat].get("followers"):
                x_followers = _to_float(cj[plat]["followers"])
                break
        if x_followers is not None:
            result["social"]["x_followers"] = x_followers

    # ── 衍生品资金面（读 biz.asset_derivatives 缓存）──
    try:
        with get_db() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute("""
                    SELECT funding_rate_pct, funding_rate_7d_avg, funding_rate_30d_avg,
                           total_oi_usd, oi_change_24h_pct, cvd_24h_usd, cvd_ratio_24h,
                           available_exchanges, fetched_at
                    FROM biz.asset_derivatives
                    WHERE asset_id = %s
                      AND fetched_at > NOW() - INTERVAL '24 hours'
                    ORDER BY fetched_at DESC
                    LIMIT 1
                """, (asset_id,))
                row = cur.fetchone()
                if row:
                    if row["funding_rate_pct"] is not None:
                        result["derivatives"]["funding_rate_pct"] = float(row["funding_rate_pct"])
                    if row["funding_rate_7d_avg"] is not None:
                        result["derivatives"]["funding_rate_7d_avg"] = float(row["funding_rate_7d_avg"])
                    if row["total_oi_usd"] is not None:
                        result["derivatives"]["total_oi_usd"] = float(row["total_oi_usd"])
                    if row["oi_change_24h_pct"] is not None:
                        result["derivatives"]["oi_change_24h_pct"] = float(row["oi_change_24h_pct"])
                    if row["cvd_ratio_24h"] is not None:
                        result["derivatives"]["cvd_ratio_24h"] = float(row["cvd_ratio_24h"])
                    if row["available_exchanges"]:
                        result["derivatives"]["available_exchanges"] = row["available_exchanges"]
    except (psycopg.errors.UndefinedTable, Exception):
        pass

    # ── 抛压评分（简化版，用解锁 + Top10 估算）──
    try:
        pressure = 0.0
        factors = 0
        if result["unlock"].get("unlock_pct_30d") is not None:
            pct = _to_float(result["unlock"]["unlock_pct_30d"]) or 0
            if pct > 5:
                pressure += 80
            elif pct > 1:
                pressure += 50
            elif pct > 0.1:
                pressure += 20
            factors += 1
        if result["onchain"].get("top10_concentration_pct") is not None:
            t10 = _to_float(result["onchain"]["top10_concentration_pct"]) or 0
            if t10 > 80:
                pressure += 90
            elif t10 > 60:
                pressure += 60
            elif t10 > 40:
                pressure += 30
            factors += 1
        if factors > 0:
            score = round(pressure / factors, 1)
            result["pressure"]["pressure_score"] = score
            result["pressure"]["risk_level"] = (
                "high" if score >= 70 else "medium" if score >= 40 else "low"
            )
    except Exception:
        pass

    return result


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
    thesis = get_latest_research_thesis(asset_id)

    # 实时拼装结构化指标（与 competitors 同源），覆盖 thesis 里的旧快照值
    # 确保页面顶部关键指标与竞品对比表的行情数字一致
    structured_metrics = _build_structured_metrics_from_snapshot(snapshot, asset_id)
    if thesis:
        thesis["structured_metrics"] = structured_metrics
        # 读取时幂等校验引用：旧 thesis 是裸数字 citations，补全 title/url + is_inferred
        sources_list = snapshot.get("sources") or []
        _sanitize_thesis_citations(thesis, sources_list)
    else:
        # 降级结论：无 LLM 生成的 stance 时，基于已有数据生成机器可读结论
        # 保证 research 页面「研究结论」卡片不为空，用户至少能看到关键指标和数据概览
        thesis = _build_fallback_thesis(snapshot, structured_metrics, asset_id)

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
            "structured_metrics": structured_metrics,
            "counts": snapshot["counts"],
            "messages": messages,
            "thesis": thesis,
            "created_at": str(notebook["created_at"]),
            "updated_at": str(notebook["updated_at"]),
        },
    }


def _build_fallback_thesis(snapshot: dict, structured_metrics: dict, asset_id: int) -> dict:
    """降级结论：无 LLM 生成的 stance 时，基于已有结构化数据生成机器可读结论。

    保证 research 页面「研究结论」卡片不为空，用户至少能看到：
    - 数据覆盖度概览（哪些维度有数据、哪些缺失）
    - 关键市场指标（价格/市值/成交量等）
    - 近期异动信号摘要
    - 明确的「待人工研判」标记，避免误导

    不编造 stance/conviction，统一标记为 neutral / 0，
    thesis 正文全部来自真实数据，不做任何主观推断。
    """
    import datetime

    structured = snapshot.get("structured") or {}
    sources = snapshot.get("sources") or []
    counts = snapshot.get("counts") or {}

    # ── 1. 统计各维度数据覆盖度 ──
    coverage = []
    market = structured_metrics.get("market") or {}
    if market.get("price") is not None:
        coverage.append("行情数据")
    tokenomics = structured_metrics.get("tokenomics") or {}
    if tokenomics:
        coverage.append("代币经济")
    onchain = structured_metrics.get("onchain") or {}
    if onchain:
        coverage.append("链上持仓")
    social = structured_metrics.get("social") or {}
    if social:
        coverage.append("社交热度")
    derivatives = structured_metrics.get("derivatives") or {}
    if derivatives:
        coverage.append("衍生品")
    unlock = structured_metrics.get("unlock") or {}
    if unlock:
        coverage.append("解锁日历")

    # ── 2. 尝试获取异动信号摘要（不抛异常，失败则跳过） ──
    signal_summary = ""
    try:
        sig_result = detect_asset_signals(asset_id)
        signals = sig_result.get("signals") or []
        if signals:
            critical = [s for s in signals if s.get("severity") == "critical"]
            warning = [s for s in signals if s.get("severity") == "warning"]
            parts = []
            if critical:
                parts.append(f"{len(critical)} 个高危信号")
            if warning:
                parts.append(f"{len(warning)} 个警告信号")
            info_count = len(signals) - len(critical) - len(warning)
            if info_count > 0:
                parts.append(f"{info_count} 个提示信号")
            signal_summary = "；".join(parts)
    except Exception:
        pass

    # ── 3. 拼装 thesis 正文（纯事实陈述，无主观判断） ──
    thesis_items = []

    # 数据覆盖度
    if coverage:
        thesis_items.append({
            "point": f"当前已采集 {len(coverage)} 个维度数据：{', '.join(coverage)}。",
            "citations": [],
            "is_inferred": False,
        })
    else:
        thesis_items.append({
            "point": "当前暂无结构化数据，建议先等待数据采集完成。",
            "citations": [],
            "is_inferred": False,
        })

    # 市场关键指标摘要
    price = market.get("price")
    mcap = market.get("market_cap")
    change_24h = market.get("change_24h")
    if price is not None or mcap is not None:
        parts = []
        if price is not None:
            parts.append(f"价格 ${price:,.4f}" if price < 1 else f"价格 ${price:,.2f}")
        if mcap is not None:
            if mcap >= 1e9:
                parts.append(f"市值 ${mcap/1e9:.2f}B")
            elif mcap >= 1e6:
                parts.append(f"市值 ${mcap/1e6:.2f}M")
            else:
                parts.append(f"市值 ${mcap:,.0f}")
        if change_24h is not None:
            direction = "上涨" if change_24h >= 0 else "下跌"
            parts.append(f"24h {direction} {abs(change_24h):.2f}%")
        if parts:
            thesis_items.append({
                "point": "市场概览：" + "，".join(parts) + "。",
                "citations": [],
                "is_inferred": False,
            })

    # 异动信号
    if signal_summary:
        thesis_items.append({
            "point": f"近期异动检测：{signal_summary}，详见异动信号卡片。",
            "citations": [],
            "is_inferred": False,
        })

    # 资料数量
    total_sources = len(sources)
    if total_sources > 0:
        thesis_items.append({
            "point": f"资料库已收录 {total_sources} 条资料来源，可在对话区提问获取深度分析。",
            "citations": [],
            "is_inferred": False,
        })

    # 明确提示：这是机器生成的降级结论
    thesis_items.append({
        "point": "⚠️ 本结论为系统自动生成的数据概览，尚未经过 AI 深度研判。点击「生成研究结论」可获取基于四维框架的完整分析。",
        "citations": [],
        "is_inferred": True,
    })

    # ── 4. 关键指标（直接透传 structured_metrics.market） ──
    key_metrics = {}
    if market.get("price") is not None:
        key_metrics["price"] = market["price"]
    if market.get("market_cap") is not None:
        key_metrics["market_cap"] = market["market_cap"]
    if market.get("fdv") is not None:
        key_metrics["fdv"] = market["fdv"]
    if market.get("volume_24h") is not None:
        key_metrics["volume_24h"] = market["volume_24h"]
    if market.get("change_24h") is not None:
        key_metrics["change_24h"] = market["change_24h"]

    return {
        "thesis_id": 0,  # 0 表示降级结论，非数据库记录
        "asset_id": asset_id,
        "stance": "neutral",  # 无立场，避免误导
        "conviction": 0,  # 零置信度
        "thesis": thesis_items,
        "dimensions": None,  # 无四维框架分析
        "key_metrics": key_metrics,
        "risks": [],
        "catalysts": [],
        "source_notebook_id": None,
        "is_fallback": True,  # 标记为降级结论，前端可据此展示不同样式
        "created_at": str(datetime.datetime.now()),
        "updated_at": str(datetime.datetime.now()),
        "structured_metrics": structured_metrics,
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
    thesis_json = row["thesis_json"] or []
    # thesis_json 可能是 list（旧格式，纯论点列表）或 dict（新格式，含 dimensions + 旧字段）
    if isinstance(thesis_json, dict):
        thesis_list = thesis_json.get("thesis") or []
        dimensions = thesis_json.get("dimensions") or None
    else:
        thesis_list = thesis_json
        dimensions = None
    return {
        "thesis_id": row["thesis_id"],
        "asset_id": row["asset_id"],
        "stance": row["stance"],
        "conviction": row["conviction"],
        "thesis": thesis_list,
        "dimensions": dimensions,
        "key_metrics": row["key_metrics_json"] or {},
        "risks": row["risks_json"] or [],
        "catalysts": row["catalysts_json"] or [],
        "source_notebook_id": row["source_notebook_id"],
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _is_self_serving_source(s: dict) -> bool:
    """判断引用源是否为项目方自引页面（官网交易页/产品页等非独立信源）。

    这类页面不能作为事实引用的依据（循环引用），在后处理中过滤掉。
    判定：类型为 official_website 且 URL 有具体子路径（非首页）。
    """
    if s.get("type") != "official_website":
        return False
    url = (s.get("url") or "").rstrip("/")
    # 去掉协议和域名，看是否有路径
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        # 有具体子路径（如 /trade、/product、/usdf 等）= 产品/交易页
        if path and path != "":
            return True
    except Exception:
        pass
    return False


def _sanitize_thesis_citations(thesis_data: dict | None, sources: list[dict]) -> dict | None:
    """读取时幂等校验 thesis/risks 的引用：过滤越界/重复/自引，补充 title/url，无引用标记推断。

    旧 thesis 是落库快照（citations 为裸数字如 [1,3]，无 title/url，无 is_inferred），
    每次读取时用当前 sources 列表做后处理，确保前端展示一致。
    """
    if not thesis_data:
        return thesis_data

    def _sanitize(items: list[dict]) -> list[dict]:
        cleaned = []
        for item in items:
            cites = []
            seen_idx = set()
            for c in item.get("citations") or []:
                try:
                    if isinstance(c, dict):
                        idx = int(c.get("index", 0))
                    else:
                        idx = int(c)
                except (TypeError, ValueError):
                    continue
                if idx < 1 or idx > len(sources) or idx in seen_idx:
                    continue
                s = sources[idx - 1]
                # 过滤项目官网交易页/产品页的自引（循环引用）
                if _is_self_serving_source(s):
                    continue
                seen_idx.add(idx)
                cites.append({
                    "index": idx,
                    "title": s.get("title") or s.get("url") or "",
                    "url": s.get("url") or "",
                })
            new_item = dict(item)
            new_item["citations"] = cites
            if not cites:
                new_item["is_inferred"] = True
            cleaned.append(new_item)
        return cleaned

    thesis_data["thesis"] = _sanitize(thesis_data.get("thesis") or [])
    thesis_data["risks"] = _sanitize(thesis_data.get("risks") or [])

    # 四维框架引用校验
    dimensions = thesis_data.get("dimensions")
    if isinstance(dimensions, dict):
        for dim_key in ("valuation", "supply", "sentiment", "catalyst"):
            dim = dimensions.get(dim_key)
            if isinstance(dim, dict):
                dim["points"] = _sanitize(dim.get("points") or [])

    return thesis_data


def get_cex_netflow(asset_id: int, hours: int = 24) -> dict:
    """链上 CEX 净流入/流出计算。

    CEX Netflow = 从交易所转出金额 - 转入交易所金额
      - 正值 = 净流入（从交易所提币到链上，潜在看涨/惜售）
      - 负值 = 净流出（充值到交易所，潜在抛压）

    数据来源：biz.onchain_transfer_log（链上大额转账监控）
    标签来源：biz.onchain_exchange_wallet（交易所钱包地址库）

    返回：
      - 24h / 7d 两个时间窗口的净流入金额
      - 按交易所分组明细
      - 大额转账 Top 列表
    """
    hours = max(1, min(720, hours))

    with get_db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            # 确保表存在
            cur.execute("""
                CREATE TABLE IF NOT EXISTS biz.onchain_transfer_log (
                    log_id SERIAL PRIMARY KEY,
                    asset_id INTEGER,
                    chain TEXT NOT NULL,
                    contract_address TEXT NOT NULL,
                    tx_hash TEXT NOT NULL,
                    from_address TEXT NOT NULL,
                    to_address TEXT NOT NULL,
                    value NUMERIC NOT NULL,
                    value_usd NUMERIC(15,2),
                    from_label TEXT,
                    to_label TEXT,
                    from_exchange TEXT,
                    to_exchange TEXT,
                    block_number INTEGER,
                    block_timestamp TIMESTAMPTZ,
                    is_to_exchange BOOLEAN DEFAULT FALSE,
                    alert_sent_at TIMESTAMPTZ,
                    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT uq_onchain_tx UNIQUE (chain, tx_hash, contract_address, from_address, to_address)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS biz.onchain_exchange_wallet (
                    wallet_id SERIAL PRIMARY KEY,
                    address TEXT NOT NULL,
                    exchange_name TEXT NOT NULL,
                    chain TEXT NOT NULL,
                    label TEXT DEFAULT 'exchange',
                    confidence TEXT DEFAULT 'high',
                    source TEXT DEFAULT 'seed',
                    added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT uq_exchange_wallet UNIQUE (address, chain)
                )
            """)

            # 先确保种子地址已导入（幂等）
            _ensure_exchange_wallet_seeds(cur)

            # 统计总转账数（判断是否有数据）
            cur.execute("""
                SELECT COUNT(*) AS cnt FROM biz.onchain_transfer_log
                WHERE asset_id = %s
            """, (asset_id,))
            total_transfers = cur.fetchone()["cnt"]

            if total_transfers == 0:
                return {
                    "ok": True,
                    "asset_id": asset_id,
                    "has_data": False,
                    "message": "暂无链上转账数据，链上监控采集中",
                    "netflow_24h_usd": None,
                    "netflow_7d_usd": None,
                    "inflow_24h_usd": None,
                    "outflow_24h_usd": None,
                    "by_exchange": [],
                    "top_transfers": [],
                }

            # 24h 净流入
            cur.execute("""
                SELECT
                    COALESCE(SUM(CASE WHEN from_label = 'exchange' THEN value_usd ELSE 0 END), 0) AS outflow_from_exchange,
                    COALESCE(SUM(CASE WHEN to_label = 'exchange' THEN value_usd ELSE 0 END), 0) AS inflow_to_exchange
                FROM biz.onchain_transfer_log
                WHERE asset_id = %s
                  AND block_timestamp >= NOW() - INTERVAL '24 hours'
            """, (asset_id,))
            r24 = cur.fetchone()
            outflow_24h = float(r24["outflow_from_exchange"] or 0)
            inflow_24h = float(r24["inflow_to_exchange"] or 0)
            netflow_24h = outflow_24h - inflow_24h  # 正=从交易所出来=净流入链上

            # 7d 净流入
            cur.execute("""
                SELECT
                    COALESCE(SUM(CASE WHEN from_label = 'exchange' THEN value_usd ELSE 0 END), 0) AS outflow_from_exchange,
                    COALESCE(SUM(CASE WHEN to_label = 'exchange' THEN value_usd ELSE 0 END), 0) AS inflow_to_exchange
                FROM biz.onchain_transfer_log
                WHERE asset_id = %s
                  AND block_timestamp >= NOW() - INTERVAL '7 days'
            """, (asset_id,))
            r7 = cur.fetchone()
            outflow_7d = float(r7["outflow_from_exchange"] or 0)
            inflow_7d = float(r7["inflow_to_exchange"] or 0)
            netflow_7d = outflow_7d - inflow_7d

            # 按交易所分组（24h）
            cur.execute("""
                SELECT
                    COALESCE(from_exchange, to_exchange) AS exchange_name,
                    COALESCE(SUM(CASE WHEN from_label = 'exchange' THEN value_usd ELSE 0 END), 0) AS outflow,
                    COALESCE(SUM(CASE WHEN to_label = 'exchange' THEN value_usd ELSE 0 END), 0) AS inflow
                FROM biz.onchain_transfer_log
                WHERE asset_id = %s
                  AND block_timestamp >= NOW() - INTERVAL '24 hours'
                  AND (from_label = 'exchange' OR to_label = 'exchange')
                GROUP BY COALESCE(from_exchange, to_exchange)
                ORDER BY GREATEST(outflow, inflow) DESC
                LIMIT 10
            """, (asset_id,))
            by_exchange = [
                {
                    "exchange": r["exchange_name"],
                    "outflow_usd": float(r["outflow"]),
                    "inflow_usd": float(r["inflow"]),
                    "netflow_usd": float(r["outflow"]) - float(r["inflow"]),
                }
                for r in cur.fetchall()
            ]

            # Top 大额转账（24h）
            cur.execute("""
                SELECT tx_hash, from_address, to_address, from_label, to_label,
                       from_exchange, to_exchange, value, value_usd, block_timestamp
                FROM biz.onchain_transfer_log
                WHERE asset_id = %s
                  AND block_timestamp >= NOW() - INTERVAL '24 hours'
                  AND (from_label = 'exchange' OR to_label = 'exchange')
                ORDER BY value_usd DESC NULLS LAST
                LIMIT 10
            """, (asset_id,))
            top_transfers = [
                {
                    "tx_hash": r["tx_hash"],
                    "from": r["from_address"],
                    "to": r["to_address"],
                    "from_label": r["from_label"],
                    "to_label": r["to_label"],
                    "from_exchange": r["from_exchange"],
                    "to_exchange": r["to_exchange"],
                    "value": float(r["value"]),
                    "value_usd": float(r["value_usd"]) if r["value_usd"] else None,
                    "timestamp": r["block_timestamp"].isoformat() if r["block_timestamp"] else None,
                    "direction": "to_exchange" if r["to_label"] == "exchange" else "from_exchange",
                }
                for r in cur.fetchall()
            ]

    return {
        "ok": True,
        "asset_id": asset_id,
        "has_data": True,
        "netflow_24h_usd": round(netflow_24h, 2),
        "netflow_7d_usd": round(netflow_7d, 2),
        "inflow_24h_usd": round(inflow_24h, 2),
        "outflow_24h_usd": round(outflow_24h, 2),
        "inflow_7d_usd": round(inflow_7d, 2),
        "outflow_7d_usd": round(outflow_7d, 2),
        "by_exchange": by_exchange,
        "top_transfers": top_transfers,
        "total_transfers": total_transfers,
    }


def _ensure_exchange_wallet_seeds(cur) -> None:
    """确保交易所钱包种子地址已导入（幂等操作）。

    只在表为空时导入，避免重复执行开销。
    """
    cur.execute("SELECT COUNT(*) AS cnt FROM biz.onchain_exchange_wallet")
    if cur.fetchone()["cnt"] > 0:
        return  # 已有数据，跳过

    # 插入种子地址（高可信度，来自 Etherscan/BSCScan 公开标签）
    seed_addresses = [
        # Binance - ETH
        ('0xBE0eB53F46cd790Cd13851d5EFf43D12404d33E8', 'Binance', 'eth', 'high', 'etherscan-label'),
        ('0xf977814e90da44bfa03b6295a0616a897441acec', 'Binance', 'eth', 'high', 'etherscan-label'),
        ('0x28C6c06298d514Db089934071355E5743bf21d60', 'Binance', 'eth', 'high', 'etherscan-label'),
        ('0x21a31Ee1afC51d94C2eFcCAa2092aD1028285549', 'Binance', 'eth', 'high', 'etherscan-label'),
        ('0xDFd5293D8e347dFe59E90eFd55b2956a1343963d', 'Binance', 'eth', 'high', 'etherscan-label'),
        ('0x5a52e96bacdabb82fd05763e25335261b270efcb', 'Binance', 'eth', 'high', 'etherscan-label'),
        ('0x47ac0Fb4F2D84898e4D9E7b4DaB3C24507a6D503', 'Binance', 'eth', 'high', 'etherscan-label'),
        # Binance - BSC
        ('0x8894E0a0c962CB723c1976a4421c95949bE2D4E3', 'Binance', 'bsc', 'high', 'bscscan-label'),
        ('0x0D0707963952f2fBA59dD06f2b425ace40b492Fe', 'Binance', 'bsc', 'high', 'bscscan-label'),
        ('0x18b2a687610328590bc8f2e5fedde3b582a49cda', 'Binance', 'bsc', 'medium', 'bscscan-label'),
        # Coinbase - ETH
        ('0x71660c4005BA85476C0FE5d080f20C20e7b61C94', 'Coinbase', 'eth', 'high', 'etherscan-label'),
        ('0x503828976D22510aA0d5d6b773756A3e02c1b97f', 'Coinbase', 'eth', 'high', 'etherscan-label'),
        ('0xA090e606E30bD747d4E6245a1517EbE430F0057e', 'Coinbase', 'eth', 'high', 'etherscan-label'),
        # OKX - ETH
        ('0x6CC14824Ea2918f5De5C2f75A9Da968ad4BD6344', 'OKX', 'eth', 'high', 'etherscan-label'),
        ('0x9696f59E4d72E237d85aB7F66B9eB7d5bB7eB7d5', 'OKX', 'eth', 'high', 'etherscan-label'),
        # Huobi - ETH
        ('0xAb5801a7D398351b8bE11C439e05C5B3259aeC9B', 'Huobi', 'eth', 'high', 'etherscan-label'),
    ]

    for addr, name, chain, conf, src in seed_addresses:
        cur.execute("""
            INSERT INTO biz.onchain_exchange_wallet
                (address, exchange_name, chain, label, confidence, source)
            VALUES (%s, %s, %s, 'exchange', %s, %s)
            ON CONFLICT (address, chain) DO NOTHING
        """, (addr, name, chain, conf, src))


def get_whale_flow(asset_id: int) -> dict:
    """鲸鱼/聪明钱行为流分析。

    不依赖预定义鲸鱼地址标签，而是从两个维度综合判断：
      1. 持仓集中度变化（Top10/Top100 鲸鱼持仓变化率）
      2. 大额转账流向（按金额分级 + 交易所进出方向）

    返回：
      - 鲸鱼持仓变化（7d/30d）
      - 大额转账统计（24h/7d，按金额分级 + 流向分类）
      - Top 大额转账明细
      - 综合行为信号（增持/减持/中性 + 置信度）
    """
    with get_db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            # 确保表存在
            cur.execute("""
                CREATE TABLE IF NOT EXISTS biz.onchain_holder_snapshot (
                    snapshot_id SERIAL PRIMARY KEY,
                    asset_id INTEGER,
                    chain TEXT NOT NULL,
                    contract_address TEXT NOT NULL,
                    snapshot_date DATE NOT NULL,
                    total_supply NUMERIC,
                    total_holders INTEGER,
                    top10_concentration NUMERIC(5,2),
                    top50_concentration NUMERIC(5,2),
                    top100_concentration NUMERIC(5,2),
                    whale_balance_change_7d_pct NUMERIC(6,2),
                    whale_balance_change_30d_pct NUMERIC(6,2),
                    exchange_wallet_pct NUMERIC(5,2),
                    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS biz.onchain_transfer_log (
                    log_id SERIAL PRIMARY KEY,
                    asset_id INTEGER,
                    chain TEXT NOT NULL,
                    contract_address TEXT NOT NULL,
                    tx_hash TEXT NOT NULL,
                    from_address TEXT NOT NULL,
                    to_address TEXT NOT NULL,
                    value NUMERIC NOT NULL,
                    value_usd NUMERIC(15,2),
                    from_label TEXT,
                    to_label TEXT,
                    from_exchange TEXT,
                    to_exchange TEXT,
                    block_number INTEGER,
                    block_timestamp TIMESTAMPTZ,
                    is_to_exchange BOOLEAN DEFAULT FALSE,
                    alert_sent_at TIMESTAMPTZ,
                    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            # ── 1. 鲸鱼持仓变化（从最新快照）──
            cur.execute("""
                SELECT chain, snapshot_date, top10_concentration, top50_concentration,
                       top100_concentration, total_holders,
                       whale_balance_change_7d_pct, whale_balance_change_30d_pct,
                       exchange_wallet_pct
                FROM biz.onchain_holder_snapshot
                WHERE asset_id = %s
                ORDER BY snapshot_date DESC
                LIMIT 5
            """, (asset_id,))
            snapshot_rows = [dict(r) for r in cur.fetchall()]

            holding = {}
            if snapshot_rows:
                latest = snapshot_rows[0]
                holding = {
                    "chain": latest["chain"],
                    "snapshot_date": str(latest["snapshot_date"]),
                    "top10_concentration": float(latest["top10_concentration"]) if latest["top10_concentration"] else None,
                    "top50_concentration": float(latest["top50_concentration"]) if latest["top50_concentration"] else None,
                    "top100_concentration": float(latest["top100_concentration"]) if latest["top100_concentration"] else None,
                    "total_holders": latest["total_holders"],
                    "whale_change_7d_pct": float(latest["whale_balance_change_7d_pct"]) if latest["whale_balance_change_7d_pct"] else None,
                    "whale_change_30d_pct": float(latest["whale_balance_change_30d_pct"]) if latest["whale_balance_change_30d_pct"] else None,
                    "exchange_wallet_pct": float(latest["exchange_wallet_pct"]) if latest["exchange_wallet_pct"] else None,
                }
                # 多链的话取所有链
                by_chain = {}
                for r in snapshot_rows:
                    ch = r["chain"]
                    if ch not in by_chain:
                        by_chain[ch] = {
                            "snapshot_date": str(r["snapshot_date"]),
                            "top10_concentration": float(r["top10_concentration"]) if r["top10_concentration"] else None,
                            "whale_change_7d_pct": float(r["whale_balance_change_7d_pct"]) if r["whale_balance_change_7d_pct"] else None,
                            "whale_change_30d_pct": float(r["whale_balance_change_30d_pct"]) if r["whale_balance_change_30d_pct"] else None,
                        }

            # ── 2. 大额转账统计（24h）──
            cur.execute("SELECT COUNT(*) AS cnt FROM biz.onchain_transfer_log WHERE asset_id = %s", (asset_id,))
            total_transfers = cur.fetchone()["cnt"]

            large_transfers = {"has_data": total_transfers > 0}

            if total_transfers > 0:
                # 按金额分级统计（24h）
                cur.execute("""
                    SELECT
                        COUNT(*) FILTER (WHERE value_usd >= 1000000) AS ultra_large,
                        COUNT(*) FILTER (WHERE value_usd >= 100000 AND value_usd < 1000000) AS large,
                        COUNT(*) FILTER (WHERE value_usd >= 10000 AND value_usd < 100000) AS medium,
                        COALESCE(SUM(CASE WHEN value_usd >= 10000 THEN value_usd END), 0) AS total_large_volume,
                        -- 流向：转入交易所
                        COALESCE(SUM(CASE WHEN is_to_exchange = TRUE THEN value_usd END), 0) AS inflow_to_exchange,
                        -- 流向：从交易所转出
                        COALESCE(SUM(CASE WHEN from_label = 'exchange' THEN value_usd END), 0) AS outflow_from_exchange
                    FROM biz.onchain_transfer_log
                    WHERE asset_id = %s
                      AND block_timestamp >= NOW() - INTERVAL '24 hours'
                """, (asset_id,))
                r24 = cur.fetchone()

                large_transfers["24h"] = {
                    "ultra_large_count": r24["ultra_large"],
                    "large_count": r24["large"],
                    "medium_count": r24["medium"],
                    "total_large_volume": float(r24["total_large_volume"]),
                    "inflow_to_exchange": float(r24["inflow_to_exchange"]),
                    "outflow_from_exchange": float(r24["outflow_from_exchange"]),
                    "net_exchange_flow": float(r24["outflow_from_exchange"]) - float(r24["inflow_to_exchange"]),
                }

                # 7d 统计
                cur.execute("""
                    SELECT
                        COUNT(*) FILTER (WHERE value_usd >= 1000000) AS ultra_large,
                        COUNT(*) FILTER (WHERE value_usd >= 100000 AND value_usd < 1000000) AS large,
                        COUNT(*) FILTER (WHERE value_usd >= 10000 AND value_usd < 100000) AS medium,
                        COALESCE(SUM(CASE WHEN value_usd >= 10000 THEN value_usd END), 0) AS total_large_volume,
                        COALESCE(SUM(CASE WHEN is_to_exchange = TRUE THEN value_usd END), 0) AS inflow_to_exchange,
                        COALESCE(SUM(CASE WHEN from_label = 'exchange' THEN value_usd END), 0) AS outflow_from_exchange
                    FROM biz.onchain_transfer_log
                    WHERE asset_id = %s
                      AND block_timestamp >= NOW() - INTERVAL '7 days'
                """, (asset_id,))
                r7 = cur.fetchone()

                large_transfers["7d"] = {
                    "ultra_large_count": r7["ultra_large"],
                    "large_count": r7["large"],
                    "medium_count": r7["medium"],
                    "total_large_volume": float(r7["total_large_volume"]),
                    "inflow_to_exchange": float(r7["inflow_to_exchange"]),
                    "outflow_from_exchange": float(r7["outflow_from_exchange"]),
                    "net_exchange_flow": float(r7["outflow_from_exchange"]) - float(r7["inflow_to_exchange"]),
                }

                # Top 大额转账（24h）
                cur.execute("""
                    SELECT tx_hash, from_address, to_address, from_label, to_label,
                           from_exchange, to_exchange, value, value_usd,
                           block_timestamp, is_to_exchange
                    FROM biz.onchain_transfer_log
                    WHERE asset_id = %s
                      AND block_timestamp >= NOW() - INTERVAL '24 hours'
                      AND value_usd >= 10000
                    ORDER BY value_usd DESC
                    LIMIT 10
                """, (asset_id,))
                large_transfers["top_transfers"] = [
                    {
                        "tx_hash": r["tx_hash"],
                        "from": r["from_address"],
                        "to": r["to_address"],
                        "from_label": r["from_label"],
                        "to_label": r["to_label"],
                        "from_exchange": r["from_exchange"],
                        "to_exchange": r["to_exchange"],
                        "value": float(r["value"]),
                        "value_usd": float(r["value_usd"]) if r["value_usd"] else None,
                        "timestamp": r["block_timestamp"].isoformat() if r["block_timestamp"] else None,
                        "direction": _classify_transfer_direction(r),
                    }
                    for r in cur.fetchall()
                ]
            else:
                large_transfers["24h"] = None
                large_transfers["7d"] = None
                large_transfers["top_transfers"] = []

            # ── 3. 综合行为信号 ──
            signal = _compute_whale_signal(holding, large_transfers)

    return {
        "ok": True,
        "asset_id": asset_id,
        "holding": holding,
        "large_transfers": large_transfers,
        "signal": signal,
        "has_holding_data": bool(holding),
        "has_transfer_data": total_transfers > 0,
    }


def _classify_transfer_direction(row: dict) -> str:
    """分类大额转账方向。"""
    from_ex = row.get("from_label") == "exchange" or row.get("from_exchange")
    to_ex = row.get("to_label") == "exchange" or row.get("to_exchange") or row.get("is_to_exchange")
    from_whale = row.get("from_label") == "whale"
    to_whale = row.get("to_label") == "whale"

    if from_ex and not to_ex:
        return "from_exchange"  # 从交易所转出（吸筹）
    if to_ex and not from_ex:
        return "to_exchange"    # 转入交易所（抛压）
    if from_ex and to_ex:
        return "exchange_internal"  # 交易所内划转
    if from_whale and to_whale:
        return "whale_to_whale"  # 鲸鱼互转
    if from_whale:
        return "whale_sell"     # 鲸鱼卖出/派发
    if to_whale:
        return "whale_buy"      # 鲸鱼买入/增持
    return "unknown"


def _compute_whale_signal(holding: dict, transfers: dict) -> dict:
    """综合持仓变化 + 大额转账流向，计算鲸鱼行为信号。

    返回：
      - stance: accumulating / distributing / neutral
      - confidence: 0-100
      - factors: 各因子得分明细
    """
    score = 0  # 正=增持，负=减持
    factors = []
    weight_sum = 0

    # 因子 1: 鲸鱼 7d 持仓变化（权重 40%）
    whale_7d = holding.get("whale_change_7d_pct")
    if whale_7d is not None:
        w = 40
        # +5% 以上 = 满分，-5% 以下 = 最低分
        s = max(-1, min(1, whale_7d / 5.0))
        score += s * w
        weight_sum += w
        factors.append({
            "name": "鲸鱼 7d 持仓变化",
            "value": f"{whale_7d:+.2f}%",
            "weight": w,
            "score": round(s * w, 1),
            "bullish": s > 0,
        })

    # 因子 2: 鲸鱼 30d 持仓变化（权重 20%）
    whale_30d = holding.get("whale_change_30d_pct")
    if whale_30d is not None:
        w = 20
        s = max(-1, min(1, whale_30d / 15.0))  # 30d ±15% 为满格
        score += s * w
        weight_sum += w
        factors.append({
            "name": "鲸鱼 30d 持仓变化",
            "value": f"{whale_30d:+.2f}%",
            "weight": w,
            "score": round(s * w, 1),
            "bullish": s > 0,
        })

    # 因子 3: 24h 大额转账净流向（权重 25%）
    t24 = transfers.get("24h")
    if t24 and t24.get("total_large_volume", 0) > 0:
        w = 25
        net = t24["net_exchange_flow"]  # 正=从交易所出来=看涨
        vol = t24["total_large_volume"]
        # 净流向占大额成交量 ±20% 为满格
        ratio = net / vol if vol > 0 else 0
        s = max(-1, min(1, ratio / 0.2))
        score += s * w
        weight_sum += w
        factors.append({
            "name": "24h 大额转账净流向",
            "value": f"{'+' if net >= 0 else ''}${net/1e6:.2f}M",
            "weight": w,
            "score": round(s * w, 1),
            "bullish": s > 0,
        })

    # 因子 4: 7d 大额转账净流向（权重 15%）
    t7 = transfers.get("7d")
    if t7 and t7.get("total_large_volume", 0) > 0:
        w = 15
        net = t7["net_exchange_flow"]
        vol = t7["total_large_volume"]
        ratio = net / vol if vol > 0 else 0
        s = max(-1, min(1, ratio / 0.15))  # 7d ±15% 为满格
        score += s * w
        weight_sum += w
        factors.append({
            "name": "7d 大额转账净流向",
            "value": f"{'+' if net >= 0 else ''}${net/1e6:.2f}M",
            "weight": w,
            "score": round(s * w, 1),
            "bullish": s > 0,
        })

    if weight_sum == 0:
        return {
            "stance": "unknown",
            "confidence": 0,
            "factors": [],
            "message": "数据不足，无法判断鲸鱼行为方向",
        }

    # 归一化到 -100 ~ +100
    normalized = score / weight_sum * 100
    confidence = min(100, int(weight_sum))  # 数据越全置信度越高

    if normalized > 20:
        stance = "accumulating"  # 增持/吸筹
    elif normalized < -20:
        stance = "distributing"  # 减持/派发
    else:
        stance = "neutral"

    return {
        "stance": stance,
        "stance_label": {
            "accumulating": "🐋 鲸鱼增持中",
            "distributing": "🐋 鲸鱼减持中",
            "neutral": "🐋 鲸鱼行为中性",
            "unknown": "❓ 数据不足",
        }.get(stance, stance),
        "score": round(normalized, 1),
        "confidence": confidence,
        "factors": factors,
    }


def get_asset_market_history(
    asset_id: int,
    days: int = 30,
    source_code: str = "cmc",
) -> dict:
    """获取资产行情历史时间序列（日级）。

    数据来源：biz.asset_market_daily（CMC 快照聚合 + CMC 历史 API 回填）。

    合并策略：同时读取 cmc（快照聚合）和 cmc_historical（历史 API 回填）两个数据源，
    按日期合并，同一天优先使用 cmc 快照数据（更接近实时），历史 API 数据补全更早的日期。
    这样即使快照只积累了几天，也能通过历史回填获得更长的时间序列。

    Args:
        asset_id: 资产 ID
        days: 取最近 N 天，默认 30 天
        source_code: 数据源，默认 'cmc'（实际会同时查 cmc_historical 合并）

    Returns:
        {
            "ok": bool,
            "asset_id": int,
            "source_code": str,
            "days": int,
            "data_points": int,
            "series": [
                {"date": "2026-08-01", "price_usd": ..., "market_cap": ...,
                 "volume_24h": ..., "change_24h": ..., "fdv": ...}
            ],
            "latest": {...},  # 最新一天数据
        }
    """
    if days < 1:
        days = 1
    if days > 3650:
        days = 3650

    def _rows_to_series(rows) -> dict[str, dict]:
        """把行列表转成 {date_str: item_dict} 映射，方便按日期合并。"""
        result: dict[str, dict] = {}
        for r in rows:
            date_str = str(r["market_date"])
            result[date_str] = {
                "date": date_str,
                "price_usd": float(r["price_usd"]) if r["price_usd"] is not None else None,
                "market_cap": float(r["market_cap"]) if r["market_cap"] is not None else None,
                "fdv": float(r["fdv"]) if r["fdv"] is not None else None,
                "volume_24h": float(r["volume_24h"]) if r["volume_24h"] is not None else None,
                "change_24h": float(r["change_24h"]) if r["change_24h"] is not None else None,
                "change_7d": float(r["change_7d"]) if r["change_7d"] is not None else None,
                "circulating_supply": float(r["circulating_supply"]) if r["circulating_supply"] is not None else None,
                "total_supply": float(r["total_supply"]) if r["total_supply"] is not None else None,
            }
        return result

    with get_db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            # 一次性查出所有 source_code 的数据，按优先级在 Python 层合并
            # 优先级：cmc（快照聚合，最新）> cmc_historical（历史 API 回填）> 其他所有源
            cur.execute(
                """
                SELECT
                    source_code,
                    market_date,
                    price_usd,
                    market_cap,
                    fdv,
                    circulating_supply,
                    total_supply,
                    volume_24h,
                    change_24h,
                    change_7d
                FROM biz.asset_market_daily
                WHERE asset_id = %s
                  AND market_date >= CURRENT_DATE - %s * INTERVAL '1 day'
                ORDER BY market_date ASC
                """,
                (asset_id, days),
            )
            all_rows = cur.fetchall()

    # 按 source_code 优先级分组合并：同一天优先用高优先级源
    # 优先级从高到低：cmc > cmc_historical > 其他
    PRIORITY = {"cmc": 0, "cmc_historical": 1}
    merged: dict[str, dict] = {}
    for r in all_rows:
        date_str = str(r["market_date"])
        sc = r["source_code"]
        priority = PRIORITY.get(sc, 99)
        existing = merged.get(date_str)
        if existing is None or priority < existing["_priority"]:
            item = {
                "date": date_str,
                "price_usd": float(r["price_usd"]) if r["price_usd"] is not None else None,
                "market_cap": float(r["market_cap"]) if r["market_cap"] is not None else None,
                "fdv": float(r["fdv"]) if r["fdv"] is not None else None,
                "volume_24h": float(r["volume_24h"]) if r["volume_24h"] is not None else None,
                "change_24h": float(r["change_24h"]) if r["change_24h"] is not None else None,
                "change_7d": float(r["change_7d"]) if r["change_7d"] is not None else None,
                "circulating_supply": float(r["circulating_supply"]) if r["circulating_supply"] is not None else None,
                "total_supply": float(r["total_supply"]) if r["total_supply"] is not None else None,
                "_priority": priority,
            }
            merged[date_str] = item

    # 清理内部优先级字段
    for item in merged.values():
        del item["_priority"]

    # 按日期排序
    series = [merged[d] for d in sorted(merged.keys())]

    return {
        "ok": True,
        "asset_id": asset_id,
        "source_code": source_code,
        "days": days,
        "data_points": len(series),
        "series": series,
        "latest": series[-1] if series else None,
    }


def detect_asset_signals(asset_id: int) -> dict:
    """检测单资产的异动信号（diff 检测）。

    机器擅长发现变化，不擅长判断重要性。本函数只负责「发现 diff」，
    不做重要性评级，由人或上层逻辑判断是否值得关注。

    信号类型：
    - price_surge / price_dump: 价格异动（24h 涨跌幅超阈值）
    - volume_surge: 成交量放大（24h 成交量 > 7日均量 * 倍数）
    - oi_surge / oi_dump: OI 异动（衍生品未平仓合约 24h 变化大）
    - funding_extreme: 资金费率极端（> 0.05% 或 < -0.05%）
    - unlock_soon: 近期解锁（未来 7 天内有解锁事件）
    - holder_concentration_change: 持仓集中度变化（需历史持仓数据）

    Returns:
        {
            "ok": bool,
            "asset_id": int,
            "signals": [
                {
                    "type": str,           # 信号类型
                    "direction": str,      # up / down / neutral
                    "severity": str,       # info / warning / critical
                    "value": float,        # 当前值
                    "threshold": float,    # 阈值
                    "description": str,    # 人类可读描述
                    "source": str,         # 数据来源
                }
            ],
            "signal_count": int,
        }
    """
    signals: list[dict] = []
    data_status = {}  # 记录各维度数据可用性，用于输出"无信号原因"

    # ── 1. 价格 & 成交量信号（来自行情历史） ──
    try:
        history = get_asset_market_history(asset_id, days=14)
        series = history.get("series") or []
        data_status["market_days"] = len(series)
        if len(series) >= 1:
            latest = series[-1]

            # 价格异动（24h 涨跌幅）—— 只要有 change_24h 字段就能检测
            change_24h = latest.get("change_24h")
            if change_24h is not None:
                if change_24h >= 15:
                    signals.append({
                        "type": "price_surge",
                        "direction": "up",
                        "severity": "critical" if change_24h >= 30 else "warning",
                        "value": round(change_24h, 2),
                        "threshold": 15,
                        "description": f"24h 涨幅 {change_24h:.2f}%",
                        "source": "cmc",
                    })
                elif change_24h <= -15:
                    signals.append({
                        "type": "price_dump",
                        "direction": "down",
                        "severity": "critical" if change_24h <= -30 else "warning",
                        "value": round(change_24h, 2),
                        "threshold": -15,
                        "description": f"24h 跌幅 {abs(change_24h):.2f}%",
                        "source": "cmc",
                    })

            # 成交量放大（24h 成交量 vs 7日均量）—— 需要至少 8 天数据
            vol_24h = latest.get("volume_24h")
            if vol_24h and len(series) >= 8:
                last7_vols = [s["volume_24h"] for s in series[-8:-1] if s.get("volume_24h")]
                if last7_vols:
                    avg_vol = sum(last7_vols) / len(last7_vols)
                    if avg_vol > 0 and vol_24h / avg_vol >= 2:
                        signals.append({
                            "type": "volume_surge",
                            "direction": "up",
                            "severity": "warning" if vol_24h / avg_vol < 5 else "critical",
                            "value": round(vol_24h / avg_vol, 2),
                            "threshold": 2.0,
                            "description": f"24h 成交量是 7 日均量的 {vol_24h / avg_vol:.1f} 倍",
                            "source": "cmc",
                        })
        else:
            data_status["market_error"] = "行情历史数据不足"
    except Exception as e:
        data_status["market_error"] = str(e)
        import logging
        logging.warning(f"detect_asset_signals: market signal error: {e}")

    # ── 2. 衍生品信号（OI / 资金费率） ──
    try:
        deriv = get_asset_derivatives(asset_id)
        if deriv and deriv.get("ok"):
            d = deriv.get("data") or {}
            data_status["has_derivatives"] = True

            # OI 变化
            oi_change = d.get("oi_change_24h_pct")
            if oi_change is not None:
                if oi_change >= 20:
                    signals.append({
                        "type": "oi_surge",
                        "direction": "up",
                        "severity": "warning" if oi_change < 50 else "critical",
                        "value": round(oi_change, 2),
                        "threshold": 20,
                        "description": f"OI 24h 增长 {oi_change:.1f}%（新资金入场/杠杆增加）",
                        "source": "derivatives",
                    })
                elif oi_change <= -20:
                    signals.append({
                        "type": "oi_dump",
                        "direction": "down",
                        "severity": "warning" if oi_change > -50 else "critical",
                        "value": round(oi_change, 2),
                        "threshold": -20,
                        "description": f"OI 24h 下降 {abs(oi_change):.1f}%（资金离场/去杠杆）",
                        "source": "derivatives",
                    })

            # 资金费率极端
            fr_pct = d.get("funding_rate_pct")
            if fr_pct is not None:
                if fr_pct >= 0.05:
                    signals.append({
                        "type": "funding_extreme",
                        "direction": "up",
                        "severity": "warning",
                        "value": round(fr_pct, 4),
                        "threshold": 0.05,
                        "description": f"资金费率 {fr_pct:.4f}% 偏高（多头拥挤，注意回调风险）",
                        "source": "derivatives",
                    })
                elif fr_pct <= -0.05:
                    signals.append({
                        "type": "funding_extreme",
                        "direction": "down",
                        "severity": "warning",
                        "value": round(fr_pct, 4),
                        "threshold": -0.05,
                        "description": f"资金费率 {fr_pct:.4f}% 偏低（空头拥挤，可能有轧空机会）",
                        "source": "derivatives",
                    })
        else:
            data_status["has_derivatives"] = False
    except Exception as e:
        data_status["derivatives_error"] = str(e)
        import logging
        logging.warning(f"detect_asset_signals: derivatives signal error: {e}")

    # ── 3. 解锁信号 ──
    try:
        pressure = compute_unlock_pressure(asset_id)
        if pressure:
            data_status["has_unlock"] = True
            unlock_pct_30d = pressure.get("unlock_pct_30d")
            if unlock_pct_30d is not None and unlock_pct_30d > 0:
                # 30 天解锁 > 5% 流通量算显著
                if unlock_pct_30d >= 5:
                    signals.append({
                        "type": "unlock_soon",
                        "direction": "down",
                        "severity": "warning" if unlock_pct_30d < 20 else "critical",
                        "value": round(unlock_pct_30d, 2),
                        "threshold": 5,
                        "description": f"未来 30 天解锁 {unlock_pct_30d:.1f}% 流通量（抛压风险）",
                        "source": "unlock",
                    })
        else:
            data_status["has_unlock"] = False
    except Exception as e:
        data_status["unlock_error"] = str(e)
        import logging
        logging.warning(f"detect_asset_signals: unlock signal error: {e}")

    # 按严重程度排序：critical > warning > info
    severity_order = {"critical": 0, "warning": 1, "info": 2}
    signals.sort(key=lambda s: severity_order.get(s.get("severity", "info"), 99))

    return {
        "ok": True,
        "asset_id": asset_id,
        "signals": signals,
        "signal_count": len(signals),
        "data_status": data_status,
    }


def compute_correlation_matrix(
    asset_ids: list[int] | None = None,
    tier: str | None = None,
    top_n: int = 30,
    days: int = 90,
    metric: str = "price",
    source_code: str = "cmc",
) -> dict:
    """计算资产间价格收益相关性矩阵（Pearson）。

    支持两种输入方式：
      1. 直接传入 asset_ids 列表
      2. 指定 tier + top_n，按市值从高到低取该分层的 top N

    Args:
        asset_ids: 资产 ID 列表，传 None 则用 tier+top_n 模式
        tier: 市值分层，如 'top100' / 'top500' / 'top1000' / 'other'
        top_n: 分层内取前 N 个，默认 30
        days: 回溯天数，默认 90 天
        metric: 计算相关性的指标，'price'（日收益率）或 'volume'（成交量变化率）
        source_code: 数据源，默认 'cmc'

    Returns:
        {
            "ok": bool,
            "metric": str,
            "days": int,
            "asset_count": int,
            "assets": [{"asset_id": int, "symbol": str, "name": str, "cmc_rank": int}],
            "matrix": [[float, ...], ...],  # N x N 相关系数矩阵，行/列顺序与 assets 一致
            "top_positive": [{"asset_a": {...}, "asset_b": {...}, "correlation": float}],
            "top_negative": [{"asset_a": {...}, "asset_b": {...}, "correlation": float}],
        }
    """
    import math

    # --- 1. 确定资产列表 ---
    if asset_ids is None:
        # 按分层 + 市值排序取 top N
        tier_cond = ""
        tier_params: list = []
        if tier and tier != "all":
            if tier == "top100":
                tier_cond = "AND COALESCE(cm.rank_num, ci.market_cap_rank, 999999) <= 100"
            elif tier == "top500":
                tier_cond = "AND COALESCE(cm.rank_num, ci.market_cap_rank, 999999) <= 500"
            elif tier == "top1000":
                tier_cond = "AND COALESCE(cm.rank_num, ci.market_cap_rank, 999999) <= 1000"
            elif tier == "other":
                tier_cond = "AND COALESCE(cm.rank_num, ci.market_cap_rank, 999999) > 1000"
            else:
                tier_cond = ""

        with get_db() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute(
                    f"""
                    SELECT cb.asset_id, a.canonical_symbol AS symbol, a.canonical_name AS name,
                           COALESCE(cm.rank_num, ci.market_cap_rank) AS cmc_rank
                    FROM biz.coin_basic cb
                    JOIN core.asset a ON a.asset_id = cb.asset_id
                    LEFT JOIN src_cmc.cmc_asset_map cm ON cm.cmc_id = cb.cmc_id
                    LEFT JOIN core.asset_source_map asm ON asm.asset_id = cb.asset_id
                        AND asm.source_code = 'cg' AND asm.is_primary = TRUE
                    LEFT JOIN src_cg.coin_info ci ON ci.coin_id = asm.source_asset_key
                    WHERE (cm.rank_num IS NOT NULL OR ci.market_cap_rank IS NOT NULL)
                      {tier_cond}
                    ORDER BY COALESCE(cm.rank_num, ci.market_cap_rank, 999999) ASC
                    LIMIT %s
                    """,
                    (*tier_params, top_n),
                )
                assets = [dict(r) for r in cur.fetchall()]
    else:
        with get_db() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute(
                    """
                    SELECT cb.asset_id, a.canonical_symbol AS symbol, a.canonical_name AS name,
                           COALESCE(cm.rank_num, ci.market_cap_rank) AS cmc_rank
                    FROM biz.coin_basic cb
                    JOIN core.asset a ON a.asset_id = cb.asset_id
                    LEFT JOIN src_cmc.cmc_asset_map cm ON cm.cmc_id = cb.cmc_id
                    LEFT JOIN core.asset_source_map asm ON asm.asset_id = cb.asset_id
                        AND asm.source_code = 'cg' AND asm.is_primary = TRUE
                    LEFT JOIN src_cg.coin_info ci ON ci.coin_id = asm.source_asset_key
                    WHERE cb.asset_id = ANY(%s)
                    ORDER BY COALESCE(cm.rank_num, ci.market_cap_rank, 999999) ASC NULLS LAST
                    """,
                    (asset_ids,),
                )
                assets = [dict(r) for r in cur.fetchall()]

    if len(assets) < 2:
        return {
            "ok": False,
            "error": f"资产数量不足（{len(assets)} 个），无法计算相关性",
            "assets": assets,
        }

    asset_id_list = [a["asset_id"] for a in assets]

    # --- 2. 批量拉取行情历史（cmc 快照优先，cmc_historical 回填兜底，按日期合并） ---
    value_col = "price_usd" if metric == "price" else "volume_24h"

    with get_db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                f"""
                SELECT asset_id, market_date, val
                FROM (
                    SELECT asset_id, market_date, {value_col} AS val,
                           ROW_NUMBER() OVER (
                               PARTITION BY asset_id, market_date
                               ORDER BY CASE source_code
                                   WHEN 'cmc' THEN 0
                                   WHEN 'cmc_historical' THEN 1
                                   ELSE 99 END
                           ) AS rn
                    FROM biz.asset_market_daily
                    WHERE asset_id = ANY(%s)
                      AND source_code IN ('cmc', 'cmc_historical')
                      AND market_date >= CURRENT_DATE - %s * INTERVAL '1 day'
                      AND {value_col} IS NOT NULL
                      AND {value_col} > 0
                ) sub
                WHERE rn = 1
                ORDER BY asset_id, market_date ASC
                """,
                (asset_id_list, days),
            )
            rows = cur.fetchall()

    # --- 3. 构建 {asset_id: {date: val}} ---
    price_map: dict[int, dict[str, float]] = {}
    for r in rows:
        aid = r["asset_id"]
        if aid not in price_map:
            price_map[aid] = {}
        price_map[aid][str(r["market_date"])] = float(r["val"])

    # --- 4. 计算日收益率序列 ---
    def _returns(prices: dict[str, float]) -> tuple[list[str], list[float]]:
        """按日期排序，返回 (dates, returns_pct)。"""
        sorted_dates = sorted(prices.keys())
        rets = []
        ret_dates = []
        for i in range(1, len(sorted_dates)):
            prev = prices[sorted_dates[i - 1]]
            curr = prices[sorted_dates[i]]
            if prev and prev > 0:
                rets.append((curr - prev) / prev * 100)  # 百分比收益率
                ret_dates.append(sorted_dates[i])
        return ret_dates, rets

    returns_map: dict[int, list[float]] = {}
    date_sets: list[set[str]] = []
    for aid in asset_id_list:
        if aid in price_map:
            d, r = _returns(price_map[aid])
            returns_map[aid] = r
            date_sets.append(set(d))
        else:
            returns_map[aid] = []
            date_sets.append(set())

    # --- 5. 找共同日期交集（简化：用索引对齐，假设日期连续且相近） ---
    # 更稳妥的方式：按日期对齐
    # 先找所有资产都有的日期
    if date_sets:
        common_dates = date_sets[0].copy()
        for ds in date_sets[1:]:
            common_dates &= ds
        common_dates = sorted(common_dates)
    else:
        common_dates = []

    if len(common_dates) < 5:
        return {
            "ok": False,
            "error": f"共同交易日不足（{len(common_dates)} 天），无法可靠计算相关性",
            "assets": assets,
            "common_days": len(common_dates),
        }

    # 按共同日期对齐收益率
    aligned_returns: list[list[float]] = []
    for aid in asset_id_list:
        prices = price_map.get(aid, {})
        rets = []
        sorted_dates = sorted(prices.keys())
        date_to_ret = {}
        for i in range(1, len(sorted_dates)):
            prev = prices[sorted_dates[i - 1]]
            curr = prices[sorted_dates[i]]
            if prev and prev > 0:
                date_to_ret[sorted_dates[i]] = (curr - prev) / prev * 100
        aligned = [date_to_ret.get(d, 0.0) for d in common_dates]
        aligned_returns.append(aligned)

    # --- 6. 计算 Pearson 相关系数矩阵 ---
    n = len(assets)

    def _pearson(x: list[float], y: list[float]) -> float:
        m = len(x)
        if m < 2:
            return 0.0
        mx = sum(x) / m
        my = sum(y) / m
        num = sum((x[i] - mx) * (y[i] - my) for i in range(m))
        dx = math.sqrt(sum((xi - mx) ** 2 for xi in x))
        dy = math.sqrt(sum((yi - my) ** 2 for yi in y))
        if dx == 0 or dy == 0:
            return 0.0
        r = num / (dx * dy)
        # 数值稳定性：钳位到 [-1, 1]
        return max(-1.0, min(1.0, r))

    matrix: list[list[float]] = []
    pairs: list[tuple[int, int, float]] = []
    for i in range(n):
        row = []
        for j in range(n):
            if i == j:
                row.append(1.0)
            elif j < i:
                row.append(matrix[j][i])
            else:
                r = _pearson(aligned_returns[i], aligned_returns[j])
                row.append(round(r, 4))
                pairs.append((i, j, r))
        matrix.append(row)

    # --- 7. Top 正相关 / 负相关 ---
    pairs_sorted = sorted(pairs, key=lambda p: p[2], reverse=True)
    top_positive = []
    for i, j, r in pairs_sorted[:10]:
        if r > 0:
            top_positive.append({
                "asset_a": assets[i],
                "asset_b": assets[j],
                "correlation": round(r, 4),
            })
    top_negative = []
    for i, j, r in reversed(pairs_sorted[-10:]):
        if r < 0:
            top_negative.append({
                "asset_a": assets[i],
                "asset_b": assets[j],
                "correlation": round(r, 4),
            })

    return {
        "ok": True,
        "metric": metric,
        "days": days,
        "common_days": len(common_dates),
        "asset_count": n,
        "assets": assets,
        "matrix": matrix,
        "top_positive": top_positive,
        "top_negative": top_negative,
    }


def get_asset_derivatives(asset_id: int, force_refresh: bool = False) -> dict:
    """获取代币衍生品资金面数据（多交易所聚合）。

    聚合 Binance / OKX / Bybit / Bitget / Gate 五家交易所的：
      - 资金费率（实时 + 7d/30d 历史平均，按 OI 加权）
      - 未平仓合约 OI（总价值 + 24h 变化）
      - CVD 成交净流入（24h 主动买卖净额）

    缓存 15 分钟，命中直接返回。
    """
    CACHE_TTL = 15 * 60  # 15 分钟

    with get_db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            # 确保表存在
            cur.execute("""
                CREATE TABLE IF NOT EXISTS biz.asset_derivatives (
                    asset_id INTEGER PRIMARY KEY REFERENCES core.asset(asset_id) ON DELETE CASCADE,
                    symbol TEXT NOT NULL,
                    funding_rate NUMERIC(12,8),
                    funding_rate_pct NUMERIC(8,4),
                    next_funding_time TIMESTAMPTZ,
                    funding_rate_7d_avg NUMERIC(12,8),
                    funding_rate_30d_avg NUMERIC(12,8),
                    total_oi_usd NUMERIC(20,2),
                    oi_change_24h_pct NUMERIC(8,2),
                    cvd_24h_usd NUMERIC(20,2),
                    cvd_ratio_24h NUMERIC(8,4),
                    exchanges_json JSONB,
                    available_exchanges TEXT[],
                    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            # 查缓存
            if not force_refresh:
                cur.execute("""
                    SELECT * FROM biz.asset_derivatives
                    WHERE asset_id = %s
                      AND fetched_at > NOW() - %s * INTERVAL '1 second'
                """, (asset_id, CACHE_TTL))
                row = cur.fetchone()
                if row:
                    return {
                        "ok": True,
                        "cached": True,
                        "asset_id": asset_id,
                        "symbol": row["symbol"],
                        "funding_rate": float(row["funding_rate"]) if row["funding_rate"] else None,
                        "funding_rate_pct": float(row["funding_rate_pct"]) if row["funding_rate_pct"] else None,
                        "next_funding_time": row["next_funding_time"].isoformat() if row["next_funding_time"] else None,
                        "funding_rate_7d_avg": float(row["funding_rate_7d_avg"]) if row["funding_rate_7d_avg"] else None,
                        "funding_rate_30d_avg": float(row["funding_rate_30d_avg"]) if row["funding_rate_30d_avg"] else None,
                        "total_oi_usd": float(row["total_oi_usd"]) if row["total_oi_usd"] else None,
                        "oi_change_24h_pct": float(row["oi_change_24h_pct"]) if row["oi_change_24h_pct"] else None,
                        "cvd_24h_usd": float(row["cvd_24h_usd"]) if row["cvd_24h_usd"] else None,
                        "cvd_ratio_24h": float(row["cvd_ratio_24h"]) if row["cvd_ratio_24h"] else None,
                        "exchanges": row["exchanges_json"] or {},
                        "available_exchanges": row["available_exchanges"] or [],
                        "fetched_at": row["fetched_at"].isoformat() if row["fetched_at"] else None,
                    }

            # 取代币 symbol
            cur.execute(
                "SELECT canonical_symbol, canonical_name FROM core.asset WHERE asset_id = %s",
                (asset_id,),
            )
            asset_row = cur.fetchone()
            if not asset_row:
                return {"ok": False, "error": "资产不存在"}
            symbol = asset_row["canonical_symbol"].upper()

    # ── 实时抓取（多交易所聚合）──
    try:
        from derivatives_client import (
            EXCHANGE_CLIENTS, FundingRate, OpenInterest, Trade,
        )
    except ImportError:
        return {"ok": False, "error": "derivatives_client 未就绪"}

    import concurrent.futures
    import time as _time

    exchanges_detail = {}
    available = []

    # 并行探测各交易所是否有该合约 + 取数据
    def _fetch_exchange(ex_name: str, client) -> dict:
        try:
            sym = client.format_symbol(symbol)
            # 先试资金费率，能拿到说明合约存在
            fr = client.get_funding_rate(sym)
            if not fr:
                return {"exchange": ex_name, "available": False}

            result = {"exchange": ex_name, "symbol": sym, "available": True}

            # 资金费率
            result["funding_rate"] = fr.funding_rate
            result["next_funding_time"] = fr.next_funding_time
            result["mark_price"] = fr.mark_price

            # 历史资金费率（30 条，约 7.5 天，每 8 小时一次）
            try:
                fr_hist = client.get_funding_rate_history(sym, limit=90)
                if fr_hist:
                    rates = [f.funding_rate for f in fr_hist]
                    result["funding_history_count"] = len(rates)
                    # 7 天 ≈ 21 次结算（每 8h 一次），30 天 ≈ 90 次
                    result["funding_rate_7d_avg"] = sum(rates[:21]) / min(21, len(rates)) if rates else None
                    result["funding_rate_30d_avg"] = sum(rates) / len(rates) if rates else None
            except Exception:
                pass

            # OI
            try:
                oi = client.get_open_interest(sym)
                if oi:
                    result["open_interest"] = oi.open_interest
                    result["open_interest_value"] = oi.open_interest_value
                    # 估算 OI 价值（如果没直接返回）
                    if not oi.open_interest_value and fr.mark_price:
                        result["open_interest_value"] = oi.open_interest * fr.mark_price
            except Exception:
                pass

            # OI 历史（24h 变化）
            try:
                oi_hist = client.get_open_interest_history(sym, period="1h", limit=24)
                if oi_hist and len(oi_hist) >= 2:
                    first_oi = oi_hist[0].open_interest_value or oi_hist[0].open_interest
                    last_oi = oi_hist[-1].open_interest_value or oi_hist[-1].open_interest
                    if first_oi and last_oi and first_oi > 0:
                        result["oi_change_24h_pct"] = (last_oi - first_oi) / first_oi * 100
            except Exception:
                pass

            # 最近成交（CVD 计算用）
            try:
                trades = client.get_recent_trades(sym, limit=500)
                if trades:
                    # CVD = sum(buy_quote_qty) - sum(sell_quote_qty)
                    # is_buyer_maker = True → taker 是卖方 → 卖出成交
                    buy_volume = sum(t.quote_qty for t in trades if not t.is_buyer_maker)
                    sell_volume = sum(t.quote_qty for t in trades if t.is_buyer_maker)
                    total_volume = buy_volume + sell_volume
                    cvd = buy_volume - sell_volume
                    result["cvd_recent"] = cvd
                    result["total_volume_recent"] = total_volume
                    result["cvd_ratio"] = cvd / total_volume if total_volume > 0 else 0
                    result["trades_count"] = len(trades)
            except Exception:
                pass

            return result
        except Exception as e:
            return {"exchange": ex_name, "available": False, "error": str(e)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(_fetch_exchange, name, client): name
            for name, client in EXCHANGE_CLIENTS.items()
        }
        for future in concurrent.futures.as_completed(futures):
            ex_name = futures[future]
            try:
                result = future.result()
                exchanges_detail[ex_name] = result
                if result.get("available"):
                    available.append(ex_name)
            except Exception:
                exchanges_detail[ex_name] = {"exchange": ex_name, "available": False}

    # ── 聚合计算 ──

    # 1. 加权平均资金费率（按 OI 价值加权）
    total_oi_value = 0.0
    weighted_funding = 0.0
    weighted_funding_7d = 0.0
    weighted_funding_30d = 0.0
    next_funding_ts = None

    for ex in available:
        d = exchanges_detail[ex]
        oi_val = d.get("open_interest_value") or 0
        fr = d.get("funding_rate")
        if oi_val and fr is not None:
            total_oi_value += oi_val
            weighted_funding += fr * oi_val
            if d.get("funding_rate_7d_avg") is not None:
                weighted_funding_7d += d["funding_rate_7d_avg"] * oi_val
            if d.get("funding_rate_30d_avg") is not None:
                weighted_funding_30d += d["funding_rate_30d_avg"] * oi_val
        if d.get("next_funding_time"):
            if next_funding_ts is None or d["next_funding_time"] < next_funding_ts:
                next_funding_ts = d["next_funding_time"]

    avg_funding = weighted_funding / total_oi_value if total_oi_value > 0 else None
    avg_funding_7d = weighted_funding_7d / total_oi_value if total_oi_value > 0 else None
    avg_funding_30d = weighted_funding_30d / total_oi_value if total_oi_value > 0 else None

    # 2. OI 24h 变化（按 OI 价值加权）
    total_oi_change_weighted = 0.0
    oi_change_total_weight = 0.0
    for ex in available:
        d = exchanges_detail[ex]
        oi_val = d.get("open_interest_value") or 0
        oi_chg = d.get("oi_change_24h_pct")
        if oi_val and oi_chg is not None:
            total_oi_change_weighted += oi_chg * oi_val
            oi_change_total_weight += oi_val

    oi_change_24h = total_oi_change_weighted / oi_change_total_weight if oi_change_total_weight > 0 else None

    # 3. CVD 聚合（各交易所相加）
    total_cvd = 0.0
    total_volume = 0.0
    for ex in available:
        d = exchanges_detail[ex]
        if d.get("cvd_recent") is not None:
            total_cvd += d["cvd_recent"]
        if d.get("total_volume_recent") is not None:
            total_volume += d["total_volume_recent"]

    cvd_ratio = total_cvd / total_volume if total_volume > 0 else None

    # ── 写入缓存 ──
    import json
    from datetime import datetime, timezone

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO biz.asset_derivatives
                    (asset_id, symbol, funding_rate, funding_rate_pct, next_funding_time,
                     funding_rate_7d_avg, funding_rate_30d_avg,
                     total_oi_usd, oi_change_24h_pct,
                     cvd_24h_usd, cvd_ratio_24h,
                     exchanges_json, available_exchanges, fetched_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (asset_id) DO UPDATE SET
                    symbol = EXCLUDED.symbol,
                    funding_rate = EXCLUDED.funding_rate,
                    funding_rate_pct = EXCLUDED.funding_rate_pct,
                    next_funding_time = EXCLUDED.next_funding_time,
                    funding_rate_7d_avg = EXCLUDED.funding_rate_7d_avg,
                    funding_rate_30d_avg = EXCLUDED.funding_rate_30d_avg,
                    total_oi_usd = EXCLUDED.total_oi_usd,
                    oi_change_24h_pct = EXCLUDED.oi_change_24h_pct,
                    cvd_24h_usd = EXCLUDED.cvd_24h_usd,
                    cvd_ratio_24h = EXCLUDED.cvd_ratio_24h,
                    exchanges_json = EXCLUDED.exchanges_json,
                    available_exchanges = EXCLUDED.available_exchanges,
                    fetched_at = NOW()
            """, (
                asset_id,
                symbol,
                avg_funding,
                round(avg_funding * 100, 4) if avg_funding is not None else None,
                datetime.fromtimestamp(next_funding_ts / 1000, tz=timezone.utc) if next_funding_ts else None,
                avg_funding_7d,
                avg_funding_30d,
                round(total_oi_value, 2) if total_oi_value > 0 else None,
                round(oi_change_24h, 2) if oi_change_24h is not None else None,
                round(total_cvd, 2) if total_cvd else None,
                round(cvd_ratio, 4) if cvd_ratio is not None else None,
                json.dumps(exchanges_detail, default=str),
                available,
            ))
        conn.commit()

    return {
        "ok": True,
        "cached": False,
        "asset_id": asset_id,
        "symbol": symbol,
        "funding_rate": avg_funding,
        "funding_rate_pct": round(avg_funding * 100, 4) if avg_funding is not None else None,
        "next_funding_time": datetime.fromtimestamp(next_funding_ts / 1000, tz=timezone.utc).isoformat() if next_funding_ts else None,
        "funding_rate_7d_avg": avg_funding_7d,
        "funding_rate_30d_avg": avg_funding_30d,
        "total_oi_usd": round(total_oi_value, 2) if total_oi_value > 0 else None,
        "oi_change_24h_pct": round(oi_change_24h, 2) if oi_change_24h is not None else None,
        "cvd_24h_usd": round(total_cvd, 2) if total_cvd else None,
        "cvd_ratio_24h": round(cvd_ratio, 4) if cvd_ratio is not None else None,
        "exchanges": exchanges_detail,
        "available_exchanges": available,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def get_recommendation_backtest(days: int = 30, top_n: int = 10) -> dict:
    """每日推荐质量回测：统计过去 N 天推荐币的后续表现。

    由于没有完整历史价格序列，用「推荐日存档价格 vs 当前价格」近似计算持有收益。
    等 asset_price_daily 积累数据后可升级为精确的 1d/3d/7d 周期回测。

    指标：
      - 平均收益率、胜率（上涨比例）、中位数收益
      - 按评分分层（高/中/低评分组表现对比）
      - 按赛道分层
      - 最佳/最差推荐
    """
    try:
        return _get_recommendation_backtest_inner(days, top_n)
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "days": days,
            "top_n": top_n,
            "notice": "回测计算失败，请检查数据库表是否完整（需 biz.daily_recommendation、raw.api_response、src_cg.coin_info）",
            "total_recommendations": 0,
            "overall": {},
            "by_score_tier": [],
            "by_sector": [],
            "best": [],
            "worst": [],
        }


def _get_recommendation_backtest_inner(days: int, top_n: int) -> dict:
    days = max(7, min(180, days))
    with get_db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            # 确保表存在
            cur.execute("""
                CREATE TABLE IF NOT EXISTS biz.daily_recommendation (
                    rec_date DATE NOT NULL,
                    rank INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    name TEXT,
                    chain TEXT,
                    contract TEXT,
                    sector TEXT,
                    source_count INTEGER,
                    composite_score NUMERIC(6,2),
                    change_24h NUMERIC(8,2),
                    volume_24h NUMERIC(20,2),
                    price_usd NUMERIC(18,8),
                    market_cap_usd NUMERIC(20,2),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (rec_date, symbol, chain)
                )
            """)

            # 取过去 N 天的 Top N 推荐
            cur.execute("""
                SELECT rec_date, rank, symbol, name, chain, contract, sector,
                       source_count, composite_score, price_usd, market_cap_usd
                FROM biz.daily_recommendation
                WHERE rec_date >= CURRENT_DATE - %s * INTERVAL '1 day'
                  AND rank <= %s
                ORDER BY rec_date DESC, rank ASC
            """, (days, top_n))
            recs = [dict(r) for r in cur.fetchall()]

            if not recs:
                return {
                    "ok": True,
                    "days": days,
                    "top_n": top_n,
                    "total_recommendations": 0,
                    "unique_days": 0,
                    "message": "暂无历史推荐数据，每日推荐会自动存档，积累几天后可查看回测结果",
                    "overall": {},
                    "by_score_tier": [],
                    "by_sector": [],
                    "best": [],
                    "worst": [],
                }

            # 取每个推荐币的当前价格
            # 关联路径：daily_recommendation → core.asset_contract (contract+chain) → core.asset
            #           → core.asset_source_map (cg, is_primary) → raw.api_response (cg/coin_info)
            # 对于无合约的主流币，fallback 到 src_cg.coin_info 按 symbol+排名取
            price_map = {}  # key: (symbol_upper, chain_lower), value: current_price
            asset_id_map = {}  # key: (symbol_upper, chain_lower), value: asset_id

            # 第一步：有合约的币，用 contract+chain 精确匹配
            rec_with_contract = [r for r in recs if r.get("contract") and r.get("chain")]
            if rec_with_contract:
                contract_keys = list(set(
                    (r["contract"].lower(), (r["chain"] or "").lower())
                    for r in rec_with_contract
                ))
                placeholders = ",".join(["(%s, %s)"] * len(contract_keys))
                params = []
                for addr, ch in contract_keys:
                    params.extend([addr, ch])

                cur.execute(f"""
                    WITH rec_inputs(contract_address, chain) AS (
                        VALUES {placeholders}
                    ),
                    ranked_contracts AS (
                        SELECT ac.asset_id, LOWER(ac.contract_address) AS contract_address,
                               LOWER(ac.chain) AS chain, a.canonical_symbol,
                               ROW_NUMBER() OVER (
                                   PARTITION BY LOWER(ac.contract_address), LOWER(ac.chain)
                                   ORDER BY ac.is_primary DESC, ac.contract_id
                               ) AS rn
                        FROM rec_inputs ri
                        JOIN core.asset_contract ac
                            ON LOWER(ac.contract_address) = ri.contract_address
                            AND LOWER(ac.chain) = ri.chain
                        JOIN core.asset a ON a.asset_id = ac.asset_id
                    ),
                    primary_sources AS (
                        SELECT rc.asset_id, rc.canonical_symbol, rc.contract_address, rc.chain,
                               asm.source_asset_key AS cg_id
                        FROM ranked_contracts rc
                        LEFT JOIN core.asset_source_map asm
                            ON asm.asset_id = rc.asset_id
                            AND asm.source_code = 'cg'
                            AND asm.is_primary = TRUE
                        WHERE rc.rn = 1
                    )
                    SELECT ps.asset_id, ps.canonical_symbol, ps.contract_address, ps.chain,
                           ps.cg_id,
                           (rar.payload->'market_data'->'current_price'->>'usd')::NUMERIC AS current_price
                    FROM primary_sources ps
                    LEFT JOIN raw.api_response rar
                        ON rar.platform_code = 'cg'
                        AND rar.endpoint_code = 'coin_info'
                        AND rar.request_key = 'id=' || ps.cg_id
                    WHERE ps.cg_id IS NOT NULL
                    ORDER BY rar.fetched_at DESC NULLS LAST
                """, params)

                for r in cur.fetchall():
                    key = (r["canonical_symbol"].upper(), r["chain"])
                    if r["current_price"] and key not in price_map:
                        price_map[key] = float(r["current_price"])
                        asset_id_map[key] = r["asset_id"]

            # 第二步：无合约的主流币，用 symbol + src_cg.coin_info 匹配（取排名最高的）
            rec_no_contract = [r for r in recs if not r.get("contract") or not r.get("chain")]
            if rec_no_contract:
                symbols = list(set(r["symbol"].upper() for r in rec_no_contract))
                placeholders = ",".join(["%s"] * len(symbols))
                cur.execute(f"""
                    WITH ranked_coins AS (
                        SELECT ci.coin_id, ci.symbol, ci.market_cap_rank,
                               ROW_NUMBER() OVER (
                                   PARTITION BY UPPER(ci.symbol)
                                   ORDER BY ci.market_cap_rank NULLS LAST, ci.coin_id
                               ) AS rn
                        FROM src_cg.coin_info ci
                        WHERE UPPER(ci.symbol) IN ({placeholders})
                    ),
                    top_coins AS (
                        SELECT coin_id, UPPER(symbol) AS symbol_upper
                        FROM ranked_coins
                        WHERE rn = 1
                    )
                    SELECT tc.symbol_upper,
                           (rar.payload->'market_data'->'current_price'->>'usd')::NUMERIC AS current_price
                    FROM top_coins tc
                    LEFT JOIN raw.api_response rar
                        ON rar.platform_code = 'cg'
                        AND rar.endpoint_code = 'coin_info'
                        AND rar.request_key = 'id=' || tc.coin_id
                    WHERE rar.payload IS NOT NULL
                    ORDER BY rar.fetched_at DESC NULLS LAST
                """, symbols)

                for r in cur.fetchall():
                    key = (r["symbol_upper"], "")
                    if r["current_price"] and key not in price_map:
                        price_map[key] = float(r["current_price"])

            # 计算每个推荐的收益率
            results = []
            for r in recs:
                rec_price = float(r["price_usd"] or 0)
                key = (r["symbol"].upper(), (r["chain"] or "").lower())
                cur_price = price_map.get(key)
                if not rec_price or not cur_price or rec_price <= 0:
                    continue
                return_pct = round((cur_price - rec_price) / rec_price * 100, 2)
                days_held = (
                    __import__("datetime").date.today() - r["rec_date"]
                ).days
                results.append({
                    "symbol": r["symbol"],
                    "name": r["name"],
                    "sector": r["sector"],
                    "rec_date": str(r["rec_date"]),
                    "days_held": days_held,
                    "rec_price": rec_price,
                    "current_price": cur_price,
                    "return_pct": return_pct,
                    "score": float(r["composite_score"] or 0),
                    "rank": r["rank"],
                    "source_count": r["source_count"],
                })

            if not results:
                return {
                    "ok": True,
                    "days": days,
                    "top_n": top_n,
                    "total_recommendations": len(recs),
                    "unique_days": len(set(r["rec_date"] for r in recs)),
                    "message": "有推荐存档但当前价格数据不足，无法计算收益",
                    "overall": {},
                    "by_score_tier": [],
                    "by_sector": [],
                    "best": [],
                    "worst": [],
                }

            # 总体统计
            returns = [r["return_pct"] for r in results]
            wins = [r for r in results if r["return_pct"] > 0]
            losses = [r for r in results if r["return_pct"] < 0]
            avg_win = sum(r["return_pct"] for r in wins) / len(wins) if wins else 0
            avg_loss = abs(sum(r["return_pct"] for r in losses) / len(losses)) if losses else 0
            win_rate = round(len(wins) / len(results) * 100, 1)
            avg_return = round(sum(returns) / len(results), 2)
            median_return = sorted(returns)[len(returns) // 2]
            profit_factor = round(avg_win / avg_loss, 2) if avg_loss > 0 else None

            overall = {
                "total_samples": len(results),
                "unique_days": len(set(r["rec_date"] for r in results)),
                "avg_return_pct": avg_return,
                "median_return_pct": round(median_return, 2),
                "win_rate_pct": win_rate,
                "profit_factor": profit_factor,
                "avg_win_pct": round(avg_win, 2),
                "avg_loss_pct": round(avg_loss, 2),
                "best_return_pct": round(max(returns), 2),
                "worst_return_pct": round(min(returns), 2),
            }

            # 按评分分层（高 >=70, 中 50-70, 低 <50）
            tiers = [
                ("high", "高评分 (≥70)", lambda r: r["score"] >= 70),
                ("medium", "中评分 (50-70)", lambda r: 50 <= r["score"] < 70),
                ("low", "低评分 (<50)", lambda r: r["score"] < 50),
            ]
            by_score_tier = []
            for tid, tlabel, tfn in tiers:
                group = [r for r in results if tfn(r)]
                if not group:
                    continue
                g_returns = [r["return_pct"] for r in group]
                g_wins = [r for r in group if r["return_pct"] > 0]
                by_score_tier.append({
                    "tier": tid,
                    "label": tlabel,
                    "count": len(group),
                    "avg_return_pct": round(sum(g_returns) / len(group), 2),
                    "win_rate_pct": round(len(g_wins) / len(group) * 100, 1),
                })

            # 按赛道分层
            sector_groups = {}
            for r in results:
                sec = r["sector"] or "other"
                sector_groups.setdefault(sec, []).append(r)
            by_sector = []
            for sec, group in sorted(sector_groups.items(), key=lambda x: -len(x[1])):
                if len(group) < 2:
                    continue
                g_returns = [r["return_pct"] for r in group]
                g_wins = [r for r in group if r["return_pct"] > 0]
                by_sector.append({
                    "sector": sec,
                    "sector_label": SECTOR_LABELS.get(sec, sec),
                    "count": len(group),
                    "avg_return_pct": round(sum(g_returns) / len(group), 2),
                    "win_rate_pct": round(len(g_wins) / len(group) * 100, 1),
                })

            # 最佳/最差 Top 5
            sorted_by_return = sorted(results, key=lambda r: -r["return_pct"])
            best = sorted_by_return[:5]
            worst = sorted_by_return[-5:][::-1]

    return {
        "ok": True,
        "days": days,
        "top_n": top_n,
        "total_recommendations": len(recs),
        "unique_days": len(set(r["rec_date"] for r in recs)),
        "overall": overall,
        "by_score_tier": by_score_tier,
        "by_sector": by_sector,
        "best": best,
        "worst": worst,
    }


def get_divergence_signals(asset_id: int) -> dict:
    """情绪 × 价格 × 链上 背离检测。

    基于当前快照 + 已有变化率指标（7d/30d），识别两类经典背离：

    顶部背离（看空）：
      - 社交情绪高涨（sentiment_score > 65）
      - 价格滞涨或微涨（24h 涨幅 < 5%，或 7d 涨幅 < 10%）
      - 链上资金流出（鲸鱼 7d 减持 > 3%，或 24h 大额转入交易所 > 市值 0.1%）
      → 散户 FOMO 但聪明钱在出货

    底部背离（看多）：
      - 社交情绪低迷（sentiment_score < 35）
      - 价格微跌或横盘（24h 跌幅 < 5%，或 7d 跌幅 < 10%）
      - 链上资金流入（鲸鱼 7d 增持 > 3%，或持有者 7d 增长 > 5%）
      → 散户绝望但聪明钱在吸筹

    返回信号列表 + 各维度原始指标。
    """
    with get_db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            # 1. 基础信息
            cur.execute(
                "SELECT asset_id, canonical_symbol, canonical_name, primary_sector "
                "FROM core.asset WHERE asset_id = %s",
                (asset_id,),
            )
            asset = cur.fetchone()
            if not asset:
                return {"ok": False, "error": "资产不存在"}

            # 2. 价格/市值优先从权威行情源 biz.asset_market_daily 取（与 market-history 实时同步）
            #    social_heat.market_json 缺失率高且可能陈旧，仅作为 sentiment_score 来源和行情 fallback。
            sentiment_score = None
            price_change_24h = None
            price_change_7d = None
            market_cap = None
            try:
                cur.execute("""
                    SELECT change_24h, change_7d, market_cap, price_usd
                    FROM (
                        SELECT change_24h, change_7d, market_cap, price_usd,
                               ROW_NUMBER() OVER (
                                   ORDER BY CASE source_code
                                       WHEN 'cmc' THEN 0
                                       WHEN 'cmc_historical' THEN 1
                                       ELSE 99 END,
                                   market_date DESC
                               ) AS rn
                        FROM biz.asset_market_daily
                        WHERE asset_id = %s
                          AND source_code IN ('cmc', 'cmc_historical')
                    ) sub
                    WHERE rn = 1
                    LIMIT 1
                """, (asset_id,))
                md = cur.fetchone()
                if md:
                    price_change_24h = md.get("change_24h")
                    price_change_7d = md.get("change_7d")
                    market_cap = md.get("market_cap")
            except psycopg.errors.UndefinedTable:
                pass

            cur.execute("""
                SELECT score, sentiment_json, trend_json, market_json, fetched_at
                FROM biz.asset_social_heat WHERE asset_id = %s
            """, (asset_id,))
            social_row = cur.fetchone()

            if social_row:
                sent = social_row.get("sentiment_json") or {}
                sentiment_score = sent.get("sentiment_score") or sent.get("score")
                market = social_row.get("market_json") or {}
                # 若日级行情表无数据，才回退到 social_heat
                if price_change_24h is None:
                    price_change_24h = market.get("price_change_24h")
                if price_change_7d is None:
                    price_change_7d = market.get("price_change_7d")
                if market_cap is None:
                    market_cap = market.get("market_cap_usd")

            # 3. 链上持仓变化（取最新一条，主链优先）
            cur.execute("""
                SELECT DISTINCT ON (asset_id)
                       top10_concentration, total_holders,
                       holder_change_7d, holder_change_30d,
                       whale_balance_change_7d_pct, whale_balance_change_30d_pct,
                       exchange_wallet_pct, chain, snapshot_date
                FROM biz.onchain_holder_snapshot
                WHERE asset_id = %s
                ORDER BY asset_id, snapshot_date DESC, chain
            """, (asset_id,))
            onchain_row = cur.fetchone()

            whale_change_7d = None
            whale_change_30d = None
            holder_change_7d = None
            holder_change_7d_pct = None
            total_holders = None
            if onchain_row:
                whale_change_7d = onchain_row.get("whale_balance_change_7d_pct")
                whale_change_30d = onchain_row.get("whale_balance_change_30d_pct")
                holder_change_7d = onchain_row.get("holder_change_7d")
                total_holders = onchain_row.get("total_holders")
                if total_holders and total_holders > 0 and holder_change_7d is not None:
                    holder_change_7d_pct = round(holder_change_7d / total_holders * 100, 2)

            # 4. 24h 大额转入交易所
            cur.execute("""
                SELECT COUNT(*) AS tx_count,
                       COALESCE(SUM(value_usd), 0) AS total_value_usd
                FROM biz.onchain_transfer_log
                WHERE asset_id = %s
                  AND is_to_exchange = TRUE
                  AND block_timestamp >= NOW() - INTERVAL '24 hours'
            """, (asset_id,))
            transfer_row = cur.fetchone()
            exchange_inflow_24h = float(transfer_row["total_value_usd"] or 0) if transfer_row else 0
            exchange_inflow_tx = int(transfer_row["tx_count"] or 0) if transfer_row else 0

            # 流入占市值比例
            exchange_inflow_pct = None
            if market_cap and market_cap > 0 and exchange_inflow_24h > 0:
                exchange_inflow_pct = round(exchange_inflow_24h / market_cap * 100, 4)

    # ── 背离检测逻辑 ──
    signals = []

    def _num(v):
        try:
            return float(v)
        except (ValueError, TypeError):
            return None

    s_score = _num(sentiment_score)
    p_24h = _num(price_change_24h)
    p_7d = _num(price_change_7d)
    w_7d = _num(whale_change_7d)
    h_7d_pct = _num(holder_change_7d_pct)
    ex_pct = _num(exchange_inflow_pct)

    # 顶部背离条件
    top_conds = []
    if s_score is not None:
        top_conds.append(("high_sentiment", s_score > 65, f"情绪分 {s_score:.1f}（>65 高涨）"))
    if p_24h is not None:
        top_conds.append(("price_stagnant_up", p_24h < 5, f"24h 涨幅 {p_24h:+.2f}%（<5% 滞涨）"))
    elif p_7d is not None:
        top_conds.append(("price_stagnant_up", p_7d < 10, f"7d 涨幅 {p_7d:+.2f}%（<10% 滞涨）"))
    if w_7d is not None:
        top_conds.append(("whale_selling", w_7d < -3, f"鲸鱼 7d 变化 {w_7d:+.2f}%（<-3% 减持）"))
    if ex_pct is not None:
        top_conds.append(("exchange_inflow", ex_pct > 0.1, f"24h 转入交易所 {ex_pct:.4f}%（>0.1% 抛压）"))

    top_matched = [c for c in top_conds if c[1]]
    if len(top_matched) >= 3:
        signals.append({
            "type": "bearish_divergence",
            "label": "顶部背离",
            "severity": "high" if len(top_matched) >= 4 else "medium",
            "confidence": round(len(top_matched) / len(top_conds) * 100, 0) if top_conds else 0,
            "description": "社交情绪高涨但价格滞涨、链上资金流出，散户 FOMO 而聪明钱出货",
            "conditions": [{"key": k, "matched": m, "detail": d} for k, m, d in top_conds],
        })

    # 底部背离条件
    bot_conds = []
    if s_score is not None:
        bot_conds.append(("low_sentiment", s_score < 35, f"情绪分 {s_score:.1f}（<35 低迷）"))
    if p_24h is not None:
        bot_conds.append(("price_stagnant_down", p_24h > -5, f"24h 跌幅 {p_24h:+.2f}%（>-5% 抗跌）"))
    elif p_7d is not None:
        bot_conds.append(("price_stagnant_down", p_7d > -10, f"7d 跌幅 {p_7d:+.2f}%（>-10% 抗跌）"))
    if w_7d is not None:
        bot_conds.append(("whale_buying", w_7d > 3, f"鲸鱼 7d 变化 {w_7d:+.2f}%（>+3% 增持）"))
    if h_7d_pct is not None:
        bot_conds.append(("holder_growth", h_7d_pct > 5, f"持有者 7d 增长 {h_7d_pct:+.2f}%（>+5% 扩散）"))

    bot_matched = [c for c in bot_conds if c[1]]
    if len(bot_matched) >= 3:
        signals.append({
            "type": "bullish_divergence",
            "label": "底部背离",
            "severity": "high" if len(bot_matched) >= 4 else "medium",
            "confidence": round(len(bot_matched) / len(bot_conds) * 100, 0) if bot_conds else 0,
            "description": "社交情绪低迷但价格抗跌、链上资金流入，散户绝望而聪明钱吸筹",
            "conditions": [{"key": k, "matched": m, "detail": d} for k, m, d in bot_conds],
        })

    # 原始指标（用于前端展示）
    metrics = {
        "sentiment_score": s_score,
        "price_change_24h": p_24h,
        "price_change_7d": p_7d,
        "market_cap_usd": market_cap,
        "whale_change_7d_pct": w_7d,
        "whale_change_30d_pct": _num(whale_change_30d),
        "holder_change_7d_pct": h_7d_pct,
        "total_holders": total_holders,
        "exchange_inflow_24h_usd": exchange_inflow_24h,
        "exchange_inflow_24h_pct": ex_pct,
        "exchange_inflow_tx_24h": exchange_inflow_tx,
    }

    return {
        "ok": True,
        "asset_id": asset_id,
        "symbol": asset["canonical_symbol"],
        "name": asset["canonical_name"],
        "sector": asset["primary_sector"] or "other",
        "signals": signals,
        "metrics": metrics,
        "data_availability": {
            "sentiment": s_score is not None,
            "price": p_24h is not None or p_7d is not None,
            "onchain": w_7d is not None or h_7d_pct is not None,
            "exchange_flow": transfer_row is not None,
        },
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
            #    额外过滤：meme 赛道排除 asset_type='coin'（主流公链币被误分类的情况）
            extra_filter = ""
            if sector == "meme":
                extra_filter = "AND a.asset_type != 'coin'"
            cur.execute(f"""
                WITH sector_assets AS (
                    SELECT a.asset_id, a.canonical_symbol, a.canonical_name, a.asset_type,
                           a.primary_sector
                    FROM core.asset a
                    WHERE a.primary_sector = %s
                      AND a.asset_id <> %s
                      {extra_filter}
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

            # 3f. 日级行情表（与 /api/research/<id>/market-history 同源，最可靠）
            #     cmc 快照优先，cmc_historical 回填兜底，按优先级取每个资产最新一条
            try:
                cur.execute("""
                    SELECT asset_id, price_usd, market_cap, fdv,
                           circulating_supply, total_supply
                    FROM (
                        SELECT asset_id, price_usd, market_cap, fdv,
                               circulating_supply, total_supply,
                               ROW_NUMBER() OVER (
                                   PARTITION BY asset_id
                                   ORDER BY CASE source_code
                                       WHEN 'cmc' THEN 0
                                       WHEN 'cmc_historical' THEN 1
                                       ELSE 99 END,
                                   market_date DESC
                               ) AS rn
                        FROM biz.asset_market_daily
                        WHERE asset_id = ANY(%s)
                          AND source_code IN ('cmc', 'cmc_historical')
                    ) sub
                    WHERE rn = 1
                """, (all_ids,))
                market_daily_map = {r["asset_id"]: dict(r) for r in cur.fetchall()}
            except psycopg.errors.UndefinedTable:
                market_daily_map = {}

            # 3g. CMC 报价快照 fallback（按资产取该资产最新 quote_time，避免全局最大时间导致该资产无记录）
            try:
                cur.execute("""
                    SELECT DISTINCT ON (cb.asset_id)
                           cb.asset_id, q.price_usd, q.market_cap, q.fdv,
                           q.circulating_supply, q.total_supply, q.max_supply
                    FROM biz.coin_basic cb
                    JOIN src_cmc.cmc_asset_quote_snapshot q ON q.cmc_id = cb.cmc_id
                    WHERE cb.asset_id = ANY(%s)
                    ORDER BY cb.asset_id, q.quote_time DESC
                """, (all_ids,))
                cmc_quote_map = {r["asset_id"]: dict(r) for r in cur.fetchall()}
            except psycopg.errors.UndefinedTable:
                cmc_quote_map = {}

            # 3h. 市值/价格（多源 fallback：日级行情 > unlock > social_heat > cmc_snapshot > tokenomics推算）
            def _get_mcap_price(aid):
                # 1. 从日级行情表取（与 market-history 同源，最可靠）
                m = market_daily_map.get(aid)
                if m:
                    mcap = m.get("market_cap")
                    price = m.get("price_usd")
                    fdv = m.get("fdv")
                    if mcap or price or fdv:
                        return (mcap or fdv, price, fdv)
                # 2. 从 unlock input_snapshot 取
                row = unlock_map.get(aid)
                if row:
                    snap = row.get("input_snapshot_json") or {}
                    mcap = snap.get("market_cap") or snap.get("market_cap_usd")
                    price = snap.get("price") or snap.get("price_usd")
                    fdv = snap.get("fdv") or snap.get("fdv_usd")
                    if mcap or price:
                        return (mcap or fdv, price, fdv)
                # 3. 从 social_heat market_json 取
                s = social_map.get(aid)
                if s:
                    mj = s.get("market_json") or {}
                    mcap = mj.get("market_cap") or mj.get("market_cap_usd")
                    price = mj.get("price") or mj.get("price_usd")
                    fdv = mj.get("fdv") or mj.get("fully_diluted_valuation")
                    if mcap or price:
                        return (mcap or fdv, price, fdv)
                # 3. 从 CMC 报价快照取（最可靠的 fallback）
                q = cmc_quote_map.get(aid)
                if q:
                    mcap = q.get("market_cap")
                    price = q.get("price_usd")
                    fdv = q.get("fdv")
                    if mcap or price:
                        return (mcap, price, fdv)
                # 4. 从 tokenomics 推算（价格 * 流通量）
                t = tokenomics_map.get(aid) or {}
                price = t.get("price_usd")
                circ = t.get("circulating_supply")
                fdv = None
                mcap = None
                if price and circ:
                    try:
                        mcap = float(price) * float(circ)
                    except (ValueError, TypeError):
                        pass
                total = t.get("total_supply")
                if price and total:
                    try:
                        fdv = float(price) * float(total)
                    except (ValueError, TypeError):
                        pass
                return (mcap, price, fdv)

            # 4. 组装竞品列表
            def _build_coin(aid, symbol, name, atype):
                mcap, price, fdv = _get_mcap_price(aid)
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
                max_supply = t.get("max_supply")
                # CMC 权威 supply 校验：tokenomics 为空或偏离 >10 倍时用 CMC 覆盖
                q = cmc_quote_map.get(aid)
                if q:
                    def _sanitize_supply(tok_val, q_key):
                        q_val = q.get(q_key)
                        if q_val is None:
                            return tok_val
                        if tok_val is None:
                            return q_val
                        try:
                            tvf = float(tok_val)
                            qvf = float(q_val)
                            if qvf > 0 and (tvf / qvf > 10 or tvf / qvf < 0.1):
                                return q_val  # 单位疑似错误，用权威值覆盖
                        except (ValueError, TypeError, ZeroDivisionError):
                            pass
                        return tok_val

                    circ_supply = _sanitize_supply(circ_supply, "circulating_supply")
                    total_supply = _sanitize_supply(total_supply, "total_supply")
                    max_supply = _sanitize_supply(max_supply, "max_supply")
                # 未流通占比（原 inflation_pct 命名误导，实为「未流通占比」，已更名 unlocked_pct）。
                # 防御：circ > total 说明 supply 来源单位不一致（tokenomics 与 CMC 混用），
                # 此时不输出负值爆炸，置为 None。
                unlocked_pct = None
                try:
                    circ_f = float(circ_supply)
                    total_f = float(total_supply)
                    if total_f > 0 and circ_f >= 0 and circ_f <= total_f:
                        unlocked_pct = round((1 - circ_f / total_f) * 100, 2)
                except (ValueError, TypeError, ZeroDivisionError):
                    pass

                return {
                    "asset_id": aid,
                    "symbol": symbol,
                    "name": name,
                    "type": atype,
                    "is_target": aid == asset_id,
                    "market_cap": mcap,
                    "fdv": fdv,
                    "price": price,
                    "total_supply": total_supply,
                    "circulating_supply": circ_supply,
                    "unlocked_pct": unlocked_pct,
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
                coin = _build_coin(
                    r["asset_id"], r["canonical_symbol"],
                    r["canonical_name"], r["asset_type"],
                )
                # 过滤全列未采集的噪声行：除基础字段外，所有数据字段都为空/None/0
                data_fields = ("market_cap", "fdv", "price", "total_supply",
                               "circulating_supply", "unlocked_pct", "unlock_30d_pct",
                               "top10_concentration", "total_holders", "social_score",
                               "x_followers", "raise_count", "total_raised",
                               "buy_tax_pct", "sell_tax_pct", "contract_renounced", "lp_locked")
                has_any_data = any(
                    coin.get(f) is not None and coin.get(f) != 0 and coin.get(f) != ""
                    for f in data_fields
                )
                if has_any_data:
                    competitors.append(coin)

            # 5. 指标定义（前端表格列）
            metrics = [
                {"key": "market_cap", "label": "市值", "format": "usd_big"},
                {"key": "fdv", "label": "FDV", "format": "usd_big"},
                {"key": "price", "label": "价格", "format": "usd_price"},
                {"key": "total_supply", "label": "总供应量", "format": "number_big"},
                {"key": "circulating_supply", "label": "流通量", "format": "number_big"},
                {"key": "unlocked_pct", "label": "未流通占比", "format": "pct"},
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

    # 2. 收集所有结构化指标（强制 LLM 只能引用这些数字，禁止幻觉）
    structured = snapshot.get("structured") or {}
    pressure = compute_unlock_pressure(asset_id)
    tokenomics = structured.get("tokenomics") or {}
    social = structured.get("social") or {}
    onchain = structured.get("onchain") or {}
    unlocks = structured.get("unlocks") or {}
    contracts = structured.get("contracts") or []

    # 组装结构化指标 JSON（LLM 所有数字必须从这里取）
    metrics_structured = {
        "market": {},
        "tokenomics": {},
        "unlock": {},
        "onchain": {},
        "social": {},
        "pressure": {},
        "derivatives": {},
    }

    # 市场数据（多源 fallback：日级行情 > unlock snapshot > social_heat market > CMC 快照 > tokenomics推算）
    # 优先级与 /api/research/<id>/market-history、divergence 对齐，确保结论页数字与实时 API 一致。
    market_price = None
    market_mcap = None
    market_fdv = None
    market_snapshot_time = None

    # 1. 从 biz.asset_market_daily 取（与实时 API 同源，最可靠）
    #    cmc 快照优先，cmc_historical 回填兜底
    try:
        with get_db() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute("""
                    SELECT price_usd, market_cap, fdv, market_date
                    FROM (
                        SELECT price_usd, market_cap, fdv, market_date,
                               ROW_NUMBER() OVER (
                                   ORDER BY CASE source_code
                                       WHEN 'cmc' THEN 0
                                       WHEN 'cmc_historical' THEN 1
                                       ELSE 99 END,
                                   market_date DESC
                               ) AS rn
                        FROM biz.asset_market_daily
                        WHERE asset_id = %s
                          AND source_code IN ('cmc', 'cmc_historical')
                    ) sub
                    WHERE rn = 1
                    LIMIT 1
                """, (asset_id,))
                row = cur.fetchone()
                if row:
                    market_price = _to_float(row.get("price_usd"))
                    market_mcap = _to_float(row.get("market_cap"))
                    market_fdv = _to_float(row.get("fdv"))
                    market_snapshot_time = str(row.get("market_date")) if row.get("market_date") else None
    except (psycopg.errors.UndefinedTable, Exception):
        pass

    # 2. 从 unlock input_snapshot 取
    if (market_price is None and market_mcap is None and market_fdv is None) and isinstance(unlocks, dict):
        snap = unlocks.get("input_snapshot_json") or {}
        p = _to_float(snap.get("price") or snap.get("price_usd"))
        m = _to_float(snap.get("market_cap") or snap.get("market_cap_usd"))
        f = _to_float(snap.get("fdv") or snap.get("fdv_usd") or snap.get("fully_diluted_valuation"))
        t = snap.get("snapshot_time") or snap.get("updated_at") or snap.get("quote_time")
        if p is not None or m is not None or f is not None:
            market_price = p
            market_mcap = m
            market_fdv = f
            market_snapshot_time = t

    # 3. 从 social_heat market_json 取
    if (market_price is None and market_mcap is None) and isinstance(social, dict):
        mj = social.get("market_json") or {}
        p = _to_float(mj.get("price") or mj.get("price_usd"))
        m = _to_float(mj.get("market_cap") or mj.get("market_cap_usd"))
        f = _to_float(mj.get("fdv") or mj.get("fully_diluted_valuation"))
        t = mj.get("snapshot_time") or mj.get("updated_at") or mj.get("quote_time")
        if p is not None or m is not None or f is not None:
            market_price = p
            market_mcap = m
            market_fdv = f
            market_snapshot_time = t

    # 4. 从 CMC 最新报价快照取（按资产取该资产最新 quote_time，避免全局最大时间导致该资产无记录）
    if market_price is None and market_mcap is None:
        try:
            with get_db() as conn:
                with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                    cur.execute("""
                        SELECT q.price_usd, q.market_cap, q.fdv, q.quote_time
                        FROM biz.coin_basic cb
                        JOIN src_cmc.cmc_asset_quote_snapshot q ON q.cmc_id = cb.cmc_id
                        WHERE cb.asset_id = %s
                        ORDER BY q.quote_time DESC
                        LIMIT 1
                    """, (asset_id,))
                    row = cur.fetchone()
                    if row:
                        market_price = _to_float(row.get("price_usd"))
                        market_mcap = _to_float(row.get("market_cap"))
                        market_fdv = _to_float(row.get("fdv"))
                        market_snapshot_time = str(row.get("quote_time")) if row.get("quote_time") else None
        except (psycopg.errors.UndefinedTable, Exception):
            pass

    # 5. 从 tokenomics 推算（价格 * 流通量/总量）
    if market_mcap is None and tokenomics:
        price = _to_float(tokenomics.get("price_usd"))
        circ = _to_float(tokenomics.get("circulating_supply"))
        total = _to_float(tokenomics.get("total_supply"))
        if price is not None and circ is not None:
            market_price = price
            market_mcap = price * circ
        if market_fdv is None and price is not None and total is not None:
            market_fdv = price * total

    if market_price is not None:
        metrics_structured["market"]["price_usd"] = market_price
    if market_mcap is not None:
        metrics_structured["market"]["market_cap_usd"] = market_mcap
    if market_fdv is not None:
        metrics_structured["market"]["fdv_usd"] = market_fdv
    if market_snapshot_time is not None:
        metrics_structured["market"]["snapshot_time"] = market_snapshot_time

    # 代币经济学（supply 优先使用 CMC 权威快照，避免 tokenomics 提取的单位错误传导到结论）
    _auth_supply = {}
    try:
        with get_db() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute("""
                    SELECT q.total_supply, q.circulating_supply, q.max_supply
                    FROM biz.coin_basic cb
                    JOIN src_cmc.cmc_asset_quote_snapshot q ON q.cmc_id = cb.cmc_id
                    WHERE cb.asset_id = %s
                    ORDER BY q.quote_time DESC
                    LIMIT 1
                """, (asset_id,))
                _row = cur.fetchone()
                if _row:
                    _auth_supply = {k: _row[k] for k in ("total_supply", "circulating_supply", "max_supply") if _row[k] is not None}
    except (psycopg.errors.UndefinedTable, Exception):
        pass

    def _prefer_auth_supply(tok_key: str):
        tok_val = tokenomics.get(tok_key)
        auth_val = _auth_supply.get(tok_key)
        if auth_val is None:
            return tok_val
        if tok_val is None:
            return auth_val
        try:
            tv = float(tok_val)
            av = float(auth_val)
            if av > 0 and (tv / av > 10 or tv / av < 0.1):
                return auth_val  # 单位疑似错误，用权威值覆盖
        except (ValueError, TypeError, ZeroDivisionError):
            pass
        return tok_val

    if tokenomics:
        _total_supply = _prefer_auth_supply("total_supply")
        _circ_supply = _prefer_auth_supply("circulating_supply")
        _max_supply = _prefer_auth_supply("max_supply")
        if _total_supply:
            metrics_structured["tokenomics"]["total_supply"] = _total_supply
        if _circ_supply:
            metrics_structured["tokenomics"]["circulating_supply"] = _circ_supply
        if _max_supply:
            metrics_structured["tokenomics"]["max_supply"] = _max_supply
        if tokenomics.get("buy_tax_pct") is not None:
            metrics_structured["tokenomics"]["buy_tax_pct"] = tokenomics["buy_tax_pct"]
        if tokenomics.get("sell_tax_pct") is not None:
            metrics_structured["tokenomics"]["sell_tax_pct"] = tokenomics["sell_tax_pct"]

    # 解锁数据（结构化，LLM 必须严格引用，禁止自行推演解锁时间/金额）
    unlock_pct_30d = None
    if pressure and pressure.get("unlock_pct_30d") is not None:
        unlock_pct_30d = pressure["unlock_pct_30d"]
    metrics_structured["unlock"]["unlock_pct_30d"] = unlock_pct_30d
    if isinstance(unlocks, dict):
        events = unlocks.get("events") or unlocks.get("unlock_events_json") or []
        upcoming = [e for e in events if e.get("is_upcoming")]
        metrics_structured["unlock"]["upcoming_events_count"] = len(upcoming)
        if upcoming:
            next_event = upcoming[0]
            metrics_structured["unlock"]["next_unlock_date"] = next_event.get("date")
            metrics_structured["unlock"]["next_unlock_pct"] = next_event.get("pct")
            metrics_structured["unlock"]["next_unlock_value_usd"] = next_event.get("value_usd") or next_event.get("amount_usd")
        metrics_structured["unlock"]["total_events"] = len(events)

    # 链上持仓
    if onchain and onchain.get("by_chain"):
        chains = list(onchain["by_chain"].keys())
        metrics_structured["onchain"]["chains"] = chains
        # 取第一条链的集中度数据
        first_chain = chains[0] if chains else None
        if first_chain and onchain["by_chain"][first_chain]:
            cd = onchain["by_chain"][first_chain]
            if isinstance(cd, list) and cd:
                latest = cd[-1] if isinstance(cd[-1], dict) else {}
            elif isinstance(cd, dict):
                latest = cd
            else:
                latest = {}
            if latest.get("top10_concentration") is not None:
                metrics_structured["onchain"]["top10_concentration_pct"] = latest["top10_concentration"]
            if latest.get("total_holders") is not None:
                metrics_structured["onchain"]["total_holders"] = latest["total_holders"]

    # 社交热度
    if isinstance(social, dict):
        if social.get("score") is not None:
            metrics_structured["social"]["social_score"] = social["score"]
        if social.get("sentiment_score") is not None:
            metrics_structured["social"]["sentiment_score"] = social["sentiment_score"]
        cj = social.get("community_json") or {}
        for plat in ("x", "twitter", "X"):
            if plat in cj and cj[plat].get("followers"):
                metrics_structured["social"]["x_followers"] = cj[plat]["followers"]
                break

    # 抛压评分
    if pressure:
        metrics_structured["pressure"]["pressure_score"] = pressure.get("pressure_score")
        metrics_structured["pressure"]["risk_level"] = pressure.get("risk_level")
        if pressure.get("top10_concentration") is not None:
            metrics_structured["pressure"]["top10_concentration_pct"] = pressure["top10_concentration"]

    # 衍生品资金面（情绪维度核心数据）
    _emit("采集衍生品资金面数据...")
    try:
        deriv = get_asset_derivatives(asset_id)
        if deriv and deriv.get("ok"):
            d = deriv.get("data") or {}
            if d.get("funding_rate") is not None:
                metrics_structured["derivatives"]["funding_rate"] = d["funding_rate"]
            if d.get("funding_rate_pct") is not None:
                metrics_structured["derivatives"]["funding_rate_pct"] = d["funding_rate_pct"]
            if d.get("funding_rate_7d_avg") is not None:
                metrics_structured["derivatives"]["funding_rate_7d_avg"] = d["funding_rate_7d_avg"]
            if d.get("funding_rate_30d_avg") is not None:
                metrics_structured["derivatives"]["funding_rate_30d_avg"] = d["funding_rate_30d_avg"]
            if d.get("total_oi_usd") is not None:
                metrics_structured["derivatives"]["total_oi_usd"] = d["total_oi_usd"]
            if d.get("oi_change_24h_pct") is not None:
                metrics_structured["derivatives"]["oi_change_24h_pct"] = d["oi_change_24h_pct"]
            if d.get("cvd_24h_usd") is not None:
                metrics_structured["derivatives"]["cvd_24h_usd"] = d["cvd_24h_usd"]
            if d.get("cvd_ratio_24h") is not None:
                metrics_structured["derivatives"]["cvd_ratio_24h"] = d["cvd_ratio_24h"]
            if d.get("available_exchanges"):
                metrics_structured["derivatives"]["exchange_count"] = len(d["available_exchanges"])
    except Exception as e:
        _emit(f"衍生品数据采集失败（不影响结论生成）: {e}")

    # ── 催化剂数据（从 asset_catalyst 取，建立 ID 级关联）──
    catalysts_list = []
    try:
        with get_db() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute(
                    """
                    SELECT ac.catalyst_id, ac.source_code, ac.title, ac.body_text,
                           ac.published_at, ac.event_category,
                           ac.ai_event_type, ac.ai_sentiment, ac.ai_summary,
                           ac.related_pairs, ac.source_url,
                           cal.link_source, cal.confidence
                    FROM biz.catalyst_asset_link cal
                    JOIN biz.asset_catalyst ac ON ac.catalyst_id = cal.catalyst_id
                    WHERE cal.asset_id = %s
                      AND ac.is_active = TRUE
                      AND ac.published_at >= NOW() - INTERVAL '180 days'
                    ORDER BY ac.published_at DESC
                    LIMIT 30
                    """,
                    (asset_id,),
                )
                rows = cur.fetchall()
                for r in rows:
                    catalysts_list.append({
                        "catalyst_id": r["catalyst_id"],
                        "source_code": r["source_code"],
                        "title": r["title"],
                        "summary": r["ai_summary"] or r["title"],
                        "published_at": str(r["published_at"]) if r["published_at"] else None,
                        "event_category": r["event_category"],
                        "event_type": r["ai_event_type"],
                        "sentiment": r["ai_sentiment"],
                        "related_pairs": r["related_pairs"],
                        "source_url": r["source_url"],
                        "link_source": r["link_source"],
                        "confidence": float(r["confidence"]) if r["confidence"] else None,
                    })
        if catalysts_list:
            metrics_structured["catalysts"] = {
                "total_count": len(catalysts_list),
                "bullish_count": sum(1 for c in catalysts_list if c["sentiment"] == "bullish"),
                "bearish_count": sum(1 for c in catalysts_list if c["sentiment"] == "bearish"),
                "neutral_count": sum(1 for c in catalysts_list if c["sentiment"] == "neutral"),
                "source_count": len(set(c["source_code"] for c in catalysts_list)),
                "items": catalysts_list,
            }
            _emit(f"催化剂数据：{len(catalysts_list)} 条（近180天）")
    except Exception as e:
        _emit(f"催化剂数据采集失败（不影响结论生成）: {e}")

    metrics_json_str = json.dumps(metrics_structured, ensure_ascii=False, indent=2, default=str)

    _emit("调用 LLM 生成研究结论...")
    system_prompt = (
        "你是一名资深加密货币投研分析师。请严格只依据下面「资料库」和「结构化指标」中的内容，"
        "输出一个结构化的研究结论 JSON。\n\n"
        "【绝对禁止规则】\n"
        "1. 所有数字、百分比、金额、时间点必须直接引用「结构化指标」中的数据，禁止自行推演、估算或编造。\n"
        "2. 如果结构化指标中某项为 null 或不存在，你不能在结论中写入相关数字，也不能猜测。\n"
        "3. 供应量相关数字（total_supply / circulating_supply / max_supply / 总供应量 / 流通量 / 最大供应量）"
        "必须严格以「结构化指标」中的 tokenomics 字段为准，绝对禁止使用资料库正文中出现的 supply 数字"
        "（文档原文可能存在单位错误或过时数据，一律以结构化指标为准）。\n"
        "4. 解锁风险描述必须严格使用 unlock_pct_30d 和 upcoming_events_count："
        "   - 若 unlock_pct_30d = 0 或 null，且 upcoming_events_count = 0，则必须写「未来30天无代币解锁」，禁止写存在解锁。\n"
        "   - 若有解锁，只能引用 next_unlock_date / next_unlock_pct / next_unlock_value_usd 的具体值。\n"
        "5. 抛压风险等级必须严格使用 pressure.risk_level，不能自行判断高低。\n"
        "6. 营收/收入趋势描述必须基于实际序列判断，禁止用「持续下滑」等绝对化表述描述非单调序列："
        "   - 仅当最近 N 个月/周连续下降时，才可用「持续下滑」。"
        "   - 若存在反弹（如 6 月→7 月回升），必须描述为「先降后升」或「近 X 月/周整体下降但期间有反弹」。"
        "   - 若数据末尾月份带 * 号（表示预估/月度未完结），必须标注「* 为预估/不完整数据」。\n"
        "7. 金额单位必须统一、无歧义：\n"
        "   - 英文/代码场景用 B=十亿、M=百万、K=千；中文场景统一用「亿」「千万」「百万」，禁止把 2.48B 写成「约2.48亿」（2.48B=24.8亿）。\n"
        "   - 引用结构化指标 market.market_cap_usd / market.fdv_usd 时，按实际数值换算，不得篡改数量级。\n"
        "8. 论点必须基于资料库事实，并在 citations 中用 [编号] 标注依据（编号对应资料库条目）。\n"
        "9. 衍生品数据规则：\n"
        "   - 若结构化指标 derivatives.total_oi_usd 存在且 > 0，sentiment 维度必须提及衍生品 OI 数据，\n"
        "     禁止写「缺乏衍生品数据」「衍生品维度缺失」等类似表述。\n"
        "   - 若 derivatives 数据为空，sentiment 维度可说明「衍生品数据暂缺」，但不得编造数字。\n"
        "10. 催化剂数据规则：\n"
        "    - 若结构化指标 catalysts.items 存在且非空，catalyst 维度必须优先引用这些真实新闻事件，\n"
        "      每条都要带上对应的 catalyst_id（从 items 中取），禁止凭空编造催化剂事件。\n"
        "    - catalysts 数组的每项必须包含 catalyst_id（数字）、catalyst（描述）、timing（时间）、\n"
        "      source_code（来源）、event_type（事件类型）、sentiment（情感）。\n"
        "    - 如果 catalysts.items 为空，可以基于 unlock 等其他结构化指标推导催化剂，但不能编造具体新闻事件。\n\n"
        "【四维框架】结论必须按以下四个维度组织，每维都要有数据支撑和引用：\n"
        "1. valuation（估值）：回答「值不值得」——价格、市值、FDV、估值分位、竞品对比\n"
        "2. supply（筹码）：回答「风险在哪（筹码层面）」——持仓集中度、代币分配、解锁抛压、鲸鱼动向\n"
        "3. sentiment（情绪）：回答「现在是不是时机」——社交热度、衍生品资金面、市场情绪\n"
        "   情绪维度必须优先引用 derivatives 数据：\n"
        "   - funding_rate/funding_rate_pct：当前资金费率（正=多头付费给空头，市场偏多；负=空头付费，市场偏空）\n"
        "   - funding_rate_7d_avg/funding_rate_30d_avg：7天/30天平均资金费率（趋势判断）\n"
        "   - total_oi_usd：全市场未平仓合约价值（OI高=市场热度高，杠杆多）\n"
        "   - oi_change_24h_pct：OI 24h变化（OI上升=新资金入场，OI下降=资金离场）\n"
        "   - cvd_24h_usd：24h累计主动买卖净流入（正=主动买入多，看涨；负=主动卖出多，看跌）\n"
        "   - cvd_ratio_24h：CVD占总成交额比例（绝对值高=方向性强）\n"
        "4. catalyst（催化）：回答「风险在哪（外部催化）」——解锁事件、上所、融资、监管、项目动态\n\n"
        "只输出 JSON，不要输出其他内容。JSON 格式：\n"
        '{"stance": "bullish|bearish|neutral", '
        '"conviction": "high|medium|low", '
        '"dimensions": {'
        '"valuation": {"summary": "估值维度一句话结论", "points": [{"point": "论点描述", "citations": [1, 2]}], "data_keys": ["market.price_usd", "market.market_cap_usd"]}, '
        '"supply": {"summary": "筹码维度一句话结论", "points": [{"point": "论点描述", "citations": [3]}], "data_keys": ["onchain.top10_concentration_pct", "unlock.unlock_pct_30d"]}, '
        '"sentiment": {"summary": "情绪维度一句话结论", "points": [{"point": "论点描述", "citations": [4]}], "data_keys": ["social.social_score", "social.sentiment_score"]}, '
        '"catalyst": {"summary": "催化维度一句话结论", "points": [{"point": "论点描述", "timing": "预期时间", "citations": [5]}], "data_keys": ["unlock.next_unlock_date"]}'
        '}, '
        '"thesis": [{"point": "核心论点（一句话）", "citations": [1, 2]}], '
        '"risks": [{"risk": "风险点", "citations": [3]}], '
        '"catalysts": [{"catalyst_id": 123, "catalyst": "催化剂/事件", "timing": "预期时间", "source_code": "binance_news", "event_type": "listing", "sentiment": "bullish"}], '
        '"key_metrics": {"价格": "...", "市值": "...", "FDV": "...", "其他关键指标": "..."}}'
        "\n\n说明：dimensions 是主结构（四维框架），thesis/risks/catalysts 是旧格式兼容字段，两者都要填。"
    )
    user_prompt = (
        f"【资料库】\n\n{context}\n\n"
        f"【结构化指标（所有数字必须从这里取，禁止自行推演）】\n\n{metrics_json_str}\n\n"
        f"请给出该代币的研究结论。"
    )

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

    # ── 催化剂后处理：校验 catalyst_id 有效性，补全来源信息，向后兼容
    if catalysts and catalysts_list:
        _valid_ids = {c["catalyst_id"] for c in catalysts_list}
        _id_map = {c["catalyst_id"]: c for c in catalysts_list}
        _sanitized_catalysts = []
        for cat in catalysts:
            if not isinstance(cat, dict):
                continue
            cid = cat.get("catalyst_id")
            if cid is not None and int(cid) in _valid_ids:
                # 有效引用：补全缺失字段（以 DB 为准）
                src = _id_map[int(cid)]
                cat.setdefault("catalyst", src["summary"] or src["title"])
                cat.setdefault("source_code", src["source_code"])
                cat.setdefault("event_type", src["event_type"] or "other")
                cat.setdefault("sentiment", src["sentiment"] or "neutral")
                if not cat.get("timing") and src.get("published_at"):
                    cat["timing"] = src["published_at"][:10]
                cat["catalyst_id"] = int(cid)
                _sanitized_catalysts.append(cat)
            elif cat.get("catalyst"):
                # 无 ID 但有描述：保留（可能是 LLM 从 unlock 等推导的）
                _sanitized_catalysts.append(cat)
        catalysts = _sanitized_catalysts

    # ── Citation 后处理校验 ──
    # 过滤无效引用（越界/重复/自引），补充 title/url，对无有效引用的论点标记为推断
    def _sanitize_citations(items: list[dict], text_key: str) -> list[dict]:
        cleaned = []
        seen_idx = set()
        for item in items:
            cites = []
            for c in item.get("citations") or []:
                try:
                    idx = int(c) if isinstance(c, (int, float, str)) else int(c.get("index", 0))
                except (TypeError, ValueError):
                    continue
                if idx < 1 or idx > len(sources) or idx in seen_idx:
                    continue
                s = sources[idx - 1]
                # 过滤项目官网交易页/产品页的自引（循环引用）
                if _is_self_serving_source(s):
                    continue
                seen_idx.add(idx)
                cites.append({
                    "index": idx,
                    "title": s.get("title") or s.get("url") or "",
                    "url": s.get("url") or "",
                })
            new_item = dict(item)
            new_item["citations"] = cites
            if not cites:
                new_item["is_inferred"] = True  # 无引用源，标记为推断
            cleaned.append(new_item)
        return cleaned

    thesis = _sanitize_citations(thesis, "point")
    risks = _sanitize_citations(risks, "risk")

    # 四维框架 citation 校验
    dimensions_raw = est.get("dimensions") or {}
    dimensions_clean = {}
    if isinstance(dimensions_raw, dict):
        for dim_key in ("valuation", "supply", "sentiment", "catalyst"):
            dim = dimensions_raw.get(dim_key)
            if isinstance(dim, dict):
                clean_dim = {
                    "summary": dim.get("summary", ""),
                    "points": _sanitize_citations(dim.get("points") or [], "point"),
                    "data_keys": dim.get("data_keys") or [],
                }
                dimensions_clean[dim_key] = clean_dim

    # thesis_json 存储为 dict（含 dimensions + 旧 thesis 列表），兼容旧格式读取
    thesis_payload = {
        "thesis": thesis,
        "dimensions": dimensions_clean,
    }

    with get_db() as conn:
        _ensure_research_tables(conn)
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            # 同一资产 + 同一笔记本只保留最新一条结论，避免重复插入
            cur.execute("""
                INSERT INTO biz.research_thesis
                    (asset_id, stance, conviction, thesis_json, key_metrics_json,
                     risks_json, catalysts_json, source_notebook_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (asset_id, source_notebook_id) DO UPDATE SET
                    stance = EXCLUDED.stance,
                    conviction = EXCLUDED.conviction,
                    thesis_json = EXCLUDED.thesis_json,
                    key_metrics_json = EXCLUDED.key_metrics_json,
                    risks_json = EXCLUDED.risks_json,
                    catalysts_json = EXCLUDED.catalysts_json,
                    updated_at = NOW()
                RETURNING thesis_id, asset_id, stance, conviction, thesis_json,
                          key_metrics_json, risks_json, catalysts_json,
                          source_notebook_id, created_at, updated_at
            """, (
                asset_id, stance, conviction,
                json.dumps(thesis_payload, ensure_ascii=False),
                json.dumps(key_metrics, ensure_ascii=False, default=str),
                json.dumps(risks, ensure_ascii=False),
                json.dumps(catalysts, ensure_ascii=False),
                notebook_id,
            ))
            row = cur.fetchone()
        conn.commit()

    _emit("研究结论已生成")
    result = _thesis_row_to_dict(row)
    result["structured_metrics"] = metrics_structured
    return {"ok": True, "data": result}


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


def get_onchain_holder_trend(asset_id: int, days: int = 30) -> dict:
    """链上持仓趋势：返回指定天数内的每日快照时间序列，用于趋势图。

    每条数据点包含：日期、Top10/50/100 集中度、持有者数、鲸鱼余额变化、交易所钱包占比。
    按链分组，每条链独立时间序列。
    """
    days = max(7, min(90, days))
    with get_db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("""
                SELECT hs.*, a.canonical_symbol, a.canonical_name
                FROM biz.onchain_holder_snapshot hs
                INNER JOIN core.asset a ON a.asset_id = hs.asset_id
                WHERE hs.asset_id = %s
                  AND hs.snapshot_date >= CURRENT_DATE - %s * INTERVAL '1 day'
                ORDER BY hs.chain, hs.snapshot_date ASC
            """, (asset_id, days))
            rows = [dict(r) for r in cur.fetchall()]

            if not rows:
                return {
                    "ok": True,
                    "asset_id": asset_id,
                    "symbol": "",
                    "name": "",
                    "days": days,
                    "data_points": 0,
                    "by_chain": {},
                    "has_enough_data": False,
                }

            # 按链分组
            by_chain = {}
            for r in rows:
                chain = r["chain"]
                if chain not in by_chain:
                    by_chain[chain] = []
                by_chain[chain].append({
                    "date": str(r["snapshot_date"]),
                    "top10_concentration": float(r["top10_concentration"]) if r["top10_concentration"] else None,
                    "top50_concentration": float(r["top50_concentration"]) if r["top50_concentration"] else None,
                    "top100_concentration": float(r["top100_concentration"]) if r["top100_concentration"] else None,
                    "total_holders": r["total_holders"],
                    "whale_balance_change_7d_pct": float(r["whale_balance_change_7d_pct"]) if r["whale_balance_change_7d_pct"] else None,
                    "exchange_wallet_pct": float(r["exchange_wallet_pct"]) if r["exchange_wallet_pct"] else None,
                })

            # 计算每条链的变化趋势（首末对比）
            trend_summary = {}
            for chain, points in by_chain.items():
                if len(points) < 2:
                    trend_summary[chain] = {"points": len(points), "trend": "insufficient"}
                    continue
                first = points[0]
                last = points[-1]
                def _chg(a, b):
                    if a is None or b is None or a == 0:
                        return None
                    return round((b - a) / abs(a) * 100, 2)
                trend_summary[chain] = {
                    "points": len(points),
                    "first_date": first["date"],
                    "last_date": last["date"],
                    "top10_change_pct": _chg(first["top10_concentration"], last["top10_concentration"]),
                    "holders_change_pct": _chg(first["total_holders"], last["total_holders"]),
                    "trend": "available",
                }

            total_points = len(rows)
            has_enough = total_points >= 7  # 至少 7 个数据点才算有意义的趋势

    return {
        "ok": True,
        "asset_id": asset_id,
        "symbol": rows[0]["canonical_symbol"] if rows else "",
        "name": rows[0]["canonical_name"] if rows else "",
        "days": days,
        "data_points": total_points,
        "has_enough_data": has_enough,
        "by_chain": by_chain,
        "trend_summary": trend_summary,
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

            # 排除测试数据
            conditions.append("tl.tx_hash NOT LIKE '0xtest%'")

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
    """获取链上告警摘要：最近 24h 转入交易所的大额转账统计。

    自动过滤测试数据（tx_hash 以 0xtest 开头），并返回采集状态。
    """
    with get_db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            # 先判断链上采集是否已启用（有真实数据才算启用）
            cur.execute("""
                SELECT COUNT(*) AS real_count
                FROM biz.onchain_transfer_log
                WHERE tx_hash NOT LIKE '0xtest%'
            """)
            real_count = cur.fetchone()["real_count"] or 0
            is_enabled = real_count > 0

            # 24h 转入交易所汇总（排除测试数据）
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
                  AND tl.tx_hash NOT LIKE '0xtest%'
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

            # 总览（排除测试数据）
            cur.execute("""
                SELECT
                    COUNT(*) AS total_transfers,
                    COUNT(*) FILTER (WHERE is_to_exchange) AS to_exchange_count,
                    COALESCE(SUM(value_usd), 0) AS total_value_usd
                FROM biz.onchain_transfer_log
                WHERE tx_hash NOT LIKE '0xtest%'
            """)
            totals = dict(cur.fetchone()) if cur.rowcount else {}

    return {
        "ok": True,
        "is_enabled": is_enabled,
        "notice": "" if is_enabled else "链上大额转账采集尚未启用，当前无真实数据",
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
    日期按北京时间（Asia/Shanghai）计算，与调度时区对齐，避免 UTC 数据库下凌晨跑批日期错位。
    """
    with get_db() as conn:
        _ensure_onchain_snapshot_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM biz.onchain_holder_snapshot
                WHERE asset_id = %s AND chain = %s
                  AND snapshot_date = (CURRENT_DATE AT TIME ZONE 'Asia/Shanghai')::date
                """,
                (asset_id, chain),
            )
            cur.execute(
                """
                INSERT INTO biz.onchain_holder_snapshot
                    (asset_id, chain, contract_address, snapshot_date,
                     top10_concentration, top50_concentration, top100_concentration,
                     total_holders, fetched_at)
                VALUES (%s, %s, %s,
                        (CURRENT_DATE AT TIME ZONE 'Asia/Shanghai')::date,
                        %s, %s, %s, %s, NOW())
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

    数据来源：biz.asset_token_unlocks（未来 7/30 天解锁占比）+ biz.onchain_holder_snapshot
    （Top10 集中度，取最新快照）+ CoinGecko（市值/24h 交易量算换手率）。结果缓存到
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
                "SELECT top10_concentration AS top_10_pct FROM biz.onchain_holder_snapshot "
                "WHERE asset_id = %s ORDER BY snapshot_date DESC LIMIT 1",
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


def analyze_unlock_event_impact(
    asset_id: int,
    window_days: int = 14,
) -> dict:
    """解锁事件研究：分析每个历史解锁事件前后的价格走势。

    核心问题：解锁前会不会跌？解锁后会不会反弹？
    对每个历史解锁事件，取前后 N 天的价格数据，计算：
    - 事件前收益（pre_return）
    - 事件后收益（post_return）
    - 事件前最大回撤（pre_max_drawdown）
    - 事件后最大涨幅（post_max_rally）
    - 解锁规模 vs 价格波动的相关性

    Args:
        asset_id: 资产 ID
        window_days: 事件前后窗口天数，默认 14 天

    Returns:
        {
            "ok": bool,
            "asset_id": int,
            "window_days": int,
            "total_events": int,
            "analyzed_events": int,  # 有足够行情数据的事件数
            "events": [
                {
                    "unlock_date": str,
                    "unlock_pct": float,
                    "unlock_value_usd": float,
                    "price_at_event": float,
                    "pre_return_pct": float,       # 事件前 N 天涨跌幅
                    "post_return_pct": float,      # 事件后 N 天涨跌幅
                    "pre_max_drawdown_pct": float, # 事件前最大回撤（从窗口高点到事件日）
                    "post_max_rally_pct": float,   # 事件后最大涨幅（从事件日到窗口高点）
                    "volume_surge": float,         # 事件日成交量 / 前 7 日均量
                }
            ],
            "summary": {
                "avg_pre_return_pct": float,
                "avg_post_return_pct": float,
                "pre_positive_rate": float,     # 事件前上涨比例
                "post_positive_rate": float,    # 事件后上涨比例
                "avg_pre_max_drawdown_pct": float,
                "avg_post_max_rally_pct": float,
                "big_unlock_pre_return_pct": float,   # 大额解锁（>=5%）前收益
                "big_unlock_post_return_pct": float,  # 大额解锁（>=5%）后收益
            },
        }
    """
    from datetime import timedelta

    # 1. 读取解锁事件
    with get_db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT unlock_events_json FROM biz.asset_token_unlocks WHERE asset_id = %s",
                (asset_id,),
            )
            urow = cur.fetchone()

    if not urow:
        return {"ok": False, "error": "无解锁数据", "asset_id": asset_id}

    events_raw = urow.get("unlock_events_json") or []
    if not events_raw:
        return {"ok": False, "error": "解锁事件为空", "asset_id": asset_id}

    # 2. 解析历史解锁事件（排除 upcoming）
    historical_events = []
    for e in events_raw:
        if e.get("is_upcoming"):
            continue
        d = _parse_unlock_event_date(e.get("date"))
        if d is None:
            continue
        pct = _to_float(e.get("pct"))
        value_usd = _to_float(e.get("value_usd") or e.get("amount_usd"))
        historical_events.append({
            "date": d,
            "pct": pct or 0.0,
            "value_usd": value_usd,
        })

    if not historical_events:
        return {"ok": False, "error": "无历史解锁事件", "asset_id": asset_id}

    # 3. 取足够长的行情历史（最早事件前 window 天 到 最晚事件后 window 天）
    dates = [e["date"] for e in historical_events]
    earliest = min(dates) - timedelta(days=window_days)
    latest = max(dates) + timedelta(days=window_days)
    total_days = (latest - earliest).days + 1

    history = get_asset_market_history(asset_id, days=total_days + 30)  # 多取 30 天兜底
    series = history.get("series") or []
    if not series:
        return {"ok": False, "error": "无行情历史数据", "asset_id": asset_id}

    # 构建 date -> price/volume 映射
    price_map = {}
    for s in series:
        price_map[s["date"]] = s

    # 4. 逐个事件分析
    analyzed = []
    for ev in historical_events:
        ev_date_str = str(ev["date"])
        if ev_date_str not in price_map:
            continue  # 事件日无行情数据，跳过

        event_price = price_map[ev_date_str].get("price_usd")
        if event_price is None or event_price <= 0:
            continue

        # 收集前后窗口数据
        pre_prices = []
        post_prices = []
        pre_volumes = []

        for i in range(1, window_days + 1):
            pre_date = str(ev["date"] - timedelta(days=i))
            post_date = str(ev["date"] + timedelta(days=i))
            if pre_date in price_map and price_map[pre_date].get("price_usd"):
                pre_prices.append((i, price_map[pre_date]["price_usd"]))
                if price_map[pre_date].get("volume_24h"):
                    pre_volumes.append(price_map[pre_date]["volume_24h"])
            if post_date in price_map and price_map[post_date].get("price_usd"):
                post_prices.append((i, price_map[post_date]["price_usd"]))

        if len(pre_prices) < 3 or len(post_prices) < 3:
            continue  # 数据点太少，跳过

        # 事件前收益（窗口起点到事件日）
        first_pre = pre_prices[-1][1]  # 最远的一天
        pre_return = (event_price - first_pre) / first_pre * 100

        # 事件后收益（事件日到窗口终点）
        last_post = post_prices[-1][1]
        post_return = (last_post - event_price) / event_price * 100

        # 事件前最大回撤（窗口内高点到事件日的跌幅）
        pre_high = max(p for _, p in pre_prices)
        pre_max_dd = (event_price - pre_high) / pre_high * 100  # 负值

        # 事件后最大涨幅（事件日到窗口内高点）
        post_high = max(p for _, p in post_prices)
        post_max_rally = (post_high - event_price) / event_price * 100

        # 成交量放大倍数（事件日 vs 前7日均量）
        volume_surge = None
        event_vol = price_map[ev_date_str].get("volume_24h")
        if event_vol and pre_volumes:
            avg_vol = sum(pre_volumes[:7]) / min(7, len(pre_volumes))
            if avg_vol > 0:
                volume_surge = round(event_vol / avg_vol, 2)

        analyzed.append({
            "unlock_date": ev_date_str,
            "unlock_pct": ev["pct"],
            "unlock_value_usd": ev["value_usd"],
            "price_at_event": event_price,
            "pre_return_pct": round(pre_return, 2),
            "post_return_pct": round(post_return, 2),
            "pre_max_drawdown_pct": round(pre_max_dd, 2),
            "post_max_rally_pct": round(post_max_rally, 2),
            "volume_surge": volume_surge,
        })

    if not analyzed:
        return {
            "ok": False,
            "error": "无足够行情数据进行事件研究",
            "asset_id": asset_id,
            "total_events": len(historical_events),
        }

    # 5. 汇总统计
    pre_returns = [e["pre_return_pct"] for e in analyzed]
    post_returns = [e["post_return_pct"] for e in analyzed]
    pre_dds = [e["pre_max_drawdown_pct"] for e in analyzed]
    post_rallies = [e["post_max_rally_pct"] for e in analyzed]

    pre_positive = sum(1 for r in pre_returns if r > 0)
    post_positive = sum(1 for r in post_returns if r > 0)

    # 大额解锁（>=5%）单独统计
    big_events = [e for e in analyzed if e["unlock_pct"] >= 5]
    big_pre = [e["pre_return_pct"] for e in big_events]
    big_post = [e["post_return_pct"] for e in big_events]

    summary = {
        "avg_pre_return_pct": round(sum(pre_returns) / len(pre_returns), 2),
        "avg_post_return_pct": round(sum(post_returns) / len(post_returns), 2),
        "pre_positive_rate": round(pre_positive / len(analyzed) * 100, 1),
        "post_positive_rate": round(post_positive / len(analyzed) * 100, 1),
        "avg_pre_max_drawdown_pct": round(sum(pre_dds) / len(pre_dds), 2),
        "avg_post_max_rally_pct": round(sum(post_rallies) / len(post_rallies), 2),
        "big_unlock_count": len(big_events),
        "big_unlock_avg_pre_return_pct": round(sum(big_pre) / len(big_pre), 2) if big_pre else None,
        "big_unlock_avg_post_return_pct": round(sum(big_post) / len(big_post), 2) if big_post else None,
    }

    # 按日期倒序
    analyzed.sort(key=lambda e: e["unlock_date"], reverse=True)

    return {
        "ok": True,
        "asset_id": asset_id,
        "window_days": window_days,
        "total_events": len(historical_events),
        "analyzed_events": len(analyzed),
        "events": analyzed,
        "summary": summary,
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


def get_social_heat_leaderboard(
    tier: str | None = None,
    limit: int = 20,
    sort_by: str = "score",
) -> dict:
    """社交热度排行榜：按市值分层展示社交热度最高的资产。

    Args:
        tier: 市值分层，None/'all' 表示全部
        limit: 返回数量，默认 20
        sort_by: 排序字段，score / sentiment_score / trending_rank

    Returns:
        {
            "ok": bool,
            "tier": str,
            "sort_by": str,
            "total": int,
            "assets": [
                {
                    "asset_id": int, "symbol": str, "name": str, "cmc_rank": int,
                    "score": float, "confidence": str,
                    "sentiment": str, "sentiment_score": float,
                    "trending_rank": int | None,
                    "community_size": int | None,
                    "fetched_at": str,
                }
            ],
            "sentiment_distribution": {"positive": int, "neutral": int, "negative": int},
        }
    """
    # 分层条件
    tier_cond = ""
    tier_params: list = []
    if tier and tier != "all":
        if tier == "top100":
            tier_cond = "AND COALESCE(cm.rank_num, ci.market_cap_rank, 999999) <= 100"
        elif tier == "top500":
            tier_cond = "AND COALESCE(cm.rank_num, ci.market_cap_rank, 999999) <= 500"
        elif tier == "top1000":
            tier_cond = "AND COALESCE(cm.rank_num, ci.market_cap_rank, 999999) <= 1000"
        elif tier == "other":
            tier_cond = "AND COALESCE(cm.rank_num, ci.market_cap_rank, 999999) > 1000"
        else:
            tier_cond = ""

    # 排序字段
    sort_col = "sh.score DESC"
    if sort_by == "sentiment_score":
        sort_col = "(sh.sentiment_json->>'sentiment_score')::float DESC"
    elif sort_by == "trending_rank":
        sort_col = "(sh.trend_json->>'trending_rank')::int ASC NULLS LAST"

    with get_db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            # 总数
            cur.execute(
                f"""
                SELECT COUNT(*) AS cnt
                FROM biz.asset_social_heat sh
                JOIN biz.coin_basic cb ON cb.asset_id = sh.asset_id
                LEFT JOIN src_cmc.cmc_asset_map cm ON cm.cmc_id = cb.cmc_id
                LEFT JOIN core.asset_source_map asm ON asm.asset_id = cb.asset_id
                    AND asm.source_code = 'cg' AND asm.is_primary = TRUE
                LEFT JOIN src_cg.coin_info ci ON ci.coin_id = asm.source_asset_key
                WHERE sh.score IS NOT NULL
                  {tier_cond}
                """,
                tier_params,
            )
            total = cur.fetchone()["cnt"]

            # 排行榜
            cur.execute(
                f"""
                SELECT
                    cb.asset_id,
                    a.canonical_symbol AS symbol,
                    a.canonical_name AS name,
                    COALESCE(cm.rank_num, ci.market_cap_rank) AS cmc_rank,
                    sh.score,
                    sh.confidence,
                    sh.sentiment_json->>'sentiment' AS sentiment,
                    (sh.sentiment_json->>'sentiment_score')::float AS sentiment_score,
                    (sh.trend_json->>'trending_rank')::int AS trending_rank,
                    COALESCE(
                        (sh.community_json->'x'->>'followers')::bigint,
                        (sh.community_json->'twitter'->>'followers')::bigint,
                        (sh.community_json->'reddit'->>'subscribers')::bigint,
                        0
                    ) AS community_size,
                    sh.fetched_at
                FROM biz.asset_social_heat sh
                JOIN biz.coin_basic cb ON cb.asset_id = sh.asset_id
                JOIN core.asset a ON a.asset_id = cb.asset_id
                LEFT JOIN src_cmc.cmc_asset_map cm ON cm.cmc_id = cb.cmc_id
                LEFT JOIN core.asset_source_map asm ON asm.asset_id = cb.asset_id
                    AND asm.source_code = 'cg' AND asm.is_primary = TRUE
                LEFT JOIN src_cg.coin_info ci ON ci.coin_id = asm.source_asset_key
                WHERE sh.score IS NOT NULL
                  {tier_cond}
                ORDER BY {sort_col}
                LIMIT %s
                """,
                (*tier_params, limit),
            )
            rows = cur.fetchall()

            # 情绪分布
            cur.execute(
                f"""
                SELECT
                    COALESCE(sh.sentiment_json->>'sentiment', 'neutral') AS sentiment,
                    COUNT(*) AS cnt
                FROM biz.asset_social_heat sh
                JOIN biz.coin_basic cb ON cb.asset_id = sh.asset_id
                LEFT JOIN src_cmc.cmc_asset_map cm ON cm.cmc_id = cb.cmc_id
                LEFT JOIN core.asset_source_map asm ON asm.asset_id = cb.asset_id
                    AND asm.source_code = 'cg' AND asm.is_primary = TRUE
                LEFT JOIN src_cg.coin_info ci ON ci.coin_id = asm.source_asset_key
                WHERE sh.score IS NOT NULL
                  {tier_cond}
                GROUP BY COALESCE(sh.sentiment_json->>'sentiment', 'neutral')
                """,
                tier_params,
            )
            dist_rows = cur.fetchall()

    assets = []
    for r in rows:
        assets.append({
            "asset_id": r["asset_id"],
            "symbol": r["symbol"],
            "name": r["name"],
            "cmc_rank": r["cmc_rank"],
            "score": _social_float(r.get("score")),
            "confidence": r.get("confidence"),
            "sentiment": r.get("sentiment") or "neutral",
            "sentiment_score": float(r["sentiment_score"]) if r.get("sentiment_score") is not None else None,
            "trending_rank": r.get("trending_rank"),
            "community_size": int(r["community_size"]) if r.get("community_size") else None,
            "fetched_at": str(r["fetched_at"]) if r.get("fetched_at") else None,
        })

    sentiment_dist = {"positive": 0, "neutral": 0, "negative": 0}
    for d in dist_rows:
        s = d["sentiment"] or "neutral"
        if s in sentiment_dist:
            sentiment_dist[s] = d["cnt"]
        else:
            sentiment_dist["neutral"] += d["cnt"]

    return {
        "ok": True,
        "tier": tier or "all",
        "sort_by": sort_by,
        "total": total,
        "assets": assets,
        "sentiment_distribution": sentiment_dist,
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

        # supply 安全校验：对比 CMC 权威快照，偏离 >10 倍则用权威值覆盖
        try:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as _cur:
                _cur.execute(
                    """
                    SELECT q.total_supply AS auth_total,
                           q.circulating_supply AS auth_circ,
                           q.max_supply AS auth_max
                    FROM biz.coin_basic cb
                    JOIN src_cmc.cmc_asset_quote_snapshot q ON q.cmc_id = cb.cmc_id
                    WHERE cb.asset_id = %s
                      AND q.quote_time = (SELECT MAX(quote_time) FROM src_cmc.cmc_asset_quote_snapshot)
                    """,
                    (asset_id,),
                )
                _auth = _cur.fetchone()
                if _auth:
                    tkn = dict(tkn)  # 转可写 dict
                    for _tok_key, _auth_key in [
                        ("total_supply", "auth_total"),
                        ("circulating_supply", "auth_circ"),
                        ("max_supply", "auth_max"),
                    ]:
                        _tv = tkn.get(_tok_key)
                        _av = _auth.get(_auth_key)
                        if _av is None:
                            continue
                        if _tv is None:
                            tkn[_tok_key] = _av
                            continue
                        try:
                            _tvf = float(_tv)
                            _avf = float(_av)
                            if _avf > 0 and (_tvf / _avf > 10 or _tvf / _avf < 0.1):
                                tkn[_tok_key] = _av  # 单位疑似错误，用权威值覆盖
                        except (ValueError, TypeError, ZeroDivisionError):
                            pass
        except (psycopg.errors.UndefinedTable, Exception):
            pass

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
    """读取 biz.onchain_holder_snapshot 中最新的持仓分布数据。"""
    with get_db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """SELECT s.*, a.canonical_symbol AS symbol, a.canonical_name AS name
                   FROM biz.onchain_holder_snapshot s
                   JOIN core.asset a ON a.asset_id = s.asset_id
                   WHERE s.asset_id = %s
                   ORDER BY s.snapshot_date DESC
                   LIMIT 1""",
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
            "top_5_pct": None,  # onchain_holder_snapshot 不存 top5
            "top_10_pct": float(row["top10_concentration"]) if row["top10_concentration"] else None,
            "top_50_pct": float(row["top50_concentration"]) if row["top50_concentration"] else None,
            "top_100_pct": float(row["top100_concentration"]) if row["top100_concentration"] else None,
            "top_holders": json.loads(row["top_holders_json"]) if isinstance(row["top_holders_json"], str) else row["top_holders_json"],
            "tier_distribution": json.loads(row["tier_distribution_json"]) if isinstance(row["tier_distribution_json"], str) else row["tier_distribution_json"],
            "scraped_at": str(row["fetched_at"]) if row["fetched_at"] else None,
            "snapshot_date": str(row["snapshot_date"]) if row["snapshot_date"] else None,
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


# ═══════════════════════════════════════════════════════════════
# 每日 diff 变化榜
# ═══════════════════════════════════════════════════════════════

def get_daily_diff_summary(diff_date: str | None = None, categories: list[str] | None = None) -> dict:
    """获取每日 diff 变化榜。

    Args:
        diff_date: 指定日期（YYYY-MM-DD），None 取最新一天
        categories: 过滤榜单类型，None 返回全部

    Returns:
        {
            "ok": True,
            "diff_date": "2026-08-20",
            "categories": {
                "price_change_24h": {
                    "up": [...],
                    "down": [...]
                },
                ...
            }
        }
    """
    with get_db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            # 确定日期
            if diff_date:
                target_date = diff_date
            else:
                cur.execute("SELECT max(diff_date) AS d FROM biz.daily_diff_summary")
                row = cur.fetchone()
                if not row or not row["d"]:
                    return {"ok": True, "diff_date": None, "categories": {}}
                target_date = str(row["d"])

            cat_filter = ""
            params: list = [target_date]
            if categories:
                placeholders = ",".join(["%s"] * len(categories))
                cat_filter = f" AND category IN ({placeholders})"
                params.extend(categories)

            cur.execute(
                f"""
                SELECT
                    d.category,
                    d.direction,
                    d.rank,
                    d.metric_value,
                    d.metric_label,
                    d.detail_json,
                    a.asset_id,
                    a.canonical_symbol,
                    a.canonical_name,
                    a.market_cap_rank,
                    -- 同名 symbol 可能对应多个项目（如 TUT=Tutorial 与 TUT=Tutellus），
                    -- 取主链以便前端展示，避免两个 TUT 在榜单上被误认为同一资产。
                    (SELECT ac.chain
                     FROM core.asset_contract ac
                     WHERE ac.asset_id = a.asset_id
                     ORDER BY ac.is_primary DESC NULLS LAST
                     LIMIT 1) AS chain
                FROM biz.daily_diff_summary d
                JOIN core.asset a ON a.asset_id = d.asset_id
                WHERE d.diff_date = %s
                  {cat_filter}
                ORDER BY d.category, d.direction, d.rank
                """,
                tuple(params),
            )
            rows = cur.fetchall()

            result: dict = {}
            for r in rows:
                cat = r["category"]
                direction = r["direction"]
                if cat not in result:
                    result[cat] = {"up": [], "down": []}
                result[cat][direction].append({
                    "asset_id": r["asset_id"],
                    "symbol": r["canonical_symbol"],
                    "name": r["canonical_name"],
                    "chain": r["chain"],
                    "market_cap_rank": r["market_cap_rank"],
                    "rank": r["rank"],
                    "metric_value": float(r["metric_value"]) if r["metric_value"] is not None else None,
                    "metric_label": r["metric_label"],
                    "detail": r["detail_json"] or {},
                })

            return {
                "ok": True,
                "diff_date": target_date,
                "categories": result,
            }
