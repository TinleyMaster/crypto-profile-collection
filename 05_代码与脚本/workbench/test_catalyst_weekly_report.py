"""催化剂周报（weekly_report，叙事型）离线护栏测试。

覆盖 2026-09-29 新增「每周一 09:00 催化剂周报」的硬约束：
  1. 通道独立：notification_type=weekly_report 与既有 5 种类型互不重叠（独立去重）
  2. 负号哨兵：SENTINEL_WEEKLY_REPORT_SIGNAL_ID = -4（与 -1/-2/-3 互异）
  3. 自然周窗口：北京时区，上周一 00:00 ~ 本周一 00:00，恰好 7 天
  4. 概览统计 SQL：事件类型/情感用 COALESCE 统一口径，含 tier/来源分布
  5. 重要事件 SQL（2026-09-29 重建「市场显著性」口径）：素材取自
     biz.asset_catalyst 原始事件（含无 asset_id 的宏观/监管类），关键词兜底归类
     security/etf/macro，剔除 market_update，按 importance 降序；
     **不**按 status='open' 过滤（confirmed 的 watch 事件正是周报素材）
  5b. Python 侧：同一事件多来源去重、广度加成、单类型配额、无标的占位标的
  6. 叙事链路：LLM 优先、失败回退模板拼装（不阻断）；events 为空时 LLM 直接跳过
  7. 自然周去重：本周已发过 → skipped；发送锁拒绝 → skipped；dry-run 不占去重位
  8. 渲染护栏：含「本周总述 / 主线主题 / 大事记与影响」与影响解读，
     不含运维告警字样、不含交易档位指令字样（区别于 Alert 通道）
  9. 失败不阻断：统计/查询异常返回 dict 而非抛异常

运行: python test_catalyst_weekly_report.py
"""
import datetime as _dt
import os
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


print("== 1. 通道独立 ==")
check(N.NTYPE_WEEKLY_REPORT == "weekly_report", "notification_type 常量为 weekly_report",
      N.NTYPE_WEEKLY_REPORT)
check(len({N.NTYPE_WEEKLY_REPORT, N.NTYPE_CHANNEL_SILENCE, N.NTYPE_MAJOR_EVENT,
           N.NTYPE_FAST_ALERT, N.NTYPE_SLOW_DIGEST, N.NTYPE_SLOW_DIGEST_STOCK}) == 6,
      "weekly_report 与既有 5 种类型互不重叠（独立去重）")

print("== 2. 负号哨兵 ==")
check(N.SENTINEL_WEEKLY_REPORT_SIGNAL_ID == -4,
      "哨兵为 -4（与 -1/-2/-3 互异，NULL 不触发 UNIQUE）",
      str(N.SENTINEL_WEEKLY_REPORT_SIGNAL_ID))
check(len({N.SENTINEL_WEEKLY_REPORT_SIGNAL_ID, N.SENTINEL_CHANNEL_SILENCE_SIGNAL_ID,
           -1, -2}) == 4, "哨兵集合 {-4,-3,-1,-2} 互不重复")
check(N.WEEKLY_EVENT_LIMIT >= 10, "LLM 事件上限 ≥10（保证素材量）",
      str(N.WEEKLY_EVENT_LIMIT))
check(0 < N.WEEKLY_MAX_PER_TYPE < N.WEEKLY_EVENT_LIMIT,
      "单类型配额 < 总上限（防止某一类淹没清单）",
      f"cap={N.WEEKLY_MAX_PER_TYPE} limit={N.WEEKLY_EVENT_LIMIT}")
check(N._WEEKLY_TYPE_WEIGHT["security"] > N._WEEKLY_TYPE_WEIGHT["listing"],
      "周报权重：security > listing（市场显著性 ≠ 可交易性）",
      str(N._WEEKLY_TYPE_WEIGHT.get("security")))

print("== 3. 自然周窗口 ==")
_tz = _dt.timezone(_dt.timedelta(hours=8))
_end = _dt.datetime(2026, 9, 29, 12, 0, tzinfo=_tz)
_start, _end_utc, _label = N._weekly_window(_end)
check((_end_utc - _start).total_seconds() == 7 * 86400, "窗口恰好 7 天",
      f"start={_start} end={_end_utc}")
check(_start.astimezone(_tz).weekday() == 0 and _start.astimezone(_tz).hour == 0,
      "起点为北京时区周一 00:00", str(_start.astimezone(_tz)))
check(_end_utc.astimezone(_tz).weekday() == 0 and _end_utc.astimezone(_tz).hour == 0,
      "终点为北京时区周一 00:00", str(_end_utc.astimezone(_tz)))
check(_label.endswith("（北京）") and "~" in _label, "label 含「~」与「（北京）」", _label)


class _CaptureConn:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

        class _C:
            def fetchone(self):
                return {"cnt": 0}

            def fetchall(self):
                return []
        return _C()


print("== 4. 概览统计 SQL ==")
_c = _CaptureConn()
_stats = N._weekly_overview_stats(_c, _start, _end_utc)
_all_sql = "\n".join(s for s, _ in _c.calls)
check(set(_stats) == {"signals_total", "tier_dist", "event_type_dist",
                      "sentiment_dist", "source_dist", "catalysts_new"},
      "返回 6 个统计键", str(list(_stats)))
check("COALESCE(ac.ai_event_type, ac.rule_event_type, 'other')" in _all_sql,
      "事件类型统一口径 COALESCE(ai/rule/'other')")
check("COALESCE(ac.ai_sentiment, 'neutral')" in _all_sql, "情感统一口径 COALESCE(ai/'neutral')")
check("GROUP BY tier" in _all_sql, "含 tier 分布 GROUP BY")
check("ac.source_code AS key" in _all_sql and "GROUP BY 1" in _all_sql,
      "含来源分布（source_code AS key + GROUP BY 1）")

print("== 5. 重要事件 SQL（重建口径 2026-09-29） ==")
_c2 = _CaptureConn()
N._weekly_key_events(_c2, _start, _end_utc)
_sql = _c2.calls[0][0]
_params = _c2.calls[0][1]
check("FROM biz.asset_catalyst ac" in _sql,
      "素材取自 biz.asset_catalyst 原始事件（含无 asset_id 的宏观/监管类）")
check("LEFT JOIN LATERAL" in _sql and "FROM biz.catalyst_signal si" in _sql,
      "signal 表降级为左连补充（tier/resonance），不再做过滤底盘")
check("(s.tier = 'A' OR s.resonance_state = 'confirmed')" not in _sql,
      "废弃旧口径 tier='A' OR confirmed（tier 是可交易性，非重要性）")
check("COALESCE(s.resonance_state, '') <> 'divergent'" in _sql,
      "排除 divergent（方向背离无有效影响）")
check("'hack|exploit|stolen|steal|breach|drain|被盗|被黑|遭攻击|漏洞|rug ?pull'" in _sql,
      "黑客/被盗类关键词兜底归类 security（库中误落 other）")
check("WHEN head ~ 'etf' THEN 'etf'" in _sql,
      "ETF 类关键词兜底归类（库中误落 market_update）")
check("head ~ '美联储|federal reserve|rate hike|rate cut|加息|降息" in _sql,
      "宏观（美联储/利率）关键词兜底归类（库中误落 market_update）")
check("COALESCE(ac.title_cn, '') || ' ' || COALESCE(ac.title, '')) AS head" in _sql,
      "关键词匹配字段 head 仅由标题构成（不含 ai_summary）", "")
check("NOT IN ('', 'null', 'none', 'nan', 'tl;dr')" in _sql,
      "剔除占位标题（LLM 漏译写成字面量 'null'）")
check("!~ '^(今日要闻|要闻预告|要闻提示|一、|二、|热点新闻|行情" in _sql,
      "剔除「要闻汇总/日报」类无单一事件的聚合帖")
check("WHERE category <> 'market_update'" in _sql,
      "剔除 market_update 纯行情播报（窗口占 38%）")
check("WHEN 'security' THEN 95" in _sql and "WHEN 'listing' THEN 62" in _sql,
      "周报权重表：security 最高权、listing 降权（区别于 grade 的可交易性权重）")
check("WHEN raw_asset_id IS NULL THEN 0.75" in _sql,
      "无标的的宏观/监管事件按「市场级」计权（不再因无 asset_id 被埋没）")
check("ORDER BY importance DESC" in _sql and "LIMIT %s" in _sql,
      "按 importance 降序截断（单一排序键，无 OR/LIMIT 吞没问题）")
check(_params[2] == max(N.WEEKLY_EVENT_LIMIT * 10, 400),
      "候选池放大后续由 Python 去重/配额收敛", str(_params))
check("s.status = 'open'" not in _sql,
      "**不**按 status='open' 过滤（confirmed 的 watch 事件是周报素材）")


class _RowsConn:
    """返回预设行的假连接（用于验证 Python 侧去重/配额/占位标的逻辑）。"""
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, params=None):
        rows = self.rows

        class _C:
            def fetchall(_self):
                return rows
        return _C()


print("== 5b. 去重 / 单类型配额 / 占位标的 ==")
_dup = [
    {"catalyst_id": 1, "signal_id": 11, "event_type": "regulation", "importance": 80.0,
     "composite_score": 50, "symbol": "BTC", "title_cn": "《CLARITY法案》在参议院受阻",
     "sentiment": "bearish"},
    {"catalyst_id": 2, "signal_id": 12, "event_type": "regulation", "importance": 78.0,
     "composite_score": 40, "symbol": "ETH", "title_cn": "《CLARITY法案》在参议院受阻",
     "sentiment": "bearish"},
    {"catalyst_id": 3, "signal_id": None, "event_type": "macro", "importance": 88.0,
     "composite_score": None, "symbol": None, "title_cn": "美联储加息 25 个基点",
     "sentiment": "bearish"},
]
_ev = N._weekly_key_events(_RowsConn(_dup), _start, _end_utc)
check(len(_ev) == 2, "同一事件多来源重复入库 → 去重为 1 条", str(len(_ev)))
check(_ev[0]["event_type"] == "macro" and _ev[0]["symbol"] == "宏观",
      "无标的的宏观事件给可读占位标的，且按 importance 居首",
      str({k: _ev[0].get(k) for k in ("event_type", "symbol", "importance")}))
_reg = [d for d in _ev if d["event_type"] == "regulation"][0]
check(_reg["symbols"] == ["BTC", "ETH"], "去重后聚合同一事件的多个标的", str(_reg["symbols"]))
check(_reg["importance"] == 83.0, "市场级事件获得广度加成（80 + 3）", str(_reg["importance"]))

_cap_rows = [{"catalyst_id": 100 + i, "signal_id": 200 + i, "event_type": "listing",
              "importance": 40.0, "composite_score": 30, "symbol": "AAA",
              "title_cn": f"某交易平台上新公告第 {i} 号", "sentiment": "bullish"}
             for i in range(12)]
_cap = N._weekly_key_events(_RowsConn(_cap_rows), _start, _end_utc)
check(len(_cap) == N.WEEKLY_MAX_PER_TYPE,
      "单类型配额生效（12 条 listing 仅保留 WEEKLY_MAX_PER_TYPE 条）", str(len(_cap)))

_k = N._weekly_story_key
check(_k({"title_cn": "ChainCatcher 消息，Payy Network 遭攻击"})
      == _k({"title_cn": "火星财经消息，Payy Network 遭攻击"}),
      "剥离来源署名前缀后同一事件归并（跨来源转述）")
check(_k({"title_cn": "PANews 9月23日消息，据 SoSoValue 数据，XRP 现货 ETF 单日净流入"})
      == _k({"title_cn": "ChainCatcher 消息，据 SoSoValue 数据，XRP 现货 ETF 单日净流入"}),
      "剥离来源 + 日期前缀后归并")
check(_k({"title_cn": "BTC 现货 ETF 创纪录净流入"}) != _k({"title_cn": "ETH 升级完成"}),
      "不同事件不误归并")
check(_k({"title_cn": "null"}) == "", "占位标题 'null' → 空键（回退 catalyst_id）")

print("== 6. 叙事链路 ==")
check(N._weekly_llm_narrative([], {"signals_total": 0}, "w") is None,
      "无事件时 LLM 直接跳过（返回 None）")
_fb = N._weekly_fallback_narrative([{
    "symbol": "BTC", "title_cn": "比特币现货ETF获批", "title": "ETF",
    "ai_summary": "机构资金可通过合规通道流入，中期利好流动性。",
    "sentiment": "bullish",
}])
check(_fb["source"] == "fallback", "回退叙事 source=fallback", str(_fb["source"]))
check(len(_fb["events"]) == 1 and _fb["events"][0]["symbol"] == "BTC",
      "回退叙事按事件逐条组装", str(_fb["events"]))
check(_fb["events"][0]["impact"].startswith("机构资金"),
      "回退叙事 impact 复用 ai_summary", _fb["events"][0]["impact"])
check(_fb["themes"] == [], "回退叙事无主题分组（明确降级）")

_brief = N._weekly_events_brief([{
    "symbol": "BTC", "canonical_name": "Bitcoin", "primary_sector": "L1",
    "event_type": "regulatory", "sentiment": "bullish", "tier": "A",
    "resonance_state": "weak", "composite_score": 88.5, "created_at": _end,
    "title_cn": "标题", "ai_summary": "摘要",
}])
check(set(_brief[0]) == {"symbol", "symbols", "name", "sector", "event_type",
                         "sentiment", "tier", "resonance_state", "score",
                         "published_at", "title", "summary"},
      "LLM 输入字段集固定（去噪，含 symbols 供主题聚合）", str(sorted(_brief[0])))
check(_brief[0]["score"] == 88.5, "score 数值化（非字符串）", str(_brief[0]["score"]))

print("== 7. 发送流程 ==")
_now = _dt.datetime.now(_dt.timezone.utc)
_orig = {k: getattr(N, k) for k in
         ("ensure_notification_table", "_weekly_window",
          "_weekly_overview_stats", "_weekly_key_events",
          "_weekly_report_already_sent", "_weekly_llm_narrative",
          "_weekly_fallback_narrative", "_try_acquire_send_lock",
          "_send_email", "_mark_sent")}


def _reset():
    for k, v in _orig.items():
        setattr(N, k, v)


def _stats_fake():
    return {"signals_total": 12, "tier_dist": [("A", 3), ("B", 9)],
            "event_type_dist": [("listing", 5)],
            "sentiment_dist": [("bullish", 7)],
            "source_dist": [("cmc", 12)], "catalysts_new": 30}


_EVENTS = [{"symbol": "BTC", "composite_score": 90}]


def _narr_llm():
    return {"overview": "本周主线为监管与ETF。",
            "themes": [{"name": "ETF 与机构资金", "summary": "资金面改善。",
                        "symbols": ["BTC"]}],
            "events": [{"symbol": "BTC", "headline": "ETF获批",
                        "impact": "机构资金流入，中期利好。", "direction": "bullish"}],
            "source": "llm"}


try:
    # 7a 本周已发过 → skipped
    N.ensure_notification_table = lambda conn: None
    N._weekly_window = lambda end_ts=None: (_start, _end_utc, _label)
    N._weekly_report_already_sent = lambda conn, s, e: True
    N._try_acquire_send_lock = lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应进锁"))
    _r = N.send_catalyst_weekly_report(_CaptureConn())
    check(_r["sent"] == 0 and _r["skipped"] == 1 and _r["failed"] == 0,
          "本周已发过：sent=0/skipped=1", str(_r))
    check("已发送过" in _r["reason"], "reason 标注「已发送过」", _r["reason"])

    # 7b 发送锁拒绝 → skipped
    N._weekly_report_already_sent = lambda conn, s, e: False
    N._weekly_overview_stats = lambda conn, s, e: _stats_fake()
    N._weekly_key_events = lambda conn, s, e, limit=None: _EVENTS
    N._weekly_llm_narrative = lambda e, s, w: _narr_llm()
    N._try_acquire_send_lock = lambda *a, **k: False
    _send_email = lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应发信"))
    N._send_email = _send_email
    _r = N.send_catalyst_weekly_report(_CaptureConn())
    check(_r["sent"] == 0 and _r["skipped"] == 1, "发送锁拒绝：skipped=1", str(_r))

    # 7c 发送成功 → sent=1，哨兵 -4 记录
    N._try_acquire_send_lock = lambda *a, **k: True
    sent = {}
    N._send_email = lambda subject, body: sent.update(subject=subject, body=body) or (True, "ok")
    marked = {}
    N._mark_sent = lambda conn, sid, ntype, tier, subj, **k: marked.update(
        sid=sid, ntype=ntype, **k)
    _r = N.send_catalyst_weekly_report(_CaptureConn())
    check(_r["sent"] == 1 and _r["failed"] == 0, "发送成功：sent=1", str(_r))
    check(marked.get("sid") == N.SENTINEL_WEEKLY_REPORT_SIGNAL_ID
          and marked.get("ntype") == N.NTYPE_WEEKLY_REPORT,
          "以哨兵 -4 + weekly_report 记录发送日志", str(marked))
    check(_r["narrative_source"] == "llm", "回传 narrative_source=llm", str(_r["narrative_source"]))
    check("周报" in sent.get("subject", "") and "重要事件" in sent.get("subject", ""),
          "主题含「周报」与「重要事件」", sent.get("subject"))
    check("本周总述" in sent.get("body", "") and "机构资金流入" in sent.get("body", ""),
          "邮件正文含总述与影响解读", "")

    # 7d LLM 失败 → 回退模板，仍发送成功
    N._weekly_llm_narrative = lambda e, s, w: None
    N._weekly_key_events = lambda conn, s, e, limit=None: [{
        "symbol": "ETH", "title_cn": "升级完成", "title": "upgrade",
        "ai_summary": "性能提升。", "sentiment": "bullish",
    }]
    sent.clear()
    _r = N.send_catalyst_weekly_report(_CaptureConn())
    check(_r["sent"] == 1 and _r["narrative_source"] == "fallback",
          "LLM 失败时回退模板且仍发送成功", str(_r))
    check("回退" in sent.get("body", ""), "正文标注 AI 回退", "")

    # 7e dry-run → 不占去重位、不发信、返回 body
    N._weekly_llm_narrative = lambda e, s, w: _narr_llm()
    N._weekly_report_already_sent = lambda conn, s, e: (_ for _ in ()).throw(
        AssertionError("dry-run 不应查去重"))
    N._try_acquire_send_lock = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("dry-run 不应占锁"))
    N._send_email = lambda *a, **k: (_ for _ in ()).throw(AssertionError("dry-run 不应发信"))
    _r = N.send_catalyst_weekly_report(_CaptureConn(), dry_run=True)
    check(_r["sent"] == 0 and _r["skipped"] == 0 and _r["body"],
          "dry-run：不发送但返回渲染 body", str({k: v for k, v in _r.items() if k != "body"}))
    check("[DRY-RUN]" in _r["reason"], "dry-run reason 标注 [DRY-RUN]", _r["reason"])

    # 7f 查询异常 → 返回 dict 不抛异常
    N._weekly_overview_stats = lambda conn, s, e: (_ for _ in ()).throw(RuntimeError("boom"))
    _r = N.send_catalyst_weekly_report(_CaptureConn())
    check(isinstance(_r, dict) and _r["sent"] == 0 and _r["failed"] == 0,
          "统计查询异常返回 dict 且 sent=0/failed=0", str(_r))
    check("查询失败" in _r["reason"], "reason 标注查询失败", _r["reason"])
finally:
    _reset()

print("== 8. 自然周去重 SQL ==")
_c3 = _CaptureConn()
N._weekly_report_already_sent(_c3, _start, _end_utc)
_sql3 = _c3.calls[0][0]
check("notification_type = %s" in _sql3 and "status = 'sent'" in _sql3,
      "按 notification_type + status='sent' 判重")
check("sent_at >= %s AND sent_at < %s" in _sql3, "按自然周窗口内 sent_at 判重")

print("== 9. 渲染护栏 ==")
_html_llm = N._build_weekly_report_html(_stats_fake(), _narr_llm(), _label)
check("本周重要催化剂事件与影响" in _html_llm, "标题为「本周重要催化剂事件与影响」")
check("本周总述" in _html_llm and "主线主题" in _html_llm and "大事记与影响" in _html_llm,
      "含三段结构（总述 / 主线主题 / 大事记与影响）")
check(_label in _html_llm, "含窗口 label", _label)
check("ETF 与机构资金" in _html_llm, "含 LLM 生成的主题名")
check("机构资金流入，中期利好。" in _html_llm, "含逐条影响解读原文")
check("利好" in _html_llm, "含方向徽章（利好）")
check("本周概览（附录）" in _html_llm, "概览统计降级为附录")
check("叙事由 AI 生成" in _html_llm, "标注叙事来源为 AI")
check("非投资建议" in _html_llm, "含免责声明")

_banned = ("做多", "做空", "止损", "止盈", "入场", "买入", "卖出", "目标价", "交易建议")
_hit = [k for k in _banned if k in _html_llm]
check(not _hit, "不含交易档位/方向指令字样（叙事视角）", f"命中: {_hit}")
check("运维告警" not in _html_llm and "这不是发送故障" not in _html_llm,
      "不含运维告警字样（区别于 channel_silence 通道）")

_html_fb = N._build_weekly_report_html(
    _stats_fake(), N._weekly_fallback_narrative([{
        "symbol": "ETH", "title_cn": "升级完成", "title": "upgrade",
        "ai_summary": "性能提升。", "sentiment": "neutral",
    }]), _label)
check("AI 叙事不可用" in _html_fb, "回退时正文明确标注 AI 叙事不可用")
check("本周无显著主线主题" in _html_fb, "回退时主题区给出降级占位")

_html_empty = N._build_weekly_report_html(
    _stats_fake(), {"overview": "", "themes": [], "events": [], "source": "llm"}, _label)
check("本周无重要催化剂事件" in _html_empty, "无事件时给出占位（不静默空白）")

print(f"\n{'=' * 50}\n通过 {passed} / 失败 {failed}\n{'=' * 50}")
sys.exit(1 if failed else 0)