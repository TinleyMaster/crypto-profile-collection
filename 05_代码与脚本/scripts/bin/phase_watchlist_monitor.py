"""
解锁追踪监控：定期检查追踪列表的价格跌幅与解锁到期，发送邮件提醒。

用法：
    python phase_watchlist_monitor.py              # 单次执行（适合 cron / n8n 定时调用）
    python phase_watchlist_monitor.py --loop 300   # 循环模式，每 300 秒执行一次

提醒规则：
    1. 解锁到期提醒：target_unlock_date 距今 <= UNLOCK_ALERT_DAYS 天，且未提醒过 → 发邮件
    2. 空头趋势提醒：最新价格相对 entry_price 跌幅 <= -TREND_DROP_PCT%，且未提醒过 → 发邮件

配置（环境变量）：
    SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / SMTP_TO / SMTP_FROM  邮件通知
    TREND_DROP_PCT   空头趋势跌幅阈值（默认 15，表示跌 15% 触发）
    UNLOCK_ALERT_DAYS 解锁提前提醒天数（默认 14）
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import time
from datetime import date
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

import psycopg
import psycopg.rows
import requests

from crypto_research.config import get_settings
from crypto_research.clients.notifier import (
    EmailNotifier,
    build_unlock_alert_html,
    build_trend_alert_html,
)

TREND_DROP_PCT = float(os.getenv("TREND_DROP_PCT", "15"))
UNLOCK_ALERT_DAYS = int(os.getenv("UNLOCK_ALERT_DAYS", "14"))


def _ensure_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS biz.unlock_watchlist (
                watch_id            SERIAL PRIMARY KEY,
                asset_id            INTEGER NOT NULL,
                symbol              TEXT,
                short_plan_note     TEXT,
                target_unlock_date  DATE,
                target_unlock_pct   NUMERIC(8,2),
                entry_price         NUMERIC(24,8),
                last_price          NUMERIC(24,8),
                last_price_at       TIMESTAMPTZ,
                unlock_alert_sent_at TIMESTAMPTZ,
                trend_alert_sent_at  TIMESTAMPTZ,
                created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT uq_watchlist_asset UNIQUE (asset_id),
                CONSTRAINT fk_watchlist_asset
                    FOREIGN KEY (asset_id) REFERENCES core.asset(asset_id)
                    ON DELETE CASCADE
            )
        """)


def _get_coin_id(conn, asset_id: int, symbol: str, settings) -> str | None:
    """从 asset_source_map 查 CG coin_id，无则按 symbol 搜索。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "SELECT source_asset_key FROM core.asset_source_map "
            "WHERE asset_id = %s AND source_code = 'cg'",
            (asset_id,),
        )
        row = cur.fetchone()
    if row:
        return row["source_asset_key"]

    if not symbol:
        return None
    try:
        search_url = f"{settings.coingecko_base_url}/search"
        headers = {"Accept": "application/json"}
        if settings.coingecko_api_key:
            headers["x-cg-demo-api-key"] = settings.coingecko_api_key
        resp = requests.get(search_url, params={"query": symbol.lower()},
                            headers=headers, timeout=10)
        resp.raise_for_status()
        coins = resp.json().get("coins", [])
        if coins:
            exact = [c for c in coins if c.get("symbol", "").lower() == symbol.lower()]
            return (exact[0] if exact else coins[0]).get("id")
    except Exception:
        pass
    return None


def _get_price(coin_id: str, settings) -> float | None:
    """从 CoinGecko 获取当前价格（带重试）。"""
    url = f"{settings.coingecko_base_url}/simple/price"
    params = {"ids": coin_id, "vs_currencies": "usd"}
    headers = {"Accept": "application/json"}
    if settings.coingecko_api_key:
        headers["x-cg-demo-api-key"] = settings.coingecko_api_key
    last_err = None
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            resp.raise_for_status()
            return resp.json().get(coin_id, {}).get("usd")
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(1 * (attempt + 1))
    print(f"  [WARN] 价格获取失败 {coin_id}: {last_err}")
    return None


def run_once(settings) -> None:
    notifier = EmailNotifier(settings)
    today = date.today()

    with psycopg.connect(settings.database_url) as conn:
        _ensure_table(conn)
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("""
                SELECT w.watch_id, w.asset_id, w.symbol, w.target_unlock_date,
                       w.target_unlock_pct, w.entry_price, w.unlock_alert_sent_at,
                       w.trend_alert_sent_at, a.canonical_name AS name
                FROM biz.unlock_watchlist w
                JOIN core.asset a ON a.asset_id = w.asset_id
            """)
            items = cur.fetchall()

        if not items:
            print("追踪列表为空，无需监控。")
            return

        for it in items:
            watch_id = it["watch_id"]
            asset_id = it["asset_id"]
            symbol = it["symbol"]
            name = it["name"] or symbol

            coin_id = _get_coin_id(conn, asset_id, symbol, settings)
            price = _get_price(coin_id, settings) if coin_id else None

            entry = it["entry_price"]

            # 更新 last_price
            if price is not None:
                with conn.cursor() as ucur:
                    ucur.execute(
                        "UPDATE biz.unlock_watchlist SET last_price = %s, last_price_at = NOW(), updated_at = NOW() WHERE watch_id = %s",
                        (price, watch_id),
                    )

            # 无 entry_price → 以当前价格初始化基准（不触发趋势提醒）
            if entry is None and price is not None:
                with conn.cursor() as ucur:
                    ucur.execute(
                        "UPDATE biz.unlock_watchlist SET entry_price = %s, updated_at = NOW() WHERE watch_id = %s",
                        (price, watch_id),
                    )
                entry = price

            # 1. 解锁到期提醒
            ud = it["target_unlock_date"]
            if ud and it["unlock_alert_sent_at"] is None:
                days_left = (ud - today).days
                if 0 <= days_left <= UNLOCK_ALERT_DAYS:
                    ok, msg = notifier.send(
                        f"🔓 {symbol} 将于 {days_left} 天后解锁",
                        build_unlock_alert_html(
                            symbol, name, str(ud),
                            float(it["target_unlock_pct"]) if it["target_unlock_pct"] is not None else None,
                            days_left,
                        ),
                    )
                    print(f"  [{symbol}] 解锁提醒 {days_left} 天: {msg}")
                    if ok:
                        with conn.cursor() as ucur:
                            ucur.execute(
                                "UPDATE biz.unlock_watchlist SET unlock_alert_sent_at = NOW(), updated_at = NOW() WHERE watch_id = %s",
                                (watch_id,),
                            )

            # 2. 空头趋势提醒
            if price is not None and entry is not None and it["trend_alert_sent_at"] is None:
                try:
                    change_pct = (float(price) - float(entry)) / float(entry) * 100
                except ZeroDivisionError:
                    change_pct = 0.0
                if change_pct <= -TREND_DROP_PCT:
                    ok, msg = notifier.send(
                        f"📉 {symbol} 跌幅达 {change_pct:.2f}%，空头趋势形成",
                        build_trend_alert_html(symbol, name, float(entry), float(price), change_pct),
                    )
                    print(f"  [{symbol}] 空头趋势 {change_pct:.2f}%: {msg}")
                    if ok:
                        with conn.cursor() as ucur:
                            ucur.execute(
                                "UPDATE biz.unlock_watchlist SET trend_alert_sent_at = NOW(), updated_at = NOW() WHERE watch_id = %s",
                                (watch_id,),
                            )

        conn.commit()

    print(f"监控完成：{len(items)} 个追踪项。")


def main() -> int:
    parser = argparse.ArgumentParser(description="解锁追踪监控")
    parser.add_argument("--loop", type=int, default=0, help="循环间隔（秒），0 表示只执行一次")
    args = parser.parse_args()

    settings = get_settings(require_database=True)

    if args.loop > 0:
        print(f"循环模式：每 {args.loop} 秒执行一次。Ctrl+C 退出。")
        while True:
            try:
                run_once(settings)
            except Exception as e:
                print(f"[ERROR] 监控执行失败: {e}")
            time.sleep(args.loop)
    else:
        run_once(settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
