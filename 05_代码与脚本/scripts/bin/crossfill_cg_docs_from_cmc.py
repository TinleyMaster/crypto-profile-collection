from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))


BATCH_UPSERT_DOC = """
INSERT INTO biz.doc_source_entry (
    entity_type, asset_id, protocol_id, source_code,
    entry_type, entry_url, discovered_from, is_primary
) VALUES {}
ON CONFLICT (entity_type, COALESCE(asset_id, -1), COALESCE(protocol_id, -1), entry_url) DO UPDATE SET
    source_code = EXCLUDED.source_code,
    updated_at = NOW()
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Cross-fill CG doc_source_entry from CMC entries for same asset_id."
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=5000)
    return p


def main() -> int:
    args = build_parser().parse_args()

    import psycopg
    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    settings = get_settings(require_database=True)

    with get_connection(settings.database_url) as conn:
        # Find CMC doc entries whose asset_id is also mapped to CG, but no CG doc entry exists yet
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """
                SELECT dse.entity_type, dse.asset_id, dse.entry_type, dse.entry_url,
                       dse.discovered_from, dse.is_primary
                FROM biz.doc_source_entry dse
                INNER JOIN core.asset_source_map cg
                    ON cg.asset_id = dse.asset_id AND cg.source_code = 'cg'
                WHERE dse.source_code = 'cmc'
                  AND NOT EXISTS (
                      SELECT 1 FROM biz.doc_source_entry cg_dse
                      WHERE cg_dse.asset_id = dse.asset_id
                        AND cg_dse.source_code = 'cg'
                        AND cg_dse.entry_url = dse.entry_url
                  )
                LIMIT %s
            """,
                (args.limit,),
            )
            rows = [dict(row) for row in cur.fetchall()]

        if not rows:
            print(json.dumps({"status": "noop"}, ensure_ascii=False))
            return 0

        # Dedup by (asset_id, entry_url)
        seen = set()
        values = []
        params = []
        for r in rows:
            key = (r["asset_id"], r["entry_url"])
            if key in seen:
                continue
            seen.add(key)
            values.append("(%s, %s, %s, %s, %s, %s, %s, %s)")
            params.extend(
                [
                    r["entity_type"],
                    r["asset_id"],
                    None,
                    "cg",
                    r["entry_type"],
                    r["entry_url"],
                    "crossfill_from_cmc",
                    r["is_primary"],
                ]
            )

        if args.dry_run:
            print(
                json.dumps(
                    {
                        "mode": "dry_run",
                        "cmc_rows": len(rows),
                        "deduped": len(values),
                    },
                    ensure_ascii=False,
                )
            )
            return 0

        if values:
            sql = BATCH_UPSERT_DOC.format(", ".join(values))
            with conn.cursor() as cur:
                cur.execute(sql, params)

        print(
            json.dumps(
                {
                    "status": "success",
                    "cmc_rows": len(rows),
                    "cg_entries": len(values),
                },
                ensure_ascii=False,
            )
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
