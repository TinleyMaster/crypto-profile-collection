#!/usr/bin/env python3
"""信号实际出场画像（exit profile）：胜率 / 盈亏比 / 期望 / PF / 实际持仓时长。

口径（对齐《盘面异动扫描系统设计方案 v2》§10.3）：
  - 数据：放量大阳事件（chg1h≥3% & vr≥2，2023~2026 全周期 529 合约）缓存 trade_params_events.csv
  - SHORT_LONG（短线做多 ≥50%）：TRAIL 3% 跟踪止盈实际出场（出场价 = 触达时峰值×0.97）
  - MID_LONG  （次档做多 20~50%）  ：同上
  - TRAP_SHORT（诱多做空 <5%）    ：FIX 12h / TP50% / SL10%（做空不启用跟踪）

用途：供 scan_gainers_v2.py 告警邮件引用，输出 data/exit_profile.json。
用法：
    python bin/backtest_exit_profile.py            # 读缓存，计算并写 json + 控制台
    python bin/backtest_exit_profile.py --refresh  # 强制从远程库重建事件缓存（约 20 分钟）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from backtest_trade_params import (  # noqa: E402
    CACHE, HP_COLS, MIN_N, SIGNALS, load_events, _returns_vec, _vec_rows,
)

OUT = SCRIPT_DIR.parent / "data" / "exit_profile.json"
TR = 0.03
COST = 0.003        # 往返成本（0.3%）
FIX_N, FIX_TP, FIX_SL = 12, 0.50, 0.10     # TRAP_SHORT 参数
TR_LIST = [0.03, 0.05, 0.08, 0.10, 0.15]   # 跟踪止盈档位扫描（峰值回撤）
TR_COL = {0.03: "ph_tr3", 0.05: "ph_tr5", 0.08: "ph_tr8", 0.10: "ph_tr10", 0.15: "ph_tr15"}

# scan_gainers_v2.py 信号类型 → (chg 判定, 方向)
# 注：chg24 采用「滚动 24h」口径（对齐线上 ticker/24hr，无前视偏差）。
# 已核实：TRAP_SHORT 在滚动口径下全周期为负期望（文档§2.2 的 77.9% 是日收盘口径，含前视偏差）。
PROFILE_DEFS = [
    ("SHORT_LONG", "短线做多 ≥50%", lambda c: c >= 0.50, 1),
    ("MID_LONG", "次档做多 20~50%", lambda c: (0.20 <= c) & (c < 0.50), 1),
    ("TRAP_SHORT", "诱多做空 <5%", lambda c: c < 0.05, -1),
]


def _stats(ret: np.ndarray) -> dict | None:
    rs = ret[~np.isnan(ret)]
    if len(rs) < MIN_N:
        return None
    win = rs > 0
    n_w = int(win.sum())
    n_l = int((~win).sum())
    avg_win = float(rs[win].mean()) if n_w else 0.0
    avg_loss = float(rs[~win].mean()) if n_l else 0.0
    gross_w = float(rs[win].sum())
    gross_l = abs(float(rs[~win].sum())) if n_l else 0.0
    pf = gross_w / gross_l if gross_l > 0 else float("inf")
    return {
        "n": len(rs),
        "n_win": n_w,
        "n_loss": n_l,
        "win_rate": float((rs > 0).mean()),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff": abs(avg_win / avg_loss) if avg_loss != 0 else float("inf"),
        "mean": float(rs.mean()),
        "median": float(np.median(rs)),
        "pf": pf,
        "worst": float(np.min(rs)),
        "p5": float(np.percentile(rs, 5)),
        "p95": float(np.percentile(rs, 95)),
    }


def _hold_hours(arr: dict[str, np.ndarray], mask: np.ndarray, ph_col: str) -> float:
    """TRAIL 实际持仓时长：首次满足 hp_N >= 触发峰值的最小 N（中位）。"""
    ph = arr[ph_col]
    m = mask & ~np.isnan(ph)
    if not m.any():
        return float("nan")
    exit_h = np.full(int(m.sum()), np.nan)
    ph_m = ph[m]
    for col, _ in HP_COLS:
        hit = arr[col][m] >= ph_m
        exit_h[hit & np.isnan(exit_h)] = int(col[2:])
    return float(np.nanmedian(exit_h))


def _net_stats(ret: np.ndarray, cost: float = COST) -> dict:
    """扣成本后的胜率/期望/PF。"""
    rs = ret[~np.isnan(ret)]
    if len(rs) == 0:
        return {"win_rate": float("nan"), "mean": float("nan"), "pf": float("nan")}
    rs_net = rs - cost
    gross_w = float(rs_net[rs_net > 0].sum())
    gross_l = abs(float(rs_net[rs_net <= 0].sum())) if (rs_net <= 0).any() else 0.0
    return {
        "win_rate": float((rs_net > 0).mean()),
        "mean": float(rs_net.mean()),
        "pf": gross_w / gross_l if gross_l > 0 else float("inf"),
    }


def run_sensitivity(arr: dict[str, np.ndarray], chg24: np.ndarray, entry: np.ndarray) -> None:
    """TR 档位敏感性：对做多信号扫描 3/5/8/10/15% 跟踪止盈，对比胜率/期望/PF。"""
    print("\n" + "=" * 96)
    print("TR 档位敏感性（做多信号，实际出场口径，毛收益）")
    print("=" * 96)
    for sig, label, c_ok, direction in PROFILE_DEFS:
        if direction != 1:
            continue
        mask = c_ok(chg24)
        print(f"\n■ {label} ({sig})  n={int(mask.sum()):,}")
        print(f"  {'TR':>6} {'触发率%':>8} {'胜率%':>7} {'盈亏比':>6} {'期望%':>7} "
              f"{'中位%':>7} {'PF':>6} {'最差%':>7} {'持仓h':>6} {'扣0.3%胜率%':>10} {'扣0.3%期望%':>10}")
        best_pf, best_tr = -1.0, None
        for tr in TR_LIST:
            ph_col = TR_COL[tr]
            ph = arr[ph_col]
            ret = np.where(~np.isnan(ph), ph * (1 - tr) / entry - 1, np.nan)
            ret = np.where(mask, ret, np.nan)
            st = _stats(ret)
            if st is None:
                continue
            hold = _hold_hours(arr, mask, ph_col)
            net = _net_stats(ret)
            trigger = int(((~np.isnan(ph)) & mask).sum())
            hold_s = f"{hold:.0f}" if not np.isnan(hold) else "n/a"
            print(f"  {tr*100:>5.0f}% {trigger/int(mask.sum())*100:>7.1f}% "
                  f"{st['win_rate']*100:>7.1f} {st['payoff']:>6.2f} {st['mean']*100:>7.2f} "
                  f"{st['median']*100:>7.2f} {st['pf']:>6.1f} {st['worst']*100:>7.1f} "
                  f"{hold_s:>6} {net['win_rate']*100:>10.1f} {net['mean']*100:>10.2f}")
            if st["pf"] > best_pf and st["worst"] >= -0.15:
                best_pf, best_tr = st["pf"], tr
        if best_tr is not None:
            print(f"  → 最优 TR（风险约束最差≥-15%）: {best_tr*100:.0f}%  (PF={best_pf:.1f})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="强制重建事件缓存（约 20 分钟）")
    ap.add_argument("--sensitivity", action="store_true", help="TR 档位敏感性扫描（3~15%）")
    args = ap.parse_args()

    if args.refresh and CACHE.exists():
        CACHE.unlink()
    rows = load_events()
    arr = _vec_rows(rows)
    chg24 = arr["chg24"]
    entry = arr["entry"]

    if args.sensitivity:
        run_sensitivity(arr, chg24, entry)
        return 0

    print(f"事件总数: {len(rows):,}  缓存: {CACHE.name}")
    print("=" * 72)
    print(f"{'信号':<24} {'n':>7} {'胜率%':>7} {'盈亏比':>6} {'期望%':>7} "
          f"{'中位%':>7} {'PF':>6} {'最差%':>7} {'实际持仓h':>9}")
    print("-" * 72)

    profiles: dict[str, dict] = {}
    for sig, label, c_ok, direction in PROFILE_DEFS:
        mask = c_ok(chg24)
        n_all = int(mask.sum())
        if n_all < MIN_N:
            print(f"{label:<24} 样本不足 {n_all}")
            continue

        if direction == 1:      # 做多：TRAIL 3% 实际出场
            ph = arr["ph_tr3"]
            ret = np.where(~np.isnan(ph), ph * (1 - TR) / entry - 1, np.nan)
            ret = np.where(mask, ret, np.nan)
            hold = _hold_hours(arr, mask, "ph_tr3")
            mode = "TRAIL3"
        else:                   # 做空：FIX 12h TP50 SL10
            ret = _returns_vec(arr, direction, FIX_N, FIX_TP, FIX_SL, None)
            ret = np.where(mask, ret, np.nan)
            hold = float(FIX_N)
            mode = f"FIX{FIX_N}h"

        st = _stats(ret)
        if st is None:
            print(f"{label:<24} 有效样本不足 {MIN_N}")
            continue

        # 扣成本
        ret_net = ret[~np.isnan(ret)] - COST
        win_c = float((ret_net > 0).mean())
        mean_c = float(ret_net.mean())
        gross_w = float(ret_net[ret_net > 0].sum())
        gross_l = abs(float(ret_net[ret_net <= 0].sum())) if (ret_net <= 0).any() else 0.0
        pf_c = gross_w / gross_l if gross_l > 0 else float("inf")

        hold_s = f"{hold:.0f}" if not np.isnan(hold) else "n/a"
        print(f"{label:<24} {st['n']:>7,} {st['win_rate']*100:>7.1f} {st['payoff']:>6.2f} "
              f"{st['mean']*100:>7.2f} {st['median']*100:>7.2f} {st['pf']:>6.1f} "
              f"{st['worst']*100:>7.1f} {hold_s:>9}")

        profiles[sig] = {
            "label": label, "mode": mode, "direction": "LONG" if direction == 1 else "SHORT",
            **st,
            "hold_hours": hold,
            "cost0.3": {"win_rate": win_c, "mean": mean_c, "pf": pf_c},
        }

    out = {"generated_at": __import__("datetime").datetime.now().astimezone().isoformat(),
           "event_cache": CACHE.name, "signals": profiles}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写出: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
