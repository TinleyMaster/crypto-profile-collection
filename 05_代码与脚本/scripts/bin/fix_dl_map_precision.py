"""dl 映射精度存量修复（阶段 2 存权重映射 / 阶段 3 语义修正）。

背景：`core.asset_source_map` 中 `source_code='dl'` 的 4244 条映射由 `bootstrap_dl`
建立，判据只有「cmc_id 精确命中 / symbol 相等」两条，且 matched 分支统一写
`match_status='confirmed'`、`match_confidence=100` —— 使「一手 id 命中」与「symbol 撞车」
在库里无法区分。见 04_架构与代码方案/dl映射精度诊断与修复方案_2026-09-23.md。

阶段 2（需人工研判的可证伪清单，本脚本只做确定性部分）：
  - Tier A = 一手判据（协议自带 cmc_id / gecko_id）与落点资产映射**矛盾**的 505 条
  - 能确定性重指：落点资产持有该一手键、且该键唯一指向一个真实资产（非占位壳）
      → UPDATE asset_id
  - 无一手落点 → DELETE 该映射（明确不按名称/symbol 猜测回填）

阶段 3（语义）：`match_status='confirmed'` 中**未被一手判据证实**的（Tier A 残余 +
  Tier B + Tier C）降级为 `candidate`，并同步把 `match_confidence` 从 100 降为低值，
  避免下游把「进了 matched 分支」误读为「已核验」。

口径复用 `diag_dl_map_precision.BASE_CTE`，与诊断脚本同源，避免口径漂移。
默认 dry-run；`--apply` 执行；自动备份（`--no-backup` 跳过）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
for _p in (str(PROJECT_SRC), str(SCRIPT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg.rows  # noqa: E402

import diag_dl_map_precision as diag  # noqa: E402

# 占位壳（无真实 canonical_symbol）不作重指目标：与 P1-4 写入侧守卫同口径
PLACEHOLDER_PREDICATE = (
    "ta.canonical_symbol IS NOT NULL AND TRIM(ta.canonical_symbol) NOT IN ('', '-', '?')"
)

# 阶段 2 的确定性重指目标：一手键必须唯一指向一个资产，且该资产非占位壳
STAGE2_SQL = f"""
{diag.BASE_CTE}
, tier_a AS (
    SELECT * FROM tiered WHERE tier = 'A'
)
SELECT
    t.asset_id            AS landing_asset_id,
    t.protocol_id,
    t.p_cmc,
    t.p_gecko,
    t.match_status,
    t.match_confidence,
    t.cmc_confirms,
    t.cg_confirms,
    (SELECT MIN(m.asset_id)
       FROM core.asset_source_map m
       JOIN core.asset ta ON ta.asset_id = m.asset_id
      WHERE m.source_code = 'cmc' AND m.source_asset_key = t.p_cmc
        AND {PLACEHOLDER_PREDICATE}
        AND (SELECT COUNT(DISTINCT m2.asset_id) FROM core.asset_source_map m2
              WHERE m2.source_code = 'cmc'
                AND m2.source_asset_key = t.p_cmc) = 1
    ) AS tgt_cmc,
    (SELECT MIN(m.asset_id)
       FROM core.asset_source_map m
       JOIN core.asset ta ON ta.asset_id = m.asset_id
      WHERE m.source_code = 'cg' AND m.source_asset_key = t.p_gecko
        AND {PLACEHOLDER_PREDICATE}
        AND (SELECT COUNT(DISTINCT m2.asset_id) FROM core.asset_source_map m2
              WHERE m2.source_code = 'cg'
                AND m2.source_asset_key = t.p_gecko) = 1
    ) AS tgt_gecko
FROM tier_a t
ORDER BY t.asset_id, t.protocol_id
"""

# 阶段 3：未被一手判据证实（非 tier V）却留痕为 confirmed 的行
STAGE3_SQL = f"""
{diag.BASE_CTE}
SELECT
    t.protocol_id,
    t.asset_id,
    t.tier,
    t.match_status,
    t.match_confidence
FROM tiered t
WHERE t.tier <> 'V'
  -- 三值逻辑：落点资产无该来源映射时判据为 NULL（未证实），必须 COALESCE 为 FALSE，
  -- 否则 `NOT NULL` 会把「未证实」的行漏掉不降级。
  AND NOT COALESCE(t.cg_confirms, FALSE)
  AND NOT COALESCE(t.cmc_confirms, FALSE)
  AND t.match_status = 'confirmed'
ORDER BY t.tier, t.asset_id, t.protocol_id
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="dl 映射精度存量修复：阶段 2 重映射/删除、阶段 3 语义降级。"
    )
    parser.add_argument("--apply", action="store_true", help="实际写入（默认仅统计）。")
    parser.add_argument("--no-backup", action="store_true", help="跳过备份表创建。")
    parser.add_argument(
        "--stage", choices=("2", "3", "all"), default="all", help="只跑某一阶段。"
    )
    return parser


def _backup_table(cur, table: str, keys: list[str]) -> None:
    cur.execute(
        f"CREATE TABLE IF NOT EXISTS {table} AS "
        f"SELECT * FROM core.asset_source_map WHERE FALSE"
    )
    cur.execute(
        f"INSERT INTO {table} SELECT * FROM core.asset_source_map "
        f"WHERE source_code = 'dl' AND source_asset_key = ANY(%s)",
        (keys,),
    )


def run_stage2(cur, apply: bool, no_backup: bool, suffix: str) -> dict:
    cur.execute(STAGE2_SQL)
    rows = [dict(r) for r in cur.fetchall()]

    remap = []
    keep = []
    remove = []
    for r in rows:
        if r["cmc_confirms"] or r["cg_confirms"]:
            # 落点资产**确实持有**协议的一手键（另一侧矛盾只是协议自带 id 不准），
            # 已被一手判据证实：不可删除、也不降级。
            keep.append(r)
        elif r["tgt_cmc"] and r["tgt_cmc"] != r["landing_asset_id"]:
            r["target_asset_id"] = r["tgt_cmc"]
            r["via"] = "cmc"
            remap.append(r)
        elif r["tgt_gecko"] and r["tgt_gecko"] != r["landing_asset_id"]:
            r["target_asset_id"] = r["tgt_gecko"]
            r["via"] = "gecko"
            remap.append(r)
        else:
            remove.append(r)

    result = {
        "tier_a": len(rows),
        "remap": len(remap),
        "remap_by_cmc": sum(1 for r in remap if r["via"] == "cmc"),
        "remap_by_gecko": sum(1 for r in remap if r["via"] == "gecko"),
        "keep_gecko_confirmed": len(keep),
        "delete": len(remove),
    }
    if not apply:
        result["samples_remap"] = [
            {"protocol_id": r["protocol_id"], "from": r["landing_asset_id"],
             "to": r["target_asset_id"], "via": r["via"]}
            for r in remap[:5]
        ]
        result["samples_delete"] = [r["protocol_id"] for r in remove[:5]]
        return result

    keys = [r["protocol_id"] for r in rows]
    if keys and not no_backup:
        _backup_table(cur, f"core.asset_source_map_dl_precision_bak_{suffix}", keys)

    for r in remap:
        cur.execute(
            """
            UPDATE core.asset_source_map
               SET asset_id = %s,
                   match_status = 'confirmed',
                   match_method = %s,
                   match_confidence = %s,
                   updated_at = NOW()
             WHERE source_code = 'dl' AND source_asset_key = %s
            """,
            (
                r["target_asset_id"],
                f"fix_precision_{r['via']}",
                90 if r["via"] == "cmc" else 85,
                r["protocol_id"],
            ),
        )
    if remove:
        cur.execute(
            "DELETE FROM core.asset_source_map "
            "WHERE source_code = 'dl' AND source_asset_key = ANY(%s)",
            ([r["protocol_id"] for r in remove],),
        )
    return result


def run_stage3(cur, apply: bool, no_backup: bool, suffix: str) -> dict:
    cur.execute(STAGE3_SQL)
    rows = [dict(r) for r in cur.fetchall()]

    by_tier: dict[str, int] = {}
    for r in rows:
        by_tier[r["tier"]] = by_tier.get(r["tier"], 0) + 1

    result = {"downgrade": len(rows), "by_tier": by_tier}
    if not apply:
        result["samples"] = [
            {"protocol_id": r["protocol_id"], "asset_id": r["asset_id"],
             "tier": r["tier"], "confidence": float(r["match_confidence"] or 0)}
            for r in rows[:5]
        ]
        return result

    keys = [r["protocol_id"] for r in rows]
    if keys and not no_backup:
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS "
            f"core.asset_source_map_dl_status_bak_{suffix} "
            f"(source_asset_key TEXT, match_status TEXT, match_confidence NUMERIC, "
            f" snapshot_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
        )
        cur.execute(
            f"INSERT INTO core.asset_source_map_dl_status_bak_{suffix} "
            f"(source_asset_key, match_status, match_confidence) "
            f"SELECT source_asset_key, match_status, match_confidence "
            f"FROM core.asset_source_map "
            f"WHERE source_code = 'dl' AND source_asset_key = ANY(%s)",
            (keys,),
        )
    if keys:
        cur.execute(
            "UPDATE core.asset_source_map "
            "SET match_status = 'candidate', match_confidence = 40, updated_at = NOW() "
            "WHERE source_code = 'dl' AND source_asset_key = ANY(%s) "
            "AND match_status = 'confirmed'",
            (keys,),
        )
    return result


def main() -> int:
    args = build_parser().parse_args()

    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    suffix = date.today().strftime("%Y%m%d")
    out: dict = {"mode": "applied" if args.apply else "dry_run", "stage": args.stage}

    with get_connection(get_settings(require_database=True).database_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            if args.stage in ("2", "all"):
                out["stage2"] = run_stage2(cur, args.apply, args.no_backup, suffix)
            if args.stage in ("3", "all"):
                # 阶段 2 若已执行，本阶段在同一事务内即读到重映射后的状态
                out["stage3"] = run_stage3(cur, args.apply, args.no_backup, suffix)

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())