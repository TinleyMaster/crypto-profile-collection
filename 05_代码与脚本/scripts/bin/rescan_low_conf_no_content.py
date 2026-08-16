"""历史无正文回扫脚本。

旧版 AI 分类对「抓不到正文」的链接会让 LLM 凭 URL 硬猜，标成 ai_content（conf 封顶 0.6）。
本脚本回扫这些低置信度 ai_content，重新抓正文确认：

- 仍抓不到正文 → 标 needs_browser=TRUE + classify_method='ai_failed'，交 SPA 浏览器重抓；
- 能抓到正文 → 保留 ai_content 不动（说明不是无正文，是低置信度或上次暂时失败）。

用法：
    python rescan_low_conf_no_content.py --dry-run --limit 50       # 预览，不写库
    python rescan_low_conf_no_content.py --max-conf 0.6             # 实际回扫全部低置信度
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# 清除代理变量：requests 会读取 HTTP(S)_PROXY，本地 socks5 代理不可用会导致请求失败
for _pv in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_pv, None)

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(line_buffering=True)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

SNIPPET_LIMIT = 3000
PDF_MAX_PAGES = 30

DEFAULT_ENTRY_TYPES = "official_website,docs,docs_portal,whitepaper_page,medium,announcement"


def _fetch_page_text(url: str, timeout: int) -> str:
    """抓取 URL 正文文本：HTML 去标签，PDF 用 PyPDF2 抽取。失败返回空串。"""
    import requests

    try:
        resp = requests.get(
            url,
            headers={"User-Agent": UA},
            timeout=timeout,
            allow_redirects=True,
        )
        resp.raise_for_status()
        ctype = (resp.headers.get("content-type") or "").lower()
        if "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf"):
            from PyPDF2 import PdfReader

            reader = PdfReader(io.BytesIO(resp.content))
            parts = []
            for page in reader.pages[:PDF_MAX_PAGES]:
                t = page.extract_text()
                if t:
                    parts.append(t)
            text = "\n\n".join(parts)
            return re.sub(r"\s+", " ", text).strip()[:SNIPPET_LIMIT]
        if "html" not in ctype and "text" not in ctype:
            return ""
        text = resp.text
    except Exception:
        return ""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:SNIPPET_LIMIT]


def _db_retry(database_url, fn, retries=8, delay=5):
    """在可重试连接下执行 fn(conn)，PG 周期性重启时自动重连重试。"""
    import psycopg
    from psycopg.errors import AdminShutdown
    from crypto_research.db.conn import get_connection

    last_err = None
    for i in range(retries):
        try:
            with get_connection(database_url) as conn:
                return fn(conn)
        except (psycopg.OperationalError, AdminShutdown) as e:
            last_err = e
            print(f"  [WARN] DB 操作失败({i + 1}/{retries}): {str(e)[:70]}，{delay}s 后重试...", flush=True)
            time.sleep(delay)
    raise last_err


def main() -> int:
    parser = argparse.ArgumentParser(description="回扫低置信度 ai_content，确认并标记无正文")
    parser.add_argument("--max-conf", type=float, default=0.6, help="classify_confidence 阈值（默认 0.6）")
    parser.add_argument(
        "--entry-types", type=str, default=DEFAULT_ENTRY_TYPES,
        help="逗号分隔的 entry_type 白名单",
    )
    parser.add_argument("--limit", type=int, default=0, help="最多回扫多少条（0=全部）")
    parser.add_argument("--batch-size", type=int, default=2000, help="每批读取条数")
    parser.add_argument("--workers", type=int, default=8, help="抓正文并发线程数")
    parser.add_argument("--fetch-timeout", type=int, default=15, help="抓正文超时（秒）")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不写库")
    args = parser.parse_args()

    entry_types = [t.strip() for t in args.entry_types.split(",") if t.strip()]
    if not entry_types:
        print("错误：--entry-types 不能为空")
        return 2

    from crypto_research.config import get_settings

    settings = get_settings(require_database=True)
    db_url = settings.database_url

    def _count(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*)
                FROM biz.doc_source_entry
                WHERE classify_method = 'ai_content'
                  AND classify_confidence <= %s
                  AND entry_type = ANY(%s)
                """,
                (args.max_conf, entry_types),
            )
            return cur.fetchone()[0]

    total = _db_retry(db_url, _count)
    print(f"待回扫低置信度 ai_content 总数: {total:,}")
    if total == 0:
        print("无待回扫链接。")
        return 0

    limit = args.limit or total
    print(f"回扫上限: {limit:,} | 模式: {'DRY-RUN' if args.dry_run else '执行回写'}\n")

    def _fetch_batch(conn, last_id, batch_size):
        with conn.cursor(row_factory=__import__("psycopg").rows.dict_row) as cur:
            cur.execute(
                """
                SELECT entry_id, entry_url
                FROM biz.doc_source_entry
                WHERE entry_id > %s
                  AND classify_method = 'ai_content'
                  AND classify_confidence <= %s
                  AND entry_type = ANY(%s)
                ORDER BY entry_id
                LIMIT %s
                """,
                (last_id, args.max_conf, entry_types, batch_size),
            )
            return [dict(r) for r in cur.fetchall()]

    confirmed = 0  # 确认无正文（回写 needs_browser）
    has_text = 0   # 能抓到正文（保留 ai_content）
    processed = 0
    last_id = 0
    start = time.time()

    while processed < limit:
        rows = _db_retry(db_url, lambda conn: _fetch_batch(conn, last_id, args.batch_size))
        if not rows:
            break

        # 并发抓正文
        texts = [""] * len(rows)
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {
                ex.submit(_fetch_page_text, r["entry_url"], args.fetch_timeout): i
                for i, r in enumerate(rows)
            }
            for fut in as_completed(futs):
                i = futs[fut]
                try:
                    texts[i] = fut.result()
                except Exception:
                    texts[i] = ""

        no_content_ids = [r["entry_id"] for r, t in zip(rows, texts) if not t]
        confirmed += len(no_content_ids)
        has_text += len(rows) - len(no_content_ids)

        if no_content_ids and not args.dry_run:
            def _write(conn):
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE biz.doc_source_entry
                        SET needs_browser = TRUE, classify_method = 'ai_failed',
                            classify_error = '无正文（回扫确认）', classify_reason = NULL,
                            classify_confidence = NULL
                        WHERE entry_id = ANY(%s)
                        """,
                        (no_content_ids,),
                    )
                    return cur.rowcount

            _db_retry(db_url, _write)

        processed += len(rows)
        last_id = rows[-1]["entry_id"]

        elapsed = time.time() - start
        rate = processed / elapsed if elapsed > 0 else 0
        eta = int((limit - processed) / rate) if rate > 0 else 0
        eta_str = f"{eta // 60}m {eta % 60}s" if eta >= 60 else f"{eta}s"
        print(
            f"[{processed:,}/{limit:,}] 确认无正文:{confirmed:,} 有正文:{has_text:,} "
            f"| {rate:.1f}条/s ETA:{eta_str}"
        )

    print("\n============================================================")
    print(f"回扫完成: 确认无正文 {confirmed:,} 条（标 needs_browser），有正文 {has_text:,} 条（保留）")
    if args.dry_run:
        print("[DRY-RUN] 未写入数据库")
    return 0


if __name__ == "__main__":
    sys.exit(main())
