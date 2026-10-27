#!/usr/bin/env python3
"""把 raw_ref 里已经存在的受益人明细回填到事件级 beneficiary_type（HC-014）。

背景
----
数据其实一直在库里，只是没人用：
  biz.asset_unlock_event.raw_ref->'recipients'           形如
      [{"name": "Insiders", "pct": 35.8}, {"name": "Private Investors", "pct": 27.2}]
  biz.asset_unlock_event.raw_ref->'tokenomics_recipients' 同上（backfill 抓的 hero 快照）

而 beneficiary_type 却有 2,974 行「source=asset_level_inferred 但 type=NULL」的矛盾，
原因是三处叠加缺陷：
  1. sync 的 UPSERT 用 EXCLUDED.beneficiary_type（多为 NULL）覆盖已回填值，
     且 `raw_ref = EXCLUDED.raw_ref` 整体替换，把 tokenomics_recipients 一起冲掉；
  2. classify() 词表缺 'insider'，`Insiders` 被判成 other；
  3. 调用处把 `allocations`（其实是类别计数 '1'/'2'/'4'）当文本喂给推断函数。

本脚本做一件事：**只读本地 raw_ref，按 pct 加权重算事件级主导桶并回填**，不联网。
写库后 beneficiary_source='event_recipients'，与资产级推断（asset_level_only）明确区分，
下游算 insider 占比时只认事件级精确来源。

为什么不用资产级 dominant_bucket 兜底
------------------------------------
实测 157 个「资产级主桶 + 事件级精确明细」双全的样本，仅 33% 一致。典型反例：
  ARKM 资产级=community，事件级 Insiders 35.8% + Private Investors 27.2%（63% insider）
  ATH  资产级=other，    事件级 Insiders 43.3% + Private Investors 42.7%（86% insider）
方向性错误会直接把内部抛压洗成社区解锁，故资产级一律不写事件级 type。

用法
----
    python bin/backfill_beneficiary_event_level.py --dry-run
    python bin/backfill_beneficiary_event_level.py                 # 全表回填
    python bin/backfill_beneficiary_event_level.py --future-only   # 只回填未来解锁
    python bin/backfill_beneficiary_event_level.py --symbol ARKM,ATH
    python bin/backfill_beneficiary_event_level.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
for _p in (PROJECT_SRC, SCRIPT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg  # noqa: E402
import psycopg.types.json  # noqa: E402

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

from build_allocation_mix import mixture_from_recipients, parse_recipients  # noqa: E402

SOURCE_TAG = "event_recipients"


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="从 raw_ref.recipients 回填事件级受益人主导桶")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    ap.add_argument("--future-only", action="store_true", help="只处理 unlock_date >= 今天")
    ap.add_argument("--symbol", default=None, help="逗号分隔的符号过滤，如 ARKM,ATH")
    ap.add_argument("--json", action="store_true", help="输出 JSON 汇总")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    url = get_settings(require_database=True).database_url

    where, params = ["TRUE"], []
    if args.future_only:
        where.append("e.unlock_date >= CURRENT_DATE")
    if args.symbol:
        syms = [s.strip().upper() for s in args.symbol.split(",") if s.strip()]
        where.append("a.canonical_symbol = ANY(%s)")
        params.append(syms)
    where_sql = " AND ".join(where)

    summary = {
        "scanned": 0, "with_recipients": 0, "parsed": 0, "no_name": 0,
        "updated": 0, "already_ok": 0, "assets_typed": set(), "buckets": {},
        "samples": [],
    }

    with get_connection(url) as conn, conn.cursor() as cur:
        cur.execute(f"""
            SELECT e.asset_id, a.canonical_symbol, e.unlock_date, e.unlock_type,
                   e.source_code, e.raw_ref, e.beneficiary_type, e.beneficiary_source
            FROM biz.asset_unlock_event e
            JOIN core.asset a USING (asset_id)
            WHERE {where_sql}
            ORDER BY e.unlock_date
        """, params)
        rows = cur.fetchall()
        summary["scanned"] = len(rows)

        updates = []
        for (aid, sym, udate, utype, scode, raw, btype, bsource) in rows:
            raw = raw or {}
            items = []
            for key in ("recipients", "tokenomics_recipients", "beneficiaries"):
                items = parse_recipients(raw.get(key))
                if items:
                    break
            if not items:
                continue
            summary["with_recipients"] += 1
            mix = mixture_from_recipients(items)
            if not mix:
                summary["no_name"] += 1
                continue
            summary["parsed"] += 1
            summary["buckets"][mix["dominant_bucket"]] = \
                summary["buckets"].get(mix["dominant_bucket"], 0) + 1

            if btype == mix["dominant_bucket"] and bsource == SOURCE_TAG:
                summary["already_ok"] += 1
                continue

            patch = {
                "beneficiary_parse": {
                    "dominant": mix["dominant_bucket"],
                    "insider_pct": mix["insider_pct"],
                    "shares": mix["shares"],
                    "n_recipients": mix["n_recipients"],
                    "parsed_at": datetime.now().isoformat(timespec="seconds"),
                    "method": "pct_weighted/v1",
                }
            }
            updates.append((mix["dominant_bucket"], SOURCE_TAG,
                            json.dumps(patch, ensure_ascii=False),
                            aid, udate, utype, scode))
            summary["assets_typed"].add(sym)
            if len(summary["samples"]) < 12:
                summary["samples"].append({
                    "symbol": sym, "date": str(udate), "dominant": mix["dominant_bucket"],
                    "insider_pct": mix["insider_pct"], "shares": mix["shares"],
                    "prev_type": btype, "prev_source": bsource,
                })

        if args.dry_run:
            print(f"[dry-run] 扫描 {summary['scanned']} ｜ 有明细 {summary['with_recipients']} "
                  f"｜ 可解析 {summary['parsed']} ｜ 待更新 {len(updates)} "
                  f"｜ 已一致 {summary['already_ok']}")
            print("  dominant 分布:", summary["buckets"])
            for s in summary["samples"]:
                print(f"  {s['symbol']:8s} {s['date']} → {s['dominant']:12s} "
                      f"insider={s['insider_pct']:5.1f}%  原={s['prev_type']}/{s['prev_source']}")
            return 0

        if updates:
            cur.executemany("""
                UPDATE biz.asset_unlock_event e
                SET beneficiary_type = %s,
                    beneficiary_source = %s,
                    raw_ref = COALESCE(e.raw_ref, '{}'::jsonb) || %s::jsonb,
                    updated_at = NOW()
                WHERE e.asset_id = %s AND e.unlock_date = %s
                  AND e.unlock_type = %s AND e.source_code = %s
            """, [(b, s, js, aid, ud, ut, sc) for (b, s, js, aid, ud, ut, sc) in updates])
            summary["updated"] = len(updates)
        conn.commit()

        cur.execute("""
            SELECT beneficiary_source, count(*), count(*) FILTER (WHERE beneficiary_type IS NOT NULL)
            FROM biz.asset_unlock_event GROUP BY 1 ORDER BY 2 DESC
        """)
        dist = cur.fetchall()

    out = {
        "scanned": summary["scanned"],
        "with_recipients": summary["with_recipients"],
        "parsed": summary["parsed"],
        "updated": summary["updated"],
        "already_ok": summary["already_ok"],
        "assets_typed": len(summary["assets_typed"]),
        "buckets": summary["buckets"],
        "source_dist": [{"source": r[0], "rows": r[1], "typed": r[2]} for r in dist],
        "samples": summary["samples"],
    }
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return 0

    print(f"扫描 {out['scanned']} 行 ｜ 含明细 {out['with_recipients']} ｜ "
          f"可解析 {out['parsed']} ｜ 更新 {out['updated']} ｜ 已一致 {out['already_ok']} ｜ "
          f"涉及资产 {out['assets_typed']} 只")
    print("  dominant 分布:", out["buckets"])
    print("\n  source 分布（回填后）：")
    for r in out["source_dist"]:
        print(f"    {str(r['source']):24s} rows={r['rows']:6d} typed={r['typed']:6d}")
    print("\n  样本：")
    for s in out["samples"]:
        print(f"    {s['symbol']:8s} {s['date']} → {s['dominant']:12s} "
              f"insider={s['insider_pct']:5.1f}%  原={s['prev_type']}/{s['prev_source']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
