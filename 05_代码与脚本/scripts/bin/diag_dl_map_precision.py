"""dl 映射精度诊断（**只读**，不写任何表）。

背景
----
`core.asset_source_map` 中 `source_code='dl'` 的 **4244** 条映射全部由
`bootstrap_dl` 建立，其匹配判据只有两条（见
[select_dl_assets_for_core_bootstrap.sql](file:///e:/瞎搞乱搞/web3/加密货币研究报告/05_代码与脚本/scripts/sql/src_dl/select_dl_assets_for_core_bootstrap.sql#L15-L39)）：

- Priority 1：`p.cmc_id` 精确命中
- Priority 2：`UPPER(a.canonical_symbol) = UPPER(p.symbol)`（名称仅作并列 tie-break）

而 `matched` 分支的留痕是**硬编码**的
（`bootstrap_dl_assets_batch.py` L189-191 统一写 `match_status='confirmed'`、
`match_confidence=100`）—— **该 confidence 不代表已核验**。于是「仅 symbol 相等」
的命中与「cmc_id 精确命中」在库里长得一模一样。

本脚本用**一手判据**（协议自带 `cmc_id` / `gecko_id`）把这些映射分层，
产出可复核清单，供人工研判。**不做任何数据修改。**

分层口径
--------
| 层 | 判据 | 含义 |
|---|---|---|
| **A1** | 协议有 `cmc_id`，且落点资产**有** cmc 映射，但该资产**没有**任何键等于该 `cmc_id` | 一手判据**证伪** |
| **A2** | 协议有 `gecko_id`，且落点资产**有** cg 映射，但该资产 cg 映射键**不含**该 `gecko_id` | 一手判据**证伪** |
| **B** | 仅 symbol 相等、名称不等，且 A1/A2 均未命中 | 无一手判据，**既不能证实也不能证伪** |
| **C** | 名称与 symbol **皆不等**（任何一方为空则不计） | 判据最弱 |
| 对照 | 名称与 symbol 皆相等 | 最可能正确 |

注意 A1/A2 都要求落点资产**本身持有**另一来源映射——即「资产确实是某个真实 CMC/CG
代币，只是不是协议声称的那一个」。若只要求「协议有 `cmc_id` 而资产完全没有 cmc 映射」
（更弱的变体），命中数会放大到 934 / 1248，含大量「资产只是没被 CMC 收录」的假阳性，
故**不作主张**。

用法
----
    python diag_dl_map_precision.py                          # 摘要（含样本）
    python diag_dl_map_precision.py --sample 20              # 调整样本条数
    python diag_dl_map_precision.py --export-md <path>       # 导出完整清单附录
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg.rows  # noqa: E402

# 底表：dl 映射 × 协议 × 落点资产 × 各来源映射键
BASE_CTE = """
WITH dl_map AS (
    SELECT asm.asset_id, asm.source_asset_key AS protocol_id,
           asm.match_status, asm.match_confidence,
           a.canonical_name, a.canonical_symbol,
           p.name AS p_name, p.symbol AS p_symbol, p.slug AS p_slug,
           p.cmc_id AS p_cmc, p.gecko_id AS p_gecko, p.category AS p_category
    FROM core.asset_source_map asm
    JOIN core.asset a ON a.asset_id = asm.asset_id
    LEFT JOIN src_dl.protocol_list p ON p.protocol_id = asm.source_asset_key
    WHERE asm.source_code = 'dl'
),
asset_keys AS (
    SELECT asm.asset_id,
           ARRAY_REMOVE(ARRAY_AGG(DISTINCT asm.source_asset_key)
                        FILTER (WHERE asm.source_code = 'cmc'), NULL) AS cmc_keys,
           ARRAY_REMOVE(ARRAY_AGG(DISTINCT asm.source_asset_key)
                        FILTER (WHERE asm.source_code = 'cg'), NULL)  AS cg_keys
    FROM core.asset_source_map asm
    GROUP BY asm.asset_id
),
classified AS (
    SELECT d.*, k.cmc_keys, k.cg_keys,
           (UPPER(TRIM(d.canonical_name)) = UPPER(TRIM(d.p_name)))      AS name_eq,
           (UPPER(TRIM(d.canonical_symbol)) = UPPER(TRIM(d.p_symbol)))  AS symbol_eq,
           -- A1：协议有 cmc_id、资产有 cmc 映射、但不含该 id
           (d.p_cmc IS NOT NULL
            AND COALESCE(ARRAY_LENGTH(k.cmc_keys, 1), 0) > 0
            AND NOT (d.p_cmc = ANY (k.cmc_keys)))                       AS a1_cmc_falsified,
           -- A2：协议有 gecko_id、资产有 cg 映射、但不含该 key
           (d.p_gecko IS NOT NULL
            AND COALESCE(ARRAY_LENGTH(k.cg_keys, 1), 0) > 0
            AND NOT (d.p_gecko = ANY (k.cg_keys)))                      AS a2_cg_falsified,
           -- V：一手判据**证实**（cmc_id 精确命中优先于 gecko_id）
           (d.p_cmc IS NOT NULL AND d.p_cmc = ANY (k.cmc_keys))          AS cmc_confirms,
           (d.p_gecko IS NOT NULL AND d.p_gecko = ANY (k.cg_keys))       AS cg_confirms
    FROM dl_map d
    LEFT JOIN asset_keys k ON k.asset_id = d.asset_id
),
tiered AS (
    SELECT *,
        CASE
            WHEN p_name IS NULL THEN 'X'
            -- cmc_id 精确命中 = bootstrap 的 Priority 1，最强正向证据
            WHEN cmc_confirms THEN 'V'
            -- 负向一手证据优先于 Tier B/C 的「名称/符号」表象
            WHEN a1_cmc_falsified OR a2_cg_falsified THEN 'A'
            WHEN cg_confirms THEN 'V'
            WHEN symbol_eq AND NOT name_eq THEN 'B'
            WHEN NOT symbol_eq AND NOT name_eq THEN 'C'
            ELSE 'control'
        END AS tier
    FROM classified
)
"""

SUMMARY_SQL = BASE_CTE + """
SELECT
    COUNT(*)                                                        AS total,
    COUNT(*) FILTER (WHERE tier = 'A')                              AS tier_a,
    COUNT(*) FILTER (WHERE a1_cmc_falsified)                        AS tier_a1_cmc,
    COUNT(*) FILTER (WHERE a2_cg_falsified)                         AS tier_a2_cg,
    COUNT(*) FILTER (WHERE a1_cmc_falsified AND a2_cg_falsified)    AS tier_a_both,
    COUNT(*) FILTER (WHERE tier = 'B')                              AS tier_b,
    COUNT(*) FILTER (WHERE tier = 'C')                              AS tier_c,
    COUNT(*) FILTER (WHERE tier = 'control' AND name_eq AND symbol_eq) AS both_eq,
    COUNT(*) FILTER (WHERE tier = 'X')                              AS proto_missing,
    COUNT(*) FILTER (WHERE tier = 'A' AND match_status = 'confirmed') AS tier_a_marked_confirmed,
    COUNT(*) FILTER (WHERE tier = 'V')                              AS tier_v_verified,
    COUNT(*) FILTER (WHERE cmc_confirms)                            AS v_by_cmc,
    COUNT(*) FILTER (WHERE cg_confirms AND NOT cmc_confirms)        AS v_by_cg_only
FROM tiered
"""

# 与文档保持一致的分组计数（用于交叉核对）
BUCKET_SQL = BASE_CTE + """
SELECT
    COUNT(*) FILTER (WHERE name_eq)                     AS name_eq,
    COUNT(*) FILTER (WHERE symbol_eq AND NOT name_eq)   AS symbol_only,
    COUNT(*) FILTER (WHERE NOT symbol_eq AND NOT name_eq) AS neither
FROM tiered
"""

DETAIL_SQL = BASE_CTE + """
SELECT tier, asset_id, canonical_name AS asset_name, canonical_symbol AS asset_symbol,
       cmc_keys, cg_keys, protocol_id, p_name AS protocol_name, p_symbol AS protocol_symbol,
       p_slug AS protocol_slug, p_category, p_cmc, p_gecko, match_status, match_confidence,
       a1_cmc_falsified, a2_cg_falsified, cmc_confirms, cg_confirms, name_eq, symbol_eq
FROM tiered
WHERE tier = %s
ORDER BY p_name, asset_id
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="dl 映射精度分层诊断（只读）。")
    parser.add_argument("--sample", type=int, default=8, help="摘要中每层展示的样本条数。")
    parser.add_argument("--export-md", type=str, default=None,
                        help="导出完整清单附录（Markdown）到指定路径。")
    return parser


def _md_table(rows: list[dict], cols: list[tuple[str, str]]) -> list[str]:
    lines = ["| " + " | ".join(h for _, h in cols) + " |",
             "|" + "|".join("---" for _ in cols) + "|"]
    for r in rows:
        cells = []
        for k, _ in cols:
            v = r.get(k)
            if isinstance(v, list):
                v = "、".join(str(x) for x in v) if v else "—"
            elif v is None:
                v = "—"
            cells.append(str(v).replace("|", "\\|"))
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def main() -> int:
    args = build_parser().parse_args()

    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    settings = get_settings(require_database=True)

    with get_connection(settings.database_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(SUMMARY_SQL)
            summary = dict(cur.fetchone())
            cur.execute(BUCKET_SQL)
            buckets = dict(cur.fetchone())
            details = {}
            for tier in ("V", "A", "B", "C", "control"):
                cur.execute(DETAIL_SQL, (tier,))
                details[tier] = [dict(r) for r in cur.fetchall()]

    a_rows = details["A"]
    a1 = [r for r in a_rows if r["a1_cmc_falsified"]]
    a2 = [r for r in a_rows if r["a2_cg_falsified"]]

    print(json.dumps({
        "summary": summary,
        "buckets": buckets,
        "tier_a_breakdown": {"A1_cmc_falsified": len(a1), "A2_cg_falsified": len(a2)},
        "samples": {
            "A1": a1[:args.sample],
            "A2": a2[:args.sample],
            "C": details["C"][:args.sample],
        },
    }, ensure_ascii=False, indent=2, default=str))

    if args.export_md:
        cols = [("protocol_name", "协议"), ("protocol_id", "dl id"),
                ("protocol_symbol", "协议 symbol"), ("p_cmc", "协议 cmc_id"),
                ("p_gecko", "协议 gecko_id"), ("protocol_slug", "slug"),
                ("asset_id", "落点 asset_id"), ("asset_name", "落点名称"),
                ("asset_symbol", "落点 symbol"), ("cmc_keys", "落点 cmc 键"),
                ("cg_keys", "落点 cg 键"), ("match_status", "match_status")]
        lines = [
            "# dl 映射精度诊断清单（附录）",
            "",
            "> 由 `scripts/bin/diag_dl_map_precision.py --export-md` 生成，**只读诊断，未修改任何数据**。",
            "> 分层口径见脚本 docstring 与方案文档 §4.6。",
            "",
            "## 1. 汇总",
            "",
            "| 指标 | 数量 |", "|---|---|",
            f"| dl 映射总量 | {summary['total']} |",
            f"| **Tier A 合计（一手判据证伪，去重后）** | **{summary['tier_a']}** |",
            f"| ├ 含 `cmc_id` 矛盾（全库） | {summary['tier_a1_cmc']} |",
            f"| ├ 含 `gecko_id` 矛盾（全库） | {summary['tier_a2_cg']} |",
            f"| └ 两者同时矛盾 | {summary['tier_a_both']} |",
            f"| 其中留痕为 `confirmed` | {summary['tier_a_marked_confirmed']} |",
            f"| Tier B：仅 symbol 相等、无一手判据 | {summary['tier_b']} |",
            f"| Tier C：名称与 symbol 皆不等 | {summary['tier_c']} |",
            f"| 对照：名称与 symbol 皆相等 | {summary['both_eq']} |",
            f"| **Tier V（一手判据证实，可作为重建基线）** | **{summary['tier_v_verified']}** |",
            f"| ├ 按 `cmc_id` 精确命中 | {summary['v_by_cmc']} |",
            f"| └ 仅按 `gecko_id` 命中 | {summary['v_by_cg_only']} |",
            f"| 协议已不在 `protocol_list`（无法判定） | {summary['proto_missing']} |",
            "",
            "分组交叉核对（与 P1-1/P1-4 文档 §4.6 一致）："
            f"名称相等 {buckets['name_eq']} / 仅符号相等 {buckets['symbol_only']} / 皆不等 {buckets['neither']}。",
            "",
            "> **读法**：Tier A 是**可证伪**清单（有矛盾的一手 id），Tier V 是**已证实**清单，"
            "二者互补。Tier B/C 只是「名称/符号表象」，**不是**错误清单——"
            "例如协议名/symbol 与落点均不相同的 `AITECH`(cmc 19055) → 资产 3355 "
            "`AITECH Cloud Network`(cmc 19055)，因 `cmc_id` 精确命中而属**正确**映射"
            "（已归入 Tier V，而非 Tier C）。",
            "",
            f"## 2. Tier A1 —— 协议 `cmc_id` 矛盾（全库 {summary['tier_a1_cmc']} 条，全部归属 Tier A）",
            "",
            "协议的 `cmc_id` 指向另一个 CMC 资产，而落点资产持有**不同**的 cmc 映射。",
            "",
        ]
        lines += _md_table(a1, cols)
        lines += ["",
                  f"## 3. Tier A2 —— 协议 `gecko_id` 矛盾（本层 {len(a2)} 条；"
                  f"全库 {summary['tier_a2_cg']} 条）",
                  "",
                  "协议的 `gecko_id` 不在落点资产的 cg 映射键中。",
                  f"全库 {summary['tier_a2_cg']} 条中另有 "
                  f"{summary['tier_a2_cg'] - len(a2)} 条**同时被 `cmc_id` 精确命中**（bootstrap 的 "
                  "Priority 1），已按分层优先级归入 Tier V，故不在下表。", ""]
        lines += _md_table(a2, cols)
        lines += ["", f"## 4. Tier C —— 名称与 symbol 皆不等（{len(details['C'])} 条）", ""]
        lines += _md_table(details["C"], cols)
        lines += ["", f"## 5. Tier B —— 仅 symbol 相等、无一手判据（{len(details['B'])} 条）", "",
                  "既不能证实也不能证伪，**需人工研判**。Tier B 与 Tier A 有交集的行已归入 Tier A。", ""]
        lines += _md_table(details["B"][:200], cols)
        if len(details["B"]) > 200:
            lines += ["", f"> 其余 {len(details['B']) - 200} 条因篇幅省略；"
                          "可用 `--export-md` 配合调整脚本内上限导出全量。"]
        out_path = Path(args.export_md)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"附录已写入: {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())