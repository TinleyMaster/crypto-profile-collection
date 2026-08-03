"""
Phase B5: 链接健康检查 + AI 投研相关性筛选

从 biz.doc_asset 和 biz.doc_source_entry 中提取所有投研相关链接，
进行健康检查（HEAD 请求检测可达性），然后用 AI 筛选投研相关性，
结果写入 biz.research_url 表，供 B6 生成投研网址链接文件。

流程：
  1. 从 DB 收集所有候选链接（doc_asset + doc_source_entry 中与投研相关的）
  2. 并发 HEAD 请求检查链接健康状态
  3. 用 AI 批量评估链接的投研相关性
  4. 分类写入 biz.research_url 表
     - status: healthy / protected / dead / unchecked
     - relevance_score: 0.0-1.0
     - ai_reason: AI 判定理由（简短中文）
"""
from __future__ import annotations

import argparse
import json
import sys
import io
import time
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

# ── 健康检查并发限速 ──
MAX_HEAD_WORKERS = 30
HEAD_TIMEOUT = 8
HEAD_RETRIES = 1

# ── AI 批量大小 ──
AI_BATCH_SIZE = 30

# ── 全局统计 ──
_stats_lock = threading.Lock()
_stats = {"checked": 0, "healthy": 0, "protected": 0, "dead": 0, "error": 0}
_pending_inserts: list[tuple] = []
_db_lock = threading.Lock()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Phase B5: 链接健康检查 + AI 投研筛选")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=200, help="最大处理链接数")
    p.add_argument("--ai-batch-size", type=int, default=30, help="AI 调用每批链接数")
    p.add_argument("--skip-ai", action="store_true", help="跳过 AI 筛选（仅做健康检查）")
    p.add_argument("--skip-health", action="store_true", help="跳过健康检查（仅做 AI 筛选）")
    p.add_argument("--asset-id", type=int, default=0, help="只处理指定 asset_id")
    p.add_argument("--flush-every", type=int, default=100, help="每 N 条 flush 一次 DB")
    return p


# ── 步骤 1: 从 DB 收集候选链接 ──
def collect_candidate_urls(conn, limit: int, asset_id: int = 0) -> list[dict]:
    """
    从 biz.doc_asset (已发现的文档) 和 biz.doc_source_entry (原始入口)
    中收集所有投研相关链接。每个链接带上 asset 信息和来源。
    """
    import psycopg

    urls: list[dict] = []
    seen: set[str] = set()

    asset_filter = "AND cb.asset_id = %s" if asset_id else ""
    params_base = (asset_id,) if asset_id else ()

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        # 1. doc_asset 中的文档链接（白皮书等）
        query_docs = f"""
            SELECT DISTINCT ON (da.source_url)
                da.doc_id, da.source_url AS url, da.doc_type,
                da.entity_type, da.asset_id, da.protocol_id,
                cb.coin_symbol, cb.coin_name,
                da.file_name, da.mime_type,
                'doc_asset' AS url_source
            FROM biz.doc_asset da
            JOIN biz.coin_basic cb ON cb.asset_id = da.asset_id
            WHERE da.source_url IS NOT NULL
              AND da.parse_status != 'dead'
              {asset_filter.replace('cb.asset_id', 'da.asset_id') if asset_id else ''}
            ORDER BY da.source_url, da.last_seen_at DESC NULLS LAST
            LIMIT {limit}
        """
        cur.execute(query_docs, params_base if asset_id else ())
        for row in cur.fetchall():
            r = dict(row)
            url_key = (r["url"] or "").strip().rstrip("/")
            if url_key and url_key not in seen:
                seen.add(url_key)
                urls.append(r)

        # 2. doc_source_entry 中的官网/文档入口
        remaining = limit - len(urls)
        if remaining > 0:
            query_entries = f"""
                SELECT DISTINCT ON (dse.entry_url)
                    dse.entry_id, dse.entry_url AS url, dse.entry_type AS doc_type,
                    dse.entity_type, dse.asset_id, dse.protocol_id,
                    dse.source_code,
                    cb.coin_symbol, cb.coin_name,
                    NULL AS file_name, NULL AS mime_type,
                    'doc_source_entry' AS url_source
                FROM biz.doc_source_entry dse
                JOIN biz.coin_basic cb ON cb.asset_id = dse.asset_id
                WHERE dse.entry_type IN ('docs', 'official_website', 'github', 'medium')
                  AND dse.entry_url IS NOT NULL
                  {asset_filter.replace('cb.asset_id', 'dse.asset_id') if asset_id else ''}
                ORDER BY dse.entry_url, dse.is_primary DESC NULLS LAST, dse.updated_at DESC NULLS LAST
                LIMIT {remaining}
            """
            cur.execute(query_entries, params_base if asset_id else ())
            for row in cur.fetchall():
                r = dict(row)
                url_key = (r["url"] or "").strip().rstrip("/")
                if url_key and url_key not in seen:
                    seen.add(url_key)
                    urls.append(r)

    return urls


# ── 步骤 2: 健康检查 ──
def _make_head_session():
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "*/*",
    })
    retry = Retry(total=HEAD_RETRIES, backoff_factor=0.5,
                  status_forcelist=[429, 500, 502, 503])
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def check_one_url(url_info: dict) -> dict:
    """
    对单个 URL 执行 HEAD 请求，判定健康状态。
    返回: healthy / protected / dead / error
    """
    url = url_info["url"]
    result = {**url_info, "http_status": None, "health_status": "error", "final_url": url}

    session = _make_head_session()
    try:
        resp = session.head(url, timeout=HEAD_TIMEOUT, allow_redirects=True)
        status = resp.status_code
        result["http_status"] = status
        result["final_url"] = resp.url

        if 200 <= status < 400:
            result["health_status"] = "healthy"
        elif status in (403, 401):
            # 403 可能是 Cloudflare/WAF 防护
            result["health_status"] = "protected"
        elif status == 404 or status == 410:
            result["health_status"] = "dead"
        elif status == 429:
            result["health_status"] = "protected"  # 速率限制也当做受保护
        else:
            result["health_status"] = "dead"  # 其他错误码视作死链

    except Exception as e:
        err_str = str(e).lower()
        if "timeout" in err_str or "timed out" in err_str:
            result["health_status"] = "error"
            result["error"] = "timeout"
        elif "connection" in err_str or "resolve" in err_str or "dns" in err_str:
            result["health_status"] = "dead"
            result["error"] = str(e)[:100]
        elif "ssl" in err_str or "certificate" in err_str:
            # SSL 错误仍可能访问，标记为 protected
            result["health_status"] = "protected"
            result["error"] = "ssl_error"
        else:
            result["health_status"] = "error"
            result["error"] = str(e)[:100]

    return result


def run_health_check(urls: list[dict], max_workers: int = MAX_HEAD_WORKERS) -> list[dict]:
    """并发健康检查"""
    results: list[dict] = []
    total = len(urls)
    print(f"健康检查: {total} 个链接, {max_workers} workers")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(check_one_url, u): u for u in urls}

        for i, future in enumerate(as_completed(futures), 1):
            r = future.result()
            results.append(r)

            with _stats_lock:
                _stats["checked"] += 1
                _stats[r["health_status"]] = _stats.get(r["health_status"], 0) + 1

            if i % 100 == 0 or i == total:
                with _stats_lock:
                    s = dict(_stats)
                print(f"  [{i}/{total}] healthy:{s['healthy']} "
                      f"protected:{s['protected']} dead:{s['dead']} error:{s['error']}")

    return results


# ── 步骤 3: AI 投研相关性筛选 ──
def build_ai_prompt(urls_batch: list[dict]) -> str:
    """构建 AI 批量评估的 prompt"""
    lines = []
    for i, u in enumerate(urls_batch, 1):
        symbol = u.get("coin_symbol", "?")
        name = u.get("coin_name", "?")
        url = u.get("url", "")
        doc_type = u.get("doc_type", "")
        domain = urlparse(url).netloc
        lines.append(
            f"[{i}] {symbol}({name}) | type:{doc_type} | domain:{domain}\n    URL: {url}"
        )

    prompt = f"""你是一个加密货币投研资料筛选助手。以下是 {len(urls_batch)} 个链接，请判断每个链接是否适合作为该代币的投研参考资料。

投研资料包括但不限于：
- 白皮书/技术文档/经济模型
- 官方文档/开发者文档
- 审计报告
- 路线图/Roadmap
- 项目博客/Mirror文章（技术类、经济类）
- GitHub 仓库（非垃圾/模板仓库）
- 代币经济学/Tokenomics说明

不包含：
- 社交媒体主页（Twitter/X, Discord, Telegram）
- 交易所页面
- 纯价格/行情页面
- 空页面/重定向到社交媒体
- 通用条款/隐私政策页面

对每个链接给出：
- relevance_score: 0.0-1.0（1.0=非常适合投研参考）
- reason: 简短中文理由（15字以内）
- category: whitepaper/docs/audit/tokenomics/github/blog/official/other/none

请以 JSON 格式返回，格式如下：
{{"results":[{{"index":1,"relevance_score":0.95,"reason":"官方白皮书","category":"whitepaper"}}, ...]}}

链接列表：
{chr(10).join(lines)}"""
    return prompt


def call_ai_batch(urls_batch: list[dict]) -> list[dict] | None:
    """
    调用 AI API 批量评估链接投研相关性。
    优先使用 OpenAI-compatible API，fallback 到简单的关键词评分。
    """
    import os
    import requests as req

    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY") or ""
    api_base = os.getenv("OPENAI_BASE_URL") or os.getenv("LLM_BASE_URL") or "https://api.openai.com/v1"
    model = os.getenv("LLM_MODEL") or "gpt-4o-mini"

    if not api_key:
        print("  [AI] 无 API key，使用关键词启发式评分")
        return _keyword_fallback(urls_batch)

    prompt = build_ai_prompt(urls_batch)

    try:
        resp = req.post(
            f"{api_base}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": "你是一个加密货币投研链接筛选助手。只返回 JSON，不要有其他内容。"},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,
                "max_tokens": 2000,
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"].strip()

        # 提取 JSON
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()

        result = json.loads(content)
        return result.get("results", [])

    except Exception as e:
        print(f"  [AI] 调用失败: {e}，使用关键词 fallback")
        return _keyword_fallback(urls_batch)


# ── 关键词启发式评分 (AI API 不可用时的 fallback) ──
HIGH_RELEVANCE_KEYWORDS = [
    "whitepaper", "litepaper", "lightpaper", "yellowpaper",
    "white-paper", "lite-paper", "白皮书",
    "tokenomics", "token-economics", "代币经济",
    "audit", "审计",
    "technical-paper", "technical_paper",
    "economic-paper",
]

MEDIUM_RELEVANCE_KEYWORDS = [
    "/docs/", "documentation", "文档",
    "roadmap", "路线图",
    "governance", "治理",
    "lightpaper",
    "deck",
    "report",
]

LOW_RELEVANCE_KEYWORDS = [
    "blog", "medium.com", "mirror.xyz",
    "github.com", "gitlab.com",
]

IRRELEVANT_DOMAINS = {
    "twitter.com", "x.com", "t.me", "discord.gg", "discord.com",
    "reddit.com", "facebook.com", "instagram.com", "youtube.com",
    "linkedin.com", "coinmarketcap.com", "coingecko.com",
    "dexscreener.com", "birdeye.so", "dextools.io",
}


def _keyword_fallback(urls_batch: list[dict]) -> list[dict]:
    """关键词启发式评分"""
    results = []
    for i, u in enumerate(urls_batch, 1):
        url = (u.get("url") or "").lower()
        domain = urlparse(u.get("url", "")).netloc.lower()

        score = 0.0
        category = "other"
        reason = ""

        # 域名黑名单
        if domain in IRRELEVANT_DOMAINS or any(d in domain for d in IRRELEVANT_DOMAINS):
            score = 0.0
            reason = "社交媒体/行情网站"
            category = "none"
        # PDF 高优先级
        elif url.endswith(".pdf"):
            score = 0.9
            reason = "PDF文档"
            category = "whitepaper" if "whitepaper" in url or "白皮书" in url else "docs"
        else:
            # 高分关键词
            for kw in HIGH_RELEVANCE_KEYWORDS:
                if kw in url:
                    score = max(score, 0.9)
                    if "whitepaper" in kw or "paper" in kw:
                        category = "whitepaper"
                    elif "audit" in kw:
                        category = "audit"
                    elif "tokenomics" in kw or "代币" in kw:
                        category = "tokenomics"
                    reason = kw
                    break

            if score < 0.5:
                for kw in MEDIUM_RELEVANCE_KEYWORDS:
                    if kw in url:
                        score = max(score, 0.7)
                        if "/docs/" in kw or "documentation" in kw or "文档" in kw:
                            category = "docs"
                        elif "roadmap" in kw:
                            category = "other"
                        elif "governance" in kw:
                            category = "docs"
                        reason = kw
                        break

            if score < 0.5:
                for kw in LOW_RELEVANCE_KEYWORDS:
                    if kw in url:
                        score = max(score, 0.5)
                        if "github" in kw:
                            category = "github"
                        elif "blog" in kw or "medium" in kw or "mirror" in kw:
                            category = "blog"
                        reason = kw
                        break

            if score < 0.3:
                # 检查是否是官网首页或文档站
                if u.get("doc_type") in ("docs", "official_website"):
                    score = 0.4
                    reason = "官方入口"
                    category = u.get("doc_type", "other")
                else:
                    score = 0.15
                    reason = "通用链接"
                    category = "other"

        reason = reason[:15]
        results.append({
            "index": i,
            "relevance_score": min(score, 1.0),
            "reason": reason,
            "category": category,
        })

    return results


def run_ai_filter(urls: list[dict], batch_size: int = 30) -> list[dict]:
    """批量 AI 筛选，顺便 merge 回健康检查结果"""
    total = len(urls)
    results: list[dict] = []
    print(f"AI 筛选: {total} 个链接, batch_size={batch_size}")

    for batch_start in range(0, total, batch_size):
        batch = urls[batch_start:batch_start + batch_size]
        ai_results = call_ai_batch(batch)

        if not ai_results:
            # AI 完全失败，全用 fallback
            ai_results = _keyword_fallback(batch)

        # Merge
        ai_map = {r["index"]: r for r in ai_results}
        for i, u in enumerate(batch, 1):
            ai = ai_map.get(i, {})
            merged = {**u}
            merged["relevance_score"] = ai.get("relevance_score", 0.0)
            merged["ai_reason"] = ai.get("reason", "")[:50]
            merged["category"] = ai.get("category", "other")
            results.append(merged)

        done = min(batch_start + batch_size, total)
        high = sum(1 for r in results[batch_start:done] if r.get("relevance_score", 0) >= 0.6)
        print(f"  [{done}/{total}] 高相关(>=0.6): {high}/{done - batch_start}")

    return results


# ── 步骤 4: 写入 DB ──
def ensure_table(conn):
    """确保 biz.research_url 表存在"""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS biz.research_url (
                url_id BIGSERIAL PRIMARY KEY,
                asset_id BIGINT NOT NULL,
                coin_symbol TEXT,
                coin_name TEXT,
                url TEXT NOT NULL,
                url_source TEXT NOT NULL,
                doc_type TEXT,
                category TEXT,
                health_status TEXT NOT NULL DEFAULT 'unchecked',
                http_status INT,
                final_url TEXT,
                relevance_score REAL DEFAULT 0.0,
                ai_reason TEXT,
                file_name TEXT,
                mime_type TEXT,
                source_code TEXT,
                is_selected BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (asset_id, url)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_research_url_asset
                ON biz.research_url(asset_id)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_research_url_health
                ON biz.research_url(health_status)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_research_url_relevance
                ON biz.research_url(relevance_score DESC)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_research_url_selected
                ON biz.research_url(asset_id, is_selected)
                WHERE is_selected = TRUE
        """)
    conn.commit()


def upsert_results(conn, results: list[dict]):
    """批量 upsert 到 biz.research_url"""
    with conn.cursor() as cur:
        for r in results:
            cur.execute("""
                INSERT INTO biz.research_url (
                    asset_id, coin_symbol, coin_name, url, url_source,
                    doc_type, category, health_status, http_status,
                    final_url, relevance_score, ai_reason,
                    file_name, mime_type, source_code
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (asset_id, url) DO UPDATE SET
                    health_status = EXCLUDED.health_status,
                    http_status = EXCLUDED.http_status,
                    final_url = EXCLUDED.final_url,
                    relevance_score = EXCLUDED.relevance_score,
                    ai_reason = EXCLUDED.ai_reason,
                    category = EXCLUDED.category,
                    updated_at = NOW()
            """, (
                r["asset_id"], r.get("coin_symbol"), r.get("coin_name"),
                r["url"], r.get("url_source", ""),
                r.get("doc_type"), r.get("category", "other"),
                r.get("health_status", "unchecked"), r.get("http_status"),
                r.get("final_url"), r.get("relevance_score", 0.0),
                r.get("ai_reason", ""),
                r.get("file_name"), r.get("mime_type"),
                r.get("source_code"),
            ))


def mark_selected_urls(conn, asset_id: int = 0, max_per_asset: int = 40):
    """
    为每个 asset 标记最适合导入 NotebookLM 的链接。
    规则：
    - healthy + relevance_score >= 0.5 的链接，按分数降序选最多 max_per_asset 个
    """
    where = "WHERE asset_id = %s" if asset_id else ""
    params = (asset_id, max_per_asset) if asset_id else (max_per_asset,)

    with conn.cursor() as cur:
        cur.execute("UPDATE biz.research_url SET is_selected = FALSE")
        cur.execute(f"""
            UPDATE biz.research_url ru SET is_selected = TRUE
            FROM (
                SELECT url_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY asset_id
                           ORDER BY relevance_score DESC, health_status ASC
                       ) AS rn
                FROM biz.research_url
                WHERE health_status = 'healthy'
                  AND relevance_score >= 0.5
                  {where.replace('asset_id', 'ru.asset_id') if asset_id else ''}
            ) ranked
            WHERE ru.url_id = ranked.url_id AND ranked.rn <= %s
        """, params if asset_id else (max_per_asset,))
    conn.commit()


# ── 主流程 ──
def main() -> int:
    args = build_parser().parse_args()

    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    settings = get_settings(require_database=True)

    with get_connection(settings.database_url) as conn:
        # 确保表存在
        ensure_table(conn)

        # 1. 收集候选链接
        print(f"\n=== Step 1: 收集候选链接 (limit={args.limit}) ===")
        urls = collect_candidate_urls(conn, args.limit, args.asset_id)
        print(f"收集到 {len(urls)} 个候选链接")
        if not urls:
            print("无候选链接，退出")
            return 0

    if args.dry_run:
        print("\n[Dry-run] 示例链接:")
        for u in urls[:10]:
            print(f"  [{u.get('coin_symbol', '?')}] {u.get('url', '')[:100]} "
                  f"type={u.get('doc_type', '')} source={u.get('url_source', '')}")
        return 0

    # 2. 健康检查
    if not args.skip_health:
        print(f"\n=== Step 2: 健康检查 ===")
        urls = run_health_check(urls)
    else:
        for u in urls:
            u["health_status"] = "unchecked"
            u["http_status"] = None
            u["final_url"] = u.get("url", "")

    # 3. AI 筛选
    if not args.skip_ai:
        print(f"\n=== Step 3: AI 投研相关性筛选 ===")
        urls = run_ai_filter(urls, args.ai_batch_size)
    else:
        for u in urls:
            u["relevance_score"] = 0.0
            u["ai_reason"] = ""
            u["category"] = "other"

    # 4. 写入 DB
    print(f"\n=== Step 4: 写入 biz.research_url ===")
    with get_connection(settings.database_url) as conn:
        upsert_results(conn, urls)
        mark_selected_urls(conn, args.asset_id)

    # 5. 统计
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT health_status, COUNT(*) as cnt
                FROM biz.research_url
                GROUP BY health_status
                ORDER BY cnt DESC
            """)
            health_stats = cur.fetchall()

            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE relevance_score >= 0.8) as high,
                    COUNT(*) FILTER (WHERE relevance_score >= 0.5 AND relevance_score < 0.8) as medium,
                    COUNT(*) FILTER (WHERE relevance_score > 0 AND relevance_score < 0.5) as low,
                    COUNT(*) FILTER (WHERE relevance_score = 0) as none
                FROM biz.research_url
            """)
            rel_stats = cur.fetchone()

            cur.execute("SELECT COUNT(*) FROM biz.research_url WHERE is_selected = TRUE")
            selected = cur.fetchone()[0]

    print(f"\n=== 完成 ===")
    print(f"健康状态: {[(r[0], r[1]) for r in health_stats]}")
    print(f"相关性: 高={rel_stats[0]} 中={rel_stats[1]} 低={rel_stats[2]} 无={rel_stats[3]}")
    print(f"已入选: {selected} 条（将用于生成投研网址链接文件）")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
