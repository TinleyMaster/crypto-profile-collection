"""
AI 信号综合分析模块（FEAT-AI-HIGHLIGHT）。

核心思路：
1. 从各板块信号中收集出现机会的代币
2. 汇总该代币的全维度数据（市场/链上/解锁/社交/催化剂等）
3. 调用 LLM 综合判断，决定是否进入【高亮信号】或【高危信号】
4. 输出 AI 推荐原因，供前端点击查看

设计原则：
- 不替代现有规则筛选，而是作为增强层（rule-based 初筛 + AI 精选）
- 缓存 AI 分析结果，避免重复调用
- 失败时优雅降级（回退到原规则筛选结果）
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

# 确保 crypto_research 可导入
_SCRIPTS_SRC = str(Path(__file__).parent.parent / "scripts" / "src")
if _SCRIPTS_SRC not in sys.path:
    sys.path.insert(0, _SCRIPTS_SRC)

# ── 缓存 ──
_ai_analysis_cache: dict[str, dict[str, Any]] = {}  # cache_key -> {result, ts}
AI_CACHE_TTL = 1800  # 30 分钟缓存


def _get_cache_key(asset_id: int, signal_types: list[str]) -> str:
    return f"{asset_id}:{','.join(sorted(signal_types))}"


def analyze_asset_signals(
    asset_basic: dict,
    asset_signals: list[dict],
    asset_extra: dict | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """
    对单个代币的多条信号进行 AI 综合分析。

    Args:
        asset_basic: 代币基础信息 {asset_id, symbol, name, sector, price, market_cap, ...}
        asset_signals: 该代币在各板块触发的信号列表
        asset_extra: 额外维度数据（链上/解锁/社交/催化剂等）
        force_refresh: 是否强制刷新缓存

    Returns:
        {
            "should_highlight": bool,     # 是否进入高亮信号
            "should_risk": bool,          # 是否进入高危信号
            "direction": "long"|"short"|"neutral",  # AI 判断方向
            "ai_score": 0-100,            # AI 综合评分
            "confidence": "HIGH"|"MED"|"LOW",  # AI 置信度
            "reason_summary": str,        # 一句话理由（卡片显示）
            "reason_detail": str,         # 详细分析（弹窗显示）
            "key_factors": [str],         # 关键因子列表
            "risk_warnings": [str],       # 风险提示
            "suggested_horizon": "short"|"medium"|"long",  # 建议周期
            "analysis_ts": float,         # 分析时间戳
            "from_cache": bool,
            "error": str | None,          # 错误信息（调用失败时）
        }
    """
    asset_extra = asset_extra or {}
    asset_id = asset_basic.get("asset_id", 0)
    signal_types = sorted(set(s.get("signal_type", "") for s in asset_signals if s.get("signal_type")))
    cache_key = _get_cache_key(asset_id, signal_types)

    # 缓存命中
    if not force_refresh and cache_key in _ai_analysis_cache:
        cached = _ai_analysis_cache[cache_key]
        if time.time() - cached["ts"] < AI_CACHE_TTL:
            return {**cached["result"], "from_cache": True}

    # 尝试调用 LLM
    try:
        result = _call_llm_analysis(asset_basic, asset_signals, asset_extra)
        result["from_cache"] = False
        result["analysis_ts"] = time.time()
        # 写缓存（即使失败也缓存错误，避免短时间内重复失败）
        _ai_analysis_cache[cache_key] = {
            "result": result,
            "ts": time.time(),
        }
        # 限制缓存大小
        if len(_ai_analysis_cache) > 200:
            oldest_key = min(_ai_analysis_cache.keys(),
                             key=lambda k: _ai_analysis_cache[k]["ts"])
            del _ai_analysis_cache[oldest_key]
        return result
    except Exception as e:
        error_result = {
            "should_highlight": False,
            "should_risk": False,
            "direction": "neutral",
            "ai_score": 0,
            "confidence": "LOW",
            "reason_summary": f"AI分析失败: {str(e)[:50]}",
            "reason_detail": str(e),
            "key_factors": [],
            "risk_warnings": [],
            "suggested_horizon": "medium",
            "analysis_ts": time.time(),
            "from_cache": False,
            "error": str(e),
        }
        _ai_analysis_cache[cache_key] = {
            "result": error_result,
            "ts": time.time(),
        }
        return error_result


def _call_llm_analysis(
    asset_basic: dict,
    asset_signals: list[dict],
    asset_extra: dict,
) -> dict[str, Any]:
    """调用 LLM 进行综合分析。"""
    from crypto_research.config import get_settings
    from crypto_research.clients.llm_client import LLMClient, extract_json_from_llm_response

    settings = get_settings(require_database=False)
    llm = LLMClient(settings, rpm=30, timeout=60)

    if not llm.is_available():
        raise RuntimeError("LLM 不可用（未配置 API Key）")

    # 构建 prompt
    system_prompt = _build_system_prompt()
    user_prompt = _build_user_prompt(asset_basic, asset_signals, asset_extra)

    raw = llm.chat(
        system_prompt, user_prompt,
        temperature=0.2,
        max_tokens=4096,
        response_format={"type": "json_object"},
        use_cache=True,
    )

    data = extract_json_from_llm_response(raw)

    # 标准化输出
    return {
        "should_highlight": bool(data.get("should_highlight", False)),
        "should_risk": bool(data.get("should_risk", False)),
        "direction": str(data.get("direction", "neutral")).lower() or "neutral",
        "ai_score": min(100, max(0, int(float(data.get("ai_score", 0) or 0)))),
        "confidence": str(data.get("confidence", "MED")).upper() or "MED",
        "reason_summary": str(data.get("reason_summary", ""))[:200],
        "reason_detail": str(data.get("reason_detail", ""))[:2000],
        "key_factors": [str(x)[:100] for x in (data.get("key_factors") or [])][:8],
        "risk_warnings": [str(x)[:100] for x in (data.get("risk_warnings") or [])][:5],
        "suggested_horizon": str(data.get("suggested_horizon", "medium")).lower() or "medium",
        "error": None,
    }


def _build_system_prompt() -> str:
    return """你是一位经验丰富的加密货币投研分析师，擅长综合多维度数据判断代币的短期/中期投资机会与风险。

你的任务是：给定一个代币的基础信息、触发的各维度信号、以及链上/解锁/社交/催化剂等额外数据，
综合判断该代币是否应该进入【高亮信号】（看多机会）或【高危信号】（看空风险），并给出详细分析理由。

分析原则：
1. 多维度共振优先：多个不同维度同时发出同向信号的可信度远高于单一信号
2. 信号强度分级：HIGH 级信号权重远高于 MED/LOW 级
3. 估值过滤：估值极度高估时，即使有看多信号也需谨慎；极度低估时即使有看空信号也不必过度悲观
4. 时间维度：短期信号（链上异动、资金费率）适合短周期操作；长期信号（开发活跃、基本面变化）适合中长期布局
5. 风险收益比：不仅要看方向，还要评估潜在收益空间和风险敞口

判定标准：
- 高亮信号（should_highlight=true）：
  * 至少 2 个不同维度的看多信号共振，且整体偏多
  * AI 综合评分 >= 65 分
  * 或单一超强信号（如大额解锁前的极度恐慌砸盘 + 链上巨鲸抄底）
- 高危信号（should_risk=true）：
  * 至少 2 个不同维度的看空信号共振，或单一极端风险信号
  * AI 综合评分 <= 35 分（风险评分倒过来）
  * 或重大风险事件（如大额解锁、黑客攻击、监管利空）
- 两者可能同时为 true（如短期看空但长期看多，或高波动品种）
- 两者都为 false 表示信号不明确或强度不足

只输出 JSON，不要输出其他内容。JSON 格式：
{
  "should_highlight": true/false,
  "should_risk": true/false,
  "direction": "long|short|neutral",
  "ai_score": 0-100,
  "confidence": "HIGH|MED|LOW",
  "reason_summary": "一句话总结，80字以内，用于卡片展示",
  "reason_detail": "详细分析，500字以内，涵盖：1)多维度信号共振分析 2)关键驱动因子 3)风险点 4)操作建议",
  "key_factors": ["关键因子1", "关键因子2", ...],
  "risk_warnings": ["风险1", "风险2", ...],
  "suggested_horizon": "short|medium|long"
}"""


def _build_user_prompt(
    asset_basic: dict,
    asset_signals: list[dict],
    asset_extra: dict,
) -> str:
    """构建用户 prompt。"""
    # 基础信息
    basic_lines = [
        f"- 代币: {asset_basic.get('symbol', '?')} ({asset_basic.get('name', '?')})",
        f"- 赛道: {asset_basic.get('sector', '未知')}",
    ]
    if asset_basic.get("price"):
        basic_lines.append(f"- 当前价格: ${asset_basic['price']}")
    if asset_basic.get("market_cap"):
        mcap = float(asset_basic["market_cap"])
        if mcap >= 1e9:
            basic_lines.append(f"- 市值: ${mcap/1e9:.2f}B")
        elif mcap >= 1e6:
            basic_lines.append(f"- 市值: ${mcap/1e6:.1f}M")
    if asset_basic.get("fdv"):
        fdv = float(asset_basic["fdv"])
        if fdv >= 1e9:
            basic_lines.append(f"- FDV: ${fdv/1e9:.2f}B")
    if asset_basic.get("change_24h") is not None:
        basic_lines.append(f"- 24h涨跌: {float(asset_basic['change_24h']):+.2f}%")
    if asset_basic.get("change_7d") is not None:
        basic_lines.append(f"- 7d涨跌: {float(asset_basic['change_7d']):+.2f}%")

    # 触发的信号
    signal_lines = []
    for i, s in enumerate(asset_signals[:15], 1):  # 最多 15 条，避免过长
        stype = s.get("signal_type") or s.get("type") or "unknown"
        direction = s.get("direction", "")
        tier = s.get("conviction_tier") or s.get("confidence") or "MED"
        score = s.get("conviction_score") or s.get("score") or 0
        title = s.get("title") or s.get("trigger_logic") or s.get("reason", "")
        desc = str(title)[:120]
        signal_lines.append(
            f"  {i}. [{stype}] 方向={direction} 强度={tier}({score}分) - {desc}"
        )
    if len(asset_signals) > 15:
        signal_lines.append(f"  ... 还有 {len(asset_signals) - 15} 条信号")

    # 额外数据：链上
    extra_lines = []
    onchain = asset_extra.get("onchain") or {}
    if onchain:
        extra_lines.append("【链上数据】")
        if onchain.get("holder_concentration_top10") is not None:
            extra_lines.append(f"- 前10持仓集中度: {onchain['holder_concentration_top10']}%")
        if onchain.get("whale_flow_24h"):
            extra_lines.append(f"- 巨鲸24h流向: {onchain['whale_flow_24h']}")
        if onchain.get("exchange_flow_7d"):
            extra_lines.append(f"- 交易所7d净流入: {onchain['exchange_flow_7d']}")
        if onchain.get("active_addresses_change"):
            extra_lines.append(f"- 活跃地址变化: {onchain['active_addresses_change']}")

    # 额外数据：解锁
    unlocks = asset_extra.get("unlocks") or {}
    if unlocks:
        extra_lines.append("【解锁数据】")
        if unlocks.get("next_unlock_date"):
            extra_lines.append(f"- 下次解锁: {unlocks['next_unlock_date']}")
        if unlocks.get("next_unlock_pct") is not None:
            extra_lines.append(f"- 下次解锁占比: {unlocks['next_unlock_pct']}%")
        if unlocks.get("unlock_30d_pct") is not None:
            extra_lines.append(f"- 30天内解锁占比: {unlocks['unlock_30d_pct']}%")

    # 额外数据：社交热度
    social = asset_extra.get("social") or {}
    if social:
        extra_lines.append("【社交热度】")
        if social.get("twitter_followers"):
            extra_lines.append(f"- Twitter粉丝: {social['twitter_followers']}")
        if social.get("social_volume_24h"):
            extra_lines.append(f"- 24h社交量: {social['social_volume_24h']}")
        if social.get("sentiment"):
            extra_lines.append(f"- 情绪倾向: {social['sentiment']}")

    # 额外数据：催化剂/事件
    catalysts = asset_extra.get("catalysts") or []
    if catalysts:
        extra_lines.append("【近期催化剂/事件】")
        for i, c in enumerate(catalysts[:5], 1):
            date = c.get("event_date", "")
            title = c.get("title", "")
            impact = c.get("strength") or c.get("impact", "")
            extra_lines.append(f"  {i}. {date} [{impact}] {str(title)[:80]}")

    # 额外数据：估值
    valuation = asset_extra.get("valuation") or {}
    if valuation:
        extra_lines.append("【估值状态】")
        if valuation.get("mvrv_percentile") is not None:
            pct = float(valuation["mvrv_percentile"])
            zone = "极度高估" if pct > 85 else "高估" if pct > 70 else "中性" if pct > 30 else "低估" if pct > 15 else "极度低估"
            extra_lines.append(f"- MVRV百分位: {pct:.0f}% ({zone})")
        if valuation.get("price_to_atl"):
            extra_lines.append(f"- 距ATL倍数: {valuation['price_to_atl']}x")
        if valuation.get("price_to_ath"):
            extra_lines.append(f"- 距ATH回撤: {valuation['price_to_ath']}")

    prompt_parts = [
        "=== 代币基础信息 ===",
        "\n".join(basic_lines),
        "",
        f"=== 触发的信号（共 {len(asset_signals)} 条） ===",
        "\n".join(signal_lines) if signal_lines else "（无信号）",
        "",
    ]
    if extra_lines:
        prompt_parts.extend([
            "=== 额外维度数据 ===",
            "\n".join(extra_lines),
            "",
        ])
    prompt_parts.append("请综合以上所有信息，判断该代币是否进入【高亮信号】或【高危信号】，并给出详细分析。")

    return "\n".join(prompt_parts)


# ══════════════════════════════════════════════════════════════
# 批量分析：从机会列表中提取代币，批量调用 AI 精选
# ══════════════════════════════════════════════════════════════

def ai_enrich_highlight_signals(
    highlight_signals: list[dict],
    max_ai_analyze: int = 15,
) -> list[dict]:
    """
    对规则筛选出的高亮信号做 AI 增强：
    - 为每条信号补充 ai_reason 字段（AI 推荐理由摘要）
    - 按 AI 评分重新排序
    - 过滤掉 AI 认为不值得高亮的信号（可选，默认保留但降权）

    Args:
        highlight_signals: 规则筛选出的高亮信号列表（已按 target 合并）
        max_ai_analyze: 最多 AI 分析多少条（控制成本）

    Returns:
        增强后的信号列表，每条增加 ai_analysis 字段
    """
    if not highlight_signals:
        return []

    # 只对前 N 条做 AI 分析（成本控制）
    to_analyze = highlight_signals[:max_ai_analyze]
    rest = highlight_signals[max_ai_analyze:]

    enriched = []
    for sig in to_analyze:
        ai_result = _analyze_merged_signal(sig, direction="long")
        enriched_sig = {**sig, "ai_analysis": ai_result}
        # 如果 AI 不认为是高亮，降低排序权重但仍保留（避免一刀切）
        if not ai_result.get("should_highlight") and not ai_result.get("error"):
            enriched_sig["_ai_downgraded"] = True
        enriched.append(enriched_sig)

    # 按 AI 评分 + 原分数混合排序
    def _sort_key(s):
        ai_score = s.get("ai_analysis", {}).get("ai_score", 0) or 0
        base_score = s.get("conviction_score", 0) or 0
        downgraded = 1 if s.get("_ai_downgraded") else 0
        # 混合分：AI分占40%，原规则分占60%；被降级的排后面
        mixed = ai_score * 0.4 + base_score * 0.6
        return (-downgraded, mixed, ai_score)

    enriched.sort(key=_sort_key, reverse=True)
    return enriched + rest


def ai_enrich_risk_signals(
    risk_signals: list[dict],
    max_ai_analyze: int = 12,
) -> list[dict]:
    """对高危信号做 AI 增强（对称于高亮）。"""
    if not risk_signals:
        return []

    to_analyze = risk_signals[:max_ai_analyze]
    rest = risk_signals[max_ai_analyze:]

    enriched = []
    for sig in to_analyze:
        ai_result = _analyze_merged_signal(sig, direction="short")
        enriched_sig = {**sig, "ai_analysis": ai_result}
        if not ai_result.get("should_risk") and not ai_result.get("error"):
            enriched_sig["_ai_downgraded"] = True
        enriched.append(enriched_sig)

    def _sort_key(s):
        ai_score = s.get("ai_analysis", {}).get("ai_score", 0) or 0
        base_score = s.get("conviction_score", 0) or 0
        downgraded = 1 if s.get("_ai_downgraded") else 0
        # 风险信号：ai_score 越低风险越高？反过来用 100 - ai_score
        risk_score = 100 - ai_score if ai_score > 0 else 0
        mixed = risk_score * 0.4 + base_score * 0.6
        return (-downgraded, mixed, base_score)

    enriched.sort(key=_sort_key, reverse=True)
    return enriched + rest


def _analyze_merged_signal(merged_sig: dict, direction: str) -> dict:
    """对一条合并后的信号（可能包含多个子信号）做 AI 分析。"""
    asset_basic = {
        "asset_id": merged_sig.get("asset_id", 0),
        "symbol": merged_sig.get("target") or merged_sig.get("symbol", "?"),
        "name": merged_sig.get("name", ""),
        "sector": merged_sig.get("sector", ""),
        "price": merged_sig.get("price"),
        "market_cap": merged_sig.get("market_cap_usd"),
        "fdv": merged_sig.get("fdv"),
        "change_24h": merged_sig.get("change_24h"),
        "change_7d": merged_sig.get("change_7d"),
    }
    # 合并卡中的所有子信号
    asset_signals = merged_sig.get("all_signals") or [merged_sig]

    # 从信号中提取额外数据
    asset_extra = _extract_extra_from_signals(asset_signals)

    return analyze_asset_signals(asset_basic, asset_signals, asset_extra)


def _extract_extra_from_signals(signals: list[dict]) -> dict:
    """从信号列表中提取零散的额外数据，组装成 asset_extra 结构。"""
    extra = {
        "onchain": {},
        "unlocks": {},
        "social": {},
        "catalysts": [],
        "valuation": {},
    }

    for s in signals:
        stype = s.get("signal_type", "")
        # 解锁信号
        if stype == "token_unlock":
            if s.get("unlock_pct") is not None:
                extra["unlocks"]["next_unlock_pct"] = s["unlock_pct"]
            if s.get("unlock_date"):
                extra["unlocks"]["next_unlock_date"] = s["unlock_date"]
        # MVRV 估值
        if stype and stype.startswith("mvrv"):
            if s.get("mvrv_percentile") is not None:
                extra["valuation"]["mvrv_percentile"] = s["mvrv_percentile"]
        # 链上巨鲸
        if stype == "whale_flow" or stype == "kol_onchain":
            if s.get("usd_value") or s.get("event_usd_value"):
                extra["onchain"]["whale_flow_24h"] = (
                    f"${float(s.get('usd_value') or s.get('event_usd_value', 0))/1e6:.1f}M"
                )
            if s.get("address_label"):
                extra["onchain"]["address_label"] = s["address_label"]
        # 催化剂
        if stype == "catalyst":
            extra["catalysts"].append({
                "title": s.get("title", ""),
                "event_date": s.get("event_date", ""),
                "strength": s.get("strength", ""),
            })
        # 社交热度
        if stype and "social" in stype.lower():
            if s.get("volume"):
                extra["social"]["social_volume_24h"] = s["volume"]

    # 清理空字典
    for k in list(extra.keys()):
        v = extra[k]
        if isinstance(v, dict) and not v:
            del extra[k]
        elif isinstance(v, list) and not v:
            del extra[k]

    return extra
