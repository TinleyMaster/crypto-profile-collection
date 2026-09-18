#!/usr/bin/env python3
"""催化剂结局追踪（P0）：把 catalyst_signal 的信号补齐多窗口价格结局 → biz.catalyst_outcome。

参考《催化剂实测评分与反馈校准方案_2026-09-17.md》v1.1。
对每个未结算的 (catalyst_id, asset_id)：

  窗口收益（绝对 + 扣 BTC 超额）：
    L1（有币安 K 线）：4h / 24h / 72h 用 1h K 线精确计算
    L2（无 K 线）    ：24h 用 CMC 快照 percent_change_24h 近似；4h/72h 记 NULL
    全部            ：7d / 14d 用 asset_market_daily 日频计算
  命中判定：
    优先 catalyst_impact.impact_direction，缺失用 event_type 默认方向
  结算状态：
    14d(336h) 窗口算完后置 outcome_state='resolved'

性能：K 线 / CMC 快照 / market_daily 全部一次性批量预加载到内存，
窗口计算在内存完成，避免逐条 DB 查询。

幂等：last_window 记录已结算的最大窗口，已结算的不重复计算。

用法：
    python collect_catalyst_outcome.py                # 增量：处理所有未 resolved 信号
    python collect_catalyst_outcome.py --limit 100    # 只处理前 100 条
    python collect_catalyst_outcome.py --dry-run      # 预览，不写入
"""
from __future__ import annotations

import argparse
import bisect
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

# 窗口定义（小时）：K线窗口 L1 专用，日频窗口全部
KLINE_WINDOWS = [4, 24, 72]
DAILY_WINDOWS = [168, 336]  # 7d / 14d
RESOLVED_WINDOW = 336

# event_type → 默认方向（catalyst_impact 缺失时兜底）
EVENT_TYPE_DIRECTION = {
    "listing": "bullish",
    "delisting": "bearish",
    "burn": "bullish",
    "airdrop": "bullish",
    "funding": "bullish",
    "partnership": "bullish",
    "tech_upgrade": "bullish",
    "regulation": "neutral",
    "market_update": "neutral",
    "other": None,
}


def get_conn():
    """获取数据库连接（dict_row，带 TCP keepalive 防空闲回收）。"""
    settings = get_settings(require_database=True)
    return psycopg.connect(
        settings.database_url,
        row_factory=psycopg.rows.dict_row,
        connect_timeout=30,
        options="-c lock_timeout=30000",
        keepalives=1,
        keepalives_idle=15,
        keepalives_interval=5,
        keepalives_count=3,
    )


# =====================================================================
# 批量预加载
# =====================================================================

def load_asset_symbols(conn) -> dict[int, str]:
    """asset_id → canonical_symbol + 'USDT'（active 资产）。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT asset_id, canonical_symbol || 'USDT' AS symbol
            FROM core.asset
            WHERE canonical_symbol IS NOT NULL AND status = 'active'
            """
        )
        return {r["asset_id"]: r["symbol"] for r in cur.fetchall()}


def load_kline_symbols(conn) -> set[str]:
    """asset_klines 中有 1h 数据的符号集合（L1 判定）。"""
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT symbol FROM biz.asset_klines WHERE interval = '1h'")
        return {r["symbol"] for r in cur.fetchall()}


def load_klines(conn, symbols: set[str]) -> dict[str, list]:
    """批量预加载指定符号的 1h K 线 → {symbol: [(open_time, close, high, low), ...]}。

    返回的列表按 open_time 升序。
    """
    if not symbols:
        return {}
    data: dict[str, list] = {}
    with conn.cursor() as cur:
        # 分批查，避免 IN 列表过长
        sym_list = sorted(symbols)
        batch_size = 100
        for i in range(0, len(sym_list), batch_size):
            batch = sym_list[i:i + batch_size]
            cur.execute(
                """
                SELECT symbol, open_time, close_px, high_px, low_px, quote_vol
                FROM biz.asset_klines
                WHERE interval = '1h' AND symbol = ANY(%s)
                ORDER BY symbol, open_time ASC
                """,
                (batch,),
            )
            for r in cur.fetchall():
                sym = r["symbol"]
                data.setdefault(sym, []).append(
                    (
                        r["open_time"],
                        float(r["close_px"]) if r["close_px"] is not None else None,
                        float(r["high_px"]) if r["high_px"] is not None else None,
                        float(r["low_px"]) if r["low_px"] is not None else None,
                        float(r["quote_vol"]) if r["quote_vol"] is not None else None,
                    )
                )
    return data


def load_cmc_pct(conn, asset_ids: set[int]) -> dict[int, list]:
    """批量预加载 L2 资产的 CMC 快照 → {asset_id: [(quote_time, pct_24h), ...]}。

    asset_id → cmc_id 走 asset_source_map（is_primary）。
    """
    if not asset_ids:
        return {}
    result: dict[int, list] = {}
    id_list = sorted(asset_ids)
    with conn.cursor() as cur:
        batch_size = 100
        for i in range(0, len(id_list), batch_size):
            batch = id_list[i:i + batch_size]
            # asset_id → cmc_id
            cur.execute(
                """
                SELECT asset_id, source_asset_key
                FROM core.asset_source_map
                WHERE source_code = 'cmc' AND is_primary = true
                  AND asset_id = ANY(%s)
                """,
                (batch,),
            )
            id2cmc = {r["asset_id"]: r["source_asset_key"] for r in cur.fetchall()}
            cmc_ids = [c for c in id2cmc.values() if c and str(c).isdigit()]
            if not cmc_ids:
                continue
            cur.execute(
                """
                SELECT cmc_id, quote_time, percent_change_24h
                FROM src_cmc.cmc_asset_quote_snapshot
                WHERE cmc_id = ANY(%s)
                  AND (is_anomaly IS NOT TRUE OR is_anomaly IS NULL)
                ORDER BY cmc_id, quote_time ASC
                """,
                (cmc_ids,),
            )
            # cmc_id → asset_id 反查
            cmc2id = {str(v): k for k, v in id2cmc.items()}
            for r in cur.fetchall():
                aid = cmc2id.get(str(r["cmc_id"]))
                if aid is None:
                    continue
                if r["percent_change_24h"] is not None:
                    result.setdefault(aid, []).append(
                        (r["quote_time"], float(r["percent_change_24h"]))
                    )
    return result


def load_market_daily(conn, asset_ids: set[int]) -> dict[int, list]:
    """批量预加载指定资产的 market_daily → {asset_id: [(market_date, price, vol_24h), ...]}。"""
    if not asset_ids:
        return {}
    result: dict[int, list] = {}
    id_list = sorted(asset_ids)
    with conn.cursor() as cur:
        batch_size = 200
        for i in range(0, len(id_list), batch_size):
            batch = id_list[i:i + batch_size]
            cur.execute(
                """
                SELECT asset_id, market_date, price_usd, volume_24h
                FROM biz.asset_market_daily
                WHERE source_code = 'cmc'
                  AND (is_anomaly IS NOT TRUE OR is_anomaly IS NULL)
                  AND asset_id = ANY(%s)
                ORDER BY asset_id, market_date ASC
                """,
                (batch,),
            )
            for r in cur.fetchall():
                if r["price_usd"] is not None:
                    result.setdefault(r["asset_id"], []).append(
                        (
                            r["market_date"],
                            float(r["price_usd"]),
                            float(r["volume_24h"]) if r["volume_24h"] is not None else None,
                        )
                    )
    return result


# =====================================================================
# 内存查询辅助（bisect）
# =====================================================================

def kline_close_at(klines: list, ts: datetime) -> float | None:
    """open_time <= ts 的最近一根 close_px。"""
    if not klines:
        return None
    times = [k[0] for k in klines]
    idx = bisect.bisect_right(times, ts) - 1
    if idx < 0:
        return None
    return klines[idx][1]


def kline_high_low_between(klines: list, t0: datetime, t1: datetime) -> tuple:
    """(t0, t1] 区间内 max(high), min(low)。"""
    if not klines:
        return None, None
    times = [k[0] for k in klines]
    i0 = bisect.bisect_right(times, t0)
    i1 = bisect.bisect_right(times, t1)
    if i0 >= i1:
        return None, None
    seg = klines[i0:i1]
    hi = max(k[2] for k in seg if k[2] is not None)
    lo = min(k[3] for k in seg if k[3] is not None)
    return (hi, lo) if (hi is not None and lo is not None) else (None, None)


def cmc_pct_at(snapshots: list, ts: datetime) -> float | None:
    """quote_time <= ts 的最近一条 percent_change_24h。"""
    if not snapshots:
        return None
    times = [s[0] for s in snapshots]
    idx = bisect.bisect_right(times, ts) - 1
    if idx < 0:
        return None
    return snapshots[idx][1]


def daily_price_at(rows: list, target_date) -> float | None:
    """market_date >= target_date 的第一个收盘价。"""
    dates = [r[0] for r in rows]
    idx = bisect.bisect_left(dates, target_date)
    if idx >= len(rows):
        return None
    return rows[idx][1]


def daily_vol_ratio_at(rows: list, base_date) -> float | None:
    """日频量比：base_date 当日 volume / 其前 7 日均量。

    rows 元素为 (market_date, price, vol_24h)。
    """
    vols = [r[2] for r in rows if r[2] is not None and r[0] <= base_date]
    if not vols:
        return None
    today_vol = vols[-1]
    prior = vols[:-1][-7:]  # 不含当日的前 7 天
    if not prior or not any(v > 0 for v in prior):
        return None
    avg7 = sum(prior) / len(prior)
    if avg7 <= 0:
        return None
    return round(today_vol / avg7, 4)


# =====================================================================
# 业务逻辑
# =====================================================================

def resolve_direction(event_type: str, impact_direction) -> tuple:
    """命中判定方向：优先 impact 表，缺失用 event_type 默认。"""
    if impact_direction:
        return impact_direction, "impact"
    default = EVENT_TYPE_DIRECTION.get(event_type)
    if default:
        return default, "event_type"
    return None, "null"


def hit_of(excess: float | None, direction) -> bool | None:
    """excess 方向是否符合预期方向。neutral/None 方向不参与命中。"""
    if excess is None or direction in (None, "neutral"):
        return None
    if direction == "bullish":
        return excess > 0
    return excess < 0


def compute_outcome(row: dict, now: datetime, symbol: str | None,
                    kline_symbols: set[str], klines: dict, cmc_pct: dict,
                    daily: dict, btc_asset_id: int | None) -> tuple:
    """内存计算一条信号的全部窗口。返回 (computed, data_tier, direction, direction_src)。

    computed 中 last_window 为已结算的最大窗口小时数。
    """
    asset_id = row["asset_id"]
    base_time = row["created_at"]
    data_tier = "L1" if symbol and symbol in kline_symbols else "L2"
    direction, direction_src = resolve_direction(row["event_type"], row["impact_direction"])

    computed: dict = {}
    last_window = 0
    base_price = None

    # ---- L1：K 线精确窗口（4h/24h/72h） ----
    if data_tier == "L1":
        sym_klines = klines.get(symbol, [])
        btc_klines = klines.get("BTCUSDT", [])
        base_price = kline_close_at(sym_klines, base_time)
        if base_price is None:
            return None, data_tier, direction, direction_src  # 信号太新，K线未到
        btc_base = kline_close_at(btc_klines, base_time)
        for w in KLINE_WINDOWS:
            if base_time + timedelta(hours=w) > now:
                break  # 窗口未到
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
        # 24h 极端风险（K 线 high/low）
        if base_time + timedelta(hours=24) <= now:
            hi, lo = kline_high_low_between(sym_klines, base_time, base_time + timedelta(hours=24))
            if hi is not None and base_price:
                computed["max_gain_24h"] = round((hi / base_price - 1) * 100, 4)
            if lo is not None and base_price:
                computed["max_drawdown_24h"] = round((lo / base_price - 1) * 100, 4)

    # ---- L2：CMC 快照 24h 近似 ----
    if data_tier == "L2" and base_time + timedelta(hours=24) <= now:
        ts24 = base_time + timedelta(hours=24)
        pct = cmc_pct_at(cmc_pct.get(asset_id, []), ts24)
        if pct is not None:
            computed["ret_24h"] = round(pct, 4)
            # BTC 同期 CMC 近似（用 asset_id 为 BTC 的快照）
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
        if base_time + timedelta(hours=w) > now:
            continue  # 窗口未到
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
        if key == "7d":  # 表只有 hit_7d，14d 不落命中
            hit = hit_of(computed.get("excess_7d"), direction)
            if hit is not None:
                computed["hit_7d"] = hit
        last_window = max(last_window, w)

    # ---- 量比 24h（日频通用：base 当日 vs 前 7 日均量） ----
    if "ret_24h" in computed and computed.get("vol_ratio_24h") is None:
        vr = daily_vol_ratio_at(asset_daily, base_date)
        if vr is not None:
            computed["vol_ratio_24h"] = vr

    if not computed:
        return None, data_tier, direction, direction_src

    computed["last_window"] = last_window
    return computed, data_tier, direction, direction_src


def upsert_outcome(conn, row: dict, computed: dict, direction: str, direction_src: str,
                   data_tier: str, base_price: float | None, base_time: datetime) -> None:
    """写入/更新 catalyst_outcome。"""
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
                %(catalyst_id)s, %(asset_id)s, %(signal_id)s, %(base_time)s, %(base_price)s,
                %(ret_4h)s, %(ret_24h)s, %(ret_72h)s, %(ret_7d)s, %(ret_14d)s,
                %(excess_4h)s, %(excess_24h)s, %(excess_72h)s, %(excess_7d)s, %(excess_14d)s,
                %(hit_4h)s, %(hit_24h)s, %(hit_72h)s, %(hit_7d)s,
                %(vol_ratio_24h)s, %(vol_ratio_72h)s,
                %(max_drawdown_24h)s, %(max_gain_24h)s,
                %(impact_direction)s, %(direction_src)s, %(data_tier)s, %(state)s, %(last_window)s,
                %(ret_source)s, NOW()
            )
            ON CONFLICT (catalyst_id, asset_id) DO UPDATE SET
                signal_id = EXCLUDED.signal_id,
                base_time = EXCLUDED.base_time,
                base_price_usd = COALESCE(EXCLUDED.base_price_usd, biz.catalyst_outcome.base_price_usd),
                ret_4h = COALESCE(EXCLUDED.ret_4h, biz.catalyst_outcome.ret_4h),
                ret_24h = COALESCE(EXCLUDED.ret_24h, biz.catalyst_outcome.ret_24h),
                ret_72h = COALESCE(EXCLUDED.ret_72h, biz.catalyst_outcome.ret_72h),
                ret_7d = COALESCE(EXCLUDED.ret_7d, biz.catalyst_outcome.ret_7d),
                ret_14d = COALESCE(EXCLUDED.ret_14d, biz.catalyst_outcome.ret_14d),
                excess_4h = COALESCE(EXCLUDED.excess_4h, biz.catalyst_outcome.excess_4h),
                excess_24h = COALESCE(EXCLUDED.excess_24h, biz.catalyst_outcome.excess_24h),
                excess_72h = COALESCE(EXCLUDED.excess_72h, biz.catalyst_outcome.excess_72h),
                excess_7d = COALESCE(EXCLUDED.excess_7d, biz.catalyst_outcome.excess_7d),
                excess_14d = COALESCE(EXCLUDED.excess_14d, biz.catalyst_outcome.excess_14d),
                hit_4h = COALESCE(EXCLUDED.hit_4h, biz.catalyst_outcome.hit_4h),
                hit_24h = COALESCE(EXCLUDED.hit_24h, biz.catalyst_outcome.hit_24h),
                hit_72h = COALESCE(EXCLUDED.hit_72h, biz.catalyst_outcome.hit_72h),
                hit_7d = COALESCE(EXCLUDED.hit_7d, biz.catalyst_outcome.hit_7d),
                vol_ratio_24h = COALESCE(EXCLUDED.vol_ratio_24h, biz.catalyst_outcome.vol_ratio_24h),
                vol_ratio_72h = COALESCE(EXCLUDED.vol_ratio_72h, biz.catalyst_outcome.vol_ratio_72h),
                max_drawdown_24h = COALESCE(EXCLUDED.max_drawdown_24h, biz.catalyst_outcome.max_drawdown_24h),
                max_gain_24h = COALESCE(EXCLUDED.max_gain_24h, biz.catalyst_outcome.max_gain_24h),
                impact_direction = EXCLUDED.impact_direction,
                direction_src = EXCLUDED.direction_src,
                data_tier = EXCLUDED.data_tier,
                outcome_state = EXCLUDED.outcome_state,
                last_window = GREATEST(biz.catalyst_outcome.last_window, EXCLUDED.last_window),
                ret_source = EXCLUDED.ret_source,
                updated_at = NOW()
            """,
            {
                "catalyst_id": row["catalyst_id"],
                "asset_id": row["asset_id"],
                "signal_id": row["signal_id"],
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
                "ret_source": "klines+market_daily" if data_tier == "L1" else "cmc+market_daily",
            },
        )


def fetch_pending_signals(conn, limit: int | None) -> list[dict]:
    """获取待处理信号 + 事件类型 + impact_direction。

    只取两类：
      1. 尚无 outcome 记录的新信号
      2. 已有 outcome 但 last_window 落后于已到期窗口的（7d/14d 推进）
    """
    sql = """
        SELECT cs.signal_id, cs.catalyst_id, cs.asset_id, cs.created_at,
               COALESCE(ac.ai_event_type, ac.rule_event_type, 'other') AS event_type,
               ci.impact_direction
        FROM biz.catalyst_signal cs
        JOIN biz.asset_catalyst ac ON ac.catalyst_id = cs.catalyst_id
        LEFT JOIN biz.catalyst_impact ci
               ON ci.catalyst_id = cs.catalyst_id AND ci.asset_id = cs.asset_id
        LEFT JOIN biz.catalyst_outcome co
               ON co.catalyst_id = cs.catalyst_id AND co.asset_id = cs.asset_id
        WHERE co.outcome_id IS NULL
           OR (
                co.outcome_state IN ('pending', 'no_data')
                AND (
                      (co.last_window < 168 AND cs.created_at + INTERVAL '168 hours' <= NOW())
                   OR (co.last_window < 336 AND cs.created_at + INTERVAL '336 hours' <= NOW())
                )
              )
        ORDER BY cs.created_at ASC
    """
    params: list = []
    if limit:
        sql += " LIMIT %s"
        params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def main() -> int:
    parser = argparse.ArgumentParser(description="催化剂结局追踪（P0）")
    parser.add_argument("--limit", type=int, default=None, help="最多处理 N 条")
    parser.add_argument("--dry-run", action="store_true", help="预览不写入")
    args = parser.parse_args()

    conn = get_conn()
    try:
        # 1. 预取基础映射
        asset_symbols = load_asset_symbols(conn)
        kline_symbols = load_kline_symbols(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT asset_id FROM core.asset WHERE canonical_symbol = 'BTC' AND status = 'active' LIMIT 1"
            )
            row = cur.fetchone()
            btc_asset_id = row["asset_id"] if row else None

        # 2. 待处理信号
        rows = fetch_pending_signals(conn, args.limit)
        print(f"待处理信号: {len(rows)} 条")
        if not rows:
            print("无待处理信号")
            return 0

        # 3. 收集需要的符号与资产
        l1_symbols = {asset_symbols[r["asset_id"]] for r in rows
                      if asset_symbols.get(r["asset_id"]) in kline_symbols}
        l1_symbols.add("BTCUSDT")
        l2_assets = {r["asset_id"] for r in rows
                     if not (asset_symbols.get(r["asset_id"]) in kline_symbols)}
        all_assets = {r["asset_id"] for r in rows} | ({btc_asset_id} if btc_asset_id else set())

        # 4. 批量预加载（K 线 / CMC / market_daily）
        print(f"  预加载 K 线({len(l1_symbols)} 符号), CMC({len(l2_assets)} 资产), 日频({len(all_assets)} 资产)...")
        klines = load_klines(conn, l1_symbols)
        cmc_pct = load_cmc_pct(conn, l2_assets | ({btc_asset_id} if btc_asset_id else set()))
        daily = load_market_daily(conn, all_assets)
        print(f"  K线 {sum(len(v) for v in klines.values())} 行, CMC {sum(len(v) for v in cmc_pct.values())} 行, "
              f"日频 {sum(len(v) for v in daily.values())} 行")

        # 5. 内存计算 + 落库（断线自动重连，内存预加载数据不丢）
        now = datetime.now(timezone.utc)
        done = 0
        skipped = 0
        BATCH_COMMIT = 100  # 每 N 行批量提交，减少网络往返
        for i, row in enumerate(rows, 1):
            symbol = asset_symbols.get(row["asset_id"])
            for attempt in range(4):  # 断线重连最多 3 次
                try:
                    res = compute_outcome(row, now, symbol, kline_symbols, klines, cmc_pct,
                                          daily, btc_asset_id)
                    if res is None or res[0] is None:
                        skipped += 1
                        break
                    computed, data_tier, direction, direction_src = res
                    base_price = None
                    if data_tier == "L1":
                        sym_klines = klines.get(symbol, [])
                        base_price = kline_close_at(sym_klines, row["created_at"])
                    if not args.dry_run:
                        upsert_outcome(conn, row, computed, direction, direction_src,
                                       data_tier, base_price, row["created_at"])
                        if done % BATCH_COMMIT == BATCH_COMMIT - 1:
                            conn.commit()
                    done += 1
                    break
                except psycopg.OperationalError as e:
                    if attempt >= 3 or args.dry_run:
                        print(f"  [失败] signal={row['signal_id']} cat={row['catalyst_id']}: {e}")
                        break
                    # 断线重连
                    print(f"  [重连] signal={row['signal_id']} (attempt {attempt + 1})")
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = get_conn()
                except Exception as e:
                    print(f"  [失败] signal={row['signal_id']} cat={row['catalyst_id']}: {e}")
                    break
            if i % 500 == 0:
                print(f"  进度: {i}/{len(rows)} (已结算 {done})")
        if not args.dry_run:
            conn.commit()

        print(f"完成: 处理 {len(rows)} 条，结算/更新 {done} 条，跳过 {skipped} 条")
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
