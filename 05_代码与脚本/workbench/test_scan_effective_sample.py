#!/usr/bin/env python3
"""`scan_effective_sample.py` 横截面去相关单测（离线护栏，不连库）。

运行：python test_scan_effective_sample.py

为什么要有它（2026-09-24 第 2 天 soak 验收）：
  `2026-09-23 14:04~14:50` 主池落了 192 条信号（189 down / 187 S4）= **一次大盘同步
  下跌**，不是 192 个独立观测。按条数算胜率/赔率会把 1 个宏观事件当成上百个样本，
  统计显著性被系统性高估。本工具把读数换算成**有效样本数 n_eff**。

覆盖：
  1) `cluster_means` 聚类语义（同键聚 1 簇、簇内等权、该窗口 NULL 成员跳过）；
  2) `stat` 口径（胜率 = `>0` 占比、**平盘计负**、赔率/PF 一侧为空记 inf、n=0 → None）；
  3) 🔴 核心护栏：**单一大簇**不得被当成 N 个样本（raw n=N 但 L1=L2=1 → 样本不可用）；
  4) L1（时×向）/ L2（日×向）键的独立性与「降粒度」方向（L1 ≥ L2）；
  5) `--json` 结构回归护栏（`windows` 四窗口齐、含 raw/l1_event/l2_day/n_clusters）
     —— 曾因 stats 与 dict 写入被误包在 `if not args.json` 内导致 `windows` 恒空；
  6) 退出码契约：`n_eff(L2) >= --min-eff` → 0，否则 3；空样本 → 3。
"""
import contextlib
import io
import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, os.path.join(_SCRIPTS, "src"))
sys.path.insert(0, os.path.join(_SCRIPTS, "bin"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import scan_effective_sample as ses  # noqa: E402

NOW = datetime(2026, 9, 24, 2, 0, tzinfo=timezone.utc)
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


def row(sid, t, p_dir, r1=1.0, r4=1.0, r12=1.0, r24=1.0, pool="main"):
    """构造一条 `biz.scan_signal_outcome` 行（只填本工具消费的列）。"""
    return {
        "signal_id": sid, "symbol": f"S{sid}", "pool": pool, "scenario": "S4",
        "timeframe": "1h", "p_dir": p_dir, "base_time": t,
        "aligned_ret_1h": r1, "aligned_ret_4h": r4,
        "aligned_ret_12h": r12, "aligned_ret_24h": r24,
    }


T = datetime(2026, 9, 23, 6, 0, tzinfo=timezone.utc)


# ────────────────────────────── 1) cluster_means ──────────────────────────────
print("\n[1] cluster_means：聚类语义")
rows1 = [
    row(1, T, "up", r24=2.0),
    row(2, T + timedelta(minutes=10), "up", r24=4.0),   # 同小时同向 → 与 1 同簇
    row(3, T, "down", r24=-3.0),                        # 同小时异向 → 另起簇
    row(4, T + timedelta(hours=2), "up", r24=6.0),      # 另小时 → 另起簇
]
means, groups = ses.cluster_means(rows1, 24, lambda r: (r["base_time"].replace(
    minute=0, second=0, microsecond=0), r["p_dir"]))
check(len(means) == 3, "同小时同向合并 / 异向异时分开 → 3 个簇", f"实际 {len(means)}")
check(sorted(round(m, 4) for m in means) == [-3.0, 3.0, 6.0],
      "簇内等权平均（2.0 与 4.0 → 3.0），非简单求和",
      f"实际 {sorted(round(m, 4) for m in means)}")
check(len(groups[(T, "up")]) == 2, "同簇成员数正确（2 条）")

rows_null = [
    row(1, T, "up", r24=None),
    row(2, T + timedelta(minutes=5), "up", r24=2.0),
    row(3, T + timedelta(minutes=6), "up", r24=None),
]
means_n, _ = ses.cluster_means(rows_null, 24, lambda r: (r["base_time"].replace(
    minute=0, second=0, microsecond=0), r["p_dir"]))
check(means_n == [2.0], "该窗口 NULL 的成员被跳过（不污染簇均值、不产生空簇）",
      f"实际 {means_n}")
check(ses.cluster_means([], 24, lambda r: r["signal_id"])[0] == [],
      "空输入 → 空列表（不抛异常）")


# ────────────────────────────── 2) stat 口径 ──────────────────────────────
print("\n[2] stat：胜率 / 赔率 / PF 口径")
s = ses.stat([1.0, 3.0])
check(s["n"] == 2 and s["win"] == 1.0 and s["avg"] == 2.0, "全胜：win=1.0 / avg=2.0")
check(s["odds"] is None and s["pf"] is None,
      "全胜：无亏损侧 ⇒ 赔率/PF 记 None（不记 ∞，与 agg 口径一致）", f"{s}")

s = ses.stat([2.0, -2.0])
check(abs(s["win"] - 0.5) < 1e-9 and abs(s["odds"] - 1.0) < 1e-9 and abs(s["pf"] - 1.0) < 1e-9,
      "一胜一负：win=50% / 赔率=1.0 / PF=1.0", f"{s}")

s = ses.stat([0.0, 2.0])
check(abs(s["win"] - 0.5) < 1e-9, "平盘计负（0.0 不算胜）", f"{s}")

s = ses.stat([-1.0, -2.0])
check(s["win"] == 0.0 and s["odds"] is None and s["pf"] == 0.0,
      "全负：win=0、赔率 None（无盈利侧）、PF=0.0（有亏损侧 ⇒ 定义良好）", f"{s}")

check(ses.stat([]) is None, "n=0 → None（调用方据此打「无样本」）")
check(all(v != float("inf") and v != float("-inf") for v in ses.stat([1.0, 3.0]).values()),
      "stat 输出不含 inf（JSON 合法性前提）")


# ────────────────── 3) 核心护栏：单一大簇 ≠ N 个独立样本 ──────────────────
print("\n[3] 🔴 核心护栏：单一大簇（192 条 = 1 个宏观事件）")
burst = [row(i, T + timedelta(minutes=(i % 45)), "down", r24=-3.96) for i in range(192)]
m_l1, _ = ses.cluster_means(burst, 24, lambda r: (
    r["base_time"].replace(minute=0, second=0, microsecond=0), r["p_dir"]))
m_l2, _ = ses.cluster_means(burst, 24, lambda r: (r["base_time"].date(), r["p_dir"]))
check(len(burst) == 192 and len(m_l1) == 1, "raw 192 条 → L1 事件级仅 1 个 bet",
      f"L1={len(m_l1)}")
check(len(m_l2) == 1, "L2 日级仅 1 个 bet（同日同向）", f"L2={len(m_l2)}")
s_raw, s_l1, s_l2 = ses.stat([-3.96] * 192), ses.stat(m_l1), ses.stat(m_l2)
check(s_raw["n"] == 192 and s_l1["n"] == 1 and s_l2["n"] == 1,
      "点估计 n 缩水 192→1（显著性不得按条数计）")
check(abs(s_raw["avg"] - s_l1["avg"]) < 1e-9,
      "去相关不改变点估计（等权重加权 ⇒ 均值不动）", f"{s_raw['avg']} vs {s_l1['avg']}")


# ────────────────────── 4) L1 / L2 键的独立性与粒度 ──────────────────────
print("\n[4] L1（时×向）/ L2（日×向）粒度关系")
rows4 = [row(i, T + timedelta(hours=i), "up", r24=1.0) for i in range(5)]  # 同日 5 个不同小时
n1, _ = ses.cluster_means(rows4, 24, lambda r: (
    r["base_time"].replace(minute=0, second=0, microsecond=0), r["p_dir"]))
n2, _ = ses.cluster_means(rows4, 24, lambda r: (r["base_time"].date(), r["p_dir"]))
check(len(n1) == 5 and len(n2) == 1, "同日 5 个不同小时同向 → L1=5、L2=1",
      f"L1={len(n1)} L2={len(n2)}")

rows4b = [row(1, T, "up", r24=1.0), row(2, T + timedelta(hours=1), "down", r24=-1.0)]
n2b, _ = ses.cluster_means(rows4b, 24, lambda r: (r["base_time"].date(), r["p_dir"]))
check(len(n2b) == 2, "同日两个方向 → L2=2（方向是 key 的一部分）", f"L2={len(n2b)}")
check(len(n2) <= len(n1) + 0, "粒度单调：L2 ≤ L1（日粒度粗于小时粒度）")


# ────────────────────── 5) main() --json 结构回归护栏 ──────────────────────
print("\n[5] main()：--json 结构 + 退出码契约")


class _Cur:
    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return {"now": NOW}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def cursor(self, **k):
        return _Cur()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def run_main(argv, rows):
    """打桩 DB（只回 NOW）+ 打桩取数，跑真实 main()，捕获 stdout 与退出码。"""
    ses.get_settings = lambda: types.SimpleNamespace(database_url="stub://")
    ses.get_connection = lambda url: _Conn()
    ses.load_rows = lambda conn, days, pool: sorted(rows, key=lambda r: r["base_time"])
    old_argv = sys.argv
    sys.argv = ["scan_effective_sample.py"] + argv
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = ses.main()
    finally:
        sys.argv = old_argv
    return rc, buf.getvalue()


rc, out = run_main(["--json", "--days", "7"], burst)
d = json.loads(out)
check(set(d["windows"].keys()) == {"1h", "4h", "12h", "24h"},
      "--json：windows 四个窗口键齐全（曾因误缩进恒为空 dict）",
      f"实际 {sorted(d['windows'].keys())}")
missing = [k for k, v in d["windows"].items()
           if not {"raw", "l1_event", "l2_day", "n_clusters"} <= set(v)]
check(not missing, "每个窗口含 raw / l1_event / l2_day / n_clusters", f"缺 {missing}")
check(d["windows"]["24h"]["n_clusters"] == {"l1": 1, "l2": 1},
      "24h 窗口簇计数与护栏 3 一致")
check(d["n_raw"] == 192 and d["n_eff_l2"] == 1 and d["sample_ok"] is False,
      "n_raw=192 / n_eff_l2=1 / sample_ok=False")
check(rc == 3, "样本不足（L2=1 < min_eff=10）→ 退出码 3", f"实际 {rc}")
check(d["min_eff"] == 10 and d["pool"] == "all" and len(d["base_time_range"]) == 2,
      "min_eff 默认 10 / pool 默认 all / base_time_range 两元素")

# JSON 合法性：全胜样本曾产出 `Infinity`（非法 JSON）
_win_rows = [row(i, T + timedelta(hours=i), "up", r24=1.0) for i in range(12)]
rc_w, out_w = run_main(["--json", "--days", "30"], _win_rows)
check("Infinity" not in out_w and "NaN" not in out_w,
      "全胜样本的 JSON 不含 Infinity/NaN（合法性护栏）",
      f"含非法字面量：{[t for t in ('Infinity', 'NaN') if t in out_w]}")
check(json.dumps(json.loads(out_w), allow_nan=False) is not None,
      "输出可被严格 JSON 解析器往返（allow_nan=False 不报错）")

# 12 个独立日 → L2 n_eff=12 ≥ 10 ⇒ 可用、rc=0
spread = [row(i, T + timedelta(days=i), "up", r24=0.5) for i in range(12)]
rc2, out2 = run_main(["--json", "--days", "30"], spread)
d2 = json.loads(out2)
check(d2["n_eff_l2"] == 12 and d2["sample_ok"] is True and rc2 == 0,
      "12 个独立日 → n_eff_l2=12 ≥ 10 ⇒ sample_ok=True、退出码 0",
      f"n_eff={d2['n_eff_l2']} rc={rc2}")

# 空样本
rc3, out3 = run_main(["--json"], [])
d3 = json.loads(out3)
check(d3["n_raw"] == 0 and d3["n_eff_l2"] == 0 and d3["sample_ok"] is False and rc3 == 3,
      "空样本 → n_raw=0 / sample_ok=False / rc=3（不抛异常）", f"rc={rc3}")
check("windows" in d3, "空样本时 windows 键仍在（下游可安全取键）")

# 人类可读输出不崩（同样两次跑通）
rc4, out4 = run_main(["--days", "7"], burst)
check(rc4 == 3 and "L2 日级(日×向)" in out4 and "集中度" in out4,
      "非 --json 模式：输出含读数表与集中度行，不崩")
check("原始(按条数)" in out4, "非 --json 模式：三个口径行齐全")


print(f"\n{'=' * 56}\n通过 {passed} / 失败 {failed}\n{'=' * 56}")
sys.exit(1 if failed else 0)