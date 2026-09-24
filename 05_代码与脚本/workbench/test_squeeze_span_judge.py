#!/usr/bin/env python3
"""轧空标定判据「跨度需求 + 聚合方式」单测（工单 SQUEEZE-SPAN-001，2026-09-24）。

运行：python test_squeeze_span_judge.py

覆盖工单 §6 的三条 P1（A 段层面稳健统计 / B 极端段显式列出 / C 跨度充分性前置门）
与 §6-D/E，按 §7 验收的**注入法**：

  1) `segment_judge`：A 的注入（「5 段 10% + 1 段 70%」→ 中位数 10%，而非合并均值 ~20%）、
     「全部 22%」→ 22%、`decisive` 的 IQR 跨线边界、B 的极端段清单；
  2) `span_sufficiency`：C 的四象限（跨度/极端段各自单独触发）；
  3) 常量与源码接线守卫（判据输入改段中位数、sample_ok 纳入 span、段上界前移、无跨度承诺）；
  4) **C 的端到端注入**：桩接 `psycopg.connect`/`get_settings`，构造「分母/分子合格、跨度不足」
     的样本 ⇒ 断言 rc=3、**未打印**判据输入比率、且段上界仍打印（E）。

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
# 3. 源码接线守卫（§6-A/B/C/D/E）
# ════════════════════════════════════════════════════════════
print("\n【测试3】源码接线守卫")
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
      "C：拒绝出结论时打印跨度理由（不给会被误读的比率）")
check("段上界】← 工单 §6-E" in _src
      and _src.index("段上界】← 工单 §6-E") < _src.index("if not sample_ok:"),
      "E：段上界输出前移到前置门之前（任何路径都能看到）")
check('"extreme_segments": sj["extreme_segments"]' in _src
      and "极端段（上界 ≥" in _src,
      "B：极端段显式列出（JSON 字段 + 文本渲染）")
check("非承诺" in _src and "量级估计" in _src,
      "D：文档写明跨度量级是「量级估计、非承诺」（不得写『再等 N 天就能判』）")

# ════════════════════════════════════════════════════════════
# 4. C 的端到端注入：桩接 DB，构造「分母/分子合格、跨度不足」样本
# ════════════════════════════════════════════════════════════
print("\n【测试4】C 端到端注入（rc=3 且不打印判据输入比率）")


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
# 分母合格：每币 288 根（days=1）；分子合格：24/24 整点、无长洞；跨度仅 71h ⇒ 跨度门拦下
_bound = {"mn": _now - dt.timedelta(hours=71), "mx": _now, "n": 1000, "syms": 2}
_sample = [{"symbol": "AAAUSDT", "ts": _now - dt.timedelta(hours=1),
            "long_liq": 1.0, "short_liq": 1.0, "vol_win": 1e9,
            "peak_hi": 1.0, "trough_lo": 1.0, "peak_c": 1.0, "trough_c": 1.0,
            "close_now": 1.0},
           {"symbol": "BBBUSDT", "ts": _now - dt.timedelta(hours=1),
            "long_liq": 1.0, "short_liq": 1.0, "vol_win": 1e9,
            "peak_hi": 1.0, "trough_lo": 1.0, "peak_c": 1.0, "trough_c": 1.0,
            "close_now": 1.0}]
_denom = [{"symbol": "AAAUSDT", "bars": 288, "hours": 24},
          {"symbol": "BBBUSDT", "bars": 288, "hours": 24}]
_mol = [{"n": 5}] * 24
_incl0 = [{"ratio": 0.0001}]

_orig_connect, _orig_settings = calib.psycopg.connect, calib.get_settings
calib.psycopg.connect = lambda *a, **k: _FakeConn(_FakeCursor(_bound, _sample, _denom, _mol, _incl0))
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
check("【判定】" not in _out and "判据输入（段median）" not in _out,
      "C：**未打印**判据输入/判定比率（避免被误读成「判据不通过」）")
check("【各 4h 段上界】" in _out,
      "E：即使样本不可用，段上界仍打印（诊断入口）")

print(f"\n{passed}/{passed + failed} passed")
sys.exit(1 if failed else 0)
