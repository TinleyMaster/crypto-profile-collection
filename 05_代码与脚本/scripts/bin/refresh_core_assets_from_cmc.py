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
    parser = argparse.ArgumentParser(
        description="Bootstrap core.asset and core.asset_source_map from src_cmc tables."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview rows without writing database."
    )
    parser.add_argument(
        "--limit", type=int, default=100, help="Maximum number of rows to process."
    )
    parser.add_argument(
        "--include-mapped",
        action="store_true",
        help="Also refresh assets that already have a confirmed CMC mapping.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection
    from crypto_research.db.upsert import fetch_one, load_sql
    from crypto_research.mapping.cmc_asset_bootstrap import (
        build_description_short,
        classify_asset_type,
    )

    settings = get_settings(require_database=True)

    select_candidates_sql = load_sql("src_cmc/select_cmc_assets_for_core_bootstrap.sql")
    insert_asset_sql = load_sql("core/insert_asset.sql")
    update_asset_sql = load_sql("core/update_asset_from_cmc.sql")
    upsert_source_map_sql = load_sql("core/upsert_asset_source_map.sql")

    with get_connection(settings.database_url) as conn:
        with conn.cursor(row_factory=__import__("psycopg").rows.dict_row) as cur:
            cur.execute(select_candidates_sql, (args.include_mapped, args.limit))
            source_rows = [dict(row) for row in cur.fetchall()]

        prepared_rows: list[dict[str, object]] = []
        for row in source_rows:
            urls = row.get("urls") or {}
            asset_type = classify_asset_type(
                symbol=row.get("symbol"),
                category_hint=row.get("category_hint"),
                urls=urls,
                has_platform=bool(row.get("platform_name") or row.get("token_address")),
            )
            prepared_rows.append(
                {
                    "cmc_id": row["cmc_id"],
                    "source_asset_key": str(row["cmc_id"]),
                    "canonical_symbol": row["symbol"],
                    "canonical_name": row["name"],
                    "asset_type": asset_type,
                    "status": "active",
                    "launch_date": row.get("date_launched"),
                    "description_short": build_description_short(
                        row.get("description")
                    ),
                    "existing_asset_id": row.get("existing_asset_id"),
                }
            )

        if args.dry_run:
            print(
                json.dumps(
                    {
                        "mode": "dry-run",
                        "row_count": len(prepared_rows),
                        "first_row": prepared_rows[0] if prepared_rows else None,
                    },
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
            )
            return 0

        created_count = 0
        refreshed_count = 0
        mapped_count = 0

        for row in prepared_rows:
            if row["existing_asset_id"]:
                asset_row = fetch_one(
                    conn,
                    update_asset_sql,
                    (
                        row["canonical_symbol"],
                        row["canonical_name"],
                        row["asset_type"],
                        row["status"],
                        row["launch_date"],
                        row["description_short"],
                        row["existing_asset_id"],
                    ),
                )
                asset_id = asset_row["asset_id"]
                refreshed_count += 1
            else:
                asset_row = fetch_one(
                    conn,
                    insert_asset_sql,
                    (
                        row["canonical_symbol"],
                        row["canonical_name"],
                        row["asset_type"],
                        row["status"],
                        row["launch_date"],
                        row["description_short"],
                    ),
                )
                asset_id = asset_row["asset_id"]
                created_count += 1

            fetch_one(
                conn,
                upsert_source_map_sql,
                (
                    asset_id,
                    "cmc",
                    row["source_asset_key"],
                    "confirmed",
                    "bootstrap_cmc",
                    100,
                    True,
                    "agent",
                ),
            )
            mapped_count += 1

        print(
            json.dumps(
                {
                    "status": "success",
                    "processed_rows": len(prepared_rows),
                    "created_assets": created_count,
                    "refreshed_assets": refreshed_count,
                    "mapped_rows": mapped_count,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
