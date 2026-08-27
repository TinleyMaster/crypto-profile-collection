"""清理 biz.research_thesis 重复行。

按 (asset_id, source_notebook_id) 分组，保留 updated_at 最新的一条，
删除其余重复记录，并补加唯一约束防止未来重复插入。

用法：
    python cleanup_research_thesis_duplicates.py --dry-run   # 预览
    python cleanup_research_thesis_duplicates.py             # 执行清理
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


def main() -> int:
    parser = argparse.ArgumentParser(description="清理 research_thesis 重复行")
    parser.add_argument("--dry-run", action="store_true", help="预览，不写入")
    args = parser.parse_args()

    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    settings = get_settings(require_database=True)

    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            # 查看重复情况
            cur.execute("""
                SELECT asset_id, source_notebook_id, COUNT(*) AS cnt
                FROM biz.research_thesis
                GROUP BY asset_id, source_notebook_id
                HAVING COUNT(*) > 1
                ORDER BY cnt DESC
            """)
            dupes = cur.fetchall()
            print(f"重复组合数: {len(dupes)}")
            for asset_id, nb_id, cnt in dupes[:20]:
                print(f"  asset_id={asset_id} notebook_id={nb_id} 重复 {cnt} 条")

            if args.dry_run:
                print("\n[DRY-RUN] 未写入数据库")
                return 0

            # 删除重复，保留最新
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

            # 添加唯一约束
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

            # 最终统计
            cur.execute("""
                SELECT COUNT(*) AS rows, COUNT(DISTINCT asset_id) AS assets
                FROM biz.research_thesis
            """)
            rows, assets = cur.fetchone()

        conn.commit()

    print(f"\n已删除 {deleted} 条重复，当前 {rows} 行 / {assets} 资产")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
