"""技术指标纯计算模块（RSI / 布林带 / ATR / MACD / Volume Profile），零第三方依赖、不碰 DB / 网络。

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


def vwap_series(highs: list[float], lows: list[float], closes: list[float],
                volumes: list[float], period: int = 20) -> list[float | None]:
    """滚动成交量加权均价 VWAP；输出与输入等长，前 period-1 个为 None。

    典型价 TP = (high + low + close) / 3，VWAP[i] = Σ(TP × vol) / Σvol（窗口内）。
    BTC 现货/合约常用：价格持续在 VWAP 上方=多头强势，下方=空头。
    用于「窗口 VWAP」参考中枢（与 backtest_vp_levels 的朴素基准同口径）。
    """
    n = len(closes)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    tp = [(h + l + c) / 3.0 for h, l, c in zip(highs, lows, closes)]
    pv = sum(tp[i] * volumes[i] for i in range(period))
    vsum = sum(volumes[i] for i in range(period))
    out[period - 1] = pv / vsum if vsum > 0 else None
    for i in range(period, n):
        pv += tp[i] * volumes[i] - tp[i - period] * volumes[i - period]
        vsum += volumes[i] - volumes[i - period]
        out[i] = pv / vsum if vsum > 0 else None
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


# =====================================================================
# 经典 K 线形态识别（纯形状，无趋势上下文）
# =====================================================================

# 形态名 → 中文（供展示层使用）
PATTERN_CN = {
    "doji": "十字星",
    "marubozu_bull": "光头光脚大阳",
    "marubozu_bear": "光头光脚大阴",
    "hammer": "锤子（长下影）",
    "shooting_star": "射击之星（长上影）",
    "bullish_engulfing": "看涨吞没",
    "bearish_engulfing": "看跌吞没",
    "morning_star": "启明星",
    "evening_star": "黄昏星",
}


def candlestick_patterns(opens: list[float], highs: list[float], lows: list[float],
                         closes: list[float]) -> list[list[str]]:
    """逐根识别经典 K 线形态；输出与输入等长，每项为该根命中的形态名列表（英文键）。

    仅识别**形状**（不判断所处趋势方向）：
      - doji 十字星：实体 ≤ 10% 振幅；
      - marubozu_bull/bear 光头光脚：实体 ≥ 90% 振幅；
      - hammer 锤子 / shooting_star 射击之星：长影线 ≥ 2×实体 且对侧影线 ≤ 30% 实体
        （锤子出现在下跌后 = 潜在反转向上，射击之星出现在上涨后 = 潜在反转向下，
        本函数只标形状，方向解读交给展示层结合前 5 根涨跌）；
      - bullish_engulfing / bearish_engulfing 吞没：当前实体完全吞没前一根实体且方向相反；
      - morning_star 启明星 / evening_star 黄昏星：三根组合（长反向实体 → 小实体 → 长同向实体
        且收盘收回第一根实体中点之上/之下）。
    ⚠️ 形态是**辅助确认**不是独立信号：需配合 VP 关键位 + 量能验证，避免单形态裸做。
    """
    n = len(closes)
    out: list[list[str]] = [[] for _ in range(n)]
    if n < 2:
        return out

    def body(i):
        return abs(closes[i] - opens[i])

    def rng(i):
        return highs[i] - lows[i]

    def bull(i):
        return closes[i] > opens[i]

    def bear(i):
        return closes[i] < opens[i]

    for i in range(n):
        r = rng(i)
        if r <= 0:
            continue
        b = body(i)
        up = highs[i] - max(opens[i], closes[i])
        dn = min(opens[i], closes[i]) - lows[i]
        if b <= 0.10 * r:
            out[i].append("doji")
        if b >= 0.90 * r:
            out[i].append("marubozu_bull" if bull(i) else "marubozu_bear")
        if b > 0 and dn >= 2 * b and up <= 0.3 * b:
            out[i].append("hammer")
        if b > 0 and up >= 2 * b and dn <= 0.3 * b:
            out[i].append("shooting_star")
        if i >= 1:
            pb = body(i - 1)
            pr = rng(i - 1)
            if pb > 0 and b > 0 and pr > 0:
                if (bear(i - 1) and bull(i) and opens[i] <= closes[i - 1]
                        and closes[i] >= opens[i - 1] and b > pb):
                    out[i].append("bullish_engulfing")
                if (bull(i - 1) and bear(i) and opens[i] >= closes[i - 1]
                        and closes[i] <= opens[i - 1] and b > pb):
                    out[i].append("bearish_engulfing")
        if i >= 2:
            b2, b1, b0 = body(i - 2), body(i - 1), body(i)
            r2 = rng(i - 2)
            if r2 > 0 and b1 > 0:
                mid2 = (opens[i - 2] + closes[i - 2]) / 2.0
                if (bear(i - 2) and b2 >= 0.5 * r2 and b1 <= 0.3 * r2
                        and bull(i) and b0 > 0 and closes[i] > mid2):
                    out[i].append("morning_star")
                if (bull(i - 2) and b2 >= 0.5 * r2 and b1 <= 0.3 * r2
                        and bear(i) and b0 > 0 and closes[i] < mid2):
                    out[i].append("evening_star")
    return out


# =====================================================================
# Volume Profile（成交量分布 / 成交堆）
# =====================================================================

# 档位上限：防止极宽价格区间 + 极细档位导致内存/算力失控，超出后自动加宽档宽。
_MAX_PROFILE_ROWS = 2000


def _empty_profile(cur: float | None) -> dict:
    return {
        "current_price": cur,
        "window_bars": 0,
        "price_low": None,
        "price_high": None,
        "row_width": None,
        "total_volume": 0.0,
        "poc": None,
        "poc_volume": 0.0,
        "value_area": {"high": None, "low": None, "volume_pct": 0.0},
        "bins": [],
        "hvn": [],
        "lvn": [],
        "resistance": None,
        "support": None,
    }


def volume_profile(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    volumes: list[float],
    *,
    window: int | None = None,
    row_pct: float = 0.005,
    body_mult: float = 2.0,
    node_floor_ratio: float = 0.5,
    lvn_cap_ratio: float = 0.25,
    value_area_pct: float = 0.70,
) -> dict:
    """Volume Profile（成交量分布）估算——从 OHLCV 序列计算 POC / 价值区 / 高低量节点 / 支撑阻力。

    ⚠️ **口径说明（必须先读）**：经典 VP 需要「成交量×价格」的逐笔数据；本项目只有
    `biz.asset_klines` 的 OHLCV（Binance 公开 API 不提供历史 volume-by-price），
    因此本函数采用**行业标准估算口径**（TradingView 内置 VP 对加密资产的做法）：

      1. 价格分档：档宽 = 区间跨度按 `row_pct × 区间最高价` 折算的行数（默认 0.5%），
         每档以档中心价代表；
      2. 单根量分布：把该根 `volumes[i]` 撒到 high–low 覆盖的各档，权重向实体倾斜
         —— 与实体区重叠的部分 ×`body_mult`（默认 2），纯影线区权重 1，逐档归一化
         保证**单根量守恒**（Σ 落到档上的量 == 该根成交量）；
      3. 聚合后：POC = 累计量最大的档；70% 价值区(VA) = 从 POC 向两侧逐档扩张
         （每次取两侧中量更大的一档）直到累计 ≥ `value_area_pct × 总成交量`；
         HVN = 局部极大且量 ≥ `node_floor_ratio × POC`；LVN = 局部极小且量 ≤
         `lvn_cap_ratio × POC`；
      4. 支撑 = 现价下方最近的 HVN，阻力 = 现价上方最近的 HVN（无则退回 VA 低/高）。

    设计约定（沿用模块 §14 原则）：VP 目前只提供**纯函数 + 离线计算/回测**，不接入
    线上扫描；要接入须先有 holdout 为正的消融证据。估算是启发式，`body_mult` 等参数
    是默认值，用于相对比较（POC 位置、节点分布）足够稳健，绝对量仅作参考。

    Args:
        opens/highs/lows/closes/volumes: 等长 OHLCV 序列（volumes 建议传 quote_vol 美元口径，
            跨币可比；base_vol 亦可用）。
        window: 只用最近 N 根；None 表示全部。
        row_pct: 价格档宽度（占区间最高价的比例），默认 0.005（0.5%）。
        body_mult: 实体区相对影线区的密度权重，默认 2.0。
        node_floor_ratio: HVN 过滤下限（相对 POC 量的比例），默认 0.5。
        lvn_cap_ratio: LVN 过滤上限（相对 POC 量的比例），默认 0.25。
        value_area_pct: 价值区累计量占比，默认 0.70。

    Returns:
        dict：current_price / window_bars / price_low / price_high / row_width /
        total_volume / poc / poc_volume / value_area{high,low,volume_pct} /
        bins[{price,volume}, ...]（全档位，供画图）/ hvn / lvn（按量排序的
        [{price,volume}, ...]）/ support / resistance（{price,volume,kind} 或 None）。
    """
    n = len(closes)
    if n < 2 or row_pct <= 0 or body_mult <= 0 or not (0 < value_area_pct <= 1):
        return _empty_profile(closes[-1] if closes else None)

    start = max(0, n - window) if window is not None else 0
    o = opens[start:n]
    h = highs[start:n]
    l = lows[start:n]
    c = closes[start:n]
    v = volumes[start:n]

    price_hi = max(h)
    price_lo = min(l)
    if price_hi <= price_lo:
        return _empty_profile(c[-1] if c else None)

    span = price_hi - price_lo
    n_rows = max(1, int(math.ceil(span / (price_hi * row_pct))))
    n_rows = min(n_rows, _MAX_PROFILE_ROWS)
    row_w = span / n_rows
    bins = [0.0] * n_rows

    for oi, hi, lo, ci, vi in zip(o, h, l, c, v):
        if vi <= 0 or hi <= lo:
            continue
        body_lo = min(oi, ci)
        body_hi = max(oi, ci)
        i0 = max(0, int(math.floor((lo - price_lo) / row_w)))
        i1 = min(n_rows - 1, int(math.ceil((hi - price_lo) / row_w)) - 1)
        if i1 < i0:
            continue
        # 每档权重：与 [low,high] 重叠部分为 1，与实体重叠部分再 ×body_mult；
        # 最后按 Σ权重归一化 → 单根量守恒。
        ws = []
        for i in range(i0, i1 + 1):
            r_lo = price_lo + i * row_w
            r_hi = r_lo + row_w
            ot = max(0.0, min(r_hi, hi) - max(r_lo, lo)) / row_w   # 与整根区间重叠比例
            if ot <= 0:
                ws.append(0.0)
                continue
            ob = max(0.0, min(r_hi, body_hi) - max(r_lo, body_lo)) / row_w  # 与实体重叠比例
            ws.append(body_mult * ob + (ot - ob))
        tot = sum(ws)
        if tot <= 0:
            continue
        for k, i in enumerate(range(i0, i1 + 1)):
            bins[i] += vi * ws[k] / tot

    total_vol = sum(bins)
    if total_vol <= 0:
        return _empty_profile(c[-1] if c else None)

    # ── POC：最大量档 ──
    poc_idx = max(range(n_rows), key=lambda i: bins[i])
    poc_vol = bins[poc_idx]
    poc_price = price_lo + (poc_idx + 0.5) * row_w

    # ── 价值区：从 POC 向两侧逐档扩张，直到累计 ≥ value_area_pct ──
    left = right = poc_idx
    acc = bins[poc_idx]
    target = total_vol * value_area_pct
    while acc < target and (left > 0 or right < n_rows - 1):
        if left == 0:
            right += 1
            acc += bins[right]
        elif right == n_rows - 1:
            left -= 1
            acc += bins[left]
        elif bins[left - 1] >= bins[right + 1]:
            left -= 1
            acc += bins[left]
        else:
            right += 1
            acc += bins[right]
    va_lo = price_lo + (left + 0.5) * row_w
    va_hi = price_lo + (right + 0.5) * row_w

    # ── HVN / LVN：局部极值 + 相对 POC 的量过滤 ──
    # LVN 用「≤ 两侧且至少一侧严格更小」而非严格 `<`：宽真空区是平坦低谷（连续零/极低
    # 量档），严格判定会漏掉；放宽后平坦低谷的边缘各出一个 LVN，正好标出真空区边界。
    hvn: list[tuple[float, float]] = []
    lvn: list[tuple[float, float]] = []
    for i in range(1, n_rows - 1):
        p = bins[i]
        if p > bins[i - 1] and p > bins[i + 1] and p >= node_floor_ratio * poc_vol:
            hvn.append((price_lo + (i + 0.5) * row_w, p))
        elif (p <= bins[i - 1] and p <= bins[i + 1]
              and (p < bins[i - 1] or p < bins[i + 1])
              and p <= lvn_cap_ratio * poc_vol):
            lvn.append((price_lo + (i + 0.5) * row_w, p))
    hvn.sort(key=lambda x: -x[1])
    lvn.sort(key=lambda x: x[1])

    # ── 支撑 / 阻力：现价下方/上方最近的高量节点（无则退回 VA 低/高，且必须在现价该侧）──
    cur = c[-1]
    res_hvn = min(((pr, pv) for pr, pv in hvn if pr > cur), default=None, key=lambda x: x[0])
    sup_hvn = max(((pr, pv) for pr, pv in hvn if pr < cur), default=None, key=lambda x: x[0])
    if res_hvn is not None:
        resistance = {"price": res_hvn[0], "volume": res_hvn[1], "kind": "hvn"}
    elif va_hi > cur:
        resistance = {"price": va_hi, "volume": bins[right], "kind": "value_area"}
    else:
        resistance = None
    if sup_hvn is not None:
        support = {"price": sup_hvn[0], "volume": sup_hvn[1], "kind": "hvn"}
    elif va_lo < cur:
        support = {"price": va_lo, "volume": bins[left], "kind": "value_area"}
    else:
        support = None

    return {
        "current_price": cur,
        "window_bars": n - start,
        "price_low": price_lo,
        "price_high": price_hi,
        "row_width": row_w,
        "total_volume": total_vol,
        "poc": poc_price,
        "poc_volume": poc_vol,
        "value_area": {"high": va_hi, "low": va_lo, "volume_pct": value_area_pct},
        "bins": [{"price": price_lo + (i + 0.5) * row_w, "volume": bins[i]} for i in range(n_rows)],
        "hvn": [{"price": pr, "volume": pv} for pr, pv in hvn],
        "lvn": [{"price": pr, "volume": pv} for pr, pv in lvn],
        "resistance": resistance,
        "support": support,
    }
