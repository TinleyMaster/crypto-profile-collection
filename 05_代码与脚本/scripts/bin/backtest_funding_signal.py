#!/usr/bin/env python3
"""资金费率 fade 策略回测（工单 SCAN-FUNDING-001，只读 prod，可重入）。

假设 H1（funding fade / 逼空反转）
--------------------------------
资金费率 = 持仓拥挤度。极端**负**费率（空头拥挤、空头付钱养多头）⇒ 后续**反弹**（逼空）；
极端**正**费率（多头拥挤）⇒ 后续**回落**。操作：极端负 → 做多；极端正 → 做空。

判据（预登记在工单 §1.1，**跑之前已写死，不得事后调**）
------------------------------------------------------
* 分位窗口 N = **21 个结算点**（7 天 × 3/日，逐符号滑窗）；分位 = `count(win 内 < 当前) / (n−1)`；
* 触发：分位 ≤ **0.10** → 做多；≥ **0.90** → 做空；
* 主持有期 H = **24h**；成本 = **双边 taker 0.1%**，**方向对齐之后**扣（防空头虚增）；
* 切分：**时序**——test = 最近 `--holdout-days` 天（上海日），其余 train；
* 样本门槛：任一侧 ≥ 50 笔且 ≥ 5 独立日，不足判 `insufficient`；
* **PASS** = 合并两侧 24h 净均 train/test **双侧 > +0.20%** 且 **日 t > 0**，且**相对无条件基线（always-long）双侧为正**；
  任一侧反向即 **FAIL**（§14.4 纪律）。

取价口径（**无前视**，重要）
--------------------------
1h 棒 `open_time=X` 的 `close_px` 是 **X+1h 时刻**的价。故：
* 入场 = `open_time <= funding_time − 1h` 的最后一根 ⇒ 其收盘价 = **结算时刻价**；
* 出场 = `open_time <= funding_time + H − 1h` 的最后一根。
（若写成 `open_time <= funding_time`，拿到的是结算后 1h 的价 = 前视。）

⚠️ 读数口径：`净均` 是按笔均值、`日t` 是按日等权聚类，两者可反号 ⇒ **判显著性看日 t**。
⚠️ 24h 持仓会跨多个结算点，相邻信号窗口重叠 ⇒ 按笔 n 被高估，故一律以日 t 为准。

用法
----
    python backtest_funding_signal.py                  # 主口径 N=21 / holdout 21 天
    python backtest_funding_signal.py --holdout-days 14
    python backtest_funding_signal.py --json           # 只打印不落 CSV

边界：1h K 线仅回溯至 2026-06-18 ⇒ 可用价格窗 ≈ 105 天、且**单边上涨 regime**，
train/test 同在上涨段，**切不出跨 regime 对照**（工单 §6.1）。故设「防 beta 闸门」。
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from datetime import date, timedelta, timezone
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

# ── 预登记参数（工单 §1.1，禁止事后调） ──
PRIMARY_N = 21               # 分位窗口：21 个结算点 = 7 天 × 3/日
ROBUST_NS = (9, 42)          # 稳健性档（仅描述，不参与判定）：3 天 / 14 天
PCT_LO = 0.10                # 分位 ≤ 0.10 → 做多
PCT_HI = 0.90                # 分位 ≥ 0.90 → 做空
PRIMARY_H = 24               # 主持有期（小时）
HORIZONS = (1, 4, 12, 24, 72)
COST = 0.1                   # 双边 taker 手续费（%），与 collect_scan_outcome.COST 同口径
MIN_N = 50                   # 单侧样本门槛（笔）
MIN_DAYS = 5                 # 独立日门槛（项目铁律：独立日 <5 不下结论）
PASS_NET = 0.20              # PASS 净均门槛（%，覆盖双边成本）

# 宇宙：有 1h K 线（能取价）的符号的 funding 历史。只取价格窗往前 20 天的预热段
# （最大窗口 N=42 个结算点 ≈ 14 天，20 天留足余量），避免拉全量 181 天白占带宽
# ——本机到远端的链路实测会退化（同一查询 5s ↔ 198s），带宽是稀缺资源。
FUNDING_SQL = """
SELECT symbol, funding_time, rate
  FROM biz.funding_rate_hist
 WHERE symbol IN (SELECT DISTINCT symbol FROM biz.asset_klines WHERE interval = '1h')
   AND funding_time >= %(warm_lo)s
 ORDER BY symbol, funding_time
"""


def price_sql(horizons: tuple[int, ...]) -> str:
    """一次取回「入场上/下幅 + 各持有期」收盘价（全 SQL 侧 LATERAL，不拉 K 线明细）。"""
    lat = "".join(
        f"""
  LEFT JOIN LATERAL (
      SELECT k.close_px AS px FROM biz.asset_klines k
       WHERE k.symbol = s.symbol AND k.interval = '1h'
         AND k.open_time <= s.funding_time + INTERVAL '{h} hour' - INTERVAL '1 hour'
       ORDER BY k.open_time DESC LIMIT 1
  ) h{h} ON TRUE"""
        for h in horizons
    )
    cols = ", ".join(f"h{h}.px AS px_{h}h" for h in horizons)
    return f"""
SELECT s.symbol, s.funding_time, e.px AS entry_px, {cols}
  FROM unnest(%(syms)s::text[], %(tss)s::timestamptz[])
       AS s(symbol, funding_time)
  LEFT JOIN LATERAL (
      SELECT k.close_px AS px FROM biz.asset_klines k
       WHERE k.symbol = s.symbol AND k.interval = '1h'
         AND k.open_time <= s.funding_time - INTERVAL '1 hour'
       ORDER BY k.open_time DESC LIMIT 1
  ) e ON TRUE
{lat}
"""


# ───────────────────────────── 纯函数（可离线单测） ─────────────────────────────

def rolling_pct(rates: list[float], n: int) -> list[float | None]:
    """逐点滚动分位（**tie-aware**）：`(count(win 内 < x) + 0.5·count(win 内 == x)) / n`。

    ⚠️ 2026-10-01 修正：工单 §1.1 原式是 `count(< x) / (n−1)`（tie-naive）。实测
    `biz.funding_rate_hist` 有 **45,984 / 109,364（42%）条 == 0.00005**——即 Binance **默认费率**，
    是**中性值而非极端值**；tie-naive 下任何落在该众数上的点都算「窗口内最小」⇒ 分位≈0
    ⇒ 被误判为「极端负费率」。实测该式标出 **41%** 的点为做多（而非预登记的 ~10%），
    **未实现预登记意图**。故改用 tie-aware 均秩式——这是**口径修正（bug fix），不是调参**：
    N=21 / 阈值 0.10·0.90 / H=24 / 切分 全部未动。分母用 `n`（非 n−1）使两侧严格对称：
    唯一最小 → 0.5/21=0.024；唯一最大 → 20.5/21=0.976。
    """
    out: list[float | None] = [None] * len(rates)
    for i in range(n - 1, len(rates)):
        win = rates[i - n + 1: i + 1]
        x = rates[i]
        less = sum(1 for v in win if v < x)
        eq = sum(1 for v in win if v == x)
        out[i] = (less + 0.5 * eq) / len(win)
    return out


def fwd_ret(entry_px, exit_px) -> float | None:
    """方向**未**对齐的持有期毛收益（%）；缺价或 entry=0 返回 None。"""
    if entry_px is None or exit_px is None:
        return None
    e = float(entry_px)
    if e == 0:
        return None
    return (float(exit_px) - e) / e * 100


def aligned_ret(raw: float | None, direction: str | None, cost: float = COST) -> float | None:
    """方向对齐净收益（%）：先按方向对齐毛收益，**再**扣成本。

    ⚠️ 顺序不能反（与 `collect_scan_outcome.aligned_ret` 同口径）：先扣费再取反
    会把空头手续费加成收益、系统性虚增 0.2pp。
    """
    if raw is None or direction not in ("up", "down"):
        return None
    signed = raw if direction == "up" else -raw
    return signed - cost


def summarize(rows: list[dict], key: str) -> dict | None:
    """按笔均值 + 胜率 + PF + 按日等权聚类 t。空样本返回 None。"""
    vals = [(float(r[key]), r["day"]) for r in rows if r.get(key) is not None]
    if not vals:
        return None
    v = [x for x, _ in vals]
    wins = [x for x in v if x > 0]
    gross_loss = abs(sum(x for x in v if x < 0))
    per_day: dict[date, list[float]] = {}
    for x, d in vals:
        per_day.setdefault(d, []).append(x)
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


def fmt(s: dict | None) -> str:
    if not s:
        return "n=  0"
    return (f"n={s['n']:5d} 日={s['days']:3d} 胜率={s['win'] * 100:5.1f}% "
            f"PF={(('%.2f' % s['pf']) if s['pf'] else ' n/a')} "
            f"净={s['avg']:+.3f}% 日t={(('%+.2f' % s['t']) if s['t'] is not None else ' n/a')}")


# ─────────────────────────────────── 主流程 ───────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback-settlements", type=int, default=PRIMARY_N)
    ap.add_argument("--holdout-days", type=int, default=21, help="test = 最近 N 天（上海日）")
    ap.add_argument("--out", default=str(DATA_DIR / "backtest_funding_signal.csv"))
    ap.add_argument("--json", action="store_true", help="只打印不落 CSV")
    args = ap.parse_args()

    s = get_settings(require_database=True)
    with get_connection(s.database_url) as conn:
        # 1) 价格窗（1h K 线边界）——决定哪些结算点可结算
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("SELECT MIN(open_time) lo, MAX(open_time) hi"
                        " FROM biz.asset_klines WHERE interval = '1h'")
            b = cur.fetchone()
        klo, khi = b["lo"], b["hi"]
        # 入场棒 = funding_time − 1h ≥ klo ⇒ funding_time ≥ klo + 1h
        # 最长持有期需 bar(open_time ≤ funding_time + 72h − 1h) 存在 ⇒ funding_time ≤ khi − 72h + 1h
        lo_ts = klo + timedelta(hours=1)
        hi_ts = khi - timedelta(hours=max(HORIZONS) - 1)
        # 价格窗按「上一根已收盘棒」对齐到整点：hi_ts 向下取整到小时
        hi_ts = hi_ts.replace(minute=0, second=0, microsecond=0)

        # 2) funding 全历史（含预热）
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(FUNDING_SQL, {"warm_lo": lo_ts - timedelta(days=20)})
            frows = [dict(r) for r in cur.fetchall()]
        if not frows:
            print("无 funding 数据")
            return 1

        # 3) 逐符号滑窗分位（每个 N 各算一遍）→ 标出价格窗内的结算点
        by_sym: dict[str, list[dict]] = {}
        for r in frows:
            by_sym.setdefault(r["symbol"], []).append(r)
        del frows

        pts: list[dict] = []          # 价格窗内的全部结算点（含基线）
        for sym, rs in by_sym.items():
            rs.sort(key=lambda r: r["funding_time"])
            rates = [float(r["rate"]) for r in rs]
            pcts = {n: rolling_pct(rates, n) for n in (args.lookback_settlements, *ROBUST_NS)}
            for i, r in enumerate(rs):
                ft = r["funding_time"]
                if lo_ts <= ft <= hi_ts:
                    pts.append({"symbol": sym, "funding_time": ft, "rate": rates[i],
                                "day": (ft + timedelta(hours=8)).date(),
                                "pcts": {n: pcts[n][i] for n in pcts}})
        del by_sym

        if not pts:
            print("价格窗内无结算点")
            return 1
        print(f"价格窗 {lo_ts:%Y-%m-%d %H:%M} → {hi_ts:%Y-%m-%d %H:%M} UTC；"
              f"可结算结算点 {len(pts)} 个")

    # 4) 分批取价（SQL 侧 LATERAL）+ 就地组装
    # ⚠️ 两个坑（2026-10-01 实测）：① 命名/服务端游标在这么重的 LATERAL 计划上会退化成
    #    反复 `FETCH FORWARD 20000`，实测卡死（服务端 idle in transaction 干等客户端）；
    #    ② 一次性取 93K×7 列在跨区链路上会长时间阻塞在 ClientWrite。故改为**普通游标 + 分批**，
    #    每批 `--chunk`（默认 12000）个点，既降峰值内存又可观测进度。
    rows: list[dict] = []
    CHUNK = 12000
    for i in range(0, len(pts), CHUNK):
        part = pts[i: i + CHUNK]
        with get_connection(s.database_url) as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute(price_sql(HORIZONS),
                            {"syms": [p["symbol"] for p in part],
                             "tss": [p["funding_time"] for p in part]})
                got = {r["symbol"] + "|" + r["funding_time"].isoformat(): dict(r)
                       for r in cur.fetchall()}
        for p in part:
            q = got.get(p["symbol"] + "|" + p["funding_time"].isoformat())
            if not q or q["entry_px"] is None:
                continue
            rec = {"symbol": p["symbol"], "funding_time": p["funding_time"], "day": p["day"],
                   "rate": p["rate"], "pcts": p["pcts"], "entry_px": float(q["entry_px"])}
            for h in HORIZONS:
                rec[f"raw_{h}h"] = fwd_ret(q["entry_px"], q.get(f"px_{h}h"))
            rows.append(rec)
        print(f"  取价 {min(i + CHUNK, len(pts))}/{len(pts)} → 有效 {len(rows)}")
        del got
    if not rows:
        print("无有效取价样本")
        return 1

    days = sorted({r["day"] for r in rows})
    if len(days) < 2:
        print("独立日不足 2，无法切分")
        return 1
    cut = days[-min(args.holdout_days, len(days) - 1)]
    seg = {"train": [r for r in rows if r["day"] < cut],
           "test": [r for r in rows if r["day"] >= cut], "ALL": rows}
    print(f"有效样本 {len(rows)} 点 / {len(days)} 天（{days[0]} → {days[-1]}）；"
          f"cutoff={cut}（test=最近 {args.holdout_days} 天，train={len(seg['train'])} 点）")

    # 6) 基线 = 无条件 always-long（同一批结算点的毛收益均值）
    print("\n基线（无条件 always-long，毛收益、不扣费）：")
    base: dict[str, dict] = {}
    for k in ("ALL", "train", "test"):
        base[k] = {h: summarize(seg[k], f"raw_{h}h") for h in HORIZONS}
        b = base[k][PRIMARY_H]
        print(f"  {k:5s} {PRIMARY_H}h {fmt(b)}")
        print(f"        {PRIMARY_H}h 毛均={b['avg']:+.3f}%" if b else "")

    out_rows: list[dict] = []

    def bucket_rows(seg_rows: list[dict], n_key: int, direction: str | None) -> list[dict]:
        """按（N, 方向）筛信号行，并写入对齐净收益列 `al_{h}h`。"""
        res = []
        for r in seg_rows:
            pct = r.get("pcts", {}).get(n_key)
            if pct is None:
                continue
            d = "up" if pct <= PCT_LO else ("down" if pct >= PCT_HI else None)
            if d is None or (direction and d != direction):
                continue
            rr = dict(r)
            rr["dir"] = d
            rr["pct"] = pct
            for h in HORIZONS:
                rr[f"al_{h}h"] = aligned_ret(r.get(f"raw_{h}h"), d)
            res.append(rr)
        return res

    def emit(rows_in, bucket, n_key, k, horizon, note=""):
        st = summarize(rows_in, f"al_{horizon}h")
        b = base[k][horizon]
        delta = (st["avg"] - b["avg"]) if (st and b) else None
        out_rows.append({
            "lookback": n_key, "bucket": bucket, "seg": k, "horizon_h": horizon,
            "n": (st["n"] if st else 0), "days": (st["days"] if st else 0),
            "win_rate": round(st["win"], 4) if st else None,
            "pf": (round(st["pf"], 3) if st and st["pf"] else None),
            "avg_net_pct": round(st["avg"], 4) if st else None,
            "daily_avg_pct": round(st["daily_avg"], 4) if st else None,
            "t_daily": (round(st["t"], 3) if st and st["t"] is not None else None),
            "base_avg_pct": round(b["avg"], 4) if b else None,
            "excess_pp": round(delta, 4) if delta is not None else None, "note": note,
        })
        return st, delta

    # 触发占比自检（应 ≈ P10 / P90；严重偏离即口径有问题）
    for label, d in (("多头", "up"), ("空头", "down")):
        cnt = len(bucket_rows(rows, args.lookback_settlements, d))
        print(f"  触发占比 {label}: {cnt}/{len(rows)} = {cnt / len(rows) * 100:.1f}%")

    # 7) 主口径：N=21，多头/空头/合并 × 持有期 × 段
    print(f"\n主口径（N={args.lookback_settlements} 结算点，分位 ≤{PCT_LO}/≥{PCT_HI}）：")
    primary = {}
    for label, direction in (("多头(负费率→做多)", "up"), ("空头(正费率→做空)", "down"), ("合并两侧", None)):
        print(f"  ── {label}")
        primary[label] = {}
        for k in ("ALL", "train", "test"):
            sub = bucket_rows(seg[k], args.lookback_settlements, direction)
            for h in HORIZONS:
                st, delta = emit(sub, label, args.lookback_settlements, k, h)
                if h == PRIMARY_H:
                    primary[label][k] = (st, delta)
                    print(f"     {k:5s} {h}h {fmt(st)} ｜ 基线 {base[k][h]['avg']:+.3f}% "
                          f"超额 {delta:+.3f}pp" if (st and delta is not None) else f"     {k:5s} {h}h no_sample")

    # 8) 稳健性档（仅描述，不参与判定）
    print("\n稳健性（不进判定，仅描述）：N=9/42 的 24h 合并两侧")
    for n_key in ROBUST_NS:
        for k in ("ALL", "train", "test"):
            sub = bucket_rows(seg[k], n_key, None)
            emit(sub, "combined", n_key, k, PRIMARY_H, note="robustness")
            st = summarize(sub, f"al_{PRIMARY_H}h")
            print(f"  N={n_key:2d} {k:5s} {fmt(st)}")

    # 9) 判定（预登记判据，只认主口径 + 合并两侧）
    print("\n" + "=" * 78)
    reasons: list[str] = []
    for k in ("train", "test"):
        st, delta = primary["合并两侧"].get(k, (None, None))
        if not st or st["n"] < MIN_N or st["days"] < MIN_DAYS:
            reasons.append(f"{k}: 样本不足（n={st['n'] if st else 0}）")
            continue
        if st["avg"] <= PASS_NET:
            reasons.append(f"{k}: 净均 {st['avg']:+.3f}% ≤ +{PASS_NET}%")
        if st["t"] is None or st["t"] <= 0:
            reasons.append(f"{k}: 日 t={('%.2f' % st['t']) if st['t'] is not None else 'n/a'} ≤ 0")
        if delta is not None and delta <= 0:
            reasons.append(f"{k}: 相对 always-long 基线无超额（{delta:+.3f}pp ≤ 0）")
    passed = not reasons
    verdict = "PASS" if passed else "FAIL"
    print(f"结论【{verdict}】" + ("：H1 在主口径双侧成立 → 可进入下一阶段（模拟盘 ≥30 独立日）。"
                                  if passed else "：按工单 §1.1 预登记判据，H1 **不成立**。"))
    for r in reasons:
        print(f"  ✗ {r}")
    if not passed:
        print("  ⇒ 不得进入模拟盘、不得改任何线上代码；如实记录并回到方向选择。")
    print("  ⚠️ 边界：价格史仅 105 天且单边上涨，train/test 同在上涨 regime；"
          "通过也不等于跨 regime 有效。")

    if args.json:
        print("\n" + json.dumps(out_rows, ensure_ascii=False, indent=2))
        return 0
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)
    print(f"\nCSV → {out}（{len(out_rows)} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
