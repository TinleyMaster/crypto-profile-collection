"""
催化剂 AI 增强模块（快通道 A 级信号专用）。

两大职责：
1. 催化剂内容中文化：将英文标题/摘要翻译为中文，写入 title_cn / ai_summary
2. A 级信号深度评审：汇总全维度数据，调用 LLM 做深度投资分析，写入 ai_deep_review

设计原则：
- 失败静默降级：LLM 不可用/超时不影响主流程，返回 None
- 结果持久化：翻译和评审结果都写入 DB，避免重复调用
- 快通道优先：A 级信号即时评审，B/C 级留到慢通道批量处理
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


# =====================================================================
# 系统提示词：催化剂翻译 + 结构化摘要
# =====================================================================

_TRANSLATE_SYSTEM_PROMPT = (
    "你是加密货币行业专业翻译。将给定的英文催化剂标题和正文翻译成地道中文。\n"
    "要求：\n"
    "1. 标题简洁有力，不超过 80 字，保留关键信息（代币名、事件类型、核心动作）\n"
    "2. 摘要控制在 300 字以内，准确传达催化剂核心内容，不添加臆测\n"
    "3. 专业术语准确：spot trading=现货交易、futures=合约、listing=上线、delisting=下架、"
    "burn=销毁、airdrop=空投、staking=质押、governance=治理、partnership=合作、"
    "upgrade=升级、regulation=监管、security=安全事件、tokenomics=代币经济\n"
    "4. 保持客观中立，不加入投资建议\n"
    "5. 只输出 JSON，不要其他内容"
)

_TRANSLATE_USER_TEMPLATE = """请翻译以下催化剂内容。

## 标题（英文）
{title}

## 正文摘要（英文）
{body}

## 输出 JSON 格式
{{
  "title_cn": "中文标题（不超过80字）",
  "summary_cn": "中文摘要（300字以内）",
  "event_type": "事件类型英文枚举（listing/delisting/partnership/funding/upgrade/regulation/burn/airdrop/staking/governance/security/market_update/other）"
}}
"""


# =====================================================================
# 系统提示词：A 级信号深度评审
# =====================================================================

_DEEP_REVIEW_SYSTEM_PROMPT = (
    "你是一位经验丰富的加密货币交易员，擅长催化剂驱动的短线交易决策。\n"
    "你的任务是：给定一个 A 级催化剂信号的全维度数据，进行深度投资评审，\n"
    "输出结构化的分析报告，帮助交易者快速判断是否开单、如何操作。\n\n"
    "分析原则：\n"
    "1. 数据驱动：所有判断必须基于提供的数据，禁止虚构\n"
    "2. 客观中立：既说机会也说风险，不偏不倚\n"
    "3. 可操作性：给出明确的操作建议（开/不开/观察）和仓位建议\n"
    "4. 风险优先：先评估下行风险，再看上行空间\n"
    "5. 时效性：考虑催化剂阶段（预期阶段/落地阶段/兑现后）和剩余有效期\n\n"
    "只输出 JSON，不要其他内容。"
)

_DEEP_REVIEW_USER_TEMPLATE = """请对以下 A 级催化剂信号进行深度投资评审。

## 一、代币基本信息
- 代币：{symbol} / {asset_name}
- 资产类型：{asset_type}
- 赛道：{primary_sector}
- 分类标签：{categories}
- 市值：{market_cap} 美元（排名 #{market_cap_rank}）
- 当前价格：{current_price} 美元
- 24h 涨跌幅：{change_24h}%
- ATH：{ath_usd} 美元（距ATH {ath_distance}%）
- 流通量：{circulating_supply} / 总量 {total_supply}（流通率 {circulating_ratio}%）
- 上线时间：{launch_date}

## 二、催化剂信息
- 催化剂标题（中文）：{catalyst_title_cn}
- 催化剂类型：{catalyst_kind}
- 事件类型：{event_type}
- 信息来源：{source_code}
- 发布时间：{published_at}
- 催化剂摘要（中文）：
{catalyst_summary_cn}

## 三、信号评分
- 综合评分：{composite_score}/100（等级 {tier}）
- 基础强度（催化权重）：{base_strength}/100
- 共振得分：{resonance_score}/100（状态：{resonance_state}）
- 市场环境：{regime}
- 技术面状态：{technical_state}
- 持续性：{persistence}
- 置信度：{confidence}

## 四、交易计划（规则计算）
- 方向：{direction}
- 入场价：{entry_price}
- 目标价：{take_profit}
- 止损价：{stop_loss}
- 盈亏比：{rr_ratio}
- 失效条件：{invalidation}

## 五、风险与流动性
- 流动性评分：{liquidity_score}
- 风险标签：
{risk_labels}

## 六、输出 JSON 格式
{{
  "verdict": "强烈推荐开仓 / 建议轻仓参与 / 建议观望 / 不建议参与",
  "confidence_level": "极高 / 高 / 中 / 低",
  "position_suggestion": "建议仓位比例，如 30% 仓位或 半仓",
  "core_logic": "核心驱动逻辑（2-3句话，为什么这个催化剂值得参与）",
  "key_risks": [
    "风险1（具体描述）",
    "风险2（具体描述）",
    "风险3（具体描述）"
  ],
  "catalyst_stage": "预期阶段 / 发酵阶段 / 落地前夕 / 刚落地 / 兑现后",
  "timing_advice": "进场时机建议（立即挂单/等回踩/等突破/观望）",
  "stop_loss_advice": "止损建议（是否认同规则止损价，或给出调整建议）",
  "take_profit_advice": "止盈建议（是否认同规则目标价，建议分批止盈点位）",
  "alternative_scenarios": "替代情景（如果催化剂不达预期/超预期，如何应对）",
  "overall_review": "综合评审总结（5-8句话，涵盖机会与风险的完整判断）"
}}
"""


# =====================================================================
# 翻译器
# =====================================================================

class CatalystTranslator:
    """催化剂中英文翻译器。"""

    def __init__(self, llm_client=None):
        self._llm = llm_client

    @classmethod
    def from_settings(cls):
        """从配置构建，失败返回 None。"""
        try:
            from crypto_research.config import get_settings
            from crypto_research.clients.llm_client import LLMClient
            settings = get_settings(require_database=False)
            llm = LLMClient(settings, rpm=20, timeout=60)
            if not llm.is_available():
                return None
            return cls(llm)
        except Exception as e:
            logger.warning("构建翻译器失败: %s", e)
            return None

    def translate(self, title: str, body: str = "") -> Optional[dict]:
        """翻译标题和正文摘要。

        Returns:
            {"title_cn": str, "summary_cn": str, "event_type": str} 或 None
        """
        if not self._llm:
            return None
        if not title and not body:
            return None

        user_prompt = _TRANSLATE_USER_TEMPLATE.format(
            title=title or "(无标题)",
            body=(body or "")[:800],
        )

        try:
            raw = self._llm.chat(
                _TRANSLATE_SYSTEM_PROMPT,
                user_prompt,
                temperature=0.1,
                max_tokens=800,
                response_format={"type": "json_object"},
                use_cache=True,
            )
            data = _extract_json(raw)
            if not data:
                return None
            return {
                "title_cn": str(data.get("title_cn") or "")[:256] or None,
                "summary_cn": str(data.get("summary_cn") or "")[:1500] or None,
                "event_type": str(data.get("event_type") or "other")[:32],
            }
        except Exception as e:
            logger.warning("催化剂翻译失败: %s", e, exc_info=True)
            return None


# =====================================================================
# A 级信号深度评审器
# =====================================================================

class AISignalDeepReviewer:
    """A 级信号 AI 深度评审器。

    收集全维度数据（基本面 + 催化剂 + 技术面 + 风险 + 交易计划），
    调用 LLM 生成结构化深度评审报告。
    """

    def __init__(self, llm_client=None):
        self._llm = llm_client

    @classmethod
    def from_settings(cls):
        """从配置构建，失败返回 None。"""
        try:
            from crypto_research.config import get_settings
            from crypto_research.clients.llm_client import LLMClient
            settings = get_settings(require_database=False)
            llm = LLMClient(settings, rpm=10, timeout=120)
            if not llm.is_available():
                return None
            return cls(llm)
        except Exception as e:
            logger.warning("构建深度评审器失败: %s", e)
            return None

    def review(self, signal_data: dict) -> Optional[dict]:
        """对一条 A 级信号做深度评审。

        Args:
            signal_data: 包含全维度数据的 dict（来自 notifier 的 SQL 查询）

        Returns:
            dict 结构化评审结果，或 None（失败/不可用）
        """
        if not self._llm:
            return None

        user_prompt = _build_deep_review_prompt(signal_data)

        try:
            raw = self._llm.chat(
                _DEEP_REVIEW_SYSTEM_PROMPT,
                user_prompt,
                temperature=0.3,
                max_tokens=2048,
                response_format={"type": "json_object"},
                use_cache=False,  # 深度评审不缓存，每次重新评估
            )
            data = _extract_json(raw)
            if not data:
                return None

            # 标准化输出
            result = {
                "verdict": str(data.get("verdict") or "")[:64],
                "confidence_level": str(data.get("confidence_level") or "")[:16],
                "position_suggestion": str(data.get("position_suggestion") or "")[:128],
                "core_logic": str(data.get("core_logic") or "")[:500],
                "key_risks": [str(x)[:200] for x in (data.get("key_risks") or [])][:5],
                "catalyst_stage": str(data.get("catalyst_stage") or "")[:32],
                "timing_advice": str(data.get("timing_advice") or "")[:300],
                "stop_loss_advice": str(data.get("stop_loss_advice") or "")[:300],
                "take_profit_advice": str(data.get("take_profit_advice") or "")[:300],
                "alternative_scenarios": str(data.get("alternative_scenarios") or "")[:500],
                "overall_review": str(data.get("overall_review") or "")[:1500],
            }
            # 至少要有 verdict 和 overall_review 才算有效
            if not result["verdict"] or not result["overall_review"]:
                return None
            return result
        except Exception as e:
            logger.warning("AI深度评审失败: %s", e, exc_info=True)
            return None


# =====================================================================
# 辅助函数
# =====================================================================

def _extract_json(raw: Any) -> Optional[dict]:
    """从 LLM 输出中提取 JSON dict。"""
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        from crypto_research.clients.llm_client import extract_json_from_llm_response
        data = extract_json_from_llm_response(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    try:
        data = json.loads(str(raw))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return None


def _build_deep_review_prompt(d: dict) -> str:
    """根据信号数据构建深度评审的 user prompt。"""
    # 数值格式化（None 兜底）
    def _n(v, unit="", default="未知"):
        if v is None:
            return default
        try:
            if isinstance(v, float):
                return f"{v:,.6f}{unit}" if v < 0.01 else f"{v:,.2f}{unit}"
            return f"{v:,}{unit}"
        except Exception:
            return str(v)

    def _pct(v, default="未知"):
        if v is None:
            return default
        try:
            return f"{float(v):+.2f}"
        except Exception:
            return default

    # 方向
    tp = d.get("take_profit")
    sl = d.get("stop_loss")
    if tp and sl and tp > sl:
        direction = "做多（看涨）"
    elif tp and sl and tp < sl:
        direction = "做空（看跌）"
    else:
        direction = "未确定"

    # ATH 距离
    ath = d.get("ath_usd")
    cur = d.get("current_price")
    ath_distance = ""
    if ath and cur and ath > 0:
        dist = (cur - ath) / ath * 100
        ath_distance = f"{dist:+.2f}"

    # 流通率
    circ = d.get("circulating_supply")
    total = d.get("total_supply")
    circulating_ratio = ""
    if circ and total and total > 0:
        circulating_ratio = f"{circ / total * 100:.1f}"

    # 风险标签格式化
    risk_labels = d.get("risk_labels") or []
    if isinstance(risk_labels, list):
        risk_text = "\n".join(
            f"  - [{_risk_level_cn(rl.get('level',''))}] {rl.get('label','')}"
            for rl in risk_labels
        ) or "  暂无风险标签"
    else:
        risk_text = "  暂无风险标签"

    # 赛道 / 分类
    categories = d.get("categories")
    if isinstance(categories, list):
        cat_text = "、".join(categories[:8])
    elif categories:
        cat_text = str(categories)
    else:
        cat_text = "未知"

    return _DEEP_REVIEW_USER_TEMPLATE.format(
        symbol=d.get("symbol", "?"),
        asset_name=d.get("canonical_name", d.get("asset_name", "?")),
        asset_type=_asset_type_cn(d.get("asset_type")),
        primary_sector=d.get("primary_sector") or "未知",
        categories=cat_text,
        market_cap=_fmt_mcap(d.get("market_cap")),
        market_cap_rank=d.get("market_cap_rank") or "N/A",
        current_price=_n(d.get("current_price")),
        change_24h=_pct(d.get("change_24h_pct")),
        ath_usd=_n(d.get("ath_usd")),
        ath_distance=ath_distance or "N/A",
        circulating_supply=_fmt_supply(d.get("circulating_supply")),
        total_supply=_fmt_supply(d.get("total_supply")),
        circulating_ratio=circulating_ratio or "N/A",
        launch_date=str(d.get("launch_date") or "未知"),
        catalyst_title_cn=d.get("title_cn") or d.get("catalyst_title") or "(无标题)",
        catalyst_kind=_kind_cn(d.get("kind")),
        event_type=_event_type_cn(d.get("event_type") or d.get("ai_event_type") or "other"),
        source_code=d.get("source_code", "未知"),
        published_at=str(d.get("published_at") or "未知"),
        catalyst_summary_cn=(d.get("catalyst_summary") or d.get("ai_summary") or "无摘要")[:800],
        composite_score=d.get("composite_score", 0),
        tier=d.get("tier", "?"),
        base_strength=d.get("base_strength", 0),
        resonance_score=d.get("resonance_score", 0),
        resonance_state=_resonance_cn(d.get("resonance_state")),
        regime=_regime_cn(d.get("regime")),
        technical_state=_tech_cn(d.get("technical_state")),
        persistence=_persist_cn(d.get("persistence")),
        confidence=_fmt_confidence(d.get("confidence")),
        direction=direction,
        entry_price=_n(d.get("entry_price")),
        take_profit=_n(d.get("take_profit")),
        stop_loss=_n(d.get("stop_loss")),
        rr_ratio=d.get("rr_ratio") or "未知",
        invalidation=d.get("invalidation") or "未设置",
        liquidity_score=d.get("liquidity_score") or "未知",
        risk_labels=risk_text,
    )


# ---- 翻译字典（枚举值 → 中文）----

def _risk_level_cn(level: str | None) -> str:
    return {
        "critical": "严重",
        "high": "高",
        "medium": "中",
        "low": "低",
    }.get((level or "").lower(), level or "未知")


def _asset_type_cn(t: str | None) -> str:
    return {
        "coin": "公链币",
        "token": "代币",
        "stablecoin": "稳定币",
        "stock": "股票",
        "commodity": "商品",
        "index": "指数",
    }.get((t or "").lower(), t or "未知")


def _kind_cn(k: str | None) -> str:
    return {
        "structural": "结构性催化",
        "event": "事件型催化",
        "sentiment": "情绪型催化",
        "noise": "噪声",
    }.get((k or "").lower(), k or "未知")


def _event_type_cn(e: str | None) -> str:
    return {
        "listing": "上线交易所",
        "delisting": "下架",
        "partnership": "合作/集成",
        "funding": "融资",
        "upgrade": "技术升级",
        "regulation": "监管动态",
        "burn": "销毁",
        "airdrop": "空投",
        "staking": "质押/挖矿",
        "governance": "治理投票",
        "security": "安全事件",
        "market_update": "市场动态",
        "tokenomics": "代币经济调整",
        "other": "其他",
    }.get((e or "").lower(), e or "其他")


def _resonance_cn(s: str | None) -> str:
    return {
        "confirmed": "强共振",
        "weak": "弱共振",
        "divergent": "背离",
        "pending": "待确认",
    }.get((s or "").lower(), s or "未知")


def _regime_cn(r: str | None) -> str:
    return {
        "risk_on": "风险偏好（Risk On）",
        "neutral": "中性（Neutral）",
        "risk_off": "风险规避（Risk Off）",
    }.get((r or "").lower(), r or "中性")


def _tech_cn(t: str | None) -> str:
    return {
        "up": "上升趋势",
        "range": "震荡整理",
        "down": "下降趋势",
        "unknown": "未知",
    }.get((t or "").lower(), t or "未知")


def _persist_cn(p: str | None) -> str:
    return {
        "structural": "持续性（结构性，7天+）",
        "one_off": "一次性催化（3天内）",
        "decaying": "衰减型（1天内）",
    }.get((p or "").lower(), p or "未知")


def _fmt_confidence(c) -> str:
    if c is None:
        return "未知"
    try:
        pct = float(c) * 100
        return f"{pct:.0f}%"
    except Exception:
        return str(c)


def _fmt_mcap(val) -> str:
    if val is None:
        return "未知"
    try:
        v = float(val)
    except (TypeError, ValueError):
        return str(val)
    if v >= 1e12:
        return f"${v/1e12:.2f}T"
    if v >= 1e9:
        return f"${v/1e9:.2f}B"
    if v >= 1e6:
        return f"${v/1e6:.2f}M"
    if v >= 1e3:
        return f"${v/1e3:.2f}K"
    return f"${v:,.2f}"


def _fmt_supply(val) -> str:
    if val is None:
        return "未知"
    try:
        v = float(val)
    except (TypeError, ValueError):
        return str(val)
    if v >= 1e12:
        return f"{v/1e12:.2f}T"
    if v >= 1e9:
        return f"{v/1e9:.2f}B"
    if v >= 1e6:
        return f"{v/1e6:.2f}M"
    return f"{v:,.0f}"
