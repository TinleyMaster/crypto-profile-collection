"""机会评分回测框架（OBI-OPT-BACKTEST-001）。

基于 market_overview_snapshot 快照历史，回测各 signal_type 的命中率与 alpha，
并按刀2（2026-09-26 审计·P0-B）把结果落 `biz.signal_type_calibration`，
供 `macro_market.py` 做 conviction 衰减 / HIGH 门控，打通「回测 → 权重」反馈闭环。

三阻塞处置（2026-09-26）：
  · D1 日价缺口：取价改为「一次批量拉全窗口日价」+ 外部 klines 兜底 + 按原因计数
    （`skipped_no_price`），不再逐条静默丢弃；
  · D2 不可回测类型：`NOT_BACKTESTABLE` 扩到全部聚合/非可交易 target 类型，
    同快照同 target 去重与跳过均落 `skipped_dup` / `skipped_not_backtestable` 计数，
    并进入返回值（原实现算完即丢）；无 signal_type 的记录单独计数；
  · D3 快照滞后/样本不足：`token_unlock` 原留 [0,1,3] 的 h=0 使 entry==exit →
    pnl 恒为 0 → hit_rate 假 0%，已修正为 [1,3,7]；BTC 基准按 (entry,exit) 缓存，
    避免重复外呼导致回测跑不完。

用法：
    python backtest_opportunities.py                 # 回测最近 30 天（只读报表）
    python backtest_opportunities.py --days 7
    python backtest_opportunities.py --write         # 附加：写入 biz.signal_type_calibration
    python backtest_opportunities.py --list-snapshots
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

# ── 路径兼容 ──
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent / "scripts" / "src"))

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection


# ── 常量 ──

# 各 signal_type 的持有期（天）：T+X 平仓。
# 未登记类型回退 DEFAULT_HORIZONS。NOT_BACKTESTABLE 的类型不会走到这里（先被跳过）。
SIGNAL_HORIZONS: dict[str, list[int]] = {
    "mvrv_deep_under":  [7, 14, 30],
    "btc_left_accum":   [1, 7],
    "cm_adoption_divergence": [1, 7],
    "catalyst":         [7, 14, 30],
    "whale_flow":       [1, 7],
    "github_activity":  [1, 7],
    "funding":          [1, 7],
    # 刀2 修复（D3）：原 [0, 1, 3] 的 h=0 使 entry_date == exit_date → pnl 恒 0
    # → 该类型 hit_rate 被算成 0%（51/51 全零），是假的低命中率，必须先修再校准。
    "token_unlock":     [1, 3, 7],
    "kol_onchain":      [1, 7],
    "etf_flow":         [1, 7],
    "conflict_game":    [1, 7],
    "price_surge":      [1, 7],
    "price_crash":      [1, 7],
    "price_volume_surge": [1, 7],
    "volume_surge":     [1, 7],
}
DEFAULT_HORIZONS = [1, 7]

# 样本量门控（与 macro_market 消费口径一致）
MIN_SAMPLES = 30
HIT_RATE_MIN = 0.5
DECAY_FACTOR = 0.60

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,10}$")

# NOT_CALIBRABLE：估值极值类，回测口径不适用（无法靠历史命中率校准）
NOT_CALIBRABLE = {"mvrv_deep_under"}

# NOT_BACKTESTABLE：target 不是可交易 asset（聚合观察池 / 衍生品极值 / 情绪极值 /
# 赛道资金流 / 无法取价的冷门币），取不到价 ⇒ 无法回测。
# 刀2：原集合只有 {funding, mvrv_under_watch}，导致 narrative / chain_inflow /
# mvrv_deep_over / fng_extreme / leverage_extreme / stablecoin_inflow 等聚合类型
# 在回测里被当成「有价但失败」静默丢弃、且未计入 skipped。
NOT_BACKTESTABLE = {
    "funding",              # 融资落地：target 多为未收录新币，core.asset 无对应日价
    "mvrv_under_watch", "mvrv_deep_over", "mvrv_over_watch",  # 聚合观察池（"N 币 MVRV …"）
    "narrative",            # 叙事板块（"AI & Big Data"）
    "chain_inflow",         # 公链（"Base 链"）
    "sector_inflow", "sector_outflow",                        # 赛道资金流
    "fng_extreme",          # 恐贪指数极值（指数本身不可交易）
    "leverage_extreme",     # 衍生品杠杆极值（合约指标不可交易）
    "stablecoin_inflow",    # 稳定币净流入（宏观指标不可交易）
}

# 豁免集合：不参与衰减、保留 HIGH（刀2 §3「映射到权重豁免」，用户 2026-09-26 确认）
EXEMPT_SIGNAL_TYPES = NOT_CALIBRABLE | NOT_BACKTESTABLE

# 外部兜底上限：避免冷门币把回测拖成小时级
MAX_EXTERNAL_LOOKUPS = 200

# ── 运行期缓存（每次 backtest 开始清空）──
_BTC_CACHE: dict[tuple, float | None] = {}
_EXTERNAL_CACHE: dict[tuple, float | None] = {}


def _reset_caches() -> None:
    _BTC_CACHE.clear()
    _EXTERNAL_CACHE.clear()


# ── 价格取数（三段式）──

def _get_price_external(symbol: str, target_date: date | None = None) -> float | None:
    """外部 fallback：target_date 有值时用 Binance klines 历史收盘，否则实时价。"""
    key = (symbol.upper(), target_date)
    if key in _EXTERNAL_CACHE:
        return _EXTERNAL_CACHE[key]
    if len(_EXTERNAL_CACHE) >= MAX_EXTERNAL_LOOKUPS:
        _EXTERNAL_CACHE[key] = None
        return None
    val: float | None = None
    try:
        import requests as _req
        pair = f"{symbol.upper()}USDT"
        if target_date:
            start_ms = int(target_date.strftime("%s")) * 1000
            end_ms = start_ms + 86400000 - 1
            url = (f"https://api.binance.com/api/v3/klines?symbol={pair}"
                   f"&interval=1d&startTime={start_ms}&endTime={end_ms}&limit=1")
            r = _req.get(url, timeout=10)
            if r.status_code == 200:
                klines = r.json()
                if klines and len(klines) >= 1:
                    val = float(klines[0][4])
        else:
            url = f"https://api.binance.com/api/v3/ticker/price?symbol={pair}"
            r = _req.get(url, timeout=10)
            if r.status_code == 200:
                val = float(r.json().get("price", 0)) or None
    except Exception:
        val = None
    _EXTERNAL_CACHE[key] = val
    return val


def _fetch_price_map(cur, symbols: list[str], d_from: date, d_to: date) -> dict:
    """D1：一次批量拉全窗口日价，避免逐条 round-trip。

    返回 {(symbol, market_date): price}；同 symbol 多 asset 时取 asset_id 最小（与
    原 `ORDER BY asset_id LIMIT 1` 同口径）。包装/桥接币排除（腐败价源）。
    """
    if not symbols:
        return {}
    cur.execute(
        """
        SELECT DISTINCT ON (UPPER(a.canonical_symbol), m.market_date)
               UPPER(a.canonical_symbol), m.market_date, m.price_usd
        FROM biz.v_asset_market_daily_primary m
        JOIN core.asset a ON a.asset_id = m.asset_id
        WHERE m.market_date BETWEEN %s AND %s
          AND m.price_usd > 0
          AND UPPER(a.canonical_symbol) = ANY(%s)
          AND a.canonical_name NOT LIKE '%%Bridged%%'
          AND a.canonical_name NOT LIKE '%%Wrapped%%'
        ORDER BY UPPER(a.canonical_symbol), m.market_date, a.asset_id
        """,
        (d_from, d_to, symbols),
    )
    out: dict = {}
    for sym, mdate, price in cur.fetchall():
        out[(sym, mdate)] = float(price)
    return out


def direction_multiplier(direction: str) -> int:
    """short → -1，long/watch → 1（看多方向）。"""
    return -1 if direction == "short" else 1


def _get_btc_benchmark(entry_date: date, exit_date: date) -> float | None:
    """BTC 在 [entry, exit] 的收益（%）。刀2：按 (entry, exit) 缓存，避免重复外呼。"""
    key = (entry_date, exit_date)
    if key in _BTC_CACHE:
        return _BTC_CACHE[key]
    val: float | None = None
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
                    val = round((p_exit - p_entry) / p_entry * 100, 4)
    except Exception:
        val = None
    _BTC_CACHE[key] = val
    return val


# ── 快照机会抽取（只传小字段，不拉整份 payload）──

def _fetch_opportunities(cur, days: int) -> list[dict]:
    """D3：用 SQL 抽出 signal_type/target/direction 等最小元组。

    原实现逐快照 `SELECT payload`（实测 25 份 × ~500KB ≈ 12.5MB）在远程库上
    单次回测要十几分钟且经常跑不完，是「回测无法稳定产出」的直接原因。
    """
    cur.execute(
        """
        SELECT s.snap_date,
               o->>'signal_type'          AS signal_type,
               o->>'target'               AS target,
               COALESCE(o->>'direction', 'long') AS direction,
               o->>'conviction_tier'      AS conviction_tier,
               o->>'conviction_strength'  AS conviction_strength
        FROM biz.market_overview_snapshot s,
             jsonb_array_elements(s.payload->'opportunity_list'->'opportunities') o
        WHERE s.snap_date >= CURRENT_DATE - %s::interval
        ORDER BY s.snap_date
        """,
        (f"{days} days",),
    )
    rows = []
    for sd, st, tgt, direction, tier, strength in cur.fetchall():
        rows.append({
            "snap_date": sd if isinstance(sd, date) else date.fromisoformat(str(sd)),
            "signal_type": st,
            "target": (tgt or "").strip(),
            "direction": direction,
            "conviction_tier": tier,
            "conviction_strength": float(strength) if strength not in (None, "") else None,
        })
    return rows


def _is_symbol_target(tgt: str) -> bool:
    """target 是否为具体币种 symbol（聚合类 target 无法取价回测）。"""
    return bool(_SYMBOL_RE.match((tgt or "").strip().upper()))


def _gate_for(signal_type: str, total: int, hit_rate: float | None) -> tuple[str, float, bool]:
    """刀2 门控判定（纯函数）：返回 (gate, weight_factor, no_high)。

    豁免类型恒不衰减、保留 HIGH；样本不足(<MIN_SAMPLES)或命中率<HIT_RATE_MIN
    的类型衰减到 DECAY_FACTOR 且不进 HIGH 候选。
    """
    if signal_type in EXEMPT_SIGNAL_TYPES:
        if signal_type in NOT_CALIBRABLE:
            return "exempt_not_calibrable", 1.0, False
        return "exempt_not_backtestable", 1.0, False
    if total < MIN_SAMPLES:
        return "preliminary", DECAY_FACTOR, True
    if hit_rate is not None and hit_rate < HIT_RATE_MIN:
        return "calibrated_low", DECAY_FACTOR, True
    return "calibrated_ok", 1.0, False


# ── 回测核心 ──

def backtest_opportunities(days: int = 30, persist: bool = False) -> dict:
    """回测最近 N 天的快照机会，返回 signal_type 级汇总；persist=True 时落校准表。"""
    _reset_caches()
    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(DISTINCT snap_date) FROM biz.market_overview_snapshot "
                "WHERE snap_date >= CURRENT_DATE - %s::interval",
                (f"{days} days",),
            )
            n_snap = cur.fetchone()[0]

            if not n_snap:
                return {"status": "no_data", "message": f"无 {days} 天内快照"}

            raw = _fetch_opportunities(cur, days)
            if not raw:
                return {"status": "no_data", "message": f"快照内无 opportunity_list"}

            window_start = min(r["snap_date"] for r in raw)
            window_end = max(r["snap_date"] for r in raw)

            # 跳过计数（按原因 × 按类型）
            skipped = {"no_signal_type": 0, "not_backtestable": 0, "dup": 0, "no_price": 0}
            skip_by_type: dict[str, dict] = {}

            def _bump(kind: str, st: str) -> None:
                skipped[kind] += 1
                if st:
                    d = skip_by_type.setdefault(st, {"dup": 0, "no_price": 0, "not_backtestable": 0})
                    if kind in d:
                        d[kind] += 1

            seen: set[tuple] = set()
            candidates: list[dict] = []
            for r in raw:
                st = r["signal_type"]
                tgt = r["target"]
                if not st or not tgt:
                    _bump("no_signal_type", st or "")
                    continue
                if st in NOT_BACKTESTABLE or not _is_symbol_target(tgt):
                    _bump("not_backtestable", st)
                    continue
                key = (r["snap_date"], tgt.upper())
                if key in seen:
                    _bump("dup", st)
                    continue
                seen.add(key)
                candidates.append(r)

            # D1：一次批量取价
            d_from = window_start - timedelta(days=2)
            d_to = window_end + timedelta(days=31)
            symbols = sorted({r["target"].upper() for r in candidates})
            price_map = _fetch_price_map(cur, symbols, d_from, d_to)

            # 逐条回测（主指标 = 最短持有期）
            by_type: dict[str, list[dict]] = {}
            for r in candidates:
                st = r["signal_type"]
                sym = r["target"].upper()
                entry = r["snap_date"] - timedelta(days=1)
                dm = direction_multiplier(r["direction"])
                for h in SIGNAL_HORIZONS.get(st, DEFAULT_HORIZONS):
                    ex = entry + timedelta(days=h)
                    pe = price_map.get((sym, entry)) or _get_price_external(sym, entry)
                    px = price_map.get((sym, ex)) or _get_price_external(sym, ex)
                    if not pe or not px:
                        continue
                    ret = (px - pe) / pe * 100
                    pnl = round(ret * dm, 4)
                    btc = _get_btc_benchmark(entry, ex)
                    by_type.setdefault(st, []).append({
                        "horizon_days": h,
                        "pnl_pct": pnl,
                        "win": 1 if pnl > 0 else 0,
                        "loss": 1 if pnl < 0 else 0,
                        "alpha": round(pnl - (btc or 0) * dm, 4) if btc is not None else None,
                        "btc_ret_pct": btc,
                        "raw_strength": r.get("conviction_strength") or 0,
                    })
                    break  # 主指标 = 最短可取价持有期
                else:
                    _bump("no_price", st)

            summary: dict[str, dict] = {}
            for st, items in sorted(by_type.items()):
                total = len(items)
                wins = sum(i["win"] for i in items)
                losses = sum(i["loss"] for i in items)
                alphas = [i["alpha"] for i in items if i["alpha"] is not None]
                avg_alpha = round(sum(alphas) / len(alphas), 4) if alphas else None
                avg_pnl = round(sum(i["pnl_pct"] for i in items) / total, 4) if total else None
                hit_rate = round(wins / total, 4) if total else None

                # 门控（刀2）：豁免 → 不衰减；样本不足 / 命中率低 → 衰减且不进 HIGH
                gate, factor, no_high = _gate_for(st, total, hit_rate)

                sk = skip_by_type.get(st, {"dup": 0, "no_price": 0, "not_backtestable": 0})
                summary[st] = {
                    "total": total,
                    "wins": wins,
                    "losses": losses,
                    "win_rate": round(wins / total * 100, 1) if total else 0,
                    "hit_rate": hit_rate,
                    "avg_pnl_pct": avg_pnl,
                    "avg_alpha": avg_alpha,
                    "horizon_days": items[0]["horizon_days"] if items else None,
                    "gate": gate,
                    "weight_factor": factor,
                    "no_high": no_high,
                    "skipped_dup": sk["dup"],
                    "skipped_no_price": sk["no_price"],
                    "skipped_not_backtestable": sk["not_backtestable"],
                }

            # 豁免但本窗口无样本的类型也要在表里留痕（否则「无样本」与「未登记」不可分）
            for st in sorted(EXEMPT_SIGNAL_TYPES):
                if st in summary:
                    continue
                _g, _f, _nh = _gate_for(st, 0, None)
                summary[st] = {
                    "total": 0, "wins": 0, "losses": 0, "win_rate": 0, "hit_rate": None,
                    "avg_pnl_pct": None, "avg_alpha": None, "horizon_days": None,
                    "gate": _g, "weight_factor": _f, "no_high": _nh,
                    "skipped_dup": skip_by_type.get(st, {}).get("dup", 0),
                    "skipped_no_price": skip_by_type.get(st, {}).get("no_price", 0),
                    "skipped_not_backtestable": skip_by_type.get(st, {}).get("not_backtestable", 0),
                }

            written = 0
            if persist:
                written = _write_calibration(cur, summary, window_start, window_end)
                conn.commit()

    result = {
        "status": "ok",
        "snapshots": n_snap,
        "window": [window_start.isoformat(), window_end.isoformat()],
        "total_opportunities": sum(len(v) for v in by_type.values()),
        "skipped": skipped,
        "signal_types": summary,
    }
    if persist:
        result["calibration_rows_written"] = written
    return result


# ── 落表 ──

def _write_calibration(cur, summary: dict, window_start: date, window_end: date) -> int:
    """写 biz.signal_type_calibration（幂等：同 window_end 覆盖）。返回写入行数。"""
    n = 0
    for st, s in summary.items():
        cur.execute(
            """
            INSERT INTO biz.signal_type_calibration (
                signal_type, horizon_days, sample_count, wins, losses, hit_rate,
                avg_pnl_pct, avg_alpha, skipped_dup, skipped_no_price,
                skipped_not_backtestable, weight_factor, gate, no_high,
                window_start, window_end
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (signal_type, horizon_days, window_end) DO UPDATE SET
                sample_count = EXCLUDED.sample_count,
                wins = EXCLUDED.wins,
                losses = EXCLUDED.losses,
                hit_rate = EXCLUDED.hit_rate,
                avg_pnl_pct = EXCLUDED.avg_pnl_pct,
                avg_alpha = EXCLUDED.avg_alpha,
                skipped_dup = EXCLUDED.skipped_dup,
                skipped_no_price = EXCLUDED.skipped_no_price,
                skipped_not_backtestable = EXCLUDED.skipped_not_backtestable,
                weight_factor = EXCLUDED.weight_factor,
                gate = EXCLUDED.gate,
                no_high = EXCLUDED.no_high,
                window_start = EXCLUDED.window_start,
                created_at = NOW()
            """,
            (
                st, int(s["horizon_days"] or 1), s["total"], s["wins"], s["losses"],
                s["hit_rate"], s["avg_pnl_pct"], s["avg_alpha"],
                s["skipped_dup"], s["skipped_no_price"], s["skipped_not_backtestable"],
                s["weight_factor"], s["gate"], s["no_high"], window_start, window_end,
            ),
        )
        n += 1
    return n


# ── CLI ──

def main() -> int:
    parser = argparse.ArgumentParser(description="机会评分回测框架")
    parser.add_argument("--days", type=int, default=30, help="回测天数（默认30）")
    parser.add_argument("--write", action="store_true",
                        help="把结果写入 biz.signal_type_calibration（供 macro_market 消费）")
    parser.add_argument("--list-snapshots", action="store_true", help="列出可用快照")
    args = parser.parse_args()

    if args.list_snapshots:
        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT snap_date FROM biz.market_overview_snapshot "
                    "ORDER BY snap_date DESC LIMIT 30"
                )
                dates = [row[0] for row in cur.fetchall()]
        print(f"可用快照: {len(dates)} 天")
        for d in dates[:10]:
            print(f"  {d}")
        return 0

    result = backtest_opportunities(days=args.days, persist=args.write)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())