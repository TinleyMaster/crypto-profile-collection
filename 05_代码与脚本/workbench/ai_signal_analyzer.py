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


# ══════════════════════════════════════════════════════════════
# 全量代币画像构建（FEAT-AI-HIGHLIGHT-V2）
# 直接从数据库各维度表拉取，组装完整 asset_profile
# ══════════════════════════════════════════════════════════════

def build_asset_profile(asset_id: int, conn=None) -> dict[str, Any]:
    """
    构建代币全量画像，用于喂给 AI 做综合分析。

    返回结构：
    {
        "basic": {...},           # 基础信息
        "market": {...},          # 行情数据
        "valuation": {...},       # 估值
        "onchain_holders": {...}, # 链上持仓
        "onchain_transfers": {...}, # 近期大额转账
        "unlocks": {...},         # 解锁数据
        "tokenomics": {...},      # 代币经济学
        "derivatives": {...},     # 衍生品数据
        "liquidity": {...},       # 流动性
        "social": {...},          # 社交热度
        "github": {...},          # 开发活跃
        "raises": [...],          # 融资历史
        "catalysts": [...],       # 近期催化剂
        "hacks": [...],           # 历史安全事件
        "kol_signals": [...],     # KOL 信号
        "sector": {...},          # 赛道信息
        "active_signals": [...],  # 当前触发的信号（由调用方填入）
    }

    缺少的数据维度会被省略（不填 null 字段，省 token）。
    """
    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    if conn is None:
        settings = get_settings()
        with get_connection(settings.database_url) as c:
            return build_asset_profile(asset_id, conn=c)

    profile: dict[str, Any] = {}

    # ── 1. 基础信息（coin_basic + asset_sector）──
    _add_basic_info(conn, asset_id, profile)

    # ── 2. 估值（MVRV 百分位 + CM 链上数据）──
    _add_valuation_data(conn, asset_id, profile)

    # ── 3. 链上持仓快照 ──
    _add_holder_snapshot(conn, asset_id, profile)

    # ── 4. 近 7 日大额转账聚合 ──
    _add_recent_transfers(conn, asset_id, profile)

    # ── 5. 解锁数据 ──
    _add_unlock_data(conn, asset_id, profile)

    # ── 6. 代币经济学 ──
    _add_tokenomics(conn, asset_id, profile)

    # ── 7. 衍生品资金面 ──
    _add_derivatives(conn, asset_id, profile)

    # ── 8. 流动性 ──
    _add_liquidity(conn, asset_id, profile)

    # ── 8b. 协议 TVL（DeFi 协议）──
    _add_protocol_tvl(conn, asset_id, profile)

    # ── 8c. 价格技术指标（从日价计算波动率/RSI/高低点）──
    _add_price_technical(conn, asset_id, profile)

    # ── 9. 社交热度（social_heat）──
    _add_social_heat(conn, asset_id, profile)

    # ── 10. GitHub 开发活跃 ──
    _add_github_activity(conn, asset_id, profile)

    # ── 11. 融资历史（近 1 年）──
    _add_raises(conn, asset_id, profile)

    # ── 12. 近期催化剂（±30 天）──
    _add_catalysts(conn, asset_id, profile)

    # ── 13. 历史安全事件 ──
    _add_hacks(conn, asset_id, profile)

    # ── 14. 近 7 天 KOL 信号 ──
    _add_kol_signals(conn, asset_id, profile)

    # ── 14b. 实时价格（Binance API，失败静默跳过）──
    _add_realtime_price(asset_id, profile)

    # ── 15. 风险汇总（综合各维度 + 标注数据缺口）──
    _add_risk_summary(conn, asset_id, profile)

    return profile


def _safe_float(v) -> float | None:
    """安全转 float，None/空值 返回 None。"""
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _add_basic_info(conn, asset_id: int, profile: dict):
    """基础信息 + 赛道。"""
    import psycopg.rows
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            SELECT cb.asset_id, cb.coin_symbol, cb.coin_name, cb.asset_type,
                   cb.main_chain, cb.primary_contract_address, cb.official_website,
                   cb.description_short, cb.logo_url,
                   a.primary_sector
            FROM biz.coin_basic cb
            JOIN core.asset a ON a.asset_id = cb.asset_id
            WHERE cb.asset_id = %s
        """, (asset_id,))
        row = cur.fetchone()
        if not row:
            return

        basic = {
            "asset_id": row["asset_id"],
            "symbol": row["coin_symbol"],
            "name": row["coin_name"],
            "asset_type": row["asset_type"],
            "main_chain": row["main_chain"],
            "primary_contract": row["primary_contract_address"],
            "website": row["official_website"],
            "description": row["description_short"],
            "primary_sector": row["primary_sector"],
        }
        # 去掉 None 值
        profile["basic"] = {k: v for k, v in basic.items() if v is not None}


def _add_valuation_data(conn, asset_id: int, profile: dict):
    """MVRV 百分位 + CM 链上指标（活跃地址、交易所资金流等）。

    注意：市值/价格/流通量以 core.asset（CMCsnapshot, 流通量口径）为准，
    CM 全量表(cap_mrkt_cur_usd) 使用 total supply 口径，仅供参考，不做主值。
    """
    import psycopg.rows
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        # 0. core.asset 权威数据（市值 = 流通量 × 价格）
        cur.execute("""
            SELECT market_cap, circulating_supply, total_supply, market_cap_rank
            FROM core.asset
            WHERE asset_id = %s
        """, (asset_id,))
        core_row = cur.fetchone()

        # 1. CM 最新日频数据（链上指标 + MVRV，做参考用）
        cur.execute("""
            SELECT d.metric_date, d.price_usd, d.cap_mvrv_cur,
                   d.adr_act_cnt, d.tx_tfr_cnt, d.tx_cnt, d.adr_bal_cnt,
                   d.flow_in_ex_usd, d.flow_out_ex_usd,
                   d.roi_30d, d.roi_1yr,
                   d.volume_reported_spot_usd_1d,
                   d.sply_cur, d.cap_mrkt_cur_usd, d.cap_mrkt_est_usd,
                   d.fee_tot_native, d.iss_tot_usd
            FROM biz.cm_asset_onchain_daily d
            WHERE d.asset_id = %s
            ORDER BY d.metric_date DESC
            LIMIT 1
        """, (asset_id,))
        row = cur.fetchone()
        if not row and not core_row:
            return

        market_extra = {}
        # 权威市值 & 流通量（来自 CMC / core.asset）
        if core_row and core_row["market_cap"] is not None:
            market_extra["market_cap_usd"] = _safe_float(core_row["market_cap"])
        if core_row and core_row["circulating_supply"] is not None:
            market_extra["circulating_supply"] = _safe_float(core_row["circulating_supply"])
        if core_row and core_row["total_supply"] is not None:
            market_extra["total_supply"] = _safe_float(core_row["total_supply"])
        if core_row and core_row["market_cap_rank"] is not None:
            market_extra["market_cap_rank"] = int(core_row["market_cap_rank"])

        if not row:
            if market_extra:
                profile["market"] = market_extra
            return

        # CM 价格（可能滞后，仅供参考；实时价由 price_technical / 实时 API 覆盖）
        if row["price_usd"] is not None:
            market_extra["price_cm_reference"] = _safe_float(row["price_usd"])
        # CM ROI 是市值回报率（含 supply 变化），仅供链上研究参考，不做主值
        if row["roi_30d"] is not None:
            market_extra["roi_30d_mcap_pct"] = round(float(row["roi_30d"]), 2)
        if row["roi_1yr"] is not None:
            market_extra["roi_1yr_mcap_pct"] = round(float(row["roi_1yr"]), 2)
        if row["volume_reported_spot_usd_1d"] is not None:
            market_extra["volume_cm_24h_usd"] = _safe_float(row["volume_reported_spot_usd_1d"])
        # CM 全量市值（total supply 口径，标注清楚）
        if row["cap_mrkt_cur_usd"] is not None:
            market_extra["market_cap_total_supply_usd"] = _safe_float(row["cap_mrkt_cur_usd"])
        if row["sply_cur"] is not None:
            market_extra["cm_current_supply"] = _safe_float(row["sply_cur"])

        # 链上指标（CM）
        onchain_cm = {}
        if row["adr_act_cnt"] is not None:
            onchain_cm["active_addresses_24h"] = int(row["adr_act_cnt"])
        if row["adr_bal_cnt"] is not None:
            onchain_cm["balance_addresses"] = int(row["adr_bal_cnt"])
        if row["tx_tfr_cnt"] is not None:
            onchain_cm["transfer_count_24h"] = int(row["tx_tfr_cnt"])
        if row["tx_cnt"] is not None:
            onchain_cm["tx_count_24h"] = int(row["tx_cnt"])
        if row["cap_mvrv_cur"] is not None:
            onchain_cm["mvrv_ratio"] = round(_safe_float(row["cap_mvrv_cur"]), 3)
        if row["flow_in_ex_usd"] is not None and row["flow_out_ex_usd"] is not None:
            net_ex = _safe_float(row["flow_in_ex_usd"]) - _safe_float(row["flow_out_ex_usd"])
            onchain_cm["exchange_net_flow_usd_24h"] = round(net_ex, 2)
            onchain_cm["exchange_inflow_usd_24h"] = _safe_float(row["flow_in_ex_usd"])
            onchain_cm["exchange_outflow_usd_24h"] = _safe_float(row["flow_out_ex_usd"])
        if row["fee_tot_native"] is not None:
            onchain_cm["fee_total_native_24h"] = _safe_float(row["fee_tot_native"])
        if row["iss_tot_usd"] is not None and row["iss_tot_usd"] != 0:
            onchain_cm["issuance_usd_24h"] = _safe_float(row["iss_tot_usd"])

        if market_extra:
            profile["market"] = market_extra
        if onchain_cm:
            profile.setdefault("onchain", {})["cm_metrics"] = onchain_cm

        # 2. 全部 CM 百分位指标（mvrv/adr_act/tx_tfr/flow_in/flow_out/roi30d/roi1yr）
        cur.execute("""
            SELECT metric, pct_full, pct_roll_365d, flag_full
            FROM biz.cm_onchain_percentile
            WHERE asset_id = %s
            ORDER BY metric_date DESC, metric
            LIMIT 20
        """, (asset_id,))
        rows = cur.fetchall()
        if not rows:
            return

        # 取最新日期各 metric 的百分位
        latest_pct = {}
        for r in rows:
            m = r["metric"]
            if m not in latest_pct:
                latest_pct[m] = r

        valuation = profile.get("valuation", {})
        metric_label_map = {
            "mvrv": "mvrv",
            "adr_act": "active_addr",
            "tx_tfr": "tx_transfer",
            "flow_in": "exchange_inflow",
            "flow_out": "exchange_outflow",
            "roi30d": "roi_30d",
            "roi1yr": "roi_1yr",
        }
        for m, r in latest_pct.items():
            label = metric_label_map.get(m, m)
            if r["pct_full"] is not None:
                valuation[f"{label}_percentile"] = round(float(r["pct_full"]), 1)
            if r["pct_roll_365d"] is not None:
                valuation[f"{label}_percentile_365d"] = round(float(r["pct_roll_365d"]), 1)

        # MVRV 估值区间判断
        # 优先用 CM 官方 extreme 标志（flag_full），其次用百分位+绝对值综合判断
        mvrv_ratio = None
        cm_metrics = profile.get("onchain", {}).get("cm_metrics", {})
        if cm_metrics.get("mvrv_ratio") is not None:
            mvrv_ratio = float(cm_metrics["mvrv_ratio"])

        mvrv_flag = None
        if "mvrv" in latest_pct:
            mvrv_flag = latest_pct["mvrv"]["flag_full"]

        if mvrv_flag and mvrv_flag != "NONE":
            # CM 官方极端标志：HIGH = 极度高估，LOW = 极度低估
            if mvrv_flag == "HIGH":
                valuation["mvrv_zone"] = "极度高估"
            elif mvrv_flag == "LOW":
                valuation["mvrv_zone"] = "极度低估"
            else:
                valuation["mvrv_zone"] = "中性"
            valuation["mvrv_zone_source"] = "cm_extreme_flag"
        elif mvrv_ratio is not None:
            # 用 MVRV 绝对值 + 百分位综合判断（更可靠）
            mvrv_pct = valuation.get("mvrv_percentile")
            if mvrv_ratio >= 3.0:
                valuation["mvrv_zone"] = "极度高估"
            elif mvrv_ratio >= 2.0:
                valuation["mvrv_zone"] = "高估"
            elif mvrv_ratio >= 1.2:
                valuation["mvrv_zone"] = "偏高于公允价值"
            elif mvrv_ratio >= 0.8:
                valuation["mvrv_zone"] = "中性（近公允价值）"
            elif mvrv_ratio >= 0.5:
                valuation["mvrv_zone"] = "偏低于公允价值"
            else:
                valuation["mvrv_zone"] = "低估"
            valuation["mvrv_zone_source"] = "mvrv_absolute"
            if mvrv_pct is not None:
                valuation["mvrv_percentile_note"] = f"相对自身历史百分位 {mvrv_pct}%"
        elif "mvrv_percentile" in valuation:
            # 兜底：仅百分位
            mvrv_pct = valuation["mvrv_percentile"]
            valuation["mvrv_zone"] = (
                "极度高估" if mvrv_pct > 85 else
                "高估" if mvrv_pct > 70 else
                "中性" if mvrv_pct > 30 else
                "低估" if mvrv_pct > 15 else
                "极度低估"
            )
            valuation["mvrv_zone_source"] = "percentile_only"

        if valuation:
            profile["valuation"] = valuation


def _add_holder_snapshot(conn, asset_id: int, profile: dict):
    """链上持仓快照（优先取主链，其次取持仓人数最多的链）。"""
    import psycopg.rows
    main_chain = profile.get("basic", {}).get("main_chain")

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        # 取最新日期各链的持仓数据，优先匹配主链
        cur.execute("""
            SELECT chain, contract_address, snapshot_date,
                   top10_concentration, top50_concentration, top100_concentration,
                   total_holders, holder_change_7d, holder_change_30d,
                   whale_balance_change_7d_pct, whale_balance_change_30d_pct,
                   exchange_wallet_pct, vc_wallet_pct, smart_money_pct,
                   retail_pct, contract_pct
            FROM biz.onchain_holder_snapshot
            WHERE asset_id = %s
              AND snapshot_date = (
                  SELECT MAX(snapshot_date) FROM biz.onchain_holder_snapshot WHERE asset_id = %s
              )
            ORDER BY total_holders DESC
        """, (asset_id, asset_id))
        rows = cur.fetchall()
        if not rows:
            return

        # 优先主链，没有则取持仓人数最多的链
        row = None
        if main_chain:
            for r in rows:
                if r["chain"] and r["chain"].lower() == main_chain.lower():
                    row = r
                    break
        if row is None:
            row = rows[0]  # 兜底：取持仓人数最多的

        holders = {
            "snapshot_date": str(row["snapshot_date"]),
            "chain": row["chain"],
            "contract_address": row["contract_address"],
        }
        if row["top10_concentration"] is not None:
            holders["top10_pct"] = _safe_float(row["top10_concentration"])
        if row["top50_concentration"] is not None:
            holders["top50_pct"] = _safe_float(row["top50_concentration"])
        if row["top100_concentration"] is not None:
            holders["top100_pct"] = _safe_float(row["top100_concentration"])
        if row["total_holders"] is not None:
            holders["total_holders"] = int(row["total_holders"])
        if row["holder_change_7d"] is not None:
            holders["holder_change_7d"] = int(row["holder_change_7d"])
        if row["holder_change_30d"] is not None:
            holders["holder_change_30d"] = int(row["holder_change_30d"])
        if row["whale_balance_change_7d_pct"] is not None:
            holders["whale_change_7d_pct"] = _safe_float(row["whale_balance_change_7d_pct"])
        if row["whale_balance_change_30d_pct"] is not None:
            holders["whale_change_30d_pct"] = _safe_float(row["whale_balance_change_30d_pct"])

        # 地址类型分布
        dist = {}
        if row["exchange_wallet_pct"] is not None:
            dist["exchange_pct"] = _safe_float(row["exchange_wallet_pct"])
        if row["vc_wallet_pct"] is not None:
            dist["vc_pct"] = _safe_float(row["vc_wallet_pct"])
        if row["smart_money_pct"] is not None:
            dist["smart_money_pct"] = _safe_float(row["smart_money_pct"])
        if row["retail_pct"] is not None:
            dist["retail_pct"] = _safe_float(row["retail_pct"])
        if dist:
            holders["address_distribution"] = dist

        # 如果有其他链的数据，简要标注
        other_chains = [r["chain"] for r in rows if r["chain"] != row["chain"]]
        if other_chains:
            holders["other_chains_available"] = other_chains

        profile["onchain_holders"] = holders


def _add_recent_transfers(conn, asset_id: int, profile: dict):
    """近 7 天大额转账聚合统计。"""
    import psycopg.rows
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            SELECT
                COUNT(*) AS tx_count_7d,
                COALESCE(SUM(CASE WHEN is_to_exchange THEN value_usd ELSE 0 END), 0) AS to_exchange_usd_7d,
                COALESCE(SUM(CASE WHEN NOT is_to_exchange THEN value_usd ELSE 0 END), 0) AS to_non_exchange_usd_7d,
                COALESCE(SUM(value_usd), 0) AS total_usd_7d,
                MAX(value_usd) AS max_single_tx_usd
            FROM biz.onchain_transfer_log
            WHERE asset_id = %s
              AND block_timestamp >= NOW() - INTERVAL '7 days'
              AND is_suspect IS NOT TRUE
        """, (asset_id,))
        row = cur.fetchone()
        if not row or row["tx_count_7d"] == 0:
            return

        transfers = {
            "tx_count_7d": int(row["tx_count_7d"]),
            "total_value_usd_7d": _safe_float(row["total_usd_7d"]),
            "to_exchange_usd_7d": _safe_float(row["to_exchange_usd_7d"]),
            "to_non_exchange_usd_7d": _safe_float(row["to_non_exchange_usd_7d"]),
            "max_single_tx_usd": _safe_float(row["max_single_tx_usd"]),
        }
        # 净流入方向判断（转出交易所=吸筹，转入交易所=抛压）
        net = _safe_float(row["to_non_exchange_usd_7d"]) - _safe_float(row["to_exchange_usd_7d"])
        transfers["net_flow_direction"] = "吸筹（转出交易所）" if net > 0 else "抛压（转入交易所）"
        transfers["net_flow_usd_7d"] = abs(net)

        # Top 5 最大额转账明细（去重：同金额同from同to且时间差<30分钟视为重复）
        cur.execute("""
            SELECT block_timestamp, from_label, to_label,
                   from_exchange, to_exchange,
                   value, value_usd, is_to_exchange, tx_hash
            FROM biz.onchain_transfer_log
            WHERE asset_id = %s
              AND block_timestamp >= NOW() - INTERVAL '7 days'
              AND is_suspect IS NOT TRUE
            ORDER BY value_usd DESC
            LIMIT 20
        """, (asset_id,))
        rows = cur.fetchall()
        if rows:
            top_list = []
            seen_keys = set()
            for r in rows:
                if len(top_list) >= 5:
                    break
                # 构造去重 key：from + to + 金额(整数万美元) + 时间(30分钟桶)
                from_side = r["from_label"] or r["from_exchange"] or "unknown"
                to_side = r["to_label"] or r["to_exchange"] or "unknown"
                val_bucket = int(_safe_float(r["value_usd"]) / 10000) if r["value_usd"] else 0
                ts = r["block_timestamp"]
                time_bucket = 0
                if ts:
                    time_bucket = int(ts.timestamp() / 1800)  # 30分钟桶
                key = (from_side, to_side, val_bucket, time_bucket)
                if key in seen_keys:
                    continue
                seen_keys.add(key)

                item = {
                    "time": str(r["block_timestamp"])[:19] if r["block_timestamp"] else "",
                    "from": from_side,
                    "to": to_side,
                    "value_usd": _safe_float(r["value_usd"]),
                    "is_to_exchange": bool(r["is_to_exchange"]),
                }
                top_list.append(item)
            transfers["top_5_transfers"] = top_list
            # 标注数据覆盖范围
            transfers["coverage_note"] = (
                "链上转账数据为大额抽样覆盖，可能遗漏部分 Smart Money/巨鲸交易；"
                "实际市场大单规模可能大于此统计。"
            )

        profile["onchain_transfers_7d"] = transfers


def _add_unlock_data(conn, asset_id: int, profile: dict):
    """解锁数据（从 asset_token_unlocks 的 JSONB 字段中提取）。"""
    import psycopg.rows
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            SELECT source_url, source_name, slug,
                   overview_json, unlock_events_json,
                   revenue_json, valuation_json,
                   methodology_json, input_snapshot_json,
                   crawl_status, unlock_ratio_mcap,
                   scraped_at, updated_at
            FROM biz.asset_token_unlocks
            WHERE asset_id = %s
              AND crawl_status = 'ok'
            LIMIT 1
        """, (asset_id,))
        row = cur.fetchone()
        if not row:
            return

        ov = row["overview_json"] or {}
        if not ov:
            return

        unlocks = {
            "data_source": row["source_name"],
            "source_url": row["source_url"],
            "status": row["crawl_status"],
        }
        if ov.get("next_unlock_date"):
            unlocks["next_unlock_date"] = ov["next_unlock_date"]
        if ov.get("next_unlock_pct") is not None:
            unlocks["next_unlock_pct"] = _safe_float(ov["next_unlock_pct"])
        if ov.get("next_unlock_pct_mcap") is not None:
            unlocks["next_unlock_pct_mcap"] = _safe_float(ov["next_unlock_pct_mcap"])
        if ov.get("next_unlock_value_str"):
            unlocks["next_unlock_value_usd_str"] = ov["next_unlock_value_str"]
        if ov.get("next_unlock_amount_str"):
            unlocks["next_unlock_amount_str"] = ov["next_unlock_amount_str"]
        if ov.get("released_pct") is not None:
            unlocks["released_pct"] = _safe_float(ov["released_pct"])
        if ov.get("released_amount_str"):
            unlocks["released_amount_str"] = ov["released_amount_str"]
        if ov.get("total_supply_str"):
            unlocks["total_supply_str"] = ov["total_supply_str"]
        if row["unlock_ratio_mcap"] is not None:
            unlocks["unlock_ratio_mcap"] = _safe_float(row["unlock_ratio_mcap"])
        if row["updated_at"]:
            unlocks["last_updated"] = str(row["updated_at"])[:19]

        # 从 unlock_events_json 提取未来 30/90/180 天解锁占比
        events = row["unlock_events_json"] or []
        if events and isinstance(events, list):
            from datetime import datetime, date
            today = date.today()
            unlock_30d_pct = 0.0
            unlock_90d_pct = 0.0
            unlock_180d_pct = 0.0
            total_events_future = 0
            for evt in events:
                d_str = evt.get("date") or evt.get("unlock_date")
                if not d_str:
                    continue
                try:
                    # 尝试解析日期（支持 YYYY-MM-DD 和其他常见格式）
                    if isinstance(d_str, str) and len(d_str) >= 10:
                        evt_date = datetime.strptime(d_str[:10], "%Y-%m-%d").date()
                    else:
                        continue
                except (ValueError, TypeError):
                    continue
                days_ahead = (evt_date - today).days
                if days_ahead < 0:
                    continue
                total_events_future += 1
                pct = _safe_float(evt.get("pct") or evt.get("unlock_pct") or 0) or 0
                if days_ahead <= 30:
                    unlock_30d_pct += pct
                if days_ahead <= 90:
                    unlock_90d_pct += pct
                if days_ahead <= 180:
                    unlock_180d_pct += pct
            if total_events_future > 0:
                unlocks["total_future_events"] = total_events_future
                unlocks["unlock_30d_pct"] = round(unlock_30d_pct, 4)
                unlocks["unlock_90d_pct"] = round(unlock_90d_pct, 4)
                unlocks["unlock_180d_pct"] = round(unlock_180d_pct, 4)
            # 列出最近 5 个事件
            upcoming = [e for e in events if (
                e.get("date") or e.get("unlock_date")
            )]
            if upcoming:
                unlocks["upcoming_events"] = upcoming[:5]

        # 估值数据（如有）
        val = row["valuation_json"] or {}
        if val and isinstance(val, dict):
            unlocks["valuation_info"] = {
                k: v for k, v in val.items()
                if k in ("fdv", "mcap", "price", "revenue")
            }

        profile["unlocks"] = unlocks


def _add_tokenomics(conn, asset_id: int, profile: dict):
    """代币经济学。"""
    import psycopg.rows
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            SELECT total_supply, max_supply, circulating_supply,
                   buy_tax_pct, sell_tax_pct, contract_renounced,
                   lp_locked, allocation_json, burn_info,
                   emission_schedule, inflation_info, utility_info,
                   confidence
            FROM biz.asset_tokenomics
            WHERE asset_id = %s
            LIMIT 1
        """, (asset_id,))
        row = cur.fetchone()
        if not row:
            return

        tko = {}
        if row["total_supply"] is not None:
            tko["total_supply"] = _safe_float(row["total_supply"])
        if row["max_supply"] is not None:
            tko["max_supply"] = _safe_float(row["max_supply"])
        if row["circulating_supply"] is not None:
            tko["circulating_supply"] = _safe_float(row["circulating_supply"])
        if row["circulating_supply"] is not None and row["max_supply"] and float(row["max_supply"]) > 0:
            tko["circulating_ratio_pct"] = round(
                float(row["circulating_supply"]) / float(row["max_supply"]) * 100, 2
            )
        if row["buy_tax_pct"] is not None:
            tko["buy_tax_pct"] = _safe_float(row["buy_tax_pct"])
        if row["sell_tax_pct"] is not None:
            tko["sell_tax_pct"] = _safe_float(row["sell_tax_pct"])
        if row["contract_renounced"] is not None:
            tko["contract_renounced"] = bool(row["contract_renounced"])
        if row["lp_locked"] is not None:
            tko["lp_locked"] = bool(row["lp_locked"])
        if row["allocation_json"]:
            tko["allocation"] = row["allocation_json"]
        if row["burn_info"]:
            info = row["burn_info"]
            tko["burn_info"] = info[:200]
            if len(info) > 200:
                tko["burn_info"] += "…[已截断]"
        if row["emission_schedule"]:
            info = row["emission_schedule"]
            tko["emission_schedule"] = info[:400]
            if len(info) > 400:
                tko["emission_schedule"] += "…[已截断]"
        if row["inflation_info"]:
            info = row["inflation_info"]
            tko["inflation_info"] = info[:200]
            if len(info) > 200:
                tko["inflation_info"] += "…[已截断]"
        if row["utility_info"]:
            info = row["utility_info"]
            tko["utility_info"] = info[:300]
            if len(info) > 300:
                tko["utility_info"] += "…[已截断]"
        if row["confidence"] is not None:
            tko["data_confidence"] = _safe_float(row["confidence"])

        if tko:
            profile["tokenomics"] = tko


def _add_derivatives(conn, asset_id: int, profile: dict):
    """衍生品资金面。"""
    import psycopg.rows
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            SELECT funding_rate_pct, next_funding_time,
                   funding_rate_7d_avg, funding_rate_30d_avg,
                   total_oi_usd, oi_change_24h_pct,
                   cvd_24h_usd, cvd_ratio_24h,
                   available_exchanges, fetched_at
            FROM biz.asset_derivatives
            WHERE asset_id = %s
            LIMIT 1
        """, (asset_id,))
        row = cur.fetchone()
        if not row:
            return

        der = {}
        if row["funding_rate_pct"] is not None:
            der["funding_rate_pct"] = _safe_float(row["funding_rate_pct"])
        if row["funding_rate_7d_avg"] is not None:
            der["funding_rate_7d_avg"] = _safe_float(row["funding_rate_7d_avg"])
        if row["funding_rate_30d_avg"] is not None:
            der["funding_rate_30d_avg"] = _safe_float(row["funding_rate_30d_avg"])
        if row["total_oi_usd"] is not None:
            der["total_oi_usd"] = _safe_float(row["total_oi_usd"])
        if row["oi_change_24h_pct"] is not None:
            der["oi_change_24h_pct"] = _safe_float(row["oi_change_24h_pct"])
        if row["cvd_24h_usd"] is not None:
            der["cvd_24h_usd"] = _safe_float(row["cvd_24h_usd"])
        if row["cvd_ratio_24h"] is not None:
            der["cvd_ratio_24h"] = _safe_float(row["cvd_ratio_24h"])
        if row["available_exchanges"]:
            der["exchanges"] = list(row["available_exchanges"])
        if row["fetched_at"]:
            der["fetched_at"] = str(row["fetched_at"])[:19]

        if der:
            profile["derivatives"] = der


def _add_liquidity(conn, asset_id: int, profile: dict):
    """流动性（DEX + CEX）。"""
    import psycopg.rows
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            SELECT chain, pool_count, total_liquidity_usd,
                   top_pool_share_pct, cex_listed, cex_exchanges, source
            FROM biz.asset_liquidity
            WHERE asset_id = %s
            ORDER BY total_liquidity_usd DESC NULLS LAST
        """, (asset_id,))
        rows = cur.fetchall()
        if not rows:
            return

        liq_list = []
        for r in rows:
            item = {}
            item["chain"] = r["chain"]
            if r["total_liquidity_usd"] is not None:
                item["total_liquidity_usd"] = _safe_float(r["total_liquidity_usd"])
            if r["pool_count"] is not None:
                item["pool_count"] = int(r["pool_count"])
            if r["top_pool_share_pct"] is not None:
                item["top_pool_share_pct"] = _safe_float(r["top_pool_share_pct"])
            if r["cex_listed"] is not None:
                item["cex_listed"] = bool(r["cex_listed"])
            if r["cex_exchanges"]:
                item["cex_exchanges"] = list(r["cex_exchanges"])
            liq_list.append(item)

        # 聚合：总流动性、是否上大所
        summary = {"chains": liq_list}
        total_liq = sum(
            item.get("total_liquidity_usd", 0) or 0
            for item in liq_list
        )
        if total_liq > 0:
            summary["total_liquidity_usd"] = round(total_liq, 2)
        any_cex = any(item.get("cex_listed", False) for item in liq_list)
        summary["cex_listed"] = any_cex

        profile["liquidity"] = summary


def _add_protocol_tvl(conn, asset_id: int, profile: dict):
    """DeFi 协议 TVL（总锁仓价值）及变化。"""
    import psycopg.rows
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            SELECT metric_date, tvl, tvl_change_1d, tvl_change_7d, source_code
            FROM biz.protocol_metric_daily
            WHERE asset_id = %s
            ORDER BY metric_date DESC
            LIMIT 1
        """, (asset_id,))
        row = cur.fetchone()
        if not row or row["tvl"] is None:
            return

        tvl_data = {}
        if row["tvl"] is not None:
            tvl_data["tvl_usd"] = _safe_float(row["tvl"])
        if row["tvl_change_1d"] is not None:
            tvl_data["tvl_change_1d_pct"] = round(float(row["tvl_change_1d"]), 2)
        if row["tvl_change_7d"] is not None:
            tvl_data["tvl_change_7d_pct"] = round(float(row["tvl_change_7d"]), 2)
        if row["metric_date"]:
            tvl_data["metric_date"] = str(row["metric_date"])
        if row["source_code"]:
            tvl_data["source"] = row["source_code"]

        # 计算 TVL / 市值比（如果有市值数据）
        mcap = (profile.get("market", {}).get("market_cap_usd")
                or profile.get("market", {}).get("fdv"))
        if mcap and mcap > 0 and tvl_data.get("tvl_usd"):
            tvl_data["tvl_to_market_cap_ratio"] = round(tvl_data["tvl_usd"] / mcap, 3)

        profile["protocol_tvl"] = tvl_data


def _add_price_technical(conn, asset_id: int, profile: dict):
    """从 asset_market_daily 日价数据计算技术指标：
    波动率(30d/90d)、RSI(14)、近 30/90 日高低点、价格位置。
    """
    import psycopg.rows
    import math
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            SELECT market_date, price_usd, volume_24h, change_24h
            FROM biz.asset_market_daily
            WHERE asset_id = %s
            ORDER BY market_date ASC
        """, (asset_id,))
        rows = cur.fetchall()
        if len(rows) < 15:  # 至少需要 15 天才能算 RSI
            return

        prices = [_safe_float(r["price_usd"]) for r in rows if r["price_usd"] is not None]
        dates = [str(r["market_date"]) for r in rows if r["price_usd"] is not None]
        if len(prices) < 15:
            return

        tech = {}
        latest = prices[-1]
        tech["latest_price"] = round(latest, 6)
        tech["latest_date"] = dates[-1]
        tech["data_points"] = len(prices)

        # ── 日收益率（用于波动率计算）──
        returns = []
        for i in range(1, len(prices)):
            if prices[i - 1] and prices[i - 1] > 0:
                returns.append((prices[i] - prices[i - 1]) / prices[i - 1])

        if len(returns) >= 30:
            vol_30d = math.sqrt(sum(r * r for r in returns[-30:]) / 30) * 100
            tech["volatility_30d_pct"] = round(vol_30d, 2)

        if len(returns) >= 90:
            vol_90d = math.sqrt(sum(r * r for r in returns[-90:]) / 90) * 100
            tech["volatility_90d_pct"] = round(vol_90d, 2)

        # ── RSI(14) ──
        if len(prices) >= 15:
            gains = []
            losses = []
            for i in range(1, len(prices)):
                diff = prices[i] - prices[i - 1]
                gains.append(max(diff, 0))
                losses.append(max(-diff, 0))

            # 简单移动平均法计算 RSI
            period = 14
            if len(gains) >= period:
                avg_gain = sum(gains[-period:]) / period
                avg_loss = sum(losses[-period:]) / period
                if avg_loss == 0:
                    rsi = 100.0
                else:
                    rs = avg_gain / avg_loss
                    rsi = 100 - (100 / (1 + rs))
                tech["rsi_14"] = round(rsi, 2)
                if rsi >= 70:
                    tech["rsi_signal"] = "超买"
                elif rsi <= 30:
                    tech["rsi_signal"] = "超卖"
                else:
                    tech["rsi_signal"] = "中性"

        # ── 近 30 日高低点 ──
        if len(prices) >= 30:
            p30 = prices[-30:]
            hi30 = max(p30)
            lo30 = min(p30)
            tech["high_30d"] = round(hi30, 6)
            tech["low_30d"] = round(lo30, 6)
            if hi30 != lo30:
                pos_30d = (latest - lo30) / (hi30 - lo30) * 100
                tech["price_position_30d_pct"] = round(pos_30d, 1)

        # ── 近 90 日高低点 ──
        if len(prices) >= 90:
            p90 = prices[-90:]
            hi90 = max(p90)
            lo90 = min(p90)
            tech["high_90d"] = round(hi90, 6)
            tech["low_90d"] = round(lo90, 6)
            if hi90 != lo90:
                pos_90d = (latest - lo90) / (hi90 - lo90) * 100
                tech["price_position_90d_pct"] = round(pos_90d, 1)

        # ── 累计涨跌幅 ──
        if len(prices) >= 7 and prices[-8] and prices[-8] > 0:
            tech["change_7d_pct"] = round((latest - prices[-8]) / prices[-8] * 100, 2)
        if len(prices) >= 30 and prices[-31] and prices[-31] > 0:
            tech["change_30d_pct"] = round((latest - prices[-31]) / prices[-31] * 100, 2)
        if len(prices) >= 90 and prices[-91] and prices[-91] > 0:
            tech["change_90d_pct"] = round((latest - prices[-91]) / prices[-91] * 100, 2)

        profile["price_technical"] = tech

        # 把权威的价格变化率同步到 market 段（覆盖 CM 的市值 ROI）
        market = profile.setdefault("market", {})
        if "change_7d_pct" in tech:
            market["roi_7d_pct"] = tech["change_7d_pct"]
        if "change_30d_pct" in tech:
            market["roi_30d_pct"] = tech["change_30d_pct"]
        if "change_90d_pct" in tech:
            market["roi_90d_pct"] = tech["change_90d_pct"]
        # 以最新日价为权威价（比 CM 数据更新到 9/8）
        if "latest_price" in tech:
            market["price"] = tech["latest_price"]
            market["price_date"] = tech["latest_date"]
            market["price_source"] = "asset_market_daily"


def _add_realtime_price(asset_id: int, profile: dict):
    """从 Binance API 获取实时价格（失败静默跳过，不影响主流程）。

    注意：这是纯价格补充，不阻塞、不缓存到 DB，只在本次画像中使用。
    """
    symbol = profile.get("basic", {}).get("symbol", "")
    if not symbol:
        return

    pair = f"{symbol.upper()}USDT"
    try:
        import requests
        resp = requests.get(
            "https://api.binance.com/api/v3/ticker/24hr",
            params={"symbol": pair},
            timeout=5,
        )
        if resp.status_code != 200:
            return
        data = resp.json()
        if not data or "lastPrice" not in data:
            return

        last_price = _safe_float(data["lastPrice"])
        change_24h = _safe_float(data.get("priceChangePercent"))
        high_24h = _safe_float(data.get("highPrice"))
        low_24h = _safe_float(data.get("lowPrice"))
        volume_24h = _safe_float(data.get("quoteVolume"))

        if last_price is None:
            return

        rt = {
            "price": last_price,
            "source": "binance_realtime",
            "fetched_at": __import__("time").time(),
            "pair": pair,
        }
        if change_24h is not None:
            rt["change_24h_pct"] = change_24h
        if high_24h is not None:
            rt["high_24h"] = high_24h
        if low_24h is not None:
            rt["low_24h"] = low_24h
        if volume_24h is not None:
            rt["volume_24h_usd"] = volume_24h

        profile["realtime_price"] = rt

        # 实时价同步到 market 段（覆盖日线价，作为最新价）
        market = profile.setdefault("market", {})
        market["price"] = last_price
        market["price_source"] = "binance_realtime"
        # 重算市值（用流通量 × 实时价）
        new_mcap = None
        if market.get("circulating_supply"):
            new_mcap = round(
                last_price * float(market["circulating_supply"]), 2
            )
            market["market_cap_usd"] = new_mcap

        # 同步重算派生指标（TVL/MCap、OI/MCap），保持一致
        if new_mcap and new_mcap > 0:
            # TVL / 市值
            tvl_data = profile.get("protocol_tvl", {})
            if tvl_data.get("tvl_usd"):
                tvl_data["tvl_to_market_cap_ratio"] = round(
                    float(tvl_data["tvl_usd"]) / new_mcap, 3
                )
            # OI / 市值（风险汇总里的也同步更新）
            der = profile.get("derivatives", {})
            if der.get("total_oi_usd"):
                oi_ratio = float(der["total_oi_usd"]) / new_mcap * 100
                risk = profile.setdefault("risk_summary", {})
                risk["oi_to_mcap_pct"] = round(oi_ratio, 2)
                if oi_ratio > 15:
                    risk["oi_risk"] = "高（衍生品占市值比例大）"
                elif oi_ratio > 5:
                    risk["oi_risk"] = "中"
                else:
                    risk["oi_risk"] = "低"

    except Exception:
        # 实时价获取失败就跳过，不影响主流程
        pass


def _add_social_heat(conn, asset_id: int, profile: dict):
    """社交热度（从 asset_social_heat 的 JSONB 字段中提取）。"""
    import psycopg.rows
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            SELECT score, confidence,
                   community_json, sentiment_json, trend_json,
                   market_json, score_detail_json,
                   dex_trending_json, dex_boost_score, dex_source,
                   fetched_at, updated_at
            FROM biz.asset_social_heat
            WHERE asset_id = %s
            ORDER BY fetched_at DESC
            LIMIT 1
        """, (asset_id,))
        row = cur.fetchone()
        if not row:
            return

        social = {}
        if row["score"] is not None:
            social["heat_score"] = _safe_float(row["score"])
        if row["confidence"]:
            social["data_confidence"] = row["confidence"]

        # 社区数据
        comm = row["community_json"] or {}
        if comm and isinstance(comm, dict):
            community = {}
            for key, label in [
                ("twitter_followers", "twitter_followers"),
                ("github_stars", "github_stars"),
                ("github_forks", "github_forks"),
                ("reddit_subscribers", "reddit_subscribers"),
                ("reddit_accounts_active_48h", "reddit_active_48h"),
                ("reddit_average_posts_48h", "reddit_posts_48h"),
                ("telegram_channel_user_count", "telegram_members"),
            ]:
                if key in comm and comm[key] is not None:
                    community[label] = comm[key]
            if community:
                social["community"] = community

        # 情绪数据
        sent = row["sentiment_json"] or {}
        if sent and isinstance(sent, dict):
            sentiment = {}
            for k, v in sent.items():
                if v is not None:
                    sentiment[k] = v
            if sentiment:
                social["sentiment"] = sentiment

        # 趋势/新闻数据
        trend = row["trend_json"] or {}
        if trend and isinstance(trend, dict):
            trend_info = {}
            if trend.get("trending_rank") is not None:
                trend_info["trending_rank"] = trend["trending_rank"]
            news_list = trend.get("news") or []
            if news_list and isinstance(news_list, list):
                trend_info["recent_news_count"] = len(news_list)
                trend_info["recent_news"] = [
                    n.get("title", "") for n in news_list[:3] if n.get("title")
                ]
            if trend_info:
                social["trend"] = trend_info

        # 市场热度
        mkt = row["market_json"] or {}
        if mkt and isinstance(mkt, dict):
            market_heat = {}
            if mkt.get("total_volume_usd") is not None:
                market_heat["volume_usd"] = _safe_float(mkt["total_volume_usd"])
            if mkt.get("price_change_7d") is not None:
                market_heat["price_change_7d_pct"] = _safe_float(mkt["price_change_7d"])
            if market_heat:
                social["market_heat"] = market_heat

        # 评分详情
        detail = row["score_detail_json"] or {}
        if detail and isinstance(detail, dict):
            scores = {}
            sub = detail.get("sub") or {}
            for k in ["trend", "market", "community", "sentiment"]:
                if detail.get(k) is not None:
                    scores[k] = _safe_float(detail[k])
                elif sub.get(k) is not None:
                    scores[k] = _safe_float(sub[k])
            if scores:
                social["score_breakdown"] = scores

        if row["dex_boost_score"] is not None:
            social["dex_boost_score"] = _safe_float(row["dex_boost_score"])
        if row["dex_source"]:
            social["dex_source"] = row["dex_source"]
        if row["updated_at"]:
            social["last_updated"] = str(row["updated_at"])[:19]

        if social:
            profile["social"] = social


def _add_github_activity(conn, asset_id: int, profile: dict):
    """GitHub 开发活跃度。"""
    import psycopg.rows
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        # 关联 asset_github_repo + github_repo_activity
        cur.execute("""
            SELECT gra.owner_login, gra.repo_name, gra.description,
                   gra.stars_count, gra.forks_count, gra.language,
                   gra.topics, gra.license_name, gra.archived, gra.disabled,
                   gra.total_commits_52w, gra.contributor_count_52w,
                   gra.weekly_commit_counts, gra.top_contributors,
                   gra.pushed_at, gra.fetched_at, gra.created_at
            FROM biz.asset_github_repo agr
            JOIN biz.github_repo_activity gra
                 ON gra.id = agr.repo_id
            WHERE agr.asset_id = %s
              AND agr.is_primary = TRUE
            ORDER BY agr.confidence DESC
            LIMIT 1
        """, (asset_id,))
        row = cur.fetchone()
        if not row:
            return

        gh = {
            "repo": f"{row['owner_login']}/{row['repo_name']}",
        }
        if row["stars_count"] is not None:
            gh["stars"] = int(row["stars_count"])
        if row["forks_count"] is not None:
            gh["forks"] = int(row["forks_count"])
        if row["language"]:
            gh["language"] = row["language"]
        if row["total_commits_52w"] is not None:
            gh["commits_52w"] = int(row["total_commits_52w"])
        if row["contributor_count_52w"] is not None:
            gh["contributors_52w"] = int(row["contributor_count_52w"])
        if row["topics"]:
            gh["topics"] = list(row["topics"])[:10]
        if row["pushed_at"]:
            gh["last_pushed_at"] = str(row["pushed_at"])[:10]

        # 近 4 周 vs 前 4 周 commits 变化趋势
        if row["weekly_commit_counts"]:
            weekly = row["weekly_commit_counts"]
            if isinstance(weekly, list) and len(weekly) >= 8:
                recent_4 = sum(weekly[:4]) if all(isinstance(x, (int, float)) for x in weekly[:4]) else 0
                prev_4 = sum(weekly[4:8]) if all(isinstance(x, (int, float)) for x in weekly[4:8]) else 0
                if prev_4 > 0:
                    change_pct = (recent_4 - prev_4) / prev_4 * 100
                    gh["commits_4w_change_pct"] = round(change_pct, 1)
                    gh["commits_recent_4w"] = recent_4
                    gh["commits_prev_4w"] = prev_4
                    if change_pct > 30:
                        gh["dev_trend"] = "爆发式增长"
                    elif change_pct > 10:
                        gh["dev_trend"] = "稳步上升"
                    elif change_pct < -20:
                        gh["dev_trend"] = "明显停滞"
                    else:
                        gh["dev_trend"] = "平稳"

        if gh:
            profile["github"] = gh


def _add_raises(conn, asset_id: int, profile: dict):
    """近 2 年融资历史。"""
    import psycopg.rows
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            SELECT round, raise_date, amount, lead_investors,
                   other_investors, valuation, sector, chains
            FROM biz.asset_raises
            WHERE asset_id = %s
              AND raise_date >= NOW() - INTERVAL '5 years'
            ORDER BY raise_date DESC
            LIMIT 10
        """, (asset_id,))
        rows = cur.fetchall()
        if not rows:
            return

        raises = []
        for r in rows:
            item = {
                "round": r["round"],
                "date": str(r["raise_date"]) if r["raise_date"] else "",
            }
            if r["amount"] is not None:
                item["amount_usd"] = _safe_float(r["amount"])
            if r["valuation"] is not None:
                item["valuation_usd"] = _safe_float(r["valuation"])
            if r["lead_investors"]:
                item["lead_investors"] = list(r["lead_investors"])[:5]
            if r["sector"]:
                item["sector"] = r["sector"]
            raises.append(item)

        profile["raises_recent_5y"] = raises


def _add_catalysts(conn, asset_id: int, profile: dict):
    """近 30 天 + 未来 60 天催化剂事件（币安新闻等官方源）。"""
    import psycopg.rows
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            SELECT ac.catalyst_id, ac.title, ac.published_at,
                   ac.event_category, ac.event_subcategory,
                   ac.source_code, ac.source_url,
                   ac.ai_event_type, ac.ai_sentiment, ac.ai_summary,
                   ac.share_count,
                   ci.impact_direction, ci.impact_strength, ci.horizon_days
            FROM biz.asset_catalyst ac
            LEFT JOIN biz.catalyst_impact ci
                   ON ci.catalyst_id = ac.catalyst_id
                  AND ci.asset_id = ac.asset_id
            WHERE ac.asset_id = %s
              AND ac.published_at >= NOW() - INTERVAL '30 days'
            ORDER BY ac.published_at DESC
            LIMIT 10
        """, (asset_id,))
        rows = cur.fetchall()
        if not rows:
            return

        catalysts = []
        sentiment_counts = {"positive": 0, "negative": 0, "neutral": 0, "unknown": 0}
        for r in rows:
            item = {
                "title": r["title"][:120] if r["title"] else "",
                "published_at": str(r["published_at"])[:19] if r["published_at"] else "",
                "source": r["source_code"],
            }
            if r["event_category"]:
                item["category"] = r["event_category"]
            if r["event_subcategory"]:
                item["subcategory"] = r["event_subcategory"]
            if r["ai_event_type"]:
                item["ai_event_type"] = r["ai_event_type"]
            if r["ai_sentiment"]:
                item["ai_sentiment"] = r["ai_sentiment"]
                sent = r["ai_sentiment"].lower()
                if sent in sentiment_counts:
                    sentiment_counts[sent] += 1
                else:
                    sentiment_counts["unknown"] += 1
            if r["ai_summary"]:
                item["ai_summary"] = r["ai_summary"][:200]
            if r["impact_direction"]:
                item["impact_direction"] = r["impact_direction"]
            if r["impact_strength"]:
                item["impact_strength"] = r["impact_strength"]
            if r["horizon_days"] is not None:
                item["horizon_days"] = int(r["horizon_days"])
            if r["share_count"] is not None:
                item["share_count"] = int(r["share_count"])
            if r["source_url"]:
                item["source_url"] = r["source_url"]
            catalysts.append(item)

        result = {"events": catalysts, "sentiment_counts": sentiment_counts}
        profile["catalysts_near"] = result


def _add_hacks(conn, asset_id: int, profile: dict):
    """历史安全事件（近 2 年）。"""
    import psycopg.rows
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            SELECT name, technique, amount, returned_funds,
                   hack_date, chain, target_type, bridge_hack
            FROM biz.asset_hacks
            WHERE asset_id = %s
              AND hack_date >= NOW() - INTERVAL '5 years'
            ORDER BY hack_date DESC
            LIMIT 5
        """, (asset_id,))
        rows = cur.fetchall()
        if not rows:
            return

        hacks = []
        for r in rows:
            item = {
                "name": r["name"],
                "date": str(r["hack_date"]) if r["hack_date"] else "",
                "technique": r["technique"],
                "amount_usd": _safe_float(r["amount"]),
            }
            if r["returned_funds"] is not None:
                item["returned_usd"] = _safe_float(r["returned_funds"])
            if r["bridge_hack"] is not None:
                item["is_bridge_hack"] = bool(r["bridge_hack"])
            hacks.append(item)

        profile["security_hacks_5y"] = hacks


def _add_risk_summary(conn, asset_id: int, profile: dict):
    """风险维度汇总：从已有数据中提炼风险指标，
    同时明确标注哪些风险维度当前数据缺失。
    """
    risk = {}

    # ── 筹码风险（从持仓集中度）──
    holders = profile.get("onchain_holders", {})
    top10 = holders.get("top10_pct")
    if top10 is not None:
        risk["top10_concentration_pct"] = top10
        if top10 >= 70:
            risk["concentration_risk"] = "高"
        elif top10 >= 50:
            risk["concentration_risk"] = "中高"
        elif top10 >= 30:
            risk["concentration_risk"] = "中"
        else:
            risk["concentration_risk"] = "低"

    # ── 安全风险（从黑客历史）──
    hacks = profile.get("security_hacks_5y", [])
    if hacks:
        risk["hacks_5y_count"] = len(hacks)
        total_lost = sum(h.get("amount_usd", 0) or 0 for h in hacks)
        risk["hacks_total_loss_usd"] = round(total_lost, 2)
        if total_lost >= 100_000_000:
            risk["security_risk"] = "高（历史大额被盗）"
        elif total_lost >= 10_000_000:
            risk["security_risk"] = "中（有被盗记录）"
        else:
            risk["security_risk"] = "低（小额历史事件）"

    # ── 衍生品风险（从资金费率和 OI）──
    der = profile.get("derivatives", {})
    if der.get("funding_rate_pct") is not None:
        fr = der["funding_rate_pct"]
        risk["funding_rate_pct"] = fr
        if abs(fr) > 0.1:
            risk["funding_rate_risk"] = "高（极端多空失衡）"
        elif abs(fr) > 0.05:
            risk["funding_rate_risk"] = "中"
        else:
            risk["funding_rate_risk"] = "低"
    if der.get("total_oi_usd") and profile.get("market", {}).get("market_cap_usd"):
        oi_ratio = der["total_oi_usd"] / profile["market"]["market_cap_usd"] * 100
        risk["oi_to_mcap_pct"] = round(oi_ratio, 2)
        if oi_ratio > 15:
            risk["oi_risk"] = "高（衍生品占市值比例大）"
        elif oi_ratio > 5:
            risk["oi_risk"] = "中"
        else:
            risk["oi_risk"] = "低"

    # ── 波动率风险（从技术指标）──
    tech = profile.get("price_technical", {})
    if tech.get("volatility_30d_pct") is not None:
        vol = tech["volatility_30d_pct"]
        risk["volatility_30d_pct"] = vol
        if vol > 10:
            risk["volatility_risk"] = "高"
        elif vol > 5:
            risk["volatility_risk"] = "中"
        else:
            risk["volatility_risk"] = "低"

    # ── 解锁风险（从 tokenomics/unlocks）──
    unlocks = profile.get("unlocks", {})
    if unlocks.get("next_30d_unlock_pct_of_supply") is not None:
        pct = unlocks["next_30d_unlock_pct_of_supply"]
        risk["unlock_30d_pct"] = pct
        if pct > 5:
            risk["unlock_risk"] = "高（30天内大额解锁）"
        elif pct > 1:
            risk["unlock_risk"] = "中"
        else:
            risk["unlock_risk"] = "低"

    # ── 消息面风险（从催化剂负面数）──
    cat = profile.get("catalysts_near", {})
    if isinstance(cat, dict) and cat.get("sentiment_counts"):
        sc = cat["sentiment_counts"]
        total = sum(sc.values())
        if total > 0:
            neg_ratio = sc.get("negative", 0) / total
            risk["catalyst_negative_ratio"] = round(neg_ratio, 2)
            if neg_ratio > 0.5:
                risk["news_risk"] = "高（负面消息为主）"
            elif neg_ratio > 0.2:
                risk["news_risk"] = "中（有负面消息）"
            else:
                risk["news_risk"] = "低"

    # ── 明确标注当前数据无法覆盖的风险维度 ──
    missing = []
    # BTC/ETH 大盘相关性：从 asset_market_daily 计算
    import psycopg.rows
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        # 找 BTC 的 asset_id
        cur.execute("""
            SELECT asset_id FROM core.asset
            WHERE canonical_symbol = 'BTC'
            LIMIT 1
        """)
        btc_row = cur.fetchone()
        if not btc_row:
            missing.append("BTC相关性（未找到BTC资产ID）")
        else:
            btc_id = btc_row["asset_id"]
            # 检查两个资产都有多少天数据
            cur.execute("""
                SELECT market_date, price_usd
                FROM biz.asset_market_daily
                WHERE asset_id = %s
                  AND market_date >= NOW() - INTERVAL '90 days'
                ORDER BY market_date ASC
            """, (asset_id,))
            asset_rows = cur.fetchall()
            cur.execute("""
                SELECT market_date, price_usd
                FROM biz.asset_market_daily
                WHERE asset_id = %s
                  AND market_date >= NOW() - INTERVAL '90 days'
                ORDER BY market_date ASC
            """, (btc_id,))
            btc_rows_list = cur.fetchall()

            if len(asset_rows) < 20 or len(btc_rows_list) < 20:
                missing.append("BTC相关性（价格数据不足）")
            else:
                # 对齐日期
                btc_dict = {str(r["market_date"]): _safe_float(r["price_usd"]) for r in btc_rows_list}
                aligned = []
                for r in asset_rows:
                    d = str(r["market_date"])
                    ap = _safe_float(r["price_usd"])
                    bp = btc_dict.get(d)
                    if ap is not None and bp is not None and ap > 0 and bp > 0:
                        aligned.append((ap, bp))

                if len(aligned) < 20:
                    missing.append("BTC相关性（日期对齐不足）")
                else:
                    # 计算收益率相关性（取最近 30 条有效日）
                    a_rets = []
                    b_rets = []
                    for i in range(1, len(aligned)):
                        a_rets.append((aligned[i][0] - aligned[i-1][0]) / aligned[i-1][0])
                        b_rets.append((aligned[i][1] - aligned[i-1][1]) / aligned[i-1][1])
                    # 只取最近 30 天
                    a_rets = a_rets[-30:]
                    b_rets = b_rets[-30:]
                    n = len(a_rets)
                    if n < 10:
                        missing.append("BTC相关性（有效样本不足）")
                    else:
                        mean_a = sum(a_rets) / n
                        mean_b = sum(b_rets) / n
                        cov = sum((a_rets[i] - mean_a) * (b_rets[i] - mean_b) for i in range(n))
                        var_a = sum((r - mean_a) ** 2 for r in a_rets)
                        var_b = sum((r - mean_b) ** 2 for r in b_rets)
                        if var_a <= 0 or var_b <= 0:
                            missing.append("BTC相关性（波动率为零）")
                        else:
                            corr = cov / ((var_a * var_b) ** 0.5)
                            risk["btc_correlation_30d"] = round(corr, 3)
                            if abs(corr) < 0.3:
                                risk["btc_correlation_level"] = "低（相对独立行情）"
                            elif abs(corr) < 0.6:
                                risk["btc_correlation_level"] = "中"
                            else:
                                risk["btc_correlation_level"] = "高（跟随大盘）"

    # 其他数据完全缺失的维度
    missing.extend([
        "宏观流动性（美元指数/流动性指标）",
        "监管合规风险（DeFi 监管状态）",
        "协议安全审计报告",
        "订单簿深度（现货买卖盘挂单）",
        "地址标签智能分类（Smart Money/做市商/团队）",
    ])

    if missing:
        risk["data_gaps"] = missing

    if risk:
        profile["risk_summary"] = risk


def _add_kol_signals(conn, asset_id: int, profile: dict):
    """近 7 天 KOL 信号（预测 + 分析类），含发帖正文。"""
    import psycopg.rows
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            SELECT ks.post_type, ks.direction, ks.confidence,
                   ks.entry_price, ks.stop_loss, ks.take_profit,
                   ks.entry_condition, ks.support_level, ks.resistance_level,
                   ks.leverage, ks.signal_category, ks.signal_subtype,
                   ks.created_at as signal_time,
                   kp.nickname, kp.platform_code, kp.follower_count, kp.win_rate,
                   kpo.content_text, kpo.post_url, kpo.posted_at,
                   array_length(kpo.image_urls, 1) as image_count
            FROM biz.kol_signal ks
            JOIN biz.kol_profile kp ON kp.profile_id = ks.profile_id
            JOIN biz.kol_post kpo ON kpo.post_id = ks.post_id
            WHERE ks.asset_id = %s
              AND ks.created_at >= NOW() - INTERVAL '7 days'
              AND ks.post_type IN ('prediction', 'analysis')
            ORDER BY ks.confidence DESC, ks.created_at DESC
            LIMIT 10
        """, (asset_id,))
        rows = cur.fetchall()
        if not rows:
            return

        signals = []
        long_count = 0
        short_count = 0
        neutral_count = 0
        for r in rows:
            item = {
                "kol": r["nickname"],
                "platform": r["platform_code"],
                "type": r["post_type"],
                "direction": r["direction"],
                "confidence": _safe_float(r["confidence"]),
                "time": str(r["signal_time"])[:16],
            }
            if r["follower_count"]:
                item["followers"] = int(r["follower_count"])
            if r["win_rate"] is not None:
                item["win_rate"] = _safe_float(r["win_rate"])
            # 发帖正文（截断到 300 字，省 token）
            if r["content_text"]:
                content = r["content_text"].strip()
                if len(content) > 300:
                    content = content[:300] + "..."
                item["content"] = content
            # 交易信号详情（如果有）
            if r["entry_price"] is not None:
                item["entry_price"] = _safe_float(r["entry_price"])
            if r["stop_loss"] is not None:
                item["stop_loss"] = _safe_float(r["stop_loss"])
            if r["take_profit"] is not None:
                item["take_profit"] = _safe_float(r["take_profit"])
            if r["leverage"] is not None:
                item["leverage"] = _safe_float(r["leverage"])
            if r["entry_condition"]:
                item["entry_condition"] = r["entry_condition"][:150]
            if r["signal_category"]:
                item["category"] = r["signal_category"]
            if r["signal_subtype"]:
                item["subtype"] = r["signal_subtype"]
            if r["post_url"]:
                item["post_url"] = r["post_url"]
            if r["image_count"] and r["image_count"] > 0:
                item["has_image"] = True
            signals.append(item)
            if r["direction"] == "long":
                long_count += 1
            elif r["direction"] == "short":
                short_count += 1
            elif r["direction"] == "neutral":
                neutral_count += 1

        kol_summary = {
            "total_signals_7d": len(signals),
            "long_count": long_count,
            "short_count": short_count,
            "neutral_count": neutral_count,
            "signals": signals,
        }
        # 只基于有明确多空方向的信号计算情绪
        directional = long_count + short_count
        if directional == 0:
            kol_summary["sentiment"] = "无明确多空方向"
        else:
            ratio = long_count / directional
            kol_summary["sentiment"] = (
                "极度看多" if ratio > 0.8 else
                "偏多" if ratio > 0.6 else
                "中性偏多" if ratio > 0.55 else
                "多空平衡" if ratio >= 0.45 else
                "中性偏空" if ratio >= 0.4 else
                "偏空" if ratio > 0.2 else
                "极度看空"
            )
            kol_summary["long_ratio"] = round(ratio, 3)

        # 重要提示：方向标签可能与内容不一致，需 AI 自行判断正文观点
        kol_summary["note"] = (
            "方向标签(direction)由系统自动标注，可能与发帖正文实际观点不一致；"
            "请结合 content 正文内容独立判断 KOL 的真实多空倾向。"
        )

        profile["kol_signals_7d"] = kol_summary


# ══════════════════════════════════════════════════════════════
# AI 信号精选 V2 — 全量画像 + 信号分类过滤 + 结构化评分卡
# ══════════════════════════════════════════════════════════════

# V2 缓存（独立于 V1）
_ai_v2_cache: dict[str, dict] = {}
AI_V2_CACHE_TTL = 3600  # 1 小时


def _get_v2_cache_key(asset_id: int, signal_types: list[str]) -> str:
    return f"v2:{asset_id}:{','.join(sorted(signal_types))}"


def load_ai_signal_rules(config_path: str | None = None) -> dict:
    """加载 AI 信号分类规则（从 yaml 配置）。"""
    import yaml
    if config_path is None:
        config_path = Path(__file__).parent / "market_rules.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        rules = yaml.safe_load(f)
    opp = rules.get("opportunity_rules", {})
    return {
        "event_driven": set(opp.get("ai_event_driven_signals", [])),
        "slow_variable": set(opp.get("ai_slow_variable_signals", [])),
        "max_review_per_run": opp.get("ai_max_review_per_run", 15),
        "slow_min_resonance": opp.get("ai_slow_min_resonance", 2),
    }


def should_send_to_ai(signals: list[dict], rules: dict | None = None) -> tuple[bool, str]:
    """
    判断一个代币的信号集合是否值得送 AI 分析。

    规则：
    - 有任一事件驱动类信号 → 直接送 AI
    - 全是慢变量类 → 信号类型数 >= slow_min_resonance 才送
    - 混合（事件驱动 + 慢变量）→ 送 AI（事件驱动触发）

    Returns:
        (should_send, reason)
    """
    if not signals:
        return False, "无信号"

    if rules is None:
        rules = load_ai_signal_rules()

    event_driven_set = rules["event_driven"]
    slow_set = rules["slow_variable"]
    slow_min = rules["slow_min_resonance"]

    # 收集信号类型（去重）
    signal_types = set()
    for s in signals:
        st = s.get("signal_type") or s.get("type")
        if st:
            signal_types.add(st)

    if not signal_types:
        return False, "无有效信号类型"

    # 分类
    event_types = signal_types & event_driven_set
    slow_types = signal_types & slow_set
    other_types = signal_types - event_driven_set - slow_set

    if event_types:
        return True, f"事件驱动信号触发: {', '.join(sorted(event_types))}"

    if other_types:
        # 有未分类的信号类型，保守起见也送（可能是新信号类型）
        return True, f"含未分类信号: {', '.join(sorted(other_types))}"

    # 全是慢变量类
    if len(slow_types) >= slow_min:
        return True, f"慢变量共振: {len(slow_types)}种 ({', '.join(sorted(slow_types))})"
    else:
        return False, f"慢变量信号不足: {len(slow_types)}种（需要{slow_min}种）"


def analyze_asset_v2(
    asset_id: int,
    asset_signals: list[dict],
    conn=None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """
    V2 版单币 AI 分析：用全量画像替代零散数据提取。

    输入：
        asset_id: 代币 ID
        asset_signals: 该代币触发的所有信号
        conn: 数据库连接（可选，不传则新建）
        force_refresh: 强制刷新缓存

    输出（结构化评分卡）：
        {
          "should_highlight": bool,
          "should_risk": bool,
          "direction": "long"|"short"|"neutral",
          "overall_score": 0-100,          # 综合评分
          "confidence": "HIGH"|"MED"|"LOW",
          "score_card": {                   # 六维度打分
            "valuation": { "score": 0-100, "comment": "..." },
            "technical": { "score": 0-100, "comment": "..." },
            "onchain": { "score": 0-100, "comment": "..." },
            "fundamental": { "score": 0-100, "comment": "..." },
            "sentiment": { "score": 0-100, "comment": "..." },
            "catalyst": { "score": 0-100, "comment": "..." },
          },
          "reason_summary": str,            # 一句话理由
          "reason_detail": str,             # 详细分析
          "key_drivers": [str],             # 关键驱动因子
          "risk_warnings": [str],           # 风险提示
          "suggested_horizon": "short"|"medium"|"long",
          "investment_logic": str,          # 投资逻辑/交易思路
          "analysis_ts": float,
          "from_cache": bool,
          "error": str | None,
        }
    """
    signal_types = sorted(set(s.get("signal_type", "") for s in asset_signals if s.get("signal_type")))
    cache_key = _get_v2_cache_key(asset_id, signal_types)

    # 缓存命中
    if not force_refresh and cache_key in _ai_v2_cache:
        cached = _ai_v2_cache[cache_key]
        if time.time() - cached["ts"] < AI_V2_CACHE_TTL:
            return {**cached["result"], "from_cache": True}

    try:
        # 1. 构建全量画像
        profile = build_asset_profile(asset_id, conn=conn)

        # 2. 调用 LLM
        result = _call_llm_analysis_v2(profile, asset_signals)
        result["from_cache"] = False
        result["analysis_ts"] = time.time()

        # 3. 写缓存
        _ai_v2_cache[cache_key] = {"result": result, "ts": time.time()}
        # 限制缓存大小
        if len(_ai_v2_cache) > 100:
            oldest_key = min(_ai_v2_cache.keys(), key=lambda k: _ai_v2_cache[k]["ts"])
            del _ai_v2_cache[oldest_key]

        return result
    except Exception as e:
        error_result = {
            "should_highlight": False,
            "should_risk": False,
            "direction": "neutral",
            "overall_score": 0,
            "confidence": "LOW",
            "score_card": {},
            "reason_summary": f"AI分析失败: {str(e)[:50]}",
            "reason_detail": str(e),
            "key_drivers": [],
            "risk_warnings": [],
            "suggested_horizon": "medium",
            "investment_logic": "",
            "analysis_ts": time.time(),
            "from_cache": False,
            "error": str(e),
        }
        _ai_v2_cache[cache_key] = {"result": error_result, "ts": time.time()}
        return error_result


def _call_llm_analysis_v2(
    profile: dict,
    asset_signals: list[dict],
) -> dict[str, Any]:
    """V2 版 LLM 调用：全量画像输入 + 结构化评分卡输出。"""
    from crypto_research.config import get_settings
    from crypto_research.clients.llm_client import LLMClient, extract_json_from_llm_response

    settings = get_settings(require_database=False)
    llm = LLMClient(settings, rpm=30, timeout=90)

    if not llm.is_available():
        raise RuntimeError("LLM 不可用（未配置 API Key）")

    system_prompt = _build_system_prompt_v2()
    user_prompt = _build_user_prompt_v2(profile, asset_signals)

    # V2 高亮信号分析：开启思考模式，让 AI 有更充分的推理
    raw = llm.chat(
        system_prompt, user_prompt,
        temperature=0.3,
        max_tokens=8192,
        response_format={"type": "json_object"},
        use_cache=True,
        enable_thinking=True,
    )

    # 追溯日志：保存完整请求/响应/思考过程
    _write_ai_trace(
        tag="signal_v2",
        asset_id=profile.get("asset_id"),
        symbol=profile.get("symbol"),
        signal_types=sorted(set(s.get("signal_type", "") for s in asset_signals if s.get("signal_type"))),
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        raw_response=raw,
        thinking_content=llm.get_last_thinking(),
        provider=getattr(llm, "provider", ""),
        model=getattr(llm, "model", ""),
    )

    data = extract_json_from_llm_response(raw)

    # 标准化输出
    score_card = data.get("score_card") or {}
    # 确保六个维度都有 score
    for dim in ["valuation", "technical", "onchain", "fundamental", "sentiment", "catalyst"]:
        if dim not in score_card:
            score_card[dim] = {"score": 50, "comment": "数据不足"}
        elif isinstance(score_card[dim], dict):
            score_card[dim].setdefault("score", 50)
            score_card[dim].setdefault("comment", "")

    return {
        "should_highlight": bool(data.get("should_highlight", False)),
        "should_risk": bool(data.get("should_risk", False)),
        "direction": str(data.get("direction", "neutral")).lower() or "neutral",
        "overall_score": min(100, max(0, int(float(data.get("overall_score", 0) or 0)))),
        "confidence": str(data.get("confidence", "MED")).upper() or "MED",
        "score_card": score_card,
        "reason_summary": str(data.get("reason_summary", ""))[:200],
        "reason_detail": str(data.get("reason_detail", ""))[:2000],
        "key_drivers": [str(x)[:100] for x in (data.get("key_drivers") or [])][:6],
        "risk_warnings": [str(x)[:100] for x in (data.get("risk_warnings") or [])][:5],
        "suggested_horizon": str(data.get("suggested_horizon", "medium")).lower() or "medium",
        "investment_logic": str(data.get("investment_logic", ""))[:500],
        "error": None,
    }


def _write_ai_trace(
    tag: str,
    asset_id: int | None,
    symbol: str | None,
    signal_types: list[str],
    system_prompt: str,
    user_prompt: str,
    raw_response: str,
    thinking_content: str | None,
    provider: str = "",
    model: str = "",
) -> None:
    """写入 AI 追溯日志（JSONL 格式，按日期分文件）。

    日志内容：完整的 system_prompt、user_prompt、原始响应、思考过程。
    保存位置：workbench/output/ai_trace/{tag}_{YYYY-MM-DD}.jsonl
    """
    import json
    import datetime
    try:
        trace_dir = Path(__file__).parent / "output" / "ai_trace"
        trace_dir.mkdir(parents=True, exist_ok=True)
        date_str = datetime.datetime.now().strftime("%Y-%m-%d")
        trace_file = trace_dir / f"{tag}_{date_str}.jsonl"

        entry = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "tag": tag,
            "asset_id": asset_id,
            "symbol": symbol,
            "signal_types": signal_types,
            "provider": provider,
            "model": model,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "raw_response": raw_response,
            "thinking_content": thinking_content,
        }
        with open(trace_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        # 日志写入失败不影响主流程
        pass


def _build_system_prompt_v2() -> str:
    return """你是一位顶级加密货币投研分析师，擅长从多维度数据中提炼投资机会与风险。

你的任务：给定一个代币的【全量画像数据】（市场/链上/估值/持仓/解锁/衍生品/技术面/社交/开发/催化剂/风险等）和【当前触发的信号】，
综合判断该代币是否值得进入【高亮信号】（看多机会池）或【高危信号】（看空风险池），并输出六维度评分卡。

## 分析框架（六维评分卡）

对以下六个维度分别打分（0-100）并写一句话点评：

1. **估值维度**（valuation）：MVRV 百分位、市值/FDV 比、ROI 历史位置
   - 极度低估 → 高分（80+），极度高估 → 低分（<30）
2. **技术维度**（technical）：价格位置、RSI、波动率、趋势强度
   - 强势突破/趋势健康 → 高分；超买/破位 → 低分
3. **链上维度**（onchain）：活跃地址、交易所资金流、巨鲸动向、持仓集中度
   - 吸筹/地址增长 → 高分；派发/集中度极高 → 低分
4. **基本面维度**（fundamental）：开发活跃度、融资、TVL、协议收入、代币经济
   - 基本面强劲 → 高分；衰退/解锁压力大 → 低分
5. **情绪维度**（sentiment）：KOL 情绪、社交热度、衍生品资金费率
   - 情绪回暖但不狂热 → 高分；极度贪婪或极度恐慌 → 调整分值
6. **催化剂维度**（catalyst）：近期/即将发生的事件、融资、上线、合作
   - 明确正面催化 → 高分；明确负面催化 → 低分

## 综合判断原则

- **多维度共振优先**：3+ 个维度同时同向 → 高置信度
- **估值锚定**：再好的故事，估值极度高估也要谨慎；再差的消息，极度低估也不必恐慌
- **事件驱动权重**：重大催化剂（融资/上线/监管）权重高于慢变量
- **风险收益比**：不仅看方向，更看盈亏比和时间窗口
- **数据诚实**：画像中缺少的数据维度不要瞎编，在点评中说明"数据不足"

## 高亮/高危判定

- **高亮信号**（should_highlight=true）：
  - 综合评分 >= 65，且至少 2 个维度 >= 70 分
  - 或有重大正面催化剂 + 估值不高估
  - 或极度低估 + 链上出现吸筹信号
- **高危信号**（should_risk=true）：
  - 综合评分 <= 35，且至少 2 个维度 <= 30 分
  - 或有重大风险事件（大额解锁/黑客/监管利空）
  - 或极度高估 + 链上出现派发信号
- 两者可以同时为 true（如短期风险但长期机会）
- 都为 false 表示信号不明确

## 输出格式（严格 JSON）

{
  "should_highlight": true/false,
  "should_risk": true/false,
  "direction": "long|short|neutral",
  "overall_score": 0-100,
  "confidence": "HIGH|MED|LOW",
  "score_card": {
    "valuation": {"score": 0-100, "comment": "一句话点评"},
    "technical": {"score": 0-100, "comment": "一句话点评"},
    "onchain": {"score": 0-100, "comment": "一句话点评"},
    "fundamental": {"score": 0-100, "comment": "一句话点评"},
    "sentiment": {"score": 0-100, "comment": "一句话点评"},
    "catalyst": {"score": 0-100, "comment": "一句话点评"}
  },
  "reason_summary": "一句话总结（80字内，用于卡片展示）",
  "reason_detail": "详细分析（400字内）：涵盖核心逻辑、多维度共振分析、关键驱动、风险点",
  "key_drivers": ["驱动因子1", "驱动因子2", ...],
  "risk_warnings": ["风险1", "风险2", ...],
  "suggested_horizon": "short|medium|long",
  "investment_logic": "投资逻辑/交易思路（300字内）"
}"""


def _build_user_prompt_v2(profile: dict, asset_signals: list[dict]) -> str:
    """V2 版 user prompt：输入全量画像 + 触发信号。"""
    parts = []

    # ── 1. 代币基础信息 ──
    basic = profile.get("basic", {})
    parts.append("=== 代币基础信息 ===")
    name = basic.get("name") or basic.get("coin_name") or "?"
    symbol = basic.get("symbol") or basic.get("coin_symbol") or "?"
    sector = basic.get("sector") or basic.get("primary_sector") or "未知"
    rank = basic.get("market_cap_rank")
    parts.append(f"- 代币: {symbol} ({name})")
    parts.append(f"- 赛道: {sector}")
    if rank:
        parts.append(f"- 市值排名: #{rank}")

    market = profile.get("market", {})
    if market.get("price_cm"):
        parts.append(f"- 当前价格: ${market['price_cm']}")
    if market.get("market_cap_usd"):
        mcap = float(market["market_cap_usd"])
        if mcap >= 1e9:
            parts.append(f"- 市值: ${mcap/1e9:.2f}B")
        elif mcap >= 1e6:
            parts.append(f"- 市值: ${mcap/1e6:.1f}M")

    # ── 2. 价格技术面 ──
    tech = profile.get("price_technical", {})
    if tech:
        parts.append("")
        parts.append("=== 价格技术面 ===")
        if tech.get("rsi_14") is not None:
            parts.append(f"- RSI(14): {tech['rsi_14']} ({tech.get('rsi_signal', '')})")
        if tech.get("volatility_30d_pct") is not None:
            parts.append(f"- 30日波动率: {tech['volatility_30d_pct']}%")
        if tech.get("price_position_30d_pct") is not None:
            parts.append(f"- 30日价格位置: {tech['price_position_30d_pct']}%")
        if tech.get("price_position_90d_pct") is not None:
            parts.append(f"- 90日价格位置: {tech['price_position_90d_pct']}%")
        if tech.get("change_7d_pct") is not None:
            parts.append(f"- 7日涨跌幅: {tech['change_7d_pct']:+.2f}%")
        if tech.get("change_30d_pct") is not None:
            parts.append(f"- 30日涨跌幅: {tech['change_30d_pct']:+.2f}%")

    # ── 3. 估值状态 ──
    val = profile.get("valuation", {})
    if val:
        parts.append("")
        parts.append("=== 估值状态 ===")
        if val.get("mvrv_zone"):
            parts.append(f"- MVRV区间: {val['mvrv_zone']}")
        if val.get("mvrv_percentile") is not None:
            parts.append(f"- MVRV百分位: {val['mvrv_percentile']}%")
        if val.get("roi_30d_percentile") is not None:
            parts.append(f"- 30日ROI百分位: {val['roi_30d_percentile']}%")
        if val.get("active_addr_percentile") is not None:
            parts.append(f"- 活跃地址百分位: {val['active_addr_percentile']}%")
        if val.get("exchange_inflow_percentile") is not None:
            parts.append(f"- 交易所流入百分位: {val['exchange_inflow_percentile']}%")
        if val.get("exchange_outflow_percentile") is not None:
            parts.append(f"- 交易所流出百分位: {val['exchange_outflow_percentile']}%")

    # ── 4. 链上数据 ──
    onchain = profile.get("onchain", {})
    cm = onchain.get("cm_metrics", {}) if isinstance(onchain, dict) else {}
    if cm:
        parts.append("")
        parts.append("=== 链上数据（CoinMetrics） ===")
        if cm.get("active_addresses_24h"):
            parts.append(f"- 24h活跃地址: {cm['active_addresses_24h']:,}")
        if cm.get("balance_addresses"):
            parts.append(f"- 持币地址数: {cm['balance_addresses']:,}")
        if cm.get("exchange_net_flow_usd_24h") is not None:
            nf = cm["exchange_net_flow_usd_24h"]
            direction = "净流入" if nf > 0 else "净流出"
            parts.append(f"- 24h交易所{direction}: ${abs(nf)/1e6:.2f}M")
        if cm.get("mvrv_ratio") is not None:
            parts.append(f"- MVRV比率: {cm['mvrv_ratio']}")

    # ── 5. 持仓结构 ──
    holders = profile.get("onchain_holders", {})
    if holders:
        parts.append("")
        parts.append("=== 持仓结构 ===")
        if holders.get("top10_pct") is not None:
            parts.append(f"- Top10集中度: {holders['top10_pct']}%")
        if holders.get("total_holders"):
            parts.append(f"- 总持币地址: {holders['total_holders']:,}")
        if holders.get("holder_change_7d") is not None:
            hc = holders["holder_change_7d"]
            parts.append(f"- 7日地址变化: {hc:+d}")
        if holders.get("whale_change_7d_pct") is not None:
            parts.append(f"- 巨鲸7日持仓变化: {holders['whale_change_7d_pct']:+.2f}%")
        # 地址分类（如果有）
        for label in ["exchange_pct", "vc_pct", "smart_money_pct", "retail_pct"]:
            if holders.get(label) is not None:
                label_zh = {"exchange_pct": "交易所", "vc_pct": "VC",
                           "smart_money_pct": "Smart Money", "retail_pct": "散户"}[label]
                parts.append(f"- {label_zh}持仓占比: {holders[label]}%")

    # ── 6. 大额转账（7日）──
    transfers = profile.get("onchain_transfers_7d", {})
    if transfers and transfers.get("total_transfers", 0) > 0:
        parts.append("")
        parts.append("=== 近7日大额转账 ===")
        parts.append(f"- 转账笔数: {transfers.get('total_transfers', 0)}")
        parts.append(f"- 总金额: ${transfers.get('total_value_usd', 0)/1e6:.2f}M")
        if transfers.get("net_flow_direction"):
            parts.append(f"- 净额方向: {transfers['net_flow_direction']}")
        if transfers.get("to_exchange_count") is not None:
            parts.append(f"- 转入交易所: {transfers['to_exchange_count']}笔")
        if transfers.get("from_exchange_count") is not None:
            parts.append(f"- 转出交易所: {transfers['from_exchange_count']}笔")

    # ── 7. 解锁 & 代币经济 ──
    unlocks = profile.get("unlocks", {})
    if unlocks:
        parts.append("")
        parts.append("=== 解锁数据 ===")
        if unlocks.get("next_unlock_date"):
            parts.append(f"- 下次解锁: {unlocks['next_unlock_date']}")
        if unlocks.get("next_unlock_pct") is not None:
            parts.append(f"- 下次解锁占比: {unlocks['next_unlock_pct']}%")
        if unlocks.get("next_30d_unlock_pct_of_supply") is not None:
            parts.append(f"- 30天内解锁占比: {unlocks['next_30d_unlock_pct_of_supply']}%")

    tokenomics = profile.get("tokenomics", {})
    if tokenomics:
        parts.append("")
        parts.append("=== 代币经济学 ===")
        for k, v in tokenomics.items():
            if k != "asset_id":
                parts.append(f"- {k}: {v}")

    # ── 8. 衍生品 ──
    der = profile.get("derivatives", {})
    if der:
        parts.append("")
        parts.append("=== 衍生品数据 ===")
        if der.get("funding_rate_pct") is not None:
            parts.append(f"- 资金费率: {der['funding_rate_pct']}%")
        if der.get("total_oi_usd"):
            parts.append(f"- 未平仓合约: ${der['total_oi_usd']/1e6:.2f}M")
        if der.get("oi_change_24h_pct") is not None:
            parts.append(f"- OI 24h变化: {der['oi_change_24h_pct']:+.2f}%")
        if der.get("cvd_24h_usd") is not None:
            parts.append(f"- 24h CVD: ${der['cvd_24h_usd']/1e6:.2f}M")

    # ── 9. 协议 TVL（DeFi）──
    tvl = profile.get("protocol_tvl", {})
    if tvl:
        parts.append("")
        parts.append("=== 协议 TVL ===")
        if tvl.get("tvl_usd"):
            parts.append(f"- TVL: ${tvl['tvl_usd']/1e6:.2f}M")
        if tvl.get("tvl_change_7d_pct") is not None:
            parts.append(f"- 7日TVL变化: {tvl['tvl_change_7d_pct']:+.2f}%")
        if tvl.get("tvl_to_market_cap_ratio") is not None:
            parts.append(f"- TVL/市值比: {tvl['tvl_to_market_cap_ratio']}")

    # ── 10. 社交热度 & KOL 信号 ──
    social = profile.get("social", {})
    if social:
        parts.append("")
        parts.append("=== 社交热度 ===")
        for k, v in social.items():
            if k != "asset_id" and v is not None:
                parts.append(f"- {k}: {v}")

    kol = profile.get("kol_signals_7d", {})
    if kol and kol.get("signal_count", 0) > 0:
        parts.append("")
        parts.append(f"=== 近7日 KOL 信号（{kol.get('signal_count', 0)}条） ===")
        if kol.get("sentiment"):
            parts.append(f"- 情绪倾向: {kol['sentiment']}")
        if kol.get("bull_count") is not None and kol.get("bear_count") is not None:
            parts.append(f"- 多空比: {kol['bull_count']}多 / {kol['bear_count']}空")
        # 列几条有代表性的
        if kol.get("sample_signals"):
            for i, s in enumerate(kol["sample_signals"][:5], 1):
                content = (s.get("content") or s.get("title") or "")[:80]
                sent = s.get("sentiment") or s.get("direction") or ""
                parts.append(f"  {i}. [{sent}] {content}")

    # ── 11. 开发活跃度 ──
    github = profile.get("github_activity", {})
    if github and github.get("repo_count", 0) > 0:
        parts.append("")
        parts.append("=== 开发活跃度 ===")
        if github.get("total_stars"):
            parts.append(f"- Star总数: {github['total_stars']}")
        if github.get("total_commits_30d") is not None:
            parts.append(f"- 30日提交数: {github['total_commits_30d']}")
        if github.get("active_devs_30d") is not None:
            parts.append(f"- 30日活跃开发者: {github['active_devs_30d']}人")

    # ── 12. 催化剂事件 ──
    cat = profile.get("catalysts_near", {})
    cat_events = cat.get("events", []) if isinstance(cat, dict) else cat
    if cat_events:
        parts.append("")
        parts.append(f"=== 近期催化剂（{len(cat_events)}条） ===")
        for i, c in enumerate(cat_events[:8], 1):
            date = c.get("published_at", "")[:10]
            senti = c.get("ai_sentiment", "")
            title = (c.get("title") or c.get("ai_summary") or "")[:80]
            parts.append(f"  {i}. {date} [{senti}] {title}")

    # ── 13. 风险汇总 ──
    risk = profile.get("risk_summary", {})
    if risk:
        parts.append("")
        parts.append("=== 风险提示 ===")
        for k, v in risk.items():
            if k == "data_gaps":
                continue
            if isinstance(v, (int, float, str)):
                parts.append(f"- {k}: {v}")
        if risk.get("data_gaps"):
            parts.append(f"- 数据缺口: {', '.join(risk['data_gaps'][:3])}")

    # ── 14. 当前触发的信号 ──
    parts.append("")
    parts.append(f"=== 当前触发的信号（共 {len(asset_signals)} 条） ===")
    for i, s in enumerate(asset_signals[:20], 1):
        stype = s.get("signal_type") or s.get("type") or "unknown"
        direction = s.get("direction", "")
        tier = s.get("conviction_tier") or s.get("confidence") or "MED"
        score = s.get("conviction_score") or s.get("score") or 0
        title = s.get("title") or s.get("trigger_logic") or s.get("reason", "")
        desc = str(title)[:100]
        parts.append(f"  {i}. [{stype}] 方向={direction} 强度={tier}({score}分) - {desc}")
    if len(asset_signals) > 20:
        parts.append(f"  ... 还有 {len(asset_signals) - 20} 条信号")

    parts.append("")
    parts.append("请基于以上全量画像和触发信号，输出你的六维度评分卡和综合判断。")

    return "\n".join(parts)


def ai_enrich_signals_v2(
    signals_by_asset: list[dict],
    direction: str = "long",
    max_ai_review: int | None = None,
) -> list[dict]:
    """
    V2 版 AI 信号增强：
    1. 先按信号分类过滤（事件驱动直接送，慢变量需要共振）
    2. 对符合条件的代币用全量画像做 AI 分析
    3. 按 AI 综合评分排序，取消类型配额

    Args:
        signals_by_asset: 按代币合并后的信号列表（每条约含 asset_id + all_signals）
        direction: "long"（高亮机会）或 "short"（高危风险）
        max_ai_review: 最多 AI 分析多少个（不传则用 yaml 配置）

    Returns:
        增强后的信号列表，每条增加 ai_analysis_v2 字段
    """
    if not signals_by_asset:
        return []

    rules = load_ai_signal_rules()
    if max_ai_review is None:
        max_ai_review = rules["max_review_per_run"]

    # 第一步：过滤哪些该送 AI
    import re
    _symbol_re = re.compile(r'^[A-Z0-9]{2,10}$')
    to_analyze = []
    skipped = []
    for sig in signals_by_asset:
        all_signals = sig.get("all_signals") or [sig]
        should_send, reason = should_send_to_ai(all_signals, rules)
        sig["_ai_filter_reason"] = reason
        # 聚合/宏观类信号（无 asset_id 且非单一币种）不送全量画像分析，
        # 避免 LLM 因数据不足误判降级，保持其原始排名
        target = (sig.get("target") or "").strip()
        has_valid_asset = bool(sig.get("asset_id")) or bool(_symbol_re.match(target.upper()))
        if should_send and has_valid_asset:
            to_analyze.append(sig)
        else:
            if not has_valid_asset:
                sig["_ai_filter_reason"] = "聚合/宏观信号，跳过全量画像"
            skipped.append(sig)

    # 成本控制：只取前 N 个（按原规则信念分排序）
    to_analyze.sort(key=lambda s: s.get("conviction_score", 0) or 0, reverse=True)
    to_analyze = to_analyze[:max_ai_review]

    # 第二步：逐个做 AI 分析
    enriched = []
    for sig in to_analyze:
        asset_id = sig.get("asset_id", 0)
        all_signals = sig.get("all_signals") or [sig]
        ai_result = analyze_asset_v2(asset_id, all_signals)
        sig = {**sig, "ai_analysis_v2": ai_result}

        # 方向校验：如果 AI 不认可该方向，标记降级
        if direction == "long" and not ai_result.get("should_highlight") and not ai_result.get("error"):
            sig["_ai_downgraded"] = True
        elif direction == "short" and not ai_result.get("should_risk") and not ai_result.get("error"):
            sig["_ai_downgraded"] = True

        enriched.append(sig)

    # 第三步：全量统一排序（AI认可的排前，被降级的和未送AI的按基础分混排）
    def _sort_key(s):
        ai_score = s.get("ai_analysis_v2", {}).get("overall_score", 0) or 0
        base_score = s.get("conviction_score", 0) or 0
        has_v2 = bool(s.get("ai_analysis_v2") and not s.get("ai_analysis_v2", {}).get("error"))
        downgraded = 1 if s.get("_ai_downgraded") else 0

        if direction == "long":
            # 排序优先级：AI认可且不降级 > 基础分 > AI分
            # - 第1层：AI认可（有V2分析且没被降级）的排最前
            # - 第2层：按混合分排序（有V2的用混合分，没V2的用基础分）
            ai_approved = 1 if (has_v2 and not downgraded) else 0
            mixed = ai_score * 0.5 + base_score * 0.5 if has_v2 else base_score
            return (ai_approved, mixed, ai_score)
        else:
            # 风险信号对称
            ai_approved = 1 if (has_v2 and not downgraded) else 0
            risk_score = 100 - ai_score if ai_score > 0 else 0
            mixed = risk_score * 0.5 + base_score * 0.5 if has_v2 else base_score
            return (ai_approved, mixed, base_score)

    all_signals = enriched + skipped
    all_signals.sort(key=_sort_key, reverse=True)
    return all_signals
