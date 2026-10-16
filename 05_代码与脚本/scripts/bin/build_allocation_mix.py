#!/usr/bin/env python3
"""构建 biz.asset_allocation_mix + 回填解锁受益人类型（UNLOCK-REACTION 补齐）。

背景
----
biz.asset_unlock_event.beneficiary_type 长期全空 → 无法区分「团队/VC 解锁（通常砸盘）」
与「生态/社区解锁（通常无碍）」。数据源其实一直都在：
    biz.asset_tokenomics.allocation_json  —— 777 资产的分配类别（Team/Investors/Ecosystem/...）
    biz.asset_unlock_event.raw_ref->>'label' —— 76 条精确类别标签

本脚本
------
1. 关键词分类器把 allocation_json 的每个 category 归入 8 个桶：
   team / investor / community / ecosystem / foundation / public / liquidity / other
   → 计算占比，写 biz.asset_allocation_mix（资产级受益人构成）。
2. 回填 asset_unlock_event.beneficiary_type：
   - 有 raw_ref.label → 用分类器映射（beneficiary_source='label'，精确）
   - 否则有分配构成 → 用资产级 dominant_bucket（beneficiary_source='asset_level_inferred'，粗粒度）
   - 都没有 → beneficiary_source='unspecified'

用法
----
    python bin/build_allocation_mix.py --dry-run     # 只看统计，不写库
    python bin/build_allocation_mix.py               # 写 mix 表 + 回填 beneficiary
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

METHOD_VERSION = "v1"

# 桶分类：first-match-wins，顺序即优先级（先特殊后一般）
BUCKET_RULES = [
    ("liquidity", ("liquidity", "market mak", "market-mak", "initial liq", "exchange liq",
                   "liquidity pool", "liquidity provision", "dex ", "cex ", "amm")),
    ("foundation", ("foundation", "treasury", "dao", "reserve", "insurance fund", "buyback", "buy-back")),
    ("team", ("team", "founder", "core contributor", "contributor", "advisor", "adviser",
              "employee", "core dev", "dev fund", "builder")),
    ("investor", ("investor", "seed", "private", "strategic", "backer", "venture", "angel",
                  "series ", "capital", "presale", "pre-sale", "pre sale", "kols", "kol round")),
    ("public", ("public sale", "public", "ido", "ieo", "ico", "launchpad", "launchpool",
                "token sale", "community sale", "sale", "offering", "tge")),
    ("community", ("community", "airdrop", "incentive", "reward", "staking", "mining",
                   "user", "farming", "quest", "points")),
    ("ecosystem", ("ecosystem", "grant", "development", "develop", "growth", "partnership",
                   "partner", "hook")),
    ("other", ("marketing", "operation", "legal", "audit", "security", "misc", "untracked",
               "circulating", "burn", "charity", "ops", "unallocated", "未明确")),
]
BUCKETS = ("team", "investor", "community", "ecosystem", "foundation", "public", "liquidity", "other")


def classify(category: str) -> str:
    if not category:
        return "other"
    c = category.strip().lower()
    for bucket, keys in BUCKET_RULES:
        for k in keys:
            if k in c:
                return bucket
    return "other"


def build_mix(allocation_json) -> dict | None:
    """allocation_json → {team_pct, ..., dominant_bucket, mix}"""
    if not isinstance(allocation_json, list) or not allocation_json:
        return None
    bucket_sum = defaultdict(float)
    raw_mix = {}
    n = 0
    for a in allocation_json:
        if not isinstance(a, dict):
            continue
        cat = (a.get("category") or "").strip()
        try:
            pct = float(a.get("pct"))
        except (TypeError, ValueError):
            continue
        if not cat:
            continue
        n += 1
        raw_mix[cat] = round(raw_mix.get(cat, 0.0) + pct, 4)
        bucket_sum[classify(cat)] += pct
    if n == 0:
        return None
    b = {k: round(bucket_sum.get(k, 0.0), 4) for k in BUCKETS}
    insider = round(b["team"] + b["investor"], 4)
    outsider = round(b["community"] + b["ecosystem"] + b["foundation"] + b["public"] + b["liquidity"], 4)
    dominant = max(BUCKETS, key=lambda k: bucket_sum.get(k, 0.0))
    return {
        "team_pct": b["team"], "investor_pct": b["investor"], "insider_pct": insider,
        "community_pct": b["community"], "ecosystem_pct": b["ecosystem"],
        "foundation_pct": b["foundation"], "public_pct": b["public"],
        "liquidity_pct": b["liquidity"], "other_pct": b["other"], "outsider_pct": outsider,
        "dominant_bucket": dominant, "n_categories": n, "mix": raw_mix,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="构建分配构成表 + 回填解锁受益人类型")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    args = ap.parse_args()

    with get_connection(get_settings(require_database=True).database_url) as conn:
        cur = conn.cursor()
        # 1) allocation_json
        cur.execute("""
            SELECT asset_id, allocation_json, confidence
            FROM biz.asset_tokenomics
            WHERE allocation_json IS NOT NULL
              AND jsonb_typeof(allocation_json) = 'array'
              AND jsonb_array_length(allocation_json) > 0
        """)
        rows = cur.fetchall()
        mixes = {}
        dom_cnt = defaultdict(int)
        for aid, aj, conf in rows:
            m = build_mix(aj)
            if m:
                m["confidence"] = float(conf) if conf is not None else None
                mixes[aid] = m
                dom_cnt[m["dominant_bucket"]] += 1
        print(f"allocation_json 资产 {len(rows)} → 生成构成 {len(mixes)}")
        print("  dominant_bucket 分布:", dict(dom_cnt))

        if args.dry_run:
            # 预览 beneficiary 回填覆盖
            cur.execute("SELECT count(*) FROM biz.asset_unlock_event WHERE raw_ref->>'label' IS NOT NULL")
            print("  raw_ref.label 事件:", cur.fetchone()[0])
            print("  (dry-run，不写库)")
            return 0

        # 2) 写 asset_allocation_mix（集合式 executemany）
        params = []
        for aid, m in mixes.items():
            params.append((
                aid, m["team_pct"], m["investor_pct"], m["insider_pct"],
                m["community_pct"], m["ecosystem_pct"], m["foundation_pct"], m["public_pct"],
                m["liquidity_pct"], m["other_pct"], m["outsider_pct"], m["dominant_bucket"],
                m["n_categories"], m["confidence"], "allocation_json", METHOD_VERSION,
                json.dumps(m["mix"], ensure_ascii=False),
            ))
        cur.executemany("""
                INSERT INTO biz.asset_allocation_mix (
                    asset_id, team_pct, investor_pct, insider_pct,
                    community_pct, ecosystem_pct, foundation_pct, public_pct,
                    liquidity_pct, other_pct, outsider_pct, dominant_bucket,
                    n_categories, confidence, source, method_version, mix, updated_at
                ) VALUES (%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s,%s, NOW())
                ON CONFLICT (asset_id) DO UPDATE SET
                    team_pct=EXCLUDED.team_pct, investor_pct=EXCLUDED.investor_pct,
                    insider_pct=EXCLUDED.insider_pct, community_pct=EXCLUDED.community_pct,
                    ecosystem_pct=EXCLUDED.ecosystem_pct, foundation_pct=EXCLUDED.foundation_pct,
                    public_pct=EXCLUDED.public_pct, liquidity_pct=EXCLUDED.liquidity_pct,
                    other_pct=EXCLUDED.other_pct, outsider_pct=EXCLUDED.outsider_pct,
                    dominant_bucket=EXCLUDED.dominant_bucket, n_categories=EXCLUDED.n_categories,
                    confidence=EXCLUDED.confidence, source=EXCLUDED.source,
                    method_version=EXCLUDED.method_version, mix=EXCLUDED.mix,
                    updated_at=NOW()
        """, params)
        conn.commit()
        print(f"写入 biz.asset_allocation_mix: {len(params)} 行")

        # 3) 回填 asset_unlock_event.beneficiary_type
        #    3a) label 精确
        cur.execute("""
            SELECT asset_id, unlock_date, unlock_type, source_code, raw_ref->>'label'
            FROM biz.asset_unlock_event
            WHERE raw_ref->>'label' IS NOT NULL
        """)
        label_rows = cur.fetchall()
        label_params = [
            (classify(label), aid, ud, ut, sc)
            for aid, ud, ut, sc, label in label_rows
        ]
        cur.executemany("""
            UPDATE biz.asset_unlock_event
            SET beneficiary_type = %s, beneficiary_source = 'label', updated_at = NOW()
            WHERE asset_id=%s AND unlock_date=%s AND unlock_type=%s AND source_code=%s
        """, label_params)
        conn.commit()
        print(f"回填 label 精确类别: {len(label_params)} 行")

        #    3b) 资产级 dominant_bucket（仅对尚无精确类别的行）—— 集合式 JOIN 更新
        cur.execute("""
            UPDATE biz.asset_unlock_event e
            SET beneficiary_type = m.dominant_bucket,
                beneficiary_source = 'asset_level_inferred',
                updated_at = NOW()
            FROM biz.asset_allocation_mix m
            WHERE e.asset_id = m.asset_id
              AND (e.beneficiary_source IS NULL OR e.beneficiary_source = 'unspecified')
        """)
        n_dom = cur.rowcount
        conn.commit()
        print(f"回填资产级 dominant_bucket: {n_dom} 行")

        #    3c) 其余标记 unspecified
        cur.execute("""
            UPDATE biz.asset_unlock_event
            SET beneficiary_source = 'unspecified'
            WHERE beneficiary_source IS NULL
        """)
        conn.commit()
        print(f"标记 unspecified: {cur.rowcount} 行")

        # 4) 汇总
        cur.execute("""
            SELECT beneficiary_source, beneficiary_type, count(*)
            FROM biz.asset_unlock_event
            GROUP BY 1,2 ORDER BY 3 DESC LIMIT 25
        """)
        print("\n== beneficiary 回填结果 ==")
        for r in cur.fetchall():
            print(f"  {str(r[0]):<24} {str(r[1]):<20} {r[2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
