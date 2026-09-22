#!/usr/bin/env python3
"""轧空胜负判定单测（工单 SQZ-04）。

运行：python test_squeeze_battle.py

覆盖：
  1) 判定纯函数 `evaluate_battle` 的 7 个注入用例（含缺失数据口径 P1-3）；
  2) 18 个阈值常量断言（断言当前值，含 SQZ-02 重标定后的 LONG_LIQ_RATIO_THR）；
  3) 渲染层 `_render_squeeze_alert` 段内不得残留 `or 0`（None 与 0 必须可区分）；
  4) 判定窗口连续性闸门 `window_gate` 的 C1~C10 用例（复验 P2-d/P3，搬自复验报告 §3），
    外加 max_gap 夹取（D7）与拒判文案 `gap_reason` 前缀守卫（D6）。

⚠️ case1/case5 结论随 SQZ-02 由 `churn` 变为 `profit_take`：旧阈值 8e-5 会把
   `long_liq=1.66e-4` 判成「大额多单踩踏」从而屏蔽 profit_take；重标定到
   P95=4.07e-4 后该样本不再算踩踏，而它 OI 快速下降 ⇒ 归入 profit_take。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, os.path.join(_SCRIPTS, "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.analysis import squeeze as sqz  # noqa: E402

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


# ════════════════════════════════════════════════════════════
# 1. 阈值常量断言（含 SQZ-02 新值）
# ════════════════════════════════════════════════════════════
print("\n【测试1】阈值常量")
CONSTS = {
    "SURGE_THR_5M": 2.0, "SURGE_THR_15M": 3.5, "SURGE_VOL_RATIO_MIN": 1.5,
    "OI_SHORT_COVER_PCT": -0.3, "SQZ_CVD_RATIO_MIN": 0.02,
    "SQZ_SHORT_LIQ_RATIO_MIN": 0.00008,
    "RETRACE_THR": 2.0, "MIN_OBSERVE_MIN": 15, "TRACK_EXPIRE_MIN": 180,
    "TRACK_QUEUE_MAX": 12, "SQUEEZE_COOLDOWN_H": 6,
    "OI_EXIT_PCT": -1.0, "CVD_SELL_STRONG": -0.15, "CVD_SELL_MILD": -0.08,
    "CVD_BUY_MILD": 0.02, "MIN_WINDOW_COVERAGE": 0.6,
    "MAX_MID_GAP_BUCKETS": 2,
    "GAP_METRIC_VER": 2,   # 复验 D5：metrics 版本位（v2 = 左端单列 head_gap）
}
for name, want in CONSTS.items():
    check(getattr(sqz, name, None) == want, f"{name} == {want}",
          f"实际 {getattr(sqz, name, '<无>')}")
check(abs(sqz.LONG_LIQ_RATIO_THR - 0.00040709) < 1e-12,
      "LONG_LIQ_RATIO_THR == 0.00040709（SQZ-02 P95 重标定）",
      f"实际 {sqz.LONG_LIQ_RATIO_THR}")

# ════════════════════════════════════════════════════════════
# 2. 判定用例（注入）
# ════════════════════════════════════════════════════════════
print("\n【测试2】evaluate_battle 注入用例")
CASES = [
    ("case1 线上真实输入(long_liq=1.66e-4)→SQZ-02 后归 profit_take",
     dict(d_oi_pct=-2.786, cvd_ratio=0.1538, long_liq_ratio=0.000166,
          top_ratio_chg=0.0522, taker_ratio=0.8536),
     sqz.PROFIT_TAKE, "medium"),
    ("case2 爆仓缺失(long_liq=None)【旧码曾 profit_take】",
     dict(d_oi_pct=-2.786, cvd_ratio=0.1538, long_liq_ratio=None,
          top_ratio_chg=0.0522, taker_ratio=0.8536),
     sqz.CHURN, "low"),
    ("case3 OI+爆仓双缺失【旧码曾 long_win/high】",
     dict(d_oi_pct=None, cvd_ratio=0.1538, long_liq_ratio=None,
          top_ratio_chg=0.0522, taker_ratio=0.8536),
     sqz.CHURN, "low"),
    ("case4 三维度全缺失",
     dict(d_oi_pct=None, cvd_ratio=None, long_liq_ratio=None,
          top_ratio_chg=None, taker_ratio=None),
     sqz.CHURN, "low"),
    ("case5 仅 CVD 缺失→OI 快降+非踩踏 归 profit_take",
     dict(d_oi_pct=-2.786, cvd_ratio=None, long_liq_ratio=0.000166,
          top_ratio_chg=0.0522, taker_ratio=0.8536),
     sqz.PROFIT_TAKE, "medium"),
    ("case6 空头胜正例(cvd=-0.20, 长爆=1e-3≥阈值)",
     dict(d_oi_pct=-0.5, cvd_ratio=-0.20, long_liq_ratio=0.001,
          top_ratio_chg=-0.1, taker_ratio=0.7),
     sqz.SHORT_WIN, "high"),
    ("case7 多头胜正例(d_oi=+0.5, cvd=+0.10, 长爆=0)",
     dict(d_oi_pct=0.5, cvd_ratio=0.10, long_liq_ratio=0.0,
          top_ratio_chg=0.02, taker_ratio=1.2),
     sqz.LONG_WIN, "high"),
]
for title, kw, want_c, want_conf in CASES:
    v = sqz.evaluate_battle(**kw)
    check(v["conclusion"] == want_c, f"{title} → {want_c}",
          f"实际 {v['conclusion']}")
    check(v["confidence"] == want_conf, f"{title} 置信度 {want_conf}",
          f"实际 {v['confidence']}")

# 缺失维度必须显式标注
v = sqz.evaluate_battle(d_oi_pct=-2.786, cvd_ratio=0.1538, long_liq_ratio=None,
                        top_ratio_chg=None, taker_ratio=None)
check(v["metrics"]["data_missing"] == ["long_liq_ratio"],
      "缺失维度写入 data_missing", str(v["metrics"]["data_missing"]))
check("数据不足" in v["reason"], "reason 显式标注数据不足", v["reason"])

# 新阈值边界：OI 快降 + 爆仓刚好越阈 → 不应归 profit_take
v = sqz.evaluate_battle(d_oi_pct=-2.0, cvd_ratio=0.10, long_liq_ratio=0.0005,
                        top_ratio_chg=0.0, taker_ratio=1.0)
check(v["conclusion"] == sqz.CHURN, "越阈爆仓屏蔽 profit_take → churn",
      v["conclusion"])

# ════════════════════════════════════════════════════════════
# 3. 渲染层回归护栏：or 0 残留
# ════════════════════════════════════════════════════════════
print("\n【测试3】_render_squeeze_alert 段内 `or 0` 残留")
daemon_path = os.path.join(_SCRIPTS, "bin", "scan_daemon.py")
with open(daemon_path, encoding="utf-8") as f:
    src = f.read()
i = src.find("def _render_squeeze_alert")
j = src.find("def task_scan_squeeze")
seg = src[i:j] if i >= 0 and j > i else ""
hits = [ln.strip() for ln in seg.split("\n") if "or 0" in ln]
check(i >= 0 and j > i, "能定位渲染段", f"i={i} j={j}")
check(len(hits) == 0, "渲染段内 `or 0` 命中数 == 0", "命中: " + " | ".join(hits))

# ════════════════════════════════════════════════════════════
# 4. 渲染层：None 与 0 可区分 + oi_lag_sec 披露（SQZ-03）
# ════════════════════════════════════════════════════════════
print("\n【测试4】渲染层 None/0 区分与 oi_lag_sec")
sys.path.insert(0, os.path.join(_SCRIPTS, "bin"))
try:
    import scan_daemon as sd  # noqa: E402
    check(sd._fmt_ratio(None) == "\u2014", "None → '—'", sd._fmt_ratio(None))
    check(sd._fmt_ratio(0.0, 3) == "+0.000%", "0.0 → '+0.000%'", sd._fmt_ratio(0.0, 3))
    _item = {
        "verdict": {"conclusion": sqz.CHURN, "confidence": "low", "reason": "x"},
        "metrics": {"surge_pct": 1.0, "surge_timeframe": "5m", "peak_px": 1, "last_px": 1,
                    "retrace_pct": 2.0, "d_oi_pct": -2.0, "cvd_ratio": None,
                    "long_liq_ratio": 0.0, "short_liq_ratio": None,
                    "top_ratio_chg": 0.0, "taker_ratio": 1.0, "taker_ts": None,
                    "oi_cover": {"have": 5, "expect": 13}, "oi_lag_sec": 900,
                    "data_missing": ["cvd_ratio"], "peak_ts": None},
        "track": {"symbol": "XUSDT", "started_at": None},
    }
    _html = sd._render_squeeze_alert([_item])
    check("OI滞后 900s" in _html, "渲染披露 OI滞后 900s")
    check("CVD占比 \u2014" in _html, "缺失 CVD 渲染为 '—'")
    check("最近1h空单爆仓/成交额 \u2014" in _html, "缺失爆仓渲染为 '—'")
except Exception as e:  # noqa: BLE001
    check(False, "渲染层测试可执行", f"{type(e).__name__}: {e}")


# ════════════════════════════════════════════════════════════
# 5. 判定窗口连续性闸门 window_gate（复验 P2-d / P3，用例搬自复验报告 §3）
# ════════════════════════════════════════════════════════════
print("\n【测试5】window_gate 连续性闸门 C1~C10")
_FIRST, _LAST = 1000, 1012           # 判定窗口 [first, last) = 12 桶，期望 13 桶
_FULL = set(range(_FIRST, _LAST))    # 右端正在采集的桶本就不在区间内


def _daemon_verdict(present, tail_gap, first=_FIRST, last=_LAST):
    """复刻 scan_daemon 三段闸门的判定与**归因优先级**（覆盖率 > 尾部 > 中段）。"""
    coverage = len(present) / (last - first + 1)
    ok, head, mid = sqz.window_gate(set(present), first, last)
    if coverage < sqz.MIN_WINDOW_COVERAGE:
        return True, "覆盖率", head, mid
    if tail_gap:
        return True, "尾部", head, mid
    if not ok:
        return True, "中段", head, mid
    return False, "", head, mid


def _minus(s, *drop):
    return s - set(drop)


# (用例, present 桶, tail_gap, 期望拒判, 期望 head_gap, 期望 mid_gap, 期望归因)
_GATE_CASES = [
    ("C1 正常（仅缺正在采集的尾桶）", _FULL, False, False, 0, 0, ""),
    ("C2 中段缺 2 连续桶", _minus(_FULL, 1005, 1006), False, True, 0, 2, "中段"),
    ("C3 中段缺 1 桶", _minus(_FULL, 1005), False, False, 0, 1, ""),
    ("C4 中段缺 3 连续", _minus(_FULL, 1005, 1006, 1007), False, True, 0, 3, "中段"),
    ("C5 左端缺 2 连续", _minus(_FULL, 1000, 1001), False, True, 2, 0, "中段"),
    ("C6 完全无 OI 数据", set(), False, True, 12, 0, "覆盖率"),
    ("C7 尾部断 4 桶（窗口自身完整）", _FULL, True, True, 0, 0, "尾部"),
    ("C8 中段缺 2 + 尾部断 4", _minus(_FULL, 1005, 1006), True, True, 0, 2, "尾部"),
    ("C9 覆盖率<0.6 + 中段缺 2",
     {1000, 1001, 1004, 1005, 1008, 1009, 1011}, False, True, 0, 2, "覆盖率"),
    ("C10 两处各缺 1（非连续）", _minus(_FULL, 1005, 1010), False, False, 0, 1, ""),
]
for title, present, tail_gap, want_rej, want_head, want_mid, want_why in _GATE_CASES:
    rej, why, head, mid = _daemon_verdict(present, tail_gap)
    check(rej == want_rej, f"{title} → {'拒判' if want_rej else '通过'}",
          f"实际 {'拒判' if rej else '通过'}")
    check((head, mid) == (want_head, want_mid),
          f"{title} 缺口计数 head={want_head}/mid={want_mid}",
          f"实际 head={head}/mid={mid}")
    if want_why or why:
        check(why == want_why, f"{title} 归因 → {want_why or '不拒判'}",
              f"实际 {why or '不拒判'}")

# 边界：空区间（peak==now）与 max_gap 可调
check(sqz.window_gate(set(), 1000, 1000) == (True, 0, 0),
      "空区间（last==first）不拒判，且无缺口计数",
      str(sqz.window_gate(set(), 1000, 1000)))
check(sqz.window_gate(set(), 1000, 1001, max_gap=1)[0] is False,
      "max_gap 可注入（缺 1 桶在 max_gap=1 时拒判）")
check(sqz.window_gate(_minus(_FULL, 1000), 1000, 1012)[1] == 1,
      "左端缺 1 桶计入 head_gap（而非 mid_gap）",
      str(sqz.window_gate(_minus(_FULL, 1000), 1000, 1012)))
check(sqz.window_gate(_minus(_FULL, 1001), 1000, 1012)[2] == 1,
      "左端起第 2 桶才缺 → 计 mid_gap（head_gap 必须为 0）",
      str(sqz.window_gate(_minus(_FULL, 1001), 1000, 1012)))

# 复验 D7：max_gap 必须 ≥1 —— 判据是 `max(...) < max_gap`，传 0 会「任何窗口都拒判」
# 从而静默停摆整条判定；实现对 0/负数显式夹到 1（缺 1 桶即拒判 = 最严可用档）。
check(sqz.window_gate(_minus(_FULL, 1005), 1000, 1012, max_gap=0) == (False, 0, 1),
      "max_gap=0 被夹到 1（缺 1 桶即拒判，而非全量拒判）",
      str(sqz.window_gate(_minus(_FULL, 1005), 1000, 1012, max_gap=0)))
check(sqz.window_gate(_FULL, 1000, 1012, max_gap=0)[0] is True,
      "max_gap=0 下完整窗口仍通过（证明未退化为「全量拒判」）")
check(sqz.window_gate(_minus(_FULL, 1005), 1000, 1012, max_gap=-3) == (False, 0, 1),
      "负 max_gap 同样被夹到 1")

# 复验 D6：拒判文案抽成 squeeze.gap_reason() 纯函数 —— 前缀「判定窗口」是
# check_scan_freshness 的 `reason LIKE '判定窗口%'` 耦合点，必须由测试守住。
_r = sqz.gap_reason(2, 0, 11, 13)
check(_r.startswith("判定窗口"),
      "gap_reason 必须以「判定窗口」开头（否则看门狗拒判计数静默失联）", _r)
check("连续缺桶 2 个" in _r and "左端起 2 个" in _r and "中段 0 个" in _r
      and "11/13 桶" in _r,
      "gap_reason 含最大游程/左右端拆分/桶数", _r)
check(sqz.gap_reason(0, 3, 10, 13).startswith("判定窗口")
      and "连续缺桶 3 个" in sqz.gap_reason(0, 3, 10, 13),
      "gap_reason 中段游程口径正确", sqz.gap_reason(0, 3, 10, 13))
check(sqz.gap_reason(1, 2, 10, 13).startswith("判定窗口内连续缺桶 2 个"),
      "gap_reason 取 max(head, mid) 作为「连续缺桶 N 个」", sqz.gap_reason(1, 2, 10, 13))

# 闸门等价性：新实现与「旧实现（含左端游程 + 阈值 2）」逐例一致
def _old_gate(present, first=_FIRST, last=_LAST, thr=sqz.MAX_MID_GAP_BUCKETS):
    worst = run = 0
    for b in range(first, last):
        if b in present:
            run = 0
        else:
            run += 1
            worst = max(worst, run)
    return worst < thr


for title, present, *_ in _GATE_CASES:
    new_ok = sqz.window_gate(set(present), _FIRST, _LAST)[0]
    check(new_ok == _old_gate(set(present)),
          f"{title} 与旧实现判定一致", f"新={new_ok} 旧={_old_gate(set(present))}")


# ════════════════════════════════════════════════════════════
# 复验 F6c：标定脚本（calib_squeeze_liq_thr.py）此前**零单测** —— 本轮新增的三个纯函数
# （wilson_ci / segment_upper_bounds / molecule_coverage）与退出码四态全部无断言，
# 若只报「单测 91/91」，覆盖的其实只是未改动的 squeeze.py。这里补齐。
# ════════════════════════════════════════════════════════════
print("\n【测试·标定脚本】复验 F2/F4/F6b 三部纯函数 + 退出码四态")
sys.path.insert(0, _HERE)
import datetime as _dt  # noqa: E402

import calib_squeeze_liq_thr as calib  # noqa: E402

# ── wilson_ci：与复验报告的独立参考值对照 ──────────────────────
def _ci_close(got, want, tol=0.02):
    return got is not None and abs(got[0] - want[0]) < tol and abs(got[1] - want[1]) < tol


check(_ci_close(calib.wilson_ci(0, 100), (0.0, 3.6995)),
      "wilson_ci(0,100) ≈ [0, 3.70]", str(calib.wilson_ci(0, 100)))
check(_ci_close(calib.wilson_ci(350, 1745), (18.245, 22.001)),
      "wilson_ci(350,1745) ≈ [18.25, 22.00]", str(calib.wilson_ci(350, 1745)))
check(_ci_close(calib.wilson_ci(100, 100), (96.30, 100.0)),
      "wilson_ci(100,100) 上界恰为 100", str(calib.wilson_ci(100, 100)))
check(calib.wilson_ci(0, 0) is None and calib.wilson_ci(1, 0) is None,
      "wilson_ci n≤0 → None（不除零）")
check(_ci_close(calib.wilson_ci(120, 100), (96.30, 100.0)),
      "F6b：k>n 被夹取到 n（旧码此处抛 ValueError: math domain error）",
      str(calib.wilson_ci(120, 100)))

# ── exit_code：四态码位互不重合（F2 的核心）────────────────────
# 只列**语义自洽**的组合（pass=True 本身就蕴含 decisive=True）
_CODES = {(True, True, True): 0, (False, True, True): 2, (False, True, False): 4,
          (True, False, True): 3, (False, False, True): 3, (False, False, False): 3}
for (p_, s_, d_), want in _CODES.items():
    got = calib.exit_code(p_, s_, d_)
    check(got == want, f"exit_code(pass={p_}, sample_ok={s_}, decisive={d_}) = {want}", f"得 {got}")
check(len(set(_CODES.values())) == 4, "四态码位互不重合（0/2/3/4）")

# ── segment_upper_bounds：F4 守卫必须按**变体过滤后**的 n ────────
_T0 = _dt.datetime.now()


def _row(days_ago_min, hit_c_only=False):
    """构造一行：hit_c_only=True 时只满足变体 C（仅回撤）。"""
    ts = _T0 - _dt.timedelta(minutes=days_ago_min)
    if hit_c_only:
        return {"ts": ts, "long_liq": 1.0, "vol_win": 1.0, "peak_hi": 100.0,
                "close_now": 90.0, "trough_lo": None, "peak_c": None, "trough_c": None}
    return {"ts": ts, "long_liq": 1.0, "vol_win": 100.0, "peak_hi": None,
            "close_now": None, "trough_lo": None, "peak_c": None, "trough_c": None}


_seg_rows = [_row(i) for i in range(39)] + [_row(39, hit_c_only=True)]
_seg = calib.segment_upper_bounds(_seg_rows, 0.00008)
check(_seg["n_segments"] == 0 and _seg["skipped"] == 1,
      "F4：段内仅 1 行命中变体 C ⇒ 该变体不进本段、本段整体跳过（旧码会取到 100%）",
      f"n_segments={_seg['n_segments']} skipped={_seg['skipped']} min={_seg['min_pct']}")
_seg2 = calib.segment_upper_bounds([_row(i, hit_c_only=True) for i in range(40)], 0.00008)
check(_seg2["n_segments"] == 1 and _seg2["min_pct"] == 100.0
      and _seg2["segments"][0]["n_by_variant"] is not None,
      "F4：n≥MIN_SEGMENT_N 的段照常给出上界并附 n_by_variant",
      str(_seg2["segments"][0] if _seg2["segments"] else None))

# ── molecule_coverage：整点覆盖 / 最长连续空洞（伪游标）─────────
class _FakeCur:
    def __init__(self, hist):
        self._hist = hist

    def execute(self, *a, **k):
        pass

    def fetchall(self):
        return [{"n": n} for n in self._hist]


_mol = calib.molecule_coverage(_FakeCur([3, 0, 0, 5]), 1)
# 复验 I5：分母必须是 `expect_hours`（=24）而非网格实际长度（=4）—— 伪游标故意给出
# 「网格长度 ≠ expect_hours」的不一致网格，正是为了钉住「二者同源」这条不变量
# （旧码写 `present / len(hist)` ⇒ 此处会得 0.5，与折叠判据用的 `missing_hours` 不同源）。
check(_mol["hours_present"] == 2 and _mol["hours_present_ratio"] == round(2 / 24, 4)
      and _mol["grid_hours"] == 4 and _mol["max_hole_hours"] == 2,
      "molecule_coverage/I5：ratio 分母 = expect_hours(24) 而非网格长度(4)；空洞 2",
      str(_mol))
check(calib.molecule_coverage(_FakeCur([]), 1)["max_hole_hours"] == 0,
      "molecule_coverage：空网格不崩、空洞 0")

# ── 复验 G1/G3：`conclusion` 必须由 `exit_code()` 单一真源派生 ──
check(calib.CONCLUSION_BY_CODE
      == {0: "PASS", 2: "FAIL", 3: "SAMPLE_UNUSABLE", 4: "INCONCLUSIVE"},
      "G1：码位↔结论映射表覆盖 0/2/3/4（无遗漏、无别名）", str(calib.CONCLUSION_BY_CODE))
# 实测复现：`--days 7` 曾打出 sample_ok=false + conclusion="FAIL"（与 rc=3 打架，第三次复发）
check(calib.CONCLUSION_BY_CODE[calib.exit_code(False, False, True)] == "SAMPLE_UNUSABLE",
      "G1：sample_ok=False ⇒ 结论不再是强判定词 FAIL",
      str(calib.CONCLUSION_BY_CODE[calib.exit_code(False, False, True)]))
check(all(calib.CONCLUSION_BY_CODE[calib.exit_code(p_, s_, d_, m_)] != "FAIL"
          for p_, s_, d_, m_ in [(False, False, True, True), (False, True, False, False),
                                 (True, False, True, True)]),
      "G1：样本不可用/上界不可算的任何组合都不映射到 FAIL")
# G3：`measurable=False`（无任何变体命中 ⇒ 上界根本算不出来）旧码落到 rc=4
# （其文档语义是「样本可用、只是判据不具判别力」）⇒ 并入 3。
check(calib.exit_code(False, True, True, False) == 3,
      "G3：measurable=False 并入 3，而非语义不符的 4",
      str(calib.exit_code(False, True, True, False)))
check(calib.exit_code(True, True, True, False) == 3 and calib.exit_code(True, True, True) == 0,
      "G3：可算性优先于结论（pass=True 但不可算仍 3；可算才 0）")

# ── 复验 G4 + H4：CI **带符号**裕度（绝对值会把 PASS 侧/FAIL 侧抹平）──
check(calib.ci_margin_pp((20.01, 23.54), 20.0) == -0.01,
      "G4/H4：CI=[20.01,23.54] 整段在线上方（FAIL 侧）⇒ 裕度 -0.01pp（贴线被暴露）",
      str(calib.ci_margin_pp((20.01, 23.54), 20.0)))
check(calib.ci_margin_pp((10.0, 15.0), 20.0) == 5.0,
      "G4/H4：CI 整段在线下方（PASS 侧）⇒ 裕度 +5.0pp",
      str(calib.ci_margin_pp((10.0, 15.0), 20.0)))
check(calib.ci_margin_pp((25.0, 30.0), 20.0) == -5.0,
      "H4：CI 整段在线上方（FAIL 侧）⇒ 裕度 -5.0pp（旧码与 PASS 侧同为 +5.0，方向被抹平）",
      str(calib.ci_margin_pp((25.0, 30.0), 20.0)))
check(calib.ci_margin_pp((15.0, 25.0), 20.0) == 0.0,
      "H4：CI 跨判据线 ⇒ 裕度 0.0（谈不上裕度，不是「较小的那个距离」）",
      str(calib.ci_margin_pp((15.0, 25.0), 20.0)))
check(calib.ci_margin_pp(None, 20.0) is None, "G4：ci=None → None（不崩）")

# ── 复验 G5 + H3：分子组理由折叠（同源才折叠，判据用**观测相对**）──
def _mol_fakes(cov, hole, expect=24):
    present = round(cov * expect)
    return {"expect_hours": expect, "hours_present": present,
            "hours_present_ratio": cov, "max_hole_hours": hole}


_f1 = calib.molecule_fail_reasons(_mol_fakes(0.4167, 14))
check(len(_f1) == 1 and "全部缺失" in _f1[0] and "整点覆盖" in _f1[0],
      "G5：hole=14h 恰为全部缺失（24-10）⇒ 折叠为 1 条（真同源）", str(_f1))
_f2 = calib.molecule_fail_reasons(_mol_fakes(0.4167, 2))
check(len(_f2) == 1 and "整点覆盖" in _f2[0] and "分散形态" in _f2[0],
      "G5：2h 空洞（未超限）+ 41.7% 覆盖 ⇒ 只报覆盖、并注明「分散形态」", str(_f2))
_f3 = calib.molecule_fail_reasons(_mol_fakes(0.9167, 5))
check(len(_f3) == 1 and "最长连续空洞 5h" in _f3[0] and "整点覆盖" not in _f3[0],
      "G5：5h 空洞但覆盖 91.7% 合格 ⇒ 只报空洞", str(_f3))
check(calib.molecule_fail_reasons(_mol_fakes(1.0, 0)) == [],
      "G5：全覆盖无空洞 ⇒ 无理由")
# H3 反例（prod --days 7 真值）：hole=139 < missing=153 ⇒ 另有 14h 散点缺失 ⇒ 两个独立缺陷。
# 旧码用「门槛相对」（139 > 168×0.2 = 33.6）判同源 ⇒ 既印出**事实错误的因果**（声称压到实测
# 8.9%），又吞掉「散点缺失」这一独立缺陷。
_f4 = calib.molecule_fail_reasons({"expect_hours": 168, "hours_present": 15,
                                   "hours_present_ratio": 0.0893, "max_hole_hours": 139})
check(len(_f4) == 2, "H3：hole=139h 但 missing=153h ⇒ 不折叠，报 2 条（散点+长洞是两个缺陷）",
      str(_f4))
check(any("另有散点缺失" in x for x in _f4),
      "H3：覆盖那条须点明「空洞仅占 139h ⇒ 另有散点缺失」", str(_f4))

# ── 复验 I1/I3：**所有**结论类字段一律走映射表，且三表键集一致 ──
check(calib.RELIABLE_BY_CODE == {0: True, 2: True, 3: False, 4: True}
      and calib.DECISIVE_BY_CODE == {0: True, 2: True, 3: False, 4: False},
      "I1/I3：`reliable`/`decisive` 亦由码位映射表派生（不再 `rc != 3` 式「非 3」写法）",
      f"{calib.RELIABLE_BY_CODE} / {calib.DECISIVE_BY_CODE}")
check(set(calib.CONCLUSION_BY_CODE) == set(calib.RELIABLE_BY_CODE)
      == set(calib.DECISIVE_BY_CODE),
      "I1/I3：三张映射表**键集一致** —— 新增码位时三处同时 KeyError 炸响（fail-loud），"
      "不再出现「conclusion 炸了而 reliable/decisive 静默放行」的鲁棒性不对称",
      str(sorted(calib.CONCLUSION_BY_CODE)))
check(calib.DECISIVE_BY_CODE[calib.exit_code(False, False, True)] is False,
      "I1：sample_ok=False ⇒ decisive=False（prod `--days 7 --json` 曾印 decisive=true，"
      "与 rc=3/reliable=false 并列 ⇒ 同类第 5 次复发）",
      str(calib.DECISIVE_BY_CODE[calib.exit_code(False, False, True)]))
check(calib.RELIABLE_BY_CODE[calib.exit_code(False, False, True)] is False,
      "I1/I3：sample_ok=False ⇒ reliable=False（同源，不再各算各的）")

# ── 复验 H1/H2/H5 + I1/I3/I4/I6：源码级守卫（同一缺陷类已**五次**复发 ⇒ 不再只靠人工复核）──
def _code_only(src: str) -> str:
    """剥离行内注释后再匹配（复验 I4：旧守卫直接扫全文 ⇒ 仅因 calib 注释里写的是
    `elif not j["pass"]`（无冒号）才通过；一旦有人在注释/文档里写出带冒号的同名字面量
    就会**误报失败**。注释不是代码，守卫不该耦合注释措辞）。"""
    return "\n".join(ln.split("#", 1)[0] for ln in src.splitlines())


with open(os.path.join(_HERE, "calib_squeeze_liq_thr.py"), encoding="utf-8") as _fh:
    _calib_src = _fh.read()
_calib_code = _code_only(_calib_src)
_j = _calib_code.find('out["judge"] = {')
_jseg = _calib_code[_j:_calib_code.find("\n    }", _j)] if _j >= 0 else ""
check('"conclusion": CONCLUSION_BY_CODE[rc]' in _jseg,
      "G1 守卫：`conclusion` 由 rc 派生", _jseg[:80])
check('"reliable": RELIABLE_BY_CODE[rc]' in _jseg,
      "H1/I3 守卫：`reliable` 走映射表（不再 `rc != 3` 式「非 3」写法 —— 同类第 4 次复发）")
check('"decisive": DECISIVE_BY_CODE[rc]' in _jseg,
      "I1 守卫：`decisive` 收口到映射表（不再由 ci+seg 各自算、不读 sample_ok "
      "—— 同类第 5 次复发）")
check('"ci_decisive_raw": ci_decisive' in _jseg
      and '"segment_straddle_raw": seg_straddle' in _jseg,
      "I1 守卫：CI/分段的原始性质改名 `*_raw` 保留（避免与三个已收口字段并列时被误读为结论）")
check('"exit_code": rc' in _jseg, "G3 守卫：`exit_code` 与 conclusion 同源（均为 rc）")
check('elif not j["pass"]:' not in _calib_code
      and 'elif j["conclusion"] == "FAIL"' in _calib_code,
      "H2 守卫：判定解读按 `conclusion` 精确分派（I4：已在剥离注释后的代码面上匹配 ⇒ "
      "不耦合注释措辞）")
check('if j["upper_bound_pct"] is None:' in _calib_code
      and "上界**不可算**" in _calib_code,
      "H5 守卫：`ub_sub=None` 单列文案（不再印「上界由 None 决定」/「CI 含 20%？」）")
check("退出码 3（样本不可比" not in _calib_code,
      "I6 守卫：H5 文案不再写死「退出码 3」（改用 `{rc}` 动态引用 ⇒ 码表变动时不静默漂移）")

# ════════════════════════════════════════════════════════════
print(f"\n{passed}/{passed + failed} passed")
sys.exit(1 if failed else 0)
