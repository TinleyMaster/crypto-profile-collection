from __future__ import annotations

from typing import Any

# CoinGecko 映射确定性择优（单一真源）。
#
# 背景：core.asset_source_map 中同一资产可能有多条 source_code='cg' 映射
# （同名币 / 桥接币 / 已改名旧 id），且多数没有 is_primary 标记。旧代码用
# fetchone() 任取首行，导致 AVA(1506) 取到 meme 币 'ansem-vs-alon'
# （审计 F2 / 工单 W1、W4）。择优顺序：
#   1. is_primary = TRUE
#   2. coin_info.name 与资产 canonical_name 完全一致（区分同名币）
#   3. coin_info.symbol 与 canonical_symbol 一致
#   4. coin_info.market_cap_rank 与资产 market_cap_rank 最接近
#   5. source_asset_key 字典序（确定性兜底）
_CG_BEST_MATCH_SQL = """
    SELECT asm.source_asset_key
    FROM core.asset a
    JOIN core.asset_source_map asm
      ON asm.asset_id = a.asset_id AND asm.source_code = 'cg'
    LEFT JOIN src_cg.coin_info ci ON ci.coin_id = asm.source_asset_key
    WHERE a.asset_id = %s
    ORDER BY
        asm.is_primary DESC,
        (lower(ci.name) IS NOT DISTINCT FROM lower(a.canonical_name)) DESC,
        COALESCE(upper(ci.symbol) = upper(a.canonical_symbol), false) DESC,
        COALESCE(abs(ci.market_cap_rank - a.market_cap_rank), 999999),
        asm.source_asset_key
    LIMIT 1
"""


def resolve_cg_coin_id(cur, asset_id: int) -> str | None:
    """按确定性规则返回该资产最优的 CoinGecko coin_id（无映射返回 None）。

    ``cur`` 可为 psycopg 游标（dict_row 或元组行均可）。
    """
    cur.execute(_CG_BEST_MATCH_SQL, (asset_id,))
    row: Any = cur.fetchone()
    if not row:
        return None
    if isinstance(row, dict):
        return row.get("source_asset_key")
    return row[0]
