"""
催化剂 AI 决策层（G7，慢通道增强）。

职责：
    对已生成 G1-G6 的 open 信号，用 LLM 补全「投资决策四要素」：
      1. reasoning          —— AI 推荐原因（为什么值得关注/买入）
      2. investment_cycle   —— 投资周期（短期/中期/长期）
      3. target_price_review —— 目标价（对已有 take_profit 做合理性校验）
      4. stop_loss_review    —— 止损价（对已有 stop_loss 做合理性校验)

设计约束：
    - LLM 不可用时返回 None，慢通道其他步骤不受影响（降级不阻塞）
    - 目标价/止损以 G5 已算的 take_profit/stop_loss 为基线，AI 只能返回建议，
      是否采用由调用方决定（默认采用 AI 建议，无建议则保留规则值）
    - 一次调用补全一个信号，便于针对性失败重试

输出 JSON 结构（固定）：
    {
      "reasoning": "……",
      "investment_cycle": "短期|中期|长期",
      "target_price": <float|null>,
      "stop_loss": <float|null>
    }
"""
from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# 投资周期枚举（signal 表 CHECK 约束）
INVESTMENT_CYCLES = ("短期", "中期", "长期")

# AI 决策系统提示词
_SYSTEM_PROMPT = (
    "你是加密货币催化剂决策的资深分析师。基于给定的信号上下文，输出"
    "『AI推荐原因+投资周期+目标价评审+止损评审』。只输出合法 JSON，"
    "不要输出任何多余文字。"
)

# AI 决策用户提示词模板
_USER_PROMPT_TEMPLATE = """请对以下催化剂信号给出投资决策建议。

## 信号上下文
- 代币: {asset_name} ({symbol})
- 催化剂类型: {kind}
- 催化剂标题: {catalyst_title}
- 催化剂摘要(截断): {catalyst_summary}
- 市场环境: {regime}
- 综合评分: {composite_score}/100（等级 {tier}）
- 技术面: {technical_state}
- 持续性: {persistence}
- 当前规则目标价: {take_profit}
- 当前规则止损价: {stop_loss}
- 进场价(参考): {entry_price}
- 风险收益比: {rr_ratio}

## 你的任务
返回如下 JSON（字段固定，勿增删）：
{{
  "reasoning": "2-4 句话的中文推荐原因，说明催化剂驱动力、上行逻辑与主要风险，禁止虚构数据。",
  "investment_cycle": "{cycles_enum}",
  "target_price": 浮点数或 null,
  "stop_loss": 浮点数或 null
}}

## 约束
- target_price/stop_loss 是「对规则值的评审」，若合理可原样返回；若有更强依据可调整，否则返回 null 表示沿用规则值。
- investment_cycle 只能取枚举之一。
- 必须为纯 JSON，无需 markdown 代码块。
"""


class AIDecisionGenerator:
    """AI 决策生成器（慢通道 G7）。"""

    def __init__(self, llm_client, timeout: int = 60):
        """
        Args:
            llm_client: crypto_research.clients.llm_client.LLMClient 实例
            timeout: 单次调用超时（秒）
        """
        self._llm = llm_client
        self._timeout = timeout

    def generate(
        self,
        symbol: str,
        asset_name: str,
        kind: str,
        catalyst_title: str,
        catalyst_summary: Optional[str],
        regime: Optional[str],
        composite_score: int,
        tier: Optional[str],
        technical_state: Optional[str],
        persistence: Optional[str],
        take_profit: Optional[float],
        stop_loss: Optional[float],
        entry_price: Optional[float],
        rr_ratio: Optional[float],
    ) -> Optional[dict]:
        """对单个信号生成 AI 决策建议。

        Returns:
            dict: {"reasoning", "investment_cycle", "target_price", "stop_loss"}
            LLM 不可用/失败/解析失败时返回 None
        """
        if self._llm is None or not getattr(self._llm, "is_available", lambda: False)():
            return None

        user_prompt = _USER_PROMPT_TEMPLATE.format(
            asset_name=asset_name or symbol,
            symbol=symbol,
            kind=kind,
            catalyst_title=(catalyst_title or "")[:80],
            catalyst_summary=(catalyst_summary or "")[:200],
            regime=regime or "neutral",
            composite_score=composite_score,
            tier=tier or "none",
            technical_state=technical_state or "unknown",
            persistence=persistence or "unknown",
            take_profit=_fmt_price(take_profit),
            stop_loss=_fmt_price(stop_loss),
            entry_price=_fmt_price(entry_price),
            rr_ratio=rr_ratio,
            cycles_enum="/".join(INVESTMENT_CYCLES),
        )

        try:
            raw = self._llm.chat(
                _SYSTEM_PROMPT,
                user_prompt,
                temperature=0.2,
                max_tokens=512,
                response_format={"type": "json_object"},
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("AI 决策调用失败: %s", e, exc_info=True)
            return None

        result = self._parse(raw)
        if result is None:
            return None

        # 校验周期枚举
        cyc = result.get("investment_cycle")
        if cyc not in INVESTMENT_CYCLES:
            result["investment_cycle"] = None
        return result

    def _parse(self, raw) -> Optional[dict]:
        """解析 LLM 原始输出为 JSON dict。"""
        if not raw:
            return None
        # 兼容 extract_json_from_llm_response 已解出的 dict，或原始字符串
        if isinstance(raw, dict):
            text = raw
        else:
            try:
                from crypto_research.clients.llm_client import extract_json_from_llm_response
                text = extract_json_from_llm_response(raw)
            except Exception:  # noqa: BLE001
                try:
                    text = json.loads(str(raw))
                except Exception:  # noqa: BLE001
                    logger.warning("AI 决策响应无法解析: %r", raw, exc_info=True)
                    return None
        if not isinstance(text, dict):
            return None

        out = {}
        out["reasoning"] = str(text.get("reasoning") or "").strip() or None
        out["investment_cycle"] = (text.get("investment_cycle") or "").strip() or None
        out["target_price"] = _to_optional_float(text.get("target_price"))
        out["stop_loss"] = _to_optional_float(text.get("stop_loss"))

        # 至少要产出推荐原因或周期之一，否则视为无效
        if out["reasoning"] is None and out["investment_cycle"] is None:
            return None
        return out


def _fmt_price(v) -> str:
    if v is None:
        return "null"
    try:
        return f"{float(v):,.6f}"
    except (TypeError, ValueError):
        return "null"


def _to_optional_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        return None if f <= 0 else f
    except (TypeError, ValueError):
        return None