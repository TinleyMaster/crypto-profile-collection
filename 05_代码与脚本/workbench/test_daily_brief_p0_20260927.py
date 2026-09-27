#!/usr/bin/env python3
"""早报「投资指导意义」重构 P0 回归护栏（方案_大盘早报_投资指导意义重构_2026-09-27）。

运行：.venv/bin/python workbench/test_daily_brief_p0_20260927.py
      （纯离线：渲染层喂合成 brief；prompt 层用假 LLM 捕获 system/user prompt；
        不连 prod DB、不发信）

  P0-a：去硬编码置信度（删 {"high":85,...}）→ 头部改「证据覆盖 N/M 项」
  P0-b：数据门控 missing ≠ 0（None-aware 渲染 + 空段标注 + data_quality 块 + 硬约束）
  P0-c：渲染层兜底（横盘且无新鲜信号时 AI 仍给方向 → 强制「今日无操作」）
  P0-d：修假入口（href=0 的「查看详情」affordance 删除）
  P0-e：砍脏卡（催化剂热点 B/C 卡片整块移除；Meme 纯数字改名单；KOL 卡加就绪门）
  P0-f：邮件未发出不得静默成功（SMTP 未配 → 非零退出，交 task_manager/watchdog 可见）
  P1-a：交易方向可执行化（六要素齐备才进「交易方向」区，缺任一 → 降级「👀 观察」区且计数可查）
  P1-b：观望闸门（data_quality 中 ok 维度数 < 阈值 → 强制「今日无操作」，只降不升）
  P2-a/M4：单一口径裁决（同一 target 全邮件唯一结论：折叠关联 + 等级分组排序 + 同赛道唯一事实源 + 冲突裁决）
"""
import ast
import contextlib
import io
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_BIN = os.path.join(os.path.dirname(_HERE), "scripts", "bin")
_SCRIPTS_SRC = os.path.join(os.path.dirname(_HERE), "scripts", "src")
for _p in (_HERE, _SCRIPTS_BIN, _SCRIPTS_SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import send_daily_brief as sdb  # noqa: E402

passed = 0
failed = 0


def check(cond, name, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  \u2713 {name}")
    else:
        failed += 1
        print(f"  \u2717 {name}")
        if detail:
            print(f"    {detail}")


def _brief():
    """最小可用 brief（贴近 2026-09-27 早报：横盘、有高亮/风险）。"""
    return {
        "M0_tldr": {
            "date": "2026-09-27",
            "btc_price": 109800, "btc_change_24h_pct": 0.4,
            "eth_price": 3920, "eth_change_24h_pct": 0.2,
            "fear_greed": 72, "fear_greed_label": "Greed",
        },
        "DIFF": {"total_mcap_pct": 0.3},
        "M0_ai_summary": {
            "status": "ok", "headline": "AI赛道领涨，市场整体偏多", "bias": "偏多",
            "market_regime": "震荡", "conviction": "high",
            "trade_suggestions": [
                {"asset": "BTC", "direction": "做多", "horizon": "波段(1-2周)",
                 "trigger": "BTC 4h 收盘站上 110500", "invalidate": "BTC 跌破 107200",
                 "target": "114000-118000", "ref_price": 109800, "ref_as_of": "2026-09-27 08:30",
                 "reason": "ETF 持续净流入"},
                {"asset": "ETH", "direction": "做多", "horizon": "短线(1-3日)",
                 "trigger": "ETH 站上 3960", "invalidate": "ETH 跌破 3820",
                 "target": "4080", "ref_price": 3920, "ref_as_of": "2026-09-27 08:30",
                 "reason": "链上吸筹"},
            ],
            "watchlist": ["SOL", "AI"],
        },
        "M2_etf_flow": {
            "status": "ok", "latest_date": "2026-09-25",
            "assets": [{"symbol": "BTC", "flow_7d_usd": 1530e6}],
        },
        "M3_highlights": [{
            "target": "SOL", "direction": "long", "conviction_score": 67,
            "ai_analysis_v2": {"overall_score": 67, "confidence": "MED",
                               "reason_summary": "ETF 持续净流入", "key_drivers": ["链上吸筹"]},
        }],
        "M4_risks": [{"target": "2Z", "ai_analysis_v2": {"overall_score": 88}}],
        "M6_upcoming_unlocks": {
            "status": "ok",
            "unlocks": [{"symbol": "XPL", "unlock_date": "2026-09-29",
                         "unlock_value_usd": 147e6, "unlock_ratio_circulating": 63.2,
                         "risk_level": "high"}],
        },
    }


# ════════════════════════════════════════════════════════
# P0-d 假入口
# ════════════════════════════════════════════════════════
print("[P0-d] 假入口（href=0 的「查看详情」）")
_html = sdb.render_brief_html(_brief())
check("查看详情" not in _html, "高危信号条不再出现「查看详情 ↓」")
check('cursor:pointer">查看详情' not in _html, "假入口的 cursor:pointer span 已删")
check("今日高危信号（综合风险）" in _html, "高危信号条本体保留（只删假入口）")

# ════════════════════════════════════════════════════════
# P0-a 去硬编码置信度 → 证据覆盖
# ════════════════════════════════════════════════════════
print("[P0-a] 去硬编码置信度 → 证据覆盖 N/M")
_b = _brief()
_b["M0_ai_summary"]["data_quality"] = [
    {"section": "大盘概况", "status": "ok", "items": 1},
    {"section": "交易所净流量", "status": "empty", "items": 0},
    {"section": "即将解锁", "status": "empty", "items": 0},
]
_html = sdb.render_brief_html(_b)
check("证据覆盖" in _html, "头部改标「证据覆盖」")
check("1/3" in _html, "覆盖数 = status ok 维度数 / 总维度数（1/3）")
check("85%" not in _html, "硬编码 85% 已消失")
check("置信度" not in _html, "裸「置信度 N%」标签已消失")
check("证据覆盖</div>" in _html and "项 · 信心" in _html, "覆盖数带单位「项」并保留 LLM 自评信心")

# ════════════════════════════════════════════════════════
# P0-c 渲染层兜底
# ════════════════════════════════════════════════════════
print("[P0-c] 渲染层兜底：横盘 + 无新鲜信号 → 强制「今日无操作」")
_bc = _brief()
_bc.pop("M3_highlights")
_bc.pop("M4_risks")
_html = sdb.render_brief_html(_bc)
check("⚪ 今日无操作" in _html, "横盘且无信号 → 输出「今日无操作」")
check("做多" not in _html, "AI 的做多方向被拦截（不进入邮件）")
check("当日横盘、无新鲜信号，无明确可执行机会" in _html, "兜底原因写入正文")

print("[P0-c] 不误伤：横盘但有新鲜高亮/风险信号 → 保留 AI 方向")
_html2 = sdb.render_brief_html(_brief())
check("做多" in _html2 and "⚪ 今日无操作" not in _html2, "有新鲜信号时不降级")

print("[P0-c] 不误伤：有信号但不横盘 → 保留 AI 方向")
_bn = _brief()
_bn["M0_tldr"]["btc_change_24h_pct"] = -2.4
_bn["M0_tldr"]["eth_change_24h_pct"] = -2.5
_html3 = sdb.render_brief_html(_bn)
check("做多" in _html3 and "⚪ 今日无操作" not in _html3, "非横盘时不降级")

# ════════════════════════════════════════════════════════
# P0-b 空建议为合法输出（no_trade_reason 上屏）
# ════════════════════════════════════════════════════════
print("[P0-b] trade_suggestions=[] 是合法输出")
_be = _brief()
_be["M0_ai_summary"] = {
    "status": "ok", "headline": "今日观望", "bias": "中性", "market_regime": "震荡",
    "trade_suggestions": [],
    "no_trade_reason": "多处数据不可用且无满足门槛的机会，今日不动是对的",
    "data_quality": [{"section": "交易所净流量", "status": "empty", "items": 0}],
}
_html = sdb.render_brief_html(_be)
check("⚪ 今日无操作" in _html, "空建议渲染「今日无操作」卡片")
check("多处数据不可用且无满足门槛的机会，今日不动是对的" in _html, "no_trade_reason 上屏")
check("0/1" in _html, "覆盖数 0/1（全空，不虚高）")

print("[P0-b] 渲染幂等（同输入两次渲染一致）")
check(sdb.render_brief_html(_brief()) == sdb.render_brief_html(_brief()), "两次渲染字节一致")

# ════════════════════════════════════════════════════════
# P0-e 砍脏卡
# ════════════════════════════════════════════════════════
print("[P0-e] 催化剂热点（B/C 脏卡）整块移除")
_bh = _brief()
_bh["CATALYST_HOTSPOTS"] = [
    {"symbol": "XRP", "tier": "B", "catalyst_title": "作者：谷昱，ChainCatcher",
     "resonance_state": "weak", "ai_reason": "仅观察"},
    {"symbol": "BTC", "tier": "C", "catalyst_title": "Bitget said the vulnerability involved in its",
     "resonance_state": "weak", "ai_reason": "英文截断"},
]
_html = sdb.render_brief_html(_bh)
check("催化剂热点" not in _html, "卡片标题不再出现")
check("谷昱" not in _html, "爬虫署名（脏正文）不再上屏")
check("高置信度 A 级见邮件 Alert" not in _html, "自指另一封邮件的文案已删除")

print("[P0-e] Meme 卡：纯数字 → 名单")
_bm = _brief()
_bm["M8_meme"] = {"status": "ok", "summary": {"block": 1, "high": 2, "medium": 102, "low": 3},
                  "buckets": {"block": [{"symbol": "AAA", "name": "Aaa"}],
                              "high": [{"symbol": "BBB"}, {"symbol": "CCC"}]}}
_html = sdb.render_brief_html(_bm)
check("排雷 1：AAA" in _html, "排雷名单带符号")
check("高危 2：BBB、CCC" in _html, "高危名单带符号")
check("中危102" not in _html, "「中危102」纯数字不再上屏")

print("[P0-e] Meme 卡：无 block/high 名单时不出卡")
_bm2 = _brief()
_bm2["M8_meme"] = {"status": "ok", "summary": {"block": 0, "high": 0, "medium": 102, "low": 3},
                   "buckets": {"block": [], "high": [], "medium": [{"symbol": "X"}]}}
_html = sdb.render_brief_html(_bm2)
check("Meme 风险" not in _html, "只有中危计数 → 不出卡（原「高危0 · 中危102」噪声）")

print("[P0-e / W-01] KOL 卡：需「事件时间 + 金额」才出（就绪门查 event_time 而非 created_at）")
# W-01-a：event_time 全空（仅有 created_at 入库时间）→ 卡整块不出
_bk = _brief()
_bk["kol_onchain"] = {"status": "ok", "kols": ["Ai姨"], "signals": [
    {"symbol": "BTC", "signal_subtype": "smart_money", "kol_name": "Ai姨",
     "event_usd_value": 12500000, "event_direction": "inflow",
     "created_at": "2026-09-27T08:00:00"}]}
_html = sdb.render_brief_html(_bk)
check("KOL 链上信号" not in _html,
      "event_time 全空 → 卡整块不出（不再被 created_at 假门放过）")

# W-01-b：有事件时间 + 有金额 → 出卡且印事件时间（带标签）
_bk2 = _brief()
_bk2["kol_onchain"] = {"status": "ok", "kols": ["Ai姨"], "signals": [
    {"symbol": "BTC", "signal_subtype": "smart_money", "kol_name": "Ai姨",
     "event_usd_value": 12500000, "event_direction": "inflow",
     "event_time": "2026-09-27T08:00:00"}]}
_html = sdb.render_brief_html(_bk2)
check("KOL 链上信号" in _html, "有事件时间+金额 → 出卡")
check("12.5M USD" in _html, "金额上屏（可判定）")
check("事件时间 2026-09-27T08:00" in _html, "事件时间上屏且带「事件时间」标签")

# W-01-c：event_time 有、金额无 → 该条被过滤（卡不出）
_bk3 = _brief()
_bk3["kol_onchain"] = {"status": "ok", "kols": ["Ai姨"], "signals": [
    {"symbol": "BTC", "signal_subtype": "smart_money", "kol_name": "Ai姨",
     "event_time": "2026-09-27T08:00:00"}]}
_html = sdb.render_brief_html(_bk3)
check("KOL 链上信号" not in _html, "有事件时间无金额 → 该条被过滤，卡不出")

print("[W-01] 源码核验：就绪门与渲染处均用 event_time，KOL 卡代码不再出现 created_at")
_kol_src = open(os.path.join(_SCRIPTS_BIN, "send_daily_brief.py"), encoding="utf-8").read()
_kol_block = _kol_src[_kol_src.find("# KOL 链上信号（兜底）"):_kol_src.find("html_parts.append(\"</div>\")", _kol_src.find("# KOL 链上信号（兜底）"))]
_kol_code = "\n".join(l for l in _kol_block.splitlines() if not l.strip().startswith("#"))
check("event_time" in _kol_code, "KOL 卡代码含 event_time")
check("created_at" not in _kol_code, "KOL 卡代码不再出现 created_at（注释除外）")

# ════════════════════════════════════════════════════════
# P0-f 邮件未发出不得静默成功（AST 守卫，比字符串 grep 精确）
# ════════════════════════════════════════════════════════
print("[P0-f] SMTP 未配 → 非零退出（不静默成功）")
try:
    _sdb_src = open(os.path.join(_SCRIPTS_BIN, "send_daily_brief.py"), encoding="utf-8").read()
    _tree = ast.parse(_sdb_src)
    _main_fn = next(
        n for n in ast.walk(_tree)
        if isinstance(n, ast.FunctionDef) and n.name == "main"
    )
    _unconfigured_returns = []
    for _node in ast.walk(_main_fn):
        if not isinstance(_node, ast.If):
            continue
        _t = _node.test
        if (isinstance(_t, ast.UnaryOp) and isinstance(_t.op, ast.Not)
                and isinstance(_t.operand, ast.Attribute)
                and _t.operand.attr == "configured"):
            for _sub in _node.body:
                if isinstance(_sub, ast.Return) and isinstance(_sub.value, ast.Constant):
                    _unconfigured_returns.append(_sub.value.value)
    check(_unconfigured_returns == [1],
          "main() 的「not notifier.configured」分支返回 1（task_manager 记 failed 可见）",
          f"实际 return 值: {_unconfigured_returns}")
    check("SMTP 未配置，跳过邮件发送" not in _sdb_src,
          "旧「跳过邮件发送」措辞已移除（不再表现为正常跳过）")
    # 依赖断言：非零退出确实会被记为失败（task_manager 的判定式）
    _tm_src = open(os.path.join(_HERE, "task_manager.py"), encoding="utf-8").read()
    check('error=None if returncode == 0 else f"exit code {returncode}"' in _tm_src,
          "task_manager 仍以 returncode != 0 记 error（P0-f 的可见性依赖此）")
except Exception as _e:
    check(False, "P0-f AST 守卫执行", f"{type(_e).__name__}: {_e}")

# ════════════════════════════════════════════════════════
# P0-b prompt 层（假 LLM 捕获 prompt）
# ════════════════════════════════════════════════════════
print("[P0-b] prompt 层：数据门控 missing ≠ 0")
_captured = {}


class _FakeLLM:
    def __init__(self, *a, **k):
        pass

    def is_available(self):
        return True

    def chat(self, system_prompt, user_prompt, **kw):
        _captured["system"] = system_prompt
        _captured["user"] = user_prompt
        return json.dumps({
            "headline": "数据多处不可用", "market_regime": "震荡", "bias": "中性",
            "conviction": "low", "key_drivers": ["数据不可用"], "sector_rotation": "",
            "trade_suggestions": [],
            "no_trade_reason": "无满足门槛的机会，今日不动是对的",
            "risk_warnings": ["数据不可用，依据不足"], "watchlist": [],
        }, ensure_ascii=False)


try:
    import macro_market as mm  # noqa: E402
    import crypto_research.clients.llm_client as _llmmod  # noqa: E402
    _llmmod.LLMClient = _FakeLLM

    _empty_brief = {
        "M0_tldr": {"btc_change_24h_pct": 0.4, "eth_change_24h_pct": 0.2},
        "M1_cycle": {}, "M2_flow": {},
        "M2_sector_flow": {"status": "empty", "sectors": []},
        "M2_etf_flow": {"status": "empty", "assets": []},
        "M2_whale_moves": {"status": "empty", "transfers": [], "total_count": 0,
                           "total_usd": 0, "net_exchange_usd": 0},
        "M2_exchange_flow": {"status": "empty", "assets": []},
        "M2_holder_concentration": {"status": "empty", "whale_buying": [], "whale_selling": []},
        "M3_highlights": [], "M4_risks": [], "M6_catalyst": {},
        "M6_upcoming_unlocks": {"status": "empty", "unlocks": []},
    }
    _res = mm.generate_morning_brief_ai_summary(_empty_brief)
    _up = _captured.get("user", "")
    _sp = _captured.get("system", "")

    check(_res.get("status") == "ok", "假 LLM 路径返回 ok", str(_res.get("error")))
    check("【数据可用性（下结论前必读" in _up, "user_prompt 顶部有数据可用性块")
    check("- 交易所净流量: empty（无数据）" in _up, "交易所净流量标为 empty")
    check("- 大盘概况: empty（无数据）" in _up, "空段（M2_flow={}）标为 empty 而非 ok")
    check("（暂无数据：交易所净流量不可用）" in _up, "空段落渲染「暂无数据」而非空字符串")
    check("数据不可用（无有效样本）" in _up, "净流入无样本 → 「数据不可用」，不渲染 0.0M")
    check("交易所净流入：0.0M USD" not in _up, "旧「净流入：0.0M USD」已消失")
    check("数据不可用" in _up and "- 总笔数：数据不可用 笔" in _up, "总笔数 None-aware")
    check("严禁表述为" in _sp, "system prompt 含「缺失不得表述为零」硬约束")
    check("依据不足" in _sp, "system prompt 含「依据不足」硬约束")
    check("默认输出" in _sp and "no_trade_reason" in _sp, "system prompt 含「默认无操作 + no_trade_reason」")
    check("不允许" not in _sp and "禁止为凑满建议数量" in _sp, "禁止凑数方向")
    check(_res.get("no_trade_reason") == "无满足门槛的机会，今日不动是对的", "no_trade_reason 透传")
    _dq = _res.get("data_quality") or []
    check(len(_dq) == 7, f"data_quality 覆盖 7 个维度（实际 {len(_dq)}）")
    check(all(d["status"] == "empty" for d in _dq), "全空场景下无一维度被记为 ok（覆盖不虚高）",
          str(_dq))

    print("[P0-b] prompt 层：有样本时仍渲染真值")
    _ok_brief = dict(_empty_brief)
    _ok_brief["M2_whale_moves"] = {
        "status": "ok", "transfers": [{"symbol": "BTC", "value_usd": 8e6,
                                       "to_exchange": True, "from_exchange": False, "chain": "BTC"}],
        "total_count": 12, "total_usd": 96e6, "net_exchange_usd": 24e6,
        "exchange_in_count": 3, "exchange_out_count": 1,
    }
    _ok_brief["M2_exchange_flow"] = {"status": "ok",
                                     "assets": [{"symbol": "BTC", "net_flow_usd": 12e6}]}
    mm.generate_morning_brief_ai_summary(_ok_brief)
    _up2 = _captured.get("user", "")
    check("24.0M USD（样本 4 笔）" in _up2, "有样本时渲染真值与样本数")
    check("- 总笔数：12 笔" in _up2, "有样本时渲染真总笔数")
    check("- BTC: 12.0M" in _up2, "交易所净流量有数据时正常渲染")
except Exception as _e:
    check(False, "prompt 层用例执行", f"{type(_e).__name__}: {_e}")

# ════════════════════════════════════════════════════════
# W-02 风险条目与系统自身判定一致性校验
# ════════════════════════════════════════════════════════
print("[W-02] 源码核验：校验函数存在且被调用；prompt 含第 6 条约束")
try:
    import macro_market as _mm2  # noqa: E402
    _mm2_src = open(os.path.join(_HERE, "macro_market.py"), encoding="utf-8").read()
    check("def _validate_against_payload" in _mm2_src, "校验函数 _validate_against_payload 存在")
    check("_validate_against_payload(_raw_risks, brief, payload" in _mm2_src
          and "_validate_against_payload(_raw_trades, brief, payload" in _mm2_src,
          "risk_warnings / trade_suggestions 返回前均调用校验")
    check("6. 风险条目若引用系统已判定的信号" in _mm2_src, "system prompt 含第 6 条硬约束")
    check("generate_morning_brief_ai_summary(brief, today)" in _mm2_src,
          "组装层把 payload 传给 AI 摘要（供校验读系统判定）")

    _b_w02 = {"M4_risks": [{"target": "恐贪指数极度贪婪", "direction": "short",
                            "signal_type": "fng_extreme"}], "M3_highlights": []}
    _p_none = {"summary": {"emotion_subscore": {"components": {
        "fear_greed": {"score": 70.0, "value": 70.0, "extreme": "NONE", "percentile": None}}}}}
    _p_high = {"summary": {"emotion_subscore": {"components": {
        "fear_greed": {"score": 92.0, "value": 92.0, "extreme": "HIGH", "percentile": 99.0}}}}}
    _item = "恐贪指数极度贪婪，市场情绪过热可能引发短期剧烈回调"

    _buf = io.StringIO()
    with contextlib.redirect_stdout(_buf):
        _kept = _mm2._validate_against_payload([_item], _b_w02, _p_none, kind="risk")
    check(_kept == [], "extreme=NONE + 「极度贪婪」→ 该条被丢弃")
    check("丢弃与系统判定矛盾的条目" in _buf.getvalue() and "extreme=NONE" in _buf.getvalue(),
          "丢弃日志含原文与 payload 判定", _buf.getvalue()[-200:])

    _kept2 = _mm2._validate_against_payload([_item], _b_w02, _p_high, kind="risk")
    check(_kept2 == [_item], "extreme=HIGH（系统判定为极值）→ 同文案保留")

    _kept3 = _mm2._validate_against_payload(["BTC 跌破 107200 则止损离场"], _b_w02, _p_none)
    check(_kept3 == ["BTC 跌破 107200 则止损离场"], "与系统信号无关的条目不被误伤")

    _b_sol = {"M3_highlights": [{"target": "SOL", "symbol": "SOL", "direction": "long"}]}
    _kept4 = _mm2._validate_against_payload(
        [{"asset": "SOL", "direction": "做空", "reason": "逆势"}], _b_sol, None, kind="trade")
    check(_kept4 == [], "方向矛盾（payload long vs 建议做空）→ 丢弃")
    _kept5 = _mm2._validate_against_payload(
        [{"asset": "SOL", "direction": "做多", "reason": "顺势"}], _b_sol, None, kind="trade")
    check(_kept5 and _kept5[0]["direction"] == "做多", "方向一致 → 保留")
except Exception as _e:
    check(False, "W-02 用例执行", f"{type(_e).__name__}: {_e}")

# ════════════════════════════════════════════════════════
# P1-a 交易方向可执行化（六要素齐备才进「交易方向」区）
# ════════════════════════════════════════════════════════
print("[P1-a] 六要素齐备 → 结构化渲染 ≥5 行")
_html = sdb.render_brief_html(_brief())
for _need in ("💡 具体交易方向", "进场", "BTC 4h 收盘站上 110500",
              "失效", "BTC 跌破 107200", "目标", "114000-118000",
              "参照", "2026-09-27 08:30"):
    check(_need in _html, f"交易方向渲染含「{_need}」")

print("[P1-a] 缺可判定要素 → 不进交易区，落「观察」区且计数可见")
_bad = _brief()
_bad["M0_ai_summary"]["trade_suggestions"] = [
    {"asset": "SOL", "direction": "做多", "horizon": "短线(1-3日)", "reason": "仅一句理由"},
]
_buf = io.StringIO()
with contextlib.redirect_stdout(_buf):
    _html_bad = sdb.render_brief_html(_bad)
_log = _buf.getvalue()
check("💡 具体交易方向" not in _html_bad, "缺字段条不进入「具体交易方向」区")
check("👀 观察（不构成建议·缺可判定条件）" in _html_bad, "降级渲染「👀 观察」区（信息不丢弃）")
check("SOL" in _html_bad and "缺：进场条件" in _html_bad, "观察区列出标的与缺失要素")
check("交易方向拒收：1 条" in _log, "拒收计数写入渲染日志", _log[-200:])

print("[P1-a] 混合：齐备条进交易区，缺字段条进观察区")
_mix = _brief()
_mix["M0_ai_summary"]["trade_suggestions"] = [
    _brief()["M0_ai_summary"]["trade_suggestions"][0],
    {"asset": "DOGE", "direction": "做多", "horizon": "短线", "reason": "缺阈值"},
]
_html_mix = sdb.render_brief_html(_mix)
check("💡 具体交易方向" in _html_mix and "BTC" in _html_mix, "齐备条进交易区")
check("DOGE" in _html_mix and "👀 观察" in _html_mix, "缺字段条进观察区，与交易区并存")

# ════════════════════════════════════════════════════════
# P1-b 观望闸门（证据覆盖不足 → 强制「今日无操作」，只降不升）
# ════════════════════════════════════════════════════════
print("[P1-b] 闸门生效：ok 维度数 < 阈值 → 强制「今日无操作」")
_bl = _brief()
_bl["M0_ai_summary"]["data_quality"] = [
    {"section": "大盘概况", "status": "ok", "items": 1},
    {"section": "ETF资金流", "status": "empty", "items": 0},
    {"section": "即将解锁", "status": "empty", "items": 0},
]
_html_low = sdb.render_brief_html(_bl)
check("⚪ 今日无操作" in _html_low, "证据不足 → 输出「今日无操作」")
check("💡 具体交易方向" not in _html_low, "AI 方向被闸门拦截")
check("数据覆盖不足" in _html_low, "闸门原因写入正文")

print("[P1-b] 不误伤：ok 维度数达阈值 → 保留方向")
_bok = _brief()
_bok["M0_ai_summary"]["data_quality"] = [
    {"section": "大盘概况", "status": "ok", "items": 1},
    {"section": "ETF资金流", "status": "ok", "items": 1},
    {"section": "即将解锁", "status": "empty", "items": 0},
]
_html_ok = sdb.render_brief_html(_bok)
check("做多" in _html_ok and "⚪ 今日无操作" not in _html_ok, "覆盖达标时不降级")

print("[P1-b] 无 data_quality 块 → 不触发闸门（不误伤旧 payload）")
check("做多" in sdb.render_brief_html(_brief()), "无 data_quality 时保留方向")

# ════════════════════════════════════════════════════════
# M4 单一口径裁决（方案 §3.3 M4）
# ════════════════════════════════════════════════════════
print("[M4-2] 排序口径分离：先按等级分组，组内再按分数")
_b_tier = _brief()
_b_tier["M8_opportunities"] = [
    {"target": "ZZHIGH", "conviction_tier": "HIGH", "conviction_score": 50,
     "direction": "long", "trigger_logic": "高等级低分"},
]
_b_tier["M8_watchlist"] = [
    {"target": "ZZMED", "conviction_tier": "MED", "conviction_score": 90,
     "direction": "long", "trigger_logic": "低等级高分"},
]
_html_tier = sdb.render_brief_html(_b_tier)
check(_html_tier.find("ZZHIGH") != -1 and _html_tier.find("ZZHIGH") < _html_tier.find("ZZMED"),
      "HIGH(50) 排在 MED(90) 之前（不跨口径按数值直排）")
check("按可验证性（是否被回测）分组" in _html_tier, "机会卡标题披露排序口径（W-03 后）")

print("[M4-1] 同一 target 只保留一条结论，其余折叠为「关联」")
_b_fold = _brief()
_b_fold["M3_highlights"] = [{
    "target": "SOL", "symbol": "SOL", "direction": "long", "conviction_score": 67,
    "ai_analysis_v2": {"overall_score": 67, "confidence": "MED", "reason_summary": "高亮结论"},
}]
_b_fold["M8_watchlist"] = [{
    "target": "Solana 链", "symbol": "SOL", "conviction_tier": "MED",
    "conviction_score": 76, "direction": "long", "trigger_logic": "精选机会的第二份结论",
}]
_html_fold = sdb.render_brief_html(_b_fold)
check("精选机会的第二份结论" not in _html_fold, "重复标的的第二次结论不再上屏")
check("关联折叠" in _html_fold and "Solana 链 → 见「AI 精选高亮」" in _html_fold,
      "折叠为「关联」并注明去向")
check("76分" not in _html_fold, "重复分数 76 被折叠（不并排展示）")

print("[M4-1/M4-3] 同一赛道不出现两个涨幅数字")
_b_sec = _brief()
_b_sec["M2_sector_flow"] = {"metric_date": "2026-09-26", "sectors": [
    {"sector_key": "ai", "sector_label": "AI & Big Data",
     "mcap_change_7d_pct": 18.3, "composite_score": 90, "leaders": []},
]}
_b_sec["M8_opportunities"] = [{
    "target": "AI & Big Data", "conviction_tier": "HIGH", "conviction_score": 76,
    "direction": "long", "signal_type": "narrative",
    "trigger_logic": "AI & Big Data 7d 市值 +17.1% → 资金净流入",
}]
_html_sec = sdb.render_brief_html(_b_sec)
check("+18.3%" in _html_sec, "赛道轮动卡显示赛道 SSOT 涨幅")
check("+17.1%" not in _html_sec, "同一赛道不再出现第二个涨幅数字")

print("[M4-4] 冲突必须裁决（领涨币同时入高危）")
_b_arb = _brief()
_b_arb["M2_sector_flow"] = {"metric_date": "2026-09-26", "sectors": [
    {"sector_key": "infra", "sector_label": "Infrastructure",
     "mcap_change_7d_pct": 10.1, "composite_score": 80,
     "leaders": [{"symbol": "2Z", "name": "DoubleZero"}]},
]}
_b_arb["M4_risks"] = [{"target": "2Z", "ai_analysis_v2": {"overall_score": 88, "confidence": "HIGH"}}]
_html_arb = sdb.render_brief_html(_b_arb)
check("⚖️ 单一口径裁决" in _html_arb, "输出裁决区块")
check("2Z 领涨Infrastructure属资金驱动" in _html_arb and "判定：不参与" in _html_arb,
      "裁决语含归属与判定（不允许两条并列无解释）")

print("[M4-4] 不误伤：无冲突时不输出裁决区块")
check("⚖️ 单一口径裁决" not in sdb.render_brief_html(_brief()), "无冲突 → 不出现裁决区块")

print("[M4-3] 数据层：叙事机会市值涨幅统一到赛道 SSOT（幂等）")
try:
    import macro_market as _mm  # noqa: E402
    _ssot = _mm._sector_ssot_map({"sectors": [
        {"sector_key": "ai", "sector_label": "AI & Big Data", "mcap_change_7d_pct": 18.3},
    ]})
    check(_ssot.get("aiandbigdata") == 18.3, "SSOT 表按归一化标签索引")
    _opps = [{"target": "AI & Big Data", "signal_type": "narrative",
              "trigger_logic": "AI & Big Data 7d 市值 +17.1% → 资金净流入",
              "key_metric": "市值 +17.1%"}]
    _n = _mm._unify_sector_metric(_opps, _ssot)
    check(_n == 1 and "+18.3%" in _opps[0]["trigger_logic"] and "+17.1%" not in _opps[0]["trigger_logic"],
          "叙事机会的市值涨幅改写为赛道口径")
    check(_opps[0].get("mcap_change_7d_pct") == 18.3 and _opps[0].get("mcap_ssot") is True,
          "回填 mcap_change_7d_pct + 标记来源")
    _mm._unify_sector_metric(_opps, _ssot)
    check(_opps[0]["trigger_logic"].count("18.3") == 1, "幂等：重复执行不叠加")
except Exception as _e:
    check(False, "M4-3 数据层用例执行", f"{type(_e).__name__}: {_e}")

# ════════════════════════════════════════════════════════
# W-03 gate 分层排序（M4-2 的下一层：可验证性优先）
# ════════════════════════════════════════════════════════
print("[W-03] 源码核验：_GATE_RANK 存在且 _tier_score_key 返回 3 元组")
check(hasattr(sdb, "_GATE_RANK"), "_GATE_RANK 已定义")
_gr = getattr(sdb, "_GATE_RANK", {})
check(_gr.get("calibrated_ok", 0) == 4 and _gr.get("calibrated_low", 0) == 3
      and _gr.get("preliminary", 0) == 2 and _gr.get("exempt_not_calibrable", 0) == 1
      and _gr.get("exempt_not_backtestable", 0) == 0,
      "gate 权级与工单定死值一致（ok4>low3>prelim2>not_calibrable1>not_backtestable0）")
_k = sdb._tier_score_key({"conviction_tier": "MED", "conviction_score": 55,
                          "calibration_status": {"gate": "calibrated_low"}})
check(isinstance(_k, tuple) and len(_k) == 3, f"_tier_score_key 返回 3 元组（实得 {_k}）")

print("[W-03] 注入：未回测(99) 与 已回测(50) → 已回测必须排前")
check(sdb._tier_score_key({"conviction_tier": "MED", "conviction_score": 50,
                           "calibration_status": {"gate": "calibrated_ok"}})
      > sdb._tier_score_key({"conviction_tier": "MED", "conviction_score": 99,
                             "calibration_status": {"gate": "exempt_not_backtestable"}}),
      "score=50/calibrated_ok 的键 > score=99/exempt_not_backtestable 的键")

print("[W-03] 注入：gate 缺失 → 按 0 处理，排在 calibrated_low 之后")
check(sdb._tier_score_key({"conviction_tier": "MED", "conviction_score": 99})  # 无 calibration_status
      < sdb._tier_score_key({"conviction_tier": "MED", "conviction_score": 50,
                             "calibration_status": {"gate": "calibrated_low"}}),
      "gate 缺失(0) < calibrated_low(3)：即使分数更高也排后")

print("[W-03] 渲染端：未回测沉到已回测之后 + 徽章对称")
_bw = _brief()
_bw.pop("M3_highlights", None)
_bw.pop("M4_risks", None)
_bw["M8_watchlist"] = [
    {"target": "XXXEXEMPT", "conviction_tier": "MED", "conviction_score": 99,
     "direction": "short", "trigger_logic": "从未回测",
     "calibration_status": {"gate": "exempt_not_backtestable"}},
    {"target": "YYYCALOK", "conviction_tier": "HIGH", "conviction_score": 50,
     "direction": "long", "trigger_logic": "有回测背书",
     "calibration_status": {"gate": "calibrated_ok"}},
]
_html_w = sdb.render_brief_html(_bw)
check(_html_w.find("YYYCALOK") != -1 and _html_w.find("YYYCALOK") < _html_w.find("XXXEXEMPT"),
      "渲染顺序：calibrated_ok(50) 排在 exempt_not_backtestable(99) 之前")
check("未回测" in _html_w, "exempt_* 卡打「未回测」徽章")
check("回测背书" in _html_w, "calibrated_ok 卡打「回测背书」徽章（对称）")

print("[W-03] 不误伤：calibrated_low(50) 排在同 gate 无字段(99) 之前")
_bn_w = _brief()
_bn_w.pop("M3_highlights", None)
_bn_w.pop("M4_risks", None)
_bn_w["M8_watchlist"] = [
    {"target": "NOCALFIELD", "conviction_tier": "MED", "conviction_score": 99,
     "direction": "long", "trigger_logic": "无校准字段"},
    {"target": "LOWCALX", "conviction_tier": "MED", "conviction_score": 50,
     "direction": "long", "trigger_logic": "已回测低命中",
     "calibration_status": {"gate": "calibrated_low"}},
]
_html_wn = sdb.render_brief_html(_bn_w)
check(_html_wn.find("LOWCALX") != -1 and _html_wn.find("LOWCALX") < _html_wn.find("NOCALFIELD"),
      "calibrated_low(50) 排在无校准字段(99) 之前")

# ════════════════════════════════════════════════════════
# W-04 MVRV over 方向语义：止盈 ≠ 看空
# ════════════════════════════════════════════════════════
print("[W-04] 源码核验：mvrv_deep_over 不再为 short，且不列入风险类型")
_mm_src = open(os.path.join(os.path.dirname(_HERE), "workbench", "macro_market.py"),
               encoding="utf-8").read()
_ds = _mm_src.find('"target": f"{len(deep_over)} 币 MVRV 极度高估"')
_de = _mm_src.find("over_watch = [", _ds)
_block = _mm_src[_ds:_de] if _ds != -1 and _de != -1 else ""
check('"direction": "watch"' in _block, "mvrv_deep_over 的 direction 已改为 watch")
check('"direction": "short"' not in _block, "mvrv_deep_over 生成块内不再出现 short")
check('"action_hint": "不追高 · 中线止盈"' in _block, "action_hint 承载「不追高 · 中线止盈」")
_rt = _mm_src.find("risk_types = {")
_rt_end = _mm_src.find("}", _rt)
check("mvrv_deep_over" not in _mm_src[_rt:_rt_end],
      "risk_types 不再含 mvrv_deep_over（该条不进 risk_signals）")

print("[W-04] 注入：direction=short 的 mvrv_deep_over 不再渲染为看空")
_bo = _brief()
_bo.pop("M3_highlights", None)
_bo.pop("M4_risks", None)
_bo["M8_watchlist"] = [{
    "target": "MVRVTEST", "conviction_tier": "MED", "conviction_score": 91,
    "direction": "short", "signal_type": "mvrv_deep_over",
    "trigger_logic": "doge, ada 等 2 个代币 MVRV 百分位 ≥85%",
    "action_hint": "不追高 · 中线止盈",
}]
_ho = sdb.render_brief_html(_bo)
check("止盈提示" in _ho, "mvrv_deep_over(short) → 渲染「◆ 止盈提示」")
check("▼ 看空" not in _ho, "不再渲染为「▼ 看空」（对只做现货读者不可执行）")
check("不追高 · 中线止盈" in _ho, "文案取 action_hint")

print("[W-04] 注入：direction=watch + action_hint → 文案为 action_hint")
_bo2 = _brief()
_bo2.pop("M3_highlights", None)
_bo2.pop("M4_risks", None)
_bo2["M8_watchlist"] = [{
    "target": "WATCHTEST", "conviction_tier": "MED", "conviction_score": 55,
    "direction": "watch", "signal_type": "mvrv_deep_over",
    "trigger_logic": "MVRV 百分位 90%", "action_hint": "不追高 · 中线止盈",
}]
_ho2 = sdb.render_brief_html(_bo2)
check("止盈提示" in _ho2 and "不追高 · 中线止盈" in _ho2, "watch 卡渲染止盈提示 + action_hint 文案")

print("[W-04] 持仓提示区：被 top-N 截断的 watch/止盈卡仍上屏")
_bh = _brief()
_bh.pop("M3_highlights", None)
_bh.pop("M4_risks", None)
_bh["M8_watchlist"] = [
    {"target": f"FILLER{i}", "conviction_tier": "LOW", "conviction_score": 40,
     "direction": "long", "trigger_logic": "填充", "calibration_status": {"gate": "calibrated_low"}}
    for i in range(7)
] + [
    {"target": "MVRVBURIED", "conviction_tier": "MED", "conviction_score": 91,
     "direction": "watch", "signal_type": "mvrv_deep_over", "trigger_logic": "MVRV 百分位 90%",
     "action_hint": "不追高 · 中线止盈", "calibration_status": {"gate": "exempt_not_backtestable"}},
]
_html_hh = sdb.render_brief_html(_bh)
check("◆ 持仓提示" in _html_hh, "出现「◆ 持仓提示」区")
check("MVRVBURIED" in _html_hh and "不追高 · 中线止盈" in _html_hh,
      "被 top-N 截断的止盈卡仍在持仓提示区上屏（不被埋没）")

print("[W-04] 数据层：mvrv_deep_over 不再进入 risk_signals")
try:
    import macro_market as _mmw  # noqa: E402
    _ro = [
        {"target": "2 币 MVRV 极度高估", "direction": "watch", "signal_type": "mvrv_deep_over",
         "conviction_score": 91, "conviction_tier": "MED", "related_dims": ["mvrv_universe"]},
        {"target": "SOMECOIN", "direction": "short", "signal_type": "whale_flow",
         "conviction_score": 80, "conviction_tier": "HIGH", "related_dims": ["a", "b"]},
    ]
    _sel = _mmw.select_risk_signals(_ro, max_total=8)
    _sel_types = [s.get("signal_type") for s in _sel]
    check("mvrv_deep_over" not in _sel_types, "select_risk_signals 不再输出 mvrv_deep_over")
    check("whale_flow" in _sel_types, "真风险信号（whale_flow）仍被选出（不误伤）")
except Exception as _e:
    check(False, "W-04 数据层用例执行", f"{type(_e).__name__}: {_e}")

# ════════════════════════════════════════════════════════
# W-05 数据新鲜度阈值按源配置（days > 2 → days >= max_lag_days）
# ════════════════════════════════════════════════════════
print("[W-05] 纯函数判定：>= max_lag 判滞后（日更阈值 1；None 视为滞后）")
try:
    from datetime import date as _date, timedelta as _td
    import build_daily_brief as _bdb  # noqa: E402
    _today = _date(2026, 9, 27)
    check(_bdb._freshness_verdict(_today, 1, _today)[0] is False, "今日数据（0 天）→ 不告警")
    check(_bdb._freshness_verdict(_today - _td(days=1), 1, _today)[0] is True,
          "昨日数据（1 天 ≥ 1）→ 告警（旧实现 1>2 为 False 会静默放过）")
    check(_bdb._freshness_verdict(None, 1, _today)[0] is True, "无数据（None）→ 告警")
    check(_bdb._freshness_verdict(_today - _td(days=3), 4, _today)[0] is False,
          "ETF 阈值 4：滞后 3 天（跨周末）→ 不告警")
    check(_bdb._freshness_verdict(_today - _td(days=4), 4, _today)[0] is True,
          "ETF 阈值 4：滞后 4 天 → 告警")
except Exception as _e:
    check(False, "W-05 纯函数用例执行", f"{type(_e).__name__}: {_e}")

print("[W-05] 源码核验：checks 为 4 元组且判定用 >=")
_bdb_src = open(os.path.join(_SCRIPTS_BIN, "build_daily_brief.py"), encoding="utf-8").read()
check('"snapshot_date", 1)' in _bdb_src and '"flow_date",     4)' in _bdb_src,
      "checks 列表已扩为 (名称, 表, 时间列, max_lag_days)")
check("days >= max_lag_days" in _bdb_src, "判定改为 days >= max_lag_days")
check("if days > 2" not in _bdb_src, "旧的 days > 2 判定已移除")

# ════════════════════════════════════════════════════════
# W-06 落库早报完整 brief（daily_brief_snapshot 表 + upsert）
# ════════════════════════════════════════════════════════
print("[W-06] 源码核验：DDL / upsert / app_commit / 顶层 data_quality 提升")
_w06_ddl = os.path.join(os.path.dirname(_HERE), "scripts", "migrations",
                        "fix_074_daily_brief_snapshot.sql")
check(os.path.isfile(_w06_ddl), "迁移文件 fix_074_daily_brief_snapshot.sql 存在")
if os.path.isfile(_w06_ddl):
    _ddl_src = open(_w06_ddl, encoding="utf-8").read()
    check("CREATE TABLE IF NOT EXISTS biz.daily_brief_snapshot" in _ddl_src,
          "DDL 创建 biz.daily_brief_snapshot")
    check("brief_date  DATE        PRIMARY KEY" in _ddl_src, "brief_date 为主键（upsert 依据）")
    check("app_commit" in _ddl_src, "DDL 含 app_commit 列")

check("ON CONFLICT (brief_date) DO UPDATE" in _bdb_src, "落库使用 ON CONFLICT DO UPDATE（幂等 upsert）")
check("def _save_brief_snapshot(" in _bdb_src, "_save_brief_snapshot 函数存在")
check("def _read_app_commit(" in _bdb_src, "_read_app_commit 函数存在")
check('"APP_COMMIT", "GIT_COMMIT", "GIT_SHA"' in _bdb_src, "app_commit 支持 env GIT_COMMIT 等键")
check("/app/.git_head" in _bdb_src, "app_commit 回退读 /app/.git_head")
check('if "data_quality" not in payload_obj' in _bdb_src
      and '(brief.get("M0_ai_summary") or {}).get("data_quality")' in _bdb_src,
      "顶层缺 data_quality 时从 M0_ai_summary 提升（满足 payload ? 'data_quality'）")
_i_save_snap = _bdb_src.find("save_snapshot(date.today().isoformat(), today)")
_i_brief_snap = _bdb_src.find("_save_brief_snapshot(date.today().isoformat(), brief)")
check(_i_save_snap != -1 and _i_brief_snap != -1 and _i_save_snap < _i_brief_snap,
      "main() 中 brief 落库在 save_snapshot 之后")

print("[W-06] 纯函数：_read_app_commit 读 env（env 优先，读不到 None）")
_old_env = {k: os.environ.pop(k, None) for k in ("APP_COMMIT", "GIT_COMMIT", "GIT_SHA")}
try:
    os.environ["GIT_COMMIT"] = "abc123"
    check(_bdb._read_app_commit() == "abc123", "env GIT_COMMIT 被读取")
    os.environ["APP_COMMIT"] = "top999"
    check(_bdb._read_app_commit() == "top999", "APP_COMMIT 优先级高于 GIT_COMMIT")
    del os.environ["APP_COMMIT"], os.environ["GIT_COMMIT"]
    _no_env = _bdb._read_app_commit()
    check(_no_env is None or isinstance(_no_env, str), "无 env 时回退文件或返回 None（不抛异常）")
finally:
    for k, v in _old_env.items():
        if v is not None:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)

# ③ 注入测试（需 DATABASE_URL 才跑，保持离线默认全绿）
print("[W-06] 注入测试：同日写两次 → count(*)=1（需 DATABASE_URL）")
if os.environ.get("DATABASE_URL"):
    _SENT = "1900-01-01"
    try:
        import psycopg  # noqa: E402
        _b1 = {"M0_tldr": {"date": _SENT, "mark": "first"},
               "M0_ai_summary": {"data_quality": [{"section": "market", "status": "ok"}]}}
        _b2 = {"M0_tldr": {"date": _SENT, "mark": "second"},
               "M0_ai_summary": {"data_quality": [{"section": "market", "status": "ok"}]}}
        os.environ["GIT_COMMIT"] = "probecommit"
        _bdb._save_brief_snapshot(_SENT, _b1)
        _bdb._save_brief_snapshot(_SENT, _b2)
        _c = psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=30)
        try:
            with _c.cursor() as _cur:
                _cur.execute("SELECT count(*), max(jsonb_typeof(payload)), "
                             "bool_and(payload ? 'M0_ai_summary'), "
                             "bool_and(payload ? 'data_quality'), "
                             "max(payload->'M0_tldr'->>'mark'), max(app_commit) "
                             "FROM biz.daily_brief_snapshot WHERE brief_date=%s", (_SENT,))
                _r = _cur.fetchone()
            check(_r[0] == 1, "同日两次写入 → count(*)=1（upsert 不产生重复行）", f"实际 count={_r[0]}")
            check(_r[1] == "object", "payload 为 jsonb 对象")
            check(_r[2] is True, "payload 含 M0_ai_summary")
            check(_r[3] is True, "payload 含顶层 data_quality")
            check(_r[4] == "second", "第二次写入生效（DO UPDATE 覆盖）")
            check(_r[5] == "probecommit", "app_commit 落库正确")
            _cur2 = _c.cursor()
            _cur2.execute("DELETE FROM biz.daily_brief_snapshot WHERE brief_date=%s", (_SENT,))
            _c.commit()
            _cur2.close()
        finally:
            _c.close()
    except Exception as _e:
        check(False, "W-06 注入测试执行", f"{type(_e).__name__}: {_e}")
    finally:
        os.environ.pop("GIT_COMMIT", None)
else:
    print("  - 跳过（未设置 DATABASE_URL，注入测试为可选联网项）")

# ════════════════════════════════════════════════════════
# W-07 数据状态表置顶（字段级 usable/total；ok 必须 usable>0）
# ════════════════════════════════════════════════════════
print("[W-07] 源码核验：字段级规格 / 归一函数 / 置顶状态表 / 路由校验")
check("_DQ_FIELD_SPEC" in _mm_src, "macro_market 含字段级规格 _DQ_FIELD_SPEC")
check("def _dq_normalize(" in _mm_src, "macro_market 含 _dq_normalize（ok 必须 usable>0）")
check("def _build_data_quality(" in _mm_src, "macro_market 含 _build_data_quality")
check("\"usable\"" in _mm_src and "\"total\"" in _mm_src and "\"coverage\"" in _mm_src,
      "data_quality 条目含 usable/total/coverage")
try:
    _sdb_src = open(os.path.join(_SCRIPTS_BIN, "send_daily_brief.py"), encoding="utf-8").read()
    check("def _data_status_table_html(" in _sdb_src, "渲染层含置顶数据状态表 _data_status_table_html")
    check("def _dq_usable(" in _sdb_src, "渲染层含路由校验 _dq_usable")
    check("模块 0.1：📋 数据状态" in _sdb_src, "状态表置于 M0 之后（模块 0.1）")
    check("数据不可用" in _sdb_src, "渲染层含「数据不可用」文案")
except Exception as _e:
    check(False, "W-07 渲染层源码核验", f"{type(_e).__name__}: {_e}")

print("[W-07] 字段级判定：usable=0 的 ok 必须降为 empty")
try:
    import macro_market as _mmw7  # noqa: E402
    _n0 = _mmw7._dq_normalize({"section": "交易所净流量", "status": "ok", "usable": 0, "total": 16})
    check(_n0["status"] == "empty", "usable=0,total=16 的 ok → 降为 empty")
    check(_n0["coverage"] == 0.0, "usable=0 → coverage=0.0")
    _n1 = _mmw7._dq_normalize({"section": "交易所净流量", "status": "ok", "usable": 2, "total": 16})
    check(_n1["status"] == "ok", "usable=2 → 仍为 ok")
    check(abs(_n1["coverage"] - 0.125) < 1e-9, "usable=2,total=16 → coverage=0.125")
    _dq7 = _mmw7._build_data_quality({
        "交易所净流量": {"status": "ok", "latest_date": "2026-09-25",
                      "assets": [{"symbol": "BTC", "net_flow_usd": 1.0},
                                 {"symbol": "ETH", "net_flow_usd": None}]},
        "大盘概况": {"status": "ok"},
    }, today=__import__("datetime").date(2026, 9, 27))
    _ex7 = next(d for d in _dq7 if d["section"] == "交易所净流量")
    check(_ex7["usable"] == 1 and _ex7["total"] == 2, "字段级 usable/total 正确（1/2）")
    check(_ex7["as_of"] == "2026-09-25" and _ex7["lag_days"] == 2, "as_of / lag_days 正确（09-25 → 滞后 2 天）")
except Exception as _e:
    check(False, "W-07 字段级用例执行", f"{type(_e).__name__}: {_e}")

print("[W-07] 渲染层：注入 usable=0,total=16 → 状态列「数据不可用」，不渲染 0")
try:
    _dq_inj = [
        {"section": "交易所净流量", "status": "empty", "as_of": "2026-09-25",
         "lag_days": 2, "coverage": 0.0, "usable": 0, "total": 16},
        {"section": "大盘概况", "status": "ok", "as_of": None, "lag_days": None,
         "coverage": 1.0, "usable": 1, "total": 1},
    ]
    _tbl = sdb._data_status_table_html(_dq_inj)
    check("📋 数据状态" in _tbl, "状态表区块渲染存在")
    check("数据不可用" in _tbl, "empty 维度渲染「数据不可用」")
    check("0/16" not in _tbl, "empty 维度不得渲染 0/16（不显示 0）")
    check(">1/1<" in _tbl.replace(" ", ""), "ok 维度渲染可用率 1/1")
    # 路由校验：不可用维度 → _dq_usable False
    _br_inj = {"M0_ai_summary": {"data_quality": _dq_inj}}
    check(sdb._dq_usable(_br_inj, "交易所净流量") is False, "_dq_usable 对 empty 维度返回 False")
    check(sdb._dq_usable(_br_inj, "大盘概况") is True, "_dq_usable 对 ok 维度返回 True")
    check(sdb._dq_usable({}, "任意") is True, "无 data_quality 时按可用（不误伤旧 payload）")
    # 置顶位置：数据状态出现在第一张数据卡之前
    _html7 = sdb.render_brief_html({**_brief(), "M0_ai_summary": {
        **(_brief()["M0_ai_summary"]), "data_quality": _dq_inj}})
    _i_status = _html7.find("📋 数据状态")
    _i_card = _html7.find("📉 告警质量")
    check(_i_status != -1 and (_i_card == -1 or _i_status < _i_card),
          "数据状态表出现在 M0 定调之后、第一张数据卡之前")
except Exception as _e:
    check(False, "W-07 渲染层用例执行", f"{type(_e).__name__}: {_e}")

# ════════════════════════════════════════════════════════
print(f"\n{'=' * 46}\n通过 {passed} / 失败 {failed}\n{'=' * 46}")
sys.exit(1 if failed else 0)