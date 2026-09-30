#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""流动性分层回测（只读分析）——回答「头部币的信号是否真的更好」。

背景（2026-09-30 口径更正）：
    此前用 CMC 全市场成交量分位当「前 20%」门槛，得 31.4 万 USD/天；而币安 U 本位
    永续全量的 p80 是 2102 万 USD/天——**差 67 倍**。根因是分母不同：CMC 全市场含
    上万种几乎无成交的币（p50 仅 1,650 USD/天），币安永续是有流动性的子集。
    本脚本按 PO 的「币安口径排名」做分层观察，判断「只做头部」是否值得。

触发逻辑与 backtest_scan_scenarios.scan_symbol **逐行一致**（PRICE_THR_1H /
VOL_RATIO_THR / LOOKBACK / HORIZONS / COST 同源 import，杜绝逻辑漂移），
仅额外记录 symbol 与 entry 前 7 天常态日均成交额 vol7d：

    vol7d[t] = (过去 168 根 1h 的 quote_vol 之和) / 7     # 单位 USD/天

为什么用 7 天常态而不是「当前 24h」：
    实测 21 个常态 <300 万的小币中有 13 个（61.9%）靠异动当日放量冲过 300 万门槛
    （CELO 45.5 万 → 8138 万，178.9x）；反向 218 个大币中 24 个（11%）在清淡日
    跌破门槛被误杀。看当前 24h 双向都错，故用 entry **之前**的 7 天常态（无前视）。

纪律（§14）：
    本脚本是**描述性分层观察**，不是阈值选型证据。45 天单一 regime 不具备验证能力
    （已在 P↑OI↑ 上坐实 train +6.4% / test −3.5% 的过拟合），本脚本所有结论一律
    provisional，只能用来「排除明显无差异」或「发现需要跨 regime 复验的线索」。
"""

from __future__ import annotations

import argparse
import bisect
import statistics
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import backtest_scan_scenarios as bss  # noqa: E402
from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

VOL7D_BARS = 168          # 7 天 × 24 根 1h
VOL24_BARS = 24           # 24 根 1h（旧口径，双口径对照用）
MIN_VOL7D_BARS = 168      # 不足 7 天历史的触发点跳过（不猜）
LIQ_MIN_USD = 3_000_000.0  # 现网门槛 X/ρ（300 / 1e-4），双口径对照的切点

# ---- 滑点模型（阶段 C，SCAN-LIQ-DEPTH-001）----
# 线性深度模型：±1% 单侧累计挂单 D（USD），吃 X 美元单的价格冲击 ≈
#   impact_bp = 100 * X / D
# 推导：深度在 ±1% 区间内近似均匀 ⇒ 消耗占比 f = X/D，价格位移 = f × 1%，
# 换算 bp = f × 1% × 10000 = 100·X/D。例：X=300、D=6 万 ⇒ 0.5bp/边。
# 局限（必须随表声明）：①均匀分布假设——真实盘口近密远疏，本模型是**下界偏乐观**；
# ②静态快照是 MM 毫秒补单后的瞬时存量，@4h 采样点与实际成交时刻可能错位；
# ③ X > 单侧深度时模型失效（冲击>100bp），一律打 unfillable 标记。
SLIP_RANGE_PCT = 1        # 用 ±1% 档（最保守的近端深度）
SLIP_UNFILLABLE_BP = 100  # 单边冲击超此值视为模型失效
SLIP_X_SENS = (300, 1000, 3000, 10000)  # X 敏感度档（USD）

# 绝对档（USD/天，常态日均）
ABS_EDGES = [
    (0, 1_000_000, "<100万"),
    (1_000_000, 3_000_000, "100-300万"),
    (3_000_000, 10_000_000, "300-1000万"),
    (10_000_000, 30_000_000, "1000-3000万"),
    (30_000_000, 100_000_000, "3000万-1亿"),
    (100_000_000, float("inf"), "≥1亿"),
]
# 池内横截面五分位（每日重算，对应「排名」语义）
QUINT_LABELS = ("Q1 最冷20%", "Q2", "Q3", "Q4", "Q5 最热20%")


def load_depth_history(conn, universe):
    """加载 ±1% 档 4h 深度快照：{sym: [(ts, bids_usd, asks_usd), ...] 按 ts 升序}。

    exchange_scope='binance'（与现网扫描对象一致）。无数据的币不在返回映射里。
    """
    out = defaultdict(list)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, ts, bids_usd, asks_usd FROM biz.orderbook_depth_history "
            "WHERE exchange_scope='binance' AND interval='4h' AND range_pct=%s "
            "AND symbol = ANY(%s) ORDER BY symbol, ts",
            (SLIP_RANGE_PCT, universe),
        )
        for sym, ts, bids, asks in cur.fetchall():
            out[sym].append((ts, float(bids or 0), float(asks or 0)))
    return out


def slip_roundtrip_bp(d_ask, d_bid, x_usd):
    """给定 ±1% 单侧深度，按线性模型算 X 美元单的往返滑点（bp）。

    买入口吃 asks、卖出口吃 bids，双边 impact 相加。
    返回 (slip_bp, flag)；flag: 'ok' | 'unfillable'（单边 X>D，模型失效）。
    """
    slip_ask = 100.0 * x_usd / d_ask
    slip_bid = 100.0 * x_usd / d_bid
    slip = slip_ask + slip_bid
    if slip_ask > SLIP_UNFILLABLE_BP or slip_bid > SLIP_UNFILLABLE_BP:
        return slip, "unfillable"
    return slip, "ok"


def depth_at(depth_list, entry_ts):
    """取 ts <= entry_ts 的最近 4h 快照（无前视）。返回 (d_ask, d_bid) 或 None。"""
    if not depth_list:
        return None
    idx = bisect.bisect_right(depth_list, (entry_ts, float("inf"), float("inf"))) - 1
    if idx < 0:
        return None
    _, bids, asks = depth_list[idx]
    if asks <= 0 or bids <= 0:
        return None
    return asks, bids


def scan_symbol_liq(sym, bars, oi_hours, trades, cost,
                    price_thr=bss.PRICE_THR_1H, vol_thr=bss.VOL_RATIO_THR,
                    depth_list=None, x_usd=0.0):
    """与 bss.scan_symbol 触发逻辑一致，额外落 symbol + vol7d（entry 之前，无前视）。"""
    n = len(bars)
    if n <= bss.LOOKBACK + 1 + max(bss.HORIZONS) + 1:
        return
    max_h = max(bss.HORIZONS)
    # vol7d 前缀和：cum[i] = bars[0..i-1] 的 vol 之和
    cum = [0.0] * (n + 1)
    for i, b in enumerate(bars):
        cum[i + 1] = cum[i] + float(b["vol"] or 0)

    for t in range(bss.LOOKBACK + 1, n - max_h - 1):
        bar = bars[t]
        prev = bars[t - 1]
        if not prev["close"] or not bar["close"]:
            continue
        chg = (bar["close"] - prev["close"]) / prev["close"] * 100
        vols = [b["vol"] for b in bars[t - bss.LOOKBACK:t]]
        vol_mean = sum(vols) / bss.LOOKBACK if vols else 0
        vol_ratio = bar["vol"] / vol_mean if vol_mean else 0.0
        if abs(chg) < price_thr or vol_ratio < vol_thr:
            continue
        # vol7d：entry 之前 7 天（用 bars[t] 及更早，杜绝前视）
        if t + 1 - VOL7D_BARS < 0:
            vol7d = None
        else:
            vol7d = (cum[t + 1] - cum[t + 1 - VOL7D_BARS]) / 7.0
        # vol24：entry 之前 24 根 1h（旧口径，同样无前视）——仅用于双口径对照
        vol24 = None if t + 1 - VOL24_BARS < 0 else (cum[t + 1] - cum[t + 1 - VOL24_BARS])
        direction = "up" if chg >= 0 else "down"

        h = bss.hour_key(bar["t"])
        oi_now = oi_hours.get(h)
        oi_prev = oi_hours.get(h - timedelta(hours=1))
        if oi_now is not None and oi_prev is not None and oi_prev != 0:
            oi_dir = "up" if oi_now > oi_prev else "down"
            scenario = f"P{direction}_OI{oi_dir}"
        else:
            scenario = "BASELINE_ONLY"

        entry = bars[t + 1]["open"]
        if not entry:
            continue
        day = bars[t + 1]["t"].date()
        entry_ts = bars[t + 1]["t"]
        d_pair = depth_at(depth_list, entry_ts) if depth_list is not None else None
        d_ask, d_bid = d_pair if d_pair else (None, None)
        for hz in bss.HORIZONS:
            exit_close = bars[t + hz]["close"]
            if not exit_close:
                continue
            ret_long = (exit_close - entry) / entry
            ret = ret_long if direction == "up" else -ret_long
            trades[scenario].append({
                "symbol": sym, "hz": hz, "day": day,
                "ret": ret - cost, "vol7d": vol7d, "vol24": vol24,
                "d_ask": d_ask, "d_bid": d_bid,
            })


def day_clustered_stats(rows):
    """日级聚类：先按天平均再算 t，避免同日多条信号互相放大显著性。"""
    if not rows:
        return None
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["day"]].append(r["ret"])
    day_avgs = [statistics.fmean(v) for v in by_day.values()]
    n_days = len(day_avgs)
    mean = statistics.fmean(day_avgs)
    sd = statistics.stdev(day_avgs) if n_days > 1 else 0.0
    t = mean / (sd / (n_days ** 0.5)) if sd > 0 else 0.0
    wins = sum(1 for r in rows if r["ret"] > 0)
    return {
        "n": len(rows), "n_days": n_days, "win_rate": wins / len(rows),
        "avg_ret": statistics.fmean([r["ret"] for r in rows]),
        "day_avg": mean, "day_t": t,
    }


def print_table(title, buckets, order):
    print(f"\n=== {title} ===")
    print(f"{'分组':<14}{'窗口h':>6}{'n':>7}{'天数':>6}{'胜率':>8}"
          f"{'净均收益%':>11}{'日均值%':>10}{'日t值':>8}")
    print("-" * 70)
    for key in order:
        rows = buckets.get(key)
        if not rows:
            continue
        for hz in bss.HORIZONS:
            sub = [r for r in rows if r["hz"] == hz]
            s = day_clustered_stats(sub)
            if not s or s["n"] < 5:
                continue
            print(f"{key:<14}{hz:>6}{s['n']:>7}{s['n_days']:>6}{s['win_rate']:>8.1%}"
                  f"{s['avg_ret'] * 100:>11.3f}{s['day_avg'] * 100:>10.3f}{s['day_t']:>8.2f}")


def print_dual(rows, thr, label):
    """双口径对照：同一批信号下，「当前 24h」与「7 天常态」分别过门槛，差在哪。

    回答的问题：把判据从 vol24 换成 vol7d，实际改变了什么？
      A 误杀纠正 —— vol24<thr 但 vol7d>=thr（旧口径剔、新口径留）
      B 一波流漏网 —— vol24>=thr 但 vol7d<thr（旧口径留、新口径剔）
      C 都剔 / D 都留 —— 两口径一致的部分
    仅看 24h 窗口（聚焦，避免三窗口把结论摊薄）。
    """
    quads = {"A 误杀纠正(旧剔→新留)": [], "B 一波流漏网(旧留→新剔)": [],
             "C 两口径都剔": [], "D 两口径都留": []}
    skipped = 0
    for r in rows:
        if r["hz"] != 24:
            continue
        if r["vol24"] is None or r["vol7d"] is None:
            skipped += 1
            continue
        lo24, lo7d = r["vol24"] < thr, r["vol7d"] < thr
        if lo24 and not lo7d:
            quads["A 误杀纠正(旧剔→新留)"].append(r)
        elif (not lo24) and lo7d:
            quads["B 一波流漏网(旧留→新剔)"].append(r)
        elif lo24:
            quads["C 两口径都剔"].append(r)
        else:
            quads["D 两口径都留"].append(r)

    print(f"\n=== ③ 双口径对照（门槛 {thr:,.0f} USD，24h 窗口）— {label} ===")
    print(f"{'象限':<22}{'n':>6}{'天数':>6}{'胜率':>8}{'净均收益%':>11}"
          f"{'日均值%':>10}{'日t值':>8}")
    print("-" * 72)
    for k in ("A 误杀纠正(旧剔→新留)", "B 一波流漏网(旧留→新剔)",
              "C 两口径都剔", "D 两口径都留"):
        s = day_clustered_stats(quads[k])
        if not s:
            print(f"{k:<22}{0:>6}")
            continue
        print(f"{k:<22}{s['n']:>6}{s['n_days']:>6}{s['win_rate']:>8.1%}"
              f"{s['avg_ret'] * 100:>11.3f}{s['day_avg'] * 100:>10.3f}{s['day_t']:>8.2f}")
    if skipped:
        print(f"（跳过 {skipped} 条：历史不足 24h/7d）")

    old_cut = quads["A 误杀纠正(旧剔→新留)"] + quads["C 两口径都剔"]
    new_cut = quads["B 一波流漏网(旧留→新剔)"] + quads["C 两口径都剔"]
    so, sn = day_clustered_stats(old_cut), day_clustered_stats(new_cut)
    print(f"{'— 旧口径剔除集 (A+C)':<22}{so['n']:>6}{so['n_days']:>6}{so['win_rate']:>8.1%}"
          f"{so['avg_ret'] * 100:>11.3f}{so['day_avg'] * 100:>10.3f}{so['day_t']:>8.2f}")
    print(f"{'— 新口径剔除集 (B+C)':<22}{sn['n']:>6}{sn['n_days']:>6}{sn['win_rate']:>8.1%}"
          f"{sn['avg_ret'] * 100:>11.3f}{sn['day_avg'] * 100:>10.3f}{sn['day_t']:>8.2f}")


def print_slip(rows, label, cost_bp=bss.COST * 1e4):
    """滑点透视：盘口深度线性模型下，X 美元单的往返滑点对收益的侵蚀。

    输出三块：
      ④a 默认 X 档的滑点分布 + 含/不含滑点的收益对照（24h 窗口）；
      ④b X 敏感度（同一批信号按不同仓位重算）；
      ④c 按深度五分位分层的滑点与净收益（谁在吃滑点）。
    覆盖外/缺快照的信号单独计数，不混入。
    """
    rows24 = [r for r in rows if r["hz"] == 24]
    have = [r for r in rows24 if r["d_ask"] is not None]
    missing = len(rows24) - len(have)
    print(f"\n=== ④ 盘口滑点透视（±1% 深度线性模型，24h 窗口）— {label} ===")
    print(f"覆盖 {len(have)}/{len(rows24)}（缺深度快照 {missing} 条，不入表）")
    if not have:
        return
    print(f"⚠️ 模型假设：深度在 ±1% 内均匀（下界偏乐观）；静态快照、MM 毫秒补单。")

    for x in SLIP_X_SENS:
        slips = []
        for r in have:
            s, flag = slip_roundtrip_bp(r["d_ask"], r["d_bid"], x)
            if flag == "ok":
                slips.append((s, r))
        vals = sorted(s for s, _ in slips)
        n = len(vals)
        q = lambda p: vals[min(n - 1, int(n * p))]
        unfill = len(have) - n
        # 收益对照（日聚类）：**同一 ok 子集**，含/不含滑点才可比
        base_rows = [dict(r) for _, r in slips]
        adj_rows = [dict(r, ret=r["ret"] - s / 1e4) for s, r in slips]
        base = day_clustered_stats(base_rows)
        adj = day_clustered_stats(adj_rows)
        print(f"\n-- X = {x:,} USDT（单边 unfillable {unfill} 条不计）--")
        print(f"   往返滑点 bp：p50={q(0.5):.2f}  p90={q(0.9):.2f}  "
              f"p99={q(0.99):.2f}  max={vals[-1]:.1f}")
        print(f"   滑点>手续费({cost_bp:.1f}bp) 的信号占比："
              f"{sum(1 for v in vals if v > cost_bp) / n:.1%}")
        if base and adj:
            print(f"   24h 净均收益：不含滑点 {base['avg_ret'] * 100:.3f}% → "
                  f"含滑点 {adj['avg_ret'] * 100:.3f}%"
                  f"（侵蚀 {base['avg_ret'] * 100 - adj['avg_ret'] * 100:.3f}pp）")

    # ④c 按深度五分位（横截面，同批信号按 entry 日重算排名）
    by_day = defaultdict(list)
    for r in have:
        by_day[r["day"]].append(r)
    dlab = ("D1 最薄20%", "D2", "D3", "D4", "D5 最厚20%")
    for day, rs in by_day.items():
        vals = sorted((r["d_ask"] + r["d_bid"]) / 2 for r in rs)
        m = len(vals)
        for r in rs:
            rank = bisect.bisect_right(vals, (r["d_ask"] + r["d_bid"]) / 2) / m
            r["dq"] = min(4, int(rank * 5))
    print(f"\n-- ④c 按 entry 日横截面深度五分位（X={SLIP_X_SENS[0]:,}）--")
    print(f"{'分组':<12}{'n':>7}{'天数':>6}{'滑点p50bp':>11}{'滑点p90bp':>11}"
          f"{'净均收益%(含滑点)':>16}{'日t':>8}")
    print("-" * 74)
    for i, lab in enumerate(dlab):
        grp = [r for r in have if r.get("dq") == i]
        if not grp:
            continue
        slips = sorted(slip_roundtrip_bp(r["d_ask"], r["d_bid"], SLIP_X_SENS[0])[0]
                       for r in grp)
        n = len(slips)
        p50, p90 = slips[n // 2], slips[min(n - 1, int(n * 0.9))]
        adj_rows = [dict(r, ret=r["ret"]
                         - slip_roundtrip_bp(r["d_ask"], r["d_bid"], SLIP_X_SENS[0])[0] / 1e4)
                    for r in grp]
        s = day_clustered_stats(adj_rows)
        if not s or s["n"] < 5:
            continue
        print(f"{lab:<12}{s['n']:>7}{s['n_days']:>6}{p50:>11.2f}{p90:>11.2f}"
              f"{s['avg_ret'] * 100:>16.3f}{s['day_t']:>8.2f}")


def main() -> int:
    ap = argparse.ArgumentParser(description="流动性分层回测（只读，描述性）")
    ap.add_argument("--symbols", type=int, default=0, help="只测前 N 个符号")
    ap.add_argument("--cost", type=float, default=bss.COST)
    ap.add_argument("--lookback-days", type=int, default=45)
    ap.add_argument("--scenario", type=str, default="Pup_OIup",
                    help="聚焦场景（默认 Pup_OIup）；ALL=全部场景合并")
    ap.add_argument("--dual", action="store_true",
                    help="追加双口径对照（vol24 vs vol7d 过同一门槛的四象限差异）")
    ap.add_argument("--slip", action="store_true",
                    help="追加盘口滑点透视（±1% 深度线性模型，需 biz.orderbook_depth_history）")
    args = ap.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol, COUNT(*) AS n FROM biz.asset_klines "
                "WHERE interval='1h' GROUP BY symbol HAVING COUNT(*) >= %s ORDER BY symbol",
                (bss.MIN_KLINES_BARS,),
            )
            universe = [r[0] for r in cur.fetchall()]
        if args.symbols:
            universe = universe[: args.symbols]
        print(f"[liq-layer] 符号宇宙 {len(universe)}，1h 窗口 {bss.HORIZONS}，"
              f"成本 {args.cost:.3f}，回看 {args.lookback_days} 天")
        klines = bss.load_klines(conn, universe, args.lookback_days)
        oi_hourly = bss.load_oi_hourly(conn, universe)
        depth_hist = load_depth_history(conn, universe)
        print(f"[liq-layer] K线 {sum(len(v) for v in klines.values())} 根 · "
              f"深度快照覆盖 {len(depth_hist)}/{len(universe)} 币")
    trades = defaultdict(list)
    for sym, bars in klines.items():
        scan_symbol_liq(sym, bars, oi_hourly.get(sym, {}), trades, args.cost,
                        depth_list=depth_hist.get(sym))

    if args.scenario == "ALL":
        rows_all = [r for recs in trades.values() for r in recs]
        scen_label = "全部场景合并"
    else:
        rows_all = list(trades.get(args.scenario, []))
        scen_label = args.scenario
    print(f"[liq-layer] 场景 {scen_label}：触发 {len(rows_all)} 条（含各 horizon）")

    # ---- 绝对档 ----
    abs_buckets = defaultdict(list)
    skipped = 0
    for r in rows_all:
        if r["vol7d"] is None:
            skipped += 1
            continue
        for lo, hi, label in ABS_EDGES:
            if lo <= r["vol7d"] < hi:
                abs_buckets[label].append(r)
                break
    if skipped:
        print(f"[liq-layer] 跳过 {skipped} 条（7天历史不足，不猜 vol7d）")
    print_table(f"① 按 7 天常态日均成交额分层（绝对档）— {scen_label}",
                abs_buckets, [lbl for _, _, lbl in ABS_EDGES])

    # ---- 池内横截面五分位（按 entry 当日重算排名）----
    by_day = defaultdict(list)
    for r in rows_all:
        if r["vol7d"] is not None:
            by_day[r["day"]].append(r)
    for day, rs in by_day.items():
        # 排序一次 + bisect 定位，O(m log m)（原 O(m²) 在 ALL 场景需 17min+）
        vals = sorted(x["vol7d"] for x in rs)
        m = len(vals)
        for r in rs:
            rank = bisect.bisect_right(vals, r["vol7d"]) / m
            r["q"] = min(4, int(rank * 5))
    q_buckets = defaultdict(list)
    for r in rows_all:
        if r.get("q") is not None:
            q_buckets[QUINT_LABELS[r["q"]]].append(r)
    print_table(f"② 按池内当日横截面五分位分层（排名语义）— {scen_label}",
                q_buckets, list(QUINT_LABELS))

    if args.dual:
        print_dual(rows_all, LIQ_MIN_USD, scen_label)
    if args.slip:
        print_slip(rows_all, scen_label)

    print("\n[liq-layer] ⚠️ 纪律：以上均为**描述性观察**（45 天单一 regime）。"
          "\n  差异只能作为「需跨 regime 复验的线索」，不能据此选阈值（§14.4）。"
          "\n  真正的执行门槛应由 X（单笔仓位）/ ρ（容忍参与度）先验推导，与收益无关。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
