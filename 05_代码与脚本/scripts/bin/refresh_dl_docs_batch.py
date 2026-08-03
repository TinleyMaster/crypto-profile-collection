from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Batch refresh DL doc_source_entry.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=2000)
    return parser


BATCH_UPSERT_DOC = """
INSERT INTO biz.doc_source_entry (
    entity_type, asset_id, protocol_id, source_code,
    entry_type, entry_url, discovered_from, is_primary
) VALUES {}
ON CONFLICT (entity_type, COALESCE(asset_id, -1), COALESCE(protocol_id, -1), entry_url) DO UPDATE SET
    source_code = EXCLUDED.source_code,
    entry_type = EXCLUDED.entry_type,
    discovered_from = EXCLUDED.discovered_from,
    is_primary = EXCLUDED.is_primary,
    updated_at = NOW()
"""


def main() -> int:
    args = build_parser().parse_args()

    import psycopg
    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection
    from crypto_research.db.upsert import load_sql

    settings = get_settings(require_database=True)
    select_sql = load_sql("src_dl/select_dl_doc_source_candidates.sql")

    with get_connection(settings.database_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(select_sql, (args.limit,))
            rows = [dict(row) for row in cur.fetchall()]

        if not rows:
            print(json.dumps({"status": "noop"}, ensure_ascii=False))
            return 0

        # Build entries with dedup by (asset_id, entry_url)
        seen = set()
        values = []
        params = []
        for row in rows:
            url = row.get("url")
            if url and isinstance(url, str) and url.strip():
                key = (row["asset_id"], url.strip())
                if key not in seen:
                    seen.add(key)
                    values.append("(%s, %s, %s, %s, %s, %s, %s, %s)")
                    params.extend(
                        [
                            "asset",
                            row["asset_id"],
                            None,
                            "dl",
                            "official_website",
                            url.strip(),
                            "dl_info.url",
                            True,
                        ]
                    )
            twitter = row.get("twitter")
            if twitter and isinstance(twitter, str) and twitter.strip():
                twitter_url = (
                    f"https://twitter.com/{twitter.strip()}"
                    if not twitter.startswith("http")
                    else twitter.strip()
                )
                key = (row["asset_id"], twitter_url)
                if key not in seen:
                    seen.add(key)
                    values.append("(%s, %s, %s, %s, %s, %s, %s, %s)")
                    params.extend(
                        [
                            "asset",
                            row["asset_id"],
                            None,
                            "dl",
                            "other",
                            twitter_url,
                            "dl_info.twitter",
                            False,
                        ]
                    )

        if args.dry_run:
            print(
                json.dumps(
                    {"mode": "dry_run", "assets": len(rows), "entries": len(values)},
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
                {"status": "success", "assets": len(rows), "entries": len(values)},
                ensure_ascii=False,
            )
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
