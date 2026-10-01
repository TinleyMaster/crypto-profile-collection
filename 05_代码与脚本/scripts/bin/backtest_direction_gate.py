#!/usr/bin/env python3
"""方向闸门验证：趋势方向是否可作为信号方向的前置过滤（工单 SCAN-DIR-GATE-001）。

背景
----
2026-10-01 线上闭环复核（设计方案 §8.1.7 / §8.1.8）发现：样本期 BTC 单边上涨，
系统仍发出 145 条做空（占 23%），这批 24h 均净 −3.20%、超额 −3.28pp、胜率 26.9%；
剔掉空头整体从 −0.85% → −0.15%。**但"剔掉空头"是事后观测、不是规则**
⇒ 本脚本回答：存在一条**事前可得**的趋势规则，能把这批逆势信号筛掉吗？

⚠️ 与 `SCAN-REGIME-GATE-001` 的区别（勿混淆）
--------------------------------------------
那个工单验证的是 `build_scan_edge_report.classify_regime` 的**日级 trend/range/mixed 标签**
（已闭环 `descriptive_only`：无信号级因果预测力，train/test 双侧反向）。
本脚本验证的是**另一构念**——「趋势方向」（价格 vs 均线），**独立成立或独立证伪**。

判据（预登记，防事后挑数）
------------------------
* 构造：`alerted_at` 时刻的 BTC 1h 收盘价 vs 其 MA20/MA50/MA200（含看该币自身 MA50 的对照），
  逐笔判定 `trend_up`；闸门 = **仅保留与 `trend_up` 同向的信号**；
* 切分：`--holdout-days`（默认 5）为 test=最近 N 天、其余为 train，**时序切分**；
* **PASS**：闸门后的 24h 净均在 **train 与 test 双侧均优于基线**，且 test 侧保留样本 ≥ 50 笔；
  任一构念双侧一致即为「候选」，可进入下一工单；
* **FAIL**：任一侧反向 —— 按 §14.4 纪律，**不得上线任何方向闸门**。

铁律
----
* **全流程只读 prod**（纯 SELECT，无 INSERT/UPDATE/DDL）、不写库；
* 结论一律带 train/test 双侧与 n，样本<门槛不下结论；
* 与线上代码完全解耦：本脚本不改 `scan_daemon.py` / 不改任何阈值 / 不入调度。

⚠️ 读数口径：`净均` 是**按笔**均值、`日t` 是**按日等权聚类**，两者可反号 —— 判显著性看日 t。

用法
----
    python backtest_direction_gate.py                    # 默认全窗口 + holdout 5 天
    python backtest_direction_gate.py --holdout-days 4
    python backtest_direction_gate.py --json             # 只打印不落 CSV
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from datetime import timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg.rows  # noqa: E402

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

SH = timezone(timedelta(hours=8))
DATA_DIR = SCRIPT_DIR.parent / "data"

# 闸门构念：(键, 人读名, 趋势来源)
#   btc_*  = BTC 1h 收盘 vs 该均线；sym_up = 该币自身 1h 收盘 vs 其 MA50
GATES: list[tuple[str, str]] = [
    ("btc_ma20", "BTC 1h > MA20"),
    ("btc_ma50", "BTC 1h > MA50"),
    ("btc_ma200", "BTC 1h > MA200"),
    ("sym_ma50", "币自身 1h > MA50"),
]
MIN_KEEP_N = 50          # test 侧保留样本门槛（不足则不下结论）
MIN_DAYS = 3             # 独立天数门槛

# 趋势源统一在一个 SQL 里算好：BTC 三条均线 + 各币自身 MA50，全部在 alerted_at 之前（不含未来）
TREND_SQL = """
WITH btc AS (
    SELECT open_time, close_px,
           avg(close_px) OVER (ORDER BY open_time ROWS BETWEEN 19  PRECEDING AND CURRENT ROW) ma20,
           avg(close_px) OVER (ORDER BY open_time ROWS BETWEEN 49  PRECEDING AND CURRENT ROW) ma50,
           avg(close_px) OVER (ORDER BY open_time ROWS BETWEEN 199 PRECEDING AND CURRENT ROW) ma200
      FROM biz.asset_klines
     WHERE symbol = 'BTCUSDT' AND interval = '1h'
       AND open_time BETWEEN %(from_ts)s AND %(to_ts)s
), sym AS (
    SELECT symbol, open_time,
           avg(close_px) OVER (PARTITION BY symbol ORDER BY open_time
                               ROWS BETWEEN 49 PRECEDING AND CURRENT ROW) ma50
      FROM biz.asset_klines
     WHERE interval = '1h' AND open_time BETWEEN %(from_ts)s AND %(to_ts)s
)
SELECT o.pool, o.scenario, o.p_dir, o.symbol, o.alerted_at, o.aligned_ret_24h AS r24,
       (o.alerted_at AT TIME ZONE 'Asia/Shanghai')::date AS d,
       (SELECT b.close_px > b.ma20  FROM btc b WHERE b.open_time <= o.alerted_at ORDER BY b.open_time DESC LIMIT 1) AS btc_ma20,
       (SELECT b.close_px > b.ma50  FROM btc b WHERE b.open_time <= o.alerted_at ORDER BY b.open_time DESC LIMIT 1) AS btc_ma50,
       (SELECT b.close_px > b.ma200 FROM btc b WHERE b.open_time <= o.alerted_at ORDER BY b.open_time DESC LIMIT 1) AS btc_ma200,
       (SELECT s.ma50 IS NOT NULL AND o.base_px > s.ma50 FROM sym s
         WHERE s.symbol = o.symbol AND s.open_time <= o.alerted_at ORDER BY s.open_time DESC LIMIT 1) AS sym_ma50
  FROM biz.scan_signal_outcome o
 WHERE o.outcome_state = 'resolved' AND o.aligned_ret_24h IS NOT NULL
 ORDER BY o.alerted_at
"""


def summarize(rows: list[dict], key: str = "r24") -> dict | None:
    """按笔均值 + 胜率 + PF + 按日等权聚类 t。空样本返回 None。"""
    v = [float(r[key]) for r in rows if r.get(key) is not None]
    if not v:
        return None
    wins = [x for x in v if x > 0]
    gross_loss = abs(sum(x for x in v if x < 0))
    per_day: dict = {}
    for r in rows:
        if r.get(key) is not None:
            per_day.setdefault(r["d"], []).append(float(r[key]))
    daily = [statistics.fmean(x) for x in per_day.values()]
    t = None
    if len(daily) > 1:
        sd = statistics.stdev(daily)
        if sd > 0:
            t = statistics.fmean(daily) / (sd / (len(daily) ** 0.5))
    return {
        "n": len(v), "days": len(daily), "win": len(wins) / len(v),
        "pf": (sum(wins) / gross_loss) if gross_loss > 0 else None,
        "avg": statistics.fmean(v), "daily_avg": statistics.fmean(daily), "t": t,
    }


def same_direction(r: dict, gate: str) -> bool:
    """信号方向是否与趋势同向（趋势未知 → 视为不同向，被剔除）。"""
    up = r.get(gate)
    if up is None:
        return False
    return (up and r["p_dir"] == "up") or ((not up) and r["p_dir"] == "down")


def verdict(base: dict | None, kept: dict | None) -> tuple[str, float | None]:
    """按预登记判据给单侧结论；`delta` 为 kept.avg - base.avg（pp）。"""
    if not base or not kept:
        return "no_sample", None
    if kept["n"] < MIN_KEEP_N or kept["days"] < MIN_DAYS:
        return "insufficient", kept["avg"] - base["avg"]
    d = kept["avg"] - base["avg"]
    return ("better" if d > 0 else "worse"), d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--holdout-days", type=int, default=5, help="test = 最近 N 天（上海日）")
    ap.add_argument("--out", default=str(DATA_DIR / "backtest_direction_gate.csv"))
    ap.add_argument("--json", action="store_true", help="只打印不落 CSV")
    args = ap.parse_args()

    s = get_settings()
    with get_connection(s.database_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("SELECT min(alerted_at) lo, max(alerted_at) hi FROM biz.scan_signal_outcome"
                        " WHERE outcome_state='resolved'")
            span = cur.fetchone()
            from_ts = span["lo"] - timedelta(days=10)   # 均线预热
            to_ts = span["hi"] + timedelta(days=1)
            cur.execute(TREND_SQL, {"from_ts": from_ts, "to_ts": to_ts})
            rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        print("无已结算样本")
        return 1
    days = sorted({r["d"] for r in rows})
    cut = days[-min(args.holdout_days, len(days) - 1)] if len(days) > 1 else days[0]
    seg = {"train": [r for r in rows if r["d"] < cut], "test": [r for r in rows if r["d"] >= cut],
           "ALL": rows}

    print(f"样本 {len(rows)} 笔 / {len(days)} 天（{days[0]} → {days[-1]}）；"
          f"cutoff={cut}（test=最近 {args.holdout_days} 天，train={len(seg['train'])} 笔）")
    base = {k: summarize(v) for k, v in seg.items()}
    print("\n基线：")
    for k in ("ALL", "train", "test"):
        b = base[k]
        if b:
            print(f"  {k:5s} n={b['n']:3d} 胜率={b['win']*100:.1f}% "
                  f"PF={(('%.2f' % b['pf']) if b['pf'] else 'n/a')} 24h净={b['avg']:+.3f}% "
                  f"日t={(('%+.2f' % b['t']) if b['t'] is not None else 'n/a')}")

    out_rows: list[dict] = []
    print("\n闸门（仅保留与趋势同向的信号）：")
    for gk, glabel in GATES:
        print(f"  ── {glabel}")
        rec = {"gate": gk, "label": glabel}
        for k in ("ALL", "train", "test"):
            kept = [r for r in seg[k] if same_direction(r, gk)]
            ks = summarize(kept)
            v, d = verdict(base[k], ks)
            rec[f"{k}_n"] = ks["n"] if ks else 0
            rec[f"{k}_avg"] = round(ks["avg"], 4) if ks else None
            rec[f"{k}_delta_pp"] = round(d, 4) if d is not None else None
            rec[f"{k}_verdict"] = v
            if ks:
                print(f"     {k:5s} 保留 n={ks['n']:3d}（基线 {base[k]['n']:3d}）胜率={ks['win']*100:.1f}% "
                      f"PF={(('%.2f' % ks['pf']) if ks['pf'] else 'n/a')} 净={ks['avg']:+.3f}% "
                      f"日t={(('%+.2f' % ks['t']) if ks['t'] is not None else 'n/a')} "
                      f"→ 改善 {d:+.3f}pp [{v}]")
        consistent = (rec["train_verdict"] == rec["test_verdict"] == "better")
        rec["PASS"] = consistent
        print(f"     ⇒ 双侧一致改善：{'PASS' if consistent else 'FAIL'}")
        out_rows.append(rec)

    print("\n" + "=" * 78)
    ok = [r for r in out_rows if r["PASS"]]
    if ok:
        print("候选（双侧一致改善）：" + ", ".join(r["label"] for r in ok))
        print("→ 可进入下一工单；上线前仍须影子期。")
    else:
        print("结论【FAIL / descriptive_only】：四个趋势构念**全部不通过**——")
        print("  任一侧反向 ⇒ 按 §14.4 纪律，**不得上线任何方向闸门**。")
        print("  ⚠️ 推论：「逆势空头亏损」不是趋势问题——空头在 BTC 均线上/下方均亏约 3.2%，")
        print("     即闸门条件本就近似生效却无效；该缺口须另寻构念验证。")

    if args.json:
        print("\n" + json.dumps(out_rows, ensure_ascii=False, indent=2))
        return 0
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cols = list(out_rows[0].keys())
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(out_rows)
    print(f"\n已写 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
