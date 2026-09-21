#!/usr/bin/env python3
"""CoinGecko 映射一致性校验（只读）。

背景（待修复工单 W1 / 审计 F2）：`core.asset_source_map` 中同一资产可能存在多条
`source_code='cg'` 映射（同名币 / 桥接币 / 已改名旧 id），且多数没有 `is_primary` 标记。
旧的 `fetchone()` 会取到任意一行，导致 AVA(1506) 的社交/市值取自 meme 币
`ansem-vs-alon`。社交采集侧已改为确定性择优（`phase_c_social_heat._CG_BEST_MATCH_SQL`），
本脚本用于离线巡检「择优后仍不匹配」的资产，异常即告警。

用法：
    python verify_cg_mapping.py                 # 打印异常 TOP N + 汇总，异常时退出码 1
    python verify_cg_mapping.py --limit 100     # 打印条数
    python verify_cg_mapping.py --json          # 机器可读输出

判定：
    - no_symbol_match：该资产所有 cg 候选中没有任何一个 coin_info.symbol 与
      canonical_symbol 一致（可能映射整体错配）。
    - ambiguous：有多条 cg 映射但没有 is_primary（依赖择优规则兜底）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

# 与 phase_c_social_heat._CG_BEST_MATCH_SQL 保持同一择优口径
BEST_MATCH = """
    (
        SELECT asm.source_asset_key
        FROM core.asset_source_map asm
        LEFT JOIN src_cg.coin_info ci ON ci.coin_id = asm.source_asset_key
        WHERE asm.asset_id = a.asset_id AND asm.source_code = 'cg'
        ORDER BY
            asm.is_primary DESC,
            (lower(ci.name) IS NOT DISTINCT FROM lower(a.canonical_name)) DESC,
            COALESCE(upper(ci.symbol) = upper(a.canonical_symbol), false) DESC,
            COALESCE(abs(ci.market_cap_rank - a.market_cap_rank), 999999),
            asm.source_asset_key
        LIMIT 1
    )
"""


def run(limit: int) -> dict:
    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            # 1) 择优后 symbol 仍不匹配（且确有 coin_info 记录）
            cur.execute(f"""
                WITH picked AS (
                    SELECT a.asset_id, a.canonical_symbol, a.canonical_name,
                           a.market_cap_rank, {BEST_MATCH} AS cg_id
                    FROM core.asset a
                    WHERE EXISTS (
                        SELECT 1 FROM core.asset_source_map m
                        WHERE m.asset_id = a.asset_id AND m.source_code = 'cg'
                    )
                )
                SELECT p.asset_id, p.canonical_symbol, p.canonical_name, p.cg_id,
                       ci.symbol AS cg_symbol, ci.name AS cg_name, p.market_cap_rank
                FROM picked p
                JOIN src_cg.coin_info ci ON ci.coin_id = p.cg_id
                WHERE ci.symbol IS NOT NULL AND ci.symbol <> ''
                  AND upper(ci.symbol) <> upper(p.canonical_symbol)
                ORDER BY p.market_cap_rank NULLS LAST
                LIMIT %s
            """, (limit,))
            mismatches = [
                {
                    "asset_id": r[0], "symbol": r[1], "name": r[2], "cg_id": r[3],
                    "cg_symbol": r[4], "cg_name": r[5], "market_cap_rank": r[6],
                }
                for r in cur.fetchall()
            ]

            # 2) 汇总计数
            cur.execute(f"""
                WITH picked AS (
                    SELECT a.asset_id, a.canonical_symbol, {BEST_MATCH} AS cg_id
                    FROM core.asset a
                    WHERE EXISTS (
                        SELECT 1 FROM core.asset_source_map m
                        WHERE m.asset_id = a.asset_id AND m.source_code = 'cg'
                    )
                )
                SELECT
                    count(*) AS total_with_cg,
                    count(*) FILTER (
                        WHERE ci.symbol IS NOT NULL AND ci.symbol <> ''
                          AND upper(ci.symbol) <> upper(p.canonical_symbol)
                    ) AS no_symbol_match
                FROM picked p
                LEFT JOIN src_cg.coin_info ci ON ci.coin_id = p.cg_id
            """)
            total, no_symbol_match = cur.fetchone()

            cur.execute("""
                SELECT count(*) FROM (
                    SELECT asset_id FROM core.asset_source_map WHERE source_code='cg'
                    GROUP BY asset_id HAVING count(*) > 1
                ) t
            """)
            ambiguous = cur.fetchone()[0]

            cur.execute("""
                SELECT count(*) FROM (
                    SELECT asset_id FROM core.asset_source_map WHERE source_code='cg'
                    GROUP BY asset_id HAVING count(*) FILTER (WHERE is_primary) = 0
                ) t
            """)
            no_primary = cur.fetchone()[0]

    return {
        "total_with_cg": total,
        "no_symbol_match": no_symbol_match,
        "ambiguous_multi_mapping": ambiguous,
        "no_primary": no_primary,
        "mismatch_samples": mismatches,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="CoinGecko 映射一致性校验（只读）")
    p.add_argument("--limit", type=int, default=50, help="异常样本打印条数")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    args = p.parse_args()

    try:
        result = run(args.limit)
    except Exception as e:
        print(f"校验失败: {e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print("=== CG 映射一致性 ===")
        print(f"有 cg 映射的资产        : {result['total_with_cg']}")
        print(f"择优后 symbol 仍不匹配   : {result['no_symbol_match']}")
        print(f"多条 cg 映射（需择优兜底）: {result['ambiguous_multi_mapping']}")
        print(f"无 is_primary 标记       : {result['no_primary']}")
        if result["mismatch_samples"]:
            print("\n异常样本（按市值排名）：")
            for m in result["mismatch_samples"]:
                print(f"  asset_id={m['asset_id']} {m['symbol']} ({m['name']}) "
                      f"-> cg_id={m['cg_id']} (symbol={m['cg_symbol']}, name={m['cg_name']})")

    return 1 if result["no_symbol_match"] > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
