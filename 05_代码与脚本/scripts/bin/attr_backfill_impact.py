#!/usr/bin/env python3
"""
存量关联回填的**只读影响面预检**（P3-ATTR-001 P3-F 配套工具）。

回答一个写库前必须先答的问题：
    用**当前**归属逻辑重算一遍，会有哪些催化剂「新增关联」、哪些「失效关联（stale）」？

- **新增（gain）**：现在无关联/少关联 → 重算后有关联。回填的正收益。
- **失效（stale）**：现在有关联 → 重算后无关联/少关联。**必须逐条看**——
  它可能是「正确的收紧」（如 `现货黄金失守 X 美元` 误连 XAU9999 Meme），
  也可能是误杀。`stale` 就是 `backfill_catalyst_links.py --prune` 会删掉的行。

本脚本**纯 SELECT，零写入**；`--out` 落 JSONL 供人工逐条复核（写库授权的前提材料）。

用法：
    python scripts/bin/attr_backfill_impact.py [--scope unlinked|all] [--limit 2000] [--out xxx.jsonl]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
_candidate = SCRIPT_DIR.parent.parent / "workbench"
WORKBENCH_DIR = _candidate if _candidate.exists() else SCRIPT_DIR.parent.parent
sys.path.insert(0, str(WORKBENCH_DIR))
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

import psycopg.rows  # noqa: E402
from catalyst.db import get_conn  # noqa: E402
from catalyst.models import CatalystItem  # noqa: E402
from catalyst.pipeline import (  # noqa: E402
    _resolve_asset_ids,
    _match_project_name_to_assets,
)
from catalyst.linker import (  # noqa: E402
    _NON_CRYPTO_NAME_SQL,
    extract_pairs_from_text,
)


def _channel(cat: dict) -> str:
    """**主信号来源**（注意：有 related_pairs 的行也可能额外获得 project_name 的补关联，
    故「弱信号风险」须另看 `pn_hit`，不要用本字段代替）。

    · pairs        —— 源给了 related_pairs（强信号）
    · cashtag      —— related_pairs 空，但正文/标题能提出交易对
    · project_name —— 两路皆空 ⇒ 只能来自「标题项目方名 → 代币」前置
    """
    if cat.get("related_pairs"):
        return "pairs"
    if extract_pairs_from_text((cat.get("title") or "") + " " + (cat.get("body_text") or "")):
        return "cashtag"
    return "project_name"


def fetch_scope(conn, limit: int, scope: str) -> list[dict]:
    extra = ""
    if scope == "unlinked":
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
            WHERE 1 = 1 {extra}
            ORDER BY c.catalyst_id
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        d = dict(r)
        rp = d.get("related_pairs")
        if isinstance(rp, str):
            try:
                rp = json.loads(rp)
            except Exception:
                rp = []
        d["related_pairs"] = rp or []
        out.append(d)
    return out


def load_links(conn, cids: list[int]) -> dict[int, set[int]]:
    """一次性取这批催化剂的现有链接。"""
    if not cids:
        return {}
    res: dict[int, set[int]] = {c: set() for c in cids}
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT catalyst_id, asset_id FROM biz.catalyst_asset_link
            WHERE catalyst_id = ANY(%s)
            """,
            (cids,),
        )
        for r in cur.fetchall():
            res.setdefault(r["catalyst_id"], set()).add(r["asset_id"])
    return res


def symbol_map(conn, aid_set: set[int]) -> dict[int, str]:
    if not aid_set:
        return {}
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "SELECT asset_id, canonical_symbol, canonical_name FROM core.asset WHERE asset_id = ANY(%s)",
            (list(aid_set),),
        )
        return {r["asset_id"]: r["canonical_symbol"] for r in cur.fetchall()}


def main() -> int:
    ap = argparse.ArgumentParser(description="存量关联回填影响面预检（只读）")
    ap.add_argument("--scope", choices=["unlinked", "all"], default="unlinked",
                    help="unlinked=只看当前无关联的（默认）；all=全量")
    ap.add_argument("--limit", type=int, default=2000, help="扫描条数上限（默认 2000）")
    ap.add_argument("--out", default="", help="把逐条明细写 JSONL（人工复核材料）")
    ap.add_argument("--sample", type=int, default=12, help="每类打印的样例条数")
    args = ap.parse_args()

    with get_conn() as conn:
        cats = fetch_scope(conn, args.limit, args.scope)
        print(f"扫描范围: scope={args.scope}  条数={len(cats)}")
        if not cats:
            return 0

        cids = [c["catalyst_id"] for c in cats]
        old = load_links(conn, cids)

        n_gain_c = n_gain_r = n_stale_c = n_stale_r = 0
        n_pn_gain_c = n_pn_gain_r = 0
        n_unchanged = 0
        n_empty_after = 0
        ch_gain: dict[str, int] = {"pairs": 0, "cashtag": 0, "project_name": 0}
        gain_examples: list[tuple] = []
        stale_examples: list[tuple] = []
        detail: list[dict] = []
        all_gained_aids: set[int] = set()

        # 增量落盘 + 进度：全量扫描要跑 1~2 小时（远程库 0.25s/条），
        # 原实现只在结束时一次性写盘 ⇒ 中断即全丢、且无法判断进度（2026-10-19 踩）。
        out_fh = None
        if args.out:
            out_p = Path(args.out)
            out_p.parent.mkdir(parents=True, exist_ok=True)
            out_fh = out_p.open("w", encoding="utf-8")

        for i, c in enumerate(cats, 1):
            item = CatalystItem(
                source_code=c.get("source_code") or "",
                source_item_id=str(c["catalyst_id"]),
                title=c.get("title") or "",
                body_text=c.get("body_text") or "",
                published_at=0.0,
                related_pairs=list(c.get("related_pairs") or []),
            )
            new_ids = set(_resolve_asset_ids(item, conn, "trading_pairs"))
            o = old.get(c["catalyst_id"], set())
            gained = new_ids - o
            stale = o - new_ids
            ch = _channel(c)
            # 弱信号（标题项目方名 → 代币）是否参与本次归属
            pn_hit = bool(item.title and _match_project_name_to_assets(conn, item.title))

            if gained:
                n_gain_c += 1
                n_gain_r += len(gained)
                ch_gain[ch] += len(gained)
                if pn_hit:
                    n_pn_gain_c += 1
                    n_pn_gain_r += len(gained)
                all_gained_aids |= gained
            if stale:
                n_stale_c += 1
                n_stale_r += len(stale)
            if not gained and not stale:
                n_unchanged += 1
            if not new_ids:
                n_empty_after += 1

            if gained or stale:
                rec = {
                    "catalyst_id": c["catalyst_id"],
                    "source_code": c.get("source_code"),
                    "channel": ch,
                    "pn_hit": pn_hit,
                    "title": (c.get("title") or "")[:120],
                    "related_pairs": c.get("related_pairs") or [],
                    "old_asset_ids": sorted(o),
                    "new_asset_ids": sorted(new_ids),
                    "gained": sorted(gained),
                    "stale": sorted(stale),
                }
                detail.append(rec)
                if out_fh:
                    out_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

            if out_fh and i % 200 == 0:
                out_fh.flush()
            if i % 500 == 0:
                print(f"  … {i}/{len(cats)} 已扫（gain {n_gain_c} 条 / {n_gain_r} 行）",
                      file=sys.stderr, flush=True)

        smap = symbol_map(conn, all_gained_aids)
        # 精度自检：新增的关联里有多少落在「合成/代币化/商品衍生」资产上（应为 0）
        pseudo = 0
        if all_gained_aids:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute(
                    f"""
                    SELECT count(*) n FROM core.asset
                    WHERE asset_id = ANY(%s)
                      AND LOWER(COALESCE(canonical_name, '')) ~ '{_NON_CRYPTO_NAME_SQL}'
                    """,
                    (list(all_gained_aids),),
                )
                pseudo = cur.fetchone()["n"]

        for d in detail:
            if d["gained"] and len(gain_examples) < args.sample:
                syms = ",".join(smap.get(a, str(a)) for a in d["gained"])
                gain_examples.append((d["catalyst_id"], d["channel"], syms, d["title"]))
            if d["stale"] and len(stale_examples) < args.sample:
                stale_examples.append((d["catalyst_id"], ",".join(str(a) for a in d["stale"]), d["title"]))

        print()
        print("=" * 72)
        print(f"受影响催化剂:            {n_gain_c + n_stale_c}  /  扫过的 {len(cats)}")
        print(f"  🟢 新增关联 (gain) :  {n_gain_c} 条催化剂 / {n_gain_r} 行")
        print(f"        ├ 主信号 pairs : {ch_gain['pairs']} 行")
        print(f"        ├ 主信号 cashtag: {ch_gain['cashtag']} 行")
        print(f"        └ 主信号 project_name: {ch_gain['project_name']} 行")
        print(f"  ⚠️  其中 project_name **参与**者: {n_pn_gain_c} 条催化剂 / {n_pn_gain_r} 行"
              f"   ← 弱信号，须逐条复核")
        print(f"  🟠 失效关联 (stale):  {n_stale_c} 条催化剂 / {n_stale_r} 行   ← --prune 会删")
        print(f"  ⚪ 无变化          :  {n_unchanged}")
        print(f"  ⬜ 重算后仍无关联  :  {n_empty_after}")
        print(f"  ℹ️  新增关联落在合成/代币化资产上: {pseudo}  （应为 0；>0 即精度回归）")
        print("=" * 72)

        if gain_examples:
            print("\n【新增关联样例】")
            for cid, ch, syms, t in gain_examples:
                print(f"  {cid:<7} [{ch:<12}] → {syms:<30} | {t}")

        pn_gain = [d for d in detail if d["gained"] and d["pn_hit"]]
        if pn_gain:
            print(f"\n【⚠️ project_name 参与的归属（弱信号，逐条复核）共 {len(pn_gain)} 条】")
            for d in pn_gain[: args.sample]:
                syms = ",".join(smap.get(a, str(a)) for a in d["gained"])
                print(f"  {d['catalyst_id']:<7} → {syms:<24} | {d['title']}")

        if stale_examples:
            print("\n【失效关联样例（必须逐条确认是「正确收紧」还是「误杀」）】")
            for cid, aids, t in stale_examples:
                print(f"  {cid:<7} - {aids:<30} | {t}")

        if out_fh:
            out_fh.flush()
            out_fh.close()
            print(f"\n逐条明细已写入: {args.out}  （{len(detail)} 条）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
