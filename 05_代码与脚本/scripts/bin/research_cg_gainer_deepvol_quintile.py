#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""涨幅榜 × 深缩排序 分档回测（2026-10-07）

问题：深缩指标不应看绝对值，应作为横截面排序因子——「当日涨幅榜（24h 涨幅>0）内，
越缩量（VOL_ratio 越低）越靠前」，检验持仓 8h 价格表现是否随缩量程度单调。

设计（用户确认口径）：
  候选池 = 滚动 24h 涨幅 > 0 的全部币（用 1h K 线算：close(now)/close(now-24h)-1）
  排序   = 池内按当前桶 VOL_ratio 分 5 档（Q1 最缩量 … Q5 最不缩量/放量）
  对照   = 涨幅榜>0 全体基线 / 涨幅榜≤0（跌的币）
  持仓   = 4h 粒度入场（桶末），H4/H8/H12/H24 全看，重点 H8
  无前视 = 24h 涨幅与 VOL_ratio 均为桶末已知值

输出：候选池内 Q1~Q5 各档 H4/H8/H12/H24 均值/中位/胜率 + 单调性判定；
      与涨幅榜≤0 对照。跨币合并（横截面，无单币样本不足问题）。

用法：
  python research_cg_gainer_deepvol_quintile.py --interval 4h
  python research_cg_gainer_deepvol_quintile.py --interval 12h   # 附看长粒度
"""
from __future__ import annotations

import argparse
import math
import statistics
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

HORIZONS = {"4h": (4, 8, 12, 24), "12h": (12, 24, 48, 72)}
MIN_MED_ABS_RET = 0.003


def load_cvd(conn, interval: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, ts, taker_buy_usd, taker_sell_usd FROM biz.cg_taker_volume_hist "
            "WHERE interval=%s AND taker_buy_usd IS NOT NULL AND taker_sell_usd IS NOT NULL "
            "ORDER BY symbol, ts", (interval,))
        return cur.fetchall()


def load_klines_1h(conn, symbols: set[str], lo_ts) -> dict[str, dict]:
    out: dict[str, dict] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, open_time, close_px FROM biz.asset_klines "
            "WHERE interval='1h' AND open_time >= %s::timestamp AND symbol = ANY(%s::text[]) "
            "ORDER BY symbol, open_time", (lo_ts, list(symbols)))
        for sym, ot, close in cur.fetchall():
            if close is None or float(close) <= 0:
                continue
            out.setdefault(sym, {})[ot] = float(close)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", choices=("4h", "12h"), default="4h")
    args = ap.parse_args()
    interval = args.interval
    iv_h = int(interval[:-1])

    s = get_settings()
    with get_connection(s.database_url) as conn:
        cvd_rows = load_cvd(conn, interval)
        syms = {r[0] for r in cvd_rows} | {"BTCUSDT"}
        lo = min((r[1] for r in cvd_rows), default=None)
        if lo is None:
            print("无数据")
            return
        klines = load_klines_1h(conn, syms, lo)

    # 每币 VOL 中位数 + 每桶 taker 总额
    vol_sum: dict[tuple, float] = {}
    for sym, ts, b, sl in cvd_rows:
        vol_sum[(sym, ts)] = float(b) + float(sl)
    vol_by_sym: dict[str, list[float]] = defaultdict(list)
    for (sym, ts), v in vol_sum.items():
        vol_by_sym[sym].append(v)
    vol_med = {sym: statistics.median(vs) for sym, vs in vol_by_sym.items() if vs}
    # 每币的桶时间戳集合（用于遍历真实桶）
    ts_by_sym: dict[str, list] = defaultdict(list)
    for sym, ts in vol_sum:
        ts_by_sym[sym].append(ts)

    # 构建桶级面板：每币每桶 → (24h涨幅, vr, fwd)
    rows_out: list[dict] = []
    for sym, tss in ts_by_sym.items():
        px = klines.get(sym)
        if not px:
            continue
        hours = sorted(px)
        idx = {d: i for i, d in enumerate(hours)}
        n = len(hours)
        rets = []
        for i in range(1, n):
            if px[hours[i - 1]] > 0:
                rets.append(abs(px[hours[i]] / px[hours[i - 1]] - 1.0))
        if rets and statistics.median(rets) < MIN_MED_ABS_RET:
            continue
        vm = vol_med.get(sym)
        if vm is None or vm <= 0:
            continue
        for ts in sorted(tss):
            if ts not in idx or (sym, ts) not in vol_sum:
                continue
            if idx[ts] + iv_h + max(HORIZONS[interval]) >= n:
                continue
            i_entry = idx[ts] + iv_h
            entry_px = px[hours[i_entry]]
            # 24h 涨幅（桶末相对 24h 前，用 1h K 线）
            i24 = idx[ts] - 23 if idx[ts] >= 23 else None
            if i24 is None or i24 < 0:
                continue
            px24 = px[hours[i24]]
            if entry_px <= 0 or px24 <= 0:
                continue
            ret24 = entry_px / px24 - 1.0
            vr = vol_sum[(sym, ts)] / vm
            fwd = {}
            for h in HORIZONS[interval]:
                fwd[h] = px[hours[i_entry + h]] / entry_px - 1.0
            rows_out.append({"sym": sym, "ret24": ret24, "vr": vr, "fwd": fwd})

    print(f"粒度={interval} | 面板 {len(rows_out)} 行 / {len({r['sym'] for r in rows_out})} 币")

    # 候选池 = 24h 涨幅 > 0
    pool = [r for r in rows_out if r["ret24"] > 0]
    neg = [r for r in rows_out if r["ret24"] <= 0]
    print(f"涨幅>0 候选池 {len(pool)} 行 | 涨幅<=0 对照 {len(neg)} 行\n")

    # 候选池内按 vr 分 5 档
    vrs = sorted(r["vr"] for r in pool)
    nq = len(vrs) // 5
    bounds = [vrs[i * nq] if i * nq < len(vrs) else vrs[-1] for i in range(1, 5)]
    buckets = {1: [], 2: [], 3: [], 4: [], 5: []}
    for r in pool:
        b = 5
        for i, bd in enumerate(bounds, start=1):
            if r["vr"] < bd:
                b = i
                break
        buckets[b].append(r)

    hs = HORIZONS[interval]
    print("候选池（24h涨幅>0）内按缩量程度分档：")
    print(f"  {'档':<8} {'vr区间':<18} {'n':>6} | " + " | ".join(f"H{h}" for h in hs))
    means_by_bucket = {}
    for b in (1, 2, 3, 4, 5):
        rows = buckets[b]
        if not rows:
            continue
        vr_lo = min(r["vr"] for r in rows)
        vr_hi = max(r["vr"] for r in rows)
        cells = []
        for h in hs:
            rs = [r["fwd"][h] for r in rows]
            m = statistics.mean(rs) * 100
            md = statistics.median(rs) * 100
            win = sum(1 for x in rs if x > 0) / len(rs) * 100
            cells.append(f"{m:+5.2f}/{md:+5.2f}/{win:3.0f}%")
        means_by_bucket[b] = statistics.mean([r["fwd"][hs[1]] for r in rows]) if hs else 0
        print(f"  Q{b} 最{'缩' if b==1 else ('放' if b==5 else '中')}"
              f"{'量' if b==1 else ('量' if b==5 else '')}  [{vr_lo:.2f},{vr_hi:.2f}]  "
              f"{len(rows):>6} | " + " | ".join(cells))

    # 基线
    def base_line(rows, tag):
        cells = []
        for h in hs:
            rs = [r["fwd"][h] for r in rows]
            if not rs:
                cells.append("--")
                continue
            m = statistics.mean(rs) * 100
            md = statistics.median(rs) * 100
            win = sum(1 for x in rs if x > 0) / len(rs) * 100
            cells.append(f"{m:+5.2f}/{md:+5.2f}/{win:3.0f}%")
        print(f"  {tag:<12} {'':18} {len(rows):>6} | " + " | ".join(cells))

    base_line(pool, "候选池全体")
    base_line(neg, "涨幅<=0 对照")

    # 单调性：Q1(最缩量) vs Q5(最放量) H8
    h_primary = hs[1] if len(hs) > 1 else hs[0]
    q1 = statistics.mean([r["fwd"][h_primary] for r in buckets[1]]) if buckets[1] else float("nan")
    q5 = statistics.mean([r["fwd"][h_primary] for r in buckets[5]]) if buckets[5] else float("nan")
    print(f"\nH{h_primary}: Q1最缩量 {q1*100:+.2f}% vs Q5最放量 {q5*100:+.2f}% "
          f"→ 差值 {(q1-q5)*100:+.2f}pp {'（缩量更优 ✓）' if q1 > q5 else '（放量更优 ✗）'}")
    print("\n完成。")


if __name__ == "__main__":
    main()
