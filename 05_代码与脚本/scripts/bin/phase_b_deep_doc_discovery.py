"""
Phase B: 第二版文档发现
从 doc_source_entry 的网页入口深入分析 HTML，发现嵌入的文档链接(PDF/白皮书/Tokenomics 等)。
第一版只发现了直链 PDF（<10 条），第二版通过爬取网页内容来发现更多文档入口。
"""

from __future__ import annotations

import argparse
import json
import sys
import io
from pathlib import Path
from urllib.parse import urljoin

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

# 在 <a href> 中匹配文档链接的关键词
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

# 需要深入爬取的 entry_type
ENTRY_TYPES_TO_CRAWL = {"docs", "official_website"}

# URL 路径黑名单（媒体维基特殊页面、GitHub 自身导航等）
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
    # GitHub 仓库导航链接（非文档）
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

# 域名黑名单（GitHub 自身的文档中心，不是项目文档）
EXCLUDE_DOMAINS = {
    "docs.github.com",
    "support.scribd.com",
    "twitter.com",
    "x.com",
}

# 路径黑名单（GitHub 自身资源页）
EXCLUDE_PATH_EXACT = {
    "/resources/whitepapers",
}

# 域名本身含文档关键词的站点（如 whitepaper.io），所有内链都匹配，需更严格验证
NOISY_DOC_DOMAINS = {
    "whitepaper.io",
    "docs.eth",
}


def _has_doc_keyword(url: str) -> bool:
    lowered = url.lower()
    return any(kw in lowered for kw in DOC_URL_KEYWORDS)


def _doc_keyword_in_path_only(url: str) -> bool:
    """检查文档关键词是否仅出现在 path 部分（排除纯域名匹配）"""
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
    """检查 URL 是否在黑名单中"""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    path = parsed.path.lower()
    query = (parsed.query or "").lower()
    full_path = f"{path}?{query}" if query else path

    # 域名黑名单
    if domain in EXCLUDE_DOMAINS:
        return True
    # 精确路径黑名单
    if path.rstrip("/") in EXCLUDE_PATH_EXACT:
        return True
    # 路径模式黑名单（含 query string）
    for pattern in EXCLUDE_PATH_PATTERNS:
        if pattern.lower() in full_path or pattern.lower() in path:
            return True
    return False


def extract_doc_links(
    html: str, base_url: str, same_domain_only: bool = True
) -> list[tuple[str, str]]:
    """解析 HTML，提取文档链接，返回 [(url, entry_type), ...]"""
    from bs4 import BeautifulSoup
    from urllib.parse import urlparse

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

        # 跳过非 http(s) 链接
        if parsed.scheme not in ("http", "https"):
            continue

        link_domain = parsed.netloc.lower()

        # 黑名单过滤
        if _is_excluded_url(absolute_url):
            continue

        # 判断是否值得记录
        should_record = False
        link_text = a.get_text(strip=True)
        link_text_is_doc = _has_doc_keyword(link_text)

        # 噪音域名（如 whitepaper.io）：必须在 path 中有关键词，或链接文本有关键词
        if link_domain in NOISY_DOC_DOMAINS:
            if _doc_keyword_in_path_only(absolute_url) or link_text_is_doc:
                should_record = True
        # 1. URL 本身包含文档关键词
        elif _has_doc_keyword(absolute_url):
            should_record = True
        # 2. 链接文本包含文档关键词
        elif link_text_is_doc:
            # 需要同域验证，避免抓取不相关的链接
            if same_domain_only:
                if link_domain == base_domain:
                    should_record = True
            else:
                should_record = True

        if not should_record:
            continue

        # 跳过太长的 URL（可能是 tracking 链接）
        if len(absolute_url) > 500:
            continue

        normalized = absolute_url.rstrip("/")
        if normalized not in seen:
            seen.add(normalized)
            entry_type = infer_doc_entry_type(absolute_url)
            results.append((absolute_url, entry_type))

    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Phase B: 第二版文档发现 - 深入网页分析发现文档链接"
    )
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不写入数据库")
    parser.add_argument("--limit", type=int, default=50, help="最大处理的入口数量")
    parser.add_argument("--timeout", type=int, default=15, help="每个网页请求超时(秒)")
    parser.add_argument("--all-domains", action="store_true", help="不过滤外部域名链接")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    import psycopg
    import requests

    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection
    from crypto_research.db.upsert import load_sql

    settings = get_settings(require_database=True)
    upsert_sql = load_sql("biz/upsert_doc_source_entry.sql")

    # 1. 添加 deep_crawled_at 列（如果不存在）
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                ALTER TABLE biz.doc_source_entry
                ADD COLUMN IF NOT EXISTS deep_crawled_at TIMESTAMPTZ;
            """)

    # 2. 查询待处理的入口
    with get_connection(settings.database_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """
                SELECT entry_id, entity_type, asset_id, protocol_id, source_code,
                       entry_type, entry_url, discovered_from
                FROM biz.doc_source_entry
                WHERE entry_type = ANY(%s)
                  AND deep_crawled_at IS NULL
                ORDER BY
                    CASE entry_type WHEN 'docs' THEN 1 WHEN 'official_website' THEN 2 ELSE 3 END,
                    entry_id
                LIMIT %s
            """,
                (list(ENTRY_TYPES_TO_CRAWL), args.limit),
            )
            entries = [dict(row) for row in cur.fetchall()]

    if not entries:
        print(
            json.dumps(
                {"status": "no_candidates", "message": "没有待处理的入口"},
                ensure_ascii=False,
            )
        )
        return 0

    docs_count = sum(1 for e in entries if e["entry_type"] == "docs")
    website_count = sum(1 for e in entries if e["entry_type"] == "official_website")
    print(
        f"待处理入口: {len(entries)} (docs: {docs_count}, official_website: {website_count})",
        flush=True,
    )

    # 3. HTTP Session
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    # 连接超时 + 读取超时分别设置（许多山寨币网站已死，降低等待时间）
    req_timeout = (5, args.timeout)

    total_discovered = 0
    crawled = 0
    failed = 0
    skipped_not_html = 0
    empty_pages = 0
    dry_run_preview: list[dict] = []

    for entry in entries:
        entry_url = entry["entry_url"]
        entry_id = entry["entry_id"]
        source_code = entry["source_code"]

        try:
            resp = session.get(entry_url, timeout=req_timeout, allow_redirects=True)
            resp.raise_for_status()

            content_type = (resp.headers.get("Content-Type") or "").lower()
            if "text/html" not in content_type and "text/plain" not in content_type:
                skipped_not_html += 1
                _mark_crawled(settings.database_url, entry_id)
                continue

            doc_links = extract_doc_links(
                resp.text,
                resp.url,
                same_domain_only=not args.all_domains,
            )

            if args.dry_run:
                dry_run_preview.append(
                    {
                        "entry_url": entry_url[:120],
                        "entry_type": entry["entry_type"],
                        "source_code": source_code,
                        "final_url": resp.url[:120],
                        "discovered": len(doc_links),
                        "sample_links": doc_links[:5],
                    }
                )
            else:
                if doc_links:
                    rows = [
                        (
                            entry["entity_type"],
                            entry["asset_id"],
                            entry["protocol_id"],
                            source_code,
                            link_type,
                            link_url,
                            f"deep_crawl:{entry_url[:50]}",
                            False,
                        )
                        for link_url, link_type in doc_links
                    ]
                    with get_connection(settings.database_url) as conn:
                        from crypto_research.db.upsert import execute_many

                        execute_many(conn, upsert_sql, rows)
                    total_discovered += len(doc_links)
                else:
                    empty_pages += 1

            _mark_crawled(settings.database_url, entry_id)
            crawled += 1

            status = f"+{len(doc_links)} docs" if doc_links else "无文档链接"
            print(
                f"[{crawled}/{len(entries)}] {entry_url[:80]}... -> {status}",
                flush=True,
            )

        except Exception as e:
            failed += 1
            try:
                _mark_crawled(settings.database_url, entry_id)
            except Exception:
                pass
            print(
                f"[{crawled + failed}/{len(entries)}] FAILED: {entry_url[:80]}... -> {e}",
                flush=True,
            )

    if args.dry_run:
        print(
            json.dumps(
                {
                    "mode": "dry-run",
                    "candidates": len(entries),
                    "preview": dry_run_preview[:10],
                    "total_preview_entries": len(dry_run_preview),
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0

    print(
        json.dumps(
            {
                "status": "complete",
                "candidates": len(entries),
                "crawled": crawled,
                "failed": failed,
                "skipped_not_html": skipped_not_html,
                "empty_pages": empty_pages,
                "total_discovered": total_discovered,
            },
            ensure_ascii=False,
        )
    )
    return 0


def _mark_crawled(database_url: str, entry_id: int) -> None:
    from crypto_research.db.conn import get_connection

    with get_connection(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE biz.doc_source_entry SET deep_crawled_at = NOW() WHERE entry_id = %s",
                (entry_id,),
            )


if __name__ == "__main__":
    raise SystemExit(main())
