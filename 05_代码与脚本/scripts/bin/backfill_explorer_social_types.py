"""存量 entry_type 规则重分类：other → explorer / social（非 AI）。

背景：区块浏览器（etherscan/solscan/arkm 等）与社交媒体（instagram/linkedin/
youtube 等）在 taxonomy 中缺少对应来源类型，导致大量链接被判为 other（method=default
或 url_key），需要后续 AI 补分类。这些链接靠域名即可精确判断，无需抓正文/AI。

本脚本复用统一分类器 infer_source_type 的域名规则，对 entry_type='other' 的存量记录
重跑域名判定，命中 explorer/social 的批量修正 entry_type / classify_method / confidence。
content_topics 保持不变（explorer/social 不是投研资料主题，本就应为 other）。

用法：
    python backfill_explorer_social_types.py --dry-run   # 预览，不写库
    python backfill_explorer_social_types.py             # 实际执行
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(line_buffering=True)

import psycopg
from psycopg.errors import AdminShutdown

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection
from crypto_research.mapping.classify_link import infer_source_type


def _db_retry(database_url, fn, retries=8, delay=5):
    """在可重试连接下执行 fn(conn)，PG 周期性重启时自动重连重试。"""
    last_err = None
    for i in range(retries):
        try:
            with get_connection(database_url) as conn:
                return fn(conn)
        except (psycopg.OperationalError, AdminShutdown) as e:
            last_err = e
            print(
                f"  [WARN] DB 操作失败({i + 1}/{retries}): {str(e)[:70]}，{delay}s 后重试...",
                flush=True,
            )
            time.sleep(delay)
    raise last_err


def main() -> int:
    parser = argparse.ArgumentParser(description="存量 entry_type 规则重分类：other → explorer/social")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不写库")
    parser.add_argument("--batch-size", type=int, default=2000, help="每批读取条数")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    db_url = settings.database_url

    def _count(conn):
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM biz.doc_source_entry WHERE entry_type = 'other'")
            return cur.fetchone()[0]

    total = _db_retry(db_url, _count)
    print(f"待检查 entry_type='other' 总数: {total:,}")
    print(f"模式: {'DRY-RUN 预览' if args.dry_run else '执行修正'}\n")

    processed = 0
    matched_explorer = 0
    matched_social = 0
    last_id = 0
    samples: list[tuple[str, str]] = []

    while True:
        def _select(conn):
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute(
                    """
                    SELECT entry_id, entry_url
                    FROM biz.doc_source_entry
                    WHERE entry_type = 'other' AND entry_id > %s
                    ORDER BY entry_id
                    LIMIT %s
                    """,
                    (last_id, args.batch_size),
                )
                return [dict(r) for r in cur.fetchall()]

        rows = _db_retry(db_url, _select)
        if not rows:
            break

        explorer_ids: list[int] = []
        social_ids: list[int] = []
        for r in rows:
            stype = infer_source_type(r["entry_url"])
            if stype == "explorer":
                explorer_ids.append(r["entry_id"])
                if len(samples) < 20:
                    samples.append((stype, r["entry_url"]))
            elif stype == "social":
                social_ids.append(r["entry_id"])
                if len(samples) < 20:
                    samples.append((stype, r["entry_url"]))

        matched_explorer += len(explorer_ids)
        matched_social += len(social_ids)

        if not args.dry_run and (explorer_ids or social_ids):
            def _write(conn):
                with conn.cursor() as cur:
                    if explorer_ids:
                        cur.execute(
                            """
                            UPDATE biz.doc_source_entry
                            SET entry_type = 'explorer',
                                classify_method = 'domain',
                                classify_confidence = 0.98,
                                updated_at = NOW()
                            WHERE entry_id = ANY(%s)
                            """,
                            (explorer_ids,),
                        )
                    if social_ids:
                        cur.execute(
                            """
                            UPDATE biz.doc_source_entry
                            SET entry_type = 'social',
                                classify_method = 'domain',
                                classify_confidence = 0.98,
                                updated_at = NOW()
                            WHERE entry_id = ANY(%s)
                            """,
                            (social_ids,),
                        )

            _db_retry(db_url, _write)

        processed += len(rows)
        last_id = rows[-1]["entry_id"]
        print(
            f"[{processed:,}/{total:,}] explorer:{matched_explorer:,} social:{matched_social:,}",
            flush=True,
        )

    print()
    print("=" * 60)
    print(f"命中 explorer: {matched_explorer:,}")
    print(f"命中 social:  {matched_social:,}")
    print(f"剩余 other（未命中域名规则）: {total - matched_explorer - matched_social:,}")
    print("\n样本：")
    for stype, url in samples:
        print(f"  {stype:<10} {url[:80]}")
    if args.dry_run:
        print("\n[DRY-RUN] 未写入数据库")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
