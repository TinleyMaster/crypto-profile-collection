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
    "5. 时效性：考虑催化剂阶段（预期阶段/落地阶段/兑现后）和剩余有效期\n"
    "6. 资产校验：第一步必须校验代币信息与催化剂内容是否匹配。\n"
    "   如果代币名称/描述/赛道与催化剂中的项目明显不符（如同名不同币、ticker冲突），\n"
    "   必须将 asset_match_confidence 设为 low，并在 overall_review 中明确指出，\n"
    "   同时 verdict 应降级为「不建议参与」或「建议观望」。\n"
    "7. 盘面异动：结合 24h 成交量与过去 7 日均量的比值、24h/7d 涨跌幅判断市场是否已提前反应。\n"
    "   - 若量比 >= 2 且伴随大幅涨跌，说明资金已在异动，需警惕追高/接盘风险；\n"
    "   - 若量比 < 1 且价格平稳，说明市场尚未充分关注，可能存在预期差；\n"
    "   - 需将盘面异动情况纳入开仓时机与仓位决策的重要参考。\n\n"
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
- 代币简介：{asset_description}

## 二、催化剂信息
- 催化剂标题（{title_language}）：{catalyst_title_cn}
- 催化剂类型：{catalyst_kind}
- 事件类型：{event_type}
- 信息来源：{source_code}
- 发布时间：{published_at}
- 催化剂摘要（{summary_language}）：
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
- 流动性（24h 总流动性）：{liquidity_score}
- 风险等级：{risk_level}

## 六、盘面异动
- 24h 成交量：{volume_24h_usd} 美元
- 7 日均量：{avg_volume_7d} 美元
- 量比（24h / 7日均量）：{volume_ratio_7d}
- 7 日涨跌幅：{change_7d}%
- 异动判定：{anomaly_summary}

## 七、输出 JSON 格式
{{
  "asset_match_confidence": "high / medium / low（代币与催化剂的匹配置信度，ticker同名但项目不同为 low）",
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
  "overall_review": "综合评审总结（5-8句话，涵盖机会与风险的完整判断；如果资产不匹配必须明确指出）"
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
                "asset_match_confidence": str(data.get("asset_match_confidence") or "medium")[:16],
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

    # 风险等级（asset_risk_labels.risk_label 存的就是 high/medium/low）
    raw_risk = d.get("risk_level") or d.get("risk_label")
    # 如果是 risk_labels 数组格式，从里面提取
    if not raw_risk:
        rlabels = d.get("risk_labels")
        if isinstance(rlabels, list) and rlabels:
            rl = rlabels[0]
            if isinstance(rl, dict):
                raw_risk = rl.get("level") or rl.get("label")
    risk_level = _risk_level_cn(raw_risk) if raw_risk else "未知"

    # 流动性格式化（total_liquidity_usd 是美元金额）
    liq = d.get("liquidity_score") or d.get("total_liquidity_usd")
    if liq is None or liq == "":
        liquidity_text = "未知"
    else:
        try:
            liquidity_text = _fmt_mcap(float(liq))
        except (TypeError, ValueError):
            liquidity_text = str(liq)

    # 代币简介
    desc = d.get("description_short") or d.get("description") or d.get("asset_description")
    if not desc:
        desc = "暂无简介"
    else:
        desc = str(desc)[:300]

    # 判断标题/摘要语言
    title_val = d.get("title_cn") or ""
    title_lang = "中文" if title_val.strip() else "英文"
    summary_val = d.get("catalyst_summary") or d.get("ai_summary") or ""
    # 简单判断：如果含有中文字符就是中文，否则英文
    def _has_cn(text):
        import re
        return bool(re.search(r'[\u4e00-\u9fff]', text or ''))
    summary_lang = "中文" if _has_cn(str(summary_val)) else "英文"
    if not summary_val:
        summary_lang = "无"

    # 赛道 / 分类
    categories = d.get("categories")
    if isinstance(categories, list):
        cat_text = "、".join(categories[:8])
    elif categories:
        cat_text = str(categories)
    else:
        cat_text = "未知"

    # ---- 盘面异动 ----
    vol_24h = d.get("volume_24h_usd")
    avg_vol_7d = d.get("avg_volume_7d")
    vol_ratio = d.get("volume_ratio_7d")
    change_7d_val = d.get("change_7d_pct")
    change_24h_val = d.get("change_24h_pct")

    # 24h 成交量格式化
    if vol_24h is None or vol_24h == "":
        volume_24h_text = "未知"
    else:
        try:
            volume_24h_text = _fmt_mcap(float(vol_24h))
        except (TypeError, ValueError):
            volume_24h_text = str(vol_24h)

    # 7 日均量格式化
    if avg_vol_7d is None or avg_vol_7d == "":
        avg_volume_7d_text = "未知"
    else:
        try:
            avg_volume_7d_text = _fmt_mcap(float(avg_vol_7d))
        except (TypeError, ValueError):
            avg_volume_7d_text = str(avg_vol_7d)

    # 量比
    if vol_ratio is None or vol_ratio == "":
        vol_ratio_text = "未知"
    else:
        try:
            vol_ratio_text = f"{float(vol_ratio):.2f}x"
        except (TypeError, ValueError):
            vol_ratio_text = str(vol_ratio)

    # 7 日涨跌幅
    change_7d_text = _pct(change_7d_val)

    # 异动判定（综合量价）
    anomaly_parts = []
    try:
        vr = float(vol_ratio) if vol_ratio is not None and vol_ratio != "" else None
        c24 = float(change_24h_val) if change_24h_val is not None and change_24h_val != "" else None
        c7 = float(change_7d_val) if change_7d_val is not None and change_7d_val != "" else None

        if vr is not None:
            if vr >= 3.0:
                anomaly_parts.append(f"显著放量（量比 {vr:.1f}x，远超近期均值）")
            elif vr >= 2.0:
                anomaly_parts.append(f"放量（量比 {vr:.1f}x，高于近期均值）")
            elif vr >= 1.5:
                anomaly_parts.append(f"温和放量（量比 {vr:.1f}x）")
            elif vr <= 0.5:
                anomaly_parts.append(f"极度缩量（量比 {vr:.1f}x，远低于均值）")
            elif vr <= 0.7:
                anomaly_parts.append(f"缩量（量比 {vr:.1f}x）")
            else:
                anomaly_parts.append(f"量能正常（量比 {vr:.1f}x）")

        if c24 is not None:
            if abs(c24) >= 20:
                anomaly_parts.append(f"24h 剧烈波动（{c24:+.1f}%）")
            elif abs(c24) >= 10:
                anomaly_parts.append(f"24h 大幅{'上涨' if c24 > 0 else '下跌'}（{c24:+.1f}%）")
            elif abs(c24) >= 5:
                anomaly_parts.append(f"24h 明显{'上涨' if c24 > 0 else '下跌'}（{c24:+.1f}%）")

        if c7 is not None:
            if abs(c7) >= 30:
                anomaly_parts.append(f"7d 剧烈{'上涨' if c7 > 0 else '下跌'}（{c7:+.1f}%）")
            elif abs(c7) >= 15:
                anomaly_parts.append(f"7d 大幅{'上涨' if c7 > 0 else '下跌'}（{c7:+.1f}%）")
    except (TypeError, ValueError):
        pass

    anomaly_summary = "；".join(anomaly_parts) if anomaly_parts else "数据不足，无法判定"

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
        asset_description=desc,
        title_language=title_lang,
        summary_language=summary_lang,
        catalyst_title_cn=title_val or d.get("catalyst_title") or "(无标题)",
        catalyst_kind=_kind_cn(d.get("kind")),
        event_type=_event_type_cn(d.get("event_type") or d.get("ai_event_type") or "other"),
        source_code=d.get("source_code", "未知"),
        published_at=str(d.get("published_at") or "未知"),
        catalyst_summary_cn=(str(summary_val) or "无摘要")[:800],
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
        liquidity_score=liquidity_text,
        risk_level=risk_level,
        # 盘面异动
        volume_24h_usd=volume_24h_text,
        avg_volume_7d=avg_volume_7d_text,
        volume_ratio_7d=vol_ratio_text,
        change_7d=change_7d_text,
        anomaly_summary=anomaly_summary,
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
