"""
催化剂 G5 技术面分析（日线级，基于 biz.asset_market_daily）。

⚠️ 重要限制声明：
仅使用日频收盘数据，无 OHLC / 无分钟线。
只能算：MA排列、价格 vs 均线、量能 z、距高低位、相对强度。
不能做：K 线形态、缠论、分钟级结构。
所有输出必须标注「日线级」。

输出：
- technical_state: up / range / down（趋势状态）
- entry_trigger: 触发条件描述（回踩MA20 / 突破30d前高 等）
- entry_trigger_price: 触发价
- support_price: 关键支撑位（MA20 / 前低）
- resistance_price: 关键阻力位（前高 / MA60）
- stop_loss_price: 建议止损价
- take_profit_price: 建议止盈价
- rr_ratio: 预估风险收益比
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class TechnicalResult:
    asset_id: int
    technical_state: str = "range"     # up / range / down
    entry_trigger: Optional[str] = None
    entry_trigger_price: Optional[float] = None
    support_price: Optional[float] = None
    resistance_price: Optional[float] = None
    ma5: Optional[float] = None
    ma20: Optional[float] = None
    ma60: Optional[float] = None
    price_30d_high: Optional[float] = None
    price_30d_low: Optional[float] = None
    atr_30d: Optional[float] = None    # 30 日波动率（近似 ATR，用日涨跌幅 std 估算）
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    rr_ratio: Optional[float] = None
    detail: dict = None


class TechnicalAnalyzer:
    """G5 日线级技术分析器。

    输入：最近 N 天的日线数据（price_usd, volume_24h, market_date）
    输出：趋势状态 + 入场触发位 + 支撑阻力
    """

    def __init__(self, config: dict):
        g5 = config.get("g5_technical", {})
        self.ma_short = int(g5.get("ma_short", 5))
        self.ma_mid = int(g5.get("ma_mid", 20))
        self.ma_long = int(g5.get("ma_long", 60))
        self.high_low_days = int(g5.get("high_low_days", 30))
        self.atr_days = int(g5.get("atr_days", 30))

        # 入场触发配置
        self.entry = g5.get("entry_triggers", {})

    def analyze(self,
                asset_id: int,
                daily_data: list[dict],
                impact_direction: Optional[str] = None) -> TechnicalResult:
        """日线级技术分析。

        Args:
            asset_id: 资产 ID
            daily_data: 日线数据列表，按日期升序排列（老→新）。
                每个 dict 需含: market_date, price_usd, volume_24h
            impact_direction: 催化剂影响方向（bullish / bearish / neutral）

        Returns:
            TechnicalResult
        """
        result = TechnicalResult(asset_id=asset_id)

        if not daily_data or len(daily_data) < 5:
            result.detail = {"error": "insufficient_data", "days": len(daily_data)}
            return result

        prices = [float(d["price_usd"]) for d in daily_data if d.get("price_usd") is not None]
        if len(prices) < 5:
            result.detail = {"error": "insufficient_prices"}
            return result

        # 最新价
        last_price = prices[-1]

        # 计算 MA
        ma5 = self._ma(prices, self.ma_short) if len(prices) >= self.ma_short else None
        ma20 = self._ma(prices, self.ma_mid) if len(prices) >= self.ma_mid else None
        ma60 = self._ma(prices, self.ma_long) if len(prices) >= self.ma_long else None

        result.ma5 = ma5
        result.ma20 = ma20
        result.ma60 = ma60

        # 30d 高低位
        recent = prices[-self.high_low_days:] if len(prices) >= self.high_low_days else prices
        result.price_30d_high = max(recent)
        result.price_30d_low = min(recent)

        # 30d 波动率（ATR 近似：日收益率标准差 * 价格）
        if len(prices) >= self.atr_days:
            returns = []
            for i in range(1, len(prices)):
                if prices[i - 1] > 0:
                    returns.append(abs(prices[i] / prices[i - 1] - 1))
            if returns:
                avg_ret = sum(returns[-self.atr_days:]) / min(len(returns), self.atr_days)
                result.atr_30d = round(last_price * avg_ret, 6)

        # 判定趋势状态
        result.technical_state = self._determine_state(last_price, ma5, ma20, ma60)

        # 支撑位 & 阻力位
        result.support_price = ma20 if ma20 else result.price_30d_low
        result.resistance_price = result.price_30d_high

        # 入场触发（根据影响方向）
        # impact_direction 缺失时，用技术面趋势状态推断方向（避免一律默认 bullish）
        if impact_direction:
            direction = impact_direction.lower()
        else:
            if result.technical_state == "up":
                direction = "bullish"
            elif result.technical_state == "down":
                direction = "bearish"
            else:
                direction = "neutral"

        if direction == "bullish":
            # 多头：回踩 MA20 企稳 或 突破 30d 前高
            if ma20 and last_price > ma20 and (result.price_30d_high - last_price) / last_price < 0.10:
                # 接近前高，突破型
                result.entry_trigger = f"突破30d前高 ${result.price_30d_high:.4g}"
                result.entry_trigger_price = result.price_30d_high
            elif ma20 and last_price > ma20:
                # 回踩 MA20
                result.entry_trigger = f"回踩MA20企稳（${ma20:.4g}）"
                result.entry_trigger_price = ma20
            else:
                # 下跌趋势中，等企稳
                result.entry_trigger = f"企稳并突破MA5（${ma5:.4g}）" if ma5 else "等待企稳信号"
                result.entry_trigger_price = ma5 if ma5 else last_price * 1.05

        elif direction == "bearish":
            # 空头：跌破 MA20 或 跌破 30d 前低
            if ma20 and last_price < ma20:
                result.entry_trigger = f"跌破MA20确认（${ma20:.4g}）"
                result.entry_trigger_price = ma20
            else:
                result.entry_trigger = f"跌破30d前低 ${result.price_30d_low:.4g}"
                result.entry_trigger_price = result.price_30d_low

        else:
            # 中性：区间上下沿
            mid = (result.price_30d_high + result.price_30d_low) / 2
            if last_price > mid:
                result.entry_trigger = f"回踩区间中轨 ${mid:.4g} 企稳"
                result.entry_trigger_price = mid
            else:
                result.entry_trigger = f"突破区间中轨 ${mid:.4g}"
                result.entry_trigger_price = mid

        # 止盈止损计算（基于支撑阻力位，RR 动态而非固定 2.2）
        self._calc_risk_reward(result, last_price, direction)

        # detail
        result.detail = {
            "last_price": last_price,
            "ma5": ma5,
            "ma20": ma20,
            "ma60": ma60,
            "high_30d": result.price_30d_high,
            "low_30d": result.price_30d_low,
            "atr_30d": result.atr_30d,
            "state": result.technical_state,
            "impact_direction": impact_direction,
            "stop_loss": result.stop_loss_price,
            "take_profit": result.take_profit_price,
            "rr_ratio": result.rr_ratio,
        }

        return result

    def _calc_risk_reward(self, result: TechnicalResult, last_price: float,
                          direction: str) -> None:
        """基于支撑阻力位计算止盈止损，RR 因资产技术位而异。

        原则：
        - 多头：止损在支撑位，止盈在阻力位
        - 空头：止损在阻力位，止盈在支撑位
        - 如果阻力/支撑空间不足导致 RR < 2.0，用 ATR 扩展目标位
        - 最小止损距离 = 1x ATR（防止止损太近被洗）
        """
        entry = result.entry_trigger_price or last_price
        support = result.support_price
        resistance = result.resistance_price
        atr = result.atr_30d or (entry * 0.05)  # 兜底 5%
        min_stop_dist = atr  # 最小止损 = 1x ATR
        min_rr = 2.0

        if direction == "bullish":
            # 多头：止损 = max(支撑位, entry - 1x ATR)
            raw_stop = support if support and support < entry else entry - min_stop_dist
            stop = min(raw_stop, entry - min_stop_dist)
            risk = entry - stop
            if risk <= 0:
                risk = min_stop_dist
                stop = entry - risk

            # 止盈优先用阻力位
            if resistance and resistance > entry:
                reward_res = resistance - entry
                rr_res = reward_res / risk if risk > 0 else 0
                if rr_res >= min_rr:
                    # 阻力位空间足够，直接用
                    take_profit = resistance
                else:
                    # 阻力位太近，用 min_rr 倍数扩展
                    take_profit = entry + risk * max(2.5, min_rr)
            else:
                # 无阻力位，用 ATR 目标
                take_profit = entry + risk * 2.5

            rr = round((take_profit - entry) / risk, 2) if risk > 0 else None

        elif direction == "bearish":
            # 空头：止损 = min(阻力位, entry + 1x ATR)
            raw_stop = resistance if resistance and resistance > entry else entry + min_stop_dist
            stop = max(raw_stop, entry + min_stop_dist)
            risk = stop - entry
            if risk <= 0:
                risk = min_stop_dist
                stop = entry + risk

            # 止盈优先用支撑位
            if support and support < entry:
                reward_sup = entry - support
                rr_sup = reward_sup / risk if risk > 0 else 0
                if rr_sup >= min_rr:
                    take_profit = support
                else:
                    take_profit = entry - risk * max(2.5, min_rr)
            else:
                take_profit = entry - risk * 2.5

            rr = round((entry - take_profit) / risk, 2) if risk > 0 else None

        else:
            # 中性：默认对称 1:2.5 ATR
            stop = entry - atr
            take_profit = entry + atr * 2.5
            rr = 2.5

        result.stop_loss_price = round(stop, 6) if stop and stop > 0 else None
        result.take_profit_price = round(take_profit, 6) if take_profit and take_profit > 0 else None
        result.rr_ratio = rr if rr and rr > 0 else None

        # ── 防御性校验：确保止盈止损价格与方向一致 ──
        # 任何上游 bug 导致方向错位，这里兜底纠正
        entry_val = result.entry_trigger_price or last_price
        sl_val = result.stop_loss_price
        tp_val = result.take_profit_price

        if entry_val and sl_val and tp_val and sl_val > 0 and tp_val > 0:
            if direction == "bullish" and not (sl_val < entry_val < tp_val):
                # 看多但价格不对，交换止盈止损并重新算 RR
                result.stop_loss_price, result.take_profit_price = min(sl_val, tp_val), max(sl_val, tp_val)
                risk = entry_val - result.stop_loss_price
                reward = result.take_profit_price - entry_val
                if risk > 0:
                    result.rr_ratio = round(reward / risk, 2)
            elif direction == "bearish" and not (tp_val < entry_val < sl_val):
                # 看空但价格不对，交换止盈止损并重新算 RR
                result.stop_loss_price, result.take_profit_price = max(sl_val, tp_val), min(sl_val, tp_val)
                risk = result.stop_loss_price - entry_val
                reward = entry_val - result.take_profit_price
                if risk > 0:
                    result.rr_ratio = round(reward / risk, 2)

    # ---- 内部方法 ----

    def _ma(self, prices: list[float], period: int) -> Optional[float]:
        """简单移动平均线。"""
        if len(prices) < period:
            return None
        return sum(prices[-period:]) / period

    def _determine_state(self,
                         price: float,
                         ma5: Optional[float],
                         ma20: Optional[float],
                         ma60: Optional[float]) -> str:
        """判定趋势状态。

        up:  MA5 > MA20 > MA60 且 价在 MA20 之上
        down: MA5 < MA20 < MA60 且 价在 MA20 之下
        range: 其他情况
        """
        if ma5 is None or ma20 is None:
            return "range"

        if ma60 is not None:
            if ma5 > ma20 > ma60 and price > ma20:
                return "up"
            if ma5 < ma20 < ma60 and price < ma20:
                return "down"
        else:
            if ma5 > ma20 and price > ma20:
                return "up"
            if ma5 < ma20 and price < ma20:
                return "down"

        return "range"
