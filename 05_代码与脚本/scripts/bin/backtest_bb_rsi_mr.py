#!/usr/bin/env python3
"""布林带 + RSI 均值回归策略 · 事件级 MAE/MFE 研究（不调参，只测量）。

背景
----
评审一套流传的「布林带下轨 + RSI 超卖 + RSI 拐头向上」均值回归策略。评审前的**先验
猜想**是赔率倒挂（止损 1.5×ATR 比止盈 2σ 宽），但该猜想建立在「ATR 恒大于 σ」之上。

⚠️ **该先验已被实测推翻**：`biz.asset_klines` 1h 实测 `ATR(14)/σ20 ≈ 0.66`（ATR 反而
**小于** σ，因为加密 1h 收盘波动大、而 TR 的跳空项常被 high−low 幅盖过），计划赔率
R:R ≈ 1.82:1，**对交易者有利**——赔率没有倒挂。

另一先验「中轨在移动 ⇒ 吃掉了利润」也**被双臂实测证伪**（4273 笔事件）：dynamic 与
static 两臂期望几乎相同（−0.461% vs −0.460%）。漂移确实把每笔盈利从计划中位 4.44%
压到实际均值 3.00%，但**同时**把命中率从 static 的 30.7% 抬到 46.9%，两效应恰好抵消。
真实亏损机制是：① 实际亏损均值 −3.32% 大于止损幅度中位 2.33%（被止损打掉的事件风险
偏大），② 约 4.6% 的「止盈」实际是在中轨跌破入场价后以亏损出场。

故本脚本设 **dynamic / static 双臂**，两臂之差用于**量化**漂移效应（而非预设它有罪）。

2026-10-02 追加：**MACD(12,26,9) 趋势过滤臂**（`--macd-filter`）。规则一次定参：
`hist > 0` 只做多、`hist < 0` 只做空（上涨趋势买回调 / 下跌趋势卖反弹），预热期与
`hist == 0` 两侧都挡。`hist` 取信号根 t（无前视）。周期给两个口径做 A/B：`1h`（同周期）
与 `1d`（**库中无日线**，由 1h 现聚合，且每根 1h 只用「最近一个已收盘日」的日线 hist）。
⚠️ 该门**只减不增**：它只能筛掉趋势不同向的事件，不能创造新事件——故 A/B 只能回答
「留下来的那部分是否变好」，不能证明 MACD 有边际。

本脚本**不做参数拟合、不做网格搜索、不改线上任何逻辑**，只回答一个问题：

    按实际成交口径，单笔期望收益是否为正、且日级 t 值是否显著？

判定一律以「实际期望收益 + 日级 t 值」为准；不可用计划赔率的盈亏平衡胜率判生死
（首版据此误报「通过」，见 `summarize` 的 ⚠️ 说明）。样本内选参已多次证明会反向
（见 SCAN-REGIME-GATE-001），故本脚本一次定参、不扫参。

口径（逐条可核）
----------------
- 取价：`biz.asset_klines` 1h（open / high / low / close），服务端游标分批取数
- 指标：SMA20 + 总体标准差 → 布林带(20, 2σ)；Wilder RSI(14)；Wilder ATR(14)
- 做多事件（信号根 t，全部在 t 收盘时刻可得，无未来信息）：
      low[t] ≤ lower[t]  且  rsi[t] < 30  且  rsi[t] > rsi[t-1]
- 做空事件（镜像）：high[t] ≥ upper[t]  且  rsi[t] > 70  且  rsi[t] < rsi[t-1]
- 入场：**t+1 根开盘**成交（与 backtest_scan_scenarios.py 同口径）
- 止盈（dynamic 臂，主口径）：中轨——逐根取 mid[t+k]，忠于「价格回到布林带中轨」字面
- 止盈（static 臂，对照）：锁定信号根中轨——与 dynamic 之差即中轨漂移的净效应
  （实测两臂期望几乎相等，漂移「缩盈利」与「提命中」相抵，见文件头）
- 止损：下轨 − 1.5×ATR[t]（做多）／上轨 + 1.5×ATR[t]（做空），入场时锁定
- 同一根内既触止损又触止盈 → **保守判为止损先**（用 high/low 而非收盘判定，
  故同一根内两者都可能被触及，取悲观解）
- 上限 `--max-hold` 小时仍未触发 → 按该根收盘平仓，记为 timeout（未决）
- 全程只 SELECT；产物只落本地 CSV，不写库

用法
----
    python backtest_bb_rsi_mr.py --self-test                 # 离线注入测试（无 DB）
    python backtest_bb_rsi_mr.py --lookback-days 90          # 快速版
    python backtest_bb_rsi_mr.py --holdout-days 30           # 时序切分 train/test
    python backtest_bb_rsi_mr.py --holdout-days 30 --macd-filter both   # 加 MACD 过滤双臂
"""
from __future__ import annotations

import argparse
import csv
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg.rows  # noqa: E402

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402
from crypto_research.analysis.technical_indicators import (  # noqa: E402
    atr_series,
    bollinger_series,
    macd_series,
    rsi_series,
)

# ── 策略参数：**固定为策略文本给定值，不做任何扫描/拟合**（改这里=改策略，须留档）──
BB_PERIOD = 20
BB_STD = 2.0
RSI_PERIOD = 14
ATR_PERIOD = 14
RSI_OVERSOLD = 30.0
RSI_OVERBOUGHT = 70.0
ATR_STOP_MULT = 1.5

# ── MACD 趋势过滤（2026-10-02 追加，**一次定参**：标准 12/26/9，不扫参不调周期）──
# 规则（预登记，结果出来后不得回改）：
#   hist > 0 ⇒ 只允许做多（上升趋势里买回调）
#   hist < 0 ⇒ 只允许做空（下降趋势里卖反弹）
#   hist 无值（预热期）或 == 0 ⇒ 两侧都挡（无趋势信息，不入场）
# hist 取**信号根 t** 的值（只用 close[0..t]，入场在 t+1 开盘）⇒ 无前视。
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

DEFAULT_MAX_HOLD = 72          # 小时；超时按最后一根收盘平仓
COST = 0.001                   # 双边 taker 手续费（0.05%×2），与 backtest_scan_scenarios 同口径
BATCH_ROWS = 20000             # 服务端游标分批行数（大结果集防整段挂死）
MIN_KLINES_BARS = 1000         # 只测有足够 1h 历史的符号

KLINES_SQL = (
    "SELECT symbol, open_time, open_px, high_px, low_px, close_px "
    "FROM biz.asset_klines WHERE interval='1h' AND symbol = ANY(%s) "
)
UNIVERSE_SQL = (
    "SELECT symbol, COUNT(*) AS n FROM biz.asset_klines "
    "WHERE interval='1h' GROUP BY symbol HAVING COUNT(*) >= %s ORDER BY symbol"
)


def load_klines(conn, symbols: list[str], lookback_days: int = 0) -> dict[str, list[dict]]:
    """加载 1h K 线（含 high/low——「触及下轨」与 ATR 都依赖极值，缺一不可）。

    服务端游标 + `itersize` 分批：远端抖动时单批次失败面更小、峰值内存同步下降。
    """
    out: dict[str, list[dict]] = defaultdict(list)
    with conn.cursor(name="bbmr_klines", row_factory=psycopg.rows.dict_row) as cur:
        cur.itersize = BATCH_ROWS
        if lookback_days > 0:
            cur.execute(KLINES_SQL + "AND open_time >= NOW() - make_interval(days => %s) "
                                     "ORDER BY symbol, open_time", (symbols, lookback_days))
        else:
            cur.execute(KLINES_SQL + "ORDER BY symbol, open_time", (symbols,))
        for r in cur:
            if None in (r["open_px"], r["high_px"], r["low_px"], r["close_px"]):
                continue
            out[r["symbol"]].append({
                "t": r["open_time"],
                "open": float(r["open_px"]),
                "high": float(r["high_px"]),
                "low": float(r["low_px"]),
                "close": float(r["close_px"]),
            })
    return dict(out)


def resample_daily(bars: list[dict]) -> list[tuple]:
    """把 1h K 线按 `open_time` 的日期聚合成日 K，返回 [(date, bar), ...]（升序）。

    close 取当日最后一根 1h 的收盘（即日线收盘），high/low 取当日极值。
    库中**只有 5m/15m/1h，没有日线表**，故日级口径一律由 1h 现聚合，避免引入第二个
    数据源造成口径分叉。
    """
    out: list[tuple] = []
    for b in bars:
        d = b["t"].date()
        if out and out[-1][0] == d:
            row = out[-1][1]
            row["high"] = max(row["high"], b["high"])
            row["low"] = min(row["low"], b["low"])
            row["close"] = b["close"]
        else:
            out.append((d, {"t": b["t"], "open": b["open"], "high": b["high"],
                            "low": b["low"], "close": b["close"]}))
    return out


def daily_hist_for_bars(bars: list[dict], fast: int, slow: int, signal: int,
                        ) -> list[float | None]:
    """返回与 1h `bars` **等长**的「日线 MACD hist」序列（跨周期过滤用）。

    第 i 根 1h 取「日期严格早于该根所属日」的**最后一个已收盘日**的 hist——
    当天的日 K 尚未收盘，用它会引入前视。预热不足时该根为 None（两侧都挡）。
    """
    n = len(bars)
    out: list[float | None] = [None] * n
    days = resample_daily(bars)
    if len(days) < slow + signal:
        return out
    closes = [d[1]["close"] for d in days]
    hist = macd_series(closes, fast, slow, signal)[2]
    j = -1                                   # 指向「最近一个已收盘日」
    for i, b in enumerate(bars):
        d = b["t"].date()
        while j + 1 < len(days) and days[j + 1][0] < d:
            j += 1
        if j >= 0:
            out[i] = hist[j]
    return out


def resolve_trade(bars: list[dict], bands: list, t: int, side: str,
                  entry: float, stop: float, max_hold: int,
                  target_static: float, target_mode: str = "dynamic") -> dict:
    """自 t+1 根起逐根走路径，判定止盈/止损谁先到（同根内保守判止损先）。

    target_mode="dynamic"：止盈位逐根取 mid[t+k]——忠于「价格回到布林带中轨」的字面
        语义（交易者看的是活着的中轨）。**中轨会移动**，趋势中会一路下移/上移，
        因此「摸到中轨」并不等于「赚到入场时的 2σ」，这是本脚本要量化的关键效应。
    target_mode="static"：止盈位锁定为信号根的中轨——把「移动球门」的衰减单独剥离
        出来的对照臂：两臂之差即中轨漂移吃掉的收益。

    返回 exit_px / hold_h / ret_pct / mae_pct / mfe_pct / outcome / target_px。
    mae/mfe 以入场价百分比计，统计至终止根为止。
    """
    n = len(bars)
    last = min(t + 1 + max_hold, n - 1)
    mae = mfe = 0.0
    outcome, exit_px, exit_k, target_px = "timeout", bars[last]["close"], last, None
    for k in range(t + 1, last + 1):
        bk = bars[k]
        band = bands[k]
        tgt = band[0] if (target_mode == "dynamic" and band is not None) else target_static
        if side == "long":
            mae = max(mae, (entry - bk["low"]) / entry)
            mfe = max(mfe, (bk["high"] - entry) / entry)
            if bk["low"] <= stop:                        # 先查止损（悲观解）
                outcome, exit_px, exit_k = "stop", stop, k
                break
            if bk["high"] >= tgt:
                outcome, exit_px, exit_k, target_px = "target", tgt, k, tgt
                break
        else:
            mae = max(mae, (bk["high"] - entry) / entry)
            mfe = max(mfe, (entry - bk["low"]) / entry)
            if bk["high"] >= stop:
                outcome, exit_px, exit_k = "stop", stop, k
                break
            if bk["low"] <= tgt:
                outcome, exit_px, exit_k, target_px = "target", tgt, k, tgt
                break
    ret = (exit_px - entry) / entry if side == "long" else (entry - exit_px) / entry
    return {"outcome": outcome, "exit_px": exit_px, "exit_ts": bars[exit_k]["t"],
            "hold_h": exit_k - t, "ret_pct": ret, "mae_pct": mae, "mfe_pct": mfe,
            "target_px": target_px}


def scan_symbol_events(symbol: str, bars: list[dict], max_hold: int,
                       target_mode: str = "dynamic",
                       macd_hist: list[float | None] | None = None,
                       ) -> tuple[list[dict], dict]:
    """扫描单符号，返回 (事件明细, 漏斗计数)。

    漏斗逐层收窄：触及外轨 → RSI 进超买超卖区 → RSI 拐头 → （可选）MACD 趋势同向
    → 入场。相邻两层之差即该过滤器的净作用。
    `macd_hist` 为与 bars 等长的 hist 序列（None = 不过滤；1h 或日线口径由调用方决定）。
    """
    funnel = {"touch": 0, "rsi_zone": 0, "rsi_turn": 0,
              "macd_blocked": 0, "entry": 0, "dropped_geom": 0}
    n = len(bars)
    if n < BB_PERIOD + RSI_PERIOD + ATR_PERIOD + 3:
        return [], funnel
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    bands = bollinger_series(closes, BB_PERIOD, BB_STD)
    rsi = rsi_series(closes, RSI_PERIOD)
    atr = atr_series(highs, lows, closes, ATR_PERIOD)

    events: list[dict] = []
    for t in range(1, n - 1):
        band = bands[t]
        if band is None or rsi[t] is None or rsi[t - 1] is None:
            continue
        mid, upper, lower = band
        prev_rsi = rsi[t - 1]

        # 第一层：物理边界（做多触下轨 / 做空触上轨）
        if lows[t] <= lower:
            funnel["touch"] += 1
            side = "long"
        elif highs[t] >= upper:
            funnel["touch"] += 1
            side = "short"
        else:
            continue

        # 第二层：动能衰竭（RSI 进超买超卖区）
        cur_rsi = rsi[t]
        if side == "long":
            if cur_rsi >= RSI_OVERSOLD:
                continue
        else:
            if cur_rsi <= RSI_OVERBOUGHT:
                continue
        funnel["rsi_zone"] += 1

        # 第三层：拐点确认（RSI 在区内拐头）
        if side == "long":
            if cur_rsi <= prev_rsi:
                continue
        else:
            if cur_rsi >= prev_rsi:
                continue
        funnel["rsi_turn"] += 1

        # 第四层（可选）：MACD 趋势同向——上涨趋势(hist>0)只做多、下跌趋势(hist<0)只做空。
        # hist 取信号根 t（只用 close[0..t]），入场在 t+1 开盘 ⇒ 无前视；预热期 None 两侧都挡。
        if macd_hist is not None:
            h = macd_hist[t]
            if h is None or h == 0.0 or (side == "long" and h < 0.0) \
                    or (side == "short" and h > 0.0):
                funnel["macd_blocked"] += 1
                continue

        if atr[t] is None or atr[t] <= 0:
            continue
        entry = bars[t + 1]["open"]
        if not entry:
            continue
        stop = lower - ATR_STOP_MULT * atr[t] if side == "long" \
            else upper + ATR_STOP_MULT * atr[t]
        planned_risk = (entry - stop) / entry if side == "long" else (stop - entry) / entry
        planned_reward = (mid - entry) / entry if side == "long" else (entry - mid) / entry
        # 入场价已越过止损或止盈（跳空）→ 几何无意义，丢弃并计数（不静默吞掉）
        if planned_risk <= 0 or planned_reward <= 0:
            funnel["dropped_geom"] += 1
            continue
        funnel["entry"] += 1

        ev = resolve_trade(bars, bands, t, side, entry, stop, max_hold, mid, target_mode)
        ev.update({
            "symbol": symbol, "side": side, "signal_ts": bars[t]["t"],
            "entry_ts": bars[t + 1]["t"], "entry_k": t + 1, "entry": entry,
            "day": bars[t + 1]["t"].date(),
            "stop": stop, "target_static": mid,
            "planned_risk": planned_risk, "planned_reward": planned_reward,
            "rsi": cur_rsi, "atr_pct": atr[t] / entry, "band_pct": (upper - lower) / mid,
        })
        events.append(ev)
    return events, funnel


def summarize(events: list[dict], cost: float, label: str) -> dict | None:
    """汇总。

    ⚠️ 判定口径（2026-10-01 首轮实测后修正）：**不可用「计划赔率的盈亏平衡胜率」判生死**。
    计划赔率取的是入场时的中轨，而实际止盈是逐根移动的中轨——下行趋势中它同步下移，
    使真实盈利远小于计划盈利（实测：计划中位 4.87%，实际均值仅 2.58%）。首版据此
    误报「通过」。现同时给出两条线：`breakeven_planned`（乐观口径，仅作几何参考）
    与 `breakeven_realized`（按实际成交的均值赢/均值亏反推，**这才是要跨过的线**）；
    最终判定一律以**实际期望收益 + 日级 t 值**为准。
    """
    if not events:
        return None
    n = len(events)
    cnt: dict[str, int] = defaultdict(int)
    for e in events:
        cnt[e["outcome"]] += 1
    tgt_rets = [e["ret_pct"] for e in events if e["outcome"] == "target"]
    stp_rets = [e["ret_pct"] for e in events if e["outcome"] == "stop"]
    avg_win = statistics.mean(tgt_rets) if tgt_rets else 0.0
    avg_loss = statistics.mean(stp_rets) if stp_rets else 0.0
    denom = avg_win + abs(avg_loss)
    be_realized = abs(avg_loss) / denom if denom > 0 else 0.0

    risks = sorted(e["planned_risk"] for e in events)
    rewards = sorted(e["planned_reward"] for e in events)
    # 计划盈亏平衡胜率 = risk / (risk + reward)：按入场时的赔率几何
    bes = sorted(e["planned_risk"] / (e["planned_risk"] + e["planned_reward"]) for e in events)
    rrs = sorted(e["planned_reward"] / e["planned_risk"] for e in events)
    nets = [e["ret_pct"] - cost for e in events]
    wins = sum(1 for x in nets if x > 0)
    gw = sum(x for x in nets if x > 0)
    gl = abs(sum(x for x in nets if x < 0))
    day_means: dict = defaultdict(list)
    for e in events:
        day_means[e["day"]].append(e["ret_pct"] - cost)
    dm = [sum(v) / len(v) for v in day_means.values()]
    day_mean = sum(dm) / len(dm)
    day_std = statistics.stdev(dm) if len(dm) > 1 else 0.0
    t_stat = day_mean / (day_std / len(dm) ** 0.5) if day_std > 0 else 0.0
    avg_ret = sum(nets) / n
    if avg_ret <= 0:
        verdict = "不成立（负期望收益）"
    elif t_stat < 1.0:
        verdict = "不可判定（正期望但日级 t<1，样本不足）"
    else:
        verdict = "有毛边际，需 holdout 复核"
    return {
        "label": label, "n": n, "days": len(dm), "cost": cost,
        "target_first": cnt["target"] / n, "stop_first": cnt["stop"] / n,
        "timeout_share": cnt["timeout"] / n,
        "target_at_loss": sum(1 for e in events
                              if e["outcome"] == "target" and e["ret_pct"] <= 0),
        "reward_median_pct": statistics.median(rewards),
        "risk_median_pct": statistics.median(risks),
        "avg_win_pct": avg_win, "avg_loss_pct": avg_loss,
        "rr_median": statistics.median(rrs),
        "breakeven_planned": statistics.median(bes),
        "breakeven_realized": be_realized,
        "win_rate_realized": wins / n,
        "avg_ret_net": avg_ret,
        "profit_factor": gw / gl if gl else float("inf"),
        "day_t_stat": t_stat, "verdict": verdict,
        "hold_median_h": statistics.median(e["hold_h"] for e in events),
        "mae_median_pct": statistics.median(e["mae_pct"] for e in events),
        "mfe_median_pct": statistics.median(e["mfe_pct"] for e in events),
    }


def print_summary(s: dict) -> None:
    print(f"\n=== {s['label']} ===")
    print(f"  样本            : {s['n']} 笔 / {s['days']} 个独立日")
    print(f"  ── 结局分布 ──")
    print(f"  止盈先（回中轨）: {s['target_first']:>7.1%}")
    print(f"  止损先（1.5ATR）: {s['stop_first']:>7.1%}")
    print(f"  超时未决        : {s['timeout_share']:>7.1%}")
    err = s["avg_win_pct"] / s["reward_median_pct"] if s["reward_median_pct"] else 0.0
    print("  ── 赔率：计划 vs 实际 ──")
    print(f"  计划盈利中位数  : {s['reward_median_pct']:>7.2%}   (入场时的中轨 − 入场，≈2.0σ)")
    print(f"  实际盈利均值    : {s['avg_win_pct']:>7.2%}   ← 仅为计划的 {err:.0%}（动态中轨漂移吃掉）")
    print(f"  止损幅度中位数  : {s['risk_median_pct']:>7.2%}   (下轨 − 1.5×ATR，≈2.25σ)")
    print(f"  实际亏损均值    : {s['avg_loss_pct']:>7.2%}")
    print(f"  计划 R:R / 平衡胜率 : {s['rr_median']:.2f} : 1 / {s['breakeven_planned']:.1%}"
          "   ← 乐观口径，不可用于判定")
    print(f"  实际平衡胜率        : {s['breakeven_realized']:>7.1%}   ← 真正要跨过的线")
    print(f"  ── 实际表现（已扣 {s['cost']:.3%} 双边手续费）──")
    print(f"  实际胜率        : {s['win_rate_realized']:>7.1%}")
    print(f"  单笔期望收益    : {s['avg_ret_net']:>7.3%}")
    print(f"  盈亏比 PF       : {s['profit_factor'] if s['profit_factor'] != float('inf') else 999:>7.2f}")
    print(f"  日级 t 统计量   : {s['day_t_stat']:>7.2f}")
    print("  ── 过程量 ──")
    print(f"  持仓中位时长    : {s['hold_median_h']:>7.1f} 小时")
    print(f"  MAE / MFE 中位数: {s['mae_median_pct']:>7.2%} / {s['mfe_median_pct']:.2%}")
    gap = s["target_first"] - s["breakeven_realized"]
    print("  ── 判定 ──")
    print(f"  命中率 − 实际平衡胜率 = {gap:>+7.1%}（负值 = 数学上无法盈利）")
    print(f"  结论: {s['verdict']}")


def print_funnel(funnel: dict, macd_on: bool = False) -> None:
    print("\n=== 事件漏斗（逐层条件收窄）===")
    touch, zone, turn = funnel["touch"], funnel["rsi_zone"], funnel["rsi_turn"]
    entry, blocked = funnel["entry"], funnel["macd_blocked"]
    keep2 = f"(保留 {zone / touch:.1%})" if touch else "(N/A)"
    keep3 = f"(保留 {turn / zone:.1%})" if zone else "(N/A)"
    keep4 = f"(保留 {entry / turn:.1%})" if turn else "(N/A)"
    print(f"  ① 触及布林带外轨            : {touch:>7}")
    print(f"  ② + RSI 进超买/超卖区       : {zone:>7}   {keep2}")
    if macd_on:
        print(f"  ③ + RSI 拐头（候选）        : {turn:>7}   {keep3}")
        print(f"  ④ + MACD 趋势同向（=入场）  : {entry:>7}   {keep4} ← 策略实际入场事件")
        share = f"（占候选 {blocked / turn:.1%}）" if turn else ""
        print(f"     ↳ 被 MACD 挡掉           : {blocked:>7}{share}")
    else:
        print(f"  ③ + RSI 拐头（=入场）       : {entry:>7}   {keep4} ← 策略实际入场事件")
    if funnel["dropped_geom"]:
        print(f"  [丢弃] 入场价已越过止损/止盈（跳空）: {funnel['dropped_geom']}")


def print_ab(index: dict, scenarios: list[str]) -> None:
    """横向 A/B：同一批 K 线、同一事件流，只有入场门不同。

    ⚠️ MACD 门**只减不增**——它不会新增事件，只能筛掉趋势不同向的。故 A/B 只能回答
    「留下的那一部分是否更好」，不能回答「MACD 能创造边际」。
    """
    if len(scenarios) < 2:
        return
    print("\n\n=== MACD 趋势过滤 A/B（同一批 K 线，仅入场门不同）===")
    print(f"  {'场景':<11}{'侧':<7}{'n':>7}{'止盈先':>9}{'净期望':>11}{'PF':>7}{'日t':>8}")
    for scen in scenarios:
        for lab in ("all", "long", "short"):
            s = index.get((scen, lab))
            if not s:
                continue
            pf = 999.0 if s["profit_factor"] == float("inf") else s["profit_factor"]
            print(f"  {scen:<11}{lab:<7}{s['n']:>7}{s['target_first']:>9.1%}"
                  f"{s['avg_ret_net']:>+11.3%}{pf:>7.2f}{s['day_t_stat']:>8.2f}")
    print("  ⇒ 若多头臂仍 ≈ PF 1.05 / 日 t 不显著 ⇒ **MACD 救不了该策略**（不再叠加过滤器）；"
          "若留下的事件数 <1000 ⇒ 不可判定。")


EVENT_FIELDS = (
    "symbol", "side", "signal_ts", "entry_ts", "entry", "stop", "target_static",
    "planned_risk", "planned_reward", "outcome", "target_px", "exit_ts", "exit_px",
    "ret_pct", "hold_h", "mae_pct", "mfe_pct", "rsi", "atr_pct", "band_pct",
)
SUMMARY_FIELDS = (
    "label", "n", "days", "cost",
    "target_first", "stop_first", "timeout_share", "target_at_loss",
    "reward_median_pct", "risk_median_pct", "avg_win_pct", "avg_loss_pct",
    "rr_median", "breakeven_planned", "breakeven_realized",
    "win_rate_realized", "avg_ret_net", "profit_factor",
    "day_t_stat", "hold_median_h", "mae_median_pct", "mfe_median_pct", "verdict",
)


def write_csv(path: Path, fields: tuple, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(fields), extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def self_test() -> int:
    """离线注入测试（无 DB）：合成确定性序列，验证事件检测 + 结局判定 + 不变量。"""
    rand = random.Random(20261001)
    bars: list[dict] = []
    prev = 100.0
    t0 = datetime(2026, 1, 1)
    for i in range(1500):
        px = prev * (1 + rand.uniform(-0.022, 0.020))     # 轻微下行漂移，制造超卖事件
        hi = max(prev, px) * (1 + rand.uniform(0, 0.010))
        lo = min(prev, px) * (1 - rand.uniform(0, 0.010))
        bars.append({"t": t0 + timedelta(hours=i), "open": prev,
                     "high": hi, "low": lo, "close": px})
        prev = px

    events, funnel = scan_symbol_events("SYNTH", bars, max_hold=DEFAULT_MAX_HOLD)
    assert funnel["touch"] > 0, "[self-test] 合成序列未触发任何边界事件，检测逻辑有问题"
    assert events, "[self-test] 三层漏斗未产出任何入场事件"
    for e in events:
        assert e["outcome"] in ("target", "stop", "timeout"), e
        assert 1 <= e["hold_h"] <= DEFAULT_MAX_HOLD, e
        assert e["mae_pct"] >= 0 and e["mfe_pct"] >= 0, e
        assert 0 < e["entry_k"] < len(bars), e
        assert e["entry"] == bars[e["entry_k"]]["open"], e
        if e["outcome"] == "stop":
            # 止损成交价即止损位，收益必然等于 −planned_risk
            assert abs(e["ret_pct"] + e["planned_risk"]) < 1e-9, e
        if e["outcome"] == "target":
            assert e["target_px"] is not None, e
        assert e["planned_risk"] > 0 and e["planned_reward"] > 0, e
    sides = {e["side"] for e in events}
    s = summarize(events, COST, "self-test")
    assert s is not None and s["n"] == len(events)
    assert abs(s["target_first"] + s["stop_first"] + s["timeout_share"] - 1.0) < 1e-9

    # ── MACD 趋势过滤：用常量 hist 序列验「档位」语义 + 守恒，再跑真实 MACD 序列 ──
    nb = len(bars)
    ev_up, fn_up = scan_symbol_events("SYNTH", bars, DEFAULT_MAX_HOLD,
                                      macd_hist=[1.0] * nb)
    ev_dn, fn_dn = scan_symbol_events("SYNTH", bars, DEFAULT_MAX_HOLD,
                                      macd_hist=[-1.0] * nb)
    assert all(e["side"] == "long" for e in ev_up), "[self-test] hist>0 仍放进空头"
    assert all(e["side"] == "short" for e in ev_dn), "[self-test] hist<0 仍放进多头"
    # 门在几何过滤之前 ⇒ 「只留多 + 只留空」必须恰好还原不过滤时的入场数
    assert fn_up["entry"] + fn_dn["entry"] == funnel["entry"], \
        (f"[self-test] MACD 过滤非守恒：{fn_up['entry']} + {fn_dn['entry']}"
         f" != {funnel['entry']}")
    assert fn_up["macd_blocked"] + fn_dn["macd_blocked"] == fn_up["rsi_turn"] > 0, \
        "[self-test] 常量 hist 下「被挡数之和」应等于候选总数（多头挡空头 + 空头挡多头）"
    ev_none, fn_none = scan_symbol_events("SYNTH", bars, DEFAULT_MAX_HOLD,
                                          macd_hist=[None] * nb)
    assert not ev_none and fn_none["rsi_turn"] > 0 \
        and fn_none["macd_blocked"] == fn_none["rsi_turn"], "[self-test] hist 无值时未全挡"

    hist_real = macd_series([b["close"] for b in bars],
                            MACD_FAST, MACD_SLOW, MACD_SIGNAL)[2]
    ev_m, fn_m = scan_symbol_events("SYNTH", bars, DEFAULT_MAX_HOLD, macd_hist=hist_real)
    for e in ev_m:
        h = hist_real[e["entry_k"] - 1]        # 信号根 t = entry_k − 1
        assert h is not None and ((h > 0) == (e["side"] == "long")), e

    print(f"[self-test] OK：方向 {sorted(sides)}")
    print_funnel(funnel)
    print(f"[self-test] MACD 门：hist>0 仅多头 {len(ev_up)} 笔 / hist<0 仅空头 "
          f"{len(ev_dn)} 笔（两者合计 = 基线 {funnel['entry']} 笔，守恒通过）；"
          f"恒无值全挡 {fn_none['macd_blocked']} 笔")
    print(f"[self-test] 真实 MACD(12,26,9)：入场 {len(ev_m)} 笔 / 挡掉 "
          f"{fn_m['macd_blocked']} 笔（候选 {fn_m['rsi_turn']} 笔）")
    print_funnel(fn_m, macd_on=True)
    print(f"[self-test] 止盈先 {s['target_first']:.1%} / 止损先 {s['stop_first']:.1%} / "
          f"超时 {s['timeout_share']:.1%}；计划平衡胜率 {s['breakeven_planned']:.1%} / "
          f"实际平衡胜率 {s['breakeven_realized']:.1%}")
    print("[self-test] 不变量全部通过（结局枚举 / 持仓上限 / MAE·MFE 非负 / "
          "止损收益= −planned_risk / 入场价=t+1 开盘 / MACD 门符号一致且守恒）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="布林带+RSI 均值回归 · 事件级 MAE/MFE 研究（不调参，只测量）")
    parser.add_argument("--symbols", type=int, default=0, help="只测前 N 个符号")
    parser.add_argument("--lookback-days", type=int, default=0,
                        help="只加载最近 N 天 1h K 线（0=全量；快速版建议 90）")
    parser.add_argument("--max-hold", type=int, default=DEFAULT_MAX_HOLD,
                        help=f"最长持有小时数（默认 {DEFAULT_MAX_HOLD}）")
    parser.add_argument("--cost", type=float, default=COST, help="双边手续费（默认 0.001）")
    parser.add_argument("--holdout-days", type=int, default=0,
                        help="时序切分：最近 N 天的入场为 test、其余为 train（0=关闭）")
    parser.add_argument("--out", type=str, default="", help="事件明细 CSV 路径")
    parser.add_argument("--macd-filter", choices=("none", "1h", "1d", "both"), default="none",
                        help="MACD(12,26,9) 趋势过滤（一次定参）：hist>0 只做多 / hist<0 只做空；"
                             "1h=同周期、1d=日线(1h 现聚合)、both=两者都跑做 A/B")
    parser.add_argument("--self-test", action="store_true", help="离线注入测试（无 DB）")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(UNIVERSE_SQL, (MIN_KLINES_BARS,))
            universe = [r[0] for r in cur.fetchall()]
        if args.symbols:
            universe = universe[: args.symbols]
        print(f"[bbrsi] 符号宇宙 {len(universe)}（≥{MIN_KLINES_BARS} 根 1h），"
              f"max_hold={args.max_hold}h，成本 {args.cost:.3%}")

        klines = load_klines(conn, universe, args.lookback_days)
        total_bars = sum(len(v) for v in klines.values())
        if not klines:
            print("[bbrsi] 未取到任何 K 线，退出")
            return 1
        t_min = min(v[0]["t"] for v in klines.values() if v)
        t_max = max(v[-1]["t"] for v in klines.values() if v)
        print(f"[bbrsi] K 线 {total_bars} 根，窗口 {t_min:%Y-%m-%d} ~ {t_max:%Y-%m-%d}")

    # ── 场景：baseline（不过滤）为基准；MACD 门按周期拆成 1h / 日线两臂做 A/B ──
    scenarios: list[tuple[str, str]] = [("baseline", "none")]
    if args.macd_filter in ("1h", "both"):
        scenarios.append(("macd_1h", "1h"))
    if args.macd_filter in ("1d", "both"):
        scenarios.append(("macd_1d", "1d"))
    print(f"[bbrsi] 场景 {[s for s, _ in scenarios]}；MACD 参数 "
          f"{MACD_FAST}/{MACD_SLOW}/{MACD_SIGNAL}（一次定参，不扫参不调周期）")

    hist_cache: dict[tuple[str, str], list[float | None]] = {}

    def hist_for(sym: str, bars: list[dict], tf: str) -> list[float | None] | None:
        """按符号取该场景的 hist 序列（1h 用同周期收盘；1d 用 1h 现聚合的日线）。"""
        if tf == "none":
            return None
        key = (sym, tf)
        if key not in hist_cache:
            if tf == "1h":
                hist_cache[key] = macd_series(
                    [b["close"] for b in bars], MACD_FAST, MACD_SLOW, MACD_SIGNAL)[2]
            else:
                hist_cache[key] = daily_hist_for_bars(
                    bars, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
        return hist_cache[key]

    # 双臂：dynamic（主口径，逐根中轨）/ static（对照，锁定信号根中轨）
    arms: dict[str, dict[str, list[dict]]] = {}
    funnels: dict[str, dict] = {}
    for scen, tf in scenarios:
        arms[scen] = {"dynamic": [], "static": []}
        fn_total: dict[str, int] = defaultdict(int)
        for sym in universe:
            bars = klines.get(sym) or []
            hist = hist_for(sym, bars, tf)
            for mode in ("dynamic", "static"):
                evs, fn = scan_symbol_events(sym, bars, args.max_hold,
                                             target_mode=mode, macd_hist=hist)
                arms[scen][mode].extend(evs)
                if mode == "dynamic":      # 事件检测与 target_mode 无关，漏斗只记一次
                    for k, v in fn.items():
                        fn_total[k] += v
        for m in arms[scen]:
            arms[scen][m].sort(key=lambda e: e["entry_ts"])
        funnels[scen] = fn_total

    base = scenarios[0][0]
    if not arms[base]["dynamic"]:
        print("\n[bbrsi] 基线无入场事件，无法判定（放宽 --lookback-days 或检查数据覆盖）")
        return 1

    cutoff = t_max - timedelta(days=args.holdout_days) if args.holdout_days > 0 else None
    if cutoff is not None:
        print(f"[holdout] entry < {cutoff:%Y-%m-%d %H:%M} → train，其余 → test")
        print("[holdout] ⚠️ 纪律：test 含不同 regime 才有验证意义；"
              "本脚本不调参，故 test 仅作稳定性对照，结论一律 provisional")

    # 逐场景：漏斗 → 多空分布 → all/long/short（+ train/test）分节汇总
    rows: list[dict] = []
    index: dict[tuple[str, str], dict] = {}
    for scen, _tf in scenarios:
        ev_all = arms[scen]["dynamic"]
        print(f"\n\n########## 场景 {scen}（MACD 过滤 "
              f"{'开' if scen != base else '关'}） ##########")
        print_funnel(funnels[scen], macd_on=scen != base)
        print("[bbrsi] 多空分布: " + "，".join(
            f"{sd} {sum(1 for e in ev_all if e['side'] == sd)}"
            for sd in sorted({e['side'] for e in ev_all})))
        sections = [("all", ev_all),
                    ("long", [e for e in ev_all if e["side"] == "long"]),
                    ("short", [e for e in ev_all if e["side"] == "short"])]
        if cutoff is not None:
            sections += [("train", [e for e in ev_all if e["entry_ts"] < cutoff]),
                         ("test", [e for e in ev_all if e["entry_ts"] >= cutoff])]
        for lab, sub in sections:
            s = summarize(sub, args.cost, f"{scen}:{lab}")
            if s:
                print_summary(s)
                rows.append(s)
                index[(scen, lab)] = s

    print_ab(index, [s for s, _ in scenarios])

    # 对照臂：static 锁定中轨（仅基线）。两臂之差 = 「中轨漂移」吃掉的每笔收益
    s_dyn = summarize(arms[base]["dynamic"], args.cost, "static_arm:dynamic逐根中轨")
    s_sta = summarize(arms[base]["static"], args.cost, "static_arm:static锁定中轨")
    if s_dyn and s_sta:
        print("\n=== 中轨漂移效应（双臂对照，基线 all 口径）===")
        print(f"  dynamic（逐根中轨，主）: 止盈先 {s_dyn['target_first']:>7.1%} / "
              f"期望 {s_dyn['avg_ret_net']:>+7.3%} / PF "
              f"{s_dyn['profit_factor'] if s_dyn['profit_factor'] != float('inf') else 999:.2f}")
        print(f"  static （锁定中轨，对照）: 止盈先 {s_sta['target_first']:>7.1%} / "
              f"期望 {s_sta['avg_ret_net']:>+7.3%} / PF "
              f"{s_sta['profit_factor'] if s_sta['profit_factor'] != float('inf') else 999:.2f}")
        print(f"  ⇒ 中轨漂移吃掉每笔 {s_sta['avg_ret_net'] - s_dyn['avg_ret_net']:>+7.3%}"
              "（static − dynamic；正 = 固定目标本可多赚）")
        rows.append(s_sta)

    out_path = Path(args.out) if args.out else \
        SCRIPT_DIR.parent / "data" / "backtest_bb_rsi_mr_events.csv"
    for scen, _tf in scenarios:
        p = out_path if scen == base else \
            out_path.with_name(f"{out_path.stem}_{scen}{out_path.suffix}")
        write_csv(p, EVENT_FIELDS, arms[scen]["dynamic"])
        print(f"\n[bbrsi] 事件明细 {len(arms[scen]['dynamic'])} 行（{scen}）已存 {p}")
    sum_path = out_path.with_name(f"{out_path.stem}_summary.csv")
    write_csv(sum_path, SUMMARY_FIELDS, rows)
    print(f"[bbrsi] 汇总 {len(rows)} 行已存 {sum_path}")
    print("[bbrsi] ⚠️ 本脚本只测量赔率几何与路径结局，不含任何参数选择；"
          "结论可用于否决策略，不构成准入证据")
    return 0


if __name__ == "__main__":
    sys.exit(main())