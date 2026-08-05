"""
B2 噪声 AI 清理：用 AI 批量判断深度爬取发现的链接是否与加密货币投研相关，
无关的直接从 biz.doc_source_entry 删除，相关的标记 ai_noise_checked_at
避免下次重复判断。

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

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

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
    p.add_argument("--batch-size", type=int, default=100, help="AI 每批条数")
    p.add_argument("--rpm", type=int, default=300, help="AI 调用速率限制（次/分钟）")
    p.add_argument(
        "--skip-rule-delete",
        action="store_true",
        help="跳过规则直删 step（paperdigest 等纯噪声域名），只做 AI 筛选",
    )
    p.add_argument(
        "--source",
        type=str,
        default="all",
        choices=list(SUSPICIOUS_SOURCES.keys()) + ["all", "remaining"],
        help="只处理某类可疑来源，默认 all。remaining=所有未检查的 deep_crawl 条目",
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

    if source_filter == "remaining":
        # 所有未检查的 deep_crawl 条目（不限域名）
        where = "discovered_from LIKE 'deep_crawl:%%' AND ai_noise_checked_at IS NULL"
        params: list = []
    elif source_filter == "all":
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

    # 只处理 deep_crawl 来源的（原始入口不动），跳过已标记的
    where = f"({where}) AND discovered_from LIKE 'deep_crawl:%%' AND ai_noise_checked_at IS NULL"

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
        # ── 自动迁移：确保 ai_noise_checked_at 列存在 ──
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE biz.doc_source_entry "
                "ADD COLUMN IF NOT EXISTS ai_noise_checked_at TIMESTAMPTZ DEFAULT NULL"
            )
        conn.commit()

        # ── Step 1: 规则直删（纯噪声域名，不需要 AI） ──
        rule_deleted = 0
        if not args.skip_rule_delete:
            rule_domains = {
                "paperdigest": "%paperdigest.org%",
                "arxiv": "%arxiv.org%",
                "papers-nips": "%papers.nips.cc%",
                "neurips": "%neurips.cc%",
                "springer": "%link.springer.com%",
                "researchgate": "%researchgate.net%",
                # ── 2026-08 新增：关键词误匹配 + 非加密噪声域名 ──
                "papermc": "%papermc.io%",
                "paperspace": "%paperspace.com%",
                "ijcai": "%ijcai.org%",
                "digitalocean": "%digitalocean.com%",
                "powershellgallery": "%powershellgallery.com%",
                "rubydoc": "%rubydoc.info%",
                "rubygems": "%rubygems.org%",
                "openai": "%developers.openai.com%",
                "linkedin": "%linkedin.com%",
                "facebook": "%facebook.com%",
                "t-me": "%t.me%",
                "telegram-me": "%telegram.me%",
                "web-archive": "%web.archive.org%",
                "dropbox": "%dropbox.com%",
                "webflow-cdn": "%cdn.prod.website-files.com%",
                "certora-cdn": "%certora.cdn.prismic.io%",
            }
            for label, pattern in rule_domains.items():
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT count(*) FROM biz.doc_source_entry "
                        "WHERE entry_url LIKE %s AND discovered_from LIKE 'deep_crawl:%%'",
                        (pattern,),
                    )
                    cnt = cur.fetchone()[0]
                if cnt == 0:
                    continue
                if args.execute:
                    with conn.cursor() as cur:
                        cur.execute(
                            "DELETE FROM biz.doc_source_entry "
                            "WHERE entry_url LIKE %s AND discovered_from LIKE 'deep_crawl:%%'",
                            (pattern,),
                        )
                    conn.commit()
                print(f"  [规则直删] {label}: {cnt} 条{' (dry-run)' if not args.execute else ' ✓'}")
                rule_deleted += cnt

            if rule_deleted > 0:
                print(f"  规则直删合计: {rule_deleted} 条\n")

        # ── Step 2: AI 精筛（剩余可疑条目） ──
        entries = get_suspicious_entries(conn, args.source, args.limit)
        print(f"获取到 {len(entries)} 条可疑条目（AI 判断）")
        if not entries:
            print("没有可处理的条目")
            return 0

        total = len(entries)
        noise_count = 0
        keep_count = 0
        error_count = 0
        deleted_count = 0
        marked_count = 0
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

            # 第一批打印原始响应样例，方便排查问题
            if batch_num == 1:
                import json as _json
                if hasattr(llm, '_last_full_response') and llm._last_full_response:
                    full_sample = _json.dumps(llm._last_full_response, ensure_ascii=False)[:1500]
                    print(f"  [DEBUG] API完整响应: {full_sample}")
                if hasattr(llm, '_last_diag') and llm._last_diag:
                    print(f"  [DEBUG] 诊断信息: {_json.dumps(llm._last_diag, ensure_ascii=False)}")
                if hasattr(llm, '_last_raw_response'):
                    raw_sample = (llm._last_raw_response or "")[:300]
                    print(f"  [DEBUG] 提取文本: '{raw_sample}'")

            # 统计本批结果
            batch_noise = 0
            batch_keep = 0
            batch_parse_fail = 0
            noise_ids: list[int] = []
            keep_ids: list[int] = []

            for entry, result in zip(batch, results):
                entry_id = entry["entry_id"]
                score = result["score"]
                relevant = result["relevant"]
                reason = result.get("reason", "")

                # 解析失败的单独统计（score=0.5 + 理由含"失败"字样）
                if "失败" in reason and score == 0.5:
                    batch_parse_fail += 1

                if not relevant or score < args.threshold:
                    batch_noise += 1
                    noise_ids.append(entry_id)
                else:
                    batch_keep += 1
                    keep_ids.append(entry_id)

            noise_count += batch_noise
            keep_count += batch_keep
            if batch_parse_fail > 0:
                error_count += batch_parse_fail

            # 执行删除
            if args.execute and noise_ids:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM biz.doc_source_entry WHERE entry_id = ANY(%s)",
                        (noise_ids,),
                    )
                conn.commit()
                deleted_count += len(noise_ids)

            # 标记已检查的"投研相关"条目，下次跳过
            if args.execute and keep_ids:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE biz.doc_source_entry SET ai_noise_checked_at = NOW() "
                        "WHERE entry_id = ANY(%s)",
                        (keep_ids,),
                    )
                conn.commit()
                marked_count += len(keep_ids)

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
        if marked_count > 0:
            print(f"  已标记:      {marked_count:>8,} (下次跳过)")
    else:
        print(f"  (DRY RUN) 使用 --execute 执行删除")
    print(f"  耗时:        {int(time.time()-start_time)}s")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
