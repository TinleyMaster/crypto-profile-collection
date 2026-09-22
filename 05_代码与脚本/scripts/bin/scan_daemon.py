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
  - [30min]  expire_signals    — 信号生命周期巡检（active/confirmed → expired）
  - [30min]  confirm_signals   — 延续确认巡检（active → confirmed，越过 breakout_px）
  - [24h]    prune_scan_data   — 采集数据保留期清理（保留 30 天）

设计原则：
  - 单进程，多线程调度（每个任务独立线程，内部循环）
  - SkipIfRunning：上一轮未结束则跳过本轮（防堆积）
  - 共享 DB 连接池（psycopg_pool）和 requests Session
  - 单任务崩溃不影响其他任务（try/except 包裹每轮）
  - 内存占用 ≈ 100-150MB（对比 7 个独立进程的 ~700MB）

用法：
    python scan_daemon.py                    # 启动全部任务
    python scan_daemon.py --run-once expire_signals  # 只跑一次指定任务（调试；需先停常驻实例，同样取单实例锁）
    python scan_daemon.py --only klines,oi   # 只启动指定任务
    python scan_daemon.py --min-vol-usd 5000000  # 过滤低流动性合约
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
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

    def reconfigure(self, **kwargs):
        """就地重配置底层流；失败即忽略。

        `__getattr__` 会把 `reconfigure` 转发给底层流，而底层流若已被关闭，
        该调用抛 `ValueError: I/O operation on closed file.` —— 这正是 2026-09-21
        停摆的引信之一（`_wait_until_ban_expires()` 里的 `_log` 走 print 抛错，
        使 `_get_usdt_perpetuals()` 这类**不在 try 内**的调用直接判整轮失败）。
        显式定义并吞掉异常，代理才真正「写失败即丢弃」。
        """
        try:
            return self._inner.reconfigure(**kwargs)
        except Exception:  # noqa: BLE001
            return None

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
# 判定窗口连续性闸门的阈值与实现统一收在 `squeeze.MAX_MID_GAP_BUCKETS` /
# `squeeze.window_gate()`（纯函数，可离线注入单测，见 workbench/test_squeeze_battle.py）。
# 此处只保留引用，避免两处实现漂移。

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
# 每周期的触发根**优先取最近一根已收盘条**、不合格才回退未收盘条（2026-09-22 修 P1
# 漏检，见 `_l1_screen` docstring）；入场/失效位锚定被判定的那一根。
# ⚠️ 1h 的 3.0 是**线上口径，刻意不改**（2026-09-21 决策，见设计文档 §4.2 / §12.1-14）：
#   回测 backtest_scan_scenarios.py 的 PRICE_THR_1H=4.0 是「单周期 1h 单根」口径（无 5m/15m 通道），
#   与这里「三周期并列、15m 2.0% 也能独立入池」不是同一个量。把本值改成 4.0 属伪对齐——
#   回测里被 4.0 滤掉的「1h 涨 3~4%」样本，在线上多数已被 15m 通道先捕获，改了并不削减那批样本。
PRICE_THR = {"5m": 1.5, "15m": 2.0, "1h": 3.0}
VOL_RATIO_THR = 2.0
LOOKBACK_BARS_MAIN = 20
OI_RISE_BARS = 2
LEVEL_RANK = {"5m": 1, "15m": 2, "1h": 3}
# 失效位幅度（工单 P2-4）：原用「触发周期近 21 根反向极值」，实测幅度**不可用** ——
# XMR -11.97% / EPIC -8.02% / 1000BONK -8.13% / APT -6.90%，一个自身涨幅仅 +4.69%
# 的多头信号配 -11.97% 止损 ⇒ 风险回报倒挂；而窄幅盘整时 21 根极值可能只 0.3%
# ⇒ 被噪音打掉。改用 2×ATR(14) 并夹在 [STOP_PCT_MIN, STOP_PCT_MAX] 带内：与波动率
# 挂钩（自适应），且两端有界（幅度可用）。主池与 BRK 共用（P2-9）。
#
# 倍数与夹带标定（2026-09-22，离线回放 131 条已满 24h 的主池信号 / 1h K 线；
# 入场 = 触发根收盘价，离场 = signal_ts+24h，方向对齐；窗口内先触失效位记 -stop）：
#   · **纯 24h 持有基线**：均 +1.44% / 中 +0.32% / 胜 53.4% —— 任何 ≤5% 的失效位
#     都把期望做差（m=1.5 夹[3,12] 均 +0.58%、固定 3% 均 +1.28% 但止损率 68.7%）。
#   · **固定百分比扫描在 8% 见顶**（唯一呈「内部极值」而非单调）：3%→+1.28%/止损68.7%
#     、5%→+1.29%/52.7%、6%→+1.58%/42.7%、**8%→+1.69%/32.8%**、10%→+1.29%/26.7%、
#     12%→+1.04%、20%→+0.84%。即「宽到躲开噪音、窄到能截掉尾部」的位置在 8%。
#   · 带内扫描同向收敛：m=2.0 带[3,12] 均 +0.53%/止损 57.3% → 带[6,15] +1.05%/38.9%
#     → **带[8,20] +1.60%/31.3%**（与固定 8% 同量级，但 ATR 会给高波动币放宽）。
#   ⇒ 取 STOP_PCT_MIN=8.0 使得**多数信号落在 8% 下限**（m=2.0 的 ATR 幅度中位 3.56%、
#     p75 6.14%，仅约 1/4 越过 8%），ATR 只在波动显著更大时放宽；上限 20% 覆盖
#     p90(9.48%)~极值(48%) 的尾部，避免旧口径那种 48%~88% 的荒谬失效位。
#   ⚠️ 样本 131 条 / 约 6 天 / 单边上涨为主（90 多 : 41 空）⇒ 统计力有限，勿据此微调；
#     且失效位当前**仅作风险披露渲染**，未接任何自动平仓（数据证明止损会削期望）。
STOP_ATR_MULT = 2.0
STOP_ATR_PERIOD = 14
STOP_PCT_MIN = 8.0
STOP_PCT_MAX = 20.0
# 渲染/日志用的带内文案**由常量派生**：2026-09-22 重标定 [3%,12%]→[8%,20%] 时，
# 邮件正文与图例里三处硬编码的 `[3%,12%]` 漏改，用户看到的仍是旧带 ⇒ 加此常量
# 让文案随常量走，杜绝同类漂移（改带只需改上面两行）。
STOP_BAND_TXT = f"{STOP_PCT_MIN:.0f}%~{STOP_PCT_MAX:.0f}%"

# ── 数据新鲜度护栏 ──────────────────────────────────────────────
# 各周期最新 K 线 open_time 距今最大分钟数（采集每 5min、扫描每 15min，留足余量；
# 超过即视为数据陈旧，跳过该币，避免采集停摆时用旧数据出假信号——见 2026-09-18 USELESS 事件）
MAX_KLINE_AGE_MIN = {"5m": 15, "15m": 35, "1h": 80}
# 最新 OI 桶 ts 距今最大分钟数（采样每 5min、错峰 +2min）
MAX_OI_BUCKET_AGE_MIN = 20
# 蓄势池：按小时聚合的 OI 桶允许的最大年龄（分钟）
MAX_OI_ACC_AGE_MIN = 90
# ── L0 市场环境阈值（审计 P1-2 标定）─────────────────────────────
# 原值 `btc_1h ±1.0` / `fgi 25·75` / `cap_trend ±1.0` 里两个近乎死条件。用近 7 天
# 「up + OI↑」触发子集（context_tags 回放，n=98）复算：原阈值下唯一真正降级的维度
# 是 `cap_trend`，`btc_1h` 从未越过 ±1.0、`fgi` 从未越过 75 ⇒ `confidence='high'`
# 的实际含义退化成「涨 + OI 涨」，regime 只当摆设。按「与触发条件同构的尺度」收紧：
# 单币触发阈值是 1.5%~3%（PRICE_THR），BTC 1h 取 0.5% 作方向性门槛。
# ⚠️ 收紧后同一份回放里 high 条数不变（60/98）——该子集的 btc_1h 全在 ±0.5% 内、
# fgi 最低 63，故无回归风险；改动的作用是让下一次真实回调/贪婪时能提前否决。
REGIME_BTC_1H_THR = 0.5
REGIME_FGI_GREED = 72
REGIME_FGI_FEAR = 28
REGIME_CAP_TREND_THR = 0.5

# 采集停摆告警：数据年龄超过该分钟数即视为停摆；同一告警最短重发间隔（小时）
STALL_ALERT_AGE_MIN = 30
STALL_ALERT_MIN_INTERVAL_H = 6
# 任务心跳停摆：心跳年龄 > N × 任务周期 即视为该任务停产（与外部看门狗
# check_scan_freshness.py 的 HEARTBEAT_MAX_AGE_MIN 保持同一口径：3× 周期）
STALL_HEARTBEAT_GRACE = 3
# 「某任务在本实例内尚未跑完首轮」这一状态的宽限上限（分钟）。该状态的等待时间应由
# **该任务的 offset** 决定（首轮最早可能的时刻），而不是 3×周期：周期 1800s / 86400s
# 的任务会得到 90 / 4320 分钟，远大于容器重启周期（实测约 15 分钟）⇒ 每次重启都刷新
# 宽限，「线程从未启动」被无限期掩盖（工单 P0-1 根因②：4 次查询、跨 2 个实例，
# expire_signals 始终无心跳行而两条监控同时静默）。
# ⚠️ 必须 > TASK_DEFS 中最大 offset（现为 `prune_scan_data` 的 600s；改 offset 时同步复核）
#    + 首轮函数自身耗时；
#    且必须与 check_scan_freshness.FIRST_ROUND_GRACE_MAX_MIN 同值。
FIRST_ROUND_GRACE_MAX_MIN = 30.0
# 参与心跳检查的任务（审计 P0-B：原先漏了 squeeze/liquidation/watchlist，
# 而 2026-09-21 停摆中唯一留下物证的恰恰是盲区里的 scan_squeeze）。必须与
# TASK_DEFS 全覆盖（阈值由各自周期 ×STALL_HEARTBEAT_GRACE 自动推导）。
STALL_HEARTBEAT_TASKS = ("scan_klines", "scan_oi_cvd", "scan_liquidation", "scan_alert",
                         "scan_squeeze", "scan_main_pool", "scan_accumulation",
                         "watchlist_monitor", "expire_signals", "confirm_signals",
                         "prune_scan_data")
# 连续失败自杀：同一任务连续 N 轮**连心跳都写不进 DB** 即退出进程，交 supervisord
# 拉起（审计 P1-4）。半死进程（stdout 关闭/线程卡死）会一直占着单实例锁，新实例
# 永远起不来 —— 这是 2026-09-18 63h、2026-09-21 48min 两次停摆的共同放大器。
# ⚠️ 判据是「进程级故障」而不是「任务失败」（审计复验 P1-1）：外部依赖抖动
# （Binance/CoinGlass 限频、超时）只记 last_error，绝不重启进程，否则会把一次
# API 抖动放大成全局停产（见 _run_task_loop 注释）。
MAX_CONSEC_FAILURES = 3
# 进程级「无产出」看护（2026-09-21 停摆 14h+ 的直接补救）。
# 背景：任务线程可能**静默死亡**（异常发生在 try 之外的每轮首行 print）或**永久卡住**
# （外部 API 无界退避），而主线程只是 sleep ⇒ 进程活着、持着单实例锁、
# supervisord 因「进程未退出」永不重启 ⇒ 无限期停摆，只有人工干预才能恢复。
# 判据取「全进程最近一次『有任务跑完一轮』距今的分钟数」：最频繁的任务周期 300s，
# 故 30 分钟 ≈ 6 个周期都没跑完一轮，此时进程已确定无产出，退出交 supervisord 重启。
# ⚠️ 必须远大于最大 offset（现为 `prune_scan_data` 的 600s）+ 单轮耗时，否则会把正常的慢轮误判成卡死。
HANG_EXIT_MIN = 30.0
# 主线程看护的检查周期（秒）。取 60s：相对 30 分钟阈值足够密（最多晚 1 分钟发现），
# 又不会让主线程成为负担。
WATCHDOG_TICK_SEC = 60.0
# 全进程最近一次「有任务完成一轮（无论成败）」的单调时钟时刻（_run_task_loop 维护）。
# ⚠️ 哨兵用 None 而非 0.0：`time.monotonic()` 的起点是**系统/容器启动时刻**，在刚启动
# 不久的机器上「当前时刻 − 30 分钟」是负数，用 0.0 当哨兵会与合法值混淆、把卡死判成
# 正常（本机实测 uptime 21 分钟时 monotonic()=1276s，31 分钟前 = −584s）。
_LAST_ANY_ROUND_TS: float | None = None
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
    """L0 市场环境。返回 {tags: [...], long_fav: bool, short_fav: bool}。

    阈值见 REGIME_* 常量。被否决时把**原因**写进 tags（审计 P1-2）：否则
    `confidence='high'` 无从解释 —— 邮件里三个 regime 标签看不出是谁在否决、
    甚至看不出发生过否决。
    """
    tags: list[str] = []
    long_fav = short_fav = True
    long_block: list[str] = []
    short_block: list[str] = []

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
            if btc_1h < -REGIME_BTC_1H_THR:
                long_fav = False
                long_block.append(f"BTC1h{btc_1h:+.2f}%")
            elif btc_1h > REGIME_BTC_1H_THR:
                short_fav = False
                short_block.append(f"BTC1h{btc_1h:+.2f}%")
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
            if fgi < REGIME_FGI_FEAR:
                long_fav = False
                long_block.append(f"FGI{fgi:.0f}")
            elif fgi > REGIME_FGI_GREED:
                short_fav = False
                short_block.append(f"FGI{fgi:.0f}")
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
            if trend < -REGIME_CAP_TREND_THR:
                long_fav = False
                long_block.append(f"市值{trend:+.2f}%")
            elif trend > REGIME_CAP_TREND_THR:
                short_fav = False
                short_block.append(f"市值{trend:+.2f}%")
    except Exception:
        pass

    if not long_fav:
        tags.append(f"多头环境受限（{'/'.join(long_block) or '原因未知'}）")
    if not short_fav:
        tags.append(f"空头环境受限（{'/'.join(short_block) or '原因未知'}）")
    return {"tags": tags, "long_fav": long_fav, "short_fav": short_fav}


def _last_closed_idx(rows: list[dict], iv: str, now: datetime) -> int | None:
    """最近一根**已收盘**条的索引（`open_time + 周期时长 <= now`），全未收盘返回 None。"""
    dur = INTERVAL_SECONDS[iv]
    for i in range(len(rows) - 1, -1, -1):
        if (now - rows[i]["open_time"]).total_seconds() >= dur:
            return i
    return None


def _l1_eval(rows: list[dict], idx: int, iv: str) -> dict | None:
    """对 `rows[idx]` 做单周期价量粗筛：达标返回 {dir, chg_pct, vol_ratio}，否则 None。

    口径：`|chg| >= PRICE_THR[iv]`（收盘价 vs 前一根收盘价）且 `vol_ratio >= VOL_RATIO_THR`
    （成交额 vs 前 `LOOKBACK_BARS_MAIN` 根均值）。与 `backtest_scan_scenarios.py`
    的 `scan_symbol` 逐根判定同口径（该回测只在已收盘历史条上迭代）。
    """
    if idx < LOOKBACK_BARS_MAIN:
        return None
    closes = [float(r["close_px"]) for r in rows]
    vols = [float(r["quote_vol"]) for r in rows]
    prev_close = closes[idx - 1]
    if not prev_close:
        return None
    chg = (closes[idx] - prev_close) / prev_close * 100
    vol_mean = sum(vols[idx - LOOKBACK_BARS_MAIN:idx]) / LOOKBACK_BARS_MAIN
    vol_ratio = vols[idx] / vol_mean if vol_mean else 0.0
    if abs(chg) < PRICE_THR[iv] or vol_ratio < VOL_RATIO_THR:
        return None
    return {"dir": "up" if chg > 0 else "down", "chg_pct": chg, "vol_ratio": vol_ratio}


def _l1_screen(klines_by_iv: dict[str, list[dict]], now: datetime) -> dict | None:
    """L1 粗筛：单周期异动 + 多周期共振升级。

    新鲜度护栏：最新 K 线 open_time 距今超过 MAX_KLINE_AGE_MIN[iv] 则跳过该周期，
    避免采集停摆时用陈旧数据出假信号。

    **触发根选择（2026-09-22 修 P1 系统性漏检）**：优先最近一根**已收盘**条
    （值稳定 ⇒ 判定可复现），不合格再回退最新（未收盘）条以保持及时性。
    原实现只取 `closes[-1]`，而 `scan_klines` 每 5min 把未收盘条 UPSERT 覆盖
    （同一行 `close_px`/`quote_vol` 随时间内变）⇒ 一根条的「终值可用且仍是最新根」
    窗口仅约 2~5 分钟，而扫描每 15min 一轮、容器重启又不断打乱相位 ⇒ 能否看到
    收盘终值取决于相位。离线量化（6 天 / 280 币）：真异动条 1719 根中 **53.5%
    只有收盘才越阈**，故旧实现存在三到四成的漏检。

    返回 dict 额外带 `bar_idx`（被判定那一根的索引），供调用侧锚定入场/失效位。
    """
    best = None
    best_level = 0
    for iv in ("1h", "15m", "5m"):
        rows = klines_by_iv.get(iv, [])
        if len(rows) < LOOKBACK_BARS_MAIN + 1:
            continue
        if (now - rows[-1]["open_time"]).total_seconds() / 60 > MAX_KLINE_AGE_MIN[iv]:
            continue  # 该周期数据陈旧，不参与判定
        last_closed = _last_closed_idx(rows, iv, now)
        # 候选顺序：已收盘条 → 未收盘条（后者仅在前者未越阈时兜底）
        candidates = [i for i in (last_closed, len(rows) - 1) if i is not None]
        if len(candidates) == 2 and candidates[0] == candidates[1]:
            candidates = candidates[:1]
        for idx in candidates:
            hit = _l1_eval(rows, idx, iv)
            if hit is None:
                continue
            level = LEVEL_RANK[iv]
            if level > best_level:
                best_level = level
                best = {**hit, "iv": iv, "level": level, "bar_idx": idx}
            break
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
    # 审计 P1-3：原实现只把和的**符号**落库（`cvd_dir`），金额从未入库 ⇒ 渲染层
    # 不可能显示幅度，也无法区分「杠杆推涨（OI↑+价↑+现货主动卖）」。
    # 现同时落 `cvd_usd`（净额）与 `cvd_ratio`（净额 / 同窗口成交额）。
    win = oi_rows[-OI_RISE_BARS:] if len(oi_rows) >= OI_RISE_BARS else oi_rows
    cvd_vals = [float(r["cvd_5m_usd"]) for r in win if r.get("cvd_5m_usd") is not None]
    vol_vals = [float(r["vol_5m_usd"]) for r in win if r.get("vol_5m_usd") is not None]
    cvd_sum = sum(cvd_vals) if cvd_vals else None
    if cvd_sum is None:
        cvd_dir = None
    else:
        cvd_dir = "up" if cvd_sum > 0 else "down" if cvd_sum < 0 else None
    vol_sum = sum(vol_vals) if vol_vals else None
    cvd_ratio = (cvd_sum / vol_sum) if (cvd_sum is not None and vol_sum) else None

    fr = _lookup_funding(funding_map, symbol)
    scenario = f"S{1 if oi_dir=='up' and direction=='up' else 2 if oi_dir=='up' and direction=='down' else 3 if oi_dir=='down' and direction=='up' else 4}"
    return {
        "oi_dir": oi_dir,
        "oi_chg_pct": oi_chg,
        "cvd_dir": cvd_dir,
        "cvd_usd": cvd_sum,
        "cvd_ratio": cvd_ratio,
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


def _atr_stop_pct(bars: list[dict], ref_px: float) -> float | None:
    """失效位幅度（%）：`STOP_ATR_MULT × ATR(STOP_ATR_PERIOD)` / 参考价，夹在带内。

    `bars` 需含 `high_px` / `low_px` / `close_px`（按时间升序，最后一根为触发条）。
    数据不足 `STOP_ATR_PERIOD + 1` 根时返回 None —— **不落库也不兜底**，渲染层
    按「无失效位」显示（与费率/共振的 n/a 口径一致：不知道就别说）。
    """
    if ref_px <= 0 or len(bars) < STOP_ATR_PERIOD + 1:
        return None
    win = bars[-(STOP_ATR_PERIOD + 1):]
    trs = []
    for prev, cur in zip(win, win[1:]):
        h, l, pc = float(cur["high_px"]), float(cur["low_px"]), float(prev["close_px"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs) / len(trs)
    pct = STOP_ATR_MULT * atr / ref_px * 100
    return max(STOP_PCT_MIN, min(STOP_PCT_MAX, pct))


def task_scan_main_pool(cooldown_h: float = 6.0) -> dict:
    """主池扫描（单轮）。"""
    with _db() as conn:
        regime = _build_regime(conn)

        # 最近 1 天 K 线
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT symbol, interval, open_time, high_px, low_px, close_px, quote_vol "
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
                "SELECT symbol, ts, oi_usd, cvd_5m_usd, vol_5m_usd FROM biz.oi_cvd_snapshot "
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
            # 入场/失效位（审计 P2-4：表里有 trigger_price / stop_loss_pct，但主池
            # 52 条告警 100% 为空 —— 属**生产者从未计算**）。
            # 入场 = **被 L1 判定那一根**的收盘价（`l1["bar_idx"]`，多为已收盘条；
            # 见 `_l1_screen` 的触发根选择注释）；失效 = 2×ATR(STOP_ATR_PERIOD) 夹在
            # [STOP_PCT_MIN, STOP_PCT_MAX] 带内（值见常量注释，文案用 STOP_BAND_TXT）
            # （工单 P2-4：原「近 21 根反向极值」幅度不可用，实测 -11.97% 配 +4.69%
            #  涨幅 ⇒ 风险回报倒挂；窄幅盘整时又会紧到 0.3% 被噪音打掉）。
            # ⚠️ ATR 窗口必须截断到该根（`bars[:bar_idx+1]`），否则「判定的根」与
            #    「算 ATR 的根」不一致（原实现两者都写死 `bars[-1]` 才侥幸自洽）。
            trig_px = brk_px = stop_pct = None
            bars = by_sym_k[sym].get(l1["iv"]) or []
            if l1.get("bar_idx") is not None and l1["bar_idx"] < len(bars):
                bar = bars[l1["bar_idx"]]
                trig_px = float(bar["close_px"])
                stop_pct = _atr_stop_pct(bars[:l1["bar_idx"] + 1], trig_px)
                # 延续确认位（迁移 fix_061）：触发根的**方向侧极值**，与 trigger_price
                # 取同一根。该根可能是未收盘条（回退分支）⇒ 极值是「截至信号时刻」的
                # 运行极值，无未来信息；又因 high ≥ close ≥ low，该位恒在入场价的正确
                # 一侧（做多在价上、做空在价下）⇒ 不会出现「一建仓就已确认」。
                brk_px = float(bar["high_px"] if direction == "up" else bar["low_px"])
            signals.append((
                now, sym, l2["scenario"], l1["iv"], direction, round(l1["chg_pct"], 2),
                "up" if l1["vol_ratio"] >= VOL_RATIO_THR else "flat",
                round(l1["vol_ratio"], 2), l2["oi_dir"], round(l2["oi_chg_pct"], 2),
                l2["cvd_dir"], l2["funding_rate"], confidence, ctx_tags,
                None if l2["cvd_usd"] is None else round(l2["cvd_usd"], 2),
                None if l2["cvd_ratio"] is None else round(l2["cvd_ratio"], 6),
                None if trig_px is None else round(trig_px, 8),
                None if stop_pct is None else round(stop_pct, 3),
                None if brk_px is None else round(brk_px, 8),
            ))

        if signals:
            insert_sql = """
                INSERT INTO biz.scan_signal
                    (signal_ts, symbol, pool, scenario, timeframe, p_dir, price_chg_pct,
                     vol_state, vol_ratio, oi_dir, oi_chg_pct, cvd_dir, funding_rate,
                     confidence, context_tags, cvd_usd, cvd_ratio, trigger_price,
                     stop_loss_pct, breakout_px, status)
                VALUES (%s,%s,'main',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active')
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


def _detect_brk(k1h: list[dict], acc_range: tuple[float, float], now: datetime,
                max_age_min: float | None = None) -> dict | None:
    if len(k1h) < LOOKBACK_BARS_ACC + 1:
        return None
    # 传进来的必须是**已收盘**的 1h 条（见 task_scan_accumulation），其年龄天然比
    # 未收盘条多 0~60 分钟 ⇒ 阈值放宽一个周期，否则每小时前 40 分钟会误判「陈旧」。
    age_limit = MAX_KLINE_AGE_MIN["1h"] if max_age_min is None else max_age_min
    if (now - k1h[-1]["open_time"]).total_seconds() / 60 > age_limit:
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
                "SELECT symbol, open_time, high_px, low_px, close_px, quote_vol "
                "FROM biz.asset_klines "
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
            # BRK 已判决过的（symbol, 触发条 open_time）（工单 P2-7）：周期 1800s 而
            # 「已收盘条」从收盘到不再是 closed[-1] 的窗口是 3600s ⇒ 同一根条会被
            # 连续两轮判定（09:00 轮与 09:30 轮都以 08:00 根为 closed[-1]），
            # biz.scan_signal 对 BRK 无唯一约束 ⇒ 同一突破插 2 行（告警层 12h 冷却
            # 兜底不会重复发信，但库与统计重复）。用 context_tags 里的
            # `bar=<open_time>` 标签做**精确**去重（不按时间窗近似）。
            cur.execute(
                "SELECT symbol, tag FROM biz.scan_signal s, "
                "LATERAL unnest(s.context_tags) AS tag "
                "WHERE s.scenario='BRK' AND s.signal_ts > NOW() - INTERVAL '3 hours' "
                "AND left(tag, 4) = 'bar='"
            )
            brk_done: dict[str, set[str]] = {}
            for r in cur.fetchall():
                brk_done.setdefault(r["symbol"], set()).add(r["tag"])

        now = datetime.now(timezone.utc)
        signals: list[tuple] = []
        acc_skipped_cooldown = 0
        brk_dup_skipped = 0
        for sym in sorted(by_sym_oi):
            if sym in acc_cooldown:
                acc_skipped_cooldown += 1
                continue
            acc = _detect_acc(sym, by_sym_oi[sym], by_sym_k.get(sym, []), funding_map, now)
            if not acc:
                continue
            tags = [f"oi_rise={acc['oi_rise_ratio']:.0f}%", f"oi_cum={acc['oi_cum_chg']:.1f}%",
                    acc["fund_label"]]
            # ⚠️ 占位符数量必须与下面 accumulation 的 INSERT 严格一致（AST 已核对）：
            # 末三位依次是 trigger_price / stop_loss_pct / breakout_px，ACC 三者皆 None
            # —— ACC 是「区间内蓄势、无方向」，既无入场位也无延续确认位（不进
            # confirm_signals 的样本集）。此前 tuple 少一个元素（15 vs 16），
            # 只要有一轮同时出现 ACC 候选，executemany 就会抛「占位符不匹配」而整批失败。
            signals.append((now, sym, "accumulation", "ACC", "1h", "flat",
                            acc["chg1h"] or 0, "flat", acc["vol_ratio"] or 0.0,
                            "up", acc["oi_cum_chg"], _lookup_funding(funding_map, sym), "medium",
                            tags, None, None, None, "active"))

        for sym in sorted(acc_symbols):
            k1h = by_sym_k.get(sym, [])
            # 审计 P1-4：BRK 通道从未产出（全表 `scenario='BRK'` 0 行）源于两处**不可达**：
            #   ① 区间极值原先把触发条本身也算进去（`k1h[-(OI_HOURS+1):]`），于是
            #      `close > hi` / `close < lo` 数学上恒假 —— close 正是取极值的同一集合成员；
            #   ② 库里的最后一根 1h 是**未收盘的当前小时**（实测 08:09 时 open_time=08:00）。
            #      其 quote_vol 只累积了该小时的一部分 ⇒ 同一根 K 线在不同时刻判定结果
            #      不同、**不可复现**（实测未收盘条 vol_ratio 随采样分钟漂移：min 0.194 /
            #      中位 0.789 / max 8.263，既有 < 1 也有 > 3）。工单 DOC-1 更正：此前
            #      「125 币最高 0.93 ⇒ BRK_VOL_RATIO=3.0 永不可达」是在 08:09（整点后
            #      9 分钟）采样的快照，值随分钟漂移，该论证不成立；正确理由是「不可复现」。
            # 故：只用**已收盘**条，且区间取触发条**之前**的 OI_HOURS 根。
            closed = [k for k in k1h
                      if (now - k["open_time"]).total_seconds() >= INTERVAL_SECONDS["1h"]]
            if len(closed) < OI_HOURS + 1:
                continue
            win = closed[-(OI_HOURS + 1):-1]
            lo = min(float(k["close_px"]) for k in win)
            hi = max(float(k["close_px"]) for k in win)
            brk = _detect_brk(closed, (lo, hi), now,
                              max_age_min=MAX_KLINE_AGE_MIN["1h"] + INTERVAL_SECONDS["1h"] / 60)
            if not brk:
                continue
            brk_stop = _atr_stop_pct(closed, float(brk["break_px"]))
            # P2-7：同一根已收盘条（= closed[-1]）只判决一次
            bar_tag = f"bar={closed[-1]['open_time']:%Y-%m-%dT%H}"
            if bar_tag in brk_done.get(sym, set()):
                brk_dup_skipped += 1
                continue
            tags = [f"brk_{brk['dir']}", f"vol_x={brk['vol_ratio']:.1f}", bar_tag]
            signals.append((now, sym, "accumulation", "BRK", "1h", brk["dir"],
                            None, "up" if brk["vol_ratio"] >= VOL_CAP_RATIO else "flat",
                            round(brk["vol_ratio"], 2), None, None,
                            _lookup_funding(funding_map, sym), "high", tags,
                            round(brk["break_px"], 8),
                            # P2-9：BRK 原先只落 trigger_price、不落 stop_loss_pct
                            # ⇒ 卡片有入场价、无失效位。与主池同口径（2×ATR 夹带）。
                            None if brk_stop is None else round(brk_stop, 3),
                            # 延续确认（fix_061）：BRK **本身就是突破事件** —— 它要求
                            # 一根**已收盘** 1h 条的收盘价对前 OI_HOURS 根区间极值
                            # 越位、且量比 ≥ BRK_VOL_RATIO。故不写 active 等确认，
                            # 直接落 confirmed（= 延续已被市场跟随），breakout_px 记
                            # 突破位。若走 confirm_signals，需等**下一根** 1h 收盘越过
                            # 本根突破价，语义上多了 1h 无谓滞后。
                            round(brk["break_px"], 8), "confirmed"))

        if signals:
            insert_sql = """
                INSERT INTO biz.scan_signal
                    (signal_ts, symbol, pool, scenario, timeframe, p_dir, price_chg_pct,
                     vol_state, vol_ratio, oi_dir, oi_chg_pct, funding_rate, confidence,
                     context_tags, trigger_price, stop_loss_pct, breakout_px, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """
            with conn.cursor() as cur:
                cur.executemany(insert_sql, signals)
        conn.commit()

    acc_count = sum(1 for s in signals if s[2] == 'accumulation' and s[3] == 'ACC')
    brk_count = sum(1 for s in signals if s[2] == 'accumulation' and s[3] == 'BRK')
    return {"oi_symbols": len(by_sym_oi), "ACC": acc_count, "BRK": brk_count,
            "acc_skipped_cooldown": acc_skipped_cooldown,
            "brk_dup_skipped": brk_dup_skipped}


# ═══════════════════════════════════════════════════════════════
#  任务 5：告警监控（5 分钟，错峰 +3min）
# ═══════════════════════════════════════════════════════════════

NEW_WINDOW_MIN = 20
COOLDOWN_H = 12
CATALYST_DAYS = 7
# 催化剂「陈旧」阈值（天，审计 N1）：7 天窗口会把已过期/上周的催化剂也计入共振，
# 稀释新鲜度（实测 BTWUSDT 5 条中 1 条为 09-16、且已 expired）。渲染层对超过本
# 阈值的条数显式标注「含 N 条 >X 天」，让收件人自行打折；**不改窗口本身**
# （窗口语义与 catalyst_signal 对齐，缩短会改变共振口径）。
CATALYST_STALE_DAYS = 3
KOL_DAYS = 7
# 「丢信号」检测回溯上限（小时）。**必须有界**：库里存在历史遗留的
# `alerted_at IS NULL` 的 high 行（如 09-16 那次停摆的 8 条），无界查询会让
# 停摆告警被这批陈年行永久钉住 —— 每 6h 去重期一过就再发一封，而当时并没有停摆。
LOST_SIGNAL_LOOKBACK_H = 6
# 跨池互斥窗口（分钟，审计 P1-5）：同币在 main 与 squeeze 两个通道各发一封、口径相反
# （实测 XMR 39min / 龙虾 41min，间隔都 < 60min）。窗口内只发**先到**的那封，后者标
# `alert_suppressed_at` 留痕（**不能**只跳过 —— 主池候选集按 `alerted_at IS NULL` 取，
# 不留下标记的话它会在 20 分钟窗口内反复重试、随后又被「丢信号检测」当异常计数）。
CROSS_POOL_MUTE_MIN = 60
# 历史先验（审计 P2-5）：同场景已告警信号的方向对齐后验。用**中位数 + 胜率 + 样本量**
# 呈现（均值会被 AKEUSDT +147.99% 这类离群值绑架，实测 +24h 均值 +8.79% vs 中位 +3.60%）。
PRIOR_HORIZON_H = 12
PRIOR_LOOKBACK_DAYS = 30
PRIOR_MIN_N = 10


def _load_alert_candidates(conn, window_min: int) -> list[dict]:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT id, signal_ts, symbol, pool, scenario, timeframe, p_dir, price_chg_pct,
                   vol_ratio, oi_dir, oi_chg_pct, cvd_dir, funding_rate, context_tags,
                   cvd_usd, cvd_ratio, trigger_price, stop_loss_pct, breakout_px, status
            FROM biz.scan_signal
            WHERE confidence = 'high'
              AND (pool = 'main' OR (pool = 'accumulation' AND scenario = 'BRK'))
              -- ⚠️ 必须含 confirmed（fix_061）：主池/BRK 的 confirmed 是**升格**
              -- （延续已被市场跟随），仍属可动作集合。若这里只筛 active，确认一落地
              -- 这些信号就静默掉出告警 —— 与「突破=质量升格」的语义正好相反。
              AND status IN ('active', 'confirmed')
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


def _cross_pool_recent(conn, symbols: list[str], pools: tuple[str, ...]) -> set[str]:
    """指定池在跨池互斥窗口内**已告警**的符号集合（审计 P1-5）。

    两个通道的告警都落在 `biz.scan_signal.alerted_at`（squeeze 池判定成功后同样回写），
    故互斥判据单点可查、无需新表。
    """
    if not symbols:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT symbol FROM biz.scan_signal "
            "WHERE symbol = ANY(%s) AND pool = ANY(%s) AND alerted_at IS NOT NULL "
            "AND alerted_at > NOW() - make_interval(mins => %s)",
            (symbols, list(pools), CROSS_POOL_MUTE_MIN))
        return {r[0] for r in cur.fetchall()}


def _mark_alert_suppressed(conn, ids: list[int], reason: str) -> None:
    """跨池互斥的**留痕**（不能只跳过）：标 `alert_suppressed_at` 后，
    「丢信号检测」不会再把这批行算成异常（见 `_stall_parts`）。"""
    if not ids:
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE biz.scan_signal SET alert_suppressed_at = NOW(), "
            "alert_suppressed_reason = %s WHERE id = ANY(%s)", (reason, ids))
    conn.commit()


def _scenario_priors(conn, scenarios: list[str]) -> dict[str, dict]:
    """同场景「已告警」信号的方向对齐后验（审计 P2-5）。

    口径：
      - 样本 = 近 `PRIOR_LOOKBACK_DAYS` 天、`alerted_at IS NOT NULL`、非 invalid 的
        主池信号，且 `signal_ts + 12h` 已过（**不混入未到期行**，同 catalyst_outcome 的教训）；
      - 基线 = `signal_ts` 当时最后一根 1h 收盘价，终点 = `signal_ts+12h` 当时最后一根；
      - 方向对齐收益 = `+pct`（做多）/ `-pct`（做空），胜率 = 对齐收益 > 0 的占比。
    用中位数不用均值：+24h 均值被单点 +147.99% 拉高到 +8.79%，而中位仅 +3.60%。

    ⚠️ 选择偏置（工单 P2-6）：样本限 `alerted_at IS NOT NULL` ⇒ 只覆盖**告警期**，
    而告警本身依赖 regime 顺风（实测覆盖 S1 31/67、S2 2/15、S3~S8 0/50；S1 正是
    「价↑+OI↑」的顺风场景）⇒ 正期望是该口径的**必然**结果。故渲染层必须显式标注
    「仅含已告警样本，非无偏基准」，不得当作信号质量证据。
    """
    if not scenarios:
        return {}
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            WITH s AS (
                SELECT sg.scenario, sg.p_dir,
                       (SELECT k.close_px FROM biz.asset_klines k
                         WHERE k.symbol = sg.symbol AND k.interval = '1h'
                           AND k.open_time <= sg.signal_ts
                         ORDER BY k.open_time DESC LIMIT 1) AS px0,
                       (SELECT k.close_px FROM biz.asset_klines k
                         WHERE k.symbol = sg.symbol AND k.interval = '1h'
                           AND k.open_time <= sg.signal_ts + make_interval(hours => %s)
                         ORDER BY k.open_time DESC LIMIT 1) AS px1
                FROM biz.scan_signal sg
                WHERE sg.pool = 'main' AND sg.alerted_at IS NOT NULL
                  AND sg.status <> 'invalid' AND sg.p_dir IS NOT NULL
                  AND sg.scenario = ANY(%s)
                  AND sg.signal_ts > NOW() - make_interval(days => %s)
                  AND sg.signal_ts < NOW() - make_interval(hours => %s)
            )
            SELECT scenario, p_dir, px0, px1 FROM s
            WHERE px0 IS NOT NULL AND px1 IS NOT NULL AND px0 <> 0
            """,
            (PRIOR_HORIZON_H, scenarios, PRIOR_LOOKBACK_DAYS, PRIOR_HORIZON_H))
        rows = cur.fetchall()

    buckets: dict[str, list[float]] = {}
    for r in rows:
        raw = (float(r["px1"]) - float(r["px0"])) / float(r["px0"]) * 100
        aligned = raw if r["p_dir"] == "up" else -raw
        buckets.setdefault(r["scenario"], []).append(aligned)

    out: dict[str, dict] = {}
    for sc, vals in buckets.items():
        if len(vals) < PRIOR_MIN_N:
            continue
        vals.sort()
        n = len(vals)
        median = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
        win = sum(1 for v in vals if v > 0) / n * 100
        out[sc] = {"n": n, "median": median, "win": win, "horizon": PRIOR_HORIZON_H}
    return out


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


# 源名白名单与元数据前缀（复验 P1-N1 附注：原实现只看「分隔符前 ≤16 字符」就剥离，
# **不看内容**，会把「BTC 上涨 5%，原因如下」这类正文当源前缀剥掉）。
# 实测近 30 天 1907 条带前缀标题的片段分布：1431 条以「消息」结尾、16「快讯」、
# 16「讯」、4「报道」、1「日报」，其余为源名（火星财经/ChainCatcher/BlockBeats/
# PANews/动察 Beating）与元数据前缀（作者/撰文/原文标题/On Sep 18）。
# 实测旧口径在近 30 天有 108 个片段属**正文**却被剥掉（如「A whale address，」
# 「Strategy，」），新判据一律不剥。
_SRC_NAMES_LOW = ("火星财经", "chaincatcher", "blockbeats", "panews", "金色财经", "动察",
                  "beating", "吴说", "odaily", "律动", "jin10", "wallstreetcn",
                  "华尔街见闻", "binance", "币安", "coindesk", "cointelegraph",
                  "theblock", "blockworks", "decrypt", "cryptoslate", "foresight")
_SRC_MARKERS = ("消息", "快讯", "讯", "报道", "日报", "公告", "news", "report")
_META_HEADS = ("作者", "撰文", "原文标题", "原标题", "来源", "编译")
_EN_DATE_RE = re.compile(r"^on\s+[a-z]{3,9}\.?\s+\d{1,2}", re.IGNORECASE)


def _is_source_prefix(seg: str) -> bool:
    """分隔符前的片段是「源前缀」（可剥）还是「正文」（不可剥）。"""
    low = seg.lower().strip()
    if not low:
        return False
    if seg.startswith(_META_HEADS) or _EN_DATE_RE.match(seg):
        return True          # 「作者」「原文标题」「On Sep 18」
    if low in _SRC_NAMES_LOW:
        return True          # 片段本身就是源名（「BlockBeats」）
    has_marker = any(m in low for m in _SRC_MARKERS)
    has_name = any(n in low for n in _SRC_NAMES_LOW)
    # 有源名 + 消息类标记（「火星财经消息 9月18日」「PANews 9月18日消息」
    # 「动察 Beating AI 快讯」）；无源名但极短（「快讯」「消息」）同样视为前缀。
    return (has_marker and (has_name or len(seg) <= 6))


def _norm_title(title) -> str:
    """标题归一（共振去重用）：去「来源：」前缀 + 仅留字母/数字/汉字。

    审计 P0-2：同一条新闻常被多源转载（原文 / 火星财经 / ChainCatcher），标题仅
    源前缀不同 ⇒ 精确字符串去重会漏。归一到「内容骨架」再比（近似事件聚类；
    仍无法处理真正的同形异义误标，那需在 classify 侧消歧）。

    复验 P1-N1：剥离判据由「纯长度启发式」改为 `_is_source_prefix()` 内容判据，
    避免把正文当源前缀剥掉。
    """
    s = (title or "").strip()
    # 源前缀结尾可能是全/半角冒号或逗号（「火星财经消息，」「ChainCatcher 消息，」
    # 「PANews 9月18日消息，」）→ 取最早出现的分隔符。
    idx = [i for i in (s.find("："), s.find(":"), s.find("，"), s.find(","))
           if 0 < i <= 16]
    if idx and _is_source_prefix(s[:min(idx)]):
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
                 "catalyst_raw": 0,
                 # 审计 N1：最新一条催化剂的 published_at 与「>CATALYST_STALE_DAYS 天」的
                 # 条数，供渲染层披露新鲜度（7 天窗口会把上周/已过期催化剂也算进共振）。
                 "catalyst_latest": None, "catalyst_stale": 0,
                 # 审计 O6：全量（去重后）结构化明细，仅供告警落 `detail` 快照回放用，
                 # **不参与渲染** —— 渲染只需方向构成计数（见下 `catalyst` 明细列表）。
                 "catalyst_all": [],
                 # 是否关联到 core.asset。未关联 ⇒ 催化剂/KOL 两段**无从查询**，
                 # 渲染时必须是 n/a 而不是 0（审计 P2-3：0 与「没这个数据」不可辨）。
                 "asset_linked": bool(asset_id)}
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
    fresh_cut = datetime.now(timezone.utc) - timedelta(days=CATALYST_STALE_DAYS)
    for r in rows:
        # 去重键长度 40 → 80（复验 P1-N1）：英文新闻大量以固定模板开头
        # （"According to the announcement from Binance, the …"），前 40 个字母数字
        # 字符全是模板，真正内容在第 40 之后 ⇒ 截断会**把不同公告并成一条**。
        # 近 30 天实测（`catalyst_impact ⋈ asset_catalyst`，4,394 行）：旧口径
        # 误合并 208 组 / 吞掉 249 条独立新闻、77/642=12.0% 资产的条数被低估，
        # 并让「利空为主」的红色警示漏报；改为 80 后误合并 1 组（唯一 key 2856，
        # 完整归一 2857，几乎无损）。
        key = _norm_title(r["title"])[:80]
        if not key or key in seen:
            continue
        seen.add(key)
        d = str(r["impact_direction"] or "neutral").lower()
        d = d if d in out["catalyst_dir"] else "neutral"
        out["catalyst_dir"][d] += 1
        pub = r["published_at"]
        if pub is not None:
            if out["catalyst_latest"] is None or pub > out["catalyst_latest"]:
                out["catalyst_latest"] = pub
            if pub < fresh_cut:
                out["catalyst_stale"] += 1
        # O6 回放用全量结构化明细（渲染层不读此键）
        out["catalyst_all"].append({
            "title": r["title"], "dir": d,
            "strength": r["impact_strength"],
            "published_at": pub,
        })
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


def _fmt_usd(v: float) -> str:
    """美元金额缩写：-1.2M / +340K（审计 P1-3 的 CVD 幅度渲染）。"""
    a = abs(v)
    if a >= 1e6:
        return f"{v / 1e6:+.2f}M"
    if a >= 1e3:
        return f"{v / 1e3:+.1f}K"
    return f"{v:+.0f}"


def _catalyst_total(cd: dict) -> int:
    """催化剂「归一标题去重后」的全量条数 = 方向构成三项之和（≤20）。

    复验 P1-N2：卡片里的 `N` 原取明细列表长度（只留前 4 条），与括注方向合计
    （≤20）不是同一集合 —— 实测会渲染出「催化剂 4（9多/1空/9中）」这种自相矛盾的
    结果（全库 282 币中 35 个、12.4% 命中）。两处统一用本函数。
    """
    return sum(int(cd.get(k, 0)) for k in ("bullish", "bearish", "neutral"))


def _alert_title(items: list[dict]) -> str:
    """邮件标题按**实际**共振条数生成，并给出催化剂方向构成。

    审计 P1-1：原为硬编码「含共振」，与内容相反。
    复验 P2-N6：只报总数会把利空也算成「多重共振支持」——实测「含共振 8 条」里
    6 条是利空/中性，故补方向构成。
    复验 P1-N2：条数必须取**去重后的全量**（`catalyst_dir` 合计；明细列表只留前 4 条，
    直接 `len()` 会与括注合计口径不同源）。
    """
    n_res = 0
    bull = bear = neut = 0
    for it in items:
        res = it["resonance"]
        cd = res.get("catalyst_dir") or {}
        bull += int(cd.get("bullish", 0))
        bear += int(cd.get("bearish", 0))
        neut += int(cd.get("neutral", 0))
        n_res += len(res["event"]) + _catalyst_total(cd) + len(res["kol"])
    if not n_res:
        return f"🚨 盘面异动告警：{len(items)} 币高置信信号（纯盘面信号，无共振）"
    dir_txt = ""
    if bull or bear or neut:
        dir_txt = f"，催化剂 {bull}多/{bear}空/{neut}中"
        # 审计 O2：中性条不计入方向。只报「多/空/中」时，扫读者会把「4多/0空/2中」
        # 读成强多；补「净多 = 多−空」，让中性不增强方向 conviction。
        if bull != bear:
            dir_txt += f"，净{'多' if bull > bear else '空'}{abs(bull - bear)}"
    return f"🚨 盘面异动告警：{len(items)} 币高置信信号（含共振 {n_res} 条{dir_txt}）"


# ── 卡片相对强度（仅用于排序与强度条，审计 §三.6） ────────────────
# 原实现按 signal_ts 平铺、同色卡片：量比 8.55x / OI +7.97% 的币与 2.69x / +2.67%
# 的在视觉上完全等价。这里给一个**可解释**的相对量：
#   基量 = 量比 × |OI 增速|
#   共振方向与结论一致 ×1.15，相悖 ×0.75，无方向数据不加不扣
#   CVD 与结论同向 ×1.05
# 只做本封邮件内的相对强弱（绝对阈值无基准；审计 P2-5 亦警示均值会被离群值绑架），
# 故图例明确写「非胜率」，不对外宣称命中率。
STRENGTH_BONUS_ALIGNED = 1.15
STRENGTH_PENALTY_CONFLICT = 0.75
STRENGTH_BONUS_CVD = 1.05
STRENGTH_BAR_CELLS = 5
# BRK 的 oi_chg_pct 为 None（突破判定只用价+量，生产者刻意不落 OI 增速）
# ⇒ 基量 |vol_ratio × 0| = 0 ⇒ 强度条整体不渲染、混排时**永远垫底**，哪怕
# vol_ratio = 4.05x（工单 P2-5，实物复现 id=1159 LAUSDT）。
# 缺 OI 增速时以「等当量」代替单因子，使 BRK 与主池强度可比。
# ⚠️ 3.0 是**临时等当量**（BRK 门槛即 BRK_VOL_RATIO=3.0），待 BRK 样本积累后按
#    实际分布标定；勿据单样本调整。
BRK_STRENGTH_OI_EQUIV = 3.0
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"


def _alert_strength(it: dict) -> float:
    sig, res = it["signal"], it["resonance"]
    oi_chg = sig.get("oi_chg_pct")
    if oi_chg is None and sig.get("scenario") == "BRK":
        oi_chg = BRK_STRENGTH_OI_EQUIV  # 见常量注释（P2-5）
    base = abs(float(sig.get("vol_ratio") or 0) * float(oi_chg or 0))
    up = sig.get("p_dir") == "up"
    cd = res.get("catalyst_dir") or {}
    bull, bear = int(cd.get("bullish", 0)), int(cd.get("bearish", 0))
    if bull != bear:
        aligned = (up and bull > bear) or (not up and bear > bull)
        base *= STRENGTH_BONUS_ALIGNED if aligned else STRENGTH_PENALTY_CONFLICT
    if sig.get("cvd_dir") and sig.get("cvd_dir") == sig.get("p_dir"):
        base *= STRENGTH_BONUS_CVD
    return base


def _strength_bar(score: float, top: float, color: str) -> str:
    """五格强度条（▉ 实 / ░ 虚）+ 原始分数，按本封邮件最高分**对数**归一。

    复验 P2-N5：线性归一时第 2/3 名常被压成同一格（实测 EPIC 7.26 与 APT 8.81
    同为 1 格，相对强弱被抹平）；对数域下二者分列为 2 / 3 格。条后附原始分数，
    保证量化信息零丢失（口径仍是本封邮件内相对值，见图例，非胜率）。
    """
    if top <= 0 or score <= 0:
        return ""
    n = max(1, min(STRENGTH_BAR_CELLS,
                   int(round(math.log1p(score) / math.log1p(top) * STRENGTH_BAR_CELLS))))
    return (f"<span style='color:{color};letter-spacing:1px'>{'▉' * n}</span>"
            f"<span style='color:#d1d5db;letter-spacing:1px'>"
            f"{'░' * (STRENGTH_BAR_CELLS - n)}</span>"
            f"<span style='color:#6b7280;font-size:11px'> {score:.1f}</span>")


def _render_alert_email(items: list[dict]) -> str:
    # 统一标注 UTC（审计 P2-1：容器 TZ=UTC，原实现无时区标注，易被读成本地时间）
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    # 卡片按相对强度降序（审计 §三.6：原按 signal_ts 平铺同色，量比 8.55x 与 2.69x
    # 视觉权重完全相同）。排序在渲染层单点完成，保证标题计数与正文一致。
    items = sorted(items, key=_alert_strength, reverse=True)
    top = _alert_strength(items[0]) if items else 0.0
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
    for idx, it in enumerate(items):
        sig = it["signal"]
        res = it["resonance"]
        pool_label = "蓄势池BRK" if sig["pool"] == "accumulation" else "主池"
        up = sig["p_dir"] == "up"
        arrow_color = "#ef4444" if up else "#22c55e"   # 中文惯例：多头=红 / 空头=绿
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
        # 资金费率原值存的是小数比例（0.00005 = 0.005%），且**不兜底 0**；
        # 补年化（币安 U 本位 8h 结算 ⇒ ×3×365），单看当期费率无可读性（审计 §三.4）
        # 费率：无数据时区分「未覆盖」与「0」（审计 P2-3：实测告警币只有 40/53 在
        # 费率源内，绝大多数 `-` 的含义是「我们没这个数据」而非「费率为 0」）。
        if fund is None:
            fund_str = "n/a（未覆盖）"
        else:
            f_pct = float(fund) * 100
            fund_str = f"{f_pct:+.4f}%（年化 {f_pct * 3 * 365:+.1f}%）"
        # CVD 幅度（审计 P1-3：`cvd_usd` / `cvd_ratio` 已随生产者落库）
        cvd_usd, cvd_ratio = sig.get("cvd_usd"), sig.get("cvd_ratio")
        cvd_amt = ""
        if cvd_usd is not None:
            cvd_amt = f" {_fmt_usd(float(cvd_usd))}"
            if cvd_ratio is not None:
                cvd_amt += f"（占比 {float(cvd_ratio) * 100:+.1f}%）"
        # 共振方向构成 + 与结论相悖警示（审计 P0-2）
        cd = res.get("catalyst_dir") or {}
        bull, bear = int(cd.get("bullish", 0)), int(cd.get("bearish", 0))
        linked = res.get("asset_linked", True)
        # 复验 P1-N2：条数与括注方向合计必须同源 —— 原 N 取明细长度（≤4）、括注取
        # 去重全量（≤20），会渲染出「催化剂 4（9多/1空/9中）」这种自相矛盾的结果。
        cat_n = _catalyst_total(cd)
        neut = int(cd.get("neutral", 0))
        cat_dir_txt = f"{bull}多/{bear}空/{neut}中" if cat_n else ""
        # 审计 O2：中性不计方向 —— 补「净多/净空 = 多−空」，避免「4多/0空/2中」被扫读为强多。
        if cat_n and bull != bear:
            cat_dir_txt += f"，净{'多' if bull > bear else '空'}{abs(bull - bear)}"
        # 审计 N1：披露催化剂新鲜度（7 天窗口会把上周/已过期催化剂也计入共振）。
        if cat_n:
            latest = res.get("catalyst_latest")
            if latest is not None:
                cat_dir_txt += f"，最新 {str(latest)[:10]}"
            stale_n = int(res.get("catalyst_stale") or 0)
            if stale_n:
                cat_dir_txt += f"，含 {stale_n} 条 >{CATALYST_STALE_DAYS} 天"
        res_txt = (f"事件{len(res['event'])} · 催化剂{cat_n if linked else 'n/a'}"
                   + (f"（{cat_dir_txt}）" if cat_dir_txt else "")
                   + f" · KOL {len(res['kol']) if linked else 'n/a'}")
        conflict = ""
        if up and bear > bull:
            conflict = ("<br><span style='color:#dc2626;font-weight:bold'>"
                        "⚠️ 共振方向以利空为主，与做多结论相悖，请复核</span>")
        elif (not up) and bull > bear:
            conflict = ("<br><span style='color:#dc2626;font-weight:bold'>"
                        "⚠️ 共振方向以利多为主，与做空结论相悖，请复核</span>")
        # CVD 机制标签（审计 P1-3 的可做部分：金额列缺失 ⇒ 不做幅度，只做机制判读）
        # 复验 P2-N3：「价涨 + 现货主动卖」有两种**相反**机制，原文案一律断言「OI 增」
        # ⇒ 空头回补（OI 降）场景说反。改为按 oi_dir 分两支，方向未知时不作机制断言。
        cvd = sig.get("cvd_dir")
        cvd_flag = ""
        if cvd and cvd != sig.get("p_dir"):
            if up:
                if sig.get("oi_dir") == "down":
                    cvd_flag = (f"<br><span style='color:#b45309'>⚠️ CVD {cvd} 与做多结论相反 → "
                                "空头回补/多头离场推涨（OI 降），持续性存疑</span>")
                elif sig.get("oi_dir") == "up":
                    cvd_flag = (f"<br><span style='color:#b45309'>⚠️ CVD {cvd} 与做多结论相反 → "
                                "杠杆驱动（OI 增而现货主动卖），无现货承接</span>")
                else:
                    cvd_flag = (f"<br><span style='color:#b45309'>⚠️ CVD {cvd} 与做多结论相反 → "
                                "现货主动卖且无现货承接，但 OI 方向未知，机制待判</span>")
            else:
                cvd_flag = (f"<br><span style='color:#0369a1'>ℹ️ CVD {cvd} 与做空结论相反 → "
                            "跌势中有现货承接，防反抽</span>")
        # 审计 O4：高置信池内质量离散（EPIC 零催化 + 费率未覆盖 + CVD 反向，却与 BTW
        # 同列 HIGH）。对「纯技术面」信号显式淡提示，避免与基本面强的信号视觉等价。
        # 仅当**资产已关联且催化剂确为 0**时判「无催化」——未关联是「无从查询」而非 0。
        tech_note = ""
        if linked and cat_n == 0 and fund is None:
            tech_note = ("<br><span style='color:#6b7280'>ℹ️ 纯技术面信号"
                         "（无催化剂、费率未覆盖），缺基本面确认</span>")
        badge = (f"<span style='background:{'#fee2e2' if up else '#dcfce7'};"
                 f"color:{'#b91c1c' if up else '#15803d'};padding:1px 5px;"
                 f"border-radius:3px;font-size:11px'>"
                 f"{str(sig.get('confidence') or '').upper()}</span>")
        # 延续确认徽章（fix_061）：主池信号在 signal_ts 后 6h 内越过 breakout_px
        # ⇒ status='confirmed'。它表示「延续已被市场跟随」，是**质量升格**而非
        # 入场门槛（回放：等确认再入场会把期望做低），故只作信息展示。
        # ⚠️ 只对主池渲染 —— 轧空池的 confirmed 是「已判定事件」，同值不同源。
        if sig.get("status") == "confirmed" and sig.get("pool") == "main":
            badge += (f" <span style='background:#dbeafe;color:#1d4ed8;padding:1px 5px;"
                      f"border-radius:3px;font-size:11px'>已确认</span>")
        bar = _strength_bar(_alert_strength(it), top, arrow_color) if top > 0 else ""
        rank = _CIRCLED[idx] if idx < len(_CIRCLED) else f"{idx + 1}."
        # 失效位（审计 P2-4）：生产者已落 trigger_price / stop_loss_pct ⇒ 渲染价格与幅度
        trig_px, stop_pct = sig.get("trigger_price"), sig.get("stop_loss_pct")
        invalid_txt = ""
        if trig_px is not None and stop_pct is not None:
            sp = float(stop_pct)
            barrier = (float(trig_px) * (1 - sp / 100) if up
                       else float(trig_px) * (1 + sp / 100))
            # 审计 O3：stop 触夹带上下限时（实测 57% 的 S1 信号落 8% 下限），前缀仍写
            # 「2×ATR(14)」会让风控读者误判波动幅度 —— 触限时显式标注真实 2×ATR 的方向。
            if sp <= STOP_PCT_MIN + 1e-9:
                atr_note = f"已触下限 {STOP_PCT_MIN:.0f}%（真实 2×ATR 更窄）"
            elif sp >= STOP_PCT_MAX - 1e-9:
                atr_note = f"已触上限 {STOP_PCT_MAX:.0f}%（真实 2×ATR 更宽）"
            else:
                atr_note = f"2×ATR({STOP_ATR_PERIOD}) 夹 [{STOP_BAND_TXT}]"
            invalid_txt = (f"<br><small style='color:#6b7280'>失效位 "
                           f"{'跌破' if up else '升破'} {_fmt_num(barrier, 6)}"
                           f"（-{sp:.2f}%，{atr_note}，入场 "
                           f"{_fmt_num(trig_px, 6)}）</small>")
        # 历史先验（审计 P2-5）：中位/胜率/样本量，**不用均值**（会被离群值绑架）
        # 工单 P2-6：样本限定 `alerted_at IS NOT NULL`（近 30 天、且已到期）⇒ 只覆盖
        # 「告警期」，而告警本身依赖 regime 顺风（S1 = 价↑+OI↑ 正是顺风场景）⇒
        # 正期望是该口径**必然**结果，不是信号质量证据。此处显式披露偏置来源，
        # 不把它包装成无偏基准（分层基准需更长样本，见 AGENTS.md 待办）。
        prior = it.get("prior")
        prior_txt = ""
        if prior:
            prior_txt = (f"<br><small style='color:#6b7280'>历史同场景 {prior['horizon']}h "
                         f"方向对齐 中位 {prior['median']:+.2f}% / 胜率 {prior['win']:.0f}%"
                         f"（n={prior['n']}，仅含已告警样本，非无偏基准）</small>")
        body_parts.append(
            f"<div style='margin:8px 0;padding:10px 12px;border-left:4px solid "
            f"{arrow_color};background:#f9fafb;color:#111'>"
            f"<div style='font-size:15px'>{rank} <b>{sig['symbol']}</b> {badge} {bar} "
            f"<span style='color:#6b7280;font-size:12px'>{pool_label} · "
            f"{lv_txt or (sig.get('timeframe') or '-')}</span></div>"
            f"<div style='margin:2px 0 4px'><b>{sc}</b> {desc} "
            f"<b style='color:{arrow_color}'>{dir_label}</b> "
            f"<span style='color:#374151'>{_fmt_num(sig.get('price_chg_pct'), 2, '%', signed=True)}</span>"
            f"</div>"
            f"<small style='color:#111'>量比 {_fmt_num(sig.get('vol_ratio'), 2, 'x')} | "
            f"OI {sig.get('oi_dir') or '-'} {_fmt_num(sig.get('oi_chg_pct'), 1, '%', signed=True)} | "
            f"CVD {cvd or '未知'}{cvd_amt} | 费率 {fund_str}</small>"
            f"{cvd_flag}{tech_note}"
            f"<br><small style='color:#111'>共振：{res_txt}{conflict}</small>"
            f"{invalid_txt}{prior_txt}"
            f"</div>"
        )
    body = "".join(body_parts)
    legend = ("<p style='color:#6b7280;font-size:12px'>图例：S1 多头进攻 / S2 诱多 / "
              "S3 空头扎实 / S4 诱空 / S5-8 兑现与反转；「N 级异动」= 触发周期；"
              "CVD up/down = 主动买/卖占比方向，其后为净额与占同窗口成交额的比；"
              f"费率年化 = 当期 ×3×365（8h 结算）；「失效位」= 2×ATR({STOP_ATR_PERIOD}) "
              f"幅度夹在 [{STOP_BAND_TXT}] "
              "带内（工单 P2-4：原 21 根反向极值实测幅度不可用，-11.97% 配 +4.69% 涨幅 "
              "⇒ 风险回报倒挂）；"
              f"「已触下限/上限」= 失效位被夹到 [{STOP_BAND_TXT}] 边界，真实 2×ATR 在"
              "该边界之外（更窄/更宽），非「2×ATR 恰等于该值」；"
              "「催化剂」括注的「净多/净空」= 利多−利空条数（中性不计方向），"
              "「最新/含 N 条 >X 天」= 催化剂新鲜度（7 天窗口含陈旧条目）；"
              "「共振」= 事件预置 + 催化剂 + KOL 三段聚合（渲染时实时查询），"
              "非 biz.catalyst_resonance 表的超额收益方向匹配评分；"
              "「历史同场景」= 同场景已告警信号的方向对齐后验（中位/胜率/样本量；"
              "样本仅覆盖告警期、含顺风期选择偏置，非无偏基准）；"
              "强度条 = 本封邮件内「相对」强弱（量比 × OI 增速，BRK 无 OI 增速时取 "
              "3.0 等当量；共振/CVD 与结论"
              "相悖则扣系数），按最高分对数归一，条后数字为原始分数，非胜率；"
              "「已确认」徽章 = 信号发出后 6 小时内出现一根已收盘 1h K 线的收盘价越过"
              "「触发根极值」⇒ 延续已被市场跟随（质量升格，非入场门槛：实测等确认再"
              "入场会把入场价抬高，故不改变执行口径）。</p>")
    footnote = ("<p style='color:#999;font-size:12px'>"
                "n/a = 该维度无从查询（资产未关联 / 不在数据源内），≠ 数值为 0；"
                "共振各段 n/a = 本库未关联该资产；催化剂 N 与括注方向合计同源"
                "（=「归一标题去重后」的全量条数，已合并多源转载）。<br>"
                "同一币在 60 分钟内若已在另一通道（轧空/主池）告警过，本通道只留痕不发信。<br>"
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
            # 本实例尚未跑完首轮（无心跳，或心跳来自上一次进程）→ 给一段宽限，
            # 避免每次部署后立刻误报「线程从未启动」（宽限上限见下）。
            if row is None or (daemon_start is not None and last_run < daemon_start):
                if daemon_start is None:
                    parts.append(f"任务 {name} 无心跳记录（该线程可能从未启动）")
                else:
                    gap_min = (now - daemon_start).total_seconds() / 60
                    # 首轮宽限取 min(3×周期, FIRST_ROUND_GRACE_MAX_MIN)：3×周期对
                    # 长周期任务（1800s/86400s）会得到 90/4320 分钟，而容器重启周期
                    # 约 15 分钟 ⇒ 每次重启都把宽限重置，「线程从未启动」永远判不出来
                    # （工单 P0-1 根因②，与 check_scan_freshness 同口径）。
                    first_limit_min = min(limit_min, FIRST_ROUND_GRACE_MAX_MIN)
                    if gap_min > first_limit_min:
                        parts.append(
                            f"任务 {name} 本实例已启动 {gap_min:.0f} 分钟仍无首轮心跳"
                            f"（阈值 {first_limit_min:.0f} 分钟）")
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
    #
    # 三个边界（否则本检测自身就会变成永久误报源）：
    #   ① 回溯**有界**（LOST_SIGNAL_LOOKBACK_H）—— 否则 09-16 那批陈年行会让告警
    #      每 6h 去重期一过就重发一次；
    #   ② 排除**冷却跳过**的行 —— 同币 12h 内已告警时 `task_scan_alert` 是**刻意**
    #      跳过且不写 `alerted_at` 的，那属于设计行为，不是丢失；
    #   ③ 排除**跨池互斥留痕**的行（`alert_suppressed_at`）—— 同币另一池已在窗口内
    #      告警过，属 P1-5 的刻意抑制。
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM biz.scan_signal s "
                "WHERE s.confidence = 'high' AND s.alerted_at IS NULL "
                "AND s.alert_suppressed_at IS NULL "
                "AND (s.pool = 'main' OR (s.pool = 'accumulation' AND s.scenario = 'BRK')) "
                "AND s.signal_ts < NOW() - INTERVAL '30 minutes' "
                "AND s.signal_ts > NOW() - make_interval(hours => %s) "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM biz.scan_signal a "
                "   WHERE a.symbol = s.symbol AND a.alerted_at IS NOT NULL "
                "     AND a.alerted_at > s.signal_ts - make_interval(hours => %s) "
                "     AND a.alerted_at < s.signal_ts + INTERVAL '10 minutes')",
                (LOST_SIGNAL_LOOKBACK_H, COOLDOWN_H))
            lost = int(cur.fetchone()[0] or 0)
        if lost:
            parts.append(
                f"近 {LOST_SIGNAL_LOOKBACK_H}h 内未告警即超窗的 high 信号 {lost} 条"
                f"（alert 任务疑似停摆 >30 分钟，这批信号已永久作废、不会补发）")
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
            "⚠️ 盘面扫描停摆告警", body, from_name="盘面信号扫描",
            # 只发系统管理员（ADMIN_EMAIL），未配置时回退 SMTP_TO：运维告警不该
            # 推给全部订阅者（与外部看门狗 check_scan_freshness._send_mail 同口径）。
            to=settings.admin_email or settings.smtp_to)
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
        # 跨池互斥（审计 P1-5）：先取 squeeze 池在互斥窗口内已告警的符号（一次查询），
        # 命中的主池候选只留痕、不进本封邮件。
        muted = _cross_pool_recent(conn, sorted({c["symbol"] for c in candidates}),
                                   ("squeeze",))
        seen: set[str] = set()
        to_alert: list[dict] = []
        suppressed = 0
        for c in candidates:
            if c["symbol"] in seen or _in_cooldown_alert(conn, c["symbol"]):
                continue
            seen.add(c["symbol"])
            if c["symbol"] in muted:
                _mark_alert_suppressed(
                    conn, [c["id"]],
                    f"跨池互斥：squeeze 池已在 {CROSS_POOL_MUTE_MIN} 分钟内告警")
                suppressed += 1
                continue
            to_alert.append({
                "signal": c,
                "resonance": _get_resonance(conn, c["symbol"], _get_asset_id(conn, c["symbol"])),
            })

        if not to_alert:
            return {"candidates": len(candidates), "alerts": 0,
                    "suppressed_cross_pool": suppressed}

        # 历史先验（审计 P2-5）：只对本封出现的场景取一次
        priors = _scenario_priors(conn, sorted({it["signal"]["scenario"] for it in to_alert}))
        for it in to_alert:
            it["prior"] = priors.get(it["signal"]["scenario"])

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
            # 审计 O6（沿用 PROC-1 精神）：共振是渲染时实时查三张动态表算的，发信后
            # 因催化剂 7 天窗口漂移而**不可字节级回放**。把三段明细连同方向构成落
            # `detail`（jsonb，主池此前恒 NULL）作为快照，使历史邮件可独立复核。
            captured = datetime.now(timezone.utc).isoformat()
            snapshots = [
                (json.dumps({"resonance_snapshot": it["resonance"],
                             "captured_at": captured},
                            ensure_ascii=False, default=str),
                 it["signal"]["id"])
                for it in to_alert
            ]
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE biz.scan_signal SET alerted_at = NOW() WHERE id = ANY(%s)",
                    (ids,),
                )
                cur.executemany(
                    "UPDATE biz.scan_signal SET detail = COALESCE(detail, '{}'::jsonb) "
                    "|| %s::jsonb WHERE id = %s",
                    snapshots,
                )
            conn.commit()
            return {"candidates": len(candidates), "alerts": len(ids),
                    "suppressed_cross_pool": suppressed}
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
            # 中段/左端连续性（复验 P2-d / P3）：覆盖率与 tail_gap 都不拦「前段齐、
            # 中段缺桶、尾部齐」，而重启丢的恰是中间桶（快照不可回补）⇒ 窗口指标被
            # 悄悄污染。判定区间 `[first_bucket, last_bucket)`（右端正在采集的桶归
            # tail_gap 管）；左端缺桶单列（head_gap）——`base_oi` 会落到窗口之外。
            present = {int(r["ts"].timestamp()) // BUCKET_SECONDS for r in win_oi}
            gap_ok, head_gap, mid_gap = sqz.window_gate(
                present, first_bucket, last_bucket)
            if coverage < sqz.MIN_WINDOW_COVERAGE or tail_gap or not gap_ok:
                stats["insufficient_coverage"] += 1
                if coverage < sqz.MIN_WINDOW_COVERAGE:
                    reason = (f"判定窗口数据覆盖不足 {len(win_oi)}/{expect_buckets} 桶，暂不判定")
                elif tail_gap:
                    reason = "判定窗口尾部 OI 桶缺失，暂不判定"
                else:
                    # 文案由 squeeze.gap_reason() 统一产出（复验 D6）——前缀「判定窗口」是
                    # check_scan_freshness `reason LIKE '判定窗口%'` 的耦合点，勿就地拼接。
                    reason = sqz.gap_reason(head_gap, mid_gap, len(win_oi), expect_buckets)
                if oi_lag_sec is not None:
                    reason += f"（OI 最新桶滞后 {oi_lag_sec:.0f}s）"
                print(f"[scan_daemon][squeeze] {sym} {reason}", file=sys.stderr)
                # 复验 E4：拒判路径**同样要写 metrics**。旧码此处传 None ⇒ SQL 的
                # `COALESCE(%s::jsonb, metrics)` 保留旧值，`head_gap_buckets`/
                # `mid_gap_buckets`/`gap_metric_ver` 只在 judged 路径写；而 judged 必经
                # 闸门 ⇒ 落库值恒 0/1，**真正要观测的病例（拒判）反而落不了库**。
                # `judged_at` 仍保持 None（本条未判定）。
                track_updates.append((
                    "tracking", peak_px, peak_ts, px, now, round(retrace, 2), None,
                    reason, json.dumps({
                        "gap_metric_ver": sqz.GAP_METRIC_VER,
                        "head_gap_buckets": head_gap,
                        "mid_gap_buckets": mid_gap,
                        "oi_cover": {"have": len(win_oi), "expect": expect_buckets},
                        "oi_lag_sec": None if oi_lag_sec is None else round(oi_lag_sec),
                    }, ensure_ascii=False), None, t["id"]))
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
                # 窗口内连续缺桶（复验 P2-d / P3）：仅作观测，不参与判定（判定已在闸门处完成）；
                # `head_gap_buckets` 单列——左端缺桶会让 base_oi 落到窗口之外，语义更重。
                # `gap_metric_ver`（复验 D5）：v1 的 `mid_gap_buckets` 含左端起，v2 不含 ⇒
                # 跨版本回看该字段必须先看版本位，否则误读历史行。
                "gap_metric_ver": sqz.GAP_METRIC_VER,
                "head_gap_buckets": head_gap,
                "mid_gap_buckets": mid_gap,
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
        id_by_symbol: dict[str, int] = {}
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
                sid = cur.fetchone()[0]
                id_by_symbol[t["symbol"]] = sid
            conn.commit()

    # ── 跨池互斥（审计 P1-5）────────────────────────────────────
    # 同币若在互斥窗口内已被主池告警过（实测 XMR 39min / 龙虾 41min），本封只留痕不发信：
    # 两侧口径相反的告警（主池「多头进攻做多」vs 轧空「多空平局 churn」）会直接互相打架。
    muted: set[str] = set()
    if judged_items:
        with _db() as conn:
            muted = _cross_pool_recent(
                conn, sorted({it["track"]["symbol"] for it in judged_items}), ("main",))
            _mark_alert_suppressed(
                conn, [id_by_symbol[s] for s in muted if s in id_by_symbol],
                f"跨池互斥：主池已在 {CROSS_POOL_MUTE_MIN} 分钟内告警")
    send_items = [it for it in judged_items if it["track"]["symbol"] not in muted]
    stats["suppressed_cross_pool"] = len(judged_items) - len(send_items)

    # ── 判定成功 → 发告警（只发一次，不做 12h 冷却）──────────────
    if send_items:
        settings = _SETTINGS or get_settings(require_database=True)
        from crypto_research.clients.notifier import EmailNotifier
        notifier = EmailNotifier(settings)
        if not notifier.configured:
            print("[WARN] SMTP 未配置，跳过轧空判定告警")
        else:
            ok, msg = notifier.send(
                f"🎯 轧空胜负判定：{len(send_items)} 币",
                _render_squeeze_alert(send_items), from_name="轧空扫描")
            if ok:
                send_ids = [id_by_symbol[it["track"]["symbol"]] for it in send_items]
                with _db() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE biz.scan_signal SET alerted_at=NOW() WHERE id = ANY(%s)",
                            (send_ids,))
                    conn.commit()
                stats["alerts"] = len(send_ids)
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
    """信号生命周期巡检：把超期的 active/confirmed 信号置 expired 并写 expired_at（每 30 分钟）。

    实现设计文档 §6.3 的「超时退出」一段（§12.1-4 缺口）。原状：`biz.scan_signal`
    只有写入没有退出，`status` 恒为 active、`expired_at` 无人写 —— 观察名单没有
    退出机制，告警冷却、执行层冷却（查 created_at 窗口）与后续统计都会越来越脏。

    有效期语义见 SIGNAL_TTL_* 常量（主池/BRK 24h、ACC 7 天）。

    两个实现选择：
      - `expired_at` 写**确定性截止时刻**（`signal_ts + TTL`）而非 `NOW()`：本任务
        每 30 分钟才跑一轮，写 NOW() 会让实际有效期随巡检相位漂移最多 30 分钟。
        这一列同时被 `phase_execute_scan_signal.load_candidates` 用作在窗前筛。
      - 只更新 `status IN ('active','confirmed')` 的行 ⇒ 幂等，可重复执行。
        **confirmed 必须覆盖**（fix_061）：主池的 confirmed 是「延续已被市场跟随」
        的**升格**（仍是观察名单成员），若只收 active，一旦升格就永不失效。
        轧空池写入的 confirmed **同值不同源**（那里是「已判定事件」，有自己的
        `biz.squeeze_track` 状态机）⇒ 靠 `pool` 条件天然排除，不受本处影响。
    """
    with _db() as conn:
        stats: dict[str, int] = {}
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE biz.scan_signal
                   SET status = 'expired',
                       expired_at = signal_ts + make_interval(hours => (%s)::int)
                 WHERE status IN ('active', 'confirmed')
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
                 WHERE status IN ('active', 'confirmed')
                   AND pool = 'accumulation' AND scenario = 'ACC'
                   AND signal_ts < NOW() - make_interval(days => (%s)::int)
                """,
                (SIGNAL_TTL_ACC_DAYS, SIGNAL_TTL_ACC_DAYS))
            stats["acc"] = cur.rowcount
        conn.commit()
    return {"signal_ttl_main_h": SIGNAL_TTL_MAIN_H,
            "signal_ttl_acc_days": SIGNAL_TTL_ACC_DAYS, **stats}


# ═══════════════════════════════════════════════════════════════
#  任务 11：延续确认（30 分钟）
# ═══════════════════════════════════════════════════════════════
# 确认窗口（小时）：signal_ts 后多久之内出现「越位」才算延续已被跟随。
#
# 标定依据（2026-09-22 离线回放：90 条前向可结算多头主池信号、1h K 线；
# 入场价固定为 signal_ts 收盘价、离场固定 signal_ts+24h、方向对齐）：
#   W=2h → 越位 n=24 均 +10.95% 胜 83.3%  | 未越位 n=66 均 +2.72% 胜 63.6%
#   W=4h → 越位 n=30 均 +10.00% 胜 83.3%  | 未越位 n=60 均 +2.38% 胜 61.7%
#   W=6h → 越位 n=40 均  +9.04% 胜 85.0%  | 未越位 n=50 均 +1.62% 胜 56.0%
#   W=8h → 越位 n=49 均  +8.56% 胜 81.6%  | 未越位 n=41 均 +0.57% 胜 53.7%
# 取 6h：区分度与 4h 相当而保留率高近一倍（44% vs 33%），保住样本量。
#
# ⚠️ 区分器 ≠ 门槛 —— 实测「等越位再入场」会把期望**做低**：越位位离入场价
# 中位 2.31%、p75 4.01%，以越位根收盘价入场时 24h 期望 +1.36% < 基线 +1.56%
# （这与催化层 d3 的教训同源：等价格确认 = 追高）。故本任务只**升格状态**，
# 绝不改入场时点 —— 消费侧仍按 signal_ts 价格口径执行。
# ⚠️ 未越位**不提前作废**：W=6h 未越位组 24h 仍 +1.62% 正期望（只有「24h 全程
# 未越位」的 21 条才是 -4.41%），提前 expired 会主动丢掉一半正期望样本。
# ⚠️ 样本仅 90 条 / 3 个交易日 / 单边上涨 regime ⇒ 统计力有限，待积累复校。
BREAKOUT_WINDOW_H = 6
# 追补余量（小时）：巡检 30min 一轮，而容器重启周期实测约 15min ⇒ 确认窗口
# 边缘错过一次就永久丢失（K 线已落库、可追补）。故扫描回溯放宽到
# BREAKOUT_WINDOW_H + BREAKOUT_CATCHUP_H；**越位判据仍严格限定 6h 窗口内**的
# K 线（见 SQL 的 k.open_time <= signal_ts + 6h）。
BREAKOUT_CATCHUP_H = 2


def task_confirm_signals() -> dict:
    """延续确认巡检：把「突破位已被越过」的 active 主池信号升格为 confirmed（每 30 分钟）。

    落地设计文档 §6.3 的 `active → confirmed（突破触发价）` 一段（§12.1-4 原先
    记的是「卡在语义分歧」）。**语义澄清（v0.6）**：
      - `trigger_price` 保持「入场位 = 触发根收盘价」不变（执行层与渲染层都在消费）；
      - 「待突破价位」**独立成列** `breakout_px`（迁移 fix_061）= 触发根方向侧极值；
      - `confirmed` = 该位已被市场跟随，属**质量升格**而非入场门槛（依据见上方常量注释）。

    判据：`signal_ts` 后 `BREAKOUT_WINDOW_H` 小时内，存在一根**已收盘**的 1h K 线，
    其收盘价越过 `breakout_px`（up→高于 / down→低于）。

    三个实现选择：
      - **只用已收盘条**（`open_time + 1h <= NOW()`）：未收盘条的 close 是滚动现价，
        同一根在不同时刻判定结果不同、不可复现（工单 P1-4 的教训）。
      - 只处理 `status='active'` ⇒ 幂等可重复执行；`expired` 不被复活。
      - `breakout_px IS NULL`（存量行 / 生产者无 K 线）跳过 —— 不猜、不兜底。

    注：轧空池的 `confirmed` 与本列同值但**不同源**（它记「已判定事件」，有独立的
    `biz.squeeze_track` 状态机），本任务按 `pool='main'` 严格限定，不会互相污染。
    """
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE biz.scan_signal s
                   SET status = 'confirmed'
                 WHERE s.pool = 'main'
                   AND s.status = 'active'
                   AND s.breakout_px IS NOT NULL
                   AND s.signal_ts > NOW() - make_interval(hours => (%s)::int)
                   AND EXISTS (
                       SELECT 1 FROM biz.asset_klines k
                        WHERE k.symbol = s.symbol
                          AND k.interval = '1h'
                          AND k.open_time > s.signal_ts
                          AND k.open_time <= s.signal_ts
                              + make_interval(hours => (%s)::int)
                          AND k.open_time + INTERVAL '1 hour' <= NOW()
                          AND ((s.p_dir = 'up'   AND k.close_px > s.breakout_px)
                            OR (s.p_dir = 'down' AND k.close_px < s.breakout_px))
                   )
                """,
                (BREAKOUT_WINDOW_H + BREAKOUT_CATCHUP_H, BREAKOUT_WINDOW_H))
            confirmed = cur.rowcount
        conn.commit()
    return {"breakout_window_h": BREAKOUT_WINDOW_H, "confirmed": confirmed}


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
    global _LAST_ANY_ROUND_TS
    time.sleep(offset_sec)  # 初始错峰

    running = False
    round_count = 0
    fail_streak = 0
    while True:
        round_count += 1
        start_ts = time.time()

        # 每轮重新加固日志流（幂等、零成本）：被本进程 exec/import 的第三方模块
        # 可能把 `sys.stdout` 换成**非代理**对象（如 `phase_watchlist_monitor.py`
        # 原来的 `sys.stdout = io.TextIOWrapper(sys.stdout.buffer, ...)`），一旦换掉，
        # `_harden_streams` 的保护就被静默摘除，此后底层流一断，下面这条 print 抛
        # `ValueError` 而它在 try 之外 ⇒ **整个任务线程静默死亡、心跳不再更新**，
        # 而主线程仍在 sleep ⇒ 进程活着持锁、supervisord 永不重启（2026-09-21 停摆 14h+）。
        _harden_streams()

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
            # 供 main() 的「无产出看护」判断进程是否还有产出（成败都算产出：
            # 失败会写 last_error 并触发数据新鲜度告警，不会静默）。
            _LAST_ANY_ROUND_TS = time.monotonic()

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
    # expire_signals 的 offset 由 900 改 90（工单 P0-1）：offset 是 `_run_task_loop`
    # 启动时的初始错峰 sleep，而容器重启周期实测约 15 分钟（493/593/719/911 秒），
    # offset=900s 与进程存活时长**同量级** ⇒ 常驻模式下首轮几乎永不触发，信号生命
    # 周期「超时退出」实际停摆（`status='active'` 一度积到 845 行），库里 132 行
    # `expired` 全部来自手工 `--run-once`。90s 保证每次重启后都能跑完首轮。
    ("expire_signals",    1800,   90, task_expire_signals,    {}),
    # 延续确认巡检（fix_061）：offset 480 与 expire_signals(90) 拉开，避免两者同轮
    # 抢同一批行（确认写 confirmed、失效写 expired，同一行不该在同一时刻被两处写）。
    # 按 signal_ts 降序语义上「先确认后失效」更自然：确认窗口 6h ≪ TTL 24h，故
    # offset 大小不影响正确性，仅取 480s 做错峰。
    ("confirm_signals",   1800,  480, task_confirm_signals,   {}),
    ("prune_scan_data",   86400, 600, task_prune_scan_data, {}),
]
# 耦合校验（工单 P0-1）：首轮宽限上限必须大于最大 offset + 首轮余量，否则「本实例
# 尚未跑完首轮」的宽限会把正常的慢启动误报成「线程从未启动」。改 TASK_DEFS 的
# offset 时必须同步复核 FIRST_ROUND_GRACE_MAX_MIN。
assert FIRST_ROUND_GRACE_MAX_MIN > max(t[2] for t in TASK_DEFS) / 60.0 + 5, \
    "FIRST_ROUND_GRACE_MAX_MIN 必须 > 最大 offset + 5 分钟"


def _watchdog_reason(threads: list) -> str | None:
    """主线程「无产出看护」的判据：返回退出原因，正常则返回 None。

    两个判据（对应 2026-09-21 停摆 14h+ 的两种成因）：

    1. **任务线程已退出**：线程是 daemon 线程，异常逃出 `_run_task_loop` 后它
       就永久消失，心跳不再更新，而进程仍在持锁 ⇒ supervisord 不重启。
       本进程里真实发生过：`phase_watchlist_monitor.py` 每轮换掉 `sys.stdout`
       关掉底层流后，各线程在「每轮首行 print」（在 try 之外）抛 ValueError
       逐个死亡。线程名（`scan_<task>`）直接带出来便于定位。
    2. **全进程无产出**：线程都活着但都没跑完一轮（卡在外部 API 的无界等待、
       或卡在锁/DB 上）⇒ 数据零写入、心跳不推进，同样是静默停摆。

    ⚠️ 判据必须只看「有没有产出」，不能看「有没有失败」：失败会写 last_error
    并触发数据新鲜度告警，属可见状态，重启只会放大故障（见 MAX_CONSEC_FAILURES
    与 binance_http.BAN_WAIT_MAX_S 的注释）。
    """
    dead = [t.name for t in threads if not t.is_alive()]
    if dead:
        return (f"任务线程已退出: {', '.join(dead)}"
                f"（心跳不再更新、进程仍在持单实例锁）")
    if _LAST_ANY_ROUND_TS is None:
        # main() 启动时即置位，理论上不会走到；兜底为「不判卡死」。
        return None
    idle_min = (time.monotonic() - _LAST_ANY_ROUND_TS) / 60.0
    if idle_min >= HANG_EXIT_MIN:
        return (f"全进程已 {idle_min:.0f} 分钟无任何任务跑完一轮"
                f"（≥{HANG_EXIT_MIN:.0f} 分钟阈值，最频繁任务周期仅 "
                f"{min(t[1] for t in TASK_DEFS)}s），判定进程无产出")
    return None


def main() -> int:
    # 立即加固 stdout/stderr：容器日志设施断开后任何 print 都会抛
    # ValueError 并（在旧实现里）被吞成「单轮失败」，导致业务函数永不执行
    # （审计 P0-A）。此后所有日志（含 binance_http 的内部日志）写失败即丢弃。
    _harden_streams()

    parser = argparse.ArgumentParser(description="盘面异动扫描守护进程（多任务单进程）")
    parser.add_argument("--run-once", metavar="TASK",
                        help="只跑一次指定任务（调试；需常驻实例已停，同样取单实例锁）")
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

    # 单跑模式（复验 P2-N4）。原实现把它放在**取锁之前**并直接 `func()`，两个后果：
    #   ① 绕锁 —— 手工 `--run-once` 可与常驻实例并行跑同一任务，对 scan_alert
    #      （发信 + 回写 alerted_at）会造成**重复发信**；
    #   ② 不留痕 —— 不走 `_run_task_loop` 故不写心跳，「部署/采集是否跑过」与
    #      `biz.scan_heartbeat` 脱节（复验即因此拿到过「未部署」的假信号）。
    # 现移到取锁之后，并在成功后补写一条任务心跳。
    if args.run_once:
        for name, _iv, _off, func, kwargs in TASK_DEFS:
            if name == args.run_once:
                start = time.time()
                result = func(**kwargs)
                _write_heartbeat(name, True)
                print(f"[scan_daemon][{name}] 单次运行完成，耗时 {time.time()-start:.1f}s，结果: {result}")
                return 0
        print(f"未知任务: {args.run_once}（可用: {', '.join(t[0] for t in TASK_DEFS)}）")
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

    # 主线程：等待 + 进程级「无产出」看护（HANG_EXIT_MIN 常量处有完整背景）。
    # 原实现只 sleep，故「线程全死 / 全卡住而进程活着」这一类故障无人收口：
    # supervisord 只在进程**退出**时重启，进程活着 ⇒ 永不重启 ⇒ 无限期停摆。
    global _LAST_ANY_ROUND_TS
    _LAST_ANY_ROUND_TS = time.monotonic()
    try:
        while True:
            time.sleep(WATCHDOG_TICK_SEC)
            reason = _watchdog_reason(threads)
            if reason:
                print(f"[scan_daemon] ⚠️ {reason}，主动退出交 supervisord 重启",
                      file=sys.stderr)
                os._exit(1)
    except KeyboardInterrupt:
        print("\n[scan_daemon] 收到退出信号，正在停止...")
        return 0


if __name__ == "__main__":
    sys.exit(main())
