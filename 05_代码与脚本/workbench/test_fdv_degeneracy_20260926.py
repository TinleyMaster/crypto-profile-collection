#!/usr/bin/env python3
"""一键投研页 P0 复验处置（2026-09-26）· 离线回归护栏。

来源：核验_一键投研页P0修复_2cc1e55_2026-09-26.md（遗留 #1 / #2 / #3）
运行：python workbench/test_fdv_degeneracy_20260926.py（纯离线，不连库、不连网）

覆盖：
  #1 FDV 退化读时兜底：快照表 fdv 退化为 ≈market_cap 时，用「被 CMC 历史印证过」的
       max_supply 重建；「流通==最大」真值不误改（复验明确警告项）
  #2 drift 无条件不变式：fdv≈market_cap 且印证 max_supply 明显大于流通量 → 无条件 stale
      （两个都错的值不得互相掩护）；无印证 max_supply 不得误报（代币化股票守卫）
  #3 ETL 流通量兜底：INSERT 侧 NULLIF/COALESCE 与存量 repair_zero_circulating 均存在
  前端       research.html 能渲染 fdv_degenerate 这一 reason kind
"""
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

spec = importlib.util.spec_from_file_location("db_stats", os.path.join(_HERE, "db_stats.py"))
db = importlib.util.module_from_spec(spec)
sys.modules["db_stats"] = db
spec.loader.exec_module(db)

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


_DB_SRC = open(os.path.join(_HERE, "db_stats.py"), encoding="utf-8").read()
_HTML_SRC = open(os.path.join(_HERE, "templates", "research.html"), encoding="utf-8").read()
_ETL_SRC = open(os.path.join(_HERE, "..", "scripts", "bin",
                             "etl_asset_market_daily_from_cmc.py"), encoding="utf-8").read()

_THESIS = {"key_metrics": {"价格": "0.630722163545 USD"}}
_TOK = {"max_supply": 1_000_000_000.0, "circulating_supply": 683_710_367.0}
_ONE_E9 = 1_000_000_000.0


def _drift(market, tokenomics=_TOK):
    sm = {"market": market, "tokenomics": tokenomics}
    return db._detect_thesis_drift(_THESIS, sm) or {}


def _kinds(d):
    return [r["kind"] for r in d.get("reasons", [])]


def main():
    print("── #2 drift 无条件不变式 ──")
    d = _drift({"price_usd": 0.63, "market_cap_usd": 430_000_000.0, "fdv_usd": 430_000_000.0,
                "fdv_corroborated_max_supply": _ONE_E9})
    check(d.get("stale") is True and "fdv_degenerate" in _kinds(d),
          "fdv==mcap 且 max_supply>流通 → 无条件 stale", f"got {d}")

    d = _drift({"price_usd": 0.63, "market_cap_usd": 430_000_000.0, "fdv_usd": 430_000_000.0,
                "fdv_corroborated_max_supply": _ONE_E9})
    check("fdv" in (d.get("checked") or []), "不变式把 fdv 计入 checked")

    # 复验警告项：08-17~08-21 ratio=1.000 是「流通==最大」的真值，不得回改/误报
    d = _drift({"price_usd": 0.05, "market_cap_usd": 50_000_000.0, "fdv_usd": 50_000_000.0,
                "fdv_corroborated_max_supply": _ONE_E9},
               tokenomics={"max_supply": _ONE_E9, "circulating_supply": _ONE_E9})
    check("fdv_degenerate" not in _kinds(d), "流通==最大 真值不误报", f"got {d}")

    # 代币化股票守卫：无「CMC 历史印证」的 max_supply 不得误报
    d = _drift({"price_usd": 0.05, "market_cap_usd": 50_000_000.0, "fdv_usd": 50_000_000.0},
               tokenomics={"max_supply": 900_000_000.0, "circulating_supply": 500_000_000.0})
    check("fdv_degenerate" not in _kinds(d), "无印证 max_supply 不误报", f"got {d}")

    # 已修正的 FDV 不得误报
    d = _drift({"price_usd": 0.63, "market_cap_usd": 430_000_000.0, "fdv_usd": 630_340_818.06,
                "fdv_corroborated_max_supply": _ONE_E9})
    check("fdv_degenerate" not in _kinds(d), "非退化 FDV 不误报", f"got {d}")

    print("── #1 读时兜底（源码级守卫）──")
    check("_corroborated_max_supply" in _DB_SRC, "存在印证式 max_supply 取值函数")
    check("cmc_hist_max >= t.max_supply * 0.98" in _DB_SRC, "守卫口径与写侧一致（0.98×）")
    check("fdv_corroborated_max_supply" in _DB_SRC, "兜底结果透出给 drift 使用")
    check("price_x_corroborated_max_supply" in _DB_SRC, "兜底修正留痕 fdv_basis")
    check("(_to_float(_arow[k]) or 0.0) > 0" in _DB_SRC, "权威供应量忽略非正值（伪 0 视为缺失）")

    print("── #3 ETL 流通量兜底 ──")
    check("def repair_zero_circulating" in _ETL_SRC, "存量修复函数存在")
    check("NULLIF(q.circulating_supply, 0)" in _ETL_SRC, "INSERT 侧对 circulating 0 判缺失")
    check("repair_zero_circulating(conn, days)" in _ETL_SRC, "主 ETL 流程自愈调用")

    print("── 前端渲染 ──")
    check("fdv_degenerate" in _HTML_SRC, "research.html 渲染 fdv_degenerate 分支")

    print(f"\n汇总: PASS={passed} FAIL={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())