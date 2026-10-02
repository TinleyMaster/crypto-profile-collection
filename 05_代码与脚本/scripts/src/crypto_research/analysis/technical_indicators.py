"""技术指标纯计算模块（RSI / 布林带 / ATR / MACD），零第三方依赖、不碰 DB / 网络。

设计约定（方案 §14，2026-09-29 评审定稿）：
  - 指标一律**先影子**（只落 metrics / 回测消融，不发信、不改判定），边际贡献经
    holdout 验证为正后才准入告警逻辑；
  - 本模块只提供**序列级纯函数**，输入 list[float]、输出等长 list（头部数据不足为 None），
    供回测（backtest_scan_scenarios.py）与未来影子采集共用同一实现，避免口径分叉；
  - 布林带用**总体标准差**（除以 n，与主流行情软件口径一致）。
"""
from __future__ import annotations

import math


def sma(values: list[float], period: int) -> list[float | None]:
    """简单移动平均；输出与输入等长，前 period-1 个为 None。"""
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    window_sum = sum(values[:period])
    out[period - 1] = window_sum / period
    for i in range(period, n):
        window_sum += values[i] - values[i - period]
        out[i] = window_sum / period
    return out


def rsi_series(closes: list[float], period: int = 14) -> list[float | None]:
    """Wilder RSI；输出与输入等长，前 period 个为 None。

    首值：前 period 个涨跌幅的简单平均；此后 avg = (avg*(period-1) + x) / period。
    全平序列（无涨跌）返回 50（中性），避免除零。
    """
    n = len(closes)
    out: list[float | None] = [None] * n
    if period <= 0 or n <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        chg = closes[i] - closes[i - 1]
        if chg > 0:
            gains += chg
        else:
            losses -= chg
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_from(avg_gain, avg_loss)
    for i in range(period + 1, n):
        chg = closes[i] - closes[i - 1]
        gain = chg if chg > 0 else 0.0
        loss = -chg if chg < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_from(avg_gain, avg_loss)
    return out


def _rsi_from(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def bollinger_series(closes: list[float], period: int = 20, num_std: float = 2.0,
                     ) -> list[tuple[float, float, float] | None]:
    """布林带；输出与输入等长，每项 (mid, upper, lower) 或 None。std 为总体标准差。"""
    n = len(closes)
    out: list[tuple[float, float, float] | None] = [None] * n
    if period <= 1 or n < period:
        return out
    mids = sma(closes, period)
    for i in range(period - 1, n):
        mid = mids[i]
        if mid is None:
            continue
        window = closes[i - period + 1:i + 1]
        var = sum((x - mid) ** 2 for x in window) / period
        sd = math.sqrt(var)
        upper = mid + num_std * sd
        lower = mid - num_std * sd
        out[i] = (mid, upper, lower)
    return out


def percent_b(close: float, upper: float, lower: float) -> float:
    """%B = (close - lower) / (upper - lower)；带宽为 0（无波动）返回 0.5 中性。"""
    width = upper - lower
    if width <= 0:
        return 0.5
    return (close - lower) / width


def bbw(mid: float, upper: float, lower: float) -> float:
    """布林带宽 = (upper - lower) / mid；mid <= 0 返回 0.0。"""
    if mid <= 0:
        return 0.0
    return (upper - lower) / mid


def atr_series(highs: list[float], lows: list[float], closes: list[float],
               period: int = 14) -> list[float | None]:
    """Wilder ATR（真实波幅均值）；输出与输入等长，前 period 个为 None。

    TR[i] = max(high-low, |high-close[i-1]|, |low-close[i-1]|)——含日内高低幅与跳空，
    与布林带用的收盘对收盘标准差（σ）**口径不同、不可互换**：布林带用 σ 度量离散度，
    ATR 度量的是含跳空的真实波动烈度。

    ⚠️ **不要假定 ATR > σ**：虽然 `high-low ≥ 0` 使 TR 的单根值一般不小于 |close 变动|，
    但经 Wilder 平滑后的 ATR 与总体 σ 的相对大小取决于波动结构。实测（`biz.asset_klines`
    1h，2026-10-01）`ATR(14)/σ20 ≈ 0.66`——加密 1h 收盘波动大，1.5×ATR 止损反而**窄于**
    2σ 止盈。任何依赖「ATR 与 σ 比值」的推断都必须实测，不得用先验。

    首值：TR[1..period] 的简单平均；此后 avg = (avg*(period-1) + TR[i]) / period
    （与 rsi_series 的 Wilder 平滑口径一致）。
    """
    n = len(closes)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period + 1:
        return out
    avg = sum(_true_range(highs[i], lows[i], closes[i - 1])
              for i in range(1, period + 1)) / period
    out[period] = avg
    for i in range(period + 1, n):
        avg = (avg * (period - 1) + _true_range(highs[i], lows[i], closes[i - 1])) / period
        out[i] = avg
    return out


def _true_range(high: float, low: float, prev_close: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def ema_series(values: list[float], period: int) -> list[float | None]:
    """指数移动平均；输出与输入等长，前 period-1 个为 None。

    首值 = 前 period 个值的简单平均（种子），此后 e[i] = v[i]*k + e[i-1]*(1-k)，
    k = 2/(period+1)。种子取 SMA 与 `rsi_series` / `atr_series` 的 Wilder 首值口径同思路
    （先简单平均、再递推），避免「首值 = values[0]」带来的长尾初始化偏差。
    """
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    k = 2.0 / (period + 1)
    for i in range(period, n):
        prev = values[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def macd_series(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9,
                ) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """MACD(12,26,9)；返回 (DIF, DEA, hist) 三个等长列表，头部数据不足为 None。

    DIF  = EMA(fast) − EMA(slow)
    DEA  = EMA(signal) of DIF（信号线）
    hist = DIF − DEA（柱；**不乘 2**，即原始口径）

    预热长度：DIF 自下标 slow−1 起有值（EMA 种子落在 slow−1），hist 自
    slow+signal−2 起有值（标准 12/26/9 ⇒ 下标 33，即前 34 根为 None）。

    ⚠️ 本函数目前**只服务离线回测**（`backtest_bb_rsi_mr.py` 的趋势过滤臂），
    **未接入线上扫描**——设计方案 §14 已判定「MACD 与现有价 × OI 方向判定冗余，
    暂不加维度」，要改该结论须先有 holdout 为正的消融证据。
    """
    n = len(closes)
    dif: list[float | None] = [None] * n
    dea: list[float | None] = [None] * n
    hist: list[float | None] = [None] * n
    if fast <= 0 or slow <= 0 or signal <= 0 or fast >= slow:
        return dif, dea, hist
    ema_fast = ema_series(closes, fast)
    ema_slow = ema_series(closes, slow)
    for i in range(n):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            dif[i] = ema_fast[i] - ema_slow[i]
    start = next((i for i, v in enumerate(dif) if v is not None), None)
    if start is not None:
        seg = dif[start:]
        if len(seg) >= signal:
            sub = ema_series([float(v) for v in seg], signal)
            for j, v in enumerate(sub):
                dea[start + j] = v
    for i in range(n):
        if dif[i] is not None and dea[i] is not None:
            hist[i] = dif[i] - dea[i]
    return dif, dea, hist
