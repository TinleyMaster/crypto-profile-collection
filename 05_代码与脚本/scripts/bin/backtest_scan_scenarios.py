#!/usr/bin/env python3
"""盘面异动扫描 P1 · 8 场景回测框架（新取价层，替代日线 T+N 口径）。

评审要点落地：
  - 取价层：biz.asset_klines 1h（分钟级入场：信号 bar 收盘 → 下一根开盘入场）
  - 收益口径：1h/4h/24h 三窗口，净收益 = 毛收益 − 双边 taker 手续费（0.05%×2）
  - 消融：baseline（价+量触发）vs 叠加 OI 方向的 P×OI 分桶 → 量化 OI 的边际增量
  - 横截面去相关：按入场日聚类，报告独立天数 + 日级 t 统计（保守 CI）
  - CVD 维度：接入 biz.oi_cvd_snapshot.cvd_5m_usd（2026-09-16 起实时积累），按「触发根所在
    小时」的净额符号拆设计方案 §4.3 ② 的 8 场景 S1..S8；P×OI 四象限表保留不变（向后兼容）。
    ⚠️ 口径边界：回测取**整小时** CVD 净额符号，线上 _compute_l2 取**近 2 个 5m 桶**（≈10min），
    两者窗口不同（与 OI 的 §12.1-B18 同类），结论映射线上时须留意
  - funding 消融（历史已回填 biz.funding_rate_hist）：每个 P×OI 场景再按结算点资金费率
    正/负/近零拆分 → 验证"高费率做多更差"假设，评估拥挤度过滤是否值得加进 L2 校验

用法：
    python backtest_scan_scenarios.py                      # 全量回测
    python backtest_scan_scenarios.py --symbols 10         # 只测前 10 个符号
    python backtest_scan_scenarios.py --min-n 20 --cost 0.001
    python backtest_scan_scenarios.py --holdout-days 10    # 时序切分：最近 10 天=test，其余=train

holdout（方案 §14，2026-09-29）：
    --holdout-days N 开启时序切分（按 entry 时间，最近 N 天为 test，其余为 train），
    train/test 并列输出 + 指标影子消融（RSI/%B/BBW 三分位分桶）。
    ⚠️ 纪律：test 含不同 regime 才有验证意义；单一 regime 内的 holdout 通过 ≠ A3 关闭，
    所有 test 侧结论一律标 provisional（不得引用为「已标定」证据）。

技术指标（方案 §14）：
    每个触发点记录 RSI(14)/布林带 %B/BBW，输出三分位分桶的影子消融——
    只验证「指标是否补真缺口」，不改触发逻辑；替换线上阈值属标定决策，须另行走查。
"""
from __future__ import annotations

import argparse
import sys
import csv
import bisect
from collections import defaultdict
from datetime import datetime, timedelta, timezone
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
    bbw as bb_width,
    bollinger_series,
    percent_b,
    rsi_series,
)

PRICE_THR_1H = 4.0            # 1h 单根涨跌幅阈值（%），2026-09-17 阈值敏感性标定：高确定性定位 3.0→4.0
VOL_RATIO_THR = 2.0           # 量 ≥ N × 近 20 根均值
LOOKBACK = 20
HORIZONS = (1, 4, 24)         # 持有小时数
COST = 0.001                  # 双边 taker 手续费（0.05%×2=0.1%）
MIN_N = 20                    # 桶最少样本数
MIN_DAYS = 5                  # 最少独立天数
MIN_KLINES_BARS = 1000        # 只测有足够 1h 历史的符号
FUND_POS_THR = 0.0001         # 资金费率正负判定阈值（±1bp/8h）
RSI_PERIOD = 14               # 影子消融用（§14.2：RSI 限衰竭方向验证，不改触发）
BB_PERIOD = 20                # 布林带周期（影子消融用）
BB_STD = 2.0                  # 布林带 σ 倍数
INDICATOR_BUCKETS = ("low", "mid", "high")   # 三分位桶
BATCH_ROWS = 20000              # 服务端游标分批取数行数（见 load_klines docstring）

SCENARIOS = ("Pup_OIup", "Pup_OIdown", "Pdown_OIup", "Pdown_OIdown")

# 设计方案 §4.3 ② 的「设计口径」8 场景：(p_dir, oi_dir, cvd_dir) → S1..S8
SCENARIO8 = {
    ("up", "up", "up"): "S1",      # 现货买盘强 + 合约新开多仓，真实多头进攻
    ("up", "up", "down"): "S2",    # 现货主动卖，上涨靠合约杠杆，诱多
    ("down", "up", "down"): "S3",  # 现货砸盘 + 合约新开空单，真实空头
    ("down", "up", "up"): "S4",    # 现货承接，下跌由合约空头砸出，诱空
    ("up", "down", "up"): "S5",    # 合约平仓 + 现货买入，获利了结
    ("up", "down", "down"): "S6",  # 空头回补，非新多进场
    ("down", "down", "down"): "S7",  # 空头止盈平仓，跌势衰竭
    ("down", "down", "up"): "S8",  # 现货承接 + 空头离场，抛压释放
}


def load_klines(conn, symbols: list[str], lookback_days: int = 0) -> dict[str, list[dict]]:
    """加载 1h K 线。

    ⚠️ 2026-10-01 排障：一次取近 50 万行时，远端连接抖动会让服务端长期阻塞在
    `ClientWrite`（客户端 0% CPU、UTIME 仅 0.5s），整段数据既拿不到也报不出，
    `--lookback-days 45` 比全量更易触发。改用**服务端游标 + 分批迭代**（`itersize`）
    把大结果集切成小批次：单批次失败面更小，峰值内存同步下降。取数语义完全不变。
    """
    out: dict[str, list[dict]] = defaultdict(list)
    with conn.cursor(name="bt_klines", row_factory=psycopg.rows.dict_row) as cur:
        cur.itersize = BATCH_ROWS
        if lookback_days > 0:
            cur.execute(
                "SELECT symbol, open_time, open_px, close_px, quote_vol FROM biz.asset_klines "
                "WHERE interval='1h' AND symbol = ANY(%s) "
                "AND open_time >= NOW() - make_interval(days => %s) "
                "ORDER BY symbol, open_time",
                (symbols, lookback_days),
            )
        else:
            cur.execute(
                "SELECT symbol, open_time, open_px, close_px, quote_vol FROM biz.asset_klines "
                "WHERE interval='1h' AND symbol = ANY(%s) ORDER BY symbol, open_time",
                (symbols,),
            )
        for r in cur:
            out[r["symbol"]].append({
                "t": r["open_time"], "open": float(r["open_px"]),
                "close": float(r["close_px"]), "vol": float(r["quote_vol"] or 0),
            })
    return dict(out)


def load_oi_hourly(conn, symbols: list[str]) -> dict[str, dict[datetime, float]]:
    """小时级 OI（回填 1h 点 + 实时 5m 桶聚合），返回 {symbol: {hour_ts: oi}}。"""
    out: dict[str, dict[datetime, float]] = defaultdict(dict)
    with conn.cursor(name="bt_oi", row_factory=psycopg.rows.dict_row) as cur:
        cur.itersize = BATCH_ROWS
        cur.execute(
            "SELECT symbol, date_trunc('hour', ts) AS h, AVG(oi_usd) AS oi "
            "FROM biz.oi_cvd_snapshot WHERE oi_usd IS NOT NULL AND symbol = ANY(%s) "
            "GROUP BY symbol, date_trunc('hour', ts)",
            (symbols,),
        )
        for r in cur:
            if r["oi"] is not None:
                out[r["symbol"]][r["h"]] = float(r["oi"])
    return dict(out)


def load_cvd_hourly(conn, symbols: list[str]) -> dict[str, dict[datetime, float]]:
    """小时级 CVD 净额（实时 5m 桶按小时求和），返回 {symbol: {hour_ts: cvd_sum}}。

    ⚠️ 窗口口径：取**触发根所在整小时**的 CVD 净额和（sum > 0 → 主动买盘强），
    与线上 `_compute_l2` 的「近 2 个 5m 桶」不同（同类边界见 §12.1-B18）。
    无数据（2026-09-16 前的触发根）返回缺键 → 该记录 cvd_dir 为 None（不进 8 场景桶）。
    """
    out: dict[str, dict[datetime, float]] = defaultdict(dict)
    with conn.cursor(name="bt_cvd", row_factory=psycopg.rows.dict_row) as cur:
        cur.itersize = BATCH_ROWS
        cur.execute(
            "SELECT symbol, date_trunc('hour', ts) AS h, SUM(cvd_5m_usd) AS cvd "
            "FROM biz.oi_cvd_snapshot WHERE cvd_5m_usd IS NOT NULL AND symbol = ANY(%s) "
            "GROUP BY symbol, date_trunc('hour', ts)",
            (symbols,),
        )
        for r in cur:
            if r["cvd"] is not None:
                out[r["symbol"]][r["h"]] = float(r["cvd"])
    return dict(out)


def load_funding(conn, symbols: list[str]) -> dict[str, tuple[list, list[float]]]:
    """资金费率历史（8h 结算点）→ {symbol: (sorted_fts, rates)}，供 bisect 近邻查找。"""
    out: dict[str, tuple[list, list[float]]] = defaultdict(lambda: ([], []))
    with conn.cursor(name="bt_funding", row_factory=psycopg.rows.dict_row) as cur:
        cur.itersize = BATCH_ROWS
        cur.execute(
            "SELECT symbol, funding_time, rate FROM biz.funding_rate_hist "
            "WHERE symbol = ANY(%s) ORDER BY symbol, funding_time",
            (symbols,),
        )
        for r in cur:
            if r["rate"] is not None:
                out[r["symbol"]][0].append(r["funding_time"])
                out[r["symbol"]][1].append(float(r["rate"]))
    return {k: v for k, v in out.items() if v[0]}


def funding_tag(funding: tuple[list, list[float]] | None, h: datetime) -> str | None:
    """取 <= h 的最近结算点费率，映射为 '+'（正）/ '-'（负）/ '0'（近零）；无数据返回 None。"""
    if not funding:
        return None
    fts, rates = funding
    i = bisect.bisect_right(fts, h) - 1
    if i < 0:
        return None
    rate = rates[i]
    if rate > FUND_POS_THR:
        return "+"
    if rate < -FUND_POS_THR:
        return "-"
    return "0"


def hour_key(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def scan_symbol(bars: list[dict], oi_hours: dict[datetime, float],
                funding: tuple[list, list[float]] | None,
                trades: dict[str, list], cost: float,
                cvd_hours: dict[datetime, float] | None = None,
                price_thr: float = PRICE_THR_1H,
                vol_thr: float = VOL_RATIO_THR) -> None:
    """扫描单符号，产出 (scenario, horizon, day, net_ret, fund_tag) 记录。

    口径对齐（2026-09-22）：本回测只在**已收盘**历史条上迭代（`t` 上界 `n-max_h-1`
    保证所判根早已收盘，且历史回填条不再被 UPSERT 覆盖）。线上 `_l1_screen` 已同步为
    「优先最近一根已收盘条、不合格才回退未收盘条、入场/失效位锚定被判定的那一根」
    ⇒ 两侧的触发根口径一致（入库后不再变），回测结论可直接映射到线上。
    """
    n = len(bars)
    max_h = max(HORIZONS)
    # 影子指标（§14）：整序列一次 O(n) 预计算，触发点查表；不改触发逻辑、只随 trade 落记录
    closes = [b["close"] for b in bars]
    rsi_s = rsi_series(closes, RSI_PERIOD)
    bb_s = bollinger_series(closes, BB_PERIOD, BB_STD)
    for t in range(LOOKBACK + 1, n - max_h - 1):
        bar = bars[t]
        prev = bars[t - 1]
        if not prev["close"]:
            continue
        chg = (bar["close"] - prev["close"]) / prev["close"] * 100
        vols = [b["vol"] for b in bars[t - LOOKBACK:t]]
        vol_mean = sum(vols) / LOOKBACK if vols else 0
        vol_ratio = bar["vol"] / vol_mean if vol_mean else 0.0
        if abs(chg) < price_thr or vol_ratio < vol_thr:
            continue
        direction = "up" if chg >= 0 else "down"

        # 影子指标取值（头部数据不足为 None → 消融时跳过）
        rsi_val = rsi_s[t]
        pb_val = bbw_val = None
        bb = bb_s[t]
        if bb is not None:
            pb_val = percent_b(bar["close"], bb[1], bb[2])
            bbw_val = bb_width(bb[0], bb[1], bb[2])

        # OI 方向（可缺失 → 基线样本，用于消融）
        h = hour_key(bar["t"])
        oi_now = oi_hours.get(h)
        oi_prev = oi_hours.get(h - timedelta(hours=1))
        if oi_now is not None and oi_prev is not None and oi_prev != 0:
            oi_dir = "up" if oi_now > oi_prev else "down"
            scenario = f"P{direction}_OI{oi_dir}"
        else:
            scenario = "BASELINE_ONLY"
        ftag = funding_tag(funding, h) or "NA"

        # CVD 方向（触发根所在小时内 cvd_5m_usd 净额和的符号；可缺失 → 不进 8 场景桶）
        cvd_sum = (cvd_hours or {}).get(h)
        if cvd_sum is None or cvd_sum == 0:
            cvd_dir = None
        else:
            cvd_dir = "up" if cvd_sum > 0 else "down"

        entry = bars[t + 1]["open"]
        if not entry:
            continue
        day = bars[t + 1]["t"].date()
        entry_ts = bars[t + 1]["t"]
        for hz in HORIZONS:
            exit_close = bars[t + hz]["close"]
            if not exit_close:
                continue
            ret_long = (exit_close - entry) / entry
            ret = ret_long if direction == "up" else -ret_long
            # 记录结构：(hz, day, net_ret, fund_tag, entry_ts, rsi, percent_b, bbw, cvd_dir)
            trades[scenario].append((hz, day, ret - cost, ftag,
                                     entry_ts, rsi_val, pb_val, bbw_val, cvd_dir))


def split_trades(trades: dict[str, list], cutoff: datetime,
                 ) -> tuple[dict[str, list], dict[str, list]]:
    """按 entry 时间（记录第 5 位）时序切分：entry < cutoff → train，否则 test。

    时序切分（非随机）——杜绝未来信息泄漏进 train。
    """
    train: dict[str, list] = defaultdict(list)
    test: dict[str, list] = defaultdict(list)
    for sc, recs in trades.items():
        for r in recs:
            (train if r[4] < cutoff else test)[sc].append(r)
    return train, test


def summarize_indicator_buckets(trades: dict[str, list], min_n: int,
                                horizons: tuple[int, ...] = (1,)) -> list[dict]:
    """指标影子消融（§14）：RSI / %B / BBW 各按全样本三分位切 low/mid/high 桶。

    只回答一个问题——「指标分位与收益是否单调/有边际」，不改触发逻辑。
    指标缺失（None）的记录跳过。horizons 默认只看 1h（§8.1 最关键窗口）。
    """
    idx_by_name = {"rsi": 5, "percent_b": 6, "bbw": 7}
    rows: list[dict] = []
    for name, idx in idx_by_name.items():
        recs = [r for sc, rs in trades.items() for r in rs
                if r[0] in horizons and r[idx] is not None]
        if len(recs) < min_n * 3:
            continue
        vals = sorted(r[idx] for r in recs)
        q1, q2 = vals[len(vals) // 3], vals[2 * len(vals) // 3]
        for bucket in INDICATOR_BUCKETS:
            if bucket == "low":
                sub = [r for r in recs if r[idx] <= q1]
            elif bucket == "mid":
                sub = [r for r in recs if q1 < r[idx] <= q2]
            else:
                sub = [r for r in recs if r[idx] > q2]
            if len(sub) < min_n:
                continue
            nets = [r[2] for r in sub]
            wins = sum(1 for x in nets if x > 0)
            gross_win = sum(x for x in nets if x > 0)
            gross_loss = abs(sum(x for x in nets if x < 0))
            rows.append({
                "indicator": name, "bucket": bucket,
                "cut_low": round(q1, 4), "cut_high": round(q2, 4),
                "n": len(sub), "win_rate": wins / len(sub),
                "avg_ret_net": sum(nets) / len(nets),
                "profit_factor": gross_win / gross_loss if gross_loss else float("inf"),
            })
    return rows


def sweep_all(klines: dict[str, list], oi_hourly: dict[str, dict[datetime, float]],
              funding: dict[str, tuple[list, list[float]]], cost: float,
              price_thrs: list[float], vol_thrs: list[float],
              min_n: int, min_days: int,
              cutoff: datetime | None = None) -> list[dict]:
    """阈值敏感性扫描：对每组 (price_thr, vol_thr) 跑全宇宙，聚焦 P↑OI↑ 场景。

    cutoff 给定时（holdout 模式），每个阈值组的 train/test 并列输出，
    行多一列 split ∈ {train, test}——阈值选择仍只允许看 train，test 只做对照。
    返回行：{price_thr, vol_thr, split, scenario, horizon, n, days, win_rate,
             avg_ret_net, profit_factor, day_t_stat}。
    """
    rows: list[dict] = []
    for pt in price_thrs:
        for vt in vol_thrs:
            trades: dict[str, list] = defaultdict(list)
            for sym in klines:
                scan_symbol(klines[sym], oi_hourly.get(sym, {}),
                            funding.get(sym), trades, cost, price_thr=pt, vol_thr=vt)
            splits: list[tuple[str, dict[str, list]]]
            if cutoff is not None:
                tr, te = split_trades(trades, cutoff)
                splits = [("train", tr), ("test", te)]
            else:
                splits = [("all", trades)]
            for split_name, sub_trades in splits:
                for r in summarize(sub_trades, min_n, min_days):
                    if r["scenario"] not in ("Pup_OIup", "Pup_OIdown"):
                        continue
                    r = dict(r)
                    r["price_thr"] = pt
                    r["vol_thr"] = vt
                    r["split"] = split_name
                    rows.append(r)
    return rows


def summarize(trades: dict[str, list], min_n: int, min_days: int) -> list[dict]:
    """按 (scenario, horizon) 统计，含日级聚类去相关。"""
    rows: list[dict] = []
    for scenario, recs in trades.items():
        for hz in HORIZONS:
            sub = [r for r in recs if r[0] == hz]
            if len(sub) < min_n:
                continue
            nets = [r[2] for r in sub]
            days = {r[1] for r in sub}
            if len(days) < min_days:
                continue
            wins = sum(1 for x in nets if x > 0)
            gross_win = sum(x for x in nets if x > 0)
            gross_loss = abs(sum(x for x in nets if x < 0))
            # 日级聚类
            day_means: dict = defaultdict(list)
            for r in sub:
                day_means[r[1]].append(r[2])
            dm = [sum(v) / len(v) for v in day_means.values()]
            day_mean = sum(dm) / len(dm)
            day_std = (sum((x - day_mean) ** 2 for x in dm) / (len(dm) - 1)) ** 0.5 if len(dm) > 1 else 0.0
            t_stat = day_mean / (day_std / (len(dm) ** 0.5)) if day_std > 0 else 0.0
            rows.append({
                "scenario": scenario, "horizon_h": hz, "n": len(sub),
                "days": len(dm), "win_rate": wins / len(sub),
                "avg_ret_net": sum(nets) / len(nets), "expectancy": sum(nets) / len(sub),
                "profit_factor": gross_win / gross_loss if gross_loss else float("inf"),
                "day_t_stat": t_stat, "day_avg": day_mean,
            })
    return rows


def _parse_quadrant(scenario: str) -> tuple[str, str] | None:
    """从 P×OI 场景键（如 "Pup_OIup"）解析 (p_dir, oi_dir)；BASELINE_ONLY 返回 None。"""
    if not scenario.startswith("P") or "_" not in scenario:
        return None
    p_part, oi_part = scenario.split("_", 1)
    return p_part[1:], oi_part[2:]


def _pick_row(rows: list[dict], split_name: str, label: str, hz: int) -> dict | None:
    return next((r for r in rows if r["split"] == split_name
                 and r["scenario"] == label and r["horizon_h"] == hz), None)


def summarize_cvd(trades: dict[str, list], min_n: int, min_days: int) -> list[dict]:
    """CVD 维度拆分：按设计方案 §4.3 ② 的 8 场景（S1..S8）分桶统计。

    仅统计有 CVD 方向的记录（cvd_dir 缺失 → 跳过，不进任何 S 桶）。
    统计口径与 summarize() 一致（日级聚类 t 统计）。
    """
    by_s: dict[str, list] = defaultdict(list)
    for scenario, recs in trades.items():
        pq = _parse_quadrant(scenario)
        if pq is None:
            continue
        p_dir, oi_dir = pq
        for r in recs:
            if r[8] is None:
                continue
            label = SCENARIO8.get((p_dir, oi_dir, r[8]))
            if label:
                by_s[label].append(r)
    rows: list[dict] = []
    for label in sorted(by_s):
        recs = by_s[label]
        for hz in HORIZONS:
            sub = [r for r in recs if r[0] == hz]
            if len(sub) < min_n:
                continue
            nets = [r[2] for r in sub]
            days = {r[1] for r in sub}
            if len(days) < min_days:
                continue
            wins = sum(1 for x in nets if x > 0)
            gross_win = sum(x for x in nets if x > 0)
            gross_loss = abs(sum(x for x in nets if x < 0))
            day_means: dict = defaultdict(list)
            for r in sub:
                day_means[r[1]].append(r[2])
            dm = [sum(v) / len(v) for v in day_means.values()]
            day_mean = sum(dm) / len(dm)
            day_std = (sum((x - day_mean) ** 2 for x in dm) / (len(dm) - 1)) ** 0.5 if len(dm) > 1 else 0.0
            t_stat = day_mean / (day_std / (len(dm) ** 0.5)) if day_std > 0 else 0.0
            rows.append({
                "scenario": label, "horizon_h": hz, "n": len(sub),
                "days": len(dm), "win_rate": wins / len(sub),
                "avg_ret_net": sum(nets) / len(nets), "expectancy": sum(nets) / len(sub),
                "profit_factor": gross_win / gross_loss if gross_loss else float("inf"),
                "day_t_stat": t_stat, "day_avg": day_mean,
            })
    return rows


def summarize_funding(trades: dict[str, list], min_n: int, min_days: int) -> list[dict]:
    """funding 消融：每个 (scenario, horizon, fund_tag) 分桶统计。"""
    rows: list[dict] = []
    for scenario, recs in trades.items():
        tags = {r[3] for r in recs}
        for ftag in sorted(tags):
            for hz in HORIZONS:
                sub = [r for r in recs if r[0] == hz and r[3] == ftag]
                if len(sub) < min_n:
                    continue
                nets = [r[2] for r in sub]
                days = {r[1] for r in sub}
                if len(days) < min_days:
                    continue
                wins = sum(1 for x in nets if x > 0)
                gross_win = sum(x for x in nets if x > 0)
                gross_loss = abs(sum(x for x in nets if x < 0))
                day_means: dict = defaultdict(list)
                for r in sub:
                    day_means[r[1]].append(r[2])
                dm = [sum(v) / len(v) for v in day_means.values()]
                day_mean = sum(dm) / len(dm)
                day_std = (sum((x - day_mean) ** 2 for x in dm) / (len(dm) - 1)) ** 0.5 if len(dm) > 1 else 0.0
                t_stat = day_mean / (day_std / (len(dm) ** 0.5)) if day_std > 0 else 0.0
                rows.append({
                    "scenario": scenario, "fund_tag": ftag, "horizon_h": hz, "n": len(sub),
                    "days": len(dm), "win_rate": wins / len(sub),
                    "avg_ret_net": sum(nets) / len(nets), "expectancy": sum(nets) / len(sub),
                    "profit_factor": gross_win / gross_loss if gross_loss else float("inf"),
                    "day_t_stat": t_stat, "day_avg": day_mean,
                })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="8 场景回测（1h 取价 + 成本 + 消融 + 日级聚类 + 阈值敏感性扫描）")
    parser.add_argument("--symbols", type=int, default=0, help="只测前 N 个符号")
    parser.add_argument("--cost", type=float, default=COST, help="双边手续费（默认 0.001）")
    parser.add_argument("--min-n", type=int, default=MIN_N)
    parser.add_argument("--out", type=str, default="", help="CSV 输出路径（默认 scripts/data/backtest_scan_results.csv）")
    parser.add_argument("--sweep", action="store_true",
                        help="阈值敏感性扫描模式：对价格/量比阈值网格跑 P↑OI↑，输出矩阵 CSV")
    parser.add_argument("--price-thrs", type=str, default="2.0,2.5,3.0,3.5,4.5,6.0",
                        help="扫描的价格阈值列表（逗号分隔，默认 2.0,2.5,3.0,3.5,4.5,6.0）")
    parser.add_argument("--vol-thrs", type=str, default="1.5,2.0,3.0,4.0",
                        help="扫描的量比阈值列表（逗号分隔，默认 1.5,2.0,3.0,4.0）")
    parser.add_argument("--lookback-days", type=int, default=0,
                        help="只加载最近 N 天 1h K 线（0=全量；回测建议 45：覆盖 30 天 OI + 缓冲）")
    parser.add_argument("--holdout-days", type=int, default=0,
                        help="时序切分：最近 N 天 entry 的信号为 test、其余为 train（0=关闭）。"
                             "test 只做对照，阈值/参数选择只允许看 train（§14.4）")
    args = parser.parse_args()
    out_explicit = bool(args.out)   # 是否显式给了 --out（下方会把默认值写回 args.out）

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol, COUNT(*) AS n FROM biz.asset_klines "
                "WHERE interval='1h' GROUP BY symbol HAVING COUNT(*) >= %s ORDER BY symbol",
                (MIN_KLINES_BARS,),
            )
            universe = [r[0] for r in cur.fetchall()]
        if args.symbols:
            universe = universe[: args.symbols]
        print(f"[backtest] 符号宇宙 {len(universe)}，1h 窗口 {HORIZONS}h，成本 {args.cost:.3f}")

        klines = load_klines(conn, universe, args.lookback_days)
        oi_hourly = load_oi_hourly(conn, universe)
        cvd_hourly = load_cvd_hourly(conn, universe)
        funding = load_funding(conn, universe)
        print(f"[backtest] K线 {sum(len(v) for v in klines.values())} 根；"
              f"OI 小时序列 {sum(len(v) for v in oi_hourly.values())} 点；"
              f"CVD 小时序列 {sum(len(v) for v in cvd_hourly.values())} 点；"
              f"funding 序列 {sum(len(v[0]) for v in funding.values())} 点")

        cutoff: datetime | None = None
        if args.holdout_days > 0 and klines:
            global_max_t = max(bars[-1]["t"] for bars in klines.values() if bars)
            cutoff = global_max_t - timedelta(days=args.holdout_days)
            print(f"[holdout] 时序切分：entry < {cutoff:%Y-%m-%d %H:%M} → train，"
                  f"其余（最近 {args.holdout_days} 天）→ test")
            print("[holdout] ⚠️ 纪律：test 含不同 regime 才有验证意义；"
                  "单一 regime 内的 holdout 通过 ≠ A3 关闭，test 侧结论一律 provisional")

        if args.sweep:
            pt_list = [float(x) for x in args.price_thrs.split(",") if x.strip()]
            vt_list = [float(x) for x in args.vol_thrs.split(",") if x.strip()]
            print(f"[sweep] 阈值网格 {len(pt_list)}×{len(vt_list)}={len(pt_list) * len(vt_list)} 组，"
                  f"聚焦 P↑OI↑ / P↑OI↓")
            srows = sweep_all(klines, oi_hourly, funding, args.cost,
                              pt_list, vt_list, args.min_n, MIN_DAYS, cutoff=cutoff)
            print(f"\n{'split':>6}{'价格阈值':>6}{'量比阈值':>6}{'场景':<10}{'窗口h':>5}{'n':>6}"
                  f"{'胜率':>8}{'净均收益%':>10}{'盈亏比':>8}{'日t值':>8}")
            print("-" * 84)
            for r in sorted(srows, key=lambda x: (x["split"], x["price_thr"], x["vol_thr"],
                                                  x["scenario"], x["horizon_h"])):
                print(f"{r['split']:>6}{r['price_thr']:>6.1f}{r['vol_thr']:>6.1f}{r['scenario']:<10}"
                      f"{r['horizon_h']:>5}{r['n']:>6}{r['win_rate']:>8.1%}"
                      f"{r['avg_ret_net'] * 100:>10.3f}"
                      f"{r['profit_factor'] if r['profit_factor'] != float('inf') else 999:>8.2f}"
                      f"{r['day_t_stat']:>8.2f}")
            if cutoff is not None:
                print("\n[sweep] ⚠️ 阈值选择只允许看 train 行；test 行仅对照，"
                      "样本内与 test 的差距即过拟合度量（§14.4）")
            if srows:
                out_path = Path(args.out) if args.out else \
                    SCRIPT_DIR.parent / "data" / "backtest_threshold_sweep.csv"
                out_path.parent.mkdir(parents=True, exist_ok=True)
                with open(out_path, "w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=list(srows[0].keys()))
                    w.writeheader()
                    w.writerows(srows)
                print(f"\n[sweep] 矩阵已存 {out_path}")
            return 0

        trades: dict[str, list] = defaultdict(list)
        for sym in universe:
            scan_symbol(klines.get(sym, []), oi_hourly.get(sym, {}),
                        funding.get(sym), trades, args.cost,
                        cvd_hours=cvd_hourly.get(sym, {}))

        def _print_rows(rs: list[dict]) -> None:
            print(f"{'场景':<16}{'窗口h':>5}{'n':>6}{'天数':>5}{'胜率':>8}{'净均收益%':>10}{'盈亏比':>8}{'日t值':>8}")
            print("-" * 76)
            for r in sorted(rs, key=lambda x: (x["scenario"], x["horizon_h"])):
                print(f"{r['scenario']:<16}{r['horizon_h']:>5}{r['n']:>6}{r['days']:>5}"
                      f"{r['win_rate']:>8.1%}{r['avg_ret_net'] * 100:>10.3f}"
                      f"{r['profit_factor'] if r['profit_factor'] != float('inf') else 999:>8.2f}"
                      f"{r['day_t_stat']:>8.2f}")

        csv_rows: list[dict] = []
        if cutoff is not None:
            tr, te = split_trades(trades, cutoff)
            for split_name, sub in (("train", tr), ("test", te)):
                print(f"\n=== {split_name}（{'训练集：阈值/参数选择依据' if split_name == 'train' else '测试集：仅对照，结论 provisional'}）===")
                rows = summarize(sub, args.min_n, MIN_DAYS)
                _print_rows(rows)
                for r in rows:
                    r = dict(r)
                    r["split"] = split_name
                    csv_rows.append(r)
            print("\n[holdout] ⚠️ train/test 差距即过拟合度量；"
                  "若全窗口为单一 regime，test 通过也不能外推（A1/A2，§12.1）")
        else:
            rows = summarize(trades, args.min_n, MIN_DAYS)
            _print_rows(rows)
            csv_rows = [dict(r, split="all") for r in rows]

        # CVD 维度拆分（设计方案 §4.3 ② 的 8 场景 S1..S8）
        cvd_csv_rows: list[dict] = []
        cvd_scope: list[tuple[str, dict[str, list]]] = (
            [("train", tr), ("test", te)] if cutoff is not None else [("all", trades)])
        for split_name, sub in cvd_scope:
            for r in summarize_cvd(sub, args.min_n, MIN_DAYS):
                r = dict(r)
                r["split"] = split_name
                cvd_csv_rows.append(r)
        _cvd_seen = sum(1 for rs in trades.values() for r in rs if r[8] is not None)
        _cvd_miss = sum(1 for rs in trades.values() for r in rs if r[8] is None)
        print(f"\n[cvd] 有 CVD 方向的记录（含各窗口）{_cvd_seen}，缺失 NA（不进 8 场景桶）{_cvd_miss}")
        if cvd_csv_rows:
            print("\n=== CVD 维度拆分（8 场景 S1..S8，触发根小时 CVD 净额符号；设计口径 §4.3 ②）===")
            print(f"{'split':>6}{'场景':<6}{'窗口h':>5}{'n':>6}{'天数':>5}{'胜率':>8}{'净均收益%':>10}{'盈亏比':>8}{'日t值':>8}")
            print("-" * 84)
            for r in sorted(cvd_csv_rows, key=lambda x: (x["split"], x["scenario"], x["horizon_h"])):
                print(f"{r['split']:>6}{r['scenario']:<6}{r['horizon_h']:>5}{r['n']:>6}{r['days']:>5}"
                      f"{r['win_rate']:>8.1%}{r['avg_ret_net'] * 100:>10.3f}"
                      f"{r['profit_factor'] if r['profit_factor'] != float('inf') else 999:>8.2f}"
                      f"{r['day_t_stat']:>8.2f}")
            print("\n[CVD 边际] 同一 P×OI 象限内 CVD↑ vs CVD↓ 的组内差（括号为各自 n / 日 t）：")
            for split_name in sorted({r["split"] for r in cvd_csv_rows}):
                for hz in HORIZONS:
                    s1 = _pick_row(cvd_csv_rows, split_name, "S1", hz)
                    s2 = _pick_row(cvd_csv_rows, split_name, "S2", hz)
                    s7 = _pick_row(cvd_csv_rows, split_name, "S7", hz)
                    s8 = _pick_row(cvd_csv_rows, split_name, "S8", hz)
                    if s1 and s2:
                        print(f"  [{split_name} {hz}h] S2(CVD↓)−S1(CVD↑) = "
                              f"{(s2['avg_ret_net'] - s1['avg_ret_net']) * 100:>+7.3f}%"
                              f"  (S1 n={s1['n']}/t={s1['day_t_stat']:.2f}；"
                              f"S2 n={s2['n']}/t={s2['day_t_stat']:.2f})")
                    if s7 and s8:
                        print(f"  [{split_name} {hz}h] S8(CVD↑)−S7(CVD↓) = "
                              f"{(s8['avg_ret_net'] - s7['avg_ret_net']) * 100:>+7.3f}%"
                              f"  (S7 n={s7['n']}/t={s7['day_t_stat']:.2f}；"
                              f"S8 n={s8['n']}/t={s8['day_t_stat']:.2f})")
            print("[cvd] ⚠️ 样本仅 09-16 起 14 天、单一 regime，结论一律 provisional（§12.1-A1/A2）")

        # 指标影子消融（§14.3）：RSI/%B/BBW 三分位 → 验证边际贡献，不改触发
        ablation_scope: list[tuple[str, dict[str, list]]] = (
            [("train", tr), ("test", te)] if cutoff is not None else [("all", trades)])
        irows: list[dict] = []
        for split_name, sub in ablation_scope:
            for r in summarize_indicator_buckets(sub, args.min_n):
                r = dict(r)
                r["split"] = split_name
                irows.append(r)
        if irows:
            print(f"\n=== 指标影子消融（1h 窗口，三分位分桶；影子模式：不改触发逻辑 §14.3）===")
            print(f"{'split':>6}{'指标':<12}{'分位':>5}{'切点':>18}{'n':>6}{'胜率':>8}{'净均收益%':>10}{'盈亏比':>8}")
            print("-" * 84)
            for r in sorted(irows, key=lambda x: (x["split"], x["indicator"], x["bucket"])):
                cuts = f"[{r['cut_low']}, {r['cut_high']}]"
                print(f"{r['split']:>6}{r['indicator']:<12}{r['bucket']:>5}{cuts:>18}"
                      f"{r['n']:>6}{r['win_rate']:>8.1%}{r['avg_ret_net'] * 100:>10.3f}"
                      f"{r['profit_factor'] if r['profit_factor'] != float('inf') else 999:>8.2f}")
            print("[indicators] ⚠️ 分位间收益单调/分化明显 → 该指标有边际，可进入 holdout 复核；"
                  "无分化 → 不准入告警逻辑（四条硬杠 §14.1）")

        # 消融对比：baseline vs 各 P×OI 桶（holdout 模式只看 train——选择依据）
        abl_rows = [r for r in csv_rows if r.get("split") in ("all", "train")]
        base = next((r for r in abl_rows if r["scenario"] == "BASELINE_ONLY" and r["horizon_h"] == 1), None)
        if base:
            print(f"\n=== 消融（1h 窗口，baseline 净均={base['avg_ret_net'] * 100:.3f}%）===")
            for r in abl_rows:
                if r["scenario"] == "BASELINE_ONLY":
                    continue
                delta = (r["avg_ret_net"] - base["avg_ret_net"]) * 100
                print(f"  {r['scenario']:<16} n={r['n']:<6} 净均={r['avg_ret_net'] * 100:>7.3f}%  "
                      f"Δvs基线={delta:>+7.3f}%")

        # funding 消融：按资金费率正/负/近零拆开
        frows = summarize_funding(trades, args.min_n, MIN_DAYS)
        if frows:
            print(f"\n=== funding 消融（资金费率: + 正 / - 负 / 0 近零 / NA 无数据）===")
            print(f"{'场景':<16}{'费率':>5}{'窗口h':>5}{'n':>6}{'天数':>5}{'胜率':>8}{'净均收益%':>10}{'盈亏比':>8}{'日t值':>8}")
            print("-" * 84)
            for r in sorted(frows, key=lambda x: (x["scenario"], x["horizon_h"], x["fund_tag"])):
                print(f"{r['scenario']:<16}{r['fund_tag']:>5}{r['horizon_h']:>5}{r['n']:>6}{r['days']:>5}"
                      f"{r['win_rate']:>8.1%}{r['avg_ret_net'] * 100:>10.3f}"
                      f"{r['profit_factor'] if r['profit_factor'] != float('inf') else 999:>8.2f}"
                      f"{r['day_t_stat']:>8.2f}")

        if not args.out:
            args.out = str(SCRIPT_DIR.parent / "data" / "backtest_scan_results.csv")
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # 护栏（2026-10-01）：兄弟 CSV 曾用固定名，`--out data/_a3_h14_scan.csv` 这类
        # 自定义输出仍会把 data/backtest_{indicator,funding,cvd}_*.csv 这些已 tracked 的
        # 全窗口证据（split=all）覆盖成 holdout（split=train/test）结果。显式给 `--out`
        # 时兄弟文件改用 `<out_stem>_*` 前缀；不给 `--out` 时保持既有文件名不变。
        def _sibling(base_name: str) -> Path:
            if out_explicit:
                return out_path.with_name(f"{out_path.stem}_{base_name}")
            return out_path.with_name(base_name)
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()) if csv_rows else ["scenario"])
            w.writeheader()
            w.writerows(csv_rows)
        print(f"\n[backtest] 结果已存 {args.out}")

        if irows:
            iout = _sibling("backtest_indicator_ablation.csv")
            with open(iout, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(irows[0].keys()))
                w.writeheader()
                w.writerows(irows)
            print(f"[backtest] 指标影子消融已存 {iout}")

        if frows:
            fout = _sibling("backtest_funding_ablation.csv")
            with open(fout, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(frows[0].keys()))
                w.writeheader()
                w.writerows(frows)
            print(f"[backtest] funding 消融已存 {fout}")

        if cvd_csv_rows:
            cout = _sibling("backtest_cvd_scenarios.csv")
            with open(cout, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(cvd_csv_rows[0].keys()))
                w.writeheader()
                w.writerows(cvd_csv_rows)
            print(f"[backtest] CVD 8 场景拆分已存 {cout}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
