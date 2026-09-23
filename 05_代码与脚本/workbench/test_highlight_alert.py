#!/usr/bin/env python3
"""高亮信号增量邮件提醒 · 离线回归护栏（send_highlight_alert.py）。

运行：python workbench/test_highlight_alert.py
      （纯离线：判定/排序/渲染为纯函数，SQL 与调度注册用源码守卫断言）

覆盖：
  H1 card_key 归一（大小写/空格/主信号类型优先级）
  H2 classify_card 四态：首次新增 / tier 升级 / 共振源数增加 / 无变化
  H3 回落不算升级（HIGH→MED），prev tier 缺失时保守视为升级
  H4 排序键：HIGH 优先 → 新增优先 → 分数降序
  H5 渲染：关键字段齐全 + HTML 转义 + 空列表提示
  H6 源码守卫：加锁 SQL 含冷却窗口与 RETURNING；DDL 幂等；调度已注册
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_BIN = os.path.join(os.path.dirname(_HERE), "scripts", "bin")
sys.path.insert(0, _HERE)
sys.path.insert(0, _SCRIPTS_BIN)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import send_highlight_alert as sha  # noqa: E402

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


def _card(**kw):
    base = {"target": "SOL", "signal_type": "whale_flow", "direction": "long",
            "conviction_tier": "MED", "conviction_score": 60, "resonance_count": 1}
    base.update(kw)
    return base


print("\n[H1] card_key 归一")
check(sha.card_key(_card(target="  sol ")) == "sol|whale_flow", "target 去空格小写归一")
check(sha.card_key(_card(target="SOL", signal_type="whale_flow",
                         signal_types=["whale_flow", "chain_inflow"])) == "sol|whale_flow",
      "signal_type 优先于 signal_types")
check(sha.card_key(_card(signal_type="", signal_types=["chain_inflow"])) == "sol|chain_inflow",
      "signal_type 空 → 回退 signal_types[0]")
check(sha.card_key(_card(signal_type="", signal_types=[])) == "sol|__default__",
      "双空 → 哨兵值 __default__")
check(sha.card_key(_card(target="恐贪指数极度恐惧", signal_type="fng_extreme"))
      == "恐贪指数极度恐惧|fng_extreme", "宏观中文 target 也能成键")

print("\n[H2] classify_card 四态")
check(sha.classify_card(_card(), None) == sha.ALERT_NEW, "无历史 → new")
check(sha.classify_card(_card(conviction_tier="HIGH"),
                        {"tier": "MED", "resonance_count": 1}) == sha.ALERT_UPGRADE,
      "MED → HIGH → upgrade")
check(sha.classify_card(_card(resonance_count=3),
                        {"tier": "MED", "resonance_count": 1}) == sha.ALERT_UPGRADE,
      "共振源数 1 → 3 → upgrade")
check(sha.classify_card(_card(conviction_tier="MED", resonance_count=2),
                        {"tier": "MED", "resonance_count": 2}) is None,
      "tier/共振均无变化 → None")
check(sha.classify_card(_card(conviction_tier="MED", conviction_score=99),
                        {"tier": "MED", "resonance_count": 2}) is None,
      "仅分数波动（无 tier/共振变化）→ None（防刷屏）")

print("\n[H3] 回落与脏数据")
check(sha.classify_card(_card(conviction_tier="MED", resonance_count=1),
                        {"tier": "HIGH", "resonance_count": 5}) is None,
      "HIGH → MED 回落不提醒")
check(sha.classify_card(_card(conviction_tier="HIGH"),
                        {"tier": None, "resonance_count": None}) == sha.ALERT_UPGRADE,
      "prev tier/resonance 缺失 → 保守判为 upgrade")
_dirty = sha.classify_card(_card(conviction_tier="MED"),
                           {"tier": "garbage", "resonance_count": "x"})
check(_dirty == sha.ALERT_UPGRADE, "prev 脏值不抛错：MED(1) > garbage(0) → upgrade", f"实际 {_dirty}")

print("\n[H4] 排序键")
items = [
    (_card(target="A", conviction_tier="MED", conviction_score=90), sha.ALERT_NEW),
    (_card(target="B", conviction_tier="HIGH", conviction_score=55), sha.ALERT_UPGRADE),
    (_card(target="C", conviction_tier="HIGH", conviction_score=80), sha.ALERT_NEW),
    (_card(target="D", conviction_tier="MED", conviction_score=99), sha.ALERT_UPGRADE),
]
order = [c["target"] for c, _ in sorted(items, key=sha.card_sort_key, reverse=True)]
check(order == ["C", "B", "A", "D"], f"HIGH 优先→新增优先→分数降序，实际 {order}")
check(order.index("C") == 0 and order.index("D") == 3, "MED 排在 HIGH 之后")
check(sha.card_sort_key((_card(decayed_score=10, conviction_score=99), sha.ALERT_NEW))
      < sha.card_sort_key((_card(decayed_score=99, conviction_score=10), sha.ALERT_NEW)),
      "decayed_score 存在时优先于 conviction_score 排序")

print("\n[H5] 渲染")
card = _card(target="SOL", key_metric="$12.4M 巨鲸转账", trigger_logic="3 笔 >$1M 转入冷钱包",
             action_hint="回踩 EMA20 分批", invalidation="跌破 $138",
             event_strength=88, resonance_count=3, conviction_strength=72,
             resonance_bonus=12, regime_mult=1.12, related_dims=["netflow", "roi"],
             all_signals=[{"signal_type": "whale_flow", "direction": "long",
                           "conviction_score": 80, "key_metric": "$12.4M"},
                          {"signal_type": "chain_inflow", "direction": "long",
                           "conviction_score": 70, "trigger_logic": "净流入 7d 转正"}],
             ai_analysis_v2={"overall_score": 71, "confidence": "medium",
                             "reason_summary": "链上吸筹 + 估值偏低",
                             "dimensions": {"valuation": {"score": 72}, "onchain": {"score": 75}}})
html = sha.render_html([(card, sha.ALERT_NEW)], "2026-09-23", 8)
for token in ["⚡ 高亮信号提醒", "🆕 新增", "SOL", "conv 60", "$12.4M 巨鲸转账",
              "3 笔 &gt;$1M 转入冷钱包", "回踩 EMA20 分批", "跌破 $138", "事件强度 88",
              "共振×3", "来源维度：链上净流, ROI动量", "全部信号原由（2个）",
              "链上净流入", "AI 综合 71", "估值 72 · 链上 75", "链上吸筹 + 估值偏低"]:
    check(token in html, f"渲染含「{token}」")
check("当日高亮池共 8 条" in html, "抬头含高亮池总量")
check("新增 1 条 · 升级 0 条" in html, "抬头统计行口径正确")
check(html.count("🆕 新增") == 1, "卡片徽章含「🆕 新增」")

# 生产口径 related_dims（数据表名 / 管线编号）必须映射成人话，不得原样透出
_src_dims = sha.render_html([(_card(related_dims=["机构ETF资金流（cryptoetf.today）", "P1 机构行为",
                                                 "catalyst_events", "P0-B 催化剂驱动",
                                                 "P1-1 叙事榜（市值）", "P1-1 链净流入榜",
                                                 "asset_raises", "P1 融资落地",
                                                 "onchain_transfer_log", "P1-3 链上巨鲸",
                                                 "asset_unlock_event", "P1 解锁抛压",
                                                 "mvrv_universe", "P0-1 估值回归",
                                                 "P1-2 多空博弈"]), sha.ALERT_NEW)],
                                        "2026-09-23", 1)
for raw in ["cryptoetf.today", "catalyst_events", "asset_raises", "onchain_transfer_log",
            "asset_unlock_event", "mvrv_universe", "P0-B", "P1-1", "P1-3"]:
    check(raw not in _src_dims, f"内部来源「{raw}」未泄漏到展示层")
check("来源维度：机构 ETF 资金流" in _src_dims, "生产来源映射为中文标签")
check(_src_dims.count("机构 ETF 资金流") == 1 and "机构行为" in _src_dims, "同卡多来源逐个映射")
_unmapped = sha.render_html([(_card(related_dims=["P1-9 未来新榜"]), sha.ALERT_NEW)], "2026-09-23", 1)
check("P1-9" not in _unmapped and "未来新榜" in _unmapped, "未登记来源剥离管线编号前缀")

xss = sha.render_html([(_card(target="<script>alert(1)</script>"), sha.ALERT_NEW)], "2026-09-23", 1)
check("<script>" not in xss, "target 中的 HTML 被转义（无裸 <script>）")
check("&lt;script&gt;" in xss, "转义后为实体")

empty = sha.render_html([], "2026-09-23", 3)
check("无新增/升级高亮信号" in empty, "空列表有明确提示")

# AI 四态披露：认可 / 未背书 / 复核失败 / 未复核，任一态都不得静默
downgraded = sha.render_html([(_card(_ai_downgraded=True,
                                     _ai_filter_reason="事件驱动信号触发: funding",
                                     ai_analysis_v2={"overall_score": 30}),
                               sha.ALERT_UPGRADE)], "2026-09-23", 1)
check("AI 未背书" in downgraded and "⬆️ 升级" in downgraded, "AI 降级卡片披露「AI 未背书」且仍展示升级徽章")
check("funding" not in downgraded, "降级卡片不透传内部 signal_type token")
check("AI 综合 30" in downgraded, "降级卡片带出 AI 综合分")
_not_reviewed = sha.render_html([(_card(_ai_filter_reason="聚合/宏观信号，跳过全量画像"), sha.ALERT_NEW)],
                                "2026-09-23", 1)
check("AI 未复核" in _not_reviewed, "AI 未复核卡片显式披露（不静默）")
check("跳过全量画像" not in _not_reviewed, "未复核原因改写成人话")
_no_asset = sha.render_html([(_card(_ai_skipped_no_asset=True,
                                    _ai_filter_reason="asset_id 解析失败，跳过全量画像"),
                              sha.ALERT_NEW)], "2026-09-23", 1)
check("AI 未复核" in _no_asset and "asset_id" not in _no_asset, "资产未匹配卡片披露且不出现内部字段名")
_ai_err = sha.render_html([(_card(ai_analysis_v2={"error": "timeout"}), sha.ALERT_NEW)], "2026-09-23", 1)
check("AI 复核失败" in _ai_err, "AI 复核失败卡片显式披露（不静默）")
check(sha.render_html([(_card(), sha.ALERT_NEW)], "2026-09-23", 1).count("<script") == 0, "正常卡片无脚本注入")

print("\n[H6] 源码守卫")
_sha_src = open(os.path.join(_SCRIPTS_BIN, "send_highlight_alert.py"), encoding="utf-8").read()
check("ON CONFLICT (card_key, alert_kind)" in _sha_src, "加锁用 UNIQUE(card_key, alert_kind) 冲突键")
check("RETURNING log_id" in _sha_src, "加锁返回 log_id（无返回=未获权）")
check("INTERVAL '1 hour'" in _sha_src and "INTERVAL '1 minute'" in _sha_src,
      "冷却窗口（小时）+ 残留 sending 锁超时（分钟）齐备")
check("status = 'failed'" in _sha_src, "失败记录允许下一轮重试")
check(_sha_src.count("CREATE TABLE IF NOT EXISTS") == 1 and "IF NOT EXISTS" in _sha_src,
      "DDL 幂等（IF NOT EXISTS）")
check("payload" in _sha_src and "highlight_signals" in _sha_src,
      "数据源为快照 payload 的 highlight_signals（只读，不重算）")

_mm = open(os.path.join(os.path.dirname(_HERE), "scripts", "migrations",
                        "fix_067_highlight_alert_log.sql"), encoding="utf-8").read()
check("UNIQUE (card_key, alert_kind)" in _mm, "迁移表含 UNIQUE(card_key, alert_kind)")
check("biz.highlight_alert_log" in _mm, "迁移表名正确")

_sched = open(os.path.join(_HERE, "scheduler.py"), encoding="utf-8").read()
check('"highlight_alert"' in _sched and '"send_highlight_alert.py"' in _sched,
      "scheduler.py 已注册 highlight_alert")
check('("highlight_alert", "5 * * * *"' in _sched, "cron 为每小时 05 分")

print(f"\n{'=' * 60}\n通过 {passed} / 失败 {failed}\n{'=' * 60}")
sys.exit(1 if failed else 0)