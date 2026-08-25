"""合并 core.asset 中「完全同名」的真重复记录（symbol + canonical_name 完全相同）。

背景：core.asset 存在 19 组完全同名重复（约 38 条），每组是一条 CoinGecko 来源
的空壳记录（无合约地址）+ 一条 CMC 来源的有合约记录，同一项目被两个数据源拆成了
两条。本脚本把这些记录合并为一条，迁移所有关联数据后删除冗余记录。

安全策略：
- 只处理「完全同名」重复（symbol + canonical_name 完全相同），无歧义。
- 每组选一条 keep（有主合约优先，其次合约数/来源映射数/文档数，最后 asset_id 小）。
- 其余作为 drop：迁移其关联数据到 keep，再删除。
- 每组独立事务：一组失败不影响其他组；失败组回滚并记录，供人工复查。

用法：
  python dedup_assets.py --dry-run   预览合并计划，不写库
  python dedup_assets.py --apply     执行合并
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg.rows
from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

# 多行关联表：asset_id 无唯一约束（或唯一键与 asset_id 无关），直接 UPDATE。
# 注意：asset_sector / asset_market_daily 有复合主键，迁移前需先删冲突行。
MANY_TABLES = [
    "core.asset_contract",          # NO ACTION, UNIQUE(chain, contract_address)
    "core.asset_source_map",        # CASCADE
    "core.protocol_asset_link",     # CASCADE
    "core.asset_contract_map",      # 无外键
    "biz.doc_source_entry",         # CASCADE（已在 _apply_group 中单独去重）
    "biz.doc_asset",                # CASCADE
    "biz.doc_crawl_staging",        # CASCADE
    "biz.doc_source_notebooklm",    # CASCADE, PK(asset_id, source_entry_id)
    "biz.asset_hacks",              # CASCADE
    "biz.asset_raises",             # CASCADE
    "biz.asset_sector",             # CASCADE, PK(asset_id, sector, source) — 有冲突需先删
    "biz.asset_tokenomics",         # CASCADE
    "biz.asset_unlock_event",       # CASCADE
    "biz.asset_market_daily",       # CASCADE, PK(asset_id, market_date, source_code) — 有冲突需先删
    "biz.onchain_holder_snapshot",  # CASCADE
    "biz.onchain_transfer_log",     # 无外键
    "biz.research_url",             # 无外键, UNIQUE(asset_id, url)
]

# 有复合唯一键的多行表：迁移前先删 drop 中与 keep 冲突的行（保留 keep 的）。
# key 是表名，value 是除 asset_id 外的主键列列表。
CONFLICT_AWARE_TABLES = {
    "biz.asset_sector": ["sector", "source"],
    "biz.asset_market_daily": ["market_date", "source_code"],
    "biz.research_url": ["url"],
    "core.asset_contract": ["chain", "contract_address"],
}

# 单行关联表：PK/UNIQUE(asset_id)，迁移时先「条件更新」再「删 drop 残留」。
SINGLE_TABLES = [
    "biz.unlock_watchlist",         # UNIQUE(asset_id)
    "biz.research_notebook",        # UNIQUE(asset_id)
    "biz.research_target",          # PK(asset_id)
    "biz.asset_token_unlocks",      # PK(asset_id), NO ACTION
    "biz.asset_social_heat",        # PK(asset_id), NO ACTION
    "biz.coin_basic",               # PK(asset_id), NO ACTION
]


def _load_groups(conn) -> list[dict]:
    """加载完全同名重复组，每条资产附带特征用于选 keep。"""
    cur = conn.cursor(row_factory=psycopg.rows.dict_row)
    cur.execute("""
        SELECT a.asset_id, a.canonical_symbol, a.canonical_name,
               a.asset_type, a.primary_sector,
               (SELECT count(*) FROM core.asset_contract c
                 WHERE c.asset_id = a.asset_id) AS n_contracts,
               (SELECT count(*) FROM core.asset_contract c
                 WHERE c.asset_id = a.asset_id AND c.is_primary) AS n_primary_contracts,
               (SELECT count(*) FROM core.asset_source_map m
                 WHERE m.asset_id = a.asset_id) AS n_maps,
               (SELECT count(*) FROM biz.doc_source_entry e
                 WHERE e.asset_id = a.asset_id) AS n_docs
        FROM core.asset a
        WHERE (a.canonical_symbol, a.canonical_name) IN (
            SELECT canonical_symbol, canonical_name FROM core.asset
            GROUP BY canonical_symbol, canonical_name HAVING count(*) > 1
        )
        ORDER BY a.canonical_symbol, a.asset_id
    """)
    rows = cur.fetchall()

    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        key = (r["canonical_symbol"], r["canonical_name"])
        groups.setdefault(key, []).append(r)

    result = []
    for (symbol, name), assets in groups.items():
        # keep 排序：主合约 > 合约数 > 来源映射数 > 文档数 > asset_id 小
        assets_sorted = sorted(
            assets,
            key=lambda x: (
                -int(x["n_primary_contracts"] or 0),
                -int(x["n_contracts"] or 0),
                -int(x["n_maps"] or 0),
                -int(x["n_docs"] or 0),
                x["asset_id"],
            ),
        )
        keep = assets_sorted[0]
        drops = assets_sorted[1:]
        result.append({
            "symbol": symbol,
            "name": name,
            "keep": keep,
            "drops": drops,
        })
    return result


def _merge_primary_sector(cur, keep_id: int, drop_id: int) -> None:
    """若 keep 赛道为 other 且 drop 有更具体赛道，则提升到 keep。"""
    cur.execute(
        "SELECT primary_sector FROM core.asset WHERE asset_id = %s", (keep_id,),
    )
    keep_sector = cur.fetchone()["primary_sector"]
    cur.execute(
        "SELECT primary_sector FROM core.asset WHERE asset_id = %s", (drop_id,),
    )
    drop_sector = cur.fetchone()["primary_sector"]
    if (keep_sector in (None, "other")) and (drop_sector not in (None, "other")):
        cur.execute(
            "UPDATE core.asset SET primary_sector = %s, updated_at = NOW() WHERE asset_id = %s",
            (drop_sector, keep_id),
        )


def _apply_group(conn, group: dict) -> dict:
    """在独立事务中合并一组；返回执行统计。"""
    keep_id = group["keep"]["asset_id"]
    stats = {"keep": keep_id, "drops": [], "migrated_rows": 0, "errors": []}

    cur = conn.cursor(row_factory=psycopg.rows.dict_row)
    for drop in group["drops"]:
        drop_id = drop["asset_id"]
        try:
            # 0) doc_source_entry 去重：drop 与 keep 收录了相同 URL 的文档时，
            #    删 drop 的重复行（保留 keep），否则后续 UPDATE 会违反
            #    uq_biz_doc_source_entry_entity_url 唯一约束。
            cur.execute("""
                DELETE FROM biz.doc_source_entry d
                USING biz.doc_source_entry k
                WHERE d.asset_id = %s AND k.asset_id = %s
                  AND d.entity_type = k.entity_type
                  AND d.entry_url = k.entry_url
                  AND COALESCE(d.protocol_id, -1) = COALESCE(k.protocol_id, -1)
            """, (drop_id, keep_id))

            # 1) 多行表：有复合唯一键的先删冲突行，再迁移
            for tbl in MANY_TABLES:
                # 冲突感知表：先删 drop 中与 keep 键重复的行（保留 keep）
                if tbl in CONFLICT_AWARE_TABLES:
                    key_cols = CONFLICT_AWARE_TABLES[tbl]
                    join_cond = " AND ".join(f"d.{c} = k.{c}" for c in key_cols)
                    cur.execute(f"""
                        DELETE FROM {tbl} d
                        USING {tbl} k
                        WHERE d.asset_id = %s AND k.asset_id = %s
                          AND {join_cond}
                    """, (drop_id, keep_id))

                cur.execute(
                    f"UPDATE {tbl} SET asset_id = %s WHERE asset_id = %s",
                    (keep_id, drop_id),
                )
                stats["migrated_rows"] += cur.rowcount

            # 2) 单行表：keep 无记录时迁移，随后清掉 drop 残留
            for tbl in SINGLE_TABLES:
                cur.execute(
                    f"UPDATE {tbl} SET asset_id = %s WHERE asset_id = %s "
                    f"AND NOT EXISTS (SELECT 1 FROM {tbl} k WHERE k.asset_id = %s)",
                    (keep_id, drop_id, keep_id),
                )
                stats["migrated_rows"] += cur.rowcount
                cur.execute(f"DELETE FROM {tbl} WHERE asset_id = %s", (drop_id,))

            # 3) 合并主赛道
            _merge_primary_sector(cur, keep_id, drop_id)

            # 4) 删除冗余资产
            cur.execute("DELETE FROM core.asset WHERE asset_id = %s", (drop_id,))
            stats["drops"].append(drop_id)
        except Exception as e:  # 单组回滚由外层 conn.rollback 处理
            stats["errors"].append(f"{drop_id}: {e}")
            raise
    return stats


def _count_drop_rows(cur, drop_id: int) -> dict:
    """dry-run 时统计 drop 在各关联表中的行数。"""
    counts = {}
    for tbl in MANY_TABLES + SINGLE_TABLES:
        cur.execute(f"SELECT count(*) AS n FROM {tbl} WHERE asset_id = %s", (drop_id,))
        n = cur.fetchone()["n"]
        if n:
            counts[tbl] = n
    return counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="预览合并计划，不写库")
    ap.add_argument("--apply", action="store_true", help="执行合并")
    args = ap.parse_args()

    if not (args.dry_run or args.apply):
        ap.print_help()
        return 1

    settings = get_settings()

    # 先加载分组（读操作）
    with get_connection(settings.database_url) as conn:
        groups = _load_groups(conn)

    total_drops = sum(len(g["drops"]) for g in groups)
    print(f"发现 {len(groups)} 组完全同名重复，共 {total_drops} 条待删冗余记录。\n")

    if args.dry_run:
        with get_connection(settings.database_url) as conn:
            cur = conn.cursor(row_factory=psycopg.rows.dict_row)
            for g in groups:
                k = g["keep"]
                print(f"[{g['symbol']}] {g['name']}")
                print(f"  KEEP  asset_id={k['asset_id']} type={k['asset_type']} "
                      f"sector={k['primary_sector']} contracts={k['n_contracts']} "
                      f"maps={k['n_maps']} docs={k['n_docs']}")
                for d in g["drops"]:
                    rows = _count_drop_rows(cur, d["asset_id"])
                    print(f"  DROP  asset_id={d['asset_id']} type={d['asset_type']} "
                          f"sector={d['primary_sector']} contracts={d['n_contracts']} "
                          f"maps={d['n_maps']} docs={d['n_docs']} -> 关联行 {rows}")
                print()
        print("DRY-RUN 完成，未做任何修改。确认无误后运行 --apply。")
        return 0

    # apply：每组独立事务
    ok, failed = 0, 0
    for g in groups:
        try:
            with get_connection(settings.database_url) as conn:
                stats = _apply_group(conn, g)
            ok += 1
            print(f"[OK] {g['symbol']} 合并完成: keep={stats['keep']}, drops={stats['drops']}, "
                  f"迁移 {stats['migrated_rows']} 行")
        except Exception as e:
            failed += 1
            print(f"[FAIL] {g['symbol']} 合并失败（已回滚）: {e}")

    print(f"\n完成：成功 {ok} 组，失败 {failed} 组。")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
