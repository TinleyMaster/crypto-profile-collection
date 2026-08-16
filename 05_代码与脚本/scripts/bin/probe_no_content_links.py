"""无正文链接甄别脚本。

对 AI 补分类中「抓不到正文」的链接（默认 classify_error 含 '无正文'）重新探测 HTTP 状态，
区分四类，供后续分别处置：

- dead       : 404/410 或域名解析失败（真死链，可删除）
- blocked    : 403/503/429/超时/连接失败（反爬或暂时失败，保留重试）
- js_rendered: 200 但 HTML 去标签后无实质正文（JS 渲染 SPA，保留 needs_browser 交 SPA 爬取）
- recovered  : 200 且有正文（上次可能是暂时失败，可重新 AI 分类）

用法：
    python probe_no_content_links.py --dry-run --limit 50     # 只预览分类统计，不写库
    python probe_no_content_links.py --execute                # 删除 dead 类，其余保留
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
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

MIN_TEXT_LEN = 30  # 去标签后正文短于该长度视为「无实质正文」


def _probe(url: str, timeout: int) -> tuple[str, str, int]:
    """探测一个 URL，返回 (分类, 详情, 正文长度)。

    分类 ∈ {dead, blocked, js_rendered, recovered}。
    """
    import requests
    import socket

    try:
        resp = requests.get(
            url,
            headers={"User-Agent": UA},
            timeout=timeout,
            allow_redirects=True,
        )
    except requests.exceptions.Timeout:
        return "blocked", "timeout", 0
    except requests.exceptions.ConnectionError as e:
        # 域名解析失败 → 死链；其余连接失败 → 暂时问题，保留重试
        msg = str(e).lower()
        cause = e
        is_dns = False
        while cause is not None:
            if isinstance(cause, socket.gaierror):
                is_dns = True
                break
            cause = cause.__cause__
        if (
            is_dns
            or "getaddrinfo" in msg
            or "name or service not known" in msg
            or "nodename nor servname" in msg
            or "failed to resolve" in msg
        ):
            return "dead", "dns", 0
        return "blocked", "conn", 0
    except requests.exceptions.TooManyRedirects:
        return "blocked", "redirect", 0
    except Exception as e:
        return "blocked", type(e).__name__, 0

    status = resp.status_code
    if status in (404, 410):
        return "dead", f"http_{status}", 0
    if status in (403, 429, 503):
        return "blocked", f"http_{status}", 0
    if status != 200:
        return "blocked", f"http_{status}", 0

    ctype = (resp.headers.get("content-type") or "").lower()
    if "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf"):
        return "recovered", "pdf", -1  # PDF 视为有内容
    if "html" not in ctype and "text" not in ctype:
        return "blocked", f"ctype:{ctype[:40]}", 0

    text = resp.text
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < MIN_TEXT_LEN:
        return "js_rendered", "empty_html", len(text)
    return "recovered", "html", len(text)


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
    parser = argparse.ArgumentParser(description="无正文链接甄别（区分死链/反爬/JS渲染/可恢复）")
    parser.add_argument("--limit", type=int, default=0, help="最多探测多少条（0=全部）")
    parser.add_argument("--timeout", type=int, default=15, help="HTTP 探测超时（秒）")
    parser.add_argument(
        "--classify-error", type=str, default="无正文",
        help="按 classify_error 前缀过滤（默认 '无正文'）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只预览分类，不删除死链")
    parser.add_argument("--execute", action="store_true", help="删除 dead 类（真死链）")
    args = parser.parse_args()

    from crypto_research.config import get_settings

    settings = get_settings(require_database=True)
    db_url = settings.database_url

    where_clause = "classify_error LIKE %s"
    where_param = f"{args.classify_error}%"

    def _count(conn):
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT count(*) FROM biz.doc_source_entry WHERE {where_clause}",
                (where_param,),
            )
            return cur.fetchone()[0]

    total = _db_retry(db_url, _count)
    print(f"待甄别无正文链接总数: {total:,}")
    if total == 0:
        print("无待甄别链接（方案 A 标记的「无正文」项暂为空）。")
        return 0

    limit = args.limit or total
    print(f"本次探测上限: {limit:,} | 模式: {'DRY-RUN' if args.dry_run else '执行删除死链' if args.execute else '仅报告'}\n")

    def _fetch_batch(conn, last_id):
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT entry_id, entry_url
                FROM biz.doc_source_entry
                WHERE entry_id > %s AND {where_clause}
                ORDER BY entry_id
                LIMIT %s
                """,
                (last_id, where_param, 500),
            )
            return [dict(r) for r in cur.fetchall()]

    stats = {"dead": 0, "blocked": 0, "js_rendered": 0, "recovered": 0}
    dead_ids: list[int] = []
    samples: list[str] = []
    processed = 0
    last_id = 0

    while processed < limit:
        rows = _db_retry(db_url, lambda conn: _fetch_batch(conn, last_id))
        if not rows:
            break

        for r in rows:
            cat, detail, text_len = _probe(r["entry_url"], args.timeout)
            stats[cat] += 1
            if cat == "dead":
                dead_ids.append(r["entry_id"])
            if len(samples) < 30:
                samples.append(f"  {cat:<11} {detail:<14} {r['entry_url'][:90]}")

        processed += len(rows)
        last_id = rows[-1]["entry_id"]
        print(
            f"[{processed:,}/{limit:,}] dead:{stats['dead']:,} blocked:{stats['blocked']:,} "
            f"js_rendered:{stats['js_rendered']:,} recovered:{stats['recovered']:,}"
        )

    print("\n============================================================")
    print(f"dead（死链，可删）:        {stats['dead']:,}")
    print(f"blocked（反爬/暂时失败）:  {stats['blocked']:,}")
    print(f"js_rendered（JS渲染SPA）:  {stats['js_rendered']:,}")
    print(f"recovered（已恢复可重分类）: {stats['recovered']:,}")
    print("============================================================")
    print("样本：")
    print("\n".join(samples) if samples else "  （无）")

    if args.dry_run:
        print("\n[DRY-RUN] 未写入数据库")
        return 0

    if args.execute and dead_ids:
        def _delete(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM biz.doc_source_entry WHERE entry_id = ANY(%s)",
                    (dead_ids,),
                )
                return cur.rowcount

        deleted = _db_retry(db_url, _delete)
        print(f"\n[执行] 已删除 {deleted:,} 条死链")
    elif args.execute:
        print("\n[执行] 无死链需要删除")
    else:
        print("\n[仅报告] 未删除任何条目（加 --execute 删除死链，加 --dry-run 显式预览）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
