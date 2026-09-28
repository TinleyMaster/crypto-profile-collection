"""A 级 Alert 通道静默可观测（channel_silence）离线护栏测试。

覆盖 2026-09-28 新增「运维侧告警」通道的硬约束：
  1. 通道独立：notification_type 与 A 级 Alert 的四种类型互不重叠（独立去重）
  2. 负号哨兵：SENTINEL_CHANNEL_SILENCE_SIGNAL_ID = -3，与 -1/-2 互不相同
  3. 快照 SQL 字段：只读地面事实（tier=A 且 status=open 的 MAX(created_at)）、
     上游供给、交叉判定（major_event / fast_alert 最后发送时间）
  4. 阈值判定：通道正常（空转 < 阈值）不发信不占去重位；空转（≥ 阈值）才发
  5. 从未产生候选：last_a_open_at 为 NULL 时主题标注「从未产生过候选」
  6. 24h 去重：_try_acquire_send_lock 拒绝时静默跳过
  7. 渲染护栏：含「这不是发送故障」「运维告警」「058c246 决议不发空窗邮件」、
     不含任何交易档位/方向指令字样
  8. 失败不阻断：快照查询异常返回 dict 而非抛异常

运行: python test_channel_silence_alert.py
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

# 抽出 _channel_silence_snapshot 的 SQL 文本（从 def 到下一个顶层 def）
_m = re.search(r"def _channel_silence_snapshot\(.*?\n(.*?)\ndef ", _SRC, re.S)
_SNAP_SRC = _m.group(1) if _m else ""


class _CaptureConn:
    def __init__(self):
        self.sql = None
        self.params = None

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params

        class _C:
            def fetchone(self):
                return {
                    "last_a_open_at": None, "a_open_total": 0, "a_new_7d": 0,
                    "upstream_24h": 0, "upstream_7d": 0,
                    "last_major_event_at": None, "last_fast_alert_at": None,
                }

            def fetchall(self):
                return []
        return _C()


print("== 1. 通道独立 ==")
check(N.NTYPE_CHANNEL_SILENCE == "channel_silence", "notification_type 常量为 channel_silence",
      N.NTYPE_CHANNEL_SILENCE)
check(len({N.NTYPE_CHANNEL_SILENCE, N.NTYPE_MAJOR_EVENT, N.NTYPE_FAST_ALERT,
           N.NTYPE_SLOW_DIGEST, N.NTYPE_SLOW_DIGEST_STOCK}) == 5,
      "channel_silence 与既有 4 种类型互不重叠（独立去重）")

print("== 2. 负号哨兵 ==")
check(N.SENTINEL_CHANNEL_SILENCE_SIGNAL_ID == -3,
      "哨兵为 -3（与 -1/-2 互异，NULL 不触发 UNIQUE）",
      str(N.SENTINEL_CHANNEL_SILENCE_SIGNAL_ID))
check(N.CHANNEL_SILENCE_DAYS >= 1, "默认阈值 ≥1 天", str(N.CHANNEL_SILENCE_DAYS))

print("== 3. 快照 SQL 字段 ==")
_c = _CaptureConn()
N._channel_silence_snapshot(_c)
check("MAX(created_at)" in _c.sql, "核心判据为 MAX(created_at)（无状态，可复算）")
check("tier = 'A' AND status = 'open'" in _c.sql, "判据口径为 tier=A 且 status=open")
check("AS upstream_24h" in _c.sql and "AS upstream_7d" in _c.sql,
      "含上游供给两段（近 24h / 近 7d）")
check("AS last_major_event_at" in _c.sql and "AS last_fast_alert_at" in _c.sql,
      "含交叉判定（major_event / fast_alert 最后发送时间）")
check("MAX(sent_at)" in _c.sql, "交叉判定按 MAX(sent_at) 取最后发送")
check("status = 'sent'" in _c.sql, "交叉判定只看 status='sent'（成功发送）")

print("== 4. 阈值判定 ==")
_now = _dt.datetime.now(_dt.timezone.utc)

_orig = {k: getattr(N, k) for k in
         ("ensure_notification_table", "_channel_silence_snapshot",
          "_try_acquire_send_lock", "_send_email", "_mark_sent")}


def _snap(last_at, **over):
    s = {"last_a_open_at": last_at, "a_open_total": 0, "a_new_7d": 0,
         "upstream_24h": 0, "upstream_7d": 0,
         "last_major_event_at": None, "last_fast_alert_at": None}
    s.update(over)
    return s


def _reset():
    for k, v in _orig.items():
        setattr(N, k, v)


try:
    # 4a 正常（空转 < 阈值）→ 不发信、不占去重位
    N.ensure_notification_table = lambda conn: None
    N._channel_silence_snapshot = lambda conn: _snap(_now - _dt.timedelta(days=1))
    N._try_acquire_send_lock = lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应进入锁"))
    N._send_email = lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应发信"))
    _r = N.send_channel_silence_alert(_CaptureConn(), days=3)
    check(_r["sent"] == 0 and _r["skipped"] == 1 and _r["failed"] == 0,
          "通道正常：sent=0/skipped=1（不发信不占去重位）", str(_r))
    check("通道正常" in _r["reason"], "reason 标注「通道正常」", _r["reason"])

    # 4b 空转（≥ 阈值）→ 进入发信流程
    sent = {}
    N._channel_silence_snapshot = lambda conn: _snap(_now - _dt.timedelta(days=5))
    N._try_acquire_send_lock = lambda *a, **k: True
    N._send_email = lambda subject, body: sent.update(subject=subject, body=body) or (True, "ok")
    N._mark_sent = lambda *a, **k: None
    _r = N.send_channel_silence_alert(_CaptureConn(), days=3)
    check(_r["sent"] == 1 and _r["failed"] == 0, "空转 ≥ 阈值：sent=1", str(_r))
    check("空转" in sent.get("subject", ""), "主题含「空转」", sent.get("subject"))
    check(_r["days_silent"] is not None and _r["days_silent"] >= 3,
          "days_silent 数值回传（≥ 阈值）", str(_r["days_silent"]))
finally:
    _reset()

print("== 5. 从未产生候选 ==")
try:
    N.ensure_notification_table = lambda conn: None
    N._channel_silence_snapshot = lambda conn: _snap(None)
    N._try_acquire_send_lock = lambda *a, **k: True
    sent = {}
    N._send_email = lambda subject, body: sent.update(subject=subject, body=body) or (True, "ok")
    N._mark_sent = lambda *a, **k: None
    _r = N.send_channel_silence_alert(_CaptureConn(), days=3)
    check(_r["sent"] == 1, "last_a_open_at 为 NULL 仍会告警（从未产生候选）", str(_r))
    check("从未产生过候选" in sent.get("subject", ""),
          "主题标注「从未产生过候选」", sent.get("subject"))
    check(_r["days_silent"] is None, "days_silent=None（无法计算空转天数）", str(_r))
finally:
    _reset()

print("== 6. 24h 去重 ==")
try:
    N.ensure_notification_table = lambda conn: None
    N._channel_silence_snapshot = lambda conn: _snap(_now - _dt.timedelta(days=5))
    N._try_acquire_send_lock = lambda *a, **k: False
    N._send_email = lambda *a, **k: (_ for _ in ()).throw(AssertionError("去重后不应发信"))
    _r = N.send_channel_silence_alert(_CaptureConn(), days=3)
    check(_r["sent"] == 0 and _r["skipped"] == 1 and _r["failed"] == 0,
          "24h 内已告警 → 跳过（不重复发信）", str(_r))
    check("24h" in _r["reason"], "reason 标注 24h 去重", _r["reason"])
finally:
    _reset()

print("== 7. 渲染护栏 ==")
_snap_html = _snap(_now - _dt.timedelta(days=5),
                   a_new_7d=2, a_open_total=0, upstream_24h=430, upstream_7d=2500,
                   last_major_event_at=_now - _dt.timedelta(hours=6),
                   last_fast_alert_at=_now - _dt.timedelta(days=5))
_html = N._build_channel_silence_html(_snap_html, 3, 5.2)
check("这不是发送故障" in _html, "含「这不是发送故障」（引导勿按 SMTP/调度排查）")
check("运维告警" in _html, "含「运维告警」标识")
check("channel_silence" in _html, "含 notification_type=channel_silence 说明")
check("058c246" in _html and "空窗" in _html and "今日无信号" in _html,
      "含 058c246 决议「空窗期不发今日无信号邮件」说明")
check("排查 SQL" in _html, "含可直接复制的排查 SQL")
check("composite_score &gt;= 80" in _html, "排查 SQL 含高分信号定位（HTML 转义后）")
_banned = ("做多", "做空", "止损", "止盈", "入场", "买入", "卖出", "目标价", "交易建议")
_hit = [k for k in _banned if k in _html]
check(not _hit, "不含任何交易档位/方向指令字样（运维视角，非投资建议）", f"命中: {_hit}")

print("== 8. 失败不阻断 ==")


class _BoomConn:
    def execute(self, *a, **k):
        raise RuntimeError("boom")


try:
    N.ensure_notification_table = lambda conn: None
    _r = N.send_channel_silence_alert(_BoomConn(), days=3)
    check(isinstance(_r, dict) and _r["sent"] == 0 and _r["failed"] == 0,
          "快照查询异常返回 dict 且 sent=0/failed=0（不抛异常）", str(_r))
    check("快照查询失败" in _r["reason"], "reason 标注查询失败", _r["reason"])
finally:
    _reset()

print(f"\n{'=' * 50}\n通过 {passed} / 失败 {failed}\n{'=' * 50}")
sys.exit(1 if failed else 0)
