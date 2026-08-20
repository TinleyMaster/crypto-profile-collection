"""
KOL 帖子 AI 信号分类与结构化提取。

调用系统已有的 llm_client，对帖子内容进行分析，输出结构化信号字段。

核心判断规则（优先级从高到低）：
  1. already_entered = true → post_type = after_action
  2. has_pnl_number = true  → post_type = after_action
  3. 持仓截图 / 回顾性语言 → 倾向 after_action
  4. 明确入场条件 + 未进场 + 无盈亏数字 → prediction
  5. 只讲方向无操作 → analysis
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# 路径兼容
if os.path.exists("/app/scripts/src"):
    SCRIPTS_SRC = Path("/app/scripts/src")
else:
    WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
    CODE_ROOT = WORKSPACE_ROOT.parent
    SCRIPTS_SRC = CODE_ROOT / "scripts" / "src"

if str(SCRIPTS_SRC) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_SRC))

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.clients.llm_client import LLMClient, extract_json_from_llm_response  # noqa: E402

_settings = get_settings(require_database=False)
_llm = LLMClient(_settings, rpm=30) if _settings else None


def _get_llm() -> LLMClient | None:
    global _llm, _settings
    if _llm is None:
        _settings = get_settings(require_database=False)
        _llm = LLMClient(_settings, rpm=30)
    return _llm if _llm.is_available() else None


SYSTEM_PROMPT = """你是一个加密货币交易信号分析专家。你的任务是分析 KOL（关键意见领袖）发布的帖子，
判断其是否为有跟单价值的「实时喊单」，并提取结构化交易信息。

## 核心判断规则（必须严格遵守）

### 最高优先级：判断是否为「事后晒单」
只要满足以下任一条件，post_type 必须为 "after_action"：
1. **博主明确表示已经进场/持仓/开仓**：出现"我已经进场了"、"我已开多"、"我已建仓"、"我拿着"、"我在持有"、"我已经上车"、"已入场"、"已布局"等表述
2. **出现具体盈亏数字**：如"+2341 USDT"、"赚了 5000u"、"浮盈 30%"、"亏损 2000刀"、"盈利 $1500"等
3. **晒持仓截图**：帖子中提到"截图"、"持仓图"、"实盘"、"交割单"，或上下文明显在晒收益
4. **回顾性语言**：出现"我说过"、"我就知道"、"果然"、"已经突破"、"如我所料"、"之前说的"、"应验了"等表述

### 「实时喊单」(prediction) 的严格定义
只有**同时满足**以下所有条件，才标记为 "prediction"：
1. 给出了**明确的入场条件**（如"突破 66600 做多"、"跌破 50000 做空"、"回踩 63000 接多"、"现价直接进"）
2. **未表示已进场**（没有"我已经"、"已开"、"已进"等表述）
3. **无具体盈亏数字**
4. **无持仓截图**

### 「纯行情分析」(analysis) 的定义
只讲方向判断、行情分析，但**无明确入场条件、无止损止盈、未说要进场**的，标记为 "analysis"。

## 输出格式
严格输出 JSON，不要任何解释文字，不要 markdown 代码块。字段如下：

{
  "post_type": "prediction | after_action | analysis",
  "direction": "long | short | neutral",
  "symbol": "BTC",
  "entry_condition": "突破 66600 做多",
  "entry_price": 66600.0,
  "stop_loss": 65000.0,
  "take_profit": 70000.0,
  "leverage": 5.0,
  "already_entered": false,
  "has_pnl_number": false,
  "confidence": 0.95
}

字段说明：
- post_type: 帖子类型，三选一
- direction: 方向，long=做多，short=做空，neutral=中性/无明确方向
- symbol: 标的币种符号，大写，如 BTC、ETH、SOL。无则为 null
- entry_condition: 入场条件文本描述，无则为 null
- entry_price: 明确给出的入场价格数值，无则为 null
- stop_loss: 止损价格，无则为 null
- take_profit: 止盈价格，无则为 null
- leverage: 杠杆倍数（数字），无则为 null
- already_entered: 博主是否已经进场持仓（布尔值）
- has_pnl_number: 帖子中是否出现具体盈亏数字（布尔值）
- confidence: 你对本次分类的置信度，0~1 之间的小数

## 注意事项
- 如果帖子提到多个币种，取最主要的那个
- 价格数字要提取为数值类型，不要带货币符号
- 如果某个字段无法确定，设为 null（布尔字段除外）
- 中文帖子用中文理解，英文帖子用英文理解
- 宁可保守标记为 analysis，也不要误判为 prediction
- already_entered 是最高优先级，只要博主说自己已经进场了，不管其他条件如何，都是 after_action
"""


def classify_post(content_text: str, image_count: int = 0) -> dict[str, Any] | None:
    """
    对单条帖子进行 AI 分类和结构化提取。

    Args:
        content_text: 帖子正文文本
        image_count: 图片数量（辅助判断是否有持仓截图）

    Returns:
        结构化信号字典，失败返回 None
    """
    llm = _get_llm()
    if llm is None:
        print("[KOL][classifier] LLM 未配置，跳过 AI 分类")
        return None

    user_prompt = f"""请分析以下 KOL 帖子：

---
帖子正文：
{content_text}

图片数量：{image_count} 张
---

请按照系统提示的规则，输出 JSON 格式的分析结果。"""

    try:
        raw = llm.chat(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.1,
            max_tokens=1024,
        )
    except Exception as e:
        print(f"[KOL][classifier] LLM 调用失败: {e}")
        return None

    if not raw:
        return None

    result = extract_json_from_llm_response(raw)
    if not isinstance(result, dict):
        print(f"[KOL][classifier] LLM 返回不是 JSON 对象: {raw[:200]}")
        return None

    # 字段校验与规范化
    normalized = _normalize_result(result)
    return normalized


def _normalize_result(data: dict) -> dict:
    """规范化 AI 返回的字段，确保类型正确。"""
    post_type = str(data.get("post_type", "analysis")).lower().strip()
    if post_type not in ("prediction", "after_action", "analysis"):
        post_type = "analysis"

    direction = data.get("direction")
    if direction:
        direction = str(direction).lower().strip()
        if direction not in ("long", "short", "neutral"):
            direction = None

    symbol = data.get("symbol")
    if symbol:
        symbol = str(symbol).upper().strip()
        # 过滤掉明显不是币种的
        if len(symbol) > 20 or not symbol.replace(" ", "").isalnum():
            symbol = None

    entry_condition = data.get("entry_condition")
    if entry_condition:
        entry_condition = str(entry_condition).strip() or None

    def _to_float(val):
        if val is None or val == "":
            return None
        try:
            s = str(val).strip().rstrip("xX")
            return float(s)
        except (ValueError, TypeError):
            return None

    entry_price = _to_float(data.get("entry_price"))
    stop_loss = _to_float(data.get("stop_loss"))
    take_profit = _to_float(data.get("take_profit"))
    leverage = _to_float(data.get("leverage"))

    already_entered = bool(data.get("already_entered", False))
    has_pnl_number = bool(data.get("has_pnl_number", False))

    confidence = data.get("confidence", 0.5)
    try:
        confidence = float(confidence)
        confidence = max(0.0, min(1.0, confidence))
    except (ValueError, TypeError):
        confidence = 0.5

    # 规则强制：already_entered 或 has_pnl_number → 无条件 after_action（最高优先级）
    if already_entered or has_pnl_number:
        post_type = "after_action"

    return {
        "post_type": post_type,
        "direction": direction,
        "symbol": symbol,
        "entry_condition": entry_condition,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "leverage": leverage,
        "already_entered": already_entered,
        "has_pnl_number": has_pnl_number,
        "confidence": confidence,
    }


def batch_classify(posts: list[dict]) -> list[dict | None]:
    """
    批量分类帖子。

    Args:
        posts: 帖子字典列表，需包含 content_text 和 image_urls 字段

    Returns:
        与输入等长的结果列表，失败项为 None
    """
    results = []
    for post in posts:
        content = post.get("content_text", "")
        img_count = len(post.get("image_urls", []))
        result = classify_post(content, img_count)
        results.append(result)
    return results
