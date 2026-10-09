#!/usr/bin/env python3
"""A 信号 + MomentumStrength（动量强度）策略完整量化指标。

策略定义（2026-10-07）：
  - 信号 A：放量大阳（chg1h≥3% & vr≥2）且滚动24h涨幅≥50%
  - 动量强度过滤：强度 = N日总涨跌幅×(2×上涨天数占比-1)，事件取**前一交易日**（无前视）；
    仅保留 强度 ≥ 阈值（主口径 30%，n=20 天）
  - 出场：TRAIL 3%（出场价=触发峰值×0.97），硬止损 SL10，持仓上限 N=24h
  - 成本：0.3% 往返；固定仓位 w=30%（线性净值）；rf=0；年化按365天

指标口径（对齐 backtest_a_metrics.py）：累计/年化(=CAGR)/胜率/PF/盈亏比、最大回撤/
波动率/下行波动/VaR/CVaR、夏普/索提诺/卡玛、平均持仓/单笔最大亏损/连续亏损、
分年度/样本外/BTC相关性/IR、强度阈值×周期敏感性。

用法：
    python bin/backtest_a_ms_metrics.py                 # A+强度≥30%(n=20) 完整指标
    python bin/backtest_a_ms_metrics.py --ms 0.20 --n 10
    python bin/backtest_a_ms_metrics.py --ms 0          # 阈值=0（对比）
    python bin/backtest_a_ms_metrics.py --ms -1         # -1=纯A不过滤（复现 a_metrics）
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from backtest_trade_params import CACHE, load_events  # noqa: E402
from crypto_research.config import get_settings  # noqa: E402

TR = 0.03
SL = 0.10
N_HOLD = 24
COST = 0.003
W = 0.30
RF = 0.0

SQL_DAILY = """
SELECT DISTINCT ON (symbol, DATE(open_time)) symbol, DATE(open_time) AS d, close_px
FROM biz.asset_klines
WHERE interval = '1h' AND close_px > 0
  AND open_time >= '2022-12-01' AND symbol = ANY(%s)
ORDER BY symbol, DATE(open_time), open_time DESC
"""

SQL_BTC = """
WITH b AS (
    SELECT DATE(open_time) AS d, close_px,
           ROW_NUMBER() OVER (PARTITION BY DATE(open_time) ORDER BY open_time DESC) AS rn
    FROM biz.asset_klines
    WHERE symbol = 'BTCUSDT' AND interval = '1h' AND close_px > 0
)
SELECT d, close_px FROM b WHERE rn = 1 ORDER BY d
"""

HP_COLS = [("hp2", 2), ("hp3", 3), ("hp5", 5), ("hp8", 8), ("hp10", 10),
           ("hp20", 20), ("hp30", 30), ("hp50", 50)]


def load_momentum_strength(symbols, batch=50) -> dict[str, pd.DataFrame]:
    """拉日线并计算 ms10/20/30 动量强度（索引=date）。分批查询规避 PG 慢计划。"""
    syms = list(symbols)
    dailies = []
    with psycopg.connect(get_settings(require_database=True).database_url,
                         connect_timeout=20) as conn:
        with conn.cursor() as cur:
            for k in range(0, len(syms), batch):
                chunk = syms[k:k + batch]
                cur.execute(SQL_DAILY, (chunk,))
                rows = cur.fetchall()
                dailies.extend(rows)
                print(f"  日线批次 {k//batch + 1}/{(len(syms) - 1)//batch + 1} "
                      f"({len(chunk)} 合约, 累计 {len(dailies):,} 行)", flush=True)
    per: dict[str, list] = defaultdict(list)
    for sym, d, close in dailies:
        per[sym].append((d, float(close)))
    out: dict[str, pd.DataFrame] = {}
    for sym, pairs in per.items():
        pairs.sort()
        df = pd.DataFrame(pairs, columns=["d", "close"]).set_index("d")
        dr = df["close"].pct_change()
        for n in (10, 20, 30):
            tr = df["close"] / df["close"].shift(n) - 1
            up = dr.rolling(n, min_periods=1).apply(
                lambda s: float((s > 0).mean()) if len(s) else 0.5, raw=True
            ).fillna(0.5)
            df[f"ms{n}"] = (tr * (2 * up - 1)).fillna(0)
        out[sym] = df
    return out


def max_drawdown(eq):
    peak = np.maximum.accumulate(eq)
    dd = eq / peak - 1
    i_end = int(np.argmin(dd))
    i_start = int(np.argmax(eq[:i_end + 1])) if i_end > 0 else 0
    return float(dd[i_end]), i_start, i_end


def _pct(x, nd=2):
    return f"{x * 100:.{nd}f}%"


def compute_metrics(evs, total_days) -> dict:
    """计算全部指标（不打印）。evs 含 gross/net/hold_h，按 eo 升序。"""
    net = np.array([e["net"] for e in evs])
    hold_h = np.array([e["hold_h"] for e in evs])

    eq = 1.0 + W * np.cumsum(net)
    eq_full = np.concatenate([[1.0], eq])
    cum = float(eq_full[-1] - 1)
    ann = (1 + cum) ** (365 / total_days) - 1

    day_net = defaultdict(float)
    for e in evs:
        day_net[e["eo"].date()] += e["net"]
    dates = sorted(day_net)
    ret_by_day = {d: W * v for d, v in day_net.items()}
    cal = (dates[-1] - dates[0]).days + 1
    full = np.zeros(cal)
    d0 = dates[0]
    for i in range(cal):
        d = d0 + timedelta(days=i)
        if d in ret_by_day:
            full[i] = ret_by_day[d]
    dr = full

    vol = float(np.std(dr, ddof=1) * np.sqrt(365))
    down = dr[dr < 0]
    down_vol = float(np.std(down, ddof=1) * np.sqrt(365)) if len(down) > 1 else 0.0
    sharpe = ann / vol if vol > 0 else float("nan")
    sortino = ann / down_vol if down_vol > 0 else float("nan")
    mdd, _, mdd_end = max_drawdown(eq_full)
    calmar = ann / abs(mdd) if mdd < 0 else float("nan")

    recover_days = None
    peak_before = float(np.max(eq_full[:mdd_end + 1]))
    trough_d = evs[max(mdd_end - 1, 0)]["eo"].date()
    for i in range(mdd_end + 1, len(eq_full)):
        if eq_full[i] >= peak_before:
            recover_days = (evs[i - 1]["eo"].date() - trough_d).days
            break

    def var_cvar(p):
        q = np.percentile(dr, (1 - p) * 100)
        tail = dr[dr <= q]
        return float(q), float(tail.mean()) if len(tail) else float(q)

    var95, cvar95 = var_cvar(0.95)
    var99, cvar99 = var_cvar(0.99)

    win = net > 0
    n_w, n_l = int(win.sum()), int((~win).sum())
    gw, gl = float(net[win].sum()), abs(float(net[~win].sum())) if n_l else 0.0
    pf = gw / gl if gl > 0 else float("inf")
    payoff = (net[win].mean() / abs(net[~win].mean())) if n_l and n_w else float("inf")
    streak = max_s = 0
    for r in net:
        streak = streak + 1 if r < 0 else 0
        max_s = max(max_s, streak)

    yr, ny = defaultdict(float), defaultdict(int)
    for e in evs:
        y = e["eo"].year
        yr[y] += e["net"]
        ny[y] += 1

    with psycopg.connect(get_settings(require_database=True).database_url,
                         connect_timeout=20) as conn:
        with conn.cursor() as cur:
            cur.execute(SQL_BTC)
            btc_rows = cur.fetchall()
    bpx = [float(r[1]) for r in btc_rows]
    bret = {r[0]: bpx[i] / bpx[i - 1] - 1 for i, r in enumerate(btc_rows) if i > 0 and bpx[i - 1] > 0}
    common = sorted(set(ret_by_day) & set(bret))
    sd = np.array([ret_by_day[d] for d in common])
    bd = np.array([bret[d] for d in common])
    corr = float(np.corrcoef(sd, bd)[0, 1]) if len(common) > 2 else float("nan")
    ex = sd - bd
    te = float(np.std(ex, ddof=1) * np.sqrt(365)) if len(ex) > 2 else float("nan")
    btc_cum = float(np.prod(1 + bd) - 1)
    btc_ann = (1 + btc_cum) ** (365 / max(len(common), 1)) - 1
    ir = (ann - btc_ann) / te if te and te > 0 else float("nan")

    return {
        "n": len(evs), "n_win": n_w, "n_loss": n_l, "win": n_w / (n_w + n_l),
        "cum": cum, "ann": ann, "pf": pf, "payoff": payoff,
        "mdd": mdd, "recover_days": recover_days, "vol": vol, "down_vol": down_vol,
        "var95": var95, "cvar95": cvar95, "var99": var99, "cvar99": cvar99,
        "sharpe": sharpe, "sortino": sortino, "calmar": calmar,
        "hold_med": float(np.nanmedian(hold_h)), "hold_mean": float(np.nanmean(hold_h)),
        "worst": float(np.min(net)), "max_streak": max_s,
        "year_net": dict(yr), "year_n": dict(ny),
        "corr_btc": corr, "ir": ir, "btc_ann": btc_ann, "common_days": len(common),
    }


def print_full(m: dict, label: str):
    print("\n" + "=" * 82)
    print(label)
    print("=" * 82)
    print("\n【一、收益类】")
    print(f"  累计总收益      {_pct(m['cum'])}")
    print(f"  年化收益(=CAGR) {_pct(m['ann'])}")
    print(f"  胜率            {m['n_win']}/{m['n']} = {m['win']*100:.1f}%")
    print(f"  Profit Factor   {m['pf']:.2f}")
    print(f"  盈亏比(均盈/均亏) {m['payoff']:.2f}")
    print(f"  交易次数        {m['n']:,}")

    print("\n【二、风险类】")
    print(f"  最大回撤        {_pct(m['mdd'])}")
    print(f"  回撤修复天数    {m['recover_days'] if m['recover_days'] is not None else '未恢复'}")
    print(f"  年化波动率      {_pct(m['vol'])}")
    print(f"  下行波动率      {_pct(m['down_vol'])}")
    print(f"  单日VaR95/CVaR95 {_pct(m['var95'],3)}/{_pct(m['cvar95'],3)}")
    print(f"  单日VaR99/CVaR99 {_pct(m['var99'],3)}/{_pct(m['cvar99'],3)}")

    print("\n【三、风险调整收益】")
    print(f"  夏普 Sharpe     {m['sharpe']:.2f}")
    print(f"  索提诺 Sortino  {m['sortino']:.2f}")
    print(f"  卡玛 Calmar     {m['calmar']:.2f}")

    print("\n【四、交易与成本】")
    print(f"  平均持仓时长    {m['hold_med']:.0f}h（中位）/ {m['hold_mean']:.0f}h（均值）")
    print(f"  单笔最大亏损    {_pct(m['worst'])}")
    print(f"  连续亏损最大次数 {m['max_streak']} 笔")
    print(f"  成本假设        0.3% 往返；滑点未计")

    print("\n【五、稳健性】")
    for y in sorted(m["year_n"]):
        print(f"    {y}: n={m['year_n'][y]:>5,}  单笔合计 {_pct(m['year_net'][y])}   净值贡献 {_pct(W*m['year_net'][y])}")
    print(f"  与 BTC 日收益相关性 {m['corr_btc']:+.2f}（共同 {m['common_days']:,} 天）")
    print(f"  信息比率 IR(vs BTC) {m['ir']:.2f}（策略年化 {_pct(m['ann'])} vs BTC年化 {_pct(m['btc_ann'])}）")


def build_events(rows, arr, ms_map, n, ms_th):
    """过滤事件并计算 gross/net/hold_h。返回按 eo 排序的列表。"""
    out = []
    for i, r in enumerate(rows):
        if arr["chg24"][i] < 0.50:
            continue
        if ms_th is not None:
            df = ms_map.get(r["symbol"])
            if df is None:
                continue
            d = r["eo"].date() - timedelta(days=1)
            if d not in df.index:
                continue
            v = float(df.loc[d, f"ms{n}"])
            if v != v or v < ms_th:
                continue
        ph = r["ph_tr3"]
        if not ph:
            continue
        gross = float(ph) * (1 - TR) / float(r["entry"]) - 1
        hold_h = np.nan
        for col, h in HP_COLS:
            v = r.get(col)
            if v and float(v) >= float(ph):
                hold_h = h
                break
        out.append({"symbol": r["symbol"], "eo": r["eo"], "gross": gross,
                    "net": gross - COST, "hold_h": hold_h})
    out.sort(key=lambda e: e["eo"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ms", type=float, default=0.30, help="动量强度阈值（默认0.30；-1=不过滤）")
    ap.add_argument("--n", type=int, default=20, choices=[10, 20, 30], help="强度周期（默认20）")
    args = ap.parse_args()
    ms_th = None if args.ms < 0 else args.ms

    rows = load_events()
    arr = {k: np.asarray([np.nan if r.get(k) is None else float(r[k]) for r in rows], float)
           for k in ("entry", "chg24", "ph_tr3")}
    syms = sorted({r["symbol"] for r in rows})
    print(f"事件缓存 {len(rows):,}  合约 {len(syms):,}")

    ms_map = None
    if ms_th is not None:
        print("加载日线 + 动量强度...", flush=True)
        ms_map = load_momentum_strength(syms)

    # ── 参数清单 ─────────────────────────────────────────────
    print("\n" + "=" * 82)
    print("策略参数清单（A 信号 + 动量强度）")
    print("=" * 82)
    print("【信号参数】")
    print("  涨幅榜档位  滚动24h涨幅 ≥ 50%（A 档）")
    print("  事件强度    chg1h ≥ 3% 且 vr ≥ 2（放量大阳）")
    print("【动量强度因子】")
    print(f"  周期 n       {args.n} 天（日线）")
    print(f"  阈值         强度 ≥ {ms_th if ms_th is not None else '（不过滤）'}")
    print("  定义         强度 = N日总涨跌幅 × (2×上涨天数占比−1)；取事件前一交易日（无前视）")
    print("【交易参数】")
    print(f"  止盈         TRAIL 跟踪止盈 {TR:.0%}（峰值回撤）")
    print(f"  止损         SL 硬止损 {SL:.0%}")
    print(f"  持仓上限     N = {N_HOLD}h")
    print(f"  成本         {COST:.1%} 往返（含手续费，滑点未计）")
    print(f"  仓位         w = {W:.0%} 固定名义（线性净值，非复利）")

    # ── 主口径事件 ───────────────────────────────────────────
    evs = build_events(rows, arr, ms_map, args.n, ms_th)
    if not evs:
        print("无事件！")
        return 1
    t0 = evs[0]["eo"]
    t1 = evs[-1]["eo"]
    total_days = max((t1 - t0).days, 1)

    label = f"A 信号 + 动量强度(≥{ms_th if ms_th is not None else '-'}) n={args.n}"
    m = compute_metrics(evs, total_days)
    print_full(m, label)

    # ── 敏感性：强度阈值 × n ─────────────────────────────────
    print("\n" + "=" * 82)
    print("敏感性：强度阈值 × 周期 n（累计/年化/回撤/夏普/索提诺/卡玛/PF）")
    print("=" * 82)
    print(f"  {'阈值':>8}{'n':>4}{'事件':>7}{'累计%':>10}{'年化%':>9}{'回撤%':>8}"
          f"{'夏普':>6}{'索提诺':>7}{'卡玛':>7}{'PF':>6}")
    for n in (10, 20, 30):
        for th in (0.0, 0.10, 0.20, 0.30, 0.40):
            sub = build_events(rows, arr, ms_map, n, th)
            if len(sub) < 20:
                continue
            mm = compute_metrics(sub, total_days)
            print(f"  {f'≥{th:.0%}':>8}{n:>4}{mm['n']:>7,}{mm['cum']*100:>10.1f}{mm['ann']*100:>9.1f}"
                  f"{mm['mdd']*100:>8.1f}{mm['sharpe']:>6.2f}{mm['sortino']:>7.1f}"
                  f"{mm['calmar']:>7.1f}{mm['pf']:>6.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
