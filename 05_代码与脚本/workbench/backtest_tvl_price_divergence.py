#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""TVL ↔ 价格背离 回测：背离是否为好的投资机会（只读，2026-10-04）。

数据：
  - biz.protocol_metric_daily：资产×日 TVL（source_code='dl'，2026-08-28 ~ 2026-10-03）
  - biz.asset_market_daily：资产日行情（cmc > cmc_historical > binance_klines 去重）
  - core.asset：symbol / market_cap_rank

背离定义（7 日慢变量口径）：
  - tvl_ret_7d(T)  = TVL(T)/TVL(T-k) - 1，k = T 往前第 7 个有 TVL 的日（序列自算，弃用脏字段）
  - ret_7d(T)      = price(T)/price(T-7) - 1（价格 7 日收益）
  - 背离分(T)      = pct_rank(tvl_ret_7d) - pct_rank(ret_7d) ∈ [-1, 1]
      >0 = 正背离：TVL 相对价格更强（基本面锁仓改善但价格未跟 / 价格跌得多但 TVL 扛住）
      <0 = 负背离：价格相对 TVL 更强（价格涨但 TVL 未确认 / TVL 缩水但价格还涨）

策略假设（待检验）：
  - 正背离 → 价格向 TVL 隐含价值回归（补涨），是买入机会
  - 负背离 → 价格透支（回落），应回避/做空

回测纪律（无前视）：
  - 信号用 T 日及之前数据计算，T 日收盘后入场
  - fwd_h = price(T+h)/price(T) - 1，h=1/3/5/10
  - 超额收益 excess_h = fwd_h - BTC 同期 h 日收益
  - 收益异常过滤：|ret| > ±300% 的行剔除
  - 输出：背离分 5 桶 / 2×2 象限 / 极端背离事件 / 多空 spread，绝对 + 超额双口径
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import statistics
import sys
from collections import defaultdict
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, os.path.join(_SCRIPTS, "src"))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

HORIZONS = (1, 3, 5, 10)
MIN_N = 30
MIN_ASSET_N = 5
MIN_DAYS = 12           # 至少 12 个 TVL 日（T-7 前有覆盖）
MIN_MED_ABS_RET = 0.003
MAX_ABS_RET = 3.0
TVL_WIN = 7             # TVL 累计窗口（回看至多 7 个 TVL 覆盖日）
PRICE_WIN = 7           # 价格 7 日收益窗口
OUT_DIR = Path(__file__).resolve().parent.parent / "data"
WINDOW_START = "2026-08-01"


# ── 数据加载（优先本地缓存，避免远端 fetchall 网络传输 3 分钟+）──────────

CACHE_DIR = OUT_DIR / "cache_tvl_price"


def _load_from_cache(cache_dir: Path):
    """从本地缓存 CSV 加载 (tvl_rows, market, meta)。缓存缺失时返回 None。"""
    tvl_path, market_path, meta_path = (cache_dir / "tvl.csv"), (cache_dir / "market.csv"), (cache_dir / "meta.csv")
    if not all(p.exists() for p in (tvl_path, market_path, meta_path)):
        return None
    tvl_rows, market, meta = [], {}, {}
    with open(tvl_path) as f:
        for r in csv.DictReader(f):
            tvl_rows.append((int(r["asset_id"]), r["date"], float(r["tvl"])))
    with open(market_path) as f:
        src_prio = {"cmc": 0, "cmc_historical": 1, "binance_klines": 2}
        best: dict[int, dict] = defaultdict(dict)
        for r in csv.DictReader(f):
            aid, d, src = int(r["asset_id"]), r["date"], r["source"]
            price = float(r["price"])
            mc = float(r["mcap"]) if r["mcap"] else None
            prio = src_prio.get(src, 9)
            cur_best = best[aid].get(d)
            if cur_best is None or prio < cur_best[0]:
                best[aid][d] = (prio, price, mc)
        market = {aid: {d: (v[1], v[2]) for d, v in days.items()} for aid, days in best.items()}
    with open(meta_path) as f:
        for r in csv.DictReader(f):
            meta[int(r["asset_id"])] = {"symbol": r["symbol"], "name": r["name"],
                                        "rank": int(r["rank"]) if r["rank"] else None}
    return tvl_rows, market, meta


def load_all(conn, cache_dir: Path | None = None):
    """加载 (tvl_rows, market, meta)。cache_dir 给定且缓存存在 → 本地读；否则 DB 拉并落缓存。"""
    if cache_dir is not None:
        cached = _load_from_cache(cache_dir)
        if cached is not None:
            print(f"[cache] 本地缓存加载: tvl={len(cached[0])} 行, market={len(cached[1])} 资产, meta={len(cached[2])}")
            return cached
    # DB 拉取
    with conn.cursor() as cur:
        cur.execute("""
            SELECT asset_id, metric_date, tvl FROM biz.protocol_metric_daily
            WHERE metric_date >= %s::date AND tvl IS NOT NULL AND tvl > 0
            ORDER BY asset_id, metric_date""", (WINDOW_START,))
        tvl_rows = [(int(a), d, float(t)) for a, d, t in cur.fetchall()]
        asset_ids = {r[0] for r in tvl_rows}
        cur.execute("SELECT asset_id FROM core.asset WHERE canonical_symbol = 'BTC'")
        row = cur.fetchone()
        if row:
            asset_ids.add(int(row[0]))
    market = load_market_daily(conn, asset_ids)
    meta = load_asset_meta(conn, asset_ids)
    # 落缓存
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        with open(cache_dir / "tvl.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["asset_id", "date", "tvl"])
            for a, d, t in tvl_rows:
                w.writerow([a, d, t])
        with open(cache_dir / "market.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["asset_id", "date", "source", "price", "mcap"])
            with conn.cursor() as cur:
                ids = sorted(asset_ids)
                for i in range(0, len(ids), 400):
                    part = ids[i:i + 400]
                    cur.execute("""
                        SELECT asset_id, market_date, source_code, price_usd, market_cap
                        FROM biz.asset_market_daily WHERE market_date >= %s::date
                          AND asset_id = ANY(%s::int[]) AND price_usd IS NOT NULL AND price_usd > 0
                          AND (is_anomaly IS NOT TRUE OR is_anomaly IS NULL)
                        ORDER BY asset_id, market_date""", (WINDOW_START, part))
                    for a, d, src, price, mc in cur.fetchall():
                        w.writerow([a, d.isoformat(), src, price, mc if mc is not None else ""])
        with open(cache_dir / "meta.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["asset_id", "symbol", "name", "rank"])
            for a, m in sorted(meta.items()):
                w.writerow([a, m["symbol"], m["name"], m["rank"] if m["rank"] is not None else ""])
        print(f"[cache] 已落缓存: {cache_dir}")
    return tvl_rows, market, meta


def load_market_daily(conn, asset_ids: set[int], chunk: int = 400) -> dict[int, dict]:
    src_prio = {"cmc": 0, "cmc_historical": 1, "binance_klines": 2}
    best: dict[int, dict] = defaultdict(dict)
    ids = sorted(asset_ids)
    with conn.cursor() as cur:
        for i in range(0, len(ids), chunk):
            part = ids[i:i + chunk]
            cur.execute("""
                SELECT asset_id, market_date, source_code, price_usd, market_cap
                FROM biz.asset_market_daily
                WHERE market_date >= %s::date AND asset_id = ANY(%s::int[])
                  AND price_usd IS NOT NULL AND price_usd > 0
                  AND (is_anomaly IS NOT TRUE OR is_anomaly IS NULL)
                ORDER BY asset_id, market_date
                """, (WINDOW_START, part))
            for aid, d, src, price, mc in cur.fetchall():
                prio = src_prio.get(src, 9)
                cur_best = best[aid].get(d)
                if cur_best is None or prio < cur_best[0]:
                    best[aid][d] = (prio, float(price), float(mc) if mc else None)
    return {aid: {d: (v[1], v[2]) for d, v in days.items()} for aid, days in best.items()}


def load_asset_meta(conn, asset_ids: set[int]) -> dict[int, dict]:
    out: dict[int, dict] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT asset_id, canonical_symbol, canonical_name, market_cap_rank "
            "FROM core.asset WHERE asset_id = ANY(%s::int[])",
            (list(asset_ids),))
        for aid, sym, name, rank in cur.fetchall():
            out[aid] = {"symbol": str(sym), "name": str(name or ""), "rank": rank}
    return out


# ── 统计工具 ──────────────────────────────────────────────────

def _pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return float("nan")
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return float("nan")
    return cov / math.sqrt(vx * vy)


def corr_stats(xs, ys, min_n=MIN_N):
    n = len(xs)
    if n < min_n:
        return {"n": n}
    pr = _pearson(xs, ys)
    t = pr * math.sqrt(n - 2) / math.sqrt(1 - pr * pr) if abs(pr) < 1 else float("nan")
    return {"n": n, "pearson": pr, "t": t}


def stats_of(rets):
    if not rets:
        return {"n": 0}
    mean = sum(rets) / len(rets)
    win = sum(1 for x in rets if x > 0) / len(rets)
    return {"n": len(rets), "mean": mean, "win": win}


def rank_pcts(values: list[float]) -> list[float]:
    """返回每个值在全序列中的分位（0~1），O(n log n)。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    for pos, idx in enumerate(order):
        out[idx] = (pos + 1) / len(values)
    return out


def enrich_divergence(panel: list[dict]) -> None:
    """预计算每行 TVL/价格 7 日变化分位与背离分（O(n log n)，避免逐行 O(n) 分位）。"""
    tvl_v = [r["tvl_ret_7d"] for r in panel]
    px_v = [r["ret_7d"] for r in panel]
    tvl_p = rank_pcts(tvl_v)
    px_p = rank_pcts(px_v)
    for r, tp, pp in zip(panel, tvl_p, px_p):
        r["tvl_pct"] = tp
        r["px_pct"] = pp
        r["div"] = tp - pp


# ── 面板构建 ──────────────────────────────────────────────────

def build_panel(tvl_rows, market, meta, min_days=MIN_DAYS, min_med_abs_ret=MIN_MED_ABS_RET):
    tvl_by_asset: dict[int, dict] = defaultdict(dict)
    for aid, d, tvl in tvl_rows:
        tvl_by_asset[aid][d] = float(tvl)

    # BTC 前瞻收益（超额基准）
    btc_fwd: dict[int, dict] = {}
    btc_id = next((aid for aid, m in meta.items() if m["symbol"] == "BTC"), None)
    if btc_id is not None:
        bdates = sorted(market.get(btc_id, {}))
        bn = len(bdates)
        for i, d in enumerate(bdates):
            btc_fwd[d] = {}
            for h in HORIZONS:
                if i + h < bn:
                    btc_fwd[d][h] = market[btc_id][bdates[i + h]][0] / market[btc_id][bdates[i]][0] - 1.0

    panel: list[dict] = []
    for aid, tvls in tvl_by_asset.items():
        meta_a = meta.get(aid)
        if not meta_a or meta_a["rank"] is None:
            continue
        if len(tvls) < min_days:
            continue
        px = market.get(aid)
        if not px or len(px) < min_days:
            continue
        tdates = sorted(tvls)
        dates = sorted(px)
        p_idx = {d: i for i, d in enumerate(dates)}
        n = len(dates)
        # 稳定币/低波动过滤（用日收益中位数）
        rets = {}
        for i in range(1, len(dates)):
            rets[dates[i]] = px[dates[i]][0] / px[dates[i - 1]][0] - 1.0
        if not rets:
            continue
        if statistics.median([abs(v) for v in rets.values()]) < min_med_abs_ret:
            continue

        for i, d in enumerate(dates):
            if i + 1 >= n or i < PRICE_WIN:
                continue
            if d not in tvls:
                continue
            # 7 日价格收益（T-7 价格须存在）
            p7 = dates[i - PRICE_WIN]
            if p7 not in px or px[p7][0] <= 0:
                continue
            ret_7d = px[d][0] / px[p7][0] - 1.0
            # 7 日 TVL 变化（回看至多 7 个 TVL 覆盖日）
            j = tdates.index(d) if d in tvls else -1
            if j < TVL_WIN:
                continue
            t0 = tvls[tdates[j - TVL_WIN]]
            if t0 <= 0:
                continue
            tvl_ret_7d = tvls[d] / t0 - 1.0
            # 收益异常过滤
            if abs(ret_7d) > MAX_ABS_RET:
                continue
            close_t = px[d][0]
            fwd = {}
            bad = False
            for h in HORIZONS:
                if i + h < n:
                    fwd[h] = px[dates[i + h]][0] / close_t - 1.0
                    if abs(fwd[h]) > MAX_ABS_RET:
                        bad = True  # 任一前瞻异常 → 整行剔除（口径一致）
            if bad:
                continue
            panel.append({
                "asset_id": aid, "symbol": meta_a["symbol"], "date": d,
                "tvl_ret_7d": tvl_ret_7d, "ret_7d": ret_7d,
                "fwd": fwd, "btc_fwd": btc_fwd.get(d, {}),
            })
    return panel


def excess_ret(row, h):
    base = row["fwd"].get(h)
    btc = row["btc_fwd"].get(h)
    if base is None or btc is None:
        return float("nan")
    return base - btc


# ── 输出 ──────────────────────────────────────────────────────

def print_5bucket(panel, title, use_excess=False):
    print(f"\n=== {title} ===")
    divs = rank_pcts([r["div"] for r in panel])
    buckets = {f"Q{i}": [] for i in range(1, 6)}
    for r, dp in zip(panel, divs):
        q = min(5, int(dp * 5) + 1)
        buckets[f"Q{q}"].append(r)
    print(f"  {'桶':<4} {'背离分区间':<14} {'n':>5} | " + " | ".join(f"H{h}均/胜" for h in HORIZONS))
    for i in range(1, 6):
        rows = buckets[f"Q{i}"]
        lo, hi = (i - 1) / 5, i / 5
        cells = []
        for h in HORIZONS:
            if use_excess:
                rets = [excess_ret(r, h) for r in rows if h in r["fwd"] and not math.isnan(excess_ret(r, h))]
            else:
                rets = [r["fwd"][h] for r in rows if h in r["fwd"]]
            st = stats_of(rets)
            cells.append(f"{st['mean']*100:+6.2f}/{st['win']*100:3.0f}%" if st["n"] >= MIN_N else "  --/--")
        dmed = statistics.median([r["div"] for r in rows]) if rows else float("nan")
        print(f"  Q{i}   [{lo:.0%},{hi:.0%})  {len(rows):>5} | " + " | ".join(cells) + f"   (中位背离分{dmed:+.2f})")
    # 多空 spread：Q5(正背离) - Q1(负背离)
    spreads = []
    for h in HORIZONS:
        if use_excess:
            q5 = [excess_ret(r, h) for r in buckets["Q5"] if h in r["fwd"] and not math.isnan(excess_ret(r, h))]
            q1 = [excess_ret(r, h) for r in buckets["Q1"] if h in r["fwd"] and not math.isnan(excess_ret(r, h))]
        else:
            q5 = [r["fwd"][h] for r in buckets["Q5"] if h in r["fwd"]]
            q1 = [r["fwd"][h] for r in buckets["Q1"] if h in r["fwd"]]
        if len(q5) >= MIN_N and len(q1) >= MIN_N:
            s = statistics.mean(q5) - statistics.mean(q1)
            # 简单两样本 t（Welch 近似）
            v5, v1 = statistics.pstdev(q5) ** 2, statistics.pstdev(q1) ** 2
            se = math.sqrt(v5 / len(q5) + v1 / len(q1))
            t = s / se if se > 0 else float("nan")
            spreads.append(f"H{h}: {s*100:+.2f}pp (t={t:+.2f})")
        else:
            spreads.append(f"H{h}: n不足")
    print(f"  SPREAD  Q5(正背离)−Q1(负背离) | " + " | ".join(spreads))


def print_quadrant(panel, title, use_excess=False):
    print(f"\n=== {title} ===")
    tvl_v = [r["tvl_ret_7d"] for r in panel]
    px_v = [r["ret_7d"] for r in panel]
    tmed = statistics.median(tvl_v)
    pmed = statistics.median(px_v)
    print(f"  （阈值：TVL 7日变化中位 {tmed*100:+.1f}%；价格 7日收益中位 {pmed*100:+.1f}%）")
    quads = {
        "TVL涨/价格涨(同步)": [r for r in panel if r["tvl_ret_7d"] >= tmed and r["ret_7d"] >= pmed],
        "TVL涨/价格跌(正背离)": [r for r in panel if r["tvl_ret_7d"] >= tmed and r["ret_7d"] < pmed],
        "TVL跌/价格跌(同步)": [r for r in panel if r["tvl_ret_7d"] < tmed and r["ret_7d"] < pmed],
        "TVL跌/价格涨(负背离)": [r for r in panel if r["tvl_ret_7d"] < tmed and r["ret_7d"] >= pmed],
    }
    print(f"  {'象限':<24} {'n':>5} | " + " | ".join(f"H{h}均/胜" for h in HORIZONS))
    for name, rows in quads.items():
        cells = []
        for h in HORIZONS:
            if use_excess:
                rets = [excess_ret(r, h) for r in rows if h in r["fwd"] and not math.isnan(excess_ret(r, h))]
            else:
                rets = [r["fwd"][h] for r in rows if h in r["fwd"]]
            st = stats_of(rets)
            cells.append(f"{st['mean']*100:+6.2f}/{st['win']*100:3.0f}%" if st["n"] >= MIN_N else "  --/--")
        print(f"  {name:<24} {len(rows):>5} | " + " | ".join(cells))


def print_extreme_div(panel, title, use_excess=False):
    print(f"\n=== {title} ===")
    groups = [
        ("强正背离 TVL≥70%且价格≤30%", lambda r: r["tvl_pct"] >= 0.70 and r["px_pct"] <= 0.30),
        ("正背离 TVL≥60%且价格≤40%", lambda r: r["tvl_pct"] >= 0.60 and r["px_pct"] <= 0.40),
        ("强负背离 TVL≤30%且价格≥70%", lambda r: r["tvl_pct"] <= 0.30 and r["px_pct"] >= 0.70),
        ("负背离 TVL≤40%且价格≥60%", lambda r: r["tvl_pct"] <= 0.40 and r["px_pct"] >= 0.60),
    ]
    for name, sel in groups:
        rows = [r for r in panel if sel(r)]
        cells = []
        for h in HORIZONS:
            if use_excess:
                rets = [excess_ret(r, h) for r in rows if h in r["fwd"] and not math.isnan(excess_ret(r, h))]
            else:
                rets = [r["fwd"][h] for r in rows if h in r["fwd"]]
            st = stats_of(rets)
            cells.append(f"H{h} {st['mean']*100:+.2f}%/{st['win']*100:.0f}%" if st["n"] >= MIN_N else f"H{h} n={st['n']}")
        print(f"  {name:<30} n={len(rows):>4} | " + " | ".join(cells))


def main():
    ap = argparse.ArgumentParser(description="TVL ↔ 价格背离回测")
    ap.add_argument("--min-days", type=int, default=MIN_DAYS)
    ap.add_argument("--out-csv", default="tvl_divergence_panel.csv")
    ap.add_argument("--no-cache", action="store_true", help="忽略本地缓存，强制从 DB 拉取并重写缓存")
    args = ap.parse_args()

    s = get_settings()
    cache_dir = None if args.no_cache else CACHE_DIR
    with get_connection(s.database_url) as conn:
        tvl_rows, market, meta = load_all(conn, cache_dir=cache_dir)
    print(f"tvl 日行={len(tvl_rows)}；行情资产={len(market)}；元数据资产={len(meta)}")

    panel = build_panel(tvl_rows, market, meta, min_days=args.min_days)
    if not panel:
        print("无有效面板样本")
        return
    enrich_divergence(panel)
    symbols = sorted({r["symbol"] for r in panel})
    days = sorted({r["date"] for r in panel})
    print(f"\n########## TVL ↔ 价格背离回测（{len(symbols)} 资产 × {len(days)} 日） ##########")
    print(f"  面板行={len(panel)}；{days[0]} ~ {days[-1]}；背离窗口：TVL 7日 vs 价格 7日")

    print_5bucket(panel, "1. 背离分 5 桶：前瞻收益（绝对口径）")
    print_quadrant(panel, "2. 2×2 象限（TVL 7日 vs 价格 7日，中位切分）")
    print_extreme_div(panel, "3. 极端背离事件（分位组合）")
    print_5bucket(panel, "4. 稳健性：背离分 5 桶（超额=减BTC）", use_excess=True)
    print_quadrant(panel, "5. 稳健性：象限（超额=减BTC）", use_excess=True)
    print_extreme_div(panel, "6. 稳健性：极端背离（超额=减BTC）", use_excess=True)

    if args.out_csv:
        path = OUT_DIR / args.out_csv
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = ["date", "symbol", "tvl_ret_7d", "ret_7d", "tvl_pct", "px_pct", "div"] + \
                     [f"fwd{h}" for h in HORIZONS]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in sorted(panel, key=lambda x: (x["date"], x["symbol"])):
                row = {"date": r["date"].isoformat() if hasattr(r["date"], "isoformat") else str(r["date"]),
                       "symbol": r["symbol"],
                       "tvl_ret_7d": round(r["tvl_ret_7d"], 6), "ret_7d": round(r["ret_7d"], 6),
                       "tvl_pct": round(r["tvl_pct"], 4), "px_pct": round(r["px_pct"], 4),
                       "div": round(r["div"], 4)}
                for h in HORIZONS:
                    row[f"fwd{h}"] = round(r["fwd"][h], 6) if h in r["fwd"] else ""
                w.writerow(row)
        print(f"\n[CSV] 面板已写: {path}")
    print("\n完成。")


if __name__ == "__main__":
    main()
