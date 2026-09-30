#!/usr/bin/env python3
"""盘口深度 vs 成交额门槛的**只读标定**（SCAN-LIQ-DEPTH-001 阶段 A 标定件）。

输入：`biz.orderbook_depth_history`（fix_081 回填）+ `biz.asset_klines`（vol7d）。
输出：三张**描述性**表（不进任何判定）：
  1. 分布对照：深度 vs vol7d 的分位；
  2. 截面关系：depth/vol7d（k 系数）稳定度 + 秩相关 —— 回答「成交额是不是合格的代理」；
  3. 分歧集：四象限（成交额留/剔 × 深度留/剔）+ 单笔 X 的真实参与度分布。

⚠️ 纪律（工单 §1.2 / §6）：
  - 本脚本**只读、只描述**，不产出阈值结论（取值走另行授权的标定流程）；
  - 深度（存量）与成交额（流量）**不同量纲**，只在「同币对比/分歧判定」里并列，绝不换算；
  - 「深度说剔、成交额说留」的分歧只是**代理可疑样本**，不是「该剔」的证据
    （§9.8 已否决「头部选币」路线，此处同样不得反推）。

用法：
    python calib_ob_depth_vs_vol.py --days 7 --range 1 --x 300 --rho 0.0001
    python calib_ob_depth_vs_vol.py --json
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "scripts" / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402


def pctl(vals, p):
    if not vals:
        return float("nan")
    v = sorted(vals)
    return v[min(len(v) - 1, int(len(v) * p))]


def fmt(x):
    if x is None or x != x:
        return "n/a"
    if abs(x) >= 1e8:
        return f"{x/1e8:.2f}亿"
    if abs(x) >= 1e4:
        return f"{x/1e4:.1f}万"
    return f"{x:.0f}"


def main() -> int:
    ap = argparse.ArgumentParser(description="盘口深度 vs 成交额门槛 · 只读标定")
    ap.add_argument("--days", type=int, default=7, help="常态窗口（与 vol7d 口径对齐）")
    ap.add_argument("--range", dest="range_pct", type=float, default=1.0, help="深度档（±%）")
    ap.add_argument("--scope", default="binance", choices=["binance", "all"])
    ap.add_argument("--x", type=float, default=300.0, help="单笔名义仓位（USDT）")
    ap.add_argument("--rho", type=float, default=0.0001, help="容忍参与度（1bp=0.0001）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            # 深度：近 N 天每桶 min(bids,asks) 的中位数（取双侧较弱一侧，双向都要能进出）
            cur.execute(
                """
                SELECT symbol, percentile_cont(0.5) WITHIN GROUP (ORDER BY side_min)
                FROM (
                    SELECT symbol, LEAST(bids_usd, asks_usd) AS side_min
                    FROM biz.orderbook_depth_history
                    WHERE interval='4h' AND exchange_scope=%s AND range_pct=%s
                      AND ts >= NOW() - make_interval(days => %s)
                      AND bids_usd IS NOT NULL AND asks_usd IS NOT NULL
                ) t
                GROUP BY symbol
                """,
                (args.scope, args.range_pct, args.days))
            depth = {r[0]: float(r[1]) for r in cur.fetchall() if r[1] is not None}
            # vol7d：与线上 _load_vol7d_avg 同口径
            cur.execute(
                """
                SELECT symbol, SUM(quote_vol) / %s AS vol_avg
                FROM biz.asset_klines
                WHERE interval='1h' AND open_time >= NOW() - make_interval(days => %s)
                GROUP BY symbol
                HAVING SUM(quote_vol) > 0
                """,
                (float(args.days), args.days))
            vol = {r[0]: float(r[1]) for r in cur.fetchall()}

    both = sorted(set(depth) & set(vol))
    only_depth = sorted(set(depth) - set(vol))
    only_vol = sorted(set(vol) - set(depth))
    print(f"[calib] 深度覆盖 {len(depth)} 币（±{args.range_pct}%、近 {args.days} 天中位）；"
          f"vol{args.days}d 覆盖 {len(vol)} 币；交集 {len(both)}")
    if only_depth:
        print(f"  仅深度侧 {len(only_depth)}（池外/已下架 K 线）: {only_depth[:8]}")
    if only_vol:
        print(f"  仅成交额侧 {len(only_vol)}（接口未覆盖/无永续）: {only_vol[:8]}")
    if len(both) < 30:
        print(f"[fatal] 交集样本 {len(both)} < 30，拒判（先跑回填）", file=sys.stderr)
        return 3

    # ── 1) 分布对照 ──
    dv = [depth[s] for s in both]
    vv = [vol[s] for s in both]
    print(f"\n[1] 分布对照（n={len(both)}）")
    print(f"{'分位':>6} | {'±' + str(args.range_pct) + '%深度(中位)':>14} | {'vol(日均成交额)':>16}")
    for p in (0.10, 0.25, 0.50, 0.75, 0.90, 1.0):
        print(f"  p{int(p*100):<4} | {fmt(pctl(dv,p)):>14} | {fmt(pctl(vv,p)):>16}")

    # ── 2) 截面关系 ──
    ks = sorted(depth[s] / vol[s] for s in both if vol[s] > 0)
    print(f"\n[2] 截面关系 depth/vol（每 1 USD 日成交额对应的 ±{args.range_pct}% 挂单深度）")
    for p in (0.10, 0.25, 0.50, 0.75, 0.90):
        print(f"    p{int(p*100):<4} = {pctl(ks,p)*1e4:>8.2f} bp（{pctl(ks,p):.2e}）")
    n = len(both)
    rv = {s: r for r, s in enumerate(sorted(both, key=lambda x: vol[x]), 1)}
    rd = {s: r for r, s in enumerate(sorted(both, key=lambda x: depth[x]), 1)}
    dsum = sum((rv[s] - rd[s]) ** 2 for s in both)
    rho_rank = 1 - 6 * dsum / (n * (n * n - 1)) if n > 1 else float("nan")
    print(f"    秩相关（成交额 vs 深度）= {rho_rank:.3f}（n={n}）")

    # ── 3) 门槛分歧 + 真实参与度 ──
    THR = args.x / args.rho
    keep_vol = {s for s in both if vol[s] >= THR}
    keep_dep = {s for s in both if depth[s] >= THR}
    quad = {
        "both_keep": keep_vol & keep_dep,
        "vol_keep_depth_cut": keep_vol - keep_dep,   # ⚠️ 代理可疑：成交额说能进出、深度说不能
        "depth_keep_vol_cut": keep_dep - keep_vol,   # ⚠️ 代理可疑：反向
        "both_cut": set(both) - keep_vol - keep_dep,
    }
    print(f"\n[3] 门槛对照（X={args.x:.0f} U, ρ={args.rho*1e4:.0f}bp ⇒ 门槛 {fmt(THR)}）")
    print(f"    vol 门槛保留 {len(keep_vol)}/{n}（剔 {n-len(keep_vol)}）")
    print(f"    深度门槛保留 {len(keep_dep)}/{n}（剔 {n-len(keep_dep)}）")
    print(f"    四象限：都留 {len(quad['both_keep'])} · 都剔 {len(quad['both_cut'])} · "
          f"成交额留/深度剔 {len(quad['vol_keep_depth_cut'])} · "
          f"深度留/成交额剔 {len(quad['depth_keep_vol_cut'])}")

    def show(title, syms, key_desc, limit=10):
        if not syms:
            print(f"\n    {title}：空")
            return
        print(f"\n    {title}（前 {limit}，按成交额降序）：")
        for s in sorted(syms, key=lambda x: -vol[x])[:limit]:
            print(f"      {s:<14} vol={fmt(vol[s]):>10}  深度={fmt(depth[s]):>10}"
                  f"  单笔占深度={args.x/depth[s]*1e4:>8.1f} bp")

    show("⚠️ 成交额留·深度剔（代理可能高估可执行性）", quad["vol_keep_depth_cut"], "vol")
    show("⚠️ 深度留·成交额剔（代理可能低估可执行性）", quad["depth_keep_vol_cut"], "vol", 8)

    parts = sorted(args.x / depth[s] * 1e4 for s in both)
    print(f"\n[3b] 单笔 {args.x:.0f} U 占 ±{args.range_pct}% 深度的真实参与度（bp）")
    for p in (0.10, 0.25, 0.50, 0.75, 0.90, 1.0):
        print(f"    p{int(p*100):<4} = {pctl(parts,p):>9.2f} bp")

    if args.json:
        out = {
            "n_both": n, "range_pct": args.range_pct, "days": args.days,
            "x": args.x, "rho": args.rho, "threshold": THR,
            "quadrant_sizes": {k: len(v) for k, v in quad.items()},
            "rank_corr": rho_rank,
            "k_bp_pctl": {f"p{int(p*100)}": pctl(ks, p) * 1e4
                          for p in (0.10, 0.50, 0.90)},
            "participation_bp_pctl": {f"p{int(p*100)}": pctl(parts, p)
                                      for p in (0.10, 0.50, 0.90)},
        }
        print("\n" + json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
