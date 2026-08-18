from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))


def classify_cg_asset_type(symbol: str | None, platforms: dict | None) -> str:
    symbol_norm = (symbol or "").strip().upper()
    if symbol_norm in {
        "USDT",
        "USDC",
        "DAI",
        "FDUSD",
        "TUSD",
        "USDE",
        "BUSD",
        "USDP",
        "GUSD",
        "LUSD",
        "FRAX",
        "MIM",
        "USTC",
        "USDD",
    }:
        return "stablecoin"
    if platforms and len(platforms) > 0:
        return "token"
    return "coin"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Batch bootstrap CG coin_list → core.asset + source_map."
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=1000)
    return parser


BATCH_INSERT_ASSETS = """
INSERT INTO core.asset (canonical_symbol, canonical_name, asset_type, status, launch_date, description_short)
VALUES {}
RETURNING asset_id, canonical_symbol
"""

BATCH_UPSERT_MAP = """
INSERT INTO core.asset_source_map (
    asset_id, source_code, source_asset_key,
    match_status, match_method, match_confidence, is_primary, verified_by
) VALUES {}
ON CONFLICT (source_code, source_asset_key) DO UPDATE SET
    asset_id = EXCLUDED.asset_id,
    match_status = EXCLUDED.match_status,
    match_method = EXCLUDED.match_method,
    match_confidence = EXCLUDED.match_confidence,
    is_primary = EXCLUDED.is_primary,
    verified_by = EXCLUDED.verified_by,
    verified_at = NOW(),
    updated_at = NOW()
"""


def main() -> int:
    args = build_parser().parse_args()

    import psycopg
    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection
    from crypto_research.db.sector import upsert_asset_sectors_batch
    from crypto_research.db.upsert import load_sql
    from crypto_research.mapping.sector import classify_cg_sectors

    settings = get_settings(require_database=True)
    select_sql = load_sql("src_cg/select_cg_assets_from_coin_list.sql")

    with get_connection(settings.database_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(select_sql, (args.limit,))
            rows = [dict(row) for row in cur.fetchall()]

        if not rows:
            print(json.dumps({"status": "noop"}, ensure_ascii=False))
            return 0

        # Classify and prepare
        matched: list[dict] = []  # rows with existing_asset_id
        unmatched: list[dict] = []  # rows needing new asset

        for row in rows:
            platforms = row.get("platforms") or {}
            if isinstance(platforms, str):
                platforms = json.loads(platforms)
            asset_type = classify_cg_asset_type(
                row["symbol"], platforms if isinstance(platforms, dict) else {}
            )
            entry = {
                "coin_id": row["coin_id"],
                "symbol": row["symbol"],
                "name": row["name"],
                "asset_type": asset_type,
                "categories": row.get("categories"),
                "existing_asset_id": row.get("existing_asset_id"),
            }
            if entry["existing_asset_id"]:
                matched.append(entry)
            else:
                unmatched.append(entry)

        new_asset_count = len(unmatched)
        matched_count = len(matched)

        # Batch insert new assets
        symbol_to_asset_id: dict[str, int] = {}
        if unmatched:
            placeholders = []
            params = []
            for e in unmatched:
                placeholders.append("(%s, %s, %s, %s, %s, %s)")
                params.extend(
                    [e["symbol"], e["name"], e["asset_type"], "active", None, None]
                )

            sql = BATCH_INSERT_ASSETS.format(", ".join(placeholders))
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute(sql, params)
                for result_row in cur.fetchall():
                    symbol_to_asset_id[result_row["canonical_symbol"]] = result_row[
                        "asset_id"
                    ]

        # Build source_map entries
        map_values = []
        map_params = []
        for e in matched:
            map_values.append("(%s, %s, %s, %s, %s, %s, %s, %s)")
            map_params.extend(
                [
                    e["existing_asset_id"],
                    "cg",
                    e["coin_id"],
                    "confirmed",
                    "bootstrap_cg_list",
                    95,
                    False,
                    "agent",
                ]
            )
        for e in unmatched:
            asset_id = symbol_to_asset_id.get(e["symbol"])
            if asset_id is None:
                print(f"WARNING: no asset_id for {e['symbol']}", flush=True)
                continue
            map_values.append("(%s, %s, %s, %s, %s, %s, %s, %s)")
            map_params.extend(
                [
                    asset_id,
                    "cg",
                    e["coin_id"],
                    "candidate",
                    "bootstrap_cg_list",
                    75,
                    False,
                    "agent",
                ]
            )

        if args.dry_run:
            print(
                json.dumps(
                    {
                        "mode": "dry_run",
                        "total": len(rows),
                        "matched": matched_count,
                        "new_assets": new_asset_count,
                        "map_entries": len(map_values),
                    },
                    ensure_ascii=False,
                )
            )
            return 0

        if map_values:
            map_sql = BATCH_UPSERT_MAP.format(", ".join(map_values))
            with conn.cursor() as cur:
                cur.execute(map_sql, map_params)

        # 批量写入 CG 来源赛道标签
        all_asset_ids: list[int] = []
        sectors_by_asset: dict[int, list[tuple[str, float]]] = {}
        for e in matched:
            aid = e["existing_asset_id"]
            all_asset_ids.append(aid)
            cats = e.get("categories")
            if isinstance(cats, str):
                import json as _json
                try:
                    cats = _json.loads(cats)
                except Exception:
                    cats = None
            sectors_by_asset[aid] = classify_cg_sectors(cats)
        for e in unmatched:
            aid = symbol_to_asset_id.get(e["symbol"])
            if aid is None:
                continue
            all_asset_ids.append(aid)
            cats = e.get("categories")
            if isinstance(cats, str):
                import json as _json
                try:
                    cats = _json.loads(cats)
                except Exception:
                    cats = None
            sectors_by_asset[aid] = classify_cg_sectors(cats)

        sector_hit_count = sum(1 for s in sectors_by_asset.values() if s)
        if all_asset_ids:
            upsert_asset_sectors_batch(conn, all_asset_ids, "cg", sectors_by_asset)

        print(
            json.dumps(
                {
                    "status": "success",
                    "total": len(rows),
                    "matched": matched_count,
                    "new_assets": new_asset_count,
                    "mapped": len(map_values),
                    "sector_hits": sector_hit_count,
                },
                ensure_ascii=False,
            )
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
