#!/usr/bin/env python3
"""顶背离（bearish divergence）因子 × A/B 做多信号回测。

假设（用户口径）：**不是**长期趋势反转，而是上涨趋势中价格创新高、但对应 RSI/成交量
未创新高 → **短期大概率回落 → 该信号不是好入场点** → 过滤掉该信号，等下一次「无背离」
信号再进场。

因此本脚本在三层验证：
  ① 长期（现状）：TRAIL3 / 168h 出场的期望差 —— 有背离 vs 无背离（旧指标，会掩盖短期回落）
  ② 短期：入场后 6/12/24h 的原始收益 + 「先触 -2% 再触 +2%」(12h 内) 的比例 —— 直接验「短期下跌」
  ③ skip-and-reenter 模拟：遇到背离信号则跳过，在同币后续 W 小时内找**下一个无背离**信号
     替代入场；窗内无信号则空仓。对比「全量取」vs「跳过+取下一个」的总 PnL / 笔数 / 单笔均值。

背离定义（双峰对比法，在信号 bar 收盘时判定，无前视）：
  近端峰 = 最近 K 根内最高价 bar（K = L//3）；远端峰 = 更早 (L−K) 根内最高价 bar
  价格创新高 = 近端峰价 > 远端峰价
  RSI 背离   = 价格创新高 且 近端峰 RSI < 远端峰 RSI
  量背离     = 价格创新高 且 近端峰 quote_vol < 远端峰 quote_vol
  组合背离   = RSI 背离 且 量背离

用法：python bin/backtest_divergence.py [--start 2023-01-01]
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
SHORT_H = [6, 12, 24]
REENTER_W = [24, 48, 72]
RSI_N = 14
WARMUP_H = 30

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
    s = pd.Series(close)
    delta = s.diff()
    up = delta.clip(lower=0.0)
    down = -delta.clip(upper=0.0)
    roll_up = up.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    roll_down = down.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = roll_up / roll_down.replace(0.0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    rsi = rsi.where(roll_down != 0, 100.0)
    return rsi.to_numpy(dtype=float)


def _load_klines(syms, start) -> dict[str, dict]:
    out: dict[str, dict] = {}
    with psycopg.connect(get_settings(require_database=True).database_url,
                         connect_timeout=20) as conn:
        with conn.cursor() as cur:
            for k in range(0, len(syms), 200):
                cur.execute(SQL_K, (start, syms[k:k + 200]))
                for sym, ts, hs, cs, vs in cur.fetchall():
                    out[sym] = {
                        "idx": pd.to_datetime(list(ts)),
                        "high": np.asarray([float(x) for x in hs]),
                        "vol": np.asarray([float(x) for x in vs]),
                        "rsi": _rsi(np.asarray([float(x) for x in cs])),
                    }
    return out


def _div_flags(d: dict, eo, L: int) -> tuple[bool, bool, bool] | None:
    idx = d["idx"]
    j = idx.searchsorted(eo)
    if j >= len(idx) or idx[j] != eo or j - L + 1 < 0:
        return None
    K = max(4, L // 3)
    high, vol, rsi = d["high"], d["vol"], d["rsi"]
    rh, ph = high[j - K + 1:j + 1], high[j - L + 1:j - K + 1]
    if rh.size == 0 or ph.size == 0:
        return None
    ir, ip = int(np.argmax(rh)), int(np.argmax(ph))
    if not float(rh[ir]) > float(ph[ip]):
        return (False, False, False)
    rp_rsi, pp_rsi = float(rsi[j - K + 1 + ir]), float(rsi[j - L + 1 + ip])
    rp_vol, pp_vol = float(vol[j - K + 1 + ir]), float(vol[j - L + 1 + ip])
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
        print(f"    {tag:<20} n<{MIN_N}（样本不足）")
        return
    lift = "" if base_mean is None else f" ↑{(st['mean']-base_mean)*100:+.2f}pp"
    print(f"    {tag:<20} n={st['n']:>5,} 胜={st['win']*100:>5.1f}% "
          f"期望={st['mean']*100:>+6.2f}% 中位={st['median']*100:>+6.2f}% "
          f"PF={st['pf']:>5.1f}{lift}")


def _short_stats(arr, mask, n):
    """入场后 n 小时的原始收益（不扣成本，隔离价格路径）。"""
    r = np.where(mask & ~np.isnan(arr[f"c{n}"]),
                 arr[f"c{n}"] / arr["entry"] - 1, np.nan)
    r = r[~np.isnan(r)]
    if len(r) < MIN_N:
        return None
    return {"n": len(r), "mean": float(r.mean()), "win": float((r > 0).mean())}


def _short_line(tag, arr, mask):
    parts = []
    for n in SHORT_H:
        st = _short_stats(arr, mask, n)
        parts.append(f"{n}h {st['mean']*100:+.2f}%(胜{st['win']*100:.0f}%,n={st['n']:,})"
                     if st else f"{n}h n/a")
    print(f"    {tag:<20} " + " | ".join(parts))


def _adv_first(arr, mask, h=12):
    """入场 h 小时内「先触 -2% 再触 +2%」的比例（坏入场代理）。"""
    valid = ~np.isnan(arr["c24"])
    sl, hp = arr["sl2"], arr["hp2"]
    first_bad = ((sl <= h) & (np.isnan(hp) | (sl < hp)))
    m = mask & valid
    n = int(m.sum())
    if n < MIN_N:
        return None
    return int((first_bad & m).sum()), n, float((first_bad & m).sum() / n)


def _sum_rets(rets):
    r = np.asarray([x for x in rets if x == x], dtype=float)
    if len(r) == 0:
        return 0, 0.0
    return len(r), float(r.sum())


def _skip_reenter(order, eo, div, ret, W):
    """跳过背离信号，在同币后续 W 小时内找下一个无背离信号替代；窗内无则空仓。"""
    base, filt = [], []
    for idxs in order:
        for i in idxs:
            base.append(ret[i])
        k = 0
        while k < len(idxs):
            i = idxs[k]
            if not div[i]:
                filt.append(ret[i])
                k += 1
                continue
            j = k + 1
            repl = None
            while j < len(idxs):
                jj = idxs[j]
                if (eo[jj] - eo[i]) / 3600.0 > W:
                    break
                if not div[jj]:
                    repl = j
                    break
                j += 1
            if repl is not None:
                filt.append(ret[idxs[repl]])
                k = repl + 1
            else:
                k += 1
    return base, filt


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2023-01-01")
    args = ap.parse_args()
    start = args.start
    rows = load_events()
    rows = [r for r in rows if r["eo"] >= pd.Timestamp(start, tz="UTC")]
    print(f"事件子集: {len(rows):,}（{start} 起）")
    arr = _vec_rows(rows)
    chg = arr["chg24"]
    eo = arr["eo"]
    ret_ab = arr["ph_tr3"] * (1 - TR) / arr["entry"] - 1

    # 事件按期排序（skip-and-reenter 用）
    order_all = sorted(range(len(rows)), key=lambda i: (rows[i]["symbol"], eo[i]))
    order, cur, buf = [], None, []
    for i in order_all:
        s = rows[i]["symbol"]
        if s != cur:
            if buf:
                order.append(buf)
            cur, buf = s, []
        buf.append(i)
    if buf:
        order.append(buf)

    syms = sorted({r["symbol"] for r in rows})
    kstart = (pd.Timestamp(start, tz="UTC") - timedelta(hours=WARMUP_H)).to_pydatetime()
    print(f"加载 1h K 线（{len(syms)} 币，{kstart:%Y-%m-%d} 起）...", flush=True)
    data = _load_klines(syms, kstart)

    sig_masks = {"A 做多(≥50%)": chg >= 0.50,
                 "B 做多(20~50%)": (0.20 <= chg) & (chg < 0.50)}

    print("=" * 118)
    print("顶背离 × A/B 做多信号（①②长/短期口径 + ③skip-and-reenter 模拟）")
    print("=" * 118)
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
        print(f"\n■ 窗口 L={L}（近端峰 {max(4, L//3)} 根）｜可关联 {int(matched.sum()):,}/{len(rows):,}"
              f" | 背离占比 RSI {flags['rsi'][matched].mean()*100:.1f}% "
              f"VOL {flags['vol'][matched].mean()*100:.1f}% "
              f"组合 {flags['combo'][matched].mean()*100:.1f}%")
        for sig, m in sig_masks.items():
            mm = m & matched
            base = _stats(np.where(mm, ret_ab, np.nan))
            if base is None:
                print(f"  ◆ {sig}: 全池样本不足")
                continue
            print(f"\n  ◆ {sig}")
            print("    【① 长期 TRAIL3/168h】")
            _line("全池基线", base)
            for v in ("rsi", "vol", "combo"):
                _line(f"有背离({v})", _stats(np.where(mm & flags[v], ret_ab, np.nan)), base["mean"])
                _line(f"无背离({v})", _stats(np.where(mm & ~flags[v], ret_ab, np.nan)), base["mean"])
            print("    【② 短期原始收益（不扣成本）】")
            _short_line("全池基线", arr, mm)
            for v in ("rsi", "vol", "combo"):
                _short_line(f"有背离({v})", arr, mm & flags[v])
            print("    【② 先行下跌：12h 内先触 -2% 再触 +2% 的比例】")
            b = _adv_first(arr, mm)
            if b:
                print(f"    {'全池基线':<20} {b[0]:,}/{b[1]:,} = {b[2]*100:.1f}%")
            for v in ("rsi", "vol", "combo"):
                a = _adv_first(arr, mm & flags[v])
                if a:
                    print(f"    {f'有背离({v})':<20} {a[0]:,}/{a[1]:,} = {a[2]*100:.1f}%")
            print("    【③ skip-and-reenter：跳过背离，W 小时内取下一个无背离信号】")
            orders = [[i for i in idxs if mm[i]] for idxs in order]
            orders = [o for o in orders if o]
            n_base, s_base = _sum_rets([ret_ab[i] for grp in orders for i in grp])
            print(f"    {'基准(全量取)':<16} 笔={n_base:>5,} 总PnL={s_base*100:>+9.0f}pp "
                  f"单笔均值={s_base/n_base*100:+.2f}%" if n_base else "")
            for v in ("rsi", "vol", "combo"):
                for W in REENTER_W:
                    _, fs = _skip_reenter(orders, eo, flags[v], ret_ab, W)
                    nf, sf = _sum_rets(fs)
                    if nf:
                        print(f"    {f'跳过{v}+取下一(W={W}h)':<16} 笔={nf:>5,} "
                              f"总PnL={sf*100:>+9.0f}pp 单笔均值={sf/nf*100:+.2f}%")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())