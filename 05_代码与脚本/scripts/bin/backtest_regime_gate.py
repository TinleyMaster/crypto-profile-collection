#!/usr/bin/env python3
"""盘面扫描 regime 闸门因果性验证（工单 SCAN-REGIME-GATE-001，2026-09-29）。

背景
----
阈值 sweep holdout 双侧矩阵（commit 34a4297）实证 P↑OI↑ 24/24 组 test 侧全负、
样本内选参反向（train 最优 = test 最差）⇒ 固定 % 阈值标定死局，出路指向 regime 闸门。
本脚本验证规则 B 依赖的 `regime_label` 是否具备**信号级**因果预测力。

做什么（全流程只读 prod，只 SELECT；产物只落本地 CSV，不写库、不改线上代码）
----------------------------------------------------------------------------
1. **标签重算**：从 `biz.asset_klines`（BTCUSDT，1h）逐日复刻
   `build_scan_edge_report.classify_regime` 的 regime 标签（trend/range/mixed），
   含 2026-09-23 修正的 trend 第二分支（单边涨跌日）。
2. **标签一致性核对**：重算标签与 `biz.scan_edge_daily.regime_label` 重叠日逐日比对，
   不一致即 FAIL（回归护栏：证明复刻是逐字口径）。
3. **信号级验证**：复用 `backtest_scan_scenarios.scan_symbol` 的信号机制，按
   「信号入场日（上海日）regime」分组，输出 regime × 场景 × 持有期收益矩阵
   （train/test 双侧），CSV 落 `scripts/data/backtest_regime_gate.csv`。
4. **预登记判据**（工单 §1，防事后挑数）：见 `judge_regime_gate`。

用法
----
    python backtest_regime_gate.py --lookback-days 45 --holdout-days 10
    python backtest_regime_gate.py --consistency-only        # 只做标签一致性核对
    python backtest_regime_gate.py --self-test               # 离线注入测试（沙盒合成数据）

纪律
----
所有结论一律标 `provisional_单regime`；45 天窗内 trend 占比 >80% 时标签分辨力不足，
结论降级为「不可判定」（工单 §1）。本脚本不触发任何线上行为变更。
"""
from __future__ import annotations

import argparse
import csv
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg.rows  # noqa: E402

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

import backtest_scan_scenarios as bss  # noqa: E402 - 复用 scan_symbol / load_* / split_trades

SH = timezone(timedelta(hours=8))

# 预登记判据参数（工单 §1）
TREND_SHARE_MAX = 0.80   # trend 占比上限：超过则标签分辨力不足，结论降级「不可判定」
PF_GAP_MIN = 0.30        # test 侧 PF 差门槛：非 trend 更差且差距 ≥ 该值才算「有因果价值」
PF_CAP = 999.0           # 无亏损组 PF 展示上限（避免 CSV 出现 inf）
PRIMARY_SCENARIO = "Pup_OIup"   # 规则 B 对应的主线场景
PRIMARY_HORIZON = 1      # 主判据窗口（与规则 B 的 roll3_pf_1h 同口径）

# ── 复刻 build_scan_edge_report 的 BTC 当日行情口径（逐字一致；改动须两侧同步）──
BTC_DAY_SQL = """
SELECT open_time, high_px, low_px, close_px
  FROM biz.asset_klines
 WHERE symbol = 'BTCUSDT' AND interval = '1h'
   AND open_time >= %s AND open_time < %s
 ORDER BY open_time
"""


def classify_regime(btc_amp_pct: float | None, gt05_ratio: float | None,
                    btc_chg_pct: float | None = None) -> str:
    """行情环境：trend（趋势）/ range（横盘低波动）/ mixed。

    逐字复刻 `build_scan_edge_report.classify_regime`（含 2026-09-23 修正的 trend
    第二分支：单边涨跌日——振幅够大但「小时|涨跌|>0.5% 占比」偏低，靠 |日涨跌|≥2%
    兜住）。同步副本；任何一侧改动必须两边同改，否则一致性核对会 FAIL。
    """
    if btc_amp_pct is None or gt05_ratio is None:
        return "mixed"
    if btc_amp_pct >= 4.0 and (gt05_ratio >= 0.35
                               or (btc_chg_pct is not None and abs(btc_chg_pct) >= 2.0)):
        return "trend"
    if btc_amp_pct < 3.0 and gt05_ratio < 0.20:
        return "range"
    return "mixed"


def btc_metrics_from_bars(rows: list[dict]) -> dict:
    """从 [start,end) 窗口内的 BTC 1h 棒算当日行情指标（复刻 btc_day_metrics 算术）。"""
    if len(rows) < 2:
        return {"btc_close": None, "btc_chg_pct": None, "btc_amp_pct": None,
                "btc_1h_gt05_ratio": None}
    closes = [float(r["close_px"]) for r in rows]
    highs = [float(r["high_px"]) for r in rows]
    lows = [float(r["low_px"]) for r in rows]
    rets = [abs(closes[i] / closes[i - 1] - 1) * 100 for i in range(1, len(closes))]
    return {
        "btc_close": closes[-1],
        "btc_chg_pct": (closes[-1] / closes[0] - 1) * 100,
        "btc_amp_pct": (max(highs) - min(lows)) / min(lows) * 100 if min(lows) else None,
        "btc_1h_gt05_ratio": sum(1 for r in rets if r > 0.5) / len(rets) if rets else None,
    }


def btc_day_metrics(conn, d: date) -> dict:
    """单日 BTC 行情（走 BTC_DAY_SQL，逐字复刻 build_scan_edge_report 的窗口）。"""
    start = datetime.combine(d, datetime.min.time(), tzinfo=SH) - timedelta(hours=1)
    end = start + timedelta(hours=25)
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(BTC_DAY_SQL, (start, end))
        rows = cur.fetchall()
    return btc_metrics_from_bars(rows)


def compute_regime_labels(conn, d_min: date, d_max: date) -> dict[date, str]:
    """[d_min, d_max] 逐日重算 regime 标签（一次批量取 BTC 1h，按窗口切片，等价单日查询）。"""
    fetch_start = datetime.combine(d_min, datetime.min.time(), tzinfo=SH) - timedelta(hours=1)
    fetch_end = datetime.combine(d_max, datetime.min.time(), tzinfo=SH) + timedelta(hours=24)
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(BTC_DAY_SQL, (fetch_start, fetch_end))
        rows = cur.fetchall()
    labels: dict[date, str] = {}
    d = d_min
    while d <= d_max:
        start = datetime.combine(d, datetime.min.time(), tzinfo=SH) - timedelta(hours=1)
        end = start + timedelta(hours=25)
        sub = [r for r in rows if start <= r["open_time"] < end]
        env = btc_metrics_from_bars(sub)
        labels[d] = classify_regime(env["btc_amp_pct"], env["btc_1h_gt05_ratio"],
                                    env["btc_chg_pct"])
        d += timedelta(days=1)
    return labels


def check_label_consistency(conn, labels: dict[date, str]) -> list[tuple]:
    """重算标签 vs `biz.scan_edge_daily.regime_label` 重叠日逐日比对，返回不一致清单。"""
    with conn.cursor() as cur:
        cur.execute("SELECT report_date, regime_label FROM biz.scan_edge_daily "
                    "ORDER BY report_date")
        rows = cur.fetchall()
    mism: list[tuple] = []
    n_overlap = 0
    for report_date, real in rows:
        mine = labels.get(report_date)
        if mine is None:
            continue
        n_overlap += 1
        if mine != real:
            mism.append((report_date, real, mine))
    print(f"[consistency] scan_edge_daily {len(rows)} 行，重叠 {n_overlap} 日，"
          f"不一致 {len(mism)} 日")
    for report_date, real, mine in mism:
        print(f"  ✗ {report_date}  库={real}  重算={mine}")
    return mism


# ──────────────────────────── 分组统计 ────────────────────────────

def _entry_sh_date(rec) -> date:
    """信号入场时间（记录第 5 位）转上海日 —— 与 regime 标签同一日界。"""
    return rec[4].astimezone(SH).date()


def _pf(nets: list[float]) -> float | None:
    """盈亏比；无亏损样本返回 None（≠ 0）。"""
    gl = abs(sum(x for x in nets if x < 0))
    if gl <= 0:
        return None
    return sum(x for x in nets if x > 0) / gl


def _pf_disp(nets: list[float]) -> float:
    """CSV 展示用 PF：无亏损 → PF_CAP，其余保留 4 位。"""
    pf = _pf(nets)
    return PF_CAP if pf is None else round(pf, 4)


def _day_means(recs: list) -> list[float]:
    dm: dict = defaultdict(list)
    for r in recs:
        dm[_entry_sh_date(r)].append(r[2])
    return [sum(v) / len(v) for v in dm.values()]


def _welch_t(a: list[float], b: list[float]) -> float | None:
    """两组日级均值差的 Welch t（样本不足返回 None）。"""
    if len(a) < 2 or len(b) < 2:
        return None
    va = statistics.variance(a) / len(a)
    vb = statistics.variance(b) / len(b)
    if va + vb <= 0:
        return None
    return (statistics.fmean(a) - statistics.fmean(b)) / (va + vb) ** 0.5


def summarize_regime(trades: dict[str, list], labels: dict[date, str], split_name: str,
                     min_n: int, min_days: int) -> list[dict]:
    """按 (regime, scenario, horizon) 分组统计（日级聚类 t 值 + 样本不足标记）。"""
    groups: dict[tuple, list] = defaultdict(list)
    for scenario, recs in trades.items():
        for r in recs:
            reg = labels.get(_entry_sh_date(r), "unknown")
            groups[(reg, scenario, r[0])].append(r)
    rows: list[dict] = []
    for (reg, scenario, hz), sub in sorted(groups.items(), key=lambda kv: str(kv[0])):
        nets = [r[2] for r in sub]
        dm = _day_means(sub)
        day_mean = statistics.fmean(dm)
        day_std = statistics.stdev(dm) if len(dm) > 1 else 0.0
        t_stat = day_mean / (day_std / len(dm) ** 0.5) if day_std > 0 else 0.0
        rows.append({
            "split": split_name, "regime": reg, "scenario": scenario, "horizon_h": hz,
            "n": len(nets), "days": len(dm),
            "win_rate": round(sum(1 for x in nets if x > 0) / len(nets), 4),
            "avg_ret_net": round(statistics.fmean(nets), 6),
            "profit_factor": _pf_disp(nets),
            "day_t_stat": round(t_stat, 4),
            "insufficient": int(len(nets) < min_n or len(dm) < min_days),
        })
    return rows


def _select(trades: dict[str, list], labels: dict[date, str], group: str,
            scenario: str, hz: int) -> list:
    recs = []
    for r in trades.get(scenario, []):
        if r[0] != hz:
            continue
        reg = labels.get(_entry_sh_date(r))
        if group == "trend" and reg == "trend":
            recs.append(r)
        elif group == "non_trend" and reg in ("range", "mixed"):
            recs.append(r)
    return recs


def judge_regime_gate(train_trades: dict[str, list], test_trades: dict[str, list],
                      labels: dict[date, str], horizon: int = PRIMARY_HORIZON) -> dict:
    """预登记判据（工单 §1）：regime 闸门是否有信号级因果预测力。

    判据：非 trend 下 P↑OI↑ 的 PF train/test 双侧一致更差 **且** test 侧 PF 差 ≥0.3
    → 有因果价值；否则标签只配当日报描述语；trend 占比 >80% → 不可判定。
    """
    n_days = len(labels) or 1
    trend_share = sum(1 for v in labels.values() if v == "trend") / n_days
    out: dict = {"trend_share": round(trend_share, 4), "horizon_h": horizon,
                 "provisional": "provisional_单regime"}
    if trend_share > TREND_SHARE_MAX:
        out["verdict"] = "inconclusive"
        out["reason"] = (f"trend 占比 {trend_share:.1%} > {TREND_SHARE_MAX:.0%}，"
                         "regime 分布单一 ⇒ 标签分辨力不足，结论降级为「不可判定」")
        return out

    def one(split_trades: dict[str, list], tag: str) -> dict:
        tr = _select(split_trades, labels, "trend", PRIMARY_SCENARIO, horizon)
        nt = _select(split_trades, labels, "non_trend", PRIMARY_SCENARIO, horizon)
        return {
            f"{tag}_trend_pf": _pf([r[2] for r in tr]),
            f"{tag}_trend_n": len(tr),
            f"{tag}_nontrend_pf": _pf([r[2] for r in nt]),
            f"{tag}_nontrend_n": len(nt),
            f"{tag}_t": _welch_t(_day_means(tr), _day_means(nt)),
        }

    info: dict = {}
    info.update(one(train_trades, "train"))
    info.update(one(test_trades, "test"))
    out.update(info)

    pf = [info["train_trend_pf"], info["train_nontrend_pf"],
          info["test_trend_pf"], info["test_nontrend_pf"]]
    if any(v is None for v in pf):
        out["verdict"] = "inconclusive"
        out["reason"] = "train/test 任一侧缺样本（PF 不可算）⇒ 不可判定"
        return out

    consistent = (info["train_nontrend_pf"] < info["train_trend_pf"]
                  and info["test_nontrend_pf"] < info["test_trend_pf"])
    gap = info["test_trend_pf"] - info["test_nontrend_pf"]
    out["consistent"] = consistent
    out["test_pf_gap"] = round(gap, 4)
    if consistent and gap >= PF_GAP_MIN:
        out["verdict"] = "gate_has_value"
        out["reason"] = (f"非 trend 下 P↑OI↑ PF 双侧一致更差，test 侧差 {gap:+.2f} "
                         f"≥ {PF_GAP_MIN} ⇒ regime 闸门有因果价值（provisional_单regime）")
    else:
        out["verdict"] = "descriptive_only"
        out["reason"] = (f"各 regime 下 P↑OI↑ PF 无稳定差异（双侧一致={consistent}、"
                         f"test PF 差 {gap:+.2f}）⇒ 标签只配当日报描述语，"
                         "不得用于任何降权/暂停动作")
    return out


# ──────────────────────────── 离线注入测试 ────────────────────────────

def _synth_bars(closes: list[float], low: float, high: float) -> list[dict]:
    """合成 K 线（只验算术，不追求 OHLC 自洽）。"""
    return [{"close_px": c, "low_px": low, "high_px": high} for c in closes]


def self_test() -> int:
    """通道③ 注入测试：合成三类日 + 缺数据（纯离线，不连库）。"""
    checks: list[tuple[str, bool]] = []

    def chk(name: str, cond: bool) -> None:
        checks.append((name, bool(cond)))

    # trend：振幅 5.0%、占比 10/24≈41.7%
    c_trend = [100.0]
    for s in range(1, 25):
        c_trend.append(c_trend[-1] * (1.01 if s <= 10 else 1.0))
    env = btc_metrics_from_bars(_synth_bars(c_trend, 100.0, 105.0))
    chk("trend 振幅=5.0", abs(env["btc_amp_pct"] - 5.0) < 1e-9)
    chk("trend 占比=10/24", abs(env["btc_1h_gt05_ratio"] - 10 / 24) < 1e-9)
    chk("trend 标签", classify_regime(env["btc_amp_pct"], env["btc_1h_gt05_ratio"],
                                      env["btc_chg_pct"]) == "trend")

    # range：振幅 2.0%、占比 3/24=12.5%
    c_range = [100.0]
    for s in range(1, 25):
        c_range.append(c_range[-1] * (1.01 if s in (3, 10, 17) else 1.0))
    env = btc_metrics_from_bars(_synth_bars(c_range, 100.0, 102.0))
    chk("range 振幅=2.0", abs(env["btc_amp_pct"] - 2.0) < 1e-9)
    chk("range 占比=3/24", abs(env["btc_1h_gt05_ratio"] - 3 / 24) < 1e-9)
    chk("range 标签", classify_regime(env["btc_amp_pct"], env["btc_1h_gt05_ratio"],
                                      env["btc_chg_pct"]) == "range")

    # mixed：振幅 3.5%、占比 25%、|日涨跌|<2%（涨 3% 再跌回）
    c_mixed = [100.0]
    for s in range(1, 25):
        if s <= 3:
            c_mixed.append(c_mixed[-1] * 1.01)
        elif s <= 6:
            c_mixed.append(c_mixed[-1] * 0.99)
        else:
            c_mixed.append(c_mixed[-1])
    env = btc_metrics_from_bars(_synth_bars(c_mixed, 100.0, 103.5))
    chk("mixed 振幅=3.5", abs(env["btc_amp_pct"] - 3.5) < 1e-9)
    chk("mixed 占比=6/24", abs(env["btc_1h_gt05_ratio"] - 6 / 24) < 1e-9)
    chk("mixed 标签", classify_regime(env["btc_amp_pct"], env["btc_1h_gt05_ratio"],
                                      env["btc_chg_pct"]) == "mixed")

    # 2026-09-23 单边跌日（第二分支）
    chk("单边涨跌日走第二分支", classify_regime(4.0966, 0.1667, -2.786) == "trend")
    chk("缺 chg 时第二分支不可用", classify_regime(4.0966, 0.1667, None) == "mixed")

    # 缺数据兜底
    env_empty = btc_metrics_from_bars([])
    chk("空序列→指标全 None", env_empty["btc_amp_pct"] is None
        and env_empty["btc_1h_gt05_ratio"] is None)
    chk("空序列→mixed", classify_regime(env_empty["btc_amp_pct"],
                                        env_empty["btc_1h_gt05_ratio"],
                                        env_empty["btc_chg_pct"]) == "mixed")
    chk("单棒→缺数据→mixed", btc_metrics_from_bars([{"close_px": 1}])["btc_amp_pct"] is None)
    chk("amp=None→mixed", classify_regime(None, 0.1, 0.0) == "mixed")
    chk("ratio=None→mixed", classify_regime(5.0, None, 0.0) == "mixed")

    # 边界：振幅恰好 4.0 且占比 0.35 → trend；恰好 3.0/0.20 → mixed
    chk("边界 amp=4.0/ratio=0.35→trend", classify_regime(4.0, 0.35, 0.0) == "trend")
    chk("边界 amp=3.0/ratio=0.20→mixed", classify_regime(3.0, 0.20, 0.0) == "mixed")

    failed = [n for n, ok in checks if not ok]
    for n, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {n}")
    print(f"[self-test] {len(checks) - len(failed)}/{len(checks)} 通过")
    return 0 if not failed else 1


# ──────────────────────────── 主流程 ────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="regime 闸门因果性验证（只读分析）")
    parser.add_argument("--lookback-days", type=int, default=45,
                        help="只加载最近 N 天 1h K 线（默认 45：覆盖 30 天 OI + 缓冲）")
    parser.add_argument("--holdout-days", type=int, default=10,
                        help="时序切分：最近 N 天 entry 为 test、其余为 train（0=关闭）")
    parser.add_argument("--symbols", type=int, default=0, help="只测前 N 个符号（0=全量）")
    parser.add_argument("--min-n", type=int, default=bss.MIN_N, help="桶最小样本（判定用）")
    parser.add_argument("--out", type=str, default="", help="CSV 输出路径")
    parser.add_argument("--consistency-only", action="store_true", help="只做标签一致性核对")
    parser.add_argument("--self-test", action="store_true", help="离线注入测试（不连库）")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        # ① 标签重算范围 = scan_edge_daily 的 report_date 区间
        with conn.cursor() as cur:
            cur.execute("SELECT MIN(report_date), MAX(report_date) FROM biz.scan_edge_daily")
            rng = cur.fetchone()
        if not rng or rng[0] is None:
            print("[error] biz.scan_edge_daily 为空，无法做标签一致性核对")
            return 1
        d_min, d_max = rng
        labels = compute_regime_labels(conn, d_min, d_max)
        mism = check_label_consistency(conn, labels)
        if mism:
            print("[consistency] FAIL：重算标签与库内标签不一致（复刻口径有偏差）")
            return 1
        print("[consistency] OK：重叠日标签全部一致")
        if args.consistency_only:
            return 0

        # ② 信号回测（复用 backtest_scan_scenarios 的信号机制）
        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol, COUNT(*) AS n FROM biz.asset_klines "
                "WHERE interval='1h' GROUP BY symbol HAVING COUNT(*) >= %s ORDER BY symbol",
                (bss.MIN_KLINES_BARS,))
            universe = [r[0] for r in cur.fetchall()]
        if args.symbols:
            universe = universe[: args.symbols]
        print(f"[backtest] 符号宇宙 {len(universe)}，1h 窗口 {bss.HORIZONS}h，"
              f"成本 {bss.COST:.3f}，lookback {args.lookback_days}d")

        klines = bss.load_klines(conn, universe, args.lookback_days)
        oi_hourly = bss.load_oi_hourly(conn, universe)
        funding = bss.load_funding(conn, universe)
        print(f"[backtest] K线 {sum(len(v) for v in klines.values())} 根；"
              f"OI 小时序列 {sum(len(v) for v in oi_hourly.values())} 点；"
              f"funding {sum(len(v[0]) for v in funding.values())} 点")

        # 标签需覆盖信号入场日：补齐 lookback 窗口内的日期
        if klines:
            t_min = min(bars[0]["t"].astimezone(SH).date()
                        for bars in klines.values() if bars)
            t_max = max(bars[-1]["t"].astimezone(SH).date()
                        for bars in klines.values() if bars)
            labels.update(compute_regime_labels(conn, t_min, t_max))

        trades: dict[str, list] = defaultdict(list)
        for sym in universe:
            bss.scan_symbol(klines.get(sym, []), oi_hourly.get(sym, {}),
                            funding.get(sym), trades, bss.COST)

        cutoff: datetime | None = None
        if args.holdout_days > 0 and klines:
            global_max_t = max(bars[-1]["t"] for bars in klines.values() if bars)
            cutoff = global_max_t - timedelta(days=args.holdout_days)
            print(f"[holdout] entry < {cutoff:%Y-%m-%d %H:%M} → train，"
                  f"其余（最近 {args.holdout_days} 天）→ test")

        if cutoff is not None:
            train, test = bss.split_trades(trades, cutoff)
            splits = [("train", train), ("test", test)]
        else:
            splits = [("all", trades)]

        matrix: list[dict] = []
        for split_name, sub in splits:
            rows = summarize_regime(sub, labels, split_name, args.min_n, bss.MIN_DAYS)
            matrix.extend(rows)
            print(f"\n=== {split_name} · regime × 场景 × 持有期 ===")
            print(f"{'regime':<9}{'场景':<14}{'h':>3}{'n':>6}{'天':>5}"
                  f"{'胜率':>8}{'净均%':>9}{'PF':>8}{'日t':>7}{'样本不足':>9}")
            print("-" * 74)
            for r in rows:
                print(f"{r['regime']:<9}{r['scenario']:<14}{r['horizon_h']:>3}{r['n']:>6}"
                      f"{r['days']:>5}{r['win_rate']:>8.1%}{r['avg_ret_net'] * 100:>9.3f}"
                      f"{r['profit_factor']:>8.2f}{r['day_t_stat']:>7.2f}"
                      f"{'⚠' if r['insufficient'] else '':>9}")

        verdict = judge_regime_gate(
            train if cutoff is not None else trades,
            test if cutoff is not None else trades, labels)
        print("\n=== 预登记判据（工单 §1，防事后挑数）===")
        print(f"  trend 占比 {verdict['trend_share']:.1%}  主判据 {PRIMARY_SCENARIO} × "
              f"T+{verdict['horizon_h']}h")
        if "train_trend_pf" in verdict:
            print(f"  train：trend PF {_fmt_pf(verdict['train_trend_pf'])} (n={verdict['train_trend_n']}) "
                  f"vs 非 trend PF {_fmt_pf(verdict['train_nontrend_pf'])} "
                  f"(n={verdict['train_nontrend_n']}, t={_fmt_t(verdict['train_t'])})")
            print(f"  test ：trend PF {_fmt_pf(verdict['test_trend_pf'])} (n={verdict['test_trend_n']}) "
                  f"vs 非 trend PF {_fmt_pf(verdict['test_nontrend_pf'])} "
                  f"(n={verdict['test_nontrend_n']}, t={_fmt_t(verdict['test_t'])})")
        print(f"  结论【{verdict['verdict']}】：{verdict['reason']}")
        print(f"  ⚠️ {verdict['provisional']}：本脚本仅回答「日级闸门是否有戏」，"
              "不构成线上降权/暂停依据")

        out_path = Path(args.out) if args.out else \
            SCRIPT_DIR.parent / "data" / "backtest_regime_gate.csv"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fields = ["split", "regime", "scenario", "horizon_h", "n", "days", "win_rate",
                  "avg_ret_net", "profit_factor", "day_t_stat", "insufficient"]
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(matrix)
        print(f"\n[backtest] 矩阵已存 {out_path}")
    return 0


def _fmt_pf(v) -> str:
    return "—" if v is None else f"{v:.2f}"


def _fmt_t(v) -> str:
    return "—" if v is None else f"{v:+.2f}"


if __name__ == "__main__":
    sys.exit(main())
