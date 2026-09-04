"""早报 AI 叙事层：把结构化 brief 压缩后喂 LLM，生成人话解读。

方案 B（混合叙事层）：
- 确定性评分/机会清单原样保留
- 仅新增一段 AI 叙事：TL;DR + 逐模块解读 + 策略暗示
- 降级铁律：LLM 不可用/异常 → 省略 AI 段，结构化早报照发

依赖：LLMClient（Ark 优先，OpenAI/DeepSeek 兜底）
"""
from __future__ import annotations

SYSTEM_PROMPT = """你是一位硬核加密数据分析师。根据以下结构化大盘数据，撰写简洁的市场早报解读。

输出结构（严格遵守）：
1. **一句话结论**（≤40字，点明今日市场状态 + 核心驱动因素）
2. **赛道洞察**（12赛道资金流向中，哪些赛道在领涨/领跌？背后可能的逻辑是什么？领涨币有哪些共性？）
3. **机会排序**（从 HIGH/MED 机会中挑出 2-3 个最值得关注的，说明理由：赛道 + 信号共振 + 催化剂）
4. **链上异动提示**（KOL 链上分析师捕捉到的异常资金流向，哪些值得警惕？）
5. **风险提示**（当前最大的 1-2 个风险点，标注「非投资建议」）

要求：
- 中文，markdown 格式
- 禁止编造 brief 中不存在的数据或事件
- 如有降级项，简要说明哪些维度缺失及影响
- 控制在 600 字以内，结论先行，干货密集"""


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

    # M4 共振榜
    m4r = brief.get("M4_resonance") or {}
    if isinstance(m4r, dict):
        r_signals = m4r.get("signals") or []
        if r_signals:
            r_lines = []
            for s in r_signals[:5]:
                r_lines.append(
                    f"  {s.get('symbol','?')} {s.get('direction','?')} "
                    f"conviction={s.get('conviction_score','?')} "
                    f"consensus={s.get('consensus_score','?')} "
                    f"sources={s.get('source_count','?')}"
                )
            parts.append("[M4 共振榜]\n" + "\n".join(r_lines))

    # M4 Meme 风险标签
    m4m = brief.get("M4_meme") or {}
    if isinstance(m4m, dict):
        summary = m4m.get("summary") or {}
        if summary:
            parts.append(
                f"[M4 Meme 风险池] block={summary.get('block',0)} high={summary.get('high',0)} "
                f"medium={summary.get('medium',0)} low={summary.get('low',0)}"
            )

    # M4 四烟囱信号
    m4c = brief.get("M4_chimney") or {}
    if isinstance(m4c, dict):
        avail = m4c.get("available") or []
        if avail:
            parts.append(f"[M4 四烟囱] 可用信号: {', '.join(avail)}")

    # M4 聪明钱背离
    m4s = brief.get("M4_smart_money") or {}
    if isinstance(m4s, dict):
        b_count = len(m4s.get("bullish") or [])
        s_count = len(m4s.get("bearish") or [])
        if b_count or s_count:
            parts.append(f"[M4 聪明钱背离] 看多{b_count} / 看空{s_count}")

    # M2 深加工
    m2i = brief.get("M2_institutional") or {}
    if isinstance(m2i, dict):
        inst = m2i.get("institutional") or {}
        layers = m2i.get("mvrv_layers") or {}
        if inst.get("bias"):
            parts.append(
                f"[M2 机构/MVRV] 机构倾向={inst.get('bias')} "
                f"深度低估={layers.get('deep_under',{}).get('count',0)} "
                f"高估={layers.get('overvalued',{}).get('count',0)}"
            )

    # M5 催化剂（event_calendar dict: hardcoded, gecko）
    m5 = brief.get("M5_catalyst") or {}
    if isinstance(m5, dict):
        events = m5.get("hardcoded") or []
        if events:
            evts_str = "; ".join(f"{e.get('date','')} {e.get('event','')}" for e in events[:5])
            parts.append(f"[M5 催化剂] {evts_str}")

    # 12 赛道资金流向（新增核心模块）
    sf = brief.get("sector_flow") or {}
    if isinstance(sf, dict) and sf.get("status") == "ok":
        sectors = sf.get("sectors") or []
        if sectors:
            # TOP3 领涨赛道 + 领跌赛道 + 各赛道领涨币
            top3 = sectors[:3]
            bottom2 = sectors[-2:] if len(sectors) >= 2 else []
            top_str = "; ".join(
                f"{s.get('sector_label','?')} 7d={s.get('mcap_change_7d_pct','?')}%"
                for s in top3
            )
            bot_str = "; ".join(
                f"{s.get('sector_label','?')} 7d={s.get('mcap_change_7d_pct','?')}%"
                for s in bottom2
            )
            parts.append(f"[赛道资金流-领涨] {top_str}")
            if bot_str:
                parts.append(f"[赛道资金流-领跌] {bot_str}")
            # 每个赛道的领涨币（取前6个赛道）
            leader_lines = []
            for s in sectors[:6]:
                leaders = s.get("leaders") or []
                if leaders:
                    ldr_str = ", ".join(
                        f"{l.get('symbol','?')}(7d{l.get('percent_change_7d','?')}%)"
                        for l in leaders[:3]
                    )
                    leader_lines.append(f"  {s.get('sector_label','?')}: {ldr_str}")
            if leader_lines:
                parts.append("[赛道领涨币]\n" + "\n".join(leader_lines))

    # KOL 链上信号（新增模块）
    ko = brief.get("kol_onchain") or {}
    if isinstance(ko, dict) and ko.get("status") == "ok":
        signals = ko.get("signals") or []
        stats = ko.get("stats") or []
        kols = ko.get("kols") or []
        if signals:
            sig_lines = []
            for s in signals[:8]:
                amt = s.get("event_usd_value")
                amt_str = f" ${amt/1e6:.1f}M" if isinstance(amt, (int, float)) and amt else ""
                sig_lines.append(
                    f"  {s.get('kol_name','?')}: {s.get('signal_subtype','?')} "
                    f"{s.get('symbol','?')} {s.get('event_direction','?')}{amt_str}"
                )
            parts.append(f"[KOL链上信号] 分析师={len(kols)}位 | 近{ko.get('hours',24)}h {len(signals)}条")
            if stats:
                stat_str = ", ".join(f"{st.get('signal_subtype','?')}:{st.get('cnt',0)}" for st in stats)
                parts.append(f"  类型分布: {stat_str}")
            parts.append("  明细:\n" + "\n".join(sig_lines))

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
            temperature=0.1,
            max_tokens=1200,
        )
        if not raw or not raw.strip():
            return None

        return raw.strip()
    except Exception:
        return None
