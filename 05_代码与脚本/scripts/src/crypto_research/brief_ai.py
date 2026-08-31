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

    # M0 头部
    m0 = brief.get("M0_tldr", {})
    if m0:
        parts.append(f"[M0 头部] BTC={m0.get('btc_price','?')} | 恐贪={m0.get('fear_greed','?')} | "
                     f"周期={m0.get('btc_cycle_phase','?')} | 总市值={m0.get('total_market_cap','?')}")

    # M1 周期
    m1 = brief.get("M1_cycle", {})
    if m1:
        parts.append(f"[M1 周期] phase={m1.get('phase','?')} | dormancy={m1.get('dormancy_pct','?')}% | "
                     f"liveliness={m1.get('liveliness_pct','?')}%")

    # M2 资金
    m2 = brief.get("M2_flow", {})
    if m2:
        parts.append(f"[M2 资金] 稳定币分位={m2.get('stablecoin_flow_pctile','?')} | "
                     f"链净流TOP={m2.get('chain_flow_top','?')}")

    # M3 背离
    m3 = brief.get("M3_divergence", {})
    if m3:
        sigs = m3.get("signals", [])
        sig_summary = ", ".join(f"{s.get('name','?')}:{s.get('label','?')}" for s in sigs[:5]) if sigs else "无"
        parts.append(f"[M3 背离] {sig_summary}")

    # M4 机会清单（最关键）
    m4 = brief.get("M4_opportunities", [])
    if m4:
        opp_lines = []
        for o in m4[:5]:
            tier = o.get("conviction_tier", "?")
            score = o.get("conviction_score", "?")
            opp_lines.append(f"  {o.get('target','?')} {o.get('direction','?')} tier={tier} score={score} | {o.get('trigger_logic','')[:60]}")
        parts.append("[M4 机会]\n" + "\n".join(opp_lines))
    else:
        parts.append("[M4 机会] 无高置信机会")

    # M4 watchlist
    m4w = brief.get("M4_watchlist", [])
    if m4w:
        watch_lines = [f"  {w.get('target','?')} {w.get('trigger_logic','')[:50]}" for w in m4w[:3]]
        parts.append("[M4 观察池]\n" + "\n".join(watch_lines))

    # M5 催化剂
    m5 = brief.get("M5_catalyst", {})
    if m5:
        parts.append(f"[M5 催化剂] {m5.get('upcoming_events','无')[:100]}")

    # M6 降级
    m6 = brief.get("M6_degraded", [])
    if m6:
        parts.append(f"[M6 降级] {', '.join(m6)}")

    # DIFF
    diff = brief.get("DIFF", {})
    if diff:
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
