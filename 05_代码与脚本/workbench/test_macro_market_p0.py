#!/usr/bin/env python3
"""早报 P0-1 / P0-2 回归护栏（复验_早报P0_5603793_2026-09-22 §四 F2）。

运行：python workbench/test_macro_market_p0.py
      （纯离线，不连网、不连库；mock requests + 用当天日期验证「今天」自检）

背景：P0-1（BTC/ETH 24h 涨跌幅改用真滚动 24h + 一致性断言）、P0-2（宏观日历 CPI
日期纠正 + 运行时自检）此前只在临时脚本里跑过、未入库，且并发进程持续改
macro_market.py，无护栏则可能被静默改坏。本文件把验收口径固化成可执行判据：

  A. _check_price_consistency —— 方向背离必须拦下（A1 即当年 P0-1 现场）
  B. _validate_macro_events   —— 日期不可解析 / 命中今天 / 重复
  C. _fetch_binance_24hr_change —— 成功 / 网络失败 / 缺字段(F1) / 非数值
  D. kline 集成 —— ticker 覆盖成功、缺字段与网络失败均回退日线口径（F1 回归）
"""
import os
import sys
from datetime import date

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import macro_market as mm  # noqa: E402

passed = 0
failed = 0


def check(cond, name, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  \u2713 {name}")
    else:
        failed += 1
        print(f"  \u2717 {name}")
        if detail:
            print(f"    {detail}")


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def _klines(closes):
    rows = []
    for c in closes:
        rows.append([0, str(c), str(c + 1), str(c - 1), str(c), "100",
                     0, 0, 0, 0, 0, 0])
    return rows


# ── A. 一致性断言 ──
print("[A] _check_price_consistency")
bug = {"M0_tldr": {"btc_change_24h_pct": -0.2, "eth_change_24h_pct": -0.2},
       "DIFF": {"total_mcap_pct": 4.5}}
check(mm._check_price_consistency(bug) is not None, "A1 方向背离(BTC-0.2 vs 总市值+4.5) 触发告警")
check(mm._check_price_consistency(
    {"M0_tldr": {"btc_change_24h_pct": 5.6, "eth_change_24h_pct": 2.8},
     "DIFF": {"total_mcap_pct": 4.5}}) is None, "A2 同向一致不触发")
check(mm._check_price_consistency({"M0_tldr": {"btc_change_24h_pct": 5.0}, "DIFF": {}}) is None,
      "A3 缺 total_mcap_pct 守卫")
check(mm._check_price_consistency({"M0_tldr": {}, "DIFF": {"total_mcap_pct": 1.0}}) is None,
      "A4 两侧均缺值不触发")

# ── B. 宏观日程自检 ──
print("[B] _validate_macro_events")
today = date.today().isoformat()
check(any("今天" in w for w in mm._validate_macro_events([{"date": today, "event": "CPI 公布"}])),
      "B1 命中「今天」告警")
check(any("不可解析" in w for w in mm._validate_macro_events([{"date": "2026/09/11", "event": "X"}])),
      "B2 日期不可解析告警")
dup = mm._validate_macro_events([{"date": "2026-10-02", "event": "NFP 非农"},
                                 {"date": "2026-10-02", "event": "NFP 非农"}])
check(any("重复" in w for w in dup), "B3 重复日程告警", str(dup))
check(mm._validate_macro_events([{"date": "2026-10-02", "event": "NFP 非农"}]) == [],
      "B4 正常日程无告警")

# ── C. ticker 助手 ──
print("[C] _fetch_binance_24hr_change")
mm.requests.get = lambda *a, **k: _Resp({"priceChangePercent": "5.63", "lastPrice": "86123.45"})
c1 = mm._fetch_binance_24hr_change("BTCUSDT")
check(c1 and c1["change_24h_pct"] == 5.63 and abs(c1["last_price"] - 86123.45) < 1e-9,
      "C1 成功解析", str(c1))

mm.requests.get = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
check(mm._fetch_binance_24hr_change("BTCUSDT") is None, "C2 网络失败返回 None")

mm.requests.get = lambda *a, **k: _Resp({"lastPrice": "86123.45"})
check(mm._fetch_binance_24hr_change("BTCUSDT") is None,
      "C3 缺 priceChangePercent 返回 None（F1 修复，旧实现误返回 0.0）")

mm.requests.get = lambda *a, **k: _Resp({"priceChangePercent": "N/A", "lastPrice": "1"})
check(mm._fetch_binance_24hr_change("BTCUSDT") is None, "C4 非数值返回 None")

# ── D. kline 集成（ticker 覆盖 + 回退） ──
print("[D] kline 集成")


def _dispatch(kl, tk):
    def _g(url, params=None, timeout=None):
        if "ticker/24hr" in url:
            if isinstance(tk, Exception):
                raise tk
            return _Resp(tk)
        return _Resp(kl)
    return _g


# 日线口径算出 +1.0%（closes[-2]=100, latest=101）
kl = _klines([100] * 88 + [100, 101])

mm.requests.get = _dispatch(kl, {"priceChangePercent": "5.63", "lastPrice": "86123.45"})
btc = mm.fetch_binance_btc_klines()
check(btc["change_24h"] == 5.63 and btc["change_24h_pct"] == 5.63,
      "D1 ticker 成功覆盖真实滚动 24h", str(btc["change_24h"]))
check(abs(btc["price"] - 86123.45) < 1e-9, "D2 价格与涨跌幅同源（lastPrice）", str(btc["price"]))

# F1 回归：缺字段必须回退日线 +1.0，而非被 0.0 覆盖
mm.requests.get = _dispatch(kl, {"lastPrice": "86123.45"})
btc = mm.fetch_binance_btc_klines()
check(btc["change_24h"] == 1.0,
      "D3 缺字段回退日线口径（F1 回归：不得为 0.0）", str(btc["change_24h"]))

# 网络失败回退
mm.requests.get = _dispatch(kl, RuntimeError("boom"))
eth = mm.fetch_binance_eth_klines()
check(eth["change_24h"] == 1.0, "D4 网络失败回退日线口径", str(eth["change_24h"]))

# ── 汇总 ──
print(f"\n{passed}/{passed + failed} 通过")
sys.exit(1 if failed else 0)
