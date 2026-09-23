#!/usr/bin/env python3
"""拉升期结构分解 / 轧空衰竭判定单测（设计方案 §10.10）。

运行：python test_squeeze_fuel.py

覆盖：
  1) 阈值常量（含「分界一律以正数幅度存储」这条约定）；
  2) 两个**代理口径**：`proxy_shares`（占比反推）与 `quadrant`（OI×CVD 四象限）；
  3) `buy_to_close_share` / `price_cvd_divergence` / `proxy_extremes` / `liq_extremes`
     / `divergence_inputs`；
  4) `classify_fuel` 五个 verdict + 优先级 + 缺失降级 + 「衰减必须先有峰值」；
  5) `fuel_gate` 五道闸门；
  6) `evaluate_fuel` 端到端（耗尽正例 / 未越阈 / 闸门未过）；
  7) 源码级守卫（daemon 接线：limit=100、merge 语义、影子期不新增发送路径、
     BUCKET_SECONDS 同源）。

⚠️ 本模块**全为未标定的经验初值 + 代理口径**（§10.10.4 / §10.10.8），
   单测只钉「结构/口径/优先级」，**不**对阈值取值本身下结论。
"""
import ast
import datetime as dt
import inspect
import io
import os
import sys
import tokenize

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, os.path.join(_SCRIPTS, "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.analysis import squeeze as sqz  # noqa: E402
from crypto_research.analysis import squeeze_fuel as sf  # noqa: E402

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
# 1. 阈值常量（§10.10.4）
# ════════════════════════════════════════════════════════════
print("\n【测试1】阈值常量与枚举")
CONSTS = {
    "OI_UP_THR": 0.3, "OI_DOWN_THR": 0.3,
    "SHORT_ADD_THR": 3.0, "SHORT_CUT_THR": 3.0,
    "LONG_ADD_THR": 3.0, "LONG_FLAT_THR": 1.0,
    "LIQ_DECAY_THR": 0.5, "MIN_LSR_POINTS": 6, "MIN_FUEL_BUCKETS": 3,
    "FUEL_METRIC_VER": 1, "BUCKET_SECONDS": 300,
}
for name, want in CONSTS.items():
    check(getattr(sf, name, None) == want, f"{name} == {want}",
          f"实际 {getattr(sf, name, '<无>')}")
# 分界一律**正数幅度**存储（判据自行取负）——若有人改成 -0.3 会让 `dOI <= -OI_DOWN_THR`
# 变成 `dOI <= +0.3`，把「OI 净增」判成「平仓驱动」，是静默的方向性错误。
check(sf.OI_DOWN_THR > 0 and sf.SHORT_CUT_THR > 0,
      "净减分界为正数幅度（判据取负，避免 ±双写导致符号漂移）")
check(set(sf.VERDICT_LABEL) == {sf.FUEL_EXHAUSTING, sf.FUEL_ACTIVE, sf.LONG_PUMP,
                                sf.SHORT_REBUILD, sf.MIXED},
      "VERDICT_LABEL 覆盖全部五个 verdict（无遗漏、无别名）",
      str(sorted(sf.VERDICT_LABEL)))

# ════════════════════════════════════════════════════════════
# 2. 代理口径：占比反推 + 四象限
# ════════════════════════════════════════════════════════════
print("\n【测试2】代理口径 proxy_shares / quadrant")
check(sf.proxy_shares(1.0) == (0.5, 0.5), "r=1 → (0.5, 0.5)", str(sf.proxy_shares(1.0)))
_l, _s = sf.proxy_shares(3.0)
check(abs(_l - 0.75) < 1e-12 and abs(_s - 0.25) < 1e-12,
      "r=3 → (0.75, 0.25)（与 Binance longAccount/shortAccount 同义）", f"{_l}/{_s}")
_l, _s = sf.proxy_shares(0.25)
check(abs(_l + _s - 1.0) < 1e-12, "多空占比之和恒为 1")
check(sf.proxy_shares(None) == (None, None) and sf.proxy_shares(0) == (None, None)
      and sf.proxy_shares(-2) == (None, None),
      "r 缺失/0/负 → (None, None)（绝不补 0 冒充占比）",
      f"{sf.proxy_shares(None)} {sf.proxy_shares(0)} {sf.proxy_shares(-2)}")

QUADS = [
    ((-1.0, 1.0), sf.BUY_CLOSE, "OI↓ & CVD>0 → 买平（空头回补）"),
    ((-1.0, -1.0), sf.SELL_CLOSE, "OI↓ & CVD<0 → 卖平（多头离场）"),
    ((1.0, 1.0), sf.LONG_OPEN, "OI↑ & CVD>0 → 新多开仓"),
    ((1.0, -1.0), sf.SHORT_OPEN, "OI↑ & CVD<0 → 新空开仓"),
    ((0.0, 1.0), None, "OI 变化为 0 → 方向无意义（None，不补 0）"),
    ((1.0, 0.0), None, "CVD 为 0 → 方向无意义（None）"),
    ((None, 1.0), None, "OI 缺失 → None"),
    ((1.0, None), None, "CVD 缺失 → None"),
]
for (d_oi, cvd), want, title in QUADS:
    check(sf.quadrant(d_oi, cvd) == want, title, str(sf.quadrant(d_oi, cvd)))

# ════════════════════════════════════════════════════════════
# 3. 序列级纯函数
# ════════════════════════════════════════════════════════════
print("\n【测试3】buy_to_close_share / 背离 / 代理极值 / 爆仓极值 / 序列对齐")
_NOW = dt.datetime(2026, 9, 23, 12, 0, tzinfo=dt.timezone.utc)
_START = _NOW - dt.timedelta(minutes=60)          # 13 个 5m 桶（含两端）


def _ts(i, start=_START):
    return start + dt.timedelta(minutes=5 * i)


def _oi(oi_vals, cvd_vals, start=_START):
    return [{"ts": _ts(i, start), "oi_usd": v, "cvd_5m_usd": cvd_vals[i],
             "vol_5m_usd": 1_000_000.0} for i, v in enumerate(oi_vals)]


def _klines(px_vals, start=_START):
    return [{"open_time": _ts(i, start), "close_px": v} for i, v in enumerate(px_vals)]


def _lsr(r_vals, start=_START):
    return [(_ts(i, start), r) for i, r in enumerate(r_vals)]


def _liq(vals, start=_START):
    return [{"ts": _ts(i, start), "short_liq_usd_1h": v} for i, v in enumerate(vals)]


# buy_to_close_share：OI [100,90,95,100]，CVD [1,1,-1,1] → 1 买平 / 3 有效桶
_btc = sf.buy_to_close_share(_oi([100, 90, 95, 100], [1.0, 1.0, -1.0, 1.0]))
check(_btc is not None and abs(_btc - 1 / 3) < 1e-12,
      "buy_to_close_share = 买平桶/有效桶 = 1/3", str(_btc))
check(sf.buy_to_close_share(_oi([100, 100, 100], [1.0, 1.0, 1.0])) is None,
      "OI 无变化 → 有效桶 0 → None（缺失 ≠ 0）")
check(sf.buy_to_close_share([]) is None and sf.buy_to_close_share(_oi([100], [1.0])) is None,
      "不足 2 桶 → None")

# price_cvd_divergence：末点价格新高 + 累计 CVD 未新高
check(sf.price_cvd_divergence([1, 2, 2.5], [1, 3, 2]) is True,
      "价格新高但累计 CVD 未新高 → True")
check(sf.price_cvd_divergence([1, 2, 3], [1, 2, 3]) is False,
      "价格与 CVD 同步新高 → 不构成背离（False）")
check(sf.price_cvd_divergence([1, 2, 1.5], [1, 2, 1]) is False,
      "末点价格未创新高 → 不构成背离（False，与「CVD 未新高」必须区分）")
check(sf.price_cvd_divergence([1], [1]) is None
      and sf.price_cvd_divergence([1, 2], [1]) is None,
      "序列不足/不等长 → None（缺失 ≠ False）")

# proxy_extremes：基准桶必须取「ts ≤ LSR 点」的最近一条，可落到窗口左端之外
# （W 内首个 OI 桶晚于首个 LSR 点 ⇒ 基准桶只能来自窗口之外那条）
_oi_full = [{"ts": _ts(0) - dt.timedelta(minutes=5), "oi_usd": 200.0, "cvd_5m_usd": 1.0,
             "vol_5m_usd": 1.0}] + _oi([90.0, 80.0], [1.0, 1.0],
                                       start=_START + dt.timedelta(minutes=5))
_prox = sf.proxy_extremes(_oi_full, [(_ts(0), 1.0), (_ts(2), 1.2)])
check(abs(_prox["l_first"] - 200.0 * 0.5) < 1e-9,
      "首个 LSR 点的基准桶取到**窗口左端之外**那条（§10.10.7-① 基准桶取错问题的解）",
      str(_prox))
check(_prox["r_first"] == 1.0 and abs(_prox["d_r"] - 0.2) < 1e-12,
      "r_first/r_last/d_r 正确", f"{_prox['r_first']} {_prox['r_last']} {_prox['d_r']}")
check(sf.proxy_extremes(_oi_full, [(_ts(0), None)])["d_l_pct"] is None,
      "LSR 值缺失 → 该点不可用 → d_l_pct None")
check(sf.proxy_extremes([], [(_ts(0), 1.0)])["l_first"] is None,
      "无 OI 桶可配 → 全部 None")
check(sf.proxy_extremes(_oi_full, [(_ts(0), 1.0)])["d_l_pct"] is None,
      "可用点不足 2 个 → 变化率 None（不得拿单点算成 0）")

# liq_extremes：滚动 1h 绝对值，不跨桶差分
_liqx = sf.liq_extremes(_liq([200.0, 500.0, 100.0]), 1_000_000.0)
check(abs(_liqx["liq_peak_ratio"] - 0.0005) < 1e-12
      and abs(_liqx["liq_now_ratio"] - 0.0001) < 1e-12
      and abs(_liqx["liq_decay"] - 0.2) < 1e-12,
      "峰值/当前/衰减比 = 5e-4 / 1e-4 / 0.2", str(_liqx))
check(sf.liq_extremes(_liq([0.0, 0.0]), 1_000_000.0)["liq_decay"] is None,
      "峰值 0 → 衰减比 None（不除零、不冒充萎缩）")
check(sf.liq_extremes([], 1_000_000.0)["liq_peak_ratio"] is None
      and sf.liq_extremes(_liq([1.0]), None)["liq_peak_ratio"] is None,
      "无爆仓行 / 无 24h 成交额 → 全部 None")

# divergence_inputs：两侧 5m 相位差 1 桶时按 at-or-before 连接（不错位）
_px, _cum = sf.divergence_inputs(_klines([1.0, 1.1], _START + dt.timedelta(minutes=5)),
                                 _oi([100.0, 100.0], [7.0, 7.0]))
check(len(_px) == 2 and len(_cum) == 2 and _cum == [7.0, 14.0],
      "K 线相位落后 1 桶时仍按 at-or-before 配对（严格等长、不错位）", str(_cum))
_px2, _cum2 = sf.divergence_inputs(_klines([1.0, 1.1]), [])
check(_px2 == [] and _cum2 == [], "无 OI 桶 → 两序列同为空（不得只留价格）")

# ════════════════════════════════════════════════════════════
# 4. classify_fuel（§10.10.3）
# ════════════════════════════════════════════════════════════
print("\n【测试4】classify_fuel 五 verdict / 优先级 / 缺失降级")
_OK = dict(liq_peak_ratio=0.001, liq_decay=0.2, cvd_divergence=True)

CASES = [
    ("短重建正例（OI升 + 空仓代理升）",
     dict(d_oi_pct=1.0, d_s_pct=5.0, d_l_pct=-2.0, **_OK), sf.SHORT_REBUILD, "medium"),
    ("多头拉盘正例（OI升 + 多仓代理升、空仓不动）",
     dict(d_oi_pct=1.0, d_s_pct=-1.0, d_l_pct=5.0, **_OK), sf.LONG_PUMP, "medium"),
    ("平仓驱动 + 峰值越阈 + 衰减 + 背离 → 弹药耗尽",
     dict(d_oi_pct=-1.0, d_s_pct=-5.0, d_l_pct=0.0, **_OK), sf.FUEL_EXHAUSTING, "medium"),
    ("平仓驱动但爆仓从未越阈 → 进行中（不得判耗尽）",
     dict(d_oi_pct=-1.0, d_s_pct=-5.0, d_l_pct=0.0,
          liq_peak_ratio=1e-5, liq_decay=0.1, cvd_divergence=True),
     sf.FUEL_ACTIVE, "medium"),
    ("平仓驱动但价格与 CVD 未背离 → 进行中",
     dict(d_oi_pct=-1.0, d_s_pct=-5.0, d_l_pct=0.0,
          liq_peak_ratio=0.001, liq_decay=0.2, cvd_divergence=False),
     sf.FUEL_ACTIVE, "medium"),
    ("平仓驱动但爆仓仍高位（衰减不足）→ 进行中",
     dict(d_oi_pct=-1.0, d_s_pct=-5.0, d_l_pct=0.0,
          liq_peak_ratio=0.001, liq_decay=0.9, cvd_divergence=True),
     sf.FUEL_ACTIVE, "medium"),
    ("CVD 背离不可判定 → 进行中（只挡耗尽）",
     dict(d_oi_pct=-1.0, d_s_pct=-5.0, d_l_pct=0.0,
          liq_peak_ratio=0.001, liq_decay=0.2, cvd_divergence=None),
     sf.FUEL_ACTIVE, "medium"),
    ("平仓驱动但爆仓缺失 → mixed（不得判「弹药尚存」）",
     dict(d_oi_pct=-1.0, d_s_pct=-5.0, d_l_pct=0.0,
          liq_peak_ratio=None, liq_decay=None, cvd_divergence=True),
     sf.MIXED, "low"),
    ("多仓代理量缺失 → mixed（结构量缺失不出方向）",
     dict(d_oi_pct=-1.0, d_s_pct=-5.0, d_l_pct=None, **_OK), sf.MIXED, "low"),
    ("OI 升但两侧代理量都未越阈 → mixed",
     dict(d_oi_pct=1.0, d_s_pct=1.0, d_l_pct=1.0, **_OK), sf.MIXED, "low"),
    ("OI 变化在死区（未达 ±0.3%）→ mixed",
     dict(d_oi_pct=0.1, d_s_pct=-5.0, d_l_pct=0.0, **_OK), sf.MIXED, "low"),
]
for title, kw, want, want_conf in CASES:
    v = sf.classify_fuel(**kw)
    check(v["verdict"] == want, f"{title} → {want}", f"实际 {v['verdict']}")
    check(v["confidence"] == want_conf, f"{title} 置信度 {want_conf}",
          f"实际 {v['confidence']}")

# 优先级：「多头拉盘」与「空头重建」同时成立时必须由书写顺序决定，且**不得**落进 sqz_fuel_*
v = sf.classify_fuel(d_oi_pct=1.0, d_s_pct=5.0, d_l_pct=5.0, **_OK)
check(v["verdict"] == sf.SHORT_REBUILD,
      "OI 升时 dS/dL 同时越阈 → 由优先级定序（short_rebuild 先判），不落 sqz_fuel_*",
      v["verdict"])
for kw in (dict(d_oi_pct=1.0, d_s_pct=5.0, d_l_pct=5.0, **_OK),
           dict(d_oi_pct=1.0, d_s_pct=-1.0, d_l_pct=5.0, **_OK)):
    check(sf.classify_fuel(**kw)["verdict"] not in (sf.FUEL_EXHAUSTING, sf.FUEL_ACTIVE),
          "假信号「多头主动开仓拉盘」结构上不可能落进 sqz_fuel_*（§10.10.3 核心约束）",
          sf.classify_fuel(**kw)["verdict"])

# 边界：恰好等于分界
check(sf.classify_fuel(d_oi_pct=0.3, d_s_pct=3.0, d_l_pct=0.0, **_OK)["verdict"]
      == sf.SHORT_REBUILD, "dOI/dS 恰好等于分界 → 越阈（闭区间）")
check(sf.classify_fuel(d_oi_pct=-0.3, d_s_pct=-3.0, d_l_pct=-1.0, **_OK)["verdict"]
      == sf.FUEL_EXHAUSTING, "dOI/dS/dL 恰好等于分界 → 平仓驱动成立（闭区间）")

# 「衰减必须先有峰值」：峰值未越阈 + 衰减比很小 + 有背离 —— 最易写成「弹药耗尽」的形态
v = sf.classify_fuel(d_oi_pct=-1.0, d_s_pct=-5.0, d_l_pct=0.0,
                     liq_peak_ratio=sqz.SQZ_SHORT_LIQ_RATIO_MIN * 0.5,
                     liq_decay=0.01, cvd_divergence=True)
check(v["verdict"] == sf.FUEL_ACTIVE and "从未越过阈值" in v["reason"],
      "§10.10.5：「衰减必须先有峰值」——峰值未越阈时 liq_decay 无意义，不得判耗尽",
      f"{v['verdict']} / {v['reason']}")

# 缺失维度必须显式列出
v = sf.classify_fuel(d_oi_pct=-1.0, d_s_pct=-5.0, d_l_pct=0.0,
                     liq_peak_ratio=None, liq_decay=None, cvd_divergence=None)
check(v["data_missing"] == ["liq"], "缺失维度写入 data_missing", str(v["data_missing"]))
v = sf.classify_fuel(d_oi_pct=None, d_s_pct=None, d_l_pct=None,
                     liq_peak_ratio=None, liq_decay=None, cvd_divergence=None)
check(v["data_missing"] == ["d_oi_pct", "d_s_pct", "d_l_pct", "liq"]
      and "数据不足" not in v["reason"] and "缺失" in v["reason"],
      "三维度全缺失 → data_missing 全列 + 文案标注缺失", str(v["data_missing"]))

# ════════════════════════════════════════════════════════════
# 5. fuel_gate（§10.10.5）
# ════════════════════════════════════════════════════════════
print("\n【测试5】fuel_gate 五道闸门")
_FULL_TS = [_ts(i) for i in range(13)]
_LSR_OK = _lsr([1.0] * 13)


def _gate(oi_rows, lsr_points=None, start=_START, now=_NOW):
    return sf.fuel_gate(oi_rows=oi_rows, lsr_points=lsr_points if lsr_points is not None
                        else _LSR_OK, surge_start_ts=start, now=now)


_ok = _gate(_oi([100.0] * 13, [1.0] * 13))
check(_ok["gate_ok"] is True and _ok["gate_reason"] is None,
      "完整窗口 → 通过", str(_ok))
check(_ok["oi_cover"] == {"have": 13, "expect": 13} and _ok["oi_lag_sec"] == 0,
      "覆盖 13/13、尾部滞后 0s", str(_ok["oi_cover"]) + str(_ok["oi_lag_sec"]))

_g = _gate(_oi([100.0] * 2, [1.0] * 2), start=_NOW - dt.timedelta(hours=6))
check(_g["gate_ok"] is False and "覆盖不足" in _g["gate_reason"],
      "① 覆盖率 < MIN_WINDOW_COVERAGE → 拒判", _g["gate_reason"])

_g = _gate(_oi([100.0] * 10, [1.0] * 10))   # 末桶在 now-15min ⇒ 滞后 900s > 2×300s
check(_g["gate_ok"] is False and "尾部" in _g["gate_reason"]
      and "滞后" in _g["gate_reason"],
      "② 尾部 oi_lag_sec > 2×桶 → 拒判", _g["gate_reason"])

_rows = _oi([100.0] * 13, [1.0] * 13)
_g = _gate([r for i, r in enumerate(_rows) if i not in (4, 5)])
check(_g["gate_ok"] is False and "连续缺桶" in _g["gate_reason"]
      and _g["head_gap_buckets"] == 0 and _g["mid_gap_buckets"] == 2,
      "③ 中段连续缺 2 桶（window_gate）→ 拒判", _g["gate_reason"])

_g = _gate(_oi([100.0] * 13, [1.0] * 13), lsr_points=_lsr([1.0] * 5))
check(_g["gate_ok"] is False and "多空比有效点仅 5" in _g["gate_reason"],
      "④ LSR 点数 < MIN_LSR_POINTS（冷启动）→ 拒判", _g["gate_reason"])

_g = _gate(_oi([100.0, 100.0], [1.0, 1.0], start=_NOW - dt.timedelta(minutes=5)),
           start=_NOW - dt.timedelta(minutes=5))
check(_g["gate_ok"] is False and "有效 5m 桶仅 2" in _g["gate_reason"],
      "⑤ 有效 5m 桶 < MIN_FUEL_BUCKETS → 拒判", _g["gate_reason"])

check(sf.fuel_gate(oi_rows=[], lsr_points=_LSR_OK, surge_start_ts=_START,
                   now=_NOW)["gate_ok"] is False,
      "完全无 OI 桶 → 拒判（不崩）")

# ════════════════════════════════════════════════════════════
# 6. evaluate_fuel 端到端
# ════════════════════════════════════════════════════════════
print("\n【测试6】evaluate_fuel 端到端")
_OI_DOWN = [100.0 - i * (10.0 / 12) for i in range(13)]        # 100 → 90（dOI = -10%）
_CVD_DIV = [10.0] * 10 + [-5.0, 2.0, 2.0]                      # 末段主动卖 ⇒ 累计 CVD 未新高
_PX_UP = [1.0 + i * 0.01 for i in range(13)]                   # 末点即窗口最高价
_R_UP = [1.0 + i * 0.025 for i in range(13)]                   # 1.0 → 1.3
_LIQ_DECAY = [200.0, 500.0, 400.0, 300.0, 200.0] + [100.0] * 8  # 峰值 500 → 当前 100
_VOL24 = 1_000_000.0


def _fuel(liq_vals, oi_vals=None, r_vals=None):
    return sf.evaluate_fuel(
        oi_rows=_oi(oi_vals if oi_vals is not None else _OI_DOWN, _CVD_DIV),
        lsr_points=_lsr(r_vals if r_vals is not None else _R_UP),
        liq_rows=_liq(liq_vals), k_rows=_klines(_PX_UP), vol24_usd=_VOL24,
        surge_start_ts=_START, now=_NOW)


_f = _fuel(_LIQ_DECAY)
_m = _f["metrics"]
check(_f["gate_ok"] is True and _f["verdict"] == sf.FUEL_EXHAUSTING,
      "端到端：平仓驱动 + 爆仓越阈衰减 + CVD 背离 → 轧空弹药耗尽",
      f"{_f['verdict']} / {_f['reason']}")
check(abs(_m["d_oi_pct"] - (-10.0)) < 1e-6, "dOI = -10%（窗口首尾桶）", str(_m["d_oi_pct"]))
check(_m["d_s_pct"] < -3.0 and _m["d_l_pct"] >= -1.0,
      "dS 显著为负、dL 基本持平（占比上升来自空头平仓而非多头加仓）",
      f"dS={_m['d_s_pct']} dL={_m['d_l_pct']}")
check(_m["r_first"] == 1.0 and _m["r_last"] == 1.3 and abs(_m["d_r"] - 0.3) < 1e-9,
      "r_first → r_last 与 d_r（仅作展示）", f"{_m['r_first']}→{_m['r_last']}")
check(abs(_m["liq_decay"] - 0.2) < 1e-9 and abs(_m["liq_peak_ratio"] - 0.0005) < 1e-9,
      "liq_peak_ratio 0.0005、liq_decay 0.2", f"{_m['liq_peak_ratio']}/{_m['liq_decay']}")
check(_m["cvd_divergence"] is True, "cvd_divergence = True")
check(abs(_m["btc_share"] - round(11 / 12, 4)) < 1e-9,
      "btc_share = 11/12（11 个买平桶 / 12 有效桶）", str(_m["btc_share"]))
check(_m["proxy_scope"] == "oi_x_share / oi_x_cvd_quadrant"
      and _m["fuel_metric_ver"] == sf.FUEL_METRIC_VER,
      "metrics 带口径标注与版本位（跨版本回看先看版本位）", str(_m["proxy_scope"]))

_f2 = _fuel([5.0] * 13)     # 爆仓峰值 5/1e6 = 5e-6 < 8e-5，从未成规模
check(_f2["verdict"] == sf.FUEL_ACTIVE and "从未越过阈值" in _f2["reason"],
      "端到端：爆仓从未越阈 → 进行中（不得判弹药耗尽）", _f2["reason"])

_f3 = sf.evaluate_fuel(oi_rows=_oi([100.0, 100.0], [1.0, 1.0]), lsr_points=_LSR_OK,
                       liq_rows=_liq([1.0, 1.0]), k_rows=_klines([1.0, 1.0]),
                       vol24_usd=_VOL24, surge_start_ts=_NOW - dt.timedelta(hours=6),
                       now=_NOW)
check(_f3["verdict"] is None and _f3["gate_ok"] is False
      and _f3["label"] == "暂不评估（闸门未过）" and "覆盖不足" in _f3["reason"],
      "闸门未过 → verdict=None（拒判，与 mixed「结构不明」是两回事）",
      f"{_f3['verdict']} / {_f3['label']}")
check(_f3["metrics"]["gate_ok"] is False and _f3["metrics"]["buckets"] == 2,
      "拒判时仍落覆盖/缺口观测位（可数出拒判分布）", str(_f3["metrics"]["oi_cover"]))

# ════════════════════════════════════════════════════════════
# 7. 源码级守卫：daemon 接线（§10.10.7）
# ════════════════════════════════════════════════════════════
print("\n【测试7】scan_daemon 接线守卫")


def _code_only(src: str) -> str:
    """只保留**非注释** token 后重建源码（词法剥离，字符串内的 `#` 不误伤）。"""
    spans: dict[int, list[tuple[int, int]]] = {}
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            spans.setdefault(tok.start[0], []).append((tok.start[1], tok.end[1]))
    if not spans:
        return src
    out = []
    for i, ln in enumerate(src.splitlines(keepends=True), start=1):
        for a, b in sorted(spans.get(i, []), reverse=True):
            ln = ln[:a] + ln[b:]
        out.append(ln)
    return "".join(out)


_daemon_path = os.path.join(_SCRIPTS, "bin", "scan_daemon.py")
with open(_daemon_path, encoding="utf-8") as _fh:
    _daemon_src = _fh.read()
check("metrics=COALESCE(metrics || COALESCE(%s::jsonb, '{}'::jsonb), metrics)" in _daemon_src,
      "squeeze_track.metrics 仍是**合并**语义（燃料写入不得覆盖入场指标）")

sys.path.insert(0, os.path.join(_SCRIPTS, "bin"))
try:
    import scan_daemon as sd  # noqa: E402
    check(inspect.signature(sd._fetch_long_short_ratio).parameters["limit"].default == 100,
          "§10.10.7-①：_fetch_long_short_ratio 默认 limit=100（≈8h，够 W + 基准桶）")
    check(sd.BUCKET_SECONDS == sf.BUCKET_SECONDS,
          "daemon 与 squeeze_fuel 的 BUCKET_SECONDS 同源（否则闸门口径静默漂移）",
          f"{sd.BUCKET_SECONDS} vs {sf.BUCKET_SECONDS}")
    _fn = next((n for n in ast.parse(_code_only(_daemon_src)).body
                if isinstance(n, ast.FunctionDef) and n.name == "task_scan_squeeze"), None)
    _src = ast.unparse(_fn).replace('"', "'") if _fn else ""
    check("sqz_fuel.evaluate_fuel(" in _src, "task_scan_squeeze 阶段 2 调用 evaluate_fuel",
          _src[:80])
    check(_src.count("fuel_json") == 3,
          "fuel_json 落 3 处：定义 1 + expired/tracking 两条 metrics 槽各 1",
          str(_src.count("fuel_json")))
    check(_src.count("'fuel': fuel") == 3,
          "'fuel' 落 3 处：fuel_json 构造 1 + 判定闸门拒判路径 1 + judged 路径的 metrics 1",
          str(_src.count("'fuel': fuel")))
    check(_src.count("notifier.send") == 1,
          "影子期不新增发送路径（task_scan_squeeze 内仍只有 §10.6 判定那一处 notifier.send）",
          str(_src.count("notifier.send")))
    check(sd.SQUEEZE_ALERT_SHADOW is True,
          "SQUEEZE_ALERT_SHADOW 仍开启（燃料评估先影子观察，不开信）")
except Exception as e:  # noqa: BLE001
    check(False, "daemon 接线守卫可执行", f"{type(e).__name__}: {e}")

# ════════════════════════════════════════════════════════════
print(f"\n{passed}/{passed + failed} passed")
sys.exit(1 if failed else 0)
