"""
回溯扫描：识别 B2 已爬取但实际是 SPA 的页面，标记 needs_browser=TRUE。

扫描逻辑：
1. 找到 B2 已爬取（deep_crawled_at IS NOT NULL）但返回 0 链接的 entry
2. 用轻量 HTTP GET 检查 HTML 是否包含 SPA 特征
3. 是 SPA → 设置 needs_browser=TRUE, deep_crawled_at=NULL（让 B2 跳过，SPA 爬虫处理）
4. 不是 SPA → 不动（正常完成的页面）
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(line_buffering=True)

import urllib.error
import urllib.request

# SPA 检测：HTML < 5000 bytes 或包含 SPA 框架标志
SPA_HTML_MAX_BYTES = 5000
SPA_MARKERS = [
    'id="app"', 'id="root"', 'id="__next"', 'id="__nuxt"',
    'react-dom', 'vue', 'window.__NUXT__', '__NEXT_DATA__',
    'ng-app', 'ng-version', 'data-reactroot', 'data-reactid',
]

_stats_lock = threading.Lock()
_stats = {"total": 0, "scanned": 0, "spa": 0, "not_spa": 0, "failed": 0}


def is_spa(url: str, timeout: int = 8) -> bool | None:
    """快速检查 URL 是否为 SPA。返回 True/False/None（失败）。"""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = (resp.headers.get("Content-Type") or "").lower()
            if "text/html" not in content_type:
                return False
            html = resp.read(SPA_HTML_MAX_BYTES + 1000).decode("utf-8", errors="replace")
    except Exception:
        return None

    if len(html) < SPA_HTML_MAX_BYTES:
        return True

    html_lower = html.lower()
    for marker in SPA_MARKERS:
        if marker in html_lower:
            return True

    return False


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="回溯扫描 B2 已爬取页面，找出 SPA")
    p.add_argument("--dry-run", action="store_true", help="仅预览不写入。")
    p.add_argument("--limit", type=int, default=500, help="最大扫描数。")
    p.add_argument("--workers", type=int, default=10, help="并发扫描线程数。")
    return p


def main() -> int:
    args = build_parser().parse_args()

    import psycopg
    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    settings = get_settings(require_database=True)

    # 确保列存在
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE biz.doc_source_entry ADD COLUMN IF NOT EXISTS needs_browser BOOLEAN DEFAULT FALSE"
            )

    # 查询候选：B2 已爬取但未标记 needs_browser 的页面
    candidate_sql = """
        SELECT dse.entry_id, dse.entry_url
        FROM biz.doc_source_entry dse
        WHERE dse.deep_crawled_at IS NOT NULL
          AND dse.entry_type IN ('official_website', 'docs')
          AND COALESCE(dse.needs_browser, FALSE) = FALSE
        ORDER BY dse.entry_id
        LIMIT %s
    """

    with get_connection(settings.database_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(candidate_sql, (args.limit,))
            candidates = [dict(row) for row in cur.fetchall()]

    if not candidates:
        print("无候选。")
        return 0

    _stats["total"] = len(candidates)
    print(f"候选: {len(candidates)} 个页面, workers={args.workers}")
    print()

    spa_ids: list[int] = []

    def check_one(entry):
        entry_id = entry["entry_id"]
        url = entry["entry_url"]
        result = is_spa(url)
        with _stats_lock:
            _stats["scanned"] += 1
            if result is True:
                _stats["spa"] += 1
            elif result is False:
                _stats["not_spa"] += 1
            else:
                _stats["failed"] += 1
            s = _stats
            print(
                f"[{s['scanned']}/{s['total']}] "
                f"{'SPA' if result else 'OK' if result is False else 'FAIL'}  "
                f"{url[:100]}",
                flush=True,
            )
        return entry_id, result

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(check_one, e): e for e in candidates}
        for future in as_completed(futures):
            entry_id, result = future.result()
            if result is True:
                spa_ids.append(entry_id)

    print(f"\n扫描完成: SPA={_stats['spa']} 非SPA={_stats['not_spa']} 失败={_stats['failed']}")

    if not spa_ids:
        print("未发现 SPA 页面。")
        return 0

    if args.dry_run:
        print(f"\n[dry-run] 将标记 {len(spa_ids)} 个页面为 needs_browser=TRUE")
        print(f"  entry_ids: {spa_ids[:20]}{'...' if len(spa_ids) > 20 else ''}")
        return 0

    # 标记 SPA
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE biz.doc_source_entry
                SET needs_browser = TRUE, deep_crawled_at = NULL
                WHERE entry_id = ANY(%s)
                """,
                (spa_ids,),
            )
        conn.commit()
        print(f"已标记 {len(spa_ids)} 个页面为 SPA（needs_browser=TRUE, deep_crawled_at=NULL）")

    return 0


if __name__ == "__main__":
    sys.exit(main())