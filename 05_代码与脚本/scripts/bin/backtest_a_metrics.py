#!/usr/bin/env python3
"""A 信号（SHORT_LONG ≥50% 做多）策略完整量化指标。

口径（2026-10-07）：
  - 事件：放量大阳 & 滚动24h涨幅≥50%（缓存 trade_params_events.csv，2023~2026，n=3,028）
  - 出场：TRAIL 3% 跟踪止盈（出场价=触发时峰值×0.97），与实盘一致
  - 成本：0.3% 往返；滑点未计（加密永续小单滑点可忽略，见成本敏感性）
  - 仓位：**固定名义**模型（实盘为固定名义下注，非净值复利）——每笔投入账户初始净值的
    30%（w=0.30）作为固定仓位，净值 = 1 + w×累计净收益（线性，不复利）
  - 无风险利率 rf=0；年化按 365 天；杠杆不计（收益%与回撤%等比放大，比率类指标不变）

用法：python bin/backtest_a_metrics.py
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import psycopg

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402

CACHE = SCRIPT_DIR.parent / "data" / "trade_params_events.csv"
TR = 0.03
COST = 0.003
W = 0.30            # 固定仓位：每笔投入初始净值比例（主口径）
RF = 0.0            # 无风险利率

HP_COLS = [("hp2", 2), ("hp3", 3), ("hp5", 5), ("hp8", 8), ("hp10", 10),
           ("hp20", 20), ("hp30", 30), ("hp50", 50)]

SQL_BTC = """
WITH b AS (
    SELECT DATE(open_time) AS d, close_px,
           ROW_NUMBER() OVER (PARTITION BY DATE(open_time) ORDER BY open_time DESC) AS rn
    FROM biz.asset_klines
    WHERE symbol = 'BTCUSDT' AND interval = '1h' AND close_px > 0
)
SELECT d, close_px FROM b WHERE rn = 1 ORDER BY d
"""


def max_drawdown(eq: np.ndarray) -> tuple[float, int, int]:
    peak = np.maximum.accumulate(eq)
    dd = eq / peak - 1
    i_end = int(np.argmin(dd))
    i_start = int(np.argmax(eq[:i_end + 1])) if i_end > 0 else 0
    return float(dd[i_end]), i_start, i_end


def _fmt_pct(x, nd=2):
    return f"{x * 100:.{nd}f}%"


def main() -> int:
    # ── 1. 读 A 信号事件 ─────────────────────────────────────
    evs = []
    with CACHE.open("r", encoding="utf-8") as fp:
        for r in csv.DictReader(fp):
            if float(r["chg24"]) >= 0.50:
                evs.append(r)
    evs.sort(key=lambda e: e["eo"])
    print(f"A 信号事件: {len(evs):,}  {CACHE.name}")

    # ── 2. 单笔收益（毛/净）与持仓小时 ──────────────────────
    gross = np.array([float(e["ph_tr3"]) * (1 - TR) / float(e["entry"]) - 1 for e in evs])
    net = gross - COST
    hold_h = np.zeros(len(evs))
    for i, e in enumerate(evs):
        ph = float(e["ph_tr3"])
        for col, h in HP_COLS:
            v = e.get(col)
            if v and float(v) >= ph:
                hold_h[i] = h
                break
        else:
            hold_h[i] = np.nan

    t0 = datetime.fromisoformat(evs[0]["eo"])
    t1 = datetime.fromisoformat(evs[-1]["eo"])
    total_days = max((t1 - t0).days, 1)

    # ── 3. 净值模拟（固定名义 w，线性） ──────────────────────
    eq = 1.0 + W * np.cumsum(net)          # 长度 = len(evs)，eq[-1]=期末净值
    eq_full = np.concatenate([[1.0], eq])  # 含初始点

    cum_ret = float(eq_full[-1] - 1)
    ann_ret = (1 + cum_ret) ** (365 / total_days) - 1

    # 日收益（按 UTC 日期聚合净收益 × w）
    day_net = defaultdict(float)
    for i, e in enumerate(evs):
        day_net[datetime.fromisoformat(e["eo"]).date()] += net[i]
    dates = sorted(day_net)
    ret_by_day = {d: W * v for d, v in day_net.items()}

    cal_days = (dates[-1] - dates[0]).days + 1
    full_daily = np.zeros(cal_days)
    day0 = dates[0]
    for i in range(cal_days):
        d = day0 + timedelta(days=i)
        if d in ret_by_day:
            full_daily[i] = ret_by_day[d]
    dr = full_daily

    vol = float(np.std(dr, ddof=1) * np.sqrt(365))
    down = dr[dr < 0]
    down_vol = float(np.std(down, ddof=1) * np.sqrt(365)) if len(down) > 1 else 0.0
    sharpe = (ann_ret - RF) / vol if vol > 0 else float("nan")
    sortino = (ann_ret - RF) / down_vol if down_vol > 0 else float("nan")
    mdd, mdd_i, mdd_end = max_drawdown(eq_full)
    calmar = ann_ret / abs(mdd) if mdd < 0 else float("nan")

    # 回撤修复天数（从谷底对应日期到再创新高日期）
    recover_days = None
    peak_before = float(np.max(eq_full[:mdd_end + 1]))
    trough_d = datetime.fromisoformat(evs[max(mdd_end - 1, 0)]["eo"]).date()
    for i in range(mdd_end + 1, len(eq_full)):
        if eq_full[i] >= peak_before:
            recover_d = datetime.fromisoformat(evs[i - 1]["eo"]).date()
            recover_days = (recover_d - trough_d).days
            break

    def var_cvar(p):
        q = np.percentile(dr, (1 - p) * 100)
        tail = dr[dr <= q]
        cvar = float(tail.mean()) if len(tail) else float(q)
        return float(q), cvar

    var95, cvar95 = var_cvar(0.95)
    var99, cvar99 = var_cvar(0.99)

    win = net > 0
    n_w, n_l = int(win.sum()), int((~win).sum())
    gw, gl = float(net[win].sum()), abs(float(net[~win].sum()))
    pf = gw / gl if gl > 0 else float("inf")
    payoff = (net[win].mean() / abs(net[~win].mean())) if n_l and n_w else float("inf")

    streak = max_s = 0
    for r in net:
        streak = streak + 1 if r < 0 else 0
        max_s = max(max_s, streak)

    yr_net, n_by_year = defaultdict(float), defaultdict(int)
    for i, e in enumerate(evs):
        y = datetime.fromisoformat(e["eo"]).year
        yr_net[y] += net[i]
        n_by_year[y] += 1

    # BTC 基准 → 相关性 / IR
    with psycopg.connect(get_settings(require_database=True).database_url,
                         connect_timeout=20) as conn:
        with conn.cursor() as cur:
            cur.execute(SQL_BTC)
            rows = cur.fetchall()
    px = [float(r[1]) for r in rows]
    btc_ret = {r[0]: px[i] / px[i - 1] - 1 for i, r in enumerate(rows) if i > 0 and px[i - 1] > 0}
    common = sorted(set(ret_by_day) & set(btc_ret))
    strat_d = np.array([ret_by_day[d] for d in common])
    btc_d = np.array([btc_ret[d] for d in common])
    corr = float(np.corrcoef(strat_d, btc_d)[0, 1]) if len(common) > 2 else float("nan")
    ex = strat_d - btc_d
    te = float(np.std(ex, ddof=1) * np.sqrt(365)) if len(ex) > 2 else float("nan")
    btc_cum = float(np.prod(1 + btc_d) - 1)
    btc_ann = (1 + btc_cum) ** (365 / max(len(common), 1)) - 1
    ir = (ann_ret - btc_ann) / te if te and te > 0 else float("nan")

    # ── 输出 ─────────────────────────────────────────────────
    print("\n" + "=" * 78)
    print("A 信号策略指标（TRAIL3 · 成本0.3% · 固定仓位30% · 2023-01~2026-10）")
    print("=" * 78)

    print("\n【一、收益类】")
    print(f"  累计总收益        {_fmt_pct(cum_ret)}")
    print(f"  年化收益率(365d)  {_fmt_pct(ann_ret)}")
    print(f"  胜率              {n_w}/{n_w + n_l} = {n_w / (n_w + n_l) * 100:.1f}%")
    print(f"  Profit Factor     {pf:.2f}")
    print(f"  盈亏比(均盈/均亏) {payoff:.2f}")
    print(f"  交易次数          {len(evs):,}（日均 {len(evs) / total_days:.1f}，周期 {total_days} 天）")

    print("\n【二、风险类】")
    print(f"  最大回撤          {_fmt_pct(mdd)}")
    print(f"  回撤修复天数      {recover_days if recover_days is not None else '未恢复'}")
    print(f"  年化波动率        {_fmt_pct(vol)}")
    print(f"  下行波动率        {_fmt_pct(down_vol)}")
    print(f"  单日VaR95 / CVaR95 {_fmt_pct(var95,3)} / {_fmt_pct(cvar95,3)}")
    print(f"  单日VaR99 / CVaR99 {_fmt_pct(var99,3)} / {_fmt_pct(cvar99,3)}")

    print("\n【三、风险调整收益】")
    print(f"  夏普比率 Sharpe    {sharpe:.2f}")
    print(f"  索提诺 Sortino     {sortino:.2f}")
    print(f"  卡玛 Calmar        {calmar:.2f}")

    print("\n【四、交易与成本】")
    print(f"  平均持仓时长      {np.nanmedian(hold_h):.0f}h（中位），均值 {np.nanmean(hold_h):.0f}h")
    print(f"  单笔最大亏损      {_fmt_pct(float(np.min(net)))}")
    print(f"  连续亏损最大次数  {max_s} 笔")
    print(f"  成本假设          0.3% 往返（已含在净收益）")

    print("\n【五、稳健性】")
    print(f"  分年度（净收益单利 / 占净值30%贡献）：")
    for y in sorted(n_by_year):
        print(f"    {y}: n={n_by_year[y]:>5,}  单笔合计 {_fmt_pct(yr_net[y])}   净值贡献 {_fmt_pct(W * yr_net[y])}")
    print(f"  与 BTC 日收益相关性  {corr:+.2f}（共同 {len(common):,} 天）")
    print(f"  信息比率 IR(vs BTC)  {ir:.2f}（策略年化 {_fmt_pct(ann_ret)} vs BTC年化 {_fmt_pct(btc_ann)}）")
    print(f"  参数敏感性：TR 3/5/8/10/15% 期望 +5.61/+4.21/+3.01/+2.55/+1.63%"
          f"（backtest_exit_profile --sensitivity）")

    print("\n【仓位敏感性（固定名义 w，净值线性）】")
    for w in (0.10, 0.20, 0.30, 0.50):
        eqw = 1.0 + w * np.cumsum(net)
        eqw_full = np.concatenate([[1.0], eqw])
        cw = float(eqw_full[-1] - 1)
        aw = (1 + cw) ** (365 / total_days) - 1
        mw, _, _ = max_drawdown(eqw_full)
        cw_r = aw / abs(mw) if mw < 0 else float("nan")
        print(f"    w={w:.0%}: 累计 {_fmt_pct(cw)} 年化 {_fmt_pct(aw)} 最大回撤 {_fmt_pct(mw)} 卡玛 {cw_r:.2f}")

    cut = datetime(2026, 5, 27, tzinfo=timezone.utc)
    recent = [net[i] for i, e in enumerate(evs) if datetime.fromisoformat(e["eo"]) >= cut]
    older = [net[i] for i, e in enumerate(evs) if datetime.fromisoformat(e["eo"]) < cut]
    print(f"\n  样本分段（净收益）：")
    print(f"    2023-01~2026-05: n={len(older):,} 期望 {np.mean(older) * 100:+.2f}% 胜率 {(np.array(older) > 0).mean() * 100:.1f}%")
    print(f"    2026-05~10(近4.5月): n={len(recent):,} 期望 {np.mean(recent) * 100:+.2f}% 胜率 {(np.array(recent) > 0).mean() * 100:.1f}%")

    return 0


if __name__ == "__main__":
    sys.exit(main())
