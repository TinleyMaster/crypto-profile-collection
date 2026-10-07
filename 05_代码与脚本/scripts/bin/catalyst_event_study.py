#!/usr/bin/env python3
"""催化剂影响事件研究（客观评估框架）。

用事件研究法（Event Study）客观评估「催化剂事件 → 代币价格影响」：

方法
----
1. 独立事件 = (asset_id, base_time) 去重后的观测
   （修复伪重复：同一时刻同一资产的多条催化剂，无论多少条只算 1 个事件）
2. 异常收益 AR = excess − 当日全市场 L1 事件中位数
   （excess 已减 BTC；再减当日中位数剔除 alt 普涨的 β，得到"纯事件影响"）
   主窗口 72h，附 24h / 7d 参考
3. 分层统计：event_type / 评分档 / 市值档 / 方向 / 来源
4. 指标（每层）：
   - n     独立事件数
   - AAR   平均异常收益（%）
   - 中位  中位异常收益（%）
   - t     AAR 横截面 t 值（mean / (sd/√n)），|t|≥1.96 即 p<0.05
   - 命中  方向命中率（hit_72h）+ binomial z 检验
   - IC    composite_score 与 AR_72h 的 Spearman 秩相关
5. 影响评级（由显著性驱动，非拍脑袋阈值）：
   显著正 / 显著负 / 无显著影响 / 样本不足(n<10) / 数据缺失

用法
----
    python catalyst_event_study.py             # 输出控制台 + Markdown 报告
    python catalyst_event_study.py --dry-run   # 仅控制台，不写报告
    python catalyst_event_study.py --include-backtest   # 并入历史回放行（base_time=published_at）
"""
from __future__ import annotations

import argparse
import math
import statistics
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg
import psycopg.rows

from crypto_research.config import get_settings  # noqa: E402

MIN_N = 10               # 分层样本下限（<10 不评级、不报命中率）
IC_MIN_N = 10            # IC 样本下限
SIG_T = 1.96             # 双尾 95% 显著性（t / z 共用）
MCAP_BIG = 1e10          # 大市值（≥100亿$）
MCAP_MID = 1e9           # 中市值（1~100亿$）
GRADE_BANDS = [(80, 101, "80+"), (60, 80, "60-79"), (40, 60, "40-59"), (0, 40, "<40")]
SRC_MIN_N = 20           # 来源分层显示下限


def get_conn():
    settings = get_settings(require_database=True)
    return psycopg.connect(
        settings.database_url,
        row_factory=psycopg.rows.dict_row,
        connect_timeout=30,
        options="-c lock_timeout=30000",
        keepalives=1, keepalives_idle=15, keepalives_interval=5, keepalives_count=3,
    )


# ═══════════════════════════════════════════════════════════════
#  纯函数层（离线可测）
# ═══════════════════════════════════════════════════════════════

def tstat(vals: list[float]) -> float | None:
    """横截面 t 值 = mean / (sd/√n)；n<2 或 sd=0 时 None。"""
    n = len(vals)
    if n < 2:
        return None
    sd = statistics.stdev(vals)
    if sd == 0:
        return None
    return statistics.mean(vals) / (sd / math.sqrt(n))


def rating(n: int, t: float | None, mean_ar: float | None) -> str:
    """影响评级（显著性驱动）。"""
    if mean_ar is None:
        return "数据缺失"
    if n < MIN_N:
        return "样本不足"
    if t is None:
        return "无显著影响"
    if t >= SIG_T:
        return "显著正影响"
    if t <= -SIG_T:
        return "显著负影响"
    return "无显著影响"


def binomial_z(hits: int, n: int) -> float | None:
    """命中率偏离 50% 的 z 值（|z|≥1.96 显著）。"""
    if n < 1:
        return None
    p = hits / n
    denom = math.sqrt(0.25 / n)
    if denom == 0:
        return None
    return (p - 0.5) / denom


def spearman(xs: list, ys: list) -> float | None:
    """Spearman 秩相关（IC）。"""
    pairs = [(float(x), float(y)) for x, y in zip(xs, ys)
             if x is not None and y is not None]
    n = len(pairs)
    if n < IC_MIN_N:
        return None
    pairs.sort(key=lambda p: p[0])
    rank_map: dict[float, list] = defaultdict(list)
    for i, (x, _) in enumerate(pairs):
        rank_map[x].append(i + 1)
    rx = [sum(rank_map[x]) / len(rank_map[x]) for x, _ in pairs]
    oy = sorted(y for _, y in pairs)
    ry = [oy.index(y) + 1 for _, y in pairs]
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx) ** 0.5
    vy = sum((b - my) ** 2 for b in ry) ** 0.5
    if vx == 0 or vy == 0:
        return None
    return round(cov / (vx * vy), 4)


def mcap_bucket(mcap: float | None) -> str:
    if mcap is None:
        return "未知市值"
    if mcap >= MCAP_BIG:
        return "大市值"
    if mcap >= MCAP_MID:
        return "中市值"
    return "小市值"


def grade_bucket(score: float | None) -> str:
    if score is None:
        return "无评分"
    for lo, hi, label in GRADE_BANDS:
        if lo <= score < hi:
            return label
    return "无评分"


# ═══════════════════════════════════════════════════════════════
#  数据层
# ═══════════════════════════════════════════════════════════════

def load_events(conn, include_backtest: bool) -> list[dict]:
    """加载 L1 事件，按 (asset_id, base_time) 去重。

    去重取该时刻 composite_score 最高的一条（同一时刻同一资产的多条催化剂
    归并为一个事件；超额收益相同，评分取信息最强者）。
    """
    bt_filter = "" if include_backtest else "AND co.ret_source <> 'backtest'"
    sql = f"""
        WITH ranked AS (
            SELECT
                co.asset_id, co.base_time,
                co.excess_24h, co.excess_72h, co.excess_7d, co.hit_72h,
                co.impact_direction,
                COALESCE(ac.ai_event_type, ac.rule_event_type, 'other') AS event_type,
                ac.source_code,
                cs.composite_score,
                a.market_cap,
                ROW_NUMBER() OVER (
                    PARTITION BY co.asset_id, co.base_time
                    ORDER BY cs.composite_score DESC NULLS LAST, co.catalyst_id
                ) AS rn
            FROM biz.catalyst_outcome co
            JOIN biz.asset_catalyst ac ON ac.catalyst_id = co.catalyst_id
            LEFT JOIN core.asset a ON a.asset_id = co.asset_id
            LEFT JOIN biz.catalyst_signal cs
                   ON cs.catalyst_id = co.catalyst_id AND cs.asset_id = co.asset_id
            WHERE co.data_tier = 'L1'
              AND co.excess_72h IS NOT NULL
              {bt_filter}
        )
        SELECT asset_id, base_time, excess_24h, excess_72h, excess_7d, hit_72h,
               impact_direction, event_type, source_code, composite_score, market_cap
        FROM ranked WHERE rn = 1
    """
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(sql)
        return cur.fetchall()


def daily_median(events: list[dict], key: str) -> dict[date, float]:
    """按日计算某窗口 excess 的中位数（当日全市场 L1 事件）。"""
    by_day: dict[date, list] = defaultdict(list)
    for e in events:
        v = e.get(key)
        if v is not None:
            by_day[e["base_time"].date()].append(float(v))
    return {d: statistics.median(vs) for d, vs in by_day.items() if vs}


def daily_median_keyed(events: list[dict], key: str, key_fn) -> dict[tuple, float]:
    """按日×键计算某窗口 excess 的中位数（如 日×市值档）。"""
    by: dict[tuple, list] = defaultdict(list)
    for e in events:
        v = e.get(key)
        if v is not None:
            by[(e["base_time"].date(), key_fn(e))].append(float(v))
    return {k: statistics.median(vs) for k, vs in by.items() if vs}


def add_ar(events: list[dict], bench: str = "market") -> None:
    """AR = excess − 当日中位数。

    bench="market"：减当日全市场 L1 事件中位数（剔除 alt 普涨 β）。
    bench="mcap"  ：减当日同市值档中位数（额外剔除市值结构偏压——
                     大市值币 excess 天然偏小，减全市场中位会把大市值系统性压低）。
    """
    key_fn = (lambda e: mcap_bucket(e.get("market_cap"))) if bench == "mcap" else (lambda e: "all")
    med72 = daily_median_keyed(events, "excess_72h", key_fn)
    med24 = daily_median_keyed(events, "excess_24h", key_fn)
    med7 = daily_median_keyed(events, "excess_7d", key_fn)
    for e in events:
        d = e["base_time"].date()
        k72 = (d, key_fn(e)) if bench == "mcap" else (d, "all")
        k24, k7 = k72, k72
        e["ar_72h"] = float(e["excess_72h"]) - med72.get(k72, 0.0) if e["excess_72h"] is not None else None
        e["ar_24h"] = float(e["excess_24h"]) - med24.get(k24, 0.0) if e["excess_24h"] is not None else None
        e["ar_7d"] = float(e["excess_7d"]) - med7.get(k7, 0.0) if e["excess_7d"] is not None else None


# ═══════════════════════════════════════════════════════════════
#  分层统计
# ═══════════════════════════════════════════════════════════════

def analyze_group(events: list[dict], label: str) -> dict:
    """对一组事件计算全部指标。"""
    n = len(events)
    ar72 = [e["ar_72h"] for e in events if e["ar_72h"] is not None]
    ar24 = [e["ar_24h"] for e in events if e["ar_24h"] is not None]
    ar7 = [e["ar_7d"] for e in events if e["ar_7d"] is not None]
    out = {
        "label": label, "n": n,
        "aar_72h": round(statistics.mean(ar72), 2) if ar72 else None,
        "med_72h": round(statistics.median(ar72), 2) if ar72 else None,
        "aar_24h": round(statistics.mean(ar24), 2) if ar24 else None,
        "aar_7d": round(statistics.mean(ar7), 2) if ar7 else None,
        "t_72h": round(tstat(ar72), 2) if tstat(ar72) is not None else None,
        "rating": None,
        "hit_rate": None, "hit_z": None, "n_hit": 0,
        "ic": None,
    }
    out["rating"] = rating(n, out["t_72h"], out["aar_72h"])
    # 命中率（仅方向明确的 bullish/bearish）
    hits = [e for e in events if e.get("hit_72h") is not None]
    if hits:
        n_hit = len(hits)
        hit_true = sum(1 for e in hits if e["hit_72h"])
        out["n_hit"] = n_hit
        out["hit_rate"] = round(hit_true / n_hit, 3)
        out["hit_z"] = round(binomial_z(hit_true, n_hit), 2) if binomial_z(hit_true, n_hit) is not None else None
    # IC（评分 vs AR_72h）
    sc = [e["composite_score"] for e in events]
    out["ic"] = spearman(sc, ar72)
    return out


def group_by(events: list[dict], key_fn) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        out[key_fn(e)].append(e)
    return out


# ═══════════════════════════════════════════════════════════════
#  渲染
# ═══════════════════════════════════════════════════════════════

def fmt_row(g: dict) -> tuple[str, int, str, str, str, str, str, str, str, str]:
    hit = f"{g['hit_rate']:.0%}" if g["hit_rate"] is not None else "-"
    return (
        g["label"],
        g["n"],
        g["rating"],
        hit,
        f"{g['aar_24h']:+.2f}" if g["aar_24h"] is not None else "-",
        f"{g['aar_72h']:+.2f}" if g["aar_72h"] is not None else "-",
        f"{g['med_72h']:+.2f}" if g["med_72h"] is not None else "-",
        f"{g['t_72h']:+.2f}" if g["t_72h"] is not None else "-",
        f"{g['aar_7d']:+.2f}" if g["aar_7d"] is not None else "-",
        f"{g['ic']:.3f}" if g["ic"] is not None else "-",
    )


HEADER = (f"  {'维度':<16} {'n':>5} {'评级':>8} {'命中率':>6} "
          f"{'AAR24h':>8} {'AAR72h':>8} {'中位72h':>8} {'t72h':>7} {'AAR7d':>8} {'IC':>7}")
MD_HEADER = ("| 维度 | n | 评级 | 命中率 | AAR24h | AAR72h | 中位72h | t72h | AAR7d | IC |\n"
             "|---|---|---|---|---|---|---|---|---|---|")


def print_table(groups: list[dict], sort_by: str = "aar_72h") -> None:
    rows = sorted(groups, key=lambda g: (g[sort_by] if g[sort_by] is not None else -999), reverse=True)
    print(HEADER)
    for g in rows:
        v = fmt_row(g)
        print(f"  {v[0]:<16} {v[1]:>5} {v[2]:>8} {v[3]:>6} {v[4]:>8} {v[5]:>8} {v[6]:>8} {v[7]:>7} {v[8]:>8} {v[9]:>7}")


def md_rows(groups: list[dict], sort_by: str = "aar_72h") -> list[str]:
    rows = sorted(groups, key=lambda g: (g[sort_by] if g[sort_by] is not None else -999), reverse=True)
    out = [MD_HEADER]
    for g in rows:
        v = fmt_row(g)
        out.append(f"| {v[0]} | {v[1]} | {v[2]} | {v[3]} | {v[4]} | {v[5]} | {v[6]} | {v[7]} | {v[8]} | {v[9]} |")
    return out


def overall(events: list[dict]) -> dict:
    return analyze_group(events, "全体")


# ═══════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════

def main() -> int:
    parser = argparse.ArgumentParser(description="催化剂影响事件研究")
    parser.add_argument("--dry-run", action="store_true", help="仅控制台不写报告")
    parser.add_argument("--include-backtest", action="store_true",
                        help="并入历史回放行（base_time=published_at，口径与 collect 不同）")
    args = parser.parse_args()

    conn = get_conn()
    try:
        events = load_events(conn, args.include_backtest)
        add_ar(events)
        if not events:
            print("无可用事件")
            return 0

        days = sorted({e["base_time"].date() for e in events})
        print("=" * 76)
        print("催化剂影响事件研究（主窗口 72h）")
        print("=" * 76)
        print(f"  独立事件: {len(events)} 个（(asset, base_time) 去重）")
        print(f"  时间跨度: {days[0]} ~ {days[-1]}（{len(days)} 天）")
        print(f"  口径: L1（K线精确）｜ 主基准 = 当日全市场中位，附同市值档基准对照")
        print(f"  说明: AR = excess − 当日中位；t≥1.96 即 p<0.05")

        ov = overall(events)
        print(f"\n  全体: n={ov['n']}  AAR72h={ov['aar_72h']:+.2f}%  中位={ov['med_72h']:+.2f}%  "
              f"t={ov['t_72h']:+.2f}  评级={ov['rating']}  IC={ov['ic']}")

        # 双基准对照：同市值档基准下重算 AR，量化市值结构偏压
        events_mcap = [dict(e) for e in events]
        add_ar(events_mcap, bench="mcap")

        md = [
            "# 催化剂影响事件研究", "",
            f"> 生成日期: {date.today()} ｜ 独立事件 {len(events)} 个（(asset, base_time) 去重）",
            f"> 时间跨度: {days[0]} ~ {days[-1]}（{len(days)} 天）",
            f"> 方法: 事件研究法。异常收益 AR = (收益 − BTC同期) − 当日中位数",
            f"> 主基准 = 当日全市场中位；对照基准 = 当日同市值档中位",
            f"> 主窗口 72h（附 24h/7d 参考）；t≥1.96 即 p<0.05",
            f"> 评级由显著性驱动: 显著正/负 / 无显著影响 / 样本不足(n<{MIN_N}) / 数据缺失", "",
            "## 全体",
            f"| n | AAR72h | 中位72h | t72h | 评级 | IC |",
            "|---|---|---|---|---|---|",
            f"| {ov['n']} | {ov['aar_72h']:+.2f}% | {ov['med_72h']:+.2f}% | {ov['t_72h']:+.2f} | {ov['rating']} | {ov['ic']} |", "",
        ]

        sections = [
            ("按事件类型", lambda e: e["event_type"]),
            ("按评分档", lambda e: grade_bucket(e["composite_score"])),
            ("按市值档", lambda e: mcap_bucket(e["market_cap"])),
            ("按方向", lambda e: e["impact_direction"] or "未知"),
        ]
        for title, key_fn in sections:
            groups = [analyze_group(v, k) for k, v in group_by(events, key_fn).items()]
            groups = [g for g in groups if g["label"]]
            print("\n" + "=" * 76)
            print(title)
            print("=" * 76)
            print_table(groups)
            md += [f"## {title}", ""] + md_rows(groups) + [""]

        # 基准敏感性对照：市值档 × 两基准
        print("\n" + "=" * 76)
        print("基准敏感性对照（市值档 × 两基准，主窗口 72h）")
        print("=" * 76)
        print(f"  {'市值档':<8} {'n':>5} {'减全市场中位':>14} {'减同市值档中位':>16}")
        mcap_groups = group_by(events, lambda e: mcap_bucket(e["market_cap"]))
        mcap_groups_m = group_by(events_mcap, lambda e: mcap_bucket(e["market_cap"]))
        md += ["## 基准敏感性对照（市值档 × 两基准，72h）", "",
               "| 市值档 | n | 减全市场中位 AAR | 减同市值档中位 AAR |", "|---|---|---|---|"]
        for k in sorted(mcap_groups, key=lambda x: -len(mcap_groups[x])):
            g = analyze_group(mcap_groups[k], k)
            gm = analyze_group(mcap_groups_m.get(k, []), k)
            line = (f"  {k:<8} {g['n']:>5} "
                    f"{g['aar_72h']:+.2f}% (t={g['t_72h']:+.2f}, {g['rating']})"
                    if g["aar_72h"] is not None else f"  {k:<8} {g['n']:>5}  -")
            line_m = (f"{gm['aar_72h']:+.2f}% (t={gm['t_72h']:+.2f}, {gm['rating']})"
                      if gm["aar_72h"] is not None else "-")
            print(line + "  " + line_m)
            md.append(f"| {k} | {g['n']} | {g['aar_72h']:+.2f}% | {gm['aar_72h']:+.2f}% |")
        md.append("")
        print("  注: 若两基准下大市值结论差异大，说明原结论含市值结构偏压，以同市值档基准为准")

        # 影响系数矩阵：事件类型 × 市值档（同市值档基准）
        print("\n" + "=" * 76)
        print("影响系数矩阵（事件类型 × 市值档，72h，同市值档基准）")
        print("=" * 76)
        et_order = sorted({e["event_type"] for e in events_mcap},
                          key=lambda x: -sum(1 for e in events_mcap if e["event_type"] == x))
        mcap_order = ["大市值", "中市值", "小市值", "未知市值"]
        hdr = f"  {'事件类型':<16}" + "".join(f" {b:>22}" for b in mcap_order)
        print(hdr)
        md += ["## 影响系数矩阵（事件类型 × 市值档，72h，同市值档基准）", "",
               "| 事件类型 | 大市值 | 中市值 | 小市值 | 未知市值 |", "|---|---|---|---|---|"]
        for et in et_order:
            if sum(1 for e in events_mcap if e["event_type"] == et) < MIN_N:
                continue
            cells = []
            row = f"  {et:<16}"
            for b in mcap_order:
                sub = [e for e in events_mcap if e["event_type"] == et and mcap_bucket(e["market_cap"]) == b]
                g = analyze_group(sub, b)
                if g["n"] < MIN_N:
                    cell_txt = f"n={g['n']}"
                    cells.append(cell_txt)
                    row += f" {cell_txt:>22}"
                else:
                    cell_txt = f"{g['aar_72h']:+.2f}% (n={g['n']}, t={g['t_72h']:+.2f})"
                    cells.append(f"{g['aar_72h']:+.2f}%(n={g['n']})")
                    row += f" {cell_txt:>22}"
            print(row)
            md.append(f"| {et} | " + " | ".join(cells) + " |")
        md.append("")
        print("  注: 样本 <10 标 n=...；此矩阵即影响系数模型参数（事件类型×市值档的期望影响）")

        # 来源（n≥20 才显示）
        src_groups = [analyze_group(v, k) for k, v in group_by(events, lambda e: e["source_code"]).items()]
        src_groups = [g for g in src_groups if g["label"] and g["n"] >= SRC_MIN_N]
        print("\n" + "=" * 76)
        print(f"按来源（n≥{SRC_MIN_N}）")
        print("=" * 76)
        print_table(src_groups)
        md += [f"## 按来源（n≥{SRC_MIN_N}）", ""] + md_rows(src_groups) + [""]

        # 附：各事件类型 IC 明细（样本充足时）
        ic_groups = [g for g in [analyze_group(v, k) for k, v in group_by(events, lambda e: e["event_type"]).items()]
                     if g["n"] >= IC_MIN_N and g["ic"] is not None]
        ic_groups.sort(key=lambda g: -abs(g["ic"]))
        print("\n" + "=" * 76)
        print(f"评分 IC 明细（按事件类型，n≥{IC_MIN_N}）")
        print("=" * 76)
        print(f"  {'事件类型':<16} {'n':>5} {'IC':>8}   （IC=composite_score vs AR_72h 秩相关）")
        for g in ic_groups:
            print(f"  {g['label']:<16} {g['n']:>5} {g['ic']:>8.3f}")
        md += [f"## 评分 IC 明细（按事件类型，n≥{IC_MIN_N}）", "",
               "| 事件类型 | n | IC |\n|---|---|---|"]
        for g in ic_groups:
            md.append(f"| {g['label']} | {g['n']} | {g['ic']:.3f} |")
        md.append("")

        # 时间外验证：影响模型（事件类型×市值档期望AAR）vs composite_score
        print("\n" + "=" * 76)
        print("模型预测力对比（时间外验证：前一半事件训练 → 后一半测试）")
        print("=" * 76)
        evs = sorted(events_mcap, key=lambda e: e["base_time"])
        cut = evs[len(evs) // 2]["base_time"]
        train = [e for e in evs if e["base_time"] < cut]
        test = [e for e in evs if e["base_time"] >= cut]
        cell: dict[tuple, list] = defaultdict(list)
        for e in train:
            cell[(e["event_type"], mcap_bucket(e["market_cap"]))].append(e["ar_72h"])
        cell_mean = {k: statistics.mean(v) for k, v in cell.items() if len(v) >= MIN_N}
        pred, scs, act = [], [], []
        for e in test:
            p = cell_mean.get((e["event_type"], mcap_bucket(e["market_cap"])))
            if p is not None and e.get("composite_score") is not None and e["ar_72h"] is not None:
                pred.append(p); scs.append(e["composite_score"]); act.append(e["ar_72h"])
        ic_model = spearman(pred, act)
        ic_score = spearman(scs, act)
        print(f"  训练 {train[0]['base_time'].date()}~{train[-1]['base_time'].date()} (n={len(train)}) "
              f"→ 测试 {test[0]['base_time'].date()}~{test[-1]['base_time'].date()} (n={len(test)})")
        print(f"  影响模型（事件类型×市值档期望AAR）IC = {ic_model}  (n={len(pred)})")
        print(f"  composite_score                  IC = {ic_score}  (n={len(scs)})")
        if ic_model is not None and ic_score is not None and ic_score != 0:
            print(f"  影响模型预测力是评分的 {ic_model / ic_score:,.1f} 倍")
        md += ["## 模型预测力对比（时间外验证）", "",
               f"> 训练 {train[0]['base_time'].date()}~{train[-1]['base_time'].date()} → 测试 {test[0]['base_time'].date()}~{test[-1]['base_time'].date()}",
               "| 模型 | IC（AR_72h 秩相关） | n |", "|---|---|---|",
               f"| 影响模型（事件类型×市值档） | {ic_model} | {len(pred)} |",
               f"| composite_score（现有评分） | {ic_score} | {len(scs)} |", "",
               "注: 影响模型 = 训练期各 (事件类型, 市值档) 单元格的平均 AAR 作为期望影响；"
               "时间外 IC 高于评分说明市值×事件类型分层比现有评分更能预测影响。", ""]

        md += [
            "## 口径与局限", "",
            "- 异常收益 = (资产收益 − BTC同期) − 当日中位数：主基准减当日全市场中位（剔除 alt 普涨 β）",
            "- 基准敏感性对照：减当日同市值档中位可剔除市值结构偏压（大市值币 excess 天然偏小），"
            "若两基准结论差异大，以同市值档基准为准",
            "- 独立事件 = (asset_id, base_time) 去重：同一时刻同一资产的多条催化剂只算 1 个事件，避免伪重复放大样本",
            "- 命中率仅统计方向明确（bullish/bearish）的事件；z≥1.96 表示命中率显著偏离 50%",
            "- IC = composite_score 与 AR_72h 的 Spearman 秩相关，|IC|<0.1 通常视为弱预测力",
            "- 评分档 80+ 样本通常较少（<50），结论需谨慎",
            "- 影响系数矩阵（事件类型×市值档）即影响模型参数：单元格 = 期望 AAR（同市值档基准）",
            "- 币安广场 KOL 批次新闻统一挂到 BTC 等资产的归因问题未处理，可能稀释单币影响",
            "- --include-backtest 会并入 base_time=published_at 的历史回放行，与 collect 的 "
            "created_at 基线口径不同，默认不启用以免混口径",
        ]

        if not args.dry_run:
            out = SCRIPT_DIR / "data" / f"catalyst_event_study_{date.today()}.md"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("\n".join(md) + "\n", encoding="utf-8")
            print(f"\n报告已导出: {out}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
