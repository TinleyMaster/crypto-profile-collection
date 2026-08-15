"""
Phase B3: 无头浏览器爬取 SPA 页面
处理 needs_browser=TRUE 的 entry，使用 Playwright 渲染 JavaScript 后提取文档链接。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(line_buffering=True)

# 复用 B2 的 extract_doc_links（包含所有过滤、密度控制逻辑）
from phase_b2_deep_doc_discovery import extract_doc_links

# 有效的 entry_type
VALID_ENTRY_TYPES = {
    "official_website", "docs", "github", "medium",
    "docs_portal", "whitepaper_page", "twitter", "telegram", "other", "reddit",
}

# 并发数（浏览器资源有限）
DEFAULT_CONCURRENCY = 4
# 页面加载超时
PAGE_TIMEOUT_MS = 20000
# HEAD 预检超时
HEAD_TIMEOUT_S = 8
# 浏览器启动 + 整批处理超时（秒）
BROWSER_LAUNCH_TIMEOUT = 30
BATCH_TIMEOUT = 300
# 死链接标记文件（用于跨轮记忆）
DEAD_CHECK_FILE = SCRIPT_DIR / ".." / ".." / "task_state" / "spa_dead_urls.json"


def build_project_identifiers(symbol: str, name: str, entry_url: str) -> list[str]:
    """构建项目标识符，与 B2 crawl_one 逻辑一致。"""
    identifiers: list[str] = []
    if symbol and len(symbol) >= 2:
        identifiers.append(symbol)
    if name:
        for word in name.replace("-", " ").replace(".", " ").split():
            word = word.strip()
            if len(word) >= 2 and word.lower() not in (
                "token", "coin", "dao", "protocol", "network", "chain", "finance", "swap", "defi",
            ):
                identifiers.append(word)
    domain = urlparse(entry_url).netloc.lower()
    domain_name = domain.split(".")[0]
    if len(domain_name) >= 2 and domain_name not in ("www", "docs", "blog", "app", "api"):
        identifiers.append(domain_name)
    return identifiers


def preflight_check(url: str) -> str | None:
    """HEAD 预检：返回 None 表示可以继续浏览器渲染，返回 reason 字符串表示应跳过。"""
    old_timeout = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(HEAD_TIMEOUT_S)
        resp = requests.head(url, timeout=HEAD_TIMEOUT_S, allow_redirects=True,
                             headers={"User-Agent": "Mozilla/5.0"})
        content_type = resp.headers.get("Content-Type", "").lower()
        if "text/html" in content_type:
            return None  # HTML，可以继续
        if any(t in content_type for t in ("pdf", "image/", "application/zip", "application/gzip",
                                            "application/octet-stream", "video/", "audio/")):
            return f"非HTML内容: {content_type}"
        # 其他类型也跳过（json, xml, etc.）
        return f"非HTML内容: {content_type}"
    except requests.exceptions.Timeout:
        return "HEAD 超时"
    except requests.exceptions.ConnectionError:
        return "连接失败"
    except Exception as e:
        return f"HEAD 错误: {str(e)[:60]}"
    finally:
        socket.setdefaulttimeout(old_timeout)


def crawl_one_spa(browser, entry: dict, same_domain_only: bool) -> dict:
    """用 Playwright 渲染一个 SPA 页面，提取文档链接（同步版）。"""
    entry_id = entry["entry_id"]
    entry_url = entry["entry_url"]
    symbol = entry["canonical_symbol"] or ""
    name = entry["canonical_name"] or ""

    project_identifiers = build_project_identifiers(symbol, name, entry_url)

    # 0. URL 后缀预检：PDF 等直接跳过，避免 Playwright 触发 Download 错误
    url_lower = entry_url.lower().split("?")[0]
    if url_lower.endswith((".pdf", ".zip", ".gz", ".tar", ".jpg", ".jpeg", ".png", ".svg", ".mp4", ".mp3")):
        return {
            "status": "skipped",
            "entry_id": entry_id,
            "url": entry_url,
            "reason": f"非HTML文件: {url_lower.rsplit('.', 1)[-1]}",
        }

    # 1. HEAD 预检：跳过非 HTML 内容
    skip_reason = preflight_check(entry_url)
    if skip_reason is not None:
        return {
            "status": "skipped",
            "entry_id": entry_id,
            "url": entry_url,
            "reason": skip_reason,
        }

    # 2. 浏览器渲染
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 800},
    )
    try:
        page = context.new_page()
        page.route("**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,eot,css}", lambda route: route.abort())

        # SPA 页面先尝试 domcontentloaded，超时则降级为 commit（至少拿到部分内容）
        try:
            page.goto(entry_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        except Exception:
            page.goto(entry_url, wait_until="commit", timeout=PAGE_TIMEOUT_MS)
        page.wait_for_timeout(1500)

        html = page.content()
        final_url = page.url

        doc_links = extract_doc_links(html, final_url, same_domain_only, project_identifiers, require_doc_keyword=False, skip_aggregation_filter=True)

        return {
            "status": "ok",
            "entry_id": entry_id,
            "url": entry_url,
            "final_url": final_url,
            "doc_links": doc_links,
            "entity_type": entry["entity_type"],
            "asset_id": entry["asset_id"],
            "protocol_id": entry["protocol_id"],
            "source_code": entry["source_code"],
        }
    except Exception as e:
        err_msg = str(e)
        # Playwright 遇到 PDF 下载会报 "Download is starting"，视为 SKIP
        if "Download" in err_msg or "download" in err_msg:
            return {
                "status": "skipped",
                "entry_id": entry_id,
                "url": entry_url,
                "reason": f"触发下载: {err_msg[:80]}",
            }
        return {
            "status": "failed",
            "entry_id": entry_id,
            "url": entry_url,
            "error": str(e)[:120],
        }
    finally:
        context.close()


def run_batch(entries: list[dict], concurrency: int, same_domain_only: bool) -> dict:
    """爬取一批 SPA 页面（串行，Playwright sync API 绑定创建线程）。"""

    stats = {"done": 0, "failed": 0, "discovered": 0, "empty": 0, "skipped": 0}
    db_rows: list[tuple] = []
    done_ids: list[int] = []
    failed_ids: list[int] = []
    entry_ids = [e["entry_id"] for e in entries]

    # 0. 递增重试计数（标记本轮尝试）
    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection
    from crypto_research.mapping.classify_link import classify_entry_fields
    try:
        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE biz.doc_source_entry SET spa_retry_count = COALESCE(spa_retry_count, 0) + 1 WHERE entry_id = ANY(%s)",
                    (entry_ids,),
                )
            conn.commit()
    except Exception as e:
        print(f"  [WARN] 递增重试计数失败: {e}", flush=True)

    # 1. 浏览器启动
    print(f"  [SPA] 启动 Playwright...", flush=True)
    from playwright.sync_api import sync_playwright

    try:
        playwright = sync_playwright().start()
        print(f"  [SPA] Playwright 已启动, 启动 Chromium...", flush=True)
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
    except Exception as e:
        print(f"  [ERROR] 浏览器启动失败: {e}")
        return {"stats": stats, "db_rows": [], "done_ids": [], "failed_ids": [e["entry_id"] for e in entries]}

    print(f"  [SPA] Chromium 已启动，开始爬取 {len(entries)} 个 SPA 页面...", flush=True)

    # 串行爬取（Playwright sync API 的 browser 对象绑定创建线程，不能跨线程）
    for i, entry in enumerate(entries):
        idx = i + 1
        url_short = entry["entry_url"][:80]
        print(f"  [{idx}/{len(entries)}] 爬取: {url_short} ...", flush=True)
        result = crawl_one_spa(browser, entry, same_domain_only)
        
        if result["status"] == "skipped":
            stats["skipped"] += 1
            done_ids.append(result["entry_id"])
            print(f"  [{idx}/{len(entries)}] SKIP  {url_short}  {result.get('reason','')}", flush=True)
        elif result["status"] == "ok":
            stats["done"] += 1
            done_ids.append(result["entry_id"])
            doc_links = result["doc_links"]
            if doc_links:
                stats["discovered"] += len(doc_links)
                for link_url, link_type in doc_links:
                    topics, method, confidence = classify_entry_fields(
                        link_url, source_code=result["source_code"]
                    )
                    db_rows.append((
                        result["entity_type"],
                        result["asset_id"],
                        result["protocol_id"],
                        result["source_code"],
                        link_type if link_type in VALID_ENTRY_TYPES else "other",
                        link_url,
                        f"spa_browser_crawl:{result['url'][:43]}",
                        False,
                        topics,
                        method,
                        confidence,
                    ))
            else:
                stats["empty"] += 1
            print(f"  [{idx}/{len(entries)}] OK  {url_short}  +{len(result['doc_links'])} links", flush=True)
        else:
            stats["failed"] += 1
            failed_ids.append(result["entry_id"])
            print(f"  [{idx}/{len(entries)}] FAIL  {url_short}  {result.get('error','')}", flush=True)

    browser.close()
    playwright.stop()

    return {"stats": stats, "db_rows": db_rows, "done_ids": done_ids, "failed_ids": failed_ids}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Phase B3: Playwright 无头浏览器爬取 SPA 页面")
    p.add_argument("--dry-run", action="store_true", help="预览不写入。")
    p.add_argument("--limit", type=int, default=20, help="每批最大处理数。")
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY, help="并发浏览器窗口数。")
    p.add_argument("--all-domains", action="store_true", help="不限制同域。")
    p.add_argument("--asset-id", type=int, default=None, help="仅处理指定资产ID。")
    return p


def main() -> int:
    args = build_parser().parse_args()
    print(f"[SPA crawl] 启动, limit={args.limit}, concurrency={args.concurrency}", flush=True)

    print("[SPA crawl] import psycopg...", flush=True)
    import psycopg
    print("[SPA crawl] import config...", flush=True)
    from crypto_research.config import get_settings
    print("[SPA crawl] import conn...", flush=True)
    from crypto_research.db.conn import get_connection
    print("[SPA crawl] import upsert...", flush=True)
    from crypto_research.db.upsert import fetch_one, load_sql

    print("[SPA crawl] 加载配置...", flush=True)
    settings = get_settings(require_database=True)
    upsert_sql = load_sql("biz/upsert_doc_source_entry.sql")
    print(f"[SPA crawl] 配置加载完成, 连接数据库...", flush=True)

    # 查询 needs_browser = TRUE 的条目
    asset_filter = ""
    asset_params: list = []
    if args.asset_id is not None:
        asset_filter = " AND dse.asset_id = %s"
        asset_params = [args.asset_id]
    with get_connection(settings.database_url) as conn:
        print("[SPA crawl] DB 已连接, 查询 SPA 页面...", flush=True)
        try:
            with conn.cursor() as cur:
                # 确保列存在
                cur.execute("""
                    ALTER TABLE biz.doc_source_entry ADD COLUMN IF NOT EXISTS spa_crawled_at TIMESTAMPTZ
                """)
                cur.execute("""
                    ALTER TABLE biz.doc_source_entry ADD COLUMN IF NOT EXISTS spa_retry_count INTEGER DEFAULT 0
                """)
                # 确保 needs_browser 索引存在（partial index，极快）
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_dse_needs_browser
                    ON biz.doc_source_entry (entry_id)
                    WHERE needs_browser = TRUE
                """)
            conn.commit()

            # 先自动清理：spa_retry_count >= 3 的条目直接跳过，不再重试
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute("""
                    UPDATE biz.doc_source_entry
                    SET needs_browser = FALSE, spa_crawled_at = NOW()
                    WHERE needs_browser = TRUE AND spa_retry_count >= 3
                """)
                auto_skipped = cur.rowcount
                if auto_skipped:
                    conn.commit()
                    print(f"[SPA crawl] 自动跳过 {auto_skipped} 条重试超限的 SPA 页面", flush=True)

            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute("SET statement_timeout = '30s'")
                # 按重试次数升序 + entry_id 升序：从未重试过的优先，避免被顽固条目卡死
                cur.execute(
                    f"""
                    SELECT dse.entry_id, dse.entity_type, dse.asset_id, dse.protocol_id,
                           dse.source_code, dse.entry_type, dse.entry_url,
                           a.canonical_symbol, a.canonical_name,
                           COALESCE(dse.spa_retry_count, 0) AS retries
                    FROM biz.doc_source_entry dse
                    LEFT JOIN core.asset a ON dse.asset_id = a.asset_id
                    WHERE dse.needs_browser = TRUE{asset_filter}
                    ORDER BY COALESCE(dse.spa_retry_count, 0) ASC, dse.entry_id
                    LIMIT %s
                    """,
                    asset_params + [args.limit],
                )
                entries = [dict(row) for row in cur.fetchall()]
        except Exception as e:
            print(f"[SPA crawl] DB 查询失败: {e}", flush=True)
            import traceback
            traceback.print_exc()
            return 2

    print(f"[SPA crawl] 查询完成: {len(entries)} 条 SPA 页面", flush=True)

    if not entries:
        print(json.dumps({"status": "no_candidates"}, ensure_ascii=False))
        return 0

    print(f"待处理 SPA 页面: {len(entries)}, 并发: {args.concurrency}", flush=True)

    start_time = time.time()
    same_domain_only = not args.all_domains

    print(f"[SPA crawl] 开始爬取...", flush=True)
    result = run_batch(entries, args.concurrency, same_domain_only)

    stats = result["stats"]
    elapsed = time.time() - start_time

    print(f"\n完成: done={stats['done']} skipped={stats['skipped']} failed={stats['failed']} +{stats['discovered']} docs empty={stats['empty']} | {elapsed:.1f}s")

    if args.dry_run:
        preview = result["db_rows"][:5] if result["db_rows"] else []
        print(json.dumps({
            "mode": "dry_run",
            "candidates": len(entries),
            "done": stats["done"],
            "skipped": stats["skipped"],
            "failed": stats["failed"],
            "discovered": stats["discovered"],
            "empty": stats["empty"],
            "elapsed_sec": round(elapsed, 1),
            "first_entry": preview[0] if preview else None,
        }, ensure_ascii=False))
        return 0

    # 写入数据库
    with get_connection(settings.database_url) as conn:
        written = 0
        write_errors = 0
        with conn.cursor() as cur:
            for row in result["db_rows"]:
                try:
                    cur.execute(upsert_sql, row)
                    written += 1
                except Exception as e:
                    write_errors += 1
                    conn.rollback()  # 重置事务状态，防止后续 SQL 被拒
                    print(f"  [WARN] 写入失败: {row[5][:80]}  {str(e)[:100]}")
        if write_errors:
            print(f"  写入: {written} 成功, {write_errors} 失败")

        # 清除成功处理的条目标记（failed 保留 needs_browser=TRUE 下轮重试）
        cleared = 0
        if result["done_ids"]:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE biz.doc_source_entry SET needs_browser = FALSE, deep_crawled_at = NOW(), spa_crawled_at = NOW() WHERE entry_id = ANY(%s)",
                        (result["done_ids"],),
                    )
                cleared = cur.rowcount
                conn.commit()
                print(f"  [CLEAR] 已清除 {cleared} 条 needs_browser 标记", flush=True)
            except Exception as e:
                print(f"  [CLEAR ERROR] 清除标记失败: {e}", flush=True)
                conn.rollback()
        else:
            print(f"  [CLEAR] done_ids 为空，无条目标记清除", flush=True)

    print(f"写入: {written} 条目, 清除标记: {cleared}（{len(result['failed_ids'])} 失败保留重试，已重试次数见 spa_retry_count）")
    print(json.dumps({
        "status": "complete",
        "candidates": len(entries),
        "done": stats["done"],
        "skipped": stats["skipped"],
        "failed": stats["failed"],
        "discovered": stats["discovered"],
        "empty": stats["empty"],
        "written": written,
        "elapsed_sec": round(elapsed, 1),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())