"""催化剂周报（weekly_report）离线护栏测试。

覆盖 2026-09-29 新增「每周一 09:00 催化剂周报」的硬约束：
  1. 通道独立：notification_type=weekly_report 与既有 5 种类型互不重叠（独立去重）
  2. 负号哨兵：SENTINEL_WEEKLY_REPORT_SIGNAL_ID = -4（与 -1/-2/-3 互异）
  3. 自然周窗口：北京时区，上周一 00:00 ~ 本周一 00:00，恰好 7 天
  4. 概览统计 SQL：事件类型/情感用 COALESCE 统一口径，含 tier/来源分布
  5. A 级清单 SQL：tier='A' + status='open' + entry/stop/tp 齐全，按 composite_score 降序
  6. 自然周去重：本周已发过 → skipped；发送锁拒绝 → skipped
  7. 渲染护栏：含「周报」「A 级清单」及交易档位（方向/入场/止损/止盈），
     不含运维告警字样（区别于 channel_silence 通道）
  8. 失败不阻断：统计/查询异常返回 dict 而非抛异常

运行: python test_catalyst_weekly_report.py
"""
import datetime as _dt
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

print("== 4. 概览统计 SQL ==")


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
check("FROM biz.asset_catalyst" in _all_sql and "WHERE created_at >= %s AND created_at < %s" in _all_sql,
      "催化剂入库统计用 biz.asset_catalyst 窗口过滤")

print("== 5. A 级清单 SQL ==")
_c2 = _CaptureConn()
_rows = N._weekly_a_signals(_c2, _start, _end_utc)
_sql = _c2.calls[0][0]
check("s.tier = 'A'" in _sql, "仅 tier='A'")
check("s.status = 'open'" in _sql, "仅 status='open'（仍在持仓）")
check("s.entry_price IS NOT NULL" in _sql and "s.stop_loss IS NOT NULL" in _sql
      and "s.take_profit IS NOT NULL" in _sql, "entry/stop/tp 三档齐全才入选")
check("ORDER BY s.composite_score DESC" in _sql, "按 composite_score 降序")
check("JOIN core.asset a" in _sql and "JOIN biz.asset_catalyst ac" in _sql,
      "联表 core.asset + biz.asset_catalyst")

print("== 6. 自然周去重 SQL ==")
_c3 = _CaptureConn()
N._weekly_report_already_sent(_c3, _start, _end_utc)
_sql3 = _c3.calls[0][0]
check("notification_type = %s" in _sql3 and "status = 'sent'" in _sql3,
      "按 notification_type + status='sent' 判重")
check("sent_at >= %s AND sent_at < %s" in _sql3, "按自然周窗口内 sent_at 判重")

print("== 7. 发送流程 ==")
_now = _dt.datetime.now(_dt.timezone.utc)
_orig = {k: getattr(N, k) for k in
         ("ensure_notification_table", "_weekly_window",
          "_weekly_overview_stats", "_weekly_a_signals",
          "_weekly_report_already_sent", "_try_acquire_send_lock",
          "_send_email", "_mark_sent")}


def _reset():
    for k, v in _orig.items():
        setattr(N, k, v)


def _stats_fake():
    return {"signals_total": 12, "tier_dist": [("A", 3), ("B", 9)],
            "event_type_dist": [("listing", 5)],
            "sentiment_dist": [("bullish", 7)],
            "source_dist": [("cmc", 12)], "catalysts_new": 30}


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
    N._weekly_a_signals = lambda conn, s, e: []
    N._try_acquire_send_lock = lambda *a, **k: False
    N._send_email = lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应发信"))
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
    check("周报" in sent.get("subject", "") and _label.split("~")[0].strip() in sent["subject"],
          "主题含「周报」与窗口起始日", sent.get("subject"))

    # 7d 查询异常 → 返回 dict 不抛异常
    N._weekly_overview_stats = lambda conn, s, e: (_ for _ in ()).throw(RuntimeError("boom"))
    _r = N.send_catalyst_weekly_report(_CaptureConn())
    check(isinstance(_r, dict) and _r["sent"] == 0 and _r["failed"] == 0,
          "统计查询异常返回 dict 且 sent=0/failed=0", str(_r))
    check("查询失败" in _r["reason"], "reason 标注查询失败", _r["reason"])
finally:
    _reset()

print("== 8. 渲染护栏 ==")
_rows_fake = [{
    "signal_id": 1, "tier": "A", "composite_score": 88.5,
    "entry_price": 100, "stop_loss": 90, "take_profit": 130, "rr_ratio": 3.0,
    "confidence": 0.9, "resonance_state": "confirm", "created_at": _now,
    "canonical_name": "Bitcoin", "symbol": "BTC", "primary_sector": "L1",
    "title": "现货ETF获批", "title_cn": "比特币现货ETF获批", "ai_summary": "重大利好",
    "event_type": "regulatory", "sentiment": "bullish", "source_code": "cmc",
    "published_at": _now,
}]
_html = N._build_weekly_report_html(_stats_fake(), _rows_fake, _label)
check("催化剂决策管道" in _html and "周报" in _html, "含「催化剂决策管道·周报」标识")
check("A 级清单" in _html, "含「A 级清单」区块")
check(_label in _html, "含窗口 label", _label)
check("BTC" in _html and "比特币现货ETF获批" in _html, "A 级卡片含 symbol 与中文标题")
for _kw in ("方向", "入场", "止损", "止盈", "盈亏比"):
    check(_kw in _html, f"A 级卡片含交易档位「{_kw}」")
check("运维告警" not in _html and "这不是发送故障" not in _html,
      "不含运维告警字样（区别于 channel_silence 通道）")

print(f"\n{'=' * 50}\n通过 {passed} / 失败 {failed}\n{'=' * 50}")
sys.exit(1 if failed else 0)
