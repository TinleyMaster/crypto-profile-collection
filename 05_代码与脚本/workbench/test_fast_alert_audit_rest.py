"""A 级快讯邮件「剩余 11 项」离线护栏测试。

审计来源：审计_催化剂A级邮件_XRP_BCH_2026-09-24.md（P1-D3/D4/D5、P2-D6/D7/D8/D9/D10/D12）。
P0-D1/D2 的护栏已在 test_fast_alert_ai_veto.py 覆盖，本文件只覆盖其余项。

覆盖硬约束：
  D3  资产简介里 CMC 拼的过期行情句必须剥掉（否则与实时价格区块同屏打架）
  D4  量比缺失不得伪造 0.00x「极度缩量」；三条 SQL 的行情列口径必须同源
  D5  流动性是「单链 DEX 池快照」，标签须带链、SQL 须确定性取最大池、
      prompt 须显式禁止据此断言资产稀薄
  D6  共振分与共振状态两套口径须在展示层说明
  D7  「模型方向置信」须与 AI「信心度」在标签上区分
  D8  资产错配成因须由 AI 动态给出，不得硬编码「ticker同名但不同项目」
  D9  规则档位须标注「未与 AI 风控建议校准」
  D10 赛道须标注 CMC 分类口径；上线时间缺失须显示「未收录」
  D12 标题 symbol 与 canonical_name 相同时须去重

运行: python test_fast_alert_audit_rest.py
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
_AE_SRC = open(
    os.path.join(_here, "catalyst", "ai_enhance.py"), encoding="utf-8"
).read()

# 三条使用共享行情列的 SQL 片段（发送首查 / 发送重查 / AI 评审输入）
_SQL_BLOCKS = re.findall(
    r'conn\.execute\(\s*"""(.*?)"""\s*\+\s*_MARKET_COLS_SQL', _SRC, re.S
)
_MARKET_COLS = N._MARKET_COLS_SQL
_MARKET_LATERAL = N._MARKET_LATERAL_SQL


def _row(**over):
    """最小可渲染行（字段名与 SQL 输出一致）。"""
    r = {
        "signal_id": 1085989, "tier": "A", "composite_score": 86, "kind": "structural",
        "asset_id": 1127, "canonical_name": "XRP", "symbol": "XRP",
        "asset_type": "coin", "primary_sector": "l1", "categories": ["Layer 1 (L1)"],
        "market_cap": 9.091e10, "market_cap_rank": 5,
        "circulating_supply": 5.9e10, "total_supply": 9.999e10,
        "ath_usd": 3.65, "launch_date": None,
        "description_short": (
            "XRP is the native asset of the XRP Ledger. "
            "The last known price of XRP is 1.08727528 USD and is up 3.13 over the "
            "last 24 hours. It is currently trading on 2020 active market(s) with "
            "$310,493,897.96 traded over the last 24 hours. More information can be "
            "found at https://xrpl.org/."
        ),
        "catalyst_title": "Bitcoin Cash jumped 28% after CME said it will list BCH and UNI",
        "title_cn": None, "source_code": "kol_catalyst_binance_square_7",
        "catalyst_summary": "CME 计划上线 BCH 与 UNI 期货。", "ai_summary": None,
        "entry_price": 1.5788, "stop_loss": 1.4079, "take_profit": 2.0061,
        "rr_ratio": 2.5, "investment_cycle": "中期", "ai_reason": None,
        "ai_deep_review": None, "technical_state": "up", "resonance_state": "weak",
        "invalidation": "跌破止损", "persistence": "structural",
        "base_strength": 79, "resonance_score": 90, "confidence": 0.86,
        "regime": "neutral", "risk_labels": [],
        "current_price": 1.4995, "change_24h_pct": -5.02, "change_7d_pct": 15.37,
        "volume_24h_usd": 7.969e9, "avg_volume_7d": 4.55e9, "volume_ratio_7d": 1.75,
        "is_volume_spike": False,
        "liquidity_score": 1913193.87, "liquidity_chain": "solana",
        "liquidity_source": "geckoterminal",
    }
    r.update(over)
    return r


def _ai(**over):
    a = {
        "asset_match_confidence": "low",
        "asset_match_reason": "事件主体是 BCH/UNI，XRP 仅为同帖被动提及",
        "verdict": "不建议参与", "confidence_level": "低",
        "position_suggestion": "0% 仓位（空仓观望）",
        "core_logic": "该催化剂的受益标的是 BCH/UNI，XRP 属被动提及。",
        "key_risks": ["资产错配"], "catalyst_stage": "兑现后",
        "timing_advice": "观望", "stop_loss_advice": "收紧至 1.50 下方",
        "take_profit_advice": "分批止盈", "overall_review": "不建议参与。",
    }
    a.update(over)
    return a


print("== 1. D3 资产简介剥除过期行情句 ==")
_s = N._strip_stale_price_sentences
check(_s(None) == "" and _s("") == "", "空/None 输入返回空串")
check("last known price" not in _s(_row()["description_short"]),
      "剥离「last known price」句")
check("1.08727528" not in _s(_row()["description_short"]), "剥离 stale 价格数字")
check("3.13" not in _s(_row()["description_short"]), "剥离 stale 涨跌幅")
check("active market" not in _s(_row()["description_short"]), "剥离 stale 成交量句")
check("XRP is the native asset" in _s(_row()["description_short"]),
      "保留项目介绍本身")
check(_s("XRP is a payment-focused L1.") == "XRP is a payment-focused L1.",
      "无反例误伤：正常简介原样保留")
check(_s("中文简介，无行情句。") == "中文简介，无行情句。", "中文简介不受影响")
_html_desc = N._build_fast_alert_html(_row())
check("1.08727528" not in _html_desc, "渲染层已剥离 stale 价格")
check("$1.08727528" not in _html_desc and "1.09" not in _html_desc,
      "渲染层无 stale 价格残影")

print("== 2. D4 量比缺失不得伪造「0.00x 极度缩量」 ==")
# 盘面异动速览卡只在 AI 深度评审区块内渲染，故需带一份非否决的评审
_AI_OK = _ai(asset_match_confidence="high", verdict="建议轻仓参与")
_card = N._build_fast_alert_html(
    _row(volume_ratio_7d=None, ai_deep_review=_AI_OK)
)
check("量比 (24h/7d)" in _card, "量比缺失但 24h 成交量存在时仍渲染卡片")
check("0.00x" not in _card, "不出现伪造的 0.00x")
check("极度缩量" not in _card, "不出现「极度缩量」误标")
check("无法计算" in _card, "如实标注无法计算")
_card_ok = N._build_fast_alert_html(_row(volume_ratio_7d=1.75, ai_deep_review=_AI_OK))
check("1.75x" in _card_ok and "温和放量" in _card_ok, "有真值时正常渲染 1.75x")
_card_low = N._build_fast_alert_html(_row(volume_ratio_7d=0.2, ai_deep_review=_AI_OK))
check("0.20x" in _card_low and "极度缩量" in _card_low,
      "真值为 0.2 时「极度缩量」判定仍生效（未误伤）")
_card_none = N._build_fast_alert_html(
    _row(volume_ratio_7d=None, volume_24h_usd=None, ai_deep_review=_AI_OK)
)
check("量比 (24h/7d)" not in _card_none, "量比与成交量双缺时整卡不渲染")

print("== 3. D4 三条 SQL 行情列口径同源 ==")
check(_SRC.count("AS volume_ratio_7d") == 1,
      "volume_ratio_7d 只在共享片段里定义一次（不再三处手写）",
      f"count={_SRC.count('AS volume_ratio_7d')}")
check(_SRC.count("_MARKET_COLS_SQL +") == 3,
      "三条 SQL 均引用 _MARKET_COLS_SQL",
      f"count={_SRC.count('_MARKET_COLS_SQL +')}")
check(_SRC.count("_MARKET_LATERAL_SQL +") == 3, "三条 SQL 均引用 _MARKET_LATERAL_SQL")
check(len(_SQL_BLOCKS) == 3, "正则定位到 3 处使用点", str(len(_SQL_BLOCKS)))
check("LIMIT 7" in _MARKET_LATERAL and "ORDER BY md2.market_date DESC" in _MARKET_LATERAL,
      "7 日均量窗口统一为「最近 7 个交易日」")
check("AS is_volume_spike" in _MARKET_COLS, "is_volume_spike 也收进共享片段")
check("md_avg.avg_volume_7d" in _MARKET_COLS, "avg_volume_7d 由共享片段提供")

print("== 4. D5 链上池流动性口径 ==")
check("ORDER BY al.total_liquidity_usd DESC NULLS LAST" in _MARKET_LATERAL,
      "流动性取数确定性排序（原 LIMIT 1 无 ORDER BY ⇒ 取哪条不确定）")
check("al.chain" in _MARKET_LATERAL and "al.source" in _MARKET_LATERAL,
      "流动性同时带出 chain/source 供标注口径")
check(N._liquidity_label({"liquidity_chain": "solana"}) == "链上池流动性（solana）",
      "标签带链名")
check(N._liquidity_label({}) == "链上池流动性", "无链名时回退不带括号")
_html_liq = N._build_fast_alert_html(_row(ai_deep_review=_AI_OK))
check("链上池流动性（solana）" in _html_liq, "渲染层标签带链")
check("流动性（24h）" not in _html_liq, "不再使用会被读成全局流动性的旧标签")
check("流动性（24h 总流动性）" not in _AE_SRC, "prompt 不再称其为「总流动性」")
check("链上池流动性快照" in _AE_SRC, "prompt 已改口径名")
check("不得" in _AE_SRC and "流动性稀薄" in _AE_SRC,
      "prompt 显式禁止据此断言资产稀薄")

print("== 5. D6 共振分与共振状态口径说明 ==")
check("与上方共振分不同口径" in _html_liq, "渲染层说明两套口径不同")
check("弱共振" in _html_liq, "共振状态仍正常展示")

print("== 6. D7 双置信度标签区分 ==")
check("模型方向置信" in _html_liq, "改名为「模型方向置信」")
check("· 置信度" not in _html_liq, "不再出现易被读作「系统确信度」的裸标签")
check("信心度" in _html_liq, "AI 侧「信心度」标签保留（两者并存但已可区分）")

print("== 7. D8 资产错配成因动态化 ==")
_html_reason = N._build_fast_alert_html(_row(ai_deep_review=_ai()))
check("ticker同名但不同项目" not in _html_reason, "不再硬编码「ticker同名」成因")
check("被动提及" in _html_reason, "展示 AI 给出的具体错配原因")
check("AI 判定原因" in _html_reason, "原因带来源标识")
_html_noreason = N._build_fast_alert_html(
    _row(ai_deep_review=_ai(asset_match_reason=""))
)
check("ticker同名但不同项目" not in _html_noreason, "无原因时也不臆断")
check("请谨慎核实后再做决策" in _html_noreason, "无原因时回退通用文案")
check("asset_match_reason" in _AE_SRC, "prompt schema 已新增 asset_match_reason")
check("被动提及" in _AE_SRC and "跨链同名" in _AE_SRC,
      "prompt 已扩展 low 的定义（不止 ticker 撞名）")
check('"asset_match_reason": str(data.get' in _AE_SRC,
      "ai_enhance 输出白名单已放行 asset_match_reason")

print("== 8. D9 规则档位与 AI 风控口径标注 ==")
_html_plan = N._build_fast_alert_html(
    _row(ai_deep_review=_ai(asset_match_confidence="high", verdict="建议轻仓参与"))
)
check("未与 AI 风控建议校准" in _html_plan, "交易计划块标注口径差异")
check("交易计划（规则计算）" in _html_plan, "标题标明是规则计算")
check("📈 做多" in _html_plan, "非否决时档位仍正常展示（未误伤）")
check("入场价" in _html_plan and "止损价" in _html_plan, "档位字段完整")
_html_veto = N._build_fast_alert_html(_row(ai_deep_review=_ai()))
check("交易计划：已抑制" in _html_veto, "P0-D1/D2 否决闸门未回归")
check("未与 AI 风控建议校准" not in _html_veto, "被否决时不展示档位说明")

print("== 9. D10 赛道来源 / 上线时间 ==")
check("CMC 分类口径" in _html_liq, "赛道标注数据来源")
check("未收录" in _html_liq, "上线时间缺失时显示「未收录」而非裸「—」")
_html_launch = N._build_fast_alert_html(_row(launch_date="2012-08-01"))
check("2012-08-01" in _html_launch and "未收录" not in _html_launch,
      "有上线时间时正常展示")

print("== 10. D12 标题去重 ==")
check(">XRP / XRP<" not in _html_liq, "symbol 与 canonical_name 相同时去重")
check(">XRP<" in _html_liq, "去重后仍展示标的")
_html_diff = N._build_fast_alert_html(_row(canonical_name="Bitcoin Cash", symbol="BCH"))
check("BCH / Bitcoin Cash" in _html_diff, "两者不同时仍并列展示")

print("== 11. 返回契约一致 ==")
check('"suppressed": 0, "skipped": len(new_signal_ids)' in _SRC,
      "无候选早退分支含 suppressed 键")
check(_SRC.count('"suppressed": 0') >= 2, "两处早退分支均已对齐键集")
check("'suppressed'" in _SRC or '"suppressed"' in _SRC, "正常返回含 suppressed")

print(f"\n{'=' * 50}\n通过 {passed} / 失败 {failed}\n{'=' * 50}")
sys.exit(1 if failed else 0)
