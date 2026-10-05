#!/usr/bin/env python3
"""币安涨幅榜冲高回落回测（贴近币安交易口径，含资金费率拥挤度交叉）。

背景（2026-10-03）：用户主要用币安交易，希望结论贴近币安。CMC 官方涨幅榜为空表
（付费套餐受限），币安亦无历史榜单接口 —— 本脚本用项目已采集的 Binance USDT
永续 1h K 线（biz.asset_klines，529 合约，1h 回溯至 2026-06-18）**自行重构**
币安 24h 涨幅榜历史，并做冲高回落回测 + 资金费率拥挤度交叉。

A · 主回测：
  - 数据：biz.asset_klines interval='1h'，每日取最后一根 bar 收盘为日收盘口径
  - 信号：chg24 = close_d / close_{d-1} - 1 ≥ 5%（分桶），同日 24h 涨幅榜语义
  - 后续：1/3/7/14 日收盘收益；判据「下跌概率 > 50%」（与 CMC 版一致）
  - 附加：按日成交额分档（流动性）、阈值扫描 + 临界涨幅
C · 资金费率拥挤度交叉：
  - 信号日当天最后结算点资金费率（biz.funding_rate_hist，191 币子集）
  - 分档 正高/正/近零/负 × 涨幅桶 → 后续表现（验证「高涨幅+高正费率=拥挤多头更差」）

⚠️ 与 CMC 快照版的差异（务必区分）：
  - 本版是**币安永续合约**（529 个，偏中大市值/有流动性），**不含 CMC 那种 $50M 以下
    极端小市值** ⇒ 结论与 CMC 小市值版不可混用，是「币安真实可成交」视角的补充。
  - 永续价格含资金费率扰动；1h 历史仅回溯 2026-06-18（~3.5 个月，日口径 ~100 天）。

用法：
    python backtest_binance_gainers.py
    python backtest_binance_gainers.py --min-gain 10
    python backtest_binance_gainers.py --min-n 30
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

MIN_GAIN = 5.0                  # 信号最小 24h 涨幅（%）
HORIZONS = (1, 3, 7, 14)        # 后续观察窗口（日）
BUCKETS = ((5, 10), (10, 20), (20, 30), (30, 50), (50, 1e9))
THRESH_GRID = (10, 20, 30, 50, 75, 100)
MIN_N = 20                      # 统计最少样本
MIN_DAYS = 10                   # 最少独立信号日
DECLINE_CRIT = 0.50
FUND_POS_HI = 0.0005            # 高正费率阈值（5bp/结算点）
FUND_POS = 0.0001               # 正/近零分界（1bp）

# 服务端回测 SQL：每条信号一行回传（chg/vol/rate + 各窗口收益 + peak/trough/first_neg）。
#   daily = 每 (symbol, 日) 最后一根 1h bar 收盘（日收盘口径）
#   sig   = chg24 ≥ min_gain，且含完整 14 日前向窗口
#   fund  = 信号日当天最后结算点资金费率（子集 191 币，缺则 rate 为 NULL）
SQL_BACKTEST = """
WITH daily AS (
    SELECT DISTINCT ON (symbol, DATE(open_time))
           symbol, DATE(open_time) AS d, close_px, quote_vol
    FROM biz.asset_klines
    WHERE interval = '1h' AND close_px > 0
    ORDER BY symbol, DATE(open_time), open_time DESC
),
sig AS (
    SELECT s.symbol, s.d, s.close_px AS entry, s.quote_vol AS vol,
           s.close_px / p.close_px - 1 AS chg
    FROM daily s
    JOIN daily p ON p.symbol = s.symbol AND p.d = s.d - 1
    WHERE s.close_px / p.close_px - 1 >= %s
      AND s.d <= (SELECT MAX(d) FROM daily) - 14
),
fwd AS (
    SELECT s.symbol, s.d AS sig_d, s.entry, s.chg, s.vol,
           fk.close_px / s.entry - 1 AS ret, (fk.d - s.d) AS k
    FROM sig s JOIN daily fk ON fk.symbol = s.symbol AND fk.d > s.d AND fk.d <= s.d + 14
),
r AS (
    SELECT symbol, sig_d,
           MAX(ret) FILTER (WHERE k = 1) AS r1,
           MAX(ret) FILTER (WHERE k = 3) AS r3,
           MAX(ret) FILTER (WHERE k = 7) AS r7,
           MAX(ret) FILTER (WHERE k = 14) AS r14
    FROM fwd GROUP BY symbol, sig_d
),
peak AS (
    SELECT DISTINCT ON (symbol, sig_d) symbol, sig_d, k AS peak_day, ret AS peak
    FROM fwd ORDER BY symbol, sig_d, ret DESC, k
),
trough AS (
    SELECT DISTINCT ON (symbol, sig_d) symbol, sig_d, k AS trough_day, ret AS trough
    FROM fwd ORDER BY symbol, sig_d, ret ASC, k
),
firstneg AS (
    SELECT symbol, sig_d, MIN(k) AS first_neg_day
    FROM fwd WHERE ret < 0 GROUP BY symbol, sig_d
),
fund AS (
    SELECT DISTINCT ON (symbol, DATE(funding_time))
           symbol, DATE(funding_time) AS fd, rate
    FROM biz.funding_rate_hist
    ORDER BY symbol, DATE(funding_time), funding_time DESC
)
SELECT s.symbol, s.d AS sig_d, s.chg, s.vol,
       f.rate AS fund_rate,
       r.r1, r.r3, r.r7, r.r14,
       p.peak, p.peak_day, t.trough, t.trough_day,
       fn.first_neg_day
FROM sig s
JOIN r ON r.symbol = s.symbol AND r.sig_d = s.d
JOIN peak p ON p.symbol = s.symbol AND p.sig_d = s.d
JOIN trough t ON t.symbol = s.symbol AND t.sig_d = s.d
LEFT JOIN firstneg fn ON fn.symbol = s.symbol AND fn.sig_d = s.d
LEFT JOIN fund f ON f.symbol = s.symbol AND f.fd = s.d
"""


def _med(vals: list) -> float | None:
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    vals.sort()
    return vals[len(vals) // 2]


def _pct(vals: list, p: float) -> float | None:
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    vals.sort()
    return vals[min(len(vals) - 1, int(len(vals) * p))]


def _bucket_of(chg: float) -> tuple:
    for b in BUCKETS:
        if b[0] <= chg < b[1]:
            return b
    return BUCKETS[-1]


def _fund_tag(rate: float | None) -> str:
    if rate is None:
        return "NA"
    if rate > FUND_POS_HI:
        return "正高"
    if rate > FUND_POS:
        return "正"
    if rate < -FUND_POS:
        return "负"
    return "近零"


def _slice_stats(sub: list, label: str, min_n: int) -> dict:
    n = len(sub)
    out = {"label": label, "n": n}
    for hz, key in ((1, "r1"), (3, "r3"), (7, "r7"), (14, "r14")):
        rets = [s[key] for s in sub if s[key] is not None]
        if len(rets) >= min_n:
            out[f"pdn{hz}"] = sum(1 for x in rets if x < 0) / len(rets)
            out[f"med{hz}"] = _med(rets)
        else:
            out[f"pdn{hz}"] = out[f"med{hz}"] = None
    pe = [s["peak"] for s in sub if s["peak"] is not None]
    pd_ = [s["peak_day"] for s in sub if s["peak_day"] is not None]
    nv = [s["first_neg_day"] for s in sub if s["first_neg_day"] is not None]
    out["med_peak"] = _med(pe)
    out["med_peak_day"] = _med(pd_)
    out["never_neg"] = 1 - len(nv) / n if n else None
    return out


def _print_slice(rows: list[dict], title: str, cols: list[str],
                 col_head: list[str], raw_cols: tuple = ()) -> None:
    print(f"\n=== {title} ===")
    print(f"{'分组':>12}" + "".join(f"{h:>10}" for h in col_head))
    print("-" * (12 + 10 * len(col_head)))
    for r in rows:
        line = f"{r['label']:>12}"
        for c in cols:
            v = r.get(c)
            if v is None:
                line += f"{float('nan'):>10.1f}"
            elif c in raw_cols:
                line += f"{v:>10.1f}"
            else:
                line += f"{v * 100:>10.1f}"
        print(line)


def _short_stats(sub: list, min_n: int) -> dict:
    """做空视角统计。做空 1x 于信号日收盘、N 日后平仓：
      短胜率 = P(价格下跌) = 跌概率；中位/平均做空收益 = -中位/平均 forward 收益；
      右尾风险 = 14 日内 peak ≥ +30%/+50%/+100% 的概率（做空入场后被继续拉爆的风险）。
    ⚠️ 平均做空收益被右尾拖累（少数继续暴涨的币让空头巨亏），判据用胜率+中位+右尾风险。"""
    n = len(sub)
    out = {"label": "", "n": n}
    for hz, key in ((1, "r1"), (3, "r3"), (7, "r7"), (14, "r14")):
        rets = [s[key] for s in sub if s[key] is not None]
        if len(rets) < min_n:
            out[f"sw{hz}"] = out[f"smed{hz}"] = out[f"smean{hz}"] = None
            continue
        wins = [x for x in rets if x < 0]
        losses = [x for x in rets if x >= 0]
        out[f"sw{hz}"] = len(wins) / len(rets)
        out[f"smed{hz}"] = -_med(rets)
        out[f"smean{hz}"] = -sum(rets) / len(rets)
        if hz == 7 and wins and losses:
            avg_win = sum(-x for x in wins) / len(wins)
            avg_loss = sum(x for x in losses) / len(losses)
            out["spf7"] = avg_win / avg_loss if avg_loss else None
    pe = [s["peak"] for s in sub if s["peak"] is not None]
    for thr, k in ((0.30, "tail30"), (0.50, "tail50"), (1.00, "tail100")):
        out[k] = (sum(1 for x in pe if x >= thr) / len(pe)) if pe else None
    return out


def run_short(sigs: list[dict], args) -> int:
    """做空视角回测：多高阈值做空胜率大？含右尾爆仓风险与严格组合。"""
    def _print(rows: list[dict], title: str, cols: list[str], heads: list[str],
               raw_cols: tuple = ()) -> None:
        print(f"\n=== {title} ===")
        print(f"{'分组':>12}" + "".join(f"{h:>10}" for h in heads))
        print("-" * (12 + 10 * len(heads)))
        for r in rows:
            line = f"{r['label']:>12}"
            for c in cols:
                v = r.get(c)
                if v is None:
                    line += f"{float('nan'):>10.1f}"
                elif c in raw_cols:
                    line += f"{v:>10.1f}"
                else:
                    line += f"{v * 100:>10.1f}"
            print(line)

    print(f"\n[short] 做空口径：信号日收盘做空 1x，D+1/3/7/14 平仓（毛收益，未含滑点/资金费率）")
    # ① 分桶
    rows = []
    by_bucket = {}
    for s in sigs:
        by_bucket.setdefault(_bucket_of(s["chg"]), []).append(s)
    for b in BUCKETS:
        sub = by_bucket.get(b, [])
        if len(sub) >= args.min_n:
            st = _short_stats(sub, args.min_n)
            st["label"] = f"+{b[0]:.0f}~{int(b[1]) if b[1] < 1e9 else 999}%"
            rows.append(st)
    _print(rows, "做空视角 · 分桶（短胜率% / 中位做空收益%，窗口 1/3/7/14d；"
                 "右尾=14日内继续涨≥30%/50%概率）",
           ["sw1", "sw3", "sw7", "sw14", "smed7", "smed14", "tail30", "tail50"],
           ["胜1d", "胜3d", "胜7d", "胜14d", "中位空7d", "中位空14d", "涨≥30%", "涨≥50%"])

    # ② 阈值扫描
    sweep = []
    for thr in (10, 15, 20, 25, 30, 40, 50, 75, 100):
        sub = [s for s in sigs if s["chg"] >= thr]
        if len(sub) >= args.min_n:
            st = _short_stats(sub, args.min_n)
            st["label"] = f">={thr}%"
            sweep.append(st)
    _print(sweep, "做空视角 · 阈值扫描（累计口径；回答『多高阈值做空胜率大』）",
           ["sw1", "sw7", "sw14", "smed7", "smean7", "spf7", "tail50"],
           ["胜1d", "胜7d", "胜14d", "中位空7d", "均空7d", "盈亏比7d", "涨≥50%"])

    # ③ 严格组合（chg≥20 + 正费率 + 放量）做空
    # 注：scan_chase_risk 用 vol≥$50M 太严（3.5 个月仅 6 条，样本不足），
    # 回测口径放宽到 ≥$10M（64 条）作「高涨幅+拥挤+可成交」的近似。
    for vol_min, tag in ((10_000_000, "严格组合(vol≥10M)"), (20_000_000, "严格组合(vol≥20M)")):
        strict = [s for s in sigs if s["chg"] >= 20
                  and s["fund_rate"] is not None and s["fund_rate"] > 0
                  and s["vol"] >= vol_min]
        if len(strict) >= args.min_n:
            st = _short_stats(strict, args.min_n)
            st["label"] = tag
            _print([st], f"{tag} 做空（涨幅≥20% + 正资金费率 + 成交额≥${vol_min / 1e6:.0f}M）",
                   ["sw1", "sw7", "sw14", "smed7", "smean7", "spf7", "tail30", "tail50"],
                   ["胜1d", "胜7d", "胜14d", "中位空7d", "均空7d", "盈亏比7d", "涨≥30%", "涨≥50%"])

    print("\n[short] ⚠️ 做空风险提示：")
    print("  1) 未含滑点/手续费/资金费率成本（永续正费率时空头收取 → 对做空有利，见 Part C）；")
    print("  2) 右尾风险：即使胜率高，做空的最大亏损无上限（未含强平），均值口径被右尾拖累；")
    print("  3) 样本仅 ~3.5 个月、单一 regime，结论 provisional。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="币安涨幅榜冲高回落回测（含资金费率交叉）")
    parser.add_argument("--min-gain", type=float, default=MIN_GAIN, help="信号最小 24h 涨幅（%）")
    parser.add_argument("--min-n", type=int, default=MIN_N, help="统计最少样本")
    parser.add_argument("--short", action="store_true", help="做空视角回测（胜率/期望/右尾风险）")
    parser.add_argument("--out", type=str, default="", help="CSV 输出路径")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(SQL_BACKTEST, (args.min_gain / 100,))
            raw = cur.fetchall()

    sigs = []
    for r in raw:
        sigs.append({
            "symbol": r[0], "sig_d": r[1],
            "chg": float(r[2] or 0) * 100, "vol": float(r[3] or 0),
            "fund_rate": float(r[4]) if r[4] is not None else None,
            "r1": float(r[5]) if r[5] is not None else None,
            "r3": float(r[6]) if r[6] is not None else None,
            "r7": float(r[7]) if r[7] is not None else None,
            "r14": float(r[8]) if r[8] is not None else None,
            "peak": float(r[9]) if r[9] is not None else None,
            "peak_day": int(r[10]) if r[10] is not None else None,
            "trough": float(r[11]) if r[11] is not None else None,
            "trough_day": int(r[12]) if r[12] is not None else None,
            "first_neg_day": int(r[13]) if r[13] is not None else None,
        })
    n_sym = len({s["symbol"] for s in sigs})
    print(f"[binance-gainers] 信号 {len(sigs)} 条 / 唯一合约 {n_sym} 个 "
          f"(1h 回溯 2026-06-18 起)")
    if not sigs:
        print("[binance-gainers] 无信号")
        return 1
    n_fund = sum(1 for s in sigs if s["fund_rate"] is not None)
    print(f"[binance-gainers] 有资金费率信号的 {n_fund} 条（{n_fund / len(sigs):.0%}，"
          f"费率仅覆盖 191 合约子集）")
    if args.short:
        return run_short(sigs, args)

    # ── ① 分桶表 ──
    rows = []
    by_bucket = {}
    for s in sigs:
        by_bucket.setdefault(_bucket_of(s["chg"]), []).append(s)
    for b in BUCKETS:
        sub = by_bucket.get(b, [])
        if len(sub) >= args.min_n and len({x["sig_d"] for x in sub}) >= MIN_DAYS:
            rows.append(_slice_stats(sub, f"+{b[0]:.0f}~{int(b[1]) if b[1] < 1e9 else 999}%", args.min_n))
    _print_slice(rows, f"币安涨幅榜分桶（永续 USDT；跌概率% / 中位收益%，窗口 1/3/7/14 日）",
                 ["pdn1", "pdn3", "pdn7", "pdn14", "med1", "med7", "med14"],
                 ["跌1d", "跌3d", "跌7d", "跌14d", "中位1d", "中位7d", "中位14d"])

    # 冲高空间
    _print_slice(rows, "冲高空间（14 日内峰值 / 到顶日 / 未跌率）",
                 ["med_peak", "med_peak_day", "never_neg"],
                 ["中位峰值%", "中位到顶日", "14日未跌率%"],
                 raw_cols=("med_peak_day",))

    # ── ② 阈值扫描 + 临界 ──
    sweep = []
    for thr in THRESH_GRID:
        sub = [s for s in sigs if s["chg"] >= thr]
        if len(sub) >= args.min_n:
            st = _slice_stats(sub, f">={thr}%", args.min_n)
            sweep.append(st)
    _print_slice(sweep, "阈值扫描（累计口径；下跌概率 > 50% 即『大概率下跌』区）",
                 ["pdn1", "pdn7", "pdn14", "med7"],
                 ["跌1d", "跌7d", "跌14d", "中位7d"])
    print("\n=== 临界涨幅（首个 P(下跌)>50%，n ≥ {}）===".format(args.min_n))
    for hz in (1, 7):
        crit = next((r["label"] for r in sweep
                     if r.get(f"pdn{hz}") is not None and r[f"pdn{hz}"] > DECLINE_CRIT), None)
        print(f"  {hz} 日后：" + (f"24h 涨幅 {crit} 时跌概率 "
                                 f"{next(r[f'pdn{hz}'] for r in sweep if r['label']==crit):.1%}"
                                 if crit else "全部档 P≤50%"))

    # ── ③ 成交额（流动性）分档 ──
    vols = sorted(s["vol"] for s in sigs if s["vol"] is not None and s["vol"] > 0)
    if vols:
        q1, q2, q3 = (_pct(vols, 0.25), _pct(vols, 0.5), _pct(vols, 0.75))
        vol_bands = ((0, q1, "成交额低"), (q1, q2, "成交额中"), (q2, q3, "成交额高"),
                     (q3, 1e30, "成交额最高"))
        vrows = []
        for lo, hi, lb in vol_bands:
            sub = [s for s in sigs if s["vol"] is not None and lo <= s["vol"] < hi]
            if len(sub) >= args.min_n:
                vrows.append(_slice_stats(sub, lb, args.min_n))
        _print_slice(vrows, "信号日成交额分档（四分位，流动性 × 后续）",
                     ["pdn1", "pdn7", "med7", "med_peak"],
                     ["跌1d", "跌7d", "中位7d", "中位峰值"])

    # ── ④ 资金费率拥挤度交叉（C；191 合约子集）──
    fund_rows = []
    for tag in ("正高", "正", "近零", "负", "NA"):
        sub = [s for s in sigs if _fund_tag(s["fund_rate"]) == tag]
        if len(sub) >= args.min_n:
            fund_rows.append(_slice_stats(sub, f"费率{tag}", args.min_n))
    _print_slice(fund_rows, "资金费率分档（信号日最后结算点；正高=拥挤多头）",
                 ["pdn1", "pdn7", "med7", "med_peak"],
                 ["跌1d", "跌7d", "中位7d", "中位峰值"])

    # 涨幅 × 费率 交叉
    print("\n=== 涨幅桶 × 资金费率 交叉（7 日跌概率% / 中位7d%，n）===")
    cross = [("正高", "正"), ("近零", "负", "NA")]
    print(f"{'涨幅桶':>10}" + "".join(f"{h:>16}" for h in ["正高", "正", "近零", "负", "NA"]))
    print("-" * 90)
    for b in BUCKETS:
        label = f"+{b[0]:.0f}~{int(b[1]) if b[1] < 1e9 else 999}%"
        cell = []
        for tag in ("正高", "正", "近零", "负", "NA"):
            sub = [s for s in by_bucket.get(b, []) if _fund_tag(s["fund_rate"]) == tag]
            r7 = [s["r7"] for s in sub if s["r7"] is not None]
            if len(r7) >= args.min_n:
                p7 = sum(1 for x in r7 if x < 0) / len(r7)
                cell.append(f"{p7 * 100:.0f}%/{_med(r7) * 100:.0f}%/{len(sub)}")
            else:
                cell.append("--")
        print(f"{label:>10}" + "".join(f"{c:>16}" for c in cell))
    print("\n[binance-gainers] ⚠️ 费率交叉仅在 191 个有 funding 历史的合约上；"
          "永续涨跌幅榜含资金费率扰动，且 1h 历史仅 ~3.5 个月（单一 regime，provisional）")

    # ── CSV ──
    if not args.out:
        args.out = str(SCRIPT_DIR.parent / "data" / "backtest_binance_gainers.csv")
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["section", "label", "n"] +
                   sum(([f"pdn_{hz}d", f"med_{hz}d"] for hz in HORIZONS), []))
        for r in rows + sweep:
            section = "bucket" if "~" in r["label"] else "threshold"
            w.writerow([section, r["label"], r["n"]] +
                       sum(([r.get(f"pdn{hz}"), r.get(f"med{hz}")] for hz in HORIZONS), []))
    print(f"\n[binance-gainers] 结果已存 {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
