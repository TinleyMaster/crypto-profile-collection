#!/usr/bin/env python3
"""轧空胜负判定单测（工单 SQZ-04）。

运行：python test_squeeze_battle.py

覆盖：
  1) 判定纯函数 `evaluate_battle` 的 7 个注入用例（含缺失数据口径 P1-3）；
  2) 16 个阈值常量断言（断言当前值，含 SQZ-02 重标定后的 LONG_LIQ_RATIO_THR）；
  3) 渲染层 `_render_squeeze_alert` 段内不得残留 `or 0`（None 与 0 必须可区分）。

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
print(f"\n{passed}/{passed + failed} passed")
sys.exit(1 if failed else 0)
