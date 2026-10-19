#!/usr/bin/env python3
"""
存量催化剂资产关联展开（linker 全量重算）。

问题：`biz.catalyst_asset_link` 只有部分催化剂有 N:N 关联，
      多交易对公告未展开到关联表（`asset_catalyst.asset_id` 只存单个主资产）。

修法：对所有催化剂重跑 → **生产同一条归属代码路径** → 写入 catalyst_asset_link。

用法：
    python scripts/bin/backfill_catalyst_links.py --dry-run [--max-items 100]
    python scripts/bin/backfill_catalyst_links.py               # 增量（只补空，不删）
    python scripts/bin/backfill_catalyst_links.py --prune       # 重算并剔除失效关联（写库！）

━━━ P3-F 单一真相收敛（2026-10-16）━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
本脚本原先把 linker 全套逻辑（~260 行）**内联复制**在文件里，并注明「手动同步」。
P3-C 归属护栏改造后，这份副本**已经落后**（仍是：
  · 宽口径 gate（含 `现货/合约/交易所/spot` ⇒ 商品/美股稿恒过闸）
  · `len(base) >= 2` 死门（裸 symbol `CT` / 单字符 `W` 全漏）
  · 旧撞名清单（缺 SNPS/RTX/B3）
  · 无弱信号护栏（裸 symbol 无标题佐证即认领）
⇒ 一旦重跑，会以**旧 bug 覆盖新护栏**，重演 582 条错连事故。

现改为**单一真相**：import `workbench/catalyst` 里生产的同一函数，
归属结果与生产摄入**逐字节一致**。全部内联副本删除。
（`_resolve_asset_ids` 即 `pipeline._insert_catalyst` 调用的同一个函数。）

⚠️ 本脚本为运维工具，**不在 scheduler 调度内**，须手工执行。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
# prod 结构: /app/scripts/bin/ → /app/（workbench 文件直接在 /app/ 下）
# 本地结构: .../scripts/bin/ → .../workbench/
_candidate = SCRIPT_DIR.parent.parent / "workbench"
WORKBENCH_DIR = _candidate if _candidate.exists() else SCRIPT_DIR.parent.parent

sys.path.insert(0, str(WORKBENCH_DIR))
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

import psycopg.rows  # noqa: E402

# 连接工厂同样统一到 catalyst.db.get_conn()（P3-F）：
# 生产的 runner.py / pipeline 全走它 —— `dict_row` + commit/rollback 语义一致。
# ⚠️ 不得改用 crypto_research.db.conn.get_connection：那是 `tuple_row`（且池复用时
#    重置 row_factory），而生产归属代码内部用 `row["asset_id"]` 取值 ⇒ 一跑即
#    TypeError: tuple indices must be integers。
from catalyst.db import get_conn  # noqa: E402
from catalyst.models import CatalystItem  # noqa: E402
from catalyst.pipeline import (  # noqa: E402
    _resolve_asset_ids,
    _update_asset_links,
)

# 机器生成的关联来源（`--prune` 只清这些；`legacy` / `manual` 等人工行一律不动）
_MACHINE_LINK_SOURCES = ("trading_pairs", "cashtag", "project_name")


# ---------------------------------------------------------------------------
# 归属（**委托生产代码**，本文件不再持有任何归属规则）
# ---------------------------------------------------------------------------


def link_catalyst(cat: dict, conn) -> tuple[list[int], str]:
    """对单条催化剂做资产关联 —— 与生产 `_insert_catalyst` 同一条路径。

    Returns:
        (asset_ids, link_source) —— link_source 仅作写入标记（生产默认 trading_pairs）
    """
    item = CatalystItem(
        source_code=cat.get("source_code") or "",
        source_item_id=str(cat["catalyst_id"]),
        title=cat.get("title") or "",
        body_text=cat.get("body_text") or "",
        published_at=0.0,
        related_pairs=list(cat.get("related_pairs") or []),
    )
    # 生产 `upsert_catalyst_item(link_source="trading_pairs")` 的同款默认值
    asset_ids = _resolve_asset_ids(item, conn, "trading_pairs")
    return list(asset_ids), "trading_pairs"


# ---------------------------------------------------------------------------
# 业务逻辑
# ---------------------------------------------------------------------------


def fetch_catalysts(conn, limit: int = 500, after_id: int = 0, only_unlinked: bool = False) -> list[dict]:
    """分批取催化剂 —— **keyset 分页**（`catalyst_id > after_id`）。

    不用 OFFSET：① `--only-unlinked` 是「排空」语义，本次补上关联的行会退出过滤集，
    OFFSET 会整批跳行（或整批无进展时死循环）；② keyset 在 3.6 万行上也不会越翻越慢。
    """
    extra = ""
    if only_unlinked:
        extra = """
          AND NOT EXISTS (
              SELECT 1 FROM biz.catalyst_asset_link l WHERE l.catalyst_id = c.catalyst_id
          )
        """
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            f"""
            SELECT c.catalyst_id, c.source_code, c.title, c.body_text, c.related_pairs
            FROM biz.asset_catalyst c
            WHERE c.catalyst_id > %s
            {extra}
            ORDER BY c.catalyst_id
            LIMIT %s
            """,
            (after_id, limit),
        )
        rows = cur.fetchall()

    result = []
    for row in rows:
        d = dict(row)
        rp = d.get("related_pairs")
        if isinstance(rp, str):
            try:
                rp = json.loads(rp)
            except Exception:
                rp = []
        d["related_pairs"] = rp or []
        result.append(d)
    return result


def current_links(catalyst_id: int, conn) -> set[int]:
    """当前关联表里该催化剂的 asset_id 集合。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "SELECT asset_id FROM biz.catalyst_asset_link WHERE catalyst_id = %s",
            (catalyst_id,),
        )
        return {r["asset_id"] for r in cur.fetchall()}


def prune_links(catalyst_id: int, keep: set[int], conn) -> int:
    """删除该催化剂**机器生成**且不在 keep 集合内的关联行（写库，需 --prune）。"""
    if keep:
        sql = (
            "DELETE FROM biz.catalyst_asset_link "
            "WHERE catalyst_id = %s AND link_source = ANY(%s) AND NOT (asset_id = ANY(%s))"
        )
        params = (catalyst_id, list(_MACHINE_LINK_SOURCES), list(keep))
    else:
        sql = (
            "DELETE FROM biz.catalyst_asset_link "
            "WHERE catalyst_id = %s AND link_source = ANY(%s)"
        )
        params = (catalyst_id, list(_MACHINE_LINK_SOURCES))
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount


def main() -> int:
    parser = argparse.ArgumentParser(description="存量催化剂资产关联展开（P3-F 单一真相版）")
    parser.add_argument("--dry-run", action="store_true", help="只预览不修改")
    parser.add_argument("--prune", action="store_true",
                        help="重算并剔除失效机器关联（写库，先跑 --dry-run 评估）")
    parser.add_argument("--only-unlinked", action="store_true",
                        help="只处理当前无关联的催化剂（受控回填的最小面）")
    parser.add_argument("--max-items", type=int, default=0, help="最多处理条数（0=全部）")
    parser.add_argument("--batch-size", type=int, default=500, help="每批查询数量")
    args = parser.parse_args()

    total = 0
    total_added = 0
    total_pruned = 0
    total_no_link = 0
    gain_catalysts = 0
    after_id = 0

    with get_conn() as conn:
        while True:
            batch = fetch_catalysts(conn, args.batch_size, after_id, args.only_unlinked)
            if not batch:
                break

            print(f"\n处理 catalyst_id > {after_id}（本批 {len(batch)} 条，已完成 {total}）", flush=True)

            for cat in batch:
                if args.max_items and total >= args.max_items:
                    break

                cid = cat["catalyst_id"]
                asset_ids, link_source = link_catalyst(cat, conn)
                # `--only-unlinked` 取的行**按定义**没有任何关联行（SQL 已 NOT EXISTS 过滤），
                # 再查一次 `current_links` 纯属多一次远程往返（实测占总耗时 ~45%）。
                old_ids = set() if args.only_unlinked else current_links(cid, conn)
                new_ids = set(asset_ids)
                gained = new_ids - old_ids
                lost = old_ids - new_ids

                if not args.dry_run:
                    n_add = _update_asset_links(cid, asset_ids, link_source, conn)
                    n_prune = prune_links(cid, new_ids, conn) if args.prune else 0
                else:
                    n_add, n_prune = len(gained), len(lost) if args.prune else 0

                total_added += n_add
                total_pruned += n_prune
                if gained:
                    gain_catalysts += 1
                if not asset_ids:
                    total_no_link += 1

                parts = []
                if asset_ids:
                    parts.append(f"ids:{len(asset_ids)}")
                if gained:
                    parts.append(f"+{len(gained)}")
                if lost:
                    parts.append(f"-{len(lost)}" if args.prune else f"stale:{len(lost)}")
                if not parts:
                    parts.append("无关联")

                mode = "DRY" if args.dry_run else ("PRUNE" if args.prune else "OK")
                print(f"  [{cid}] {mode} {' '.join(parts)} | {(cat.get('title') or '')[:50]}")

                total += 1

            if args.max_items and total >= args.max_items:
                break
            # keyset 推进：下一批从本批最后一个 catalyst_id 之后取
            after_id = batch[-1]["catalyst_id"]
            if len(batch) < args.batch_size:
                break

    print(f"\n{'=' * 60}")
    print(f"处理催化剂总数: {total}")
    print(f"  新增关联行:        {total_added}")
    print(f"  剔除关联行:        {total_pruned}{'' if args.prune else '（未开 --prune）'}")
    print(f"  产生新关联的催化剂: {gain_catalysts}")
    print(f"  仍无任何关联:      {total_no_link}")
    if args.dry_run:
        print("（dry-run 模式，未实际写入）")
    elif args.prune:
        print("（--prune 已启用：失效的机器关联已被剔除）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
