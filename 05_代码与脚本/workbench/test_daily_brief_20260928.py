#!/usr/bin/env python3
"""加密大盘早报 2026-09-28 审计护栏（指导意义 + 详细度维度）。

运行：python workbench/test_daily_brief_20260928.py
      （纯离线：渲染层直接喂合成 brief；数据层用源码/纯函数断言）

修复清单（审计_加密大盘早报_2026-09-28）：
  P0-1：顶部/贪婪环境与「全做多」拼接层 → 交易区强制约束注脚（轻仓/止损/不追高）
  P0-2：维度标「数据不可用」时不得无交代地并排具体数字 → 替代源/解锁口径 note
  P1-1：今日操作清单（TL;DR）置顶，且遵守 M4-1 折叠（不重复已折叠结论）
  P1-3：高危信号逐条「风险点 + 应对」，稳定币类必解释
  P1-4：证据覆盖 N/M 旁披露缺哪几项
  P1-5：赛道领涨币带 7d 涨幅
  P1-6：ETF/赛道/巨鲸卡片标注真实截至日与滞后天数
  P1-7：正文自称「不构成高亮」→ 显式标「观察级（非高亮）」
  准确性 P1：恐贪 70 不再被标「极度贪婪」（≥75 才称极度）
  P2-1：大额转账多跳去重与处理顺序无关（共享端点 + 金额相近）
  P2-2：巨鲸摘要须列增持/减持数量（prompt）
  P2-3：聪明钱/告警质量补白话注解
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_BIN = os.path.join(os.path.dirname(_HERE), "scripts", "bin")
sys.path.insert(0, _HERE)
sys.path.insert(0, _SCRIPTS_BIN)
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


# ── P1-2 伪值判定 / 六要素 ──────────────────────────────────────────
print("[P1-2] 占位符视为缺字段")
check(sdb._is_placeholder_value(None) and sdb._is_placeholder_value("") and
      sdb._is_placeholder_value("N/A") and sdb._is_placeholder_value("—") and
      sdb._is_placeholder_value("无"), "None/空/N/A/—/无 均判缺失")
check(not sdb._is_placeholder_value("2.66197") and not sdb._is_placeholder_value("移动止盈"),
      "真实值不误判")
_full = {"trigger": "站上 50 日线", "invalidate": "跌破 100", "target": "120",
         "horizon": "短线", "ref_price": "110", "ref_as_of": "2026-09-28"}
check(sdb._trade_missing_fields(_full) == [], "六要素齐备 → 无缺字段")
_bad = dict(_full, ref_price="N/A")
check("参照价" in sdb._trade_missing_fields(_bad), "ref_price=N/A → 判缺「参照价」")

# ── P1-4 证据覆盖缺项 ───────────────────────────────────────────────
print("[P1-4] 证据覆盖缺项明细")
_dq = [{"section": "大盘概况", "status": "ok"}, {"section": "交易所净流量", "status": "empty"},
       {"section": "即将解锁", "status": "error"}]
check(sdb._coverage_missing_names(_dq) == ["交易所净流量", "即将解锁"], "缺项名列表正确")

# ── P0-1 顶部风险缝合层 ─────────────────────────────────────────────
print("[P0-1] 顶部/贪婪环境约束注脚")
_g1 = sdb._top_risk_guard_html("顶部风险（早期）", 50, has_trades=True)
check("高风险环境约束" in _g1 and "轻仓" in _g1 and "失效翻转" in _g1, "顶部周期 → 出注脚")
_g2 = sdb._top_risk_guard_html("中性", 72, has_trades=True)
check("高风险环境约束" in _g2 and "恐贪" in _g2, "恐贪≥70 → 出注脚")
check(sdb._top_risk_guard_html("中性", 40, has_trades=True) == "", "非顶部非贪婪 → 不出注脚")
_g3 = sdb._top_risk_guard_html("顶部风险（早期）", 70, has_trades=False)
check("今日方向" in _g3, "无方向时措辞用「今日方向」")
check("常态仓位的 50%" in _g3, "轻仓比例有明确数值")

# ── P1-1 TL;DR ─────────────────────────────────────────────────────
print("[P1-1] 今日操作清单")
_tldr = sdb._build_tldr_html(
    [dict(_full, asset="ETH", direction="做多", trigger="站上 2700")], [], {})
check("今日操作清单" in _tldr and "ETH" in _tldr and "站上 2700" in _tldr, "摘要有可执行方向")
_tldr_owner = sdb._build_tldr_html(
    [], [{"target": "Solana", "symbol": "SOL", "trigger_logic": "第二份结论"}], {"sol": "AI 精选高亮"})
check("第二份结论" not in _tldr_owner, "已折叠标的的结论不重复上屏（M4-1）")
check(sdb._build_tldr_html([], [], {}) == "", "无可摘要项 → 不出空壳")

# ── P1-3 高危逐条解读 ───────────────────────────────────────────────
print("[P1-3] 高危信号逐条解读")
_r_stable = sdb._risk_one_liner({"target": "USDC", "signal_type": "x"})
check("稳定币" in _r_stable and "脱锚" in _r_stable, "稳定币必解释为何高危")
_r_gen = sdb._risk_one_liner({"target": "ENA", "key_metric": "7日解锁 9%", "action_hint": "规避"})
check("ENA" in _r_gen and "解锁" in _r_gen and "规避" in _r_gen, "一般信号给「风险点+应对」")

# ── P1-7 观察级标注 ─────────────────────────────────────────────────
print("[P1-7] DePIN 自相矛盾 → 观察级标注")
_hl = [{"target": "DePIN", "direction": "long", "conviction_score": 60,
        "ai_analysis_v2": {"overall_score": 60, "confidence": "MED",
                           "reason_summary": "缺乏个券验证，不构成高亮或高危"}}]
_b_de = {"M0_tldr": {}, "M0_ai_summary": {"status": "ok", "headline": "x", "bias": "中性"},
         "M3_highlights": _hl}
check("观察级（非高亮）" in sdb.render_brief_html(_b_de), "自称非高亮 → 出「观察级（非高亮）」")

# ── P1-5 赛道领涨币涨幅 + P1-6 时效 + P0-1 集成 ─────────────────────
print("[P1-5/1-6/P0-1] 渲染集成（赛道涨幅 / 时效 / 顶部注脚 / 缺项披露）")
_b_int = {
    "M0_tldr": {"btc_cycle_phase": "顶部风险（早期）", "fear_greed": 72,
                "fear_greed_label": "Greed", "btc_price": 85000, "btc_change_24h_pct": 0.6,
                "eth_price": 2692, "eth_change_24h_pct": -0.1},
    "M0_ai_summary": {"status": "ok", "headline": "顶部风险", "bias": "中性", "conviction": "medium",
                      "trade_suggestions": [dict(_full, asset="SOL", direction="做多")],
                      "data_quality": [{"section": "大盘概况", "status": "ok"},
                                       {"section": "交易所净流量", "status": "empty"},
                                       {"section": "即将解锁", "status": "empty"}]},
    "M2_sector_flow": {"metric_date": "2026-09-25", "sectors": [
        {"sector_key": "ai", "sector_label": "AI & Big Data", "mcap_change_7d_pct": 18.3,
         "composite_score": 90,
         "leaders": [{"symbol": "QNT", "percent_change_7d": 12.3}]}]},
    "M2_etf_flow": {"status": "ok", "latest_date": "2026-09-25",
                    "assets": [{"symbol": "BTC", "flow_7d_usd": 582.4e6}]},
    "M2_holder_concentration": {"status": "ok", "snapshot_date": "2026-09-27",
                                "whale_buying": [], "whale_selling": []},
    "M3_highlights": [{"target": "ETH", "direction": "long", "conviction_score": 80,
                       "ai_analysis_v2": {"overall_score": 80, "confidence": "HIGH",
                                          "reason_summary": "趋势走强"}}],
    "M4_risks": [{"target": "USDS", "ai_analysis_v2": {"overall_score": 30, "error": None}}],
}
_h = sdb.render_brief_html(_b_int)
check("高风险环境约束" in _h, "P0-1：渲染层出现环境约束注脚")
check("QNT" in _h and "+12.3%" in _h, "P1-5：领涨币带 7d 涨幅")
check("截至 09-25，滞后" in _h, "P1-6：赛道卡标注真实截至日与滞后")
check("缺 交易所净流量、即将解锁" in _h, "P1-4：证据覆盖旁披露缺项")
check("USDS" in _h and "稳定币" in _h, "P1-3：渲染层高危稳定币有解读")
check("**" not in _h, "渲染文案无 markdown 强调符（HTML 邮件护栏）")

# ── P2-1 大额转账去重（顺序无关） ───────────────────────────────────
print("[P2-1] 大额转账多跳去重与顺序无关")
try:
    import macro_market as mm  # noqa: E402
    _ab = {"symbol": "TAO", "from_address": "5Q", "to_address": "BQ", "value_usd": 133.3e6,
           "block_timestamp": "2026-09-28T00:00:00Z"}
    _bc = {"symbol": "TAO", "from_address": "BQ", "to_address": "J6", "value_usd": 133.3e6,
           "block_timestamp": "2026-09-28T00:00:00Z"}
    check(len(mm._dedup_whale_transfers([_ab, _bc])) == 1, "A→B→C 正向 → 合并为 1")
    check(len(mm._dedup_whale_transfers([_bc, _ab])) == 1, "C←B←A 反向顺序 → 仍合并为 1")
    _rt = {"symbol": "TAO", "from_address": "BQ", "to_address": "5Q", "value_usd": 133.3e6,
           "block_timestamp": "2026-09-28T00:00:01Z"}
    check(len(mm._dedup_whale_transfers([_ab, _rt])) == 1, "往返 A→B / B→A → 合并为 1")
    _other = {"symbol": "TAO", "from_address": "X1", "to_address": "Y2", "value_usd": 133.3e6,
              "block_timestamp": "2026-09-28T00:00:02Z"}
    check(len(mm._dedup_whale_transfers([_ab, _other])) == 2, "无共享端点 → 不合并")

    # ── 准确性 P1 恐贪标签 ──
    print("[准确性 P1] 恐贪标签分档")
    check(mm._fng_extreme_label(70, True) == "恐贪指数贪婪", "70 → 贪婪（非极度）")
    check(mm._fng_extreme_label(80, True) == "恐贪指数极度贪婪", "80 → 极度贪婪")
    check(mm._fng_extreme_label(20, False) == "恐贪指数极度恐惧", "20 → 极度恐惧")
    check(mm._fng_extreme_label(40, False) == "恐贪指数恐惧", "40 → 恐惧")
except Exception as _e:
    check(False, "macro_market 纯函数可导入", f"{type(_e).__name__}: {_e}")

# ── P0-2 数据状态表 note 渲染 ───────────────────────────────────────
print("[P0-2] 数据状态表口径 note")
_tbl = sdb._data_status_table_html([
    {"section": "交易所净流量", "status": "empty", "as_of": None,
     "usable": 0, "total": 0, "note": "本封以替代源估算 (+113.2M)"}])
check("本封以替代源估算" in _tbl, "不可用维度可披露替代源 note")
_tbl2 = sdb._data_status_table_html([
    {"section": "即将解锁", "status": "empty", "as_of": None, "usable": 0, "total": 0,
     "note": "解锁事件由催化剂管道（宏观&代币事件）提供，与「即将解锁」模块口径不同"}])
check("催化剂管道" in _tbl2, "解锁口径 note 渲染")

# ── P2-2 / P2-3 源码守卫 ────────────────────────────────────────────
print("[P2-2/2-3] prompt 与渲染守卫")
_mm_src = open(os.path.join(_HERE, "macro_market.py"), encoding="utf-8").read()
check("增持与减持" in _mm_src and "向优质标的集中" in _mm_src.replace("不得用", "不得用"),
      "巨鲸摘要要求同时提增持/减持")
check("替代源估算" in _mm_src, "prompt 保留替代源估算标注")
check("聪明钱 = 链上监控地址的净买入" in open(
    os.path.join(_SCRIPTS_BIN, "send_daily_brief.py"), encoding="utf-8").read(),
    "聪明钱术语注解在场")
check("白话结论" in open(os.path.join(_SCRIPTS_BIN, "send_daily_brief.py"),
                        encoding="utf-8").read(), "告警质量补白话结论")

# ── U-A 可见性 + P2-3 TL;DR 措辞 + P1-7 观察级标题（审计 2026-09-29）──
print("[U-A 可见性] trigger_logic 连板注脚补显到高亮卡")
_logic = ("QNT, SOON, AUDIO 24h 涨幅超 15%，最高 50.8%，平均 23.6%（其中 QNT连续4天 持续强势）")
check(sdb._streak_hint(_logic) == "（其中 QNT连续4天 持续强势）", "H-1 _streak_hint 正确提取")
check(sdb._streak_hint("A, B 24h 涨幅超 15%，最高 10%") == "", "H-2 无注脚 → 空串")
# AI reason_summary 覆盖 trigger_logic 时，注脚须补显
_b_hl = {
    "M0_tldr": {}, "M0_ai_summary": {"status": "ok", "headline": "x", "bias": "中性"},
    "M3_highlights": [{"target": "17 币 24h 暴涨", "direction": "long", "conviction_score": 80,
                       "trigger_logic": _logic,
                       "ai_analysis_v2": {"overall_score": 80, "confidence": "HIGH",
                                          "reason_summary": "RWA催化剂驱动QNT暴涨，RSI 96.7 透支"}}],
}
_h_hl = sdb.render_brief_html(_b_hl)
check("QNT连续4天 持续强势" in _h_hl, "H-3 AI reason_summary 覆盖时仍显连板注脚")
check("RSI 96.7" in _h_hl, "H-4 原 reason_summary 不丢")
check("（含观察级）" not in _h_hl, "H-5 无观察级条目 → 标题不加注")

# P1-7：含「不构成高亮」条目 → 标题注明（含观察级）
_b_obs = {
    "M0_tldr": {}, "M0_ai_summary": {"status": "ok", "headline": "x", "bias": "中性"},
    "M3_highlights": [{"target": "DePIN", "direction": "long", "conviction_score": 60,
                       "ai_analysis_v2": {"overall_score": 60, "confidence": "MED",
                                          "reason_summary": "缺乏个券验证，不构成高亮或高危"}}],
}
check("（含观察级）" in sdb.render_brief_html(_b_obs), "H-6 含观察级 → 高亮标题注明")

# P2-3：无新开方向时 TL;DR 标题改「观察 / 持仓参考」
check(sdb._build_tldr_html([], [{"target": "PENDLE", "trigger_logic": "站上 2.66 观察"}], {})
      .find("观察 / 持仓参考") != -1, "H-7 无方向 → TL;DR 标题改「观察/持仓参考」")
check("今日操作清单" in sdb._build_tldr_html(
    [{"asset": "ETH", "direction": "做多", "trigger": "站上 2700",
      "invalidate": "跌破 2600"}], [], {}), "H-8 有方向 → 仍为「今日操作清单」")

# ── 审计 2026-09-29 小白可读性：R-1/R-5/R-6/R-8/R-11/R-12 + R-3（SSOT 删除）──
print("[审计0929] 小白可读性 R-1/5/6/8/11/12 + R-3")
_b_rd = {
    "M0_tldr": {"fear_greed": 74, "fear_greed_label": "Greed"},
    "M0_ai_summary": {
        "status": "ok", "headline": "RWA 单周暴涨 34%，资金转向实体资产赛道",
        "bias": "偏多", "market_regime": "震荡",
        "trade_suggestions": [dict(_full, asset="ETH", direction="做多")],
        "watchlist": ["PENDLE", "AI"],
        "data_quality": [{"section": "大盘概况", "status": "ok"}],
    },
    "M3_highlights": [{
        "target": "17 币 24h 暴涨", "direction": "long", "conviction_score": 80,
        "trigger_logic": _logic,
        "ai_analysis_v2": {"overall_score": 80, "confidence": "LOW", "direction": "short",
                           "reason_summary": "RWA催化剂驱动QNT暴涨，RSI 96.7 透支"}}],
}
_h_rd = sdb.render_brief_html(_b_rd)
check("今日 3 句话" in _h_rd and "名词速查" in _h_rd, "R-12/R-7 顶部 3 句话 + 名词速查")
check("行动：" in _h_rd, "R-1 头条下加「行动」chip")
check("关键三维评分（技术/基本面/情绪）" in _h_rd and "六维评分 ·" not in _h_rd,
      "R-6 标题改「关键三维评分」（不再称六维）")
check("AI信心 LOW" in _h_rd and "AI存疑" not in _h_rd, "R-5/R-11 徽章标「AI信心」且不再叫「AI存疑」")
check("AI观点分歧" in _h_rd, "R-11 AI 判反 → 「AI观点分歧」")
check("综合评分（满分100）" in _h_rd, "R-8 综合评分加满分刻度")
# R-8：恐贪刻度（大盘脉搏卡）
_b_fg = {"M0_tldr": {"fear_greed": 74, "fear_greed_label": "Greed"},
         "M0_ai_summary": {"status": "ok", "headline": "x", "bias": "中性"}}
check("（0-100，>50 偏贪婪）" in sdb.render_brief_html(_b_fg), "R-8 恐贪加 0-100 刻度")
# R-1：无方向时行动 chip 显示「今日无操作」
_b_noop = {"M0_tldr": {}, "M0_ai_summary": {"status": "ok", "headline": "x", "bias": "中性"}}
check("行动：今日无操作" in sdb.render_brief_html(_b_noop), "R-1 无方向 → 「行动：今日无操作」")

# R-5 降档文案不再与徽章口径冲突（信号档位 ≠ 模型信心）
check("信号档位封顶 MED" in _mm_src and "档位降为 MED" not in _mm_src,
      "R-5 降档文案改「信号档位封顶 MED」")
# R-3：恐贪 note 删内部词 SSOT
_fgv = mm._fear_greed_ssot_verdict(74, "2026-09-26", "Greed", 72, "2026-09-25")
check("SSOT" not in (_fgv.get("note") or "") and "最新官方值 74" in (_fgv.get("note") or ""),
      "R-3 恐贪 note 删「SSOT」改「最新官方值」")

print("\n" + "=" * 46)
print(f"{passed}/{passed + failed} 通过")
print("=" * 46)
sys.exit(1 if failed else 0)
