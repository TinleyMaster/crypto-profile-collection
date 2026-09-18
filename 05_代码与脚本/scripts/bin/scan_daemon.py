#!/usr/bin/env python3
"""
盘面异动扫描守护进程（scan_daemon）。

将原先由 scheduler 每 5/15/30 分钟调度的多个扫描脚本合并为单进程常驻，
共享 DB 连接池、Binance API session 和币对列表，显著降低内存和启动开销。

包含任务（按调度周期）：
  - [5min]   scan_klines       — Binance USDT 永续 K 线增量（5m/15m/1h）
  - [5min]   scan_oi_cvd       — OI/CVD 5 分钟采样（错峰 +2min）
  - [5min]   scan_alert        — 高置信信号告警（错峰 +3min）
  - [15min]  scan_main_pool    — 主池扫描 L0+L1+L2
  - [30min]  scan_accumulation — 蓄势池 ACC/BRK
  - [30min]  watchlist_monitor — 解锁/空头/大户监控

设计原则：
  - 单进程，多线程调度（每个任务独立线程，内部循环）
  - SkipIfRunning：上一轮未结束则跳过本轮（防堆积）
  - 共享 DB 连接池（psycopg_pool）和 requests Session
  - 单任务崩溃不影响其他任务（try/except 包裹每轮）
  - 内存占用 ≈ 100-150MB（对比 7 个独立进程的 ~700MB）

用法：
    python scan_daemon.py                    # 启动全部任务
    python scan_daemon.py --run-once klines  # 只跑一次指定任务（调试）
    python scan_daemon.py --only klines,oi   # 只启动指定任务
    python scan_daemon.py --min-vol-usd 5000000  # 过滤低流动性合约
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg.rows  # noqa: E402
import psycopg_pool  # noqa: E402

from crypto_research.clients.binance_http import fapi_get, set_min_request_gap  # noqa: E402
from crypto_research.config import get_settings  # noqa: E402

# ── 全局共享资源 ────────────────────────────────────────────────

_SETTINGS = None
_DB_POOL: psycopg_pool.ConnectionPool | None = None
_MIN_HTTP_GAP = 0.0  # 全局 Binance API 请求最小间隔（秒），0=不限速，默认关闭（适配美国独立IP节点）

FAPI_BASE = "https://fapi.binance.com"
DEFAULT_INTERVALS = ("5m", "15m", "1h")
INCREMENTAL_LIMIT = 10
BUCKET_SECONDS = 300  # 5m OI/CVD 桶

# ── 初始化 ──────────────────────────────────────────────────────


def _init():
    """惰性初始化全局资源（DB 连接池）。HTTP 请求统一走 binance_http.fapi_get。"""
    global _SETTINGS, _DB_POOL
    if _SETTINGS is None:
        _SETTINGS = get_settings(require_database=True)
    if _DB_POOL is None:
        _DB_POOL = psycopg_pool.ConnectionPool(
            _SETTINGS.database_url,
            min_size=2,
            max_size=6,
            open=True,
            timeout=30,
            kwargs={"connect_timeout": 30, "options": "-c lock_timeout=30000"},
        )
    return _SETTINGS, _DB_POOL


def _db():
    """从连接池取一个连接（上下文管理器风格）。"""
    if _DB_POOL is None:
        _init()
    return _DB_POOL.connection()  # type: ignore


def _http_get(url: str, params: dict | None = None, timeout: int = 20) -> dict | list:
    """Binance API GET：委托 binance_http.fapi_get（全局限频 + 429/418 指数退避 + 全局封禁闸门）。"""
    return fapi_get(url, params, timeout=timeout)


# ═══════════════════════════════════════════════════════════════
#  任务 1：K 线增量采集（5 分钟）
# ═══════════════════════════════════════════════════════════════

def _get_usdt_perpetuals() -> list[str]:
    """获取全部 USDT 永续合约列表（缓存 1 小时）。"""
    cache_key = "_usdt_perps_cache"
    cache_ts_key = "_usdt_perps_cache_ts"
    now = time.time()
    if (getattr(_get_usdt_perpetuals, cache_ts_key, 0) + 3600) > now:
        return getattr(_get_usdt_perpetuals, cache_key, [])
    data = _http_get(f"{FAPI_BASE}/fapi/v1/exchangeInfo")
    syms = sorted(
        s["symbol"] for s in data["symbols"]
        if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"
        and s["contractType"] == "PERPETUAL"
    )
    setattr(_get_usdt_perpetuals, cache_key, syms)
    setattr(_get_usdt_perpetuals, cache_ts_key, now)
    return syms


def _get_24h_quote_volume() -> dict[str, float]:
    """获取 24h 成交额（USDT），用于过滤低流动性合约。"""
    data = _http_get(f"{FAPI_BASE}/fapi/v1/ticker/24hr")
    return {row["symbol"]: float(row.get("quoteVolume") or 0) for row in data}


def _fetch_klines_incremental(symbol: str, interval: str) -> list[tuple]:
    """拉取单个币单个周期的最近 N 根 K 线，并校验数据新鲜度。"""
    raw = _http_get(
        f"{FAPI_BASE}/fapi/v1/klines",
        {"symbol": symbol, "interval": interval, "limit": INCREMENTAL_LIMIT},
    )
    rows = []
    for k in raw:
        open_time = datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc)
        rows.append((
            symbol, interval, open_time,
            float(k[1]), float(k[2]), float(k[3]), float(k[4]),
            float(k[5]), float(k[7]), int(k[8]),
        ))
    # 新鲜度校验：最新一根 K 线不应早于「2×周期 + 5 分钟」（防写陈旧/异常数据，
    # 如交易所返回旧缓存或该币已停牌）。不新鲜则整批跳过，宁缺毋错。
    if rows:
        latest_ot = rows[-1][2]
        max_age_sec = INTERVAL_SECONDS[interval] * 2 + 300
        if (datetime.now(timezone.utc) - latest_ot).total_seconds() > max_age_sec:
            return []
    return rows


INTERVAL_SECONDS = {"5m": 300, "15m": 900, "1h": 3600}


def task_scan_klines(min_vol_usd: float = 0, intervals: tuple = DEFAULT_INTERVALS,
                     workers: int = 8) -> dict:
    """K 线增量采集（单轮）。返回统计 dict。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    symbols = _get_usdt_perpetuals()
    if min_vol_usd:
        vol_map = _get_24h_quote_volume()
        symbols = [s for s in symbols if vol_map.get(s, 0) >= min_vol_usd]

    all_rows: list[tuple] = []
    errors = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = []
        for sym in symbols:
            for iv in intervals:
                futures.append(pool.submit(_fetch_klines_incremental, sym, iv))
        for fut in as_completed(futures):
            try:
                all_rows.extend(fut.result())
            except Exception:
                errors += 1

    upsert_sql = """
        INSERT INTO biz.asset_klines
            (symbol, interval, open_time, open_px, high_px, low_px, close_px,
             base_vol, quote_vol, trade_count, fetched_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
        ON CONFLICT (symbol, interval, open_time) DO UPDATE SET
            open_px=EXCLUDED.open_px, high_px=EXCLUDED.high_px, low_px=EXCLUDED.low_px,
            close_px=EXCLUDED.close_px, base_vol=EXCLUDED.base_vol,
            quote_vol=EXCLUDED.quote_vol, trade_count=EXCLUDED.trade_count,
            fetched_at=NOW()
    """
    if all_rows:
        with _db() as conn:
            with conn.cursor() as cur:
                cur.executemany(upsert_sql, all_rows)
            conn.commit()

    return {"symbols": len(symbols), "rows": len(all_rows), "errors": errors}


# ═══════════════════════════════════════════════════════════════
#  任务 2：OI/CVD 采样（5 分钟，错峰 +2min）
# ═══════════════════════════════════════════════════════════════

def _load_cursors(conn) -> dict[str, int]:
    cur = conn.cursor()
    cur.execute("SELECT symbol, last_trade_id FROM biz.scan_sampler_state")
    return {row[0]: int(row[1] or 0) for row in cur.fetchall()}


def _sample_symbol(symbol: str, last_trade_id: int) -> dict:
    """采样单个币：OI（张数×标记价，USD）+ 增量 CVD。"""
    # OI 价值 = openInterest qty × markPrice（与 phase_oi_cvd_sampler 口径一致，
    # 不可直接用张数当 USD——历史曾因此写入量级错误数据）
    oi_data = _http_get(f"{FAPI_BASE}/fapi/v1/openInterest", {"symbol": symbol})
    try:
        mark = _http_get(f"{FAPI_BASE}/fapi/v1/premiumIndex", {"symbol": symbol})
        oi_usd = float(oi_data["openInterest"]) * float(mark["markPrice"])
    except (KeyError, TypeError, ValueError):
        oi_usd = None  # 标记价拉取失败 → OI 缺失（宁缺毋错，不写张数当美元）

    # aggTrades（增量 CVD）
    params = {"symbol": symbol, "limit": 1000}
    if last_trade_id:
        params["fromId"] = last_trade_id + 1
    trades = _http_get(f"{FAPI_BASE}/fapi/v1/aggTrades", params)

    cvd = 0.0
    vol = 0.0
    max_id = last_trade_id
    for t in trades:
        qty = float(t["q"])
        price = float(t["p"])
        notional = qty * price
        is_buyer_maker = t["m"]  # True=卖主动（主动卖），False=买主动（主动买）
        cvd += notional if not is_buyer_maker else -notional
        vol += notional
        if t["a"] > max_id:
            max_id = t["a"]

    return {
        "symbol": symbol,
        "oi_usd": oi_usd,
        "cvd_5m": cvd,
        "vol_5m": vol,
        "last_trade_id": max_id,
    }


def _bucket_5m(dt: datetime) -> datetime:
    epoch = dt.timestamp()
    return datetime.fromtimestamp(epoch - epoch % BUCKET_SECONDS, tz=timezone.utc)


def task_scan_oi_cvd(min_vol_usd: float = 0, workers: int = 8) -> dict:
    """OI/CVD 5 分钟采样（单轮）。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    symbols = _get_usdt_perpetuals()
    if min_vol_usd:
        vol_map = _get_24h_quote_volume()
        symbols = [s for s in symbols if vol_map.get(s, 0) >= min_vol_usd]

    with _db() as conn:
        cursors = _load_cursors(conn)
        conn.commit()

    ts = _bucket_5m(datetime.now(timezone.utc))
    results: list[dict] = []
    errors = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_sample_symbol, s, cursors.get(s, 0)): s for s in symbols}
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception:
                errors += 1

    if results:
        rows = [(r["symbol"], ts, "binance", r["oi_usd"], r["cvd_5m"], None, r["vol_5m"])
                for r in results]
        state_rows = [(r["symbol"], r["last_trade_id"], datetime.now(timezone.utc))
                      for r in results]
        with _db() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO biz.oi_cvd_snapshot
                        (symbol, ts, exchange, oi_usd, cvd_5m_usd, cvd_1h_usd, vol_5m_usd)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (symbol, exchange, ts) DO UPDATE SET
                        oi_usd=EXCLUDED.oi_usd, cvd_5m_usd=EXCLUDED.cvd_5m_usd,
                        vol_5m_usd=EXCLUDED.vol_5m_usd
                    """,
                    rows,
                )
                cur.executemany(
                    """
                    INSERT INTO biz.scan_sampler_state (symbol, last_trade_id, updated_at)
                    VALUES (%s,%s,%s)
                    ON CONFLICT (symbol) DO UPDATE SET
                        last_trade_id=EXCLUDED.last_trade_id, updated_at=EXCLUDED.updated_at
                    """,
                    state_rows,
                )
                # 回填近 1h 的 cvd_1h
                cur.execute(
                    """
                    UPDATE biz.oi_cvd_snapshot s SET cvd_1h_usd = w.c1h
                    FROM (
                        SELECT symbol, ts,
                               SUM(cvd_5m_usd) OVER (
                                   PARTITION BY symbol ORDER BY ts
                                   ROWS BETWEEN 11 PRECEDING AND CURRENT ROW) AS c1h
                        FROM biz.oi_cvd_snapshot
                        WHERE ts >= NOW() - INTERVAL '2 hours'
                    ) w
                    WHERE s.symbol = w.symbol AND s.ts = w.ts
                      AND s.ts >= NOW() - INTERVAL '1 hour'
                    """,
                )
            conn.commit()

    return {"symbols": len(symbols), "results": len(results), "errors": errors, "bucket": ts.isoformat()}


# ═══════════════════════════════════════════════════════════════
#  任务 3：主池扫描（15 分钟）
# ═══════════════════════════════════════════════════════════════

PRICE_THR = {"5m": 1.5, "15m": 2.0, "1h": 3.0}
VOL_RATIO_THR = 2.0
LOOKBACK_BARS_MAIN = 20
OI_RISE_BARS = 2
LEVEL_RANK = {"5m": 1, "15m": 2, "1h": 3}

# ── 数据新鲜度护栏 ──────────────────────────────────────────────
# 各周期最新 K 线 open_time 距今最大分钟数（采集每 5min、扫描每 15min，留足余量；
# 超过即视为数据陈旧，跳过该币，避免采集停摆时用旧数据出假信号——见 2026-09-18 USELESS 事件）
MAX_KLINE_AGE_MIN = {"5m": 15, "15m": 35, "1h": 80}
# 最新 OI 桶 ts 距今最大分钟数（采样每 5min、错峰 +2min）
MAX_OI_BUCKET_AGE_MIN = 20
# 蓄势池：按小时聚合的 OI 桶允许的最大年龄（分钟）
MAX_OI_ACC_AGE_MIN = 90
# 采集停摆告警：数据年龄超过该分钟数即视为停摆；同一告警最短重发间隔（小时）
STALL_ALERT_AGE_MIN = 30
STALL_ALERT_MIN_INTERVAL_H = 6


def _build_regime(conn) -> dict:
    """L0 市场环境。返回 {tags: [...], long_fav: bool, short_fav: bool}。"""
    tags: list[str] = []
    long_fav = short_fav = True

    # BTC 1h 方向
    try:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT close_px, open_time FROM biz.asset_klines "
                "WHERE symbol='BTCUSDT' AND interval='1h' "
                "ORDER BY open_time DESC LIMIT 5"
            )
            closes = [float(r["close_px"]) for r in cur.fetchall()]
        if len(closes) >= 2:
            btc_1h = (closes[0] - closes[1]) / closes[1] * 100
            tags.append(f"btc_1h={'up' if btc_1h >= 0 else 'down'}({btc_1h:+.2f}%)")
            if btc_1h < -1.0:
                long_fav = False
            elif btc_1h > 1.0:
                short_fav = False
    except Exception:
        pass

    # 恐慌贪婪
    try:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT value, value_classification FROM biz.fear_greed_daily "
                "ORDER BY metric_date DESC LIMIT 1"
            )
            row = cur.fetchone()
        if row:
            fgi = float(row["value"])
            tags.append(f"fgi={fgi:.0f}({row['value_classification'] or '?'})")
            if fgi < 25:
                long_fav = False
            elif fgi > 75:
                short_fav = False
    except Exception:
        pass

    # 总市值趋势
    try:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT metric_date, total_market_cap FROM biz.global_metric_daily "
                "ORDER BY metric_date DESC LIMIT 2"
            )
            rows = cur.fetchall()
        if len(rows) >= 2 and rows[0]["total_market_cap"] and rows[1]["total_market_cap"]:
            trend = (float(rows[0]["total_market_cap"]) - float(rows[1]["total_market_cap"])) / float(rows[1]["total_market_cap"]) * 100
            tags.append(f"cap_trend={trend:+.2f}%")
            if trend < -1.0:
                long_fav = False
            elif trend > 1.0:
                short_fav = False
    except Exception:
        pass

    return {"tags": tags, "long_fav": long_fav, "short_fav": short_fav}


def _l1_screen(klines_by_iv: dict[str, list[dict]], now: datetime) -> dict | None:
    """L1 粗筛：单周期异动 + 多周期共振升级。

    新鲜度护栏：最新 K 线 open_time 距今超过 MAX_KLINE_AGE_MIN[iv] 则跳过该周期，
    避免采集停摆时用陈旧数据出假信号。
    """
    best = None
    best_level = 0
    for iv in ("1h", "15m", "5m"):
        rows = klines_by_iv.get(iv, [])
        if len(rows) < LOOKBACK_BARS_MAIN + 1:
            continue
        if (now - rows[-1]["open_time"]).total_seconds() / 60 > MAX_KLINE_AGE_MIN[iv]:
            continue  # 该周期数据陈旧，不参与判定
        closes = [float(r["close_px"]) for r in rows]
        vols = [float(r["quote_vol"]) for r in rows]
        chg = (closes[-1] - closes[-2]) / closes[-2] * 100
        vol_mean = sum(vols[-(LOOKBACK_BARS_MAIN + 1):-1]) / LOOKBACK_BARS_MAIN
        vol_ratio = vols[-1] / vol_mean if vol_mean else 0.0
        if abs(chg) >= PRICE_THR[iv] and vol_ratio >= VOL_RATIO_THR:
            level = LEVEL_RANK[iv]
            if level > best_level:
                best_level = level
                best = {
                    "iv": iv,
                    "dir": "up" if chg > 0 else "down",
                    "chg_pct": chg,
                    "vol_ratio": vol_ratio,
                    "level": level,
                }
    return best


def _compute_l2(symbol: str, oi_rows: list[dict], funding_map: dict[str, float],
                direction: str, now: datetime) -> dict | None:
    """L2 校验：OI 方向 + 资金费率拥挤度 → 场景判定。

    新鲜度护栏：最新 OI 桶 ts 距今超过 MAX_OI_BUCKET_AGE_MIN 则返回 None（数据陈旧）。
    """
    if len(oi_rows) < OI_RISE_BARS + 1:
        return None
    if (now - oi_rows[-1]["ts"]).total_seconds() / 60 > MAX_OI_BUCKET_AGE_MIN:
        return None  # OI 数据陈旧，不参与判定
    ois = [float(r["oi_usd"]) for r in oi_rows if r.get("oi_usd")]
    if len(ois) < OI_RISE_BARS + 1:
        return None
    oi_chg = (ois[-1] - ois[-1 - OI_RISE_BARS]) / ois[-1 - OI_RISE_BARS] * 100
    oi_dir = "up" if oi_chg > 0 else "down"

    cvds = [float(r["cvd_5m_usd"]) for r in oi_rows if r.get("cvd_5m_usd") is not None]
    cvd_sum = sum(cvds[-OI_RISE_BARS:]) if len(cvds) >= OI_RISE_BARS else (sum(cvds) if cvds else 0)
    cvd_dir = "up" if cvd_sum > 0 else "down"

    fr = funding_map.get(symbol)
    scenario = f"S{1 if oi_dir=='up' and direction=='up' else 2 if oi_dir=='up' and direction=='down' else 3 if oi_dir=='down' and direction=='up' else 4}"
    return {
        "oi_dir": oi_dir,
        "oi_chg_pct": oi_chg,
        "cvd_dir": cvd_dir,
        "funding_rate": fr,
        "scenario": scenario,
    }


def _symbol_fresh(sym: str, klines_by_iv: dict[str, list[dict]],
                  oi_rows: list[dict], now: datetime) -> bool:
    """该币数据新鲜度总检：任一周期最新 K 线或最新 OI 桶陈旧即视为不新鲜。

    供主池扫描做陈旧统计与提前跳过（L1/L2 内还有各自的兜底护栏）。
    """
    for iv, max_age in MAX_KLINE_AGE_MIN.items():
        rows = klines_by_iv.get(iv, [])
        if rows and (now - rows[-1]["open_time"]).total_seconds() / 60 > max_age:
            return False
    if oi_rows and (now - oi_rows[-1]["ts"]).total_seconds() / 60 > MAX_OI_BUCKET_AGE_MIN:
        return False
    return True


def _in_cooldown_main(conn, symbol: str, cooldown_h: float) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM biz.scan_signal WHERE symbol=%s AND pool='main' "
            "AND created_at > NOW() - make_interval(hours => (%s)::int) LIMIT 1",
            (symbol, cooldown_h),
        )
        return cur.fetchone() is not None


def task_scan_main_pool(cooldown_h: float = 6.0) -> dict:
    """主池扫描（单轮）。"""
    with _db() as conn:
        regime = _build_regime(conn)

        # 最近 1 天 K 线
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT symbol, interval, open_time, close_px, quote_vol "
                "FROM biz.asset_klines WHERE open_time >= NOW() - INTERVAL '1 day' "
                "ORDER BY symbol, interval, open_time"
            )
            k_rows = cur.fetchall()

        by_sym_k: dict[str, dict[str, list[dict]]] = {}
        for r in k_rows:
            by_sym_k.setdefault(r["symbol"], {}).setdefault(r["interval"], []).append(r)

        # OI/CVD 近 3h
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT symbol, ts, oi_usd, cvd_5m_usd FROM biz.oi_cvd_snapshot "
                "WHERE ts >= NOW() - INTERVAL '3 hours' ORDER BY symbol, ts"
            )
            oi_rows = cur.fetchall()
        by_sym_oi: dict[str, list[dict]] = {}
        for r in oi_rows:
            by_sym_oi.setdefault(r["symbol"], []).append(r)

        # funding
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("SELECT symbol, funding_rate FROM biz.asset_derivatives")
            funding_map = {r["symbol"]: float(r["funding_rate"]) for r in cur.fetchall()
                           if r["funding_rate"] is not None}

        now = datetime.now(timezone.utc)
        signals: list[tuple] = []
        stale_symbols = 0
        for sym in sorted(by_sym_k):
            # 新鲜度护栏：数据陈旧直接跳过（防采集停摆时用旧数据出假信号）
            if not _symbol_fresh(sym, by_sym_k[sym], by_sym_oi.get(sym, []), now):
                stale_symbols += 1
                continue
            l1 = _l1_screen(by_sym_k[sym], now)
            if not l1 or l1["level"] < 2:
                continue
            if cooldown_h > 0 and _in_cooldown_main(conn, sym, cooldown_h):
                continue
            l2 = _compute_l2(sym, by_sym_oi.get(sym, []), funding_map, l1["dir"], now)
            if not l2:
                continue

            direction = l1["dir"]
            if direction == "up" and l2["oi_dir"] == "up":
                confidence = "high" if regime["long_fav"] else "medium"
            elif direction == "up":
                confidence = "medium"
            else:
                confidence = "medium" if regime["short_fav"] else "low"

            ctx_tags = regime["tags"] + [f"lv{l1['level']}_{l1['iv']}"]
            signals.append((
                now, sym, l2["scenario"], l1["iv"], direction, round(l1["chg_pct"], 2),
                "up" if l1["vol_ratio"] >= VOL_RATIO_THR else "flat",
                round(l1["vol_ratio"], 2), l2["oi_dir"], round(l2["oi_chg_pct"], 2),
                l2["cvd_dir"], l2["funding_rate"] or 0, confidence, ctx_tags,
            ))

        if signals:
            insert_sql = """
                INSERT INTO biz.scan_signal
                    (signal_ts, symbol, pool, scenario, timeframe, p_dir, price_chg_pct,
                     vol_state, vol_ratio, oi_dir, oi_chg_pct, cvd_dir, funding_rate,
                     confidence, context_tags, status)
                VALUES (%s,%s,'main',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active')
            """
            with conn.cursor() as cur:
                cur.executemany(insert_sql, signals)
        conn.commit()

    total = len(by_sym_k)
    if total and stale_symbols / total > 0.2:
        print(f"[scan_daemon][main_pool] ⚠️ {stale_symbols}/{total} 币数据陈旧，"
              f"疑似采集停摆，本轮信号不完整", file=sys.stderr)

    return {"regime_tags": regime["tags"], "symbols_checked": total,
            "stale_skipped": stale_symbols, "signals": len(signals)}


# ═══════════════════════════════════════════════════════════════
#  任务 4：蓄势池扫描（30 分钟）
# ═══════════════════════════════════════════════════════════════

PRICE_QUIET_THR = 1.5
VOL_CAP_RATIO = 1.5
OI_HOURS = 12
OI_RISE_RATIO = 0.7
OI_CUM_CHG_PCT = 5.0
BRK_VOL_RATIO = 3.0
LOOKBACK_BARS_ACC = 20


def _detect_acc(symbol: str, oi_hourly: list[dict], k1h: list[dict],
                funding_map: dict[str, float], now: datetime) -> dict | None:
    if len(oi_hourly) < OI_HOURS:
        return None
    # 新鲜度护栏：1h K 线或小时聚合 OI 陈旧则跳过
    if k1h and (now - k1h[-1]["open_time"]).total_seconds() / 60 > MAX_KLINE_AGE_MIN["1h"]:
        return None
    if oi_hourly and (now - oi_hourly[-1]["h"]).total_seconds() / 60 > MAX_OI_ACC_AGE_MIN:
        return None
    if len(k1h) >= 2:
        chg1h = (k1h[-1]["close_px"] - k1h[-2]["close_px"]) / k1h[-2]["close_px"] * 100
        if abs(chg1h) >= PRICE_QUIET_THR:
            return None
    else:
        chg1h = None

    vols = [float(k["quote_vol"] or 0) for k in k1h]
    if len(vols) >= LOOKBACK_BARS_ACC + 1:
        vol_mean = sum(vols[-(LOOKBACK_BARS_ACC + 1):-1]) / LOOKBACK_BARS_ACC
        vol_ratio = vols[-1] / vol_mean if vol_mean else 0.0
        if vol_mean and vol_ratio >= VOL_CAP_RATIO:
            return None
    else:
        vol_ratio = None

    ois = [float(o["oi"]) for o in oi_hourly if o.get("oi") is not None]
    if len(ois) < OI_HOURS:
        return None
    rising = sum(1 for i in range(1, len(ois)) if ois[i] > ois[i - 1]) / (len(ois) - 1)
    cum_chg = (ois[-1] - ois[0]) / ois[0] * 100 if ois[0] else 0.0
    if rising < OI_RISE_RATIO or cum_chg <= OI_CUM_CHG_PCT:
        return None

    fr = funding_map.get(symbol)
    if fr is not None:
        if fr <= 0.0001:
            fund_label = "OI↑+费率偏低/负：偏逼空潜力"
        elif fr >= 0.0005:
            fund_label = "OI↑+费率偏高：警惕多头派发"
        else:
            fund_label = "OI↑+费率中性：多空均衡吸筹"
    else:
        fund_label = "费率无数据"
    return {"chg1h": chg1h, "vol_ratio": vol_ratio,
            "oi_rise_ratio": rising * 100, "oi_cum_chg": cum_chg, "fund_label": fund_label}


def _detect_brk(k1h: list[dict], acc_range: tuple[float, float], now: datetime) -> dict | None:
    if len(k1h) < LOOKBACK_BARS_ACC + 1:
        return None
    if (now - k1h[-1]["open_time"]).total_seconds() / 60 > MAX_KLINE_AGE_MIN["1h"]:
        return None  # 数据陈旧，不参与突破判定
    vols = [float(k["quote_vol"] or 0) for k in k1h]
    vol_mean = sum(vols[-(LOOKBACK_BARS_ACC + 1):-1]) / LOOKBACK_BARS_ACC
    if not vol_mean:
        return None
    vol_ratio = vols[-1] / vol_mean
    if vol_ratio < BRK_VOL_RATIO:
        return None
    lo, hi = acc_range
    close = float(k1h[-1]["close_px"])
    if close > hi:
        return {"dir": "up", "vol_ratio": vol_ratio, "break_px": close}
    if close < lo:
        return {"dir": "down", "vol_ratio": vol_ratio, "break_px": close}
    return None


def task_scan_accumulation() -> dict:
    """蓄势池 ACC/BRK 扫描（单轮）。"""
    with _db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT symbol, date_trunc('hour', ts) AS h, AVG(oi_usd) AS oi "
                "FROM biz.oi_cvd_snapshot WHERE ts >= NOW() - INTERVAL '13 hours' "
                "GROUP BY symbol, date_trunc('hour', ts) ORDER BY symbol, h"
            )
            oi_rows = cur.fetchall()
        by_sym_oi: dict[str, list[dict]] = {}
        for r in oi_rows:
            by_sym_oi.setdefault(r["symbol"], []).append(r)

        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT symbol, open_time, close_px, quote_vol FROM biz.asset_klines "
                "WHERE interval='1h' AND open_time >= NOW() - INTERVAL '1 day' "
                "ORDER BY symbol, open_time"
            )
            k_rows = cur.fetchall()
        by_sym_k: dict[str, list[dict]] = {}
        for r in k_rows:
            by_sym_k.setdefault(r["symbol"], []).append(r)

        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("SELECT symbol, funding_rate FROM biz.asset_derivatives")
            funding_map = {r["symbol"]: float(r["funding_rate"]) for r in cur.fetchall()
                           if r["funding_rate"] is not None}

        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT symbol FROM biz.scan_signal WHERE pool='accumulation' "
                "AND scenario='ACC' AND status='active' AND created_at > NOW() - INTERVAL '7 days'"
            )
            acc_symbols = {r["symbol"] for r in cur.fetchall()}

        now = datetime.now(timezone.utc)
        signals: list[tuple] = []
        for sym in sorted(by_sym_oi):
            acc = _detect_acc(sym, by_sym_oi[sym], by_sym_k.get(sym, []), funding_map, now)
            if not acc:
                continue
            tags = [f"oi_rise={acc['oi_rise_ratio']:.0f}%", f"oi_cum={acc['oi_cum_chg']:.1f}%",
                    acc["fund_label"]]
            signals.append((now, sym, "accumulation", "ACC", "1h", "flat",
                            acc["chg1h"] or 0, "flat", acc["vol_ratio"] or 0.0,
                            "up", acc["oi_cum_chg"], funding_map.get(sym), "medium", tags))

        for sym in sorted(acc_symbols):
            k1h = by_sym_k.get(sym, [])
            if len(k1h) < OI_HOURS + 1:
                continue
            lo = min(float(k["close_px"]) for k in k1h[-(OI_HOURS + 1):])
            hi = max(float(k["close_px"]) for k in k1h[-(OI_HOURS + 1):])
            brk = _detect_brk(k1h, (lo, hi), now)
            if not brk:
                continue
            tags = [f"brk_{brk['dir']}", f"vol_x={brk['vol_ratio']:.1f}"]
            signals.append((now, sym, "accumulation", "BRK", "1h", brk["dir"],
                            None, "up" if brk["vol_ratio"] >= VOL_CAP_RATIO else "flat",
                            round(brk["vol_ratio"], 2), None, None,
                            funding_map.get(sym), "high", tags))

        if signals:
            insert_sql = """
                INSERT INTO biz.scan_signal
                    (signal_ts, symbol, pool, scenario, timeframe, p_dir, price_chg_pct,
                     vol_state, vol_ratio, oi_dir, oi_chg_pct, funding_rate, confidence,
                     context_tags, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active')
            """
            with conn.cursor() as cur:
                cur.executemany(insert_sql, signals)
        conn.commit()

    acc_count = sum(1 for s in signals if s[2] == 'accumulation' and s[3] == 'ACC')
    brk_count = sum(1 for s in signals if s[2] == 'accumulation' and s[3] == 'BRK')
    return {"oi_symbols": len(by_sym_oi), "ACC": acc_count, "BRK": brk_count}


# ═══════════════════════════════════════════════════════════════
#  任务 5：告警监控（5 分钟，错峰 +3min）
# ═══════════════════════════════════════════════════════════════

NEW_WINDOW_MIN = 20
COOLDOWN_H = 12
CATALYST_DAYS = 7
KOL_DAYS = 7


def _load_alert_candidates(conn, window_min: int) -> list[dict]:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT id, signal_ts, symbol, pool, scenario, timeframe, p_dir, price_chg_pct,
                   vol_ratio, oi_dir, oi_chg_pct, cvd_dir, funding_rate, context_tags
            FROM biz.scan_signal
            WHERE confidence = 'high'
              AND (pool = 'main' OR (pool = 'accumulation' AND scenario = 'BRK'))
              AND alerted_at IS NULL
              AND signal_ts > NOW() - make_interval(mins => %s)
            ORDER BY signal_ts DESC
            """,
            (window_min,),
        )
        return cur.fetchall()


def _in_cooldown_alert(conn, symbol: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM biz.scan_signal "
            "WHERE symbol=%s AND confidence='high' AND alerted_at IS NOT NULL "
            "AND alerted_at > NOW() - make_interval(hours => %s) LIMIT 1",
            (symbol, COOLDOWN_H),
        )
        return cur.fetchone() is not None


def _get_asset_id(conn, symbol: str) -> int | None:
    with conn.cursor() as cur:
        cur.execute("SELECT asset_id FROM core.asset WHERE canonical_symbol = %s", (symbol,))
        r = cur.fetchone()
        return r[0] if r else None


def _get_resonance(conn, symbol: str, asset_id: int | None) -> dict:
    """返回 {event: [...], catalyst: [...], kol: [...]} 三段共振信息。"""
    out: dict = {"event": [], "catalyst": [], "kol": []}
    # 1) 事件预置（领先型）
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "SELECT event_type, event_date, event_pct, detail FROM biz.event_watchlist "
            "WHERE symbol = %s", (symbol,))
        for r in cur.fetchall():
            out["event"].append(
                f"{'🔓解锁' if r['event_type'] == 'unlock' else '🔄链上转账'}: {r['detail']}")

    if not asset_id:
        return out
    # 2) 催化剂（已发布，确认型）
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT ac.title, ac.published_at, ci.impact_direction, ci.impact_strength
            FROM biz.catalyst_impact ci
            JOIN biz.asset_catalyst ac ON ac.catalyst_id = ci.catalyst_id
            WHERE ci.asset_id = %s AND ac.published_at > NOW() - make_interval(days => %s)
            ORDER BY ac.published_at DESC LIMIT 4
            """,
            (asset_id, CATALYST_DAYS),
        )
        for r in cur.fetchall():
            tag = f"{r['impact_direction']}/{r['impact_strength']}"
            out["catalyst"].append(
                f"{r['title'][:60]}（{tag}，{str(r['published_at'])[:10]}）")
    # 3) KOL 预测（滞后确认型）
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT direction, symbol, confidence, created_at
            FROM biz.kol_signal
            WHERE asset_id = %s AND post_type = 'prediction'
              AND created_at > NOW() - make_interval(days => %s)
            ORDER BY created_at DESC LIMIT 4
            """,
            (asset_id, KOL_DAYS),
        )
        for r in cur.fetchall():
            out["kol"].append(
                f"KOL {r['direction']} {r['symbol']} (conf {float(r['confidence']):.2f}, {str(r['created_at'])[:10]})")
    return out


def _render_alert_email(items: list[dict]) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    body_parts = []
    for it in items:
        sig = it["signal"]
        res = it["resonance"]
        pool_label = "蓄势池BRK" if sig["pool"] == "accumulation" else "主池"
        dir_label = "↑ 做多" if sig["p_dir"] == "up" else "↓ 做空"
        tags = sig.get("context_tags") or []
        if isinstance(tags, list):
            tag_str = " ".join(str(t) for t in tags[:5])
        else:
            tag_str = str(tags)[:50]
        body_parts.append(
            f"<div style='margin:8px 0;padding:10px;border-left:4px solid "
            f"{'#22c55e' if sig['p_dir']=='up' else '#ef4444'};background:#f9fafb'>"
            f"<b>{sig['symbol']}</b> {pool_label} {sig['scenario']} {dir_label} "
            f"({sig['timeframe']}, {sig['price_chg_pct']:+.2f}%)<br>"
            f"<small>OI: {sig.get('oi_dir','?')} {sig.get('oi_chg_pct',0):+.1f}% | "
            f"共振: 事件{len(res['event'])} 催化剂{len(res['catalyst'])} KOL{len(res['kol'])}</small><br>"
            f"<small style='color:#666'>{tag_str}</small>"
            f"</div>"
        )
    body = "".join(body_parts)
    footnote = "<p style='color:#999;font-size:12px'>本邮件为盘面数据分析参考，不构成投资建议。</p>"
    return (f"<html><body style='font-family:Arial,\"Microsoft YaHei\",sans-serif'>"
            f"<h2 style='margin:0'>🚨 盘面异动告警</h2>"
            f"<p style='margin:0 0 8px;color:#666;font-size:13px'>生成于 {now} · 共 {len(items)} 个币</p>"
            f"{body}{footnote}</body></html>")


def _check_and_alert_stall(conn) -> bool:
    """采集停摆检测：15m K 线 / OI 采样数据年龄超过阈值 → 发告警邮件。

    去重：同一告警 STALL_ALERT_MIN_INTERVAL_H 小时内不重发（biz.scan_stall_alert）。
    返回 True 表示当前处于（或刚触发）停摆状态。
    """
    try:
        now = datetime.now(timezone.utc)
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT MAX(open_time) AS mx FROM biz.asset_klines WHERE interval='15m'")
            mx_k = cur.fetchone()["mx"]
            cur.execute(
                "SELECT MAX(ts) AS mx FROM biz.oi_cvd_snapshot WHERE exchange='binance'")
            mx_oi = cur.fetchone()["mx"]
            cur.execute(
                "SELECT last_email_ts FROM biz.scan_stall_alert WHERE task='stall_alert'")
            row = cur.fetchone()

        parts = []
        for label, mx in (("15m K线", mx_k), ("OI 采样", mx_oi)):
            if mx is None:
                continue
            age_min = (now - mx).total_seconds() / 60
            if age_min > STALL_ALERT_AGE_MIN:
                parts.append(f"{label} 停在 {mx.strftime('%m-%d %H:%M')} UTC（约 {age_min:.0f} 分钟前）")
        if not parts:
            return False

        last = row["last_email_ts"] if row else None
        if last and (now - last).total_seconds() < STALL_ALERT_MIN_INTERVAL_H * 3600:
            return True  # 仍处于停摆，但刚告警过（去重）

        settings = _SETTINGS or get_settings(require_database=True)
        from crypto_research.clients.notifier import EmailNotifier
        notifier = EmailNotifier(settings)
        if not notifier.configured:
            print("[WARN] SMTP 未配置，跳过采集停摆告警邮件")
            return True

        body = (
            "<h2 style='margin:0'>⚠️ 盘面扫描数据采集停摆告警</h2>"
            f"<p>采集数据已超过 <b>{STALL_ALERT_AGE_MIN} 分钟</b>未更新，"
            f"主池/蓄势池扫描已暂停出信号（防止陈旧数据假信号）。</p>"
            f"<p>{'<br>'.join(parts)}</p>"
            "<p style='color:#999'>请检查 scan_daemon 进程 / Binance IP 限频状态，"
            "采集恢复后自动解除。</p>"
        )
        ok, msg = notifier.send(
            "⚠️ 盘面扫描数据采集停摆告警", body, from_name="盘面信号扫描")
        if ok:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO biz.scan_stall_alert (task, last_email_ts, updated_at) "
                    "VALUES ('stall_alert', NOW(), NOW()) "
                    "ON CONFLICT (task) DO UPDATE SET "
                    "last_email_ts=EXCLUDED.last_email_ts, updated_at=EXCLUDED.updated_at")
            conn.commit()
            print("[scan_daemon][stall] 已发送采集停摆告警邮件")
        else:
            print(f"[scan_daemon][stall] 停摆告警邮件发送失败: {msg}", file=sys.stderr)
        return True
    except Exception as e:
        print(f"[scan_daemon][stall] 停摆检测异常: {e}", file=sys.stderr)
        return False


def task_scan_alert(window_min: int = NEW_WINDOW_MIN) -> dict:
    """告警监控（单轮）。"""
    with _db() as conn:
        _check_and_alert_stall(conn)
        candidates = _load_alert_candidates(conn, window_min)
        seen: set[str] = set()
        to_alert: list[dict] = []
        for c in candidates:
            if c["symbol"] in seen or _in_cooldown_alert(conn, c["symbol"]):
                continue
            seen.add(c["symbol"])
            to_alert.append({
                "signal": c,
                "resonance": _get_resonance(conn, c["symbol"], _get_asset_id(conn, c["symbol"])),
            })

        if not to_alert:
            return {"candidates": len(candidates), "alerts": 0}

        html = _render_alert_email(to_alert)
        from crypto_research.clients.notifier import EmailNotifier
        settings = _SETTINGS or get_settings(require_database=True)
        notifier = EmailNotifier(settings)
        if not notifier.configured:
            print("[WARN] SMTP 未配置，跳过发送")
            return {"candidates": len(candidates), "alerts": 0, "note": "smtp_not_configured"}

        ok, msg = notifier.send(
            f"🚨 盘面异动告警：{len(to_alert)} 币高置信信号（含共振）",
            html,
            from_name="盘面信号扫描",
        )
        if ok:
            ids = [it["signal"]["id"] for it in to_alert]
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE biz.scan_signal SET alerted_at = NOW() WHERE id = ANY(%s)",
                    (ids,),
                )
            conn.commit()
            return {"candidates": len(candidates), "alerts": len(ids)}
        else:
            print(f"[alert] 发送失败: {msg}")
            return {"candidates": len(candidates), "alerts": 0, "error": msg}


# ═══════════════════════════════════════════════════════════════
#  任务 6：Watchlist 监控（30 分钟）
# ═══════════════════════════════════════════════════════════════

def task_watchlist_monitor() -> dict:
    """解锁/空头/大户监控（单轮）。
    
    为减少重复实现，直接调用原脚本的 run_once 函数。
    """
    # 动态导入原脚本的 run_once
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "watchlist_mod", SCRIPT_DIR / "phase_watchlist_monitor.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore

    settings = _SETTINGS or get_settings(require_database=True)
    mod.run_once(settings)
    return {"status": "ok"}


# ═══════════════════════════════════════════════════════════════
#  守护进程调度框架
# ═══════════════════════════════════════════════════════════════

def _run_task_loop(name: str, interval_sec: int, func, offset_sec: int = 0,
                   func_kwargs: dict | None = None):
    """单个任务的常驻循环。
    
    - 首次等待 offset_sec 秒（错峰）
    - 每 interval_sec 执行一次
    - SkipIfRunning：上一轮未结束则跳过
    - 单轮异常不影响下一轮
    """
    func_kwargs = func_kwargs or {}
    time.sleep(offset_sec)  # 初始错峰

    running = False
    round_count = 0
    while True:
        round_count += 1
        start_ts = time.time()

        if running:
            print(f"[scan_daemon][{name}] 跳过第 {round_count} 轮（上一轮仍在运行）")
            time.sleep(interval_sec)
            continue

        running = True
        try:
            print(f"[scan_daemon][{name}] 第 {round_count} 轮开始")
            result = func(**func_kwargs)
            elapsed = time.time() - start_ts
            print(f"[scan_daemon][{name}] 第 {round_count} 轮完成，耗时 {elapsed:.1f}s，结果: {result}")
        except Exception as e:
            elapsed = time.time() - start_ts
            print(f"[scan_daemon][{name}] 第 {round_count} 轮异常 ({elapsed:.1f}s): {e}",
                  file=sys.stderr)
            traceback.print_exc()
        finally:
            running = False

        # 计算下一轮等待时间（扣除本轮耗时，保持固定节奏）
        elapsed = time.time() - start_ts
        sleep_time = max(1.0, interval_sec - elapsed)
        time.sleep(sleep_time)


# 任务定义：(name, interval_sec, offset_sec, func, kwargs)
TASK_DEFS = [
    ("scan_klines",       300,  0,  task_scan_klines,       {"min_vol_usd": 5_000_000}),
    ("scan_oi_cvd",       300,  120, task_scan_oi_cvd,      {"min_vol_usd": 5_000_000}),
    ("scan_alert",        300,  180, task_scan_alert,       {}),
    ("scan_main_pool",    900,  60,  task_scan_main_pool,    {}),
    ("scan_accumulation", 1800, 0,  task_scan_accumulation, {}),
    ("watchlist_monitor", 1800, 300, task_watchlist_monitor, {}),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="盘面异动扫描守护进程（多任务单进程）")
    parser.add_argument("--run-once", metavar="TASK", help="只跑一次指定任务（调试）")
    parser.add_argument("--only", default="",
                        help="只启动指定任务（逗号分隔，如 klines,oi）")
    parser.add_argument("--min-vol-usd", type=float, default=5_000_000,
                        help="仅采集 24h 成交额 ≥ 该值的合约（默认 500 万 USDT）")
    parser.add_argument("--rate-limit", type=float, default=0.0,
                        help="全局 Binance API 请求最小间隔秒（默认 0=不限速，"
                             "共享 IP 环境建议设 0.2-0.5）")
    args = parser.parse_args()

    # 全局限速配置（转发到 binance_http 公共模块）
    global _MIN_HTTP_GAP
    _MIN_HTTP_GAP = args.rate_limit
    set_min_request_gap(args.rate_limit)

    # 初始化共享资源
    settings, db_pool = _init()
    print(f"[scan_daemon] 初始化完成，DB 池大小={db_pool.max_size}，"
          f"min_vol_usd={args.min_vol_usd:,}，"
          f"rate_limit={args.rate_limit or 'unlimited'}")

    # 更新采集类任务的过滤参数
    for _name, _iv, _off, _func, kwargs in TASK_DEFS:
        if _name in ("scan_klines", "scan_oi_cvd") and "min_vol_usd" in kwargs:
            kwargs["min_vol_usd"] = args.min_vol_usd

    # 单跑模式
    if args.run_once:
        for name, _iv, _off, func, kwargs in TASK_DEFS:
            if name == args.run_once:
                start = time.time()
                result = func(**kwargs)
                print(f"[scan_daemon][{name}] 单次运行完成，耗时 {time.time()-start:.1f}s，结果: {result}")
                return 0
        print(f"未知任务: {args.run_once}（可用: {', '.join(t[0] for t in TASK_DEFS)}）")
        return 1

    # 过滤任务
    only = [x.strip() for x in args.only.split(",") if x.strip()]
    tasks_to_run = [t for t in TASK_DEFS if not only or t[0] in only]
    if not tasks_to_run:
        print("没有可运行的任务")
        return 1

    print(f"[scan_daemon] 启动 {len(tasks_to_run)} 个任务: {', '.join(t[0] for t in tasks_to_run)}")

    # 每个任务一个线程
    threads: list[threading.Thread] = []
    for name, interval, offset, func, kwargs in tasks_to_run:
        t = threading.Thread(
            target=_run_task_loop,
            args=(name, interval, func, offset, kwargs),
            daemon=True,
            name=f"scan_{name}",
        )
        t.start()
        threads.append(t)
        print(f"[scan_daemon] 启动任务 {name}（间隔 {interval}s，错峰 {offset}s）")

    # 主线程等待（Ctrl+C 退出）
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("\n[scan_daemon] 收到退出信号，正在停止...")
        return 0


if __name__ == "__main__":
    sys.exit(main())
