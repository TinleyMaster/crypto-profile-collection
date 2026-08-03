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
    # stablecoins
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
        description="Bootstrap core.asset from CoinGecko coin_list (no coin_info needed)."
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=500)
    return parser


def main() -> int:
    args = build_parser().parse_args()

    import psycopg
    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection
    from crypto_research.db.upsert import fetch_one, load_sql

    settings = get_settings(require_database=True)

    select_sql = load_sql("src_cg/select_cg_assets_from_coin_list.sql")
    insert_asset_sql = load_sql("core/insert_asset.sql")
    upsert_map_sql = load_sql("core/upsert_asset_source_map.sql")

    with get_connection(settings.database_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(select_sql, (args.limit,))
            rows = [dict(row) for row in cur.fetchall()]

        if not rows:
            print(
                json.dumps(
                    {
                        "status": "noop",
                        "message": "No CG coin_list assets to bootstrap.",
                    },
                    ensure_ascii=False,
                )
            )
            return 0

        created = 0
        matched = 0
        mapped = 0

        for row in rows:
            existing_id = row.get("existing_asset_id")
            platforms = row.get("platforms") or {}
            if isinstance(platforms, str):
                import json as _json

                platforms = _json.loads(platforms)
            asset_type = classify_cg_asset_type(
                row["symbol"], platforms if isinstance(platforms, dict) else {}
            )

            if existing_id:
                asset_id = existing_id
                matched += 1
            else:
                asset_row = fetch_one(
                    conn,
                    insert_asset_sql,
                    (row["symbol"], row["name"], asset_type, "active", None, None),
                )
                asset_id = asset_row["asset_id"]
                created += 1

            fetch_one(
                conn,
                upsert_map_sql,
                (
                    asset_id,
                    "cg",
                    row["coin_id"],
                    "confirmed" if existing_id else "candidate",
                    "bootstrap_cg_list",
                    95 if existing_id else 75,
                    False,
                    "agent",
                ),
            )
            mapped += 1

        print(
            json.dumps(
                {
                    "status": "success",
                    "processed": len(rows),
                    "created": created,
                    "matched": matched,
                    "mapped": mapped,
                },
                ensure_ascii=False,
            )
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
