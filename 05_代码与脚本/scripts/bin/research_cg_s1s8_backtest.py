#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""S1-S8 盘面八象限场景回测（P × OI × CVD × VOL）—— Coinglass 全年历史（2026-10-07）。

框架（用户口径，桶级）：
  - P   = 桶内价格方向（桶收益 > 0 = ↑）
  - OI  = 桶内 OI 变化方向（oi_close/oi_open - 1 > 0 = ↑）
  - CVD = 桶内净主动方向（Σtaker_buy - Σtaker_sell > 0 = ↑）
  - VOL = 桶内成交额相对放量（Σ(buy+sell) ≥ 该币自身中位数 = ↑）
  八象限 = P/OI/CVD 三方向组合；VOL 作为第四维做**分列对比**（每个场景输出
  放量↑ / 缩量↓ 两行），检验成交量是否在场景内产生额外区分（v2.4）。

  | # | P | OI | CVD | 解读 | 信号 |
  | S1 | ↑ | ↑ | ↑ | 现货买盘强+合约新开多仓，真实多头进攻 | 多头趋势延续 |
  | S2 | ↑ | ↑ | ↓ | 现货主动卖，上涨靠合约杠杆，诱多 | 警惕回调（空头候选） |
  | S3 | ↓ | ↑ | ↓ | 现货砸盘+合约新开空单，真实空头 | 空头趋势延续 |
  | S4 | ↓ | ↑ | ↑ | 现货承接，下跌由合约空头砸出，诱空 | 存在反弹潜力 |
  | S5 | ↑ | ↓ | ↑ | 合约平仓+现货买入，获利了结 | 反弹近尾声 |
  | S6 | ↑ | ↓ | ↓ | 空头回补，非新多进场 | 修复反弹 |
  | S7 | ↓ | ↓ | ↓ | 空头止盈平仓，跌势衰竭 | 衰竭信号 |
  | S8 | ↓ | ↓ | ↑ | 现货承接+空头离场，抛压释放 | 见底反弹 |

口径与纪律（与因子投研一致）：
  - 因子桶 [T, T+iv) → 入场 = 桶末价 close(T+iv)（无前视）→
    fwd_h = close(T+iv+h)/close(T+iv) - 1
  - 每币需 ≥ MIN_BUCKETS 有效桶；剔除稳定币/低波动
  - 每组输出 n / 均值收益 / 胜率，对比基线（全部放量桶）
  - 稳健性：超额收益（减 BTC 同期）重复
  - 面板 t 仅参考（结论看桶均值 vs 基线的相对偏移与一致性）

用法：
  python research_cg_s1s8_backtest.py --interval 4h
  python research_cg_s1s8_backtest.py --interval 12h
"""
from __future__ import annotations

import argparse
import math
import os
import statistics
import sys
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

HORIZONS = {"4h": (4, 8, 12, 24, 48), "12h": (12, 24, 48, 72, 96)}
MIN_BUCKETS = {"4h": 60, "12h": 30}
MIN_N = 30
MIN_MED_ABS_RET = 0.003

# 场景元信息（# → 名称/解读/信号）
SCENARIOS = {
    "S1": ("真实多头进攻", "多头趋势延续"),
    "S2": ("诱多（杠杆推动）", "警惕回调·空头候选"),
    "S3": ("真实空头", "空头趋势延续"),
    "S4": ("诱空（现货承接）", "存在反弹潜力"),
    "S5": ("获利了结", "反弹近尾声"),
    "S6": ("空头回补", "修复反弹"),
    "S7": ("空头止盈·跌势衰竭", "衰竭信号"),
    "S8": ("抛压释放·现货承接", "见底反弹"),
}


# ── 数据加载 ──────────────────────────────────────────────────

def load_oi(conn, interval: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, ts, oi_open, oi_close
            FROM biz.cg_oi_hist
            WHERE interval = %s AND oi_open IS NOT NULL AND oi_close IS NOT NULL AND oi_open > 0
            ORDER BY symbol, ts
            """, (interval,))
        return cur.fetchall()


def load_cvd(conn, interval: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, ts, taker_buy_usd, taker_sell_usd
            FROM biz.cg_taker_volume_hist
            WHERE interval = %s AND taker_buy_usd IS NOT NULL AND taker_sell_usd IS NOT NULL
            ORDER BY symbol, ts
            """, (interval,))
        return cur.fetchall()


def load_klines_1h(conn, symbols: set[str], lo_ts) -> dict[str, dict]:
    out: dict[str, dict] = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, open_time, close_px
            FROM biz.asset_klines
            WHERE interval = '1h' AND open_time >= %s::timestamp
              AND symbol = ANY(%s::text[])
            ORDER BY symbol, open_time
            """, (lo_ts, list(symbols)))
        for sym, ot, close in cur.fetchall():
            if close is None or float(close) <= 0:
                continue
            out.setdefault(sym, {})[ot] = float(close)
    return out


def stats_of(rets: list[float]) -> dict:
    if not rets:
        return {"n": 0}
    mean = sum(rets) / len(rets)
    win = sum(1 for x in rets if x > 0) / len(rets)
    return {"n": len(rets), "mean": mean, "win": win}


# ── 面板构建 ──────────────────────────────────────────────────

def build_scene_panel(oi_rows: list, cvd_rows: list, klines: dict[str, dict],
                      interval: str) -> tuple[list[dict], dict]:
    """构建桶级八象限面板：每行 = 币×桶（含 P/OI/CVD/VOL 方向、fwd 收益、BTC 基准）。

    方向：P = 桶收益符号；OI = oi_close/oi_open-1 符号；CVD = (buy-sell) 符号；
          VOL = 桶 taker 总额 vs 该币中位数（相对放量）。
    入场 = 桶末价 close(T+iv)；fwd_h 从入场价起算（无前视）。
    """
    iv_h = int(interval[:-1]) if interval.endswith("h") else 4

    # OI 方向 + CVD 方向 + VOL 量（按币×ts）
    oi_dir: dict[tuple, int] = {}
    for sym, ts, o, c in oi_rows:
        o, c = float(o), float(c)
        if o > 0:
            oi_dir[(sym, ts)] = 1 if c / o - 1.0 > 0 else 0
    cvd_net: dict[tuple, float] = {}
    cvd_vol: dict[tuple, float] = {}
    for sym, ts, b, sl in cvd_rows:
        b, sl = float(b), float(sl)
        cvd_net[(sym, ts)] = b - sl
        cvd_vol[(sym, ts)] = b + sl

    # 每币 VOL 中位数（相对放量阈值）
    vol_by_sym: dict[str, list[float]] = defaultdict(list)
    for (sym, ts), v in cvd_vol.items():
        vol_by_sym[sym].append(v)
    vol_med: dict[str, float] = {sym: statistics.median(vs) for sym, vs in vol_by_sym.items() if vs}

    # BTC 基准
    btc_id = "BTCUSDT"
    btc_px = klines.get(btc_id, {})
    btc_hours = sorted(btc_px)
    bn = len(btc_hours)
    btc_fwd: dict = {}
    btc_day: dict = {}
    for i, d in enumerate(btc_hours):
        btc_fwd[d] = {}
        for h in HORIZONS[interval]:
            if i + h < bn and btc_px[btc_hours[i]] > 0:
                btc_fwd[d][h] = btc_px[btc_hours[i + h]] / btc_px[btc_hours[i]] - 1.0
    for i in range(1, bn):
        if btc_px[btc_hours[i - 1]] > 0:
            btc_day[btc_hours[i]] = btc_px[btc_hours[i]] / btc_px[btc_hours[i - 1]] - 1.0

    # 按币组装
    keys_by_sym: dict[str, set] = defaultdict(set)
    for (sym, ts) in oi_dir:
        keys_by_sym[sym].add(ts)
    for (sym, ts) in cvd_net:
        keys_by_sym[sym].add(ts)

    panel: list[dict] = []
    per_asset_mar: dict[str, list[float]] = {}

    for sym, tss in keys_by_sym.items():
        px = klines.get(sym)
        if not px:
            continue
        hours = sorted(px)
        idx = {d: i for i, d in enumerate(hours)}
        n = len(hours)

        # 低波动过滤（1h 收益中位数）
        rets = []
        for i in range(1, n):
            if px[hours[i - 1]] > 0:
                rets.append(abs(px[hours[i]] / px[hours[i - 1]] - 1.0))
        if rets:
            per_asset_mar[sym] = rets
            if statistics.median(rets) < MIN_MED_ABS_RET:
                continue

        vm = vol_med.get(sym)
        if vm is None or vm <= 0:
            continue

        for ts in sorted(tss):
            if ts not in idx or (sym, ts) not in oi_dir or (sym, ts) not in cvd_net:
                continue
            if idx[ts] + iv_h + max(HORIZONS[interval]) >= n:
                continue
            i_entry = idx[ts] + iv_h
            entry_px = px[hours[i_entry]]
            if entry_px <= 0:
                continue
            p_dir = 1 if entry_px / px[hours[idx[ts]]] - 1.0 > 0 else 0
            o_dir = oi_dir[(sym, ts)]
            net = cvd_net[(sym, ts)]
            c_dir = 1 if net > 0 else 0
            vol_flag = 1 if cvd_vol[(sym, ts)] >= vm else 0   # VOL↑ 前提
            fwd: dict = {}
            for h in HORIZONS[interval]:
                fwd[h] = px[hours[i_entry + h]] / entry_px - 1.0
            panel.append({
                "symbol": sym, "bucket": ts,
                "p": p_dir, "oi": o_dir, "cvd": c_dir, "vol": vol_flag,
                "fwd": fwd, "btc_fwd": btc_fwd.get(hours[i_entry], {}),
            })
    return panel, per_asset_mar


# ── 输出 ──────────────────────────────────────────────────────

# 用户口径 S1-S8 的三元方向 (p, oi, cvd)，1=↑ 0=↓（与用户表格严格一致）
SCENE_MAP = {
    (1, 1, 1): "S1",   # P↑ OI↑ CVD↑  真实多头进攻
    (1, 1, 0): "S2",   # P↑ OI↑ CVD↓  诱多（杠杆推动）
    (0, 1, 0): "S3",   # P↓ OI↑ CVD↓  真实空头
    (0, 1, 1): "S4",   # P↓ OI↑ CVD↑  诱空（现货承接）
    (1, 0, 1): "S5",   # P↑ OI↓ CVD↑  获利了结
    (1, 0, 0): "S6",   # P↑ OI↓ CVD↓  空头回补
    (0, 0, 0): "S7",   # P↓ OI↓ CVD↓  空头止盈·跌势衰竭
    (0, 0, 1): "S8",   # P↓ OI↓ CVD↑  抛压释放·现货承接
}


def scene_key(r: dict) -> str:
    return SCENE_MAP.get((r["p"], r["oi"], r["cvd"]), "S?")


def print_results(panel: list[dict], title: str, use_excess: bool = False) -> None:
    hs = HORIZONS["4h" if "4h" in title else "12h"]
    print(f"\n=== {title} ===")

    def _y(r, h):
        if h == "same":
            return None
        if use_excess:
            b = r["fwd"].get(h)
            btc = r["btc_fwd"].get(h)
            return b - btc if b is not None and btc is not None else None
        return r["fwd"].get(h)

    # 全样本基线（含放量+缩量）与 VOL 分列基线
    base_all = {h: stats_of([_y(r, h) for r in panel if _y(r, h) is not None]) for h in hs}
    vol_up = [r for r in panel if r["vol"] == 1]
    vol_dn = [r for r in panel if r["vol"] == 0]
    base_up = {h: stats_of([_y(r, h) for r in vol_up if _y(r, h) is not None]) for h in hs}
    base_dn = {h: stats_of([_y(r, h) for r in vol_dn if _y(r, h) is not None]) for h in hs}
    print(f"  {'场景':<4} {'P':>2} {'OI':>2} {'CVD':>2} {'VOL':>2} {'n':>7} | "
          + " | ".join(f"H{h}h" for h in hs))
    for k in [f"S{i}" for i in range(1, 9)]:
        rows_up = [r for r in vol_up if scene_key(r) == k]
        rows_dn = [r for r in vol_dn if scene_key(r) == k]
        p = (rows_up or rows_dn)[0]
        for tag, rows in (("↑", rows_up), ("↓", rows_dn)):
            if not rows:
                print(f"  {k:<4} {'↑' if p['p'] else '↓':>2} {'↑' if p['oi'] else '↓':>2} "
                      f"{'↑' if p['cvd'] else '↓':>2} {tag:>2} {0:>7} | 无样本")
                continue
            cells = []
            for h in hs:
                st = stats_of([_y(r, h) for r in rows if _y(r, h) is not None])
                cells.append(f"{st['mean']*100:+6.2f}/{st['win']*100:3.0f}%"
                             if st["n"] >= MIN_N else "  --/--")
            print(f"  {k:<4} {'↑' if p['p'] else '↓':>2} {'↑' if p['oi'] else '↓':>2} "
                  f"{'↑' if p['cvd'] else '↓':>2} {tag:>2} {len(rows):>7} | " + " | ".join(cells))

    def _base_line(base: dict, label: str) -> None:
        bcells = []
        for h in hs:
            st = base[h]
            bcells.append(f"{st['mean']*100:+6.2f}/{st['win']*100:3.0f}%" if st["n"] >= MIN_N else "  --/--")
        print(f"  {label:<14} {len(vol_up) if '放量' in label else len(vol_dn) if '缩量' in label else len(panel):>7} | "
              + " | ".join(bcells))

    _base_line(base_up, "基线·放量VOL↑")
    _base_line(base_dn, "基线·缩量VOL↓")
    _base_line(base_all, "基线·全样本")

    # VOL 分列差异核对（放量 vs 缩量，最远档）
    print(f"\n  —— VOL 分列差异核对（H{hs[-1]}h 均值收益 %） ——")
    for k in [f"S{i}" for i in range(1, 9)]:
        rows_up = [r for r in vol_up if scene_key(r) == k]
        rows_dn = [r for r in vol_dn if scene_key(r) == k]
        hlast = hs[-1]
        mu = statistics.mean([_y(r, hlast) for r in rows_up if _y(r, hlast) is not None]) if len(rows_up) >= MIN_N else None
        md = statistics.mean([_y(r, hlast) for r in rows_dn if _y(r, hlast) is not None]) if len(rows_dn) >= MIN_N else None
        nm, desc = SCENARIOS[k]
        if mu is None and md is None:
            print(f"  {k} {nm:<10} | 样本不足")
            continue
        fmt = lambda v: "  --" if v is None else f"{v*100:+6.2f}"
        print(f"  {k} {nm:<10} | 放量{fmt(mu)}  缩量{fmt(md)}  (Δ{'%+.2f' % ((mu-md)*100) if mu is not None and md is not None else '--'})")


def main() -> None:
    ap = argparse.ArgumentParser(description="S1-S8 盘面八象限场景回测（P×OI×CVD×VOL）")
    ap.add_argument("--interval", choices=("4h", "12h"), default="4h")
    args = ap.parse_args()

    s = get_settings()
    with get_connection(s.database_url) as conn:
        oi_rows = load_oi(conn, args.interval)
        cvd_rows = load_cvd(conn, args.interval)
        all_rows = oi_rows + cvd_rows
        syms = {r[0] for r in all_rows} | {"BTCUSDT"}
        lo = min([r[1] for r in all_rows], default=None)
        if lo is None:
            print("无 Coinglass 历史数据")
            return
        klines = load_klines_1h(conn, syms, lo)

    print(f"粒度={args.interval} | oi={len(oi_rows)} cvd={len(cvd_rows)} | K线币数={len(klines)}")

    panel, _ = build_scene_panel(oi_rows, cvd_rows, klines, args.interval)
    syms_n = len({r["symbol"] for r in panel})
    print(f"面板：{len(panel)} 行 / {syms_n} 币（VOL↑ 桶 {sum(1 for r in panel if r['vol'])}）")

    print_results(panel, f"S1-S8 八象限回测（{args.interval}，n 桶）")
    print_results(panel, f"稳健性：超额(减BTC)（{args.interval}）", use_excess=True)

    print("\n完成。")


if __name__ == "__main__":
    main()
