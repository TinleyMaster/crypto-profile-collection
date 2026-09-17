#!/usr/bin/env python3
"""盘面异动扫描 · 事件预置层：领先型事件 → biz.event_watchlist。

事件先行于盘面的信息（评审/§时序讨论结论）：
  - 解锁：`unlock_watchlist.target_unlock_date` 在未来 N 天内的币（可预知，提前埋伏）
  - 链上大额转账：`onchain_transfer_log` 近 7 天单笔 ≥ 阈值的币（转账常领先于砸盘）
  - 催化剂：当前无"排期"字段（asset_catalyst 仅有已发布事件），不作为预置源，
    仅作为盘面触发时的共振确认（见 scan_alert_monitor.py）

用法：
    python phase_build_event_watchlist.py                 # 重建预置名单
    python phase_build_event_watchlist.py --dry-run       # 打印不落库
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg.rows  # noqa: E402

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

UNLOCK_DAYS_AHEAD = 14        # 解锁预置：未来 N 天
TRANSFER_LOOKBACK_DAYS = 7    # 链上转账预置：近 N 天
TRANSFER_MIN_USD = 1_000_000  # 单笔转账阈值（美元）
TRANSFER_AGG_MIN_USD = 10_000_000  # 7 天聚合阈值（美元），过滤零散噪音
# 转账流噪音：稳定币 / 封装资产 / 流动性质押 / 跨链桥（非"巨鲸动向"）
NOISE_SYMBOLS = {
    # 稳定币
    "USDT", "USDC", "USDS", "DAI", "FDUSD", "RLUSD", "CRVUSD", "USDE", "USDD",
    "TUSD", "PYUSD", "GUSD", "FRAX", "LUSD", "BUSD", "USDP", "EURS", "CEUR",
    "USTC", "USD1", "USDL", "USDG", "GHO", "USDY", "USDF",
    # 封装/桥接/质押
    "WETH", "WBTC", "VBTC", "CBTC", "WBETH", "WSTETH", "STETH", "RETH", "SETH",
    "CBBTC", "SBTC", "BBTC", "WBNB", "VBNB", "WXDAI", "WGLMR", "WBTRST",
    "STBTC", "SOLVBTC", "LBTC", "SWBTC", "BITCOIN",
    "FRXETH", "SFRXETH", "EZETH", "RSETH", "WEETH", "PUFETH", "ETHX", "OSETH", "EETH",
    "USDGO", "SYRUPUSDT", "SYRUPUSDC",
    # 跨链桥 / 封装协议代币
    "AXL", "WORMHOLE", "LAYERZERO", "OFTCORE",
}

UPSERT_SQL = """
    INSERT INTO biz.event_watchlist
        (asset_id, symbol, event_type, event_date, event_pct, detail, source_ref, updated_at)
    VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())
    ON CONFLICT (symbol, event_type) DO UPDATE SET
        asset_id=EXCLUDED.asset_id, event_date=EXCLUDED.event_date,
        event_pct=EXCLUDED.event_pct, detail=EXCLUDED.detail,
        source_ref=EXCLUDED.source_ref, updated_at=NOW()
"""


def build_unlock(conn, today: date) -> list[dict]:
    """解锁预置：未来 UNLOCK_DAYS_AHEAD 天内到期的 unlock_watchlist。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT w.asset_id, w.symbol, w.target_unlock_date, w.target_unlock_pct
            FROM biz.unlock_watchlist w
            WHERE w.target_unlock_date BETWEEN %s AND %s
              AND w.symbol IS NOT NULL
            ORDER BY w.target_unlock_date
            """,
            (today, today + timedelta(days=UNLOCK_DAYS_AHEAD)),
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        out.append({
            "asset_id": r["asset_id"],
            "symbol": r["symbol"].upper(),
            "event_type": "unlock",
            "event_date": r["target_unlock_date"],
            "event_pct": float(r["target_unlock_pct"]) if r["target_unlock_pct"] is not None else None,
            "detail": f"解锁 {r['target_unlock_date']}（{(r['target_unlock_date'] - today).days} 天后）",
            "source_ref": None,
        })
    return out


def build_transfers(conn, since: date) -> list[dict]:
    """链上大额转账预置：近 7 天单笔 ≥ 阈值的资产（按 asset 聚合，保留最大一笔明细）。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT t.asset_id, a.canonical_symbol AS symbol,
                   COUNT(*) AS n_tx,
                   SUM(t.value_usd) AS total_usd,
                   MAX(t.value_usd) AS max_usd,
                   MAX(t.chain) AS chain,
                   MAX(t.from_label) AS from_label,
                   MAX(t.to_label) AS to_label
            FROM biz.onchain_transfer_log t
            JOIN core.asset a ON a.asset_id = t.asset_id
            WHERE t.block_timestamp >= %s
              AND t.value_usd >= %s
              AND t.is_suspect IS NOT TRUE
              AND a.canonical_symbol IS NOT NULL
              AND UPPER(a.canonical_symbol) <> ALL(%s)
            GROUP BY t.asset_id, a.canonical_symbol
            HAVING SUM(t.value_usd) >= %s
            ORDER BY total_usd DESC
            """,
            (since.isoformat(), TRANSFER_MIN_USD, sorted(NOISE_SYMBOLS), TRANSFER_AGG_MIN_USD),
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        total_usd = float(r["total_usd"])
        max_usd = float(r["max_usd"])
        n_tx = int(r["n_tx"])
        detail = (f"近{TRANSFER_LOOKBACK_DAYS}天大额转账 {n_tx} 笔 / "
                  f"合计 ${total_usd / 1e6:.1f}M / 最大单笔 ${max_usd / 1e6:.1f}M"
                  f"（{r['chain']}，{r['from_label'] or '?'}→{r['to_label'] or '?'}）")
        out.append({
            "asset_id": r["asset_id"],
            "symbol": r["symbol"].upper(),
            "event_type": "onchain_transfer",
            "event_date": None,
            "event_pct": None,
            "detail": detail,
            "source_ref": {"n_tx": n_tx, "total_usd": total_usd,
                           "max_usd": max_usd, "chain": r["chain"]},
        })
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="事件预置层：解锁/链上转账 → event_watchlist")
    parser.add_argument("--dry-run", action="store_true", help="只打印不落库")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    today = date.today()
    since = today - timedelta(days=TRANSFER_LOOKBACK_DAYS)
    with get_connection(settings.database_url) as conn:
        unlocks = build_unlock(conn, today)
        transfers = build_transfers(conn, since)
        all_rows = unlocks + transfers
        print(f"[event-watchlist] 解锁预置 {len(unlocks)}，链上转账预置 {len(transfers)}，共 {len(all_rows)}")
        if args.dry_run:
            for r in all_rows[:15]:
                print(f"  {r['event_type']:<16} {r['symbol']:<14} {r['detail']}")
            return 0
        if not all_rows:
            return 0
        params = [(r["asset_id"], r["symbol"], r["event_type"], r["event_date"],
                   r["event_pct"], r["detail"],
                   # source_ref 是 JSONB 列，dict 需序列化为 JSON 字符串，
                   # 否则 psycopg AUTO 格式无法适配（cannot adapt type 'dict'）
                   json.dumps(r["source_ref"], ensure_ascii=False)
                   if r["source_ref"] is not None else None)
                  for r in all_rows]
        with conn.cursor() as cur:
            cur.executemany(UPSERT_SQL, params)
        print(f"[db] upsert {len(params)} 条事件预置")
    return 0


if __name__ == "__main__":
    sys.exit(main())
