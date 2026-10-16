#!/usr/bin/env python3
"""单币价格弹性（Price Elasticity）——投研 SOP P1.6。

回答一个催化剂研究最大的盲区：**这个币的好消息，到底值不值钱？**
（有的币利好很多但价格不涨，典型如 LINK；有的币则消息一出就大幅超额。）

四条口径（method_version = v1）
-------------------------------
1. **事件弹性（主口径）**：该币 L1 催化剂事件的 AR_72h 均值
   AR = excess_72h − 当日全市场 L1 事件 excess_72h 中位
   （excess_72h 已扣 BTC；再减当日中位剔除 alt 普涨 β，得"纯事件影响"）
   独立事件 = (asset_id, base_time) 去重：同一时刻多条催化剂只算 1 个事件。
2. **弹性质量**：平均 24h 最大涨 / 平均 24h 最大跌（上下行不对称 asym_ratio）。
3. **基础相对强度 RS**：无事件窗口 7d/30d/90d 相对 BTC 超额收益（日线，币安 K 线优先，
   降级 CMC 日线），回答"平时是强是弱"。
4. **对 BTC 敏感度**：最近 90 交易日的日收益对 BTC 回归 beta。

综合输出**弹性评级**（高/中高/低/负/样本不足/无数据）+ **弹性分 0-100**，
并给出**分事件类型弹性**（哪类消息对它有效、哪类无效）。

用法
----
    python bin/asset_price_elasticity.py --symbol LINK            # 控制台
    python bin/asset_price_elasticity.py --symbol LINK --md       # 追加 markdown 片段
    python bin/asset_price_elasticity.py --asset-id 1372 --persist  # 落库快照
    python bin/asset_price_elasticity.py --symbol LINK --json     # JSON 数据包

口径局限（必须如实标注，不得外推）
--------------------------------
- 事件源为币安广场 KOL 新闻流 + 交易所公告，非项目基本面催化；存在同名/批次归因噪音。
- 样本期随库内数据滚动（当前约 2.5 个月），弹性能否外推需谨慎。
- BTC 自身事件弹性恒 ≈ 0（它是基准），hit 恒 0，不代表"BTC 无弹性"。
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

METHOD_VERSION = "v1"
MIN_N = 10                 # 事件弹性评级样本下限（沿用 catalyst_event_study 约定）
GRADE_HIGH = 3.0           # AR72 均值 ≥ +3% → 高弹性
GRADE_MID = 1.0            # ≥ +1% → 中高
GRADE_NEG = -1.0           # < −1% → 负弹性
BETA_WINDOW = 90           # beta 回归交易日数
RS_WINDOWS = (7, 30, 90)

# ── 该币独立事件（L1，(asset_id, base_time) 去重）──────────────
EVENTS_SQL = """
WITH ranked AS (
    SELECT co.asset_id, co.base_time,
           co.excess_24h, co.excess_72h, co.excess_7d,
           co.hit_72h, co.max_gain_24h, co.max_drawdown_24h,
           COALESCE(ac.ai_event_type, ac.rule_event_type, 'other') AS et,
           ROW_NUMBER() OVER (PARTITION BY co.asset_id, co.base_time
                              ORDER BY ac.catalyst_id) AS rn
    FROM biz.catalyst_outcome co
    JOIN biz.asset_catalyst ac ON ac.catalyst_id = co.catalyst_id
    WHERE co.data_tier = 'L1' AND co.excess_72h IS NOT NULL
)
SELECT asset_id, base_time, excess_24h, excess_72h, excess_7d,
       hit_72h, max_gain_24h, max_drawdown_24h, et
FROM ranked WHERE rn = 1
"""

DAILY_KLINES_SQL = """
SELECT symbol,
       (open_time AT TIME ZONE 'UTC')::date AS d,
       (array_agg(close_px ORDER BY open_time DESC))[1] AS close
FROM biz.asset_klines
WHERE interval = '1d' AND symbol = ANY(%s) AND open_time >= %s
GROUP BY symbol, (open_time AT TIME ZONE 'UTC')::date
ORDER BY symbol, d
"""

# 批量模式：一次拉全市场日线（interval='1d'，~40 万行，远轻于 1h 的 970 万行）
BATCH_KLINES_SQL = """
SELECT symbol,
       (open_time AT TIME ZONE 'UTC')::date AS d,
       (array_agg(close_px ORDER BY open_time DESC))[1] AS close
FROM biz.asset_klines
WHERE interval = '1d' AND open_time >= %s
GROUP BY symbol, (open_time AT TIME ZONE 'UTC')::date
ORDER BY symbol, d
"""

DAILY_CMC_SQL = """
SELECT asset_id, market_date, price_usd
FROM biz.asset_market_daily
WHERE source_code = 'cmc' AND asset_id = ANY(%s)
  AND (is_anomaly IS NOT TRUE OR is_anomaly IS NULL) AND price_usd > 0
ORDER BY asset_id, market_date
"""


# ═══════════════════════════════════════════════════════════════
#  纯函数层
# ═══════════════════════════════════════════════════════════════

def clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def binomial_z(hits: int, n: int):
    if n < 1:
        return None
    denom = math.sqrt(0.25 / n)
    if denom == 0:
        return None
    return (hits / n - 0.5) / denom


def grade_of(ar72, n: int) -> str:
    if ar72 is None:
        return "无数据"
    if n < MIN_N:
        return "样本不足"
    if ar72 >= GRADE_HIGH:
        return "高弹性"
    if ar72 >= GRADE_MID:
        return "中高弹性"
    if ar72 > GRADE_NEG:
        return "低弹性"
    return "负弹性"


def elasticity_score(ar72, hit_rate, asym, n: int):
    """综合弹性分 0-100：核心=AR72(0.6)、命中率(0.25)、上下行不对称(0.15)；n<30 向 50 收缩。"""
    parts = []
    if ar72 is not None:
        parts.append((clamp(50 + 5 * ar72), 0.60))
    if hit_rate is not None:
        parts.append((clamp(100 * hit_rate), 0.25))
    if asym is not None:
        parts.append((clamp(50 + 50 * (asym - 1)), 0.15))
    if not parts:
        return None
    wsum = sum(w for _, w in parts)
    score = sum(v * w for v, w in parts) / wsum
    if n < 30:
        score = 50 + (score - 50) * n / 30
    return round(clamp(score), 2)


# ═══════════════════════════════════════════════════════════════
#  计算层
# ═══════════════════════════════════════════════════════════════

def event_elasticity(target_rows, day_med24, day_med72, day_med7) -> dict:
    """目标币独立事件 → AR 统计 + 分事件类型弹性 + 上下行不对称。"""
    evs = []
    for r in target_rows:
        d = r[1].date()
        evs.append({
            "ar24": (float(r[2]) - day_med24.get(d, 0.0)) if r[2] is not None else None,
            "ar72": float(r[3]) - day_med72.get(d, 0.0),
            "ar7d": (float(r[4]) - day_med7.get(d, 0.0)) if r[4] is not None else None,
            "hit": r[5],
            "gain": float(r[6]) if r[6] is not None else None,
            "dd": float(r[7]) if r[7] is not None else None,
            "et": r[8] or "other",
            "date": d,
        })
    n = len(evs)
    out = {"n_events": n, "event_from": None, "event_to": None}
    if n == 0:
        return out
    dates = sorted(e["date"] for e in evs)
    out["event_from"], out["event_to"] = dates[0], dates[-1]

    def mean(key):
        v = [e[key] for e in evs if e[key] is not None]
        return round(sum(v) / len(v), 4) if v else None

    out["ar_24h_mean"] = mean("ar24")
    out["ar_72h_mean"] = mean("ar72")
    out["ar_72h_median"] = round(median([e["ar72"] for e in evs]), 4)
    out["ar_7d_mean"] = mean("ar7d")

    hits = [e["hit"] for e in evs if e["hit"] is not None]
    out["n_hit"] = len(hits)
    if hits:
        nh = sum(1 for h in hits if h)
        out["hit_rate"] = round(nh / len(hits), 4)
        z = binomial_z(nh, len(hits))
        out["hit_z"] = round(z, 3) if z is not None else None
    else:
        out["hit_rate"] = None
        out["hit_z"] = None

    gains = [e["gain"] for e in evs if e["gain"] is not None]
    dds = [e["dd"] for e in evs if e["dd"] is not None]
    out["upside_mean_24h"] = round(sum(gains) / len(gains), 4) if gains else None
    out["downside_mean_24h"] = round(sum(dds) / len(dds), 4) if dds else None
    if out["upside_mean_24h"] is not None and out["downside_mean_24h"] not in (None, 0):
        out["asym_ratio"] = round(out["upside_mean_24h"] / abs(out["downside_mean_24h"]), 3)
    else:
        out["asym_ratio"] = None

    # 分事件类型（n≥3 才列，避免碎片）
    by = defaultdict(list)
    for e in evs:
        by[e["et"]].append(e["ar72"])
    out["by_event_type"] = {
        k: {"n": len(v), "ar72_mean": round(sum(v) / len(v), 2)}
        for k, v in sorted(by.items(), key=lambda x: -len(x[1])) if len(v) >= 3
    }
    return out


def daily_series_from_klines(rows):
    """[(symbol, d, close)] → {symbol: [(d, close)]}"""
    out = defaultdict(list)
    for sym, d, close in rows:
        if close is not None:
            out[sym].append((d, float(close)))
    for v in out.values():
        v.sort()
    return out


def daily_series_from_cmc(rows):
    """[(asset_id, d, price)] → {asset_id: [(d, price)]}"""
    out = defaultdict(list)
    for aid, d, px in rows:
        if px is not None:
            out[aid].append((d, float(px)))
    for v in out.values():
        v.sort()
    return out


def _price_at(series, target):
    """series 中 market_date <= target 的最后一个收盘价。"""
    lo, hi = 0, len(series)
    while lo < hi:
        mid = (lo + hi) // 2
        if series[mid][0] <= target:
            lo = mid + 1
        else:
            hi = mid
    return series[lo - 1][1] if lo > 0 else None


def rs_and_beta(asset_series, btc_series, as_of):
    """相对 BTC 的 7d/30d/90d 超额收益 + 对 BTC beta（最近 BETA_WINDOW 交易日）。"""
    out = {"rs_7d": None, "rs_30d": None, "rs_90d": None, "beta_btc": None}
    if not asset_series or not btc_series:
        return out
    p_now = _price_at(asset_series, as_of)
    b_now = _price_at(btc_series, as_of)
    if p_now is None or b_now is None:
        return out
    for w in RS_WINDOWS:
        tgt = as_of - timedelta(days=w)
        pa = _price_at(asset_series, tgt)
        pb = _price_at(btc_series, tgt)
        if pa and pb:
            out[f"rs_{w}d"] = round((p_now / pa - 1) * 100 - (b_now / pb - 1) * 100, 4)
    # beta：共同交易日日收益回归
    a_map = dict(asset_series)
    b_map = dict(btc_series)
    common = sorted(set(a_map) & set(b_map))
    common = [d for d in common if d <= as_of][-(BETA_WINDOW + 1):]
    if len(common) >= 20:
        ra, rb = [], []
        for i in range(1, len(common)):
            d0, d1 = common[i - 1], common[i]
            if a_map[d0] and b_map[d0]:
                ra.append(a_map[d1] / a_map[d0] - 1)
                rb.append(b_map[d1] / b_map[d0] - 1)
        if len(ra) >= 20:
            mb = sum(rb) / len(rb)
            ma = sum(ra) / len(ra)
            cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
            var = sum((y - mb) ** 2 for y in rb)
            if var > 0:
                out["beta_btc"] = round(cov / var, 4)
    return out


# ═══════════════════════════════════════════════════════════════
#  数据访问层
# ═══════════════════════════════════════════════════════════════

def resolve_asset(conn, symbol, asset_id):
    cur = conn.cursor()
    if asset_id:
        cur.execute("SELECT asset_id, canonical_symbol, canonical_name, market_cap, market_cap_rank "
                    "FROM core.asset WHERE asset_id=%s", (asset_id,))
    else:
        cur.execute("SELECT asset_id, canonical_symbol, canonical_name, market_cap, market_cap_rank "
                    "FROM core.asset WHERE upper(canonical_symbol)=upper(%s) "
                    "ORDER BY market_cap DESC NULLS LAST LIMIT 1", (symbol,))
    r = cur.fetchone()
    if not r:
        return None
    return {"asset_id": r[0], "symbol": r[1], "name": r[2], "market_cap": r[3], "rank": r[4]}


def compute(conn, asset, as_of: date) -> dict:
    aid, sym = asset["asset_id"], asset["symbol"]
    cur = conn.cursor()

    # 1) 事件弹性
    cur.execute(EVENTS_SQL)
    rows = cur.fetchall()
    by_day24, by_day72, by_day7 = defaultdict(list), defaultdict(list), defaultdict(list)
    target = []
    for r in rows:
        d = r[1].date()
        if r[2] is not None:
            by_day24[d].append(float(r[2]))
        by_day72[d].append(float(r[3]))
        if r[4] is not None:
            by_day7[d].append(float(r[4]))
        if r[0] == aid:
            target.append(r)
    dm24 = {d: statistics.median(v) for d, v in by_day24.items()}
    dm72 = {d: statistics.median(v) for d, v in by_day72.items()}
    dm7 = {d: statistics.median(v) for d, v in by_day7.items()}
    ev = event_elasticity(target, dm24, dm72, dm7)

    # 2) 基础相对强度 RS + beta（币安日线优先，降级 CMC 日线）
    since = datetime.combine(as_of - timedelta(days=400), datetime.min.time())
    rs = {"rs_7d": None, "rs_30d": None, "rs_90d": None, "beta_btc": None}
    rs_source = "none"
    if sym:
        pair = sym.upper() + "USDT"
        cur.execute(DAILY_KLINES_SQL, ([pair, "BTCUSDT"], since))
        ser = daily_series_from_klines(cur.fetchall())
        if ser.get(pair) and ser.get("BTCUSDT"):
            rs = rs_and_beta(ser[pair], ser["BTCUSDT"], as_of)
            rs_source = "klines"
    if rs_source == "none":
        cur.execute("SELECT asset_id FROM core.asset WHERE upper(canonical_symbol)='BTC' LIMIT 1")
        btc_row = cur.fetchone()
        btc_id = btc_row[0] if btc_row else None
        ids = [aid] + ([btc_id] if btc_id else [])
        cur.execute(DAILY_CMC_SQL, (ids,))
        ser = daily_series_from_cmc(cur.fetchall())
        if ser.get(aid) and btc_id and ser.get(btc_id):
            rs = rs_and_beta(ser[aid], ser[btc_id], as_of)
            rs_source = "market_daily"

    # 3) 评级 + 分数
    ar72 = ev.get("ar_72h_mean")
    n = ev.get("n_events", 0)
    ev.update(rs)
    ev["rs_source"] = rs_source
    ev["elasticity_grade"] = grade_of(ar72, n)
    ev["elasticity_score"] = elasticity_score(ar72, ev.get("hit_rate"), ev.get("asym_ratio"), n)
    return ev


def persist(conn, asset, as_of: date, ev: dict) -> str:
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO biz.asset_price_elasticity (
            asset_id, as_of, method_version,
            event_from, event_to, n_events, n_l1, n_hit,
            ar_24h_mean, ar_72h_mean, ar_72h_median, ar_7d_mean, hit_rate, hit_z,
            upside_mean_24h, downside_mean_24h, asym_ratio,
            rs_7d, rs_30d, rs_90d, beta_btc, rs_source,
            elasticity_grade, elasticity_score, by_event_type, updated_at
        ) VALUES (
            %s,%s,%s, %s,%s,%s,%s,%s,
            %s,%s,%s,%s,%s,%s,
            %s,%s,%s,
            %s,%s,%s,%s,%s,
            %s,%s,%s, NOW()
        )
        ON CONFLICT (asset_id, as_of) DO UPDATE SET
            method_version = EXCLUDED.method_version,
            event_from = EXCLUDED.event_from, event_to = EXCLUDED.event_to,
            n_events = EXCLUDED.n_events, n_l1 = EXCLUDED.n_l1, n_hit = EXCLUDED.n_hit,
            ar_24h_mean = EXCLUDED.ar_24h_mean, ar_72h_mean = EXCLUDED.ar_72h_mean,
            ar_72h_median = EXCLUDED.ar_72h_median, ar_7d_mean = EXCLUDED.ar_7d_mean,
            hit_rate = EXCLUDED.hit_rate, hit_z = EXCLUDED.hit_z,
            upside_mean_24h = EXCLUDED.upside_mean_24h,
            downside_mean_24h = EXCLUDED.downside_mean_24h, asym_ratio = EXCLUDED.asym_ratio,
            rs_7d = EXCLUDED.rs_7d, rs_30d = EXCLUDED.rs_30d, rs_90d = EXCLUDED.rs_90d,
            beta_btc = EXCLUDED.beta_btc, rs_source = EXCLUDED.rs_source,
            elasticity_grade = EXCLUDED.elasticity_grade,
            elasticity_score = EXCLUDED.elasticity_score,
            by_event_type = EXCLUDED.by_event_type, updated_at = NOW()
    """, (
        asset["asset_id"], as_of, METHOD_VERSION,
        ev.get("event_from"), ev.get("event_to"), ev.get("n_events"),
        ev.get("n_events"), ev.get("n_hit"),
        ev.get("ar_24h_mean"), ev.get("ar_72h_mean"), ev.get("ar_72h_median"),
        ev.get("ar_7d_mean"), ev.get("hit_rate"), ev.get("hit_z"),
        ev.get("upside_mean_24h"), ev.get("downside_mean_24h"), ev.get("asym_ratio"),
        ev.get("rs_7d"), ev.get("rs_30d"), ev.get("rs_90d"), ev.get("beta_btc"),
        ev.get("rs_source"),
        ev.get("elasticity_grade"), ev.get("elasticity_score"),
        json.dumps(ev.get("by_event_type") or {}, ensure_ascii=False),
    ))
    return f"asset_id={asset['asset_id']} as_of={as_of}"


# ═══════════════════════════════════════════════════════════════
#  批量模式（全市场落库）
# ═══════════════════════════════════════════════════════════════

def load_asset_map(conn, ids):
    """asset_id → {asset_id, symbol, name, market_cap, rank}"""
    cur = conn.cursor()
    cur.execute("SELECT asset_id, canonical_symbol, canonical_name, market_cap, market_cap_rank "
                "FROM core.asset WHERE asset_id = ANY(%s)", (list(ids),))
    return {r[0]: {"asset_id": r[0], "symbol": r[1], "name": r[2],
                   "market_cap": r[3], "rank": r[4]} for r in cur.fetchall()}


def compute_preloaded(aid, sym, as_of, target_rows, dm24, dm72, dm7,
                      klines_by_sym, btc_sym, cmc_by_id, btc_cmc_id, btc_cmc):
    """用预载数据算单资产弹性（不触库）。"""
    ev = event_elasticity(target_rows, dm24, dm72, dm7)
    if ev.get("n_events", 0) == 0:
        return None
    rs = {"rs_7d": None, "rs_30d": None, "rs_90d": None, "beta_btc": None}
    rs_source = "none"
    pair = (sym or "").upper() + "USDT"
    a_ser = klines_by_sym.get(pair)
    if a_ser and btc_sym:
        rs = rs_and_beta(a_ser, btc_sym, as_of)
        rs_source = "klines"
    if rs_source == "none":
        a_cmc = cmc_by_id.get(aid)
        if a_cmc and btc_cmc:
            rs = rs_and_beta(a_cmc, btc_cmc, as_of)
            rs_source = "market_daily"
    ev.update(rs)
    ev["rs_source"] = rs_source
    ar72 = ev.get("ar_72h_mean")
    n = ev.get("n_events", 0)
    ev["elasticity_grade"] = grade_of(ar72, n)
    ev["elasticity_score"] = elasticity_score(ar72, ev.get("hit_rate"), ev.get("asym_ratio"), n)
    return ev


def batch_run(conn, as_of, commit_every=200) -> dict:
    """对全部有 catalyst_outcome 事件的资产批量计算并落库。"""
    cur = conn.cursor()
    # 1) 事件（全局一次）
    cur.execute(EVENTS_SQL)
    evrows = cur.fetchall()
    by_day24, by_day72, by_day7 = defaultdict(list), defaultdict(list), defaultdict(list)
    by_asset = defaultdict(list)
    for r in evrows:
        d = r[1].date()
        if r[2] is not None:
            by_day24[d].append(float(r[2]))
        by_day72[d].append(float(r[3]))
        if r[4] is not None:
            by_day7[d].append(float(r[4]))
        by_asset[r[0]].append(r)
    dm24 = {d: statistics.median(v) for d, v in by_day24.items()}
    dm72 = {d: statistics.median(v) for d, v in by_day72.items()}
    dm7 = {d: statistics.median(v) for d, v in by_day7.items()}

    # 2) 资产元信息
    amap = load_asset_map(conn, by_asset.keys())

    # 3) 全市场日线（一次拉全量）
    since = datetime.combine(as_of - timedelta(days=400), datetime.min.time())
    cur.execute(BATCH_KLINES_SQL, (since,))
    klines_by_sym = daily_series_from_klines(cur.fetchall())
    btc_sym = klines_by_sym.get("BTCUSDT")

    # 4) CMC 降级（仅对无币安 K 线的资产）
    need_cmc = [aid for aid in by_asset
                if (amap.get(aid, {}).get("symbol") or "").upper() + "USDT" not in klines_by_sym]
    cmc_by_id, btc_cmc_id, btc_cmc = {}, None, None
    if need_cmc:
        cur.execute("SELECT asset_id FROM core.asset WHERE upper(canonical_symbol)='BTC' LIMIT 1")
        br = cur.fetchone()
        btc_cmc_id = br[0] if br else None
        ids = list(set(need_cmc + ([btc_cmc_id] if btc_cmc_id else [])))
        cur.execute(DAILY_CMC_SQL, (ids,))
        cmc_by_id = daily_series_from_cmc(cur.fetchall())
        btc_cmc = cmc_by_id.get(btc_cmc_id)

    # 5) 逐资产计算 + 落库
    n_done = n_kline = n_cmc = n_skip = 0
    grades = defaultdict(int)
    for i, (aid, rows) in enumerate(sorted(by_asset.items())):
        asset = amap.get(aid)
        if not asset:
            n_skip += 1
            continue
        ev = compute_preloaded(aid, asset["symbol"], as_of, rows, dm24, dm72, dm7,
                               klines_by_sym, btc_sym, cmc_by_id, btc_cmc_id, btc_cmc)
        if ev is None:
            n_skip += 1
            continue
        persist(conn, asset, as_of, ev)
        n_done += 1
        grades[ev["elasticity_grade"]] += 1
        if ev["rs_source"] == "klines":
            n_kline += 1
        elif ev["rs_source"] == "market_daily":
            n_cmc += 1
        if n_done % commit_every == 0:
            conn.commit()
    conn.commit()
    return {"n_done": n_done, "n_skip": n_skip, "n_kline": n_kline, "n_cmc": n_cmc,
            "total_assets_with_events": len(by_asset), "grades": dict(grades)}


# ═══════════════════════════════════════════════════════════════
#  渲染
# ═══════════════════════════════════════════════════════════════

def f1(v, suffix="%"):
    return f"{v:+.2f}{suffix}" if isinstance(v, (int, float)) else "-"


def render_console(asset, as_of, ev):
    print("=" * 72)
    print(f"单币价格弹性 · {asset['name']}({asset['symbol']})  asset_id={asset['asset_id']}  as_of={as_of}")
    print("=" * 72)
    print(f"  评级: {ev['elasticity_grade']}    弹性分: {ev['elasticity_score']}")
    print(f"  事件弹性 AR72 均值: {f1(ev.get('ar_72h_mean'))}   中位: {f1(ev.get('ar_72h_median'))}"
          f"   样本: n={ev.get('n_events')}")
    print(f"  命中率: {ev['hit_rate']*100:.0f}% (n={ev.get('n_hit')}, z={ev.get('hit_z')})"
          if ev.get("hit_rate") is not None else f"  命中率: - (无方向样本)")
    print(f"  上下行不对称: 涨{f1(ev.get('upside_mean_24h'))} / 跌{f1(ev.get('downside_mean_24h'))}"
          f"  → asym={ev.get('asym_ratio')}")
    print(f"  基础相对强度(vs BTC, {ev['rs_source']}): 7d {f1(ev.get('rs_7d'))} | "
          f"30d {f1(ev.get('rs_30d'))} | 90d {f1(ev.get('rs_90d'))}")
    print(f"  对 BTC beta(90d): {ev.get('beta_btc')}")
    bt = ev.get("by_event_type") or {}
    if bt:
        print("  分事件类型弹性(AR72, n≥3):")
        for k, v in list(bt.items())[:12]:
            print(f"    {k:<22} n={v['n']:<4} {v['ar72_mean']:+.2f}%")


def render_md(asset, as_of, ev):
    bt = ev.get("by_event_type") or {}
    lines = [
        f"### 价格弹性（{asset['name']}({asset['symbol']})）", "",
        f"> 口径: 事件弹性 = 独立催化剂事件 AR_72h 均值（AR = excess_72h − 当日全市场中位，已扣 BTC）"
        f" ｜ method={METHOD_VERSION}",
        f"> 样本: {ev.get('event_from')} ~ {ev.get('event_to')}（n={ev.get('n_events')}）",
        "",
        f"**弹性评级：{ev['elasticity_grade']}（弹性分 {ev['elasticity_score']}/100）**", "",
        "| 指标 | 值 | 说明 |",
        "|---|---|---|",
        f"| 事件弹性 AR_72h 均值 | {f1(ev.get('ar_72h_mean'))} | 主指标：利好兑现的平均超额 |",
        f"| AR_72h 中位 | {f1(ev.get('ar_72h_median'))} | 抗极端值 |",
        f"| 命中率 | "
        f"{str(round(ev['hit_rate']*100))+'% (n='+str(ev.get('n_hit'))+')' if ev.get('hit_rate') is not None else '-'} | "
        f"方向兑现比例（50%=抛硬币）|",
        f"| 上下行不对称 | asym={ev.get('asym_ratio')}（涨{f1(ev.get('upside_mean_24h'))}/跌{f1(ev.get('downside_mean_24h'))}） | >1 涨多跌少 |",
        f"| 基础相对强度 vs BTC | 7d {f1(ev.get('rs_7d'))} / 30d {f1(ev.get('rs_30d'))} / 90d {f1(ev.get('rs_90d'))} | 无事件时的相对强弱（{ev['rs_source']}）|",
        f"| 对 BTC beta(90d) | {ev.get('beta_btc')} | 越高越跟大盘 |",
    ]
    if bt:
        lines += ["", "**分事件类型弹性（AR_72h 均值，n≥3）**", "",
                  "| 事件类型 | n | AR72 均值 |", "|---|---|---|"]
        for k, v in list(bt.items())[:12]:
            lines.append(f"| {k} | {v['n']} | {v['ar72_mean']:+.2f}% |")
    lines += ["", "> 局限: 事件源为币安广场 KOL 新闻流 + 交易所公告（非项目基本面催化，含同名/批次归因噪音）；"
              "样本期随库内数据滚动，弹性能否外推需谨慎。", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="单币价格弹性（投研 SOP P1.6）")
    ap.add_argument("--symbol", help="币种符号，如 LINK")
    ap.add_argument("--asset-id", type=int, help="asset_id")
    ap.add_argument("--as-of", help="基准日 YYYY-MM-DD（默认今天）")
    ap.add_argument("--persist", action="store_true", help="落库快照 biz.asset_price_elasticity")
    ap.add_argument("--batch", action="store_true",
                    help="全市场批量：对全部有 catalyst_outcome 事件的资产计算并落库")
    ap.add_argument("--md", action="store_true", help="输出 markdown 片段")
    ap.add_argument("--json", action="store_true", help="输出 JSON 数据包")
    args = ap.parse_args()

    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()

    if args.batch:
        with get_connection(get_settings(require_database=True).database_url) as conn:
            stat = batch_run(conn, as_of)
        print(json.dumps({"as_of": str(as_of), **stat}, ensure_ascii=False, indent=2))
        return 0

    if not args.symbol and not args.asset_id:
        print("需指定 --symbol 或 --asset-id（或 --batch）")
        return 1

    with get_connection(get_settings(require_database=True).database_url) as conn:
        asset = resolve_asset(conn, args.symbol, args.asset_id)
        if not asset:
            print(f"未找到标的: {args.symbol or args.asset_id}")
            return 1
        ev = compute(conn, asset, as_of)
        if args.json:
            print(json.dumps({"asset": asset, "as_of": str(as_of), "elasticity": ev},
                             ensure_ascii=False, indent=2, default=str))
        elif args.md:
            print(render_md(asset, as_of, ev))
        else:
            render_console(asset, as_of, ev)
        if args.persist:
            tag = persist(conn, asset, as_of, ev)
            print(f"\n[persist] 已写 biz.asset_price_elasticity: {tag}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
