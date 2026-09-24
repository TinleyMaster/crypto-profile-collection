#!/usr/bin/env python3
"""早报 2026-09-24 审计回归护栏（审计_加密大盘早报_2026-09-24）。

运行：python workbench/test_daily_brief_20260924.py
      （纯离线：渲染层直接喂合成 brief；数据层用源码守卫 + 只读 SQL 结构断言）

  P1-1：ETF 卡片「合计净流入」把全部资产加总，与只展示的 BTC/ETH 分项对不上
        （486 vs 609）→ 改标「全部 ETF 合计」+ 附分项拆解，使算术自洽。
  P1-2：XPL「占流通」同封出现 64.80%（实时计算）与 63.20%（源口径）→
        fetch_upcoming_unlocks 优先取源 unlock_ratio_mcap（与事件栏同源）。
  P2-A：AI 方向偏多 vs 当日全跌 → 加结构性判断注解。
  P2-B：告警质量当日胜率 vs 近3日滚动胜率并排打架 → 显式分列窗口。
  P2-C：大盘脉搏补数据时点（北京时间）。
  P2-D：精选信号 reason / 驱动因子截断补省略号。
  P2-E：高危信号维度标注（综合 vs Meme 专项）。
  P2-I：解锁「未来14天」补「含今日」。
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


# ── 合成 brief（贴近 2026-09-24 早报的数值形态） ──
def _brief():
    return {
        "M0_tldr": {
            "btc_price": 84380, "btc_change_24h_pct": -2.4,
            "eth_price": 2689, "eth_change_24h_pct": -2.5,
            "fear_greed": 71, "fear_greed_label": "Greed",
            "data_as_of": 1790209801,  # 2026-09-24 00:30:01 UTC = 08:30 CST
        },
        "DIFF": {"total_mcap_pct": -2.7},
        "M0_alert_quality": {
            "report_date": "2026-09-23", "alerts_n": 176,
            "win_1h": 0.5909, "be_1h": 0.5680, "odds_1h": 0.7606, "pf_1h": 1.0986,
            "roll3_win_1h": 0.4453, "roll3_be_1h": 0.4573, "roll3_pf_1h": 0.9557,
            "regime_label": "trend", "severity": "high",
            "conclusion": "阈值-行情失配（规则 A/C/D）…",
        },
        "M0_ai_summary": {"status": "ok", "headline": "结构性偏多", "bias": "偏多",
                          "market_regime": "震荡"},
        "M2_etf_flow": {
            "status": "ok", "latest_date": "2026-09-22",
            "assets": [
                {"symbol": "BTC", "flow_7d_usd": 582.4e6},
                {"symbol": "ETH", "flow_7d_usd": -95.5e6},
                {"symbol": "SOL", "flow_7d_usd": 93.1e6},
                {"symbol": "XRP", "flow_7d_usd": 17.0e6},
            ],
        },
        "M3_highlights": [{
            "target": "ETH", "direction": "long", "conviction_score": 80,
            "ai_analysis_v2": {
                "overall_score": 80, "confidence": "HIGH",
                "reason_summary": "A" * 200,
                "key_drivers": ["B" * 40],
            },
        }],
        "M4_risks": [{"target": "XXXUSDT", "ai_analysis_v2": {"overall_score": 30}}],
        "M8_meme": {"status": "ok", "summary": {"high": 0, "medium": 102,
                                                 "low": 0, "block": 0}},
        "M6_upcoming_unlocks": {
            "status": "ok",
            "unlocks": [{"symbol": "XPL", "unlock_date": "2026-09-25",
                         "unlock_value_usd": 147e6, "unlock_ratio_circulating": 63.2}],
        },
    }


html = sdb.render_brief_html(_brief())

print("[P1-1] ETF 卡片合计口径自洽")
check("全部 ETF 合计" in html, "第三列标注「全部 ETF 合计」")
check("合计净流入" not in html, "旧「合计净流入」标签已移除")
check("分项：BTC +$582M + ETH -$96M + 其他 +$110M" in html,
      "分项拆解 = BTC + ETH + 其他（算术自洽）", html[html.find("分项"):html.find("分项") + 120])
check("SOL +93M" in html and "XRP +17M" in html, "其他资产明细列出")
check("+$597M" in html, "全部 ETF 合计 = 597M（582.4-95.5+93.1+17）")

print("[P1-2] fetch_upcoming_unlocks 优先源口径")
_mm = open(os.path.join(_HERE, "macro_market.py"), encoding="utf-8").read()
check("WHEN e.unlock_ratio_mcap IS NOT NULL\n                                   THEN e.unlock_ratio_mcap" in _mm,
      "流通占比回退链含源市值占比（在实时计算之前）")
check("WHEN e.unlock_ratio_mcap IS NOT NULL THEN 'source'" in _mm,
      "源市值占比标记为 source（不标 ~computed）")

print("[P2-A] AI 方向与当日盘面背离注解")
check("方向偏多属结构性判断" in html, "偏多 + 当日全跌 → 结构性判断注解")

print("[P2-B] 告警质量窗口分列")
check("当日 T+1h 胜率 59.1%" in html, "当日胜率显式标注「当日」")
check("近3日滚动 44.5%" in html, "滚动胜率显式标注「近3日滚动」")
check("失配判定以滚动口径为准" in html, "口径说明（失配以滚动为准）")

print("[P2-C] 大盘脉搏数据时点")
check("数据截至 09-24 08:30（北京时间）" in html, "脉搏补北京时间数据时点")

print("[P2-D] 截断补省略号")
check("A" * 200 not in html, "reason 未整段裸渲染")
check("A" * 119 + "…" in html, "reason 截断补省略号")
check("B" * 29 + "…" in html, "驱动因子截断补省略号")

print("[P2-E] 高危维度标注")
check("今日高危信号（综合风险）" in html, "综合高危标注维度")
check("Meme 风险（Meme 专项）" in html, "Meme 高危标注专项")

print("[P2-I] 解锁措辞")
check("即将解锁（未来14天，含今日）" in html, "「未来14天」补「含今日」")

print("[辅助函数] _clip / _fmt_data_as_of")
check(sdb._clip("abc", 10) == "abc", "短文本不截断")
check(sdb._clip("abcdef", 4) == "abc…", "超长截断补省略号（含边界）")
check(sdb._clip("abc   ", 4) == "abc…", "截断时去尾空白")
check(sdb._clip("ab   ", 5) == "ab   ", "n 内不截断（保留原样）")
check(sdb._clip(None, 5) == "", "None → 空串")
check(sdb._fmt_data_as_of(1790209801) == "09-24 08:30", "epoch → 北京时间",
      sdb._fmt_data_as_of(1790209801))
check(sdb._fmt_data_as_of(None) == "", "None → 空串")
check(sdb._fmt_data_as_of("2026-09-24T00:30:01Z") == "09-24 08:30", "ISO 串兜底")

print("[边界] 无 data_as_of / 非偏多 / 无异动时不误触发")
_b2 = _brief()
_b2["M0_tldr"].pop("data_as_of")
_b2["M0_ai_summary"]["bias"] = "偏空"
_b2["M0_tldr"]["btc_change_24h_pct"] = 1.5
_b2["M0_tldr"]["eth_change_24h_pct"] = 1.2
_b2["DIFF"]["total_mcap_pct"] = 0.5
_b2["M2_etf_flow"] = {"status": "empty", "assets": []}
_h2 = sdb.render_brief_html(_b2)
check("数据截至" not in _h2, "无 data_as_of → 不渲染时点")
check("方向偏空属结构性判断" not in _h2, "非偏多 → 不加注解")
check("全部 ETF 合计" not in _h2, "ETF 空 → 不渲染卡片")

print(f"\n{passed}/{passed + failed} 通过")
sys.exit(1 if failed else 0)
