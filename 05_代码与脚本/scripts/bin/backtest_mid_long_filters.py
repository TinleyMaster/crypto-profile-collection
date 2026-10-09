#!/usr/bin/env python3
"""B 信号（MID_LONG 20~50% 做多）辅助指标增强回测。

口径（2026-10-07，对齐《盘面异动扫描系统设计方案 v2》§10.3）：
  - 事件：放量大阳（chg1h≥3% & vr≥2）且滚动 24h 涨幅 20~50%，全周期 12,529 个
    （缓存 data/trade_params_events.csv，2023~2026）
  - 出场：TRAIL 3% 跟踪止盈实际出场（出场价 = 触发时峰值 × 0.97），与实盘一致
  - 辅助指标（四维）：
      balance  多空平衡K线：doji 十字星 / quiet 小实体缩量 / mid 中枢位（事件 bar，全周期）
      funding  资金费率：事件时点最近一期（funding_rate_hist，2023-01 起）
      oi       OI 变化：事件时点 vs 24h 前（cg_oi_hist 4h，2025-10 起）
      mcap     市值：事件日市值（asset_market_daily，2026-05-27 起 → 仅子样本 2,661 个）
  - 目标：叠加过滤后 B 的胜率/期望/PF 能否接近 A 信号（71.1% / +5.6% / PF 14.2）

用法：python bin/backtest_mid_long_filters.py
"""
from __future__ import annotations

import csv
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import psycopg

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402

CACHE = SCRIPT_DIR.parent / "data" / "trade_params_events.csv"
TR = 0.03
COST = 0.003
MIN_N = 50
BATCH = 2000

# 事件 bar 平衡形态判定（与 backtest_balance_bar_long.py 一致）
SQL_KL = """
WITH ev(symbol, open_time) AS (VALUES {v})
SELECT ev.symbol, ev.open_time,
       k.open_px, k.high_px, k.low_px, k.close_px, k.quote_vol,
       v20.vol20, lo.lo24, hi.hi24
FROM ev
JOIN biz.asset_klines k ON k.symbol = ev.symbol AND k.open_time = ev.open_time AND k.interval = '1h'
LEFT JOIN LATERAL (
    SELECT AVG(q.quote_vol) AS vol20 FROM biz.asset_klines q
    WHERE q.symbol = ev.symbol AND q.interval = '1h' AND q.quote_vol IS NOT NULL
      AND q.open_time >= ev.open_time - INTERVAL '20 hours' AND q.open_time < ev.open_time
) v20 ON true
LEFT JOIN LATERAL (
    SELECT MIN(q.low_px) AS lo24 FROM biz.asset_klines q
    WHERE q.symbol = ev.symbol AND q.interval = '1h'
      AND q.open_time > ev.open_time - INTERVAL '24 hours' AND q.open_time <= ev.open_time
) lo ON true
LEFT JOIN LATERAL (
    SELECT MAX(q.high_px) AS hi24 FROM biz.asset_klines q
    WHERE q.symbol = ev.symbol AND q.interval = '1h'
      AND q.open_time > ev.open_time - INTERVAL '24 hours' AND q.open_time <= ev.open_time
) hi ON true
"""

SQL_FUNDING = """
WITH ev(symbol, open_time) AS (VALUES {v})
SELECT DISTINCT ON (ev.symbol, ev.open_time) ev.symbol, ev.open_time, f.rate
FROM ev
JOIN biz.funding_rate_hist f ON f.symbol = ev.symbol AND f.funding_time <= ev.open_time
ORDER BY ev.symbol, ev.open_time, f.funding_time DESC
"""

SQL_OI = """
WITH ev(symbol, open_time) AS (VALUES {v})
SELECT ev.symbol, ev.open_time, cur.oi_close AS oi_cur, prev.oi_close AS oi_prev
FROM ev
LEFT JOIN LATERAL (
    SELECT o.oi_close FROM biz.cg_oi_hist o
    WHERE o.symbol = ev.symbol AND o.interval = '4h' AND o.ts <= ev.open_time
    ORDER BY o.ts DESC LIMIT 1
) cur ON true
LEFT JOIN LATERAL (
    SELECT o.oi_close FROM biz.cg_oi_hist o
    WHERE o.symbol = ev.symbol AND o.interval = '4h' AND o.ts <= ev.open_time - INTERVAL '24 hours'
    ORDER BY o.ts DESC LIMIT 1
) prev ON true
"""

SQL_MCAP = """
WITH ev(symbol, open_time) AS (VALUES {v})
SELECT DISTINCT ON (ev.symbol, ev.open_time) ev.symbol, ev.open_time, d.market_cap
FROM ev
JOIN biz.coin_basic c ON c.coin_symbol = REPLACE(ev.symbol, 'USDT', '')
JOIN biz.asset_market_daily d ON d.asset_id = c.asset_id AND d.market_date <= ev.open_time::date
ORDER BY ev.symbol, ev.open_time, d.market_date DESC
"""


def _vals(rows) -> str:
    return ",".join("('{}','{}'::timestamptz)".format(s, t.replace("'", "''")) for s, t in rows)


def _fetch_batches(cur, sql, events, key):
    """按 VALUES 分批执行 SQL，返回 {(symbol, eo_datetime): 行元组}。"""
    out = {}
    for i in range(0, len(events), BATCH):
        chunk = events[i:i + BATCH]
        rows = [(e["symbol"], e["eo"]) for e in chunk]
        cur.execute(sql.format(v=_vals(rows)))
        for r in cur.fetchall():
            out[(r[0], r[1])] = r
    return out


def _stats(ret: np.ndarray) -> dict | None:
    rs = ret[~np.isnan(ret)]
    if len(rs) < MIN_N:
        return None
    win = rs > 0
    n_w, n_l = int(win.sum()), int((~win).sum())
    avg_win = float(rs[win].mean()) if n_w else 0.0
    avg_loss = float(rs[~win].mean()) if n_l else 0.0
    gw = float(rs[win].sum())
    gl = abs(float(rs[~win].sum())) if n_l else 0.0
    return {
        "n": len(rs), "win_rate": float((rs > 0).mean()),
        "payoff": abs(avg_win / avg_loss) if avg_loss else float("inf"),
        "mean": float(rs.mean()), "median": float(np.median(rs)),
        "pf": gw / gl if gl > 0 else float("inf"), "worst": float(np.min(rs)),
    }


def main() -> int:
    # ── 1. 事件缓存：筛 B 信号 ──────────────────────────────
    events = []
    with CACHE.open("r", encoding="utf-8") as fp:
        for r in csv.DictReader(fp):
            if 0.20 <= float(r["chg24"]) < 0.50:
                events.append(r)
    print(f"B 信号事件: {len(events):,}  缓存: {CACHE.name}")
    sym_eo = [(e["symbol"], e["eo"]) for e in events]

    # ── 2. 关联四维特征 ─────────────────────────────────────
    with psycopg.connect(get_settings(require_database=True).database_url,
                         connect_timeout=20) as conn:
        with conn.cursor() as cur:
            print("关联 K 线平衡特征...")
            kl = _fetch_batches(cur, SQL_KL, events, "kl")
            print("关联资金费率...")
            fnd = _fetch_batches(cur, SQL_FUNDING, events, "f")
            print("关联 OI 变化...")
            oi = _fetch_batches(cur, SQL_OI, events, "oi")
            print("关联市值...")
            mc = _fetch_batches(cur, SQL_MCAP, events, "mc")

    # ── 3. 构建向量 ─────────────────────────────────────────
    n = len(events)
    ret = np.full(n, np.nan)
    doji = np.zeros(n, bool)
    quiet = np.zeros(n, bool)
    mid = np.zeros(n, bool)
    funding = np.full(n, np.nan)
    oi_chg = np.full(n, np.nan)
    mcap = np.full(n, np.nan)
    for i, e in enumerate(events):
        key = (e["symbol"], datetime.fromisoformat(e["eo"]))
        ph = e["ph_tr3"]
        if ph:
            ret[i] = float(ph) * (1 - TR) / float(e["entry"]) - 1
        k = kl.get(key)
        if k:
            op, hp, lp, cp, vol, vol20, lo24, hi24 = (float(k[j]) for j in range(2, 10))
            rng = hp - lp
            body = abs(cp - op)
            upsh = hp - max(op, cp)
            dnsh = min(op, cp) - lp
            if rng > 0:
                doji[i] = body / rng <= 0.25 and abs(upsh - dnsh) / rng <= 0.35
                quiet[i] = body / rng <= 0.25 and (vol20 and vol / vol20 < 1.0)
            if hi24 and lo24 and hi24 > lo24:
                mid[i] = 0.40 <= (cp - lo24) / (hi24 - lo24) <= 0.60
        f = fnd.get(key)
        if f and f[2] is not None:
            funding[i] = float(f[2])
        o = oi.get(key)
        if o and o[2] is not None and o[3] is not None:
            oi_chg[i] = float(o[2]) / float(o[3]) - 1
        m = mc.get(key)
        if m and m[2] is not None:
            mcap[i] = float(m[2])

    net_mask = None  # 无全周期掩码，直接按过滤子集统计

    def S(mask, label, sub=None):
        r = np.where(mask, ret, np.nan)
        st = _stats(r)
        if st is None:
            print(f"  {label:<44} n 不足")
            return None
        rn = ret[mask] - COST
        win_c = float((rn > 0).mean())
        mean_c = float(rn.mean())
        gw = float(rn[rn > 0].sum())
        gl = abs(float(rn[rn <= 0].sum())) if (rn <= 0).any() else 0.0
        pf_c = gw / gl if gl > 0 else float("inf")
        print(f"  {label:<44} n={st['n']:>6,} 胜率={st['win_rate']*100:>6.1f}% "
              f"盈亏比={st['payoff']:>5.2f} 期望={st['mean']*100:>+7.2f}% "
              f"PF={st['pf']:>6.1f} 最差={st['worst']*100:>+6.1f}% "
              f"| 扣0.3%: 胜率={win_c*100:>5.1f}% 期望={mean_c*100:>+6.2f}% PF={pf_c:>5.1f}")
        return st

    print("\n" + "=" * 120)
    print("B 信号（20~50% 做多）× TRAIL3 出场 · 辅助指标过滤对比（全周期 2023~2026，除非标注）")
    print("=" * 120)
    all_mask = np.ones(n, bool)
    print("\n■ 基准（无过滤）")
    S(all_mask, "B 全部", )

    print("\n■ ① 多空平衡K线（事件 bar）")
    S(doji, "A_doji 十字星/影线均衡")
    S(quiet, "B_quiet 小实体+缩量")
    S(mid, "C_mid 中枢位收盘")
    S(doji | quiet | mid, "任一平衡形态")

    print("\n■ ② 资金费率（事件时点最近一期）")
    S(funding <= 0, "funding ≤ 0（空头付费）")
    S((funding > 0) & (funding <= 0.0001), "0 < funding ≤ 0.01%")
    S((funding > 0.0001) & (funding <= 0.0003), "0.01% < funding ≤ 0.03%")
    S(funding > 0.0003, "funding > 0.03%（多空拥挤）")
    S(~np.isnan(funding) & (funding <= 0.0001), "funding ≤ 0.01%（合并）")

    print("\n■ ③ OI 变化（2025-10 起子样本）")
    S(~np.isnan(oi_chg), "OI 可查（子样本基准）")
    S(oi_chg > 0, "OI 增仓 >0")
    S(oi_chg <= 0, "OI 减仓 ≤0")
    S(oi_chg > 0.05, "OI 增仓 >5%")
    S((oi_chg > 0) & (oi_chg <= 0.20), "OI 增仓 0~20%")

    print("\n■ ④ 市值（2026-05-27 起子样本）")
    S(~np.isnan(mcap), "市值可查（子样本基准）")
    S(mcap < 2e7, "市值 < 2000万")
    S((mcap >= 2e7) & (mcap < 1e8), "市值 2000万~1亿")
    S((mcap >= 1e8) & (mcap < 5e8), "市值 1亿~5亿")
    S(mcap >= 5e8, "市值 ≥ 5亿")

    print("\n■ ⑤ 关键组合")
    S(quiet & (funding <= 0.0001), "quiet & funding≤0.01%")
    S(quiet & (funding <= 0.0001) & (oi_chg > 0), "quiet & funding≤0.01% & OI增")
    S((funding <= 0.0001) & (oi_chg > 0), "funding≤0.01% & OI增")
    full_cov = ~np.isnan(funding) & ~np.isnan(oi_chg) & ~np.isnan(mcap)
    S(full_cov, "三维可查交集（子样本基准）")
    S(full_cov & (funding <= 0.0001) & (oi_chg > 0) & (mcap >= 2e7),
      "funding≤0.01% & OI增 & 市值≥2000万")
    S(full_cov & quiet & (funding <= 0.0001) & (oi_chg > 0) & (mcap >= 2e7),
      "四维全过（quiet+funding+OI+市值）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
