"""
Phase B7: 防屏蔽链接本地下载 Fallback

针对 NotebookLM 无法访问的受保护链接（Cloudflare/WAF/403），
尝试通过浏览器 UA 模拟 + 完整 GET 请求下载页面内容，
保存为本地文件（HTML → 尝试提取纯文本或保存原始 HTML），
供后续手动上传到知识库/Google Drive。

策略：
  1. 从 biz.research_url 查询 health_status='protected' 且 relevance_score >= 0.5 的链接
  2. 用完整的浏览器请求头尝试下载（部分 WAF 对 HEAD 请求返回 403，GET 可过）
  3. 保存到 docs_storage/{symbol}_{asset_id}/fallback_docs/
  4. PDF 文件优先直接下载
  5. HTML 页面保存为 .html，同时尝试提取纯文本保存为 .txt
"""
from __future__ import annotations

import argparse
import json
import sys
import io
import re
import time
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, unquote

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

STORAGE_ROOT = Path(__file__).resolve().parents[3] / "docs_storage"

MAX_WORKERS = 10
DOWNLOAD_TIMEOUT = 20
MAX_FILE_SIZE_MB = 50

_stats_lock = threading.Lock()
_stats = {"downloaded": 0, "failed": 0, "skipped_exists": 0, "too_large": 0}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Phase B7: 防屏蔽链接本地下载")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=100, help="最大下载数")
    p.add_argument("--asset-id", type=int, default=0, help="只处理指定 asset_id")
    p.add_argument("--workers", type=int, default=10, help="并发数")
    p.add_argument("--storage-root", type=str, default=str(STORAGE_ROOT))
    p.add_argument("--min-relevance", type=float, default=0.4,
                   help="最低相关性分数阈值")
    p.add_argument("--retry-healthy", action="store_true",
                   help="也重新尝试 healthy 链接（用于修复文件名等）")
    return p


def sanitize_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', '_', name)


def extract_filename_from_url(url: str, content_type: str = "") -> str:
    """从 URL 或 Content-Type 提取合适的文件名"""
    parsed = urlparse(url)
    path = unquote(parsed.path)

    # 从 URL 路径提取文件名
    if path and "/" in path:
        filename = path.rsplit("/", 1)[-1]
        if filename and "." in filename:
            return filename

    # 从 Content-Disposition 中解析（在 download_one 中处理）
    # 如果是 GitHub，特殊处理
    if "github.com" in parsed.netloc:
        parts = path.strip("/").split("/")
        if len(parts) >= 2:
            return f"{parts[0]}_{parts[1]}.html"

    # Fallback: 使用域名+路径哈希
    import hashlib
    path_hash = hashlib.md5(url.encode()).hexdigest()[:8]

    if path:
        last_segment = path.strip("/").split("/")[-1]
        if last_segment:
            return f"{last_segment}_{path_hash}.html"

    domain = parsed.netloc.replace(".", "_")
    return f"{domain}_{path_hash}.html"


def extract_text_from_html(html: str) -> str:
    """从 HTML 中提取纯文本（移除标签，保留内容）"""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")

        # 移除 script 和 style 标签
        for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
            tag.decompose()

        # 获取文本
        text = soup.get_text(separator="\n", strip=True)

        # 清理多余空行
        lines = [line.strip() for line in text.splitlines()]
        lines = [line for line in lines if line]
        return "\n".join(lines)

    except ImportError:
        # bs4 不可用时的简单 fallback
        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', '\n', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r'[ \t]{2,}', ' ', text)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return "\n".join(lines)


def _make_download_session():
    """创建带完整浏览器头的下载 session"""
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "max-age=0",
        "Sec-Ch-Ua": '"Chromium";v="130", "Google Chrome";v="130"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    })

    retry = Retry(total=2, backoff_factor=1,
                  status_forcelist=[429, 500, 502, 503])
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)

    return s


def download_one(url_info: dict, storage_root: Path) -> dict:
    """下载单个链接"""
    url = url_info["url"]
    asset_id = url_info["asset_id"]
    symbol = url_info.get("coin_symbol", str(asset_id))
    safe_symbol = sanitize_name(symbol)
    asset_dir = storage_root / f"{safe_symbol}_{asset_id}" / "fallback_docs"
    asset_dir.mkdir(parents=True, exist_ok=True)

    result = {
        "url": url,
        "asset_id": asset_id,
        "symbol": symbol,
        "status": "failed",
        "local_path": None,
        "file_size": 0,
    }

    session = _make_download_session()

    try:
        resp = session.get(url, timeout=DOWNLOAD_TIMEOUT, allow_redirects=True,
                           stream=True)
        status = resp.status_code

        if status == 404 or status == 410:
            result["status"] = "dead"
            result["error"] = f"HTTP {status}"
            return result

        if status >= 400:
            # 带完整浏览器头的 GET 仍然失败
            result["status"] = "still_protected"
            result["error"] = f"HTTP {status}"
            return result

        # 检查 Content-Type
        content_type = (resp.headers.get("Content-Type") or "").lower()
        content_length = resp.headers.get("Content-Length")

        # 检查文件大小
        if content_length:
            size_bytes = int(content_length)
            if size_bytes > MAX_FILE_SIZE_MB * 1024 * 1024:
                result["status"] = "too_large"
                result["file_size"] = size_bytes
                with _stats_lock:
                    _stats["too_large"] += 1
                return result

        # 获取文件名
        filename = None
        content_disposition = resp.headers.get("Content-Disposition", "")
        if "filename=" in content_disposition:
            # 解析 Content-Disposition
            match = re.search(r'filename[*]?=["\']?([^"\';\s]+)', content_disposition)
            if match:
                filename = unquote(match.group(1))

        if not filename:
            filename = extract_filename_from_url(resp.url, content_type)

        # 确定扩展名
        if "application/pdf" in content_type:
            if not filename.endswith(".pdf"):
                filename += ".pdf"
        elif "text/html" in content_type:
            if not filename.endswith(".html") and not filename.endswith(".htm"):
                filename += ".html"
        elif "text/plain" in content_type:
            if not filename.endswith(".txt"):
                filename += ".txt"
        elif "application/json" in content_type:
            if not filename.endswith(".json"):
                filename += ".json"

        filename = sanitize_name(filename)
        filepath = asset_dir / filename

        # 下载文件内容（限制大小）
        downloaded = 0
        chunks = []
        for chunk in resp.iter_content(chunk_size=8192):
            chunks.append(chunk)
            downloaded += len(chunk)
            if downloaded > MAX_FILE_SIZE_MB * 1024 * 1024:
                result["status"] = "too_large"
                result["file_size"] = downloaded
                with _stats_lock:
                    _stats["too_large"] += 1
                return result

        content = b"".join(chunks)
        filepath.write_bytes(content)

        result["status"] = "downloaded"
        result["local_path"] = str(filepath)
        result["file_size"] = downloaded

        # 如果是 HTML，额外提取纯文本
        if "text/html" in content_type and downloaded < 10 * 1024 * 1024:
            try:
                html_text = content.decode("utf-8", errors="replace")
                text = extract_text_from_html(html_text)
                if text and len(text) > 200:  # 至少有点内容
                    txt_path = filepath.with_suffix(".txt")
                    txt_path.write_text(text, encoding="utf-8")
                    result["text_path"] = str(txt_path)
            except Exception:
                pass  # 文本提取失败不影响主流程

        with _stats_lock:
            _stats["downloaded"] += 1

    except Exception as e:
        err_str = str(e).lower()
        if "timeout" in err_str or "timed out" in err_str:
            result["status"] = "timeout"
        elif "connection" in err_str or "resolve" in err_str:
            result["status"] = "dead"
        elif "ssl" in err_str:
            result["status"] = "ssl_error"
        else:
            result["status"] = "failed"
        result["error"] = str(e)[:100]
        with _stats_lock:
            _stats["failed"] += 1

    return result


def update_db_status(conn, results: list[dict]):
    """更新 biz.research_url 的状态"""
    with conn.cursor() as cur:
        for r in results:
            if r["status"] in ("downloaded",):
                cur.execute("""
                    UPDATE biz.research_url
                    SET health_status = 'local_fallback',
                        ai_reason = COALESCE(ai_reason, '') || ' [已下载本地]',
                        updated_at = NOW()
                    WHERE asset_id = %s AND url = %s
                """, (r["asset_id"], r["url"]))
            elif r["status"] == "dead":
                cur.execute("""
                    UPDATE biz.research_url
                    SET health_status = 'dead',
                        updated_at = NOW()
                    WHERE asset_id = %s AND url = %s
                """, (r["asset_id"], r["url"]))
    conn.commit()


def get_protected_urls(conn, limit: int, asset_id: int, min_relevance: float,
                       retry_healthy: bool = False) -> list[dict]:
    """查询需要本地下载的链接"""
    import psycopg

    # 先检查表是否存在
    with conn.cursor() as cur:
        cur.execute("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'biz' AND table_name = 'research_url'
            )
        """)
        has_table = cur.fetchone()[0]

    if not has_table:
        print("[警告] biz.research_url 表不存在，请先运行 B5")
        return []

    if retry_healthy:
        health_cond = "health_status IN ('protected', 'healthy')"
    else:
        health_cond = "health_status = 'protected'"

    where = f"{health_cond} AND relevance_score >= %s"
    params = [min_relevance, limit]

    if asset_id:
        where += " AND asset_id = %s"
        params.insert(1, asset_id)

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(f"""
            SELECT url_id, asset_id, coin_symbol, coin_name, url,
                   category, relevance_score, health_status, doc_type,
                   file_name, mime_type
            FROM biz.research_url
            WHERE {where}
            ORDER BY relevance_score DESC
            LIMIT %s
        """, params)
        return [dict(row) for row in cur.fetchall()]


def main() -> int:
    args = build_parser().parse_args()

    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    settings = get_settings(require_database=True)
    storage_root = Path(args.storage_root)
    storage_root.mkdir(parents=True, exist_ok=True)

    with get_connection(settings.database_url) as conn:
        urls = get_protected_urls(
            conn, args.limit, args.asset_id, args.min_relevance, args.retry_healthy
        )

    if not urls:
        print("无 protected 链接需要处理")
        return 0

    print(f"待下载: {len(urls)} 个 protected 链接, {args.workers} workers")
    print(f"相关性阈值: >= {args.min_relevance}")

    if args.dry_run:
        for u in urls[:10]:
            print(f"  [{u.get('coin_symbol', '?')}] {u['url'][:100]} "
                  f"score={u.get('relevance_score', 0)}")
        return 0

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(download_one, u, storage_root): u for u in urls}

        for i, future in enumerate(as_completed(futures), 1):
            r = future.result()
            results.append(r)

            if i % 20 == 0 or i == len(urls):
                with _stats_lock:
                    s = dict(_stats)
                print(f"  [{i}/{len(urls)}] downloaded:{s['downloaded']} "
                      f"failed:{s['failed']} too_large:{s['too_large']}")

    # 更新 DB
    with get_connection(settings.database_url) as conn:
        update_db_status(conn, results)

    # 统计
    status_counts = {}
    for r in results:
        st = r["status"]
        status_counts[st] = status_counts.get(st, 0) + 1

    print(f"\n=== 完成 ===")
    for st, cnt in sorted(status_counts.items()):
        print(f"  {st}: {cnt}")
    print(f"输出目录: {storage_root}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
