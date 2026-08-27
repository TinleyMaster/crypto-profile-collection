"""
KOL 信号回测批量脚本。

对 biz.kol_signal 中 backtest_done = FALSE 的 prediction 类信号，
用 biz.asset_market_daily 日频行情做简化回测：
  - 入场价：信号发布当日收盘价（或 entry_price，优先 entry_price）
  - 止损：stop_loss（若缺失则按方向默认 -10%）
  - 止盈：take_profit（若缺失则按方向默认 +20%）
  - 持仓窗口：30 天
  - 结果：先触发止损/止盈则记为对应命中；都没触发则按期末价计算 PnL

设计原则：
  - 只回测有 asset_id + 有行情数据的 prediction 信号
  - 回测结果写回 kol_signal.backtest_* 字段
  - 幂等：已回测（backtest_done=TRUE）的跳过

用法：
    python kol_backtest_batch.py                # 回测所有未回测信号
    python kol_backtest_batch.py --limit 100    # 最多回测 100 条
    python kol_backtest_batch.py --dry-run      # 预览，不写入
"""
from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import psycopg
import psycopg.rows

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection


# 默认止损/止盈比例（无明确价位时使用）
DEFAULT_STOP_LOSS_PCT = Decimal("0.10")   # 10%
DEFAULT_TAKE_PROFIT_PCT = Decimal("0.20")  # 20%
HOLDING_WINDOW_DAYS = 30


def fetch_pending_signals(conn, limit: int) -> list[dict]:
    """获取待回测的 prediction 信号。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT s.signal_id, s.post_id, s.profile_id, s.asset_id,
                   s.direction, s.entry_price, s.stop_loss, s.take_profit,
                   p.posted_at
            FROM biz.kol_signal s
            JOIN biz.kol_post p ON p.post_id = s.post_id
            WHERE s.post_type = 'prediction'
              AND s.backtest_done = FALSE
              AND s.asset_id IS NOT NULL
              AND s.direction IN ('long', 'short')
            ORDER BY p.posted_at ASC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def get_price_on_date(conn, asset_id: int, target_date) -> Decimal | None:
    """获取某资产在指定日期的收盘价（CMC 源优先）。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT price_usd
            FROM biz.asset_market_daily
            WHERE asset_id = %s
              AND source_code = 'cmc'
              AND market_date = %s::DATE
              AND price_usd IS NOT NULL
            LIMIT 1
            """,
            (asset_id, target_date),
        )
        row = cur.fetchone()
        return row[0] if row else None


def get_first_price_after(conn, asset_id: int, start_date) -> tuple | None:
    """获取 start_date 之后（含）第一个有价格的日期和价格。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT market_date, price_usd
            FROM biz.asset_market_daily
            WHERE asset_id = %s
              AND source_code = 'cmc'
              AND market_date >= %s::DATE
              AND price_usd IS NOT NULL
            ORDER BY market_date ASC
            LIMIT 1
            """,
            (asset_id, start_date),
        )
        return cur.fetchone()


def get_price_series(conn, asset_id: int, start_date, end_date) -> list[dict]:
    """获取 [start_date, end_date] 区间的日频价格序列（按日期升序）。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT market_date, price_usd
            FROM biz.asset_market_daily
            WHERE asset_id = %s
              AND source_code = 'cmc'
              AND market_date BETWEEN %s::DATE AND %s::DATE
              AND price_usd IS NOT NULL
            ORDER BY market_date ASC
            """,
            (asset_id, start_date, end_date),
        )
        return cur.fetchall()


def backtest_signal(conn, signal: dict) -> dict:
    """
    对单条信号做回测，返回结果 dict。

    返回字段：
      - done: bool        是否成功完成回测
      - entry_price: Decimal
      - stop_loss: Decimal
      - take_profit: Decimal
      - hit_type: str     'stop_loss' / 'take_profit' / 'expired'
      - hit_date: date
      - pnl_pct: Decimal  盈亏比例（正数=盈利，负数=亏损）
      - reason: str       失败原因（done=False 时）
    """
    asset_id = signal["asset_id"]
    direction = signal["direction"]
    posted_at = signal["posted_at"]
    if not posted_at:
        return {"done": False, "reason": "no posted_at"}

    post_date = posted_at.date()

    # 入场价：优先 entry_price，否则用发帖后第一个收盘价
    entry_price = signal["entry_price"]
    if entry_price is None or entry_price <= 0:
        first = get_first_price_after(conn, asset_id, post_date)
        if not first:
            return {"done": False, "reason": "no entry price data"}
        entry_price = first["price_usd"]
        entry_date = first["market_date"]
    else:
        entry_date = post_date

    # 止损/止盈：优先信号给出，否则按默认比例
    if signal["stop_loss"] and signal["stop_loss"] > 0:
        stop_loss = signal["stop_loss"]
    else:
        if direction == "long":
            stop_loss = entry_price * (Decimal("1") - DEFAULT_STOP_LOSS_PCT)
        else:
            stop_loss = entry_price * (Decimal("1") + DEFAULT_STOP_LOSS_PCT)

    if signal["take_profit"] and signal["take_profit"] > 0:
        take_profit = signal["take_profit"]
    else:
        if direction == "long":
            take_profit = entry_price * (Decimal("1") + DEFAULT_TAKE_PROFIT_PCT)
        else:
            take_profit = entry_price * (Decimal("1") - DEFAULT_TAKE_PROFIT_PCT)

    # 持仓窗口
    end_date = entry_date + timedelta(days=HOLDING_WINDOW_DAYS)

    # 拉取价格序列
    prices = get_price_series(conn, asset_id, entry_date, end_date)
    if not prices:
        return {"done": False, "reason": "no price data in window"}

    # 逐日判断（跳过入场当天，从第二天开始）
    hit_type = "expired"
    hit_date = prices[-1]["market_date"]
    final_price = prices[-1]["price_usd"]

    for p in prices[1:]:  # 从第二天开始
        price = p["price_usd"]
        if direction == "long":
            if price <= stop_loss:
                hit_type = "stop_loss"
                hit_date = p["market_date"]
                final_price = stop_loss
                break
            if price >= take_profit:
                hit_type = "take_profit"
                hit_date = p["market_date"]
                final_price = take_profit
                break
        else:  # short
            if price >= stop_loss:
                hit_type = "stop_loss"
                hit_date = p["market_date"]
                final_price = stop_loss
                break
            if price <= take_profit:
                hit_type = "take_profit"
                hit_date = p["market_date"]
                final_price = take_profit
                break

    # 计算盈亏比例
    if direction == "long":
        pnl_pct = (final_price - entry_price) / entry_price * Decimal("100")
    else:
        pnl_pct = (entry_price - final_price) / entry_price * Decimal("100")

    return {
        "done": True,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "hit_type": hit_type,
        "hit_date": hit_date,
        "pnl_pct": pnl_pct,
    }


def save_backtest_result(conn, signal_id: int, result: dict) -> None:
    """写回回测结果。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE biz.kol_signal
            SET backtest_pnl = %s,
                backtest_hit_stop_loss = %s,
                backtest_hit_take_profit = %s,
                backtest_hitted_at = %s,
                backtest_done = TRUE,
                updated_at = NOW()
            WHERE signal_id = %s
            """,
            (
                float(result["pnl_pct"]),
                result["hit_type"] == "stop_loss",
                result["hit_type"] == "take_profit",
                result["hit_date"],
                signal_id,
            ),
        )


def update_profile_win_rate(conn, profile_id: int) -> None:
    """更新博主的累计胜率和信号数。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE biz.kol_profile
            SET win_rate = (
                SELECT ROUND(
                    COUNT(*) FILTER (WHERE backtest_pnl > 0)::NUMERIC
                    / NULLIF(COUNT(*) FILTER (WHERE backtest_done), 0)::NUMERIC
                    * 100, 2
                )
                FROM biz.kol_signal
                WHERE profile_id = %s AND backtest_done
            ),
            total_signals = (
                SELECT COUNT(*) FROM biz.kol_signal WHERE profile_id = %s AND post_type = 'prediction'
            ),
            updated_at = NOW()
            WHERE profile_id = %s
            """,
            (profile_id, profile_id, profile_id),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="KOL 信号回测批量脚本")
    parser.add_argument("--limit", type=int, default=0, help="最多回测条数（0=不限）")
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不写入数据库")
    args = parser.parse_args()

    settings = get_settings(require_database=True)

    print("=" * 60)
    print("KOL 信号回测")
    print("=" * 60)

    with get_connection(settings.database_url) as conn:
        signals = fetch_pending_signals(conn, args.limit if args.limit > 0 else 100000)
        if not signals:
            print("无待回测信号")
            return 0

        print(f"待回测: {len(signals)} 条")
        if args.dry_run:
            print("[dry-run] 仅预览，不写入")

        done = 0
        skipped = 0
        win_count = 0
        loss_count = 0
        expired_count = 0
        profile_ids = set()

        for i, sig in enumerate(signals, 1):
            result = backtest_signal(conn, sig)
            if not result["done"]:
                skipped += 1
                print(f"  [{i}/{len(signals)}] signal_id={sig['signal_id']} SKIP ({result['reason']})")
                continue

            if not args.dry_run:
                save_backtest_result(conn, sig["signal_id"], result)
                conn.commit()

            done += 1
            profile_ids.add(sig["profile_id"])

            hit_label = {
                "take_profit": "TP ✅",
                "stop_loss": "SL ❌",
                "expired": "到期",
            }.get(result["hit_type"], result["hit_type"])

            if result["hit_type"] == "take_profit":
                win_count += 1
            elif result["hit_type"] == "stop_loss":
                loss_count += 1
            else:
                expired_count += 1

            print(
                f"  [{i}/{len(signals)}] signal_id={sig['signal_id']} "
                f"{sig['direction']:>5} PnL={float(result['pnl_pct']):+.2f}% "
                f"[{hit_label}]"
            )

        # 更新博主胜率
        if not args.dry_run and profile_ids:
            for pid in profile_ids:
                update_profile_win_rate(conn, pid)
            conn.commit()

        total = done + skipped
        print("\n" + "=" * 60)
        print(f"完成：成功 {done}, 跳过 {skipped} / 总计 {total}")
        if done > 0:
            print(f"  止盈: {win_count} ({win_count/done*100:.1f}%)")
            print(f"  止损: {loss_count} ({loss_count/done*100:.1f}%)")
            print(f"  到期: {expired_count} ({expired_count/done*100:.1f}%)")
        print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
