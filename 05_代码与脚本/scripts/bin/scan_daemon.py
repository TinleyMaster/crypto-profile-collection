#!/usr/bin/env python3
"""
盘面异动扫描守护进程（scan_daemon）。

将原先由 scheduler 每 5/15/30 分钟调度的多个扫描脚本合并为单进程常驻，
共享 DB 连接池、Binance API session 和币对列表，显著降低内存和启动开销。

包含任务（按调度周期）：
  - [5min]   scan_klines       — Binance USDT 永续 K 线增量（5m/15m/1h）
  - [5min]   scan_oi_cvd       — OI/CVD 5 分钟采样（错峰 +2min）
  - [5min]   scan_liquidation  — CoinGlass 爆仓滚动窗口快照（错峰 +4min）
  - [5min]   scan_alert        — 高置信信号告警（错峰 +3min）
  - [5min]   scan_squeeze      — 轧空 扫描/跟踪/判定（错峰 +7min，仅告警不下单）
  - [15min]  scan_main_pool    — 主池扫描 L0+L1+L2
  - [30min]  scan_accumulation — 蓄势池 ACC/BRK
  - [30min]  watchlist_monitor — 解锁/空头/大户监控
  - [30min]  expire_signals    — 信号生命周期巡检（active → expired）
  - [24h]    prune_scan_data   — 采集数据保留期清理（保留 30 天）

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
import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg.rows  # noqa: E402
import psycopg_pool  # noqa: E402

from crypto_research.analysis import squeeze as sqz  # noqa: E402
from crypto_research.clients.binance_http import fapi_get, set_min_request_gap  # noqa: E402
from crypto_research.clients.coinglass_client import CoinGlassClient  # noqa: E402
from crypto_research.config import get_settings  # noqa: E402

# ── 日志流韧性（审计 P0-A 根因修复） ────────────────────────────


class _ResilientStream:
    """stdout/stderr 代理：底层流被关闭/断开时吞掉写入错误。

    容器日志设施（Zeabur 日志管道）断开会把子进程的 stdout 变成「已关闭文件」，
    此后任何 ``print`` 都抛 ``ValueError: I/O operation on closed file.``。
    原实现把每轮第一行 print 放在 ``try`` 内、``func()`` 之前，于是「打印失败」
    被 ``except`` 吞成「单轮失败」，``func()`` 从未执行 → K线/OI/信号全线静默停产，
    而心跳（只走 DB）照常推进（2026-09-21 停摆 48 分钟，审计 P0-A）。

    此代理让日志故障与业务彻底解耦：写失败静默丢弃，绝不向上抛异常。
    """

    def __init__(self, inner):
        self._inner = inner

    def write(self, s):
        try:
            return self._inner.write(s)
        except Exception:  # noqa: BLE001
            return len(s)

    def flush(self):
        try:
            self._inner.flush()
        except Exception:  # noqa: BLE001
            pass

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    @property
    def closed(self) -> bool:
        # 恒为 False：底层流即便被关闭，本代理仍可安全 write（写入被丢弃）
        return False

    def __getattr__(self, name):
        # fileno / reconfigure / encoding / errors 等一律转发给底层流
        return getattr(self._inner, name)


def _harden_streams() -> None:
    """把 stdout/stderr 换成韧性代理（幂等，重复调用无副作用）。"""
    if not isinstance(sys.stdout, _ResilientStream):
        sys.stdout = _ResilientStream(sys.stdout)
    if not isinstance(sys.stderr, _ResilientStream):
        sys.stderr = _ResilientStream(sys.stderr)


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


# ── 跨表符号归一（唯一入口，禁止各查各的） ──────────────────────

def _symbol_candidates(symbol: str) -> list[str]:
    """Binance 永续合约符号 → 本库可能使用的符号候选（由最贴切到最宽松）。

    扫描侧一律用合约符号（'B2USDT'），而 DB 侧关联表一律存裸符号：
    core.asset / biz.event_watchlist / biz.asset_derivatives 全部如此。
    不做归一化会让事件/催化剂/KOL/资金费率四条关联链**恒空**（2026-09-21 审计 P0-1）。

    两类后缀/前缀必须处理：
      - 结算后缀 'USDT'：'B2USDT' → 'B2'；
      - Binance 的 1000X / 1000000X 合约代表裸币 X（'1000FLOKIUSDT' → 'FLOKI'），
        本库关联表存的是裸 X（实测 asset_derivatives / event_watchlist 里
        一个 1000 前缀符号都没有），而 core.asset 对 1000FLOKI/1000PEPE
        根本没有对应行 → 只剥 USDT 时这两条链依然全断（审计 P1-N2）。
    """
    s = (symbol or "").upper()
    base = s[:-4] if s.endswith("USDT") and len(s) > 4 else s
    out = [s, base]                          # 原样 → 去结算后缀
    for pre in ("1000000", "1000"):          # 倍数前缀只从裸形态剥离
        if base.startswith(pre) and len(base) > len(pre):
            out.append(base[len(pre):])
            break
    return list(dict.fromkeys(out))


def _lookup_funding(funding_map: dict[str, float], symbol: str) -> float | None:
    """按候选序列在资金费率表里取值（未命中返回 None，不写 0）。"""
    for cand in _symbol_candidates(symbol):
        if cand in funding_map:
            return funding_map[cand]
    return None


def _load_funding_map(conn) -> dict[str, float]:
    """资金费率表：裸符号 → 费率。

    - biz.asset_derivatives.symbol 存裸符号（'B2'），故 key 归一为裸符号；
      同时把候选别名一并注册（'BTCUSDT' 行也注册 'BTC'），使查询侧只需遍历候选；
    - 同符号存在多行历史快照（BTC 12 行），无 ORDER BY 时取到哪条不确定，
      故用 DISTINCT ON 取 fetched_at 最新的一条（审计 P2-4）。
    """
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "SELECT DISTINCT ON (symbol) symbol, funding_rate "
            "FROM biz.asset_derivatives WHERE funding_rate IS NOT NULL "
            "ORDER BY symbol, fetched_at DESC"
        )
        out: dict[str, float] = {}
        for r in cur.fetchall():
            rate = float(r["funding_rate"])
            for cand in _symbol_candidates(r["symbol"]):
                out[cand] = rate
        return out


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


def _fetch_agg_trades(symbol: str, last_trade_id: int) -> list:
    """拉取增量 aggTrades；游标失效时回退为「最新 1000 笔」重同步。

    Binance aggTrades 只保留近期成交，采样中断超过保留窗口后，用过期 fromId 请求
    会返回 400（参数非法）。此时不带 fromId 拉最新 1000 笔，让游标跳到当前值，
    下一轮起恢复增量。注意：该次重同步桶的 cvd_5m_usd / vol_5m_usd 只覆盖部分
    窗口（偏小），属一次性重同步产物。
    """
    params = {"symbol": symbol, "limit": 1000}
    if last_trade_id:
        try:
            return _http_get(f"{FAPI_BASE}/fapi/v1/aggTrades",
                             {**params, "fromId": last_trade_id + 1})
        except Exception as e:
            print(f"[scan_daemon][oi_cvd] {symbol} aggTrades 游标 {last_trade_id} 失效"
                  f"（{type(e).__name__}），回退最新成交重同步", file=sys.stderr)
    return _http_get(f"{FAPI_BASE}/fapi/v1/aggTrades", params)


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
    trades = _fetch_agg_trades(symbol, last_trade_id)

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
    err_samples: list[str] = []  # 保留前几条错误原文，避免异常被静默吞掉无法定位
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_sample_symbol, s, cursors.get(s, 0)): s for s in symbols}
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:
                errors += 1
                if len(err_samples) < 5:
                    err_samples.append(f"{futures[fut]}: {type(e).__name__}: {str(e)[:160]}")

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

    if err_samples:
        print(f"[scan_daemon][oi_cvd] {errors}/{len(symbols)} 个币采样失败，样例："
              + " | ".join(err_samples), file=sys.stderr)

    return {"symbols": len(symbols), "results": len(results), "errors": errors,
            "bucket": ts.isoformat(), "error_samples": err_samples}


# ═══════════════════════════════════════════════════════════════
#  任务 3：主池扫描（15 分钟）
# ═══════════════════════════════════════════════════════════════

# 三周期各自独立成通道（_l1_screen 取 level 最高者，level < 2 才被丢弃）。
# ⚠️ 1h 的 3.0 是**线上口径，刻意不改**（2026-09-21 决策，见设计文档 §4.2 / §12.1-14）：
#   回测 backtest_scan_scenarios.py 的 PRICE_THR_1H=4.0 是「单周期 1h 单根」口径（无 5m/15m 通道），
#   与这里「三周期并列、15m 2.0% 也能独立入池」不是同一个量。把本值改成 4.0 属伪对齐——
#   回测里被 4.0 滤掉的「1h 涨 3~4%」样本，在线上多数已被 15m 通道先捕获，改了并不削减那批样本。
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
# 任务心跳停摆：心跳年龄 > N × 任务周期 即视为该任务停产（与外部看门狗
# check_scan_freshness.py 的 HEARTBEAT_MAX_AGE_MIN 保持同一口径：3× 周期）
STALL_HEARTBEAT_GRACE = 3
# 参与心跳检查的任务（审计 P0-B：原先漏了 squeeze/liquidation/watchlist，
# 而 2026-09-21 停摆中唯一留下物证的恰恰是盲区里的 scan_squeeze）。必须与
# TASK_DEFS 全覆盖（阈值由各自周期 ×STALL_HEARTBEAT_GRACE 自动推导）。
STALL_HEARTBEAT_TASKS = ("scan_klines", "scan_oi_cvd", "scan_liquidation", "scan_alert",
                         "scan_squeeze", "scan_main_pool", "scan_accumulation",
                         "watchlist_monitor", "expire_signals", "prune_scan_data")
# 连续失败自杀：同一任务连续 N 轮**连心跳都写不进 DB** 即退出进程，交 supervisord
# 拉起（审计 P1-4）。半死进程（stdout 关闭/线程卡死）会一直占着单实例锁，新实例
# 永远起不来 —— 这是 2026-09-18 63h、2026-09-21 48min 两次停摆的共同放大器。
# ⚠️ 判据是「进程级故障」而不是「任务失败」（审计复验 P1-1）：外部依赖抖动
# （Binance/CoinGlass 限频、超时）只记 last_error，绝不重启进程，否则会把一次
# API 抖动放大成全局停产（见 _run_task_loop 注释）。
MAX_CONSEC_FAILURES = 3
# 单实例锁取锁重试（审计复验 P1-3）：os._exit(1) 后 supervisord 立即拉起新实例，
# 旧实例的锁连接 TCP 释放通常 <1s，但撞上窗口就取不到锁 → main() 返回 1 且耗时
# < startsecs=10 → supervisord 计为「启动失败」，连续 3 次即 FATAL 且不再拉起
# （autorestart 对 FATAL 无效）。故取锁失败先等一会儿重试。
LOCK_ACQUIRE_RETRIES = 3
LOCK_ACQUIRE_RETRY_WAIT_SEC = 2.0
# 取锁失败后，进程必须至少运行到 STARTUP_MIN_SEC 秒才退出（审计复验 FIX-061）。
# 仅靠「重试次数 × 间隔」不足以保证越过 startsecs ——
# 当前 3×2s 实测退出耗时 4.00s（端到端 6.66s）< supervisord 的 startsecs=10，
# 仍会被计为「启动失败」，耗尽 startretries 后进入 FATAL（autorestart 无效）。
# ⚠️ 本值必须 **严格大于** workbench/supervisord.conf 中
#    [program:scan_daemon].startsecs（当前 10s）；改任一值须同步核对另一个。
STARTUP_MIN_SEC = 12.0
# 与 supervisord.conf 的 startsecs 耦合，改一个必须改另一个（见上）。
# 若配置被调大而未同步本值，这里会立刻暴露而不是在部署窗口静默 FATAL。
assert STARTUP_MIN_SEC > 10, "STARTUP_MIN_SEC 必须 > supervisord startsecs"
# 僵尸实例判定：锁被占用但心跳已停止推进超过该分钟数
SINGLETON_ZOMBIE_GRACE_MIN = 10
# 进程启动标记：main() 取得单实例锁后写一条 last_run_at=进程启动时刻的心跳，
# 供停摆检测区分「线程从未启动」与「本实例刚重启、首轮还没跑完」——
# 后者若按普通心跳判据会在每次部署后误报一次停摆告警。
DAEMON_START_TASK = "__daemon__"

# 本进程实际调度的任务名（main() 启动时填充，停摆检测只检查这些任务）
_SCHEDULED_TASKS: set[str] = set()


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
    # 方向保持二值（与 backtest_scan_scenarios.py 的 oi_dir 口径一致，2026-09-21 已核对），
    # 不引入幅度阈值——否则线上与「P↑OI↑ 唯一稳定正期望」的回测分组口径不一致。
    oi_dir = "up" if oi_chg > 0 else "down"

    # CVD：无数据时写 NULL（未知），**不得** fallback 成某个方向（审计 P1-2：
    # 09-19/09-20 cvd 全为 NULL 时被误判成 'down'，把「无数据」当成了「空头方向」）
    cvds = [float(r["cvd_5m_usd"]) for r in oi_rows if r.get("cvd_5m_usd") is not None]
    if not cvds:
        cvd_dir = None
    else:
        cvd_sum = sum(cvds[-OI_RISE_BARS:]) if len(cvds) >= OI_RISE_BARS else sum(cvds)
        cvd_dir = "up" if cvd_sum > 0 else "down" if cvd_sum < 0 else None

    fr = _lookup_funding(funding_map, symbol)
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

        # OI/CVD 近 3h（只取实时 5m 采样：1h 历史回填行会污染「近 2 桶 OI 变化」的计算）
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT symbol, ts, oi_usd, cvd_5m_usd FROM biz.oi_cvd_snapshot "
                "WHERE source='realtime' AND ts >= NOW() - INTERVAL '3 hours' "
                "ORDER BY symbol, ts"
            )
            oi_rows = cur.fetchall()
        by_sym_oi: dict[str, list[dict]] = {}
        for r in oi_rows:
            by_sym_oi.setdefault(r["symbol"], []).append(r)

        funding_map = _load_funding_map(conn)

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
            # 多空**对称**通道（审计 P1-1）：原实现 down 最高只能到 medium，而
            # `_load_alert_candidates` 只要 high ⇒ 做空通道数学上不可达、下跌行情
            # 一封邮件都收不到。S3（价跌 + OI 增 = 空头扎实）与 S1 同构，同样可达 high。
            if direction == "up" and l2["oi_dir"] == "up":
                confidence = "high" if regime["long_fav"] else "medium"
            elif direction == "up":
                confidence = "medium"
            elif l2["oi_dir"] == "up":
                confidence = "high" if regime["short_fav"] else "medium"
            else:
                confidence = "medium" if regime["short_fav"] else "low"

            ctx_tags = regime["tags"] + [f"lv{l1['level']}_{l1['iv']}"]
            signals.append((
                now, sym, l2["scenario"], l1["iv"], direction, round(l1["chg_pct"], 2),
                "up" if l1["vol_ratio"] >= VOL_RATIO_THR else "flat",
                round(l1["vol_ratio"], 2), l2["oi_dir"], round(l2["oi_chg_pct"], 2),
                l2["cvd_dir"], l2["funding_rate"], confidence, ctx_tags,
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
# ACC 冷却小时：同币已有 active ACC 信号且在冷却期内则不再重复入池。
# 审计 §12.1-5：原先每轮无条件 INSERT，实测 807 行只有 123 个不同币（6.6× 重复，
# JUPUSDT×26 / BNBUSDT×20），既稀释统计又让「蓄势中」的币被反复推送。
ACC_COOLDOWN_H = 24
# BRK 候选窗口：近 N 天内 active 的 ACC 信号才有资格升级突破
ACC_BRK_CANDIDATE_DAYS = 7


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

    fr = _lookup_funding(funding_map, symbol)
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
            # 此处**刻意不过滤 source**（与新鲜度判断、主池 OI 变化不同）：
            # 蓄势池 ACC 需要「连续 ≥12 个小时桶」，而实时 5m 采样在停摆/部署窗口
            # 会断行，1h 回填行正好补上这些小时桶（同一小时两者 OI 值相近，
            # AVG 混合误差可接受）。过滤掉 backfill 会让停摆后 ACC 长时间凑不满桶。
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

        funding_map = _load_funding_map(conn)

        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            # BRK 候选：近 ACC_BRK_CANDIDATE_DAYS 天内 active 的 ACC 信号
            cur.execute(
                "SELECT symbol FROM biz.scan_signal WHERE pool='accumulation' "
                "AND scenario='ACC' AND status='active' "
                "AND created_at > NOW() - make_interval(days => (%s)::int)",
                (ACC_BRK_CANDIDATE_DAYS,),
            )
            acc_symbols = {r["symbol"] for r in cur.fetchall()}
            # ACC 冷却：冷却期内已有 active ACC 的币不再重复入池（审计 §12.1-5）
            cur.execute(
                "SELECT DISTINCT symbol FROM biz.scan_signal WHERE pool='accumulation' "
                "AND scenario='ACC' AND status='active' "
                "AND created_at > NOW() - make_interval(hours => (%s)::int)",
                (ACC_COOLDOWN_H,),
            )
            acc_cooldown = {r["symbol"] for r in cur.fetchall()}

        now = datetime.now(timezone.utc)
        signals: list[tuple] = []
        acc_skipped_cooldown = 0
        for sym in sorted(by_sym_oi):
            if sym in acc_cooldown:
                acc_skipped_cooldown += 1
                continue
            acc = _detect_acc(sym, by_sym_oi[sym], by_sym_k.get(sym, []), funding_map, now)
            if not acc:
                continue
            tags = [f"oi_rise={acc['oi_rise_ratio']:.0f}%", f"oi_cum={acc['oi_cum_chg']:.1f}%",
                    acc["fund_label"]]
            signals.append((now, sym, "accumulation", "ACC", "1h", "flat",
                            acc["chg1h"] or 0, "flat", acc["vol_ratio"] or 0.0,
                            "up", acc["oi_cum_chg"], _lookup_funding(funding_map, sym), "medium", tags))

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
                            _lookup_funding(funding_map, sym), "high", tags))

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
    return {"oi_symbols": len(by_sym_oi), "ACC": acc_count, "BRK": brk_count,
            "acc_skipped_cooldown": acc_skipped_cooldown}


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
              AND status = 'active'
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
    """合约符号 → asset_id。按候选序列逐个查（原样 → 去 USDT → 去倍数前缀）。

    core.asset.canonical_symbol 存裸符号（'B2'/'FLOKI'），而扫描侧传的是
    'B2USDT'/'1000FLOKIUSDT' —— 不归一化会返回 None，使催化剂/KOL 两段
    共振被短路（审计 P0-1 / P1-N2）。

    ⚠️ canonical_symbol **不唯一**（1,866 个符号有重复，单符号最多 18 行，
    PEPE/BTC/ETH 等热门币全在列），不加定序的 fetchone() 可能取到**别的币**
    → 催化剂/KOL 共振张冠李戴（审计 P1-N3），比"无共振"更危险。
    故按 market_cap_rank 升序取主流那条（实测 PEPE→3978 / BTC→2 / FLOKI→2553）。
    注意候选序列是「原样优先」：'1000SHIBUSDT' 会先命中 core.asset 里的 1000SHIB
    孤条目（12559），而非裸币 SHIB(1886) —— 该条 1000SHIB 行在关联表里无数据，
    会退回"无共振"，不会串到裸币 SHIB 的共振上，属可接受行为。
    """
    with conn.cursor() as cur:
        for cand in _symbol_candidates(symbol):
            cur.execute(
                "SELECT asset_id FROM core.asset WHERE canonical_symbol = %s "
                "ORDER BY market_cap_rank NULLS LAST, asset_id LIMIT 1", (cand,))
            r = cur.fetchone()
            if r:
                return r[0]
    return None


def _norm_title(title) -> str:
    """标题归一（共振去重用）：去「来源：」前缀 + 仅留字母/数字/汉字。

    审计 P0-2：同一条新闻常被多源转载（原文 / 火星财经 / ChainCatcher），标题仅
    源前缀不同 ⇒ 精确字符串去重会漏。归一到「内容骨架」再比（近似事件聚类；
    仍无法处理真正的同形异义误标，那需在 classify 侧消歧）。
    """
    s = (title or "").strip()
    # 源前缀结尾可能是全/半角冒号或逗号（「火星财经消息，」「ChainCatcher 消息，」
    # 「PANews 9月18日消息，」）→ 取最早出现的分隔符，且前缀 ≤16 字符时剥离。
    idx = [i for i in (s.find("："), s.find(":"), s.find("，"), s.find(","))
           if 0 < i <= 16]
    if idx:
        s = s[min(idx) + 1:].strip()
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _get_resonance(conn, symbol: str, asset_id: int | None) -> dict:
    """返回 {event: [...], catalyst: [...], catalyst_dir: {...}, kol: [...]} 三段共振。

    biz.event_watchlist.symbol 同样存裸符号，故事件段也必须用候选序列查询
    （原样查询在本库「数学上恒 0」）。
    催化剂**去重后**计数，并给出方向构成（审计 P0-2：原实现只 `len()`、不看
    `impact_direction` 也不去重，把转载与利空都算成「共振」）。
    """
    out: dict = {"event": [], "catalyst": [], "kol": [],
                 "catalyst_dir": {"bullish": 0, "bearish": 0, "neutral": 0},
                 "catalyst_raw": 0}
    # 1) 事件预置（领先型）
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "SELECT event_type, event_date, event_pct, detail FROM biz.event_watchlist "
            "WHERE symbol = ANY(%s)", (_symbol_candidates(symbol),))
        for r in cur.fetchall():
            out["event"].append(
                f"{'🔓解锁' if r['event_type'] == 'unlock' else '🔄链上转账'}: {r['detail']}")

    if not asset_id:
        return out
    # 2) 催化剂（已发布，确认型）：多取候选以便「归一标题」去重后仍有 4 条可展示
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT ac.title, ac.published_at, ci.impact_direction, ci.impact_strength
            FROM biz.catalyst_impact ci
            JOIN biz.asset_catalyst ac ON ac.catalyst_id = ci.catalyst_id
            WHERE ci.asset_id = %s AND ac.published_at > NOW() - make_interval(days => %s)
            ORDER BY ac.published_at DESC LIMIT 20
            """,
            (asset_id, CATALYST_DAYS),
        )
        rows = cur.fetchall()
    out["catalyst_raw"] = len(rows)
    seen: set[str] = set()
    for r in rows:
        key = _norm_title(r["title"])[:40]
        if not key or key in seen:
            continue
        seen.add(key)
        d = str(r["impact_direction"] or "neutral").lower()
        d = d if d in out["catalyst_dir"] else "neutral"
        out["catalyst_dir"][d] += 1
        if len(out["catalyst"]) < 4:
            tag = f"{r['impact_direction'] or 'neutral'}/{r['impact_strength'] or '-'}"
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


SCENARIO_DESC = {
    "S1": "多头进攻", "S2": "诱多", "S3": "空头扎实", "S4": "诱空",
    "S5": "多头兑现", "S6": "修复反弹", "S7": "跌势衰竭", "S8": "见底反弹",
    "ACC": "蓄势(吸筹?)", "BRK": "蓄势突破",
}


def _fmt_num(v, digits: int = 2, suffix: str = "", signed: bool = False) -> str:
    """容错数值格式化：None / 非数值 → '-'。

    审计 P2-2：原实现直接 `{v:+.2f}` 且 `dict.get(k, 0)`（键存在但值为 None 时
    不返回默认值），遇 NULL 会抛 TypeError 让**整封邮件**渲染失败。
    """
    if v is None:
        return "-"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "-"
    return f"{f:+.{digits}f}{suffix}" if signed else f"{f:.{digits}f}{suffix}"


def _alert_title(items: list[dict]) -> str:
    """邮件标题按**实际**共振条数生成（审计 P1-1：原为硬编码「含共振」，与内容相反）。"""
    n_res = sum(len(it["resonance"]["event"]) + len(it["resonance"]["catalyst"])
                + len(it["resonance"]["kol"]) for it in items)
    suffix = f"含共振 {n_res} 条" if n_res else "纯盘面信号，无共振"
    return f"🚨 盘面异动告警：{len(items)} 币高置信信号（{suffix}）"


def _render_alert_email(items: list[dict]) -> str:
    # 统一标注 UTC（审计 P2-1：容器 TZ=UTC，原实现无时区标注，易被读成本地时间）
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    # 市场环境是同一时刻的全局常量 → 提到头部只渲染一次（审计 P2-1）
    regime_tags: list[str] = []
    if items:
        for t in (items[0]["signal"].get("context_tags") or []):
            if not str(t).startswith("lv"):
                regime_tags.append(str(t))
    env_line = ""
    if regime_tags:
        env_line = ("<p style='margin:0 0 8px;color:#374151;font-size:13px'>"
                    "市场环境 " + " | ".join(regime_tags) + "</p>")
    body_parts = []
    for it in items:
        sig = it["signal"]
        res = it["resonance"]
        pool_label = "蓄势池BRK" if sig["pool"] == "accumulation" else "主池"
        up = sig["p_dir"] == "up"
        dir_label = "↑ 做多" if up else "↓ 做空"
        sc = sig.get("scenario") or "-"
        desc = SCENARIO_DESC.get(sc, "")
        # lv 代号 → 可读级别（审计 P2-2：原样渲染 lv3_1h 对收件人不可自解释）
        lv_txt = ""
        for t in (sig.get("context_tags") or []):
            if str(t).startswith("lv") and "_" in str(t):
                lv_txt = f"{str(t).split('_')[-1]} 级异动"
                break
        fund = sig.get("funding_rate")
        # 资金费率原值存的是小数比例（0.00005 = 0.005%），无数据一律显示 '-' 而非 0
        fund_str = "-" if fund is None else f"{float(fund) * 100:+.4f}%"
        # 共振方向构成 + 与结论相悖警示（审计 P0-2）
        cd = res.get("catalyst_dir") or {}
        cat_n = len(res.get("catalyst", []))
        cat_dir_txt = (f"{cd.get('bullish', 0)}多/{cd.get('bearish', 0)}空/"
                       f"{cd.get('neutral', 0)}中") if cat_n else ""
        res_txt = (f"事件{len(res['event'])} 催化剂{cat_n}"
                   + (f"（{cat_dir_txt}）" if cat_dir_txt else "")
                   + f" KOL{len(res['kol'])}")
        conflict = ""
        if up and cd.get("bearish", 0) > cd.get("bullish", 0):
            conflict = ("<br><span style='color:#dc2626;font-weight:bold'>"
                        "⚠️ 共振方向以利空为主，与做多结论相悖，请复核</span>")
        elif (not up) and cd.get("bullish", 0) > cd.get("bearish", 0):
            conflict = ("<br><span style='color:#dc2626;font-weight:bold'>"
                        "⚠️ 共振方向以利多为主，与做空结论相悖，请复核</span>")
        badge = (f"<span style='background:{'#fee2e2' if up else '#dcfce7'};"
                 f"color:{'#b91c1c' if up else '#15803d'};padding:1px 5px;"
                 f"border-radius:3px;font-size:11px'>"
                 f"{str(sig.get('confidence') or '').upper()}</span> ")
        body_parts.append(
            f"<div style='margin:8px 0;padding:10px;border-left:4px solid "
            f"{'#22c55e' if up else '#ef4444'};background:#f9fafb;color:#111'>"
            f"{badge}<b>{sig['symbol']}</b> {pool_label} <b>{sc}</b> {desc} {dir_label} "
            f"({lv_txt or (sig.get('timeframe') or '-')}, "
            f"{_fmt_num(sig.get('price_chg_pct'), 2, '%', signed=True)})<br>"
            f"<small style='color:#111'>量比 {_fmt_num(sig.get('vol_ratio'), 2, 'x')} | "
            f"OI {sig.get('oi_dir') or '-'} {_fmt_num(sig.get('oi_chg_pct'), 1, '%', signed=True)} | "
            f"CVD {sig.get('cvd_dir') or '未知'} | 费率 {fund_str}</small><br>"
            f"<small style='color:#111'>共振: {res_txt}{conflict}</small>"
            f"</div>"
        )
    body = "".join(body_parts)
    legend = ("<p style='color:#6b7280;font-size:12px'>图例：S1 多头进攻 / S2 诱多 / "
              "S3 空头扎实 / S4 诱空 / S5-8 兑现与反转；「N 级异动」= 触发周期；"
              "CVD up/down = 主动买/卖占比方向；共振方向构成＝去重后的事件方向计数。</p>")
    footnote = ("<p style='color:#999;font-size:12px'>"
                "本邮件为盘面数据分析参考，不构成投资建议。</p>")
    # 可访问性（审计 P2-6）：显式 charset/lang/color-scheme；所有文本节点给 color，
    # 防深色模式客户端下浅底 + 继承浅色字导致不可读。
    return ("<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<meta name='color-scheme' content='light'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'></head>"
            "<body style='font-family:Arial,\"Microsoft YaHei\",sans-serif;"
            "-webkit-text-size-adjust:100%;color:#111;background:#ffffff'>"
            f"<h2 style='margin:0 0 6px;color:#111'>🚨 盘面异动告警</h2>"
            f"<p style='margin:0 0 6px;color:#374151;font-size:13px'>"
            f"生成于 {now} · 共 {len(items)} 个币</p>"
            f"{env_line}{body}{legend}{footnote}</body></html>")


def _stall_parts(conn, now: datetime) -> list[str]:
    """收集停摆项描述（空列表 = 一切正常）。

    判据（2026-09-21 审计 P0-2 修订）：
      1) 15m K 线 / OI **实时**采样 MAX(ts) 年龄 —— OI 只看 source='realtime'，
         否则历史回填写入的 1h 行会冒充心跳把停摆「骗过去」；
      2) 任务心跳（biz.scan_heartbeat）—— 覆盖「采集正常但扫描线程卡死」这一整类
         此前完全无感的故障（原实现只看数据 MAX(ts)，不看任务是否真的在跑）。
    """
    parts: list[str] = []
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("SELECT MAX(open_time) AS mx FROM biz.asset_klines WHERE interval='15m'")
        mx_k = cur.fetchone()["mx"]
        cur.execute(
            "SELECT MAX(ts) AS mx FROM biz.oi_cvd_snapshot "
            "WHERE exchange='binance' AND source='realtime'")
        mx_oi = cur.fetchone()["mx"]

    for label, mx in (("15m K线", mx_k), ("OI 实时采样", mx_oi)):
        if mx is None:
            continue
        age_min = (now - mx).total_seconds() / 60
        if age_min > STALL_ALERT_AGE_MIN:
            parts.append(f"{label} 停在 {mx.strftime('%m-%d %H:%M')} UTC（约 {age_min:.0f} 分钟前）")

    # 任务心跳（表不存在时跳过，兼容迁移未执行的部署）
    try:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT task, last_run_at, last_ok_at, last_error FROM biz.scan_heartbeat")
            hb = {r["task"]: r for r in cur.fetchall()}
        scheduled = _SCHEDULED_TASKS or {t[0] for t in TASK_DEFS}
        start_row = hb.get(DAEMON_START_TASK)
        daemon_start = start_row["last_run_at"] if start_row else None
        for name in STALL_HEARTBEAT_TASKS:
            if name not in scheduled:
                continue
            iv = next((t[1] for t in TASK_DEFS if t[0] == name), 0)
            limit_min = iv * STALL_HEARTBEAT_GRACE / 60.0
            row = hb.get(name)
            last_run = row["last_run_at"] if row else None
            # 本实例尚未跑完首轮（无心跳，或心跳来自上一次进程）→ 给整个
            # limit_min 宽限，避免每次部署后立刻误报「线程从未启动」。
            if row is None or (daemon_start is not None and last_run < daemon_start):
                if daemon_start is None:
                    parts.append(f"任务 {name} 无心跳记录（该线程可能从未启动）")
                else:
                    gap_min = (now - daemon_start).total_seconds() / 60
                    if gap_min > limit_min:
                        parts.append(
                            f"任务 {name} 本实例已启动 {gap_min:.0f} 分钟仍无首轮心跳"
                            f"（阈值 {limit_min:.0f} 分钟）")
                continue
            # 判据用 last_ok_at（最近一次**成功**）而非 last_run_at：stdout/日志
            # 设施故障时每轮都在 print 处抛异常，func() 从未执行，而 last_run_at
            # 照常推进 —— 只看 last_run_at 会把这类「静默失败」判为正常
            # （审计 P0-B，2026-09-21 数据停摆 50 分钟而任务行全绿）。
            # 未成功过（last_ok_at IS NULL）→ 以进程启动时刻起算宽限。
            base = row["last_ok_at"] or daemon_start
            if base is None:
                parts.append(f"任务 {name} 从未成功执行（无成功记录）")
                continue
            age_min = (now - base).total_seconds() / 60
            if age_min > limit_min:
                err = row["last_error"]
                detail = f"，最近一轮报错 {err}" if err else ""
                parts.append(
                    f"任务 {name} 最近一次成功停在 {base.strftime('%m-%d %H:%M')} UTC"
                    f"（约 {age_min:.0f} 分钟前，阈值 {limit_min:.0f} 分钟）{detail}")
    except Exception as e:  # noqa: BLE001
        print(f"[scan_daemon][stall] 心跳检查跳过（{e}）", file=sys.stderr)

    # 丢信号检测（审计 P0-1）：alert 任务停摆 > 窗口(20min)+宽限(10min) 时，窗口内
    # 未告警的 high 信号会被 `signal_ts > NOW()-20min` 永久滤掉、`alerted_at` 恒 NULL，
    # 且与「超窗作废」在库里不可区分。这里把它显式并入停摆告警。
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM biz.scan_signal "
                "WHERE confidence='high' AND alerted_at IS NULL "
                "AND (pool='main' OR (pool='accumulation' AND scenario='BRK')) "
                "AND signal_ts < NOW() - INTERVAL '30 minutes'")
            lost = int(cur.fetchone()[0] or 0)
        if lost:
            parts.append(
                f"未告警即超窗的 high 信号 {lost} 条（alert 任务疑似停摆 >30 分钟，"
                f"这批信号已永久作废、不会补发）")
    except Exception as e:  # noqa: BLE001
        print(f"[scan_daemon][stall] 丢信号检查跳过（{e}）", file=sys.stderr)
    return parts


def _check_and_alert_stall(conn) -> bool:
    """采集/扫描停摆检测：数据年龄或任务心跳超阈值 → 发告警邮件。

    去重：同一告警 STALL_ALERT_MIN_INTERVAL_H 小时内不重发（biz.scan_stall_alert，
    与外部看门狗 check_scan_freshness.py 共用 task='scan_stall' 这一去重键，
    避免同一停摆事件被两条路径各发一封邮件）。
    返回 True 表示当前处于（或刚触发）停摆状态。
    """
    try:
        now = datetime.now(timezone.utc)
        parts = _stall_parts(conn, now)
        if not parts:
            return False

        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT last_email_ts FROM biz.scan_stall_alert WHERE task='scan_stall'")
            row = cur.fetchone()
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
            "<h2 style='margin:0'>⚠️ 盘面扫描停摆告警</h2>"
            f"<p>以下数据/任务已超过阈值未更新，主池/蓄势池扫描可能已停止产出：</p>"
            f"<p>{'<br>'.join(parts)}</p>"
            "<p><b>两类成因怎么区分</b>（审计 P2-5/P2-7）：<br>"
            "① 任务项写「最近一次成功停在 …」⇒ <b>线程在跑但每轮都失败</b>（静默失败），"
            "最常见是容器日志设施断开后 print 抛 <code>ValueError: I/O operation on "
            "closed file.</code>，任务函数从未执行，而心跳照常推进；<br>"
            "② 任务项写「无心跳 / 从未启动」⇒ 进程或线程确实没起来。<br>"
            "两者也可能是 Binance IP 限频、部署被移除（<b>文件更新 ≠ 进程重启</b>）。</p>"
            f"<p style='color:#999'>任务连续 ≥{MAX_CONSEC_FAILURES} 轮连心跳都写不进 DB"
            "（进程级故障）时 scan_daemon 会自行退出，交 supervisord 拉起（自愈）；"
            "业务层失败（Binance/CoinGlass 限频等）只记 last_error、不重启进程。"
            "数据恢复后本告警自动解除。</p>"
        )
        ok, msg = notifier.send(
            "⚠️ 盘面扫描停摆告警", body, from_name="盘面信号扫描")
        if ok:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO biz.scan_stall_alert (task, last_email_ts, updated_at) "
                    "VALUES ('scan_stall', NOW(), NOW()) "
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
            _alert_title(to_alert),
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
#  任务 7：爆仓快照采集（5 分钟，CoinGlass coin-list）
# ═══════════════════════════════════════════════════════════════

CG_MIN_GAP = 0.3          # CoinGlass 请求最小间隔（实测 1.3 req/s 无 429，保守取 0.3）
_CG_CLIENT = None


def _coinglass():
    """惰性初始化 CoinGlass 客户端（单例，避免每轮新建 session）。"""
    global _CG_CLIENT
    if _CG_CLIENT is None:
        settings = _SETTINGS or get_settings(require_database=True)
        if not settings.coinglass_api_key:
            raise RuntimeError("COINGLASS_API_KEY 未配置，跳过爆仓采集")
        _CG_CLIENT = CoinGlassClient(
            settings.coinglass_api_key,
            base_url=settings.coinglass_base_url,
            min_request_gap=CG_MIN_GAP,
        )
    return _CG_CLIENT


def _perp_alias_map(symbols: list[str]) -> dict[str, str]:
    """CoinGlass 币种码 → 本库合约符号。

    精确码优先：CoinGlass 同时存在 PEPE 与 1000PEPE，不要让 1000PEPEUSDT 抢到 PEPE。
    """
    alias: dict[str, str] = {}
    for sym in symbols:
        for a in sqz.alias_bases(sym):
            alias.setdefault(a.upper(), sym)
    return alias


def task_scan_liquidation() -> dict:
    """刷 CoinGlass coin-list 滚动爆仓窗口 → biz.liquidation_snapshot（单轮）。

    HOBBYIST 套餐爆仓最小粒度 4h，拿不到细粒度历史序列，只能高频轮询落库。
    ⚠️ 口径（2026-09-21 审计 P1-1 更正）：`*_liq_usd_1h` 是 CoinGlass 的
    **滚动 1 小时窗口快照**，不是「某个整点的累计」。因此**相邻两次快照相减
    不等于该间隔内新增的爆仓额**——数学上是「新滚入窗口的量 − 滚出窗口的量」，
    在爆仓平稳时近似 0、在回落时恒为负（再被 max(…,0) 截断就是假 0）。
    消费侧必须直接使用该字段的**绝对值**（= 最近 1 小时爆仓额），严禁跨桶差分
    （见 `task_scan_squeeze` / `_latest_liq_snapshot`）。
    实测 coin-list 刷新约 20~40s 一次，5min 轮询不会漏采。
    """
    client = _coinglass()
    rows = client.liquidation_coin_list()
    if not rows:
        return {"coin_list": 0, "mapped": 0}

    symbols = _get_usdt_perpetuals()
    alias = _perp_alias_map(symbols)
    ts = _bucket_5m(datetime.now(timezone.utc))

    payload = []
    for r in rows:
        sym = alias.get(str(r.get("symbol") or "").upper())
        if not sym:
            continue
        payload.append((
            sym, ts, "coinglass",
            r.get("liquidation_usd_1h"), r.get("liquidation_usd_4h"),
            r.get("liquidation_usd_12h"), r.get("liquidation_usd_24h"),
            r.get("long_liquidation_usd_1h"), r.get("short_liquidation_usd_1h"),
            r.get("long_liquidation_usd_4h"), r.get("short_liquidation_usd_4h"),
        ))

    if payload:
        with _db() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO biz.liquidation_snapshot
                        (symbol, ts, source, liq_usd_1h, liq_usd_4h, liq_usd_12h, liq_usd_24h,
                         long_liq_usd_1h, short_liq_usd_1h, long_liq_usd_4h, short_liq_usd_4h,
                         fetched_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                    ON CONFLICT (symbol, ts) DO UPDATE SET
                        liq_usd_1h=EXCLUDED.liq_usd_1h, liq_usd_4h=EXCLUDED.liq_usd_4h,
                        liq_usd_12h=EXCLUDED.liq_usd_12h, liq_usd_24h=EXCLUDED.liq_usd_24h,
                        long_liq_usd_1h=EXCLUDED.long_liq_usd_1h,
                        short_liq_usd_1h=EXCLUDED.short_liq_usd_1h,
                        long_liq_usd_4h=EXCLUDED.long_liq_usd_4h,
                        short_liq_usd_4h=EXCLUDED.short_liq_usd_4h,
                        fetched_at=NOW()
                    """,
                    payload,
                )
            conn.commit()
    return {"coin_list": len(rows), "mapped": len(payload),
            "universe": len(symbols), "bucket": ts.isoformat()}


# ═══════════════════════════════════════════════════════════════
#  任务 8：轧空 扫描 / 跟踪 / 判定（5 分钟）
# ═══════════════════════════════════════════════════════════════

LSR_ENDPOINTS = {
    "top_position_ratio": "/futures/data/topLongShortPositionRatio",
    "top_account_ratio": "/futures/data/topLongShortAccountRatio",
    "global_ratio": "/futures/data/globalLongShortAccountRatio",
    "taker_ratio": "/futures/data/takerlongshortRatio",
}


def _fetch_long_short_ratio(symbol: str, period: str = "5m", limit: int = 20) -> dict:
    """拉单币多空比：4 个免费端点合并为 {ts: {key: value}}，单端点失败不影响其余。"""
    merged: dict = {}
    for key, path in LSR_ENDPOINTS.items():
        try:
            data = _http_get(f"{FAPI_BASE}{path}",
                             {"symbol": symbol, "period": period, "limit": limit})
        except Exception as e:
            print(f"[scan_daemon][squeeze] {symbol} {key} 拉取失败: {type(e).__name__}",
                  file=sys.stderr)
            continue
        for row in data or []:
            try:
                ts = datetime.fromtimestamp(int(row["timestamp"]) / 1000, tz=timezone.utc)
            except (KeyError, TypeError, ValueError):
                continue
            val = row.get("buySellRatio") if key == "taker_ratio" else row.get("longShortRatio")
            if val is None:
                continue
            merged.setdefault(ts, {})[key] = float(val)
    return merged


def _live_price(symbol: str) -> float | None:
    """最新标记价（跟踪期峰值以实时价为准，避免 5min K 线粒度掩盖瞬时高点）。"""
    try:
        d = _http_get(f"{FAPI_BASE}/fapi/v1/ticker/price", {"symbol": symbol})
        return float(d["price"])
    except Exception:
        return None


def _chg_pct(rows: list[dict], back: int = 1) -> float | None:
    """最近一根收盘价相对 back 根之前的涨跌幅（%）。"""
    if len(rows) < back + 1:
        return None
    closes = [float(r["close_px"]) for r in rows]
    if not closes[-1 - back]:
        return None
    return (closes[-1] - closes[-1 - back]) / closes[-1 - back] * 100


def _vol_ratio(rows: list[dict], lookback: int = 20) -> float | None:
    """最新一根成交额 / 前 lookback 根均量。"""
    if len(rows) < lookback + 1:
        return None
    vols = [float(r["quote_vol"] or 0) for r in rows]
    mean = sum(vols[-(lookback + 1):-1]) / lookback
    return vols[-1] / mean if mean else None


def _last_at_or_before(rows: list[dict], when: datetime) -> dict | None:
    """取 ts ≤ when 的最后一条（rows 按 ts 升序）。"""
    hit = None
    for r in rows:
        if r["ts"] <= when:
            hit = r
        else:
            break
    return hit


LIQ_SNAPSHOT_MAX_AGE_MIN = 15   # 爆仓快照新鲜度上限（分钟，=3 个 5m 桶）；超龄视为缺失


def _latest_liq_snapshot(rows: list[dict], now: datetime) -> dict | None:
    """取最近一条**新鲜**的爆仓快照（CoinGlass 滚动 1h 窗口）。

    审计 P1-1：滚动窗口值不能跨桶相减（差值 = 新滚入 − 滚出，常为负 → 假 0），
    只能直接取绝对值，语义 = 「最近 1 小时的（多/空）爆仓额」。
    快照超过 `LIQ_SNAPSHOT_MAX_AGE_MIN` 分钟未更新则视为缺失（返回 None），
    绝不用 0 兜底。
    """
    if not rows:
        return None
    last = rows[-1]
    if (now - last["ts"]).total_seconds() / 60 > LIQ_SNAPSHOT_MAX_AGE_MIN:
        return None
    return last


def _fmt(v, digits: int = 2, suffix: str = "") -> str:
    if v is None:
        return "—"
    return f"{float(v):+.{digits}f}{suffix}"


def _fmt_ratio(v, digits: int = 1) -> str:
    """比例值（0.0123）→ '+1.23%'；None → '—'（与真值 0.0 必须可区分）。"""
    if v is None:
        return "—"
    return f"{float(v) * 100:+.{digits}f}%"


def _hhmm_utc(v) -> str:
    """时间（datetime / ISO 字符串）→ 'HH:MM'（UTC）；无效值 → '—'。"""
    if not v:
        return "—"
    try:
        d = v if isinstance(v, datetime) else datetime.fromisoformat(
            str(v).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).strftime("%H:%M")
    except Exception:  # noqa: BLE001
        return "—"


def _entry_timeframe(track: dict) -> str:
    """入队时的初筛周期（5m/15m）；entry metrics 丢失时退化为 '短时'。"""
    m = track.get("metrics")
    if isinstance(m, str):
        try:
            m = json.loads(m)
        except Exception:  # noqa: BLE001
            m = None
    if isinstance(m, dict) and m.get("timeframe"):
        return str(m["timeframe"])
    return "短时"


def _render_squeeze_alert(items: list[dict]) -> str:
    now_dt = datetime.now(timezone.utc)
    now = now_dt.strftime("%Y-%m-%d %H:%M") + " UTC"
    # 配色按中文用户惯例（红涨绿跌）：多头胜=红、空头胜=绿
    colors = {sqz.LONG_WIN: "#ef4444", sqz.SHORT_WIN: "#22c55e",
              sqz.PROFIT_TAKE: "#f59e0b", sqz.CHURN: "#6b7280"}
    parts = []
    for it in items:
        v, m, t = it["verdict"], it["metrics"], it["track"]
        color = colors.get(v["conclusion"], "#6b7280")
        tf = m.get("surge_timeframe") or _entry_timeframe(t)
        timeline = (f"入队 {_hhmm_utc(t.get('started_at'))} → "
                    f"峰值 {_hhmm_utc(m.get('peak_ts'))} → 判定 {now_dt.strftime('%H:%M')} UTC")
        cover = m.get("oi_cover")
        cover_txt = ""
        if isinstance(cover, dict) and cover.get("have") is not None \
                and cover["have"] < cover.get("expect", cover["have"]):
            cover_txt = f" · 数据覆盖 {cover['have']}/{cover['expect']} 桶"
        if m.get("data_missing"):
            cover_txt += f" · 缺失维度 {', '.join(m['data_missing'])}"
        if m.get("oi_lag_sec") is not None:
            cover_txt += f" · OI滞后 {int(m['oi_lag_sec'])}s"
        if m.get("top_ratio_base") is not None and m.get("top_ratio_now") is not None:
            top_txt = (f"{float(m['top_ratio_base']):.4f} → {float(m['top_ratio_now']):.4f}"
                       f"（Δ{_fmt(m.get('top_ratio_chg'), 3)}）")
        else:
            top_txt = f"Δ {_fmt(m.get('top_ratio_chg'), 3)}"
        taker_txt = _fmt(m.get("taker_ratio"), 2)
        if m.get("taker_ts"):
            taker_txt += f"（{_hhmm_utc(m['taker_ts'])}）"
        parts.append(
            f"<div style='margin:8px 0;padding:10px;border-left:4px solid {color};background:#f9fafb'>"
            f"<b>{t['symbol']}</b> "
            f"<span style='color:{color};font-weight:bold'>{sqz.CONCLUSION_LABEL[v['conclusion']]}</span>"
            f" <small>（置信度 {v['confidence']}）</small><br>"
            f"<small>{tf}涨幅 {_fmt(m['surge_pct'], 2, '%')} · 峰值 {m['peak_px']} → 现价 {m['last_px']}"
            f"（回撤 {m['retrace_pct']:.2f}%）</small><br>"
            f"<small style='color:#666'>{timeline}{cover_txt}</small><br>"
            f"<small>ΔOI {_fmt(m['d_oi_pct'], 2, '%')} | "
            f"CVD占比 {_fmt_ratio(m.get('cvd_ratio'), 1)} | "
            f"最近1h多单爆仓/成交额 {_fmt_ratio(m.get('long_liq_ratio'), 3)} | "
            f"最近1h空单爆仓/成交额 {_fmt_ratio(m.get('short_liq_ratio'), 3)} | "
            f"大户持仓多空比 {top_txt} | "
            f"主动买卖比 {taker_txt}</small><br>"
            f"<small style='color:#444'>判定依据：{v['reason']}</small>"
            f"</div>")
    return (f"<html><body style='font-family:Arial,\"Microsoft YaHei\",sans-serif'>"
            f"<h2 style='margin:0'>🎯 轧空行情胜负判定</h2>"
            f"<p style='margin:0 0 8px;color:#666;font-size:13px'>生成于 {now} · "
            f"共 {len(items)} 个币（冲高回撤后判定，仅数据监控）</p>"
            f"{''.join(parts)}"
            f"<p style='color:#999;font-size:12px'>口径：爆仓＝CoinGlass 全交易所滚动 1 小时"
            f"（多空分列），分母为币安 24h 成交额，两者非同一交易所口径，比例仅作横向比较；"
            f"「—」＝该维度数据缺失，非 0。</p>"
            f"<p style='color:#999;font-size:12px'>本邮件为合约盘面数据分析参考，不构成投资建议。</p>"
            f"</body></html>")


def task_scan_squeeze(min_vol_usd: float = 5_000_000) -> dict:
    """轧空扫描（单轮）：① 拉升初筛+轧空确认入队 ② 队列跟踪峰值 ③ 回撤后判定胜负。

    数据口径：
      - 价格/涨幅/放量：biz.asset_klines 5m/15m（scan_klines 采集）
      - OI / CVD：biz.oi_cvd_snapshot 5m（scan_oi_cvd 采集）
      - 爆仓：biz.liquidation_snapshot（CoinGlass coin-list **滚动 1h 窗口绝对值**，见任务 7；
        严禁跨桶差分——滚动窗口相减不等于窗口内新增，见 `_latest_liq_snapshot`）
      - 多空比：biz.long_short_ratio（Binance /futures/data/*，仅对跟踪中币按需拉取）
    判定口径修正与阈值定义见 crypto_research.analysis.squeeze 模块文档。
    **只输出信号与告警，不做任何下单。**
    """
    now = datetime.now(timezone.utc)
    vol24 = _get_24h_quote_volume()
    symbols = [s for s in _get_usdt_perpetuals() if vol24.get(s, 0) >= min_vol_usd]
    stats = {"universe": len(symbols), "scanned": 0, "surge": 0, "enqueued": 0,
             "tracked": 0, "judged": 0, "expired": 0, "skipped": 0,
             "insufficient_coverage": 0, "alerts": 0}
    if not symbols:
        return stats

    # ── HTTP 先行（审计 P2-2）：先取跟踪币的实时价与多空比，再进 DB，
    # 让网络 I/O 完全不落在数据库连接/事务上 ─────────────────────────
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT symbol FROM biz.squeeze_track WHERE status='tracking'")
            pre_syms = [r[0] for r in cur.fetchall()]
        conn.commit()
    px_map: dict[str, float | None] = {}
    lsr_map: dict[str, dict] = {}
    for sym in pre_syms:
        px_map[sym] = _live_price(sym)
        lsr_map[sym] = _fetch_long_short_ratio(sym)

    with _db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT symbol, interval, open_time, close_px, quote_vol "
                "FROM biz.asset_klines WHERE interval IN ('5m','15m') "
                "AND open_time >= NOW() - INTERVAL '3 hours' "
                "ORDER BY symbol, interval, open_time")
            k_rows = cur.fetchall()
            cur.execute(
                "SELECT symbol, ts, oi_usd, cvd_5m_usd, vol_5m_usd "
                "FROM biz.oi_cvd_snapshot WHERE source='realtime' "
                "AND ts >= NOW() - INTERVAL '4 hours' "
                "ORDER BY symbol, ts")
            oi_rows = cur.fetchall()
            cur.execute(
                "SELECT symbol, ts, long_liq_usd_1h, short_liq_usd_1h "
                "FROM biz.liquidation_snapshot WHERE ts >= NOW() - INTERVAL '4 hours' "
                "ORDER BY symbol, ts")
            liq_rows = cur.fetchall()
            cur.execute("SELECT * FROM biz.squeeze_track WHERE status='tracking'")
            tracks = cur.fetchall()
            cur.execute(
                "SELECT DISTINCT symbol FROM biz.squeeze_track "
                "WHERE status IN ('judged','expired') "
                "AND updated_at > NOW() - make_interval(hours => %s)",
                (sqz.SQUEEZE_COOLDOWN_H,))
            cooldown = {r["symbol"] for r in cur.fetchall()}
        conn.commit()

        by_k: dict[str, dict[str, list[dict]]] = {}
        for r in k_rows:
            by_k.setdefault(r["symbol"], {}).setdefault(r["interval"], []).append(r)
        by_oi: dict[str, list[dict]] = {}
        for r in oi_rows:
            by_oi.setdefault(r["symbol"], []).append(r)
        by_liq: dict[str, list[dict]] = {}
        for r in liq_rows:
            by_liq.setdefault(r["symbol"], []).append(r)

        tracking = {t["symbol"] for t in tracks}
        stats["tracked"] = len(tracking)

        # ── 阶段 1：拉升初筛 + 轧空确认 → 入队 ────────────────────
        new_tracks: list[tuple] = []
        for sym in symbols:
            if sym in tracking or sym in cooldown:
                continue
            kk = by_k.get(sym) or {}
            k5, k15 = kk.get("5m", []), kk.get("15m", [])
            if not k5:
                continue
            if (now - k5[-1]["open_time"]).total_seconds() / 60 > MAX_KLINE_AGE_MIN["5m"]:
                continue  # K 线陈旧，不用旧数据出假信号
            stats["scanned"] += 1

            sur = sqz.screen_surge(_chg_pct(k5), _chg_pct(k15), _vol_ratio(k5))
            if not sur:
                continue
            stats["surge"] += 1

            oi = by_oi.get(sym, [])
            oi_chg = None
            if len(oi) >= 3 and oi[-3]["oi_usd"]:
                oi_chg = ((float(oi[-1]["oi_usd"]) - float(oi[-3]["oi_usd"]))
                          / float(oi[-3]["oi_usd"]) * 100)
            lq_now = _latest_liq_snapshot(by_liq.get(sym) or [], now)
            short_liq_ratio = None
            if lq_now and vol24.get(sym) and lq_now["short_liq_usd_1h"] is not None:
                # 滚动 1h 窗口取绝对值（不跨桶差分，不做 max(…,0) 截断）
                short_liq_ratio = float(lq_now["short_liq_usd_1h"]) / vol24[sym]
            cvd_ratio = None
            if len(oi) >= 2:
                v = sum(float(r["vol_5m_usd"] or 0) for r in oi[-2:])
                cvd_ratio = sum(float(r["cvd_5m_usd"] or 0) for r in oi[-2:]) / v if v else None

            ok, why = sqz.confirm_squeeze(oi_chg, short_liq_ratio, cvd_ratio)
            if not ok:
                continue
            if len(tracking) + len(new_tracks) >= sqz.TRACK_QUEUE_MAX:
                stats["skipped"] += 1
                continue
            entry_metrics = {
                "confirm": why, "timeframe": sur["timeframe"],
                "oi_chg_pct": None if oi_chg is None else round(oi_chg, 3),
                "short_liq_ratio": None if short_liq_ratio is None else round(short_liq_ratio, 6),
                "cvd_ratio": None if cvd_ratio is None else round(cvd_ratio, 4),
                "liq_ts": lq_now["ts"].isoformat() if lq_now else None,
                "liq_scope": "coinglass_rolling_1h",
            }
            new_tracks.append((
                sym, now, k5[-1]["open_time"], float(k5[-2]["close_px"]),
                round(sur["chg_pct"], 2), float(k5[-1]["close_px"]), now,
                float(k5[-1]["close_px"]), now, 0.0,
                now + timedelta(minutes=sqz.TRACK_EXPIRE_MIN),
                json.dumps(entry_metrics, ensure_ascii=False),
            ))

        # ── 阶段 2/3：跟踪峰值 → 回撤判定 ──────────────────────────
        ratio_upserts: list[tuple] = []
        track_updates: list[tuple] = []
        judged_items: list[dict] = []
        for t in tracks:
            sym = t["symbol"]
            px = px_map.get(sym)   # HTTP 已在进 DB 前取好（审计 P2-2）
            if px is None:
                continue
            peak_px = float(t["peak_px"] or px)
            peak_ts = t["peak_ts"] or t["started_at"]
            if px > peak_px:
                peak_px, peak_ts = px, now
            retrace = (peak_px - px) / peak_px * 100 if peak_px else 0.0

            if sqz.is_expired(t["started_at"], now):
                track_updates.append((
                    "expired", peak_px, peak_ts, px, now, round(retrace, 2), None,
                    f"跟踪 {sqz.TRACK_EXPIRE_MIN} 分钟未触发回撤判定", None, None, t["id"]))
                stats["expired"] += 1
                continue

            fire, why = sqz.should_judge(retrace, peak_ts, now)
            if not fire:
                track_updates.append((
                    "tracking", peak_px, peak_ts, px, now, round(retrace, 2), None,
                    None, None, None, t["id"]))
                continue

            # 回撤窗口 [peak_ts, now] 指标
            oi_sym = by_oi.get(sym, [])
            win_oi = [r for r in oi_sym if r["ts"] >= peak_ts]
            # 覆盖率闸门（审计 P1-2）：快照型数据停机即永久丢失，窗口残缺时
            # 任何「窗口指标」都名不副实 → 拒绝判定，宁缺毋错。
            # 期望桶数按 5m 对齐边界计（含 peak 所在桶），避免把部分桶算进去。
            first_bucket = -(-int(peak_ts.timestamp()) // BUCKET_SECONDS)
            last_bucket = int(now.timestamp()) // BUCKET_SECONDS
            expect_buckets = max(1, last_bucket - first_bucket + 1)
            coverage = len(win_oi) / expect_buckets
            # 尾部连续性（SQZ-03）：覆盖率只看「数量」，前段齐、尾部断照样骗过闸门。
            # 实测 OI 与 squeeze 同相位（offset 差 = 整周期），正常数据年龄 ≈170s；
            # >2×BUCKET(600s) 说明最近桶迟迟没落库，窗口右端已失真。
            oi_ts_max = oi_sym[-1]["ts"] if oi_sym else None
            oi_lag_sec = None if oi_ts_max is None else (now - oi_ts_max).total_seconds()
            tail_gap = oi_lag_sec is not None and oi_lag_sec > 2 * BUCKET_SECONDS
            if coverage < sqz.MIN_WINDOW_COVERAGE or tail_gap:
                stats["insufficient_coverage"] += 1
                if coverage < sqz.MIN_WINDOW_COVERAGE:
                    reason = (f"判定窗口数据覆盖不足 {len(win_oi)}/{expect_buckets} 桶，暂不判定")
                else:
                    reason = "判定窗口尾部 OI 桶缺失，暂不判定"
                if oi_lag_sec is not None:
                    reason += f"（OI 最新桶滞后 {oi_lag_sec:.0f}s）"
                print(f"[scan_daemon][squeeze] {sym} {reason}", file=sys.stderr)
                track_updates.append((
                    "tracking", peak_px, peak_ts, px, now, round(retrace, 2), None,
                    reason, None, None, t["id"]))
                continue
            # 基准取「峰值时刻或之前最近一条」；峰值早于所有可用桶时退化为窗口首条
            base_oi = _last_at_or_before(oi_sym, peak_ts) or (win_oi[0] if win_oi else None)
            d_oi = None
            if win_oi and base_oi and base_oi["oi_usd"] and win_oi[-1]["oi_usd"]:
                d_oi = ((float(win_oi[-1]["oi_usd"]) - float(base_oi["oi_usd"]))
                        / float(base_oi["oi_usd"]) * 100)
            cvd_ratio = None
            if win_oi:
                v = sum(float(r["vol_5m_usd"] or 0) for r in win_oi)
                cvd_ratio = (sum(float(r["cvd_5m_usd"] or 0) for r in win_oi) / v) if v else None

            # 爆仓取「最近 1h 滚动窗口」绝对值（不跨桶差分、不 max(…,0) 截断）
            lq_now = _latest_liq_snapshot(by_liq.get(sym) or [], now)
            long_liq_ratio = short_liq_ratio = None
            liq_ts = None
            if lq_now and vol24.get(sym):
                liq_ts = lq_now["ts"].isoformat()
                if lq_now["long_liq_usd_1h"] is not None:
                    long_liq_ratio = float(lq_now["long_liq_usd_1h"]) / vol24[sym]
                if lq_now["short_liq_usd_1h"] is not None:
                    short_liq_ratio = float(lq_now["short_liq_usd_1h"]) / vol24[sym]

            merged = lsr_map.get(sym) or {}
            for r_ts, vals in merged.items():
                ratio_upserts.append((
                    sym, "5m", r_ts, vals.get("top_position_ratio"),
                    vals.get("top_account_ratio"), vals.get("global_ratio"),
                    vals.get("taker_ratio")))
            top_chg = top_base = top_now = taker_now = taker_ts = None
            if merged:
                taker_ts, taker_now = sqz.latest_ratio_ts(merged, "taker_ratio")
                top_base = sqz.latest_ratio(merged, "top_position_ratio", peak_ts)
                top_now = sqz.latest_ratio(merged, "top_position_ratio")
                if top_base is not None and top_now is not None:
                    top_chg = top_now - top_base

            verdict = sqz.evaluate_battle(
                d_oi_pct=d_oi, cvd_ratio=cvd_ratio, long_liq_ratio=long_liq_ratio,
                top_ratio_chg=top_chg, taker_ratio=taker_now)
            metrics = dict(verdict["metrics"])
            metrics.update({
                "peak_px": peak_px, "peak_ts": peak_ts.isoformat(),
                "last_px": px, "retrace_pct": round(retrace, 2),
                "surge_pct": float(t["surge_pct"] or 0),
                "surge_timeframe": _entry_timeframe(t),
                "short_liq_ratio": None if short_liq_ratio is None else round(short_liq_ratio, 6),
                "liq_ts": liq_ts, "liq_scope": "coinglass_rolling_1h",
                "top_ratio_base": top_base, "top_ratio_now": top_now,
                "taker_ts": taker_ts.isoformat() if taker_ts else None,
                "oi_cover": {"have": len(win_oi), "expect": expect_buckets},
                "oi_lag_sec": None if oi_lag_sec is None else round(oi_lag_sec),
                "trigger": why,
            })
            track_updates.append((
                "judged", peak_px, peak_ts, px, now, round(retrace, 2),
                verdict["conclusion"], verdict["reason"],
                json.dumps(metrics, ensure_ascii=False), now, t["id"]))
            judged_items.append({"track": t, "verdict": verdict, "metrics": metrics})
            stats["judged"] += 1

        # ── 落库 ────────────────────────────────────────────────
        # 至此才开启**写事务**：读阶段已 commit、HTTP 已在进块前取完、中间为纯内存
        # 计算（不 execute），故本事务不含网络 I/O（审计 P2-2 / 工单 SQZ-05）。
        signal_ids: list[int] = []
        with conn.cursor() as cur:
            if new_tracks:
                cur.executemany(
                    """
                    INSERT INTO biz.squeeze_track
                        (symbol, status, started_at, surge_start_ts, surge_start_px,
                         surge_pct, peak_px, peak_ts, last_px, last_ts, retrace_pct,
                         expires_at, metrics, updated_at)
                    VALUES (%s,'tracking',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,NOW())
                    ON CONFLICT (symbol) WHERE status='tracking' DO NOTHING
                    """,
                    new_tracks)
                stats["enqueued"] = len(new_tracks)
            if track_updates:
                cur.executemany(
                    """
                    UPDATE biz.squeeze_track SET
                        status=%s, peak_px=%s, peak_ts=%s, last_px=%s, last_ts=%s,
                        retrace_pct=%s, conclusion=%s, reason=%s,
                        metrics=COALESCE(%s::jsonb, metrics),
                        judged_at=%s, updated_at=NOW()
                    WHERE id=%s
                    """,
                    track_updates)
            if ratio_upserts:
                cur.executemany(
                    """
                    INSERT INTO biz.long_short_ratio
                        (symbol, period, ts, top_position_ratio, top_account_ratio,
                         global_ratio, taker_ratio, fetched_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())
                    ON CONFLICT (symbol, period, ts) DO UPDATE SET
                        top_position_ratio=EXCLUDED.top_position_ratio,
                        top_account_ratio=EXCLUDED.top_account_ratio,
                        global_ratio=EXCLUDED.global_ratio,
                        taker_ratio=EXCLUDED.taker_ratio,
                        fetched_at=NOW()
                    """,
                    ratio_upserts)
            for it in judged_items:
                v, m, t = it["verdict"], it["metrics"], it["track"]
                p_dir = {sqz.LONG_WIN: "up", sqz.SHORT_WIN: "down",
                         sqz.PROFIT_TAKE: "down"}.get(v["conclusion"])
                cvd = m.get("cvd_ratio")
                cvd_dir = None if cvd is None else ("down" if cvd < 0 else "up")
                cur.execute(
                    """
                    INSERT INTO biz.scan_signal
                        (signal_ts, symbol, pool, scenario, timeframe, p_dir, price_chg_pct,
                         oi_chg_pct, cvd_dir, confidence, context_tags, trigger_price,
                         detail, status)
                    VALUES (%s,%s,'squeeze',%s,'5m',%s,%s,%s,%s,%s,%s,%s,%s::jsonb,'confirmed')
                    RETURNING id
                    """,
                    (now, t["symbol"], f"SQZ_{v['conclusion'].upper()}", p_dir,
                     round(float(t["surge_pct"] or 0), 2), m["d_oi_pct"],
                     cvd_dir, v["confidence"],
                     [sqz.CONCLUSION_LABEL[v["conclusion"]],
                      f"retrace={m['retrace_pct']}%", m["trigger"]],
                     m["peak_px"], json.dumps(m, ensure_ascii=False)))
                signal_ids.append(cur.fetchone()[0])
            conn.commit()

    # ── 判定成功 → 发告警（只发一次，不做 12h 冷却）──────────────
    if judged_items:
        settings = _SETTINGS or get_settings(require_database=True)
        from crypto_research.clients.notifier import EmailNotifier
        notifier = EmailNotifier(settings)
        if not notifier.configured:
            print("[WARN] SMTP 未配置，跳过轧空判定告警")
        else:
            ok, msg = notifier.send(
                f"🎯 轧空胜负判定：{len(judged_items)} 币",
                _render_squeeze_alert(judged_items), from_name="轧空扫描")
            if ok:
                with _db() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE biz.scan_signal SET alerted_at=NOW() WHERE id = ANY(%s)",
                            (signal_ids,))
                    conn.commit()
                stats["alerts"] = len(signal_ids)
            else:
                print(f"[scan_daemon][squeeze] 告警发送失败: {msg}", file=sys.stderr)

    return stats


# ═══════════════════════════════════════════════════════════════
#  任务 9：采集数据保留期清理（每日，按表分档）
# ═══════════════════════════════════════════════════════════════

LIQ_RETENTION_DAYS = 30   # biz.liquidation_snapshot 保留天数（约 15 万行/天）
# 审计 §12.1-1：原先只清 liquidation_snapshot，oi_cvd_snapshot / asset_klines 只增不减。
# oi_cvd_snapshot 约 15 万行/天（288 桶 × 528 币），90 天 ≈ 1350 万行。
OI_CVD_RETENTION_DAYS = 90
# asset_klines 按周期分档：5m 量最大、只留够回测的窗口；1h 便宜且是最长参考周期，留 2 年。
KLINE_RETENTION_DAYS = {"5m": 90, "15m": 180, "1h": 730}


def task_prune_scan_data(retention_days: int = LIQ_RETENTION_DAYS) -> dict:
    """清理超出保留期的高频采集数据（每日一次）。

    覆盖三张高频时序表（审计 §12.1-1：原先只清 liquidation_snapshot）：

      - `biz.liquidation_snapshot`：`LIQ_RETENTION_DAYS`（30 天，稳态约 450 万行）
      - `biz.oi_cvd_snapshot`：`OI_CVD_RETENTION_DAYS`（90 天）
      - `biz.asset_klines`：按周期分档 `KLINE_RETENTION_DAYS`（5m/15m/1h）

    两条 DELETE 都命中现有索引（`idx_oi_cvd_snapshot_ts`、
    `idx_asset_klines_interval_ot`），不会退化成全表扫描。
    `biz.squeeze_track` 终态行与 `biz.scan_signal` 体量小且有回溯价值，刻意不清理。
    """
    with _db() as conn:
        stats: dict[str, int] = {}
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM biz.liquidation_snapshot "
                "WHERE ts < NOW() - make_interval(days => %s)",
                (int(retention_days),))
            stats["liquidation_snapshot"] = cur.rowcount

            cur.execute(
                "DELETE FROM biz.oi_cvd_snapshot "
                "WHERE ts < NOW() - make_interval(days => (%s)::int)",
                (int(OI_CVD_RETENTION_DAYS),))
            stats["oi_cvd_snapshot"] = cur.rowcount

            kline_deleted = 0
            for iv, days in KLINE_RETENTION_DAYS.items():
                cur.execute(
                    "DELETE FROM biz.asset_klines "
                    "WHERE interval = %s AND open_time < NOW() - make_interval(days => (%s)::int)",
                    (iv, int(days)))
                kline_deleted += cur.rowcount
            stats["asset_klines"] = kline_deleted
        conn.commit()
    return {"retention_days": int(retention_days),
            "oi_cvd_retention_days": int(OI_CVD_RETENTION_DAYS),
            "kline_retention_days": dict(KLINE_RETENTION_DAYS),
            **stats}


# ═══════════════════════════════════════════════════════════════
#  任务 10：信号生命周期巡检（30 分钟）
# ═══════════════════════════════════════════════════════════════

# 信号有效期（设计文档 §6.3 / §12.1-4）。各池语义不同，取值依据：
#   - 主池 / 蓄势池 BRK：24h —— §8 结论 3「1h 窗口全档位期望 <0.6%（成本吃光），
#     24h 为主窗口 → 警报语义为『24h 持有观察』」，超过 24h 的信号不再具参考性。
#   - 蓄势池 ACC：7 天 —— 与 ACC_BRK_CANDIDATE_DAYS 对齐：BRK 升级候选集要求
#     「近 7 天内 active 的 ACC 信号」，ACC 若提前过期，这批币就永远等不到突破判定。
SIGNAL_TTL_MAIN_H = 24
SIGNAL_TTL_ACC_DAYS = 7


def task_expire_signals() -> dict:
    """信号生命周期巡检：把超期的 active 信号置 expired 并写 expired_at（每 30 分钟）。

    实现设计文档 §6.3 的「超时退出」一段（§12.1-4 缺口）。原状：`biz.scan_signal`
    只有写入没有退出，`status` 恒为 active、`expired_at` 无人写 —— 观察名单没有
    退出机制，告警冷却、执行层冷却（查 created_at 窗口）与后续统计都会越来越脏。

    有效期语义见 SIGNAL_TTL_* 常量（主池/BRK 24h、ACC 7 天）。

    两个实现选择：
      - `expired_at` 写**确定性截止时刻**（`signal_ts + TTL`）而非 `NOW()`：本任务
        每 30 分钟才跑一轮，写 NOW() 会让实际有效期随巡检相位漂移最多 30 分钟。
        这一列同时被 `phase_execute_scan_signal.load_candidates` 用作在窗前筛。
      - 只更新 `status='active'` 的行 ⇒ 幂等，可重复执行；轧空池写入的 `confirmed`
        行不参与（它记录的是「已判定事件」，有自己的 `biz.squeeze_track` 状态机，
        不属于观察名单）。
    """
    with _db() as conn:
        stats: dict[str, int] = {}
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE biz.scan_signal
                   SET status = 'expired',
                       expired_at = signal_ts + make_interval(hours => (%s)::int)
                 WHERE status = 'active'
                   AND ((pool = 'main')
                        OR (pool = 'accumulation' AND scenario = 'BRK'))
                   AND signal_ts < NOW() - make_interval(hours => (%s)::int)
                """,
                (SIGNAL_TTL_MAIN_H, SIGNAL_TTL_MAIN_H))
            stats["main_brk"] = cur.rowcount

            cur.execute(
                """
                UPDATE biz.scan_signal
                   SET status = 'expired',
                       expired_at = signal_ts + make_interval(days => (%s)::int)
                 WHERE status = 'active'
                   AND pool = 'accumulation' AND scenario = 'ACC'
                   AND signal_ts < NOW() - make_interval(days => (%s)::int)
                """,
                (SIGNAL_TTL_ACC_DAYS, SIGNAL_TTL_ACC_DAYS))
            stats["acc"] = cur.rowcount
        conn.commit()
    return {"signal_ttl_main_h": SIGNAL_TTL_MAIN_H,
            "signal_ttl_acc_days": SIGNAL_TTL_ACC_DAYS, **stats}


# ═══════════════════════════════════════════════════════════════
#  守护进程调度框架
# ═══════════════════════════════════════════════════════════════

SCAN_SINGLETON_LOCK_KEY = 0x5CA9DAE1   # scan_daemon 单实例 advisory lock（固定 key）
_SINGLETON_CONN = None                 # 持有该锁的连接，进程存活期间不归还连接池


def _try_advisory_lock(conn) -> bool:
    """申请会话级单实例锁，返回是否拿到。

    必须提交：会话级 advisory lock 本身不需要事务，但 psycopg3 默认
    autocommit=False，不提交会让这条常驻连接以「idle in transaction」
    状态挂住整个进程生命周期，长期阻挡 autovacuum 回收死元组
    （对一个高频写库的常驻连接是真实副作用，审计 P2-N5）。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (SCAN_SINGLETON_LOCK_KEY,))
        got = bool(cur.fetchone()[0])
    conn.commit()
    return got


def _heartbeat_stalled() -> bool:
    """全局心跳是否已停止推进（僵尸实例判定用）。查询异常时保守返回 False。"""
    try:
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT MAX(last_run_at) FROM biz.scan_heartbeat")
                mx = cur.fetchone()[0]
        if mx is None:
            return False
        age_min = (datetime.now(timezone.utc) - mx).total_seconds() / 60
        return age_min > SINGLETON_ZOMBIE_GRACE_MIN
    except Exception as e:  # noqa: BLE001
        print(f"[scan_daemon] 僵尸实例判定跳过（{e}）", file=sys.stderr)
        return False


def _evict_zombie_lock_holder(conn) -> int | None:
    """锁被占用且心跳已停滞 → 判持有者为僵尸实例并终止其连接，返回被终止的 pid。

    半死实例（平台关掉了它的 stdout、线程卡死等）会一直占着单实例锁，让新实例
    永远起不来 —— 2026-09-18 63h、2026-09-21 48min 两次停摆都由这条链路放大
    （审计 P0-A）。终止的对象是**空闲的会话连接**，不涉及任何数据写操作；僵尸
    进程下次写库会自行发现连接断开，新实例随即接管。

    ⚠️ 必须限定当前库（审计复验 P1-2）：pg_locks 是**集群级视图**，不是当前库
    视图。不加 database 条件时，同集群其它库若有进程持有同一 key，会被跨库误杀。
    """
    key = SCAN_SINGLETON_LOCK_KEY
    hi, lo = (key >> 32) & 0xFFFFFFFF, key & 0xFFFFFFFF
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pid FROM pg_locks WHERE locktype='advisory' AND objsubid=1 "
            "AND classid::bigint = %s AND objid::bigint = %s "
            "AND database = (SELECT oid FROM pg_database "
            "                WHERE datname = current_database()) "
            "AND pid <> pg_backend_pid()",
            (hi, lo))
        row = cur.fetchone()
        if not row:
            return None
        pid = row[0]
        cur.execute("SELECT pg_terminate_backend(%s)", (pid,))
        conn.commit()
    return pid


def _acquire_singleton_lock(db_pool) -> bool:
    """申请单实例锁（审计 P0-3），成功则独占该连接直到进程退出。

    锁必须在**独立且常驻**的连接上持有：连接池连接被回收/重置时会话级锁随之释放。
    申请本身异常时**放行**（fail-open）——宁可冒双实例风险，也不能让守护进程起不来，
    因为「守护进程完全没运行」正是 2026-09-18 那次 63h 静默停摆的根因。

    锁被占用时若心跳已停滞（>SINGLETON_ZOMBIE_GRACE_MIN 分钟无推进），说明持有者
    是僵尸实例（进程活着、业务已死），先驱逐再重试 —— 否则新实例永远起不来，
    形成无限停摆（审计 P0-A）。

    取锁失败不立即放弃，而是按 LOCK_ACQUIRE_RETRIES 重试（审计复验 P1-3）：
    os._exit(1) 后 supervisord 会在旧实例锁连接尚未完全释放时就拉起新实例，
    若因此返回 1 且耗时 < startsecs，会被计为「启动失败」并可能耗尽 startretries
    进入 FATAL（autorestart 对 FATAL 无效，需人工 restart）。
    """
    global _SINGLETON_CONN
    # 记录进入时刻：退出前需保证总耗时 > STARTUP_MIN_SEC（见常量注释）
    _t0 = time.monotonic()
    try:
        conn = db_pool.getconn()
        got = False
        for attempt in range(1, LOCK_ACQUIRE_RETRIES + 1):
            if _try_advisory_lock(conn):
                got = True
                break
            # 锁被占用：若持有者心跳已停滞，判为僵尸实例并驱逐，再立刻重试一次
            if _heartbeat_stalled():
                pid = _evict_zombie_lock_holder(conn)
                if pid:
                    print(f"[scan_daemon] 单实例锁持有者(pid={pid})心跳已停滞超过 "
                          f"{SINGLETON_ZOMBIE_GRACE_MIN} 分钟，判定为僵尸实例并终止其连接，重试取锁")
                    if _try_advisory_lock(conn):
                        got = True
                        break
            if attempt < LOCK_ACQUIRE_RETRIES:
                print(f"[scan_daemon] 单实例锁被占用，{LOCK_ACQUIRE_RETRY_WAIT_SEC:.0f}s 后重试"
                      f"（{attempt}/{LOCK_ACQUIRE_RETRIES}）", file=sys.stderr)
                time.sleep(LOCK_ACQUIRE_RETRY_WAIT_SEC)
        if not got:
            db_pool.putconn(conn)
            # 审计复验 FIX-061：退出耗时必须越过 startsecs，否则 supervisord 记
            # 「Exited too quickly」→ 消耗 startretries（3 次）→ FATAL 且不再拉起。
            # 该 sleep 只在【取锁失败】分支执行，正常启动路径零影响。
            _elapsed = time.monotonic() - _t0
            if _elapsed < STARTUP_MIN_SEC:
                print(f"[scan_daemon] 单实例锁取锁失败，为使 supervisord 计为"
                      f"『已启动后正常退出』而非『启动失败』，等待 "
                      f"{STARTUP_MIN_SEC - _elapsed:.1f}s 后退出（审计 FIX-061）",
                      file=sys.stderr)
                time.sleep(STARTUP_MIN_SEC - _elapsed)
            return False
        _SINGLETON_CONN = conn
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[scan_daemon] 单实例锁申请异常（按放行处理）: {e}", file=sys.stderr)
        return True


def _write_heartbeat(name: str, ok: bool, err: str | None = None) -> bool:
    """写任务心跳（每轮都写，与是否产出信号无关）。

    biz.scan_heartbeat 让停摆检测能区分「采集正常但扫描线程卡死」——
    只看数据 MAX(ts) 无法发现这一类故障（2026-09-21 审计 P0-2）。
    心跳写失败只告警，不影响任务本身。

    返回是否写入成功：调用方据此判定「进程自身已不可用」并决定是否自杀重启
    （审计复验 P1-1 —— 只有连心跳都写不进去才说明是进程级故障）。
    """
    try:
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO biz.scan_heartbeat "
                    "(task, last_run_at, last_ok_at, last_error, round_count, updated_at) "
                    "VALUES (%s, NOW(), CASE WHEN %s THEN NOW() ELSE NULL END, %s, 1, NOW()) "
                    "ON CONFLICT (task) DO UPDATE SET "
                    "last_run_at=NOW(), "
                    "last_ok_at=CASE WHEN %s THEN NOW() "
                    "                ELSE biz.scan_heartbeat.last_ok_at END, "
                    "last_error=%s, round_count=biz.scan_heartbeat.round_count+1, updated_at=NOW()",
                    (name, ok, err, ok, err))
            conn.commit()
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[scan_daemon][{name}] 心跳写入失败: {e}", file=sys.stderr)
        return False


def _run_task_loop(name: str, interval_sec: int, func, offset_sec: int = 0,
                   func_kwargs: dict | None = None):
    """单个任务的常驻循环。
    
    - 首次等待 offset_sec 秒（错峰）
    - 每 interval_sec 执行一次
    - SkipIfRunning：上一轮未结束则跳过
    - 单轮异常不影响下一轮
    - 每轮写一次心跳（供停摆检测使用）
    """
    func_kwargs = func_kwargs or {}
    time.sleep(offset_sec)  # 初始错峰

    running = False
    round_count = 0
    fail_streak = 0
    while True:
        round_count += 1
        start_ts = time.time()

        if running:
            print(f"[scan_daemon][{name}] 跳过第 {round_count} 轮（上一轮仍在运行）")
            time.sleep(interval_sec)
            continue

        # 日志放在 try 之外、func() 之前（审计 P0-A 修复）：原实现把这条 print
        # 放在 try 内，stdout 失效时「打印失败」被 except 吞成「单轮失败」，
        # func() 从未执行。_harden_streams() 已让 print 不再抛异常，此处再加一层。
        print(f"[scan_daemon][{name}] 第 {round_count} 轮开始")

        running = True
        ok = False
        err: str | None = None
        hb_ok = True
        try:
            result = func(**func_kwargs)
            elapsed = time.time() - start_ts
            print(f"[scan_daemon][{name}] 第 {round_count} 轮完成，耗时 {elapsed:.1f}s，结果: {result}")
            ok = True
        except Exception as e:
            elapsed = time.time() - start_ts
            err = f"{type(e).__name__}: {e}"[:500]
            print(f"[scan_daemon][{name}] 第 {round_count} 轮异常 ({elapsed:.1f}s): {e}",
                  file=sys.stderr)
            traceback.print_exc()
        finally:
            running = False
            hb_ok = _write_heartbeat(name, ok, err)

        # 连续失败自杀（审计 P1-4），但判据收紧为**进程级故障**（审计复验 P1-1）：
        # 只有「连心跳都写不进 DB」才说明本进程自身已不可用（DB/连接池/日志设施坏），
        # 此时进程级重启才有意义。外部依赖抖动（Binance/CoinGlass 限频、超时、解析
        # 失败）只记 last_error、绝不自杀 —— 本机出口 IP 已被 Binance 判 418，一次
        # API 抖动若升级成进程重启，会连带打断全部 9 个任务、重置所有错峰 offset，
        # 还会打出 OI 采样桶缺口（复验 P2-1），把局部故障放大成全局停产。外部依赖
        # 故障由数据新鲜度 + last_ok_at 告警负责暴露，不需要重启。
        if hb_ok:
            fail_streak = 0
        else:
            fail_streak += 1
        if fail_streak >= MAX_CONSEC_FAILURES:
            print(f"[scan_daemon][{name}] 连续 {fail_streak} 轮连心跳都写不进 DB"
                  f"（最近: {err}），判定进程级故障，主动退出交 supervisord 重启",
                  file=sys.stderr)
            os._exit(1)

        # 计算下一轮等待时间（扣除本轮耗时，保持固定节奏）
        elapsed = time.time() - start_ts
        sleep_time = max(1.0, interval_sec - elapsed)
        time.sleep(sleep_time)


# 任务定义：(name, interval_sec, offset_sec, func, kwargs)
TASK_DEFS = [
    ("scan_klines",       300,  0,  task_scan_klines,       {"min_vol_usd": 5_000_000}),
    ("scan_oi_cvd",       300,  120, task_scan_oi_cvd,      {"min_vol_usd": 5_000_000}),
    ("scan_liquidation",  300,  240, task_scan_liquidation,  {}),
    ("scan_alert",        300,  180, task_scan_alert,       {}),
    ("scan_main_pool",    900,  60,  task_scan_main_pool,    {}),
    ("scan_accumulation", 1800, 0,  task_scan_accumulation, {}),
    ("scan_squeeze",      300,  420, task_scan_squeeze,     {"min_vol_usd": 5_000_000}),
    ("watchlist_monitor", 1800, 300, task_watchlist_monitor, {}),
    ("expire_signals",    1800, 900, task_expire_signals,    {}),
    ("prune_scan_data",   86400, 600, task_prune_scan_data, {}),
]


def main() -> int:
    # 立即加固 stdout/stderr：容器日志设施断开后任何 print 都会抛
    # ValueError 并（在旧实现里）被吞成「单轮失败」，导致业务函数永不执行
    # （审计 P0-A）。此后所有日志（含 binance_http 的内部日志）写失败即丢弃。
    _harden_streams()

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
        if _name in ("scan_klines", "scan_oi_cvd", "scan_squeeze") and "min_vol_usd" in kwargs:
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

    # 单实例闸门（审计 P0-3）：旧码实例与新码实例并行会重复采集、
    # 重复告警去重键并存，并抬高 Binance 出口 IP 被封概率。
    # 用会话级 advisory lock 保证同一时刻只有一个本进程在跑，并持有到进程退出。
    if not _acquire_singleton_lock(db_pool):
        print("[scan_daemon] ⚠️ 已有 scan_daemon 实例在运行（advisory lock 被占用），本次退出",
              file=sys.stderr)
        return 1

    _SCHEDULED_TASKS.update(t[0] for t in tasks_to_run)

    # 进程启动标记（须在各任务线程启动前写）：停摆检测据此判断「某任务在本实例
    # 里是首轮还没跑完（宽限）」还是「从未启动（真故障）」，否则每次部署后
    # 都会因首轮未完成而误报一次停摆告警（2026-09-21 实测触发）。
    _write_heartbeat(DAEMON_START_TASK, True)

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
