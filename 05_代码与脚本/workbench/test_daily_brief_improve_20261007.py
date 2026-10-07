# -*- coding: utf-8 -*-
"""验证 2026-10-07 早报改进的新增纯函数（离线，不连库）。"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_BIN = os.path.join(os.path.dirname(_HERE), "scripts", "bin")
for p in (_HERE, _SCRIPTS_BIN):
    if p not in sys.path:
        sys.path.insert(0, p)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = failed = 0


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


# ── 1. watchlist 截断修复（macro_market._clip_watch_item）──
print("[T1] watchlist 按句读边界截断")
import macro_market as mm
_c = mm._clip_watch_item

# 邮件里的真实截断样例：旧实现 [:30] 会切成「市值」→「市」
_real = "Infrastructure 赛道（7日 +24.67%，市值 312.4B，资金持续流入）"
_out = _c(_real, 30)
check(_out == "Infrastructure 赛道（7日 +24.67%）…",
      "强/弱断点截断 + 括号闭合（不再把「市值」切半）", repr(_out))

_real2 = "Layer 2 / DeFi（7日几乎零增长，观察是否补涨或承接资金）"
_out2 = _c(_real2, 30)
check(_out2.endswith("…") and not _out2.endswith("或") and "增长" in _out2,
      "不再以「或」这种半截词收尾", repr(_out2))

_long = "这是一个短句子"
check(_c(_long, 60) == _long, "未超长 → 原样返回")
check(_c(None) == "", "None → 空串")
# 前向补全：无断点时延长到下一个强断点，不切半词
_no_break = "abcdefghijklmnopqrstuvwxyz" * 3  # 60 字符无标点
check(_c(_no_break, 20).endswith("…") and len(_c(_no_break, 20)) <= 81,
      "无断点 → 前向延长仍以省略号收尾")

# ── 2. Outlook 网格辅助（send_daily_brief._grid_table）──
print("[T6] _grid_table 输出 table 布局")
import send_daily_brief as sdb
_gt = sdb._grid_table(["<div>A</div>", "<div>B</div>", "<div>C</div>"])
check(_gt.startswith("<table") and "<tr>" in _gt and "<td" in _gt, "grid 输出为 table", _gt[:80])
check('width="33%"' in _gt, "三列等宽 33%", _gt[:200])
check(sdb._grid_table([]) == "", "空列表 → 空串")

# ── 3. 昨日复盘渲染（离线 mock）──
print("[T3] 昨日复盘渲染")
_hit = {"date": "2026-10-06", "items": [
    {"target": "WMETAX", "direction": "long", "score": 76, "status": "hit", "ret": 1.23},
    {"target": "UNI", "direction": "long", "score": 71, "status": "miss", "ret": -0.87},
    {"target": "SOL", "direction": "short", "score": 55, "status": "pending"},
]}
_h = sdb._render_yesterday_review_html(_hit)
check("昨日复盘" in _h and "✅ 命中" in _h and "❌ 未中" in _h and "⏳ 待结算" in _h,
      "命中/未中/待结算三态渲染", _h[:120])
check("+1.23%" in _h and "-0.87%" in _h, "收益数值（方向对齐）渲染")
check(sdb._render_yesterday_review_html({}) == "", "空数据 → 不出空壳")
check(sdb._render_yesterday_review_html({"error": "x"}) == "", "异常 → 不出空壳")

# ── 4. 告警质量渲染（离线 mock，已下沉为函数）──
print("[T5] 告警质量函数化渲染")
_aq = {"win_1h": 0.35, "be_1h": 0.537, "odds_1h": 0.86, "pf_1h": 0.46,
       "roll3_win_1h": 0.339, "roll3_be_1h": 0.566, "roll3_pf_1h": 0.40,
       "alerts_n": 60, "report_date": "2026-10-05", "regime_label": "mixed",
       "severity": "high", "conclusion": "3 日滚动 T+1h 胜率 33.9% < 平衡线 56.6%，PF 0.40；边缘桶 funding_sign=>0"}
_a = sdb._render_alert_quality_html({"M0_alert_quality": _aq})
check("告警质量" in _a and "33.9%" in _a and "失配" in _a, "告警质量卡正常渲染")
check(sdb._render_alert_quality_html({"M0_alert_quality": {}}) == "", "无告警数据 → 空串")

# ── 5. 完整渲染冒烟：新模块 + 附录 + 失配期降级（合成 brief）──
print("[集成] render_brief_html 冒烟（含失配期 + 昨日复盘 + 附录）")
_b = {
    "M0_tldr": {"btc_price": 85000, "btc_change_24h_pct": -0.5, "eth_price": 2692,
                "eth_change_24h_pct": -0.7, "fear_greed": 73, "fear_greed_label": "Greed",
                "fear_greed_as_of": "2026-10-06", "total_market_cap": 2.9e12,
                "btc_volatility_7d": 2.67},
    "M0_ai_summary": {"status": "ok", "headline": "BTC 缩量震荡于 85K", "bias": "中性",
                      "conviction": "medium", "market_regime": "震荡",
                      "trade_suggestions": [], "no_trade_reason": "方向不明",
                      "risk_warnings": [], "watchlist": ["BTC", "ETH"],
                      "data_quality": [{"section": "大盘概况", "status": "ok"}]},
    "M0_alert_quality": _aq,
    "M0_signal_track": {"1d": {"n": 1, "hit": 0.5, "mean_pct": 0.1}, "7d": {},
                        "as_of": "2026-10-05"},
    "M0_yesterday_review": _hit,
    "M0_delta": {"维持": [], "新增": [{"target": "WMETAX", "direction": "long"}],
                 "方向反转": [], "新增风险": [], "越阈": []},
    "M3_highlights": [
        {"target": "BNB", "direction": "long", "conviction_score": 65,
         "ai_analysis_v2": {"overall_score": 65, "confidence": "MED",
                            "score_card": {"technical": {"score": 66}, "fundamental": {"score": 70},
                                           "sentiment": {"score": 62}},
                            "reason_summary": "ETF 单日净流入"}}],
    "M4_risks": [],
    "M8_opportunities": [{"target": "WMETAX", "direction": "long",
                          "conviction_tier": "HIGH", "conviction_score": 76,
                          "signal_type": "dev_activity", "calibration_status": {"gate": "calibrated_ok"},
                          "trigger_logic": "dev 活跃爆发"}],
    "M8_watchlist": [],
    "M2_sector_flow": {"sectors": []},
    "M2_etf_flow": {"status": "ok", "latest_date": "2026-10-05", "assets": [
        {"symbol": "BTC", "flow_7d_usd": 234e6, "latest_flow_usd": -89.8e6},
        {"symbol": "ETH", "flow_7d_usd": -275e6, "latest_flow_usd": -18.9e6}]},
    "M2_stablecoin": {"status": "ok", "total_usd": 312e9, "change_7d_pct": 0.4},
    "M2_exchange_flow": {"status": "empty"},
    "M2_whale_moves": {"status": "ok", "transfers": [{"symbol": "UNI", "value_usd": 44.5e6}]},
    "M2_holder_concentration": {"status": "ok", "whale_buying": [], "whale_selling": []},
    "M2_liquidation": {"liq_usd_24h": 100.3e6, "long_24h": 61.2e6, "short_24h": 39.0e6},
    "M2_liq_regime": {"status": "ok", "symbols": {"BTCUSDT": {"bucket": "NORMAL"}, "ETHUSDT": {"bucket": "NORMAL"}}},
    "M6_upcoming_unlocks": {"unlocks": []},
    "M6_catalyst": {"hardcoded": [], "token_events": []},
    "DIFF": {"total_mcap_pct": -0.6},
    "M5_daily_diff": {},
    "M7_divergence": [], "M8_smart_money": {}, "M8_meme": {}, "M8_resonance": {},
}
_html = sdb.render_brief_html(_b)
check("🔄 昨日复盘" in _html, "昨日复盘模块出现在正文")
check("附录 · 数据与方法" in _html, "附录容器出现")
check("失配期 · 观察" in _html, "失配期机会方向降级徽标出现")
check("失配期 · 下方买卖方向" in _html, "机会清单区块失配期说明出现")
# 机会清单里 WMETAX 的 ▲看多 应被降级为 ◆ 观望
import re
_wm = re.search(r'WMETAX.{0,400}?', _html, re.S)
check("看多" not in (_wm.group(0) if _wm else ""), "失配期 WMETAX 不再显示「看多」")
# 评分颜色语义：47 分与 66 分不同色（此处 65 分 → 琥珀）
check('color:#f59e0b;line-height:1">65' in _html, "65 分 → 琥珀色（非固定红）")
# table 布局出现、grid 不再出现
check('<table width="100%"' in _html, "大盘/ETF 使用 table 布局")
check('display:grid' not in _html, "主网格不再使用 display:grid")

print()
print(f"===== {passed} 通过 / {failed} 失败 =====")
sys.exit(1 if failed else 0)
