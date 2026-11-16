#!/usr/bin/env python3
"""OI 影子闸门 · 每日校验报告（只读，不影响实盘）。

用途
-----------------------------------------------------------------
  OI 影子闸门（gainer_oi_shadow.py）在扫描器落库时给每条信号写入：
    params->>'oi_chg_12h'    Binance 单所 OI 12h 变化率（无前视）
    params->>'oi_bucket'     1=放行侧（≥阈值） / 0=拦截侧（<阈值） / -1=无数据
    params->>'oi_shadow_pass' bool
    params->>'oi_shadow_ver' 版本
  本脚本每日把「放行侧 vs 拦截侧」的后续表现对比出来，积累前向验证样本。

判定标准（开闸前必须全部满足）
-----------------------------------------------------------------
  ① 放行侧胜率 − 拦截侧胜率 ≥ 10pp
  ② 放行侧 PF ≥ 1.5 且 样本 n ≥ 30
  ③ OOS 三段（按时间等分）放行侧均净全部为正
  ④ 无右尾塌陷：放行侧 MFE≥20% 占比 ≥ 拦截侧

用法：
  python bin/gainer_oi_shadow_report.py            # 全量
  python bin/gainer_oi_shadow_report.py --days 30  # 近 30 天
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


def pf(net: np.ndarray) -> float:
    l = -net[net < 0].sum()
    return float(net[net > 0].sum() / l) if l > 0 else float("inf")


def stat_block(sub: list[dict], tag: str) -> dict | None:
    if len(sub) < 5:
        print(f"  {tag:22s} n={len(sub):4d}  样本不足")
        return None
    net = np.array([r["ret"] for r in sub])
    kk = max(1, len(sub) // 3)
    segs = [float(np.mean([r["ret"] for r in g]))
            for g in (sub[:kk], sub[kk:2 * kk], sub[2 * kk:]) if g]
    t = (net.mean() / (net.std(ddof=1) / np.sqrt(len(net)))) if net.std(ddof=1) > 0 else 0.0
    r20 = 100 * float(np.mean([r["mfe"] >= 0.20 for r in sub]))
    d = {
        "n": len(sub), "win": 100 * float((net > 0).mean()), "mean": 100 * float(net.mean()),
        "med": 100 * float(np.median(net)), "PF": pf(net), "t": t, "r20": r20, "segs": segs,
    }
    print(f"  {tag:22s} n={d['n']:4d} 胜={d['win']:5.1f}% 均={d['mean']:+6.2f}% "
          f"中位={d['med']:+6.2f}% PF={d['PF']:5.2f} t={t:+5.2f} "
          f"右尾={r20:4.1f}%  OOS {'/'.join('%+.2f' % x for x in segs)}")
    return d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0, help="只看近 N 天（0=全量）")
    args = ap.parse_args()

    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    settings = get_settings(require_database=True)
    where = ""
    params: tuple = ()
    if args.days:
        where = "WHERE o.scan_ts >= NOW() - (%s * INTERVAL '1 day')"
        params = (args.days,)

    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT s.symbol, s.signal_type, s.scan_ts,
                       (s.params->>'{OI_KEY}')::numeric AS oi_chg,
                       (s.params->>'oi_bucket')::int AS bucket,
                       o.ret_net, o.exit_px, o.entry_px
                FROM biz.scan_gainer_signal s
                JOIN biz.scan_gainer_outcome o ON o.signal_id = s.id
                {where}
                ORDER BY s.scan_ts
            """, params)
            rows = cur.fetchall()

    print("=" * 118)
    print(f"OI 影子闸门 · 每日校验（版本 {OI_SHADOW_VERSION}，阈值 {OI_KEY} ≥ {OI_SHADOW_THRESHOLD}）")
    print(f"生成于 {datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 118)

    if not rows:
        print("\n[info] 暂无带 OI 特征的已结算信号（影子闸门 2026-11-16 才上线，属正常）")
        return 0

    recs = []
    for sym, sig, scan_ts, oi_chg, bucket, ret_net, exit_px, entry_px in rows:
        if bucket is None or ret_net is None:
            continue
        mfe = (float(exit_px) / float(entry_px) - 1) if (exit_px and entry_px) else 0.0
        recs.append({"sym": sym, "sig": sig, "ts": scan_ts, "oi": float(oi_chg) if oi_chg is not None else None,
                     "bucket": int(bucket), "ret": float(ret_net), "mfe": mfe})
    recs.sort(key=lambda r: r["ts"])

    n_pass = sum(1 for r in recs if r["bucket"] == OI_BUCKET_PASS)
    n_block = sum(1 for r in recs if r["bucket"] == OI_BUCKET_BLOCK)
    print(f"\n已结算且带 OI 特征：{len(recs):,} 条"
          f"（放行侧 {n_pass} / 拦截侧 {n_block}）")
    if not recs:
        return 0
    span = (recs[-1]["ts"] - recs[0]["ts"]).days
    print(f"样本窗口：{recs[0]['ts']:%Y-%m-%d} ~ {recs[-1]['ts']:%Y-%m-%d}（{span} 天）")

    print(f"\n=== 放行侧 vs 拦截侧（结算口径 ret_net）===")
    p = stat_block([r for r in recs if r["bucket"] == OI_BUCKET_PASS], f"放行侧 (OI≥阈值)")
    b = stat_block([r for r in recs if r["bucket"] == OI_BUCKET_BLOCK], f"拦截侧 (OI<阈值)")

    if p and b:
        print("\n=== 开闸判定（需全部满足才可考虑上线）===")
        c1 = (p["win"] - b["win"]) >= 10.0
        c2 = p["PF"] >= 1.5 and p["n"] >= 30
        c3 = all(x > 0 for x in p["segs"])
        c4 = p["r20"] >= b["r20"]
        print(f"  {'① 胜率差 ≥10pp':32s} {p['win']-b['win']:+6.1f}pp   {'✅' if c1 else '❌'}")
        print(f"  {'② PF≥1.5 且 n≥30':32s} PF={p['PF']:.2f} n={p['n']}   {'✅' if c2 else '❌'}")
        print(f"  {'③ OOS 三段均净全正':32s} {'/'.join('%+.2f' % x for x in p['segs'])}   {'✅' if c3 else '❌'}")
        print(f"  {'④ 右尾无塌陷':32s} {p['r20']:.1f}% vs {b['r20']:.1f}%   {'✅' if c4 else '❌'}")
        ok = c1 and c2 and c3 and c4
        print(f"\n  → {'✅ 四项全过，可提工单评审开闸' if ok else '⏳ 未达标，继续影子观察'}")

    # OI 分布
    vals = [r["oi"] for r in recs if r["oi"] is not None]
    if vals:
        v = np.array(vals)
        print(f"\n=== {OI_KEY} 分布（n={len(v)}）===")
        print("  " + "  ".join(f"p{q}={np.percentile(v, q)*100:+.1f}%" for q in (5, 25, 50, 75, 95)))
    print("\n[注] 本报告只读，不影响实盘；影子闸门未拦截任何交易。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
