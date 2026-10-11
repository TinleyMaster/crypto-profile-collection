#!/usr/bin/env python3
"""顶背离（bearish divergence）因子 × A/B 做多信号回测。

假设（用户观察）：上涨趋势中，价格创新高但 RSI / 成交量未创新高 → 后续大概率下跌。
若成立，则可作为做多信号前的**负向过滤器**（有背离 → 跳过该信号）。

背离定义（教科书「双峰对比」，在信号 bar 收盘时判定，无前视）：
  取窗口内两个「峰」：
    - 近端峰 = 最近 K 根内的最高价 bar（K = L//3）
    - 远端峰 = 更早 (L−K) 根内的最高价 bar
  价格创新高   : 近端峰价 > 远端峰价
  RSI 背离     : 价格创新高 且 近端峰处 RSI < 远端峰处 RSI
  成交量背离   : 价格创新高 且 近端峰处 quote_vol < 远端峰处 quote_vol
  组合背离     : RSI 背离 且 成交量背离
（信号 bar 本身是放量大阳，通常就是近端峰，故此定义贴合「价格新高但动能不足」。）

口径：
  - 事件：放量大阳（chg1h≥3% & vr≥2），缓存 trade_params_events.csv
  - 窗口：L ∈ {24,48,72} 根 1h；RSI 用 Wilder 14 周期
  - 出场：A/B 用 TRAIL3；成本 0.3%（与 backtest_anov.py 一致）
  - 负向过滤期望：有背离子集期望 < 全池；无背离子集 ≥ 全池

用法：python bin/backtest_divergence.py [--start 2026-01-01]
"""
from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from backtest_trade_params import _vec_rows, load_events  # noqa: E402
from crypto_research.config import get_settings  # noqa: E402

TR = 0.03
COST = 0.003
MIN_N = 30
L_LIST = [24, 48, 72]
RSI_N = 14
WARMUP_H = 30       # 额外多拉 warmup（RSI 14 + 峰值窗口 72）

SQL_K = """
SELECT symbol,
       array_agg(open_time ORDER BY open_time),
       array_agg(high_px   ORDER BY open_time),
       array_agg(close_px  ORDER BY open_time),
       array_agg(quote_vol ORDER BY open_time)
FROM biz.asset_klines
WHERE interval='1h' AND high_px > 0 AND close_px > 0 AND quote_vol > 0
  AND open_time >= %s AND symbol = ANY(%s)
GROUP BY symbol
"""


def _rsi(close: np.ndarray, n: int = RSI_N) -> np.ndarray:
    """Wilder RSI（标准 14 周期，EMA alpha=1/n）。"""
    s = pd.Series(close)
    delta = s.diff()
    up = delta.clip(lower=0.0)
    down = -delta.clip(upper=0.0)
    roll_up = up.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    roll_down = down.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = roll_up / roll_down.replace(0.0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    rsi = rsi.where(roll_down != 0, 100.0)          # 全涨 → 100
    return rsi.to_numpy(dtype=float)


def _load_klines(syms, start) -> dict[str, dict]:
    out: dict[str, dict] = {}
    with psycopg.connect(get_settings(require_database=True).database_url,
                         connect_timeout=20) as conn:
        with conn.cursor() as cur:
            for k in range(0, len(syms), 200):
                cur.execute(SQL_K, (start, syms[k:k + 200]))
                for sym, ts, hs, cs, vs in cur.fetchall():
                    close = np.asarray([float(x) for x in cs])
                    out[sym] = {
                        "idx": pd.to_datetime(list(ts)),
                        "high": np.asarray([float(x) for x in hs]),
                        "vol": np.asarray([float(x) for x in vs]),
                        "rsi": _rsi(close),
                    }
    return out


def _div_flags(d: dict, eo, L: int) -> tuple[bool, bool, bool] | None:
    """在信号 bar（eo）收盘时判定三类背离。返回 (rsi_div, vol_div, combo_div)。"""
    idx = d["idx"]
    j = idx.searchsorted(eo)
    if j >= len(idx) or idx[j] != eo or j - L + 1 < 0:
        return None
    K = max(4, L // 3)
    high, vol, rsi = d["high"], d["vol"], d["rsi"]
    rs = slice(j - K + 1, j + 1)
    ps = slice(j - L + 1, j - K + 1)
    rh, ph = high[rs], high[ps]
    if rh.size == 0 or ph.size == 0:
        return None
    ir = int(np.argmax(rh))
    ip = int(np.argmax(ph))
    rp_price, pp_price = float(rh[ir]), float(ph[ip])
    if not rp_price > pp_price:                     # 价格未创新高 → 无背离
        return (False, False, False)
    rp_rsi = float(rsi[j - K + 1 + ir])
    pp_rsi = float(rsi[j - L + 1 + ip])
    rp_vol = float(vol[j - K + 1 + ir])
    pp_vol = float(vol[j - L + 1 + ip])
    rsi_div = bool(rp_rsi == rp_rsi and pp_rsi == pp_rsi and rp_rsi < pp_rsi)
    vol_div = bool(rp_vol < pp_vol)
    return (rsi_div, vol_div, bool(rsi_div and vol_div))


def _stats(ret):
    r = ret[~np.isnan(ret)]
    if len(r) < MIN_N:
        return None
    rn = r - COST
    win = rn > 0
    gw, gl = float(rn[win].sum()), abs(float(rn[~win].sum())) if (~win).any() else 0.0
    return {"n": len(r), "win": float((rn > 0).mean()), "mean": float(rn.mean()),
            "median": float(np.median(rn)), "pf": gw / gl if gl > 0 else float("inf")}


def _line(tag, st, base_mean=None):
    if st is None:
        print(f"    {tag:<22} n<{MIN_N}（样本不足）")
        return
    lift = "" if base_mean is None else f" ↑{(st['mean']-base_mean)*100:+.2f}pp"
    print(f"    {tag:<22} n={st['n']:>5,} 胜={st['win']*100:>5.1f}% "
          f"期望={st['mean']*100:>+6.2f}% 中位={st['median']*100:>+6.2f}% "
          f"PF={st['pf']:>5.1f}{lift}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2023-01-01",
                    help="事件起始日（默认全量；快速版可给更晚日期，如 2026-01-01）")
    args = ap.parse_args()
    start = args.start
    rows = load_events()
    rows = [r for r in rows if r["eo"] >= pd.Timestamp(start, tz="UTC")]
    print(f"事件子集: {len(rows):,}（{start} 起）")
    arr = _vec_rows(rows)
    chg = arr["chg24"]
    ret_ab = arr["ph_tr3"] * (1 - TR) / arr["entry"] - 1

    syms = sorted({r["symbol"] for r in rows})
    kstart = (pd.Timestamp(start, tz="UTC") - timedelta(hours=WARMUP_H)).to_pydatetime()
    print(f"加载 1h K 线（{len(syms)} 币，{kstart:%Y-%m-%d} 起）...", flush=True)
    data = _load_klines(syms, kstart)

    print("=" * 112)
    print("顶背离 × A/B 做多信号（TRAIL3 出场，含 0.3% 成本）")
    print("=" * 112)
    for L in L_LIST:
        flags = {v: np.zeros(len(rows), dtype=bool) for v in ("rsi", "vol", "combo")}
        matched = np.zeros(len(rows), dtype=bool)
        for i, r in enumerate(rows):
            d = data.get(r["symbol"])
            if d is None:
                continue
            f = _div_flags(d, r["eo"], L)
            if f is None:
                continue
            matched[i] = True
            flags["rsi"][i], flags["vol"][i], flags["combo"][i] = f
        print(f"\n■ 窗口 L={L}（近端峰 K={max(4, L//3)} 根）｜可关联 {int(matched.sum()):,}/{len(rows):,}"
              f" | 背离占比 RSI {flags['rsi'][matched].mean()*100:.1f}% "
              f"VOL {flags['vol'][matched].mean()*100:.1f}% "
              f"组合 {flags['combo'][matched].mean()*100:.1f}%")
        for sig, m in (("A 做多(≥50%)", chg >= 0.50),
                       ("B 做多(20~50%)", (0.20 <= chg) & (chg < 0.50))):
            mm = m & matched
            base = _stats(np.where(mm, ret_ab, np.nan))
            if base is None:
                print(f"  ◆ {sig}: 全池样本不足")
                continue
            print(f"  ◆ {sig}:")
            _line("全池基线", base)
            for v in ("rsi", "vol", "combo"):
                have = mm & flags[v]
                _line(f"有背离({v})", _stats(np.where(have, ret_ab, np.nan)), base["mean"])
                _line(f"无背离({v})", _stats(np.where(mm & ~flags[v], ret_ab, np.nan)), base["mean"])
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())