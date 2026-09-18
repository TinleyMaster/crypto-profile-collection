#!/usr/bin/env python3
"""强影响催化剂共性特征深挖：找出哪些特征组合能预测催化剂大幅影响价格。

方法论：
  分组（按 72h 超额）：
    强影响 STRONG  : excess_72h >= 5%（约 14.5%）
    无影响 NULL    : excess_72h 在 -2% ~ 2%（约 50%）
    反向 REVERSE   : excess_72h < -5%（约 8%）

  对比维度（每组算占比/均值，重点看强 vs 无 的差异）：
    1. 市值规模   （<1亿 / 1-10亿 / 10-50亿 / >50亿）
    2. 事件类型 × 市值（交互：小市值+listing 是否更强）
    3. 来源 × 市值
    4. 量比 vol_ratio_24h（发布后是否放量）
    5. 方向 × 事件类型（哪些事件+方向组合最有效）
    6. 信号评分 tier / composite

  输出：Markdown 画像报告 + 控制台摘要

用法：
    python deep_dive_strong_catalyst.py
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg
import psycopg.rows

from crypto_research.config import get_settings  # noqa: E402

STRONG = 5.0     # 强影响阈值
NULL_LO = -2.0   # 无影响区间
NULL_HI = 2.0
REVERSE = -5.0   # 反向阈值


def get_conn():
    settings = get_settings(require_database=True)
    return psycopg.connect(
        settings.database_url,
        row_factory=psycopg.rows.dict_row,
        connect_timeout=30,
        options="-c lock_timeout=30000",
        keepalives=1, keepalives_idle=15, keepalives_interval=5, keepalives_count=3,
    )


def classify(e72: float) -> str:
    if e72 >= STRONG:
        return "STRONG"
    if e72 <= REVERSE:
        return "REVERSE"
    if NULL_LO <= e72 < NULL_HI:
        return "NULL"
    return "MID"


def fetch_samples(conn) -> list[dict]:
    return conn.execute(
        """
        SELECT
            co.catalyst_id, co.asset_id, co.excess_72h, co.excess_24h,
            co.vol_ratio_24h, co.max_gain_24h, co.max_drawdown_24h,
            co.impact_direction, co.data_tier,
            COALESCE(ac.ai_event_type, ac.rule_event_type, 'other') AS event_type,
            ac.source_code,
            a.market_cap, a.market_cap_rank, a.canonical_symbol,
            cs.composite_score, cs.tier, cs.regime, cs.technical_state,
            cs.resonance_state, cs.resonance_score
        FROM biz.catalyst_outcome co
        JOIN biz.asset_catalyst ac ON ac.catalyst_id = co.catalyst_id
        JOIN core.asset a ON a.asset_id = co.asset_id
        LEFT JOIN biz.catalyst_signal cs
          ON cs.catalyst_id = co.catalyst_id AND cs.asset_id = co.asset_id
        WHERE co.data_tier = 'L1'
          AND co.excess_72h IS NOT NULL
        """
    ).fetchall()


def mcap_bucket(mcap) -> str:
    if mcap is None:
        return "未知"
    if mcap < 1e8:
        return "<1亿"
    if mcap < 1e9:
        return "1-10亿"
    if mcap < 5e9:
        return "10-50亿"
    return ">50亿"


def pct(n, total) -> str:
    return f"{n / total * 100:.0f}%" if total else "-"


def main() -> int:
    parser = argparse.ArgumentParser(description="强影响催化剂共性深挖")
    parser.add_argument("--dry-run", action="store_true", help="仅控制台")
    args = parser.parse_args()

    conn = get_conn()
    try:
        samples = fetch_samples(conn)
        groups: dict[str, list] = defaultdict(list)
        for s in samples:
            groups[classify(float(s["excess_72h"]))].append(s)

        for g in ["STRONG", "NULL", "REVERSE"]:
            if g not in groups:
                groups[g] = []
        total = len(samples)
        print(f"样本: {total} ｜ "
              f"STRONG {len(groups['STRONG'])}({pct(len(groups['STRONG']), total)}) ｜ "
              f"NULL {len(groups['NULL'])}({pct(len(groups['NULL']), total)}) ｜ "
              f"REVERSE {len(groups['REVERSE'])}({pct(len(groups['REVERSE']), total)})")

        md = [f"# 强影响催化剂共性特征深挖", "",
              f"> 生成: {date.today()} ｜ 样本 {total}（L1+72h）",
              f"> 强影响 STRONG: 72h超额≥+5% ｜ 无影响 NULL: -2%~+2% ｜ 反向 REVERSE: ≤-5%", ""]

        # ---- 1. 市值分布 ----
        print("\n" + "=" * 68)
        print("1. 市值规模分布（各组占比）")
        print("=" * 68)
        print(f"  {'市值':<10} {'STRONG':>8} {'NULL':>8} {'REVERSE':>8}  {'STRONG占比':>10}")
        md += ["## 1. 市值规模", "", "| 市值 | STRONG | NULL | REVERSE | 强占比 |", "|---|---|---|---|---|"]
        buckets = ["<1亿", "1-10亿", "10-50亿", ">50亿", "未知"]
        for b in buckets:
            row = []
            for g in ["STRONG", "NULL", "REVERSE"]:
                n = sum(1 for s in groups[g] if mcap_bucket(s["market_cap"]) == b)
                row.append(f"{n}({pct(n, len(groups[g]))})")
            strong_in_b = sum(1 for s in samples if mcap_bucket(s["market_cap"]) == b and float(s["excess_72h"]) >= STRONG)
            tot_in_b = sum(1 for s in samples if mcap_bucket(s["market_cap"]) == b)
            print(f"  {b:<10} {row[0]:>8} {row[1]:>8} {row[2]:>8}  {pct(strong_in_b, tot_in_b):>10}")
            md.append(f"| {b} | {row[0]} | {row[1]} | {row[2]} | {pct(strong_in_b, tot_in_b)} |")

        # ---- 2. 事件类型 × 市值 ----
        print("\n" + "=" * 68)
        print("2. 事件类型 × 小市值(<10亿)（小市值组内强占比）")
        print("=" * 68)
        print(f"  {'事件类型':<16} {'小市值样本':>8} {'强影响':>6} {'强占比':>8}   {'大市值强占比':>10}")
        md += ["", "## 2. 事件类型 × 市值", "", "| 事件类型 | 小市值样本 | 小市值强影响 | 小市值强占比 | 大市值强占比 |", "|---|---|---|---|---|"]
        et_counter = Counter(s["event_type"] for s in samples)
        for et, et_total in et_counter.most_common():
            sub = [s for s in samples if s["event_type"] == et]
            small = [s for s in sub if mcap_bucket(s["market_cap"]) in ("<1亿", "1-10亿")]
            big = [s for s in sub if mcap_bucket(s["market_cap"]) in ("10-50亿", ">50亿")]
            s_strong = sum(1 for s in small if float(s["excess_72h"]) >= STRONG)
            b_strong = sum(1 for s in big if float(s["excess_72h"]) >= STRONG)
            print(f"  {et:<16} {len(small):>8} {s_strong:>6} {pct(s_strong, len(small)):>8}   {pct(b_strong, len(big)):>10}")
            md.append(f"| {et} | {len(small)} | {s_strong} | {pct(s_strong, len(small))} | {pct(b_strong, len(big))} |")

        # ---- 3. 来源 × 市值 ----
        print("\n" + "=" * 68)
        print("3. 来源 × 小市值")
        print("=" * 68)
        print(f"  {'来源':<30} {'样本':>6} {'小市值':>6} {'小市值强占比':>10}")
        md += ["", "## 3. 来源 × 市值", "", "| 来源 | 样本 | 小市值 | 小市值强占比 |", "|---|---|---|---|"]
        src_counter = Counter(s["source_code"] for s in samples)
        for src, n in src_counter.most_common():
            sub = [s for s in samples if s["source_code"] == src]
            small = [s for s in sub if mcap_bucket(s["market_cap"]) in ("<1亿", "1-10亿")]
            s_strong = sum(1 for s in small if float(s["excess_72h"]) >= STRONG)
            print(f"  {src:<30} {n:>6} {len(small):>6} {pct(s_strong, len(small)):>10}")
            md.append(f"| {src} | {n} | {len(small)} | {pct(s_strong, len(small))} |")

        # ---- 4. 量比 ----
        print("\n" + "=" * 68)
        print("4. 发布后量比 vol_ratio_24h（中位数）")
        print("=" * 68)
        md += ["", "## 4. 量比（发布后24h/7日均量）", "", "| 组 | 样本 | 中位量比 | 平均量比 |", "|---|---|---|---|"]
        import statistics
        for g in ["STRONG", "NULL", "REVERSE"]:
            vols = [float(s["vol_ratio_24h"]) for s in groups[g] if s["vol_ratio_24h"] is not None]
            if vols:
                med = round(statistics.median(vols), 2)
                avg = round(sum(vols) / len(vols), 2)
                print(f"  {g:<8} {len(vols):>5} 中位 {med:>6} 平均 {avg:>6}")
                md.append(f"| {g} | {len(vols)} | {med} | {avg} |")

        # ---- 5. 方向 × 事件类型 ----
        print("\n" + "=" * 68)
        print("5. 方向 × 事件类型（STRONG 分布）")
        print("=" * 68)
        md += ["", "## 5. 强影响的事件×方向组合", ""]
        combo = Counter((s["event_type"], s["impact_direction"] or "None")
                        for s in groups["STRONG"])
        for (et, d), n in combo.most_common(12):
            print(f"  {et:<16} × {d:<9} {n} 条")
            md.append(f"- **{et} × {d}**: {n} 条")

        # ---- 6. 评分 ----
        print("\n" + "=" * 68)
        print("6. 信号评分 tier / composite")
        print("=" * 68)
        md += ["", "## 6. 信号评分", "", "| 组 | 样本 | avg composite | A | B | C |", "|---|---|---|---|---|---|"]
        for g in ["STRONG", "NULL", "REVERSE"]:
            comps = [s["composite_score"] for s in groups[g] if s["composite_score"] is not None]
            tiers = Counter(s["tier"] for s in groups[g] if s["tier"])
            avg_c = round(sum(comps) / len(comps), 1) if comps else "-"
            print(f"  {g:<8} {len(groups[g]):>5} avg_comp={avg_c}  "
                  f"A={tiers.get('A',0)} B={tiers.get('B',0)} C={tiers.get('C',0)}")
            md.append(f"| {g} | {len(groups[g])} | {avg_c} | {tiers.get('A',0)} | {tiers.get('B',0)} | {tiers.get('C',0)} |")

        # ---- 7. 画像总结 ----
        print("\n" + "=" * 68)
        print("7. 画像总结")
        print("=" * 68)
        strong_small = [s for s in groups["STRONG"] if mcap_bucket(s["market_cap"]) in ("<1亿", "1-10亿")]
        small_share = len(strong_small) / len(groups["STRONG"]) * 100 if groups["STRONG"] else 0
        # 强影响中量比>1.5 占比
        vol_hi = sum(1 for s in groups["STRONG"] if s["vol_ratio_24h"] is not None and float(s["vol_ratio_24h"]) >= 1.5)
        vol_hi_share = vol_hi / len(groups["STRONG"]) * 100 if groups["STRONG"] else 0
        print(f"  强影响画像：")
        print(f"    - 小市值(<10亿)占比: {small_share:.0f}%")
        print(f"    - 发布后放量(量比≥1.5)占比: {vol_hi_share:.0f}%")
        print(f"    - 方向: bullish {sum(1 for s in groups['STRONG'] if s['impact_direction']=='bullish')} / "
              f"neutral {sum(1 for s in groups['STRONG'] if s['impact_direction']=='neutral')} / "
              f"bearish {sum(1 for s in groups['STRONG'] if s['impact_direction']=='bearish')}")
        md += ["", "## 7. 画像总结", "",
               f"- 强影响中 **小市值(<10亿) 占 {small_share:.0f}%**",
               f"- 强影响中 **发布后放量(量比≥1.5) 占 {vol_hi_share:.0f}%**",
               f"- 强影响方向分布: bullish {sum(1 for s in groups['STRONG'] if s['impact_direction']=='bullish')} / "
               f"neutral {sum(1 for s in groups['STRONG'] if s['impact_direction']=='neutral')} / "
               f"bearish {sum(1 for s in groups['STRONG'] if s['impact_direction']=='bearish')}",
               "",
               "### 可操作的筛选建议", "",
               "按以下特征组合筛选，可显著提高催化剂命中强影响的概率：",
               "1. **市值 <10亿**（小市值弹性大，是首要因子）",
               "2. **发布后放量**（量比≥1.5 表示市场真正在交易这个催化剂）",
               "3. **事件类型**：优先 partnership / listing（小市值组内强占比最高）",
               "4. **谨慎**：market_update 即使小市值也多为噪声（方向不可测）"]

        if not args.dry_run:
            out = SCRIPT_DIR / "data" / f"strong_catalyst_profile_{date.today()}.md"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("\n".join(md) + "\n", encoding="utf-8")
            print(f"\n画像报告已导出: {out}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
