from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))


def classify_dl_asset_type(category: str | None) -> str:
    if not category:
        return "other"
    cat = category.lower()
    if "stablecoin" in cat or "cdp" in cat:
        return "stablecoin"
    if "meme" in cat:
        return "meme"
    if "chain" in cat or "layer" in cat:
        return "coin"
    if "lp" in cat or "liquid staking" in cat or "lsd" in cat:
        return "lp_token"
    if "derivatives" in cat or "synthetic" in cat:
        return "synthetic"
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Batch bootstrap DL protocols → core.asset + source_map."
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=1000)
    return parser


# 留痕口径：match_status / match_method / match_confidence 由「判据强度」决定。
# 一手 id（cmc_id / gecko_id）精确命中 = 已被权威来源核验 → confirmed；
# 仅 symbol 相等 = 未核验 → candidate（旧实现把二者都写成 confirmed/100，
# 使「一手命中」与「symbol 撞车」在库里无法区分，见 2026-09-23 dl 映射精度诊断）。
MATCH_POLICY = {
    "cmc": ("confirmed", "bootstrap_dl_cmc", 95),
    "gecko": ("confirmed", "bootstrap_dl_gecko", 90),
    "symbol": ("candidate", "bootstrap_dl_symbol", 40),
}
# 无既有资产可复用 → 由协议自身新建资产，映射正确但不含一手核验
NEW_ASSET_POLICY = ("candidate", "bootstrap_dl_new", 80)


def _count_kinds(matched: list[dict]) -> dict[str, int]:
    """按判据分桶统计（可观测性：确认一手命中占比，而非只看总数）。"""
    counts = {"cmc": 0, "gecko": 0, "symbol": 0}
    for e in matched:
        counts[e["match_kind"] or "symbol"] += 1
    return counts


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
    -- is_primary 互斥保护：同 asset 已有其他源 primary 则降级
    is_primary = CASE
        WHEN EXCLUDED.is_primary = TRUE
         AND EXISTS (
             SELECT 1 FROM core.asset_source_map
             WHERE asset_id = EXCLUDED.asset_id
               AND is_primary = TRUE
               AND (source_code, source_asset_key) <> (EXCLUDED.source_code, EXCLUDED.source_asset_key)
         )
        THEN FALSE
        ELSE EXCLUDED.is_primary
    END,
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
    from crypto_research.mapping.sector import classify_dl_sectors

    settings = get_settings(require_database=True)
    select_sql = load_sql("src_dl/select_dl_assets_for_core_bootstrap.sql")

    with get_connection(settings.database_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(select_sql, (args.limit,))
            rows = [dict(row) for row in cur.fetchall()]

        if not rows:
            print(json.dumps({"status": "noop"}, ensure_ascii=False))
            return 0

        matched: list[dict] = []
        unmatched: list[dict] = []

        for row in rows:
            asset_type = classify_dl_asset_type(row.get("category"))
            desc = build_description_short(row.get("description"))
            entry = {
                "protocol_id": row["protocol_id"],
                "symbol": row["symbol"],
                "name": row["name"],
                "asset_type": asset_type,
                "category": row.get("category"),
                "description_short": desc,
                "existing_asset_id": row.get("existing_asset_id"),
                "match_kind": row.get("match_kind"),
            }
            if entry["existing_asset_id"]:
                matched.append(entry)
            else:
                unmatched.append(entry)

        # 无有效 symbol 的协议不创建新资产：无法可靠标识，避免 symbol='-' 撞车/垃圾资产
        unmatched = [
            e for e in unmatched
            if e["symbol"] and e["symbol"].strip() != "" and e["symbol"] != "-"
        ]

        new_count = len(unmatched)
        matched_count = len(matched)

        # dry-run 必须是纯只读：本分支原先位于「新建资产 INSERT」之后，
        # 而 get_connection 成功退出即 commit，导致 --dry-run 仍会落库新资产。
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "mode": "dry_run",
                        "total": len(rows),
                        "matched": matched_count,
                        "new": new_count,
                        "by_kind": _count_kinds(matched),
                    },
                    ensure_ascii=False,
                )
            )
            return 0

        # Batch insert new assets
        symbol_to_asset_id: dict[str, int] = {}
        if unmatched:
            placeholders = []
            params = []
            for e in unmatched:
                placeholders.append("(%s, %s, %s, %s, %s, %s)")
                params.extend(
                    [
                        e["symbol"],
                        e["name"],
                        e["asset_type"],
                        "active",
                        None,
                        e["description_short"],
                    ]
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
        kind_counts = _count_kinds(matched)
        for e in matched:
            status, method, confidence = MATCH_POLICY.get(
                e["match_kind"], MATCH_POLICY["symbol"]
            )
            map_values.append("(%s, %s, %s, %s, %s, %s, %s, %s)")
            map_params.extend(
                [
                    e["existing_asset_id"],
                    "dl",
                    e["protocol_id"],
                    status,
                    method,
                    confidence,
                    False,
                    "agent",
                ]
            )
        for e in unmatched:
            asset_id = symbol_to_asset_id.get(e["symbol"])
            if asset_id is None:
                print(f"WARNING: no asset_id for {e['symbol']}", flush=True)
                continue
            status, method, confidence = NEW_ASSET_POLICY
            kind_counts["new"] += 1
            map_values.append("(%s, %s, %s, %s, %s, %s, %s, %s)")
            map_params.extend(
                [
                    asset_id,
                    "dl",
                    e["protocol_id"],
                    status,
                    method,
                    confidence,
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
                        "new": new_count,
                        "map_entries": len(map_values),
                        "by_kind": kind_counts,
                    },
                    ensure_ascii=False,
                )
            )
            return 0

        if map_values:
            map_sql = BATCH_UPSERT_MAP.format(", ".join(map_values))
            with conn.cursor() as cur:
                cur.execute(map_sql, map_params)

        # 批量写入 DL 来源赛道标签
        all_asset_ids: list[int] = []
        sectors_by_asset: dict[int, list[tuple[str, float]]] = {}
        for e in matched:
            aid = e["existing_asset_id"]
            all_asset_ids.append(aid)
            sectors_by_asset[aid] = classify_dl_sectors(e.get("category"))
        for e in unmatched:
            aid = symbol_to_asset_id.get(e["symbol"])
            if aid is None:
                continue
            all_asset_ids.append(aid)
            sectors_by_asset[aid] = classify_dl_sectors(e.get("category"))

        sector_hit_count = sum(1 for s in sectors_by_asset.values() if s)
        if all_asset_ids:
            upsert_asset_sectors_batch(conn, all_asset_ids, "dl", sectors_by_asset)

        print(
            json.dumps(
                {
                    "status": "success",
                    "total": len(rows),
                    "matched": matched_count,
                    "new_assets": new_count,
                    "mapped": len(map_values),
                    "sector_hits": sector_hit_count,
                    "by_kind": kind_counts,
                },
                ensure_ascii=False,
            )
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
