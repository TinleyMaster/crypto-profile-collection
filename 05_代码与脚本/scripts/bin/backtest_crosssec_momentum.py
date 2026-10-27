# -*- coding: utf-8 -*-
"""L0-B 动量腿回测：90d 相对强度能否预测未来 30d 超额收益（vs BTC）

为什么单独回测这一项
    biz.asset_flow_factor / asset_price_elasticity 只有 11 天快照（10-16~10-26），
    无法回测「资金流」。唯一有长历史的原始数据是 biz.asset_klines（1d，530 币）。
    所以本脚本只验证**价格动量**这一条腿，资金流腿不得声称已验证。

设计（先定规则再看结果，避免事后挑参数）
    · 调仓日：每月 1 日（2022-01 ~ 最后可用）
    · 信号：rs = 资产（回看 L 天）收益 − BTC（同窗口）收益，L 由 --lookback 指定
    · 检验：fwd30 = 资产未来 30d 收益 − BTC 未来 30d 收益
    · 对照组：同一天、同一批样本中 rs<=0 的标的（**不是跟 0 比**）
      —— 加密市场山寨整体长期跑输 BTC，跟 0 比会把「beta 劣势」误读成「信号有效」
    · 稳健性：均值做 1%/99% winsorize，同时报中位数与胜率
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, r"E:\瞎搞乱搞\web3\加密货币研究报告\05_代码与脚本\scripts")
from src.crypto_research.config import get_settings  # noqa: E402
from src.crypto_research.db.conn import get_connection  # noqa: E402

START = date(2021, 1, 1)
MIN_HISTORY_DAYS = 120
FWD = 30
LOOKBACK = int(next((a.split("=")[1] for a in sys.argv if a.startswith("--lookback=")), 90))

SQL = """
SELECT symbol, open_time::date AS d, close_px::float8 AS close
FROM biz.asset_klines
WHERE interval = '1d' AND open_time >= %s AND close_px > 0
"""


def main() -> int:
    url = get_settings(require_database=True).database_url
    with get_connection(url) as conn, conn.cursor() as cur:
        cur.execute(SQL, (START,))
        rows = cur.fetchall()
    print(f"载入 1d K 线 {len(rows):,} 行")

    df = pd.DataFrame(rows, columns=["symbol", "d", "close"])
    px = df.pivot_table(index="d", columns="symbol", values="close", aggfunc="last").sort_index()
    dates = list(px.index)
    if "BTCUSDT" not in px.columns:
        print("缺 BTCUSDT，无法算相对强度")
        return 1
    print(f"交易日 {len(dates)}（{dates[0]} → {dates[-1]}），标的 {px.shape[1] - 1} 个")

    # 有效起点：每币首个有价日，要求至少 MIN_HISTORY_DAYS 历史才算「已在场」
    first_seen = {c: px[c].first_valid_index() for c in px.columns}

    def pos_on_or_before(target: date) -> int:
        """最后一个 <= target 的交易日下标；无则 -1"""
        lo, hi, best = 0, len(dates) - 1, -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if dates[mid] <= target:
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        return best

    # 调仓日：每月 1 日
    months = pd.date_range(start="2022-01-01", end=str(dates[-1]), freq="MS")
    samples = []   # (date, symbol, rs, fwd_excess)
    skipped = 0

    for m in months:
        T = m.date()
        i_now, i_back, i_fwd = pos_on_or_before(T), pos_on_or_before(T - timedelta(days=LOOKBACK)), pos_on_or_before(T + timedelta(days=FWD))
        if i_now < 0 or i_back < 0 or i_fwd < 0:
            continue
        # 前向窗口必须够长，否则 30d 收益会缩水成 5d
        if (dates[i_fwd] - dates[i_now]).days < FWD - 3:
            skipped += 1
            continue

        b_now, b_back, b_fwd = px["BTCUSDT"].iloc[i_now], px["BTCUSDT"].iloc[i_back], px["BTCUSDT"].iloc[i_fwd]
        if not (b_now and b_back and b_fwd):
            continue
        btc_90 = b_now / b_back - 1
        btc_fwd = b_fwd / b_now - 1

        for sym in px.columns:
            if sym == "BTCUSDT":
                continue
            # 上市不足 MIN_HISTORY_DAYS 的一律剔除（新股 90d 收益失真最严重）
            fs = first_seen.get(sym)
            if fs is None or (T - fs).days < MIN_HISTORY_DAYS:
                continue
            p_now, p_back, p_fwd = px[sym].iat[i_now], px[sym].iat[i_back], px[sym].iat[i_fwd]
            if not (p_now and p_back and p_fwd) or p_back <= 0:
                continue
            a90 = p_now / p_back - 1
            afwd = p_fwd / p_now - 1
            samples.append((T, sym, a90 - btc_90, afwd - btc_fwd))

    s = pd.DataFrame(samples, columns=["T", "sym", "rs", "fwd_excess"])
    print(f"\n[回看 {LOOKBACK}d] 样本 {len(s):,} 条（{s['T'].nunique()} 个调仓日，{s['sym'].nunique()} 个标的）"
          f"｜前向窗口不足跳过 {skipped} 期")

    def wins(v):
        lo, hi = np.percentile(v, [1, 99])
        return np.clip(v, lo, hi)

    def report(name, g):
        if len(g) < 50:
            print(f"  {name:22s} n={len(g):6d}  样本太小，跳过")
            return
        e = g["fwd_excess"].to_numpy()
        print(f"  {name:22s} n={len(e):6d}  均值(缩尾){wins(e).mean()*100:+6.2f}%  "
              f"中位{np.median(e)*100:+6.2f}%  胜率{(e > 0).mean()*100:5.1f}%")

    print("\n=== 全样本基线（山寨整体 vs BTC）===")
    report("全部", s)

    print(f"\n=== 按相对强度分组（回看 {LOOKBACK}d）===")
    report("rs <= 0", s[s.rs <= 0])
    report("rs >  0", s[s.rs > 0])

    q = s.rs.quantile([0.2, 0.4, 0.6, 0.8]).to_list()
    print("\n=== 五分位（Q5 = 相对最强）===")
    labels = ["Q1 最弱", "Q2", "Q3", "Q4", "Q5 最强"]
    edges = [-np.inf, q[0], q[1], q[2], q[3], np.inf]
    s["qt"] = pd.cut(s.rs, bins=edges, labels=labels)
    for lb in labels:
        report(lb, s[s.qt == lb])

    print(f"\n=== 分年度稳定性：rs{LOOKBACK}d>0 组 vs 全样本 ===")
    for y, gy in s.groupby(s["T"].map(lambda d: d.year)):
        if len(gy) < 100:
            continue
        sig = gy[gy.rs > 0]["fwd_excess"]
        allx = gy["fwd_excess"]
        if len(sig) < 30:
            continue
        print(f"  {y}  n={len(gy):5d}  信号组{wins(sig.to_numpy()).mean()*100:+6.2f}%  "
              f"全样本{wins(allx.to_numpy()).mean()*100:+6.2f}%  "
              f"超额{wins(sig.to_numpy()).mean()*100 - wins(allx.to_numpy()).mean()*100:+6.2f}%")

    print("\n=== 幅度分档（rs 强度是否越高越好）===")
    for lo, hi, lb in [(0, 0.2, "0~20%"), (0.2, 0.5, "20~50%"), (0.5, 1.0, "50~100%"),
                       (1.0, 3.0, "100~300%"), (3.0, np.inf, ">300%")]:
        report(f"rs {lb}", s[(s.rs > lo) & (s.rs <= hi)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
