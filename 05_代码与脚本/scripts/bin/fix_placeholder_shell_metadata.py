"""P1-4 存量修复（附）：清理「无符号协议壳」上错配的合约元数据。

背景
----
2026-07-31 的单一批次里，大量 DefiLlama 协议被批量映射到占位资产
（`canonical_symbol` 为 `''`/`'-'`/`'?'`）。其中 asset_id=11125（Aztec Connect）
是 7 个壳里**唯一没有任何 dl 来源映射**的孤儿壳，却留下了 11 行
`core.asset_contract`（全部 `created_at = 2026-07-31 05:43:38.192568`）：

- 7 行地址是**字面量** `'null'` / `''`（cardano/map/ethereum/bsc/klaytn/milkomeda/proton）
- 4 行是真地址，但归属**另外四个协议**——
  `0x160b1e…` → StakeHound(279)、`0x7c7022…` → Jadeswap(3290)、
  `0x992bad…` → AstarFarm(1821)、`0x8b1ea8…` → Nexapia(6458)，无一是 Aztec

危害不是「多几行垃圾」，而是**派生链**：`biz.coin_basic.main_chain` /
`primary_contract_address` 取 `core.asset_contract` 中 `is_primary DESC, contract_id`
的首行，于是 11125 的 `main_chain` 变成 `cardano`、`primary_contract_address`
变成字符串 `'null'`；[ai_signal_analyzer.py](file:///e:/瞎搞乱搞/web3/加密货币研究报告/05_代码与脚本/workbench/ai_signal_analyzer.py#L601-L618)
会把它们读进 AI 画像的 `basic.main_chain` / `basic.primary_contract`，并按 main_chain
去匹配持仓分布（同文件 L837-L839）。

处理策略
--------
1. 备份受影响行（`core.asset_contract_shell_bak_*` + `biz.coin_basic_shell_bak_*`）
2. 删除满足**任一**条件的 `core.asset_contract` 行（仅限壳资产，见下）：
   - `invalid_address`：地址为字面量 `'null'` / `''`（不是地址）
   - `orphan_no_dl_source`：该壳资产**没有任何 dl 来源映射**——即
     `POPULATE_FROM_DL`（[phase_a_build_core.py:252-323](file:///e:/瞎搞乱搞/web3/加密货币研究报告/05_代码与脚本/scripts/bin/phase_a_build_core.py#L252-L323)
     要求 `INNER JOIN core.asset_source_map`）在当前数据下**永远不会**重建该行，
     故它是历史错配残留
3. 按删除后的合约集重新派生受影响资产的 `biz.coin_basic.main_chain` /
   `primary_contract_address`（通常变为 NULL，即「未知」的诚实状态）

**为何必须删 `core.asset_contract` 而不是只改 `biz.coin_basic`**：`coin_basic` 由
`phase_a_build_core.py` 的 `REFRESH_COIN_BASIC` 全量重派生（`ON CONFLICT DO UPDATE`），
且被 `run_cmc_pipeline.py` 第⑨步定期调用 —— 只改 `coin_basic` 会在下次管线运行时被回填。

**本脚本不动 `biz.coin_basic.defillama_slug`**：该列全库 3110 条非空值**全部是数字**
（即 DL `protocol_id`，取自 `core.asset_source_map.source_asset_key`），是列名与口径
不符的**全局设计**而非壳资产专属脏数据；只清 6 个壳会让它们与另外 3104 行不一致。
该列全仓**零消费者**，如需纠正属独立工作项（全局改口径或改名），见方案文档 §4.6。

**本脚本不动 `core.asset` 本体**：11125 挂着 122 行真实安全事件
（`biz.asset_hacks`），删除资产会经 `ON DELETE CASCADE` 一并抹掉。

用法
----
    python fix_placeholder_shell_metadata.py              # 只统计（dry-run，默认）
    python fix_placeholder_shell_metadata.py --apply      # 执行（先自动备份）
    python fix_placeholder_shell_metadata.py --apply --no-backup
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

# 与 phase_b2_third_party_raises.py / fix_asset_raises_placeholder.py 保持同一口径
PLACEHOLDER_PREDICATE = (
    "a.canonical_symbol IS NOT NULL AND TRIM(a.canonical_symbol) NOT IN ('', '-', '?')"
)

# 待删行的统一筛选（仅限壳资产；两个条件互斥，各自可独立复核）
TARGET_CONTRACTS_SQL = f"""
    SELECT ac.contract_id, ac.asset_id, a.canonical_name, a.canonical_symbol,
           ac.chain, ac.contract_address, ac.source_code, ac.created_at,
           CASE
               WHEN ac.contract_address IN ('null', '')
                    OR TRIM(ac.contract_address) = ''
               THEN 'invalid_address'
               WHEN NOT EXISTS (
                   SELECT 1 FROM core.asset_source_map asm
                   WHERE asm.source_code = 'dl' AND asm.asset_id = ac.asset_id
               )
               THEN 'orphan_no_dl_source'
           END AS reason
    FROM core.asset_contract ac
    INNER JOIN core.asset a ON a.asset_id = ac.asset_id
    WHERE NOT ({PLACEHOLDER_PREDICATE})
      AND (
          ac.contract_address IN ('null', '')
          OR TRIM(ac.contract_address) = ''
          OR NOT EXISTS (
              SELECT 1 FROM core.asset_source_map asm
              WHERE asm.source_code = 'dl' AND asm.asset_id = ac.asset_id
          )
      )
    ORDER BY ac.asset_id, ac.chain, ac.contract_id
"""

# 与 phase_a_build_core.py::REFRESH_COIN_BASIC 的同口径派生（仅限指定资产）
REDERIVE_COIN_BASIC_SQL = """
    UPDATE biz.coin_basic cb
    SET main_chain = d.chain,
        primary_contract_address = d.contract_address,
        last_refreshed_at = NOW()
    FROM (
        SELECT %s::bigint AS asset_id,
               (SELECT ac.chain FROM core.asset_contract ac
                 WHERE ac.asset_id = %s
                 ORDER BY ac.is_primary DESC, ac.contract_id LIMIT 1) AS chain,
               (SELECT ac.contract_address FROM core.asset_contract ac
                 WHERE ac.asset_id = %s
                 ORDER BY ac.is_primary DESC, ac.contract_id LIMIT 1) AS contract_address
    ) d
    WHERE cb.asset_id = d.asset_id
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="清理壳资产上错配的 core.asset_contract 行并重派生 coin_basic 元数据。")
    parser.add_argument("--apply", action="store_true", help="实际写入（默认仅统计）。")
    parser.add_argument("--no-backup", action="store_true", help="跳过备份表创建。")
    return parser


def _fetch_targets(cur) -> list[dict]:
    cur.execute(TARGET_CONTRACTS_SQL)
    return [dict(row) for row in cur.fetchall()]


def _fetch_coin_basic_before(cur, asset_ids: list[int]) -> list[dict]:
    if not asset_ids:
        return []
    cur.execute("""
        SELECT asset_id, coin_symbol, coin_name, main_chain,
               primary_contract_address, defillama_slug
        FROM biz.coin_basic WHERE asset_id = ANY(%s) ORDER BY asset_id
    """, (asset_ids,))
    return [dict(row) for row in cur.fetchall()]


def main() -> int:
    args = build_parser().parse_args()

    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    settings = get_settings(require_database=True)
    today = date.today().strftime("%Y%m%d")

    with get_connection(settings.database_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            targets = _fetch_targets(cur)
            asset_ids = sorted({r["asset_id"] for r in targets})
            cb_before = _fetch_coin_basic_before(cur, asset_ids)

    summary = {
        "contract_rows_to_delete": len(targets),
        "assets_affected": asset_ids,
        "by_reason": {},
        "by_asset": {},
        "coin_basic_before": cb_before,
    }
    for r in targets:
        summary["by_reason"][r["reason"]] = summary["by_reason"].get(r["reason"], 0) + 1
        summary["by_asset"][str(r["asset_id"])] = summary["by_asset"].get(str(r["asset_id"]), 0) + 1

    if not targets:
        print(json.dumps({"status": "clean", **summary}, ensure_ascii=False, indent=2, default=str))
        return 0

    if not args.apply:
        print(json.dumps({"mode": "dry-run", **summary, "targets": targets},
                         ensure_ascii=False, indent=2, default=str))
        return 0

    with get_connection(settings.database_url) as wconn:
        if not args.no_backup:
            ac_bak = f"core.asset_contract_shell_bak_{today}"
            cb_bak = f"biz.coin_basic_shell_bak_{today}"
            with wconn.cursor() as cur:
                cur.execute(f"CREATE TABLE IF NOT EXISTS {ac_bak} AS "
                            f"SELECT * FROM core.asset_contract WHERE FALSE")
                cur.execute(
                    f"INSERT INTO {ac_bak} SELECT * FROM core.asset_contract "
                    f"WHERE contract_id = ANY(%s)", ([r["contract_id"] for r in targets],))
                cur.execute(f"CREATE TABLE IF NOT EXISTS {cb_bak} AS "
                            f"SELECT * FROM biz.coin_basic WHERE FALSE")
                if asset_ids:
                    cur.execute(
                        f"INSERT INTO {cb_bak} SELECT * FROM biz.coin_basic "
                        f"WHERE asset_id = ANY(%s)", (asset_ids,))
            summary["backup_tables"] = [ac_bak, cb_bak]

        with wconn.cursor() as cur:
            cur.execute("DELETE FROM core.asset_contract WHERE contract_id = ANY(%s)",
                        ([r["contract_id"] for r in targets],))
            summary["contract_rows_deleted"] = cur.rowcount
            for aid in asset_ids:
                cur.execute(REDERIVE_COIN_BASIC_SQL, (aid, aid, aid))
            summary["coin_basic_rederived"] = len(asset_ids)
        # get_connection 退出时统一 commit

    with get_connection(settings.database_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("SELECT COUNT(*) AS n FROM core.asset_contract WHERE asset_id = ANY(%s)",
                        (asset_ids,))
            summary["contract_rows_left"] = cur.fetchone()["n"]
            cur.execute("""
                SELECT asset_id, coin_name, main_chain, primary_contract_address
                FROM biz.coin_basic WHERE asset_id = ANY(%s) ORDER BY asset_id
            """, (asset_ids,))
            summary["coin_basic_after"] = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT COUNT(*) AS n FROM biz.asset_hacks WHERE asset_id = ANY(%s)",
                        (asset_ids,))
            summary["hacks_events_preserved"] = cur.fetchone()["n"]

    summary["status"] = "applied"
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())