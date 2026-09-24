#!/usr/bin/env python3
"""爆仓**口径隔离**单测（Coinglass 套餐数据接入方案 §8.2 的 4 条不变量）。

运行：python test_liq_history_scope.py

钉住的 4 条不变量（方案 §3.3 / §4.3 / §4.4 / §8.2）：
  ① **口径 B 不进实时判定链**：`biz.liquidation_history`（4h **分段增量**）不得出现在
     `scan_daemon.py` / `squeeze.py` / `squeeze_fuel.py` 的任何 SQL 或代码里（N2 粒度不匹配）；
  ② **interval / exchange_scope 不同不得参与同一次聚合**：存货表 PK 必须含这两列；跨所配对
     SQL 必须逐列相等且两端口径固定（`all` vs `binance`）；`assert_single_scope()` 遇混入口径
     必须**抛错**而不是静默合并；
  ③ **缺失 ≠ 0**：任一端缺失一律输出 `None`（`liq_background` / `_liq_col` / `_num`），
     且闸门未过时 24h 背景**照样落库**（缺口存在性标注）；
  ④ **口径 A / B 的比值不得进同一分布或同一分位**：两条链路的 SQL 不得互相引用（无任何一个
     SQL 常量同时出现两张表），且标定脚本里 `assert_single_scope()` 必须先于 `distribution()`。

⚠️ 本文件只钉**结构 / 口径**，不对任何阈值取值下结论（方案 §1.3 N1）。
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
sys.path.insert(0, os.path.join(_SCRIPTS, "bin"))
sys.path.insert(0, _HERE)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

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


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


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


DAEMON = os.path.join(_SCRIPTS, "bin", "scan_daemon.py")
SQUEEZE = os.path.join(_SCRIPTS, "src", "crypto_research", "analysis", "squeeze.py")
FUEL = os.path.join(_SCRIPTS, "src", "crypto_research", "analysis", "squeeze_fuel.py")
CALIB = os.path.join(_HERE, "calib_squeeze_liq_thr.py")
BACKFILL = os.path.join(_SCRIPTS, "bin", "phase_backfill_liq_history.py")
MIGRATION = os.path.join(_SCRIPTS, "migrations", "fix_069_liquidation_history.sql")

_DAEMON_SRC = _read(DAEMON)
_CALIB_SRC = _read(CALIB)


# ════════════════════════════════════════════════════════════
# 1. 不变量①：口径 B（分段增量）不进实时判定链
# ════════════════════════════════════════════════════════════
print("\n【测试1】不变量①：biz.liquidation_history 不得进实时判定链（N2）")
for name, path in (("scan_daemon.py", DAEMON), ("squeeze.py", SQUEEZE),
                   ("squeeze_fuel.py", FUEL)):
    src = _code_only(_read(path))
    check("liquidation_history" not in src,
          f"{name} 不引用 biz.liquidation_history（4h 分段增量维持「只进标定/回测」）")

# 正向对照：上面三条是**否定**断言，若路径写错会永远通过 ⇒ 必须证明检出能力存在。
for name, path in (("phase_backfill_liq_history.py", BACKFILL), ("calib_squeeze_liq_thr.py", CALIB)):
    check("liquidation_history" in _code_only(_read(path)),
          f"正向对照：{name} 确实引用 liquidation_history（证明上面的否定断言非空转）")


# ════════════════════════════════════════════════════════════
# 2. 不变量②：interval / exchange_scope 不得混桶混算
# ════════════════════════════════════════════════════════════
print("\n【测试2】不变量②：不同 interval / exchange_scope 不得参与同一次聚合")
_sql_ddl = _read(MIGRATION)
check("PRIMARY KEY (symbol, interval, exchange_scope, ts)" in _sql_ddl,
      "存货表 PK 含 (symbol, interval, exchange_scope, ts)（结构上杜绝混桶/混算）")
check("PRIMARY KEY (symbol, interval, exchange_scope)" in _sql_ddl,
      "游标表 PK 含 (symbol, interval, exchange_scope)（断点不跨口径串用）")

import calib_squeeze_liq_thr as calib  # noqa: E402  （模块级只定义常量，不连库）

_cross = calib.SQL_CROSS_EXCHANGE
check("b.interval = a.interval AND b.ts = a.ts" in _cross,
      "跨所配对 SQL：两侧 interval 与 ts 必须**逐列相等**（同粒度同区间才可比）")
check("a.exchange_scope = 'all'" in _cross and "b.exchange_scope = 'binance'" in _cross,
      "跨所配对 SQL：两端口径固定为 all / binance（不随参数漂移）")
_b4h = calib.SQL_B_4H
check("l.exchange_scope = %(scope)s" in _b4h and "l.interval = %(interval)s" in _b4h,
      "口径 B SQL：interval / exchange_scope 均由参数显式过滤（不得缺省）")
check("l.interval              AS interval" in _b4h
      and "l.exchange_scope        AS exchange_scope" in _b4h,
      "口径 B SQL 取回 interval / exchange_scope（否则 assert_single_scope 形同虚设）")

_ok_scope = calib.assert_single_scope(
    [{"interval": "4h", "exchange_scope": "binance"}], "单口径")
check(_ok_scope == ("4h", "binance"), "单一口径样本：返回该口径", str(_ok_scope))
check(calib.assert_single_scope([], "空样本") == (None, None),
      "空样本：返回 (None, None)（不抛错，由上层 b_gate 处置）")
_raised = False
try:
    calib.assert_single_scope(
        [{"interval": "4h", "exchange_scope": "binance"},
         {"interval": "1d", "exchange_scope": "binance"}], "混桶")
except ValueError:
    _raised = True
check(_raised, "混入多个 interval ⇒ **抛错**（fail-loud，不静默合并成一锅）")
_raised2 = False
try:
    calib.assert_single_scope(
        [{"interval": "4h", "exchange_scope": "binance"},
         {"interval": "4h", "exchange_scope": "all"}], "混所")
except ValueError:
    _raised2 = True
check(_raised2, "混入多个 exchange_scope ⇒ 抛错（Binance 与全所不可同分布）")


# ════════════════════════════════════════════════════════════
# 3. 不变量③：缺失 ≠ 0
# ════════════════════════════════════════════════════════════
print("\n【测试3】不变量③：任一端缺失输出 None，不得补 0")
_bg_empty = sf.liq_background([])
check(all(_bg_empty[k] is None for k in
          ("liq_bg_24h_usd", "liq_bg_24h_long_usd", "liq_bg_ts")),
      "无爆仓行 ⇒ 背景值全 None（不是 0）", str(_bg_empty))
check(_bg_empty["liq_bg_scope"] == sf.LIQ_BG_SCOPE,
      "无爆仓行时口径标签仍显式给出（展示层不得省略口径）")
check(sf.LIQ_BG_SCOPE == "coinglass_rolling_24h" and sf.FUEL_METRIC_VER == 2,
      "口径标签 = coinglass_rolling_24h 且 fuel_metric_ver 已递增到 2（结构变更须镖版本位）")

_now = dt.datetime(2026, 9, 24, 12, 0, tzinfo=dt.timezone.utc)
_bg_newest = sf.liq_background([
    {"ts": _now - dt.timedelta(minutes=10),
     "long_liq_usd_24h": 111.0, "short_liq_usd_24h": 222.0},
    {"ts": _now - dt.timedelta(minutes=5),
     "long_liq_usd_24h": 333.0, "short_liq_usd_24h": 444.0},
])
check((_bg_newest["liq_bg_24h_usd"], _bg_newest["liq_bg_24h_long_usd"]) == (444.0, 333.0),
      "取**最新一条**（背景值与燃料窗口长短无关）", str(_bg_newest))
check(_bg_newest["liq_bg_ts"] == (_now - dt.timedelta(minutes=5)).isoformat(),
      "背景值带自己的时间戳（可判断该背景有多新）")
_bg_missing = sf.liq_background([{"ts": _now, "long_liq_usd_1h": 9.0}])
check(_bg_missing["liq_bg_24h_usd"] is None and _bg_missing["liq_bg_24h_long_usd"] is None,
      "行存在但无 24h 列（旧行 / 列残缺）⇒ None，不得回落成 0")

# 闸门未过时 24h 背景照样落库（§4.1 消费点②「缺口存在性标注」）
_gate_fail = sf.evaluate_fuel(
    oi_rows=[], lsr_points=[],
    liq_rows=[{"ts": _now, "long_liq_usd_1h": 100.0, "short_liq_usd_1h": 200.0,
               "long_liq_usd_24h": 5000.0, "short_liq_usd_24h": 9000.0}],
    k_rows=[], vol24_usd=1.0e6,
    surge_start_ts=_now - dt.timedelta(hours=2), now=_now)
check(_gate_fail["gate_ok"] is False, "构造：闸门确实未过（oi_rows 为空）")
check(_gate_fail["metrics"]["liq_bg_24h_usd"] == 9000.0
      and _gate_fail["metrics"]["liq_bg_24h_long_usd"] == 5000.0,
      "闸门未过时仍落 24h 背景（1h 快照断了也能区分「无爆仓」与「快照断了」）",
      str(_gate_fail["metrics"].get("liq_bg_24h_usd")))
check(_gate_fail["metrics"]["liq_bg_scope"] == sf.LIQ_BG_SCOPE,
      "fuel.metrics 里带 `liq_bg_scope`（**不**占用判定侧的 `liq_scope`，避免一词多义）")
check("liq_bg_24h_usd" not in (sf.classify_fuel.__doc__ or "")
      and not inspect.signature(sf.classify_fuel).parameters.get("liq_bg_24h_usd"),
      "24h 背景**不进** classify_fuel（只展示、不参与判定）")

import phase_backfill_liq_history as bk  # noqa: E402
import scan_daemon as sd  # noqa: E402
check(bk._num(None) is None and bk._num("") is None and bk._num("abc") is None,
      "回填侧 _num：接口缺键 / 空串 / 非数值 ⇒ None（不写 0 冒充「无爆仓」）")
check(bk._num("1469440.5") == 1469440.5, "回填侧 _num：字符串数值正常转 float")
check(sd._liq_col(None, "liq_usd_24h") is None
      and sd._liq_col({"liq_usd_24h": None}, "liq_usd_24h") is None,
      "展示侧 _liq_col：行缺失 / 列 NULL ⇒ None")
check(sd._liq_col({"liq_usd_24h": 1.5}, "liq_usd_24h") == 1.5, "展示侧 _liq_col：正常取值")


# ════════════════════════════════════════════════════════════
# 4. 不变量④：A / B 比值不得进同一分布或同一分位
# ════════════════════════════════════════════════════════════
print("\n【测试4】不变量④：口径 A / B 的比值不得进同一分布/同一分位")
_calib_code = _code_only(_CALIB_SRC)
check("assert_single_scope(b_rows" in _calib_code,
      "标定脚本对口径 B 样本调用 assert_single_scope（不变量 4 的入口门）")
check(_calib_code.index("assert_single_scope(b_rows")
      < _calib_code.index("distribution(b_ratios)"),
      "顺序守卫：assert_single_scope 必须先于 distribution 执行（先验口径再算分位）")
check('"a_mixed"' in _calib_code and '"b_single_exchange"' in _calib_code,
      "`scope_split` 分表输出：口径 A / B 各自独立成块（无合并分位字段）")

# 静态结构性守卫：任何一个 SQL 常量都不得同时引用两张表（一参一表 = 不可混算）
_sql_consts = [n.value.value for n in ast.parse(_read(CALIB)).body
               if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
               and isinstance(n.value.value, str) and "SELECT" in n.value.value]
check(len(_sql_consts) >= 4, "标定脚本的 SQL 常量可枚举（守卫非空转）", str(len(_sql_consts)))
_both = [s[:60] for s in _sql_consts
         if "liquidation_history" in s and "liquidation_snapshot" in s]
check(not _both, "无任何 SQL 同时读 snapshot（口径 A）与 history（口径 B）", str(_both))
check(any("liquidation_snapshot" in s for s in _sql_consts)
      and any("liquidation_history" in s for s in _sql_consts),
      "正向对照：两表的 SQL 各自存在（证明上面的互斥断言非空转）")
_dist_calls = _calib_code.count("distribution(") - _calib_code.count("def distribution(")
check(_dist_calls == 2,
      "distribution() 恰好 2 次**调用**（A 一次 / B 一次；新增调用必须先证明口径单一）",
      str(_dist_calls))

# 展示层：邮件不得把两个窗口的爆仓额写成比值或合并成一个数
check("24h 累计" in _DAEMON_SRC and "_fmt_usd_abs" in _DAEMON_SRC,
      "邮件爆仓行展示 24h **规模背景**（绝对值，非比值）")
check("不可换算、不可相除" in _DAEMON_SRC,
      "邮件脚注披露「① 1h 比率 / ② 24h 累计」两者不可换算（三处披露之一）")
check("严禁跨桶差分" in _DAEMON_SRC,
      "邮件脚注保留「滚动窗口严禁跨桶差分」披露")

# ════════════════════════════════════════════════════════════
print(f"\n{passed}/{passed + failed} passed")
sys.exit(1 if failed else 0)