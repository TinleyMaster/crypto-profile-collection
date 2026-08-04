"""
B2 噪声 AI 清理：用 AI 批量判断深度爬取发现的链接是否与加密货币投研相关，
无关的直接从 biz.doc_source_entry 删除。

只针对"可疑"的链接（paperdigest、学术站、GitHub blob 源码等），
不处理明确的原始入口（官网、CMC 原始链接等）。

用法：
  python phase_b2_ai_noise_clean.py --limit 100          # dry-run，只看结果
  python phase_b2_ai_noise_clean.py --limit 100 --execute  # 实际删除
  python phase_b2_ai_noise_clean.py --source paperdigest   # 只处理 paperdigest 来源
  python phase_b2_ai_noise_clean.py --source github-blob   # 只处理 GitHub blob 源码
"""
from __future__ import annotations

import argparse
import json
import sys
import io
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

# 可疑来源域名（需要 AI 判断的）
SUSPICIOUS_SOURCES = {
    "paperdigest": "%paperdigest.org%",
    "papers-nips": "%papers.nips.cc%",
    "neurips": "%neurips.cc%",
    "springer": "%link.springer.com%",
    "arxiv": "%arxiv.org%",
    "researchgate": "%researchgate.net%",
    "github-blob": "%github.com%/blob/%",
    "github-tree": "%github.com%/tree/%",
}

# GitHub blob 源码文件扩展名（高概率噪声，但需 AI 确认）
GITHUB_CODE_EXT_PATTERNS = [
    "%.py", "%.js", "%.ts", "%.jsx", "%.tsx", "%.sol",
    "%.rs", "%.go", "%.java", "%.c", "%.cpp", "%.h", "%.hpp",
    "%.cs", "%.rb", "%.php", "%.swift", "%.kt", "%.scala",
    "%.lua", "%.r", "%.m", "%.mm",
    "%.json", "%.yaml", "%.yml", "%.toml", "%.xml", "%.ini", "%.cfg",
    "%.sh", "%.bash", "%.zsh", "%.ps1", "%.bat", "%.cmd",
    "%.svg", "%.png", "%.jpg", "%.jpeg", "%.gif", "%.ico", "%.webp",
    "%.lock", "%.css", "%.scss", "%.less", "%.sass",
    "%.wasm", "%.bin", "%.so", "%.dll", "%.dylib", "%.exe",
    "%.ipynb", "%.csv", "%.tsv",
]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="B2 噪声 AI 清理")
    p.add_argument("--limit", type=int, default=100, help="最多处理条数")
    p.add_argument("--batch-size", type=int, default=40, help="AI 每批条数")
    p.add_argument("--rpm", type=int, default=60, help="AI 调用速率限制（次/分钟）")
    p.add_argument(
        "--source",
        type=str,
        default="all",
        choices=list(SUSPICIOUS_SOURCES.keys()) + ["all"],
        help="只处理某类可疑来源，默认 all",
    )
    p.add_argument(
        "--execute",
        action="store_true",
        help="实际执行删除（默认 dry-run，只预览）",
    )
    p.add_argument(
        "--threshold",
        type=float,
        default=0.3,
        help="低于此分数判定为噪声并删除，默认 0.3",
    )
    return p


def get_suspicious_entries(conn, source_filter: str, limit: int) -> list[dict]:
    """获取可疑的 doc_source_entry 条目。"""
    import psycopg

    if source_filter == "all":
        # 所有可疑来源取并集
        clauses = []
        params: list = []

        # 学术/聚合站域名
        for key, pattern in SUSPICIOUS_SOURCES.items():
            if key in ("github-blob", "github-tree"):
                continue
            clauses.append("entry_url LIKE %s")
            params.append(pattern)

        # GitHub blob 源码文件
        ext_clauses = []
        for ext in GITHUB_CODE_EXT_PATTERNS:
            ext_clauses.append("entry_url LIKE %s")
            params.append(ext)
        clauses.append(f"(entry_url LIKE '%%github.com%%/blob/%%' AND ({' OR '.join(ext_clauses)}))")

        where = " OR ".join(clauses)
    elif source_filter in ("github-blob",):
        ext_clauses = " OR ".join(["entry_url LIKE %s"] * len(GITHUB_CODE_EXT_PATTERNS))
        where = f"entry_url LIKE '%%github.com%%/blob/%%' AND ({ext_clauses})"
        params = list(GITHUB_CODE_EXT_PATTERNS)
    elif source_filter == "github-tree":
        where = "entry_url LIKE '%%github.com%%/tree/%%'"
        params = []
    else:
        pattern = SUSPICIOUS_SOURCES[source_filter]
        where = "entry_url LIKE %s"
        params = [pattern]

    # 只处理 deep_crawl 来源的（原始入口不动）
    where = f"({where}) AND discovered_from LIKE 'deep_crawl:%%'"

    sql = f"""
        SELECT entry_id, asset_id, entry_url, entry_type, discovered_from
        FROM biz.doc_source_entry
        WHERE {where}
        ORDER BY entry_id
        LIMIT %s
    """
    params.append(limit)

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def extract_title(url: str, discovered_from: str) -> str:
    """从 URL 和来源中提取标题信息（用于 AI 判断）。"""
    from urllib.parse import urlparse, unquote

    parts = urlparse(url)
    # 取路径最后一段作为标题提示
    path = unquote(parts.path)
    filename = path.rsplit("/", 1)[-1] if "/" in path else path
    # 去掉扩展名
    if "." in filename:
        filename = filename.rsplit(".", 1)[0]
    filename = filename.replace("-", " ").replace("_", " ").strip()

    # 域名信息
    domain = parts.netloc

    # discovered_from 来源域名
    source_domain = ""
    if discovered_from and discovered_from.startswith("deep_crawl:"):
        source_url = discovered_from[len("deep_crawl:") :]
        try:
            source_domain = urlparse(source_url).netloc
        except Exception:
            pass

    return f"文件名: {filename[:100]} | 域名: {domain} | 来源页面: {source_domain}"


def main() -> int:
    args = build_parser().parse_args()

    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection
    from crypto_research.clients.llm_client import LLMClient

    settings = get_settings(require_database=True)
    llm = LLMClient(settings, rpm=args.rpm)

    if not llm.is_available():
        print("ERROR: 未配置 LLM。请设置 ARK_API_KEY 或 OPENAI_API_KEY。")
        return 1

    print(f"提供商: {llm.provider} | 模型: {llm.model}")
    print(f"模式: {'执行删除' if args.execute else 'DRY-RUN 预览'}")
    print(f"来源过滤: {args.source}")
    print(f"阈值: {args.threshold} (低于此值判定为噪声)")
    print()

    with get_connection(settings.database_url) as conn:
        entries = get_suspicious_entries(conn, args.source, args.limit)
        print(f"获取到 {len(entries)} 条可疑条目")
        if not entries:
            print("没有可处理的条目")
            return 0

        total = len(entries)
        noise_count = 0
        keep_count = 0
        error_count = 0
        deleted_count = 0
        batch_size = args.batch_size

        start_time = time.time()

        for batch_start in range(0, total, batch_size):
            batch = entries[batch_start : batch_start + batch_size]
            batch_num = batch_start // batch_size + 1
            total_batches = (total + batch_size - 1) // batch_size

            # 构造 AI 判断输入
            llm_items = [
                {
                    "id": str(e["entry_id"]),
                    "url": e["entry_url"],
                    "title": extract_title(e["entry_url"], e.get("discovered_from", "")),
                }
                for e in batch
            ]

            try:
                results = llm.batch_check_crypto_relevance(llm_items)
            except Exception as e:
                print(f"  [Batch {batch_num}/{total_batches}] AI 调用失败: {str(e)[:100]}")
                error_count += len(batch)
                continue

            # 统计本批结果
            batch_noise = 0
            batch_keep = 0
            noise_ids: list[int] = []

            for entry, result in zip(batch, results):
                entry_id = entry["entry_id"]
                score = result["score"]
                relevant = result["relevant"]

                if not relevant or score < args.threshold:
                    batch_noise += 1
                    noise_ids.append(entry_id)
                else:
                    batch_keep += 1

            noise_count += batch_noise
            keep_count += batch_keep

            # 执行删除
            if args.execute and noise_ids:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM biz.doc_source_entry WHERE entry_id = ANY(%s)",
                        (noise_ids,),
                    )
                conn.commit()
                deleted_count += len(noise_ids)

            # 进度
            elapsed = time.time() - start_time
            progress = batch_start + len(batch)
            pct = progress / total * 100
            rate = progress / elapsed if elapsed > 0 else 0
            eta = int((total - progress) / rate) if rate > 0 else 0
            eta_str = f"{eta // 60}m {eta % 60}s" if eta >= 60 else f"{eta}s"
            rate_str = f"{rate:.0f}/min" if rate >= 1 else f"{rate*60:.0f}/h"

            print(
                f"[{progress}/{total} {pct:.0f}%] "
                f"噪声:{noise_count} 保留:{keep_count} 失败:{error_count} "
                f"| {rate_str} ETA:{eta_str}"
            )

            # 每批打印几个样例
            if batch_noise > 0 and batch_num <= 3:
                sample = [
                    (e, r)
                    for e, r in zip(batch, results)
                    if not r["relevant"] or r["score"] < args.threshold
                ][:3]
                for e, r in sample:
                    short_url = e["entry_url"][:80]
                    print(f"  ✗ [{r['score']:.2f}] {short_url}...")
                    print(f"    理由: {r['reason'][:60]}")

    # 汇总
    print()
    print("=" * 60)
    print("  汇总")
    print("=" * 60)
    print(f"  处理总数:    {total:>8,}")
    print(f"  判定噪声:    {noise_count:>8,}")
    print(f"  判定保留:    {keep_count:>8,}")
    print(f"  调用失败:    {error_count:>8,}")
    if args.execute:
        print(f"  已删除:      {deleted_count:>8,}")
    else:
        print(f"  (DRY RUN) 使用 --execute 执行删除")
    print(f"  耗时:        {int(time.time()-start_time)}s")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
