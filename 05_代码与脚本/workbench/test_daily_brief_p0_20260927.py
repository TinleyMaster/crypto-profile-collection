#!/usr/bin/env python3
"""早报「投资指导意义」重构 P0 回归护栏（方案_大盘早报_投资指导意义重构_2026-09-27）。

运行：.venv/bin/python workbench/test_daily_brief_p0_20260927.py
      （渲染层喂合成 brief；prompt 层用假 LLM 捕获 system/user prompt；不发信。
        ⚠️ **并非全离线**：默认全离线；但显式设 PROBE_ALLOW_DB_WRITE=1 后，W-06 注入用例
        会**连 prod 库**、在哨兵日期 1900-01-01 上 INSERT+DELETE（跑完即删，异常也兜底清理）。
        历史坑：该用例原以 DATABASE_URL 为门控，而探针前面的 get_settings(require_database=True)
        会经 load_local_env_file() 把 DATABASE_URL 写进 os.environ → 「离线默认」永远不成立、
        必然写库。故改为显式 opt-in（W-16-c）。）

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

print("[W-01] 行为注入：event_time 全空→不出 / 有 event_time+金额→出 / 有 event_time 无金额→过滤")
try:
    _bw1 = _brief()
    _bw1["kol_onchain"] = {"status": "ok", "kols": [{"name": "A"}], "signals": [
        {"symbol": "AAA", "signal_subtype": "whale_move", "kol_name": "A"},
    ]}
    check("KOL 链上信号" not in sdb.render_brief_html(_bw1), "event_time 全空 → 整块不出（不渲染空卡）")

    _bw1b = _brief()
    _bw1b["kol_onchain"] = {"status": "ok", "kols": [{"name": "A"}], "signals": [
        {"symbol": "AAA", "signal_subtype": "whale_move", "kol_name": "A",
         "event_time": "2026-09-27 10:30:00", "event_usd_value": 2000000, "event_direction": "in"},
    ]}
    _hw1b = sdb.render_brief_html(_bw1b)
    check("KOL 链上信号" in _hw1b, "有 event_time + 金额 → 出卡")
    check("2.0M USD" in _hw1b and "事件时间 2026-09-27 10:30" in _hw1b, "金额与事件时间一并上屏")

    _bw1c = _brief()
    _bw1c["kol_onchain"] = {"status": "ok", "kols": [{"name": "A"}], "signals": [
        {"symbol": "AAA", "signal_subtype": "whale_move", "kol_name": "A",
         "event_time": "2026-09-27 10:30:00", "event_direction": "in"},
    ]}
    check("KOL 链上信号" not in sdb.render_brief_html(_bw1c), "有 event_time 无金额 → 过滤不出")
except Exception as _e:
    check(False, "W-01 行为注入执行", f"{type(_e).__name__}: {_e}")

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
    # W-08：空段落会回退到全局口径（get_global_cex_netflow，联网）；此处打桩为 None
    # 保持本探针「纯离线 + 确定性」，专门覆盖「全局也不可用」的最坏分支。
    _gn_orig = mm._fetch_global_cex_netflow_7d
    mm._fetch_global_cex_netflow_7d = lambda: None

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
    check("交易所净流量不可用，不得据此判断抛压" in _up, "空段落渲染「暂无数据」而非空字符串（W-08）")
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

    mm._fetch_global_cex_netflow_7d = _gn_orig

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

# ③ 注入测试（**显式 opt-in**；W-16-c：不再以 DATABASE_URL 为门控——它会被前面用例静默 arm）
print("[W-06] 注入测试：同日写两次 → count(*)=1（需 PROBE_ALLOW_DB_WRITE=1）")
if os.environ.get("PROBE_ALLOW_DB_WRITE") == "1" and os.environ.get("DATABASE_URL"):
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
        # W-16-b：兜底清理哨兵行——若 INSERT 与 DELETE 之间抛异常，不留 1900-01-01 残骸
        try:
            import psycopg as _pg  # noqa: E402
            _cc = _pg.connect(os.environ["DATABASE_URL"], connect_timeout=30)
            try:
                with _cc.cursor() as _cu:
                    _cu.execute("DELETE FROM biz.daily_brief_snapshot WHERE brief_date=%s", (_SENT,))
                _cc.commit()
            finally:
                _cc.close()
        except Exception:
            pass
else:
    print("  - 跳过（未设 PROBE_ALLOW_DB_WRITE=1，注入测试为显式 opt-in 的写库项）")

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
# W-08 交易所净流路径统一 + 空态「数据不可用」+ 符号标注 + 参考类拆分
# ════════════════════════════════════════════════════════
print("[W-08] 源码核验：全局口径回退 + 两处符号标注分别正确 + 系统约束 7")
check("def _fetch_global_cex_netflow_7d(" in _mm_src, "macro_market 含全局净流回退 _fetch_global_cex_netflow_7d")
check("get_global_cex_netflow" in _mm_src, "回退取数走 db_stats.get_global_cex_netflow")
check("正值 = 净流出交易所" in _mm_src, "全局净流标注「正值 = 净流出交易所（潜在看涨）」")
check("正值 = 净流入交易所" in _mm_src and "符号相反" in _mm_src,
      "whale 侧标注「正值 = 净流入交易所（潜在抛压）」且注明与全局符号相反")
check("暂无数据：交易所净流量不可用，不得据此判断抛压" in _mm_src, "空段落渲染非空「暂无数据」文案")
check("某维度显示「暂无数据」时，不得在结论中表述为" in _mm_src, "system prompt 含第 7 条硬约束（缺失不得表述为零）")

print("[W-08] 参考类名单：稳定币/黄金代币（XAUt、USDC 必须在内）")
try:
    check("XAUT" in sdb._REFERENCE_SYMBOLS, "XAUT 在参考类名单")
    check("USDC" in sdb._REFERENCE_SYMBOLS, "USDC 在参考类名单")
    check("BTC" not in sdb._REFERENCE_SYMBOLS and "ETH" not in sdb._REFERENCE_SYMBOLS,
          "BTC/ETH 不在参考类名单（属影响供给类）")
except Exception as _e:
    check(False, "W-08 参考类名单", f"{type(_e).__name__}: {_e}")

print("[W-08] 渲染层：大额转账拆两栏（XAUt/USDC 进参考类，BTC 进影响供给类）")
try:
    _bw8 = _brief()
    _bw8["M2_whale_moves"] = {
        "status": "ok",
        "transfers": [
            {"symbol": "BTC", "value_usd": 8e6, "from_label": "Binance", "to_label": "未知钱包",
             "direction": "exchange_out"},
            {"symbol": "XAUT", "value_usd": 5e6, "from_label": "未知钱包", "to_label": "OKX",
             "direction": "exchange_in"},
            {"symbol": "USDC", "value_usd": 3e6, "from_label": "Circle", "to_label": "未知钱包",
             "direction": ""},
        ],
        "total_count": 3, "total_usd": 16e6,
    }
    _html8 = sdb.render_brief_html(_bw8)
    check("影响供给类（构成潜在买卖压）" in _html8, "出现「影响供给类」分栏标题")
    check("参考类（稳定币 · 黄金代币，不构成 BTC/ETH 供给变动）" in _html8, "出现「参考类」分栏标题")
    _i_supply = _html8.find("影响供给类")
    _i_ref = _html8.find("参考类")
    # 只在「影响供给类」到「参考类」之间的片段里查符号，避免命中邮件其他处的 BTC
    _supply_zone = _html8[_i_supply:_i_ref]
    _ref_zone = _html8[_i_ref:]
    check(_i_supply != -1 and _i_ref != -1 and ">BTC<" in _supply_zone and ">XAUT<" not in _supply_zone,
          "BTC 落在「影响供给类」栏，XAUT 不在该栏")
    check(">XAUT<" in _ref_zone, "XAUT 落在「参考类」栏")
    check(">USDC<" in _ref_zone, "USDC 落在「参考类」栏")
except Exception as _e:
    check(False, "W-08 渲染层用例执行", f"{type(_e).__name__}: {_e}")

# ════════════════════════════════════════════════════════
# W-09 恐贪指数 SSOT（market_snapshot_daily.fear_greed_value）
# ════════════════════════════════════════════════════════
print("[W-09] 源码核验：SSOT 常量单点定义 + 裁决函数 + 合并入口")
check("FEAR_GREED_SSOT" in _mm_src, "macro_market 含 SSOT 常量 FEAR_GREED_SSOT")
check('FEAR_GREED_SSOT = "biz.market_snapshot_daily.fear_greed_value"' in _mm_src,
      "SSOT 定死为 biz.market_snapshot_daily.fear_greed_value")
check("def _fear_greed_ssot_verdict(" in _mm_src, "含纯函数 _fear_greed_ssot_verdict（可注入）")
check("def _apply_fear_greed_ssot(" in _mm_src, "含合并入口 _apply_fear_greed_ssot")
check("_apply_fear_greed_ssot(_build_tldr(" in _mm_src, "M0_tldr 组装处调用 SSOT 合并")

print("[W-09] 注入：两源冲突 → 取 SSOT 且标注「日线源滞后」")
try:
    import macro_market as _mmw9  # noqa: E402
    _c = _mmw9._fear_greed_ssot_verdict(74, "2026-09-26", "Greed", 73, "2026-09-25")
    check(_c.get("value") == 74, "冲突时取 SSOT 值（74，非日线 73）")
    check(_c.get("as_of") == "2026-09-26", "带 SSOT as_of")
    check("日线源滞后" in (_c.get("note") or ""), "冲突时标注「日线源滞后」")
    _c2 = _mmw9._fear_greed_ssot_verdict(74, "2026-09-26", "Greed", 74, "2026-09-26")
    check("note" not in _c2, "两源一致时不加滞后标注")
    _c3 = _mmw9._fear_greed_ssot_verdict(None, None, None, 73, "2026-09-25")
    check(_c3 == {}, "SSOT 缺失 → 返回 {}（渲染层回退，不伪造）")
except Exception as _e:
    check(False, "W-09 裁决用例执行", f"{type(_e).__name__}: {_e}")

print("[W-09] 渲染：恐贪值旁强制显示 as-of 日期")
try:
    _b9 = _brief()
    _b9["M0_tldr"] = {**_b9["M0_tldr"], "fear_greed": 74, "fear_greed_label": "Greed",
                      "fear_greed_as_of": "2026-09-26",
                      "fear_greed_note": "日线源滞后（2026-09-25 日线=73，已按 SSOT 74 取值）"}
    _h9 = sdb.render_brief_html(_b9)
    check("截至 2026-09-26" in _h9, "恐贪卡显示 SSOT as-of 日期")
    check("日线源滞后" in _h9, "冲突标注上屏")
except Exception as _e:
    check(False, "W-09 渲染用例执行", f"{type(_e).__name__}: {_e}")

# ════════════════════════════════════════════════════════
# W-10 ETF 卡强制标「上一交易日」+ 滞后 > 3 天降级
# ════════════════════════════════════════════════════════
print("[W-10] 源码核验：ETF 文案无「单日」，改用「上一交易日」")
check("ETF 上一交易日净流入" in _mm_src, "ETF 信号 trigger_logic 改「上一交易日净流入」")
check("ETF 单日净流入" not in _mm_src and "ETF 单日净流出" not in _mm_src,
      "ETF 信号 trigger_logic 不再出现「单日」")
check("上一交易日({" in open(os.path.join(_SCRIPTS_BIN, "send_daily_brief.py"),
                              encoding="utf-8").read(), "ETF 卡含「上一交易日(MM-DD)」标注")

print("[W-10] 渲染：最新交易日 → 「上一交易日(MM-DD) 净流入」，无裸「单日」")
try:
    import datetime as _dt
    _today = _dt.date.today()
    _recent = (_today - _dt.timedelta(days=2)).isoformat()
    _b10 = _brief()
    _b10["M2_etf_flow"] = {
        "status": "ok", "latest_date": _recent,
        "assets": [{"symbol": "BTC", "flow_7d_usd": 1530e6, "latest_flow_usd": 86.7e6},
                   {"symbol": "ETH", "flow_7d_usd": 870e6, "latest_flow_usd": 87.0e6}],
    }
    _h10 = sdb.render_brief_html(_b10)
    _md = _recent[5:10]
    check(f"上一交易日({_md})" in _h10, "ETF 卡显式印「上一交易日(MM-DD)」")
    check(f"上一交易日({_md}) 净流入" in _h10, "单日净流入标为「上一交易日(MM-DD) 净流入」")
    check("单日" not in _h10, "ETF 卡内无裸「单日」字样")
except Exception as _e:
    check(False, "W-10 渲染用例执行", f"{type(_e).__name__}: {_e}")

print("[W-10] 注入：flow_date = today-5 → 降级「数据不可用」+ M9_degraded")
try:
    import datetime as _dt2
    _old = (_dt2.date.today() - _dt2.timedelta(days=5)).isoformat()
    _b10b = _brief()
    _b10b["M2_etf_flow"] = {
        "status": "ok", "latest_date": _old,
        "assets": [{"symbol": "BTC", "flow_7d_usd": 1530e6, "latest_flow_usd": 86.7e6}],
    }
    _h10b = sdb.render_brief_html(_b10b)
    check("数据不可用（最新交易日" in _h10b, "滞后 > 3 天 → 卡降级为「数据不可用」")
    check(_b10b.get("M9_degraded"), "降级进 M9_degraded")
    check(any("ETF资金流" in str(x) for x in (_b10b.get("M9_degraded") or [])),
          "M9_degraded 含 ETF 降级项")
    check("1.5B" not in _h10b, "降级时数值被隐藏（不显示 +$1.53B）")
except Exception as _e:
    check(False, "W-10 降级用例执行", f"{type(_e).__name__}: {_e}")

# ════════════════════════════════════════════════════════
# W-11 昨日基准缺失 → DIFF=no_baseline + 渲染提示
# ════════════════════════════════════════════════════════
print("[W-11] 源码核验：no_baseline 分支 + 快照缺日检测")
try:
    _bdb_src = open(os.path.join(_SCRIPTS_BIN, "build_daily_brief.py"), encoding="utf-8").read()
    check('"status": "no_baseline"' in _bdb_src, "build_daily_brief 置 DIFF.status=no_baseline")
    check("def _check_snapshot_gap(" in _bdb_src, "含快照缺日检测 _check_snapshot_gap")
    check("baseline_date" in _bdb_src, "no_baseline 带 baseline_date")
except Exception as _e:
    check(False, "W-11 源码核验", f"{type(_e).__name__}: {_e}")

print("[W-11] 渲染：DIFF=no_baseline → 显式「无昨日基准」提示")
try:
    _b11 = _brief()
    _b11["DIFF"] = {"status": "no_baseline", "baseline_date": "2026-09-26"}
    _h11 = sdb.render_brief_html(_b11)
    check("无昨日基准（2026-09-26 快照缺失），本期无变化对比" in _h11, "渲染「无昨日基准」提示")
    _b11b = _brief()  # 正常 DIFF 不应出现该提示
    check("无昨日基准" not in sdb.render_brief_html(_b11b), "正常 DIFF 不误伤")
except Exception as _e:
    check(False, "W-11 渲染用例执行", f"{type(_e).__name__}: {_e}")

# ════════════════════════════════════════════════════════
# W-12 变更日志（与昨日 diff）
# ════════════════════════════════════════════════════════
print("[W-12] 源码核验：delta 函数 + 五类字段 + 渲染块位置")
try:
    _bdb_src12 = open(os.path.join(_SCRIPTS_BIN, "build_daily_brief.py"), encoding="utf-8").read()
    check("def _build_m0_delta(" in _bdb_src12, "build_daily_brief 含 _build_m0_delta")
    check("def _extract_delta_view(" in _bdb_src12, "build_daily_brief 含 _extract_delta_view")
    check("def _read_prev_brief_payload(" in _bdb_src12, "build_daily_brief 含 _read_prev_brief_payload")
    check('brief["M0_delta"] = _delta' in _bdb_src12 and '"status": "no_baseline"' in _bdb_src12,
          "main() 落 M0_delta；无 T-1 行时置 no_baseline")
    _sdb_src12 = open(os.path.join(_SCRIPTS_BIN, "send_daily_brief.py"), encoding="utf-8").read()
    check("def _m0_delta_html(" in _sdb_src12, "send_daily_brief 含 _m0_delta_html")
    check("📋 与昨日变化" in _sdb_src12, "渲染块标题「与昨日变化」")
    check("模块 0.05" in _sdb_src12, "变更日志置于 M0 之后的第二块（模块 0.05）")
except Exception as _e:
    check(False, "W-12 源码核验", f"{type(_e).__name__}: {_e}")

print("[W-12] 纯函数：五类字段齐全 + 维持续期 + 反转 + 新增风险 + 越阈")
try:
    import build_daily_brief as _bdb12  # noqa: E402
    check(_bdb12._build_m0_delta({"M0_tldr": {}}, None) is None, "无 T-1 行 → 返回 None")
    _today12 = {
        "M0_tldr": {"fear_greed": 82},
        "M8_opportunities": [
            {"target": "SOL", "direction": "long", "trigger_logic": "ETF 流入"},
            {"target": "ETH", "direction": "long", "trigger_logic": "新入选"},
        ],
        "M8_watchlist": [{"target": "ADA", "direction": "short", "trigger_logic": "反转为空"}],
        "M4_risks": [{"target": "MVRV"}],
    }
    _prev12 = {
        "M0_tldr": {"fear_greed": 70},
        "M8_opportunities": [
            {"target": "SOL", "direction": "long", "trigger_logic": "旧理由"},
            {"target": "ADA", "direction": "long", "trigger_logic": "旧方向"},
        ],
        "M4_risks": [],
        "M0_delta": {"维持": [{"target": "SOL", "days": 3}], "新增": []},
    }
    _d12 = _bdb12._build_m0_delta(_today12, _prev12)
    check(set(_d12.keys()) == {"维持", "新增", "方向反转", "新增风险", "越阈"},
          f"五类字段齐全（实得 {sorted(_d12.keys())}）")
    _sol12 = next(e for e in _d12["维持"] if e["target"] == "SOL")
    check(_sol12["days"] == 4, f"维持天数续期（3+1=4，实得 {_sol12['days']}）")
    check(any(e["target"] == "ETH" for e in _d12["新增"]), "ETH 判为新增")
    _ada12 = next(e for e in _d12["方向反转"] if e["target"] == "ADA")
    check(_ada12["from"] == "long" and _ada12["to"] == "short", "ADA 判为方向反转（long→short）")
    check(any(e["target"] == "MVRV" for e in _d12["新增风险"]), "MVRV 判为新增风险")
    check(any(e["metric"] == "fear_greed" and e["threshold"] == 75 for e in _d12["越阈"]),
          "恐贪 70→82 判为越阈 75")
except Exception as _e:
    check(False, "W-12 纯函数用例执行", f"{type(_e).__name__}: {_e}")

print("[W-12] 渲染：no_baseline → W-11 提示；有变化分列；无变化显式")
try:
    _b12a = _brief()
    _b12a["M0_delta"] = {"status": "no_baseline", "baseline_date": "2026-09-26"}
    _h12a = sdb.render_brief_html(_b12a)
    check("📋 与昨日变化" in _h12a, "变更日志块渲染")
    check("无昨日基准（2026-09-26 快照缺失），本期无变化对比" in _h12a, "no_baseline → 输出 W-11 提示（不出空块）")

    _b12b = _brief()
    _b12b["M0_delta"] = {
        "维持": [{"target": "SOL", "direction": "long", "days": 4, "reason": ""}],
        "新增": [{"target": "XPL", "direction": "long"}],
        "方向反转": [{"target": "ADA", "from": "long", "to": "no_trade", "reason": "ETF 转流出"}],
        "新增风险": [{"target": "MVRV 高估"}],
        "越阈": [{"metric": "fear_greed", "prev": 70, "curr": 82, "threshold": 75}],
    }
    _h12b = sdb.render_brief_html(_b12b)
    check("SOL 看多（第 4 日）" in _h12b, "维持行含 标的/方向/第 N 日")
    check("XPL 看多" in _h12b, "新增行渲染")
    check("ADA 看多 → 无操作" in _h12b, "方向反转行渲染 from → to")
    check("MVRV 高估" in _h12b, "新增风险行渲染")
    check("恐贪指数 70 → 82（阈值 75）" in _h12b, "越阈行渲染（metric 中文化）")
    check("较上一期无变化" not in _h12b, "有变化时不出现「无变化」")

    _b12c = _brief()
    _b12c["M0_delta"] = {"维持": [], "新增": [], "方向反转": [], "新增风险": [], "越阈": []}
    check("较上一期无变化" in sdb.render_brief_html(_b12c), "五类全空 → 显式「较上一期无变化」")
    check("📋 与昨日变化" not in sdb.render_brief_html(_brief()),
          "无 M0_delta 字段（旧 payload）→ 不出块")

    _b12d = _brief()
    _b12d["M0_ai_summary"]["data_quality"] = [{"section": "大盘概况", "status": "ok"}]
    _b12d["M0_delta"] = {"status": "no_baseline", "baseline_date": "2026-09-26"}
    _h12d = sdb.render_brief_html(_b12d)
    _i_ai12 = _h12d.find("AI Morning Call")
    _i_d12 = _h12d.find("📋 与昨日变化")
    _i_st12 = _h12d.find("📋 数据状态")
    check(_i_ai12 < _i_d12 < _i_st12, "块序：AI 定调 → 变更日志 → 数据状态（第二块）",
          f"idx={_i_ai12}/{_i_d12}/{_i_st12}")
except Exception as _e:
    check(False, "W-12 渲染用例执行", f"{type(_e).__name__}: {_e}")

# ════════════════════════════════════════════════════════
# W-15 风险条目可判定化（数值阈值 + 后验）
# ════════════════════════════════════════════════════════
print("[W-15] 源码核验：模板 / 渲染校验 / 后验统计源")
try:
    check("风险条目必须可判定" in _mm_src, "system prompt 含第 9 条风险可判定约束")
    check("无后验样本 · 经验判断" in _mm_src, "prompt schema 含「无后验样本 · 经验判断」")
    check("【风险后验统计（来源 biz.scan_edge_daily" in _mm_src, "user_prompt 含后验统计块")
    _sdb_src15 = open(os.path.join(_SCRIPTS_BIN, "send_daily_brief.py"), encoding="utf-8").read()
    check("def _risk_has_threshold(" in _sdb_src15 and "def _risk_mark_posterior(" in _sdb_src15,
          "渲染层含阈值 / 后验校验函数")
    check("注意事项（无可判定阈值·不构成风险判定）" in _sdb_src15, "无阈值条目移入「注意事项」区")
except Exception as _e:
    check(False, "W-15 源码核验", f"{type(_e).__name__}: {_e}")

print("[W-15] 渲染：无阈值 → 不进风险区；有后验 → 不加「经验判断」；无后验 → 加标注")
try:
    _b15 = _brief()
    _b15["M0_ai_summary"]["risk_warnings"] = [
        "恐贪指数极度贪婪，市场情绪过热可能引发短期剧烈回调",               # 无数值阈值
        "BTC RSI 70.4 超买，84 以上属超买风险（样本 12，窗口 09-01~09-25）",  # 有阈值 + 后验
    ]
    _buf15 = io.StringIO()
    with contextlib.redirect_stdout(_buf15):
        _h15 = sdb.render_brief_html(_b15)
    _i_note15 = _h15.find("注意事项（无可判定阈值")
    _i_greed15 = _h15.find("恐贪指数极度贪婪")
    check(_i_note15 != -1 and _i_greed15 > _i_note15, "无阈值条目落在「注意事项」区（不在风险区）")
    check("RSI 70.4 超买" in _h15, "有阈值条目进「风险」区")
    check("经验判断" not in _h15, "有后验样本的条目不加「经验判断」")
    check("风险条目移出「风险」区" in _buf15.getvalue(), "移出动作写入渲染日志", _buf15.getvalue()[-160:])

    _b15b = _brief()
    _b15b["M0_ai_summary"]["risk_warnings"] = ["BTC RSI 70.4 超买，84 以上属超买风险"]
    _h15b = sdb.render_brief_html(_b15b)
    check("（无后验样本 · 经验判断）" in _h15b, "无后验样本 → 追加「（无后验样本 · 经验判断）」")

    check(sdb._risk_has_threshold("恐贪指数极度贪婪，可能引发回调") is False, "无数字 → 判为无可判定阈值")
    check(sdb._risk_has_threshold("MVRV 3.2 阈值以上属高估") is True, "数字 + 阈值词 → 判为可判定")
except Exception as _e:
    check(False, "W-15 渲染用例执行", f"{type(_e).__name__}: {_e}")

# ════════════════════════════════════════════════════════
# W-13 机会清单落表 + T+1/T+7 效果追踪
# ════════════════════════════════════════════════════════
print("[W-13] 源码核验：迁移 / upsert / outcome 脚本 / 调度")
try:
    _mig13 = open(os.path.join(os.path.dirname(_SCRIPTS_BIN), "migrations",
                               "fix_075_opportunity_snapshot.sql"), encoding="utf-8").read()
    check("biz.opportunity_snapshot" in _mig13, "迁移建 biz.opportunity_snapshot")
    check("PRIMARY KEY (snapshot_date, target, signal_type)" in _mig13, "主键 (snapshot_date, target, signal_type)")
    check("outcome_1d" in _mig13 and "outcome_7d" in _mig13, "含 outcome_1d / outcome_7d 列")
    _bdb_src13 = open(os.path.join(_SCRIPTS_BIN, "build_daily_brief.py"), encoding="utf-8").read()
    check("def _save_opportunity_snapshot(" in _bdb_src13, "build_daily_brief 含 _save_opportunity_snapshot")
    check("_save_opportunity_snapshot(date.today().isoformat(), brief)" in _bdb_src13,
          "main() 落库机会清单")
    check("ON CONFLICT (snapshot_date, target, signal_type) DO UPDATE" in _bdb_src13, "幂等 upsert")
    check(os.path.exists(os.path.join(_SCRIPTS_BIN, "backfill_opportunity_outcome.py")),
          "存在 backfill_opportunity_outcome.py")
    _sch13 = open(os.path.join(_HERE, "scheduler.py"), encoding="utf-8").read()
    check("opportunity_outcome_backfill" in _sch13, "调度表注册 opportunity_outcome_backfill")
    check("backfill_opportunity_outcome.py" in _sch13, "调度指向回填脚本")
    # W-13-R1：非单品信号 asset_id 置 NULL
    check("_NON_ASSET_SIGNAL_TYPES" in _bdb_src13 and "def _is_asset_level_signal(" in _bdb_src13,
          "R1：含非单品信号白名单 _is_asset_level_signal")
    check('if _is_asset_level_signal(sig) else None' in _bdb_src13, "R1：asset_id 按单品判定置空")
    # W-13-R2：ref_price_date 列 + 扫描堵洞
    _mig13b = open(os.path.join(os.path.dirname(_SCRIPTS_BIN), "migrations",
                                "fix_076_opportunity_snapshot_ref_date.sql"), encoding="utf-8").read()
    check("ref_price_date" in _mig13b, "R2：迁移新增 ref_price_date 列")
    check("WHERE outcome_1d IS NULL OR outcome_7d IS NULL" in _mig13b, "R2：部分索引谓词改为两列任一为空")
    _bfo13 = open(os.path.join(_SCRIPTS_BIN, "backfill_opportunity_outcome.py"), encoding="utf-8").read()
    check("WHERE outcome_1d IS NULL OR outcome_7d IS NULL" in _bfo13, "R2：回填扫描条件同步（堵永久空洞）")
    check("ref_price_date" in _bdb_src13, "R2：upsert 写入 ref_price_date")
except Exception as _e:
    check(False, "W-13 源码核验", f"{type(_e).__name__}: {_e}")

print("[W-13] 纯函数：机会清单抽取（M8 并集 + 按主键去重 + calibration 映射）")
try:
    import build_daily_brief as _bdb13  # noqa: E402
    _rows13 = _bdb13._collect_opportunity_rows({
        "M8_opportunities": [{
            "target": "AAA", "signal_type": "etf_flow", "direction": "long",
            "conviction_score": 70, "conviction_tier": "HIGH", "asset_id": 1378,
            "calibration_status": {"gate": "calibrated_low", "sample_count": 44, "hit_rate": 0.409},
        }],
        "M8_watchlist": [
            {"target": "AAA", "signal_type": "etf_flow", "direction": "long", "conviction_score": 55},
            {"target": "BBB", "signal_type": None, "direction": "short", "conviction_score": 30},
        ],
    })
    check(len(_rows13) == 2, f"M8 并集按 (target,signal_type) 去重（实得 {len(_rows13)} 行）")
    _aaa13 = next(r for r in _rows13 if r["target"] == "AAA")
    check(_aaa13["conviction_score"] == 70, "同键取分数更高者（55 不覆盖 70）")
    check(_aaa13["calibration_gate"] == "calibrated_low" and _aaa13["sample_count"] == 44
          and abs(_aaa13["hit_rate"] - 0.409) < 1e-9, "calibration_status 映射到 gate/sample_count/hit_rate")
    _bbb13 = next(r for r in _rows13 if r["target"] == "BBB")
    check(_bbb13["signal_type"] == "", "signal_type=None 归一为 ''（PK 不允许 NULL）")

    # W-13-R1 行为用例：非单品信号（聚合 target）asset_id 必须置空，单品照常保留
    _rows13r = _bdb13._collect_opportunity_rows({
        "M8_watchlist": [
            {"target": "2 币 MVRV 极度高估", "signal_type": "mvrv_deep_over",
             "direction": "short", "conviction_score": 91, "asset_id": 1378},
            {"target": "恐贪指数极度贪婪", "signal_type": "fng_extreme",
             "direction": "short", "conviction_score": 76, "asset_id": 2},
            {"target": "Privacy", "signal_type": "narrative",
             "direction": "long", "conviction_score": 76, "asset_id": 23111},
            {"target": "SOL", "signal_type": "catalyst",
             "direction": "long", "conviction_score": 73, "asset_id": 5426},
        ],
    })
    _agg13 = {r["target"]: r["asset_id"] for r in _rows13r}
    check(_agg13.get("2 币 MVRV 极度高估") is None and _agg13.get("恐贪指数极度贪婪") is None
          and _agg13.get("Privacy") is None,
          "R1：聚合信号（mvrv_/fng_extreme/narrative）asset_id 置 NULL，不把聚合结论归因到代表币")
    check(_agg13.get("SOL") == 5426, "R1：单品信号 asset_id 照常保留")
except Exception as _e:
    check(False, "W-13 纯函数用例执行", f"{type(_e).__name__}: {_e}")

# ════════════════════════════════════════════════════════
# W-14 校准样本量诚实标注（命中率 + 样本量 + 窗口；<50% / <30 / 从未回测）
# ════════════════════════════════════════════════════════
print("[W-14] 源码核验：标注函数 + 窗口列加载")
try:
    _sdb_src14 = open(os.path.join(_SCRIPTS_BIN, "send_daily_brief.py"), encoding="utf-8").read()
    check("def _cal_line_html(" in _sdb_src14 and "def _cal_window(" in _sdb_src14,
          "渲染层含 _cal_line_html / _cal_window")
    check("历史命中率低于抛硬币" in _sdb_src14, "命中率 <50% 标注分支")
    check("样本不足，仅供参考" in _sdb_src14, "样本量 <30 标注分支")
    check("从未回测" in _sdb_src14, "exempt_* → 「从未回测」分支")
    check("cal_line = _cal_line_html(opp.get(\"calibration_status\"))" in _sdb_src14, "机会卡渲染 cal_line")
    check("window_start" in _mm_src, "macro_market 加载 window_start（窗口随校准下发）")
except Exception as _e:
    check(False, "W-14 源码核验", f"{type(_e).__name__}: {_e}")

print("[W-14] 渲染：命中率必带样本量与窗口；<50% / <30 / 无样本 分别标注")
try:
    _b14 = _brief()
    _b14["M8_watchlist"] = [{
        "target": "W14CALTEST", "signal_type": "etf_flow", "direction": "long",
        "conviction_score": 60, "conviction_tier": "MED", "trigger_logic": "ETF 净流入",
        "calibration_status": {
            "gate": "calibrated_low", "hit_rate": 0.409, "sample_count": 44,
            "window_start": "2026-08-31", "window_end": "2026-09-25",
        },
    }]
    _h14 = sdb.render_brief_html(_b14)
    check("命中率 40.9%（样本 44，窗口 08-31~09-25）" in _h14, "命中率同时带样本量与窗口")
    check("历史命中率低于抛硬币" in _h14, "hit_rate=0.41 → 「低于抛硬币」（注入用例）")

    _b14b = _brief()
    _b14b["M8_watchlist"] = [{
        "target": "W14CALTEST", "signal_type": "exempt_not_backtestable", "direction": "long",
        "conviction_score": 60, "conviction_tier": "MED", "trigger_logic": "从未回测",
        "calibration_status": {
            "gate": "exempt_not_backtestable", "hit_rate": None, "sample_count": 0,
            "window_start": None, "window_end": "2026-09-25",
        },
    }]
    _h14b = sdb.render_brief_html(_b14b)
    check("从未回测" in _h14b, "hit_rate=None + exempt_* → 「从未回测」（注入用例）")

    _b14c = _brief()
    _b14c["M8_watchlist"] = [{
        "target": "W14CALTEST", "signal_type": "github_activity", "direction": "long",
        "conviction_score": 60, "conviction_tier": "MED", "trigger_logic": "开发活跃",
        "calibration_status": {
            "gate": "preliminary", "hit_rate": 0.607, "sample_count": 28,
            "window_start": "2026-08-31", "window_end": "2026-09-25",
        },
    }]
    _h14c = sdb.render_brief_html(_b14c)
    check("样本 28" in _h14c and "样本不足，仅供参考" in _h14c, "sample_count=28 → 「样本不足，仅供参考」")
    check("历史命中率低于抛硬币" not in _h14c, "60.7% 不加「低于抛硬币」")

    check(sdb._cal_line_html(None) == "", "无 calibration_status → 不出校准行")
except Exception as _e:
    check(False, "W-14 渲染用例执行", f"{type(_e).__name__}: {_e}")

# ════════════════════════════════════════════════════════
# W-16 探针纪律（本文件自身）：写库用例显式 opt-in + 兜底清理 + docstring 如实
# ════════════════════════════════════════════════════════
print("[W-16] 探针纪律：写库用例门控 / 兜底清理 / docstring 与事实一致")
try:
    _src16 = open(__file__, encoding="utf-8").read()
    check('os.environ.get("PROBE_ALLOW_DB_WRITE") == "1"' in _src16,
          "写库用例改显式 opt-in（不再复用会被自动 arm 的 DATABASE_URL）")
    check("并非全离线" in _src16, "docstring 如实声明「会连 prod」")
    check("兜底清理哨兵行" in _src16, "INSERT/DELETE 之间异常也兜底清理哨兵行")
except Exception as _e:
    check(False, "W-16 探针纪律核验", f"{type(_e).__name__}: {_e}")

# ════════════════════════════════════════════════════════
print(f"\n{'=' * 46}\n通过 {passed} / 失败 {failed}\n{'=' * 46}")
sys.exit(1 if failed else 0)