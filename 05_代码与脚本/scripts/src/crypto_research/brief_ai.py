"""早报 AI 叙事层：把结构化 brief 压缩后喂 LLM，生成人话解读。

方案 B（混合叙事层）：
- 确定性评分/机会清单原样保留
- 仅新增一段 AI 叙事：TL;DR + 逐模块解读 + 策略暗示
- 降级铁律：LLM 不可用/异常 → 省略 AI 段，结构化早报照发

依赖：LLMClient（Ark 优先，OpenAI/DeepSeek 兜底）
"""
from __future__ import annotations

SYSTEM_PROMPT = """你是一位硬核加密数据分析师。根据以下结构化大盘数据，撰写简洁的市场早报解读。

要求：
1. 一句话 TL;DR（≤30字，点明今日核心信号）
2. 逐模块解读（为什么这些数值重要、互相印证还是背离）
3. 策略暗示（仅基于数据推导，标注「非投资建议」+风险提示）
4. 中文，markdown 格式
5. 禁止编造 brief 中不存在的数据或事件
6. 如有降级项，简要说明哪些维度缺失及影响
7. 控制在 800 字以内"""


def build_brief_context(brief: dict) -> str:
    """将 brief dict 压缩为紧凑纯文本摘要（目标 <4KB）。"""
    parts: list[str] = []

    # M0 头部（dict: btc_price, fear_greed, btc_cycle_phase, summary）
    m0 = brief.get("M0_tldr") or {}
    if isinstance(m0, dict) and m0:
        parts.append(f"[M0 头部] BTC={m0.get('btc_price','?')} | 恐贪={m0.get('fear_greed','?')}({m0.get('fear_greed_label','')}) | "
                     f"周期={m0.get('btc_cycle_phase','?')} | MVRV={m0.get('btc_mvrv_pct','?')}% | "
                     f"总市值={m0.get('total_market_cap','?')} | 主导率={m0.get('btc_dominance','?')}")
        if m0.get("summary"):
            parts.append(f"  摘要: {m0['summary']}")
    elif isinstance(m0, str) and m0:
        parts.append(f"[M0 头部] {m0}")

    # M1 周期（btc_cycle dict: phase, phase_label, dormancy_pct 等）
    m1 = brief.get("M1_cycle") or {}
    if isinstance(m1, dict) and m1:
        parts.append(f"[M1 周期] phase={m1.get('phase_label') or m1.get('phase','?')} | "
                     f"dormancy={m1.get('dormancy_pct','?')}% | liveliness={m1.get('liveliness_pct','?')}% | "
                     f"old_cdd={m1.get('old_coin_cdd_share_pct','?')}%")

    # M2 资金（_build_flow 返回的 dict）
    m2 = brief.get("M2_flow") or {}
    if isinstance(m2, dict) and m2:
        parts.append(f"[M2 资金] 总市值={m2.get('total_market_cap','?')} | BTC主导={m2.get('btc_dominance','?')}% | "
                     f"稳定币总额={m2.get('stablecoin_total_usd','?')} | 1d变化={m2.get('stablecoin_change_1d_pct','?')}% | "
                     f"7d变化={m2.get('stablecoin_change_7d_pct','?')}%")

    # M3 背离（列表，非 dict）
    m3 = brief.get("M3_divergence") or []
    if isinstance(m3, list) and m3:
        sig_summary = ", ".join(f"{s.get('name','?')}:{s.get('label','?')}" for s in m3[:5])
        parts.append(f"[M3 背离] {sig_summary}")
    elif isinstance(m3, dict):
        sigs = m3.get("signals", [])
        if sigs:
            sig_summary = ", ".join(f"{s.get('name','?')}:{s.get('label','?')}" for s in sigs[:5])
            parts.append(f"[M3 背离] {sig_summary}")

    # M4 机会（HIGH）+ M4 观察池（非 HIGH）
    all_opps = (brief.get("M4_opportunities") or []) + (brief.get("M4_watchlist") or [])
    if all_opps:
        opp_lines = []
        for o in all_opps[:5]:
            tier = o.get("conviction_tier", "?")
            score = o.get("conviction_score", "?")
            opp_lines.append(f"  {o.get('target','?')} {o.get('direction','?')} tier={tier} score={score} | {o.get('trigger_logic','')[:60]}")
        parts.append("[M4 机会]\n" + "\n".join(opp_lines))
    else:
        parts.append("[M4 机会] 无高置信机会")

    # M5 催化剂（event_calendar dict: hardcoded, gecko）
    m5 = brief.get("M5_catalyst") or {}
    if isinstance(m5, dict):
        events = m5.get("hardcoded") or []
        if events:
            evts_str = "; ".join(f"{e.get('date','')} {e.get('event','')}" for e in events[:5])
            parts.append(f"[M5 催化剂] {evts_str}")

    # M6 降级
    m6 = brief.get("M6_degraded") or []
    if m6:
        parts.append(f"[M6 降级] {', '.join(m6)}")

    # DIFF
    diff = brief.get("DIFF") or {}
    if isinstance(diff, dict) and diff:
        diff_items = [f"{k}:{v:+.1f}%" for k, v in diff.items() if v is not None]
        if diff_items:
            parts.append(f"[昨日变化] {' | '.join(diff_items[:6])}")

    return "\n".join(parts)


def generate_ai_narrative(settings, brief: dict) -> str | None:
    """调用 LLM 生成 AI 叙事段落。

    降级策略：LLM 不可用/异常 → 返回 None，由调用方决定是否降级。
    """
    try:
        from crypto_research.clients.llm_client import LLMClient

        llm = LLMClient(settings, rpm=10)
        if not llm.is_available():
            return None

        context = build_brief_context(brief)
        if len(context) > 4096:
            context = context[:4096]

        raw = llm.chat(
            SYSTEM_PROMPT,
            f"以下是今日加密大盘结构化数据摘要：\n\n{context}",
            temperature=0.2,
            max_tokens=1200,
        )
        if not raw or not raw.strip():
            return None

        return raw.strip()
    except Exception:
        return None
