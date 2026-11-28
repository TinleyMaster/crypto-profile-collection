#!/usr/bin/env python3
"""OI 影子闸门 · 修正版校验报告（v2）——只读，不影响实盘、不改阈值。

为什么有 v2（2026-11-28 女王复核）
-----------------------------------------------------------------
  原 `gainer_oi_shadow_report.py` 的四项判定是「尺子坏了」：
    ① 放行侧 8 条全 LONG、拦截侧 86% SHORT → 「放行 vs 拦截」实为「LONG vs SHORT」
       （SHORT 侧 oi_chg_12h 最大值 0.475，结构上永远到不了 1.0 阈值）。
    ② 同方向比较（同为 LONG）放行侧胜/均/PF 三项全低于拦截侧 → 原判据①的 +15.9pp
       来自方向混淆，不是 OI 质量。
    ③ 判定④的 `mfe` 实为 `exit_px/entry_px-1`（已实现毛收益），且放行侧 100% TRAIL
       出场、平均持有 0.91h → 结构上吃不到 20%，判定④永远无法满足。
    ④ 单笔 BIGTIME +13.11% 贡献 77% 利润，n=8 仅 6 独立符号 → 统计无效。
  「继续攒样本」只会把错误对比攒厚。本脚本把尺子修准，再谈 OI 因子去留。

四处修正
-----------------------------------------------------------------
  ① 方向分层：只在同方向内比较（LONG 放行 vs LONG 拦截）；SHORT 放行 n=0 时
     显式标注「结构性无样本 / 闸门不适用」，不参与判定。
  ② 真 MFE：从 `biz.asset_klines`(1h) 取 [signal_ts, +24h] 区间极值，
     LONG = max(high_px)/entry - 1；SHORT = 1 - min(low_px)/entry（方向对齐）。
     不再用 exit_px 冒充 MFE。同时保留 ret_gross 列供对照。
  ③ 12h 固定持有口径：用信号后第 12 根 1h bar 的 close（与回测固定持有对齐），
     ret_12h = direction*(close_12h/entry - 1) - COST，与 TRAIL 口径并列输出。
  ④ 单笔敏感性：输出最大单笔贡献占比、去掉最大盈利笔后的 PF/胜率、独立符号数。

判定标准（同方向内，仅作为「尺子是否指向同一方向」的观察，不构成开闸指令）
-----------------------------------------------------------------
  LONG 侧：① 胜率差 ≥10pp ② 放行 PF≥1.5 且 n≥30 ③ 12h 口径三段均净全正
           ④ 真 MFE≥20% 占比 放行 ≥ 拦截
  SHORT 侧：放行 n=0 → 标注不适用，不判。

用法
-----------------------------------------------------------------
  python bin/gainer_oi_shadow_report_v2.py            # 全量
  python bin/gainer_oi_shadow_report_v2.py --days 30
  python bin/gainer_oi_shadow_report_v2.py --selftest # 纯本地自检（不连库）
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from gainer_oi_shadow import (  # noqa: E402
    OI_BUCKET_BLOCK, OI_BUCKET_NA, OI_BUCKET_PASS,
    OI_KEY, OI_SHADOW_THRESHOLD, OI_SHADOW_VERSION,
)

COST = 0.003              # 与 collect_gainer_outcome.py / 回测 cost0.3 对齐
MFE_WINDOW_H = 24         # 真 MFE 观察窗口（信号后 24h）
HOLD12_BARS = 12          # 12h 固定持有口径（信号后第 12 根 1h bar）

# 方向标签：direction(+1/-1) → 中文
DIR_LABEL = {1: "LONG", -1: "SHORT", 0: "?"}


def pf(net: np.ndarray) -> float:
    loss = -net[net < 0].sum()
    return float(net[net > 0].sum() / loss) if loss > 0 else float("inf")


def stat_block(recs: list[dict], tag: str) -> dict | None:
    """同方向 × 同档位的一组统计。含真 MFE / 12h 口径 / 单笔敏感性。"""
    if len(recs) < 1:
        print(f"  {tag:26s} n=0  —— 无样本")
        return None
    net = np.array([r["ret_net"] for r in recs])
    r12 = np.array([r["ret_12h"] for r in recs if r["ret_12h"] is not None])
    kk = max(1, len(recs) // 3)
    segs = []
    for g in (recs[:kk], recs[kk:2 * kk], recs[2 * kk:]):
        if g:
            sub = [r["ret_12h"] for r in g if r["ret_12h"] is not None]
            segs.append(float(np.mean(sub)) if sub else 0.0)
    t = (net.mean() / (net.std(ddof=1) / np.sqrt(len(net)))) if len(net) > 1 and net.std(ddof=1) > 0 else 0.0
    r20 = 100 * float(np.mean([r["mfe_true"] is not None and r["mfe_true"] >= 0.20 for r in recs]))
    d = {
        "n": len(recs), "win": 100 * float((net > 0).mean()), "mean": 100 * float(net.mean()),
        "med": 100 * float(np.median(net)), "PF": pf(net), "t": t, "r20": r20, "segs": segs,
    }
    # 单笔敏感性
    pos = net[net > 0]
    d["max_ret"] = 100 * float(net.max()) if len(net) else 0.0
    d["top_contrib"] = (100 * float(net.max() / pos.sum())) if pos.sum() > 0 else 0.0
    sub_ex = [r for r in recs if r["ret_net"] != net.max()]
    d["ex_top_win"] = 100 * float((np.array([r["ret_net"] for r in sub_ex]) > 0).mean()) if sub_ex else 0.0
    d["ex_top_pf"] = pf(np.array([r["ret_net"] for r in sub_ex])) if sub_ex else float("inf")
    d["n_sym"] = len({r["sym"] for r in recs})
    print(f"  {tag:26s} n={d['n']:4d} 胜={d['win']:5.1f}% 均={d['mean']:+6.2f}% "
          f"中位={d['med']:+6.2f}% PF={d['PF']:5.2f} t={t:+5.2f} "
          f"真MFE20={r20:4.1f}%  12h三段 {'/'.join('%+.2f' % x for x in segs)}")
    if d["n"] >= 2:
        print(f"  {'':26s}   单笔敏感: 最大 {d['max_ret']:+.2f}% 贡献 {d['top_contrib']:.0f}% "
              f"→ 去掉后 胜={d['ex_top_win']:.1f}% PF={d['ex_top_pf']:.2f} "
              f"(独立符号 {d['n_sym']}/{d['n']})")
    return d


def _composition(recs: list[dict], title: str) -> None:
    """显式输出方向构成 + 出场类型构成，避免再被 86% SHORT 带偏。"""
    from collections import Counter
    print(f"\n=== {title} ===")
    dirs = Counter(DIR_LABEL.get(r["dir"], "?") for r in recs)
    sigs = Counter(r["sig"] for r in recs)
    exits = Counter(r["exit_type"] or "NULL" for r in recs)
    holds = [r["hold_hours"] for r in recs if r["hold_hours"] is not None]
    print(f"  方向: " + "  ".join(f"{k}={v}" for k, v in dirs.most_common()))
    print(f"  信号: " + "  ".join(f"{k}={v}" for k, v in sigs.most_common()))
    print(f"  出场: " + "  ".join(f"{k}={v}" for k, v in exits.most_common()))
    if holds:
        print(f"  平均持有: {np.mean(holds):.2f}h  oi_chg 最大: "
              f"{max((r['oi'] for r in recs if r['oi'] is not None), default=float('nan')):.3f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0, help="只看近 N 天（0=全量）")
    ap.add_argument("--selftest", action="store_true", help="纯本地自检，不连库")
    args = ap.parse_args()

    if args.selftest:
        # 自检：只验纯函数，不连 DB
        fake = [
            {"sym": "AAA", "sig": "SHORT_LONG", "dir": 1, "ret_net": 0.10, "ret_12h": -0.20,
             "mfe_true": 0.25, "exit_type": "TRAIL", "hold_hours": 0.9, "oi": 1.2},
            {"sym": "BBB", "sig": "MID_LONG", "dir": 1, "ret_net": -0.01, "ret_12h": -0.01,
             "mfe_true": 0.05, "exit_type": "TRAIL", "hold_hours": 1.1, "oi": 1.1},
            {"sym": "CCC", "sig": "TRAP_SHORT", "dir": -1, "ret_net": -0.01, "ret_12h": -0.01,
             "mfe_true": 0.02, "exit_type": "FIXCLOSE", "hold_hours": 11.8, "oi": 0.02},
        ]
        print("[selftest] stat_block 纯函数:")
        stat_block(fake[:2], "LONG/放行(样例)")
        stat_block(fake[2:], "SHORT/拦截(样例)")
        print("[selftest] pf([+1,-0.5]) =", pf(np.array([1.0, -0.5])))
        print("[selftest] OK（未连库）")
        return 0

    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    settings = get_settings(require_database=True)
    where = ""
    params: tuple = ()
    if args.days:
        where = "WHERE s.scan_ts >= NOW() - (%s * INTERVAL '1 day')"
        params = (args.days,)

    # 主查询：信号 × 结算 × 真 MFE(1h区间极值) × 12h 固定持有 close
    sql = f"""
        SELECT s.id, s.symbol, s.signal_type, s.direction, s.scan_ts,
               (s.params->>'{OI_KEY}')::numeric AS oi_chg,
               (s.params->>'oi_bucket')::int AS bucket,
               o.entry_px, o.ret_net, o.exit_type, o.hold_hours,
               m.mfe_hi, m.mfe_lo,
               c12.close_px AS close_12h
          FROM biz.scan_gainer_signal s
          JOIN biz.scan_gainer_outcome o ON o.signal_id = s.id
          LEFT JOIN LATERAL (
              SELECT max(k.high_px) AS mfe_hi, min(k.low_px) AS mfe_lo
                FROM biz.asset_klines k
               WHERE k.symbol = s.symbol AND k.interval = '1h'
                 AND k.open_time >= s.scan_ts
                 AND k.open_time <  s.scan_ts + INTERVAL '{MFE_WINDOW_H} hours'
          ) m ON TRUE
          LEFT JOIN LATERAL (
              SELECT k.close_px
                FROM biz.asset_klines k
               WHERE k.symbol = s.symbol AND k.interval = '1h'
                 AND k.open_time >= (date_trunc('hour', s.scan_ts AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')
                 AND k.open_time <= (date_trunc('hour', s.scan_ts AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')
                                     + INTERVAL '{HOLD12_BARS} hours'
               ORDER BY k.open_time DESC LIMIT 1
          ) c12 ON TRUE
          {where}
         ORDER BY s.scan_ts
    """
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    print("=" * 120)
    print(f"OI 影子闸门 · 修正版校验 v2（版本 {OI_SHADOW_VERSION}，阈值 {OI_KEY} ≥ {OI_SHADOW_THRESHOLD}）")
    print(f"生成于 {datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S')}　"
          f"[只读，不改实盘/不改阈值]")
    print("=" * 120)

    recs = []
    for (sid, sym, sig, direction, scan_ts, oi_chg, bucket, entry_px, ret_net,
         exit_type, hold_hours, mfe_hi, mfe_lo, close_12h) in rows:
        if bucket is None or bucket == OI_BUCKET_NA or ret_net is None:
            continue
        dr = int(direction) if direction is not None else 0
        entry = float(entry_px) if entry_px is not None else None
        # 真 MFE（方向对齐）
        mfe_true = None
        if entry and entry > 0:
            if dr == 1 and mfe_hi is not None:
                mfe_true = float(mfe_hi) / entry - 1.0
            elif dr == -1 and mfe_lo is not None:
                mfe_true = 1.0 - float(mfe_lo) / entry
        # 12h 固定持有口径
        ret_12h = None
        if entry and entry > 0 and close_12h is not None and dr in (1, -1):
            ret_12h = dr * (float(close_12h) / entry - 1.0) - COST
        recs.append({
            "id": sid, "sym": sym, "sig": sig, "dir": dr, "ts": scan_ts,
            "oi": float(oi_chg) if oi_chg is not None else None,
            "bucket": int(bucket), "ret_net": float(ret_net),
            "ret_12h": ret_12h, "mfe_true": mfe_true,
            "exit_type": exit_type, "hold_hours": float(hold_hours) if hold_hours is not None else None,
        })
    recs.sort(key=lambda r: r["ts"])

    if not recs:
        print("\n[info] 暂无带 OI 特征的已结算信号（影子闸门 2026-11-16 才上线，属正常）")
        return 0

    n_pass = sum(1 for r in recs if r["bucket"] == OI_BUCKET_PASS)
    n_block = sum(1 for r in recs if r["bucket"] == OI_BUCKET_BLOCK)
    span = (recs[-1]["ts"] - recs[0]["ts"]).days
    print(f"\n已结算且带 OI 特征：{len(recs):,} 条（放行 {n_pass} / 拦截 {n_block}）　"
          f"窗口 {recs[0]['ts']:%Y-%m-%d} ~ {recs[-1]['ts']:%Y-%m-%d}（{span} 天）")

    _composition(recs, "全样本方向/出场构成（先看这个，别再被 SHORT 带偏）")

    # ── 修正①：同方向分层比较 ─────────────────────────────────────
    print("\n" + "=" * 120)
    print("=== 同方向分层比较（修正①：不再拿 LONG 比 SHORT）===")
    stats: dict[tuple[int, int], dict] = {}
    for dr in (1, -1):
        for bk in (OI_BUCKET_PASS, OI_BUCKET_BLOCK):
            sub = [r for r in recs if r["dir"] == dr and r["bucket"] == bk]
            tag = f"{DIR_LABEL[dr]}/{'放行' if bk == OI_BUCKET_PASS else '拦截'}"
            d = stat_block(sub, tag)
            if d:
                stats[(dr, bk)] = d

    # ── 修正④：单笔主导 + 闸门覆盖 ────────────────────────────────
    long_pass = stats.get((1, OI_BUCKET_PASS))
    if long_pass and long_pass["top_contrib"] >= 50:
        print(f"\n  ⚠️ 单笔主导：LONG/放行 最大一笔贡献 {long_pass['top_contrib']:.0f}% "
              f"→ 去掉后 PF {long_pass['ex_top_pf']:.2f}（n={long_pass['n']}，"
              f"独立符号 {long_pass['n_sym']}）。该组统计不可用于开闸。")

    short_pass = stats.get((-1, OI_BUCKET_PASS))
    if short_pass is None:
        sp = [r for r in recs if r["dir"] == -1]
        mx = max((r["oi"] for r in sp if r["oi"] is not None), default=float("nan"))
        print(f"\n  ⚠️ SHORT 侧结构性无放行样本：n={len(sp)}，oi_chg 最大 {mx:.3f} < 阈值 "
              f"{OI_SHADOW_THRESHOLD} → OI 闸门对 SHORT 不适用，适用面仅 LONG。")

    # ── 修正②③ + 判定（同方向内）─────────────────────────────────
    lp = stats.get((1, OI_BUCKET_PASS))
    lb = stats.get((1, OI_BUCKET_BLOCK))
    print("\n" + "=" * 120)
    print("=== LONG 侧判定（同方向内；仅观察方向是否一致，不构成开闸指令）===")
    if lp and lb:
        c1 = (lp["win"] - lb["win"]) >= 10.0
        c2 = lp["PF"] >= 1.5 and lp["n"] >= 30
        c3 = all(x > 0 for x in lp["segs"])
        c4 = lp["r20"] >= lb["r20"]
        print(f"  ① 胜率差 ≥10pp（同方向）   {lp['win']-lb['win']:+6.1f}pp   {'✅' if c1 else '❌'}")
        print(f"  ② PF≥1.5 且 n≥30          PF={lp['PF']:.2f} n={lp['n']}   {'✅' if c2 else '❌'}")
        print(f"  ③ 12h三段均净全正          {'/'.join('%+.2f' % x for x in lp['segs'])}   {'✅' if c3 else '❌'}")
        print(f"  ④ 真MFE20 放行≥拦截        {lp['r20']:.1f}% vs {lb['r20']:.1f}%   {'✅' if c4 else '❌'}")
        ok = c1 and c2 and c3 and c4
        print(f"\n  → {'四项全过（仍需工单评审，非自动开闸）' if ok else '⏳ 未达标'}")
        if lp["n"] < 30:
            print(f"  ⚠️ n={lp['n']}<30：单所口径 LONG 侧放行 9 天仅 {lp['n']} 条（"
                  f"独立符号 {lp['n_sym']}），且单笔贡献 {lp['top_contrib']:.0f}%——"
                  f"这不是「攒样本」能解决的，是口径覆盖问题。")
    else:
        print("  LONG 放行或拦截侧样本不足，无法判定。")

    print("\n[注] 本报告只读；影子闸门未拦截任何交易，阈值与执行逻辑均未改动。")
    print("[注] 真 MFE 取信号后 24h 的 1h 极值；12h 口径取信号后第 12 根 1h bar close，"
          "与回测固定持有对齐；TRAIL 口径(ret_net)并列保留供对照。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
