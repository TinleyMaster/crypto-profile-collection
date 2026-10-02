#!/usr/bin/env python3
"""`technical_indicators` 单元测试（2026-10-01 建，2026-10-02 补 MACD）。

背景：为评审「布林带 + RSI 均值回归」策略新增 Wilder ATR(14)（止损位依赖它），
2026-10-02 又为该回测新增 `ema_series` / `macd_series`（趋势过滤臂依赖它）。
本模块是**纯函数**、被多处复用，故补直接单测（此前只有 rsi/bollinger，无 ATR/MACD）。

判据：手算精确值 + 不变量（长度等长、头部 None、n 不足全 None、ATR > 0 且 ≥ 单根
|收盘变动| 的平滑下界；EMA 种子=SMA；DIF=EMA_fast−EMA_slow；hist=DIF−DEA）。
**不断言 ATR 与 σ 的大小关系**——实测 ATR/σ ≈ 0.66，该关系取决于波动结构，不可写死。
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "scripts" / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

from crypto_research.analysis.technical_indicators import (  # noqa: E402
    atr_series,
    bollinger_series,
    ema_series,
    macd_series,
    rsi_series,
)

FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global FAIL
    if cond:
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def main() -> int:
    print("[test_atr_series] 手算精确值（period=3）")
    # TR[1..4] 手算全为 3；period=3 ⇒ out[3]=3，其后 Wilder 平滑亦 =3
    highs = [10.0, 12.0, 13.0, 14.0, 15.0]
    lows = [8.0, 9.0, 10.0, 11.0, 12.0]
    closes = [9.0, 11.0, 12.0, 13.0, 14.0]
    out = atr_series(highs, lows, closes, 3)
    check("长度与输入等长", len(out) == len(closes), f"{len(out)} != {len(closes)}")
    check("头部 period 个为 None", out[:3] == [None, None, None], str(out[:3]))
    check("out[3] == 3.0（手算）", out[3] == 3.0, str(out[3]))
    check("out[4] == 3.0（手算，Wilder 平滑）", out[4] == 3.0, str(out[4]))

    print("[test_atr_series] 不变量")
    check("n < period+1 ⇒ 全 None",
          atr_series([1.0, 2.0], [0.5, 1.0], [0.8, 1.5], 14) == [None, None])
    check("period <= 0 ⇒ 全 None",
          atr_series(highs, lows, closes, 0) == [None] * 5)

    # 跳空使 TR 取 |high-prev_close|
    gap_h = [10.0, 10.0, 10.0, 20.0]
    gap_l = [9.0, 9.0, 9.0, 19.0]
    gap_c = [9.5, 9.5, 9.5, 19.5]
    g = atr_series(gap_h, gap_l, gap_c, 3)
    # TR[3] = max(20-19, |20-9.5|, |19-9.5|) = 10.5；out[3] = mean(1, 1, 10.5) = 4.1667
    check("跳空被 |high-prev_close| 捕捉", abs(g[3] - (1 + 1 + 10.5) / 3) < 1e-9, str(g[3]))

    print("[test_atr_series] 与 rsi/bollinger 共存（回归护栏）")
    check("rsi_series 仍可调用且等长", len(rsi_series(closes, 2)) == len(closes))
    check("bollinger_series 仍可调用", bollinger_series(closes, 3, 2.0) is not None)

    # ── EMA：手算精确值（period=3，[1,2,3,4,5]）──
    print("[test_atr_series] ema_series 手算精确值（period=3）")
    ramp = [1.0, 2.0, 3.0, 4.0, 5.0]
    e = ema_series(ramp, 3)
    # 种子 = SMA(1,2,3) = 2.0；k = 2/4 = 0.5 ⇒ 4*0.5+2*0.5=3.0，5*0.5+3*0.5=4.0
    check("长度等长且头部 2 个 None", len(e) == 5 and e[:2] == [None, None], str(e))
    check("e[2] == 2.0（种子=SMA）", e[2] == 2.0, str(e[2]))
    check("e[3] == 3.0 / e[4] == 4.0（手算递推）", e[3] == 3.0 and e[4] == 4.0, str(e[3:]))
    check("递推口径 e[i] = v*k + e[i-1]*(1-k)",
          abs(e[4] - (5.0 * (2 / 4) + e[3] * (1 - 2 / 4))) < 1e-12, str(e[4]))
    check("n < period ⇒ 全 None", ema_series([1.0, 2.0], 3) == [None, None])
    check("period <= 0 ⇒ 全 None", ema_series(ramp, 0) == [None] * 5)

    # ── MACD：手算精确值（fast=2, slow=3, signal=2，[1,2,3,4,5]）──
    print("[test_atr_series] macd_series 手算精确值（fast=2, slow=3, signal=2）")
    dif, dea, hist = macd_series(ramp, 2, 3, 2)
    # EMA2 = [None,1.5,2.5,3.5,4.5]；EMA3 = [None,None,2.0,3.0,4.0]
    # ⇒ DIF 自下标 2 起恒为 0.5；DEA = EMA2(DIF 段[0.5,0.5,0.5]) ⇒ dea[3]=dea[4]=0.5
    check("三者长度与输入等长",
          len(dif) == len(dea) == len(hist) == 5, f"{len(dif)},{len(dea)},{len(hist)}")
    check("DIF 头部 slow-1 个为 None 且其后 = EMA2−EMA3 ≈ 0.5",
          dif[:2] == [None, None]
          and all(abs(v - 0.5) < 1e-9 for v in dif[2:]), str(dif))
    check("DEA 头部 slow+signal-2 个为 None（=下标 3）",
          dea[:3] == [None, None, None]
          and all(v is not None and abs(v - 0.5) < 1e-9 for v in dea[3:]), str(dea))
    check("hist = DIF − DEA（且此例恰为 0.0）",
          hist[:3] == [None, None, None]
          and abs(hist[3]) < 1e-12 and abs(hist[4]) < 1e-12, str(hist))

    print("[test_atr_series] macd_series 不变量（标准 12/26/9）")
    long_ramp = [float(i) for i in range(1, 61)]
    d12, a12, h12 = macd_series(long_ramp)          # 默认 12/26/9
    check("默认参数 = 12/26/9（预热：DIF 自下标 25、hist 自下标 33）",
          all(v is None for v in d12[:25]) and d12[25] is not None
          and all(v is None for v in h12[:33]) and h12[33] is not None,
          f"dif[25]={d12[25]}, hist[33]={h12[33]}")
    check("严格递增序列 ⇒ DIF > 0（快线在慢线之上 = 上升趋势）",
          d12[25] > 0, f"{d12[25]}")
    check("线性匀速上涨 ⇒ hist ≈ 0（柱 = 加速度，匀速不含加速度）",
          abs(h12[33]) < 1e-9 and abs(h12[-1]) < 1e-9, f"hist[33]={h12[33]}, hist[-1]={h12[-1]}")
    check("hist ≡ DIF − DEA（全段逐点）",
          all(abs(h12[i] - (d12[i] - a12[i])) < 1e-12
              for i in range(33, len(h12))), "存在不满足的点")
    check("n < slow ⇒ 三者全 None",
          macd_series([1.0, 2.0, 3.0]) == ([None] * 3, [None] * 3, [None] * 3))
    check("fast >= slow ⇒ 三者全 None（参数非法不静默算错）",
          macd_series(long_ramp, 26, 12, 9) == ([None] * 60, [None] * 60, [None] * 60))

    print(f"[test_atr_series] {'全部通过' if FAIL == 0 else f'{FAIL} 项失败'}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())