#!/usr/bin/env python3
"""滚动 24h 涨幅 → 后续价格表现（全市场全时段大样本回测）。

问题（用户 2026-10-11）：24h 涨幅（价格变化率）对「后续价格表现」是否有预测力？
样本：biz.asset_klines interval='1h'，**全市场全部合约、全部时段**（约 970 万根 bar，
      529 个 USDT 永续，2022-10 ~ 今），不做任何事件过滤。

口径：
  - chg24 = close_t / close_{t-24} - 1（对齐线上 ticker/24hr 滚动口径；仅用 t 及之前数据，无前视）
  - entry = 第 t 根 bar 的 close（与项目其它回测一致）
  - 后续表现三视角（全部相对 entry，扣 0.3% 往返成本）：
      ① 固定窗口 forward return：+1h/4h/12h/24h/72h（close_{t+N}/entry - 1，buy&hold）
      ② TRAIL3 跟踪止盈：入场后滚动峰值回撤 3% 触发，出场价 = 触发时峰值×0.97；
         72h 内未触发按 72h 收盘平仓（与 backtest_trade_params 同构）
      ③ MAE/MFE：72h 内最大不利 / 有利偏移（MIN low / MAX high 相对 entry）
  - 分档：按 chg24 固定阈值桶（对齐 A/B/C 信号阈值 5/20/30/50/75）
  - 另按年份分组，观察跨 regime 稳定性

⚠️ 算力说明（本会话实测）：本机 → 远端库带宽仅 ~400 行/秒，把 970 万 bar 拉到本地
   需 ~7 小时，故**全部指标在远端 SQL 聚合**，仅回传 (桶 × 年份) 级小结果（几十行）。
   TRAIL3 需滚动峰值扫描，用「有界自连接 + 逐 (symbol,eo) 滚动 MAX」在服务端实现。

判据：跨桶期望/PF 是否单调、尾部（≥50%）是否转负、MAE/MFE 风险收益比是否随涨幅恶化；
      并给出分年稳定性；小样本（n<100）档诚实标注不显著。

用法：
    python bin/backtest_chg24_quantiles.py                 # 全量（每条查询数分钟）
    python bin/backtest_chg24_quantiles.py --no-trail       # 跳过 TRAIL3（更快）
    python bin/backtest_chg24_quantiles.py --cost 0.003 --out data/backtest_chg24_buckets.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

COST = 0.003            # 往返成本（0.3%）
MIN_N = 100             # 有效样本下限（低于此档标注不显著）
TRAIL_D = 0.03          # 跟踪止盈回撤
TRAIL_WIN_H = 72        # TRAIL3 出场窗口（小时）
TRAIL_SAMPLE = 4        # TRAIL3 系统性抽样分母（1/N；固定窗口仍用全量）
# 固定窗口（列名, 前向 bar 数）
HORIZONS = [("r1", 1), ("r4", 4), ("r12", 12), ("r24", 24), ("r72", 72)]

# chg24 固定阈值桶（下界含、上界不含），对齐信号阈值 5/20/30/50/75
BUCKETS = [
    (-1e9, -0.50), (-0.50, -0.30), (-0.30, -0.20), (-0.20, -0.10),
    (-0.10, -0.05), (-0.05, 0.0), (0.0, 0.05), (0.05, 0.10),
    (0.10, 0.20), (0.20, 0.30), (0.30, 0.50), (0.50, 0.75), (0.75, 1e9),
]


def _lo(lo: float) -> str:
    return "-inf" if lo <= -1e9 else f"{lo*100:.0f}%"


def _hi(hi: float) -> str:
    return "+inf" if hi >= 1e9 else f"{hi*100:.0f}%"


def _label(idx: int, lo: float, hi: float) -> str:
    # 前缀序号保证字典序 = 阈值序（打印时用 _disp 去掉）
    return f"{idx:02d}_{_lo(lo)}~{_hi(hi)}"


def _disp(bucket: str) -> str:
    return bucket.split("_", 1)[1] if "_" in bucket else bucket


def _bucket_case(col: str = "chg24") -> str:
    """生成 chg24 → 桶标签的 CASE 表达式。"""
    whens = []
    for i, (lo, hi) in enumerate(BUCKETS[:-1]):
        whens.append(f"WHEN {col} < {hi} THEN '{_label(i, lo, hi)}'")
    i = len(BUCKETS) - 1
    whens.append(f"ELSE '{_label(i, *BUCKETS[-1])}'")
    return "CASE " + " ".join(whens) + " END"


def _agg_fixed(cost: float) -> str:
    """固定窗口聚合：每窗口 mean/med/win/pf/worst（扣成本）+ MFE/MAE。"""
    parts = []
    for name, _h in HORIZONS:
        e = f"{name} - {cost}"
        parts.append(f"avg({e}) AS mean_{name}")
        parts.append(f"percentile_cont(0.5) WITHIN GROUP (ORDER BY {e}) AS med_{name}")
        parts.append(f"avg(({e} > 0)::int) AS win_{name}")
        parts.append(f"sum({e}) FILTER (WHERE {e} > 0)"
                     f" / NULLIF(abs(sum({e}) FILTER (WHERE {e} <= 0)), 0) AS pf_{name}")
        parts.append(f"min({e}) AS worst_{name}")
    parts.append("avg(mfe) AS mfe_mean")
    parts.append("percentile_cont(0.5) WITHIN GROUP (ORDER BY mfe) AS mfe_med")
    parts.append("avg(mae) AS mae_mean")
    parts.append("percentile_cont(0.5) WITHIN GROUP (ORDER BY mae) AS mae_med")
    return ",\n       ".join(parts)


def _sql_basic(cost: float) -> str:
    return f"""
WITH b AS (
  SELECT symbol, open_time, close_px, high_px, low_px,
         close_px / NULLIF(LAG(close_px, 24) OVER (PARTITION BY symbol ORDER BY open_time), 0) - 1 AS chg24,
         LEAD(close_px, 1)  OVER (PARTITION BY symbol ORDER BY open_time) AS c1,
         LEAD(close_px, 4)  OVER (PARTITION BY symbol ORDER BY open_time) AS c4,
         LEAD(close_px, 12) OVER (PARTITION BY symbol ORDER BY open_time) AS c12,
         LEAD(close_px, 24) OVER (PARTITION BY symbol ORDER BY open_time) AS c24,
         LEAD(close_px, 72) OVER (PARTITION BY symbol ORDER BY open_time) AS c72,
         MAX(high_px) OVER (PARTITION BY symbol ORDER BY open_time
             ROWS BETWEEN 1 FOLLOWING AND 72 FOLLOWING) AS hi72,
         MIN(low_px)  OVER (PARTITION BY symbol ORDER BY open_time
             ROWS BETWEEN 1 FOLLOWING AND 72 FOLLOWING) AS lo72
  FROM biz.asset_klines
  WHERE interval = '1h' AND close_px > 0 AND high_px > 0 AND low_px > 0
)
SELECT bucket, yr, count(*) AS n,
       {_agg_fixed(cost)}
FROM (
  SELECT {_bucket_case('chg24')} AS bucket,
         EXTRACT(YEAR FROM open_time)::int AS yr,
         c1/close_px-1 AS r1, c4/close_px-1 AS r4, c12/close_px-1 AS r12,
         c24/close_px-1 AS r24, c72/close_px-1 AS r72,
         hi72/close_px-1 AS mfe, lo72/close_px-1 AS mae
  FROM b WHERE chg24 IS NOT NULL AND c72 IS NOT NULL
) x
GROUP BY GROUPING SETS ((bucket, yr), (bucket))
ORDER BY bucket, yr NULLS FIRST
"""


def _sql_trail(cost: float, sample: int) -> str:
    # 抽样：确定性 hash 取模，只用 1/sample 的入场 bar 做 TRAIL3（覆盖仍为全市场全时段）。
    # 有界自连接（9.67M × 72 ≈ 7 亿行）在服务端排序代价极高，抽样把代价降 4 倍且统计量充足。
    filt = (f"AND MOD(hashtext(symbol || open_time::text), {sample}) = 0"
            if sample > 1 else "")
    return f"""
WITH b AS (
  SELECT symbol, open_time, close_px, high_px, low_px,
         close_px / NULLIF(LAG(close_px, 24) OVER (PARTITION BY symbol ORDER BY open_time), 0) - 1 AS chg24,
         LEAD(close_px, 72) OVER (PARTITION BY symbol ORDER BY open_time) AS c72
  FROM biz.asset_klines
  WHERE interval = '1h' AND close_px > 0 AND high_px > 0 AND low_px > 0
),
e AS (
  SELECT symbol, open_time AS eo, close_px AS entry, chg24, c72
  FROM b WHERE chg24 IS NOT NULL AND c72 IS NOT NULL {filt}
),
bars AS (
  SELECT e.symbol, e.eo, e.entry, e.chg24, e.c72, k.high_px, k.low_px,
         (EXTRACT(EPOCH FROM (k.open_time - e.eo)) / 3600)::int AS h
  FROM e JOIN biz.asset_klines k
    ON k.symbol = e.symbol AND k.interval = '1h'
   AND k.open_time > e.eo AND k.open_time <= e.eo + interval '{TRAIL_WIN_H} hours'
   AND k.high_px > 0 AND k.low_px > 0
),
bq AS (
  SELECT *, MAX(high_px) OVER (PARTITION BY symbol, eo ORDER BY h
      ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS ph
  FROM bars
),
tr AS (
  SELECT DISTINCT ON (symbol, eo) symbol, eo, entry, chg24, c72, ph * {1 - TRAIL_D} AS exitpx
  FROM bq WHERE low_px <= ph * {1 - TRAIL_D}
  ORDER BY symbol, eo, h
)
SELECT bucket, yr, count(*) AS n,
       avg(exit_ret) AS mean,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY exit_ret) AS med,
       avg((exit_ret > 0)::int) AS win,
       sum(exit_ret) FILTER (WHERE exit_ret > 0)
         / NULLIF(abs(sum(exit_ret) FILTER (WHERE exit_ret <= 0)), 0) AS pf,
       min(exit_ret) AS worst,
       avg(trigger) AS trigger_rate
FROM (
  SELECT {_bucket_case('e.chg24')} AS bucket,
         EXTRACT(YEAR FROM e.eo)::int AS yr,
         (COALESCE(tr.exitpx, e.c72)) / e.entry - 1 - {cost} AS exit_ret,
         (tr.exitpx IS NOT NULL)::int AS trigger
  FROM e LEFT JOIN tr ON tr.symbol = e.symbol AND tr.eo = e.eo
) y
GROUP BY GROUPING SETS ((bucket, yr), (bucket))
ORDER BY bucket, yr NULLS FIRST
"""


_FIXED_COLS = (["bucket", "yr", "n"]
               + [f"{p}_{name}" for name, _ in HORIZONS for p in ("mean", "med", "win", "pf", "worst")]
               + ["mfe_mean", "mfe_med", "mae_mean", "mae_med"])
_TRAIL_COLS = ["bucket", "yr", "n", "mean", "med", "win", "pf", "worst", "trigger_rate"]


def _row_dict(r: tuple, trail: bool = False) -> dict:
    return dict(zip(_TRAIL_COLS if trail else _FIXED_COLS, r))


def _run(conn, sql: str, label: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = '1800s'")
        t0 = time.time()
        cur.execute(sql)
        rows = cur.fetchall()
    print(f"[{label}] 返回 {len(rows)} 行，耗时 {time.time() - t0:.0f}s", flush=True)
    return rows


def _print_fixed(rows: list[tuple]) -> None:
    overall = [r for r in rows if r[1] is None]
    print("\n" + "=" * 120)
    print("① 固定窗口 forward return（扣 0.3% 成本；期望 %）")
    print("=" * 120)
    hdr = f"{'chg24 桶':>16}{'n':>11}" + "".join(f"{name+'均':>9}" for name, _ in HORIZONS)
    hdr += f"{'r12中位':>9}{'r12胜':>8}{'r72胜':>8}{'PF_r12':>9}{'PF_r72':>9}"
    print(hdr)
    print("-" * len(hdr))
    for r in overall:
        d = _row_dict(r)
        line = f"{_disp(d['bucket']):>16}{d['n']:>11,}"
        line += "".join(f"{d['mean_' + name]*100:>+9.2f}" for name, _ in HORIZONS)
        line += (f"{d['med_r12']*100:>+9.2f}{d['win_r12']*100:>7.1f}%{d['win_r72']*100:>7.1f}%"
                 f"{d['pf_r12']:>9.2f}{d['pf_r72']:>9.2f}")
        print(line)


def _print_risk(rows_fixed: list[tuple], rows_trail: list[tuple]) -> None:
    of = {_row_dict(r)["bucket"]: _row_dict(r) for r in rows_fixed if r[1] is None}
    ot = {_row_dict(r, True)["bucket"]: _row_dict(r, True) for r in rows_trail if r[1] is None}
    print("\n" + "=" * 120)
    print("② TRAIL3 跟踪止盈（退出=峰值×0.97，72h 窗口，扣 0.3%）  ③ MAE/MFE（72h，相对 entry）")
    print("=" * 120)
    hdr = (f"{'chg24 桶':>16}{'n':>11}{'TRAIL均':>9}{'TRAIL中':>9}{'TRAIL胜':>9}"
           f"{'PF':>7}{'触发率':>8}{'MFE均':>9}{'MAE均':>9}{'MFE/|MAE|':>10}")
    print(hdr)
    print("-" * len(hdr))
    for bucket, d in of.items():
        t = ot.get(bucket)
        ratio = d["mfe_mean"] / abs(d["mae_mean"]) if d["mae_mean"] else float("nan")
        line = f"{_disp(bucket):>16}{d['n']:>11,}"
        if t is None:
            line += f"{'--':>9}{'--':>9}{'--':>9}{'--':>7}{'--':>8}"
        else:
            line += (f"{t['mean']*100:>+9.2f}{t['med']*100:>+9.2f}{t['win']*100:>8.1f}%"
                     f"{t['pf']:>7.2f}{t['trigger_rate']*100:>7.1f}%")
        line += f"{d['mfe_mean']*100:>+9.2f}{d['mae_mean']*100:>+9.2f}{ratio:>10.2f}"
        print(line)


def _print_stability(rows: list[tuple], metric: str, title: str) -> None:
    per = [r for r in rows if r[1] is not None]
    if not per:
        return
    years = sorted({r[1] for r in per})
    by: dict = {}
    for r in per:
        d = _row_dict(r)
        by.setdefault(d["bucket"], {})[d["yr"]] = d
    print("\n" + "=" * 120)
    print(f"分年稳定性：{title}（%，看跨 regime 是否同号/同序）")
    print("=" * 120)
    print(f"{'chg24 桶':>16}" + "".join(f"{y:>10}" for y in years) + f"{'n合计':>11}")
    print("-" * (16 + 10 * len(years) + 11))
    for bucket in by:
        cells = by[bucket]
        line = f"{_disp(bucket):>16}"
        for y in years:
            c = cells.get(y)
            line += f"{c[metric]*100:>+10.2f}" if c and c["n"] >= MIN_N else f"{'--':>10}"
        print(line + f"{sum(c['n'] for c in cells.values()):>11,}")


def main() -> int:
    ap = argparse.ArgumentParser(description="滚动 24h 涨幅 → 后续表现（全市场大样本）")
    ap.add_argument("--cost", type=float, default=COST, help="往返成本（默认 0.003）")
    ap.add_argument("--no-trail", action="store_true", help="跳过 TRAIL3 查询（更快）")
    ap.add_argument("--trail-sample", type=int, default=TRAIL_SAMPLE,
                    help="TRAIL3 抽样分母（1/N，默认 4；1=全量但极慢）")
    ap.add_argument("--out", type=str, default="", help="CSV 输出路径")
    args = ap.parse_args()

    out_path = Path(args.out) if args.out else SCRIPT_DIR.parent / "data" / "backtest_chg24_buckets.csv"
    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        rows_fixed = _run(conn, _sql_basic(args.cost), "固定窗口+MAE/MFE")
        rows_trail: list[tuple] = []
        if not args.no_trail:
            rows_trail = _run(conn, _sql_trail(args.cost, args.trail_sample), "TRAIL3")

    total = sum(r[2] for r in rows_fixed if r[1] is None)
    print(f"\n样本合计: {total:,} 条（全市场全时段；chg24 有效且含 72h 完整前向窗口）")

    _print_fixed(rows_fixed)
    if rows_trail:
        _print_risk(rows_fixed, rows_trail)
    _print_stability(rows_fixed, "mean_r72", "固定窗口 r72 期望")
    _print_stability(rows_fixed, "mean_r12", "固定窗口 r12 期望")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["section", *_FIXED_COLS])
        for r in rows_fixed:
            w.writerow(["fixed", *r])
        if rows_trail:
            w.writerow(["section", *_TRAIL_COLS])
            for r in rows_trail:
                w.writerow(["trail3", *r])
    print(f"\n[chg24] 结果已存 {out_path}")
    print("[chg24] ⚠️ 固定窗口/MAE-MFE 为全市场全时段；TRAIL3 为确定性 1/"
          f"{args.trail_sample} 抽样（覆盖仍全市场全时段）。成本仅含 0.3% 往返，未含滑点/资金费率。")
    return 0


if __name__ == "__main__":
    sys.exit(main())