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
       AND (o.signal_id IS NULL OR o.outcome_state <> 'resolved')
)
SELECT c.signal_id, c.symbol, c.p_dir, c.alerted_at, c.pool, c.scenario, c.timeframe,
       c.stop_loss_pct, c.iv,
       b.open_time AS base_time, b.close_px AS base_px,
       w1.close_px  AS px_1h,  w4.close_px  AS px_4h,
       w12.close_px AS px_12h, w24.close_px AS px_24h,
       bb.close_px  AS btc_base_px,
       b1.close_px  AS btc_px_1h,  b4.close_px  AS btc_px_4h,
       b12.close_px AS btc_px_12h, b24.close_px AS btc_px_24h,
       agg.low_24h, agg.high_24h, agg.bars_n
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
)

# 增量守卫：已 resolved 的行不再重算（省算力）。口径修复后需重算历史 → `--force` 摘掉它。
_RESOLVED_GUARD = "AND (o.signal_id IS NULL OR o.outcome_state <> 'resolved')"


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
    for w in WINDOWS:
        px = r.get(f"px_{w}h")
        val = aligned_ret(base_px, px, p_dir, cost) if w in avail else None
        out[f"aligned_ret_{w}h"] = val
        btc = (aligned_ret(r["btc_base_px"], r.get(f"btc_px_{w}h"), p_dir, cost)
               if w in avail else None)
        out[f"btc_ret_{w}h"] = btc
        out[f"excess_{w}h"] = (val - btc) if (val is not None and btc is not None) else None
        if val is not None:
            last_window = w

    mae, mfe = mae_mfe(base_px, r["low_24h"], r["high_24h"], p_dir) if 24 in avail else (None, None)
    out["mae_24h"], out["mfe_24h"] = mae, mfe
    sl = r["stop_loss_pct"]
    out["sl_hit_24h"] = (mae <= -float(sl)) if (mae is not None and sl is not None) else None

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
    for r in settled:
        day = r["alerted_at"].astimezone(timezone(timedelta(hours=8))).date().isoformat()
        for w in WINDOWS:
            v = r.get(f"aligned_ret_{w}h")
            if v is not None:
                by_day.setdefault(day, {}).setdefault(w, []).append(float(v))
    daily = {}
    for d, per_w in sorted(by_day.items()):
        daily[d] = {
            f"T+{w}h": {"n": len(per_w[w]),
                        "win": round(sum(1 for x in per_w[w] if x > 0) / len(per_w[w]), 4),
                        "avg": round(sum(per_w[w]) / len(per_w[w]), 4)}
            for w in WINDOWS if per_w.get(w)
        }

    summary = {
        "candidates": len(raw), "resolved": n_resolved, "pending": n_pending,
        "no_data": n_no_data, "written": written, "daily": daily,
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"[outcome] 候选 {len(raw)} 条 → resolved {n_resolved} / pending {n_pending} "
              f"/ no_data {n_no_data}｜写入 {written} 行")
        for d, per_w in daily.items():
            cells = "  ".join(f"T+{w}h {per_w[f'T+{w}h']['win']:.1%}/{per_w[f'T+{w}h']['avg']:+.2f}%"
                              f"(n={per_w[f'T+{w}h']['n']})" for w in WINDOWS if f"T+{w}h" in per_w)
            print(f"  {d}  {cells}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
