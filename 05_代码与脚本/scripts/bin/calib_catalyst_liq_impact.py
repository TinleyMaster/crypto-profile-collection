#!/usr/bin/env python3
"""催化剂影响程度 × 爆仓冲击校准（只读，事件研究法，z-score 口径）。

背景（审计 2026-10-02）：用 Coinglass 币种级爆仓历史验证 impact_strength 分档
是否对应真实强平冲击。探索结论（重要）：
  1. 窗口必须锚定**信号生成时刻 created_at**，不能用媒体 published_at——媒体发布
     滞后于市场反应，发布后窗口会得到"strong 爆仓反而更少"的假象。
  2. 用 **z-score**（窗口爆仓偏离该币自身 14 天基线）消除币种体量混杂。
  3. 实测（非 market_update，各档 12 条）：strong z中位 -2.18 > medium -4.36 >
     weak -6.86，**单调成立**——strong 事件后爆仓最活跃（衰减最慢），weak 最快枯竭。

口径：
- 样本：catalyst_signal（created_at 锚点）+ catalyst_impact 分档，非 market_update
- 窗口：created_at 起 WINDOW_HOURS 小时（默认 24h）内爆仓
- 基线：created_at 前 14 天（同 4h 粒度）爆仓序列
- 指标：窗口爆仓总和的 z-score（相对基线桶序列均值/标准差）
- 只读：不落库；CoinGlass 客户端令牌桶节流（Hobbyist 30/min，用 20/min 留余量）

用法：
    python calib_catalyst_liq_impact.py --days 90 --limit 300
    python calib_catalyst_liq_impact.py --days 90 --limit 60 --no-api   # 离线调试
"""
from __future__ import annotations

import argparse
import datetime
import statistics
import sys
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg
import psycopg.rows

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.clients.coinglass_client import CoinGlassClient  # noqa: E402

LOOKBACK_DAYS = 90
WINDOW_HOURS = 24
BASE_DAYS = 14            # z-score 基线窗口
EXCHANGES = ["Binance", "OKX", "Bybit"]
INTERVAL = "4h"
MIN_BASE_BUCKETS = 6      # 基线至少 6 个 4h 桶才可算 z


def get_conn():
    settings = get_settings(require_database=True)
    return psycopg.connect(
        settings.database_url,
        row_factory=psycopg.rows.dict_row,
        connect_timeout=30,
        keepalives=1,
        keepalives_idle=15,
        keepalives_interval=5,
        keepalives_count=3,
    )


def load_signals(conn, start: datetime.date, limit: int | None) -> list[dict]:
    """取有资产绑定 + 有 impact_strength 的信号样本（非 market_update）。"""
    sql = """
        SELECT s.signal_id, s.catalyst_id, s.asset_id,
               ci.impact_strength,
               a.canonical_symbol AS symbol,
               s.created_at,
               COALESCE(ac.ai_event_type, ac.rule_event_type, 'other') AS event_type
        FROM biz.catalyst_signal s
        JOIN core.asset a ON a.asset_id = s.asset_id
        JOIN biz.asset_catalyst ac ON ac.catalyst_id = s.catalyst_id
        JOIN biz.catalyst_impact ci
          ON ci.catalyst_id = s.catalyst_id AND ci.asset_id = s.asset_id
        WHERE ci.impact_strength IN ('strong','medium','weak')
          AND a.canonical_symbol IS NOT NULL
          AND COALESCE(ac.ai_event_type, ac.rule_event_type, 'other') <> 'market_update'
          AND s.created_at >= %s
          AND s.created_at <= NOW() - interval '24 hours'  -- 留窗口
        ORDER BY s.created_at DESC
    """
    params: list = [start]
    if limit:
        sql += " LIMIT %s"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def liq_zscore(client, symbol: str, ts: datetime.datetime,
               window_hours: int) -> float | None:
    """窗口（created_at 起 window_hours）爆仓 z-score，相对该币前 BASE_DAYS 基线。

    z = (窗口爆仓总和 - 基线桶数×基线桶均值) / (基线桶标准差 × sqrt(桶数))
    窗口内爆仓显著高于自身常态 → z>0；低于常态 → z<0。
    """
    win_end = ts + datetime.timedelta(hours=window_hours)
    base_end = ts
    base_start = ts - datetime.timedelta(days=BASE_DAYS)
    try:
        win_rows = client.liquidation_aggregated_history(
            EXCHANGES, symbol, interval=INTERVAL,
            start_time=int(ts.timestamp() * 1000),
            end_time=int(win_end.timestamp() * 1000), limit=1000)
        base_rows = client.liquidation_aggregated_history(
            EXCHANGES, symbol, interval=INTERVAL,
            start_time=int(base_start.timestamp() * 1000),
            end_time=int(base_end.timestamp() * 1000), limit=1080)
    except Exception as e:  # noqa: BLE001
        print(f"    [api_err] {symbol}: {type(e).__name__} {str(e)[:80]}")
        return None

    def _bucket_totals(rows):
        return [float(r.get("aggregated_long_liquidation_usd") or 0)
                + float(r.get("aggregated_short_liquidation_usd") or 0)
                for r in rows]

    base_vals = _bucket_totals(base_rows)
    if len(base_vals) < MIN_BASE_BUCKETS:
        return None
    mean = sum(base_vals) / len(base_vals)
    sd = statistics.pstdev(base_vals)
    if sd <= 0:
        return None
    win_tot = sum(_bucket_totals(win_rows))
    # 窗口内桶数按实际 4h 桶计（ceil(window_hours/4)）
    n_win_buckets = max(1, (window_hours + 3) // 4)
    z = (win_tot - n_win_buckets * mean) / (sd * (n_win_buckets ** 0.5))
    return z


def main() -> int:
    parser = argparse.ArgumentParser(description="催化剂影响程度 × 爆仓冲击校准（z-score）")
    parser.add_argument("--days", type=int, default=LOOKBACK_DAYS, help="回溯天数")
    parser.add_argument("--limit", type=int, default=None, help="最多处理 N 条信号")
    parser.add_argument("--window", type=int, default=WINDOW_HOURS, help="窗口小时数")
    parser.add_argument("--no-api", action="store_true", help="跳过 API（离线调试）")
    args = parser.parse_args()

    start = datetime.date.today() - datetime.timedelta(days=args.days)
    print(f"校准范围: {start} 起 | 窗口 {args.window}h | limit={args.limit or 'all'}")

    settings = get_settings(require_database=True)
    conn = get_conn()
    client = None
    if not args.no_api:
        # Hobbyist 30/min；用 20/min + 3s 间隔留余量，避免 429
        client = CoinGlassClient(settings.coinglass_api_key,
                                 base_url=settings.coinglass_base_url,
                                 min_request_gap=3.0,
                                 rate_per_min=20)

    try:
        signals = load_signals(conn, start, args.limit)
        print(f"信号样本: {len(signals)} 条")
        if not signals:
            return 0

        grp: dict[str, list[float]] = defaultdict(list)
        by_et: dict[str, list[float]] = defaultdict(list)
        done = 0
        for sig in signals:
            strength = sig["impact_strength"]
            if args.no_api:
                grp[strength].append(0.0)
                by_et[sig["event_type"]].append(0.0)
                done += 1
                continue
            z = liq_zscore(client, sig["symbol"], sig["created_at"], args.window)
            done += 1
            if done % 30 == 0:
                print(f"  进度 {done}/{len(signals)}")
            if z is None:
                continue
            grp[strength].append(z)
            by_et[sig["event_type"]].append(z)

        print("\n" + "=" * 62)
        print(f"一、按 impact_strength 分档的爆仓 z-score（{args.window}h 窗口 vs 14d 基线）")
        print("=" * 62)
        print(f"  {'档位':<10} {'样本':>5} {'z中位':>8} {'z均值':>8} {'爆仓活跃占比':>10}")
        for st in ("strong", "medium", "weak"):
            vals = grp.get(st, [])
            if not vals:
                print(f"  {st:<10} {0:>5}"); continue
            vals.sort()
            med = vals[len(vals)//2]
            avg = sum(vals)/len(vals)
            active = sum(1 for v in vals if v > 0)/len(vals)*100
            print(f"  {st:<10} {len(vals):>5} {med:>8.2f} {avg:>8.2f} {active:>9.1f}%")

        print("\n" + "=" * 62)
        print("二、按 event_type 分档的爆仓 z-score")
        print("=" * 62)
        print(f"  {'event_type':<14} {'样本':>5} {'z中位':>8} {'z均值':>8} {'爆仓活跃占比':>10}")
        for et in sorted(by_et, key=lambda k: -len(by_et[k])):
            vals = by_et[et]
            vals.sort()
            med = vals[len(vals)//2]
            avg = sum(vals)/len(vals)
            active = sum(1 for v in vals if v > 0)/len(vals)*100
            print(f"  {et:<14} {len(vals):>5} {med:>8.2f} {avg:>8.2f} {active:>9.1f}%")

        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
