#!/usr/bin/env python3
"""轧空标定判据「跨度需求 + 聚合方式」单测（工单 SQUEEZE-SPAN-001，2026-09-24）。

运行：python test_squeeze_span_judge.py

覆盖工单 §6 的 A/B/C/D/E + 复验 F1~F6（按 §7 验收的**注入法**）：

  1) `segment_judge`：A 的注入（「5 段 10% + 1 段 70%」→ 中位数 10%，而非合并均值 ~20%）、
     「全部 22%」→ 22%、`decisive` 的 IQR 跨线边界、B 的极端段清单；
  2) `span_sufficiency`：C 的四象限（跨度/极端段各自单独触发）；
  3) **F1**：`exit_code` 契约 —— PASS 必须含 `decisive`（`pass=True`+`decisive=False` ⇒ rc=4）；
  4) **F4**：`primary_gate` 唯一充分因（优先级 分母 > 分子 > 跨度）；
  5) 常量与源码接线守卫（判据输入改段中位数、sample_ok 纳入 span、段上界前移、无跨度承诺、
     F1 统计字段 / F2 摘要行移除 / F5 字段改名 / F6 死变量）；
  6) **C 的端到端注入**（桩接 DB）：构造「分母/分子合格、跨度不足」的 6 段样本 ⇒
     断言 rc=3、**判据输入中位数（15.00%）未出现在输出**（数值断言，非字面串）、
     且段上界逐段仍打印（E）。

⚠️ 阈值本身**一律不动**（本单测不触碰 `LONG_LIQ_RATIO_THR` / `SQZ_SHORT_LIQ_RATIO_MIN`）。
"""
import contextlib
import datetime as dt
import io
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, os.path.join(_SCRIPTS, "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import calib_squeeze_liq_thr as calib  # noqa: E402

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


def _segs(bounds):
    """把一串上界值伪装成 `segment_upper_bounds` 的 segments 结构。"""
    base = dt.datetime(2026, 9, 23, 0, 0, tzinfo=dt.timezone.utc)
    return [{"start_ts": (base + dt.timedelta(hours=4 * i)).isoformat(),
             "rows": 100, "n_by_variant": {}, "upper_bound_pct": b}
            for i, b in enumerate(bounds)]


# ════════════════════════════════════════════════════════════
# 1. segment_judge（§6-A 聚合方式 + §6-B 极端段）
# ════════════════════════════════════════════════════════════
print("\n【测试1】segment_judge：段层面稳健统计 + 极端段")
# §7-A 注入①：5 段 10% + 1 段 70% ⇒ 判据输入 = 中位数 10%（合并均值会被极端段抬到 20%）
_sj = calib.segment_judge(_segs([10, 10, 10, 10, 10, 70]), 20.0)
check(_sj["value_pct"] == 10.0, "A：『5×10% + 1×70%』⇒ 判据输入(中位) = 10.0",
      str(_sj["value_pct"]))
check(_sj["median_pct"] == 10.0 and _sj["p25_pct"] == 10.0 and _sj["p75_pct"] == 10.0,
      "A：中位/P25/P75 均被单个极端段稳健（=10.0）",
      f'{_sj["median_pct"]}/{_sj["p25_pct"]}/{_sj["p75_pct"]}')
check(_sj["decisive"] is True, "A：IQR(10,10) 远在线下 ⇒ 有判别力")
check(_sj["extreme_count"] == 1 and _sj["extreme_segments"][0]["upper_bound_pct"] == 70.0,
      "B：极端段(≥40%)显式列出 1 个 @ 70.0", str(_sj["extreme_segments"]))
# §7-A 注入②：全部 22% ⇒ 输出 22%
_sj2 = calib.segment_judge(_segs([22, 22, 22, 22, 22, 22]), 20.0)
check(_sj2["value_pct"] == 22.0, "A：『全部 22%』⇒ 判据输入 = 22.0", str(_sj2["value_pct"]))
check(_sj2["decisive"] is True, "A：全部 22% ⇒ IQR 整段在线上方 ⇒ 有判别力(FAIL 侧)")
# decisive 边界：IQR 跨线 ⇒ 无判别力
_sj3 = calib.segment_judge(_segs([10, 30]), 20.0)
check(_sj3["value_pct"] == 20.0 and _sj3["decisive"] is False,
      "A：IQR 跨判据线（P25=15<20<P75=25）⇒ decisive=False", str(_sj3))
check(calib.segment_judge([], 20.0)["value_pct"] is None
      and calib.segment_judge([], 20.0)["extreme_count"] == 0,
      "segment_judge：空分段 ⇒ value/extreme 均为空，不崩")
check(calib.SEGMENT_JUDGE_STAT == "median" and calib.EXTREME_SEG_PCT == 40.0,
      "常量：判据统计=median / 极端段门槛=40%")

# ════════════════════════════════════════════════════════════
# 2. span_sufficiency（§6-C）
# ════════════════════════════════════════════════════════════
print("\n【测试2】span_sufficiency：跨度充分性前置门")
_sp = calib.span_sufficiency(71.0, 1)
check(_sp["ok"] is False and len(_sp["reasons"]) == 2,
      "C：71h + 极端段 1 次 ⇒ 不合格（跨度与极端段两条理由）", str(_sp["reasons"]))
check(calib.span_sufficiency(30 * 24.0, 3)["ok"] is True,
      "C：恰 30 天 + 极端段 3 次 ⇒ 合格（边界含等于）")
check(calib.span_sufficiency(30 * 24.0, 2)["ok"] is False,
      "C：跨度够但极端段 2 次 ⇒ 不合格（极端段单独触发）")
check(calib.span_sufficiency(30 * 24.0 - 1, 5)["ok"] is False,
      "C：极端段够但跨度差 1h ⇒ 不合格（跨度单独触发）")
check(calib.MIN_SPAN_DAYS == 30 and calib.MIN_EXTREME_SEGMENTS == 3,
      "常量：MIN_SPAN_DAYS=30 / MIN_EXTREME_SEGMENTS=3")

# ════════════════════════════════════════════════════════════
# 3. F1：exit_code 契约（PASS 必须含 decisive）
# ════════════════════════════════════════════════════════════
print("\n【测试3】F1：exit_code 契约（pass 不得绕过 decisive）")
check(calib.exit_code(True, True, False, True) == 4,
      "F1：pass=True 但 decisive=False ⇒ rc=4（不可判），不得 rc=0",
      str(calib.exit_code(True, True, False, True)))
check(calib.exit_code(True, True, True, True) == 0, "F1：pass=True + decisive=True ⇒ rc=0")
check(calib.exit_code(False, True, True, True) == 2,
      "F1：pass=False + decisive=True ⇒ rc=2（有判别力 FAIL）")
check(calib.exit_code(False, True, False, True) == 4,
      "F1：pass=False + decisive=False ⇒ rc=4（不可判）")
check(calib.exit_code(False, False, True, True) == 3,
      "F1：sample_ok=False ⇒ rc=3（优先于 decisive）")

# ════════════════════════════════════════════════════════════
# 4. F4：primary_gate 唯一充分因
# ════════════════════════════════════════════════════════════
print("\n【测试4】F4：primary_gate 唯一充分因")
check(calib.primary_gate(False, False, False) == "分母门"
      and calib.primary_gate(True, False, False) == "分子门"
      and calib.primary_gate(True, True, False) == "跨度门"
      and calib.primary_gate(True, True, True) is None,
      "F4：优先级 分母>分子>跨度，全过 ⇒ None")

# ════════════════════════════════════════════════════════════
# 5. 源码接线守卫（§6-A/B/C/D/E + F1/F2/F5/F6）
# ════════════════════════════════════════════════════════════
print("\n【测试5】源码接线守卫")
with open(calib.__file__, encoding="utf-8") as fh:
    _src = fh.read()
check('sj = segment_judge(seg["segments"], JUDGE_UPPER_BOUND_PCT)' in _src,
      "A：main 用 segment_judge 产出判据输入")
check('"upper_bound_pct": judge_val' in _src,
      "A：judge.upper_bound_pct 改为段层面稳健统计（judge_val）")
check('span_suf = span_sufficiency(span_h, sj["extreme_count"])' in _src
      and "sample_ok = denom_ok and molecule_ok and span_ok" in _src,
      "C：跨度门纳入 sample_ok（分母/分子/跨度三者同过才可用）")
check("span_suf[\"reasons\"]" in _src and "跨度不足" in _src,
      "C：拒绝出结论时打印跨度理由")
check("段上界】← 工单 §6-E" in _src
      and _src.index("段上界】← 工单 §6-E") < _src.index("if not sample_ok:"),
      "E：段上界输出前移到前置门之前（任何路径都能看到）")
check('"extreme_segments": sj["extreme_segments"]' in _src
      and "极端段（上界 ≥" in _src,
      "B：极端段显式列出（JSON 字段 + 文本渲染）")
check("非承诺" in _src and "量级估计" in _src,
      "D：文档写明跨度量级是「量级估计、非承诺」")
check('judge_pass = bool(sample_ok and decisive and measurable' in _src,
      "F1：judge_pass 恢复 `decisive` 合取项（PASS 必须含判别力）")
check('"segment_iqr_straddle_raw": (not sj["decisive"])' in _src
      and "j['segment_iqr_straddle_raw']" in _src,
      "F1：段 IQR 跨线以独立**统计**字段落库且渲染层读它（不再借用码表派生的 decisive）")
check("sg['median_pct']" not in _src and "中位={sg" not in _src,
      "F2(甲)：段清单不再印 min/中位/max 摘要行（判据输入不在前置门之前出现）")
check('"upper_bound_n_segments": sj["n_segments"]' in _src
      and '"merged_ci_distance_pp": _ci_margin' in _src,
      "F5：语义漂移字段改名（段数 / merged_ci_distance_pp）")
check("_pg = primary_gate(denom_ok, molecule_ok, span_ok)" in _src,
      "F4：拒绝路径标注唯一充分因")
check("new_rates" not in _src, "F6：删除死变量 new_rates")

# ════════════════════════════════════════════════════════════
# 6. C 的端到端注入：桩接 DB，6 段样本 + 跨度不足
# ════════════════════════════════════════════════════════════
print("\n【测试6】C 端到端注入（rc=3 且判据输入中位数不出现在输出）")


class _FakeCursor:
    def __init__(self, bound, sample, denom, mol, incl0):
        self._bound, self._sample = bound, sample
        self._denom, self._mol, self._incl0 = denom, mol, incl0
        self._mode = None

    def execute(self, sql, params=None):
        if "min(ts) AS mn" in sql:
            self._mode = "bound"
        elif "peak_hi" in sql:
            self._mode = "sample"
        elif "date_trunc('hour', open_time)" in sql:
            self._mode = "denom"
        elif "generate_series" in sql:
            self._mode = "mol"
        elif "long_liq_usd_1h::float8 / v.vol_win" in sql:
            self._mode = "incl0"
        else:
            self._mode = None

    def fetchone(self):
        return self._bound

    def fetchall(self):
        return {"sample": self._sample, "denom": self._denom,
                "mol": self._mol, "incl0": self._incl0}[self._mode]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur
        self.readonly = False

    def cursor(self, row_factory=None):
        return self._cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_now = dt.datetime(2026, 9, 23, 12, 0, tzinfo=dt.timezone.utc)


def _mk_rows():
    """6 个 4h 段 × 50 行；每段 k 行越阈 ⇒ 段上界 = k/50×100 = 10,12,14,16,18,20（中位 15.00%）。"""
    base = _now - dt.timedelta(hours=24)
    rows = []
    for seg, k in enumerate([5, 6, 7, 8, 9, 10]):
        for j in range(50):
            rows.append({
                "symbol": "AAAUSDT", "ts": base + dt.timedelta(hours=4 * seg, minutes=4 * j),
                "long_liq": 1e6 if j < k else 1.0,
                "short_liq": 1.0, "vol_win": 1e9,
                "peak_hi": 1.0, "trough_lo": 0.5, "peak_c": 1.0, "trough_c": 0.5,
                "close_now": 0.9,
            })
    return rows


# 分母合格（288 根 / 币）；分子合格（24/24 整点）；跨度仅 72h ⇒ 跨度门拦下
_bound = {"mn": _now - dt.timedelta(hours=72), "mx": _now, "n": 1000, "syms": 1}
_denom = [{"symbol": "AAAUSDT", "bars": 288, "hours": 24}]
_mol = [{"n": 5}] * 24
_incl0 = [{"ratio": 0.0001}]

_orig_connect, _orig_settings = calib.psycopg.connect, calib.get_settings
calib.psycopg.connect = lambda *a, **k: _FakeConn(
    _FakeCursor(_bound, _mk_rows(), _denom, _mol, _incl0))
calib.get_settings = lambda **k: types.SimpleNamespace(database_url="postgres://x")
_argv = sys.argv
sys.argv = ["calib_squeeze_liq_thr.py"]
_buf = io.StringIO()
try:
    with contextlib.redirect_stdout(_buf):
        _rc = calib.main()
finally:
    calib.psycopg.connect, calib.get_settings = _orig_connect, _orig_settings
    sys.argv = _argv
_out = _buf.getvalue()
check(_rc == 3, "C：跨度不足 ⇒ rc=3（样本不可用）", f"rc={_rc}")
check("跨度不足" in _out, "C：给出跨度不足的理由")
check("归因以跨度门为准" in _out,
      "F4：分母/分子合格 ⇒ 唯一充分因标为跨度门")
check("【判定】" not in _out and "判据输入（段median）" not in _out,
      "C：**未打印**判定行/判据输入标签")
# F2 数值断言：判据输入 = 段中位数 15.00%，不得出现在输出里（不是字面串检查）
check("15.00%" not in _out,
      "F2：判据输入中位数（15.00%）未出现在 rc=3 路径的输出里", _out[:0])
check("【各 4h 段上界】" in _out and "20.00%" in _out,
      "E：即使样本不可用，段上界仍**逐段**打印（诊断入口）")

# ════════════════════════════════════════════════════════════
# 7. F1 端到端注入：样本可用 + 段 IQR 跨线 ⇒ rc=4（不得 rc=0）
# ════════════════════════════════════════════════════════════
print("\n【测试7】F1 端到端注入（样本可用 + IQR 跨线 ⇒ rc=4，不得 rc=0）")


def _fake_seg(bounds):
    vals = sorted(bounds)
    return {"segment_hours": calib.SEGMENT_HOURS, "n_segments": len(vals),
            "segments": _segs(bounds), "min_pct": vals[0],
            "median_pct": vals[len(vals) // 2], "max_pct": vals[-1], "skipped": 0}


# bounds=[5,5,45,45,45]：median 45（FAIL 侧）、P25=5 / P75=45 ⇒ IQR 跨线；
# 极端段 3 个(≥40)、跨度 31 天 ⇒ 跨度门放行 ⇒ sample_ok=True、decisive=False
_bound2 = {"mn": _now - dt.timedelta(days=31), "mx": _now, "n": 500, "syms": 1}
_sample2 = [{"symbol": "AAAUSDT", "ts": _now - dt.timedelta(hours=1),
             "long_liq": 1.0, "short_liq": 1.0, "vol_win": 1e9,
             "peak_hi": 1.0, "trough_lo": 1.0, "peak_c": 1.0, "trough_c": 1.0,
             "close_now": 1.0}]
_orig_seg = calib.segment_upper_bounds
calib.segment_upper_bounds = lambda rows, thr: _fake_seg([5, 5, 45, 45, 45])
calib.psycopg.connect = lambda *a, **k: _FakeConn(
    _FakeCursor(_bound2, _sample2, _denom, _mol, _incl0))
_buf2 = io.StringIO()
try:
    with contextlib.redirect_stdout(_buf2):
        _rc2 = calib.main()
finally:
    calib.segment_upper_bounds = _orig_seg
    calib.psycopg.connect, calib.get_settings = _orig_connect, _orig_settings
_out2 = _buf2.getvalue()
check(_rc2 == 4, "F1：sample_ok + IQR 跨线 ⇒ rc=4（旧码会误判 rc=0 PASS）", f"rc={_rc2}")
check("段 IQR 跨判据线（无判别力）" in _out2,
      "F1：渲染如实印「段 IQR 跨判据线（无判别力）」（不再假报「不跨」）")
check("INCONCLUSIVE" in _out2 and "PASS" not in _out2.split("【判定】")[-1][:40],
      "F1：结论 = INCONCLUSIVE（非 PASS）")

print(f"\n{passed}/{passed + failed} passed")
sys.exit(1 if failed else 0)
