#!/usr/bin/env python3
"""领先-滞后对深挖 · 事件研究 + 催化剂归因。

对上一轮 scan_lead_lag.py 扫出的最强日频对（默认 BCH→ONDO，BCH 领先 ONDO 约 2 天）
做四件事：

1. 复验：日频滞后互相关全谱（lag −max..+max），确认最优滞后与强度；
2. 事件研究：以「领先币超额 ≥ 阈值」为起飞日，测跟随币在最优滞后后的跟涨
   概率 / 平均超额 / 基线提升（lift），并做首尾分段 + 剔除大阳线的稳健性检查；
3. 催化剂归因：把领先币起飞日与跟随币跟涨窗口叠加各自催化剂事件，判断
   「叙事驱动（同一催化剂先打领先币、再传导跟随币）」还是「独立异动」；
4. 输出可读报告 + JSON。

运行：
    python dive_lead_lag_pair.py                          # 默认 BCH→ONDO
    python dive_lead_lag_pair.py --leader LTC --follower HBAR --event-thresh 4
    python dive_lead_lag_pair.py --selftest
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent
_SCRIPTS_SRC = _HERE.parent / "scripts" / "src"
for p in (_SCRIPTS_SRC, _HERE.parent / "scripts" / "bin"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import scan_lead_lag as sl  # noqa: E402  复用 symbol_candidates / _to_utc64 / get_db

UTC = timezone.utc


# ═══════════════════════════════════════════════════════════════
#  一、纯函数层
# ═══════════════════════════════════════════════════════════════

def resolve_symbol(sym: str) -> str:
    """'BCHUSDT' / 'bch' → 裸大写符号 'BCH'。"""
    s = (sym or "").upper()
    return s[:-4] if s.endswith("USDT") and len(s) > 4 else s


def lead_lag_profile(ret_l: np.ndarray, ret_f: np.ndarray, max_lag: int) -> list[dict]:
    """两日频收益序列全滞后互相关谱。k>0 = 领先币领先跟随币 k 天。"""
    out = []
    n = len(ret_l)
    for k in range(-max_lag, max_lag + 1):
        if k >= 0:
            x, y = ret_l[: n - k], ret_f[k:]
        else:
            x, y = ret_l[-k:], ret_f[: n + k]
        mask = np.isfinite(x) & np.isfinite(y)
        c = sl._pearson(x, y)
        out.append({"lag": k, "corr": c, "n": int(mask.sum())})
    return out


def surge_episodes(excess: np.ndarray, thresh: float) -> list[int]:
    """起飞日：超额 ≥ thresh 且前一日未超阈（连续涨只记起点）。"""
    hit = np.isfinite(excess) & (excess >= thresh)
    return [i for i in range(1, len(hit)) if hit[i] and not hit[i - 1]]


def event_follow(excess_l: np.ndarray, excess_f: np.ndarray, lag: int, win: int,
                 thresh: float, fee: float = 0.0, judge_min_events: int = 20) -> dict:
    """事件研究：领先币起飞日 → 跟随币 [lag, lag+win) 窗口的跟涨统计。

    返回事件侧与基线侧的 (n, 跟涨概率, 平均超额, 累计平均超额)。
    A5 扣费：fee 与 excess 同单位（本脚本 excess 为 %，对齐结算 0.1% 双边费用 0.1）。
    A3 判读门槛：n_events ≥ judge_min_events 才算 sample_ready（只披露不判读）。
    """
    n = len(excess_l)
    starts = surge_episodes(excess_l, thresh)
    ev_day: list[float] = []
    ev_cum: list[float] = []
    for i in starts:
        j = i + lag
        if j >= n:
            continue
        if np.isfinite(excess_f[j]):
            ev_day.append(excess_f[j] - fee)
        if j + win <= n:
            seg = excess_f[j: j + win]
            if np.isfinite(seg).all():
                ev_cum.append(float(seg.mean()) - fee)
    # 基线：剔除「任何事件跟随窗口」内的起点（审计 A4）——原实现只剔事件起点
    # 本身，事件窗口与基线窗口重叠的日子会把事件收益算进基线，压低「提升度」。
    # 基线起点 i 的窗口是 [i+lag, i+lag+win)，与事件 s 的窗口重叠 ⟺ i ∈ [s, s+win)。
    blocked = set()
    for s in starts:
        for t in range(s, min(s + win, n)):
            blocked.add(t)
    base_day: list[float] = []
    base_cum: list[float] = []
    for i in range(n):
        j = i + lag
        if j >= n or i in blocked:
            continue
        if np.isfinite(excess_f[j]):
            base_day.append(excess_f[j] - fee)
        if j + win <= n:
            seg = excess_f[j: j + win]
            if np.isfinite(seg).all():
                base_cum.append(float(seg.mean()) - fee)

    def stat(vals: list[float]) -> tuple[int, float | None, float | None]:
        if not vals:
            return 0, None, None
        a = np.asarray(vals)
        return len(a), float((a > 0).mean()), float(a.mean())

    ne, pe, me = stat(ev_day)
    nb, pb, mb = stat(base_day)
    nec, pec, mec = stat(ev_cum)
    nbc, pbc, mbc = stat(base_cum)
    return {
        "lag": lag, "win": win, "thresh": thresh, "n_events": len(starts),
        # 本脚本 excess/fee 单位都是 %，fee_pct 直接取 fee（不再 ×100）
        "fee_pct": round(fee, 4),
        "sample_ready": bool(len(starts) >= judge_min_events),
        "day": {"n": ne, "follow_prob": pe, "mean_excess": me,
                "base_n": nb, "base_prob": pb, "base_mean": mb,
                "lift": (pe / pb) if (pe is not None and pb) else None},
        "cum_win": {"n": nec, "follow_prob": pec, "mean_excess": mec,
                    "base_n": nbc, "base_prob": pbc, "base_mean": mbc,
                    "lift": (pec / pbc) if (pec is not None and pbc) else None},
    }


# ═══════════════════════════════════════════════════════════════
#  二、DB 读取层
# ═══════════════════════════════════════════════════════════════

def resolve_asset_ids(conn, symbols: list[str]) -> dict[str, int]:
    """裸符号 → asset_id（沿用 scan_daemon 的 market_cap_rank 定序防张冠李戴）。"""
    out: dict[str, int] = {}
    with conn.cursor() as cur:
        for sym in symbols:
            for cand in sl.symbol_candidates(sym):
                cur.execute(
                    "SELECT asset_id FROM core.asset WHERE canonical_symbol = %s "
                    "ORDER BY market_cap_rank NULLS LAST, asset_id LIMIT 1", (cand,))
                r = cur.fetchone()
                if r:
                    out[resolve_symbol(sym)] = r[0]
                    break
    return out


def load_daily_series(conn, asset_ids: list[int], days: int) -> dict[int, dict]:
    """asset_id → {dates: np.array(date), closes: np.array(float)}（三源去重）。"""
    import psycopg.rows

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT asset_id, market_date, price_usd FROM (
                SELECT v.asset_id, v.market_date, v.price_usd::float8 AS price_usd,
                       ROW_NUMBER() OVER (PARTITION BY v.asset_id, v.market_date
                                          ORDER BY CASE v.source_code
                                              WHEN 'cmc' THEN 1
                                              WHEN 'cmc_historical' THEN 2
                                              ELSE 9 END) AS rn
                FROM biz.asset_market_daily v
                WHERE v.asset_id = ANY(%s) AND v.price_usd > 0
                  AND v.market_date >= CURRENT_DATE - %s
            ) t WHERE rn = 1 ORDER BY asset_id, market_date
            """,
            (asset_ids, days),
        )
        buf: dict[int, list] = defaultdict(list)
        for r in cur.fetchall():
            buf[r["asset_id"]].append((r["market_date"], r["price_usd"]))
    out = {}
    for aid, rows in buf.items():
        rows.sort(key=lambda t: t[0])
        out[aid] = {
            "dates": np.asarray([r[0] for r in rows], dtype="datetime64[D]"),
            "closes": np.asarray([r[1] for r in rows]),
        }
    return out


def load_catalysts(conn, asset_ids: list[int], since: datetime) -> dict[int, list[dict]]:
    """asset_id → 催化剂列表（含 G1 分级）。"""
    import psycopg.rows

    out: dict[int, list[dict]] = defaultdict(list)
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT ac.asset_id, ac.catalyst_id, ac.title, ac.published_at,
                   ac.event_category, ac.ai_event_type, ac.ai_sentiment,
                   g.catalyst_kind, g.base_strength
            FROM biz.asset_catalyst ac
            LEFT JOIN biz.catalyst_grade g ON g.catalyst_id = ac.catalyst_id
            WHERE ac.asset_id = ANY(%s) AND ac.published_at >= %s
            ORDER BY ac.published_at
            """,
            (asset_ids, since),
        )
        for r in cur.fetchall():
            title = (r["title"] or "").strip()
            if not title or title.lower() == "null":  # 数据质量：部分行 title 为 'null' 占位
                continue
            out[r["asset_id"]].append({
                "catalyst_id": r["catalyst_id"],
                "title": title, "published_at": r["published_at"],
                "category": r["event_category"], "kind": r["catalyst_kind"],
                "strength": r["base_strength"], "sentiment": r["ai_sentiment"],
                "event_type": r["ai_event_type"],
            })
    return out


def catalysts_near(dt: datetime, catalysts: list[dict], within_days: int) -> list[dict]:
    """取 dt 前后 within_days 天内的催化剂。"""
    return [c for c in catalysts
            if abs((c["published_at"].replace(tzinfo=UTC) if c["published_at"].tzinfo is None
                    else c["published_at"].astimezone(UTC)) - dt.astimezone(UTC))
            <= timedelta(days=within_days)]


# ═══════════════════════════════════════════════════════════════
#  三、报告
# ═══════════════════════════════════════════════════════════════

def fmt_pct(v, d=1):
    return "-" if v is None else f"{v*100:.{d}f}%"


def fmt_num(v, d=2):
    return "-" if v is None else f"{v:.{d}f}"


def run(conn, args) -> dict:
    leader = resolve_symbol(args.leader)
    follower = resolve_symbol(args.follower)
    ids = resolve_asset_ids(conn, [leader, follower, "BTC"])
    missing = [s for s in (leader, follower, "BTC") if s not in ids]
    if missing:
        raise RuntimeError(f"无法解析 asset_id：{missing}")
    series = load_daily_series(conn, list(ids.values()), args.days)
    dates_l = series[ids[leader]]["dates"]
    closes_l = series[ids[leader]]["closes"]
    dates_f = series[ids[follower]]["dates"]
    closes_f = series[ids[follower]]["closes"]
    dates_b = series[ids["BTC"]]["dates"]
    closes_b = series[ids["BTC"]]["closes"]

    # 对齐三序列到公共日期轴
    common = np.intersect1d(np.intersect1d(dates_l, dates_f), dates_b)
    pos = {aid: {np.datetime64(d): p for p, d in enumerate(series[aid]["dates"])}
           for aid in ids.values()}
    cl = np.asarray([closes_l[pos[ids[leader]][d]] for d in common])
    cf = np.asarray([closes_f[pos[ids[follower]][d]] for d in common])
    cb = np.asarray([closes_b[pos[ids["BTC"]][d]] for d in common])

    def excess(c):
        r = np.full(len(c), np.nan)
        rb = np.full(len(c), np.nan)
        r[1:] = c[1:] / c[:-1] - 1.0
        rb[1:] = cb[1:] / cb[:-1] - 1.0
        return (r - rb) * 100.0  # %

    ex_l, ex_f = excess(cl), excess(cf)
    # A6 winsorize：本脚本 excess 单位为 %，直接按 ±winsorize_pct% 限幅（<=0 关闭）
    if args.winsorize_pct > 0:
        ex_l = np.clip(ex_l, -args.winsorize_pct, args.winsorize_pct)
        ex_f = np.clip(ex_f, -args.winsorize_pct, args.winsorize_pct)
    window = str(common[0])[:10] + " ~ " + str(common[-1])[:10]

    # 1. 全谱复验
    profile = [p for p in lead_lag_profile(ex_l, ex_f, args.max_lag) if p["corr"] is not None]
    best = max(profile, key=lambda p: abs(p["corr"]))
    # 同步相关
    sync = sl._pearson(ex_l, ex_f)
    # 首尾分段稳健性
    half = len(ex_l) // 2
    first = sl._pearson(ex_l[:half], ex_f[:half])
    second = sl._pearson(ex_l[half:], ex_f[half:])

    # 2. 事件研究（A5 扣费 + A3 判读门槛）
    lag = best["lag"] if args.follow_win == 0 else args.follow_win
    ev = event_follow(ex_l, ex_f, lag if lag > 0 else 1, args.win_days, args.event_thresh,
                      fee=args.fee_pct, judge_min_events=args.judge_min_events)

    # 3. 催化剂归因
    since = datetime.combine(np.datetime64(common[0]).astype("datetime64[D]").astype(object),
                             datetime.min.time(), tzinfo=UTC) - timedelta(days=2)
    cats = load_catalysts(conn, list(ids.values()), since)
    cat_l = cats.get(ids[leader], [])
    cat_f = cats.get(ids[follower], [])
    episodes = surge_episodes(ex_l, args.event_thresh)
    annot = []
    n_surge = len(episodes)
    n_lead_cat = 0
    n_follow_cat = 0
    for i in episodes:
        d = np.datetime64(common[i]).astype("datetime64[D]").astype(object)
        dt = datetime.combine(d, datetime.min.time(), tzinfo=UTC)
        lc = catalysts_near(dt, cat_l, 1)
        fc = catalysts_near(dt + timedelta(days=lag), cat_f, args.win_days)
        if lc:
            n_lead_cat += 1
        if fc:
            n_follow_cat += 1
        j = i + lag
        annot.append({
            "surge_date": str(common[i])[:10],
            "leader_excess_pct": round(float(ex_l[i]), 2),
            "leader_catalysts": [c["title"][:60] for c in lc],
            "follow_date": str(common[j])[:10] if j < len(common) else None,
            "follower_excess_pct": round(float(ex_f[j]), 2) if j < len(common) else None,
            "follower_catalysts": [c["title"][:60] for c in fc],
        })

    # 真传导候选：跟随币跟涨（超额>0）且自身窗口内无催化剂 → 不能用自身利好解释
    true_trans = [a["surge_date"] for a in annot
                  if a["follower_excess_pct"] is not None and a["follower_excess_pct"] > 0
                  and not a["follower_catalysts"]]

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "pair": f"{leader}→{follower}", "window": window, "n_days": len(common),
        "lag_profile": profile,
        "best": {"lag": best["lag"], "corr": round(float(best["corr"]), 4)},
        "sync_corr": round(float(sync), 4) if sync is not None else None,
        "robustness": {"first_half_corr": round(float(first), 4) if first is not None else None,
                       "second_half_corr": round(float(second), 4) if second is not None else None},
        "event_study": ev,
        "catalyst": {
            "leader_n": len(cat_l), "follower_n": len(cat_f),
            "surge_episodes": n_surge,
            "surge_with_leader_catalyst": n_lead_cat,
            "surge_with_follower_catalyst": n_follow_cat,
            "true_transmission_candidates": true_trans,
            "annotations": annot,
            "leader_catalysts": cat_l, "follower_catalysts": cat_f,
        },
    }
    return report


def print_report(r: dict) -> None:
    w = "\u2500" * 74
    print(f"\n{w}\n\u9886\u5148\u6ede\u540e\u5bf9\u6df1\u6316 \u00b7 {r['pair']}  {r['generated_at'][:10]}\n{w}")
    print(f"\u65e5\u9891\u7a97\u53e3\uff1a{r['window']}  \u5171 {r['n_days']} \u5929\uff08\u5df2\u6263 BTC \u8d85\u989d\uff09")
    b = r["best"]
    print(f"\u3010\u590d\u9a8c\u3011\u6700\u4f18\u6ede\u540e {b['lag']:+d}d  corr={b['corr']:.3f}"
          f"\uff08\u540c\u6b65 {fmt_num(r['sync_corr'])}; \u9996\u534a\u6bb5 {fmt_num(r['robustness']['first_half_corr'])}"
          f" / \u540e\u534a\u6bb5 {fmt_num(r['robustness']['second_half_corr'])}\uff09")
    # 全谱简表
    prof = r["lag_profile"]
    line = "  " + "  ".join(f"{p['lag']:+d}:{p['corr']:.2f}" for p in prof if p['lag'] % 2 == 0 or p['lag'] == b['lag'])
    print(f"  \u6ede\u540e\u5168\u8c31\uff08\u504a\u6570\uff09\uff1a{line}")

    ev = r["event_study"]
    d = ev["day"]
    smark = "（小样本，仅披露不判读）" if not ev.get("sample_ready") else ""
    print(f"\n\u3010\u4e8b\u4ef6\u7814\u7a76\u3011\u9886\u5148\u5e01\u8d85\u989d \u2265 {ev['thresh']}% \u8d77\u98de\u65e5"
          f" \u2192 \u8ddf\u968f\u5e01\u540e\u7eed (+{ev['lag']}d)"
          f"\uff08\u8d85\u989d\u5df2\u6263 {ev['fee_pct']}% \u53cc\u8fb9\u8d39\uff09{smark}")
    print(f"  \u4e8b\u4ef6\u6570 n={d['n']}\uff1a\u8ddf\u6da8\u6982\u7387 {fmt_pct(d['follow_prob'])}"
          f" vs \u57fa\u7ebf {fmt_pct(d['base_prob'])}  \u63d0\u5347\u00d7{fmt_num(d['lift'])}"
          f"  \u5e73\u5747\u8d85\u989d {fmt_num(d['mean_excess'])}%")
    c = ev["cum_win"]
    print(f"  \u7d2f\u8ba1 {ev['win']}d \u7a97\u53e3\uff1a\u8ddf\u6da8\u6982\u7387 {fmt_pct(c['follow_prob'])}"
          f" vs \u57fa\u7ebf {fmt_pct(c['base_prob'])}  \u63d0\u5347\u00d7{fmt_num(c['lift'])}"
          f"  \u5e73\u5747\u8d85\u989d {fmt_num(c['mean_excess'])}%")

    cat = r["catalyst"]
    print(f"\n\u3010\u50ac\u5316\u5242\u5f52\u56e0\u3011\u9886\u5148\u5e01 {cat['leader_n']} \u6761 / \u8ddf\u968f\u5e01"
          f" {cat['follower_n']} \u6761\uff08\u7a97\u53e3\u5185\uff09")
    print(f"  \u8d77\u98de\u65e5 {cat['surge_episodes']} \u4e2a\uff0c\u5176\u4e2d\u9886\u5148\u5e01\u81ea\u8eab\u6709\u50ac\u5316\u5242\uff1a"
          f"{cat['surge_with_leader_catalyst']}\uff1b\u8ddf\u8fdb\u65e5\u6709\u8ddf\u968f\u5e01\u50ac\u5316\u5242\uff1a"
          f"{cat['surge_with_follower_catalyst']}")
    tt = cat["true_transmission_candidates"]
    print(f"  \u771f\u4f20\u5bfc\u5019\u9009\uff08\u8ddf\u6da8\u4e14\u65e0\u81ea\u8eab\u50ac\u5316\u5242\uff09\uff1a"
          f"{len(tt)} \u4e2a  {tt}")
    for a in cat["annotations"]:
        lc = a["leader_catalysts"][0] if a["leader_catalysts"] else ""
        fc = a["follower_catalysts"][0] if a["follower_catalysts"] else ""
        print(f"  {a['surge_date']} \u9886\u5148\u8d85\u989d{a['leader_excess_pct']:+.1f}% "
              f"\u2192 {a['follow_date'] or '-'} \u8ddf\u968f\u8d85\u989d{fmt_num(a['follower_excess_pct'])}%"
              f"  \u3010\u50ac\uff1a{lc[:38] or '-'}\u3011 \u3010\u8ddf\u50ac\uff1a{fc[:38] or '-'}\u3011")
    print(f"\n\u8bfb\u6cd5\uff1a\u82e5\u201c\u9886\u5148\u6709\u50ac\u201d\u9ad8\u4e14\u201c\u8ddf\u8fdb\u6709\u50ac\u201d\u9ad8"
          f"\u2192 \u53ef\u80fd\u662f\u540c\u4e00\u53d9\u4e8b\u53d8\u4e73\u540c\u65f6\u6253\u4e24\u5e01\uff08\u4f2a\u9886\u5148\uff09\uff1b"
          f"\u82e5\u53ea\u6709\u9886\u5148\u6709\u50ac\u3001\u8ddf\u8fdb\u65e0\u50ac\u2192 \u66f4\u50cf\u771f\u6b63\u4f20\u5bfc\u3002")


def main() -> int:
    ap = argparse.ArgumentParser(description="领先-滞后对深挖 · 事件研究 + 催化剂归因")
    ap.add_argument("--leader", default="BCH", help="领先币符号（裸或 USDT）")
    ap.add_argument("--follower", default="ONDO", help="跟随币符号（裸或 USDT）")
    ap.add_argument("--days", type=int, default=120, help="日频回溯天数")
    ap.add_argument("--max-lag", type=int, default=14, help="滞后全谱最大天数")
    ap.add_argument("--event-thresh", type=float, default=5.0, help="起飞日阈值：领先币日超额 %")
    ap.add_argument("--win-days", type=int, default=2, help="跟涨累计窗口（天）")
    ap.add_argument("--follow-win", type=int, default=0,
                    help="跟涨起始滞后（0=用全谱最优滞后）")
    ap.add_argument("--judge-min-events", type=int, default=20,
                    help="判读门槛：事件数 ≥ 该值才算 sample_ready（小样本只披露不判读，A3）")
    ap.add_argument("--fee-pct", type=float, default=0.1,
                    help="双边费 %：事件/基线前瞻超额都扣（对齐结算 0.1% 双边费，A5）")
    ap.add_argument("--winsorize-pct", type=float, default=50.0,
                    help="收益 winsorize 阈值 %%（±限幅防单币离群，A6；<=0 关闭）")
    ap.add_argument("--output", type=str, default="")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    with sl.get_db() as conn:
        rep = run(conn, args)
    print_report(rep)
    out = args.output or str(_HERE / "output" / f"dive_{rep['pair']}_{datetime.now(UTC):%Y-%m-%d}.json")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(rep, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nJSON 报告：{out}")
    return 0


def selftest() -> int:
    passed = failed = 0

    def check(cond, name, detail=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  \u2713 {name}")
        else:
            failed += 1
            print(f"  \u2717 {name}  {detail}")

    print("\n\u3010\u81ea\u68c0\u3011\u7eaf\u51fd\u6570")
    check(resolve_symbol("BCHUSDT") == "BCH" and resolve_symbol("ondo") == "ONDO",
          "符号归一化")

    rng = np.random.default_rng(3)
    T = 400
    lead = rng.normal(size=T)
    follow = np.roll(lead, 3)  # 领先币领先跟随币 3 期
    prof = lead_lag_profile(lead, follow, max_lag=6)
    best = max((p for p in prof if p["corr"] is not None), key=lambda p: abs(p["corr"]))
    check(best["lag"] == 3 and best["corr"] > 0.9,
          "lead-lag 全谱找回：构造 +3 期 → 最优滞后 +3", f"got lag={best['lag']} corr={best['corr']:.3f}")

    # 事件研究：构造领先币在第 50/100 根爆量，跟随币 3 天后同步涨；基线带噪声（有正有负）
    ex_l = np.zeros(T)
    ex_l[50] = 8.0
    ex_l[100] = 7.0
    ex_f = rng.normal(0.0, 0.5, T)
    ex_f[53] = 3.0
    ex_f[103] = 4.0
    ev = event_follow(ex_l, ex_f, lag=3, win=2, thresh=5.0)
    check(ev["n_events"] == 2 and ev["day"]["n"] == 2
          and ev["day"]["follow_prob"] == 1.0 and ev["day"]["mean_excess"] > 0,
          "事件研究：2 个起飞日 → 2 个跟随日全部跟涨（概率 100%）",
          f"got={ev['day']}")
    check(ev["day"]["lift"] and ev["day"]["lift"] > 1.5,
          "跟涨概率高于基线（基线为随机日，约 50%）", f"lift={ev['day']['lift']}")

    ev0 = event_follow(np.zeros(T), ex_f, lag=3, win=2, thresh=5.0)
    check(ev0["n_events"] == 0, "无起飞日 → 事件数为 0")

    print("\n\u3010\u81ea\u68c0\u3011A5 \u6263\u8d39 / A3 \u5224\u8bfb\u95e8\u69db")
    evf = event_follow(ex_l, ex_f, lag=3, win=2, thresh=5.0, fee=0.1, judge_min_events=20)
    check(abs(evf["day"]["mean_excess"] - (ev["day"]["mean_excess"] - 0.1)) < 1e-9,
          "扣费 0.1%（本脚本 excess 为 %）：事件均值减 0.1",
          f"got {evf['day']['mean_excess']} vs {ev['day']['mean_excess']-0.1}")
    check(evf["sample_ready"] is False and evf["n_events"] == 2,
          "事件数 2 < 判读门槛 20 → sample_ready=False（只披露不判读）",
          f"got sample_ready={evf['sample_ready']}")

    print(f"\n\u81ea\u68c0\u7ed3\u679c\uff1a{passed} \u901a\u8fc7 / {failed} \u5931\u8d25")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
