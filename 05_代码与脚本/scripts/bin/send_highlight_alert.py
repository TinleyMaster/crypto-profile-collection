#!/usr/bin/env python3
"""高亮信号提醒邮件（每小时探测；发信时**同步展示当前高亮池全集**）。

数据源：biz.market_overview_snapshot.payload → opportunity_list.highlight_signals
    由 build_daily_brief.py 每日 08:30 落库。本脚本**只读**该快照，
    不重算 overview、不调用 LLM，因此每轮开销近乎为零，可安全地每小时探测。

发信触发（见 classify_card）：
    new      卡片首次出现（去重表内无「已发送」记录）
    upgrade  tier 由 MED 升到 HIGH，或共振源数（resonance_count）较上次增加
    （仅当存在 new/upgrade 时才发信；不因「退出/降级」单独发信）

正文口径（2026-09-28 「只进不出」审计处置）：邮件不再只渲染增量卡，而是渲染
    **当前高亮池全集**——被本轮判定为 new/upgrade 的标对应徽章，其余标「📌 在池」。
    这样读者每次都能看到「池子里现在有哪些」，退出/降级的卡因不在池中而自然消失，
    无需另发退场邮件（主人明确：和高亮池同步显示，不发退场邮件）。

去重冷却：biz.highlight_alert_log，UNIQUE(card_key, alert_kind) + 默认 24h 窗口，
    INSERT ... ON CONFLICT ... RETURNING 原子加锁（范式同 catalyst/notifier.py）。

用法：
    python send_highlight_alert.py                    # 探测 + 发送
    python send_highlight_alert.py --dry-run          # 只打印 HTML，不发信、不写表
    python send_highlight_alert.py --snap-date 2026-09-23
    python send_highlight_alert.py --to a@b.com --cooldown-hours 12
    python send_highlight_alert.py --force            # 忽略冷却窗口（人工补发）
    python send_highlight_alert.py --demo             # 全池按「新增」渲染并发送，不回写去重表（预览排版）
    python send_highlight_alert.py --demo --dry-run   # 预览排版且不发信

scheduler.py 注册：highlight_alert（每小时 05 分，Asia/Shanghai）。
"""
from __future__ import annotations

import argparse
import html as _html
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg.rows  # noqa: E402

from crypto_research.utils.time_utils import fmt_bj  # noqa: E402

# =====================================================================
# 常量
# =====================================================================

TIER_RANK = {"LOW": 0, "MED": 1, "HIGH": 2}
ALERT_NEW = "new"
ALERT_UPGRADE = "upgrade"
# 「在池」不是发信触发，而是池内未变化卡片的展示徽章（见 render_html / main）。
ALERT_HOLD = "hold"
ALERT_LABEL = {ALERT_NEW: "🆕 新增", ALERT_UPGRADE: "⬆️ 升级", ALERT_HOLD: "📌 在池"}

DEFAULT_COOLDOWN_HOURS = 24
DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_MAX_CARDS = 10
STALE_LOCK_MINUTES = 30  # 崩溃残留的 sending 锁，超过该时长允许重新获取

SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,10}$")

# 聚合/宏观类信号：其 target 是「板块名 / 榜单名」（如 DePIN、Solana 链），
# 不是可交易币种。这些卡片即使 target 恰好形如 symbol（如 "DEPIN"）也**不计入覆盖币数**；
# 其中真正涉及的币走 involved_symbols 计。R4（2026-09-28 审计）。
AGGREGATE_SIGNAL_TYPES = {
    "narrative", "chain_inflow", "sector_inflow", "sector_outflow",
    "stablecoin_inflow", "stablecoin_outflow",
    "mvrv_deep_under", "mvrv_deep_over", "mvrv_under_watch",
    "fng_extreme", "leverage_extreme", "btc_left_accum", "cm_adoption_divergence",
}

SIGNAL_TYPE_LABEL = {
    "mvrv_deep_under": "MVRV深度低估", "mvrv_under_watch": "MVRV偏低",
    "price_surge": "价格暴涨", "price_crash": "价格暴跌",
    "price_volume_surge": "量价齐升", "volume_surge": "成交量异动",
    "sector_inflow": "赛道资金流入", "sector_outflow": "赛道资金流出",
    "narrative": "叙事板块", "chain_inflow": "链上净流入",
    "stablecoin_inflow": "稳定币流入", "stablecoin_outflow": "稳定币流出",
    "etf_flow": "ETF资金流", "btc_left_accum": "BTC左侧吸筹",
    "cm_adoption_divergence": "采用度背离", "leverage_extreme": "杠杆极值",
    "fng_extreme": "恐贪极值", "whale_flow": "巨鲸异动",
    "funding": "融资落地", "token_unlock": "代币解锁",
    "kol_onchain": "KOL链上情报", "catalyst": "催化剂",
    "conflict_game": "多空博弈",
    "github_activity": "开发活跃", "funding_raise": "融资",
}

# 六轴（_conviction_breakdown）标签
DIM_LABEL = {
    "mvrv": "MVRV估值", "funding": "资金费率", "netflow": "链上净流",
    "stable": "稳定币流", "roi": "ROI动量", "catalyst": "催化情绪",
}

# related_dims 里的「来源」标签：生产口径写入的是数据表名 / 管线编号，
# 属内部标识，必须映射成人话后再进展示层（不得原样透出）。
SOURCE_DIM_LABEL = {
    # 估值类
    "mvrv_universe": "全市场 MVRV", "P0-1 估值回归": "估值回归",
    # 机构 / ETF
    "机构ETF资金流（cryptoetf.today）": "机构 ETF 资金流",
    "P1 机构行为": "机构行为",
    # 催化剂
    "catalyst_events": "催化剂事件", "P0-B 催化剂驱动": "催化剂驱动",
    "P1-2 多空博弈": "多空博弈",
    # 板块 / 链上资金
    "P1-1 叙事榜（市值）": "叙事板块市值榜",
    "P1-1 链净流入榜": "链上净流入榜",
    # 巨鲸
    "onchain_transfer_log": "链上大额转账", "P1-3 链上巨鲸": "链上巨鲸",
    # 融资
    "asset_raises": "融资事件", "P1 融资落地": "融资落地",
    # 解锁
    "asset_unlock_event": "代币解锁事件", "P1 解锁抛压": "解锁抛压",
}

# 未登记来源兜底：剥离 P0-1 / P1-1 / P0-B 这类管线编号前缀，避免内部编号外泄
_PIPELINE_CODE_RE = re.compile(r"^P\d[0-9A-Za-z\-]*\s+")

# AI 未复核的原因（生产口径写入 _ai_filter_reason / _ai_skipped_no_asset）
AI_SKIP_LABEL = {
    "聚合/宏观信号，跳过全量画像": "宏观/聚合类信号，未做全量画像",
    "asset_id 解析失败，跳过全量画像": "资产未匹配，未做全量画像",
}
AI_SKIP_DEFAULT = "未进入 AI 复核"

# AI 六维评分卡标签（analyze_asset_v2 dimensions）
AI_DIM_LABEL = {
    "valuation": "估值", "technical": "技术面", "onchain": "链上",
    "fundamental": "基本面", "sentiment": "情绪", "catalyst": "催化剂",
}

HORIZON_LABEL = {"short": "短期", "medium": "中期", "long": "长期"}
TIER_COLOR = {"HIGH": "#c0392b", "MED": "#e67e22"}


# =====================================================================
# 纯函数（离线可测）
# =====================================================================

def primary_signal_type(card: dict) -> str:
    """主信号类型：优先 signal_type，回退 signal_types[0]，再回退哨兵值。"""
    st = str(card.get("signal_type") or "").strip()
    if st:
        return st
    for s in (card.get("signal_types") or []):
        if s:
            return str(s)
    return "__default__"


def card_key(card: dict) -> str:
    """卡片指纹 = lower(target)|primary_signal_type。

    高亮卡片派生自 overview 快照、无独立主键，故用指纹做去重键。
    target 大小写不敏感（select_highlight_signals 合并时也按小写 target 归并）。
    """
    tgt = str(card.get("target") or "").strip().lower()
    return f"{tgt}|{primary_signal_type(card)}"


def classify_card(card: dict, prev: dict | None) -> str | None:
    """判定该卡片是否值得提醒：'new' / 'upgrade' / None（无变化）。

    prev 为该卡片最近一条「已发送」记录（含 tier / resonance_count），无则 None。
    只认「tier 升级」与「共振源数增加」两种升级，避免分数小幅波动造成刷屏。
    """
    if prev is None:
        return ALERT_NEW

    prev_rank = TIER_RANK.get(str(prev.get("tier") or "").upper(), 0)
    cur_rank = TIER_RANK.get(str(card.get("conviction_tier") or "").upper(), 0)
    if cur_rank > prev_rank:
        return ALERT_UPGRADE

    prev_res = _safe_int(prev.get("resonance_count"))
    cur_res = _safe_int(card.get("resonance_count"))
    if cur_res > prev_res:
        return ALERT_UPGRADE

    return None


def _safe_int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def card_symbols(cards: list[dict]) -> set[str]:
    """高亮池涉及的全部币种 symbol（symbol 形态 target + involved_symbols 并集）。

    R4（2026-09-28 审计）：聚合/宏观类信号（narrative / chain_inflow / sector_* 等）
    的 target 是板块名/榜单名，不是币种——即使形如 symbol（如 "DePIN"）也不计币；
    其涉及的真实币仍由 involved_symbols 计入。
    """
    syms: set[str] = set()
    for c in cards:
        tgt = str(c.get("target") or "").strip().upper()
        if SYMBOL_RE.match(tgt) and primary_signal_type(c) not in AGGREGATE_SIGNAL_TYPES:
            syms.add(tgt)
        for s in (c.get("involved_symbols") or []):
            if s:
                syms.add(str(s).upper())
    return syms


def symbol_count(cards: list[dict]) -> int:
    """覆盖币数：symbol 形态 target + involved_symbols 并集（复用 card_symbols）。"""
    return len(card_symbols(cards))


def card_sort_key(item: tuple[dict, str]) -> tuple:
    """邮件内排序：HIGH 优先 → 新增优先 → 分数降序 → 事件强度降序。"""
    card, kind = item
    is_high = 1 if str(card.get("conviction_tier") or "").upper() == "HIGH" else 0
    is_new = 1 if kind == ALERT_NEW else 0
    score = _safe_float(card.get("decayed_score")
                        if card.get("decayed_score") is not None
                        else card.get("conviction_score"))
    # 末级按事件强度：同分时避免「事件强度 86 排在 81 之后」的展示自相矛盾。
    # _safe_float 对缺失/非数值（event_strength 可由催化剂卡等路径留空）退化为 0.0。
    return (is_high, is_new, score, _safe_float(card.get("event_strength")))


def build_pool_items(highlights: list[dict], granted_kinds: dict,
                     max_cards: int | None = None) -> list[tuple[dict, str]]:
    """组装邮件正文的「当前高亮池」（池同步显示，2026-09-28）。

    - 本轮获准发送的卡（`granted_kinds[card_key]` 存在）保留其 new/upgrade 徽章；
    - 其余在池卡标 ALERT_HOLD（📌 在池），使读者每次都能看到池内全集，
      退出/降级卡因不在池中自然消失（不发退场邮件）。
    - 按 card_sort_key 排序（HIGH/新增优先），可选截断 max_cards。

    抽为纯函数以便离线行为断言（复验 `cae0431` M5：正文退回只渲染增量曾逃逸）。
    """
    items = [(c, granted_kinds.get(card_key(c)) or ALERT_HOLD) for c in highlights]
    items.sort(key=card_sort_key, reverse=True)
    if max_cards:
        return items[:max_cards]
    return items


# =====================================================================
# 渲染
# =====================================================================

def _e(v) -> str:
    return _html.escape(str(v if v is not None else ""))


def _fmt(v, decimals: int = 0) -> str:
    if v is None:
        return "N/A"
    try:
        return f"{float(v):,.{decimals}f}"
    except (TypeError, ValueError):
        return str(v)


def _dim_labels(values) -> list[str]:
    """related_dims → 展示标签（内部表名/管线编号不原样透出）。"""
    out: list[str] = []
    for v in values or []:
        s = str(v or "").strip()
        if not s:
            continue
        if s in DIM_LABEL:            # 六轴口径（历史数据）
            out.append(DIM_LABEL[s])
        elif s in SOURCE_DIM_LABEL:   # 生产来源口径
            out.append(SOURCE_DIM_LABEL[s])
        else:
            out.append(_PIPELINE_CODE_RE.sub("", s) or "其他来源")
    return out


def _ai_skip_note(card: dict) -> str:
    """AI 未复核的原因（人话，不出现 asset_id 等内部字段名）。"""
    if card.get("_ai_skipped_no_asset"):
        return AI_SKIP_LABEL["asset_id 解析失败，跳过全量画像"]
    reason = str(card.get("_ai_filter_reason") or "").strip()
    return AI_SKIP_LABEL.get(reason, AI_SKIP_DEFAULT)


def _render_signal_types(card: dict) -> str:
    types = card.get("signal_types") or ([card.get("signal_type")] if card.get("signal_type") else [])
    if not types:
        return ""
    return "".join(
        f'<span style="display:inline-block;margin:0 4px 4px 0;padding:1px 6px;'
        f'background:#eef2ff;color:#3730a3;border-radius:3px;font-size:11px">'
        f'{_e(SIGNAL_TYPE_LABEL.get(t, t))}</span>'
        for t in types if t
    )


def _render_merged_signals(card: dict) -> str:
    """合并卡片：列出全部子信号原由（主卡之外的信息不丢）。"""
    all_signals = card.get("all_signals") or []
    if len(all_signals) <= 1:
        return ""
    rows = []
    for s in all_signals:
        st = SIGNAL_TYPE_LABEL.get(s.get("signal_type"), s.get("signal_type") or "信号")
        dir_cn = {"long": "多", "short": "空"}.get(s.get("direction"), "中性")
        km = f' · {_e(s.get("key_metric"))}' if s.get("key_metric") else ""
        logic = f'<div style="color:#64748b;font-size:11px">{_e(s.get("trigger_logic"))}</div>' if s.get("trigger_logic") else ""
        rows.append(
            f'<div style="margin-top:4px;padding-left:8px;border-left:2px solid #e2e8f0">'
            f'<span style="font-size:12px;color:#0f172a">{_e(st)}</span>'
            f'<span style="font-size:11px;color:#64748b"> · {dir_cn} · conv {_fmt(s.get("conviction_score"))}{km}</span>'
            f'{logic}</div>'
        )
    return (f'<div style="margin-top:8px"><div style="font-size:12px;font-weight:600;color:#334155">'
            f'📊 全部信号原由（{len(all_signals)}个）</div>{"".join(rows)}</div>')


def _render_ai_block(card: dict) -> str:
    """AI V2 复核状态：认可 / 未背书 / 复核失败 / 未复核，四态都必须有明确披露。

    未复核与复核失败此前静默，读者无法区分「AI 认可」与「AI 没跑」——必须显式写出。
    """
    if card.get("_ai_downgraded"):
        # 不透传 _ai_filter_reason：那是管线路径原因（如「事件驱动信号触发: funding」），
        # 含内部 signal_type token，且不能表达「AI 未背书」这一语义。
        ai = card.get("ai_analysis_v2") or {}
        score = ai.get("overall_score")
        score_txt = f"（AI 综合 {_fmt(score)}）" if score is not None else ""
        return (f'<div style="margin-top:6px;padding:4px 8px;background:#fef9c3;'
                f'border-radius:3px;font-size:11px;color:#92400e">'
                f'⚡ AI 未背书{score_txt}：按事件驱动规则直通保留，未采纳 AI 结论</div>')

    ai = card.get("ai_analysis_v2") or {}
    if ai.get("error"):
        return (f'<div style="margin-top:6px;padding:4px 8px;background:#f1f5f9;'
                f'border-radius:3px;font-size:11px;color:#475569">🤖 AI 复核失败，本轮按规则分展示</div>')
    if not ai:
        return (f'<div style="margin-top:6px;padding:4px 8px;background:#f1f5f9;'
                f'border-radius:3px;font-size:11px;color:#475569">'
                f'🤖 AI 未复核（{_e(_ai_skip_note(card))}）</div>')

    dims = ai.get("dimensions") or {}
    dim_txt = " · ".join(
        f'{AI_DIM_LABEL.get(k, k)} {_fmt((v or {}).get("score"))}'
        for k, v in dims.items() if isinstance(v, dict)
    )
    parts = [f'<div style="font-size:11px;color:#334155">🤖 AI 综合 {_fmt(ai.get("overall_score"))}'
             f' · 置信 {_e(ai.get("confidence") or "-")}</div>']
    if dim_txt:
        parts.append(f'<div style="font-size:11px;color:#64748b">{_e(dim_txt)}</div>')
    if ai.get("reason_summary"):
        parts.append(f'<div style="font-size:11px;color:#64748b">{_e(ai["reason_summary"])}</div>')
    return f'<div style="margin-top:6px;padding:6px 8px;background:#f8fafc;border-radius:3px">{"".join(parts)}</div>'


def _render_calibration_line(card: dict) -> str:
    """该类型历史回测统计（审计 2026-10-03 #2）：把 signal_type 级的历史命中率 /
    平均收益 / 相对 BTC 超额显式挂到卡片上，让「conv 77」有历史统计背书可查。

    数据来自卡片自带的 `calibration_status`（macro_market 生产时透传）。无样本 /
    未校准的类型只说明「无背书」，不臆造 0。
    """
    cs = card.get("calibration_status") or {}
    if not cs or "sample_count" not in cs:
        return ""
    n = int(cs.get("sample_count") or 0)
    hr = cs.get("hit_rate")
    ap = cs.get("avg_pnl_pct")
    aa = cs.get("avg_alpha")
    parts = [f"样本 {n}"]
    if hr is not None:
        parts.append(f"命中率 {hr * 100:.0f}%")
    if ap is not None:
        parts.append(f"平均收益 {ap:+.1f}%")
    if aa is not None:
        parts.append(f"相对BTC超额 {aa:+.1f}%")
    if n == 0 or (hr is None and ap is None and aa is None):
        parts.append("无回测背书")
    return (f'<div style="font-size:11px;color:#475569;margin-top:2px">'
            f'📊 该类型历史：{" · ".join(parts)}</div>')


def _render_risk_panel(card: dict) -> str:
    """前置风险过滤面板（审计 2026-10-03 #4）：流通市值 / 未来30天解锁 / 合约审计。

    数据在 main() 内经 `load_risk_panels` 查库后注入 card["_risk_panel"]，渲染保持纯函数。
    无 asset_id / 无数据的卡不渲染（聚合类、未匹配资产天然跳过）。
    """
    rp = card.get("_risk_panel") or {}
    if not rp:
        return ""
    bits = []
    mcap = rp.get("mcap_usd")
    if mcap:
        if mcap >= 1e9:
            bits.append(f"市值 ${mcap / 1e9:.2f}B")
        elif mcap >= 1e6:
            bits.append(f"市值 ${mcap / 1e6:.1f}M")
        elif mcap > 0:
            bits.append(f"市值 ${mcap / 1e3:.0f}K")  # 小市值用 K 展示，避免 .0f 四舍五入成 $0M
    up = rp.get("unlock_30d_pct")
    up_usd = rp.get("unlock_30d_usd")
    if up is not None and up > 0:
        bits.append(f"30d解锁 {up:.1f}%")
    elif up_usd:
        bits.append(f"30d解锁 ${up_usd / 1e6:.1f}M")
    else:
        bits.append("30d解锁 无")
    audit = rp.get("audit_status")
    bits.append(f"审计 {audit or '未覆盖'}")
    color = "#b45309" if rp.get("risk_flag") else "#475569"
    return (f'<div style="font-size:11px;color:{color};margin-top:2px">'
            f'🛡 风险前置：{" · ".join(bits)}</div>')


def _render_type_position(card: dict) -> str:
    """类型定位提示（审计 2026-10-03 #3）：低权重因子（持续性开发提交）单列，
    明确「不是落地里程碑」，只作佐证维度——配合生产侧单源降档形成自洽口径。"""
    if primary_signal_type(card) != "github_activity":
        return ""
    return (f'<div style="font-size:11px;color:#92400e;margin-top:2px">'
            f'🧭 类型定位：开发提交属持续性运维行为，非落地里程碑（主网上线/审计/融资）；'
            f'仅作佐证维度，需叠加资金面/事件第二维度确认</div>')


def render_card(card: dict, kind: str) -> str:
    tier = str(card.get("conviction_tier") or "").upper()
    # M4（2026-09-24 审计）：AI 明确不背书（_ai_downgraded）的卡片不得以 HIGH 呈现，
    # 与前端 index.html「AI 降级不挂 🔥HIGH」口径一致——否则出现
    # 「AI 建议不参与 + 系统 HIGH 看多」同封自相矛盾。
    if card.get("_ai_downgraded") and tier == "HIGH":
        tier = "MED"
    tier_color = TIER_COLOR.get(tier, "#64748b")
    dir_cn = {"long": "做多", "short": "做空"}.get(card.get("direction"), "中性")
    horizon = HORIZON_LABEL.get(card.get("horizon"), "中期")
    score = _fmt(card.get("conviction_score"))
    decayed = card.get("decayed_score")
    decayed_txt = f' · 衰减后 {_fmt(decayed)}' if decayed is not None else ""

    # 强度拆解
    strength_line = ""
    if card.get("conviction_strength") is not None:
        parts = [f'强度 {_fmt(card.get("conviction_strength"))}']
        if card.get("resonance_bonus"):
            parts.append(f'共振 +{_fmt(card.get("resonance_bonus"))}')
        if card.get("regime_mult") is not None:
            parts.append(f'周期 ×{_safe_float(card.get("regime_mult"), 1.0):.2f}')
        strength_line = f'<div style="font-size:11px;color:#64748b">{" · ".join(parts)}</div>'

    event_line = ""
    if card.get("event_strength") is not None:
        res = f' · 共振×{_fmt(card.get("resonance_count"))}' if card.get("resonance_count") else ""
        event_line = f'<div style="font-size:11px;color:#64748b">事件强度 {_fmt(card.get("event_strength"))}{res}</div>'

    km = f'<span style="font-size:12px;color:#0f172a">{_e(card.get("key_metric"))}</span>' if card.get("key_metric") else ""
    logic = f'<div style="font-size:12px;color:#334155;margin-top:4px">{_e(card.get("trigger_logic"))}</div>' if card.get("trigger_logic") else ""
    action = f'<div style="font-size:11px;color:#166534;margin-top:4px">🎯 {_e(card.get("action_hint"))}</div>' if card.get("action_hint") else ""
    invalid = f'<div style="font-size:11px;color:#991b1b;margin-top:2px">⚠️ 失效条件：{_e(card.get("invalidation"))}</div>' if card.get("invalidation") else ""
    val_note = f'<div style="font-size:11px;color:#92400e;margin-top:2px">🔊 {_e(card.get("valuation_filter_note"))}</div>' if card.get("valuation_filter_note") else ""
    # M2-A1（2026-09-28）：档位被降档时给出原因，避免读者看到「78 分却是 MED」无从理解。
    demote = (f'<div style="font-size:11px;color:#b45309;margin-top:2px">⬇️ 降档说明：'
              f'{_e(card.get("tier_demote_reason"))}</div>'
              if card.get("tier_demote_reason") else "")
    # 审计 2026-10-06（高亮信号通道准入标准 · P1 档位透明）：常规 MED（未降档、纯因分数不足）
    # 的卡片也必须自证「为何 MED / 距 HIGH 差多少」——尤其催化卡读者常误以为「AI 综合 65」把
    # 它压下来，实际上档位只由 conviction_score 决定，AI 综合仅复核展示。
    med_note = ""
    if tier == "MED" and not card.get("tier_demote_reason"):
        _sc = _safe_float(card.get("conviction_score"))
        _hi = card.get("high_threshold")
        if _sc > 0 and _hi is not None:
            med_note = (f'<div style="font-size:10.5px;color:#94a3b8;margin-top:2px">'
                        f'conv {_sc:.0f} 未达 HIGH 门槛（≥{_safe_float(_hi):.0f}），常规 MED；'
                        f'档位由 conviction_score 判定，与 AI 综合无关</div>')
        elif _sc > 0:
            med_note = (f'<div style="font-size:10.5px;color:#94a3b8;margin-top:2px">'
                        f'conv {_sc:.0f} 未达 HIGH 档，常规 MED；档位由 conviction_score 判定，'
                        f'与 AI 综合无关</div>')
    dims = _dim_labels(card.get("related_dims"))
    dims_txt = f'<div style="font-size:11px;color:#64748b">来源维度：{_e(", ".join(dims))}</div>' if dims else ""

    return f"""
    <div style="margin:0 0 12px;padding:10px 12px;border:1px solid #e2e8f0;border-left:4px solid {tier_color};border-radius:5px;background:#fff">
      <div style="margin-bottom:4px">
        <span style="font-size:14px;font-weight:700;color:#0f172a">{_e(card.get("target"))}</span>
        <span style="display:inline-block;margin-left:6px;padding:1px 6px;background:{tier_color};color:#fff;border-radius:3px;font-size:10px">{_e(tier or "?")}</span>
        <span style="display:inline-block;margin-left:4px;padding:1px 6px;background:#e0e7ff;color:#3730a3;border-radius:3px;font-size:10px">{_e(ALERT_LABEL.get(kind, kind))}</span>
        <span style="font-size:11px;color:#64748b;margin-left:6px">{_e(dir_cn)} · {_e(horizon)} · conv {score}{decayed_txt}</span>
      </div>
      {_render_risk_panel(card)}
      <div style="margin-bottom:4px">{_render_signal_types(card)}</div>
      {km}
      {strength_line}
      {event_line}
      {dims_txt}
      {logic}
      {action}
      {invalid}
      {val_note}
      {demote}
      {med_note}
      {_render_calibration_line(card)}
      {_render_type_position(card)}
      {_render_merged_signals(card)}
      {_render_ai_block(card)}
    </div>"""


def _render_resonance_section(resonance_symbols: list[str] | None) -> str:
    """跨模型共振汇总区（审计 2026-10-03 #5）：同时命中「基本面高亮池」与
    「盘面异动告警」的标的单独拎出，作为最高确定性候选池。空则整块不渲染。"""
    if not resonance_symbols:
        return ""
    syms = "、".join(f"<b>{_e(s)}</b>" for s in resonance_symbols)
    return f"""
    <div style="margin:0 0 12px;padding:10px 12px;border:1px solid #f59e0b;border-left:4px solid #f59e0b;border-radius:5px;background:#fffbeb">
      <div style="font-size:13px;font-weight:700;color:#92400e">⚡ 基本面×盘面 跨模型共振（{len(resonance_symbols)} 币）</div>
      <div style="font-size:12px;color:#78350f;margin-top:2px">{syms}</div>
      <div style="font-size:11px;color:#92400e;margin-top:2px">同时命中「高亮信号池（基本面/事件）」与「盘面异动告警（资金/合约）」，两模型共振确认，作为最高确定性候选池优先观察。</div>
    </div>"""


def render_html(items: list[tuple[dict, str]], snap_date: str, total_highlights: int,
                resonance_symbols: list[str] | None = None) -> str:
    """渲染邮件正文。items = 当前高亮池（本轮新增/升级 + 在池）逐卡 (card, kind)。"""
    n_new = sum(1 for _, k in items if k == ALERT_NEW)
    n_up = sum(1 for _, k in items if k == ALERT_UPGRADE)
    n_hold = sum(1 for _, k in items if k == ALERT_HOLD)
    cards = [c for c, _ in items]
    now = fmt_bj(datetime.now(timezone.utc), "%Y-%m-%d %H:%M") + "（北京时间）"
    hold_txt = f" · 在池 {n_hold} 条" if n_hold else ""
    reso_txt = f" · 跨模型共振 {len(resonance_symbols)} 币" if resonance_symbols else ""
    head = f"""
    <h2 style="margin:0 0 4px">⚡ 高亮信号提醒</h2>
    <p style="margin:0 0 12px;color:#666;font-size:13px">
      新增 {n_new} 条 · 升级 {n_up} 条{hold_txt} · 覆盖 {symbol_count(cards)} 币{reso_txt} ·
      数据快照 {_e(snap_date)}（当日高亮池共 {total_highlights} 条） · 生成于 {now}
    </p>"""
    resonance_html = _render_resonance_section(resonance_symbols)
    # 审计 2026-10-06（高亮信号通道准入标准 · P1 档位透明）：整封说明档位/AI 综合口径，
    # 消除「为什么全是 MED」「是不是 AI 综合把我压下来的」的误读。
    tier_legend = (
        '<div style="margin:0 0 12px;padding:8px 10px;background:#f8fafc;border:1px solid #e2e8f0;'
        'border-radius:5px;font-size:11px;color:#475569;line-height:1.65">'
        '🎚 档位口径：HIGH/MED/LOW 由系统强度 <b>conviction_score</b> 结合档位纪律'
        '（多源共振 ≥2、回测背书、时间衰减）判定；每条「常规 MED」卡片会标注当前分与 HIGH 门槛差。'
        '<br>🤖 AI 综合分为复核展示：AI 不背书时档位不高于 MED，但 <b>AI 综合低 ≠ 系统分低</b>'
        '——档位不看 AI 综合。无回测背书的类型（missing_calibration）一律封顶 MED 并带降档说明。</div>'
    )
    if not items:
        body = '<p style="color:#999">本轮无新增/升级高亮信号。</p>'
    else:
        body = "".join(render_card(c, k) for c, k in items)
    return (f'<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:720px">'
            f'{head}{tier_legend}{resonance_html}{body}</div>')


# =====================================================================
# 数据库
# =====================================================================

DDL = """
CREATE TABLE IF NOT EXISTS biz.highlight_alert_log (
    log_id              BIGSERIAL PRIMARY KEY,
    card_key            TEXT NOT NULL,
    alert_kind          VARCHAR(16) NOT NULL,
    target              TEXT,
    primary_signal_type TEXT,
    tier                VARCHAR(4),
    score               NUMERIC(6,1),
    resonance_count     INT,
    sent_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    subject             VARCHAR(256),
    status              VARCHAR(16) NOT NULL DEFAULT 'sent',
    error_msg           TEXT,
    UNIQUE (card_key, alert_kind)
);
CREATE INDEX IF NOT EXISTS idx_hl_alert_kind_time ON biz.highlight_alert_log (alert_kind, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_hl_alert_card_time ON biz.highlight_alert_log (card_key, sent_at DESC);
"""

# 原子加锁：无记录→插入；失败过→重试；sending 残留超时→接管；冷却到期→重置
LOCK_SQL = """
INSERT INTO biz.highlight_alert_log
    (card_key, alert_kind, target, primary_signal_type, tier, score,
     resonance_count, subject, status)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'sending')
ON CONFLICT (card_key, alert_kind) DO UPDATE
   SET sent_at = NOW(), status = 'sending',
       target = EXCLUDED.target,
       primary_signal_type = EXCLUDED.primary_signal_type,
       tier = EXCLUDED.tier, score = EXCLUDED.score,
       resonance_count = EXCLUDED.resonance_count,
       subject = EXCLUDED.subject, error_msg = NULL
 WHERE biz.highlight_alert_log.status = 'failed'
    OR (biz.highlight_alert_log.status = 'sending'
        AND biz.highlight_alert_log.sent_at < NOW() - (%s::int * INTERVAL '1 minute'))
    OR biz.highlight_alert_log.sent_at < NOW() - (%s::int * INTERVAL '1 hour')
RETURNING log_id
"""


def ensure_table(conn) -> None:
    conn.execute(DDL)


def load_snapshot(conn, snap_date: str) -> dict | None:
    """读 <= snap_date 的最近一条 overview 快照（当日 08:30 未落库时回落上一日）。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT snap_date, payload
            FROM biz.market_overview_snapshot
            WHERE snap_date <= %s
            ORDER BY snap_date DESC
            LIMIT 1
            """,
            (snap_date,),
        )
        return cur.fetchone()


def extract_highlights(payload: dict | None) -> list[dict]:
    """从快照 payload 取高亮卡片（select_highlight_signals 的产物）。"""
    opp_list = (payload or {}).get("opportunity_list") or {}
    return list(opp_list.get("highlight_signals") or [])


def load_sent_states(conn, lookback_days: int) -> dict[str, dict]:
    """每张卡片最近一条「已发送」记录（失败记录不参与判定，避免吞掉提醒）。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (card_key)
                   card_key, tier, score, resonance_count, sent_at
            FROM biz.highlight_alert_log
            WHERE status = 'sent'
              AND sent_at > NOW() - (%s::int * INTERVAL '1 day')
            ORDER BY card_key, sent_at DESC
            """,
            (lookback_days,),
        )
        return {r["card_key"]: r for r in cur.fetchall()}


def load_risk_panels(conn, cards: list[dict]) -> dict[int, dict]:
    """前置风险面板（审计 2026-10-03 #4）：流通市值 / 未来30天解锁 / 合约审计。

    按卡片 asset_id 批量查三张表，返回 {asset_id: {mcap_usd, unlock_30d_usd,
    unlock_30d_pct, audit_status, risk_flag}}。任一查询失败整体降级为空（不阻断发信）。
    """
    ids = sorted({int(c["asset_id"]) for c in cards if c.get("asset_id") is not None})
    if not ids:
        return {}
    try:
        out: dict[int, dict] = {}
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """
                SELECT asset_id, market_cap
                FROM biz.v_asset_market_daily_primary
                WHERE market_date = (SELECT MAX(market_date) FROM biz.v_asset_market_daily_primary)
                  AND asset_id = ANY(%s)
                """,
                (ids,),
            )
            mcap = {r["asset_id"]: float(r["market_cap"] or 0) for r in cur.fetchall()}
            cur.execute(
                """
                SELECT asset_id,
                       COALESCE(SUM(unlock_value_usd), 0) AS total_usd
                FROM biz.asset_unlock_event
                WHERE unlock_date BETWEEN NOW() AND NOW() + INTERVAL '30 days'
                  AND asset_id = ANY(%s)
                GROUP BY asset_id
                """,
                (ids,),
            )
            unlock = {r["asset_id"]: float(r["total_usd"] or 0) for r in cur.fetchall()}
            cur.execute(
                """
                SELECT asset_id, source_status, is_honeypot, risk_score
                FROM biz.asset_contract_security
                WHERE asset_id = ANY(%s)
                """,
                (ids,),
            )
            audits = {r["asset_id"]: r for r in cur.fetchall()}
        for aid in ids:
            m = mcap.get(aid) or 0.0
            t_usd = unlock.get(aid) or 0.0
            entry = {
                "mcap_usd": m or None,
                "unlock_30d_usd": t_usd or None,
                "unlock_30d_pct": (t_usd / m * 100) if (m and t_usd) else None,
                "audit_status": None,
                "risk_flag": False,
            }
            a = audits.get(aid)
            if a:
                if a.get("source_status") == "hit":
                    if a.get("is_honeypot"):
                        entry["audit_status"], entry["risk_flag"] = "⚠️蜜罐", True
                    else:
                        rs = a.get("risk_score")
                        if rs is not None and float(rs) >= 70:
                            entry["audit_status"], entry["risk_flag"] = (
                                f"⚠️风险{float(rs):.0f}", True)
                        else:
                            entry["audit_status"] = "✅已扫描"
                else:
                    entry["audit_status"] = "未覆盖"
            if entry["mcap_usd"] or entry["unlock_30d_usd"] or entry["audit_status"]:
                out[aid] = entry
        return out
    except Exception as e:
        print(f"[highlight_alert] 风险面板查询失败（降级跳过）: {e}")
        return {}


def load_recent_scan_symbols(conn, hours: int = 24) -> set[str]:
    """近 N 小时盘面异动告警（biz.scan_signal high/medium 置信）涉及的币种。

    与高亮池做交集即为「基本面×盘面 跨模型共振」标的（审计 2026-10-03 #5）。
    失败返回空集（不阻断发信）。
    """
    try:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """
                SELECT DISTINCT symbol
                FROM biz.scan_signal
                WHERE created_at > NOW() - make_interval(hours => %s)
                  AND confidence IN ('high', 'medium')
                  AND symbol IS NOT NULL AND TRIM(symbol) <> ''
                """,
                (int(hours),),
            )
            return {str(r["symbol"]).strip().upper() for r in cur.fetchall()
                    if str(r["symbol"] or "").strip()}
    except Exception as e:
        print(f"[highlight_alert] 盘面共振查询失败（降级跳过）: {e}")
        return {}


def acquire_send_lock(conn, card: dict, kind: str, subject: str, cooldown_hours: int) -> bool:
    """原子获取发送权；返回 False 表示处于冷却窗口内（或取锁异常）。"""
    key = card_key(card)
    try:
        with conn.cursor() as cur:
            cur.execute(
                LOCK_SQL,
                (
                    key, kind, str(card.get("target") or ""), primary_signal_type(card),
                    str(card.get("conviction_tier") or ""), _safe_float(card.get("conviction_score")),
                    _safe_int(card.get("resonance_count")), subject,
                    STALE_LOCK_MINUTES, cooldown_hours,
                ),
            )
            return cur.fetchone() is not None
    except Exception as e:
        print(f"[highlight_alert] 取锁失败 {key}/{kind}: {e}")
        return False


def mark_result(conn, card: dict, kind: str, status: str, error: str | None = None) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE biz.highlight_alert_log
                   SET status = %s, error_msg = %s
                 WHERE card_key = %s AND alert_kind = %s
                """,
                (status, error, card_key(card), kind),
            )
    except Exception as e:
        print(f"[highlight_alert] 回写状态失败 {card_key(card)}/{kind}: {e}")


# =====================================================================
# 主流程
# =====================================================================

def _shanghai_today() -> str:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    except Exception:
        return datetime.now(timezone.utc).date().isoformat()


def main() -> int:
    ap = argparse.ArgumentParser(description="高亮信号增量邮件提醒")
    ap.add_argument("--dry-run", action="store_true", help="只打印 HTML，不发信、不写去重表")
    ap.add_argument("--snap-date", default=None, help="快照日期 YYYY-MM-DD（默认上海今日）")
    ap.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS,
                    help=f"卡片历史回溯天数（默认 {DEFAULT_LOOKBACK_DAYS}）")
    ap.add_argument("--cooldown-hours", type=int, default=DEFAULT_COOLDOWN_HOURS,
                    help=f"同类提醒冷却小时数（默认 {DEFAULT_COOLDOWN_HOURS}）")
    ap.add_argument("--max-cards", type=int, default=DEFAULT_MAX_CARDS,
                    help=f"单封邮件最多卡片数（默认 {DEFAULT_MAX_CARDS}）")
    ap.add_argument("--to", default=None, help="收件人（默认 SMTP_TO）")
    ap.add_argument("--force", action="store_true", help="忽略冷却窗口（人工补发）")
    ap.add_argument("--demo", action="store_true",
                    help="演示模式：把当前高亮池全部按「新增」渲染并发送，不回写去重表（人工预览排版用，不污染真实告警）")
    args = ap.parse_args()

    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    settings = get_settings(require_database=True)
    snap_date = args.snap_date or _shanghai_today()
    cooldown = 0 if args.force else args.cooldown_hours

    # ── 阶段 1：读快照 + 判定 + 原子加锁（单独提交，避免未提交锁被并发忽略）──
    with get_connection(settings.database_url) as conn:
        ensure_table(conn)
        row = load_snapshot(conn, snap_date)
        if not row:
            print(f"[highlight_alert] 无 <= {snap_date} 的 overview 快照，跳过")
            return 0
        actual_date = str(row["snap_date"])
        highlights = extract_highlights(row["payload"])
        prev_states = load_sent_states(conn, args.lookback_days)

        # 审计 2026-10-03：#4 前置风险面板（市值/30d解锁/审计）注入卡片；
        # #5 跨模型共振：高亮池 ∩ 近 24h 盘面异动告警币种。
        risk_panels = load_risk_panels(conn, highlights)
        for card in highlights:
            aid = card.get("asset_id")
            if aid is not None and int(aid) in risk_panels:
                card["_risk_panel"] = risk_panels[int(aid)]
        scan_syms = load_recent_scan_symbols(conn, hours=24)
        resonance_symbols = sorted(card_symbols(highlights) & scan_syms)

        candidates: list[tuple[dict, str]] = []
        if args.demo:
            # 演示模式：全池按「新增」渲染（跳过 classify 判定与发送锁），
            # 用于人工预览新排版；不回写去重表，不占冷却、不污染真实告警。
            candidates = [(c, ALERT_NEW) for c in highlights]
        else:
            for card in highlights:
                kind = classify_card(card, prev_states.get(card_key(card)))
                if kind:
                    candidates.append((card, kind))
        candidates.sort(key=card_sort_key, reverse=True)
        capped = candidates[:args.max_cards]

        print(f"[highlight_alert] 快照 {actual_date}：高亮 {len(highlights)} 条 → "
              f"新增/升级 {len(candidates)} 条 → 本轮取 {len(capped)} 条")

        if not capped:
            return 0

        if args.dry_run:
            # 与高亮池同步显示：正文给「当前池全集」，本轮新增/升级标徽章，其余「在池」
            _kinds = {card_key(c): k for c, k in candidates}
            print(render_html(build_pool_items(highlights, _kinds, args.max_cards),
                              actual_date, len(highlights), resonance_symbols))
            return 0

        subject = (f"⚡ 高亮信号提醒（新增 "
                   f"{sum(1 for _, k in capped if k == ALERT_NEW)} · 升级 "
                   f"{sum(1 for _, k in capped if k == ALERT_UPGRADE)}）")
        if args.demo:
            granted = capped          # 演示模式：不取发送锁（不写去重表、不占冷却）
        else:
            granted = [it for it in capped if acquire_send_lock(conn, it[0], it[1], subject, cooldown)]
            if not granted:
                print("[highlight_alert] 全部命中冷却窗口，不发信")
                return 0
            if len(granted) < len(capped):
                print(f"[highlight_alert] {len(capped) - len(granted)} 条命中冷却窗口，已跳过")

    # ── 阶段 2：渲染 + 发送（正文同步展示当前高亮池全集）──
    _granted_kinds = {card_key(c): k for c, k in granted}
    pool_items = build_pool_items(highlights, _granted_kinds, args.max_cards)
    html = render_html(pool_items, actual_date, len(highlights), resonance_symbols)
    cards = [c for c, _ in pool_items]
    n_new = sum(1 for _, k in pool_items if k == ALERT_NEW)
    n_up = sum(1 for _, k in pool_items if k == ALERT_UPGRADE)
    n_hold = sum(1 for _, k in pool_items if k == ALERT_HOLD)
    subject = (f"⚡ 高亮信号提醒（新增 {n_new} · 升级 {n_up} · 在池 {n_hold} 条 · "
               f"{symbol_count(cards)} 币"
               + (f" · 共振 {len(resonance_symbols)} 币" if resonance_symbols else "") + "）")

    from crypto_research.clients.notifier import EmailNotifier

    notifier = EmailNotifier(settings)
    if not notifier.configured:
        print("[WARN] SMTP 未配置，跳过发送")
        print(html)
        return 0
    ok, msg = notifier.send(
        subject, html, to=args.to, from_name="高亮信号提醒",
    )
    print(f"[highlight_alert] 发送结果: {'成功' if ok else '失败'} - {msg}")

    # ── 阶段 3：回写状态（失败留 'failed'，下一轮可重试；演示模式不回写）──
    if not args.demo:
        with get_connection(settings.database_url) as conn:
            for card, kind in granted:
                mark_result(conn, card, kind, "sent" if ok else "failed", None if ok else str(msg))

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())