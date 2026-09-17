#!/usr/bin/env python3
"""Binance FAPI REST 公共请求层：全局限频 + 429/418 指数退避 + 全局封禁闸门。

背景（盘面异动扫描鲁棒性改进）：
  scan_oi_cvd / scan_klines 在云端偶发被看护进程判为 stuck 杀掉，根因多为
  IP 权重 418 封禁后，8 个并发线程各自独立退避重试 → 在封禁窗口内反复撞墙、
  互相叠加 → 整轮任务长时间无进展。本模块解决两类问题：

  1. 全局封禁闸门（核心）：任一线程触发 418/429 后设置全局 ban_until，
     其余线程在发请求前先检查并协同等待 → 封禁期间全体静默，解封后统一重试，
     不再并发放大。
  2. 指数退避：418 按连续触发次数指数增长（最长 600s）；429 指数增长（最长 60s）；
     并读取 x-mbx-used-weight-1m 权重头，接近上限时主动加长请求间隔（软限频）。

用法：
    from crypto_research.clients.binance_http import fapi_get
    data = fapi_get("https://fapi.binance.com/fapi/v1/exchangeInfo", {})
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

import requests

TIMEOUT = 20
MAX_RETRIES = 3                # 网络抖动重试（不计 429/418 退避）
MIN_REQUEST_GAP = 0.3          # 全局请求最小间隔（秒），≈3.3 req/s 保守防限频
BAN_418_BASE_S = 60            # 418 首次退避（秒），随后按连续次数 ×2 增长
BAN_418_MAX_S = 600            # 418 退避上限（10 分钟，仍远小于 90min 看护阈值）
RATE_LIMIT_429_MAX_S = 60      # 429 退避上限
WEIGHT_WARN_THR = 1800         # x-mbx-used-weight-1m 软限频阈值（fapi 限额 2400/分钟）

_SESSION = requests.Session()
_LOCK = threading.Lock()
_LAST_REQUEST_TS = 0.0
_CUR_MIN_GAP = MIN_REQUEST_GAP

# 全局封禁闸门（跨线程共享）
_ban_until = 0.0
_consecutive_ban = 0           # 连续封禁计数（用于指数退避）
_ban_lock = threading.Lock()

_log: Callable[[str], None] = print


def configure_logger(logger: Callable[[str], None]) -> None:
    """注入日志回调（默认 print）。"""
    global _log
    _log = logger


def set_min_request_gap(seconds: float) -> None:
    """设置全局请求最小间隔（0=不限速）。默认 0.3s，scan_daemon 等可覆盖。"""
    global _CUR_MIN_GAP
    _CUR_MIN_GAP = max(0.0, float(seconds))


def _now() -> float:
    return time.time()


def _set_ban(duration: float) -> float:
    """设置全局封禁截止时间，返回本次退避时长。"""
    global _ban_until, _consecutive_ban
    with _ban_lock:
        _consecutive_ban += 1
        _ban_until = _now() + duration
        return duration


def _ban_backoff(status: int, attempt: int) -> float:
    """按状态计算退避时长：418 指数、429 指数（带全局连续计数加成）。"""
    with _ban_lock:
        n = _consecutive_ban
    if status == 418:
        return min(BAN_418_BASE_S * (2 ** n), BAN_418_MAX_S)
    return min(5 * (2 ** attempt), RATE_LIMIT_429_MAX_S)


def _wait_until_ban_expires() -> None:
    """若在全局封禁窗口内，等待解封（供请求线程发请求前调用）。"""
    while True:
        with _ban_lock:
            remaining = _ban_until - _now()
        if remaining <= 0:
            return
        _log(f"[binance] 全局封禁中，协同等待 {remaining:.0f}s 后重试")
        time.sleep(min(remaining, 30))


def _reset_ban_if_expired() -> None:
    """封禁结束后清零连续计数，避免下次从错误基数开始。"""
    global _consecutive_ban
    with _ban_lock:
        if _ban_until <= _now() and _consecutive_ban > 0:
            _consecutive_ban = 0


def fapi_get(url: str, params: dict | None = None,
             *, timeout: int = TIMEOUT) -> Any:
    """带全局限频 + 429/418 指数退避 + 全局封禁闸门的 GET。返回 r.json()。"""
    global _LAST_REQUEST_TS
    last_err: Exception | None = None
    params = params or {}

    for attempt in range(MAX_RETRIES + 2):
        _reset_ban_if_expired()
        _wait_until_ban_expires()   # 封禁期间全体静默

        try:
            with _LOCK:
                gap = _CUR_MIN_GAP - (_now() - _LAST_REQUEST_TS)
                if gap > 0:
                    time.sleep(gap)
                r = _SESSION.get(url, params=params, timeout=timeout)
                _LAST_REQUEST_TS = _now()

            # 软限频：权重头接近上限时主动加长后续请求间隔
            w = r.headers.get("x-mbx-used-weight-1m")
            if w and w.isdigit() and int(w) > WEIGHT_WARN_THR:
                extra = min(30, (int(w) - WEIGHT_WARN_THR) / 100.0)
                _log(f"[binance] 权重 {w}/min 偏高，加长间隔 {extra:.1f}s")
                time.sleep(extra)

            if r.status_code == 418:
                wait = _ban_backoff(418, attempt)
                _set_ban(wait)
                _log(f"[binance] 418 IP 封禁（第 {_consecutive_ban} 次），"
                     f"全局退避 {wait:.0f}s")
                time.sleep(min(wait, BAN_418_MAX_S))
                last_err = RuntimeError(f"418 ip banned (consecutive={_consecutive_ban})")
                continue
            if r.status_code == 429:
                wait = _ban_backoff(429, attempt)
                _set_ban(wait)
                _log(f"[binance] 429 限频，全局退避 {wait:.0f}s")
                time.sleep(wait)
                last_err = RuntimeError(f"429 rate limited (consecutive={_consecutive_ban})")
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < MAX_RETRIES + 1:
                time.sleep(0.5 * (attempt + 1))
    raise last_err
