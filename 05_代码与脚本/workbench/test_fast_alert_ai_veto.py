"""A 级快讯的 AI 否决闸门 —— 离线护栏测试。

来源：`审计_催化剂A级邮件_XRP_BCH_2026-09-24.md`（P0-D1 / P0-D2）
现象：综合分 86 的信号拿了 A 级推送位并附「📈 做多 · 入场/目标/止损」档位，
      而同一封邮件里 AI 深度评审写着「资产匹配 low · 不建议参与 · 0% 仓位」——
      自身前后矛盾，扫一眼档位的人会得到与警告相反的动作。

覆盖：
  1. 判据纯函数：只在审计列出的两条上否决；评审缺失/类型异常一律**不否决**（只做减法，
     不因 AI 异常扩大拦截面）
  2. P0-D1 渲染护栏：被否决时邮件不出现任何交易档位字样，改为「已抑制」说明
  3. P0-D1 反向：未被否决时档位照常渲染（不得误伤正常 A 级邮件）
  4. P0-D2 顺序护栏：否决判定必须发生在「拿到发送锁之后」——
     否则会把已有 sent 记录改写成 suppressed，污染历史留痕
  5. P0-D2 可观测：抑制必须落 status='suppressed' 记录并带原因
     （原先的「静默跳过」正是本次审计的投诉点）
  6. 返回契约：suppressed 计数并入返回值，供上层日志披露

运行: python test_fast_alert_ai_veto.py
"""
import os
import re
import sys

_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

from catalyst import notifier as N  # noqa: E402

passed = 0
failed = 0


def check(cond, name, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name}")
        if detail:
            print(f"    {detail}")


_SRC = open(N.__file__, encoding="utf-8").read()

# 发送函数体（从 def 到下一个顶层 def）
_m = re.search(r"def send_fast_alerts_for_new_signals\(.*?\n(.*?)\ndef ", _SRC, re.S)
_SEND_SRC = _m.group(1) if _m else ""


# ════════════════════════════════════════════════════════════
# 1. 判据纯函数
# ════════════════════════════════════════════════════════════
print("\n【测试1】_ai_review_blocks_alert：只做减法")

blocks = N._ai_review_blocks_alert

# 1a. 缺失 / 类型异常 → 不否决
check(blocks(None) == (False, ""), "评审为 None → 不否决")
check(blocks({}) == (False, ""), "评审为空 dict → 不否决")
check(blocks("不建议参与") == (False, ""), "评审为 str（类型异常）→ 不否决")
check(blocks([1, 2]) == (False, ""), "评审为 list（类型异常）→ 不否决")
check(blocks({"asset_match_confidence": None, "verdict": None}) == (False, ""),
      "两字段均为 None → 不否决")

# 1b. 判据一：asset_match_confidence == 'low'
ok, why = blocks({"asset_match_confidence": "low"})
check(ok and "资产匹配" in why, "asset_match_confidence='low' → 否决", f"got={ok},{why}")
check(blocks({"asset_match_confidence": "LOW"})[0], "大写 'LOW' → 同样否决")
check(blocks({"asset_match_confidence": " low "})[0], "带空白 ' low ' → 同样否决")
check(not blocks({"asset_match_confidence": "high"})[0], "'high' → 不否决")
check(not blocks({"asset_match_confidence": "medium"})[0], "'medium' → 不否决")

# 1c. 判据二：verdict 含「不建议」（与渲染层配色判据同一约定）
ok, why = blocks({"verdict": "不建议参与"})
check(ok and "不建议参与" in why, "verdict='不建议参与' → 否决", f"got={ok},{why}")
check(blocks({"verdict": "不建议参与 · 0% 仓位（空仓观望）"})[0],
      "审计原例「不建议参与 · 0% 仓位」→ 否决")
check(not blocks({"verdict": "建议参与"})[0], "'建议参与' → 不否决（不得被子串误伤）")
check(not blocks({"verdict": "谨慎参与"})[0], "'谨慎参与' → 不否决")
check(not blocks({"verdict": "强烈建议参与"})[0], "'强烈建议参与' → 不否决")

# 1d. 两条判据独立成立即可否决
check(blocks({"asset_match_confidence": "high", "verdict": "不建议参与"})[0],
      "匹配度 high 但结论不建议 → 仍否决（判据二独立成立）")
check(blocks({"asset_match_confidence": "low", "verdict": "建议参与"})[0],
      "结论建议但匹配度 low → 仍否决（判据一独立成立）")


# ════════════════════════════════════════════════════════════
# 2/3. P0-D1 渲染护栏
# ════════════════════════════════════════════════════════════
print("\n【测试2】P0-D1：被否决时邮件不得出现交易档位")

# 审计原例：XRP，综合分 86，档位齐全，但 AI 判「资产匹配 low + 不建议参与」
_ROW = {
    "signal_id": 650174, "asset_id": 1, "tier": "A", "composite_score": 86,
    "kind": "structural", "investment_cycle": "中期",
    "canonical_name": "XRP", "symbol": "XRP", "asset_type": "coin",
    "primary_sector": "l1", "categories": ["Smart Contract Platform Layer 1"],
    "market_cap": 90.91e9, "market_cap_rank": 5,
    "circulating_supply": 99.99e9, "total_supply": 99.99e9,
    "ath_usd": 3.65, "launch_date": None,
    "description_short": "The last known price of XRP is 1.08727528 USD.",
    "catalyst_title": "Bitcoin Cash jumped 28% after CME said it will list BCH and UNI futures",
    "title_cn": "芝商所将推出 BCH 与 Uniswap 期货",
    "catalyst_summary": "据市场消息，CME 计划于 10 月 19 日推出 BCH 与 UNI 期货。",
    "ai_summary": "芝商所计划上线 BCH 与 UNI 期货。",
    "source_code": "kol_catalyst_binance_square_7",
    "entry_price": 1.58, "stop_loss": 1.41, "take_profit": 2.01, "rr_ratio": 2.5,
    "ai_reason": "结构性催化", "technical_state": "up", "resonance_state": "weak",
    "invalidation": "跌破 1.41 失效", "persistence": "structural",
    "risk_labels": [], "liquidity_score": 2.02e6,
    "confidence": 0.86, "regime": "neutral",
    "current_price": 1.57, "change_24h_pct": 2.30, "change_7d_pct": 21.49,
    "volume_24h_usd": 7.80e9, "base_strength": 79, "resonance_score": 90,
}

_VETOED_AI = {
    "verdict": "不建议参与",
    "confidence_level": "低",
    "position_suggestion": "0%",
    "asset_match_confidence": "low",
    "core_logic": "24h 量比 1.71x 属温和放量而非新增资金驱动",
    "key_risks": ["催化剂兑现后", "归因错误"],
    "overall_review": "该代币与催化剂描述的项目可能不一致。",
}

html_vetoed = N._build_fast_alert_html(dict(_ROW, ai_deep_review=_VETOED_AI))
check("交易计划：已抑制" in html_vetoed, "出现「交易计划：已抑制」说明")
check("不予展示" in html_vetoed, "说明档位为何不予展示")
check("资产匹配度 low" in html_vetoed, "说明中带上否决原因")
for token in ("入场价", "目标价", "止损价", "盈亏比", "📈 做多", "📉 做空"):
    check(token not in html_vetoed, f"被否决时不含「{token}」")
check("$2.01" not in html_vetoed and "$1.41" not in html_vetoed,
      "被否决时目标/止损价数字不得出现")

# AI 判「不建议参与」但匹配度非 low → 同样抑制
html_vetoed2 = N._build_fast_alert_html(dict(
    _ROW, ai_deep_review=dict(_VETOED_AI, asset_match_confidence="high")))
check("交易计划：已抑制" in html_vetoed2, "仅「不建议参与」也抑制档位")

print("\n【测试3】P0-D1 反向：未否决时档位照常渲染")

html_ok = N._build_fast_alert_html(dict(_ROW, ai_deep_review=dict(
    _VETOED_AI, verdict="建议参与", asset_match_confidence="high")))
check("交易计划：已抑制" not in html_ok, "未被否决 → 不出现抑制说明")
check("📊 交易计划" in html_ok, "未被否决 → 照常渲染交易计划区块")
check("入场价" in html_ok and "目标价" in html_ok and "止损价" in html_ok,
      "未被否决 → 三档照常展示")

# 无 AI 评审（快通道首轮尚未评审）→ 保持既有行为，不抑制
html_noai = N._build_fast_alert_html(dict(_ROW, ai_deep_review=None))
check("交易计划：已抑制" not in html_noai, "无 AI 评审 → 不抑制（保持既有行为）")
check("📊 交易计划" in html_noai, "无 AI 评审 → 档位照常渲染")

# 被否决但本就无档位 → 不凭空造出抑制块
html_nolevels = N._build_fast_alert_html(dict(
    _ROW, entry_price=None, stop_loss=None, take_profit=None,
    ai_deep_review=_VETOED_AI))
check("交易计划：已抑制" not in html_nolevels, "无档位可抑制时不渲染抑制块")


# ════════════════════════════════════════════════════════════
# 4/5/6. P0-D2 发送侧护栏（源码级不变量）
# ════════════════════════════════════════════════════════════
print("\n【测试4】P0-D2：否决判定必须在「拿到发送锁之后」")

check(bool(_SEND_SRC), "成功抽出发送函数体")
_i_lock = _SEND_SRC.find("_try_acquire_send_lock(")
_i_veto = _SEND_SRC.find("_ai_review_blocks_alert(")
check(_i_lock >= 0 and _i_veto >= 0, "两个调用点都在函数体内")
check(0 <= _i_lock < _i_veto,
      "加锁调用先于否决判定（已有 sent 记录走 skipped，不被改写）",
      f"lock@{_i_lock} veto@{_i_veto}")

print("\n【测试5】P0-D2：抑制必须可观测（不得静默跳过）")

check('status="suppressed"' in _SEND_SRC, "抑制落 status='suppressed' 记录")
check('error_msg=f"AI 否决：{why}"' in _SEND_SRC, "记录带否决原因")
check("_mark_sent(" in _SEND_SRC.split("_ai_review_blocks_alert(")[1][:400],
      "抑制分支内调用 _mark_sent 留痕")
check("logger.info(" in _SEND_SRC.split("_ai_review_blocks_alert(")[1][:400],
      "抑制分支内打日志")
# 抑制分支必须 continue，不得继续走到发信
_tail = _SEND_SRC.split("_ai_review_blocks_alert(")[1][:600]
check("continue" in _tail, "抑制分支以 continue 结束（不进入 _send_email）")
_i_veto_call = _SEND_SRC.find("_ai_review_blocks_alert(")
_i_next_continue = _SEND_SRC.find("continue", _i_veto_call)
_i_next_send = _SEND_SRC.find("_send_email(", _i_veto_call)
check(_i_next_continue >= 0 and (_i_next_send == -1 or _i_next_continue < _i_next_send),
      "continue 早于下一个 _send_email（结构上不可能发出）",
      f"continue@{_i_next_continue} send@{_i_next_send}")

print("\n【测试6】返回契约：suppressed 计数外露")

check('"suppressed": suppressed,' in _SEND_SRC, "返回值含 suppressed")
check('"suppressed": 0,' in _SEND_SRC, "提前返回分支同样含 suppressed（键集合一致）")
check("suppressed += 1" in _SEND_SRC, "抑制分支有计数累加")


# ════════════════════════════════════════════════════════════
print("\n" + "=" * 50)
print(f"通过 {passed} / 失败 {failed}")
print("=" * 50)
sys.exit(1 if failed else 0)
