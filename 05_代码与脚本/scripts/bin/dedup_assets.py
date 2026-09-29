"""合并 core.asset 中「完全同名」的真重复记录（symbol + canonical_name 完全相同）。

背景：core.asset 存在若干组完全同名重复，每组通常是一条 CoinGecko 来源的空壳记录
（无合约地址）+ 一条 CMC 来源的有合约记录，同一项目被两个数据源拆成了两条。
本脚本把这些记录合并为一条，迁移所有关联数据后删除冗余记录。

安全策略：
- 只处理「完全同名」重复（symbol + canonical_name 完全相同），无歧义。
- 每组选一条 keep（有主合约优先，其次合约数/来源映射数/文档数，最后 asset_id 小）。
- 其余作为 drop：迁移其关联数据到 keep，再删除。
- 每组独立事务：一组失败不影响其他组；失败组回滚并记录，供人工复查。

韧性（2026-09-28，修 data_sync_daily 因 lock_timeout 失败）：
- 删除 core.asset 会触发 ~42 张子表的外键级联/检查（单次 DELETE 实测 ~3.4s），
  与同窗（06:30）运行的 derivatives_batch 等写子表任务并发时会撞锁。
- 全局连接池默认 lock_timeout=30s 对该清理任务过紧：一次瞬时锁竞争就让整条
  data_sync_daily 判失败（失败即终止后续 13 个子任务）。故本脚本：
  ① 本任务连接单独放宽 lock_timeout（`--lock-timeout-ms`，默认 120s）；
  ② 锁竞争错误（lock_timeout 55P03 / 死锁 40P01）按 5/15/30s 退避重试至多 3 次；
  ③ 失败信息带「出错语句」上下文，便于定位是哪张表/哪一步被锁。
  ④ **锁耗尽不算硬失败（2026-09-29 告警收敛）**：同名去重是幂等的机会性清理，06:30
     与 derivatives_batch 同窗时偶发撞锁属常态；重试耗尽后**跳过该组**（打印 [WARN]，
     次日再试），退出码仍为 0 —— 避免整条 data_sync_daily 被"任务自身失败"钉住并反复告警。
     仅**非锁竞争**的真实错误（约束冲突/数据异常等）才返回非 0（保持失败可见）。

用法：
  python dedup_assets.py --dry-run   预览合并计划，不写库
  python dedup_assets.py --apply     执行合并
  python dedup_assets.py --apply --lock-timeout-ms 180000   # 容忍更长等锁
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg
import psycopg.errors
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
    "biz.asset_token_holders",      # PK(asset_id), NO ACTION
    "biz.asset_social_heat",        # PK(asset_id), NO ACTION
    "biz.coin_basic",               # PK(asset_id), NO ACTION
]

# 默认等锁上限（毫秒）：全局连接池默认 30s 对「删 core.asset 触发大量 FK 级联」过紧。
DEFAULT_LOCK_TIMEOUT_MS = 120_000
LOCK_RETRIES = 3
LOCK_RETRY_BACKOFF_SEC = (5, 15, 30)

# 最近一次执行的语句标签（出错时随异常带出，定位是哪张表/哪一步被锁）。
_LAST_STMT = {"label": "?"}


def _run(cur, label: str, sql: str, params: tuple | None = None) -> int:
    """执行一条语句并记录标签；返回 rowcount。异常原样抛出（保留 sqlstate 供重试判定）。"""
    _LAST_STMT["label"] = label
    cur.execute(sql, params) if params is not None else cur.execute(sql)
    return cur.rowcount


def is_lock_error(e: BaseException) -> bool:
    """锁竞争类错误（可安全重试）：lock_timeout(55P03) / 死锁(40P01)。"""
    if isinstance(e, (psycopg.errors.LockNotAvailable, psycopg.errors.DeadlockDetected)):
        return True
    return getattr(e, "sqlstate", None) in ("55P03", "40P01")


def _retry_wait(attempt: int) -> int:
    """第 attempt 次重试前的等待秒数（attempt 从 0 计）。"""
    return LOCK_RETRY_BACKOFF_SEC[min(attempt, len(LOCK_RETRY_BACKOFF_SEC) - 1)]


def _stmt_label(e: BaseException) -> str:
    return getattr(e, "dedup_stmt", "?")


def _connect_direct(settings, lock_timeout_ms: int):
    """本任务专用直连（不借用全局连接池）：单独设置 lock_timeout，避免污染池内连接。"""
    return psycopg.connect(
        settings.database_url,
        connect_timeout=30,
        options=f"-c lock_timeout={int(lock_timeout_ms)}",
    )


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
    _run(cur, "读取 keep 赛道", "SELECT primary_sector FROM core.asset WHERE asset_id = %s",
         (keep_id,))
    keep_sector = cur.fetchone()["primary_sector"]
    _run(cur, "读取 drop 赛道", "SELECT primary_sector FROM core.asset WHERE asset_id = %s",
         (drop_id,))
    drop_sector = cur.fetchone()["primary_sector"]
    if (keep_sector in (None, "other")) and (drop_sector not in (None, "other")):
        _run(cur, "提升 keep 主赛道",
             "UPDATE core.asset SET primary_sector = %s, updated_at = NOW() WHERE asset_id = %s",
             (drop_sector, keep_id))


def _apply_group(conn, group: dict) -> dict:
    """在独立事务中合并一组；返回执行统计。失败时异常带 dedup_stmt 上下文后原样抛出。"""
    keep_id = group["keep"]["asset_id"]
    stats = {"keep": keep_id, "drops": [], "migrated_rows": 0, "errors": []}

    cur = conn.cursor(row_factory=psycopg.rows.dict_row)
    for drop in group["drops"]:
        drop_id = drop["asset_id"]
        try:
            # 0) doc_source_entry 去重：drop 与 keep 收录了相同 URL 的文档时，
            #    删 drop 的重复行（保留 keep），否则后续 UPDATE 会违反
            #    uq_biz_doc_source_entry_entity_url 唯一约束。
            stats["migrated_rows"] += _run(cur, "doc_source_entry 去重", """
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
                    stats["migrated_rows"] += _run(cur, f"{tbl} 冲突行删除", f"""
                        DELETE FROM {tbl} d
                        USING {tbl} k
                        WHERE d.asset_id = %s AND k.asset_id = %s
                          AND {join_cond}
                    """, (drop_id, keep_id))

                stats["migrated_rows"] += _run(
                    cur, f"{tbl} 迁移",
                    f"UPDATE {tbl} SET asset_id = %s WHERE asset_id = %s",
                    (keep_id, drop_id))

            # 2) 单行表：keep 无记录时迁移，随后清掉 drop 残留
            for tbl in SINGLE_TABLES:
                stats["migrated_rows"] += _run(
                    cur, f"{tbl} 单行迁移",
                    f"UPDATE {tbl} SET asset_id = %s WHERE asset_id = %s "
                    f"AND NOT EXISTS (SELECT 1 FROM {tbl} k WHERE k.asset_id = %s)",
                    (keep_id, drop_id, keep_id))
                stats["migrated_rows"] += _run(
                    cur, f"{tbl} 残留清理",
                    f"DELETE FROM {tbl} WHERE asset_id = %s", (drop_id,))

            # 3) 合并主赛道
            _merge_primary_sector(cur, keep_id, drop_id)

            # 4) 删除冗余资产（触发 ~42 张子表 FK 级联/检查，是撞锁高发语句）
            stats["migrated_rows"] += _run(
                cur, "删除冗余 core.asset",
                "DELETE FROM core.asset WHERE asset_id = %s", (drop_id,))
            stats["drops"].append(drop_id)
        except Exception as e:  # 单组回滚由外层 rollback 处理
            try:
                e.dedup_stmt = _LAST_STMT["label"]
            except Exception:
                pass
            stats["errors"].append(f"{drop_id}: [{_LAST_STMT['label']}] {e}")
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
    ap.add_argument("--lock-timeout-ms", type=int, default=DEFAULT_LOCK_TIMEOUT_MS,
                    help=f"本任务等锁上限（毫秒，默认 {DEFAULT_LOCK_TIMEOUT_MS}；"
                         f"全局连接池默认 30s 对本清理任务过紧）")
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

    # apply：每组独立事务；锁竞争按 5/15/30s 退避重试至多 LOCK_RETRIES 次
    ok, failed_lock, failed_other = 0, 0, 0
    for g in groups:
        last_err: BaseException | None = None
        for attempt in range(LOCK_RETRIES + 1):
            try:
                with _connect_direct(settings, args.lock_timeout_ms) as conn:
                    stats = _apply_group(conn, g)
                ok += 1
                last_err = None
                print(f"[OK] {g['symbol']} 合并完成: keep={stats['keep']}, "
                      f"drops={stats['drops']}, 迁移 {stats['migrated_rows']} 行")
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                if is_lock_error(e) and attempt < LOCK_RETRIES:
                    wait = _retry_wait(attempt)
                    print(f"[RETRY] {g['symbol']} 撞锁超时（{_stmt_label(e)}）⇒ "
                          f"{wait}s 后重试 {attempt + 1}/{LOCK_RETRIES}", flush=True)
                    time.sleep(wait)
                    continue
                break
        if last_err is not None:
            if is_lock_error(last_err):
                # 锁竞争是并发清理的常态（06:30 与 derivatives_batch 同窗）、幂等且次日重试
                # ⇒ 不计硬失败，避免整条 data_sync_daily 长期被判 failed 并反复告警。
                failed_lock += 1
                print(f"[WARN] {g['symbol']} 撞锁重试已耗尽，本轮跳过（幂等，次日重试）: "
                      f"[{_stmt_label(last_err)}] {last_err}")
            else:
                failed_other += 1
                print(f"[FAIL] {g['symbol']} 合并失败（已回滚，错误不重试）: "
                      f"[{_stmt_label(last_err)}] {last_err}")

    print(f"\n完成：成功 {ok} 组，撞锁跳过 {failed_lock} 组，失败 {failed_other} 组。")
    # 仅真实错误（非锁竞争）返回非 0；锁耗尽按「本轮跳过」处理（退出码 0）。
    return 2 if failed_other else 0


if __name__ == "__main__":
    raise SystemExit(main())
