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
        description="Bootstrap core.asset and core.asset_source_map from src_dl tables."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview rows without writing database."
    )
    parser.add_argument(
        "--limit", type=int, default=100, help="Maximum number of rows to process."
    )
    return parser


# DL 类别中明确的稳定币信号。
# 注意：'cdp' 是 CDP 协议的治理代币（如 MKR/ANGLE），并非稳定币本身，故排除；
# 仅保留明确的 'stablecoin' 子串与 'algo-stables'（算法稳定币协议，如 FRAX/LUSD）。
DL_STABLE_CATS = {"algo-stables"}


def classify_dl_asset_type(category: str | None) -> str:
    """Map DL category to core.asset valid types: token/coin/stablecoin/lp_token/meme/synthetic/other"""
    if not category:
        return "other"
    cat = category.lower()
    # 1) 稳定币优先
    if "stablecoin" in cat or cat in DL_STABLE_CATS:
        return "stablecoin"
    if "meme" in cat:
        return "meme"
    if "chain" in cat or "layer" in cat:
        return "coin"
    if "lp" in cat or "liquid staking" in cat or "lsd" in cat:
        return "lp_token"
    if "derivatives" in cat or "synthetic" in cat:
        return "synthetic"
    # All DeFi protocols (dex, lending, yield, bridge, etc.) → token
    if any(
        kw in cat
        for kw in (
            "dex",
            "lending",
            "yield",
            "bridge",
            "services",
            "restaking",
            "staking",
            "farm",
        )
    ):
        return "token"
    return "other"


def build_description_short(
    description: str | None, max_length: int = 500
) -> str | None:
    if not description:
        return None
    text = " ".join(description.split()).strip()
    if not text:
        return None
    if len(text) <= max_length:
        return text
    return text[: max_length - 3].rstrip() + "..."


def main() -> int:
    args = build_parser().parse_args()

    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection
    from crypto_research.db.upsert import fetch_one, load_sql

    settings = get_settings(require_database=True)

    select_candidates_sql = load_sql("src_dl/select_dl_assets_for_core_bootstrap.sql")
    insert_asset_sql = load_sql("core/insert_asset.sql")
    upsert_source_map_sql = load_sql("core/upsert_asset_source_map.sql")

    import psycopg

    with get_connection(settings.database_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(select_candidates_sql, (args.limit,))
            source_rows = [dict(row) for row in cur.fetchall()]

        if not source_rows:
            print(
                json.dumps(
                    {"status": "noop", "message": "No DL assets to bootstrap."},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        prepared_rows: list[dict] = []
        for row in source_rows:
            asset_type = classify_dl_asset_type(row.get("category"))
            prepared_rows.append(
                {
                    "protocol_id": row["protocol_id"],
                    "source_asset_key": row["protocol_id"],
                    "canonical_symbol": row["symbol"],
                    "canonical_name": row["name"],
                    "asset_type": asset_type,
                    "status": "active",
                    "launch_date": None,
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
        matched_count = 0
        mapped_count = 0

        for row in prepared_rows:
            existing_id = row["existing_asset_id"]
            if existing_id:
                asset_id = existing_id
                matched_count += 1
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
                    "dl",
                    row["source_asset_key"],
                    "confirmed" if existing_id else "candidate",
                    "bootstrap_dl",
                    100 if existing_id else 85,
                    False,
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
                    "matched_existing": matched_count,
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
