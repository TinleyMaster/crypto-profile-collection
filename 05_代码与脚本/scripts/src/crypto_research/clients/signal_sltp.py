"""KOL 信号止损止盈 AI 顾问（AI 建议 + 2% 总资金硬顶 + 兜底）。

设计（由用户确认的「AI+2%硬顶+兜底」方案）：
1. 入场单先行（抢进场位），AI 推理放在第二步，绝不阻塞开仓
2. AI 结合信号原文 + 现价 + 市场背景（资金费率等）给出止损/止盈距离（%）
3. 硬顶：单笔最大亏损 ≤ 账户总权益 × SIGNAL_MAX_LOSS_PCT_OF_EQUITY，
   换算成止损距离 cap_sl_pct 后 clamp —— AI 可以给更紧，绝不允许更松
4. 兜底：AI 不可用/超时/返回非法值 → 用 SIGNAL_FALLBACK_STOP_LOSS_PCT 兜底，
   绝不出现「AI 没算出来 → 无止损裸奔」
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from crypto_research.clients.llm_client import LLMClient, extract_json_from_llm_response

logger = logging.getLogger("signal_sltp")

# AI 止损距离下限（%）：防止建议过紧被盘面噪音扫掉；低于此值视为非法，走兜底
MIN_SL_PCT = 0.2
# AI 止盈距离下限 / 上限（%）
MIN_TP_PCT = 0.2
MAX_TP_PCT = 50.0


def _signal_delay_hours(signal: dict[str, Any]) -> float | None:
    """信号发帖到现在的小时数（信号时效），无发帖时间返回 None。"""
    pub = signal.get("publish_time")
    if not pub:
        return None
    try:
        delta = datetime.now(timezone.utc) - pub
        return max(0.0, delta.total_seconds() / 3600.0)
    except TypeError:
        return None


class SignalSltpAdvisor:
    """止损止盈 AI 顾问：输入信号 + 盘面快照，输出 clamp 后的止损/止盈建议。

    底线结构（机械化 + AI 定性）：
    - 机械底线：止损距离下限 = max(SIGNAL_ATR_MULTIPLIER × ATR%, MIN_SL_PCT)
      即使 AI 挂了，兜底也基于波动率而非拍脑袋
    - AI 定性：在 [底线, 2% 权益硬顶] 内判断贴紧还是放宽
    - 兜底：AI 失败时用 max(SIGNAL_FALLBACK_STOP_LOSS_PCT, 底线)，仍 clamp 到硬顶
    """

    def __init__(self, settings, llm: LLMClient | None = None) -> None:
        self.settings = settings
        self.llm = llm or LLMClient(settings, cache_ttl=0)

    @property
    def available(self) -> bool:
        return bool(self.settings.signal_ai_sltp_enabled and self.llm.is_available())

    def _mechanical_floor(self, atr_pct: float | None) -> float:
        """机械止损底线（%）：max(ATR 倍数 × ATR%, 最小止损)。"""
        if atr_pct is not None and atr_pct > 0:
            return max(self.settings.signal_atr_multiplier * atr_pct, MIN_SL_PCT)
        return MIN_SL_PCT

    @staticmethod
    def _build_prompt(signal: dict[str, Any], current_price: float,
                      context_text: str, cap_sl_pct: float, floor_sl_pct: float,
                      delay_hours: float | None) -> tuple[str, str]:
        direction_cn = "做多(long)" if signal["direction"] == "long" else "做空(short)"
        delay_text = (f"{delay_hours:.1f} 小时前发帖" if delay_hours is not None
                      else "发帖时间未知")
        system = (
            "你是加密货币合约交易风控顾问。给定一个 KOL 交易信号、盘面快照与风控约束，"
            "给出建议的止损/止盈距离（相对进场价的百分比，正值）。\n"
            "\n"
            "要求：\n"
            "- 止损距离不得低于系统给定的『机械底线』（由 ATR 波动率折算，过紧会被噪音扫掉），"
            "也不得高于硬顶（会被系统截断）。在两者之间判断：波动大/信号不确定/延迟久 → 放宽；"
            "波动小/压力位结构清晰/浮盈已厚 → 贴紧。\n"
            "- 绝不允许建议『不止损』；必须给出正的止损距离。\n"
            "- 止盈可以保守（可小于止损距离），是锦上添花。\n"
            "- 只输出 JSON，不要输出其他内容。JSON 格式："
            '{"stop_loss_pct": 数值, "take_profit_pct": 数值, "reason": "简短中文理由"}'
        )
        user = (
            f"信号：\n"
            f"- 方向：{direction_cn}\n"
            f"- 标的：{signal['symbol']}\n"
            f"- 进场价：{signal['entry_price']}\n"
            f"- 当前价：{current_price}\n"
            f"- 信号置信度：{signal.get('win_rate') or '未知'}%\n"
            f"- 信号发帖：{delay_text}\n"
            f"- 信号原文：{signal.get('raw_text') or '（无）'}\n"
            f"\n"
            f"盘面快照：\n{context_text}\n"
            f"\n"
            f"风控约束：\n"
            f"- 止损距离下限（机械底线）：{floor_sl_pct:.2f}%（基于 ATR，低于此值会被系统抬高）\n"
            f"- 单笔最大允许止损距离：{cap_sl_pct:.2f}%（账户总权益的 "
            f"{signal.get('_max_loss_pct_of_equity', 2.0)}% 折算，超出会被系统截断）\n"
            f"请在此区间内给出你认为最合理的止损距离与止盈距离。"
        )
        return system, user

    def suggest(self, signal: dict[str, Any], current_price: float,
                equity_usdt: float | None, notional_usdt: float,
                context_text: str = "", atr_pct: float | None = None) -> dict[str, Any]:
        """给出 clamp 后的止损/止盈建议。

        Args:
            signal: 解析后的信号 dict
            current_price: 当前价
            equity_usdt: 子账户总权益（USDT）；None 时硬顶不生效
            notional_usdt: 单笔名义价值
            context_text: 盘面快照文本（可空）
            atr_pct: 1h ATR 百分比（用于机械底线；None 时底线=最小止损）

        Returns:
            {stop_loss_pct, take_profit_pct, reason, source, cap_sl_pct, floor_sl_pct}
            - source: "ai" / "fallback" / "none"
        """
        cap_sl_pct = 0.0
        if equity_usdt and equity_usdt > 0 and notional_usdt > 0:
            cap_sl_pct = (equity_usdt * self.settings.signal_max_loss_pct_of_equity
                          / 100.0 / notional_usdt * 100.0)
        floor_sl_pct = self._mechanical_floor(atr_pct)
        fallback = max(self.settings.signal_fallback_stop_loss_pct, floor_sl_pct)

        base = {
            "stop_loss_pct": None, "take_profit_pct": None,
            "reason": "", "source": "none", "cap_sl_pct": cap_sl_pct,
            "floor_sl_pct": floor_sl_pct,
        }

        if not self.available:
            if fallback > 0:
                return {**base, "stop_loss_pct": fallback, "source": "fallback",
                        "reason": f"AI 未启用/不可用，使用兜底止损 {fallback:.2f}%（含 ATR 底线 {floor_sl_pct:.2f}%）"}
            return {**base, "reason": "AI 未启用且未配置兜底止损"}

        signal = {**signal, "_max_loss_pct_of_equity": self.settings.signal_max_loss_pct_of_equity}
        delay_hours = _signal_delay_hours(signal)
        system, user = self._build_prompt(
            signal, current_price, context_text, cap_sl_pct, floor_sl_pct, delay_hours,
        )
        try:
            raw = self.llm.chat(
                system, user, temperature=0.1, max_tokens=600,
                timeout_retries=1, use_cache=False,
                response_format={"type": "json_object"},
            )
            data = extract_json_from_llm_response(raw)
        except Exception as e:
            logger.warning("[sltp] AI 建议失败，使用兜底 %s%%: %s", fallback, e)
            return {**base, "stop_loss_pct": fallback or None, "source": "fallback",
                    "reason": f"AI 失败: {str(e)[:120]}"}

        try:
            sl = float(data.get("stop_loss_pct") or 0)
            tp = float(data.get("take_profit_pct") or 0)
            reason = str(data.get("reason") or "")
        except (TypeError, ValueError):
            logger.warning("[sltp] AI 返回非法数值，使用兜底 %s%%: %s", fallback, str(data)[:120])
            return {**base, "stop_loss_pct": fallback or None, "source": "fallback",
                    "reason": f"AI 返回非法数值: {str(data)[:120]}"}

        source = "ai"
        # 机械底线：AI 过紧 → 抬高到底线（波动率说了算，防噪音扫损）
        if sl < floor_sl_pct:
            sl = floor_sl_pct
            reason = (reason + f"（AI 止损低于 ATR 机械底线，已抬高到 {floor_sl_pct:.2f}%）").strip()
        # 硬顶：超出 → 截断
        elif cap_sl_pct > 0 and sl > cap_sl_pct:
            sl = cap_sl_pct
            reason = (reason + f"（AI 止损超出硬顶，已截断到 {cap_sl_pct:.2f}%）").strip()

        if tp < MIN_TP_PCT:
            tp = 0.0
        elif tp > MAX_TP_PCT:
            tp = MAX_TP_PCT

        logger.info("[sltp] AI 建议 source=%s SL=%.2f%% TP=%.2f%% floor=%.2f%% cap=%.2f%% 理由=%s",
                    source, sl, tp, floor_sl_pct, cap_sl_pct, reason)
        return {
            "stop_loss_pct": sl if sl > 0 else None,
            "take_profit_pct": tp if tp > 0 else None,
            "reason": reason,
            "source": source,
            "cap_sl_pct": cap_sl_pct,
            "floor_sl_pct": floor_sl_pct,
        }
