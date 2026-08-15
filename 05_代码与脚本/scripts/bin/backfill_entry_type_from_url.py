"""存量 entry_type（来源类型）重分类 —— SQL 批量版。

背景：早期 B2 深爬的 infer_doc_entry_type 兜底返回 docs，导致大量官网首页/普通页面
被误标为 docs/docs_portal。本脚本用 SQL 精准定位「无文档主题 + URL 无文档/社交特征」
的爬取产物，批量修正为 official_website。

安全约束：
1. 只处理 discovered_from LIKE 'deep_crawl:%' / 'spa_browser_crawl:%'（爬取产物），
   不动 cmc/cg/dl/dexscreener/binance/manual 种子。
2. 只处理 content_topics 无「明确文档主题」的记录（other / 空 / NULL）。
3. URL 不含任何文档/社交关键词，此类 URL 经统一分类器 infer_source_type 推断
   会兜底为 official_website，故可直接批量修正。

用法：
    python backfill_entry_type_from_url.py --dry-run   # 预览，不写库
    python backfill_entry_type_from_url.py             # 实际执行
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(line_buffering=True)

import psycopg

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

# URL 含任一关键词，说明是文档/社交/代码托管链接，保留其类型，不改为官网
DOC_SOCIAL_KW = (
    r"(whitepaper|litepaper|lightpaper|yellowpaper|white-paper|lite-paper"
    r"|tokenomics|audit|docs\.|documentation|gitbook|wiki"
    r"|github|medium|twitter|telegram|reddit|facebook|discord"
    r"|\.pdf|\.md)"
)

WHERE = f"""
    (discovered_from LIKE 'deep_crawl:%%' OR discovered_from LIKE 'spa_browser_crawl:%%')
    AND entry_type IN ('docs', 'docs_portal')
    AND (content_topics IS NULL OR content_topics = ARRAY['other'] OR content_topics = '{{}}'::text[])
    AND entry_url !~ '{DOC_SOCIAL_KW}'
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="存量 entry_type（来源类型）重分类")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不写库")
    args = parser.parse_args()

    settings = get_settings(require_database=True)

    with get_connection(settings.database_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(f"SELECT count(*) AS n FROM biz.doc_source_entry WHERE {WHERE}")
            total = cur.fetchone()["n"]
            print(f"待修正总数: {total:,}")
            print(f"模式: {'DRY-RUN 预览' if args.dry_run else '执行修正'}\n")

            cur.execute(
                f"""
                SELECT entry_type, count(*) AS n
                FROM biz.doc_source_entry
                WHERE {WHERE}
                GROUP BY entry_type
                ORDER BY n DESC
                """
            )
            print("按原类型分布：")
            for r in cur.fetchall():
                print(f"  {r['entry_type']:<16} {r['n']:,}")

            print("\n预览样本（前 20 条）：")
            cur.execute(
                f"""
                SELECT entry_url, entry_type
                FROM biz.doc_source_entry
                WHERE {WHERE}
                ORDER BY entry_id
                LIMIT 20
                """
            )
            for r in cur.fetchall():
                print(f"  {r['entry_type']:<14} {r['entry_url'][:72]}")

            if not args.dry_run:
                cur.execute(
                    f"""
                    UPDATE biz.doc_source_entry
                    SET entry_type = 'official_website', updated_at = NOW()
                    WHERE {WHERE}
                    """
                )
                conn.commit()
                print(f"\n已修正 {cur.rowcount:,} 条 → official_website")
            else:
                print("\n[DRY-RUN] 未写入数据库")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
