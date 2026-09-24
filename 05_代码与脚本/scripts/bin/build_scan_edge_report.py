#!/usr/bin/env python3
"""盘面告警 · 胜率赔率日报聚合（告警胜率赔率日报 P1）。

设计依据：04_架构与代码方案/告警胜率赔率日报方案_2026-09-23.md §4.5 / §5

做什么
------
读 `biz.scan_signal_outcome`（P0 结算好的逐信号多窗口结局），按 **Asia/Shanghai 日**
聚合出：多窗口胜率 / 赔率 / 盈亏平衡胜率 / 盈亏比 / 均收益 + BTC beta 对照 +
行情环境 + 近 3 日滚动 + 分桶诊断 + 阈值-行情失配判定，落：
  * `biz.scan_edge_daily`   —— 当日一行（供邮件与趋势 diff）
  * `biz.scan_edge_bucket`  —— 当日 × 维度 × 桶（定位「该收紧哪个阈值」）

指标口径（§5）
-------------
* 胜率 = `net > 0` 占比（平盘计负）；
* 赔率 = 平均盈利 ÷ |平均亏损|（一侧为空 → NULL，**不记 ∞**）；
* 盈亏平衡胜率 = `1/(1+赔率)` —— 胜率低于它即期望为负；
* 盈亏比 PF = 总盈利 ÷ |总亏损|；
* 净收益已扣 0.1% 双边费（见 `collect_scan_outcome.COST`）。

⚠️ 样本量与置信：日样本 n=17~79，单日胜率标准误 ±6~12pt ⇒ **失配判定只用近 3 日
滚动值**，当日值仅作展示；`matured_n < 10` 时 `sample_ready=FALSE`，只展示不报警。

用法
----
    python build_scan_edge_report.py                    # 聚合昨日（上海日）
    python build_scan_edge_report.py --date 2026-09-22
    python build_scan_edge_report.py --dry-run --json    # 只算不写库
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
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
WINDOWS = (1, 4, 12, 24)
MIN_SAMPLE = 10            # sample_ready 门槛
MIN_BUCKET_N = 5           # 桶最小样本
EDGE_BUCKET_SHARE = 0.2    # 边缘桶最小占比
ALERT_SURGE_X = 1.5        # 告警量异动倍数（规则 A）
MIN_ALERT_DAYS = 3         # 规则 A/B 分母「有告警日」的最少天数（不足则不做该判定）
# 上限开口桶（">6" / ">8"）：收紧阈值只会裁掉低档，永远裁不到它们 ⇒ 不列入「收紧阈值」建议
OPEN_TOP_BUCKETS = {("vol_ratio", ">6"), ("price_chg", ">8"), ("oi_chg", ">6")}
# 同一批样本被多维度重复报出时的保留优先级（越靠前越具体、越可操作）
DIM_DEDUPE_ORDER = ("scenario", "pool", "timeframe", "vol_ratio", "price_chg", "oi_chg",
                    "confidence", "regime")


def _dim_rank(dim: str) -> int:
    """维度去重优先级（见 DIM_DEDUPE_ORDER）。"""
    return DIM_DEDUPE_ORDER.index(dim) if dim in DIM_DEDUPE_ORDER else len(DIM_DEDUPE_ORDER)

# 分桶定义（与 scan_daemon 阈值常量对齐，便于直接读出「阈值该往哪挪」）
BUCKETS: dict[str, list[tuple[float | None, str]]] = {
    "vol_ratio": [(2.5, "<2.5"), (4, "2.5-4"), (6, "4-6"), (None, ">6")],
    "price_chg": [(3, "<3"), (5, "3-5"), (8, "5-8"), (None, ">8")],
    "oi_chg": [(1, "<1"), (3, "1-3"), (6, "3-6"), (None, ">6")],
}
PASSTHROUGH_DIMS = ("timeframe", "scenario", "confidence", "pool", "regime")


# ──────────────────────────── 纯函数（可离线单测） ────────────────────────────

def agg(vals: list[float]) -> dict:
    """聚合一组净收益（%）→ 胜率/赔率/盈亏平衡线/PF/均收益。空样本返回 n=0。"""
    v = [float(x) for x in vals if x is not None]
    n = len(v)
    if n == 0:
        return {"n": 0, "win": None, "odds": None, "be": None, "pf": None, "avg": None,
                "avg_win": None, "avg_loss": None}
    wins = [x for x in v if x > 0]
    losses = [x for x in v if x < 0]
    avg_win = statistics.fmean(wins) if wins else None
    avg_loss = statistics.fmean(losses) if losses else None
    odds = (avg_win / abs(avg_loss)) if (avg_win is not None and avg_loss is not None) else None
    be = (1 / (1 + odds)) if odds is not None else None
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else None
    return {"n": n, "win": len(wins) / n, "odds": odds, "be": be, "pf": pf,
            "avg": statistics.fmean(v), "avg_win": avg_win, "avg_loss": avg_loss}


def bucket_of(dim: str, value) -> str | None:
    """数值维度分桶（vol_ratio / price_chg / oi_chg）；缺值返回 None。"""
    if value is None:
        return None
    x = abs(float(value)) if dim == "vol_ratio" else float(value)
    for edge, label in BUCKETS[dim]:
        if edge is None or x < edge:
            return label
    return BUCKETS[dim][-1][1]


def classify_regime(btc_amp_pct: float | None, gt05_ratio: float | None,
                    btc_chg_pct: float | None = None) -> str:
    """行情环境：trend（趋势）/ range（横盘低波动）/ mixed。

    判据是「当日是否有方向性」，三个分支互斥且覆盖全部情形：

    * `trend`：振幅 ≥4% 且（小时|涨跌|>0.5% 占比 ≥35% **或** |日涨跌| ≥2%）。
      后一个分支覆盖**单边涨跌日**：分钟级持续单向、小时级回撤少 ⇒ 振幅够大但
      「小时|涨跌|>0.5% 占比」偏低。2026-09-23（振幅 4.10%、占比 16.7%、BTC −2.79%）
      即此类；旧判据两个分支都不命中，落成 mixed，使只在 range 生效的规则 B
      永远拿不到这类行情。
    * `range`：振幅 <3% 且占比 <20%；
    * 其余 → `mixed`（含缺行情数据的兜底）。
    """
    if btc_amp_pct is None or gt05_ratio is None:
        return "mixed"
    if btc_amp_pct >= 4.0 and (gt05_ratio >= 0.35
                               or (btc_chg_pct is not None and abs(btc_chg_pct) >= 2.0)):
        return "trend"
    if btc_amp_pct < 3.0 and gt05_ratio < 0.20:
        return "range"
    return "mixed"


def decide(*, roll3_alerts_avg, prev7_alerts_avg, roll3_win_1h, roll3_be_1h, roll3_pf_1h,
           regime_label, alerts_n, edge_buckets, tf15_bad, tf15_prev_bad, btc_win_24h,
           win_24h, be_24h, n_24h, excess_avg_24h, sample_ready: bool) -> tuple[bool, list[str], str]:
    """失配判定规则 A~E（§5.5）。样本不成熟时**只展示不报警**。"""
    if not sample_ready:
        return False, [], "ok"
    rules: list[str] = []
    # A：告警量异动 + 滚动期望转负
    if (roll3_alerts_avg is not None and prev7_alerts_avg
            and roll3_alerts_avg > prev7_alerts_avg * ALERT_SURGE_X
            and roll3_win_1h is not None and roll3_be_1h is not None
            and roll3_win_1h < roll3_be_1h):
        rules.append("A")
    # B：横盘低波动下告警不减且滚动 PF < 1
    if (regime_label == "range" and prev7_alerts_avg
            and alerts_n >= prev7_alerts_avg
            and roll3_pf_1h is not None and roll3_pf_1h < 1.0):
        rules.append("B")
    # C：边缘桶（样本足、负期望、占比高）
    if edge_buckets:
        rules.append("C")
    # D：15m 通道连续 2 日负期望。
    #    直接看 15m 桶**自身**是否负期望，不依赖「边缘桶」的占比门槛 ——
    #    占比门槛回答的是「值不值得收紧阈值」，与「该通道是否连续失效」无关；
    #    2026-09-23 的 15m 桶（胜率 20.0%、PF 0.34）占比 19.9%，仅差 0.1pp
    #    未入选边缘桶，旧写法会让规则 D 整体漏报。
    if tf15_bad and tf15_prev_bad:
        rules.append("D")
    # E：中长窗口**正期望**，但主要来自 beta（BTC 同向同样赚钱、超额接近 0）。
    #    ⚠ 必须先确认 win_24h > be_24h：否则「信号远差于 BTC」（如 2026-09-23 的
    #    win_24h 20.0% ≪ be_24h 49.3%、超额 −3.24pp）会被误读成「正期望来自 beta」，
    #    结论与实际方向相反。n_24h < MIN_SAMPLE 时样本不足以判断，不触发。
    if (n_24h is not None and n_24h >= MIN_SAMPLE
            and win_24h is not None and be_24h is not None and win_24h > be_24h
            and btc_win_24h is not None and btc_win_24h > 0.5
            and excess_avg_24h is not None and abs(excess_avg_24h) < 0.5):
        rules.append("E")
    severity = "high" if ("A" in rules or "B" in rules) else ("watch" if rules else "ok")
    return bool(rules), rules, severity


def build_conclusion(*, severity, rules, alerts_n, prev7_alerts_avg, prev7_days_n,
                     regime_label, btc_amp_pct, gt05_ratio, roll3_win_1h, roll3_be_1h,
                     roll3_pf_1h, edge_buckets) -> str:
    if severity == "ok" and not rules:
        return (f"未见阈值-行情失配：当日 {alerts_n} 条告警，3 日滚动 T+1h 胜率 "
                f"{_pct(roll3_win_1h)} ≥ 盈亏平衡线 {_pct(roll3_be_1h)}。")
    parts = [f"阈值-行情失配（规则 {'/'.join(rules)}）"]
    if regime_label == "range" and btc_amp_pct is not None:
        parts.append(f"行情横盘低波动（BTC 振幅 {btc_amp_pct:.2f}%、"
                     f"小时|涨跌|>0.5% 占比 {_pct(gt05_ratio)}）")
    if prev7_alerts_avg:
        parts.append(f"告警 {alerts_n} 条 = 最近 {prev7_days_n} 个有告警日均值 "
                     f"{prev7_alerts_avg:.1f} 的 {alerts_n / prev7_alerts_avg:.1f} 倍")
    parts.append(f"3 日滚动 T+1h 胜率 {_pct(roll3_win_1h)} "
                 f"{'<' if (roll3_win_1h or 0) < (roll3_be_1h or 1) else '≥'} "
                 f"盈亏平衡线 {_pct(roll3_be_1h)}、PF {_fmt(roll3_pf_1h, 2)}")
    if edge_buckets:
        top = sorted(edge_buckets, key=lambda b: -(b["share"] or 0))[:3]
        parts.append("边缘桶 " + "、".join(
            f"{b['dim']}={b['bucket']}(胜率 {_pct(b['win_1h'])}/占 {_pct(b['share'])})" for b in top))
    return "；".join(parts) + "。"


def _pct(v) -> str:
    return "-" if v is None else f"{float(v) * 100:.1f}%"


def _fmt(v, nd=2) -> str:
    return "-" if v is None else f"{float(v):.{nd}f}"


# ──────────────────────────── 取数 ────────────────────────────

SAMPLE_SQL = """
SELECT o.signal_id, o.symbol, o.pool, o.scenario, o.timeframe, o.p_dir, o.alerted_at,
       o.aligned_ret_1h, o.aligned_ret_4h, o.aligned_ret_12h, o.aligned_ret_24h,
       o.btc_ret_1h, o.btc_ret_24h, o.excess_24h, o.mae_24h, o.mfe_24h, o.sl_hit_24h,
       o.last_window,
       s.vol_ratio, s.price_chg_pct, s.oi_chg_pct, s.confidence
  FROM biz.scan_signal_outcome o
  JOIN biz.scan_signal s ON s.id = o.signal_id
 WHERE (o.alerted_at AT TIME ZONE 'Asia/Shanghai')::date = %s
 ORDER BY o.alerted_at
"""

BTC_DAY_SQL = """
SELECT open_time, high_px, low_px, close_px
  FROM biz.asset_klines
 WHERE symbol = 'BTCUSDT' AND interval = '1h'
   AND open_time >= %s AND open_time < %s
 ORDER BY open_time
"""


def load_samples(conn, d: date) -> list[dict]:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(SAMPLE_SQL, (d,))
        return cur.fetchall()


def btc_day_metrics(conn, d: date) -> dict:
    """当日 BTC 行情：收盘/涨跌/振幅/小时波动占比（含前一根 1h 棒以算首个收益）。"""
    start = datetime.combine(d, datetime.min.time(), tzinfo=SH) - timedelta(hours=1)
    end = start + timedelta(hours=25)
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(BTC_DAY_SQL, (start, end))
        rows = cur.fetchall()
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


def prev_daily(conn, d: date, days: int) -> list[dict]:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "SELECT report_date, alerts_n, win_1h, be_1h, pf_1h, roll3_win_1h, roll3_be_1h, "
            "       roll3_pf_1h "
            "  FROM biz.scan_edge_daily WHERE report_date < %s "
            " ORDER BY report_date DESC LIMIT %s", (d, days))
        return list(reversed(cur.fetchall()))


def macro_env(conn, d: date) -> dict:
    out = {"fgi": None, "cap_trend_pct": None}
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("SELECT value FROM biz.fear_greed_daily WHERE metric_date <= %s "
                    "ORDER BY metric_date DESC LIMIT 1", (d,))
        r = cur.fetchone()
        if r:
            out["fgi"] = int(r["value"])
        cur.execute("SELECT total_market_cap FROM biz.global_metric_daily "
                    "WHERE metric_date <= %s ORDER BY metric_date DESC LIMIT 2", (d,))
        rows = cur.fetchall()
        if len(rows) >= 2 and rows[0]["total_market_cap"] and rows[1]["total_market_cap"]:
            a, b = float(rows[0]["total_market_cap"]), float(rows[1]["total_market_cap"])
            out["cap_trend_pct"] = (a - b) / b * 100 if b else None
    return out


# ──────────────────────────── 聚合 ────────────────────────────

def compute(conn, d: date) -> tuple[dict, list[dict]]:
    rows = load_samples(conn, d)
    env = btc_day_metrics(conn, d)
    env.update(macro_env(conn, d))
    regime = classify_regime(env.get("btc_amp_pct"), env.get("btc_1h_gt05_ratio"),
                             env.get("btc_chg_pct"))

    # matured_n / pending_n 是**T+1h 窗口**口径（最短窗口，用于 sample_ready 闸门与样本账）。
    # 长窗口（T+12h/T+24h）到期数一定 ≤ 它，展示时必须逐窗口标注，否则读者会把
    # 「成熟 176 / 未到期 0」误读成「四个窗口都有 176 条」。
    n_1h = sum(1 for r in rows if r["aligned_ret_1h"] is not None)
    matured_n = n_1h
    pending_n = len(rows) - n_1h
    sample_ready = matured_n >= MIN_SAMPLE

    day: dict = {"report_date": d, "alerts_n": len(rows), "regime_label": regime,
                 "matured_n": matured_n, "pending_n": pending_n,
                 "sample_ready": sample_ready, "_n_by_window": {}, **env}

    for w in WINDOWS:
        a = agg([r[f"aligned_ret_{w}h"] for r in rows])
        day["_n_by_window"][w] = a["n"]
        day[f"win_{w}h"], day[f"odds_{w}h"] = a["win"], a["odds"]
        day[f"be_{w}h"], day[f"pf_{w}h"] = a["be"], a["pf"]
        day[f"avg_{w}h"] = a["avg"]
    btc24 = agg([r["btc_ret_24h"] for r in rows])
    day["btc_win_24h"], day["btc_avg_24h"] = btc24["win"], btc24["avg"]
    day["excess_avg_24h"] = agg([r["excess_24h"] for r in rows])["avg"]

    # ── 分桶 ──
    buckets: list[dict] = []
    n_base = matured_n or 1
    for dim in ("vol_ratio", "price_chg", "oi_chg"):
        groups: dict[str, list[dict]] = {}
        for r in rows:
            if r["aligned_ret_1h"] is None:
                continue
            b = bucket_of(dim, r[{"vol_ratio": "vol_ratio", "price_chg": "price_chg_pct",
                                 "oi_chg": "oi_chg_pct"}[dim]])
            if b:
                groups.setdefault(b, []).append(r)
        for label in [x[1] for x in BUCKETS[dim]]:
            sub = groups.get(label)
            if sub:
                buckets.append(_mk_bucket(dim, label, sub, n_base))
    for dim in PASSTHROUGH_DIMS:
        groups = {}
        for r in rows:
            if r["aligned_ret_1h"] is None:
                continue
            key = regime if dim == "regime" else r[dim]
            if key is None:
                continue
            groups.setdefault(str(key), []).append(r)
        for label, sub in sorted(groups.items()):
            buckets.append(_mk_bucket(dim, label, sub, n_base))

    # 单值维度（当日只有一个桶，如 confidence 全为 high、regime 全为 range）不携带
    # 区分信息 —— 取消其 edge 标记，否则「100% 占比且负期望」会天天误报。
    dim_counts: dict[str, int] = {}
    for b in buckets:
        dim_counts[b["dim"]] = dim_counts.get(b["dim"], 0) + 1
    for b in buckets:
        if dim_counts[b["dim"]] < 2:
            b["edge"] = False

    # 上限开口桶不是「可收紧的边」：收紧阈值只会裁掉低档，永远裁不到 ">6" / ">8" 这类桶，
    # 把它们列进「收紧这些阈值的优先级最高」是给不出去的建议。
    for b in buckets:
        if (b["dim"], b["bucket"]) in OPEN_TOP_BUCKETS:
            b["edge"] = False

    edge_buckets = [b for b in buckets if b["edge"]]
    edge_buckets.sort(key=lambda b: (-(b["share"] or 0), _dim_rank(b["dim"])))
    # 同义桶去重：同一批样本被两个维度重复报出（如 scenario=S1 ⊂ pool=main，n/胜率/PF/占比
    # 完全相同），把 1 个问题渲染成 2 个。按「维度优先级 + 四项统计量指纹」只保留一个，
    # 并把被丢弃者回写为 edge=False，保证落库与邮件一致。
    _seen_sig: set = set()
    _uniq: list[dict] = []
    for b in edge_buckets:
        sig = (b["n"], b["win_1h"], b["pf_1h"], b["share"])
        if sig in _seen_sig:
            continue
        _seen_sig.add(sig)
        _uniq.append(b)
    _kept = {id(b) for b in _uniq}
    for b in buckets:
        if b["edge"] and id(b) not in _kept:
            b["edge"] = False
    edge_buckets = _uniq
    top_share = next((b for b in edge_buckets
                      if b["dim"] in ("timeframe", "scenario", "vol_ratio")), None)

    # ── 近 3 日滚动（含当日）──
    prev = prev_daily(conn, d, 7)
    tail = prev[-2:]
    recent = tail + [{"report_date": d, "alerts_n": len(rows), "win_1h": day["win_1h"],
                      "be_1h": day["be_1h"], "pf_1h": day["pf_1h"]}]
    day["roll3_alerts_avg"] = statistics.fmean([x["alerts_n"] for x in recent]) if recent else None
    rw = agg([x["win_1h"] for x in recent if x["win_1h"] is not None])
    rb = agg([x["be_1h"] for x in recent if x["be_1h"] is not None])
    rp = agg([x["pf_1h"] for x in recent if x["pf_1h"] is not None])
    day["roll3_win_1h"], day["roll3_be_1h"], day["roll3_pf_1h"] = rw["avg"], rb["avg"], rp["avg"]
    day["n_buckets"] = len(buckets)
    day["edge_buckets"] = edge_buckets
    day["top_share_bucket"] = top_share

    # 规则 A/B 的分母：最近 7 个**有告警**的日子。
    # 0 告警日（daemon 无输出，如 09-19/09-20）不是「没机会」，计入会把均值压低、
    # 把异动倍数放大（2026-09-23 实测：含 0 值日均值 23.7 ⇒ 7.4 倍；剔除后 35.5 ⇒ 5.0 倍）。
    # 有效日不足 MIN_ALERT_DAYS 时返回 None ⇒ 规则 A/B 不做判定（宁可不报，不可虚报）。
    prev7_raw = prev_daily(conn, d, 14)
    prev7 = [x["alerts_n"] for x in prev7_raw if x["alerts_n"]][-7:]
    prev7_avg = statistics.fmean(prev7) if len(prev7) >= MIN_ALERT_DAYS else None

    # 规则 D：15m 通道当日 / 前一日是否**自身负期望**（不依赖「边缘桶」的占比门槛）
    tf15 = next((b for b in buckets
                 if b["dim"] == "timeframe" and b["bucket"] == "15m"), None)
    tf15_bad = bool(tf15 and tf15["n"] >= MIN_BUCKET_N and tf15["win_1h"] is not None
                    and tf15["be_1h"] is not None and tf15["win_1h"] < tf15["be_1h"])
    with conn.cursor() as cur:
        cur.execute("SELECT n, win_1h, be_1h FROM biz.scan_edge_bucket WHERE report_date = %s "
                    "AND dim='timeframe' AND bucket='15m'", (d - timedelta(days=1),))
        r15 = cur.fetchone()
    tf15_prev_bad = bool(r15 and r15[0] >= MIN_BUCKET_N and r15[1] is not None
                         and r15[2] is not None and float(r15[1]) < float(r15[2]))

    flag, rules, severity = decide(
        roll3_alerts_avg=day["roll3_alerts_avg"], prev7_alerts_avg=prev7_avg,
        roll3_win_1h=day["roll3_win_1h"], roll3_be_1h=day["roll3_be_1h"],
        roll3_pf_1h=day["roll3_pf_1h"], regime_label=regime, alerts_n=len(rows),
        edge_buckets=edge_buckets, tf15_bad=tf15_bad, tf15_prev_bad=tf15_prev_bad,
        btc_win_24h=day["btc_win_24h"], win_24h=day["win_24h"], be_24h=day["be_24h"],
        n_24h=day["_n_by_window"].get(24), excess_avg_24h=day["excess_avg_24h"],
        sample_ready=sample_ready)
    day["mismatch_flag"], day["mismatch_rules"], day["severity"] = flag, rules, severity
    day["conclusion"] = build_conclusion(
        severity=severity, rules=rules, alerts_n=len(rows), prev7_alerts_avg=prev7_avg,
        prev7_days_n=len(prev7), regime_label=regime, btc_amp_pct=env.get("btc_amp_pct"),
        gt05_ratio=env.get("btc_1h_gt05_ratio"), roll3_win_1h=day["roll3_win_1h"],
        roll3_be_1h=day["roll3_be_1h"], roll3_pf_1h=day["roll3_pf_1h"],
        edge_buckets=edge_buckets)
    return day, buckets


def _mk_bucket(dim: str, label: str, sub: list[dict], n_base: int) -> dict:
    a1 = agg([r["aligned_ret_1h"] for r in sub])
    a4 = agg([r["aligned_ret_4h"] for r in sub])
    a24 = agg([r["aligned_ret_24h"] for r in sub])
    share = a1["n"] / n_base
    edge = (a1["n"] >= MIN_BUCKET_N and a1["win"] is not None and a1["be"] is not None
            and a1["win"] < a1["be"] and share >= EDGE_BUCKET_SHARE)
    return {"dim": dim, "bucket": label, "n": a1["n"], "win_1h": a1["win"],
            "win_4h": a4["win"], "odds_1h": a1["odds"], "be_1h": a1["be"],
            "pf_1h": a1["pf"], "avg_1h": a1["avg"], "avg_24h": a24["avg"],
            "share": share, "edge": edge}


DAILY_COLS = (
    "report_date", "alerts_n",
    "win_1h", "win_4h", "win_12h", "win_24h",
    "odds_1h", "odds_4h", "odds_12h", "odds_24h",
    "be_1h", "be_4h", "be_12h", "be_24h",
    "pf_1h", "pf_4h", "pf_12h", "pf_24h",
    "avg_1h", "avg_4h", "avg_12h", "avg_24h",
    "btc_win_24h", "btc_avg_24h", "excess_avg_24h",
    "btc_close", "btc_chg_pct", "btc_amp_pct", "btc_1h_gt05_ratio",
    "fgi", "cap_trend_pct", "regime_label",
    "roll3_alerts_avg", "roll3_win_1h", "roll3_be_1h", "roll3_pf_1h",
    "n_buckets", "edge_buckets", "top_share_bucket",
    "matured_n", "pending_n", "sample_ready",
    "mismatch_flag", "mismatch_rules", "severity", "conclusion",
)


def _jsonb(v):
    return None if v is None else json.dumps(v, ensure_ascii=False, default=str)


def save(conn, day: dict, buckets: list[dict]) -> None:
    cols = list(DAILY_COLS)
    vals = []
    for c in cols:
        v = day.get(c)
        if c in ("edge_buckets", "top_share_bucket"):
            v = _jsonb(v)
        vals.append(v)
    updates = ",".join(f"{c}=EXCLUDED.{c}" for c in cols if c != "report_date")
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO biz.scan_edge_daily ({','.join(cols)}, updated_at) "
            f"VALUES ({','.join(['%s'] * len(cols))}, NOW()) "
            f"ON CONFLICT (report_date) DO UPDATE SET {updates}, updated_at=NOW()", vals)
        cur.execute("DELETE FROM biz.scan_edge_bucket WHERE report_date = %s", (day["report_date"],))
        for b in buckets:
            cur.execute(
                "INSERT INTO biz.scan_edge_bucket (report_date, dim, bucket, n, win_1h, win_4h, "
                "odds_1h, be_1h, pf_1h, avg_1h, avg_24h, share, edge) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (day["report_date"], b["dim"], b["bucket"], b["n"], b["win_1h"], b["win_4h"],
                 b["odds_1h"], b["be_1h"], b["pf_1h"], b["avg_1h"], b["avg_24h"],
                 b["share"], b["edge"]))
    conn.commit()


def print_report(day: dict, buckets: list[dict]) -> None:
    d = day["report_date"]
    print(f"\n=== 告警质量日报 {d} · {day['severity'].upper()} · "
          f"{day['alerts_n']} 条（T+1h 成熟 {day['matured_n']} / 未到期 {day['pending_n']}）===")
    print(f"  regime={day['regime_label']}  BTC 涨跌 {_fmt(day['btc_chg_pct'])}%  "
          f"振幅 {_fmt(day['btc_amp_pct'])}%  >0.5%占比 {_pct(day['btc_1h_gt05_ratio'])}  "
          f"FGI {day['fgi']}")
    print(f"  {'窗口':<7}{'n':>4}{'胜率':>9}{'赔率':>8}{'平衡线':>9}{'PF':>8}{'均收益':>10}")
    for w in WINDOWS:
        print(f"  T+{w}h{'':<3}{day['_n_by_window'].get(w, 0):>4}"
              f"{_pct(day[f'win_{w}h']):>9}{_fmt(day[f'odds_{w}h']):>8}"
              f"{_pct(day[f'be_{w}h']):>9}{_fmt(day[f'pf_{w}h']):>8}"
              f"{_fmt(day[f'avg_{w}h']):>9}%")
    print(f"  近 3 日滚动：告警均 {_fmt(day['roll3_alerts_avg'], 1)} 条 · "
          f"T+1h 胜率 {_pct(day['roll3_win_1h'])} / 平衡线 {_pct(day['roll3_be_1h'])} / "
          f"PF {_fmt(day['roll3_pf_1h'])}")
    print(f"  beta 对照 T+24h(n={day['_n_by_window'].get(24, 0)})："
          f"信号 {_pct(day['win_24h'])} / BTC 同向 {_pct(day['btc_win_24h'])} "
          f"· 超额 {_fmt(day['excess_avg_24h'])}%")
    if buckets:
        print(f"  ── 分桶（{len(buckets)} 个，边缘 {sum(1 for b in buckets if b['edge'])} 个）──")
        for b in sorted(buckets, key=lambda x: (x["dim"], -(x["share"] or 0))):
            mark = " ⚠边缘" if b["edge"] else ""
            print(f"    {b['dim']:<11}{b['bucket']:<9}n={b['n']:<4}胜率 {_pct(b['win_1h']):>7}  "
                  f"平衡线 {_pct(b['be_1h']):>7}  PF {_fmt(b['pf_1h']):>6}  "
                  f"占比 {_pct(b['share']):>7}{mark}")
    print(f"  结论：{day['conclusion']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="告警胜率赔率日报聚合")
    parser.add_argument("--date", type=str, default="", help="报告日 YYYY-MM-DD（默认昨日上海日）")
    parser.add_argument("--dry-run", action="store_true", help="只算不写库")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    d = (date.fromisoformat(args.date) if args.date
         else (datetime.now(SH).date() - timedelta(days=1)))
    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        day, buckets = compute(conn, d)
        if not args.dry_run:
            save(conn, day, buckets)

    if args.json:
        print(json.dumps({"daily": day, "buckets": buckets}, ensure_ascii=False,
                         indent=2, default=str))
    else:
        print_report(day, buckets)
    return 0


if __name__ == "__main__":
    sys.exit(main())
