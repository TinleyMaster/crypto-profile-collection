"""存量链接分类回填脚本（阶段1：规则 + 元数据，无 AI）。

对 biz.doc_source_entry 与 biz.doc_asset 中尚未分类（content_topics 为空）的记录，
用统一分类器 classify_link 回填 content_topics / classify_method / classify_confidence。

采用主键分页逐批读取，避免一次性加载全表。

用法：
    python backfill_classify_links.py --dry-run            # 预览，不写库
    python backfill_classify_links.py --limit 10000        # 只处理前 N 条
    python backfill_classify_links.py --upgrade-whitepaper # 额外把 docs 中白皮书页升级为 whitepaper_page
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

BATCH_SIZE = 2000


def _extract_url_key(source_code: str, discovered_from: str) -> str:
    """从 discovered_from 提取 CMC url_key（如 cmc_info.urls.website -> website）。"""
    if source_code == "cmc" and discovered_from and discovered_from.startswith("cmc_info.urls."):
        return discovered_from.rsplit(".", 1)[-1]
    return ""


def _classify_doc_source_entries(conn, limit: int, dry_run: bool, upgrade_whitepaper: bool) -> tuple[int, int]:
    from crypto_research.mapping.classify_link import classify_link

    cur = conn.cursor()
    last_id = 0
    total = 0
    upgraded = 0
    remaining = limit if limit > 0 else None

    while True:
        fetch = BATCH_SIZE
        if remaining is not None:
            fetch = min(BATCH_SIZE, remaining)

        cur.execute(
            """
            SELECT entry_id, entry_url, source_code, discovered_from, entry_type
            FROM biz.doc_source_entry
            WHERE content_topics IS NULL AND entry_id > %s
            ORDER BY entry_id
            LIMIT %s
            """,
            (last_id, fetch),
        )
        rows = cur.fetchall()
        if not rows:
            break

        updates = []
        for entry_id, entry_url, source_code, discovered_from, entry_type in rows:
            url_key = _extract_url_key(source_code, discovered_from)
            res = classify_link(entry_url, url_key=url_key, source_code=source_code)

            new_entry_type = entry_type
            if (
                upgrade_whitepaper
                and entry_type in ("docs", "docs_portal")
                and res["source_type"] == "whitepaper_page"
            ):
                new_entry_type = "whitepaper_page"

            updates.append((
                res["content_topics"],
                res["method"],
                res["confidence"],
                new_entry_type,
                entry_id,
            ))
            if new_entry_type != entry_type:
                upgraded += 1

        if not dry_run:
            cur.executemany(
                """
                UPDATE biz.doc_source_entry
                SET content_topics = %s, classify_method = %s, classify_confidence = %s, entry_type = %s
                WHERE entry_id = %s
                """,
                updates,
            )
            conn.commit()

        total += len(rows)
        last_id = rows[-1][0]
        if remaining is not None:
            remaining -= len(rows)
            if remaining <= 0:
                break

        if total % 10000 < BATCH_SIZE:
            print(f"  doc_source_entry 已处理 {total}", flush=True)

    cur.close()
    return total, upgraded


def _classify_doc_assets(conn, limit: int, dry_run: bool) -> int:
    from crypto_research.mapping.classify_link import classify_link

    cur = conn.cursor()
    last_id = 0
    total = 0
    remaining = limit if limit > 0 else None

    while True:
        fetch = BATCH_SIZE
        if remaining is not None:
            fetch = min(BATCH_SIZE, remaining)

        cur.execute(
            """
            SELECT doc_id, source_url, resolved_url, file_name, doc_type
            FROM biz.doc_asset
            WHERE content_topics IS NULL AND doc_id > %s
            ORDER BY doc_id
            LIMIT %s
            """,
            (last_id, fetch),
        )
        rows = cur.fetchall()
        if not rows:
            break

        updates = []
        for doc_id, source_url, resolved_url, file_name, doc_type in rows:
            url = resolved_url or source_url
            label = file_name or doc_type or ""
            res = classify_link(url, label=label)
            updates.append((res["content_topics"], res["method"], res["confidence"], doc_id))

        if not dry_run:
            cur.executemany(
                """
                UPDATE biz.doc_asset
                SET content_topics = %s, classify_method = %s, classify_confidence = %s
                WHERE doc_id = %s
                """,
                updates,
            )
            conn.commit()

        total += len(rows)
        last_id = rows[-1][0]
        if remaining is not None:
            remaining -= len(rows)
            if remaining <= 0:
                break

    cur.close()
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description="存量链接分类回填（规则 + 元数据）")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少条（0=全部）")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不写库")
    parser.add_argument("--upgrade-whitepaper", action="store_true", help="把 docs 中白皮书页升级为 whitepaper_page")
    args = parser.parse_args()

    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    settings = get_settings(require_database=True)

    with get_connection(settings.database_url) as conn:
        print("开始回填 doc_source_entry ...", flush=True)
        n_entries, n_upgraded = _classify_doc_source_entries(
            conn, args.limit, args.dry_run, args.upgrade_whitepaper
        )
        print("开始回填 doc_asset ...", flush=True)
        n_assets = _classify_doc_assets(conn, args.limit, args.dry_run)

    tag = "[dry-run] " if args.dry_run else ""
    print(
        f"\n{tag}完成：doc_source_entry {n_entries} 条"
        + (f"（whitepaper_page 升级 {n_upgraded} 条）" if n_upgraded else "")
        + f"，doc_asset {n_assets} 条。",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
