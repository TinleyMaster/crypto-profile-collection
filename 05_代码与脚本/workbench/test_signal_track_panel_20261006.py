#!/usr/bin/env python3
"""早报「信号战绩」板块回归护栏。

设计依据：04_架构与代码方案/早报板块顶层重设计_2026-10-06.md §2.4（信号后验 / 战绩面板）——
把 biz.opportunity_snapshot 已结算的机会前向收益（T+1/T+7）在早报顶部作「信任锚」呈现，
直接回应审计「无后验样本提示」。

覆盖：
  · macro_market._aggregate_signal_track（纯函数：方向对齐命中/均值、backed/other 分书画分）
  · send_daily_brief._render_signal_track_html（纯渲染：有样本/空样本/error 三分支）
  · 源码结构守卫（生成层聚合 + 渲染层挂在告警质量之后 + SQL 只取已结算行）
运行：venv/bin/python workbench/test_signal_track_panel_20261006.py
"""
import os
import sys
from datetime import date

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_BIN = os.path.join(os.path.dirname(_HERE), "scripts", "bin")
_SCRIPTS_SRC = os.path.join(os.path.dirname(_HERE), "scripts", "src")
for _p in (_HERE, _SCRIPTS_BIN, _SCRIPTS_SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import macro_market as mm  # noqa: E402
import send_daily_brief as sdb  # noqa: E402

_passed = 0
_failed = []


def check(cond, name, detail=""):
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS {name}")
    else:
        _failed.append(name)
        print(f"  FAIL {name}" + (f" | {detail}" if detail else ""))


def R(snap, direction, gate, o1=None, o7=None):
    return {"snapshot_date": snap, "direction": direction,
            "calibration_gate": gate, "outcome_1d": o1, "outcome_7d": o7}


# ════════════════════════════════════════════════════════
# A. _aggregate_signal_track 纯函数
# ════════════════════════════════════════════════════════
print("[A] _aggregate_signal_track（方向对齐 + 分书画分）")
try:
    rows = [
        R(date(2026, 10, 1), "long", "calibrated_ok", 5.0),   # long +5 → 命中
        R(date(2026, 10, 1), "short", "preliminary", -3.0),   # short -3 → 命中（aligned +3）
    ]
    agg = mm._aggregate_signal_track(rows)
    d1 = agg["1d"]
    check(d1["n"] == 2 and d1["hit"] == 1.0 and d1["mean_pct"] == 4.0,
          "long+5 / short-3 → n=2 命中100% 均值+4.00", str(d1))
    check(agg["backed_1d"]["n"] == 1 and agg["backed_1d"]["mean_pct"] == 5.0,
          "backed_1d 只含 calibrated_ok（5.0）", str(agg["backed_1d"]))
    check(agg["other_1d"]["n"] == 1 and agg["other_1d"]["mean_pct"] == 3.0,
          "other_1d 含 preliminary（short -3 → aligned +3）", str(agg["other_1d"]))
    check(agg["watch_n"] == 0 and agg["as_of"] == "2026-10-01", "watch=0 / as_of=最新结算日")

    rows2 = rows + [
        R(date(2026, 10, 1), "long", "calibrated_ok", -5.0),   # long -5 → 未命中
        R(date(2026, 10, 2), "watch", "missing_calibration"),  # watch 无预期
    ]
    agg2 = mm._aggregate_signal_track(rows2)
    d1b = agg2["1d"]
    check(d1b["n"] == 3 and d1b["hit"] == round(2 / 3, 4) and abs(d1b["mean_pct"] - (5 + 3 - 5) / 3) < 1e-9,
          "加入未命中的一行 → n=3 命中 2/3 均值 1.0", str(d1b))
    check(agg2["watch_n"] == 1 and agg2["as_of"] == "2026-10-01",
          "watch 行不计命中/不计 as_of（无 outcome）", f"{agg2['watch_n']} / {agg2['as_of']}")

    empty = mm._aggregate_signal_track([])
    check(empty["1d"]["n"] == 0 and empty["1d"]["hit"] is None and empty["1d"]["mean_pct"] is None,
          "空行 → 全 None", str(empty["1d"]))
    check(empty["as_of"] is None, "空行 → as_of None")

    # 7d 窗口：只有 outcome_7d 有值时计入
    rows3 = [R(date(2026, 9, 28), "long", "calibrated_ok", None, 10.0)]
    agg3 = mm._aggregate_signal_track(rows3)
    check(agg3["1d"]["n"] == 0 and agg3["7d"]["n"] == 1 and agg3["7d"]["hit"] == 1.0,
          "仅 7d 结算 → 1d 空 / 7d n=1", str(agg3["7d"]))
except Exception as _e:
    check(False, "[A] 执行", f"{type(_e).__name__}: {_e}")


# ════════════════════════════════════════════════════════
# B. _render_signal_track_html 渲染
# ════════════════════════════════════════════════════════
print("[B] _render_signal_track_html（空/error/有样本）")
try:
    check(sdb._render_signal_track_html({}) == "", "空 dict → 空串")
    check(sdb._render_signal_track_html({"error": "boom"}) == "", "error → 空串")
    check(sdb._render_signal_track_html(None) == "", "None → 空串")
    _h0 = sdb._render_signal_track_html(mm._aggregate_signal_track([]))
    check("暂无已结算样本" in _h0, "无已结算样本 → 诚实占位")

    _h = sdb._render_signal_track_html(mm._aggregate_signal_track([
        R(date(2026, 10, 1), "long", "calibrated_ok", 5.0),
        R(date(2026, 10, 1), "short", "preliminary", -3.0),
        R(date(2026, 10, 1), "long", "preliminary", -5.0),
    ]))
    check("📈 信号战绩" in _h and "T+1 已结算 3 条" in _h, "有样本 → 标题与 T+1 计数")
    check("命中" in _h and "均值" in _h, "命中与均值字段在场")
    check("按回测背书" in _h and "有背书 1 条" in _h and "无/未回测 2 条" in _h,
          "按背书分画：backed 1 / other 2", _h[:400])
    check("方向对齐" in _h and "未计交易成本" in _h, "口径脚注在场")
    check("样本覆盖截至 2026-10-01" in _h, "as_of 披露")
    check("**" not in _h, "HTML 渲染无 markdown 强调符（护栏）")
except Exception as _e:
    check(False, "[B] 执行", f"{type(_e).__name__}: {_e}")


# ════════════════════════════════════════════════════════
# C. 源码结构守卫
# ════════════════════════════════════════════════════════
print("[C] 源码结构守卫（生成层 + 渲染层 + SQL）")
try:
    _mm_src = open(os.path.join(_HERE, "macro_market.py"), encoding="utf-8").read()
    _sd_src = open(os.path.join(_HERE, "..", "scripts", "bin", "send_daily_brief.py"),
                   encoding="utf-8").read()

    check("def _aggregate_signal_track(rows: list)" in _mm_src, "聚合纯函数存在")
    check("def _load_signal_track_record()" in _mm_src, "加载函数存在")
    check('"M0_signal_track": _load_signal_track_record(),' in _mm_src,
          "生成层把 M0_signal_track 装进 brief")
    check("outcome_1d IS NOT NULL OR outcome_7d IS NOT NULL" in _mm_src,
          "SQL 只取已结算行")
    check("_render_signal_track_html(brief.get(\"M0_signal_track\")" in _sd_src,
          "渲染层挂接 M0_signal_track")
    check("模块 0.2：📉 告警质量" in _sd_src
          and _sd_src.find("html_parts.append(_render_signal_track_html") > _sd_src.find("模块 0.2：📉 告警质量"),
          "信号战绩版块位于告警质量（信任锚区）之后")
except Exception as _e:
    check(False, "[C] 执行", f"{type(_e).__name__}: {_e}")


# ════════════════════════════════════════════════════════
# D. 真库功能校验（有 DATABASE_URL 才跑，否则跳过）
# ════════════════════════════════════════════════════════
try:
    _root = os.path.abspath(os.path.join(_HERE, "..", "scripts"))
    _env = {}
    _envf = os.path.join(_root, ".env")
    if os.path.exists(_envf):
        for _line in open(_envf, encoding="utf-8", errors="ignore"):
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                _env[_k.strip()] = _v.strip().strip('"').strip("'")
    if _env.get("DATABASE_URL"):
        _rec = mm._load_signal_track_record()
        check(not _rec.get("error"), "真库加载不报错", str(_rec.get("error"))[:120])
        check(_rec.get("1d") is not None and _rec.get("7d") is not None,
              "真库返回 1d/7d 结构", str(list(_rec.keys())))
        _h = sdb._render_signal_track_html(_rec)
        check(("信号战绩" in _h) or (_h == ""), "真库数据可渲染（或不出外壳）")
    else:
        print("  [SKIP] 无 DATABASE_URL → 跳过真库校验")
except Exception as _e:
    print(f"  [SKIP] 真库校验未执行：{type(_e).__name__}: {_e}")


print()
if _failed:
    print(f"[FAIL] {len(_failed)} 项失败：")
    for _f in _failed:
        print(f"   - {_f}")
    sys.exit(1)
print(f"[OK] 全部通过（{_passed}）")
sys.exit(0)