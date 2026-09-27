#!/usr/bin/env python3
"""审计 NEW-C · 资金费率年化按品种实际结算间隔（盘面告警邮件审计 2026-09-27 §三 NEW-C）。

背景：年化原写死 ×3×365（假设 8h 结算），实测 Binance 存在 4h 结算品种
      （LSK/ZRO/PAXG/ASTER/GRAM/LA/MOODENG）⇒ 年化被**低估一半**（应 ×6×365），
      该封 9 币中 7 个受影响。

运行：python workbench/test_funding_interval_20260927.py（纯离线，不连库、不连网）

覆盖：
  1  结算间隔推导（生产者 `_derive_funding_interval_h`）：8h/4h/1h、空、噪声、倒序
  2  跨交易所间隔选取（`_aggregate_funding`）：OI 主导者优先、退化、全无
  3  年化倍率（消费侧 `_funding_annualize_mult`）：8h→×3×365、4h→×6×365、缺失回退 8h
  4  卡片渲染：4h 品种年化约为 8h 的两倍（回归审计现场，修复前两者同值）
  5  源码护栏：不再硬编码 ×3×365、图例不再写死 8h、间隔已接入渲染项与落库
  6  建表/迁移：funding_interval_h 列（基线 SQL + fix_073 迁移 + 两处 ensure_table）
  7  边界留档：7d/30d 均值窗口（rates[:21]）仍按 8h 假设 —— 同根因但非本次范围
"""
import os
import re
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))          # 05_代码与脚本/workbench
_ROOT = os.path.dirname(_HERE)                              # 05_代码与脚本
_SCRIPTS = os.path.join(_ROOT, "scripts")
sys.path.insert(0, os.path.join(_SCRIPTS, "src"))
sys.path.insert(0, os.path.join(_SCRIPTS, "bin"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import phase_derivatives_batch as pdb  # noqa: E402
import scan_daemon as sd  # noqa: E402

passed = 0
failed = 0


def check(cond, name, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}" + (f"  → {detail}" if detail else ""))


def _read(path):
    try:
        return open(path, encoding="utf-8").read()
    except OSError as e:
        return f"__READ_ERROR__ {e}"


_SD_SRC = _read(os.path.join(_SCRIPTS, "bin", "scan_daemon.py"))
_PDB_SRC = _read(os.path.join(_SCRIPTS, "bin", "phase_derivatives_batch.py"))
_DBSTATS_SRC = _read(os.path.join(_HERE, "db_stats.py"))
_SQL_TABLE = _read(os.path.join(_SCRIPTS, "sql", "biz", "derivatives_table.sql"))
_SQL_MIG = _read(os.path.join(_SCRIPTS, "migrations", "fix_073_funding_interval.sql"))

_H8 = 8 * 3600 * 1000
_H4 = 4 * 3600 * 1000
_H1 = 3600 * 1000


def _hist(step_ms, n):
    """构造 n 条等间隔结算历史（funding_time 为毫秒时间戳）。"""
    return [types.SimpleNamespace(funding_time=1_700_000_000_000 + i * step_ms)
            for i in range(n)]


print("── 1. 结算间隔推导（生产者）──")
check(pdb._derive_funding_interval_h(_hist(_H8, 10)) == 8.0, "8h 历史 → 8.0")
check(pdb._derive_funding_interval_h(_hist(_H4, 10)) == 4.0, "4h 历史 → 4.0")
check(pdb._derive_funding_interval_h(_hist(_H1, 24)) == 1.0, "1h 历史 → 1.0")
check(pdb._derive_funding_interval_h([]) is None, "空历史 → None")
check(pdb._derive_funding_interval_h(None) is None, "None → None")
check(pdb._derive_funding_interval_h(_hist(_H8, 1)) is None, "单条记录（无相邻间隔）→ None")
_noisy = _hist(_H8, 9)
_noisy[4] = types.SimpleNamespace(funding_time=_noisy[4].funding_time + _H8)  # 制造一次 16h
check(pdb._derive_funding_interval_h(_noisy) == 8.0,
      "含一次缺失（16h 间隔）时中位数仍 8.0", pdb._derive_funding_interval_h(_noisy))
check(pdb._derive_funding_interval_h(list(reversed(_hist(_H4, 6)))) == 4.0,
      "历史倒序输入 → 仍 4.0（部分交易所返回降序）")

print("\n── 2. 跨交易所间隔选取（聚合）──")
_f1 = pdb._aggregate_funding(
    ["binance", "okx"],
    {"binance": {"funding_rate": 0.0001, "open_interest_value": 1e9, "funding_interval_h": 4.0},
     "okx": {"funding_rate": 0.0002, "open_interest_value": 1e8, "funding_interval_h": 8.0}})
check(_f1["interval_h"] == 4.0, "取 OI 价值最大（binance）的间隔 4h", _f1["interval_h"])
_f2 = pdb._aggregate_funding(
    ["binance", "okx"],
    {"binance": {"funding_rate": 0.0001, "funding_interval_h": 8.0},
     "okx": {"funding_rate": 0.0002, "funding_interval_h": 4.0}})
check(_f2["interval_h"] == 8.0, "全无 OI → 取迭代首个可得者（binance 8h）", _f2["interval_h"])
_f3 = pdb._aggregate_funding(["binance"], {"binance": {"funding_rate": 0.0001}})
check(_f3["interval_h"] is None, "全部无间隔 → None（消费侧回退 8h）", _f3["interval_h"])

print("\n── 3. 年化倍率（消费侧）──")
check(abs(sd._funding_annualize_mult(8) - 3 * 365) < 1e-9, "8h → ×3×365")
check(abs(sd._funding_annualize_mult(4) - 6 * 365) < 1e-9, "4h → ×6×365（审计要求）")
check(abs(sd._funding_annualize_mult(1) - 24 * 365) < 1e-9, "1h → ×24×365")
check(abs(sd._funding_annualize_mult(None) - 3 * 365) < 1e-9, "None → 回退 8h（不劣化）")
check(abs(sd._funding_annualize_mult(0) - 3 * 365) < 1e-9, "0 → 回退 8h")
check(abs(sd._funding_annualize_mult(-2) - 3 * 365) < 1e-9, "负值 → 回退 8h")
check(abs(sd._funding_annualize_mult("x") - 3 * 365) < 1e-9, "非法字符串 → 回退 8h")
check(abs(sd._funding_annualize_mult(4) - 2 * sd._funding_annualize_mult(8)) < 1e-9,
      "4h 年化恰为 8h 的两倍（对应审计「低估一半」）")

print("\n── 4. 卡片渲染：4h 品种年化翻倍 ──")
_SIG = {"id": 1, "symbol": "LSKUSDT", "pool": "main", "scenario": "S1",
        "timeframe": "15m", "p_dir": "up", "price_chg_pct": 1.0, "vol_ratio": 2.0,
        "oi_dir": "up", "oi_chg_pct": 1.0, "cvd_dir": "up", "cvd_usd": 1000.0,
        "cvd_ratio": 0.1, "funding_rate": 0.00005, "confidence": "high", "status": "active",
        "context_tags": ["btc_1h=up(+0.06%)", "fgi=72(Greed)"],
        "trigger_price": 1.0, "stop_loss_pct": 8.0, "breakout_px": 1.0}
_RES = {"event": [], "catalyst": [], "kol": [], "kol_total": 0,
        "catalyst_dir": {"bullish": 0, "bearish": 0, "neutral": 0},
        "catalyst_dir_fresh": {}, "catalyst_raw": 0, "catalyst_latest": None,
        "catalyst_stale": 0, "catalyst_all": [], "asset_linked": True}


def _ann_pct(interval):
    it = {"signal": dict(_SIG), "resonance": _RES, "funding_interval_h": interval}
    html = sd._render_alert_email([it], [])
    m = re.search(r"年化\s*([+-][\d.]+)%", html)
    return float(m.group(1)) if m else None


_a8, _a4, _aN = _ann_pct(8.0), _ann_pct(4.0), _ann_pct(None)
_exp8 = 0.005 * sd._funding_annualize_mult(8)
check(_a8 is not None and _a4 is not None, "卡片渲染出年化数值", f"8h={_a8} 4h={_a4}")
check(_a8 is not None and abs(_a8 - _exp8) < 0.06, f"8h：+0.0050% → 年化 ≈ +{_exp8:.2f}%", _a8)
check(_a4 is not None and _a8 is not None and abs(_a4 - 2 * _a8) < 0.2,
      "4h 年化 ≈ 8h 的两倍（修复前两者同值）", f"8h={_a8} 4h={_a4}")
check(_aN == _a8, "间隔缺失 → 与 8h 同值（回退不劣化）", f"None={_aN} 8h={_a8}")

print("\n── 5. 源码护栏：不再写死 8h ──")
check("* 3 * 365" not in _SD_SRC, "scan_daemon 已无硬编码 `* 3 * 365`")
check("×3×365（8h 结算）" not in _SD_SRC, "图例不再写死「×3×365（8h 结算）」")
check("_load_funding_interval_map" in _SD_SRC, "scan_daemon 新增 _load_funding_interval_map")
check("_funding_annualize_mult" in _SD_SRC, "scan_daemon 新增 _funding_annualize_mult")
check('it["funding_interval_h"]' in _SD_SRC, "task_scan_alert 把结算间隔带入渲染项")
check("_derive_funding_interval_h" in _PDB_SRC and "funding_interval_h" in _PDB_SRC,
      "phase_derivatives_batch 推导并落库 funding_interval_h")
check("_derive_funding_interval_h" in _DBSTATS_SRC and "funding_interval_h" in _DBSTATS_SRC,
      "db_stats 并行采集路径同口径落库 funding_interval_h")

print("\n── 6. 建表 / 迁移 ──")
check("funding_interval_h" in _SQL_TABLE, "derivatives_table.sql 基线含 funding_interval_h")
check("ADD COLUMN IF NOT EXISTS funding_interval_h" in _SQL_MIG,
      "fix_073 幂等 `ADD COLUMN IF NOT EXISTS funding_interval_h`")
check("funding_interval_h NUMERIC(4,1)" in _PDB_SRC,
      "phase_derivatives_batch ensure_table 建表含该列")
check("funding_interval_h NUMERIC(4,1)" in _DBSTATS_SRC, "db_stats 建表兜底含该列")

print("\n── 7. 边界留档（非本次范围）──")
check("rates[:21]" in _PDB_SRC and "rates[:21]" in _DBSTATS_SRC,
      "7d 均值窗口仍按 8h 假设（rates[:21]）—— 与年化同根因但本轮未改，留档")

print(f"\n{'=' * 60}\nPASS {passed} / FAIL {failed}")
raise SystemExit(1 if failed else 0)