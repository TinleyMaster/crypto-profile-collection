"""
Phase B2-sitemap: sitemap 全量深爬（独立脚本，用于老币抢救 / 手动补链）。

与 phase_b2_deep_doc_discovery（边爬边写主表，分类仅作标签）不同，
本脚本分两阶段：

    阶段1 深爬：对每个种子入口（official_website/docs/docs_portal）尽可能深地
                抓取 sitemap.xml（含 sitemap index 递归、robots.txt 声明、.gz）；
                无 sitemap 时回退到 HTML 内链 + llms.txt，并跟随发现的文档站
                子域名（docs.* / gitbook 等）进一步深爬。发现的全部页面 URL
                存入临时表 biz.doc_crawl_staging（仅 URL）。
    阶段2 分类：对临时表中未处理的 URL 调用 classify_link 分类，
                命中 content_topics（非 other）的，或官网/文档首页，才写入主表
                biz.doc_source_entry；其余丢弃并标记已处理。

复用 phase_b2_deep_doc_discovery 的去重/排除/归一化工具函数，保持过滤口径一致。
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# 复用 B2 的工具函数（该模块顶层会把 stdout 包装成 UTF-8）
import phase_b2_deep_doc_discovery as b2  # noqa: E402

SEED_ENTRY_TYPES = ("official_website", "docs", "docs_portal")

# 常见 sitemap 位置兜底（robots.txt 声明优先）
SITEMAP_CANDIDATES = (
    "sitemap.xml",
    "sitemap_index.xml",
    "sitemap-index.xml",
    "wp-sitemap.xml",
)

# 官网/文档首页即使未命中投研关键词也保留
KEEP_HOMEPAGE_TYPES = {"official_website", "docs", "docs_portal"}

# 文档聚合平台：深爬时限制同 host，避免把其他项目的内容收进来
DOC_PLATFORM_HOSTS = ("gitbook.io", "readthedocs.io", "readme.io", "gitbook.com")

STAGING_DDL = """
CREATE TABLE IF NOT EXISTS biz.doc_crawl_staging (
    staging_id BIGSERIAL PRIMARY KEY,
    asset_id BIGINT NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    source_code VARCHAR(32),
    seed_entry_id BIGINT,
    entry_url TEXT NOT NULL,
    source_type VARCHAR(32),
    content_topics TEXT[],
    classify_method TEXT,
    classify_confidence REAL,
    is_keep BOOLEAN,
    written BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_doc_crawl_staging_url UNIQUE (asset_id, entry_url)
);
"""

STAGING_UPSERT_SQL = """
INSERT INTO biz.doc_crawl_staging (asset_id, source_code, seed_entry_id, entry_url)
VALUES (%s, %s, %s, %s)
ON CONFLICT (asset_id, entry_url) DO NOTHING
"""

STAGING_CLASSIFY_SQL = """
UPDATE biz.doc_crawl_staging
SET source_type = %s,
    content_topics = %s,
    classify_method = %s,
    classify_confidence = %s,
    is_keep = %s,
    written = TRUE
WHERE staging_id = %s
"""


CONNECT_RETRIES = 10
CONNECT_RETRY_DELAY = 6


@contextmanager
def _get_connection_retry(db_url: str):
    """带重试的 DB 连接（Zeabur PG 周期性重启会偶发断开）。"""
    from crypto_research.db.conn import get_connection

    last_err = None
    ctx = None
    for i in range(CONNECT_RETRIES):
        try:
            ctx = get_connection(db_url)
            conn = ctx.__enter__()
            break
        except Exception as e:
            last_err = e
            print(f"  [WARN] 连接失败({i + 1}/{CONNECT_RETRIES}): {str(e)[:80]}", flush=True)
            time.sleep(CONNECT_RETRY_DELAY)
    if ctx is None:
        raise last_err
    try:
        yield conn
    except BaseException as e:
        if not ctx.__exit__(type(e), e, e.__traceback__):
            raise
    else:
        ctx.__exit__(None, None, None)


def _ensure_staging_table(db_url: str) -> None:
    with _get_connection_retry(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(STAGING_DDL)


def _fetch_robots_sitemaps(origin: str, session, timeout: int) -> list[str]:
    """从 robots.txt 里解析 Sitemap: 声明。"""
    out: list[str] = []
    try:
        resp = session.get(f"{origin}/robots.txt", timeout=(3, timeout), allow_redirects=True)
        if resp.status_code != 200:
            return out
        for line in (resp.text or "").splitlines():
            line = line.strip()
            if line.lower().startswith("sitemap:"):
                su = line.split(":", 1)[1].strip()
                if su.startswith("http"):
                    out.append(su)
    except Exception:
        pass
    return out


def _fetch_sitemap_deep(
    base_url: str,
    session,
    timeout: int,
    max_sitemaps: int,
    max_pages: int,
    same_host_only: bool = False,
) -> list[str]:
    """尽可能深地抓取站点 sitemap，返回归一化后的页面 URL 列表。

    相比 B2 的 _fetch_sitemap（固定 200 页 / 10 个子 sitemap），这里：
    - 先读 robots.txt 的 Sitemap 声明，再兜底常见 sitemap 路径；
    - 递归展开 sitemap index，上限可配（默认 200 个子 sitemap）；
    - 页面数上限可配（默认 20000），覆盖大站；
    - 支持 .xml.gz 压缩 sitemap；
    - same_host_only=True 时仅保留同 host（子域名）链接，用于 gitbook.io 等聚合平台，
      避免跨项目污染。
    """
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    base_host = parsed.netloc.lower()
    base_root = b2._root_domain(base_host)

    def _same_site(netloc: str) -> bool:
        nl = netloc.lower()
        if same_host_only:
            return nl == base_host
        return b2._root_domain(nl) == base_root

    pending: list[str] = []
    seen_sitemaps: set[str] = set()
    for su in _fetch_robots_sitemaps(origin, session, timeout):
        if su not in seen_sitemaps and su not in pending:
            pending.append(su)
    for cand in SITEMAP_CANDIDATES:
        pending.append(f"{origin}/{cand}")

    page_urls: list[str] = []
    while pending and len(seen_sitemaps) < max_sitemaps:
        su = pending.pop(0)
        if su in seen_sitemaps:
            continue
        seen_sitemaps.add(su)
        try:
            resp = session.get(su, timeout=(3, timeout), allow_redirects=True)
            if resp.status_code != 200:
                continue
            if su.lower().endswith(".gz"):
                try:
                    text = gzip.decompress(resp.content).decode("utf-8", "ignore")
                except Exception:
                    continue
            else:
                text = resp.text or ""
            if not text.strip():
                continue
            root_tag, locs = b2._parse_sitemap(text)
            if not locs:
                continue
            if root_tag == "sitemapindex":
                for loc in locs:
                    lp = urlparse(loc)
                    if lp.scheme not in ("http", "https"):
                        continue
                    # 子 sitemap 只接受同站点，避免跨站
                    if not _same_site(lp.netloc):
                        continue
                    if loc not in seen_sitemaps and loc not in pending:
                        pending.append(loc)
            else:
                page_urls.extend(locs)
                if len(page_urls) >= max_pages:
                    break
        except Exception:
            continue

    results: list[str] = []
    seen: set[str] = set()
    for link in page_urls:
        lp = urlparse(link)
        if lp.scheme not in ("http", "https"):
            continue
        if not _same_site(lp.netloc):
            continue
        if b2._is_excluded_url(link):
            continue
        norm = b2._normalize_url(link)
        if norm in seen:
            continue
        seen.add(norm)
        results.append(norm)
        if len(results) >= max_pages:
            break
    return results


def _build_project_identifiers(entry: dict, resolved_url: str) -> list[str]:
    """构建项目标识符（代币简称 + 项目名分词 + 域名），用于放宽模式下跨域链接校验。"""
    identifiers: list[str] = []
    symbol = (entry.get("canonical_symbol") or "").strip()
    name = (entry.get("canonical_name") or "").strip()
    if symbol and len(symbol) >= 2:
        identifiers.append(symbol)
    if name:
        for word in name.replace("-", " ").replace(".", " ").split():
            word = word.strip()
            if len(word) >= 2 and word.lower() not in (
                "token", "coin", "dao", "protocol", "network", "chain",
                "finance", "swap", "defi",
            ):
                identifiers.append(word)
    domain = urlparse(resolved_url).netloc.lower()
    domain_name = domain.split(".")[0]
    if len(domain_name) >= 2 and domain_name not in ("www", "docs", "blog", "app", "api"):
        identifiers.append(domain_name)
    return identifiers


def _find_doc_sites(doc_links: list[tuple[str, str]], seed_url: str) -> list[str]:
    """从已发现的链接里识别文档站入口（docs 子域名 / gitbook 等聚合平台）。"""
    seed_host = urlparse(seed_url).netloc.lower()
    seed_root = b2._root_domain(seed_host)

    sites: dict[str, str] = {}
    for u, _ in doc_links:
        lp = urlparse(u)
        if lp.scheme not in ("http", "https"):
            continue
        host = lp.netloc.lower()
        if host == seed_host:
            continue
        # docs 子域名（同根域名）
        if host.startswith("docs.") and b2._root_domain(host) == seed_root:
            sites.setdefault(host, f"{lp.scheme}://{host}")
            continue
        # 聚合文档平台：取该 host 下路径最短的 URL 作为项目根
        if any(host == p or host.endswith("." + p) for p in DOC_PLATFORM_HOSTS):
            cur = sites.get(host)
            if cur is None or len(u) < len(cur):
                sites[host] = u
    return list(sites.values())


def _fallback_html_links(
    entry: dict, session, timeout: int, max_sitemaps: int, max_pages: int
) -> list[str]:
    """无 sitemap 时回退：抓首页 HTML，提取内链 + llms.txt，并深爬发现的文档站子域名。"""
    seed_url = entry["entry_url"]
    resp = session.get(seed_url, timeout=(3, timeout), allow_redirects=True)
    resp.raise_for_status()
    ct = (resp.headers.get("Content-Type") or "").lower()
    if "text/html" not in ct and "text/plain" not in ct:
        return []

    project_identifiers = _build_project_identifiers(entry, resp.url)
    doc_links = b2.extract_doc_links(
        resp.text,
        resp.url,
        same_domain_only=True,
        project_identifiers=project_identifiers,
        require_doc_keyword=False,
        skip_aggregation_filter=True,
    )

    existing = {u for u, _ in doc_links}

    if entry.get("entry_type") in ("docs", "docs_portal"):
        for u, t in b2._fetch_llms_txt(resp.url, session, timeout):
            if u not in existing:
                doc_links.append((u, t))
                existing.add(u)

    # 跟随文档站子域名深爬（gitbook/docs 等），补全 HTML 首页内链发现不了的章节
    for site_url in _find_doc_sites(doc_links, seed_url):
        extra = b2._fetch_llms_txt(site_url, session, timeout)
        if not extra:
            extra = [
                (u, b2.infer_doc_entry_type(u))
                for u in _fetch_sitemap_deep(
                    site_url, session, timeout, max_sitemaps, max_pages, same_host_only=True
                )
            ]
        for u, t in extra:
            if u not in existing:
                doc_links.append((u, t))
                existing.add(u)

    return [u for u, _ in doc_links]


def _crawl_seed(entry: dict, timeout: int, max_sitemaps: int, max_pages: int) -> dict:
    """深爬单个种子入口：优先 sitemap，无 sitemap 时回退 HTML 内链 + llms.txt。"""
    session = b2._make_session()
    try:
        urls = _fetch_sitemap_deep(entry["entry_url"], session, timeout, max_sitemaps, max_pages)
        if urls:
            return {"status": "ok", "entry": entry, "urls": urls, "method": "sitemap"}
        urls = _fallback_html_links(entry, session, timeout, max_sitemaps, max_pages)
        return {"status": "ok", "entry": entry, "urls": urls, "method": "html"}
    except Exception as e:
        return {"status": "failed", "entry": entry, "error": str(e)[:100]}
    finally:
        session.close()


def _select_candidates(db_url: str, args) -> list[dict]:
    import psycopg

    asset_filter = ""
    params: list = []
    if args.asset_id is not None:
        asset_filter = " AND dse.asset_id = %s"
        params.append(args.asset_id)
    if args.min_asset_id and args.min_asset_id > 0:
        asset_filter += " AND dse.asset_id >= %s"
        params.append(args.min_asset_id)

    sql = f"""
        SELECT dse.entry_id, dse.entity_type, dse.asset_id, dse.protocol_id,
               dse.source_code, dse.entry_type, dse.entry_url,
               a.canonical_symbol, a.canonical_name
        FROM biz.doc_source_entry dse
        LEFT JOIN core.asset a ON dse.asset_id = a.asset_id
        WHERE dse.entity_type = 'asset'
          AND dse.entry_type = ANY(%s)
          {asset_filter}
        ORDER BY dse.asset_id,
                 CASE dse.entry_type WHEN 'official_website' THEN 1 WHEN 'docs' THEN 2 ELSE 3 END,
                 dse.entry_id
        LIMIT %s
    """
    with _get_connection_retry(db_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(sql, [list(SEED_ENTRY_TYPES)] + params + [args.limit])
            return [dict(row) for row in cur.fetchall()]


def _is_homepage(url: str) -> bool:
    return urlparse(url).path in ("", "/")


def _should_keep(url: str, source_type: str, topics: list[str]) -> bool:
    """命中投研关键词（content_topics 非 other），或官网/文档首页，则保留。"""
    if topics and topics != ["other"]:
        return True
    if source_type in KEEP_HOMEPAGE_TYPES and _is_homepage(url):
        return True
    return False


def _stage_phase1(db_url: str, entries: list[dict], args) -> tuple[int, int, int, int]:
    """阶段1：并发深爬，把 URL 写入临时表。返回 (staged, failed, sitemap_seeds, html_seeds)。"""
    from crypto_research.db.upsert import execute_many

    staged = 0
    failed = 0
    sitemap_seeds = 0
    html_seeds = 0
    seen_urls: set[tuple[int, str]] = set()
    with _get_connection_retry(db_url) as conn:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(_crawl_seed, e, args.timeout, args.max_sitemaps, args.max_pages): e
                for e in entries
            }
            for future in as_completed(futures):
                result = future.result()
                if result["status"] != "ok":
                    failed += 1
                    continue
                if result.get("method") == "sitemap":
                    sitemap_seeds += 1
                else:
                    html_seeds += 1
                e = result["entry"]
                rows = []
                for url in result["urls"]:
                    key = (e["asset_id"], url)
                    if key in seen_urls:
                        continue
                    seen_urls.add(key)
                    rows.append((e["asset_id"], e["source_code"], e["entry_id"], url))
                if rows:
                    execute_many(conn, STAGING_UPSERT_SQL, rows)
                    staged += len(rows)
    return staged, failed, sitemap_seeds, html_seeds


def _classify_phase2(db_url: str, asset_ids: set[int], dry_run: bool) -> dict:
    """阶段2：读取临时表 → 分类 → 过滤 → 写主表并标记。"""
    import psycopg
    from crypto_research.db.upsert import execute_many, load_sql
    from crypto_research.mapping.classify_link import classify_link

    upsert_sql = load_sql("biz/upsert_doc_source_entry_noop.sql")

    with _get_connection_retry(db_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """
                SELECT s.staging_id, s.asset_id, s.source_code, s.entry_url,
                       dse.entry_url AS seed_url
                FROM biz.doc_crawl_staging s
                LEFT JOIN biz.doc_source_entry dse ON dse.entry_id = s.seed_entry_id
                WHERE s.asset_id = ANY(%s) AND s.written = FALSE
                """,
                (list(asset_ids),),
            )
            pending = [dict(row) for row in cur.fetchall()]

        promote_rows: list[tuple] = []
        classify_update_rows: list[tuple] = []
        keep_count = 0
        drop_count = 0

        for r in pending:
            url = r["entry_url"]
            cls = classify_link(url, source_code=r["source_code"] or "")
            source_type = cls["source_type"]
            topics = cls["content_topics"]
            method = cls["method"]
            confidence = cls["confidence"]
            keep = _should_keep(url, source_type, topics)

            classify_update_rows.append(
                (source_type, topics, method, confidence, keep, r["staging_id"])
            )
            if keep:
                keep_count += 1
                seed_url = r["seed_url"] or url
                discovered_from = f"sitemap_deep:{seed_url[:50]}"
                promote_rows.append(
                    (
                        "asset",
                        r["asset_id"],
                        None,
                        r["source_code"],
                        source_type,
                        url,
                        discovered_from,
                        False,
                        topics,
                        method,
                        confidence,
                    )
                )
            else:
                drop_count += 1

        if dry_run:
            return {
                "classified": len(pending),
                "keep": keep_count,
                "drop": drop_count,
                "written": 0,
            }

        if promote_rows:
            execute_many(conn, upsert_sql, promote_rows)
        if classify_update_rows:
            execute_many(conn, STAGING_CLASSIFY_SQL, classify_update_rows)

    return {
        "classified": len(pending),
        "keep": keep_count,
        "drop": drop_count,
        "written": len(promote_rows),
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Phase B2-sitemap: sitemap 全量深爬 + 分类过滤")
    p.add_argument("--asset-id", type=int, default=None, help="仅处理指定资产ID")
    p.add_argument("--min-asset-id", type=int, default=0, help="仅处理 asset_id >= 该值的资产，0 表示不过滤")
    p.add_argument("--limit", type=int, default=100, help="最多处理的种子入口数")
    p.add_argument("--workers", type=int, default=4, help="并发线程数")
    p.add_argument("--timeout", type=int, default=10, help="读取超时(秒)")
    p.add_argument("--max-sitemaps", type=int, default=200, help="单个站点最多展开的子 sitemap 数")
    p.add_argument("--max-pages", type=int, default=20000, help="单个站点最多收集的页面 URL 数")
    p.add_argument("--dry-run", action="store_true", help="只深爬+分类统计，不写主表")
    return p


def main() -> int:
    args = build_parser().parse_args()

    from crypto_research.config import get_settings

    settings = get_settings(require_database=True)
    db_url = settings.database_url

    _ensure_staging_table(db_url)

    entries = _select_candidates(db_url, args)
    if not entries:
        print(json.dumps({"status": "no_candidates"}, ensure_ascii=False))
        return 0

    print(
        f"候选种子入口: {len(entries)} 个（asset 数 {len({e['asset_id'] for e in entries})}），"
        f"{args.workers} workers"
    )

    _start = time.time()
    staged, failed, sitemap_seeds, html_seeds = _stage_phase1(db_url, entries, args)
    print(
        f"[阶段1] 深爬完成: 写入临时表 {staged} 条 URL，"
        f"sitemap 命中 {sitemap_seeds} 个入口 / HTML 回退 {html_seeds} 个入口，"
        f"失败 {failed} 个，耗时 {time.time() - _start:.0f}s"
    )

    asset_ids = {e["asset_id"] for e in entries}
    result = _classify_phase2(db_url, asset_ids, args.dry_run)
    print(
        json.dumps(
            {
                "status": "dry_run" if args.dry_run else "ok",
                "staged": staged,
                "failed_seeds": failed,
                **result,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
