#!/usr/bin/env python3
"""盘面告警 · 逐信号多窗口结局结算（告警胜率赔率日报 P0）。

设计依据：04_架构与代码方案/告警胜率赔率日报方案_2026-09-23.md §4.1 / §4.4

做什么
------
把 `biz.scan_signal` 里**已告警**的信号，按 T+1h / 4h / 12h / 24h 四个窗口结算成
「方向对齐净收益」，并同步算 BTC 同方向同窗口收益作为 **beta 对照**，落
`biz.scan_signal_outcome`。日报（`build_scan_edge_report.py`）只读这张表。

口径（**5m 为主、1h 兜底**）
--------------------------
* 周期选择：`alerted_at` 前 2h 内有 5m K 线 → 用 5m，否则退 1h（同一行 4 个窗口与
  BTC 对照**共用同一周期**，保证间隔一致）；
* 基线：`alerted_at` 时刻**最后一根**该周期 K 线（`open_time <= alerted_at`）的收盘价；
* 终点：`alerted_at + N hours` 时刻**最后一根**该周期 K 线的收盘价；
* 净收益：`(px1-px0)/px0*100 - COST`，再按 `p_dir` 对齐（up 取正、down 取反）；
* 未到期窗口**不写**（`last_window` 闸门），避免「还没跌」的样本系统性高估胜率。

资金费率成本（§12.1-A4，新增对照列，**不改** `aligned_ret_*` 既有语义）
--------------------------------------------------------------------
上面那份成本只有双边 taker 0.1%，而信号语义是「持有 N 小时」，N 小时内跨多个
资金费率结算点 ⇒ 费率是真实持仓成本却被记为 0，24h 净期望被系统性高估。故新增：

* `funding_pct_{w}h` = `sign × Σ_{f∈(alerted_at, alerted_at+w]} rate_f × 100`，
  `sign = +1`（多头 up，正费率**支付**）/ `-1`（空头 down，正费率**收取**）；
* 区间 **左开右闭**：`funding_time` 结算的是该时点**之前**那一持仓期的费用，
  恰落在 `alerted_at` 的结算点属入场前，不计入；
* `net_ret_{w}h` = `aligned_ret_{w}h − funding_pct_{w}h`；BTC 侧同口径得
  `btc_net_ret_{w}h`，`excess_net_{w}h` = 两者之差（新口径 alpha）；
* ⚠️ 结算周期**不统一**（实测 4h 73,994 条主导 / 8h 32,796 / 1h 2,891）
  ⇒ 一律按区间内**真实结算点**求和，**严禁硬编码「24h = 3 个结算点」**；
* **缺数据不得记 0**：区间内无结算点但该 symbol 费率序列有覆盖 → 合法 0；
  完全无覆盖 → 列写 NULL 并以 `funding_src`（ok/partial/none）标记，不参与统计。

⚠️ 为什么不用「1h 已收盘棒」：1h 棒 `open_time <= alerted_at` 的 `close_px` 是
**该小时收盘**价，即告警后最多 60 分钟的价；对突破类信号等于「晚进场一小时」，
实测会把 09-17 的 T+1h 胜率从 47.1% 压到 29.4%（系统性负偏）。改用 5m 棒后
09-17/09-18/09-22 与上一轮实测（47.1% / 56.2% / 35.4%）逐日吻合。

性能（重要）
-----------
`biz.asset_klines` 整表拉取会因跨区链路超时（实测 240s+ 未返回），故取价全部在
SQL 侧用 `LATERAL` 算好，只回传结果行；**不把 K 线明细拉到 Python**。

用法
----
    python collect_scan_outcome.py                     # 增量结算近 7 天未 resolved 的告警
    python collect_scan_outcome.py --days 30
    python collect_scan_outcome.py --backfill-from 2026-09-17   # 首次回填
    python collect_scan_outcome.py --dry-run --json    # 只算不写库
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg.rows  # noqa: E402

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

WINDOWS = (1, 4, 12, 24)   # 小时
COST = 0.1                 # 双边 taker 手续费（%），与 backtest_scan_scenarios.COST 同口径
# 费率覆盖容差：`biz.funding_rate_hist` 末端落后窗口末端在此以内即视为「已覆盖」。
# 取一个**最大常见结算间隔**（Binance U 本位 8h；4h/1h 品种更短），原因是窗口末端
# 一般不对齐结算点（如 alerted_at=00:30 的 24h 窗口末端 24:30，最后一个结算点是 24:00），
# 用严格比较会把正常行误判为 partial。容忍 8h 即对「数据滞后一天」这类真缺口仍有判别力。
COVER_TOL = timedelta(hours=8)
# 「费率未补齐则重算」的追溯天数（见 _RESOLVED_GUARD 注释）
FRESH_REDO_DAYS = 3

# 告警样本口径（与 scan_daemon 主池发信口径一致：main ∪ accumulation(BRK)）
CAND_SQL = """
WITH cand AS (
    SELECT sg.id AS signal_id, sg.symbol, sg.p_dir, sg.alerted_at, sg.pool, sg.scenario,
           sg.timeframe, sg.stop_loss_pct,
           (CASE WHEN EXISTS (
                     SELECT 1 FROM biz.asset_klines k
                      WHERE k.symbol = sg.symbol AND k.interval = '5m'
                        AND k.open_time <= sg.alerted_at
                        AND k.open_time > sg.alerted_at - INTERVAL '2 hours')
                 THEN '5m' ELSE '1h' END) AS iv
      FROM biz.scan_signal sg
      LEFT JOIN biz.scan_signal_outcome o ON o.signal_id = sg.id
     WHERE sg.alerted_at IS NOT NULL
       AND (sg.pool = 'main' OR (sg.pool = 'accumulation' AND sg.scenario = 'BRK'))
       AND sg.status <> 'invalid'
       AND sg.alerted_at >= %s
       __RESOLVED_GUARD__
)
SELECT c.signal_id, c.symbol, c.p_dir, c.alerted_at, c.pool, c.scenario, c.timeframe,
       c.stop_loss_pct, c.iv,
       b.open_time AS base_time, b.close_px AS base_px,
       w1.close_px  AS px_1h,  w4.close_px  AS px_4h,
       w12.close_px AS px_12h, w24.close_px AS px_24h,
       bb.close_px  AS btc_base_px,
       b1.close_px  AS btc_px_1h,  b4.close_px  AS btc_px_4h,
       b12.close_px AS btc_px_12h, b24.close_px AS btc_px_24h,
       agg.low_24h, agg.high_24h, agg.bars_n,
       fw.fn_1h,  fw.fs_1h,  fw.fn_4h,  fw.fs_4h,
       fw.fn_12h, fw.fs_12h, fw.fn_24h, fw.fs_24h,
       fb.fn_1h  AS bfn_1h,  fb.fs_1h  AS bfs_1h,
       fb.fn_4h  AS bfn_4h,  fb.fs_4h  AS bfs_4h,
       fb.fn_12h AS bfn_12h, fb.fs_12h AS bfs_12h,
       fb.fn_24h AS bfn_24h, fb.fs_24h AS bfs_24h,
       fc.f_max, fbc.bf_max
  FROM cand c
  LEFT JOIN LATERAL (
      SELECT k.open_time, k.close_px FROM biz.asset_klines k
       WHERE k.symbol = c.symbol AND k.interval = c.iv AND k.open_time <= c.alerted_at
       ORDER BY k.open_time DESC LIMIT 1
  ) b ON TRUE
  LEFT JOIN LATERAL (
      SELECT k.close_px FROM biz.asset_klines k
       WHERE k.symbol = c.symbol AND k.interval = c.iv
         AND k.open_time <= c.alerted_at + INTERVAL '1 hour'
       ORDER BY k.open_time DESC LIMIT 1
  ) w1 ON TRUE
  LEFT JOIN LATERAL (
      SELECT k.close_px FROM biz.asset_klines k
       WHERE k.symbol = c.symbol AND k.interval = c.iv
         AND k.open_time <= c.alerted_at + INTERVAL '4 hours'
       ORDER BY k.open_time DESC LIMIT 1
  ) w4 ON TRUE
  LEFT JOIN LATERAL (
      SELECT k.close_px FROM biz.asset_klines k
       WHERE k.symbol = c.symbol AND k.interval = c.iv
         AND k.open_time <= c.alerted_at + INTERVAL '12 hours'
       ORDER BY k.open_time DESC LIMIT 1
  ) w12 ON TRUE
  LEFT JOIN LATERAL (
      SELECT k.close_px FROM biz.asset_klines k
       WHERE k.symbol = c.symbol AND k.interval = c.iv
         AND k.open_time <= c.alerted_at + INTERVAL '24 hours'
       ORDER BY k.open_time DESC LIMIT 1
  ) w24 ON TRUE
  LEFT JOIN LATERAL (
      SELECT k.close_px FROM biz.asset_klines k
       WHERE k.symbol = 'BTCUSDT' AND k.interval = c.iv AND k.open_time <= c.alerted_at
       ORDER BY k.open_time DESC LIMIT 1
  ) bb ON TRUE
  LEFT JOIN LATERAL (
      SELECT k.close_px FROM biz.asset_klines k
       WHERE k.symbol = 'BTCUSDT' AND k.interval = c.iv
         AND k.open_time <= c.alerted_at + INTERVAL '1 hour'
       ORDER BY k.open_time DESC LIMIT 1
  ) b1 ON TRUE
  LEFT JOIN LATERAL (
      SELECT k.close_px FROM biz.asset_klines k
       WHERE k.symbol = 'BTCUSDT' AND k.interval = c.iv
         AND k.open_time <= c.alerted_at + INTERVAL '4 hours'
       ORDER BY k.open_time DESC LIMIT 1
  ) b4 ON TRUE
  LEFT JOIN LATERAL (
      SELECT k.close_px FROM biz.asset_klines k
       WHERE k.symbol = 'BTCUSDT' AND k.interval = c.iv
         AND k.open_time <= c.alerted_at + INTERVAL '12 hours'
       ORDER BY k.open_time DESC LIMIT 1
  ) b12 ON TRUE
  LEFT JOIN LATERAL (
      SELECT k.close_px FROM biz.asset_klines k
       WHERE k.symbol = 'BTCUSDT' AND k.interval = c.iv
         AND k.open_time <= c.alerted_at + INTERVAL '24 hours'
       ORDER BY k.open_time DESC LIMIT 1
  ) b24 ON TRUE
  LEFT JOIN LATERAL (
      SELECT MIN(k.low_px) AS low_24h, MAX(k.high_px) AS high_24h, COUNT(*) AS bars_n
        FROM biz.asset_klines k
       WHERE k.symbol = c.symbol AND k.interval = c.iv
         AND k.open_time > b.open_time
         AND k.open_time <= c.alerted_at + INTERVAL '24 hours'
  ) agg ON TRUE
  -- 区间资金费率（§12.1-A4）：左开右闭 (alerted_at, alerted_at+w]，按真实结算点求和。
  -- 一次扫描用 FILTER 出 4 个窗口；`count(f.rate)` 只数非空费率点（NULL 不计入）。
  LEFT JOIN LATERAL (
      SELECT count(f.rate) FILTER (WHERE f.funding_time <= c.alerted_at + INTERVAL '1 hour')   AS fn_1h,
             coalesce(sum(f.rate) FILTER (WHERE f.funding_time <= c.alerted_at + INTERVAL '1 hour'), 0)   AS fs_1h,
             count(f.rate) FILTER (WHERE f.funding_time <= c.alerted_at + INTERVAL '4 hours')  AS fn_4h,
             coalesce(sum(f.rate) FILTER (WHERE f.funding_time <= c.alerted_at + INTERVAL '4 hours'), 0)  AS fs_4h,
             count(f.rate) FILTER (WHERE f.funding_time <= c.alerted_at + INTERVAL '12 hours') AS fn_12h,
             coalesce(sum(f.rate) FILTER (WHERE f.funding_time <= c.alerted_at + INTERVAL '12 hours'), 0) AS fs_12h,
             count(f.rate) FILTER (WHERE f.funding_time <= c.alerted_at + INTERVAL '24 hours') AS fn_24h,
             coalesce(sum(f.rate) FILTER (WHERE f.funding_time <= c.alerted_at + INTERVAL '24 hours'), 0) AS fs_24h
        FROM biz.funding_rate_hist f
       WHERE f.symbol = c.symbol
         AND f.funding_time > c.alerted_at
         AND f.funding_time <= c.alerted_at + INTERVAL '24 hours'
  ) fw ON TRUE
  -- BTC beta 对照侧同口径（symbol='BTCUSDT'）
  LEFT JOIN LATERAL (
      SELECT count(f.rate) FILTER (WHERE f.funding_time <= c.alerted_at + INTERVAL '1 hour')   AS fn_1h,
             coalesce(sum(f.rate) FILTER (WHERE f.funding_time <= c.alerted_at + INTERVAL '1 hour'), 0)   AS fs_1h,
             count(f.rate) FILTER (WHERE f.funding_time <= c.alerted_at + INTERVAL '4 hours')  AS fn_4h,
             coalesce(sum(f.rate) FILTER (WHERE f.funding_time <= c.alerted_at + INTERVAL '4 hours'), 0)  AS fs_4h,
             count(f.rate) FILTER (WHERE f.funding_time <= c.alerted_at + INTERVAL '12 hours') AS fn_12h,
             coalesce(sum(f.rate) FILTER (WHERE f.funding_time <= c.alerted_at + INTERVAL '12 hours'), 0) AS fs_12h,
             count(f.rate) FILTER (WHERE f.funding_time <= c.alerted_at + INTERVAL '24 hours') AS fn_24h,
             coalesce(sum(f.rate) FILTER (WHERE f.funding_time <= c.alerted_at + INTERVAL '24 hours'), 0) AS fs_24h
        FROM biz.funding_rate_hist f
       WHERE f.symbol = 'BTCUSDT'
         AND f.funding_time > c.alerted_at
         AND f.funding_time <= c.alerted_at + INTERVAL '24 hours'
  ) fb ON TRUE
  -- 覆盖度：该 symbol 费率序列的末端（判「数据是否跟得上窗口末端」，见 COVER_TOL）
  LEFT JOIN LATERAL (
      SELECT max(f.funding_time) AS f_max FROM biz.funding_rate_hist f
       WHERE f.symbol = c.symbol
  ) fc ON TRUE
  LEFT JOIN LATERAL (
      SELECT max(f.funding_time) AS bf_max FROM biz.funding_rate_hist f
       WHERE f.symbol = 'BTCUSDT'
  ) fbc ON TRUE
 ORDER BY c.alerted_at
"""

OUT_COLS = (
    "signal_id", "symbol", "pool", "scenario", "timeframe", "p_dir", "alerted_at",
    "kline_iv", "base_time", "base_px",
    "aligned_ret_1h", "aligned_ret_4h", "aligned_ret_12h", "aligned_ret_24h",
    "btc_ret_1h", "btc_ret_4h", "btc_ret_12h", "btc_ret_24h",
    "excess_1h", "excess_4h", "excess_12h", "excess_24h",
    "mae_24h", "mfe_24h", "sl_hit_24h", "sl_pct_snapshot",
    "outcome_state", "last_window", "bars_n",
    # §12.1-A4 资金费率成本对照列（fix_084）
    "funding_pct_1h", "funding_pct_4h", "funding_pct_12h", "funding_pct_24h",
    "net_ret_1h", "net_ret_4h", "net_ret_12h", "net_ret_24h",
    "btc_net_ret_1h", "btc_net_ret_4h", "btc_net_ret_12h", "btc_net_ret_24h",
    "excess_net_1h", "excess_net_4h", "excess_net_12h", "excess_net_24h",
    "funding_src",
)

# 增量守卫：已 resolved 且**费率口径已完整**的行不再重算（省算力）。
# ⚠️ 例外（2026-10-02 §12.1-A4 新增）：费率回填（`scan_funding_backfill`）是**每日** 02:40
#   北京跑一次，而本结算是**每小时** ⇒ 在回填之前结算的行只看得到滞后 ≤24h 的费率序列，
#   `funding_src` 会落 `partial`/`none` 且费率成本**系统性低估**；而 resolved 行一旦被守卫
#   跳过就永久冻结在该低估值。故对近 `FRESH_REDO_DAYS` 天内、已 resolved 但
#   `funding_src <> 'ok'` 的行**继续重算**，等次日回填补齐后自动收敛为 `ok`（幂等）。
#   超出该窗口的旧行不再自动修补，由历史重算 `--force --backfill-from` 一次性补齐。
# 口径修复后需重算全部历史 → `--force` 直接摘掉整段守卫。
_RESOLVED_GUARD = (
    "AND (o.signal_id IS NULL OR o.outcome_state <> 'resolved'"
    " OR (o.funding_src IS DISTINCT FROM 'ok'"
    f" AND o.alerted_at >= NOW() - INTERVAL '{FRESH_REDO_DAYS} days'))"
)

# ⚠️ 守卫文本**只此一处定义**（原先是 CAND_SQL 里再抄一份字面量、靠 `.replace` 摘除，
#    两处一旦漂移 `--force` 会**静默失效**：实测 2026-10-02 首跑只捞到 102/829 行）。
CAND_SQL = CAND_SQL.replace("__RESOLVED_GUARD__", _RESOLVED_GUARD)


def cand_sql(force: bool = False) -> str:
    return CAND_SQL if not force else CAND_SQL.replace(_RESOLVED_GUARD, "")


# ──────────────────────────── 纯函数（可离线单测） ────────────────────────────

def aligned_ret(px0, px1, p_dir: str | None, cost: float = COST) -> float | None:
    """方向对齐净收益（%）：先按 `p_dir` 对齐毛收益，再扣 `cost`。缺价返回 None。

    ⚠️ 扣费必须**在对齐之后**（与 `backtest_scan_scenarios.scan_symbol` 同口径：
    `ret = ret_long if direction == "up" else -ret_long` 然后 `ret - cost`）。
    若写成 `(raw - cost)` 再取反，做空信号的 0.1% 手续费会被**加成收益**，
    空头系统性虚增 0.2pp —— 足以把接近 0 的期望判成正。
    """
    if px0 is None or px1 is None or p_dir not in ("up", "down"):
        return None
    p0 = float(px0)
    if p0 == 0:
        return None
    raw = (float(px1) - p0) / p0 * 100
    signed = raw if p_dir == "up" else -raw
    return signed - cost


def mae_mfe(base_px, low, high, p_dir: str | None) -> tuple[float | None, float | None]:
    """24h 窗口内方向对齐的最差/最好偏移（%）。

    最差偏移按惯例夹到 <=0（否则「未触及止损」判定会被正的最差值误判为已触及）。
    """
    if base_px is None or low is None or high is None or p_dir not in ("up", "down"):
        return None, None
    b = float(base_px)
    if b == 0:
        return None, None
    lo, hi = float(low), float(high)
    if p_dir == "up":
        worst, best = (lo - b) / b * 100, (hi - b) / b * 100
    else:
        worst, best = (b - hi) / b * 100, (b - lo) / b * 100
    return min(0.0, worst), best


def matured_windows(alerted_at: datetime, now: datetime) -> list[int]:
    """已到期窗口列表（`alerted_at + w <= now`）。"""
    return [w for w in WINDOWS if alerted_at + timedelta(hours=w) <= now]


def window_cover(f_max, alerted_at: datetime, w: int) -> str:
    """该 symbol 的费率序列对窗口 `(alerted_at, alerted_at + w]` 的覆盖度。

    * `none`：序列末端 `f_max` 早于/等于告警时刻 ⇒ 入场后一个结算点都没抓到；
    * `ok`  ：`f_max` 已达窗口末端 − `COVER_TOL`（正常取数跟得上）；
    * `partial`：介于两者之间（如序列只更新到窗口中途）。
    """
    if f_max is None or f_max <= alerted_at:
        return "none"
    return "ok" if f_max >= alerted_at + timedelta(hours=w) - COVER_TOL else "partial"


def funding_cost(sum_rate, n_pts, p_dir: str | None, cov: str) -> tuple[float | None, str]:
    """区间资金费率成本（%，已按方向对齐符号）。

    * `sum_rate`：区间内各结算点 `rate` 之和（**小数**，如 0.0001 = 1bp）；
    * `n_pts`   ：区间内非空结算点个数；
    * `cov`     ：`window_cover` 给出的覆盖度；
    返回 `(cost_pct | None, 该窗口实际状态)`，状态为 `ok`/`partial`/`none`。

    口径：多头（up）正费率**支付** ⇒ 成本 +Σ；空头（down）正费率**收取** ⇒ 成本 −Σ。
    `rate` 是小数而收益列是百分数 ⇒ 必 `× 100`。

    **缺数据不得记 0**：无覆盖（`none`）一律 None；有覆盖但区间内无结算点 ⇒ 合法 0.0
    （确无结算即确无费用）；覆盖到中途且无点 ⇒ 判不可知返回 None + `partial`。
    """
    if p_dir not in ("up", "down") or cov == "none":
        return None, "none"
    if int(n_pts or 0) == 0:
        return (0.0, "ok") if cov == "ok" else (None, "partial")
    sign = 1.0 if p_dir == "up" else -1.0
    return sign * float(sum_rate or 0.0) * 100.0, cov


def _worst_cover(states: list[str]) -> str | None:
    """行级 `funding_src` = 各已到期窗口状态里最差的那个（none > partial > ok）。"""
    if not states:
        return None
    if "none" in states:
        return "none"
    return "partial" if "partial" in states else "ok"


def settle_row(r: dict, now: datetime, cost: float = COST) -> dict:
    """把一行 SQL 结果结算成 `scan_signal_outcome` 的写入行。"""
    alerted_at = r["alerted_at"]
    p_dir = r["p_dir"]
    base_px = r["base_px"]
    avail = set(matured_windows(alerted_at, now))

    out: dict = {
        "signal_id": r["signal_id"], "symbol": r["symbol"], "pool": r["pool"],
        "scenario": r["scenario"], "timeframe": r["timeframe"], "p_dir": p_dir,
        "alerted_at": alerted_at, "base_time": r["base_time"], "base_px": base_px,
        "kline_iv": r.get("iv"),
        "sl_pct_snapshot": r["stop_loss_pct"],
        "bars_n": int(r["bars_n"]) if r["bars_n"] is not None else None,
    }

    if base_px is None or p_dir not in ("up", "down"):
        # 无基线 K 线，或方向未知（部分 BRK 行）→ 不参与统计
        for c in OUT_COLS:
            out.setdefault(c, None)
        out["outcome_state"] = "no_data"
        out["last_window"] = 0
        return out

    last_window = 0
    cover_states: list[str] = []
    for w in WINDOWS:
        if w not in avail:
            # 未到期 ⇒ 一列都不能写（含费率列），避免提前写入未来信息
            for c in (f"aligned_ret_{w}h", f"btc_ret_{w}h", f"excess_{w}h",
                      f"funding_pct_{w}h", f"net_ret_{w}h",
                      f"btc_net_ret_{w}h", f"excess_net_{w}h"):
                out[c] = None
            continue

        px = r.get(f"px_{w}h")
        val = aligned_ret(base_px, px, p_dir, cost)
        out[f"aligned_ret_{w}h"] = val
        btc = aligned_ret(r["btc_base_px"], r.get(f"btc_px_{w}h"), p_dir, cost)
        out[f"btc_ret_{w}h"] = btc
        out[f"excess_{w}h"] = (val - btc) if (val is not None and btc is not None) else None

        # §12.1-A4：区间资金费率成本（缺失一律 NULL，不记 0）
        fcost, fstate = funding_cost(r.get(f"fs_{w}h"), r.get(f"fn_{w}h"), p_dir,
                                     window_cover(r.get("f_max"), alerted_at, w))
        out[f"funding_pct_{w}h"] = fcost
        net = (val - fcost) if (val is not None and fcost is not None) else None
        out[f"net_ret_{w}h"] = net
        bcost, _ = funding_cost(r.get(f"bfs_{w}h"), r.get(f"bfn_{w}h"), p_dir,
                                window_cover(r.get("bf_max"), alerted_at, w))
        bnet = (btc - bcost) if (btc is not None and bcost is not None) else None
        out[f"btc_net_ret_{w}h"] = bnet
        out[f"excess_net_{w}h"] = (net - bnet) if (net is not None and bnet is not None) else None
        cover_states.append(fstate)

        if val is not None:
            last_window = w

    mae, mfe = mae_mfe(base_px, r["low_24h"], r["high_24h"], p_dir) if 24 in avail else (None, None)
    out["mae_24h"], out["mfe_24h"] = mae, mfe
    sl = r["stop_loss_pct"]
    out["sl_hit_24h"] = (mae <= -float(sl)) if (mae is not None and sl is not None) else None

    out["funding_src"] = _worst_cover(cover_states)
    out["last_window"] = last_window
    out["outcome_state"] = "resolved" if last_window >= 24 else "pending"
    return out


# ──────────────────────────── 落库 ────────────────────────────

def upsert(conn, rows: list[dict]) -> int:
    if not rows:
        return 0
    cols = list(OUT_COLS)
    placeholders = ",".join(["%s"] * len(cols))
    updates = ",".join(f"{c}=EXCLUDED.{c}" for c in cols if c != "signal_id")
    sql = (f"INSERT INTO biz.scan_signal_outcome ({','.join(cols)}, updated_at) "
           f"VALUES ({placeholders}, NOW()) "
           f"ON CONFLICT (signal_id) DO UPDATE SET {updates}, updated_at=NOW()")
    with conn.cursor() as cur:
        for r in rows:
            cur.execute(sql, tuple(r.get(c) for c in cols))
    conn.commit()
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="盘面告警逐信号多窗口结局结算")
    parser.add_argument("--days", type=int, default=7, help="回看天数（默认 7）")
    parser.add_argument("--backfill-from", type=str, default="",
                        help="回填起点 YYYY-MM-DD（覆盖 --days）")
    parser.add_argument("--cost", type=float, default=COST, help="双边手续费（%%，默认 0.1）")
    parser.add_argument("--force", action="store_true",
                        help="重算已 resolved 的行（口径修复后回填历史用）")
    parser.add_argument("--dry-run", action="store_true", help="只结算不写库")
    parser.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    args = parser.parse_args()

    if args.backfill_from:
        start = datetime.fromisoformat(args.backfill_from).replace(tzinfo=timezone.utc)
    else:
        start = datetime.now(timezone.utc) - timedelta(days=args.days)

    now = datetime.now(timezone.utc)
    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(cand_sql(args.force), (start,))
            raw = cur.fetchall()
        settled = [settle_row(r, now, args.cost) for r in raw]
        written = 0 if args.dry_run else upsert(conn, settled)

    n_resolved = sum(1 for r in settled if r["outcome_state"] == "resolved")
    n_pending = sum(1 for r in settled if r["outcome_state"] == "pending")
    n_no_data = sum(1 for r in settled if r["outcome_state"] == "no_data")
    by_day: dict[str, dict[int, list[float]]] = {}
    net_by_day: dict[str, dict[int, list[float]]] = {}
    for r in settled:
        day = r["alerted_at"].astimezone(timezone(timedelta(hours=8))).date().isoformat()
        for w in WINDOWS:
            v = r.get(f"aligned_ret_{w}h")
            if v is not None:
                by_day.setdefault(day, {}).setdefault(w, []).append(float(v))
            nv = r.get(f"net_ret_{w}h")
            if nv is not None:
                net_by_day.setdefault(day, {}).setdefault(w, []).append(float(nv))
    daily = {}
    for d, per_w in sorted(by_day.items()):
        daily[d] = {
            f"T+{w}h": {"n": len(per_w[w]),
                        "win": round(sum(1 for x in per_w[w] if x > 0) / len(per_w[w]), 4),
                        "avg": round(sum(per_w[w]) / len(per_w[w]), 4),
                        "avg_net": (round(sum(net_by_day[d][w]) / len(net_by_day[d][w]), 4)
                                    if net_by_day.get(d, {}).get(w) else None)}
            for w in WINDOWS if per_w.get(w)
        }

    src_cnt: dict[str, int] = {}
    for r in settled:
        k = r.get("funding_src") or "na"
        src_cnt[k] = src_cnt.get(k, 0) + 1

    summary = {
        "candidates": len(raw), "resolved": n_resolved, "pending": n_pending,
        "no_data": n_no_data, "written": written, "daily": daily,
        "funding_src": src_cnt,
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"[outcome] 候选 {len(raw)} 条 → resolved {n_resolved} / pending {n_pending} "
              f"/ no_data {n_no_data}｜写入 {written} 行")
        print(f"[outcome] 费率覆盖（§12.1-A4）：" +
              " ".join(f"{k}={v}" for k, v in sorted(src_cnt.items())))
        for d, per_w in daily.items():
            cells = "  ".join(f"T+{w}h {per_w[f'T+{w}h']['win']:.1%}/{per_w[f'T+{w}h']['avg']:+.2f}%"
                              f"(n={per_w[f'T+{w}h']['n']})" for w in WINDOWS if f"T+{w}h" in per_w)
            print(f"  {d}  {cells}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
