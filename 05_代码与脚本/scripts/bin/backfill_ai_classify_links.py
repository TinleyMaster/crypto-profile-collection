"""存量链接 AI 内容分类回填脚本（阶段2）。

对 biz.doc_source_entry 中规则/元数据分类置信度低的记录（默认 classify_method='default'，
即 content_topics=['other'] 且置信度 0.3），抓取页面正文后用 LLM 做内容主题多标签分类，
回写 content_topics / classify_method='ai_content' / classify_confidence。

采用主键分页逐批读取 + 并发抓正文 + LLM 批量分类，避免一次性加载全表。

用法：
    python backfill_ai_classify_links.py --dry-run --limit 50        # 预览，不写库
    python backfill_ai_classify_links.py --limit 5000                # 实际回填前 5000 条
    python backfill_ai_classify_links.py --method all                # default + keyword 都重分类
    python backfill_ai_classify_links.py --entry-types docs,docs_portal
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
for _proxy_var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_proxy_var, None)

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

DEFAULT_ENTRY_TYPES = "docs,docs_portal,official_website,other"


def _extract_title(url: str) -> str:
    """从 URL 路径最后一段提取一个粗略标题（供 AI 参考）。"""
    from urllib.parse import urlparse, unquote

    path = unquote(urlparse(url).path)
    name = path.rsplit("/", 1)[-1] if "/" in path else path
    if "." in name:
        name = name.rsplit(".", 1)[0]
    return name.replace("-", " ").replace("_", " ").strip()[:200]


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


def _where_method(method: str) -> str:
    if method == "default":
        return "classify_method = 'default'"
    if method == "keyword":
        return "classify_method = 'keyword'"
    return "classify_method IN ('default', 'keyword')"


def main() -> int:
    parser = argparse.ArgumentParser(description="存量链接 AI 内容分类回填（阶段2）")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少条（0=全部）")
    parser.add_argument("--batch-size", type=int, default=25, help="LLM 每批条数")
    parser.add_argument("--workers", type=int, default=8, help="抓正文并发线程数")
    parser.add_argument("--rpm", type=int, default=60, help="LLM 调用速率限制（次/分钟）")
    parser.add_argument(
        "--method", type=str, default="default",
        choices=["default", "keyword", "all"],
        help="重分类哪类记录：default=置信度0.3，keyword=置信度0.6，all=两者",
    )
    parser.add_argument(
        "--entry-types", type=str, default=DEFAULT_ENTRY_TYPES,
        help="逗号分隔的 entry_type 白名单",
    )
    parser.add_argument("--fetch-timeout", type=int, default=15, help="抓正文超时（秒）")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不写库")
    args = parser.parse_args()

    entry_types = [t.strip() for t in args.entry_types.split(",") if t.strip()]
    if not entry_types:
        print("ERROR: --entry-types 不能为空")
        return 1

    import psycopg
    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection
    from crypto_research.clients.llm_client import LLMClient

    settings = get_settings(require_database=True)
    llm = LLMClient(settings, rpm=args.rpm)
    if not llm.is_available():
        print("ERROR: 未配置 LLM。请设置 OPENAI_API_KEY/OPENAI_BASE_URL/LLM_MODEL 或 ARK_*。")
        return 1

    where_method = _where_method(args.method)

    print(f"提供商: {llm.provider} | 模型: {llm.model}")
    print(f"模式: {'DRY-RUN 预览' if args.dry_run else '执行回填'}")
    print(f"范围: method={args.method} | entry_types={entry_types}")
    print(f"批大小: {args.batch_size} | 抓取并发: {args.workers} | 限速: {args.rpm}/min")
    print()

    processed = 0
    classified = 0
    empty_text = 0
    failed = 0
    last_id = 0
    start = time.time()

    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT count(*)
                FROM biz.doc_source_entry
                WHERE {where_method}
                  AND entry_type = ANY(%s)
                  AND content_topics IS NOT NULL
                """,
                (entry_types,),
            )
            total = cur.fetchone()[0]
        print(f"待处理总数: {total:,}\n")

        while True:
            remaining = args.limit - processed if args.limit > 0 else None
            fetch = args.batch_size if remaining is None else min(args.batch_size, remaining)
            if fetch <= 0:
                break

            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute(
                    f"""
                    SELECT entry_id, entry_type, entry_url
                    FROM biz.doc_source_entry
                    WHERE entry_id > %s
                      AND {where_method}
                      AND entry_type = ANY(%s)
                      AND content_topics IS NOT NULL
                    ORDER BY entry_id
                    LIMIT %s
                    """,
                    (last_id, entry_types, fetch),
                )
                rows = [dict(r) for r in cur.fetchall()]
            if not rows:
                break

            # 并发抓正文
            texts: list[str] = [""] * len(rows)
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

            llm_items = [
                {
                    "entry_id": str(r["entry_id"]),
                    "url": r["entry_url"],
                    "title": _extract_title(r["entry_url"]),
                    "text": texts[i],
                }
                for i, r in enumerate(rows)
            ]

            results = llm.batch_classify_content_topics(llm_items)

            # 第一批打印几个样本，便于核对分类质量
            if processed == 0:
                shown = 0
                for r, t, res in zip(rows, texts, results):
                    if not res["content_topics"]:
                        continue
                    print(
                        f"  [样例] {r['entry_url'][:80]}\n"
                        f"         -> {res['content_topics']} (conf={res['confidence']:.2f}) "
                        f"| 正文 {len(t)} 字 | {res['reason'][:60]}"
                    )
                    shown += 1
                    if shown >= 3:
                        break

            updates = []
            for r, t, res in zip(rows, texts, results):
                if not res["content_topics"] or res["confidence"] <= 0:
                    failed += 1
                    continue
                conf = res["confidence"]
                if not t:
                    conf = min(conf, 0.6)  # 无正文，置信度封顶
                    empty_text += 1
                updates.append((res["content_topics"], conf, r["entry_id"]))
                classified += 1

            if updates and not args.dry_run:
                with conn.cursor() as cur:
                    cur.executemany(
                        """
                        UPDATE biz.doc_source_entry
                        SET content_topics = %s, classify_method = 'ai_content', classify_confidence = %s
                        WHERE entry_id = %s
                        """,
                        updates,
                    )
                conn.commit()

            processed += len(rows)
            last_id = rows[-1]["entry_id"]

            elapsed = time.time() - start
            pct = processed / total * 100 if total else 0
            rate = processed / elapsed if elapsed > 0 else 0
            eta = int((total - processed) / rate) if rate > 0 else 0
            eta_str = f"{eta // 60}m {eta % 60}s" if eta >= 60 else f"{eta}s"
            print(
                f"[{processed:,}/{total:,} {pct:.1f}%] "
                f"已分类:{classified:,} 无正文:{empty_text:,} 失败:{failed:,} "
                f"| {rate:.1f}条/s ETA:{eta_str}"
            )

    print()
    print("=" * 60)
    tag = "[DRY-RUN] " if args.dry_run else ""
    print(f"{tag}完成：处理 {processed:,} 条，AI 分类成功 {classified:,} 条，"
          f"无正文 {empty_text:,} 条，失败 {failed:,} 条。")
    print(f"耗时 {int(time.time() - start)}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
