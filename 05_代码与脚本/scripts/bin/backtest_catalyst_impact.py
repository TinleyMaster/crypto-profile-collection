#!/usr/bin/env python3
"""催化剂历史回测（P2+）：用历史发布时点做基线，从历史行情回看窗口收益 → catalyst_outcome。

为什么不等积累：K 线 2026-06-18 起、market_daily 2026-05-27 起，历史催化剂 8-9 月有
5608 条。用 asset_catalyst.published_at 做基线，直接查历史行情即可算出 4h/24h/72h/7d/14d，
无需等待窗口自然到期。一次回测即可把校准样本从百级扩到千级。

与 collect_catalyst_outcome.py 的区别：
  - collect：前向追踪（signal.created_at 基线，每日增量等窗口到期）
  - backtest：历史回放（published_at 基线，一次算完所有窗口）

设计原则：
  - 只处理 catalyst_outcome 中不存在的 (catalyst_id, asset_id)（ON CONFLICT DO NOTHING 兜底）
  - 已结算（outcome_state='resolved'）的不重复处理
  - signal_id 置 NULL 区分回测行；ret_source 标记 'backtest'
  - 复用 collect 的预加载/计算逻辑（K线/CMC/market_daily 全内存）

用法：
    python backtest_catalyst_impact.py              # 回测 06-18 后所有未回测组合
    python backtest_catalyst_impact.py --limit 100  # 只回测前 100 个
    python backtest_catalyst_impact.py --dry-run    # 预览不写入
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg
import psycopg.rows

from crypto_research.config import get_settings  # noqa: E402

# 复用 collect 的预加载与计算函数
from collect_catalyst_outcome import (
    get_conn,
    load_asset_symbols,
    load_kline_symbols,
    load_klines,
    load_cmc_pct,
    load_market_daily,
    kline_close_at,
    kline_high_low_between,
    cmc_pct_at,
    daily_price_at,
    daily_vol_ratio_at,
    resolve_direction,
    hit_of,
    KLINE_WINDOWS,
    DAILY_WINDOWS,
    RESOLVED_WINDOW,
)


def fetch_backtest_rows(conn, limit: int | None) -> list[dict]:
    """取未回测的 catalyst×asset 组合（published_at 基线）。

    只取：06-18 后发布（K线覆盖） + 尚无 outcome 记录 + 有关联资产。
    """
    sql = """
        SELECT ac.catalyst_id, cal.asset_id, ac.published_at AS base_time,
               COALESCE(ac.ai_event_type, ac.rule_event_type, 'other') AS event_type,
               ci.impact_direction
        FROM biz.asset_catalyst ac
        JOIN biz.catalyst_asset_link cal ON cal.catalyst_id = ac.catalyst_id
        LEFT JOIN biz.catalyst_impact ci
               ON ci.catalyst_id = ac.catalyst_id AND ci.asset_id = cal.asset_id
        LEFT JOIN biz.catalyst_outcome co
               ON co.catalyst_id = ac.catalyst_id AND co.asset_id = cal.asset_id
        WHERE ac.published_at >= '2026-05-27'  -- market_daily 起点，历史可回测最早时间
          AND co.outcome_id IS NULL
        ORDER BY ac.published_at ASC
    """
    params: list = []
    if limit:
        sql += " LIMIT %s"
        params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def upsert_backtest_outcome(conn, row: dict, computed: dict, direction, direction_src,
                            data_tier: str, base_price, base_time) -> None:
    """写入回测结果（ON CONFLICT DO NOTHING：不覆盖已有行）。"""
    last_window = computed.get("last_window", 0)
    state = "resolved" if last_window >= RESOLVED_WINDOW else "pending"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO biz.catalyst_outcome (
                catalyst_id, asset_id, signal_id, base_time, base_price_usd,
                ret_4h, ret_24h, ret_72h, ret_7d, ret_14d,
                excess_4h, excess_24h, excess_72h, excess_7d, excess_14d,
                hit_4h, hit_24h, hit_72h, hit_7d,
                vol_ratio_24h, vol_ratio_72h,
                max_drawdown_24h, max_gain_24h,
                impact_direction, direction_src, data_tier, outcome_state, last_window,
                ret_source, updated_at
            ) VALUES (
                %(catalyst_id)s, %(asset_id)s, NULL, %(base_time)s, %(base_price)s,
                %(ret_4h)s, %(ret_24h)s, %(ret_72h)s, %(ret_7d)s, %(ret_14d)s,
                %(excess_4h)s, %(excess_24h)s, %(excess_72h)s, %(excess_7d)s, %(excess_14d)s,
                %(hit_4h)s, %(hit_24h)s, %(hit_72h)s, %(hit_7d)s,
                %(vol_ratio_24h)s, %(vol_ratio_72h)s,
                %(max_drawdown_24h)s, %(max_gain_24h)s,
                %(impact_direction)s, %(direction_src)s, %(data_tier)s, %(state)s, %(last_window)s,
                'backtest', NOW()
            )
            ON CONFLICT (catalyst_id, asset_id) DO NOTHING
            """,
            {
                "catalyst_id": row["catalyst_id"],
                "asset_id": row["asset_id"],
                "base_time": base_time,
                "base_price": base_price,
                "ret_4h": computed.get("ret_4h"),
                "ret_24h": computed.get("ret_24h"),
                "ret_72h": computed.get("ret_72h"),
                "ret_7d": computed.get("ret_7d"),
                "ret_14d": computed.get("ret_14d"),
                "excess_4h": computed.get("excess_4h"),
                "excess_24h": computed.get("excess_24h"),
                "excess_72h": computed.get("excess_72h"),
                "excess_7d": computed.get("excess_7d"),
                "excess_14d": computed.get("excess_14d"),
                "hit_4h": computed.get("hit_4h"),
                "hit_24h": computed.get("hit_24h"),
                "hit_72h": computed.get("hit_72h"),
                "hit_7d": computed.get("hit_7d"),
                "vol_ratio_24h": computed.get("vol_ratio_24h"),
                "vol_ratio_72h": computed.get("vol_ratio_72h"),
                "max_drawdown_24h": computed.get("max_drawdown_24h"),
                "max_gain_24h": computed.get("max_gain_24h"),
                "impact_direction": direction,
                "direction_src": direction_src,
                "data_tier": data_tier,
                "state": state,
                "last_window": last_window,
            },
        )


def compute_one(row: dict, kline_symbols, klines, cmc_pct, daily,
                btc_asset_id, asset_symbols) -> tuple | None:
    """对单条回测组合做全窗口计算（历史数据全量可用，不依赖 now）。

    返回 (computed, data_tier, direction, direction_src, base_price) 或 None。
    """
    asset_id = row["asset_id"]
    base_time = row["base_time"]
    symbol = asset_symbols.get(asset_id)
    data_tier = "L1" if symbol and symbol in kline_symbols else "L2"
    direction, direction_src = resolve_direction(row["event_type"], row["impact_direction"])

    computed: dict = {}
    last_window = 0
    base_price = None

    # ---- L1：K 线精确（4h/24h/72h 全算，历史数据齐） ----
    if data_tier == "L1":
        sym_klines = klines.get(symbol, [])
        btc_klines = klines.get("BTCUSDT", [])
        base_price = kline_close_at(sym_klines, base_time)
        if base_price is None:
            return None
        btc_base = kline_close_at(btc_klines, base_time)
        for w in KLINE_WINDOWS:
            p_w = kline_close_at(sym_klines, base_time + timedelta(hours=w))
            if p_w is None:
                continue
            ret = (p_w / base_price - 1) * 100
            computed[f"ret_{w}h"] = round(ret, 4)
            if btc_base is not None:
                btc_p = kline_close_at(btc_klines, base_time + timedelta(hours=w))
                if btc_p is not None:
                    btc_ret = (btc_p / btc_base - 1) * 100
                    computed[f"excess_{w}h"] = round(ret - btc_ret, 4)
            excess = computed.get(f"excess_{w}h")
            hit = hit_of(excess, direction)
            if hit is not None:
                computed[f"hit_{w}h"] = hit
            last_window = max(last_window, w)
        hi, lo = kline_high_low_between(sym_klines, base_time,
                                        base_time + timedelta(hours=24))
        if hi is not None and base_price:
            computed["max_gain_24h"] = round((hi / base_price - 1) * 100, 4)
        if lo is not None and base_price:
            computed["max_drawdown_24h"] = round((lo / base_price - 1) * 100, 4)

    # ---- L2：CMC 24h 近似 ----
    if data_tier == "L2":
        ts24 = base_time + timedelta(hours=24)
        pct = cmc_pct_at(cmc_pct.get(asset_id, []), ts24)
        if pct is not None:
            computed["ret_24h"] = round(pct, 4)
            if btc_asset_id:
                btc_pct = cmc_pct_at(cmc_pct.get(btc_asset_id, []), ts24)
                if btc_pct is not None:
                    computed["excess_24h"] = round(pct - btc_pct, 4)
            hit = hit_of(computed.get("excess_24h"), direction)
            if hit is not None:
                computed["hit_24h"] = hit
            last_window = max(last_window, 24)

    # ---- 全部：日频 7d/14d ----
    base_date = base_time.date()
    asset_daily = daily.get(asset_id, [])
    btc_daily = daily.get(btc_asset_id, []) if btc_asset_id else []
    for w in DAILY_WINDOWS:
        target_date = (base_time + timedelta(hours=w)).date()
        p0 = daily_price_at(asset_daily, base_date)
        p1 = daily_price_at(asset_daily, target_date)
        if p0 is None or p1 is None:
            continue
        ret = (p1 / p0 - 1) * 100
        key = "7d" if w == 168 else "14d"
        computed[f"ret_{key}"] = round(ret, 4)
        if btc_daily:
            btc0 = daily_price_at(btc_daily, base_date)
            btc1 = daily_price_at(btc_daily, target_date)
            if btc0 and btc1:
                btc_ret = (btc1 / btc0 - 1) * 100
                computed[f"excess_{key}"] = round(ret - btc_ret, 4)
        if key == "7d":
            hit = hit_of(computed.get("excess_7d"), direction)
            if hit is not None:
                computed["hit_7d"] = hit
        last_window = max(last_window, w)

    # ---- 量比 24h ----
    if "ret_24h" in computed and computed.get("vol_ratio_24h") is None:
        vr = daily_vol_ratio_at(asset_daily, base_date)
        if vr is not None:
            computed["vol_ratio_24h"] = vr

    if not computed:
        return None

    computed["last_window"] = last_window
    return computed, data_tier, direction, direction_src, base_price


def main() -> int:
    parser = argparse.ArgumentParser(description="催化剂历史回测（P2+）")
    parser.add_argument("--limit", type=int, default=None, help="最多回测 N 个组合")
    parser.add_argument("--dry-run", action="store_true", help="预览不写入")
    args = parser.parse_args()

    conn = get_conn()
    try:
        asset_symbols = load_asset_symbols(conn)
        kline_symbols = load_kline_symbols(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT asset_id FROM core.asset WHERE canonical_symbol='BTC' AND status='active' LIMIT 1"
            )
            row = cur.fetchone()
            btc_asset_id = row["asset_id"] if row else None

        rows = fetch_backtest_rows(conn, args.limit)
        print(f"待回测组合: {len(rows)} 个")
        if not rows:
            print("无可回测组合（可能已全部回测）")
            return 0

        l1_symbols = {asset_symbols[r["asset_id"]] for r in rows
                      if asset_symbols.get(r["asset_id"]) in kline_symbols}
        l1_symbols.add("BTCUSDT")
        l2_assets = {r["asset_id"] for r in rows
                     if not (asset_symbols.get(r["asset_id"]) in kline_symbols)}
        all_assets = {r["asset_id"] for r in rows} | ({btc_asset_id} if btc_asset_id else set())

        print(f"  预加载 K线({len(l1_symbols)}), CMC({len(l2_assets)}), 日频({len(all_assets)})...")
        klines = load_klines(conn, l1_symbols)
        cmc_pct = load_cmc_pct(conn, l2_assets | ({btc_asset_id} if btc_asset_id else set()))
        daily = load_market_daily(conn, all_assets)
        print(f"  K线 {sum(len(v) for v in klines.values())} 行, CMC {sum(len(v) for v in cmc_pct.values())} 行, "
              f"日频 {sum(len(v) for v in daily.values())} 行")

        done = 0
        skipped = 0
        BATCH = 100
        for i, row in enumerate(rows, 1):
            for attempt in range(4):
                try:
                    res = compute_one(row, kline_symbols, klines, cmc_pct, daily,
                                      btc_asset_id, asset_symbols)
                    if res is None:
                        skipped += 1
                        break
                    computed, data_tier, direction, direction_src, base_price = res
                    if not args.dry_run:
                        upsert_backtest_outcome(conn, row, computed, direction, direction_src,
                                                data_tier, base_price, row["base_time"])
                        if done % BATCH == BATCH - 1:
                            conn.commit()
                    done += 1
                    break
                except psycopg.OperationalError as e:
                    if attempt >= 3 or args.dry_run:
                        print(f"  [失败] cat={row['catalyst_id']}: {e}")
                        break
                    print(f"  [重连] cat={row['catalyst_id']} (attempt {attempt+1})")
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = get_conn()
                except Exception as e:
                    print(f"  [失败] cat={row['catalyst_id']}: {e}")
                    break
            if i % 200 == 0:
                print(f"  进度: {i}/{len(rows)} (已回测 {done})")
        if not args.dry_run:
            conn.commit()

        print(f"完成: 处理 {len(rows)} 个组合，回测 {done} 个，跳过 {skipped} 个")
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
