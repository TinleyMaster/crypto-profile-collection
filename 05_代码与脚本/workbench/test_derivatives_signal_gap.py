#!/usr/bin/env python3
"""衍生生品费率覆盖缺口 O1 修复探针（待修复工单_O1_费率覆盖缺口_2026-09-22.md）。

运行：python workbench/test_derivatives_signal_gap.py
      （源码/归一化断言离线恒可跑；能连生产库时自动追加**只读**缺口枚举，连不上只跳过）

覆盖：
  1. `_signal_symbol_candidates` 合约符号 → 裸符号归一（B2USDT/1000FLOKIUSDT/…）；
  2. 源码 AST：`--signal-days` 参数存在、`main` 合并信号缺口且缺口不被 --limit 截断；
  3. `get_signal_gap_assets` 查询语义（作用域 main/BRK + NOT EXISTS 无衍生品行）；
  4. 只读 prod：缺口集合确实「无 asset_derivatives 行」；`--signal-days 0` 恒空。
"""
import ast
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, os.path.join(_SCRIPTS, "src"))
sys.path.insert(0, os.path.join(_SCRIPTS, "bin"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import phase_derivatives_batch as pdb  # noqa: E402

passed = 0
failed = 0
skipped = 0


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


def skip(name, reason):
    global skipped
    skipped += 1
    print(f"  \u2013 跳过 {name}：{reason}")


SRC = open(pdb.__file__, encoding="utf-8").read()
TREE = ast.parse(SRC)


def _func(name):
    return next((n for n in TREE.body
                 if isinstance(n, ast.FunctionDef) and n.name == name), None)


# ═══════════════════════════════════════════════════════════════
#  1. 符号归一化
# ═══════════════════════════════════════════════════════════════

print("\n【1】合约符号 → 裸符号候选（与 scan_daemon 同口径）")
cases = {
    "B2USDT": ["B2USDT", "B2"],
    "EPICUSDT": ["EPICUSDT", "EPIC"],
    "1000FLOKIUSDT": ["1000FLOKIUSDT", "1000FLOKI", "FLOKI"],
    "1000000MOGUSDT": ["1000000MOGUSDT", "1000000MOG", "MOG"],
    "BROCCOLI714USDT": ["BROCCOLI714USDT", "BROCCOLI714"],
    "BTCUSDT": ["BTCUSDT", "BTC"],
}
for sym, want in cases.items():
    got = pdb._signal_symbol_candidates(sym)
    check(got == want, f"{sym} → {want}", f"got={got}")

# ═══════════════════════════════════════════════════════════════
#  2. 源码 AST：参数 + 合并拓扑
# ═══════════════════════════════════════════════════════════════

print("\n【2】源码形状（AST）")
main_fn = _func("main")
main_src = ast.unparse(main_fn) if main_fn else ""
check(main_fn is not None, "找到 phase_derivatives_batch.main()")
check("--signal-days" in main_src, "main 定义/使用 --signal-days 参数")
check("get_signal_gap_assets" in main_src, "main 调用 get_signal_gap_assets（O1 缺口对齐）")
check("gap_assets" in main_src and "merge_pending" in main_src,
      "main 用 merge_pending 合并市值池与缺口池")
merge_fn = _func("merge_pending")
merge_src = ast.unparse(merge_fn) if merge_fn else ""
check("max(limit, len(gap_assets))" in merge_src,
      "merge_pending 内 cap = max(limit, 缺口数)（缺口不被 --limit 截断）")

gap_fn = _func("get_signal_gap_assets")
gap_src = ast.unparse(gap_fn) if gap_fn else ""
check(gap_fn is not None, "找到 get_signal_gap_assets()")
check("pool = 'main' OR scenario = 'BRK'" in gap_src,
      "作用域限定主池/BRK（funding 的消费方；squeeze 池不用 funding）")
check("asset_derivatives" in gap_src and "funding_rate IS NOT NULL" in gap_src,
      "按**符号级**覆盖判定：任一 asset_derivatives 行有费率即视为已覆盖")
check("DISTINCT ON" in gap_src,
      "按 canonical_symbol 去重（规避 core.asset 符号重复导致的缺口虚高）")
check("scan_signal" in gap_src, "数据源为 biz.scan_signal")

# ═══════════════════════════════════════════════════════════════
#  3. --signal-days 0 关闭（离线，无需连库）
# ═══════════════════════════════════════════════════════════════

print("\n【3】--signal-days 0 关闭 O1 对齐")
check(pdb.get_signal_gap_assets(None, 0) == [],
      "days=0 → 直接返回 []（不触库、不改变原行为）")

# ═══════════════════════════════════════════════════════════════
#  3b. merge_pending：缺口置顶 + 不被 --limit 截断
# ═══════════════════════════════════════════════════════════════


def _a(i, rank=None):
    return {"asset_id": i, "symbol": f"S{i}", "name": None, "market_cap_rank": rank}


print("\n【3b】merge_pending 拓扑")
# 模拟生产：scheduler 以 --limit 200 调用；近 7d 缺口 50
gap = [_a(i) for i in range(50)]
ranked = [_a(1000 + i, rank=i + 1) for i in range(200)]
merged = pdb.merge_pending(ranked, gap, 200)
check(len(merged) == 200, "limit=200 → 合并结果长度 200（cap=max(200,50)）", f"got={len(merged)}")
check(all(a["asset_id"] in {g['asset_id'] for g in gap} for a in merged[:50]),
      "前 50 位全部为缺口资产（置顶）")
check(len(merged) == len({a['asset_id'] for a in merged}),
      "结果无重复 asset_id")

m2 = pdb.merge_pending(ranked[:100], [], 100)
check(len(m2) == 100 and m2[0]["asset_id"] == 1000,
      "无缺口时行为不变（原 top-N 前 100）")

m3 = pdb.merge_pending(ranked, gap[:5], 3)
check(len(m3) == 5 and all(a["asset_id"] < 1000 for a in m3),
      "缺口数 > limit 时仍保留全部缺口（cap 兜底，不截断缺口）", f"got={len(m3)}")

# 缺口与 ranked 重叠 → 去重且缺口优先
overlap = [_a(1000, rank=1), _a(7)]
m4 = pdb.merge_pending(ranked, overlap, 10)
check(sum(1 for a in m4 if a["asset_id"] == 1000) == 1,
      "缺口与 ranked 重叠时按 asset_id 去重（只出现一次）")
check(m4[0]["asset_id"] in (1000, 7), "重叠项仍置顶")

m5 = pdb.merge_pending(ranked, gap, 0)
check(len(m5) == 250, "limit=0（全量）→ 不截断（缺口 50 + ranked 200）", f"got={len(m5)}")

# 源码断言：merge_pending 被 main 使用
check("merge_pending" in main_src, "main 调用 merge_pending（拓扑单点）")

# ═══════════════════════════════════════════════════════════════
#  3c. _aggregate_funding：OI 缺失时退化为等权平均（O1 连带修复）
# ═══════════════════════════════════════════════════════════════

print("\n【3c】_aggregate_funding（费率聚合不再被 OI 缺失吞掉）")


def _approx(a, b, eps=1e-12):
    return a is not None and b is not None and abs(a - b) < eps


f1 = pdb._aggregate_funding(["a", "b"], {
    "a": {"funding_rate": 0.0001, "open_interest_value": 100.0},
    "b": {"funding_rate": 0.0002, "open_interest_value": 300.0},
})
check(_approx(f1["avg_funding"], (0.0001 * 100 + 0.0002 * 300) / 400),
      "有 OI 价值 → 按 OI 加权（原口径不变）", f"got={f1['avg_funding']}")

f2 = pdb._aggregate_funding(["a", "b", "c"], {
    "a": {"funding_rate": 0.0001, "open_interest_value": None},
    "b": {"funding_rate": 0.0003, "open_interest_value": 0},
    "c": {"funding_rate": None, "open_interest_value": 500.0},
})
check(_approx(f2["avg_funding"], 0.0002),
      "无 OI 价值 → 等权简单平均（0.0001+0.0003)/2），不再丢费率",
      f"got={f2['avg_funding']}")
check(f2["total_oi_value"] is None, "无 OI 价值 → total_oi_value=None")

f3 = pdb._aggregate_funding(["a"], {"a": {"funding_rate": None}})
check(f3["avg_funding"] is None, "既无 OI 也无费率 → None（不兜底 0）")

f4 = pdb._aggregate_funding(["a", "b"], {
    "a": {"funding_rate": 0.0001, "open_interest_value": None,
          "funding_rate_7d_avg": 0.0002},
    "b": {"funding_rate": 0.0001, "open_interest_value": None,
          "funding_rate_7d_avg": 0.0004},
})
check(_approx(f4["avg_funding_7d"], 0.0003), "7d 平均同样支持无 OI 退化",
      f"got={f4['avg_funding_7d']}")

ts = 100, 200
f5 = pdb._aggregate_funding(["a", "b"], {
    "a": {"funding_rate": 0.0001, "next_funding_time": 200},
    "b": {"funding_rate": 0.0001, "next_funding_time": 100},
})
check(f5["next_funding_ts"] == 100, "next_funding_ts 取最早结算时间")

_fetch_src = ast.unparse(_func("fetch_one_asset"))
check("_aggregate_funding" in _fetch_src, "fetch_one_asset 使用 _aggregate_funding")


print("\n【4】只读 prod：缺口集合不变量")
try:
    import psycopg.rows  # noqa: E402
    from crypto_research.config import get_settings  # noqa: E402
    from crypto_research.db.conn import get_connection  # noqa: E402

    with get_connection(get_settings().database_url) as conn:
        gaps = pdb.get_signal_gap_assets(conn, 7)
        ids = [g["asset_id"] for g in gaps]
        if ids:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM biz.asset_derivatives "
                    "WHERE asset_id = ANY(%s) AND funding_rate IS NOT NULL",
                    (ids,),
                )
                leaked = cur.fetchone()[0]
        else:
            leaked = 0
except Exception as exc:  # noqa: BLE001
    skip("只读 prod 缺口不变量", f"无法连库：{type(exc).__name__}: {exc}")
    gaps = None

if gaps is not None:
    print(f"    （近 7 天主池/BRK 信号缺口：{len(gaps)} 个，样例："
          f"{[g['symbol'] for g in gaps[:6]]}）")
    check(leaked == 0,
          "缺口资产无任何**带费率**的衍生品行（符号级覆盖自洽）",
          f"{leaked} 个缺口资产竟已有带费率的行")
    syms_up = [str(g["symbol"]).upper() for g in gaps]
    check(len(syms_up) == len(set(syms_up)),
          "缺口按符号去重（每个 canonical_symbol 只出现一次）",
          f"重复：{[s for s in set(syms_up) if syms_up.count(s) > 1][:8]}")
    ranks = [g["market_cap_rank"] for g in gaps if g["market_cap_rank"] is not None]
    hi = sum(1 for r in ranks if r > 100)
    print(f"    \u2139 rank 分布：{len(ranks)} 个有排名，其中 rank>100 的 {hi} 个"
          f"（其余 {len(gaps) - len(ranks)} 个无排名）")

print(f"\n结果：{passed} 通过 / {failed} 失败 / {skipped} 跳过")
sys.exit(1 if failed else 0)
