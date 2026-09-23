"""P1-4 存量修复：清理 biz.asset_raises 中挂在占位资产上的错配行。

背景
----
2026-08-16 的单一批次里，大量 DefiLlama 协议被映射到同一个占位资产
（canonical_symbol 为 ''/'-'/'?'），导致 biz.asset_raises 出现
「甲协议标题 + 乙资产身份」的错配 —— 其中 asset_id=11125（Aztec Connect）
一个资产上挂了 407 行、涉及 287 个不同协议。消费侧（macro_market._recent_raises）
会把这些行渲染成融资卡并带着错误 asset_id 进 AI 画像。

处理策略
--------
1. 备份整表（默认执行，可用 --no-backup 关闭）
2. 逐行尝试「可信重映射」：仅以 core.asset_source_map 的 dl 映射（source_asset_key
   = defillama_id）为准，且目标资产必须有真实 canonical_symbol。**不做名称猜测**，
   避免把 A 协议的融资记到同名但无关的 B 代币上。
3. 重映射后若与既有行 (asset_id, round, raise_date) 冲突 → 视为重复，删除该行
4. 无可信映射的行 → 删除（其真实归属无法确定，保留即污染）

用法
----
    python fix_asset_raises_placeholder.py              # 只统计（dry-run，默认）
    python fix_asset_raises_placeholder.py --apply      # 执行（先自动备份）
    python fix_asset_raises_placeholder.py --apply --no-backup

前置：写入侧守卫（phase_b2_third_party_raises.py）应先于本脚本上线，
否则清理完存量后重跑仍会重新写入占位资产行。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg.rows  # noqa: E402

# 与 phase_b2_third_party_raises.py 保持同一口径
PLACEHOLDER_PREDICATE = (
    "a.canonical_symbol IS NOT NULL AND TRIM(a.canonical_symbol) NOT IN ('', '-', '?')"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="清理 biz.asset_raises 占位资产错配行。")
    parser.add_argument("--apply", action="store_true", help="实际写入（默认仅统计）。")
    parser.add_argument("--no-backup", action="store_true", help="跳过备份表创建。")
    return parser


def _fetch_polluted(cur) -> list[dict]:
    cur.execute(f"""
        SELECT r.id, r.asset_id, r.defillama_id, r.protocol_name,
               r.round, r.raise_date,
               a.canonical_name, a.canonical_symbol
        FROM biz.asset_raises AS r
        INNER JOIN core.asset AS a ON a.asset_id = r.asset_id
        WHERE NOT ({PLACEHOLDER_PREDICATE})
        ORDER BY r.asset_id, r.id
    """)
    return [dict(row) for row in cur.fetchall()]


def _resolve_remap(cur, defillama_id) -> int | None:
    """以 dl 映射为准反查真实 asset_id；无映射或无真实 symbol 时返回 None。"""
    if not defillama_id:
        return None
    cur.execute(f"""
        SELECT asm.asset_id
        FROM core.asset_source_map AS asm
        INNER JOIN core.asset AS a ON a.asset_id = asm.asset_id
        WHERE asm.source_code = 'dl'
          AND asm.source_asset_key = %s
          AND {PLACEHOLDER_PREDICATE}
        LIMIT 1
    """, (str(defillama_id),))
    row = cur.fetchone()
    return int(row["asset_id"]) if row else None


def _row_exists(cur, asset_id: int, round_name, raise_date) -> bool:
    cur.execute("""
        SELECT 1 FROM biz.asset_raises
        WHERE asset_id = %s AND round IS NOT DISTINCT FROM %s
          AND raise_date IS NOT DISTINCT FROM %s
        LIMIT 1
    """, (asset_id, round_name, raise_date))
    return cur.fetchone() is not None


def _count_same_name_candidates(cur, ids: list[int]) -> int:
    """待删行中「能找到同名 core.asset」的行数。

    仅作诊断：同名不等于同一项目（实测 'Cypher' 命中 3 个资产、'OrBit' 命中 2 个），
    因此脚本不做名称猜测，只把该数量报给人工研判。
    """
    if not ids:
        return 0
    cur.execute("""
        SELECT COUNT(*) AS n
        FROM biz.asset_raises r
        JOIN core.asset t
          ON UPPER(TRIM(t.canonical_name)) = UPPER(TRIM(r.protocol_name))
          OR UPPER(TRIM(t.canonical_symbol)) = UPPER(TRIM(r.protocol_name))
        WHERE r.id = ANY(%s)
    """, (ids,))
    return int(cur.fetchone()["n"])


def _list_placeholder_mappings(cur) -> list[dict]:
    """仍需人工修正的占位资产 dl 映射（后续跟进项，本脚本不改）。"""
    cur.execute(f"""
        SELECT asm.asset_id, a.canonical_name, COUNT(*) AS n_protocols
        FROM core.asset_source_map AS asm
        INNER JOIN core.asset AS a ON a.asset_id = asm.asset_id
        WHERE asm.source_code = 'dl' AND NOT ({PLACEHOLDER_PREDICATE})
        GROUP BY asm.asset_id, a.canonical_name
        ORDER BY n_protocols DESC
    """)
    return [dict(row) for row in cur.fetchall()]


def main() -> int:
    args = build_parser().parse_args()

    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    settings = get_settings(require_database=True)

    with get_connection(settings.database_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            polluted = _fetch_polluted(cur)
            placeholder_maps = _list_placeholder_mappings(cur)
            cur.execute("SELECT COUNT(*) AS n FROM biz.asset_raises")
            total = cur.fetchone()["n"]

            if not polluted:
                print(json.dumps({
                    "status": "clean",
                    "total_rows": total,
                    "placeholder_mappings": placeholder_maps,
                }, ensure_ascii=False, indent=2))
                return 0

            to_remap: list[dict] = []
            to_delete: list[dict] = []
            for row in polluted:
                new_asset_id = _resolve_remap(cur, row["defillama_id"])
                if new_asset_id and new_asset_id != row["asset_id"]:
                    if _row_exists(cur, new_asset_id, row["round"], row["raise_date"]):
                        row["reason"] = f"已存在目标行 asset_id={new_asset_id}，按重复删除"
                        to_delete.append(row)
                    else:
                        row["new_asset_id"] = new_asset_id
                        to_remap.append(row)
                else:
                    row["reason"] = "无可信 dl 映射，归属不可判定"
                    to_delete.append(row)

            # 诊断：待删行中有多少能在 core.asset 找到同名（仅供人工研判，不参与处置）
            delete_with_name_candidate = _count_same_name_candidates(
                cur, [r["id"] for r in to_delete])

        summary = {
            "total_rows": total,
            "polluted_rows": len(polluted),
            "polluted_assets": sorted({r["asset_id"] for r in polluted}),
            "remap": len(to_remap),
            "delete": len(to_delete),
            "delete_with_same_name_candidate": delete_with_name_candidate,
            "placeholder_mappings_pending_fix": placeholder_maps,
        }

        if not args.apply:
            print(json.dumps({"mode": "dry-run", **summary,
                              "remap_sample": to_remap[:5],
                              "delete_sample": to_delete[:5]},
                             ensure_ascii=False, indent=2, default=str))
            return 0

        with get_connection(settings.database_url) as wconn:
            if not args.no_backup:
                bak = f"biz.asset_raises_bak_{date.today().strftime('%Y%m%d')}"
                with wconn.cursor() as cur:
                    cur.execute(f"CREATE TABLE IF NOT EXISTS {bak} AS SELECT * FROM biz.asset_raises")
                summary["backup_table"] = bak

            with wconn.cursor() as cur:
                for row in to_remap:
                    cur.execute(
                        "UPDATE biz.asset_raises SET asset_id = %s, updated_at = NOW() WHERE id = %s",
                        (row["new_asset_id"], row["id"]),
                    )
                if to_delete:
                    cur.execute(
                        "DELETE FROM biz.asset_raises WHERE id = ANY(%s)",
                        ([r["id"] for r in to_delete],),
                    )
            # get_connection 退出时统一 commit

        with get_connection(settings.database_url) as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute(f"""
                    SELECT COUNT(*) AS n
                    FROM biz.asset_raises AS r
                    INNER JOIN core.asset AS a ON a.asset_id = r.asset_id
                    WHERE NOT ({PLACEHOLDER_PREDICATE})
                """)
                summary["polluted_rows_after"] = cur.fetchone()["n"]
                cur.execute("SELECT COUNT(*) AS n FROM biz.asset_raises")
                summary["total_rows_after"] = cur.fetchone()["n"]

        summary["status"] = "applied"
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())