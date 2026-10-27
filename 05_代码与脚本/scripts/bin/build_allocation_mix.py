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
2. 标记 asset_unlock_event 的受益人来源（**不再往事件级写粗推断的 type**）：
   - 有 raw_ref.label → 用分类器映射（beneficiary_source='label'，精确）
   - 其余有分配构成的资产 → 只标 source='asset_level_only'，type 留 NULL
     ⚠️ HC-014：原做法写 dominant_bucket 作为事件级 type，实测仅 33% 与事件级精确值
        一致 → 会假阴性。事件级 type 一律由 bin/backfill_beneficiary_event_level.py
        从 raw_ref.recipients 按 pct 加权回填（source='event_recipients'）。
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
    # 🔴 HC-014（2026-10-27）：原词表漏了 "insider"，而 tokenomics hero 区最常用的
    #    超级组名就是 `Insiders` / `Insider`（官方口径 Insiders = Team and Advisors）。
    #    classify('Insiders') 曾返回 'other'，把内部抛压直接判成无关类别 —— 这是
    #    「insider 占比算不出来」的第二个根因（第一个是 UPSERT 用 NULL 覆盖）。
    ("team", ("team", "founder", "core contributor", "contributor", "advisor", "adviser",
              "employee", "core dev", "dev fund", "builder", "insider", "内部", "团队")),
    ("investor", ("investor", "seed", "private", "strategic", "backer", "venture", "angel",
                  "series ", "capital", "presale", "pre-sale", "pre sale", "kols", "kol round")),
    ("public", ("public sale", "public", "ido", "ieo", "ico", "launchpad", "launchpool",
                "token sale", "community sale", "sale", "offering", "tge")),
    ("community", ("community", "airdrop", "incentive", "reward", "staking", "mining",
                   "user", "farming", "quest", "points", "社区")),
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


# ── 事件级受益人解析（HC-014）──────────────────────────────────────────
# 单一实现，供 sync_unlock_events_from_json / 回填脚本 / veto_check 共用。
# 为什么必须在这里：tokenomics hero 与 raw_ref 里的 recipients 是
#   [{"name": "Insiders", "pct": 35.8}, {"name": "Private Investors", "pct": 27.2}]
# 结构，按 pct 加权才能得出「本次解锁谁主导」。用关键词扫整段文本（旧做法）
# 会被首个命中词带偏，且对 None / 缺 name 的脏项无抵抗力。
_BAD_NAMES = {"", "none", "null", "nan", "n/a", "-"}


def parse_recipients(raw) -> list[dict]:
    """从 raw_ref 片段或页面快照提取 [{name, pct}]，剔除无名/无比例的脏项。

    容忍四种输入：list / JSON 字符串 / 'None' 字符串 / None。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        s = raw.strip()
        if not s or s.lower() in _BAD_NAMES:
            return []
        try:
            raw = json.loads(s)
        except (ValueError, TypeError):
            return []
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for x in raw:
        if not isinstance(x, dict):
            continue
        name = x.get("name") or x.get("category") or x.get("label")
        try:
            pct = float(x.get("pct"))
        except (TypeError, ValueError):
            continue
        if not name or str(name).strip().lower() in _BAD_NAMES:
            continue
        out.append({"name": str(name).strip(), "pct": pct})
    return out


def mixture_from_recipients(items: list[dict]) -> dict | None:
    """按 pct 汇总到 8 桶 → {dominant_bucket, insider_pct, shares, n_recipients}。

    比例先归一化到 100（源站偶有合计 98.x / 101.x）。insider = team + investor。
    """
    if not items:
        return None
    bucket_sum: dict[str, float] = defaultdict(float)
    for it in items:
        bucket_sum[classify(it["name"])] += it["pct"]
    total = sum(bucket_sum.values())
    if total <= 0:
        return None
    shares = {k: round(v / total * 100, 2) for k, v in bucket_sum.items() if v > 0}
    dominant = max(shares, key=lambda k: shares[k])
    return {
        "dominant_bucket": dominant,
        "insider_pct": round(shares.get("team", 0.0) + shares.get("investor", 0.0), 2),
        "shares": shares,
        "n_recipients": len(items),
    }


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

        #    3b) 🔴 HC-014（2026-10-27）口径变更：**不再**把资产级 dominant_bucket 写进
        #        事件级 beneficiary_type。
        #        实测依据：对 157 个同时有「资产级主桶」和「事件级精确 recipients」的样本
        #        做一致性检验，仅 33% 一致（52/157），67% 不一致。典型反例：
        #          ARKM 资产级=community，事件级 Insiders 35.8% + Private Investors 27.2%（63% insider）
        #          ATH  资产级=other，    事件级 Insiders 43.3% + Private Investors 42.7%（86% insider）
        #        拿它算 insider 占比会把内部抛压误判成社区解锁 —— 假阴性，方向性错误。
        #        现在只标 source 表示「该资产有生命周期分配构成」，type 留 NULL 由事件级
        #        recipients 回填（bin/backfill_beneficiary_event_level.py）负责。
        cur.execute("""
            UPDATE biz.asset_unlock_event e
            SET beneficiary_type = NULL,
                beneficiary_source = 'asset_level_only',
                updated_at = NOW()
            FROM biz.asset_allocation_mix m
            WHERE e.asset_id = m.asset_id
              AND e.beneficiary_source IN ('asset_level_inferred', 'unspecified')
        """)
        n_dom = cur.rowcount
        conn.commit()
        print(f"标记 asset_level_only（不写 type，等事件级回填）: {n_dom} 行")

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
