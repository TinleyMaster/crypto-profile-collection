"""重大事件通道（major_event）离线护栏测试。

覆盖 2026-09-23 新增通道的硬约束：
  1. 通道独立：notification_type 与 A 级 Alert 的三种类型互不重叠（独立去重）
  2. 重要性闸门独立于 tier：判据必须允许 tier='B'（不得只放 'A'）
  3. 事件级去重：一条新闻被多家媒体采成多条 catalyst 时只发一封
     （DISTINCT ON (s.asset_id) + 同资产 24h 已发则跳过）
  4. 负 alpha 类别排除：ai_event_type='market_update'（行情播报）不得入池
  5. 市场确认门槛：prelaunch_ret_24h >= 阈值 且 prelaunch_penalty = 0
  6. 渲染护栏：邮件不含任何交易档位字样、必须含「非交易建议」、
     必须含共振状态与催化方向（避免被读成追高指令）
  7. 失败不阻断主流程：查询异常返回 dict 而非抛异常；无候选不发信
  8. 单轮上限 ≤3（对应「日均 ≤3 条」目标）
  9. 传导逻辑模块（输出层优化）：含「影响传导/预期已消化/传导节奏」、直接度规则映射、
     二阶「板块联动」仅在数据存在时渲染；不再出现内部术语「未被计入降权」

运行: python test_major_event_alert.py
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

# 抽出 _recent_major_events 的 SQL 文本（从 def 到下一个顶层 def）
_m = re.search(
    r"def _recent_major_events\(.*?\n(.*?)\ndef ",
    _SRC, re.S,
)
_QUERY_SRC = _m.group(1) if _m else ""


def _fake_row(**over):
    r = {
        "signal_id": 650174, "catalyst_id": 10571, "asset_id": 1348,
        "tier": "B", "composite_score": 65, "resonance_state": "weak",
        "resonance_score": 62, "kind": "structural",
        "canonical_name": "Bitcoin Cash", "symbol": "BCH",
        "catalyst_title": "CME plans to launch Bitcoin Cash (BCH) and Uniswap (UNI) futures",
        "title_cn": None, "catalyst_body": "CME said it will launch futures on BCH and UNI.",
        "ai_summary": "芝商所计划上线 BCH 与 UNI 期货。",
        "ai_event_type": "listing", "ai_sentiment": "bullish",
        "rule_event_type": "regulation", "source_code": "kol_catalyst_binance_square_7",
        "source_url": "https://example.com/a", "published_at": None,
        "authority_score": 88, "event_weight": 75, "scope_score": 67,
        "prelaunch_ret_24h": 8.2556, "prelaunch_penalty": 0,
        "catalyst_kind": "structural", "tradable": True,
        "impact_direction": "bullish", "impact_strength": "strong",
        "current_price": 620.5, "change_24h": 21.96, "change_7d": 30.1,
        "volume_24h": 1.2e9,
    }
    r.update(over)
    return r


print("== 1. 通道独立（去重互不干扰） ==")
check(N.NTYPE_MAJOR_EVENT == "major_event", "notification_type 常量为 major_event",
      N.NTYPE_MAJOR_EVENT)
check(len({N.NTYPE_MAJOR_EVENT, N.NTYPE_FAST_ALERT, N.NTYPE_SLOW_DIGEST,
           N.NTYPE_SLOW_DIGEST_STOCK}) == 4,
      "major_event 与 fast_alert/slow_digest/slow_digest_stock 互不重叠")

print("== 2. 重要性闸门独立于 tier ==")
check("tier IN ('A', 'B')" in _QUERY_SRC, "候选包含 tier='B'（不因未达 A 而漏报）",
      _QUERY_SRC[:200])
check("s.tier = 'A'" not in _QUERY_SRC and "tier='A'" not in _QUERY_SRC,
      "候选未把 tier 收窄为仅 'A'")
check("s.status = 'open'" in _QUERY_SRC, "仍要求 status='open'（方向/定价闸门未放开）")
check("entry_price" not in _QUERY_SRC and "take_profit" not in _QUERY_SRC,
      "判据不依赖 entry/stop/tp（不因给不出交易计划而漏报）")

print("== 3. 事件级去重 ==")
check("DISTINCT ON (s.asset_id)" in _QUERY_SRC, "每资产只留一条（多 catalyst 归并）")
check("ns.asset_id = s.asset_id" in _QUERY_SRC,
      "24h 冷却按资产判定（代表信号变化也不会重发）")
check("nl.status = 'sent'" in _QUERY_SRC, "只有成功发送才计入冷却（失败可重试）")
check("nl.notification_type = %s" in _QUERY_SRC, "冷却按 notification_type 隔离")

print("== 4. 负 alpha 类别排除 ==")
check("<> 'market_update'" in _QUERY_SRC, "排除行情播报类（负 alpha）")
check("catalyst_kind = ANY(%s::TEXT[])" in _QUERY_SRC, "按 catalyst_kind 白名单过滤")
check(N.MAJOR_EVENT_KINDS == ("structural", "event"),
      "白名单为 structural/event（排除 sentiment/noise）", str(N.MAJOR_EVENT_KINDS))

print("== 5. 市场确认门槛 ==")
check("cg.prelaunch_ret_24h >= %s" in _QUERY_SRC, "要求事件前已异动")
check("cg.prelaunch_penalty = 0" in _QUERY_SRC, "要求异动未被计入降权")
check(N.MAJOR_EVENT_MIN_PRELAUNCH_RET >= 5.0,
      "异动阈值不低于 5%（实测 BCH 为 8.26%）", str(N.MAJOR_EVENT_MIN_PRELAUNCH_RET))
check("published_at > NOW() - " in _QUERY_SRC, "只通报新鲜事件（避免停摆后补发陈旧事件）")

print("== 6. 渲染护栏 ==")
_html = N._build_major_event_html(_fake_row())
check("非交易建议" in _html, "含「非交易建议」声明")
check("重大事件通报" in _html, "含通道标识「重大事件通报」")
_banned = ("止损", "止盈", "入场", "做多", "做空", "entry_price", "stop_loss")
_hit = [k for k in _banned if k in _html]
check(not _hit, "不含任何交易档位/方向指令字样", f"命中: {_hit}")
check("共振状态" in _html, "展示共振状态（避免引导追高）")
check("催化方向" in _html and "利好" in _html, "展示催化方向标签")
check("市场确认" in _html and "+8.26%" in _html, "展示事件前异动幅度")
_subj = N._major_event_subject(_fake_row())
check("重大事件" in _subj and "BCH" in _subj, "主题含通道标识与标的", _subj)
check(len(_subj) <= 90, "主题长度受控（≤90 字）", str(len(_subj)))

print("== 7. 失败不阻断 / 无候选不发信 ==")


class _BoomConn:
    def execute(self, *a, **k):
        raise RuntimeError("boom")


_r = N.send_major_event_alerts(_BoomConn())
check(isinstance(_r, dict) and _r.get("sent") == 0 and _r.get("failed") == 0,
      "查询异常时返回 dict 且 sent=0（不抛异常）", str(_r))


class _EmptyConn:
    def execute(self, *a, **k):
        class _C:
            def fetchall(self):
                return []

            def fetchone(self):
                return None
        return _C()


_r2 = N.send_major_event_alerts(_EmptyConn())
check(_r2 == {"sent": 0, "skipped": 0, "failed": 0, "signals": [], "reason": None},
      "无候选时静默返回（不发空窗邮件）", str(_r2))

print("== 8. 单轮上限 ==")
check(N.MAJOR_EVENT_MAX_PER_RUN <= 3, "单轮上限 ≤3", str(N.MAJOR_EVENT_MAX_PER_RUN))
check("LIMIT %s" in _QUERY_SRC, "SQL 带 LIMIT（上限由参数控制）")
check(N.MAJOR_EVENT_COOLDOWN_HOURS == 24, "事件级冷却 24h", str(N.MAJOR_EVENT_COOLDOWN_HOURS))

print("== 9. 传导逻辑模块（输出层优化护栏） ==")
check("影响传导" in _html and "传导直接度" in _html, "含「影响传导」与「传导直接度」模块")
check("预期已消化" in _html, "含「预期已消化」模块（prelaunch 重解）")
check("传导节奏" in _html and "即时" in _html and "中期" in _html,
      "含「传导节奏」三阶段（即时/短期/中期）")
check("未被计入降权" not in _html, "已去掉内部术语「未被计入降权」")
check("catalyst_second_order" in _QUERY_SRC, "SQL 消费二阶传导表（板块联动数据源）")
# 直接度规则：点名自身 + 自身受益动作 → 直接；仅作生态承载 → 间接
check(N._transmission_directness({
    "symbol": "SKY", "canonical_name": "Sky",
    "title_cn": "Galaxy 购入 SKY 并纳入财库", "ai_summary": ""})[0] == "direct",
    "自身受益动作点名该币 → 直接利好标的")
check(N._transmission_directness({
    "symbol": "SUI", "canonical_name": "Sui",
    "title_cn": "RWA 代币作 Bluefin Lend 抵押品", "ai_summary": ""})[0] == "indirect",
    "仅作生态承载链 → 生态间接受益")
# 二阶联动：有数据才渲染，无数据不臆造
_h_so = N._build_major_event_html(
    _fake_row(second_order_symbols=["AAA", "BBB"], second_order_sector="RWA"))
check("板块联动" in _h_so and "AAA" in _h_so, "有二阶数据时渲染「板块联动」")
check("板块联动" not in _html, "无二阶数据时不渲染「板块联动」（不臆造传导标的）")

print(f"\n{'=' * 50}\n通过 {passed} / 失败 {failed}\n{'=' * 50}")
sys.exit(1 if failed else 0)
