"""机会评分回测框架（OBI-OPT-BACKTEST-001）。

基于 market_overview_snapshot 快照历史，回测各 signal_type 的命中率与 alpha。
输入：快照 JSONB（含 opportunity_list.opportunities）。
输出：signal_type 级别命中率 / alpha / 门控状态。

用法：
    python backtest_opportunities.py                # 回测最近 30 天
    python backtest_opportunities.py --days 7       # 回测最近 7 天
    python backtest_opportunities.py --list-snapshots  # 列出可用快照
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

# ── 路径兼容 ──
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection


# ── 常量 ──

# 各 signal_type 的持有期（天）：T+X 平仓
SIGNAL_HORIZONS: dict[str, list[int]] = {
    "mvrv_deep_under":  [7, 14, 30],
    "mvrv_under_watch": [7, 14],
    "btc_left_accum":   [1, 7],
    "cm_adoption_divergence": [1, 7],
    "catalyst":         [7, 14, 30],
    "whale_flow":       [1, 7],
    "github_activity":  [1, 7],
    "funding":          [1, 7],
    "token_unlock":     [0, 1, 3],
    "kol_onchain":      [1, 7],
    "fng_extreme":      [1, 7],
    "leverage_extreme": [1, 7],
    "stablecoin_inflow": [1, 7],
}

# 样本量门控
MIN_SAMPLES = 30

# NOT_CALIBRABLE：无法靠回测校准的轴
NOT_CALIBRABLE = {"mvrv_deep_under"}


# ── 价格取数（三段式）──

def _resolve_symbol_to_asset_id(cur, symbol: str) -> int | None:
    """symbol → asset_id 解析（精确匹配 canonical_symbol 或 canonical_name）。"""
    cur.execute(
        "SELECT asset_id FROM core.asset WHERE canonical_symbol = %s LIMIT 1",
        (symbol.upper().strip(),),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        "SELECT asset_id FROM core.asset WHERE LOWER(canonical_name) = LOWER(%s) LIMIT 1",
        (symbol.strip(),),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _get_price_from_db(cur, asset_id: int, target_date: date) -> float | None:
    """从 asset_market_daily 取日价（FIX-1: 表名对齐 + FIX-2: 异常兜底 + FIX-5: source_code 去歧义）。"""
    try:
        cur.execute(
            "SELECT price_usd FROM biz.asset_market_daily "
            "WHERE asset_id = %s AND market_date = %s AND price_usd > 0 "
            "ORDER BY market_date DESC LIMIT 1",
            (asset_id, target_date),
        )
        row = cur.fetchone()
        return float(row[0]) if row else None
    except Exception:
        return None


def _get_price_external(symbol: str, target_date: date | None = None) -> float | None:
    """外部 fallback：target_date 有值时用 Binance klines 历史收盘（FIX-4），否则实时价。"""
    try:
        import requests as _req
        pair = f"{symbol.upper()}USDT"
        if target_date:
            # Binance klines 历史日收盘
            import time as _time
            start_ms = int(target_date.strftime("%s")) * 1000
            end_ms = start_ms + 86400000 - 1
            url = (f"https://api.binance.com/api/v3/klines?symbol={pair}"
                   f"&interval=1d&startTime={start_ms}&endTime={end_ms}&limit=1")
            r = _req.get(url, timeout=10)
            if r.status_code == 200:
                klines = r.json()
                if klines and len(klines) >= 1:
                    return float(klines[0][4])  # close price
        else:
            # 实时价（fallback）
            url = f"https://api.binance.com/api/v3/ticker/price?symbol={pair}"
            r = _req.get(url, timeout=10)
            if r.status_code == 200:
                return float(r.json().get("price", 0)) or None
    except Exception:
        pass
    return None


def get_price_at(cur, asset_id: int, symbol: str, target_date: date) -> float | None:
    """三段式取价：DB日价 → symbol→asset_id 解析 + DB日价 → 外部 klines 历史 fallback。"""
    # 1) 直接查 DB 日价
    price = _get_price_from_db(cur, asset_id, target_date)
    if price is not None:
        return price
    # 2) symbol → asset_id 解析（asset_id 可能为 None）
    if asset_id is None and symbol:
        resolved = _resolve_symbol_to_asset_id(cur, symbol)
        if resolved:
            price = _get_price_from_db(cur, resolved, target_date)
            if price is not None:
                return price
    # 3) 外部 fallback：target_date 有值用 klines 历史收盘
    return _get_price_external(symbol, target_date) if symbol else None


# ── 方向符号 ──

def direction_multiplier(direction: str) -> int:
    """short → -1，long → 1，watch → 1（看多方向）。"""
    if direction == "short":
        return -1
    return 1


# ── BTC 收益基准 ──

def _get_btc_benchmark(cur, entry_date: date, exit_date: date) -> float | None:
    """取 BTC 在 [entry, exit] 的收益（%）。FIX-3: 用 Binance klines 历史收盘（避免 DB 脏值）。"""
    try:
        import requests as _req
        start_ms = int(entry_date.strftime("%s")) * 1000
        end_ms = int(exit_date.strftime("%s")) * 1000 + 86400000 - 1
        url = (f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT"
               f"&interval=1d&startTime={start_ms}&endTime={end_ms}&limit=2")
        r = _req.get(url, timeout=10)
        if r.status_code == 200:
            klines = r.json()
            if len(klines) >= 2:
                p_entry = float(klines[0][4])
                p_exit = float(klines[-1][4])
                if p_entry > 0:
                    return round((p_exit - p_entry) / p_entry * 100, 4)
    except Exception:
        pass
    return None


# ── 回测核心 ──

def backtest_single(opportunity: dict, cur) -> dict | None:
    """对单条机会做回测，返回 {signal_type, target, direction, wins, losses, alpha, ...}。"""
    target = opportunity.get("target", "")
    signal_type = opportunity.get("signal_type", "__default__")
    direction = opportunity.get("direction", "long")
    snap_date_str = opportunity.get("_snap_date")
    if not snap_date_str:
        return None

    try:
        snap_date = date.fromisoformat(snap_date_str)
    except (ValueError, TypeError):
        return None

    # 解析 asset_id（可能为 None）
    asset_id = opportunity.get("_asset_id")
    symbol = target.upper().strip() if target else ""

    # 持有期
    horizons = SIGNAL_HORIZONS.get(signal_type, [1, 7])
    results = []
    dm = direction_multiplier(direction)

    for h_days in horizons:
        exit_date = snap_date + timedelta(days=h_days)
        price_entry = get_price_at(cur, asset_id, symbol, snap_date)
        price_exit = get_price_at(cur, asset_id, symbol, exit_date)

        if price_entry is None or price_exit is None or price_entry == 0:
            continue

        ret_pct = round((price_exit - price_entry) / price_entry * 100, 4)
        pnl_pct = round(ret_pct * dm, 4)  # short 取反
        win = 1 if pnl_pct > 0 else 0
        loss = 1 if pnl_pct < 0 else 0

        # BTC 基准
        btc_ret = _get_btc_benchmark(cur, snap_date, exit_date)
        alpha = round(pnl_pct - (btc_ret or 0) * dm, 4) if btc_ret is not None else None

        results.append({
            "horizon_days": h_days,
            "pnl_pct": pnl_pct,
            "win": win,
            "loss": loss,
            "alpha": alpha,
            "btc_ret_pct": btc_ret,
        })

    if not results:
        return None

    # 汇总（取最短持有期作为主指标）
    primary = results[0]
    return {
        "signal_type": signal_type,
        "target": target,
        "direction": direction,
        "entry_date": snap_date_str,
        "horizon_days": primary["horizon_days"],
        "pnl_pct": primary["pnl_pct"],
        "win": primary["win"],
        "loss": primary["loss"],
        "alpha": primary["alpha"],
        "btc_ret_pct": primary["btc_ret_pct"],
        "all_horizons": results,
    }


def backtest_opportunities(days: int = 30) -> dict:
    """回测最近 N 天的快照机会，返回信号类型级汇总。"""
    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            # 读快照列表
            cur.execute(
                "SELECT snap_date FROM biz.market_overview_snapshot "
                "WHERE snap_date >= CURRENT_DATE - %s::interval "
                "ORDER BY snap_date",
                (f"{days} days",),
            )
            snap_dates = [row[0] for row in cur.fetchall()]

            if not snap_dates:
                return {"status": "no_data", "message": f"无 {days} 天内快照"}

            all_results: list[dict] = []
            for sd in snap_dates:
                cur.execute(
                    "SELECT payload FROM biz.market_overview_snapshot WHERE snap_date = %s",
                    (sd,),
                )
                row = cur.fetchone()
                if not row:
                    continue
                payload = dict(row[0]) if row[0] else {}
                opps = ((payload.get("opportunity_list") or {}).get("opportunities") or [])
                for opp in opps:
                    opp["_snap_date"] = sd.isoformat() if hasattr(sd, "isoformat") else str(sd)
                    # 尝试从 involved_symbols 或 target 解析 asset_id
                    opp["_asset_id"] = None
                    bt = backtest_single(opp, cur)
                    if bt:
                        all_results.append(bt)

    # 按 signal_type 聚合
    by_type: dict[str, list[dict]] = {}
    for r in all_results:
        st = r.get("signal_type", "__default__")
        by_type.setdefault(st, []).append(r)

    summary = {}
    for st, items in sorted(by_type.items()):
        total = len(items)
        wins = sum(i["win"] for i in items)
        losses = sum(i["loss"] for i in items)
        alphas = [i["alpha"] for i in items if i["alpha"] is not None]
        avg_alpha = round(sum(alphas) / len(alphas), 4) if alphas else None

        # 门控
        if total < MIN_SAMPLES:
            gate = "preliminary"
        elif st in NOT_CALIBRABLE:
            gate = "not_calibrable"
        else:
            gate = "calibrable"

        summary[st] = {
            "total": total,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / total * 100, 1) if total else 0,
            "avg_alpha": avg_alpha,
            "gate": gate,
        }

    return {
        "status": "ok",
        "snapshots": len(snap_dates),
        "total_opportunities": len(all_results),
        "signal_types": summary,
    }


# ── CLI ──

def main() -> int:
    parser = argparse.ArgumentParser(description="机会评分回测框架")
    parser.add_argument("--days", type=int, default=30, help="回测天数（默认30）")
    parser.add_argument("--list-snapshots", action="store_true", help="列出可用快照")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT snap_date FROM biz.market_overview_snapshot ORDER BY snap_date DESC LIMIT 30"
            )
            dates = [row[0] for row in cur.fetchall()]

    if args.list_snapshots:
        print(f"可用快照: {len(dates)} 天")
        for d in dates[:10]:
            print(f"  {d}")
        return 0

    result = backtest_opportunities(days=args.days)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
