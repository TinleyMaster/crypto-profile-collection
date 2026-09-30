#!/usr/bin/env python3
"""盘口深度采集单测（工单 SCAN-LIQ-DEPTH-001 阶段 D，通道③ 注入测试）。

运行：python test_scan_depth_capture.py

覆盖（纯离线，不连库、不打 API）：
  A) `_aggregate_depth` 纯函数：正常聚合 / ±1% 边界含入 / 交叉盘口 / 空侧 /
     非法数据 → None；spread_bp 精确值；零数量档位贡献 0。
  B) 源码护栏：开关存在；采集在 commit 之后；异常绝不外抛（采集 + 落库双 except）；
     INSERT 占位符与列数一致；符号去重（set）；depth limit 走常量（weight 可控）；
     落库失败 rollback；DDL 关键列齐。
"""
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, os.path.join(_SCRIPTS, "src"))
sys.path.insert(0, os.path.join(_SCRIPTS, "bin"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import scan_daemon as sd  # noqa: E402

_SD_SRC = open(os.path.join(_SCRIPTS, "bin", "scan_daemon.py"), encoding="utf-8").read()

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


def main() -> int:
    print("A) _aggregate_depth 纯函数")
    # 正常：mid=100，spread=2 → 200bp；d_ask=101（101 ≤ 101 含边界）；d_bid=198
    agg = sd._aggregate_depth({
        "bids": [["99", "2"], ["98.5", "1"]],
        "asks": [["101", "1"], ["101.5", "2"]],
    })
    check(agg is not None and abs(agg["mid"] - 100.0) < 1e-9, "mid = (99+101)/2 = 100")
    check(agg and abs(agg["spread_bp"] - 200.0) < 1e-6, "spread_bp = 200（2/100×1e4）")
    check(agg and abs(agg["d_ask"] - 101.0) < 1e-6, "d_ask = 101（101≤101 边界含入）")
    check(agg and abs(agg["d_bid"] - 198.0) < 1e-6, "d_bid = 198（99≥99 边界含入）")
    check(agg and agg["levels"] == 4, "levels = 4")
    # 覆盖半径：最远档 101.5/98.5 距 mid=100 各 1.5%
    check(agg and abs(agg["cov_ask_pct"] - 1.5) < 1e-9
          and abs(agg["cov_bid_pct"] - 1.5) < 1e-9,
          "cov_ask/bid_pct = 1.5（最远档距离，防把没采到当深度薄）")

    # 边界严格性：mid=100，±1% = 101/99；101.01 与 98.99 必须被排除
    agg2 = sd._aggregate_depth({
        "bids": [["99", "1"], ["98.99", "5"], ["98", "5"]],
        "asks": [["101", "1"], ["101.01", "5"], ["102", "5"]],
    })
    check(agg2 and abs(agg2["d_ask"] - 101.0) < 1e-6,
          "边界外（101.01>101）排除，d_ask 仍 = 101")
    check(agg2 and abs(agg2["d_bid"] - 99.0) < 1e-6,
          "边界外（98.99<99）排除，d_bid 仍 = 99")

    # 零数量档位：贡献 0 但不崩（98<99 在 ±1% 边界外 ⇒ d_bid=0，属正确行为）
    agg3 = sd._aggregate_depth({
        "bids": [["99", "0"], ["98", "1"]],
        "asks": [["101", "1"], ["101", "0"]],
    })
    check(agg3 is not None and agg3["d_ask"] == 101.0 and agg3["d_bid"] == 0.0,
          "零数量档位贡献 0，不破坏聚合")

    # 非法输入 → None（宁缺毋假）
    check(sd._aggregate_depth({"bids": [], "asks": [["101", "1"]]}) is None,
          "空 bids → None")
    check(sd._aggregate_depth({"bids": [["99", "1"]], "asks": []}) is None,
          "空 asks → None")
    check(sd._aggregate_depth({"bids": [["100", "1"]], "asks": [["100", "1"]]}) is None,
          "交叉盘口（ask==bid）→ None")
    check(sd._aggregate_depth({"bids": [["0", "1"]], "asks": [["101", "1"]]}) is None,
          "best_bid=0 → None")
    check(sd._aggregate_depth({"bids": [["abc", "1"]], "asks": [["101", "1"]]}) is None,
          "非数字档位 → None（不抛异常）")
    check(sd._aggregate_depth({}) is None, "缺 bids/asks 键 → None")

    print("B) 源码护栏")
    check("DEPTH_CAPTURE_ENABLED = True" in _SD_SRC, "开关存在且默认开（纯影子，留 kill switch）")
    check("depth_syms: set[str] = set()" in _SD_SRC and "depth_syms.add(sym)" in _SD_SRC,
          "采集对象按轮去重（set + add）")
    i_commit = _SD_SRC.rindex("conn.commit()", 0, _SD_SRC.index("depth_capture] 采集"))
    i_cap = _SD_SRC.index("_capture_depths(conn, sorted(depth_syms)")
    check(i_commit < i_cap, "采集调用在信号/过滤日志 commit 之后（失败零影响主链路）")
    cap_fn = _SD_SRC[_SD_SRC.index("def _capture_depths"):]
    cap_fn = cap_fn[:cap_fn.index("\ndef ")]
    check(cap_fn.count("except Exception") >= 2 and "noqa: BLE001" in cap_fn,
          "采集 + 落库双重 except，绝不外抛")
    check("conn.rollback()" in cap_fn, "落库失败 rollback（不留脏事务）")
    check("limit\": DEPTH_LIMIT" in cap_fn, "depth 档位数走常量（weight 从小从紧）")
    m = re.search(r"INSERT INTO biz\.scan_depth_log[\s\"]*\(([^)]+)\)[\s\"]*"
                  r"VALUES[\s\"]*\(([^)]+)\)", cap_fn, re.S)
    if m:
        n_col = len([c for c in m.group(1).split(",") if c.strip()])
        n_ph = m.group(2).count("%s")
        check(n_col == 12 and n_ph == 12, f"INSERT 列数=占位符=12（实测 {n_col}/{n_ph}）")
    else:
        check(False, "INSERT INTO biz.scan_depth_log 可匹配")
    check("status        text         NOT NULL" in _SD_SRC, "DDL 含 status NOT NULL（失败也要落行）")
    check("d_ask_1pct" in _SD_SRC and "d_bid_1pct" in _SD_SRC and "spread_bp" in _SD_SRC,
          "DDL 含深度双侧 + 价差列（B8 实时半边）")
    check("cov_ask_pct" in _SD_SRC and "DEPTH_LOG_ALTER_COV_DDL" in _SD_SRC,
          "覆盖半径列 + 幂等 ALTER（预建旧表补列，同 vol7d 之课）")
    check("CREATE TABLE IF NOT EXISTS biz.scan_depth_log" in _SD_SRC
          and "cur.execute(DEPTH_LOG_DDL)" in _SD_SRC,
          "建表挂在惰性 ensure 内（幂等，随首轮回来自愈）")
    # 聚合半径与 CG 快照对齐（range_pct=1 ⇒ ±1%），对照才有意义
    check("DEPTH_RANGE_PCT = 0.01" in _SD_SRC, "聚合半径 ±1%（与 CG range=1 对齐）")

    print(f"\n[scan_depth_capture] {passed}/{passed + failed} 通过")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
