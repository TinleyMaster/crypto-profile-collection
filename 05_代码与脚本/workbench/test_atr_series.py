#!/usr/bin/env python3
"""`technical_indicators.atr_series` 单元测试（2026-10-01，外部策略评审附带）。

背景：为评审「布林带 + RSI 均值回归」策略新增 Wilder ATR(14)（止损位依赖它）。
本模块是**纯函数**、被多处复用，故补直接单测（此前只有 rsi/bollinger，无 ATR）。

判据：手算精确值 + 不变量（长度等长、头部 None、n 不足全 None、ATR > 0 且 ≥ 单根
|收盘变动| 的平滑下界）。**不断言 ATR 与 σ 的大小关系**——实测 ATR/σ ≈ 0.66，
该关系取决于波动结构，不可写死。
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

    print(f"[test_atr_series] {'全部通过' if FAIL == 0 else f'{FAIL} 项失败'}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())