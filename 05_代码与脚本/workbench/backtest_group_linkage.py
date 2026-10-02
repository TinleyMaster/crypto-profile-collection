#!/usr/bin/env python3
"""以关系分组为基础 · 组内联动回测。

在 token_relation_graph.py 的分组基础上回测「相关度/联动」是否真实：

1. 组相关稳健性：组内同步相关在**首/后半段**是否都高于各自基线
   （稳定组 = 两段皆超基线；只一段超 = 昙花一现，防 BCH→ONDO 式伪信号）；
2. 组内起飞事件回测：组内某币「起飞」（日超额 ≥ 阈值）后，同组其它币在
   +1/+2/+3 天是否异常跟随 —— 跟涨概率与平均超额 vs 基线，直接验证
   「一个起飞 → 同组另一个随后起飞」；
3. 同步共动对照：事件当日同组币的平均超额（区分「同时涨」vs「随后跟」）。

口径与前置脚本一致：top N 币日收益，扣 BTC 超额、排除稳定币；组来自
biz.asset_sector / core.asset_contract / sector_narrative_asset。

运行：
    python backtest_group_linkage.py
    python backtest_group_linkage.py --selftest
    python backtest_group_linkage.py --event-thresh 0.05 --min-events 10
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent
_SCRIPTS_SRC = _HERE.parent / "scripts" / "src"
for p in (_SCRIPTS_SRC, _HERE.parent / "scripts" / "bin"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import token_relation_graph as trg  # noqa: E402  复用 prepare / sync_corr_matrix

UTC = timezone.utc
DIM_LABELS = {"sector": "赛道", "chain": "公链", "narrative": "产业链叙事"}


# ═══════════════════════════════════════════════════════════════
#  一、纯函数层
# ═══════════════════════════════════════════════════════════════

def mean_offdiag(corr: np.ndarray) -> float | None:
    """相关矩阵非对角均值（全市场基线）。"""
    n = corr.shape[0]
    if n < 2:
        return None
    iu = np.triu_indices(n, k=1)
    return float(corr[iu].mean())


def group_mean_corr(corr: np.ndarray, group_idx: list[int]) -> float | None:
    """组内两两相关均值。"""
    idx = np.asarray(list(group_idx), dtype=int)
    if len(idx) < 2:
        return None
    sub = corr[np.ix_(idx, idx)]
    iu = np.triu_indices(len(idx), k=1)
    return float(sub[iu].mean())


def _stat(vals) -> tuple[int, float | None, float | None]:
    a = np.asarray(vals)
    a = a[np.isfinite(a)]
    if a.size == 0:
        return 0, None, None
    return int(len(a)), float((a > 0).mean()), float(a.mean())


def group_event_backtest(R: np.ndarray, group_idx: list[int], thresh: float,
                         horizons=(1, 2, 3), min_events: int = 5,
                         fee: float = 0.0, judge_min_events: int = 20,
                         baseline_min: int = 30) -> dict:
    """组内起飞事件回测。

    R: (T, N) 日超额收益矩阵（小数制）。group_idx: 组内成员列下标。
    事件 = 成员 i 在 t 日超额 ≥ thresh；统计同组其它币在 t+k 的平均超额。
    基线 = 成员 i 全部非事件日的同组其它币平均超额（剔除全部成员事件窗口，A4）。
    同步对照 k=0：事件当日同组币是否也同时在涨（区分「同时」vs「随后跟」）。

    A5 扣费：事件侧与基线侧的前瞻超额都扣 fee（小数制，默认 0=不扣；对齐结算口径
    用 0.001 = 0.1% 双边费）。同步对照是当日事实、不是可交易窗口，不扣费。
    A3 判读门槛：n_events ≥ judge_min_events 才算 sample_ready（只披露不判读）。
    A4 基线闸门：A4 全局剔除后事件密集组的基线会被掏空（l1 312 事件/41 天实测
    退化出 base_prob=100% 这类噪声）→ base_n ≥ baseline_min 才给 lift_ok。
    """
    idx = list(group_idx)
    T = R.shape[0]
    max_h = max(horizons)
    ev = {k: [] for k in horizons}
    base = {k: [] for k in horizons}
    n_events = 0
    # 基线去污染（审计 A4）：收集**全部成员**的起飞日，基线起点落在任一事件
    # 跟随窗口 [s, s+max_h) 内都剔除——原实现只剔本成员起飞日，其它成员事件
    # 会把事件收益算进基线、压低「提升度」。
    all_surges: set[int] = set()
    for i in idx:
        col = R[:, i]
        for t in range(T):
            if np.isfinite(col[t]) and col[t] >= thresh:
                all_surges.add(t)
    blocked = set()
    for s in all_surges:
        for k2 in range(max_h):
            if s + k2 < T - max_h:
                blocked.add(s + k2)
    for i in idx:
        peers = [j for j in idx if j != i]
        if not peers:
            continue
        col = R[:, i]
        surge_days = {t for t in range(T)
                      if np.isfinite(col[t]) and col[t] >= thresh}
        n_events += len(surge_days)
        for t in surge_days:
            for k in horizons:
                if t + k >= T:
                    ev[k].append(np.nan)
                    continue
                seg = R[t + k, peers]
                seg = seg[np.isfinite(seg)]
                ev[k].append(float(seg.mean()) - fee if seg.size else np.nan)
        for t in range(T - max_h):
            if t in blocked:
                continue
            for k in horizons:
                seg = R[t + k, peers]
                seg = seg[np.isfinite(seg)]
                base[k].append(float(seg.mean()) - fee if seg.size else np.nan)
    out = {"n_events": n_events, "n_members": len(idx),
           "fee_pct": round(fee * 100, 4),
           "sample_ready": bool(n_events >= judge_min_events),
           "skipped": bool(n_events < min_events),
           "per_h": {k: {"ev_n": 0, "ev_prob": None, "ev_mean": None,
                         "base_n": 0, "base_prob": None, "base_mean": None,
                         "lift": None, "excess_diff": None, "lift_ok": False}
                    for k in horizons},
           "sync_same_day": {"n": 0, "mean": None, "prob": None}}
    if n_events < min_events:
        return out  # 事件数不足，不判（防小样本虚高提升）
    for k in horizons:
        ne, ep, em = _stat(ev[k])
        nb, bp, bm = _stat(base[k])
        out["per_h"][k] = {
            "ev_n": ne, "ev_prob": ep, "ev_mean": em,
            "base_n": nb, "base_prob": bp, "base_mean": bm,
            "lift": (ep / bp) if (ep is not None and bp) else None,
            "excess_diff": (em - bm) if (em is not None and bm is not None) else None,
            "lift_ok": bool(nb >= baseline_min),
        }
    # 同步共动对照：事件当日同组其它币平均超额（k=0 语义）
    sync_vals = []
    for i in idx:
        peers = [j for j in idx if j != i]
        col = R[:, i]
        for t in range(T):
            if np.isfinite(col[t]) and col[t] >= thresh:
                seg = R[t, peers]
                seg = seg[np.isfinite(seg)]
                if seg.size:
                    sync_vals.append(float(seg.mean()))
    out["sync_same_day"] = {"n": len(sync_vals),
                            "mean": float(np.mean(sync_vals)) if sync_vals else None,
                            "prob": float((np.asarray(sync_vals) > 0).mean()) if sync_vals else None}
    return out


def stability(corr_first: np.ndarray, corr_second: np.ndarray,
              group_idx: list[int], base_first: float, base_second: float) -> dict | None:
    """组相关稳健性：首/后半段组内均值相关 vs 各自基线。"""
    m1 = group_mean_corr(corr_first, group_idx)
    m2 = group_mean_corr(corr_second, group_idx)
    if m1 is None or m2 is None:
        return None
    return {"first": round(m1, 4), "second": round(m2, 4),
            "base_first": round(base_first, 4), "base_second": round(base_second, 4),
            "stable": bool(m1 > base_first and m2 > base_second),
            "one_side": bool((m1 > base_first) != (m2 > base_second))}


# ═══════════════════════════════════════════════════════════════
#  二、分析
# ═══════════════════════════════════════════════════════════════

def analyze(conn, args) -> dict:
    p = trg.prepare(conn, args.top_n, args.min_days, args.min_members, args.winsorize_pct)
    cols, R, rel = p["cols"], p["ret"], p["rel"]
    T = R.shape[0]
    half = T // 2
    if half < 10:
        raise RuntimeError(f"样本 {T} 天太短，无法分两段稳健性回测（至少 20 天）")

    corr = trg.sync_corr_matrix(R)
    corr_first = trg.sync_corr_matrix(R[:half])
    corr_second = trg.sync_corr_matrix(R[half:])
    base = mean_offdiag(corr)
    base_first = mean_offdiag(corr_first)
    base_second = mean_offdiag(corr_second)

    dims = {}
    for dim, blk in rel.items():
        rows = []
        for group, idx in blk["groups"].items():
            st = stability(corr_first, corr_second, idx, base_first, base_second)
            ev = group_event_backtest(R, idx, args.event_thresh,
                                      tuple(args.horizons), args.min_events,
                                      fee=args.fee_pct / 100.0,
                                      judge_min_events=args.judge_min_events,
                                      baseline_min=args.baseline_min)
            row = {"group": group, "n": len(idx), "stability": st, "event": ev}
            rows.append(row)
        # 排序只看「样本达标且基线充足」的组；否则排后、仅披露（A3/A4 闸门）
        rows.sort(key=lambda r: (
            not r["event"].get("sample_ready"),
            not (r["event"]["per_h"].get(1, {}).get("lift_ok") or False),
            -(r["event"]["per_h"].get(1, {}).get("lift") or 0)))
        dims[dim] = {"n_groups_analyzed": len(rows),
                     "n_groups_total": blk["n_groups_total"], "groups": rows}

    return {"generated_at": datetime.now(UTC).isoformat(),
            "args": vars(args), "window": p["window"],
            "universe": {"n": len(cols), "n_days": T,
                         "n_excluded_stable": p["n_excluded_stable"]},
            "baseline": {"mean": round(base, 4), "first": round(base_first, 4),
                         "second": round(base_second, 4)},
            "dims": dims}


# ═══════════════════════════════════════════════════════════════
#  三、输出
# ═══════════════════════════════════════════════════════════════

def fmt_pct(v, d=0):
    return "-" if v is None else f"{v*100:.{d}f}%"


def fmt_num(v, d=2):
    return "-" if v is None else f"{v:.{d}f}"


def ev_fee(groups: list[dict]) -> str:
    """取批次内事件扣费百分比（各组一致），未扣费显示 0。"""
    for g in groups:
        fee = (g.get("event") or {}).get("fee_pct")
        if fee is not None:
            return f"{fee:.2f}"
    return "0.00"


def print_report(r: dict) -> None:
    w = "\u2500" * 74
    print(f"\n{w}\n\u4ee5\u5173\u7cfb\u5206\u7ec4\u4e3a\u57fa\u7840 \u00b7 \u7ec4\u5185\u8054\u52a8\u56de\u6d4b  {r['generated_at'][:10]}\n{w}")
    print(f"\u5b87\u5b99\uff1a{r['universe']['n']} \u4e2a\u5e01 \u00d7 {r['universe']['n_days']} \u5929"
          f"\uff08{r['window']} UTC\uff09\u00b7 \u5df2\u6263 BTC / \u6392\u9664\u7a33\u5b9a\u5e01"
          f" {r['universe']['n_excluded_stable']}")
    b = r["baseline"]
    print(f"\u5168\u5e02\u573a\u57fa\u7ebf\uff1a\u5168\u7a97\u53e3 {b['mean']} \uff5c \u9996\u534a\u6bb5 {b['first']}"
          f" \uff5c \u540e\u534a\u6bb5 {b['second']}")

    for dim, blk in r["dims"].items():
        label = DIM_LABELS.get(dim, dim)
        stable = [g for g in blk["groups"] if g["stability"] and g["stability"]["stable"]]
        print(f"\n\u3010{label}\u3011\u7ec4\u6570 {blk['n_groups_analyzed']}/{blk['n_groups_total']}"
              f"\uff0c\u7a33\u5b9a\u7ec4\uff08\u4e24\u6bb5\u5747\u8d85\u57fa\u7ebf\uff09{len(stable)}\u4e2a")
        for g in stable[:6]:
            s = g["stability"]
            print(f"  \u2713 {g['group']:<22} \u9996\u6bb5{s['first']:.3f} / \u540e\u6bb5{s['second']:.3f}"
                  f"  (\u57fa\u7ebf {s['base_first']:.2f}/{s['base_second']:.2f})")
        one = [g for g in blk["groups"] if g["stability"] and g["stability"]["one_side"]]
        if one:
            print(f"  \u26a0 \u4e0d\u7a33\u5b9a\uff08\u53ea\u4e00\u6bb5\u8d85\u57fa\u7ebf\uff09\uff1a"
                  + "\u3001".join(f"{g['group']}({g['stability']['first']:.2f}/{g['stability']['second']:.2f})"
                                  for g in one[:6]))
        print(f"  Top \u8d77\u98de\u56de\u6d4b\uff08\u6309 +1d \u8ddf\u6da8\u6982\u7387\u63d0\u5347\uff1b"
              f"\u8d85\u989d\u5df2\u6263 {ev_fee(blk['groups'])}% \u53cc\u8fb9\u8d39\uff09\uff1a")
        shown = 0
        for g in blk["groups"][:12]:
            ev = g["event"]
            h1 = ev["per_h"].get(1) or {}
            if not h1.get("ev_n"):
                continue
            mark = "✓" if (g["stability"] and g["stability"]["stable"]) else " "
            if not h1.get("lift_ok"):
                mark = "⚠基线薄 "  # A4 闸门：基线被事件掏空，提升度不可判读
            if not ev.get("sample_ready"):
                if shown >= 8:
                    continue  # 小样本组只露几条提示，不占判读榜
                mark = "⚠小样本 "
            shown += 1
            sync = ev["sync_same_day"]
            lift_txt = (f" \u63d0\u5347\u00d7{fmt_num(h1['lift'])}" if h1.get("lift_ok")
                        else " 基线薄不可判读")
            print(f"  {mark} {g['group']:<20} n={g['n']:<3} \u4e8b\u4ef6{ev['n_events']:<4}"
                  f" \u5f53\u65e5\u5171\u52a8{fmt_pct(sync['prob'])}"
                  f"(\u5e73\u5747{fmt_num((sync['mean'] or 0) * 100)}%)"
                  f" \u2192 +1d\u8ddf\u6da8{fmt_pct(h1['ev_prob'])} vs {fmt_pct(h1['base_prob'])}"
                  f"{lift_txt}"
                  f" \u8d85\u989d{fmt_num((h1['ev_mean'] or 0) * 100)}%"
                  f"({fmt_num((h1['base_mean'] or 0) * 100)}%)")
        n_insuff = sum(1 for g in blk["groups"] if g["event"].get("n_events", 0) > 0
                       and not g["event"].get("sample_ready"))
        if n_insuff:
            print(f"  \u26a0 {n_insuff} \u4e2a\u7ec4\u6837\u672c\u4e0d\u8db3\u4ec5\u62ab\u9732\uff08"
                  f"\u4e8b\u4ef6\u6570 < \u5224\u8bfb\u95e8\u69db\uff0c\u4e0d\u7b97\u5224\u8bfb\u7ed3\u679c\uff09\uff1a"
                  + "\u3001".join(f"{g['group']}({g['event']['n_events']})"
                                  for g in blk["groups"][:12]
                                  if g["event"].get("n_events", 0) > 0 and not g["event"].get("sample_ready"))[:120])


def main() -> int:
    ap = argparse.ArgumentParser(description="以关系分组为基础 · 组内联动回测")
    ap.add_argument("--top-n", type=int, default=400, help="分析宇宙 top N（按最新市值）")
    ap.add_argument("--min-days", type=int, default=40, help="宇宙最少日频覆盖天数")
    ap.add_argument("--min-members", type=int, default=3, help="组内最少成员数")
    ap.add_argument("--winsorize-pct", type=float, default=50.0,
                    help="收益 winsorize 阈值 %%（±限幅防单币离群，A6；<=0 关闭）")
    ap.add_argument("--event-thresh", type=float, default=0.05,
                    help="起飞阈值：日超额（小数制，0.05=5%）")
    ap.add_argument("--horizons", type=int, nargs="+", default=(1, 2, 3),
                    help="跟随观测窗口（天）")
    ap.add_argument("--min-events", type=int, default=5, help="组最少事件数才回测")
    ap.add_argument("--judge-min-events", type=int, default=20,
                    help="判读门槛：事件数 ≥ 该值才算 sample_ready（小样本只披露不判读，A3）")
    ap.add_argument("--fee-pct", type=float, default=0.1,
                    help="双边费 %：事件/基线前瞻超额都扣（对齐结算 0.1% 双边费，A5）")
    ap.add_argument("--baseline-min", type=int, default=30,
                    help="基线最小样本：base_n 低于该值 → lift_ok=False 不可判读（A4 闸门）")
    ap.add_argument("--output", type=str, default="")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    with _db() as conn:
        rep = analyze(conn, args)
    print_report(rep)
    out = args.output or str(_HERE / "output" / f"backtest_group_{datetime.now(UTC):%Y-%m-%d}.json")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(rep, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nJSON \u62a5\u544a\uff1a{out}")
    return 0


def _db():
    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    return get_connection(get_settings(require_database=True).database_url)


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

    print("\n\u3010\u81ea\u68c0\u3011\u57fa\u7ebf/\u7ec4\u5185\u76f8\u5173")
    rng = np.random.default_rng(9)
    C = trg.sync_corr_matrix(rng.normal(size=(200, 5)))
    m0 = mean_offdiag(C)
    check(m0 is not None and abs(m0) < 0.5, "全市场基线可算", f"got={m0}")
    g = group_mean_corr(C, [0, 1])
    check(g is not None, "组内均值相关可算", f"got={g}")

    print("\n\u3010\u81ea\u68c0\u3011\u7ec4\u5185\u8d77\u98de\u56de\u6d4b\uff08B \u6ede\u540e A \u4e00\u5929\u8ddf\u6da8\uff09")
    T = 300
    R = rng.normal(0, 0.002, size=(T, 3))
    A, B = 0, 1
    for t in (50, 120, 200):
        R[t, A] = 0.08          # A 起飞
        R[t + 1, B] = 0.03      # B 次日跟涨
    ev = group_event_backtest(R, [0, 1], thresh=0.05, horizons=(1, 2), min_events=2)
    h1 = ev["per_h"][1]
    check(ev["n_events"] == 3 and h1["ev_n"] == 3 and h1["ev_prob"] == 1.0,
          "3 个起飞日 → +1d 全部跟涨（概率 100%）", f"got n={ev['n_events']} h1={h1}")
    check(h1["lift"] and h1["lift"] > 1.5, "+1d 跟涨概率显著高于基线", f"lift={h1['lift']}")
    check(ev["sync_same_day"]["n"] == 3 and ev["sync_same_day"]["mean"] < h1["ev_mean"],
          "同步对照：事件当日同组币无明显共动（均值≈0），+1d 才跟涨（均值≈3%）",
          f"got={ev['sync_same_day']} vs h1_mean={h1['ev_mean']}")

    print("\n\u3010\u81ea\u68c0\u3011A5 \u6263\u8d39 / A3 \u5224\u8bfb\u95e8\u69db")
    evf = group_event_backtest(R, [0, 1], thresh=0.05, horizons=(1, 2), min_events=2,
                               fee=0.001, judge_min_events=20)
    check(abs(evf["per_h"][1]["ev_mean"] - (h1["ev_mean"] - 0.001)) < 1e-9
          and abs(evf["per_h"][1]["base_mean"] - (h1["base_mean"] - 0.001)) < 1e-9,
          "扣费 0.1%：事件/基线前瞻超额都减 0.001",
          f"got {evf['per_h'][1]['ev_mean']} vs {h1['ev_mean']-0.001}")
    check(evf["fee_pct"] == 0.1 and evf["sample_ready"] is False and evf["n_events"] == 3,
          "事件数 3 < 判读门槛 20 → sample_ready=False（只披露不判读）",
          f"got sample_ready={evf['sample_ready']}")
    check(abs(evf["sync_same_day"]["mean"] - ev["sync_same_day"]["mean"]) < 1e-12,
          "同步对照不扣费（当日事实非可交易窗口）")
    check(evf["per_h"][1]["lift_ok"] is True,
          "构造场景基线充足 → lift_ok=True（可判读）",
          f"got base_n={evf['per_h'][1]['base_n']}")
    evg = group_event_backtest(R, [0, 1], thresh=0.05, horizons=(1, 2), min_events=2,
                               fee=0.001, judge_min_events=20, baseline_min=10 ** 9)
    check(evg["per_h"][1]["lift_ok"] is False,
          "A4 闸门：base_n < baseline_min → lift_ok=False（基线薄不可判读）",
          f"got lift_ok={evg['per_h'][1]['lift_ok']} base_n={evg['per_h'][1]['base_n']}")

    print("\n\u3010\u81ea\u68c0\u3011\u7a33\u5065\u6027")
    corr1 = trg.sync_corr_matrix(R[:150])
    corr2 = trg.sync_corr_matrix(R[150:])
    b1, b2 = mean_offdiag(corr1), mean_offdiag(corr2)
    st = stability(corr1, corr2, [0, 1], b1, b2)
    check(st is not None and isinstance(st["stable"], bool), "稳健性判定可算", f"got={st}")

    print(f"\n\u81ea\u68c0\u7ed3\u679c\uff1a{passed} \u901a\u8fc7 / {failed} \u5931\u8d25")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
