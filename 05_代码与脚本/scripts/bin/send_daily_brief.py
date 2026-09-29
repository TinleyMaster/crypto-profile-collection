#!/usr/bin/env python3
"""每日早报邮件发送（第六刀）。

流程：build_daily_brief.py 生成 brief dict → 渲染 HTML → EmailNotifier 发送。
scheduler.py 注册：daily_brief_email（09:00 Asia/Shanghai，在 daily_brief_snapshot 之后）。

用法：
    python send_daily_brief.py              # 生成 + 发送
    python send_daily_brief.py --dry-run    # 仅打印 HTML，不发送
"""
from __future__ import annotations

import argparse
import html
import os
import re
import sys
from datetime import date

# 路径设置：复用 build_daily_brief.py 的逻辑
_here = os.path.dirname(os.path.abspath(__file__))
_code_root = os.path.dirname(os.path.dirname(_here))
for cand in (os.path.join(_code_root, "workbench"), "/app", _code_root):
    if cand and os.path.isdir(cand) and cand not in sys.path:
        sys.path.insert(0, cand)

# scripts/src 加入 path（crypto_research 包）
_scripts_src = os.path.join(_code_root, "scripts", "src")
if os.path.isdir(_scripts_src) and _scripts_src not in sys.path:
    sys.path.insert(0, _scripts_src)


def _fmt_num(v, decimals=0):
    """安全格式化数字，None → N/A。"""
    if v is None:
        return "N/A"
    try:
        f = float(v)
        return f"{f:,.{decimals}f}"
    except Exception:
        return str(v)


def _fmt_pct(v, decimals=1, signed=True):
    """安全格式化百分比，带颜色方向。"""
    if v is None:
        return "N/A", "#64748b"
    try:
        f = float(v)
    except Exception:
        return str(v), "#64748b"
    color = "#dc2626" if f > 0 else ("#16a34a" if f < 0 else "#64748b")
    sign = "+" if signed and f >= 0 else ""
    return f"{sign}{f:.{decimals}f}%", color


def _clip(s, n: int) -> str:
    """安全截断文本：超长时补省略号并去掉尾部空白。

    审计 2026-09-24 P2-D：精选信号的 reason / 驱动因子此前用裸切片（[:120]/[:30]），
    句子被硬切、无省略号，读起来像被吃掉半句（如「逼近 5% 供应」缺右括号）。
    """
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


# ── U-A 可见性（审计 2026-09-29）：连板注脚只写在 trigger_logic，而 AI 精选高亮卡
# 渲染 `reason_summary`、且该聚合机会被 M4-1 折叠出「精选机会」⇒ 连板信息在邮件里
# 彻底不可见（live 核实：trigger_logic 含「（其中 QNT连续4天 持续强势）」，卡片不显）。
# 此正则从 trigger_logic 提取连板段，补显到高亮卡（不改 reason 口径、不重复）。
_STREAK_HINT_RE = re.compile(r"（其中[^）]*持续(?:强势|走弱|共振)）")


def _streak_hint(logic) -> str:
    """从 trigger_logic 提取 U-A 连板注脚（形如「（其中 QNT连续4天 持续强势）」）；无则空串。"""
    m = _STREAK_HINT_RE.search(str(logic or ""))
    return m.group(0) if m else ""


def _classify_degraded(items: list[str]) -> dict:
    """降级项分类：critical(核心)/warning(辅助)/info(增强)。
    核心降级：影响主决策的关键数据缺失（BTC价格、总市值、恐贪等）
    辅助降级：不影响主结论但缺了就不完整（稳定币、KOL、巨鲸、解锁等）
    增强降级：锦上添花的功能（AI摘要、叙事榜等）
    """
    critical_keywords = ("btc", "eth", "总市值", "market_cap", "fear_greed", "恐贪",
                         "1体量", "2盘面", "3情绪", "overview")
    info_keywords = ("ai_", "ai_summary", "narrative", "叙事", "meme", "chimney",
                     "smart_money", "resonance", "背离")

    critical, warning, info = [], [], []
    for item in items:
        low = str(item).lower()
        if any(k in low for k in critical_keywords):
            critical.append(item)
        elif any(k in low for k in info_keywords):
            info.append(item)
        else:
            warning.append(item)
    return {"critical": critical, "warning": warning, "info": info}


def _render_degraded_badge(brief: dict) -> str:
    """渲染降级项徽标：核心红色/辅助黄色/增强隐藏（仅核心才显示红色告警）。"""
    # 收集所有降级来源：brief.degraded + M9_degraded
    all_degraded = list(brief.get("degraded", []) or [])
    m9 = brief.get("M9_degraded", []) or []
    all_degraded.extend(m9)

    if not all_degraded:
        return ""

    tiers = _classify_degraded(all_degraded)
    parts = []

    # 核心降级：醒目红色
    if tiers["critical"]:
        parts.append(
            f'<div style="margin-top:8px;padding:8px 12px;background:#fef2f2;'
            f'border:1px solid #fecaca;border-radius:6px;color:#991b1b;font-size:12px">'
            f'🚨 <b>核心数据降级</b>：{", ".join(tiers["critical"])}</div>'
        )

    # 辅助降级：黄色，收起来
    if tiers["warning"]:
        parts.append(
            f'<div style="margin-top:6px;padding:6px 10px;background:#fef9c3;'
            f'border-radius:4px;color:#92400e;font-size:11px">'
            f'⚠️ 辅助数据缺失：{", ".join(tiers["warning"])}</div>'
        )

    # 增强降级：不显示（避免噪音，只在debug时看）

    return "".join(parts)


def _resolve_addr_label(raw_label, labels_arr, names_arr, addr):
    """地址标签解析：从数组列优先取，再回退单值字段，最后回退地址截断。
    标签优先级：label_names[0]（具体名称） > labels[0]（类型） > raw_label（单值） > 地址前8位。
    """
    # 1. 优先用 label_names 数组（具体名称，如 "Binance 14", "Gemini"）
    if names_arr and isinstance(names_arr, list) and names_arr:
        first = names_arr[0]
        if first and str(first).strip().lower() not in ("unknown", "", "none", "null"):
            return str(first).strip()
    # 2. 其次用 labels 数组（类型，如 "exchange", "smart_money"）
    if labels_arr and isinstance(labels_arr, list) and labels_arr:
        first = labels_arr[0]
        if first and str(first).strip().lower() not in ("unknown", "", "none", "null"):
            return str(first).strip()
    # 3. 回退到单值字段
    if raw_label and str(raw_label).strip().lower() not in ("unknown", "", "none", "null"):
        return str(raw_label).strip()
    # 4. 最后回退到地址截断
    addr = addr or ""
    if addr and len(addr) >= 8:
        return addr[:8] + "..."
    return "未知地址"


def _fmt_mcap(v):
    """市值/金额缩写：B / M / K。"""
    if v is None:
        return "N/A"
    try:
        f = float(v)
        if f != f or f == float("inf") or f == float("-inf"):  # NaN or Inf
            return "—"
    except Exception:
        return str(v)
    if f >= 1e9:
        return f"${f/1e9:.1f}B"
    if f >= 1e6:
        return f"${f/1e6:.1f}M"
    if f >= 1e4:
        return f"${f/1e3:.0f}K"
    if f >= 1e3:
        # P1-A（2026-09-23 审计）：$1K~$10K 用整数 K 取整误差过大（ETH $2,750→$3K，偏 ~9%），
        # 改用精确值；≥$10K 时整数 K 相对误差已可忽略，仍用紧凑表示。
        return f"${f:,.0f}"
    return f"${f:.0f}"


def _render_liquidation_row(liq: dict) -> str:
    """渲染「24h 爆仓概况」一行（P0-D，只展示不进分）。

    口径（数据层 macro_market.LIQ_OVERVIEW_SCOPE_NOTE 同步的一致口径）：
      CoinGlass 全交易所 · 滚动 24h · 5min 快照 · **池内 N 个标的合计**
      （**禁止**写成「全网爆仓」）。
    1h/4h/12h/24h 属同族滚动窗口 ⇒ 可比较占比（「近 1h 占 24h 的 X%」合法）；
    **禁止**与 biz.liquidation_history 的 4h 分段增量混算。
    缺失（旧快照无该 key / 覆盖率不足 / 列 NULL）⇒ 返回空串，整行隐藏，**不显示 0**。
    多空分列缺失 ⇒ 只显示合计，不显示方向。
    """
    if not isinstance(liq, dict):
        return ""
    total_24h = liq.get("liq_usd_24h")
    if total_24h is None:
        return ""  # 缺失≠0：整行隐藏

    try:
        total_f = float(total_24h)
    except (TypeError, ValueError):
        return ""

    head = f"24h 爆仓 {_fmt_mcap(total_f)}"
    long24, short24 = liq.get("long_24h"), liq.get("short_24h")
    if long24 is not None and short24 is not None:
        bias = "以多头为主" if float(long24) >= float(short24) else "以空头为主"
        head += f"（多 {_fmt_mcap(long24)} / 空 {_fmt_mcap(short24)}，{bias}）"

    liq_1h = liq.get("liq_usd_1h")
    if liq_1h is not None and total_f > 0:
        try:
            head += f" · 近 1h 占 24h 的 {float(liq_1h) / total_f * 100:.1f}%"
        except (TypeError, ValueError):
            pass

    covered = liq.get("symbols_covered")
    scope_note = liq.get("scope_note") or "CoinGlass 全交易所 · 滚动 24h · 5min 快照"
    cover_txt = f"池内 {covered} 个标的合计" if covered else "池内标的合计"
    # 时效披露：批次窗口放宽到 4h 后，必须让读者看到「这个 24h 数截至何时」，
    # 否则停摆期间展示的陈旧值会被误读成实时。ts 缺失或不可解析则省略该段（不阻断整行）。
    as_of_txt = _fmt_liq_as_of(liq.get("ts"))
    foot = f"口径：{scope_note} · {cover_txt}"
    if as_of_txt:
        foot += f" · 数据截至 {as_of_txt}（北京时间）"

    return (
        '<div style="margin-top:6px;background:#f8fafc;border-radius:6px;padding:6px 8px;'
        'font-size:10.5px;color:#334155">'
        f'💥 {head}'
        f'<div style="font-size:9px;color:#94a3b8;margin-top:2px">{foot}</div>'
        '</div>'
    )


# ── U-B：每日变化榜（消费 M5_daily_diff）────────────────────────────────
_DIFF_CATEGORY_ORDER = (
    "价格涨幅榜", "价格跌幅榜", "成交量异动", "量价齐升",
    "赛道轮动", "即将解锁", "市值变化榜",
)
_DIFF_CATEGORY_COLOR = {"价格涨幅榜": "#ef4444", "价格跌幅榜": "#22c55e"}


def _fmt_diff_value(item: dict) -> str:
    """单条变化榜数值：量价齐升/赛道轮动为综合分（X.X 分）、即将解锁为美元金额，其余为百分比。

    口径依据各榜 `metric_label` / 生成器 SQL：
      - price_change_24h / volume_surge_24h（量/市值比）/ market_cap_mover → 百分比；
      - price_volume_surge / sector_rotation → 综合分；
      - unlock_7d → 7 天解锁价值（USD，非百分比，避免渲染成 `621274868.38%`）。
    缺失 metric_value 显示「—」（缺失≠0），metric_label 仅作兜底。
    """
    v = item.get("metric_value")
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return html.escape(str(item.get("metric_label") or "—"))
    if item.get("category") in ("price_volume_surge", "sector_rotation"):
        return f"{f:.1f} 分"
    if item.get("category") == "unlock_7d":
        return _fmt_mcap(f)
    sign = "+" if item.get("direction") == "up" else ""
    return f"{sign}{f:.2f}%"


def _render_daily_diff_html(brief: dict) -> str:
    """渲染「📈 每日变化榜」区块（消费 `brief["M5_daily_diff"]`）。

    - 空 dict / 非 dict / 无任何条目 → 返回空串（不产生空壳，不破坏其他模块）。
    - 7 分类按固定顺序渲染，未知类别追加在后；各榜最多展示已截断的 Top5。
    - 每条 = ⭐ 高亮 / ⚠️ 高危 标记 + symbol + 数值（格式同网页端变化榜）。
    - 纯展示：不查库、不调 AI（数据已由 `macro_market._build_daily_diff_brief` 组装进 brief）。
    """
    m5 = brief.get("M5_daily_diff")
    if not isinstance(m5, dict) or not m5:
        return ""
    keys = [k for k in _DIFF_CATEGORY_ORDER if k in m5] + [
        k for k in m5 if k not in _DIFF_CATEGORY_ORDER
    ]
    rows = []
    for label in keys:
        items = m5.get(label)
        if not isinstance(items, list) or not items:
            continue
        color = _DIFF_CATEGORY_COLOR.get(label, "#334155")
        chips = []
        for it in items:
            if not isinstance(it, dict):
                continue
            sym = html.escape(str(it.get("symbol") or "?"))
            marks = ("⭐" if it.get("is_highlight") else "") + ("⚠️" if it.get("is_risk") else "")
            chips.append(
                '<span style="display:inline-block;margin:1px 8px 1px 0;font-size:10.5px;color:#334155">'
                f'{marks}<b>{sym}</b> '
                f'<span style="color:{color};font-weight:600">{_fmt_diff_value(it)}</span>'
                '</span>'
            )
        if not chips:
            continue
        rows.append(
            '<div style="font-size:11px;color:#475569;line-height:1.9">'
            f'<span style="font-weight:700;color:#0f172a">{html.escape(str(label))}</span>'
            '<span style="color:#94a3b8">｜</span>' + "".join(chips) + '</div>'
        )
    if not rows:
        return ""
    return (
        '<div style="background:#fff;border-radius:10px;padding:10px 14px;margin-bottom:10px;'
        'box-shadow:0 1px 3px rgba(0,0,0,0.05);border-left:4px solid #0ea5e9">'
        '<div style="font-size:12.5px;font-weight:700;color:#0f172a;margin-bottom:4px">'
        '📈 每日变化榜'
        '<span style="font-size:10px;color:#94a3b8;font-weight:400;margin-left:6px">'
        '⭐ 高亮信号 · ⚠️ 高危信号｜各榜 Top5（数据源：每日变化榜快照）</span></div>'
        + "".join(rows) +
        '</div>'
    )


def _fmt_liq_as_of(ts) -> str:
    """把快照批次时间（UTC ISO 串）转成北京时间 `MM-DD HH:MM` 供披露；不可解析则返回空串。"""
    if not ts:
        return ""
    from crypto_research.utils.time_utils import fmt_bj
    return fmt_bj(ts, "%m-%d %H:%M", fallback="")


def _fmt_data_as_of(v) -> str:
    """把 overview 快照的 `fetched_at`（Unix 秒 或 ISO 串）转成北京时间 `MM-DD HH:MM`。

    审计 2026-09-24 P2-C：大盘脉搏此前只显示日期、无数据时点，读者无法判断新鲜度。
    不可解析则返回空串（渲染层省略该段，不显示错误时间）。
    """
    if v is None or v == "":
        return ""
    from crypto_research.utils.time_utils import fmt_bj
    # Unix 秒（int / float / 数字串）
    try:
        epoch = float(v)
        from datetime import datetime, timezone
        return fmt_bj(datetime.fromtimestamp(epoch, tz=timezone.utc), "%m-%d %H:%M", fallback="")
    except (TypeError, ValueError):
        pass
    # ISO 串兜底
    return fmt_bj(v, "%m-%d %H:%M", fallback="")


# ── P1-a 交易方向可执行化：一条建议要进「交易方向」区，必须齐备这 6 个可判定字段 ──
# 缺任一 → 该条降级进「👀 观察（不构成建议·缺可判定条件）」区，不可静默丢弃（信息仍在，只是不定性为建议）
_REQUIRED_TRADE_FIELDS = ("trigger", "invalidate", "target", "horizon", "ref_price", "ref_as_of")
_TRADE_FIELD_CN = {
    "trigger": "进场条件", "invalidate": "失效条件", "target": "目标",
    "horizon": "期限", "ref_price": "参照价", "ref_as_of": "参照时间",
}
# ── P1-b 观望闸门：data_quality 中 status=ok 的维度数低于此值 → 证据不足以给方向，强制「今日无操作」──
_DQ_MIN_OK_FOR_TRADE = 2

# ── P1-2（审计 2026-09-28）：可判定字段的「伪值」也要算缺失 ───────────────────
# LLM 有时把缺失字段写成字面量 "N/A" / "无" / "—"，它们非空却不可判定（如 XRP「参照 N/A」），
# 会导致缺字段条目混进「交易方向」区。与 _REQUIRED_TRADE_FIELDS 同源判定。
_VALUE_PLACEHOLDERS = {"n/a", "na", "none", "null", "无", "—", "-", "--", "未知", "待定"}


def _is_placeholder_value(v) -> bool:
    """占位符/伪值判定：None、空白、或 N/A·无·— 一类不可判定字面量。"""
    if v is None:
        return True
    s = str(v).strip()
    return (not s) or (s.lower() in _VALUE_PLACEHOLDERS)


def _trade_missing_fields(s: dict) -> list:
    """返回该条建议缺失的可判定字段（中文名列表）。空列表 = 六要素齐备、可执行。"""
    s = s or {}
    return [
        _TRADE_FIELD_CN[f] for f in _REQUIRED_TRADE_FIELDS
        if _is_placeholder_value(s.get(f))
    ]


# ── M4 单一口径裁决（方案_大盘早报_投资指导意义重构 §3.3 M4） ──────────────
# 同一封邮件内同一 target 只保留一条结论：
#   · 「精选机会」卡里凡是已在「交易方向 / AI精选高亮 / 高危信号 / 赛道轮动」出现过结论的
#     标的，一律折叠为「关联」一行，不再并排展示第二份分数/方向（M4-1）；
#   · 机会清单先按证据等级（HIGH/MED/LOW）分组、组内再按分数排序，禁止跨口径按数值直排（M4-2）；
#   · 既领涨/入选机会、又入高危的标的必须输出裁决语，不允许两条并列无解释（M4-4）。
_TIER_RANK = {"HIGH": 3, "MED": 2, "LOW": 1}

# ── W-03 gate 分层（M4-2 的下一层：可验证性优先） ───────────────────────────
# 28 条机会里 22 条并列 55 分、MED 组内分数区分度塌陷；而「从未回测」的
# exempt_not_backtestable（样本 0）分数区间 73–91 反而压过「已回测」的
# calibrated_low（样本 67，55 分）——最不可验证的信号排在最前。
# 故在 tier 之上再加一层 gate 权级（按「是否被回测」排序，已定死）：
#   calibrated_ok(4) > calibrated_low(3) > preliminary(2) > exempt_not_calibrable(1)
#   > exempt_not_backtestable(0)
# 未知 gate / calibration_status 缺失 → 0（最不安全的一侧，不是中间值）。
_GATE_RANK = {
    "calibrated_ok":           4,   # 有回测背书
    "calibrated_low":          3,   # 已回测，命中率低
    "preliminary":             2,   # 已回测，样本不足
    "exempt_not_calibrable":   1,
    "exempt_not_backtestable": 0,   # 从未回测
}


def _norm_target_key(t) -> str:
    """target 归一化键：小写、`&`→`and`、仅保留字母数字（中文保留），用于跨板块同一标的归并。"""
    s = str(t or "").strip().lower().replace("&", "and")
    return "".join(ch for ch in s if ch.isalnum())


def _target_keys(*vals) -> set:
    """把 target/symbol/name 等别名一起归一化成键集，任一命中即视为同一标的。"""
    return {k for k in (_norm_target_key(v) for v in vals) if k}


def _opp_gate(o: dict) -> str:
    """取机会的校准门（gate）。无 calibration_status 或非 dict → ""（按 0 处理）。"""
    cs = (o or {}).get("calibration_status")
    if not isinstance(cs, dict):
        return ""
    return str(cs.get("gate") or "")


# ── W-14：校准样本量诚实标注（命中率必须与样本量、窗口同时出现）──
def _cal_window(wstart, wend) -> str:
    """窗口格式化：窗口 08-31~09-25（缺一端则只显示另一端）。"""
    def _md(v):
        s = str(v or "")
        return s[5:10] if len(s) >= 10 else s
    a, b = _md(wstart), _md(wend)
    if a and b:
        return f"{a}~{b}"
    return b or a


def _cal_line_html(cal) -> str:
    """W-14：机会卡的校准行。

    - 命中率必须同时给出样本量与窗口；
    - `sample_count < 30` → 「样本不足，仅供参考」；
    - `hit_rate < 50%` → 「历史命中率低于抛硬币」；
    - `exempt_*`（从未回测）→ 「从未回测」。
    无校准信息 → 返回 ""（不出行）。
    """
    if not isinstance(cal, dict) or not cal:
        return ""
    gate = str(cal.get("gate") or "")
    n = cal.get("sample_count")
    hr = cal.get("hit_rate")
    try:
        n_int = int(n) if n is not None else None
    except (TypeError, ValueError):
        n_int = None
    seg = ""
    if gate.startswith("exempt_") or n_int == 0:
        seg = "从未回测"
    elif hr is not None:
        _win = _cal_window(cal.get("window_start"), cal.get("window_end"))
        seg = (f"命中率 {float(hr) * 100:.1f}%（样本 {n_int if n_int is not None else '—'}"
               + (f"，窗口 {_win}）" if _win else "）"))
        if n_int is not None and n_int < 30:
            seg += " · 样本不足，仅供参考"
        if float(hr) < 0.5:
            seg += " · 历史命中率低于抛硬币"
    elif n_int is not None:
        seg = f"样本 {n_int}" + (" · 样本不足，仅供参考" if n_int < 30 else "")
    if not seg:
        return ""
    return (f'<div style="font-size:9.5px;color:#94a3b8;margin-top:3px">🧪 校准：{seg}</div>')


def _tier_score_key(o: dict):
    """W-03 排序键：可验证性（gate）→ 证据等级（tier）→ 分数。

    M4-2 只按 tier 分层，无法区分「同属 MED 的已回测 calibrated_low 与从未回测
    exempt_not_backtestable」；升级为三元组后，未回测的信号一律沉到已回测之后。
    未知 gate / calibration_status 缺失按 0（最不安全的一侧）处理。
    （同分细化沿用 47bcb4d 的 decayed_score 设计，落在主榜 select_highlight_signals，
     本函数不承担该项，勿在此回退。）
    """
    gate = _opp_gate(o)
    tier = str((o or {}).get("conviction_tier") or "").upper()
    score = (o or {}).get("conviction_score")
    return (
        _GATE_RANK.get(gate, 0),
        _TIER_RANK.get(tier, 0),
        score if isinstance(score, (int, float)) else -1,
    )


# ── W-07 数据状态表（下钻到字段级：表有行 ≠ 字段有值）───────────────────────
# 置于 M0 每日定调之后、第一张数据卡之前：读者先看清哪块能信、截至哪天、滞后几天。
# empty/error → 状态列「数据不可用」（不显示 0 / —，避免被读成"净流入为零"）。
_DQ_LAG_WARN = {
    "恐贪指数": 1, "BTC OI": 1, "CEFI 指数": 1, "赛道TVL": 1,
    "ETF资金流": 4, "赛道市值": 1, "大盘快照": 1,
    "交易所净流量": 1, "巨鲸持仓变化": 1,
}
_DQ_STATUS_CN = {
    "ok": ("正常", "#16a34a"),
    "partial": ("部分可用", "#d97706"),
    "empty": ("数据不可用", "#dc2626"),
    "error": ("数据不可用", "#dc2626"),
}


def _dq_lag_warn(section: str) -> int:
    return _DQ_LAG_WARN.get(str(section or ""), 1)


# ── W-08-4：链上大额转账「参考类」名单（稳定币 / 黄金代币）────────────────────
# 这些资产的转账不构成 BTC/ETH 供给变动，只能作避险情绪/结算通道参考。
# 依据：2026-09-27 邮件把「XAUt 和 USDC 大额转账为主」当供给信号表述。
_REFERENCE_SYMBOLS = {
    "USDT", "USDC", "DAI", "TUSD", "FDUSD", "PYUSD", "RLUSD", "USDE", "USDS",
    "BUSD", "USDD", "GUSD", "FRAX", "XAUT", "PAXG",
}


def _dq_status_map(brief: dict) -> dict:
    """W-07：返回 {section: status} 路由表。"""
    ai = brief.get("M0_ai_summary") or {}
    out: dict = {}
    for d in (ai.get("data_quality") or []):
        if isinstance(d, dict) and d.get("section"):
            out[str(d["section"])] = str(d.get("status") or "").lower()
    return out


def _dq_usable(brief: dict, section: str) -> bool:
    """W-07 路由校验：该维度是否可用（status == 'ok'）。

    渲染层凡取该维度的数值位，必须先过此校验；不可用一律渲染「数据不可用」，禁止渲染 0。
    缺记录时按可用处理（不误伤无 data_quality 的旧 payload）。
    """
    return _dq_status_map(brief).get(str(section), "ok") == "ok"


def _data_status_table_html(dq: list) -> str:
    """W-07：渲染置顶数据状态表（模块 | 截至 | 滞后 | 可用率 | 状态）。

    - status != "ok" → 状态列「数据不可用」，可用率列同样显示「数据不可用」
      （绝不渲染 0，避免被读成"净流入为零"）；
    - lag_days >= 该源阈值 → 滞后列标黄。
    """
    rows = [d for d in (dq or []) if isinstance(d, dict)]
    if not rows:
        return ""
    body = []
    for d in rows:
        sec = str(d.get("section") or "?")
        st = str(d.get("status") or "").lower()
        st_cn, st_color = _DQ_STATUS_CN.get(st, ("未知", "#64748b"))
        as_of = str(d.get("as_of") or "—")
        lag = d.get("lag_days")
        if isinstance(lag, int):
            lag_txt = f"{lag} 天"
            lag_color = "#b45309" if lag >= _dq_lag_warn(sec) else "#64748b"
        else:
            lag_txt, lag_color = "—", "#94a3b8"
        if st == "ok":
            cov_txt = f"{d.get('usable')}/{d.get('total')}" if d.get("total") else "—"
            cov_color = "#334155"
        elif st == "partial":
            cov_txt = f"{d.get('usable')}/{d.get('total')}" if d.get("total") else "—"
            cov_color = "#b45309"
        else:
            cov_txt, cov_color = "数据不可用", "#dc2626"
        # P0-2（审计 2026-09-28）：不可用维度的口径披露（如「替代源估算」「解锁事件由催化剂管道提供」），
        # 避免同一封邮件里「数据不可用」旁边又给具体数字而无任何交代。
        _note = str(d.get("note") or "").strip()
        _note_html = (
            f'<div style="font-size:9px;color:#b45309;margin-top:1px;line-height:1.4">{_note}</div>'
            if _note else ""
        )
        body.append(
            f'<tr>'
            f'<td style="padding:3px 6px;font-size:11px;color:#334155">{sec}{_note_html}</td>'
            f'<td style="padding:3px 6px;font-size:11px;color:#64748b">{as_of}</td>'
            f'<td style="padding:3px 6px;font-size:11px;color:{lag_color}">{lag_txt}</td>'
            f'<td style="padding:3px 6px;font-size:11px;color:{cov_color}">{cov_txt}</td>'
            f'<td style="padding:3px 6px;font-size:11px;font-weight:600;color:{st_color}">{st_cn}</td>'
            f'</tr>'
        )
    return f"""
          <!-- 模块 0.1：📋 数据状态（置顶，字段级可用率） -->
          <div style="background:#fff;border-radius:10px;padding:10px 14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,0.05)">
            <div style="font-size:13px;font-weight:700;color:#0f172a;margin-bottom:6px">📋 数据状态</div>
            <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse">
              <tr>
                <td style="padding:3px 6px;font-size:10px;color:#94a3b8">模块</td>
                <td style="padding:3px 6px;font-size:10px;color:#94a3b8">截至</td>
                <td style="padding:3px 6px;font-size:10px;color:#94a3b8">滞后</td>
                <td style="padding:3px 6px;font-size:10px;color:#94a3b8">可用率</td>
                <td style="padding:3px 6px;font-size:10px;color:#94a3b8">状态</td>
              </tr>
              {''.join(body)}
            </table>
            <div style="font-size:9.5px;color:#94a3b8;line-height:1.5;margin-top:4px">
              「可用率」= 字段级可用条数/总条数（表有行 ≠ 字段有值）；标注「数据不可用」的维度不得作为结论依据。
            </div>
          </div>
        """


# ── P0-1（审计 2026-09-28）：顶部风险叙事与「全做多」交易方向的缝合层 ──────────
# 根因：研判模块吐「顶部/贪婪」风险，交易模块只生成多头信号，中间没有缝合层，
# 读者拿到「环境危险但快做多」。本层在交易方向区强制追加环境约束注脚 + 失效翻转视角。
_TOP_RISK_FEAR_GREED = 70     # 恐贪 ≥70 视为贪婪区（与 fng_greed_min 一致）
_TOP_RISK_MIN_POSITION = "建议单笔不超过常态仓位的 50%"


def _top_risk_guard_html(phase, fear_greed, has_trades: bool) -> str:
    """顶部/贪婪环境下的交易约束注脚。无触发条件时返回空串（不渲染空壳）。"""
    flags = []
    try:
        if phase and "顶" in str(phase):
            flags.append(f"BTC 周期：{phase}")
    except Exception:
        pass
    try:
        fg = float(fear_greed) if fear_greed is not None and str(fear_greed) != "" else None
    except (TypeError, ValueError):
        fg = None
    if fg is not None and fg >= _TOP_RISK_FEAR_GREED:
        flags.append(f"恐贪 {fg:.0f}（贪婪区）")
    if not flags:
        return ""
    _scope = "以上方向" if has_trades else "今日方向"
    _items = [
        "战术性参与：以短线 / 事件驱动为主，不新开杠杆",
        f"轻仓：{_TOP_RISK_MIN_POSITION}",
        "必带止损：未给失效价的方向不得执行",
        "可部分止盈：已有持仓者优先逢高减仓 / 对冲，不追高",
    ]
    _li = "".join(f"<li>{x}</li>" for x in _items)
    return f"""
    <div style="background:#fef9c3;border:1px solid #fde047;border-radius:6px;padding:8px 10px;margin:6px 0">
      <div style="font-size:11px;font-weight:700;color:#92400e">⚠️ 高风险环境约束（{' · '.join(flags)}）</div>
      <ul style="margin:4px 0 0 16px;padding:0;font-size:10.5px;color:#78350f;line-height:1.6">{_li}</ul>
      <div style="font-size:10.5px;color:#92400e;margin-top:4px;line-height:1.5">
        失效翻转：若 BTC 跌破 50 日均线或恐贪回落至中性以下，{_scope}全部失效，转观望 / 减仓。
      </div>
    </div>
    """


# ── P1-1（审计 2026-09-28）：今日操作清单（TL;DR）→ 前 1/3 内可见 ────────────
def _build_tldr_html(trade_ready: list, all_opps: list, owners: dict | None = None) -> str:
    """把最可执行的方向摘要置顶（六要素齐备的交易方向优先，其次机会清单）。

    `owners`（M4-1 折叠归属表）用于排除已在其他板块给出结论的标的 —— 否则摘要会把
    「精选机会」里被折叠掉的重复结论又渲染一遍（破坏 M4-1「同一标的只留一条结论」）。
    """
    rows = []
    _from_trades = False
    for s in (trade_ready or [])[:3]:
        asset = (s or {}).get("asset") or "?"
        direction = (s or {}).get("direction") or ""
        trigger = _clip((s or {}).get("trigger") or "", 70)
        invalidate = _clip((s or {}).get("invalidate") or "", 50)
        if not trigger:
            continue
        rows.append(
            f'<div style="font-size:11px;color:#0f172a;line-height:1.6;margin-bottom:3px">'
            f'<b>{asset}</b> <span style="color:#64748b">{direction}</span> · '
            f'{trigger}' + (f'（失效：{invalidate}）' if invalidate else '') + '</div>'
        )
        _from_trades = True
    # 若交易方向不足 3 条，用机会清单补足（含具体逻辑的优先）
    if len(rows) < 3:
        _seen = {str((s or {}).get("asset") or "").strip().lower() for s in (trade_ready or [])}
        for o in (all_opps or []):
            if len(rows) >= 3:
                break
            tgt = str(((o or {}).get("target") or (o or {}).get("symbol") or "")).strip()
            if not tgt or tgt.lower() in _seen:
                continue
            # M4-1：已在「高亮 / 高危 / 赛道轮动」给出结论的标的，不在摘要里重复。
            if owners:
                _tk = _target_keys((o or {}).get("target"), (o or {}).get("symbol"), (o or {}).get("name"))
                if any(k in owners for k in _tk):
                    continue
            logic = _clip((o or {}).get("trigger_logic") or "", 70)
            if not logic:
                continue
            _seen.add(tgt.lower())
            rows.append(
                f'<div style="font-size:11px;color:#0f172a;line-height:1.6;margin-bottom:3px">'
                f'<b>{tgt}</b> <span style="color:#64748b">观察/机会</span> · {logic}</div>'
            )
    if not rows:
        return ""
    # P2-3（审计 2026-09-29）：无「新开方向」时，标题/口径须与 AI 定调「今日无操作」区分，
    # 否则读者见「无操作」又见「操作清单」会困惑。
    _title = ("🎯 今日操作清单（摘要）" if _from_trades
              else "🎯 观察 / 持仓参考（非新开方向）")
    _desc = ("摘要摘自交易方向 / 机会清单；完整条件见下方对应板块，不构成投资建议。"
             if _from_trades else
             "今日无新开方向（见 AI 定调「今日无操作」）；以下为观察 / 既有持仓参考，非新开建仓建议。")
    return f"""
      <!-- 模块0.01：今日操作清单（TL;DR） -->
      <div style="background:#fff;border-radius:10px;padding:10px 14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,0.05);border-left:4px solid #0ea5e9">
        <div style="font-size:12.5px;font-weight:700;color:#0f172a;margin-bottom:4px">{_title}</div>
        {''.join(rows)}
        <div style="font-size:9.5px;color:#94a3b8;margin-top:4px">{_desc}</div>
      </div>
    """


# ── P1-3（审计 2026-09-28）：高危信号逐条「风险点 + 应对」解读 ────────────────
# 稳定币类（USDC/USDS 等）出现在高危榜时，读者无从理解，必须解释「为何高危」。
_STABLE_RISK_SYMBOLS = {"USDC", "USDS", "USDT", "DAI", "TUSD", "FDUSD", "PYUSD", "RLUSD",
                        "USDE", "BUSD", "USDD", "GUSD", "FRAX", "XAUT", "PAXG", "CBBTC"}


def _risk_one_liner(r: dict) -> str:
    """从风险信号里抽一行「风险点 + 应对」（数据驱动，不臆造）。"""
    r = r or {}
    tgt = str(r.get("target") or r.get("symbol") or "?").strip()
    key_metric = str(r.get("key_metric") or "").strip()
    hint = str(r.get("action_hint") or "").strip()
    logic = str(r.get("trigger_logic") or "").strip()
    sig = str(r.get("signal_type") or "").strip()
    _up = tgt.upper()
    if _up in _STABLE_RISK_SYMBOLS:
        return f"{tgt}：稳定币 / 锚定资产风险（脱锚、储备或监管），" \
               f"应对 = 避险时缩短稳定币敞口、分散托管。"
    # 优先用信号自带的风险点与应对，其次触发器逻辑
    risk = key_metric or logic
    if len(risk) > 60:
        risk = _clip(risk, 60)
    if risk and hint:
        return f"{tgt}：{risk}，应对 = {hint}。"
    if risk:
        return f"{tgt}：{risk}。"
    if hint:
        return f"{tgt}：应对 = {hint}。"
    if sig == "fng_extreme":
        return f"{tgt}：情绪极值，防回撤；应对 = 降杠杆、部分止盈。"
    return f"{tgt}：系统高危信号（综合风险），请结合下方风险提示谨慎对待。"


# ── P1-4（审计 2026-09-28）：证据覆盖 N/M 的缺项明细 ──────────────────────────
def _coverage_missing_names(dq: list) -> list:
    """返回 data_quality 中 status != ok 的维度名列表（供「证据覆盖」旁披露缺哪几项）。"""
    out = []
    for d in (dq or []):
        if not isinstance(d, dict):
            continue
        if str(d.get("status") or "").lower() != "ok":
            out.append(str(d.get("section") or "?"))
    return out


def _build_target_registry(brief: dict, ai_trade_ready: list):
    """M4-1 / M4-4：收集全邮件各板块的 target 结论 → 折叠归属 + 冲突裁决。

    返回 (owners, arbitrations)：
      owners: {归一化键: 板块名} —— 已在「交易方向/高亮/高危/赛道轮动」给出结论的标的，
              「精选机会」卡据此折叠重复项（不并排展示）。领涨币不参与折叠归属（仅用于裁决）。
      arbitrations: [{target, text}] —— 既领涨/入高亮、又入高危的标的裁决语（两条并列时给解释）。
    """
    owners: dict = {}
    risk_names: dict = {}
    lead_names: dict = {}
    highlight_keys: set = set()

    # 1) AI 交易方向（六要素齐备、可执行，最高优先）
    for s in ai_trade_ready or []:
        for k in _target_keys((s or {}).get("asset"), (s or {}).get("target")):
            owners.setdefault(k, "交易方向")

    # 2) AI 精选高亮
    for h in (brief.get("M3_highlights") or []):
        ai = h.get("ai_analysis_v2") or {}
        if not ai or ai.get("error"):
            continue
        for k in _target_keys(h.get("target"), h.get("symbol")):
            owners.setdefault(k, "AI 精选高亮")
            highlight_keys.add(k)

    # 3) 今日高危信号
    for r in (brief.get("M4_risks") or []):
        ai = r.get("ai_analysis_v2") or {}
        if not ai or ai.get("error"):
            continue
        tgt = r.get("target")
        for k in _target_keys(tgt, r.get("symbol")):
            owners.setdefault(k, "今日高危信号")
            risk_names.setdefault(k, tgt)

    # 4) 赛道轮动（赛道名参与折叠归属；领涨币只进裁决）
    for s in ((brief.get("M2_sector_flow") or {}).get("sectors") or []):
        label = s.get("sector_label") or s.get("sector_key")
        for k in _target_keys(label, s.get("sector_key")):
            owners.setdefault(k, "赛道轮动")
        for lc in (s.get("leaders") or []):
            sym = lc if isinstance(lc, str) else lc.get("symbol")
            if sym:
                for k in _target_keys(sym):
                    lead_names.setdefault(k, (sym, label))

    # ── 冲突裁决：同一标的既领涨/入高亮，又入高危（两条方向结论并列时必给解释）──
    arbitrations = []
    for k, tgt in risk_names.items():
        is_lead = k in lead_names
        is_hl = k in highlight_keys
        if not (is_lead or is_hl):
            continue
        if is_lead:
            sym, sector = lead_names[k]
            text = (f"{sym} 领涨{sector or '赛道'}属资金驱动，"
                    f"同时存在高危信号风险，判定：不参与")
            nm = sym
        else:
            text = f"{tgt} 同时出现在「AI 精选高亮」与「今日高危信号」，两口径冲突，判定：不参与"
            nm = tgt
        arbitrations.append({"target": nm, "text": text})
    return owners, arbitrations


# ── W-12：变更日志（与昨日 diff）渲染 ──
_M0_DELTA_DIR_CN = {"long": "看多", "short": "看空", "watch": "观望",
                    "neutral": "中性", "no_trade": "无操作", "": "—"}
_M0_DELTA_METRIC_CN = {"fear_greed": "恐贪指数"}


def _m0_delta_dir(d) -> str:
    s = str(d or "")
    return _M0_DELTA_DIR_CN.get(s, s or "—")


def _m0_delta_metric(m) -> str:
    return _M0_DELTA_METRIC_CN.get(str(m), str(m))


# ── W-15：风险条目可判定化（阈值 + 后验）──
_RISK_THRESHOLD_KW = ("阈值", "以上", "以下", "超过", "跌破", "站上", "高于", "低于",
                      "超买", "超卖", "≥", "<=", ">=", "＞", "＜")
_POSTERIOR_SAMPLE_RE = re.compile(r"样本\s*\d|历史\s*\d+\s*次|窗口\s*\S*\d")


def _risk_has_threshold(text) -> bool:
    """W-15：风险条目是否含「数值 + 阈值词」的可判定条件。"""
    s = str(text or "")
    return any(c.isdigit() for c in s) and any(k in s for k in _RISK_THRESHOLD_KW)


def _risk_mark_posterior(text) -> str:
    """W-15：无后验样本的风险条目必须显式标「（无后验样本 · 经验判断）」。"""
    s = str(text or "")
    if _POSTERIOR_SAMPLE_RE.search(s):
        return s
    return s + "（无后验样本 · 经验判断）"


def _m0_delta_html(brief: dict) -> str:
    """W-12：变更日志，置于 M0 定调之后的第二块。

    - `M0_delta` 缺失（旧 payload）→ 不出块；
    - `no_baseline` → 输出 W-11 同款「无昨日基准」提示，不输出空块；
    - 有基准但五类全空 → 显式「较上一期无变化」（区分"变化/未变化"）；
    - 有变化 → 按 维持/新增/方向反转/新增风险/越阈 五类分列。
    """
    d = brief.get("M0_delta")
    if not isinstance(d, dict):
        return ""
    if d.get("status") == "no_baseline":
        _bd = d.get("baseline_date") or "昨日"
        _inner = (f'<div style="font-size:11px;color:#b45309">'
                  f'⚠️ 无昨日基准（{_bd} 快照缺失），本期无变化对比</div>')
    else:
        _rows = []
        _keep = d.get("维持") or []
        if _keep:
            _t = "、".join(
                f"{e.get('target')} {_m0_delta_dir(e.get('direction'))}（第 {e.get('days')} 日）"
                for e in _keep[:6])
            _rows.append(f'<div style="font-size:11px;color:#475569;line-height:1.7">'
                         f'<span style="color:#0f766e;font-weight:700">维持</span> {_t}</div>')
        _add = d.get("新增") or []
        if _add:
            _t = "、".join(f"{e.get('target')} {_m0_delta_dir(e.get('direction'))}" for e in _add[:6])
            _rows.append(f'<div style="font-size:11px;color:#475569;line-height:1.7">'
                         f'<span style="color:#1d4ed8;font-weight:700">新增</span> {_t}</div>')
        _flip = d.get("方向反转") or []
        if _flip:
            _t = "；".join(
                f"{e.get('target')} {_m0_delta_dir(e.get('from'))} → {_m0_delta_dir(e.get('to'))}"
                + (f'（{_clip(e.get("reason"), 40)}）' if e.get("reason") else "")
                for e in _flip[:5])
            _rows.append(f'<div style="font-size:11px;color:#475569;line-height:1.7">'
                         f'<span style="color:#b45309;font-weight:700">方向反转</span> {_t}</div>')
        _nr = d.get("新增风险") or []
        if _nr:
            _t = "、".join(str(e.get("target")) for e in _nr[:6])
            _rows.append(f'<div style="font-size:11px;color:#475569;line-height:1.7">'
                         f'<span style="color:#dc2626;font-weight:700">新增风险</span> {_t}</div>')
        _cr = d.get("越阈") or []
        if _cr:
            _t = "；".join(
                f'{_m0_delta_metric(e.get("metric"))} {e.get("prev")} → {e.get("curr")}'
                f'（阈值 {e.get("threshold")}）' for e in _cr[:5])
            _rows.append(f'<div style="font-size:11px;color:#475569;line-height:1.7">'
                         f'<span style="color:#7c3aed;font-weight:700">越阈</span> {_t}</div>')
        _inner = ("".join(_rows) if _rows
                  else '<div style="font-size:11px;color:#64748b">较上一期无变化'
                       '（机会 / 风险 / 关键指标均未变）</div>')
    return f"""
      <!-- 模块0.05：变更日志（W-12）-->
      <div style="background:#fff;border-radius:10px;padding:10px 14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,0.05)">
        <div style="font-size:12.5px;font-weight:700;color:#0f172a;margin-bottom:6px">📋 与昨日变化</div>
        {_inner}
      </div>
    """


def render_brief_html(brief: dict) -> str:
    """
    早报 HTML V2 — 6 大模块 + AI 定调。
    模块顺序：AI定调 → AI精选高亮 → 大盘脉搏 → 赛道轮动 → 机构资金 → 链上异动 → 催化剂 → 机会清单
    """
    today = date.today().isoformat()
    m0 = brief.get("M0_tldr", {})
    ai_summary = brief.get("M0_ai_summary") or {}
    diff = brief.get("DIFF", {})
    sector_flow = brief.get("M2_sector_flow") or {}
    etf_flow = brief.get("M2_etf_flow") or {}
    whale_moves = brief.get("M2_whale_moves") or {}
    exchange_flow = brief.get("M2_exchange_flow") or {}
    holder_conc = brief.get("M2_holder_concentration") or {}
    stab = brief.get("M2_stablecoin") or {}
    upcoming_unlocks = brief.get("M6_upcoming_unlocks") or {}
    kol_onchain = brief.get("kol_onchain") or {}

    # 全部机会排序：M4-2 先按证据等级（HIGH/MED/LOW）分组，组内再按分数 —— 禁止跨口径按数值直排
    all_opps = sorted(
        (brief.get("M8_opportunities") or []) + (brief.get("M8_watchlist") or []),
        key=_tier_score_key,
        reverse=True,
    )

    html_parts = []
    # 外层容器
    html_parts.append(f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;max-width:680px;margin:auto;background:#f1f5f9;padding:10px;color:#0f172a;line-height:1.5">
    """)

    # ════════════════════════════════════════════════════════
    # 模块 0：🔥 AI 今日定调（最顶部，最醒目）
    # ════════════════════════════════════════════════════════
    ai_headline = ai_summary.get("headline") or ""
    ai_regime = ai_summary.get("market_regime") or ""
    ai_bias = ai_summary.get("bias") or ""
    ai_conviction = ai_summary.get("conviction") or "medium"
    ai_key_drivers = ai_summary.get("key_drivers") or []
    ai_sector_rotation = ai_summary.get("sector_rotation") or ""
    ai_trade_suggestions = ai_summary.get("trade_suggestions") or []
    ai_risk_warnings = ai_summary.get("risk_warnings") or []
    ai_watchlist = ai_summary.get("watchlist") or []

    # 审计 2026-09-24 P2-A：AI 定调方向（如「偏多」）与当日盘面（BTC/ETH/总市值全跌）并置时
    # 易被读成「今日看涨」。当方向含「多」而当日权重币与总市值中至少两项明显回调（≤ -1%）时，
    # 加一句依据说明，明确「结构性判断 ≠ 当日走势」。
    _bias_caveat_html = ""
    try:
        _bc = float(m0.get("btc_change_24h_pct")) if m0.get("btc_change_24h_pct") is not None else None
        _ec = float(m0.get("eth_change_24h_pct")) if m0.get("eth_change_24h_pct") is not None else None
        _mc = (float(diff.get("total_mcap_pct"))
               if isinstance(diff, dict) and diff.get("total_mcap_pct") is not None else None)
        _down = [v for v in (_bc, _ec, _mc) if v is not None and v <= -1.0]
        if ai_bias and "多" in str(ai_bias) and len(_down) >= 2:
            _bias_caveat_html = (
                '<div style="font-size:10.5px;color:#fcd34d;line-height:1.5;margin-bottom:10px">'
                '⚠️ 方向偏多属结构性判断；当日 BTC/ETH/总市值同步回调，勿与当日走势混读。</div>'
            )
    except Exception:
        _bias_caveat_html = ""

    # 渲染层兜底（LLM 非确定性）：prompt 允许 trade_suggestions 为空 ≠ LLM 会可靠输出空数组。
    # 当日横盘（|BTC|、|ETH| ≤ 1%）且无新鲜高亮/风险信号时，若 AI 仍给方向，强制降级为「观察」。
    _ai_trade_suggestions = ai_trade_suggestions
    _no_trade_reason = str(ai_summary.get("no_trade_reason") or "")
    try:
        _bc0 = float(m0.get("btc_change_24h_pct")) if m0.get("btc_change_24h_pct") is not None else None
        _ec0 = float(m0.get("eth_change_24h_pct")) if m0.get("eth_change_24h_pct") is not None else None
        _flat = (_bc0 is not None and _ec0 is not None and abs(_bc0) <= 1.0 and abs(_ec0) <= 1.0)
        _no_signal = not (brief.get("M3_highlights") or []) and not (brief.get("M4_risks") or [])
        if _flat and _no_signal and _ai_trade_suggestions:
            print(f"[render_brief_html] 兜底降级：当日横盘(BTC {_bc0}%/ETH {_ec0}%)且无新鲜信号，"
                  f"AI 仍给出 {len(_ai_trade_suggestions)} 条方向 → 强制置为「观察」")
            _ai_trade_suggestions = []
            _no_trade_reason = _no_trade_reason or "当日横盘、无新鲜信号，无明确可执行机会"

        # P1-b 观望闸门：证据覆盖不足（data_quality 中 status=ok 的维度数 < 阈值）时，
        # 即使 AI 给了方向也强制「今日无操作」。只降不升——本闸门永不把「无操作」升格为「有方向」。
        _dq0 = ai_summary.get("data_quality") or []
        _dq_ok0 = sum(1 for d in _dq0 if str((d or {}).get("status")) == "ok")
        if _dq0 and _dq_ok0 < _DQ_MIN_OK_FOR_TRADE and _ai_trade_suggestions:
            print(f"[render_brief_html] 观望闸门：证据覆盖仅 {_dq_ok0}/{len(_dq0)} 项"
                  f"（<{_DQ_MIN_OK_FOR_TRADE}），AI 仍给出 {len(_ai_trade_suggestions)} 条方向 → 强制「今日无操作」")
            _ai_trade_suggestions = []
            _no_trade_reason = _no_trade_reason or f"数据覆盖不足（{_dq_ok0}/{len(_dq0)} 项可用），证据不足以支撑方向"
    except Exception:
        pass

    # ── P1-a 可执行化分流：六要素齐备 → 交易方向区；缺任一 → 观察区（不可静默丢弃）──
    _trade_ready, _trade_excluded = [], []
    for _s in _ai_trade_suggestions:
        if _trade_missing_fields(_s):
            _trade_excluded.append(_s)
        else:
            _trade_ready.append(_s)
    if _trade_excluded:
        _ex_desc = "、".join(
            f"{(_s or {}).get('asset') or '?'}（缺 {'/'.join(_trade_missing_fields(_s))}）"
            for _s in _trade_excluded
        )
        print(f"[render_brief_html] 交易方向拒收：{len(_trade_excluded)} 条缺可判定要素 → 降级「观察」：{_ex_desc}")

    if ai_summary.get("status") == "ok" and ai_headline:
        # 方向颜色
        bias_color = "#ef4444" if "多" in str(ai_bias) else "#22c55e" if "空" in str(ai_bias) else "#f59e0b"
        conviction_cn = {"high": "高", "medium": "中", "low": "低"}.get(str(ai_conviction).lower(), "中")
        # 证据覆盖（替代原硬编码 {"high":85,...} 裸百分比）：不再展示与数据无关的置信度数字，
        # 改展示 data_quality 中 status=ok 的维度数 / 总维度数，与证据量单调相关、可复算。
        _dq = ai_summary.get("data_quality") or []
        _dq_ok = sum(1 for d in _dq if str((d or {}).get("status")) == "ok")
        _dq_total = len(_dq)
        _cover_txt = f"{_dq_ok}/{_dq_total}" if _dq_total else "—"
        # P1-4：读者看到 N/M 即需知其缺哪两项，无需翻到数据状态表
        _dq_missing = _coverage_missing_names(_dq)
        _cover_missing_txt = ("缺 " + "、".join(_dq_missing)) if _dq_missing else ""

        html_parts.append(f"""
          <!-- 模块0：AI今日定调 -->
          <div style="background:linear-gradient(135deg,#1e3a5f,#0f172a);border-radius:12px;padding:16px 18px;margin-bottom:10px;color:#fff;position:relative;overflow:hidden">
            <div style="position:absolute;top:-20px;right:-10px;font-size:80px;opacity:0.08">🤖</div>
            <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px">
              <div>
                <div style="font-size:11px;color:#94a3b8;letter-spacing:1px;text-transform:uppercase;margin-bottom:2px">AI Morning Call</div>
                <div style="font-size:18px;font-weight:800;letter-spacing:-0.5px;line-height:1.3">{ai_headline}</div>
              </div>
              <div style="text-align:right;flex-shrink:0;margin-left:12px">
                <div style="font-size:10px;color:#64748b">证据覆盖</div>
                <div style="font-size:18px;font-weight:700;color:{bias_color}">{_cover_txt}</div>
                <div style="font-size:9px;color:#64748b">项 · 信心{conviction_cn}</div>
                {f'<div style="font-size:9px;color:#fca5a5;margin-top:1px">{_cover_missing_txt}</div>' if _cover_missing_txt else ''}
              </div>
            </div>
            <div style="display:flex;gap:8px;margin-bottom:10px">
              <span style="font-size:10.5px;background:rgba(255,255,255,0.1);padding:2px 8px;border-radius:4px;color:#e2e8f0">市场：{ai_regime or '—'}</span>
              <span style="font-size:10.5px;background:{bias_color}22;padding:2px 8px;border-radius:4px;color:{bias_color}">方向：{ai_bias or '—'}</span>
            </div>
            {_bias_caveat_html}
        """)

        # 核心驱动因素
        if ai_key_drivers:
            driver_html = "<br>".join(f"• {d}" for d in ai_key_drivers[:4])
            html_parts.append(f"""
            <div style="font-size:12px;color:#cbd5e1;line-height:1.7;margin-bottom:10px;background:rgba(255,255,255,0.05);border-radius:6px;padding:8px 12px">
              {driver_html}
            </div>
            """)

        # 赛道轮动
        if ai_sector_rotation:
            html_parts.append(f"""
            <div style="font-size:11.5px;color:#e2e8f0;margin-bottom:10px">
              <span style="color:#f59e0b;font-weight:700">🔄 赛道轮动：</span>{ai_sector_rotation}
            </div>
            """)

        # 具体交易方向（「今日无操作」是一等公民，与「有方向」同等醒目）
        # P1-a：只有六要素齐备的条目才进此区；缺任一者进下方「👀 观察」区（降级而非丢弃）。
        if _trade_ready:
            html_parts.append(f"""
            <div style="font-size:11px;color:#94a3b8;margin-bottom:6px;font-weight:600;letter-spacing:0.5px">💡 具体交易方向</div>
            """)
            for s in _trade_ready[:4]:
                asset = s.get("asset") or "?"
                direction = s.get("direction") or ""
                horizon = s.get("horizon") or ""
                trigger = s.get("trigger") or ""
                invalidate = s.get("invalidate") or ""
                target = s.get("target") or ""
                ref_price = s.get("ref_price") or ""
                ref_as_of = s.get("ref_as_of") or ""
                reason = s.get("reason") or ""

                dir_color = "#ef4444" if "多" in str(direction) else "#22c55e" if "空" in str(direction) else "#eab308"
                dir_icon = "▲" if "多" in str(direction) else "▼" if "空" in str(direction) else "◆"

                html_parts.append(f"""
                <div style="background:rgba(255,255,255,0.08);border-radius:6px;padding:8px 10px;margin-bottom:5px;border-left:3px solid {dir_color}">
                  <div style="display:flex;justify-content:space-between;align-items:center">
                    <div style="font-size:13px;font-weight:700">{asset} <span style="color:{dir_color};font-size:12px;margin-left:4px">{dir_icon} {direction}</span></div>
                    <span style="font-size:10px;background:rgba(255,255,255,0.1);padding:1px 6px;border-radius:3px;color:#94a3b8">{horizon}</span>
                  </div>
                  <div style="font-size:11px;color:#e2e8f0;margin-top:4px;line-height:1.6">
                    <span style="color:#22c55e">进场</span> {trigger}<br>
                    <span style="color:#ef4444">失效</span> {invalidate}<br>
                    <span style="color:#f59e0b">目标</span> {target}<br>
                    <span style="color:#94a3b8">参照</span> {ref_price} <span style="color:#64748b">（{ref_as_of}）</span>
                  </div>
                  {f'<div style="font-size:11px;color:#94a3b8;margin-top:3px;line-height:1.5">理由：{reason}</div>' if reason else ''}
                </div>
                """)
        else:
            html_parts.append(f"""
            <div style="background:rgba(148,163,184,0.12);border-radius:6px;padding:8px 10px;margin-bottom:5px;border-left:3px solid #94a3b8">
              <div style="font-size:12.5px;font-weight:700;color:#e2e8f0">⚪ 今日无操作</div>
              {f'<div style="font-size:11px;color:#94a3b8;margin-top:2px;line-height:1.5">{_no_trade_reason}</div>' if _no_trade_reason else ''}
            </div>
            """)

        # 👀 观察区：AI 提了方向但缺可判定要素 → 不构成建议，但信息不丢弃（降级而非删除）
        if _trade_excluded:
            _obs = "".join(
                f'<div style="font-size:11px;color:#cbd5e1;line-height:1.6">'
                f'{(_s or {}).get("asset") or "?"} '
                f'<span style="color:#94a3b8">{(_s or {}).get("direction") or ""}</span> '
                f'<span style="color:#f59e0b">缺：{"、".join(_trade_missing_fields(_s))}</span></div>'
                for _s in _trade_excluded[:5]
            )
            html_parts.append(f"""
            <div style="background:rgba(148,163,184,0.10);border-radius:6px;padding:8px 10px;margin-bottom:5px;border-left:3px solid #64748b">
              <div style="font-size:11.5px;font-weight:700;color:#cbd5e1">👀 观察（不构成建议·缺可判定条件）</div>
              <div style="margin-top:3px">{_obs}</div>
            </div>
            """)

        # 关注列表
        if ai_watchlist:
            watch_str = " · ".join(ai_watchlist[:6])
            html_parts.append(f"""
            <div style="font-size:11px;color:#94a3b8;margin-top:10px">
              <span style="color:#f59e0b;font-weight:600">🎯 重点关注：</span>{watch_str}
            </div>
            """)

        # 风险提示（W-15：可判定化——含数值阈值者进「风险」区，其余移入「注意事项」）
        if ai_risk_warnings:
            _risk_ok, _risk_note = [], []
            for _r in ai_risk_warnings[:5]:
                (_risk_ok if _risk_has_threshold(_r) else _risk_note).append(_r)
            if _risk_note:
                print(f"[render_brief_html] 风险条目移出「风险」区（无数值阈值）："
                      f"{'；'.join(str(r)[:40] for r in _risk_note)}")
            if _risk_ok:
                risk_html = "<br>".join(f"⚠️ {_risk_mark_posterior(r)}" for r in _risk_ok[:3])
                html_parts.append(f"""
            <div style="font-size:11px;color:#fca5a5;margin-top:8px;line-height:1.6">
              {risk_html}
            </div>
            """)
            if _risk_note:
                note_html = "<br>".join(f"· {r}" for r in _risk_note[:3])
                html_parts.append(f"""
            <div style="font-size:11px;color:#94a3b8;margin-top:6px;line-height:1.6">
              <span style="color:#cbd5e1;font-weight:600">注意事项（无可判定阈值·不构成风险判定）：</span><br>{note_html}
            </div>
            """)

        # P0-1（审计 2026-09-28）：顶部/贪婪环境下的交易约束缝合层（顶部风险叙事 vs 全做多）。
        # 注：phase / fear_greed 在下方「大盘脉搏」才定义，此处直接用 m0 取值。
        html_parts.append(_top_risk_guard_html(
            m0.get("btc_cycle_phase"), m0.get("fear_greed"), has_trades=bool(_trade_ready)))

        html_parts.append("</div>")
    else:
        # AI 不可用时的降级：用 M0 TLDR
        tldr_text = m0.get("summary") or m0.get("tldr") or ""
        html_parts.append(f"""
          <div style="background:linear-gradient(135deg,#1e3a5f,#0f172a);border-radius:12px;padding:14px 16px;margin-bottom:10px;color:#fff">
            <div style="font-size:11px;color:#94a3b8;letter-spacing:1px;text-transform:uppercase;margin-bottom:4px">Morning Brief</div>
            <div style="font-size:18px;font-weight:800;margin-bottom:8px">加密大盘早报</div>
            <div style="font-size:13px;color:#e2e8f0;line-height:1.5">{tldr_text or '今日大盘数据更新中...'}</div>
          </div>
        """)

    # ════════════════════════════════════════════════════════
    # 模块 0.01：🎯 今日操作清单（P1-1，审计 2026-09-28）——邮件前 1/3 可见
    # M4-1 折叠归属表需先算，摘要据此排除已在其他板块给出结论的标的（避免重复结论）。
    # ════════════════════════════════════════════════════════
    _tgt_owners, _arbitrations = _build_target_registry(brief, _trade_ready)
    html_parts.append(_build_tldr_html(_trade_ready, all_opps, _tgt_owners))

    # ════════════════════════════════════════════════════════
    # 模块 0.05：📋 与昨日变化（W-12 变更日志；M0 之后的第二块）
    # ════════════════════════════════════════════════════════
    html_parts.append(_m0_delta_html(brief))

    # ════════════════════════════════════════════════════════
    # 模块 0.1：📋 数据状态（W-07 置顶；M0 定调之后、第一张数据卡之前）
    # ════════════════════════════════════════════════════════
    html_parts.append(_data_status_table_html(ai_summary.get("data_quality") or []))

    # ════════════════════════════════════════════════════════
    # 模块 0.2：📉 告警质量（昨日盘面告警胜率/赔率 → 阈值-行情失配预警）
    # 数据来自 macro_market._load_alert_quality（只读 biz.scan_edge_daily 最新一行）。
    # 缺数据时整块跳过，不影响其他模块。
    # ════════════════════════════════════════════════════════
    aq = brief.get("M0_alert_quality") or {}
    if aq.get("win_1h") is not None:
        aq_color, aq_label = "#16a34a", "正常"
        if aq.get("severity") == "watch":
            aq_color, aq_label = "#d97706", "观察"
        elif aq.get("severity") == "high":
            aq_color, aq_label = "#dc2626", "失配"
        # P2-3（审计 2026-09-28）：术语段（S1/平衡线/PF/边缘桶）对普通读者可读性低 →
        # 置顶一句白话结论，先给结论再看指标。
        _aq_plain = {
            "normal": "白话结论：近期盘面告警整体有效，阈值与行情匹配。",
            "watch": "白话结论：近期盘面告警胜率偏低（观察级），阈值或需微调，暂不宜重仓跟随。",
            "high": "白话结论：近期盘面告警胜率明显偏低、与行情失配，建议收紧阈值或暂停跟随。",
        }.get(str(aq.get("severity") or "normal").lower(),
              "白话结论：近期盘面告警状态未知，请谨慎参考。")
        aq_n = str(aq["alerts_n"]) if aq.get("alerts_n") is not None else "-"
        aq_win = f"{aq['win_1h'] * 100:.1f}%"
        aq_be = f"{aq['be_1h'] * 100:.1f}%" if aq.get("be_1h") is not None else "-"
        aq_odds = f"{aq['odds_1h']:.2f}" if aq.get("odds_1h") is not None else "-"
        aq_pf = f"{aq['pf_1h']:.2f}" if aq.get("pf_1h") is not None else "-"
        # 审计 2026-09-24 P2-B：当日 win_1h 与「近3日滚动」roll3_win_1h 是两个不同窗口的
        # 同一指标，并排显示时读者会读成「胜率到底高还是低」（实测 59.1% vs 44.5%）。
        # 现将两个窗口显式分列标注，并说明失配判定以滚动口径为准。
        aq_rwin = f"{aq['roll3_win_1h'] * 100:.1f}%" if aq.get("roll3_win_1h") is not None else "-"
        aq_rbe = f"{aq['roll3_be_1h'] * 100:.1f}%" if aq.get("roll3_be_1h") is not None else "-"
        aq_rpf = f"{aq['roll3_pf_1h']:.2f}" if aq.get("roll3_pf_1h") is not None else "-"
        # conclusion 由本系统生成，可能含 `<`（如 PF<1）；早报未引入 html.escape，这里做最小实体转义
        aq_note = (aq.get("conclusion") or "").strip().replace("<", "&lt;").replace(">", "&gt;")
        if len(aq_note) > 160:
            aq_note = aq_note[:160] + "…"
        html_parts.append(f"""
          <div style="background:#fff;border-radius:10px;padding:10px 14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,0.05);border-left:4px solid {aq_color}">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
              <div style="font-size:13px;font-weight:700;color:#0f172a">📉 告警质量（阈值-行情失配预警）</div>
              <div style="font-size:11px;font-weight:700;color:{aq_color}">{aq_label}</div>
            </div>
            <div style="font-size:11.5px;color:{aq_color};font-weight:600;margin-bottom:3px">{_aq_plain}</div>
            <div style="font-size:12px;color:#475569;line-height:1.6">
              {aq.get('report_date') or '-'} 告警 {aq_n} 条 · 当日 T+1h 胜率 {aq_win}（平衡线 {aq_be}）·
              近3日滚动 {aq_rwin}（平衡线 {aq_rbe}，PF {aq_rpf}）·
              赔率 {aq_odds} · PF {aq_pf} · 环境 {aq.get('regime_label') or '-'}
            </div>
            <div style="font-size:10.5px;color:#94a3b8;line-height:1.5;margin-top:4px">
              说明：「当日」与「近3日滚动」是两个不同窗口的同一指标，失配判定以滚动口径为准；单日胜率波动大，不宜据此判断阈值优劣。
            </div>
            {f'<div style="font-size:11px;color:#94a3b8;line-height:1.5;margin-top:4px">{aq_note}</div>' if aq_note else ''}
          </div>
        """)

    # ════════════════════════════════════════════════════════
    # 模块 0.5：🎯 AI 精选高亮信号（V2 六维评分 + Web 搜索补全）
    # ════════════════════════════════════════════════════════
    highlights = brief.get("M3_highlights") or []
    risk_signals = brief.get("M4_risks") or []
    # 只展示经过 AI V2 分析的高亮信号
    ai_highlights = [h for h in highlights if h.get("ai_analysis_v2") and not h["ai_analysis_v2"].get("error")]

    if ai_highlights:
        display_highlights = ai_highlights[:3]  # 最多 3 个
        # P1-7（审计 2026-09-29）：卡内可能含「观察级（非高亮）」条目，标题须注明，
        # 避免「非高亮却挂在高亮区」的观感。
        _has_obs = any(
            (hh.get("_ai_downgraded")
             or "不构成高亮" in str(((hh.get("ai_analysis_v2") or {}).get("reason_summary")) or "")
             or "不构成高亮" in str(hh.get("trigger_logic") or ""))
            for hh in display_highlights
        )
        _hl_title = "🎯 AI 精选高亮信号（含观察级）" if _has_obs else "🎯 AI 精选高亮信号"

        html_parts.append(f"""
          <!-- 模块：AI精选高亮信号 -->
          <div style="background:#fff;border-radius:10px;padding:12px 14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,0.05)">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
              <div style="font-size:13px;font-weight:700;color:#0f172a">{_hl_title}</div>
              <div style="font-size:10px;color:#94a3b8">六维评分 · 自动搜索补全</div>
            </div>
        """)

        for h in display_highlights:
            target = h.get("target") or "?"
            base_score = h.get("conviction_score") or 0
            ai = h.get("ai_analysis_v2") or {}
            overall_score = ai.get("overall_score") or base_score
            confidence = ai.get("confidence") or "MED"
            reason = ai.get("reason_summary") or h.get("trigger_logic") or ""
            # U-A 可见性：AI reason_summary 会覆盖 trigger_logic 的连板注脚，且该聚合机会
            # 已被 M4-1 折叠出「精选机会」⇒ 连板信息在邮件里不可见。此处补显（去重、不改口径）。
            _sh = _streak_hint(h.get("trigger_logic"))
            if _sh and _sh not in reason:
                reason = f"{reason} {_sh}" if reason else _sh
            key_drivers = ai.get("key_drivers") or []
            score_card = ai.get("score_card") or {}

            # 方向：以基础信号方向为准（本板块=看多机会）。
            # AI 判定与基础方向相反时不反向展示，而是标注"AI 存疑"，
            # 避免同一标的在高亮/机会两个板块出现相反方向的矛盾。
            direction = h.get("direction") or "long"
            ai_direction = ai.get("direction") or ""
            ai_doubt = bool(ai_direction and ai_direction not in ("long", "neutral"))
            dir_icon = "▲" if direction == "long" else "◆" if direction in ("watch", "neutral") else "▼"
            dir_color = "#dc2626" if direction == "long" else "#94a3b8" if direction in ("watch", "neutral") else "#16a34a"
            dir_cn = "看多" if direction == "long" else "观望" if direction in ("watch", "neutral") else "看空"

            # 置信度颜色
            conf_color = {"HIGH": "#dc2626", "MED": "#f59e0b", "LOW": "#94a3b8"}.get(confidence, "#f59e0b")

            # 六维评分小条（取 3 个关键维度）
            mini_dims = []
            for dim_key, dim_name in [("technical", "技术"), ("fundamental", "基本面"), ("sentiment", "情绪")]:
                dim = score_card.get(dim_key) or {}
                s = dim.get("score")
                if isinstance(s, (int, float)):
                    bar_color = "#dc2626" if s >= 70 else "#f59e0b" if s >= 50 else "#94a3b8"
                    bar_width = max(8, min(60, s * 0.6))
                    mini_dims.append(
                        f'<div style="font-size:9px;color:#64748b;margin-bottom:2px">{dim_name} {s}分</div>'
                        f'<div style="height:3px;background:#f1f5f9;border-radius:2px;margin-bottom:4px">'
                        f'<div style="width:{bar_width}px;height:100%;background:{bar_color};border-radius:2px"></div>'
                        f'</div>'
                    )
            mini_dims_html = '<div style="flex:1;min-width:0">' + "".join(mini_dims) + '</div>' if mini_dims else ""

            # 驱动因子（取前 2 个）
            drivers_html = ""
            if key_drivers:
                driver_items = " · ".join(_clip(d, 30) for d in key_drivers[:2])
                drivers_html = f'<div style="font-size:10px;color:#0369a1;margin-top:4px">💡 {driver_items}</div>'

            # AI 存疑标注（AI 判反或降级时）
            doubt_html = ""
            if ai_doubt or h.get("_ai_downgraded"):
                doubt_html = '<span style="font-size:9px;background:#fef9c3;color:#92400e;padding:1px 5px;border-radius:3px;font-weight:600;margin-left:4px">AI存疑</span>'
            # P1-7（审计 2026-09-28）：正文自称「不构成高亮或高危」却列在「AI 精选高亮」下 →
            # 归类与措辞自相矛盾，显式标注「观察级（非高亮）」。
            obs_html = ""
            if h.get("_ai_downgraded") or "不构成高亮" in str(reason) or "不构成高亮" in str(ai.get("reason_summary") or ""):
                obs_html = ('<span style="font-size:9px;background:#e2e8f0;color:#475569;'
                            'padding:1px 5px;border-radius:3px;font-weight:600;margin-left:4px">'
                            '观察级（非高亮）</span>')
            # M2-A1（2026-09-28）：档位被降档时给出原因，避免读者看到「78 分却是 MED」无从理解。
            _hl_demote = str(h.get("display_note") or h.get("tier_demote_reason") or "").strip()
            demote_html = (f'<div style="font-size:9.5px;color:#b45309;margin-top:4px">'
                           f'⬇️ 降档说明：{html.escape(_hl_demote)}</div>' if _hl_demote else "")

            html_parts.append(f"""
            <div style="padding:10px 12px;margin:6px 0;border-radius:8px;background:linear-gradient(135deg,#fef2f2,#fff1f2);border:1px solid #fecaca;border-left:3px solid #dc2626">
              <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:4px">
                <div style="display:flex;align-items:center;gap:8px">
                  <span style="font-size:15px;font-weight:800;color:#0f172a">{target}</span>
                  <span style="font-size:11px;color:{dir_color};font-weight:700">{dir_icon} {dir_cn}</span>
                  <span style="font-size:9px;background:{conf_color}22;color:{conf_color};padding:1px 5px;border-radius:3px;font-weight:600">{confidence}</span>
                  {doubt_html}
                  {obs_html}
                </div>
                <div style="text-align:right;flex-shrink:0">
                  <div style="font-size:18px;font-weight:800;color:#dc2626;line-height:1">{overall_score}</div>
                  <div style="font-size:9px;color:#94a3b8">综合评分</div>
                </div>
              </div>
              <div style="display:flex;gap:10px;align-items:center">
                <div style="flex:1;min-width:0">
                  <div style="font-size:11px;color:#475569;line-height:1.5">{_clip(reason, 120)}</div>
                  {drivers_html}
                </div>
                {mini_dims_html}
              </div>
              {demote_html}
            </div>
            """)

        # 高危信号（如果有，加一个小警示条）
        ai_risks = [r for r in risk_signals if r.get("ai_analysis_v2") and not r["ai_analysis_v2"].get("error")]
        if ai_risks:
            # P1-3（审计 2026-09-28）：原只列名字（含稳定币 USDC/USDS/CBBTC，读者无法理解），
            # 现逐条给「风险点 + 应对」一行（数据驱动，稳定币类必解释为何高危）。
            risk_count = len(ai_risks)
            _risk_rows = "".join(
                f'<div style="font-size:10.5px;color:#991b1b;line-height:1.55;margin-top:2px">'
                f'• {_risk_one_liner(r)}</div>'
                for r in ai_risks
            )
            html_parts.append(f"""
            <div style="margin-top:6px;padding:8px 12px;background:#fef2f2;border-radius:6px;font-size:11px;color:#991b1b">
              <div>⚠️ 今日高危信号（综合风险）<b>{risk_count}</b> 个</div>
              {_risk_rows}
            </div>
            """)

        html_parts.append("</div>")

    # ════════════════════════════════════════════════════════
    # 模块 0.6：⚖️ 单一口径裁决（方案 §3.3 M4-4）
    # 既领涨/入选机会、又入高危的标的：输出裁决语，避免两个方向结论并列无解释。
    # 折叠归属表（owners）供下方「精选机会」卡复用（M4-1）。
    # ════════════════════════════════════════════════════════
    if _arbitrations:
        _arb_lines = "<br>".join(f"⚖️ {a['text']}" for a in _arbitrations[:4])
        html_parts.append(f"""
          <!-- 模块0.6：单一口径裁决 -->
          <div style="background:#fff;border-radius:10px;padding:10px 14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,0.05);border-left:4px solid #7c3aed">
            <div style="font-size:12.5px;font-weight:700;color:#0f172a;margin-bottom:4px">⚖️ 单一口径裁决（同一标的只取一条结论）</div>
            <div style="font-size:11.5px;color:#475569;line-height:1.7">{_arb_lines}</div>
          </div>
        """)

    # ════════════════════════════════════════════════════════
    # 模块 1：📊 大盘脉搏
    # ════════════════════════════════════════════════════════
    btc_price = m0.get("btc_price")
    btc_change = m0.get("btc_change_24h_pct")
    btc_chg_str, btc_chg_color = _fmt_pct(btc_change)
    fear_greed = m0.get("fear_greed")
    fg_label = m0.get("fear_greed_label", "")
    # W-09：恐贪值旁强制显示 as-of 日期（SSOT = biz.market_snapshot_daily.fear_greed_value）
    fg_as_of = m0.get("fear_greed_as_of")
    fg_note = m0.get("fear_greed_note")
    _fg_label_html = f"{fg_label or '—'}" + (f" · 截至 {fg_as_of}" if fg_as_of else "")
    _fg_note_html = (
        f'<div style="font-size:9px;color:#b45309;margin-top:1px">{fg_note}</div>'
        if fg_note else ""
    )
    phase = m0.get("btc_cycle_phase", "—")
    eth_price = m0.get("eth_price")
    eth_change = m0.get("eth_change_24h_pct")
    eth_chg_str, eth_chg_color = _fmt_pct(eth_change)

    m2 = brief.get("M2_flow") or {}
    total_mcap = m2.get("total_market_cap") or m0.get("total_market_cap")
    # diff_overview 返回键为 total_mcap_pct（非 total_market_cap_pct）
    total_mcap_chg = diff.get("total_mcap_pct") if diff else None
    mcap_chg_str, mcap_chg_color = _fmt_pct(total_mcap_chg)

    total_vol = sector_flow.get("total_volume_24h")
    vol_str = _fmt_mcap(total_vol) if total_vol else "N/A"

    # 波动率
    btc_vol = m0.get("btc_volatility_7d") or m2.get("btc_volatility_7d")
    btc_vol_str = f"{btc_vol}%" if btc_vol is not None else "—"

    # P0-D：24h 爆仓概况（只读快照渲染，禁止在渲染期调 CoinGlass）
    # 旧快照缺 M2_liquidation / 覆盖率不足 / 列 NULL ⇒ 整行隐藏（缺失≠0，不显示 0）
    liq_row = _render_liquidation_row(brief.get("M2_liquidation"))

    # P2-C：大盘脉搏数据时点（此前只有日期，与爆仓/ETF/赛道/解锁的标注口径不一致）
    _pulse_as_of = _fmt_data_as_of(m0.get("data_as_of"))
    _pulse_as_of_html = f" · 数据截至 {_pulse_as_of}（北京时间）" if _pulse_as_of else ""

    # W-11：昨日基准缺失时显式提示（DIFF=no_baseline），不在「无变化」上留白
    _diff_note_html = ""
    if isinstance(diff, dict) and diff.get("status") == "no_baseline":
        _bd = diff.get("baseline_date") or "昨日"
        _diff_note_html = (
            f'<div style="font-size:10px;color:#b45309;margin-top:6px">'
            f'⚠️ 无昨日基准（{_bd} 快照缺失），本期无变化对比</div>'
        )

    html_parts.append(f"""
      <!-- 模块1：大盘脉搏 -->
      <div style="background:#fff;border-radius:10px;padding:12px 14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,0.05)">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
          <div style="font-size:13px;font-weight:700;color:#0f172a">📊 大盘脉搏</div>
          <div style="font-size:10px;color:#94a3b8">{today}{_pulse_as_of_html}</div>
        </div>

        <!-- 两排指标 -->
        <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-bottom:8px">
          <!-- BTC -->
          <div style="background:linear-gradient(135deg,#f8fafc,#f1f5f9);border-radius:8px;padding:10px 6px;text-align:center;border:1px solid #e2e8f0">
            <div style="font-size:10px;color:#64748b;margin-bottom:2px">BTC</div>
            <div style="font-size:16px;font-weight:700;color:#0f172a;letter-spacing:-0.3px">{_fmt_mcap(btc_price) if btc_price else 'N/A'}</div>
            <div style="font-size:10px;color:{btc_chg_color};margin-top:1px;font-weight:600">{btc_chg_str}</div>
          </div>
          <!-- ETH -->
          <div style="background:#f8fafc;border-radius:8px;padding:10px 6px;text-align:center;border:1px solid #e2e8f0">
            <div style="font-size:10px;color:#64748b;margin-bottom:2px">ETH</div>
            <div style="font-size:16px;font-weight:700;color:#0f172a">{_fmt_mcap(eth_price) if eth_price else 'N/A'}</div>
            <div style="font-size:10px;color:{eth_chg_color};margin-top:1px;font-weight:600">{eth_chg_str}</div>
          </div>
          <!-- 总市值 -->
          <div style="background:#f8fafc;border-radius:8px;padding:10px 6px;text-align:center;border:1px solid #e2e8f0">
            <div style="font-size:10px;color:#64748b;margin-bottom:2px">总市值</div>
            <div style="font-size:15px;font-weight:700;color:#0f172a">{_fmt_mcap(total_mcap)}</div>
            <div style="font-size:10px;color:{mcap_chg_color};margin-top:1px;font-weight:600">{mcap_chg_str}</div>
          </div>
          <!-- 恐贪 -->
          <div style="background:#f8fafc;border-radius:8px;padding:10px 6px;text-align:center;border:1px solid #e2e8f0">
            <div style="font-size:10px;color:#64748b;margin-bottom:2px">恐贪指数</div>
            <div style="font-size:17px;font-weight:700;color:{_fear_greed_color(fear_greed)}">{fear_greed if fear_greed is not None else 'N/A'}</div>
            <div style="font-size:10px;color:#64748b;margin-top:1px">{_fg_label_html}</div>
            {_fg_note_html}
          </div>
        </div>

        <!-- 底部附加指标 -->
        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:6px">
          <div style="text-align:center;background:#f8fafc;border-radius:6px;padding:6px 4px">
            <div style="font-size:9.5px;color:#94a3b8">24h 成交量</div>
            <div style="font-size:13px;font-weight:700;color:#334155">{vol_str}</div>
          </div>
          <div style="text-align:center;background:#f8fafc;border-radius:6px;padding:6px 4px">
            <div style="font-size:9.5px;color:#94a3b8">BTC 周期</div>
            <div style="font-size:13px;font-weight:700;color:#334155">{phase}</div>
          </div>
          <div style="text-align:center;background:#f8fafc;border-radius:6px;padding:6px 4px">
            <div style="font-size:9.5px;color:#94a3b8">BTC 7日波动率</div>
            <div style="font-size:13px;font-weight:700;color:#334155">{btc_vol_str}</div>
          </div>
        </div>

        <!-- P0-D：24h 爆仓概况（无数据时 liq_row 为空串，整行不出现） -->
        {liq_row}
        {_diff_note_html}
      </div>
    """)

    # ════════════════════════════════════════════════════════
    # 模块 1.5：📈 每日变化榜（U-B：消费 M5_daily_diff，缺失时整块不出现）
    # ════════════════════════════════════════════════════════
    html_parts.append(_render_daily_diff_html(brief))

    # ════════════════════════════════════════════════════════
    # 模块 2：🏭 赛道轮动（功能分类 12 赛道 + 领涨币）
    # 数据源：M2_sector_flow（与 AI 定调一致，避免摘要/榜单数据矛盾）
    # ════════════════════════════════════════════════════════
    narratives = (brief.get("M2_sector_flow") or {}).get("sectors") or []
    sector_date = (brief.get("M2_sector_flow") or {}).get("metric_date") or ""
    # P1-6（审计 2026-09-28）：赛道 / ETF / 巨鲸数据源滞后于全局标题「截至今日」，
    # 逐卡标注真实截至日与滞后天数，避免「新鲜感是假的」。
    try:
        _sector_lag = (date.today() - date.fromisoformat(str(sector_date)[:10])).days
    except (ValueError, TypeError):
        _sector_lag = None
    _sector_lag_txt = f" · 截至 {str(sector_date)[5:10]}，滞后 {_sector_lag} 天" if (
        _sector_lag is not None and _sector_lag > 0) else ""

    html_parts.append(f"""
      <!-- 模块2：赛道轮动 -->
      <div style="background:#fff;border-radius:10px;padding:12px 14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,0.05)">
        <div style="font-size:13px;font-weight:700;color:#0f172a;margin-bottom:8px;display:flex;align-items:center">
          <span style="margin-right:6px">🏭</span>赛道轮动
          <span style="margin-left:auto;font-size:10px;color:#94a3b8;font-weight:400">7日市值变化 · 功能分类{sector_date}{_sector_lag_txt}</span>
        </div>
    """)

    if narratives:
        max_score = max((float(n.get("composite_score") or 0)) for n in narratives) or 1
        for idx, n in enumerate(narratives):
            name = n.get("sector_label") or n.get("sector_key", "?")
            score = float(n.get("composite_score") or 0)
            mcap7d = n.get("mcap_change_7d_pct")
            leaders = n.get("leaders") or []

            chg_str, chg_color = _fmt_pct(mcap7d)
            bar_pct = max(3, min(100, (score / max_score) * 100))

            # 趋势标签（按 7d 涨幅简化为加速/走强/横盘/回调）
            trend_badge = ""
            if mcap7d is not None:
                f7 = float(mcap7d)
                if f7 >= 3:
                    trend_badge = '<span style="background:#dcfce7;color:#166534;font-size:9.5px;padding:1px 5px;border-radius:3px;font-weight:600;margin-left:5px">加速↑</span>'
                elif f7 > 0:
                    trend_badge = '<span style="background:#dbeafe;color:#1e40af;font-size:9.5px;padding:1px 5px;border-radius:3px;font-weight:600;margin-left:5px">走强</span>'
                elif f7 < -3:
                    trend_badge = '<span style="background:#fee2e2;color:#991b1b;font-size:9.5px;padding:1px 5px;border-radius:3px;font-weight:600;margin-left:5px">回调↓</span>'
                else:
                    trend_badge = '<span style="background:#f1f5f9;color:#64748b;font-size:9.5px;padding:1px 5px;border-radius:3px;font-weight:600;margin-left:5px">横盘</span>'

            # 领涨币（赛道内 7d 涨幅前列；赛道整体下跌时标注"相对强势"避免歧义）
            coins_html = ""
            if leaders:
                coin_parts = []
                for c in leaders[:3]:
                    if isinstance(c, str):
                        coin_parts.append(f'<span style="font-size:10px;color:#64748b">{c}</span>')
                        continue
                    sym = c.get("symbol", "?")
                    # P1-5（审计 2026-09-28）：领涨币原只给名字，读者要「买哪个」无从判断；
                    # 补个币 7d 涨幅（数据已在 fetch_sector_flow_with_leaders 的 SELECT 中）。
                    _c7 = c.get("percent_change_7d")
                    if _c7 is None:
                        coin_parts.append(f'<span style="font-size:10px;color:#64748b">{sym}</span>')
                    else:
                        _c7f = float(_c7)
                        _c7_color = "#dc2626" if _c7f >= 0 else "#16a34a"
                        coin_parts.append(
                            f'<span style="font-size:10px;color:#64748b">{sym} '
                            f'<span style="color:{_c7_color};font-weight:600">'
                            f'{_c7f:+.1f}%</span></span>'
                        )
                leader_label = "相对强势" if (mcap7d is not None and float(mcap7d) < 0) else "领涨币"
                coins_html = f'<div style="font-size:10px;color:#94a3b8;margin-top:3px">{leader_label}（7日）：{" · ".join(coin_parts)}</div>'

            html_parts.append(f"""
              <div style="padding:7px 8px;margin-bottom:4px;border-radius:6px;background:#fafafa">
                <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:3px">
                  <div style="display:flex;align-items:center;min-width:0">
                    <span style="display:inline-block;width:20px;height:20px;line-height:20px;text-align:center;background:#e2e8f0;color:#475569;font-size:10.5px;font-weight:700;border-radius:4px;margin-right:6px;flex-shrink:0">{idx+1}</span>
                    <span style="font-size:12.5px;font-weight:600;color:#0f172a;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{name}</span>
                    {trend_badge}
                  </div>
                  <div style="display:flex;align-items:center;gap:6px;margin-left:6px;flex-shrink:0">
                    <span style="font-size:12px;color:{chg_color};font-weight:700">{chg_str}</span>
                  </div>
                </div>
                <div style="height:4px;background:#e2e8f0;border-radius:2px;overflow:hidden;margin-left:26px">
                  <div style="height:100%;width:{bar_pct}%;background:linear-gradient(90deg,#3b82f6,#8b5cf6);border-radius:2px"></div>
                </div>
                {coins_html}
              </div>
            """)
    else:
        html_parts.append('<div style="color:#94a3b8;font-size:11px;padding:14px;text-align:center">暂无赛道数据</div>')

    html_parts.append("</div>")

    # ════════════════════════════════════════════════════════
    # 模块 3：💰 机构资金流（ETF + 稳定币 + 交易所净流）
    # ════════════════════════════════════════════════════════
    html_parts.append(f"""
      <!-- 模块3：机构资金流 -->
      <div style="background:#fff;border-radius:10px;padding:12px 14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,0.05)">
        <div style="font-size:13px;font-weight:700;color:#0f172a;margin-bottom:8px">💰 机构资金流</div>
    """)

    # ETF 资金流
    etf_assets = etf_flow.get("assets") or []
    # W-10：ETF 是「交易日更」数据，最新 flow_date 可能是前一交易日（周末/节假日季节性停更）。
    # 数值必须显式标注为「上一交易日(MM-DD)」，不得用无日期限定的「单日」措辞；
    # 若最新 flow_date 距今日 > 3 天 → 该卡降级为「数据不可用」并进 M9_degraded。
    _etf_as_of = etf_flow.get("latest_date")
    try:
        _etf_lag = (date.today() - date.fromisoformat(str(_etf_as_of)[:10])).days
    except (ValueError, TypeError):
        _etf_lag = None
    _etf_md = str(_etf_as_of)[5:10] if _etf_as_of else "—"
    if etf_flow.get("status") == "ok" and etf_assets and _etf_lag is not None and _etf_lag > 3:
        brief.setdefault("M9_degraded", []).append(
            f"ETF资金流(最新 {_etf_as_of}，滞后 {_etf_lag} 天 > 3 天，数值已隐藏)")
        html_parts.append(f"""
          <!-- ETF 子模块（W-10 降级：flow_date 滞后 > 3 天） -->
          <div style="background:linear-gradient(135deg,#f0f9ff,#e0f2fe);border-radius:8px;padding:10px 12px;margin-bottom:8px">
            <div style="font-size:11.5px;font-weight:700;color:#0369a1;margin-bottom:4px">📈 ETF 资金流</div>
            <div style="font-size:11px;color:#dc2626">数据不可用（最新交易日 {_etf_as_of or '—'}，滞后 {_etf_lag} 天 &gt; 3 天，数值已隐藏）</div>
          </div>
        """)
        etf_assets = []
    if etf_flow.get("status") == "ok" and etf_assets:
        # 从 assets 里提取 BTC、ETH 和总净流入。
        # 审计 2026-09-24 P1-1：原先「合计净流入」把全部资产（含 SOL/XRP/…）加总，
        # 而卡片只展示 BTC/ETH ⇒ 读者按可见两项相加（BTC+ETH）与合计对不上（486 vs 609）。
        # 现把合计明确标注为「全部 ETF 合计」，并附分项拆解使算术自洽。
        btc_net = None
        eth_net = None
        total_net = 0.0
        others = []  # [(symbol, flow_7d_usd), ...]
        for a in etf_assets:
            sym = (a.get("symbol") or "").upper()
            try:
                flow_7d = float(a.get("flow_7d_usd") or 0)
            except (TypeError, ValueError):
                flow_7d = 0.0
            if sym == "BTC":
                btc_net = flow_7d
            elif sym == "ETH":
                eth_net = flow_7d
            else:
                others.append((sym, flow_7d))
            total_net += flow_7d

        def _fmt_flow(v):
            if v is None:
                return "—", "#94a3b8"
            try:
                v = float(v)
            except Exception:
                return "—", "#94a3b8"
            sign = "+" if v >= 0 else "-"
            color = "#dc2626" if v > 0 else "#16a34a" if v < 0 else "#64748b"
            av = abs(v)
            if av >= 1e9:
                return f"{sign}${av/1e9:.2f}B", color
            elif av >= 1e6:
                return f"{sign}${av/1e6:.0f}M", color
            elif av >= 1e3:
                return f"{sign}${av/1e3:.0f}K", color
            else:
                return f"{sign}${av:.0f}", color

        btc_net_str, btc_net_color = _fmt_flow(btc_net)
        eth_net_str, eth_net_color = _fmt_flow(eth_net)
        total_net_str, total_net_color = _fmt_flow(total_net)

        # 分项拆解（使「全部 ETF 合计」可核验）：BTC + ETH + 其他 = 合计。
        # 其他按绝对额降序取前 3 个币种做明细，其余仅计金额。
        others_net = sum(v for _, v in others)
        others_net_str, _ = _fmt_flow(others_net)
        others_sorted = sorted(others, key=lambda x: -abs(x[1]))
        others_detail = " · ".join(
            f"{s} {'+' if v >= 0 else ''}{v/1e6:.0f}M" for s, v in others_sorted[:3]
        )
        breakdown = (f"分项：BTC {btc_net_str} + ETH {eth_net_str} + 其他 {others_net_str}"
                     + (f"（{others_detail}）" if others_detail else ""))

        # W-10：单日净流入强制标注「上一交易日(MM-DD)」（不得用无日期限定的「单日」）
        _latest_by_sym = {}
        for a in etf_assets:
            s = (a.get("symbol") or "").upper()
            if a.get("latest_flow_usd") is not None:
                _latest_by_sym[s] = float(a.get("latest_flow_usd") or 0)
        _l_total_s, _ = _fmt_flow(sum(_latest_by_sym.values()))
        _l_btc_s, _ = _fmt_flow(_latest_by_sym.get("BTC"))
        _l_eth_s, _ = _fmt_flow(_latest_by_sym.get("ETH"))
        _prev_day_line = (f"上一交易日({_etf_md}) 净流入：BTC {_l_btc_s} · "
                          f"ETH {_l_eth_s} · 合计 {_l_total_s}")

        html_parts.append(f"""
          <!-- ETF 子模块 -->
          <div style="background:linear-gradient(135deg,#f0f9ff,#e0f2fe);border-radius:8px;padding:10px 12px;margin-bottom:8px">
            <div style="font-size:11.5px;font-weight:700;color:#0369a1;margin-bottom:6px">📈 ETF 资金流（近 7 日累计 · 上一交易日({_etf_md}){f'，滞后 {_etf_lag} 天' if (_etf_lag is not None and _etf_lag > 0) else ''}）</div>
            <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:6px">
              <div style="text-align:center">
                <div style="font-size:10px;color:#64748b">BTC ETF</div>
                <div style="font-size:14px;font-weight:700;color:{btc_net_color}">{btc_net_str}</div>
              </div>
              <div style="text-align:center">
                <div style="font-size:10px;color:#64748b">ETH ETF</div>
                <div style="font-size:14px;font-weight:700;color:{eth_net_color}">{eth_net_str}</div>
              </div>
              <div style="text-align:center">
                <div style="font-size:10px;color:#64748b">全部 ETF 合计</div>
                <div style="font-size:14px;font-weight:700;color:{total_net_color}">{total_net_str}</div>
              </div>
            </div>
            <div style="font-size:9.5px;color:#0369a1;margin-top:6px;line-height:1.5;font-weight:600">{_prev_day_line}</div>
            <div style="font-size:9.5px;color:#64748b;margin-top:3px;line-height:1.5">{breakdown}</div>
          </div>
        """)

    # 稳定币供应
    if isinstance(stab, dict) and stab.get("status") == "ok":
        total_usd = stab.get("total_usd")
        change_7d_pct = stab.get("change_7d_pct")
        change_1d_pct = stab.get("change_1d_pct")

        # 7日供应变化金额（近似：总供应量 * 7日变化率）
        supply_change_str = ""
        if change_7d_pct is not None and total_usd is not None:
            try:
                sc = float(total_usd) * float(change_7d_pct) / 100.0
                sign = "+" if sc >= 0 else ""
                color = "#dc2626" if sc > 0 else "#16a34a" if sc < 0 else "#64748b"
                if abs(sc) >= 1e9:
                    supply_change_str = f'<span style="color:{color};font-weight:700">{sign}${sc/1e9:.2f}B</span>'
                else:
                    supply_change_str = f'<span style="color:{color};font-weight:700">{sign}${sc/1e6:.0f}M</span>'
            except Exception:
                supply_change_str = "—"
        elif change_7d_pct is not None:
            chg_str, chg_color = _fmt_pct(change_7d_pct)
            supply_change_str = f'<span style="color:{chg_color};font-weight:700">{chg_str}</span>'

        # 顶部3稳定币变化（如果有 top_3）
        top_3 = stab.get("top_3") or []
        stable_html = ""
        if top_3:
            for s in top_3[:3]:
                sym = s.get("symbol", "?")
                chg = s.get("change_7d")
                chg_str, chg_color = _fmt_pct(chg)
                stable_html += f'<span style="font-size:10.5px;background:#fff;padding:2px 8px;border-radius:4px;color:#334155;margin-right:4px">{sym} <span style="color:{chg_color};font-weight:600">{chg_str}</span></span>'
        else:
            # 兜底：显示1日/7日/总供应，分行展示避免拥挤
            rows = []
            if change_1d_pct is not None:
                c1_str, c1_color = _fmt_pct(change_1d_pct)
                rows.append(f'<div style="font-size:10.5px;color:#334155;margin:2px 0">1日变化：<span style="color:{c1_color};font-weight:600">{c1_str}</span></div>')
            if change_7d_pct is not None:
                c7_str, c7_color = _fmt_pct(change_7d_pct)
                rows.append(f'<div style="font-size:10.5px;color:#334155;margin:2px 0">7日变化：<span style="color:{c7_color};font-weight:600">{c7_str}</span></div>')
            if total_usd is not None:
                rows.append(f'<div style="font-size:10.5px;color:#334155;margin:2px 0">总供应：<span style="color:#166534;font-weight:600">${total_usd/1e9:.0f}B</span></div>')
            stable_html = "".join(rows)

        html_parts.append(f"""
          <!-- 稳定币子模块 -->
          <div style="background:linear-gradient(135deg,#f0fdf4,#dcfce7);border-radius:8px;padding:10px 12px;margin-bottom:8px">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
              <div style="font-size:11.5px;font-weight:700;color:#166534">💵 稳定币供应（7日）</div>
              {supply_change_str}
            </div>
            <div>{stable_html}</div>
          </div>
        """)

    # 交易所净流量
    exchange_assets = exchange_flow.get("assets") or []
    if exchange_flow.get("status") == "ok" and exchange_assets:
        # 从 assets 里分出净流入/净流出
        top_in = sorted(
            [a for a in exchange_assets if (a.get("net_flow_usd") or 0) > 0],
            key=lambda x: x.get("net_flow_usd") or 0,
            reverse=True,
        )
        top_out = sorted(
            [a for a in exchange_assets if (a.get("net_flow_usd") or 0) < 0],
            key=lambda x: abs(x.get("net_flow_usd") or 0),
            reverse=True,
        )

        html_parts.append("""
          <!-- 交易所净流子模块 -->
          <div style="background:#fafafa;border-radius:8px;padding:10px 12px">
            <div style="font-size:11.5px;font-weight:700;color:#475569;margin-bottom:6px">🏦 交易所净流量 TOP（7日）</div>
            <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse">
              <tr>
                <td width="50%" valign="top" style="padding-right:6px">
                  <div style="font-size:10px;color:#dc2626;font-weight:600;margin-bottom:4px">▲ 净流入（提币/看多）</div>
        """)

        for item in top_in[:5]:
            sym = item.get("symbol", "?")
            net = item.get("net_flow_usd")
            net_str = _fmt_mcap(net) if net else "—"
            html_parts.append(f"""
              <div style="display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid #f1f5f9;font-size:11px">
                <span style="color:#334155;font-weight:600">{sym}</span>
                <span style="color:#dc2626;font-weight:600">+{net_str}</span>
              </div>
            """)

        html_parts.append("""
                </td>
                <td width="50%" valign="top" style="padding-left:6px;border-left:1px solid #e2e8f0">
                  <div style="font-size:10px;color:#16a34a;font-weight:600;margin-bottom:4px">▼ 净流出（充币/看空）</div>
        """)

        for item in top_out[:5]:
            sym = item.get("symbol", "?")
            net = item.get("net_flow_usd")
            net_str = _fmt_mcap(abs(float(net))) if net else "—"
            html_parts.append(f"""
              <div style="display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid #f1f5f9;font-size:11px">
                <span style="color:#334155;font-weight:600">{sym}</span>
                <span style="color:#16a34a;font-weight:600">-{net_str}</span>
              </div>
            """)

        html_parts.append("""
                </td>
              </tr>
            </table>
          </div>
        """)
    else:
        # W-07 路由校验：status != "ok"（或无可渲染行）→ 数值位一律「数据不可用」，禁止渲染 0
        html_parts.append(f"""
          <!-- 交易所净流子模块（不可用显式标注，不渲染 0） -->
          <div style="background:#fafafa;border-radius:8px;padding:10px 12px">
            <div style="font-size:11.5px;font-weight:700;color:#475569;margin-bottom:4px">🏦 交易所净流量 TOP（7日）</div>
            <div style="font-size:11px;color:#dc2626">数据不可用</div>
          </div>
        """)

    html_parts.append("</div>")

    # ════════════════════════════════════════════════════════
    # 模块 4：🐳 链上异动（巨鲸转账 + 持仓集中度 + KOL链上信号）
    # ════════════════════════════════════════════════════════
    html_parts.append(f"""
      <!-- 模块4：链上异动 -->
      <div style="background:#fff;border-radius:10px;padding:12px 14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,0.05)">
        <div style="font-size:13px;font-weight:700;color:#0f172a;margin-bottom:8px">🐳 链上异动</div>
    """)

    # 大额转账
    if whale_moves.get("status") == "ok":
        transfers = whale_moves.get("transfers") or []
        if transfers:
            # W-08-4：拆两栏 —— 影响供给类 / 参考类（稳定币·黄金代币不构成 BTC/ETH 供给变动）。
            # 依据：邮件正文把「XAUt 和 USDC 大额转账为主」当成了供给信号，实为避险/结算通道。
            _supply_xfers = [t for t in transfers
                             if str(t.get("symbol") or "").upper() not in _REFERENCE_SYMBOLS]
            _ref_xfers = [t for t in transfers
                          if str(t.get("symbol") or "").upper() in _REFERENCE_SYMBOLS]

            def _xfer_row(t: dict, dim: bool = False) -> str:
                sym = t.get("symbol", "?")
                amount_usd = t.get("value_usd") or t.get("amount_usd")
                amt_str = _fmt_mcap(amount_usd) if amount_usd else "—"
                from_label = _resolve_addr_label(
                    t.get("from_label"), t.get("from_labels"),
                    t.get("from_label_names"), t.get("from_address"))
                to_label = _resolve_addr_label(
                    t.get("to_label"), t.get("to_labels"),
                    t.get("to_label_names"), t.get("to_address"))
                direction = t.get("direction") or ""
                is_inflow = "exchange" in str(direction).lower() and "in" in str(direction).lower()
                is_outflow = "exchange" in str(direction).lower() and "out" in str(direction).lower()
                dot_color = "#16a34a" if is_outflow else "#dc2626" if is_inflow else "#7c3aed"
                return f"""
                  <div style="padding:5px 8px;margin-bottom:3px;border-radius:5px;background:#fafafa;border-left:2px solid {dot_color};font-size:11px;{'opacity:0.7' if dim else ''}">
                    <div style="display:flex;justify-content:space-between;align-items:center">
                      <span style="font-weight:700;color:#0f172a">{sym}</span>
                      <span style="color:#475569;font-weight:600">{amt_str}</span>
                    </div>
                    <div style="font-size:10px;color:#64748b;margin-top:1px">
                      {from_label} → {to_label}
                    </div>
                  </div>
                """

            html_parts.append(f"""
              <div style="font-size:11.5px;font-weight:700;color:#7c3aed;margin-bottom:5px">💸 大额转账（24h）</div>
              <div style="font-size:10px;color:#64748b;margin-bottom:4px">影响供给类（构成潜在买卖压）</div>
            """)
            if _supply_xfers:
                for t in _supply_xfers[:5]:
                    html_parts.append(_xfer_row(t))
            else:
                html_parts.append('<div style="font-size:10.5px;color:#94a3b8">无</div>')
            if _ref_xfers:
                html_parts.append(
                    '<div style="font-size:10px;color:#94a3b8;margin:6px 0 4px">'
                    '参考类（稳定币 · 黄金代币，不构成 BTC/ETH 供给变动）</div>'
                )
                for t in _ref_xfers[:3]:
                    html_parts.append(_xfer_row(t, dim=True))

    # 持仓集中度
    if holder_conc.get("status") == "ok":
        top_concentrated = holder_conc.get("most_concentrated") or []
        whales_buying = holder_conc.get("whale_buying") or []
        whales_selling = holder_conc.get("whale_selling") or []

        _hc_as_of = str(holder_conc.get("snapshot_date") or "")[:10]
        _hc_as_of_txt = f"（截至 {_hc_as_of}）" if _hc_as_of else ""
        html_parts.append(f"""
          <div style="margin-top:8px">
            <div style="font-size:11.5px;font-weight:700;color:#b45309;margin-bottom:5px">🎯 巨鲸动向{_hc_as_of_txt}</div>
            <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse">
              <tr>
                <td width="50%" valign="top" style="padding-right:6px">
                  <div style="font-size:10px;color:#dc2626;font-weight:600;margin-bottom:3px">增持中</div>
        """)

        for item in whales_buying[:5]:
            sym = item.get("symbol", "?")
            chg = item.get("whale_balance_change_7d_pct") or item.get("whale_change_pct")
            chg_str, chg_color = _fmt_pct(chg)
            html_parts.append(f"""
              <div style="display:flex;justify-content:space-between;padding:2px 0;font-size:10.5px">
                <span style="color:#334155">{sym}</span>
                <span style="color:#dc2626;font-weight:600">{chg_str}</span>
              </div>
            """)

        html_parts.append("""
                </td>
                <td width="50%" valign="top" style="padding-left:6px;border-left:1px solid #f1f5f9">
                  <div style="font-size:10px;color:#16a34a;font-weight:600;margin-bottom:3px">减持中</div>
        """)

        for item in whales_selling[:5]:
            sym = item.get("symbol", "?")
            chg = item.get("whale_balance_change_7d_pct") or item.get("whale_change_pct")
            chg_str, _ = _fmt_pct(chg)
            html_parts.append(f"""
              <div style="display:flex;justify-content:space-between;padding:2px 0;font-size:10.5px">
                <span style="color:#334155">{sym}</span>
                <span style="color:#16a34a;font-weight:600">{chg_str}</span>
              </div>
            """)

        html_parts.append("""
                </td>
              </tr>
            </table>
          </div>
        """)

    # KOL 链上信号（兜底）
    signals = kol_onchain.get("signals") or []
    # 2026-09-27（早报重构 P0 / W-01）：原卡只印「币种 + 类型 + 分析师」，无时间/无金额/无方向，
    # 属不可判定的噪声卡片（重构方案 §2.13）。改为：仅保留「有事件时间 且 有金额」的信号，
    # 并把金额与事件时间一并渲染。字段名以 fetch_kol_onchain_signals 的 SELECT 为准
    # （event_time / event_direction / event_usd_value / event_amount）。
    # W-01：就绪门必须查 event_time（事件发生时间）。原实现查 created_at（入库时间，DB 默认值恒非空）
    # → 门形同虚设。event_time 全空时整块不出（不渲染空卡、不渲染占位）——该类信号历史命中率 20%，
    # 正确处置是不出，不是「带标注地出」。
    # ⚠️ 登记（2026-09-27 复验实测）：biz.kol_signal 全表 529 行中 event_time 非空仅 5 行，
    # 且那 5 行 created_at 都在 24h 窗口外、采集侧从未写入该字段 → **KOL 卡实际等价于「有意停用」**。
    # 这是符合预期的处置（kol_onchain 回测命中率 20%），**不要当成「卡片坏了」去修**；
    # 若要恢复出卡，须先在采集侧落 event_time。
    _kol_ready = [
        s for s in signals
        if s.get("event_time") and (s.get("event_usd_value") or s.get("event_amount"))
    ]
    if _kol_ready and kol_onchain.get("status") == "ok":
        signals = _kol_ready
        # 过滤掉 symbol 明显无效的信号（长度>12、含非字母数字、常见误判词）
        INVALID_SYMBOLS = {"LAPTOP", "PHONE", "TABLET", "DESKTOP", "COMPUTER", "MOBILE"}
        def _is_valid_sym(s):
            if not s:
                return False
            s = str(s).strip().upper()
            if not s or len(s) > 12 or len(s) < 2:
                return False
            if s in INVALID_SYMBOLS:
                return False
            if not s.replace(".", "").replace("-", "").isalnum():
                return False
            return True

        valid_signals = [s for s in signals if _is_valid_sym(s.get("symbol") or s.get("event_token"))]
        if not valid_signals:
            valid_signals = signals  # 全部过滤掉时兜底，避免空列表

        kol_count = len(kol_onchain.get("kols") or [])
        html_parts.append(f"""
          <div style="margin-top:8px">
            <div style="font-size:11.5px;font-weight:700;color:#0891b2;margin-bottom:5px">🔍 KOL 链上信号（{kol_count}位分析师）</div>
        """)
        SUBTYPE_CN = {
            "exchange_flow": "交易所", "smart_money": "聪明钱",
            "accumulation": "大额吸筹", "whale_move": "巨鲸转账",
            "distribution": "大额派发", "liquidation": "爆仓清算",
        }
        for sig in valid_signals[:4]:
            subtype = sig.get("signal_subtype") or ""
            subtype_cn = SUBTYPE_CN.get(subtype, subtype)
            sym = (sig.get("event_token")
                   or sig.get("symbol")
                   or "?")
            if isinstance(sym, str):
                sym = sym.strip().upper()
            kol = sig.get("kol_name") or ""
            is_bullish = "in" in str(sig.get("event_direction", "")).lower() or "accum" in subtype.lower()
            dot = "#dc2626" if is_bullish else "#16a34a"
            _usd = sig.get("event_usd_value")
            if isinstance(_usd, (int, float)) and _usd:
                _money = f"{round(float(_usd) / 1e6, 2)}M USD"
            else:
                _money = str(sig.get("event_amount") or "")
            _at = str(sig.get("event_time") or "")[:16]
            if _at:
                _at = f"事件时间 {_at}"
            _meta = " · ".join(x for x in (_money, _at) if x)

            html_parts.append(f"""
              <div style="padding:4px 8px;margin-bottom:2px;border-radius:4px;background:#fafafa;font-size:10.5px;border-left:2px solid {dot}">
                <span style="font-weight:700;color:#0f172a">{sym}</span>
                <span style="color:#64748b;margin-left:4px">{subtype_cn}</span>
                <span style="color:#475569;margin-left:6px">{_meta}</span>
                <span style="color:#94a3b8;margin-left:6px;float:right">{kol}</span>
              </div>
            """)
        html_parts.append("</div>")

    html_parts.append("</div>")

    # ════════════════════════════════════════════════════════
    # 模块 5：📅 催化剂（解锁 + 宏观事件）
    # ════════════════════════════════════════════════════════
    catalyst = brief.get("M6_catalyst") or {}
    macro_events = catalyst.get("hardcoded") or []
    token_events = catalyst.get("token_events") or []
    unlock_list = upcoming_unlocks.get("unlocks") or []

    html_parts.append(f"""
      <!-- 模块5：催化剂 -->
      <div style="background:#fff;border-radius:10px;padding:12px 14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,0.05)">
        <div style="font-size:13px;font-weight:700;color:#0f172a;margin-bottom:8px">📅 近期催化剂</div>
    """)

    # 解锁事件（更重要，放前面）
    if unlock_list:
        html_parts.append(f"""
          <div style="font-size:11.5px;font-weight:700;color:#dc2626;margin-bottom:5px">🔓 即将解锁（未来14天，含今日）</div>
        """)
        for u in unlock_list[:6]:
            sym = u.get("symbol") or u.get("token") or "?"
            unlock_date = u.get("unlock_date") or u.get("date") or ""
            amount = u.get("amount") or u.get("unlock_amount") or ""
            value_usd = u.get("value_usd") or u.get("unlock_value_usd")
            pct = (u.get("unlock_ratio_circulating")
                   or u.get("unlock_ratio_total")
                   or u.get("unlock_ratio_mcap")
                   or u.get("pct_of_supply")
                   or u.get("unlock_pct"))
            # 判断比值类型，用于提示标签
            circ_src = u.get("unlock_ratio_circulating_src")  # 'source' / 'computed' / None
            approx_prefix = "~" if circ_src == "computed" else ""
            pct_type = "流通"
            if pct is None:
                pct_label = "—"
            elif u.get("unlock_ratio_circulating") is not None and pct == u.get("unlock_ratio_circulating"):
                pct_label = f"占流通 {approx_prefix}{float(pct):.2f}%"
            elif u.get("unlock_ratio_total") is not None and pct == u.get("unlock_ratio_total"):
                pct_label = f"占总供给 {float(pct):.2f}%"
            elif u.get("unlock_ratio_mcap") is not None and pct == u.get("unlock_ratio_mcap"):
                pct_label = f"占市值 {float(pct):.2f}%"
            else:
                pct_label = f"{float(pct):.2f}%"
            try:
                days_until = (date.fromisoformat(str(unlock_date)[:10]) - date.today()).days
                days_str = f"{days_until}天后" if days_until > 0 else "今天" if days_until == 0 else "已过"
                days_color = "#dc2626" if 0 <= days_until <= 3 else ("#f59e0b" if days_until <= 7 else "#64748b")
            except Exception:
                days_str = str(unlock_date)[:10] if unlock_date else "—"
                days_color = "#64748b"

            value_str = _fmt_mcap(value_usd) if value_usd else "—"

            html_parts.append(f"""
              <div style="padding:5px 8px;margin-bottom:3px;border-radius:5px;background:#fef2f2;border-left:2px solid #dc2626;font-size:11px">
                <div style="display:flex;justify-content:space-between;align-items:center">
                  <span style="font-weight:700;color:#0f172a">{sym}</span>
                  <span style="font-size:10px;color:{days_color};font-weight:600">{days_str}</span>
                </div>
                <div style="font-size:10px;color:#64748b;margin-top:1px">
                  解锁 {value_str} · {pct_label}
                </div>
              </div>
            """)

    # 宏观 & 代币事件
    all_events = macro_events + token_events
    try:
        all_events.sort(key=lambda e: e.get("date", "9999"))
    except Exception:
        pass

    if all_events:
        html_parts.append(f"""
          <div style="margin-top:8px">
            <div style="font-size:11.5px;font-weight:700;color:#6366f1;margin-bottom:5px">📆 宏观 & 代币事件</div>
        """)
        for ev in all_events[:6]:
            ev_date = ev.get("date", "")
            ev_name = ev.get("event", "?")
            ev_type = ev.get("type", "")
            try:
                days_until = (date.fromisoformat(ev_date) - date.today()).days
                days_str = f"{days_until}d" if days_until > 0 else "今天" if days_until == 0 else "已过"
                days_color = "#dc2626" if 0 <= days_until <= 7 else ("#f59e0b" if days_until <= 14 else "#94a3b8")
            except Exception:
                days_str = ""
                days_color = "#94a3b8"

            type_badge = ""
            if ev_type == "macro":
                type_badge = '<span style="background:#e0e7ff;color:#4338ca;font-size:9px;padding:0 4px;border-radius:2px;font-weight:600;margin-right:4px">宏观</span>'
            elif ev_type == "unlock":
                type_badge = '<span style="background:#fef3c7;color:#92400e;font-size:9px;padding:0 4px;border-radius:2px;font-weight:600;margin-right:4px">解锁</span>'
            elif ev_type in ("listing", "exchange_listing"):
                type_badge = '<span style="background:#dcfce7;color:#166534;font-size:9px;padding:0 4px;border-radius:2px;font-weight:600;margin-right:4px">上币</span>'

            html_parts.append(f"""
              <div style="padding:4px 0;border-bottom:1px solid #f1f5f9;display:flex;justify-content:space-between;align-items:center;font-size:11px">
                <div style="min-width:0;overflow:hidden;text-overflow:ellipsis">
                  {type_badge}<span style="color:#334155">{ev_name}</span>
                </div>
                <span style="font-size:10px;color:{days_color};font-weight:600;flex-shrink:0;margin-left:8px">{days_str}</span>
              </div>
            """)
        html_parts.append("</div>")

    html_parts.append("</div>")

    # ════════════════════════════════════════════════════════
    # 模块 5.5：📡 催化剂热点 —— 已移除（2026-09-27，早报重构 P0）
    # 原卡渲染全部 B/C 级热点，实测上屏内容为脏数据（正文出现爬虫署名「作者：谷昱，
    # ChainCatcher」、英文截断半句），且卡片固定文案自称「高置信度 A 级见邮件 Alert」
    # 把读者指向另一封邮件（闭环断裂）；6 条全标「仅观察/弱共振」却占一整屏。
    # P0 判据是「不撒谎」，该卡当前无法被诚实渲染 → 整块移除，不做半修。
    # 后续重放条件：催化剂 ai_summary 正文质量修复 且 仅 A 级 且 带原文链接。
    # ════════════════════════════════════════════════════════

    # ════════════════════════════════════════════════════════
    # 模块 6：🎯 机会清单
    # ════════════════════════════════════════════════════════
    is_fallback = bool(all_opps and all_opps[0].get("is_fallback"))
    section_title = "🔥 今日热门币种" if is_fallback else "🎯 精选机会"

    html_parts.append(f"""
      <!-- 模块6：机会清单 -->
      <div style="background:#fff;border-radius:10px;padding:12px 14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,0.05)">
        <div style="font-size:13px;font-weight:700;color:#0f172a;margin-bottom:2px">
          {section_title}
        </div>
        <div style="font-size:10.5px;color:#94a3b8;margin-bottom:8px">按可验证性（是否被回测）分组 · 组内按证据等级与分数排序 · 仅供参考</div>
    """)

    _shown_keys: set = set()
    if all_opps:
        display_opps = all_opps[:8 if is_fallback else 6]
        # W-04：记录已在上方「精选机会」出现（含折叠去向）的标的，供下方「持仓提示」去重。
        for _o in all_opps[:8 if is_fallback else 6]:
            _shown_keys |= _target_keys(_o.get("target"), _o.get("symbol"), _o.get("name"))
        # M4-1 单一口径裁决：剔除已在「交易方向/高亮/高危/赛道轮动」给出结论的标的，
        # 折叠为「关联」一行，避免同一标的在邮件内出现两份分数/方向结论。
        _folded_opps, _kept_opps = [], []
        for opp in display_opps:
            _ok = _target_keys(opp.get("target"), opp.get("symbol"), opp.get("name"))
            _owner = next((_tgt_owners[k] for k in _ok if k in _tgt_owners), None)
            if _owner:
                _folded_opps.append((opp.get("target") or opp.get("symbol") or "?", _owner))
            else:
                _kept_opps.append(opp)
        display_opps = _kept_opps
        for opp in display_opps:
            tier = opp.get("conviction_tier", "?")
            score = opp.get("conviction_score", 0)
            score_str = f"{score:.0f}" if isinstance(score, (int, float)) else str(score)
            target = opp.get("target", "?")
            direction = opp.get("direction", "?")
            trigger = opp.get("trigger_logic", "")
            sector = opp.get("sector", "")
            signal_sources = opp.get("signal_sources") or []

            SRC_LABEL = {
                "sector_leader": ("📈", "赛道领涨", "#059669", "#d1fae5"),
                "smart_money": ("🔵", "聪明钱", "#2563eb", "#dbeafe"),
                "exchange_flow": ("🏦", "交易所", "#d97706", "#fef3c7"),
                "accumulation": ("🟢", "吸筹", "#059669", "#d1fae5"),
                "whale_move": ("🐳", "巨鲸", "#7c3aed", "#ede9fe"),
                "distribution": ("🔴", "派发", "#dc2626", "#fee2e2"),
            }

            if tier == "HIGH":
                accent = "#dc2626"
                badge_bg = "#fee2e2"
                badge_color = "#991b1b"
                card_bg = "#fff1f2"
            elif tier == "MED":
                accent = "#f59e0b"
                badge_bg = "#fef3c7"
                badge_color = "#92400e"
                card_bg = "#fffbeb"
            else:
                accent = "#94a3b8"
                badge_bg = "#f1f5f9"
                badge_color = "#475569"
                card_bg = "#f8fafc"

            # W-04：估值类（MVRV）是持仓管理提示（止盈），不是看空方向；即便拿到旧
            # 数据（direction=short），也不得渲染成「▼ 看空」。用中性「◆ 止盈提示」
            # 并取 action_hint 作为文案。其余 watch/neutral（如 github/博弈）用「◆ 观望」，
            # 避免把「开发停滞」等误标成「止盈」。
            _is_val_hint = (opp.get("signal_type") in ("mvrv_deep_over", "mvrv_over_watch")
                            or bool(opp.get("is_valuation")))
            _is_hold_hint = (direction in ("watch", "neutral") or _is_val_hint)
            if _is_val_hint:
                dir_icon, dir_color, dir_cn = "◆", "#64748b", "止盈提示"
            elif direction in ("watch", "neutral"):
                dir_icon, dir_color, dir_cn = "◆", "#94a3b8", "观望"
            else:
                dir_icon = "▲" if direction == "long" else "▼" if direction == "short" else "◆"
                dir_color = "#dc2626" if direction == "long" else "#16a34a" if direction == "short" else "#64748b"
                dir_cn = "看多" if direction == "long" else "看空" if direction == "short" else direction

            _ah = str(opp.get("action_hint") or "").strip()
            body_text = trigger
            if _is_hold_hint and _ah:
                body_text = f"{_ah} · {trigger}" if trigger else _ah

            # W-03：可验证性徽章——从未回测/exempt_* 显式标「未回测」，与 calibrated_ok
            # 的「回测背书」对称，避免读者把「没回测过」误当「同等可信」。
            gate = _opp_gate(opp)
            gate_badge = ""
            if gate == "calibrated_ok":
                gate_badge = ('<span style="background:#dcfce7;color:#166534;font-size:9px;'
                              'padding:1px 5px;border-radius:3px;font-weight:700">回测背书</span>')
            elif gate.startswith("exempt"):
                gate_badge = ('<span style="background:#fee2e2;color:#b91c1c;font-size:9px;'
                              'padding:1px 5px;border-radius:3px;font-weight:700">未回测</span>')
            # W-14：校准行——命中率须与样本量、窗口同时出现，并按时长/命中率诚实标注
            cal_line = _cal_line_html(opp.get("calibration_status"))
            # M2-A1/A2：降档/兜底项给出原因，避免读者看到「78 分却是 MED」无从理解。
            _opp_demote = str(opp.get("display_note") or opp.get("tier_demote_reason") or "").strip()
            demote_line = (f'<div style="font-size:9.5px;color:#b45309;margin-top:3px">'
                           f'⬇️ 降档说明：{html.escape(_opp_demote)}</div>' if _opp_demote else "")

            # 信号源标签
            src_html = ""
            if signal_sources:
                src_tags = []
                for src in signal_sources[:3]:
                    if src in SRC_LABEL:
                        icon, name, color, bg = SRC_LABEL[src]
                        src_tags.append(
                            f'<span style="background:{bg};color:{color};font-size:9.5px;padding:1px 5px;border-radius:3px;font-weight:600">{icon} {name}</span>'
                        )
                src_html = " ".join(src_tags)

            meta_parts = []
            if sector:
                meta_parts.append(f"🏷️ {sector}")
            meta_str = " · ".join(meta_parts)

            html_parts.append(f"""
            <div style="padding:9px 11px;margin:5px 0;border-radius:7px;border-left:3px solid {accent};background:{card_bg}">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:3px">
                <div style="display:flex;align-items:center;flex-wrap:wrap">
                  <span style="font-size:14px;font-weight:700;color:#0f172a">{target}</span>
                  <span style="color:{dir_color};font-size:12px;font-weight:600;margin-left:8px">{dir_icon} {dir_cn}</span>
                </div>
                <div style="display:flex;align-items:center;gap:5px">
                  <span style="background:{badge_bg};color:{badge_color};font-size:10px;padding:1px 6px;border-radius:3px;font-weight:700">{tier}</span>
                  {gate_badge}
                  <span style="font-size:11px;color:#64748b;font-weight:600">{score_str}分</span>
                </div>
              </div>
              {f'<div style="font-size:10.5px;color:#64748b">{meta_str}</div>' if meta_str else ''}
              {src_html}
              <div style="color:#475569;font-size:11px;margin-top:4px;line-height:1.4">{body_text}</div>
              {cal_line}
              {demote_line}
            </div>
            """)
        # M4-1：折叠项以「关联」一行说明去向（信息不丢，只是不再并排列示）
        if _folded_opps:
            _fold_txt = " · ".join(f"{_t} → 见「{_s}」" for _t, _s in _folded_opps)
            html_parts.append(f"""
            <div style="margin-top:4px;padding:6px 9px;background:#f8fafc;border-radius:6px;font-size:10.5px;color:#64748b;line-height:1.6">
              关联折叠（同一标的已在其他板块给出结论，此处不重复列示）：{_fold_txt}
            </div>
            """)
    else:
        html_parts.append('<div style="color:#94a3b8;font-size:12px;padding:16px;text-align:center">暂无推荐机会</div>')

    html_parts.append("</div>")

    # ════════════════════════════════════════════════════════
    # 模块 6b：◆ 持仓提示（W-04）
    # ════════════════════════════════════════════════════════
    # direction=watch/neutral 中的**估值类**（MVRV）不是看空方向，而是持仓者动作
    # （不追高 / 中线止盈）。它们会被「精选机会」按 gate 沉到尾部、被 top-N 截断，
    # 故独立小卡承载，避免「止盈」这一真正可执行的提示被埋没。
    # 仅收估值类（mvrv_* / is_valuation）；github「开发停滞」等 watch 不是止盈，不并入。
    _hold_hints = []
    for _o in all_opps:
        _is_val = (_o.get("signal_type") in ("mvrv_deep_over", "mvrv_over_watch")
                   or bool(_o.get("is_valuation")))
        if not _is_val:
            continue
        if _target_keys(_o.get("target"), _o.get("symbol"), _o.get("name")) & _shown_keys:
            continue
        _hold_hints.append(_o)
    if _hold_hints:
        _hh_rows = []
        for _h in _hold_hints[:3]:
            _tgt = _h.get("target") or _h.get("symbol") or "?"
            _hint = str(_h.get("action_hint") or "").strip()
            _metric = str(_h.get("key_metric") or "").strip()
            _tail = " · ".join(x for x in (_hint, _metric) if x)
            _hh_rows.append(
                '<div style="padding:7px 10px;margin:4px 0;border-radius:6px;'
                'background:#f8fafc;border-left:3px solid #94a3b8">'
                f'<span style="font-size:13px;font-weight:700;color:#0f172a">{_tgt}</span>'
                '<span style="color:#64748b;font-size:12px;font-weight:600;margin-left:6px">◆ 止盈提示</span>'
                f'<div style="color:#475569;font-size:11px;margin-top:3px">{_tail}</div></div>'
            )
        html_parts.append(f"""
      <!-- 模块6b：持仓提示 -->
      <div style="background:#fff;border-radius:10px;padding:12px 14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,0.05)">
        <div style="font-size:13px;font-weight:700;color:#0f172a;margin-bottom:2px">◆ 持仓提示</div>
        <div style="font-size:10.5px;color:#94a3b8;margin-bottom:6px">估值高位的不追高 / 止盈提示 · 非做空方向 · 仅供参考</div>
        {''.join(_hh_rows)}
      </div>
    """)

    # ════════════════════════════════════════════════════════
    # 其他信号（精简折叠区）
    # ════════════════════════════════════════════════════════
    extra_blocks = []

    # 宏观背离
    divs_data = brief.get("M7_divergence") or []
    if divs_data:
        items = []
        for d in divs_data[:3]:
            sig_name = d.get("signal", "?")
            label = d.get("label", "?")
            interp = d.get("interpretation", "")
            icon = "🔴" if label == "DANGEROUS" else "🟡"
            items.append(f'<span style="background:#fee2e2;color:#991b1b;font-size:11px;padding:2px 6px;border-radius:4px;margin-right:4px">{icon} {sig_name}</span> {interp}')
        extra_blocks.append(("📡 宏观背离", "<br>".join(items)))

    # 聪明钱背离
    sm = brief.get("M8_smart_money") or {}
    if isinstance(sm, dict) and sm.get("status") == "ok" and (sm.get("bullish") or sm.get("bearish")):
        bull = sm.get("bullish") or []
        bear = sm.get("bearish") or []
        bull_str = " ".join(f'<span style="color:#16a34a;font-weight:600">🐂{s.get("symbol","?")}</span>' for s in bull[:3])
        bear_str = " ".join(f'<span style="color:#dc2626;font-weight:600">🐻{s.get("symbol","?")}</span>' for s in bear[:3])
        # P2-3（审计 2026-09-28）：术语堆砌无上下文 → 补一行口径注解。
        _sm_note = ('<div style="font-size:9.5px;color:#94a3b8;margin-top:2px">'
                    '聪明钱 = 链上监控地址的净买入（🐂）/ 净卖出（🐻）方向</div>')
        extra_blocks.append(("🐋 聪明钱（链上监控地址净买卖）", f"{bull_str} {bear_str}{_sm_note}"))

    # Meme 风险
    meme = brief.get("M8_meme") or {}
    if isinstance(meme, dict) and meme.get("status") == "ok":
        summary = meme.get("summary") or {}
        # 2026-09-27（早报重构 P0）：原卡片只有「高危N·中危N」纯数字（如「中危102」），
        # 读者既不知是谁也无处置。改为给出「排雷 / 高危」名单（各最多 5 个）；无名单则不展示。
        _buckets = meme.get("buckets") or {}

        def _bucket_names(k: str) -> str:
            return "、".join(
                str(x.get("symbol") or x.get("name") or "?")
                for x in (_buckets.get(k) or [])[:5]
            )

        _block_names = _bucket_names("block")
        _high_names = _bucket_names("high")
        if _block_names or _high_names:
            _lines = []
            if _block_names:
                _lines.append(f"排雷 {summary.get('block', 0)}：{_block_names}")
            if _high_names:
                _lines.append(f"高危 {summary.get('high', 0)}：{_high_names}")
            extra_blocks.append(("🐸 Meme 风险（Meme 专项）", " · ".join(_lines)))

    if extra_blocks:
        html_parts.append("""
          <!-- 其他信号 -->
          <div style="background:#fff;border-radius:10px;padding:12px 14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,0.05)">
            <div style="font-size:12px;font-weight:700;color:#0f172a;margin-bottom:8px">📌 其他信号</div>
        """)
        for title, content in extra_blocks:
            html_parts.append(f"""
            <div style="padding:6px 8px;margin:3px 0;background:#f8fafc;border-radius:5px;font-size:11.5px">
              <b style="color:#334155">{title}</b>
              <div style="color:#475569;margin-top:2px">{content}</div>
            </div>
            """)
        html_parts.append("</div>")

    # 降级标注（分层：核心红/辅助黄/增强隐藏）
    degraded_badge = _render_degraded_badge(brief)
    if degraded_badge:
        html_parts.append(degraded_badge)

    # 页脚
    html_parts.append("""
      <div style="text-align:center;font-size:10px;color:#94a3b8;margin-top:12px;padding-bottom:8px">
        数据仅供参考，不构成投资建议 · 加密大盘早报
      </div>
    </div>
    """)
    return "\n".join(html_parts)


def _fear_greed_color(value):
    """恐贪指数颜色。"""
    if value is None:
        return "#64748b"
    try:
        v = int(value)
    except Exception:
        return "#64748b"
    if v >= 75:
        return "#16a34a"  # 极度贪婪 - 绿
    if v >= 55:
        return "#65a30d"  # 贪婪
    if v >= 45:
        return "#64748b"  # 中性
    if v >= 25:
        return "#f59e0b"  # 恐惧
    return "#dc2626"  # 极度恐惧 - 红


def main():
    parser = argparse.ArgumentParser(description="每日早报邮件发送")
    parser.add_argument("--dry-run", action="store_true", help="仅打印 HTML，不发送")
    args = parser.parse_args()

    # 1. 生成 brief：优先复用今日 08:30 快照落库的 overview。
    #    不重复 force_refresh 拉取全量数据（发信前重拉易卡顿/被收割为 stuck），
    #    且保证邮件与快照数据一致、均为已就绪的最新数据；快照缺失时兜底实时拉取。
    try:
        from datetime import timedelta as _td
        from macro_market import generate_morning_brief, get_market_overview, load_snapshot

        today = load_snapshot(date.today().isoformat())
        if not today:
            print("[INFO] 今日快照缺失，实时拉取 overview（兜底）")
            today = get_market_overview(force_refresh="1")
        y_date = (date.today() - _td(days=1)).isoformat()
        yesterday = load_snapshot(y_date)
        brief = generate_morning_brief(today, yesterday)
    except Exception as e:
        print(f"[ERROR] 生成 brief 失败: {e}")
        return 1

    # 2. 数据契约标准化 + 健康检查
    try:
        from brief_data_model import normalize_brief, check_brief_health
        brief = normalize_brief(brief)
        health = check_brief_health(brief)
        print(f"[INFO] 数据健康度: {health['score']}/100")
        if health["critical"]:
            print(f"[WARN] 核心数据缺失: {health['critical']}")
        if health["warning"]:
            print(f"[INFO] 辅助数据缺失: {health['warning']}")
    except Exception as e:
        print(f"[WARN] 数据标准化/健康检查失败，跳过: {e}")
        health = {"score": 0, "critical": [], "warning": []}

    # 3. 渲染 HTML
    try:
        html = render_brief_html(brief)
    except Exception as e:
        print(f"[ERROR] HTML 渲染失败: {e}")
        return 1

    if args.dry_run:
        print(html)
        return 0

    # 4. 发送邮件
    try:
        from crypto_research.config import get_settings
        from crypto_research.clients.notifier import EmailNotifier

        settings = get_settings(require_database=False)
        notifier = EmailNotifier(settings)
        if not notifier.configured:
            # 2026-09-27（早报重构 P0 收尾）：旧行为 `return 0` → 任务记 done 但邮件没发，
            # 属「没发却显示成功」，与 09-26 丢整天的陷阱同类（`task_manager.py:782` 只在
            # returncode != 0 时写 error）。改为非零退出 → `failed / exit code 1` →
            # `scheduler_watchdog` 告警 + 补跑（`daily_brief_email` 已在 KEY_JOBS）。
            # 不发信不可能造成重复投递，故不存在告警风暴。
            print("[ERROR] SMTP 未配置，早报邮件未发送（按失败处理，避免静默成功）")
            print(html)
            return 1

        m0 = brief.get("M0_tldr", {})
        subject = f"📊 加密大盘早报 {m0.get('date', date.today().isoformat())}"
        ok, msg = notifier.send(subject=subject, body_html=html, from_name="加密大盘早报")
        if ok:
            print(f"[OK] 早报邮件已发送: {msg}")
        else:
            print(f"[ERROR] 邮件发送失败: {msg}")
            return 1
    except Exception as e:
        print(f"[ERROR] 邮件发送异常: {e}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
