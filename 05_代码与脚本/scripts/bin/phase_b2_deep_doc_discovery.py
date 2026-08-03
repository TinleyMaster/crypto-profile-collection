"""
Phase B2: 第二版文档发现 (多线程并发版)
从 doc_source_entry 的网页入口深入分析 HTML，发现嵌入的文档链接(PDF/白皮书/Tokenomics 等)。
使用 ThreadPoolExecutor 并行爬取，大幅提升速度。
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

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

# ── 常量（与旧版一致） ──
DOC_URL_KEYWORDS = [
    "whitepaper",
    "litepaper",
    "lightpaper",
    "yellowpaper",
    "tokenomics",
    "audit",
    "deck",
    "paper",
    ".pdf",
    "/docs/",
    "documentation",
    "report",
    "technical-paper",
    "white-paper",
    "lite-paper",
]
ENTRY_TYPES_TO_CRAWL = {"docs", "official_website"}
EXCLUDE_PATH_PATTERNS = [
    "Special:",
    "UserLogin",
    "User:",
    "Talk:",
    "Help:",
    "File:",
    "Template:",
    "Category:",
    "/index.php?title=Special:",
    "action=edit",
    "action=history",
    "/login",
    "/signup",
    "/signin",
    "/register",
    "cdn-cgi/l/email-protection",
    "/_history",
    "privacy-policy",
    "/services",
    "/issues",
    "/pulls",
    "/actions",
    "/projects",
    "/security",
    "/watchers",
    "/stargazers",
    "/network",
    "/labels",
    "/milestones",
    "/settings",
    "/notifications",
    "/new",
    "/compare",
    "/releases/tag",
    "template=bug_report",
    "template=feature_request",
]
EXCLUDE_DOMAINS = {"docs.github.com", "support.scribd.com", "twitter.com", "x.com"}
EXCLUDE_PATH_EXACT = {"/resources/whitepapers"}
NOISY_DOC_DOMAINS = {"whitepaper.io", "docs.eth"}

# ── Worker 共享的全局变量 ──
_worker_settings: dict = {}  # database_url, upsert_sql
_stats_lock = threading.Lock()
_stats = {"done": 0, "failed": 0, "not_html": 0, "empty": 0, "discovered": 0}
_pending_db_rows: list[tuple] = []
_pending_crawled_ids: list[int] = []
_db_lock = threading.Lock()
_start_time: float = 0
_total: int = 0


def _has_doc_keyword(url: str) -> bool:
    lowered = url.lower()
    return any(kw in lowered for kw in DOC_URL_KEYWORDS)


def _doc_keyword_in_path_only(url: str) -> bool:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    path_and_query = (
        parsed.path + ("?" + parsed.query if parsed.query else "")
    ).lower()
    return any(kw in path_and_query for kw in DOC_URL_KEYWORDS)


def infer_doc_entry_type(url: str) -> str:
    lowered = url.lower()
    if any(
        kw in lowered
        for kw in [
            "whitepaper",
            "litepaper",
            "lightpaper",
            "yellowpaper",
            "white-paper",
            "lite-paper",
            "paper",
        ]
    ):
        return "docs"
    if "tokenomics" in lowered:
        return "docs"
    if "audit" in lowered:
        return "docs"
    if "/docs/" in lowered or "documentation" in lowered:
        return "docs_portal"
    if "github.com" in lowered:
        return "github"
    return "docs"


def _is_excluded_url(url: str) -> bool:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    path = parsed.path.lower()
    query = (parsed.query or "").lower()
    full_path = f"{path}?{query}" if query else path
    if domain in EXCLUDE_DOMAINS:
        return True
    if path.rstrip("/") in EXCLUDE_PATH_EXACT:
        return True
    for pattern in EXCLUDE_PATH_PATTERNS:
        if pattern.lower() in full_path or pattern.lower() in path:
            return True
    return False


def extract_doc_links(
    html: str, base_url: str, same_domain_only: bool = True
) -> list[tuple[str, str]]:
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin, urlparse

    soup = BeautifulSoup(html, "html.parser")
    base_domain = urlparse(base_url).netloc.lower()
    results: list[tuple[str, str]] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if (
            not href
            or href.startswith("#")
            or href.startswith("javascript:")
            or href.startswith("mailto:")
        ):
            continue
        absolute_url = urljoin(base_url, href)
        parsed = urlparse(absolute_url)
        if parsed.scheme not in ("http", "https"):
            continue
        link_domain = parsed.netloc.lower()
        if _is_excluded_url(absolute_url):
            continue

        should_record = False
        link_text = a.get_text(strip=True)
        link_text_is_doc = _has_doc_keyword(link_text)

        if link_domain in NOISY_DOC_DOMAINS:
            if _doc_keyword_in_path_only(absolute_url) or link_text_is_doc:
                should_record = True
        elif _has_doc_keyword(absolute_url):
            should_record = True
        elif link_text_is_doc:
            if same_domain_only:
                if link_domain == base_domain:
                    should_record = True
            else:
                should_record = True

        if not should_record:
            continue
        if len(absolute_url) > 500:
            continue

        normalized = absolute_url.rstrip("/")
        if normalized not in seen:
            seen.add(normalized)
            results.append((absolute_url, infer_doc_entry_type(absolute_url)))

    return results


def _make_session():
    import requests

    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return s


def crawl_one(entry: dict, same_domain_only: bool, timeout: int) -> dict:
    """单个 worker：爬一个网页（带外层超时兜底，防止 SSL 握手卡死）"""
    from concurrent.futures import ThreadPoolExecutor as InnerPool

    entry_url = entry["entry_url"]
    entry_id = entry["entry_id"]

    def _do_fetch():
        import requests

        session = _make_session()
        try:
            resp = session.get(entry_url, timeout=(3, timeout), allow_redirects=True)
            resp.raise_for_status()
            content_type = (resp.headers.get("Content-Type") or "").lower()
            if "text/html" not in content_type and "text/plain" not in content_type:
                return {"status": "not_html", "entry_id": entry_id, "url": entry_url}

            doc_links = extract_doc_links(resp.text, resp.url, same_domain_only)
            return {
                "status": "ok",
                "entry_id": entry_id,
                "url": entry_url,
                "doc_links": doc_links,
                "entity_type": entry["entity_type"],
                "asset_id": entry["asset_id"],
                "protocol_id": entry["protocol_id"],
                "source_code": entry["source_code"],
            }
        except Exception as e:
            return {
                "status": "failed",
                "entry_id": entry_id,
                "url": entry_url,
                "error": str(e)[:100],
            }

    # 外层超时兜底：即使 requests 内部卡死，也保证在 timeout+8 秒内返回
    with InnerPool(max_workers=1) as pool:
        fut = pool.submit(_do_fetch)
        try:
            return fut.result(timeout=timeout + 8)
        except Exception:
            return {
                "status": "failed",
                "entry_id": entry_id,
                "url": entry_url,
                "error": "overall_timeout",
            }


def _flush_db() -> None:
    """将累积的 DB 写入一次性提交"""
    global _pending_db_rows, _pending_crawled_ids
    if not _pending_db_rows and not _pending_crawled_ids:
        return

    from crypto_research.db.conn import get_connection
    from crypto_research.db.upsert import execute_many, load_sql

    db_url = _worker_settings["database_url"]
    upsert_sql = _worker_settings.get("upsert_sql") or load_sql(
        "biz/upsert_doc_source_entry.sql"
    )

    with get_connection(db_url) as conn:
        if _pending_db_rows:
            execute_many(conn, upsert_sql, _pending_db_rows)
        if _pending_crawled_ids:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE biz.doc_source_entry SET deep_crawled_at = NOW() WHERE entry_id = ANY(%s)",
                    (_pending_crawled_ids,),
                )
    _pending_db_rows.clear()
    _pending_crawled_ids.clear()


def _print_progress():
    with _stats_lock:
        done = _stats["done"]
        failed = _stats["failed"]
        total = _total
        discovered = _stats["discovered"]
        elapsed = time.time() - _start_time if _start_time else 0
        rate = (done + failed) / elapsed if elapsed > 0 else 0
        eta = (total - done - failed) / rate if rate > 0 else 0
        pct = (done + failed) / total * 100 if total else 0
        print(
            f"[{done + failed}/{total} {pct:.0f}%] "
            f"OK:{done} FAIL:{failed} +{discovered} docs "
            f"| {rate:.1f}/s ETA:{eta:.0f}s",
            flush=True,
        )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Phase B2: 并发版文档发现")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=500, help="最大处理数量")
    p.add_argument("--workers", type=int, default=20, help="并发线程数")
    p.add_argument("--timeout", type=int, default=10, help="读取超时(秒)")
    p.add_argument("--flush-every", type=int, default=50, help="每 N 条 flush 一次 DB")
    p.add_argument("--all-domains", action="store_true")
    return p


def main() -> int:
    global _start_time, _total
    args = build_parser().parse_args()

    import psycopg
    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection
    from crypto_research.db.upsert import load_sql

    settings = get_settings(require_database=True)
    _worker_settings["database_url"] = settings.database_url
    _worker_settings["upsert_sql"] = load_sql("biz/upsert_doc_source_entry.sql")

    # 保证列存在
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE biz.doc_source_entry ADD COLUMN IF NOT EXISTS deep_crawled_at TIMESTAMPTZ"
            )

    # 查询待处理
    with get_connection(settings.database_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """
                SELECT entry_id, entity_type, asset_id, protocol_id, source_code,
                       entry_type, entry_url
                FROM biz.doc_source_entry
                WHERE entry_type = ANY(%s) AND deep_crawled_at IS NULL
                ORDER BY CASE entry_type WHEN 'official_website' THEN 1 WHEN 'docs' THEN 2 ELSE 3 END, entry_id
                LIMIT %s
                """,
                (list(ENTRY_TYPES_TO_CRAWL), args.limit),
            )
            entries = [dict(row) for row in cur.fetchall()]

    if not entries:
        print(json.dumps({"status": "no_candidates"}, ensure_ascii=False))
        return 0

    _total = len(entries)
    docs_count = sum(1 for e in entries if e["entry_type"] == "docs")
    website_count = _total - docs_count
    print(
        f"待处理: {_total} (docs: {docs_count}, official_website: {website_count}), {args.workers} workers"
    )
    print()

    _start_time = time.time()

    if args.dry_run:
        preview = []
        for entry in entries[:10]:
            result = crawl_one(entry, not args.all_domains, args.timeout)
            preview.append(
                {
                    "url": entry["entry_url"][:120],
                    "status": result["status"],
                    "docs_found": len(result.get("doc_links", [])),
                }
            )
        print(
            json.dumps(
                {"mode": "dry_run", "preview": preview}, ensure_ascii=False, indent=2
            )
        )
        return 0

    # ── 并发爬取 ──
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(crawl_one, entry, not args.all_domains, args.timeout): entry
            for entry in entries
        }

        for future in as_completed(futures):
            result = future.result()
            entry_id = result["entry_id"]

            if result["status"] == "ok":
                doc_links = result["doc_links"]
                with _stats_lock:
                    _stats["done"] += 1
                    if doc_links:
                        _stats["discovered"] += len(doc_links)
                    else:
                        _stats["empty"] += 1

                with _db_lock:
                    _pending_crawled_ids.append(entry_id)
                    if doc_links:
                        for link_url, link_type in doc_links:
                            _pending_db_rows.append(
                                (
                                    result["entity_type"],
                                    result["asset_id"],
                                    result["protocol_id"],
                                    result["source_code"],
                                    link_type,
                                    link_url,
                                    f"deep_crawl:{result['url'][:50]}",
                                    False,
                                )
                            )

            elif result["status"] == "not_html":
                with _stats_lock:
                    _stats["done"] += 1
                    _stats["not_html"] += 1
                with _db_lock:
                    _pending_crawled_ids.append(entry_id)

            elif result["status"] == "failed":
                with _stats_lock:
                    _stats["failed"] += 1
                with _db_lock:
                    _pending_crawled_ids.append(entry_id)

            # 每 N 条 flush
            with _db_lock:
                if len(_pending_crawled_ids) >= args.flush_every:
                    _flush_db()

            _print_progress()

    # 最终 flush
    _flush_db()
    print()  # 换行

    elapsed = time.time() - _start_time
    print(
        json.dumps(
            {
                "status": "complete",
                "candidates": _total,
                "done": _stats["done"],
                "failed": _stats["failed"],
                "not_html": _stats["not_html"],
                "empty": _stats["empty"],
                "discovered": _stats["discovered"],
                "elapsed_sec": round(elapsed, 1),
                "rate": round((_stats["done"] + _stats["failed"]) / elapsed, 1)
                if elapsed
                else 0,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
