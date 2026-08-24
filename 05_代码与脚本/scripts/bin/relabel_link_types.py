"""文档链接类型批量重标工具。

支持：
    --type github          重标所有 github.com 链接为 github，并回滚 github.io 误标
    --type whitepaper      重标所有 whitepaper/litepaper 链接为 whitepaper_page
    --cleanup-thesis-duplicates  清理 research_thesis 重复行（保留最新）

用法：
    python relabel_link_types.py --type github
    python relabel_link_types.py --type whitepaper
    python relabel_link_types.py --cleanup-thesis-duplicates
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(line_buffering=True)


def relabel_github(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE biz.doc_source_entry
            SET entry_type = 'github', updated_at = NOW()
            WHERE entry_url ILIKE '%github.com%'
              AND entry_type <> 'github'
        """)
        relabeled = cur.rowcount

        cur.execute("""
            UPDATE biz.doc_source_entry
            SET entry_type = 'other', updated_at = NOW()
            WHERE entry_type = 'github'
              AND entry_url NOT ILIKE '%github.com%'
        """)
        rollback = cur.rowcount

        cur.execute("""
            SELECT COUNT(*) FILTER (WHERE entry_type = 'github') AS github_count,
                   COUNT(*) FILTER (WHERE entry_url ILIKE '%github.com%') AS url_with_github
            FROM biz.doc_source_entry
        """)
        total, with_url = cur.fetchone()
    return {
        "relabeled": relabeled,
        "rollback": rollback,
        "github_count": total,
        "url_with_github": with_url,
        "coverage_pct": round(float(total or 0) / float(with_url or 1) * 100, 1),
    }


def relabel_whitepaper(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE biz.doc_source_entry
            SET entry_type = 'whitepaper_page', updated_at = NOW()
            WHERE entry_type <> 'whitepaper_page'
              AND (
                  entry_url ILIKE '%whitepaper%'
                  OR entry_url ILIKE '%litepaper%'
                  OR entry_url ILIKE '%white-paper%'
                  OR entry_url ILIKE '%lite-paper%'
              )
        """)
        relabeled = cur.rowcount

        cur.execute("""
            SELECT COUNT(*) FILTER (WHERE entry_type = 'whitepaper_page') AS wp_count,
                   COUNT(*) FILTER (WHERE entry_url ILIKE '%whitepaper%' OR entry_url ILIKE '%litepaper%') AS url_with_wp
            FROM biz.doc_source_entry
        """)
        total, with_url = cur.fetchone()
    return {
        "relabeled": relabeled,
        "whitepaper_count": total,
        "url_with_wp": with_url,
    }


def cleanup_thesis_duplicates(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("""
            DELETE FROM biz.research_thesis
            WHERE thesis_id IN (
                SELECT thesis_id
                FROM (
                    SELECT thesis_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY asset_id, source_notebook_id
                               ORDER BY updated_at DESC, thesis_id DESC
                           ) AS rn
                    FROM biz.research_thesis
                ) t
                WHERE rn > 1
            )
        """)
        deleted = cur.rowcount

        cur.execute("""
            SELECT COUNT(*) AS rows, COUNT(DISTINCT asset_id) AS assets
            FROM biz.research_thesis
        """)
        rows, assets = cur.fetchone()

        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'uq_research_thesis_asset_notebook'
                ) THEN
                    ALTER TABLE biz.research_thesis
                    ADD CONSTRAINT uq_research_thesis_asset_notebook
                    UNIQUE (asset_id, source_notebook_id);
                END IF;
            END $$;
        """)
    return {"deleted": deleted, "rows": rows, "assets": assets}


def main() -> int:
    parser = argparse.ArgumentParser(description="文档链接类型批量重标")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--type", choices=["github", "whitepaper"], help="重标类型")
    group.add_argument("--cleanup-thesis-duplicates", action="store_true", help="清理 thesis 重复行")
    args = parser.parse_args()

    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        if args.type == "github":
            result = relabel_github(conn)
            print(json.dumps({"status": "ok", "type": "github", **result}, ensure_ascii=False, indent=2))
        elif args.type == "whitepaper":
            result = relabel_whitepaper(conn)
            print(json.dumps({"status": "ok", "type": "whitepaper", **result}, ensure_ascii=False, indent=2))
        elif args.cleanup_thesis_duplicates:
            result = cleanup_thesis_duplicates(conn)
            print(json.dumps({"status": "ok", "type": "cleanup_thesis_duplicates", **result}, ensure_ascii=False, indent=2))
        conn.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
