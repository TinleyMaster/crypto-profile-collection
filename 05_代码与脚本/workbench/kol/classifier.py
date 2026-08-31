"""
KOL 帖子 AI 信号分类与结构化提取。

调用系统已有的 llm_client，对帖子内容进行分析，输出结构化信号字段。

核心判断流程（先粗分再细分，避免保守偏见导致漏标）：
  1. 先判断是否为 noise（无交易相关内容 → 直接丢弃）
  2. 非 noise 则判断是否有明确交易信号
  3. 有信号 → 细分 prediction / after_action
  4. 无信号但有行情分析 → analysis
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
判断其类型并提取结构化交易信息。

## 分类总览（五选一）

| 类型 | 含义 | 跟单价值 |
|---|---|---|
| noise | 与交易/行情完全无关的内容（生活、营销、广告、闲聊、转发等） | 无 |
| onchain | 链上数据/资金流监控（鲸鱼转账、交易所流入流出、爆仓清算、吸筹派发、聪明钱异动），客观事实情报，无主观操作建议 | 中（情报/归因） |
| analysis | 纯行情分析/观点分享，无明确操作建议 | 低（参考） |
| prediction | 前瞻喊单：给出明确入场条件，博主尚未进场 | 高（可跟单） |
| after_action | 事后晒单：已进场或已有盈亏结果 | 中（验证用） |

## 判断优先级（从高到低）

### 第 1 优先级：noise（无交易信号，直接排除）
满足以下任一条件即为 noise：
- 内容与加密货币/交易/行情完全无关（生活日常、美食、旅游、情感、政治等）
- 纯营销广告（项目推广、空投、拉群、付费社群、带单广告等）
- 纯闲聊/吐槽/灌水，无实质观点
- 纯转发/搬运，无自己的分析
- 只有图片/表情，无有效文字内容

### 第 1.5 优先级：onchain（链上情报）
满足以下任一特征即为 onchain（signal_category="onchain"）：
- 出现链上主体词："某地址"、"某大户"、"某鲸鱼"、"巨鲸"、"聪明钱"、"0x..."、"机构地址"、"基金地址"
- 出现链上动作词："充值"、"提现"、"转入"、"转出"、"转账"、"流入"、"流出"、"清算"、"爆仓"、"加仓"、"减仓"、"建仓"、"出货"、"吸筹"、"派发"、"解锁"
- 出现交易所名 + 资金方向："转入 Binance"、"从 OKX 提走"、"充值至 Coinbase"
- 出现具体链上金额 + 币种："3.2 万枚 ETH"、"5000 万 USDT"、"1200 枚 BTC"
- 出现清算/爆仓描述："在 $2487 被清算"、"多头爆仓 X 万"、"空单被强平"

细分 signal_subtype：
- 大额转账且涉及交易所 ↔ exchange_flow（inflow/outflow 由方向判断）
- 纯地址间大额转移 ↔ whale_move
- 明确"爆仓/清算" ↔ liquidation
- "持续买入/建仓 N 天" ↔ accumulation
- "出货/减仓" ↔ distribution（event_direction=distributing）
- 已知机构/聪明钱地址异动 ↔ smart_money

注意：
- onchain 帖子若同时含主观操作建议（如"我也跟着买"），优先 onchain（情报为主），direction 仍提取但 signal_category 不变
- onchain 帖子 direction 通常留 null，改用 event_direction 描述事实方向

### 第 2 优先级：after_action（事后晒单）
满足以下任一条件即为 after_action：
1. **博主明确表示已经进场/持仓/开仓**：出现"我已经进场了"、"我已开多"、"我已建仓"、"我拿着"、"我在持有"、"我已经上车"、"已入场"、"已布局"、"上车了"、"进场了"等表述
2. **出现具体盈亏数字**：如"+2341 USDT"、"赚了 5000u"、"浮盈 30%"、"亏损 2000刀"、"盈利 $1500"、"止损了"、"止盈了"等
3. **晒持仓截图/交割单**：帖子中提到"截图"、"持仓图"、"实盘"、"交割单"、"收益图"，或上下文明显在晒收益
4. **回顾性语言 + 交易结果**：出现"我说过"、"我就知道"、"果然"、"已经突破"、"如我所料"、"之前说的"、"应验了"等，且讨论的是已发生的交易结果

### 第 3 优先级：prediction（前瞻喊单）
**只要有明确的入场/操作建议，且未说已进场，就是 prediction**。不要过度保守。

典型特征（满足 2 条以上即可）：
1. **给出明确入场条件**：
   - 突破型："突破 66600 做多"、"站稳 50000 上方进多"
   - 回踩型："回踩 63000 接多"、"跌到 48000 抄底"
   - 现价型："现价直接进"、"当前价位开多"、"这里可以空"
   - 区间型："62000-63000 区间布局多单"
2. **有止损/止盈位**（即使没说入场价，有明确的止损止盈也算）
3. **有明确的方向 + 操作建议**："建议做多"、"可以空了"、"逢低买入"、"逢高做空"
4. **博主未表示已进场**（没有"我已经"、"已开"、"已进"、"我在"等表述）

**重要**：
- 有入场价 + 止损 + 止盈的 = prediction（这是最标准的喊单格式）
- 只说"现价做多"但没给止损的 = prediction（不完整喊单，但仍是喊单）
- 说"等突破再进" = prediction（有条件的前瞻喊单）
- 有压力位/支撑位 + 操作建议 = prediction

### 第 4 优先级：analysis（纯分析）
只讲方向判断、行情分析、宏观观点，但**无明确入场条件、无止损止盈、未给出操作建议**的，标记为 analysis。

典型特征：
- "BTC 看涨"、"ETH 可能下跌"（只有方向判断，无操作建议）
- "现在是牛市"、"熊市来了"（宏观判断）
- 技术分析：画趋势线、讲指标、分析形态，但没说"怎么做"
- 基本面分析：讲项目、讲叙事、讲逻辑，但没给交易建议

## 关键字段提取

除了分类，还要尽可能提取以下结构化字段：

- **direction**: long / short / neutral — 整体方向判断
- **symbol**: 标的币种符号，大写，如 BTC、ETH、SOL。无则为 null
- **entry_condition**: 入场条件文本描述（如"突破 66600 做多"），无则为 null
- **entry_price**: 明确给出的入场价格数值，无则为 null
- **stop_loss**: 止损价格，无则为 null
- **take_profit**: 止盈价格，无则为 null
- **leverage**: 杠杆倍数（数字），无则为 null
- **support_level**: 支撑位价格（帖子中提到的关键支撑），无则为 null
- **resistance_level**: 压力位价格（帖子中提到的关键压力/阻力），无则为 null
- **already_entered**: 博主是否已经进场持仓（布尔值）
- **has_pnl_number**: 帖子中是否出现具体盈亏数字（布尔值）
- **confidence**: 你对本次分类的置信度，0~1 之间的小数

## 输出格式

严格输出 JSON，不要任何解释文字，不要 markdown 代码块。字段如下：

{
  "post_type": "noise | onchain | prediction | after_action | analysis",
  "direction": "long | short | neutral",
  "symbol": "BTC",
  "entry_condition": "突破 66600 做多",
  "entry_price": 66600.0,
  "stop_loss": 65000.0,
  "take_profit": 70000.0,
  "leverage": 5.0,
  "support_level": 62000.0,
  "resistance_level": 68000.0,
  "already_entered": false,
  "has_pnl_number": false,
  "confidence": 0.95,
  "signal_category": "trading",
  "signal_subtype": null,
  "event_direction": null,
  "from_address": null,
  "to_address": null,
  "event_amount": null,
  "event_token": null,
  "event_usd_value": null,
  "tx_hash": null,
  "event_exchange": null,
  "address_label": null,
  "event_time": null
}

## 链上字段说明（仅 onchain 类型填写）
- signal_category: "onchain"（链上情报类）
- signal_subtype: whale_move / exchange_flow / liquidation / accumulation / distribution / smart_money
- event_direction: inflow / outflow / liquidated_long / liquidated_short / accumulating / distributing
- from_address / to_address: 转账地址或交易所名
- event_amount: 转账数量（数值）
- event_token: 币种符号（大写）
- event_usd_value: 折算美元金额（数值）
- tx_hash: 交易哈希（如有）
- event_exchange: 涉及交易所（Binance/Coinbase/OKX 等）
- address_label: 地址标签（Jump/Wintermute/某巨鲸/未知）
- event_time: 链上实际发生时间（ISO 格式，如有）

## 注意事项
- 如果帖子提到多个币种，取最主要的那个
- 价格数字要提取为数值类型，不要带货币符号
- 如果某个字段无法确定，设为 null（布尔字段除外）
- 中文帖子用中文理解，英文帖子用英文理解
- **不要过度保守**：有明确交易建议的就标 prediction，不要因为"不够完整"就降级为 analysis
- **already_entered 是 after_action 的最高优先级触发条件**，只要博主说自己已经进场了，就是 after_action
- **noise 类型的帖子其他字段可以全为 null**，只要 post_type 正确即可
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
    if post_type not in ("prediction", "after_action", "analysis", "noise"):
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
    support_level = _to_float(data.get("support_level"))
    resistance_level = _to_float(data.get("resistance_level"))

    already_entered = bool(data.get("already_entered", False))
    has_pnl_number = bool(data.get("has_pnl_number", False))

    confidence = data.get("confidence", 0.5)
    try:
        confidence = float(confidence)
        confidence = max(0.0, min(1.0, confidence))
    except (ValueError, TypeError):
        confidence = 0.5

    # 规则强制：already_entered 或 has_pnl_number → 无条件 after_action（最高优先级）
    # 但 noise 优先级更高（完全无关的内容不应该被标记为 after_action）
    if post_type != "noise" and (already_entered or has_pnl_number):
        post_type = "after_action"

    # ── 链上信号维度（onchain 专用）──
    signal_category = str(data.get("signal_category", "trading")).lower().strip()
    if signal_category not in ("trading", "onchain", "news"):
        signal_category = "trading"
    # post_type=onchain 时 signal_category 强制 onchain
    if post_type == "onchain":
        signal_category = "onchain"

    signal_subtype = data.get("signal_subtype")
    if signal_subtype:
        signal_subtype = str(signal_subtype).strip() or None

    event_direction = data.get("event_direction")
    if event_direction:
        event_direction = str(event_direction).lower().strip()
        if event_direction not in ("inflow", "outflow", "liquidated_long",
                                   "liquidated_short", "accumulating", "distributing"):
            event_direction = None

    # onchain 类若无主观 direction，强制留 null
    if signal_category == "onchain" and not data.get("direction"):
        direction = None

    from_address = data.get("from_address") or None
    to_address = data.get("to_address") or None
    event_amount = _to_float(data.get("event_amount"))
    event_token = data.get("event_token")
    if event_token:
        event_token = str(event_token).upper().strip() or None
    event_usd_value = _to_float(data.get("event_usd_value"))
    tx_hash = data.get("tx_hash") or None
    event_exchange = data.get("event_exchange") or None
    address_label = data.get("address_label") or None
    event_time = data.get("event_time") or None

    return {
        "post_type": post_type,
        "direction": direction,
        "symbol": symbol,
        "entry_condition": entry_condition,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "leverage": leverage,
        "support_level": support_level,
        "resistance_level": resistance_level,
        "already_entered": already_entered,
        "has_pnl_number": has_pnl_number,
        "confidence": confidence,
        "signal_category": signal_category,
        "signal_subtype": signal_subtype,
        "event_direction": event_direction,
        "from_address": from_address,
        "to_address": to_address,
        "event_amount": event_amount,
        "event_token": event_token,
        "event_usd_value": event_usd_value,
        "tx_hash": tx_hash,
        "event_exchange": event_exchange,
        "address_label": address_label,
        "event_time": event_time,
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
