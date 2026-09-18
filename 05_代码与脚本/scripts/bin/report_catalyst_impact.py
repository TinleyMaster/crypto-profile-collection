#!/usr/bin/env python3
"""历史催化剂影响程度报告：聚合全部 outcome 实测数据，输出各维度实际影响。

回答「历史催化剂到底造成了多大影响」：
  按 event_type / source / scope / 方向 分组，统计
  - 样本数（有 72h 超额的）
  - 方向命中率（72h）
  - 平均/中位超额收益（24h / 72h / 7d）
  - 影响程度分级（强/中/弱/无/反向）

影响分级（按 72h 平均超额）：
  强   : avg_excess_72h >= +5
  中   : +2 ~ +5
  弱   : 0 ~ +2
  无   : -2 ~ 0
  反向 : < -2

用法：
    python report_catalyst_impact.py            # 输出控制台 + Markdown 报告
    python report_catalyst_impact.py --dry-run  # 仅控制台
"""
from __future__ import annotations

import argparse
import sys
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


def get_conn():
    settings = get_settings(require_database=True)
    return psycopg.connect(
        settings.database_url,
        row_factory=psycopg.rows.dict_row,
        connect_timeout=30,
        options="-c lock_timeout=30000",
        keepalives=1, keepalives_idle=15, keepalives_interval=5, keepalives_count=3,
    )


def impact_level(avg72: float | None) -> str:
    """影响程度分级。"""
    if avg72 is None:
        return "-"
    if avg72 >= 5:
        return "强"
    if avg72 >= 2:
        return "中"
    if avg72 >= 0:
        return "弱"
    if avg72 >= -2:
        return "无"
    return "反向"


def fetch_agg(conn, dim_col: str) -> list[dict]:
    """按维度聚合 outcome 实测影响。dim_col 是 SQL 表达式（带别名）。"""
    return conn.execute(
        f"""
        SELECT
            {dim_col} AS dim_value,
            COUNT(*) FILTER (WHERE co.excess_72h IS NOT NULL) AS n72,
            COUNT(*) FILTER (WHERE co.excess_72h IS NOT NULL AND co.hit_72h IS NOT NULL) AS n_hit,
            COUNT(*) FILTER (WHERE co.excess_72h IS NOT NULL AND co.hit_72h) AS hit_true,
            ROUND(AVG(co.excess_24h)::numeric, 2) AS avg_excess_24h,
            ROUND(AVG(co.excess_72h)::numeric, 2) AS avg_excess_72h,
            ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY co.excess_72h)::numeric, 2) AS med_excess_72h,
            ROUND(AVG(co.excess_7d)::numeric, 2) AS avg_excess_7d,
            COUNT(*) FILTER (WHERE co.excess_7d IS NOT NULL) AS n7d,
            COUNT(*) FILTER (WHERE co.data_tier='L1') AS l1_cnt
        FROM biz.catalyst_outcome co
        JOIN biz.asset_catalyst ac ON ac.catalyst_id = co.catalyst_id
        WHERE co.excess_72h IS NOT NULL
        GROUP BY 1
        ORDER BY avg_excess_72h DESC NULLS LAST
        """
    ).fetchall()


def fmt_row(r: dict) -> tuple:
    """格式化一行。7d 超额样本 <10 时标 '-'（避免小样本/BTC扣减失真误导）。"""
    n72 = r["n72"] or 0
    n_hit = r["n_hit"] or 0
    hit_rate = round(r["hit_true"] / n_hit, 2) if n_hit else None
    lvl = impact_level(float(r["avg_excess_72h"]) if r["avg_excess_72h"] is not None else None)
    return (
        str(r["dim_value"]), n72, lvl,
        f"{hit_rate:.0%}" if hit_rate is not None else "-",
        f"{r['avg_excess_24h']:+.1f}" if r["avg_excess_24h"] is not None else "-",
        f"{r['avg_excess_72h']:+.1f}" if r["avg_excess_72h"] is not None else "-",
        f"{r['med_excess_72h']:+.1f}" if r["med_excess_72h"] is not None else "-",
        f"{r['avg_excess_7d']:+.1f}" if r["avg_excess_7d"] is not None and (r["n7d"] or 0) >= 10 else "-",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="历史催化剂影响程度报告")
    parser.add_argument("--dry-run", action="store_true", help="仅控制台不写报告")
    args = parser.parse_args()

    conn = get_conn()
    try:
        md = ["# 历史催化剂影响程度报告", "",
              f"> 生成日期: {date.today()} ｜ 数据: biz.catalyst_outcome（全部实测结局）",
              f"> 说明: 超额收益 = 资产收益 - BTC 同期收益；命中 = 方向与催化剂预期一致",
              f"> 影响分级(按72h平均超额): 强≥5% / 中2~5% / 弱0~2% / 无-2~0% / 反向<-2%", ""]

        # ---- 1. 总览 ----
        total = conn.execute("""
            SELECT COUNT(*) n, COUNT(DISTINCT catalyst_id) cats,
                   COUNT(*) FILTER (WHERE data_tier='L1') l1,
                   COUNT(*) FILTER (WHERE outcome_state='resolved') resolved
            FROM biz.catalyst_outcome WHERE excess_72h IS NOT NULL
        """).fetchone()
        print("=" * 72)
        print(f"历史催化剂实测影响总览（72h 超额可用）")
        print("=" * 72)
        print(f"  样本: {total['n']} 个 (catalyst×asset)｜ 催化剂 {total['cats']} 个 ｜ "
              f"L1精确 {total['l1']} ｜ 已结算14d {total['resolved']}")
        md += [f"**总览**：{total['n']} 个样本（{total['cats']} 个催化剂），L1 精确 {total['l1']}，14d 已结算 {total['resolved']}", ""]

        # ---- 2. 按事件类型 ----
        print("\n" + "=" * 72)
        print("按事件类型 event_type（72h 超额排序）")
        print("=" * 72)
        header = (f"  {'事件类型':<18} {'样本':>5} {'分级':>4} {'命中率':>6} "
                  f"{'24h超额':>8} {'72h超额':>8} {'中位72h':>8} {'7d超额':>8}")
        print(header)
        md += ["## 按事件类型", "", "| 事件类型 | 样本 | 影响分级 | 命中率 | 24h超额 | 72h超额 | 中位72h | 7d超额 |",
               "|---|---|---|---|---|---|---|---|"]
        for r in fetch_agg(conn, "COALESCE(ac.ai_event_type, ac.rule_event_type, 'other')"):
            if not r["dim_value"]:
                continue
            v = fmt_row(r)
            print(f"  {v[0]:<18} {v[1]:>5} {v[2]:>4} {v[3]:>6} {v[4]:>8} {v[5]:>8} {v[6]:>8} {v[7]:>8}")
            md.append(f"| {v[0]} | {v[1]} | {v[2]} | {v[3]} | {v[4]} | {v[5]} | {v[6]} | {v[7]} |")

        # ---- 3. 按来源 ----
        print("\n" + "=" * 72)
        print("按来源 source（72h 超额排序）")
        print("=" * 72)
        print(header)
        md += ["", "## 按来源", "", "| 来源 | 样本 | 影响分级 | 命中率 | 24h超额 | 72h超额 | 中位72h | 7d超额 |",
               "|---|---|---|---|---|---|---|---|"]
        for r in fetch_agg(conn, "ac.source_code"):
            if not r["dim_value"]:
                continue
            v = fmt_row(r)
            print(f"  {v[0]:<18} {v[1]:>5} {v[2]:>4} {v[3]:>6} {v[4]:>8} {v[5]:>8} {v[6]:>8} {v[7]:>8}")
            md.append(f"| {v[0]} | {v[1]} | {v[2]} | {v[3]} | {v[4]} | {v[5]} | {v[6]} | {v[7]} |")

        # ---- 4. 按方向 ----
        print("\n" + "=" * 72)
        print("按预期方向（72h 超额排序）")
        print("=" * 72)
        print(header)
        md += ["", "## 按预期方向", "", "| 方向 | 样本 | 影响分级 | 命中率 | 24h超额 | 72h超额 | 中位72h | 7d超额 |",
               "|---|---|---|---|---|---|---|---|"]
        for r in fetch_agg(conn, "co.impact_direction"):
            if not r["dim_value"]:
                continue
            v = fmt_row(r)
            print(f"  {v[0]:<18} {v[1]:>5} {v[2]:>4} {v[3]:>6} {v[4]:>8} {v[5]:>8} {v[6]:>8} {v[7]:>8}")
            md.append(f"| {v[0]} | {v[1]} | {v[2]} | {v[3]} | {v[4]} | {v[5]} | {v[6]} | {v[7]} |")

        # ---- 5. 强影响催化剂 TOP ----
        print("\n" + "=" * 72)
        print("强影响催化剂 TOP 15（72h 超额 ≥5%）")
        print("=" * 72)
        top = conn.execute("""
            SELECT co.catalyst_id, co.asset_id, a.canonical_symbol AS sym,
                   ac.title, co.excess_72h, co.excess_24h,
                   COALESCE(ac.ai_event_type, ac.rule_event_type, 'other') AS event_type,
                   co.impact_direction
            FROM biz.catalyst_outcome co
            JOIN biz.asset_catalyst ac ON ac.catalyst_id = co.catalyst_id
            LEFT JOIN core.asset a ON a.asset_id = co.asset_id
            WHERE co.excess_72h >= 5
            ORDER BY co.excess_72h DESC LIMIT 15
        """).fetchall()
        for r in top:
            title = (r["title"] or "")[:45]
            print(f"  [{r['sym'] or r['asset_id']}] {r['event_type']} 72h={r['excess_72h']:+.1f}% "
                  f"24h={r['excess_24h']:+.1f}% dir={r['impact_direction']}  {title}")
        md += ["", "## 强影响催化剂 TOP15（72h 超额 ≥5%）", ""]
        for r in top:
            title = (r["title"] or "")[:60]
            md.append(f"- **{r['sym'] or r['asset_id']}**（{r['event_type']}）"
                      f"72h {r['excess_72h']:+.1f}% / 24h {r['excess_24h']:+.1f}% — {title}")

        # ---- 6. 影响分布直方 ----
        print("\n" + "=" * 72)
        print("72h 超额分布")
        print("=" * 72)
        dist = conn.execute("""
            SELECT
              COUNT(*) FILTER (WHERE excess_72h >= 10) AS ge10,
              COUNT(*) FILTER (WHERE excess_72h >= 5 AND excess_72h < 10) AS ge5,
              COUNT(*) FILTER (WHERE excess_72h >= 2 AND excess_72h < 5) AS ge2,
              COUNT(*) FILTER (WHERE excess_72h >= 0 AND excess_72h < 2) AS ge0,
              COUNT(*) FILTER (WHERE excess_72h >= -2 AND excess_72h < 0) AS ge_n2,
              COUNT(*) FILTER (WHERE excess_72h < -2) AS lt_n2
            FROM biz.catalyst_outcome WHERE excess_72h IS NOT NULL
        """).fetchone()
        total_n = sum(dist.values()) or 1
        for label, key in [("≥+10%", "ge10"), ("+5~10%", "ge5"), ("+2~5%", "ge2"),
                           ("0~2%", "ge0"), ("-2~0%", "ge_n2"), ("<-2%", "lt_n2")]:
            n = dist[key]
            bar = "█" * round(n / total_n * 50)
            print(f"  {label:<8} {n:>5} ({n/total_n:5.1%}) {bar}")
        md += ["", "## 72h 超额分布", ""]
        for label, key in [("≥+10%", "ge10"), ("+5~10%", "ge5"), ("+2~5%", "ge2"),
                           ("0~2%", "ge0"), ("-2~0%", "ge_n2"), ("<-2%", "lt_n2")]:
            md.append(f"- {label}: {dist[key]} 条（{dist[key]/total_n:.1%}）")

        if not args.dry_run:
            out = SCRIPT_DIR / "data" / f"catalyst_impact_report_{date.today()}.md"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("\n".join(md) + "\n", encoding="utf-8")
            print(f"\n报告已导出: {out}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
