#!/usr/bin/env python3
"""单币解锁表现（Unlock Reaction）——投研 SOP P1.7。

回答解锁研究最大的盲区：**这个币的解锁，到底是不是抛压？**
（默认叙事是「解锁 → 跌」，但实测不少币解锁照样暴涨，反之亦然。
  同一件解锁事件，不同币反应完全相反——本指标把这个分化量化。）

四条口径（method_version = v1）
-------------------------------
1. **解锁后反应（主口径）**：锚定解锁日 D，post_k = close(D+k)/close(D) − 1（k=1,3,7,14,30）
2. **超额**：excess_k = post_k − 当日全市场截面 post_k 中位（剔市场普涨 β，得"纯解锁影响"）
3. **解锁前抢跑**：pre7 = close(D)/close(D−7) − 1（解锁前的预期反应）
4. **前瞻压力**：并入 biz.asset_unlock_pressure（未来 7d/30d 解锁占比），
   与历史反应倾向组合成"下一个解锁日会怎样"。

综合输出**解锁评级**（抗解禁型/偏抗解禁/中性/偏解禁即崩/解禁即崩/样本不足/无数据）
+ **解锁反应分 0-100**。

用法
----
    python bin/asset_unlock_reaction.py --symbol LINK             # 控制台
    python bin/asset_unlock_reaction.py --symbol LINK --md        # markdown 片段
    python bin/asset_unlock_reaction.py --asset-id 1372 --persist # 落库快照
    python bin/asset_unlock_reaction.py --symbol LINK --json      # JSON 数据包

口径局限（必须如实标注，不得外推）
--------------------------------
- 解锁事件源为 tokenomics.com / tokenomist.ai 聚合站（非一手官方）。
- 受益人类别分两层（v2 起）：
  · **下一次解锁受益人 `next_unlock_beneficiary`** = **事件级**，锚定下一次解锁日，
    源 biz.asset_unlock_recipient_mix（tokenomics hero「Unlock Recipients」百分比）。
    这是回答「下一次解锁是谁的筹码」的正确字段。
  · `dominant_beneficiary` / `allocation_mix` = **资产级推断**（整条生命周期分配），
    仅作背景；两者常不一致（如 ARB 资产级=foundation，事件级下一次=team），
    判读时**以事件级为准**。
- 单币解锁事件样本量普遍偏小（多为 3–7 次），n<8 时统计推断力弱，评级仅供参考。
- 同名多 asset_id（如 ARC/JOE）已按 symbol 去重，避免样本虚高。
- **受益人来源（v3 起）**：`next_unlock_source` 标注 `next_unlock_beneficiary` 出处 ——
  `tokenomics_hero`（app.tokenomics.com 解锁页 hero，日期精确）优先；
  缺失则退 `cmc_next`（coinmarketcap token-unlocks 的 nextUnlockedDetail 归桶）。
  **两源对同一币的解锁日可能不同**（CMC 为月表口径、tokenomist 为粗粒度），故必须标注来源。
"""
from __future__ import annotations

import argparse
import bisect
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

METHOD_VERSION = "v4"  # v4: 聚合粒度 symbol → asset_id，消除同名不同币的跨币串扰（UNLOCK-SYS-002 G2.3）
MIN_N = 5                  # 解锁评级样本下限（解锁事件天然稀少，比弹性指标更低）
HORIZONS = (1, 3, 7, 14, 30)
GRADE_PUMP = 5.0           # excess7 ≥ +5% → 抗解禁型
GRADE_MID = 1.5            # ≥ +1.5% → 偏抗解禁
GRADE_NEG = -1.5           # > −1.5% → 中性
GRADE_DUMP = -5.0          # ≤ −5% → 解禁即崩
PUMP_TH = 0.05             # excess7 > +5% 记一次「抗解禁」
DUMP_TH = -0.05            # excess7 < −5% 记一次「解禁即崩」
BENCH_START = date(2022, 10, 1)  # 截面基准固定扫描窗口（保证口径稳定，与 research_unlock_longterm 同源）

# ── 该资产解锁事件（UNLOCK-SYS-002 G2.3：聚合粒度 asset_id）──────────
#
# 🔴 v4 变更（2026-10-26）：原按 `canonical_symbol` 聚合全部 asset_id，导致
#   **同名不同币的解锁事件被混算**——`core.asset` 的 ticker 命名空间被长尾山寨侵占
#   （实测 2,108 个 symbol 有多个 asset_id；BTC 撞 "Big Titty Coin"、
#   ETH 撞 "Bridged ETH"、BNB 撞 "BNB AI"…）。
#   真串扰源精确为 **11 个 symbol，各自恰有 2 个 asset_id 且都有解锁事件**：
#   AI / ARC / BOB / CATE / EDGE / GRVT / JOE / RAIN / UP / VELO / XAI，
#   同 symbol 内两个币的市值比 1.6~2121 倍 ⇒ 混算样本完全是另一个币的。
#
#   ⚠️ 早前考虑过的「按 symbol 排除撞名」方案（G2.4）**已否决**：
#   实测 168 个有解锁事件的 symbol 中撞名的有 168 个，其中 44 个是 rank≤200 主流币
#   （SOL / BNB / ADA / UNI / AVAX / ARB / TAO / NEAR / DOT / TRUMP / STX…）。
#   它们撞名的对手山寨**没有解锁数据**，聚合结果其实正确 ⇒ 按 symbol 排除会误杀主流币。
#
#   ✅ 改为 asset_id 粒度是**回归表结构的原生语义**：
#   reaction 表主键本就是 `UNIQUE (asset_id, as_of)`（已有 549 个 symbol 天然多行），
#   消费端 asset_screen.py 亦按 asset_id 关联 ⇒ 无历史可比性问题。
EVENTS_SQL = """
SELECT e.unlock_date,
       max(e.unlock_value_usd)                       AS value,
       count(*)                                       AS dup_cnt,
       count(*) FILTER (WHERE e.unlock_ratio_circulating IS NOT NULL) AS has_ratio
FROM biz.asset_unlock_event e
JOIN core.asset a ON a.asset_id = e.asset_id
WHERE e.asset_id = %(asset_id)s
  AND e.unlock_value_usd IS NOT NULL AND e.unlock_value_usd > 0
GROUP BY e.unlock_date
ORDER BY e.unlock_date
"""

# ── 全市场截面基准（只在需要的锚点日聚合，避免全量拉数） ──
BENCH_SQL = """
WITH daily AS (
    SELECT symbol,
           (open_time AT TIME ZONE 'UTC')::date AS d,
           (array_agg(close_px ORDER BY open_time DESC))[1] AS close
    FROM biz.asset_klines
    WHERE interval = '1d' AND (open_time AT TIME ZONE 'UTC')::date >= %s
    GROUP BY symbol, (open_time AT TIME ZONE 'UTC')::date
),
px AS (
    SELECT symbol, d, close,
           LEAD(close, 1)  OVER w / close - 1 AS f1,
           LEAD(close, 3)  OVER w / close - 1 AS f3,
           LEAD(close, 7)  OVER w / close - 1 AS f7,
           LEAD(close, 14) OVER w / close - 1 AS f14,
           LEAD(close, 30) OVER w / close - 1 AS f30,
           close / LAG(close, 7) OVER w - 1   AS p7
    FROM daily
    WINDOW w AS (PARTITION BY symbol ORDER BY d)
)
SELECT d,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY f1),
       percentile_cont(0.5) WITHIN GROUP (ORDER BY f3),
       percentile_cont(0.5) WITHIN GROUP (ORDER BY f7),
       percentile_cont(0.5) WITHIN GROUP (ORDER BY f14),
       percentile_cont(0.5) WITHIN GROUP (ORDER BY f30),
       percentile_cont(0.5) WITHIN GROUP (ORDER BY p7)
FROM px
WHERE d = ANY(%s) AND close > 0
GROUP BY d
"""

# ── 目标币自身日线 ──
TARGET_SERIES_SQL = """
SELECT (open_time AT TIME ZONE 'UTC')::date AS d,
       (array_agg(close_px ORDER BY open_time DESC))[1] AS close
FROM biz.asset_klines
WHERE interval = '1d' AND symbol = %s AND (open_time AT TIME ZONE 'UTC')::date >= %s
GROUP BY (open_time AT TIME ZONE 'UTC')::date
ORDER BY d
"""

# ── 前瞻解锁压力（v4：按 asset_id）──
PRESSURE_SQL = """
SELECT p.unlock_pct_7d, p.unlock_pct_30d, p.pressure_score, p.risk_level
FROM biz.asset_unlock_pressure p
JOIN core.asset a ON a.asset_id = p.asset_id
WHERE p.asset_id = %(asset_id)s
ORDER BY p.calculated_at DESC
LIMIT 1
"""

# ── 受益人构成（补齐 unlock_type 缺口；v4：按 asset_id）──
ALLOC_SQL = """
SELECT m.insider_pct, m.dominant_bucket, m.mix
FROM biz.asset_allocation_mix m
JOIN core.asset a ON a.asset_id = m.asset_id
WHERE m.asset_id = %(asset_id)s
ORDER BY m.confidence DESC NULLS LAST LIMIT 1
"""

BATCH_ALLOC_SQL = """
SELECT asset_id, insider_pct, dominant_bucket, mix FROM biz.asset_allocation_mix
"""

# ── 下一次解锁的受益人构成（事件级；源 tokenomics hero；v4：按 asset_id）──
NEXT_MIX_SQL = """
SELECT m.bucket, m.category, m.pct
FROM biz.asset_unlock_recipient_mix m
JOIN core.asset a ON a.asset_id = m.asset_id
WHERE m.asset_id = %(asset_id)s AND m.unlock_date = %(unlock_date)s
ORDER BY m.pct DESC
"""

BATCH_NEXT_MIX_SQL = """
SELECT asset_id, unlock_date, bucket, category, pct
FROM biz.asset_unlock_recipient_mix
ORDER BY asset_id, unlock_date, pct DESC
"""

# ── 下一次解锁受益人（兜底源：CMC token-unlocks，覆盖更广；见 fix_108/fix_109）──
# 仅取「有效」行：日期与主导桶都非空。源站脏日期（>today+5y，如 JUP=2123-01-01）
# 已在 ingest_cmc_unlocks.py 侧整条置空，这里再加显式条件做防御。
CMC_NEXT_SQL = """
SELECT dominant_bucket, bucket_pct, insider_pct, next_unlock_date,
       next_unlock_usd, next_unlock_pct_supply
FROM biz.asset_unlock_next_cmc
WHERE asset_id = %s
  AND dominant_bucket IS NOT NULL AND next_unlock_date IS NOT NULL
"""

BATCH_CMC_SQL = """
SELECT asset_id, dominant_bucket, bucket_pct, insider_pct,
       next_unlock_usd, next_unlock_pct_supply
FROM biz.asset_unlock_next_cmc
WHERE dominant_bucket IS NOT NULL AND next_unlock_date IS NOT NULL
"""

# ── 批量：全部已发生解锁（按 symbol 聚合去重）──
BATCH_EVENTS_SQL = """
SELECT e.asset_id,
       e.unlock_date,
       max(e.unlock_value_usd) AS value
FROM biz.asset_unlock_event e
JOIN core.asset a ON a.asset_id = e.asset_id
WHERE e.unlock_value_usd IS NOT NULL AND e.unlock_value_usd > 0
GROUP BY e.asset_id, e.unlock_date
ORDER BY e.asset_id, e.unlock_date
"""

# ── 批量：全部已发生解锁的资产（v4：按 asset_id，不再按 symbol 取代表）──
# v3 是 `DISTINCT ON (upper(canonical_symbol)) ... ORDER BY market_cap DESC`（每 symbol 一个代表），
# 那会把撞名 symbol 的两个币合并成一行。v4 改为**每个有事件的 asset_id 各出一行**，
# 与 reaction 表 `UNIQUE (asset_id, as_of)` 的原生粒度一致。
BATCH_ASSETS_SQL = """
WITH unlocked AS (
    SELECT DISTINCT e.asset_id
    FROM biz.asset_unlock_event e
    WHERE e.unlock_value_usd > 0 AND e.unlock_date <= %s
)
SELECT a.asset_id, a.canonical_symbol, a.canonical_name, a.market_cap, a.market_cap_rank
FROM core.asset a
WHERE a.asset_id IN (SELECT asset_id FROM unlocked)
ORDER BY a.asset_id
"""

# ── 批量：日线（只拉本次要评估的 symbol）──
# 🔴 2026-10-26：原版拉全市场（实测 41.7 万行 / 1,700+ symbol）。
#   但 ser_by_sym **只在批量循环里按 `upper(canonical_symbol)+'USDT'` 查**
#   （唯一读点见 batch_run 内 `series = ser_by_sym.get(...)`），
#   非目标 symbol 的序列永远不会被用到 ⇒ 按目标 symbol 过滤是**语义等价**的。
#   收益：41.7 万行 → 24.5 万行（-41%）；本机走 Tailscale DERP 时
#   行传输是唯一瓶颈（server-side 聚合只要 2s，瓶颈全在传输）。
BATCH_KLINES_SQL = """
SELECT symbol,
       (open_time AT TIME ZONE 'UTC')::date AS d,
       (array_agg(close_px ORDER BY open_time DESC))[1] AS close
FROM biz.asset_klines
WHERE interval = '1d' AND (open_time AT TIME ZONE 'UTC')::date >= %s
  AND symbol = ANY(%s)
GROUP BY symbol, (open_time AT TIME ZONE 'UTC')::date
ORDER BY symbol, d
"""

# ── 批量：全窗口截面基准（全部交易日一次算好）──
BATCH_BENCH_SQL = """
WITH daily AS (
    SELECT symbol,
           (open_time AT TIME ZONE 'UTC')::date AS d,
           (array_agg(close_px ORDER BY open_time DESC))[1] AS close
    FROM biz.asset_klines
    WHERE interval = '1d' AND (open_time AT TIME ZONE 'UTC')::date >= %s
    GROUP BY symbol, (open_time AT TIME ZONE 'UTC')::date
),
px AS (
    SELECT symbol, d, close,
           LEAD(close, 1)  OVER w / close - 1 AS f1,
           LEAD(close, 3)  OVER w / close - 1 AS f3,
           LEAD(close, 7)  OVER w / close - 1 AS f7,
           LEAD(close, 14) OVER w / close - 1 AS f14,
           LEAD(close, 30) OVER w / close - 1 AS f30,
           close / LAG(close, 7) OVER w - 1   AS p7
    FROM daily
    WINDOW w AS (PARTITION BY symbol ORDER BY d)
)
SELECT d,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY f1),
       percentile_cont(0.5) WITHIN GROUP (ORDER BY f3),
       percentile_cont(0.5) WITHIN GROUP (ORDER BY f7),
       percentile_cont(0.5) WITHIN GROUP (ORDER BY f14),
       percentile_cont(0.5) WITHIN GROUP (ORDER BY f30),
       percentile_cont(0.5) WITHIN GROUP (ORDER BY p7)
FROM px
WHERE close > 0
GROUP BY d
"""


# ═══════════════════════════════════════════════════════════════
#  纯函数层
# ═══════════════════════════════════════════════════════════════

def clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def _pct(x):
    """比率 → 百分数（保留 4 位）"""
    return None if x is None else round(100.0 * x, 4)


def grade_of(ex7_pct, n):
    """ex7_pct：解锁后7天平均超额（百分数）。"""
    if ex7_pct is None:
        return "无数据"
    if n < MIN_N:
        return "样本不足"
    if ex7_pct >= GRADE_PUMP:
        return "抗解禁型"
    if ex7_pct >= GRADE_MID:
        return "偏抗解禁"
    if ex7_pct > GRADE_NEG:
        return "中性"
    if ex7_pct > GRADE_DUMP:
        return "偏解禁即崩"
    return "解禁即崩"


def reaction_score(ex7_pct, up_rate, pump, dump, n):
    """解锁反应分 0-100：核心=excess7(0.5)、上涨率(0.25)、抗解禁−解禁即崩(0.25)；n<20 向 50 收缩。"""
    parts = []
    if ex7_pct is not None:
        parts.append((clamp(50 + 50.0 * (ex7_pct / 25.0)), 0.50))  # ±25% 映射到 0/100
    if up_rate is not None:
        parts.append((clamp(50 + (up_rate - 0.5) * 150.0), 0.25))
    if pump is not None and dump is not None:
        parts.append((clamp(50 + 100.0 * (pump - dump)), 0.25))
    if not parts:
        return None
    wsum = sum(w for _, w in parts)
    score = sum(v * w for v, w in parts) / wsum
    if n < 20:
        score = 50 + (score - 50) * n / 20.0
    return round(clamp(score), 2)


# ═══════════════════════════════════════════════════════════════
#  计算层
# ═══════════════════════════════════════════════════════════════

def _fwd(series_dates, idx, k):
    j = idx + k
    return series_dates[j][1] / series_dates[idx][1] - 1 if j < len(series_dates) else None


def compute_events(events, series, bench):
    """events: [(unlock_date, value, ...)] 已发生；series: [(d, close)]；bench: {d: {...}}
    返回逐事件明细（含 pre7 / post_k / excess_k）。"""
    dates = [d for d, _ in series]
    out = []
    for ud, val, *_ in events:
        i = bisect.bisect_left(dates, ud)
        if i >= len(dates):
            continue
        anchor = dates[i]
        if (anchor - ud).days > 3:          # 锚点离解锁日太远，放弃
            continue
        rec = {"unlock_date": ud, "anchor": anchor, "value": float(val)}
        rec["pre7"] = _fwd(series, i - 7, 7) if i >= 7 else None   # close(D)/close(D−7) − 1
        b = bench.get(anchor, {})
        for k in HORIZONS:
            f = _fwd(series, i, k)
            rec[f"post{k}"] = f
            bk = b.get(k)
            rec[f"excess{k}"] = (f - bk) if (f is not None and bk is not None) else None
        rec["excess7"] = rec.get("excess7")
        out.append(rec)
    return out


def summarize(recs):
    out = {"n_events": len(recs)}
    if not recs:
        return out

    def mean_pct(key):
        v = [r[key] for r in recs if r.get(key) is not None]
        return _pct(sum(v) / len(v)) if v else None

    out["pre7_mean"] = mean_pct("pre7")
    out["pre7_median"] = _pct(median([r["pre7"] for r in recs]))
    for k in HORIZONS:
        out[f"post{k}_mean"] = mean_pct(f"post{k}")
    out["post7_median"] = _pct(median([r["post7"] for r in recs]))
    for k in HORIZONS:
        out[f"excess{k}_mean"] = mean_pct(f"excess{k}")
    out["excess7_median"] = _pct(median([r["excess7"] for r in recs]))

    p7 = [r["post7"] for r in recs if r.get("post7") is not None]
    if p7:
        out["up_rate_7"] = round(sum(1 for v in p7 if v > 0) / len(p7), 4)
    e7 = [r["excess7"] for r in recs if r.get("excess7") is not None]
    out["n_excess7"] = len(e7)
    if e7:
        out["pump_rate"] = round(sum(1 for v in e7 if v > PUMP_TH) / len(e7), 4)
        out["dump_rate"] = round(sum(1 for v in e7 if v < DUMP_TH) / len(e7), 4)
    else:
        out["pump_rate"] = out["dump_rate"] = None

    out["by_horizon"] = {
        str(k): {
            "post_mean": out.get(f"post{k}_mean"),
            "post_median": _pct(median([r[f"post{k}"] for r in recs])),
            "excess_mean": out.get(f"excess{k}_mean"),
            "n": len([r for r in recs if r.get(f"post{k}") is not None]),
        }
        for k in HORIZONS
    }
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


def next_beneficiary(conn, asset_id, symbol, next_date):
    """下一次解锁的受益人构成 → (dominant_bucket, mix_list, source)；无数据返回 (None, None, None)。

    与 ALLOC_SQL（资产级、整条生命周期）不同，这里锚定**具体那一次解锁日**，回答
    「下一次解锁是谁的筹码」。**两源按优先级取**（v3）：
      1. `tokenomics_hero` — biz.asset_unlock_recipient_mix 命中该解锁日（事件级、日期精确）
      2. `cmc_next`        — biz.asset_unlock_next_cmc 的 nextUnlockedDetail 归桶（覆盖更广）
    """
    cur = conn.cursor()
    # 1) tokenomics hero（事件级，日期精确；v4：按 asset_id）
    if next_date:
        cur.execute(NEXT_MIX_SQL, {"asset_id": asset_id, "unlock_date": next_date})
        rows = cur.fetchall()
        if rows:
            mix = [{"category": c, "bucket": b, "pct": float(p)} for b, c, p in rows]
            return rows[0][0], mix, "tokenomics_hero"
    # 2) CMC 兜底（另一源，事件级）
    cur.execute(CMC_NEXT_SQL, (asset_id,))
    r = cur.fetchone()
    if r and r[0]:
        dom, bucket_pct, _insider, _dt, _usd, _pct_sup = r
        # bucket_pct 存的是 0-1 小数；统一成百分数口径（与 tokenomics mix 一致）
        mix = [{"category": k, "bucket": k, "pct": round(float(v) * 100, 6)}
               for k, v in (bucket_pct or {}).items()]
        return dom, mix, "cmc_next"
    return None, None, None


def compute(conn, asset, as_of: date) -> dict:
    sym = asset["symbol"]
    cur = conn.cursor()

    # 1) 解锁事件（v4：按 asset_id 聚合，去重）
    cur.execute(EVENTS_SQL, {"asset_id": asset["asset_id"]})
    all_ev = cur.fetchall()
    past = [(ud, val) for ud, val, _dup, _hr in all_ev if ud <= as_of]
    future = [ud for ud, _v, _d, _h in all_ev if ud > as_of]

    out = {
        "n_seen": len(past),
        "next_unlock": min(future) if future else None,
        "event_from": None,
        "event_to": None,
    }
    if not past:
        out.update({"n_events": 0, "reaction_grade": "无数据", "reaction_score": None})
        return out

    # 2) 目标币日线
    start = datetime.combine(min(ud for ud, _ in past) - timedelta(days=400), datetime.min.time())
    pair = sym.upper() + "USDT"
    cur.execute(TARGET_SERIES_SQL, (pair, start))
    series = [(d, float(c)) for d, c in cur.fetchall() if c is not None]
    if not series:
        out.update({"n_events": 0, "reaction_grade": "无数据", "reaction_score": None})
        return out

    # 3) 逐事件锚点 → 需要基准的日期
    recs0 = compute_events(past, series, {})   # 先只取锚点
    anchor_dates = sorted({r["anchor"] for r in recs0})
    bench = {}
    if anchor_dates:
        cur.execute(BENCH_SQL, (BENCH_START, anchor_dates))
        for row in cur.fetchall():
            d = row[0]
            bench[d] = {
                1: float(row[1]) if row[1] is not None else None,
                3: float(row[2]) if row[2] is not None else None,
                7: float(row[3]) if row[3] is not None else None,
                14: float(row[4]) if row[4] is not None else None,
                30: float(row[5]) if row[5] is not None else None,
            }
    recs = compute_events(past, series, bench)

    summary = summarize(recs)
    out.update(summary)
    ds = sorted(r["anchor"] for r in recs)
    if ds:
        out["event_from"], out["event_to"] = ds[0], ds[-1]

    # 4) 前瞻压力（v4：按 asset_id）
    cur.execute(PRESSURE_SQL, {"asset_id": asset["asset_id"]})
    pr = cur.fetchone()
    if pr:
        out["unlock_pct_7d"] = float(pr[0]) if pr[0] is not None else None
        out["unlock_pct_30d"] = float(pr[1]) if pr[1] is not None else None
        out["pressure_score"] = float(pr[2]) if pr[2] is not None else None
        out["pressure_level"] = pr[3]

    # 4b) 受益人构成（补齐 unlock_type 缺口；v4：按 asset_id）
    cur.execute(ALLOC_SQL, {"asset_id": asset["asset_id"]})
    al = cur.fetchone()
    if al:
        out["insider_pct"] = float(al[0]) if al[0] is not None else None
        out["dominant_beneficiary"] = al[1]
        out["allocation_mix"] = al[2]

    # 4c) 下一次解锁的受益人构成（事件级，比 4b 更贴近当下抛压）
    #     v3：tokenomics hero 优先，缺失则退 CMC（并标注来源）
    dom, nmix, nsrc = next_beneficiary(conn, asset["asset_id"], sym, out.get("next_unlock"))
    if dom:
        out["next_unlock_beneficiary"] = dom
        out["next_unlock_recipient_json"] = nmix
        out["next_unlock_source"] = nsrc

    # 5) 评级 + 分数
    ex7 = out.get("excess7_mean")
    n = out.get("n_events", 0)
    out["reaction_grade"] = grade_of(ex7, n)
    out["reaction_score"] = reaction_score(ex7, out.get("up_rate_7"),
                                           out.get("pump_rate"), out.get("dump_rate"), n)
    return out


# ── reaction 落库 SQL（拆成常量 + 参数组装，单条/批量共用）────────────
#
# 🔴 性能（2026-10-26 实测）：单条 upsert 在本机网络下 **0.38 秒/条**
#   （552 个资产 ⇒ 约 210 秒纯等待，叠加计算会撞 560s 超时上限）。
#   根因是 TUN 代理逐条往返的延迟，不是 SQL 本身慢。
#   ⇒ 批量路径改用 executemany（一次往返发全部），单条路径保留（CLI 单币查询用）。
_REACTION_INSERT_SQL = """
        INSERT INTO biz.asset_unlock_reaction (
            asset_id, as_of, method_version,
            n_events, n_seen, event_from, event_to, next_unlock,
            pre7_mean, pre7_median, pre7_excess_mean,
            post1_mean, post3_mean, post7_mean, post7_median, post14_mean, post30_mean,
            excess1_mean, excess7_mean, excess7_median, excess14_mean, excess30_mean,
            up_rate_7, pump_rate, dump_rate,
            unlock_pct_7d, unlock_pct_30d, pressure_score, pressure_level,
            insider_pct, dominant_beneficiary, allocation_mix,
            next_unlock_beneficiary, next_unlock_recipient_json, next_unlock_source,
            reaction_grade, reaction_score, by_horizon, updated_at
        ) VALUES (
            %s,%s,%s,
            %s,%s,%s,%s,%s,
            %s,%s,%s,
            %s,%s,%s,%s,%s,%s,
            %s,%s,%s,%s,%s,
            %s,%s,%s,
            %s,%s,%s,%s,
            %s,%s,%s,
            %s,%s,%s,
            %s,%s,%s, NOW()
        )
        ON CONFLICT (asset_id, as_of) DO UPDATE SET
            method_version = EXCLUDED.method_version,
            n_events = EXCLUDED.n_events, n_seen = EXCLUDED.n_seen,
            event_from = EXCLUDED.event_from, event_to = EXCLUDED.event_to,
            next_unlock = EXCLUDED.next_unlock,
            pre7_mean = EXCLUDED.pre7_mean, pre7_median = EXCLUDED.pre7_median,
            pre7_excess_mean = EXCLUDED.pre7_excess_mean,
            post1_mean = EXCLUDED.post1_mean, post3_mean = EXCLUDED.post3_mean,
            post7_mean = EXCLUDED.post7_mean, post7_median = EXCLUDED.post7_median,
            post14_mean = EXCLUDED.post14_mean, post30_mean = EXCLUDED.post30_mean,
            excess1_mean = EXCLUDED.excess1_mean, excess7_mean = EXCLUDED.excess7_mean,
            excess7_median = EXCLUDED.excess7_median, excess14_mean = EXCLUDED.excess14_mean,
            excess30_mean = EXCLUDED.excess30_mean,
            up_rate_7 = EXCLUDED.up_rate_7, pump_rate = EXCLUDED.pump_rate, dump_rate = EXCLUDED.dump_rate,
            unlock_pct_7d = EXCLUDED.unlock_pct_7d, unlock_pct_30d = EXCLUDED.unlock_pct_30d,
            pressure_score = EXCLUDED.pressure_score, pressure_level = EXCLUDED.pressure_level,
            insider_pct = EXCLUDED.insider_pct, dominant_beneficiary = EXCLUDED.dominant_beneficiary,
            allocation_mix = EXCLUDED.allocation_mix,
            next_unlock_beneficiary = EXCLUDED.next_unlock_beneficiary,
            next_unlock_recipient_json = EXCLUDED.next_unlock_recipient_json,
            next_unlock_source = EXCLUDED.next_unlock_source,
            reaction_grade = EXCLUDED.reaction_grade, reaction_score = EXCLUDED.reaction_score,
            by_horizon = EXCLUDED.by_horizon, updated_at = NOW()
    """


def _reaction_params(asset, as_of, ev):
    """把一条计算结果组装成 reaction 表的插入参数（单条/批量共用）。"""
    return (

        asset["asset_id"], as_of, METHOD_VERSION,
        ev.get("n_events"), ev.get("n_seen"), ev.get("event_from"), ev.get("event_to"), ev.get("next_unlock"),
        ev.get("pre7_mean"), ev.get("pre7_median"), ev.get("pre7_excess_mean"),
        ev.get("post1_mean"), ev.get("post3_mean"), ev.get("post7_mean"),
        ev.get("post7_median"), ev.get("post14_mean"), ev.get("post30_mean"),
        ev.get("excess1_mean"), ev.get("excess7_mean"), ev.get("excess7_median"),
        ev.get("excess14_mean"), ev.get("excess30_mean"),
        ev.get("up_rate_7"), ev.get("pump_rate"), ev.get("dump_rate"),
        ev.get("unlock_pct_7d"), ev.get("unlock_pct_30d"), ev.get("pressure_score"), ev.get("pressure_level"),
        ev.get("insider_pct"), ev.get("dominant_beneficiary"),
        json.dumps(ev.get("allocation_mix") or {}, ensure_ascii=False) if ev.get("allocation_mix") is not None else None,
        ev.get("next_unlock_beneficiary"),
        json.dumps(ev.get("next_unlock_recipient_json") or [], ensure_ascii=False) if ev.get("next_unlock_recipient_json") is not None else None,
        ev.get("next_unlock_source"),
        ev.get("reaction_grade"), ev.get("reaction_score"),
        json.dumps(ev.get("by_horizon") or {}, ensure_ascii=False),
    
    )


def persist(conn, asset, as_of, ev) -> str:
    cur = conn.cursor()
    cur.execute(_REACTION_INSERT_SQL, _reaction_params(asset, as_of, ev))
    return f"asset_id={asset['asset_id']} as_of={as_of}"


# ═══════════════════════════════════════════════════════════════
#  批量模式（全市场落库）
# ═══════════════════════════════════════════════════════════════

def batch_run(conn, as_of, commit_every=200) -> dict:
    from collections import defaultdict
    cur = conn.cursor()
    import time as _time
    _t0 = _time.time()
    def _lap(msg):
        print(f"    [batch {as_of}] {_time.time() - _t0:7.1f}s  {msg}", flush=True)

    # 1) 事件（v4：按 asset_id 聚合去重）
    cur.execute(BATCH_EVENTS_SQL)
    ev_by_id = defaultdict(list)
    for aid, ud, val in cur.fetchall():
        ev_by_id[aid].append((ud, val, 1, 1))
    _lap(f"events {sum(len(v) for v in ev_by_id.values())} 条 / {len(ev_by_id)} 资产")

    # 2) 资产（v4：每个有事件的 asset_id 一行）
    cur.execute(BATCH_ASSETS_SQL, (as_of,))
    assets = [{"asset_id": r[0], "symbol": r[1], "name": r[2], "market_cap": r[3], "rank": r[4]}
              for r in cur.fetchall()]
    _lap(f"assets {len(assets)} 个")

    # 3) 目标 symbol 的日线（只拉会被用到的那些，见 BATCH_KLINES_SQL 注释）
    # 🔴 2026-10-26：这里曾用「CTAS 物化 + LIMIT/OFFSET 分块读回」，
    #   那是**反向优化**——LIMIT/OFFSET 分块每块都要把全表重新排序，
    #   84 块 × 41.7 万行排序 = 实测 300s+ 仍没读完（O(n²)）。
    #   且当时的「TUN 吞大响应」结论是在**全机断网**期间测出来的，属误判：
    #   同一脚本 11:24 用原始 fetchall 路径跑完全程只用了 85s。
    # ✅ 正确解法：psycopg3 server-side cursor（cur.stream）——
    #   一次查询、服务端游标分批吐行、无重复排序、也不产生超大单次响应；
    #   再叠加「只拉目标 symbol」，把传输量从 41.7 万行降到 24.5 万行。
    need_syms = sorted({(a["symbol"] or "").upper() + "USDT" for a in assets})
    ser_by_sym = defaultdict(list)
    _n_kl = 0
    # 🔴 2026-10-26 性能实测与最终方案（prod 经 Tailscale 访问，链路会劣化）：
    #   本机到 prod 的**大响应传输**在链路劣化时极慢——实测同一条
    #   `SELECT symbol, open_time, close_px ... LIMIT 20000` 查询，
    #   13:52 时 0.0s，14:50 时 **126.6s**（≈23KB/s）；而 `SELECT 1` 只要 0.3s。
    #   ⇒ 是**传输体积**触发的，不是 SQL 慢（server-side 聚合仅 0.73~1.4s，
    #      EXPLAIN ANALYZE 已验证）。
    #   曾试过并否决的三种方案：
    #     ① CTAS 物化 + LIMIT/OFFSET 分块 → O(n²)，每块重排全表，300s+ 没读完
    #     ② psycopg3 cur.stream(size=N)     → 分块取行需 **libpq 17+ 且 PG 服务端 17+**；
    #        本机 libpq 18.3 但 prod PG **16.15** ⇒ 静默退化成单行模式（24.5 万次往返）
    #     ③ 命名游标 DECLARE + FETCH 20000  → 首次 FETCH 就要整表排序+聚合物化，
    #        实测首取 20,000 行 = **85.7s**
    # ✅ 最终方案：**按 symbol 分批**，每批响应控制在数千行（≈5,000 行上限）。
    #   ① 语义等价：ser_by_sym 只在 `ser_by_sym.get(sym+'USDT')` 一处被查，
    #      非目标 symbol 的序列永远用不到 ⇒ 只拉目标 symbol 不改变任何结果。
    #   ② 先取每个 symbol 的行数（558 行的小结果），再按累计行数贪心分组，
    #      保证每批响应体量可控；每批是独立查询，不重排序、无 O(n²)。
    _KL_BATCH_ROWS = 5000
    cur.execute("""SELECT symbol, count(*) FROM biz.asset_klines
                   WHERE interval = '1d' AND (open_time AT TIME ZONE 'UTC')::date >= %s
                     AND symbol = ANY(%s)
                   GROUP BY symbol""", (BENCH_START, need_syms))
    _cnt = {s: n for s, n in cur.fetchall()}
    _batches, _cur_batch, _cur_rows = [], [], 0
    for _s in need_syms:
        _n = _cnt.get(_s, 0)
        if _cur_batch and (_cur_rows + _n > _KL_BATCH_ROWS):
            _batches.append(_cur_batch)
            _cur_batch, _cur_rows = [], 0
        _cur_batch.append(_s)
        _cur_rows += _n
    if _cur_batch:
        _batches.append(_cur_batch)
    _lap(f"klines 分 {len(_batches)} 批（目标 {len(need_syms)} symbol / "
         f"{sum(_cnt.values())} 行，上限 {_KL_BATCH_ROWS} 行/批）")
    for _bi, _batch in enumerate(_batches, 1):
        cur.execute(BATCH_KLINES_SQL, (BENCH_START, _batch))
        for sym, d, close in cur.fetchall():
            _n_kl += 1
            if close is not None:
                ser_by_sym[sym].append((d, float(close)))
        if _bi % 10 == 0 or _bi == len(_batches):
            _lap(f"  klines 批次 {_bi}/{len(_batches)} 累计 {_n_kl} 行")
    for v in ser_by_sym.values():
        v.sort()
    _lap(f"klines 完成 {_n_kl} 行 / {len(ser_by_sym)} symbol")

    # 4) 截面基准（全窗口一次算好，1,487 行——直接取，无需流式）
    cur.execute(BATCH_BENCH_SQL, (BENCH_START,))
    bench = {}
    for row in cur.fetchall():
        d = row[0]
        bench[d] = {
            1: float(row[1]) if row[1] is not None else None,
            3: float(row[2]) if row[2] is not None else None,
            7: float(row[3]) if row[3] is not None else None,
            14: float(row[4]) if row[4] is not None else None,
            30: float(row[5]) if row[5] is not None else None,
        }
    _lap(f"bench {len(bench)} 个交易日")

    # 5) 分配构成
    cur.execute(BATCH_ALLOC_SQL)
    alloc_by_id = {r[0]: (float(r[1]) if r[1] is not None else None, r[2], r[3])
                   for r in cur.fetchall()}
    _lap(f"alloc {len(alloc_by_id)} 行")

    # 5b) 下一次解锁的受益人构成（事件级，按 (asset_id, unlock_date) 聚合）
    cur.execute(BATCH_NEXT_MIX_SQL)
    _nmix = defaultdict(list)
    for aid, ud, bucket, cat, pct in cur.fetchall():
        _nmix[(aid, ud)].append({"category": cat, "bucket": bucket, "pct": float(pct)})
    # 行已按 pct DESC 排序 → 首元素即主导桶
    next_mix_by_key = {k: (v[0]["bucket"], v) for k, v in _nmix.items() if v}
    _lap(f"next_mix {len(next_mix_by_key)} 键")

    # 5c) CMC 下一次解锁受益人（兜底源；v3）
    cur.execute(BATCH_CMC_SQL)
    cmc_by_id = {}
    for aid, dom, bpct, _ins, _usd, _ps in cur.fetchall():
        if dom:
            mix = [{"category": k, "bucket": k, "pct": round(float(v) * 100, 6)}
                   for k, v in (bpct or {}).items()]
            cmc_by_id[aid] = (dom, mix)

    # 6) 前瞻压力（v4：按 asset_id 取最新一条；原按 symbol 会让撞名币互串）
    cur.execute("""
        SELECT p.asset_id, p.unlock_pct_7d, p.unlock_pct_30d, p.pressure_score, p.risk_level
        FROM biz.asset_unlock_pressure p JOIN core.asset a ON a.asset_id = p.asset_id
        ORDER BY p.asset_id, p.calculated_at DESC NULLS LAST
    """)
    pres = {}
    for aid, p7, p30, ps, lvl in cur.fetchall():
        if aid not in pres:
            pres[aid] = (p7, p30, ps, lvl)

    # 7) 逐资产计算（先全部算完，再批量落库）
    n_done = n_nodata = 0
    grades = defaultdict(int)
    rows_to_write = []
    for i, asset in enumerate(assets):
        sym = asset["symbol"]
        all_ev = ev_by_id.get(asset["asset_id"], [])
        past = [(ud, val) for ud, val, _d, _h in all_ev if ud <= as_of]
        future = [ud for ud, _v, _d, _h in all_ev if ud > as_of]
        out = {"n_seen": len(past), "next_unlock": min(future) if future else None,
               "event_from": None, "event_to": None}
        series = ser_by_sym.get((sym or "").upper() + "USDT")
        if not past or not series:
            out.update({"n_events": 0, "reaction_grade": "无数据", "reaction_score": None})
            n_nodata += 1
        else:
            recs = compute_events(past, series, bench)
            summary = summarize(recs)
            out.update(summary)
            ds = sorted(r["anchor"] for r in recs)
            if ds:
                out["event_from"], out["event_to"] = ds[0], ds[-1]
            pr = pres.get(asset["asset_id"])
            if pr:
                out["unlock_pct_7d"] = float(pr[0]) if pr[0] is not None else None
                out["unlock_pct_30d"] = float(pr[1]) if pr[1] is not None else None
                out["pressure_score"] = float(pr[2]) if pr[2] is not None else None
                out["pressure_level"] = pr[3]
            ex7 = out.get("excess7_mean")
            n = out.get("n_events", 0)
            out["reaction_grade"] = grade_of(ex7, n)
            out["reaction_score"] = reaction_score(ex7, out.get("up_rate_7"),
                                                   out.get("pump_rate"), out.get("dump_rate"), n)
            n_done += 1
            grades[out["reaction_grade"]] += 1
        al = alloc_by_id.get(asset["asset_id"])
        if al:
            out["insider_pct"], out["dominant_beneficiary"], out["allocation_mix"] = al
        nm = next_mix_by_key.get((asset["asset_id"], out.get("next_unlock")))
        if nm:
            out["next_unlock_beneficiary"] = nm[0]
            out["next_unlock_recipient_json"] = nm[1]
            out["next_unlock_source"] = "tokenomics_hero"
        else:
            cm = cmc_by_id.get(asset["asset_id"])
            if cm:
                out["next_unlock_beneficiary"] = cm[0]
                out["next_unlock_recipient_json"] = cm[1]
                out["next_unlock_source"] = "cmc_next"
        rows_to_write.append(_reaction_params(asset, as_of, out))
    _lap(f"计算完成 {len(rows_to_write)} 行待写")

    # 7b) 批量落库（executemany 一次往返；单条 upsert 实测 0.38s/条会超时）
    with conn.cursor() as cur:
        for i in range(0, len(rows_to_write), 100):
            cur.executemany(_REACTION_INSERT_SQL, rows_to_write[i:i + 100])
            conn.commit()
    conn.commit()
    return {"n_persisted": len(assets), "n_done": n_done, "n_nodata": n_nodata,
            "grades": dict(grades)}


# ═══════════════════════════════════════════════════════════════
#  渲染
# ═══════════════════════════════════════════════════════════════

def f1(v, suffix="%"):
    return f"{v:+.2f}{suffix}" if isinstance(v, (int, float)) else "-"


def f0(v, suffix=""):
    return f"{v:.0f}{suffix}" if isinstance(v, (int, float)) else "-"


def render_console(asset, as_of, ev):
    print("=" * 72)
    print(f"单币解锁表现 · {asset['name']}({asset['symbol']})  asset_id={asset['asset_id']}  as_of={as_of}")
    print("=" * 72)
    print(f"  评级: {ev['reaction_grade']}    解锁反应分: {ev['reaction_score']}")
    print(f"  样本: n={ev.get('n_events')}（已发生 {ev.get('n_seen')}）"
          f"  事件期: {ev.get('event_from')} ~ {ev.get('event_to')}")
    print(f"  下一个解锁日: {ev.get('next_unlock') or '无'}"
          + (f"    前瞻压力: 7d {f1(ev.get('unlock_pct_7d'),'')}% / 30d {f1(ev.get('unlock_pct_30d'),'')}%"
             f"  ({ev.get('pressure_level')}, score={f0(ev.get('pressure_score'))})"
             if ev.get("unlock_pct_30d") is not None else ""))
    print(f"  解锁前抢跑 pre7(均值): {f1(ev.get('pre7_mean'))}   中位: {f1(ev.get('pre7_median'))}")
    print(f"  解锁后 post7(均值): {f1(ev.get('post7_mean'))}   中位: {f1(ev.get('post7_median'))}")
    print(f"  解锁超额 excess7(均值): {f1(ev.get('excess7_mean'))}   中位: {f1(ev.get('excess7_median'))}")
    print(f"  上涨率(post7>0): {f1(ev.get('up_rate_7')*100 if ev.get('up_rate_7') is not None else None,'%')}"
          f"   抗解禁率(ex7>+5%): {f1(ev.get('pump_rate')*100 if ev.get('pump_rate') is not None else None,'%')}"
          f"   解禁即崩率(ex7<−5%): {f1(ev.get('dump_rate')*100 if ev.get('dump_rate') is not None else None,'%')}")
    _nb = ev.get("next_unlock_beneficiary")
    if _nb:
        _mix_s = ", ".join(f"{m['category']} {m['pct']:.1f}%"
                           for m in (ev.get("next_unlock_recipient_json") or []))
        print(f"  下一次解锁受益人（事件级·{ev.get('next_unlock_source') or '-'}）: {_nb}   ｜ {_mix_s}")
    bh = ev.get("by_horizon") or {}
    if bh:
        print("  分窗口（均值/中位/超额）:")
        for k in ("1", "3", "7", "14", "30"):
            v = bh.get(k)
            if v:
                print(f"    D+{k:<3} post {f1(v.get('post_mean')):>9} / {f1(v.get('post_median')):>9}"
                      f"   excess {f1(v.get('excess_mean')):>9}   n={v.get('n')}")


def render_md(asset, as_of, ev):
    lines = [
        f"### 解锁表现（{asset['name']}({asset['symbol']})）", "",
        f"> 口径: 锚定解锁日 D，post_k = close(D+k)/close(D)−1；excess_k = post_k − 当日全市场截面中位"
        f" ｜ method={METHOD_VERSION}",
        f"> 样本: n={ev.get('n_events')}（已发生 {ev.get('n_seen')}）"
        f"  事件期 {ev.get('event_from')} ~ {ev.get('event_to')}",
        "",
        f"**解锁评级：{ev['reaction_grade']}（解锁反应分 {ev['reaction_score']}/100）**", "",
        "| 指标 | 值 | 说明 |",
        "|---|---|---|",
        f"| 解锁后 post7 均值 | {f1(ev.get('post7_mean'))} | 解锁后 7 天平均涨跌 |",
        f"| **解锁超额 excess7 均值** | **{f1(ev.get('excess7_mean'))}** | 主指标：剔除市场普涨后的纯解锁影响 |",
        f"| 解锁超额 excess7 中位 | {f1(ev.get('excess7_median'))} | 抗极端值 |",
        f"| 解锁前抢跑 pre7 | {f1(ev.get('pre7_mean'))} | 解锁前 7 天表现（预期反应）|",
        f"| 上涨率 / 抗解禁率 / 解禁即崩率 | "
        f"{f0(ev.get('up_rate_7')*100 if ev.get('up_rate_7') is not None else None,'%')} / "
        f"{f0(ev.get('pump_rate')*100 if ev.get('pump_rate') is not None else None,'%')} / "
        f"{f0(ev.get('dump_rate')*100 if ev.get('dump_rate') is not None else None,'%')} | "
        f"post7>0 / excess7>+5% / excess7<−5% |",
        f"| 下一个解锁日 | {ev.get('next_unlock') or '无'} | "
        + (f"前瞻压力 7d {f1(ev.get('unlock_pct_7d'),'')}% / 30d {f1(ev.get('unlock_pct_30d'),'')}%"
           f"（{ev.get('pressure_level')}）" if ev.get("unlock_pct_30d") is not None else "无前瞻压力数据")
        + " |",
        f"| 受益人构成 | 内部(团队+投资人) {f1(ev.get('insider_pct'),'%')}"
        f" ｜ 主导 {ev.get('dominant_beneficiary') or '-'} | 区分「团队/VC 解锁」vs「生态/社区解锁」"
        f"（资产级推断，见 allocation_mix）|",
        f"| **下一次解锁受益人** | **{ev.get('next_unlock_beneficiary') or '-'}**"
        + (" ｜ " + ", ".join(f"{m['category']} {m['pct']:.1f}%"
                              for m in (ev.get("next_unlock_recipient_json") or []))
           if ev.get("next_unlock_recipient_json") else "")
        + f" | 事件级精确归属（源 {ev.get('next_unlock_source') or '-'}）；"
          f"锚定下一次解锁日，比资产级更贴近当下抛压 |",
    ]
    bh = ev.get("by_horizon") or {}
    if bh:
        lines += ["", "**分窗口明细（post 均值 / 中位 / 超额均值）**", "",
                  "| 窗口 | post均值 | post中位 | excess均值 | n |", "|---|---|---|---|---|"]
        for k in ("1", "3", "7", "14", "30"):
            v = bh.get(k)
            if v:
                lines.append(f"| D+{k} | {f1(v.get('post_mean'))} | {f1(v.get('post_median'))} "
                             f"| {f1(v.get('excess_mean'))} | {v.get('n')} |")
    _src_txt = {"tokenomics_hero": "app.tokenomics.com 解锁页 hero",
                "cmc_next": "coinmarketcap token-unlocks（nextUnlockedDetail）"}.get(
                    ev.get("next_unlock_source"), "两个事件级源均无")
    lines += ["", "> 局限: 解锁事件源为 tokenomics.com / tokenomist.ai 聚合站；**下一次解锁受益人"
              f"为事件级**（源 {_src_txt}，锚定下一次解锁日），资产级 dominant_bucket 仅作背景"
              "（两者常不一致，判读以事件级为准）；单币样本量普遍偏小（多为 3–7 次），n<8 时推断力弱。", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="单币解锁表现（投研 SOP P1.7）")
    ap.add_argument("--symbol", help="币种符号，如 LINK")
    ap.add_argument("--asset-id", type=int, help="asset_id")
    ap.add_argument("--as-of", help="基准日 YYYY-MM-DD（默认今天）")
    ap.add_argument("--persist", action="store_true", help="落库快照 biz.asset_unlock_reaction")
    ap.add_argument("--batch", action="store_true",
                    help="全市场批量：对全部已发生解锁的资产计算并落库")
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
            print(json.dumps({"asset": asset, "as_of": str(as_of), "unlock_reaction": ev},
                             ensure_ascii=False, indent=2, default=str))
        elif args.md:
            print(render_md(asset, as_of, ev))
        else:
            render_console(asset, as_of, ev)
        if args.persist:
            tag = persist(conn, asset, as_of, ev)
            print(f"\n[persist] 已写 biz.asset_unlock_reaction: {tag}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
