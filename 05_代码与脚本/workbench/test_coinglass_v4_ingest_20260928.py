#!/usr/bin/env python3
"""CoinGlass V4 接入护栏单测（工单_CoinGlass_V4接入_按优先级_2026-09-28.md：CGV4-000/002/003）。

运行：python test_coinglass_v4_ingest_20260928.py

钉住的不变量：
  ① **仅新增不改既有链路**（工单红线⑤）：`ingest_coinglass_derivatives.py` 的**代码**
     不得引用 `biz.asset_derivatives`；既有 `phase_derivatives_batch.py` / `scan_daemon.py`
     的**代码**不得引用新表 `coinglass_derivatives_snapshot`（新表是并行源，尚未接消费端）。
  ② **额度纪律**（红线②）：TokenBucket 的令牌数学正确、`_throttle` 先补 gap 再扣令牌、
     `fetch_with_retry` 走指数退避且次数受 `MAX_RETRIES` 约束（不无限重试）。
  ③ **缺失 ≠ 0**：`_num` 对缺键/空串/非数值一律 None；OI/费率键名映射不得互相串位。
  ④ **口径隔离**（'All' 聚合行 ≠ 分所行）：新表 PK 含 exchange、迁移注释显式声明不可相加；
     `symbol` 落**币种基码**且 `base_code` 映射正确。
  ⑤ **调度**：三条日频作业在 SCHEDULE 中、错峰 01:10/02:10/03:10、且**不得**带 `--resume`
     （游标语义 = 已覆盖到 done_through，日频复用会永久跳过）。

⚠️ 本文件只钉结构/口径/节流，不连库、不打网络（时间与 session 全用替身）。
"""
from __future__ import annotations

import ast
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
_BIN = os.path.join(_SCRIPTS, "bin")
sys.path.insert(0, os.path.join(_SCRIPTS, "src"))
sys.path.insert(0, _BIN)
sys.path.insert(0, _HERE)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

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
    import io
    import tokenize
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


class _FakeClock:
    """确定性替身时钟：记录 sleep 调用，避免测试依赖真实耗时（防 flaky）。"""

    def __init__(self) -> None:
        self.t = 0.0
        self.log: list[tuple[str, float]] = []

    def monotonic(self) -> float:
        return self.t

    def time(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s
        self.log.append(("sleep", s))


INGEST = os.path.join(_BIN, "ingest_coinglass_derivatives.py")
BACKFILL = os.path.join(_BIN, "phase_backfill_liq_history.py")
DERIV_BATCH = os.path.join(_BIN, "phase_derivatives_batch.py")
DAEMON = os.path.join(_BIN, "scan_daemon.py")
SCHEDULER = os.path.join(_HERE, "scheduler.py")
MIGRATION = os.path.join(_SCRIPTS, "migrations", "fix_077_coinglass_derivatives_snapshot.sql")
CLIENT = os.path.join(_SCRIPTS, "src", "crypto_research", "clients", "coinglass_client.py")

import crypto_research.clients.coinglass_client as cg  # noqa: E402
import ingest_coinglass_derivatives as ing  # noqa: E402  （模块级只定义常量/函数，不连库）

_INGEST_SRC = _read(INGEST)
_MIG_SRC = _read(MIGRATION)


# ════════════════════════════════════════════════════════════
# 1. 不变量①：仅新增，不改既有链路
# ════════════════════════════════════════════════════════════
print("\n【测试1】不变量①：新表为并行源，既有链路零改动")
_ing_code = _code_only(_INGEST_SRC)


_bad_ref: list[str] = []
for _n in ast.parse(_INGEST_SRC).body:
    if (isinstance(_n, ast.Assign) and isinstance(_n.value, ast.Constant)
            and isinstance(_n.value.value, str) and "INSERT" in _n.value.value
            and "asset_derivatives" in _n.value.value):
        _bad_ref.append(_n.value.value[:60])
check(not _bad_ref,
      "ingest 无任何 SQL 常量写 biz.asset_derivatives（旧链路原样保留）", str(_bad_ref))
check(any(isinstance(_n, ast.Assign) and isinstance(_n.value, ast.Constant)
          and isinstance(_n.value.value, str) and "coinglass_derivatives_snapshot" in _n.value.value
          for _n in ast.parse(_INGEST_SRC).body),
      "正向对照：确有写新表的 SQL 常量（证明上面的否定断言非空转）")
check("asset_derivatives" in _INGEST_SRC,
      "正向对照：文中确有对旧链路的说明性提及（docstring/提示语，非 SQL）")
check("coinglass_derivatives_snapshot" in _ing_code,
      "正向对照：ingest 确实写入新表 coinglass_derivatives_snapshot（否定断言非空转）")
check("asset_derivatives" not in ing.UPSERT_SQL
      and "coinglass_derivatives_snapshot" in ing.UPSERT_SQL,
      "UPSERT 目标表 = 新表（旧表无任何写入路径）")
for name, path in (("phase_derivatives_batch.py", DERIV_BATCH), ("scan_daemon.py", DAEMON)):
    check("coinglass_derivatives_snapshot" not in _code_only(_read(path)),
          f"{name} 未引用新表（尚未接消费端 ⇒ 现役判定链不受影响）")
check("derivatives_client" in _read(DERIV_BATCH),
      "正向对照：phase_derivatives_batch.py 走的仍是自建多所直连（未被新源替换）")


# ════════════════════════════════════════════════════════════
# 2. 不变量②：限流与退避（额度纪律）
# ════════════════════════════════════════════════════════════
print("\n【测试2】不变量②：TokenBucket 数学 / _throttle 顺序 / 退避次数")
check(cg.DEFAULT_RATE_PER_MIN == 30.0, "默认配额 = HOBBYIST 30 req/min（客户端级常量）")

_fc = _FakeClock()
_saved_time = cg.time
cg.time = _fc
try:
    tb = cg.TokenBucket(rate_per_min=60, burst=1)   # 1 令牌/秒，容量 1
    w0 = tb.consume(1.0)
    w1 = tb.consume(1.0)
    check(w0 == 0.0, "首令牌（桶内有存量）不等待", str(w0))
    check(abs(w1 - 1.0) < 1e-6, "次令牌按速率等待 1.0s（60/min ⇒ 1/s）", str(w1))
    check(abs(_fc.log[-1][1] - 1.0) < 1e-6, "等待通过 sleep 实现（未被忙等吞掉）", str(_fc.log))

    # _throttle 顺序：先补 min_request_gap，再扣令牌
    _fc.t = 0.0
    _fc.log.clear()
    cli = cg.CoinGlassClient("k", base_url="https://x", min_request_gap=2.5)

    class _RecBucket:
        def consume(self, n=1.0):
            _fc.log.append(("bucket", n))
            return 0.0

    cli._bucket = _RecBucket()
    cli._throttle()
    check(_fc.log[0] == ("sleep", 2.5) and _fc.log[1] == ("bucket", 1.0),
          "先 gap-sleep(2.5s) 再扣令牌（顺序不可颠倒）", str(_fc.log))

    # fetch_with_retry：失败 → 退避 1/2/4 后放弃（共 MAX_RETRIES+1 次尝试）
    _fc.log.clear()
    ing.time = _fc
    n_attempt = {"n": 0}

    def _boom(*a, **k):
        n_attempt["n"] += 1
        raise cg.CoinGlassError("429 限频")

    res, err = ing.fetch_with_retry(_boom)
    check(n_attempt["n"] == ing.MAX_RETRIES + 1 == 4,
          "持续失败 ⇒ 尝试 4 次（MAX_RETRIES=3）后放弃，不无限重试", str(n_attempt["n"]))
    check(res is None and err and "429" in err, "放弃时返回 (None, 原因)（调用方据此跳过该币）",
          str(err))
    check([s for k, s in _fc.log if k == "sleep"] == [1, 2, 4],
          "退避序列 = 1→2→4s（末次不 sleep）", str(_fc.log))

    _fc.log.clear()
    n_attempt["n"] = 0
    ok_res, ok_err = ing.fetch_with_retry(lambda *a, **k: (n_attempt.__setitem__("n", n_attempt["n"] + 1) or [{"a": 1}]))
    check(ok_err is None and ok_res == [{"a": 1}], "成功即返回，不退避")
    check(n_attempt["n"] == 1, "成功时只尝试 1 次")
finally:
    cg.time = _saved_time
    ing.time = __import__("time")

check(ing.DEFAULT_MIN_GAP == 2.5 and cg.DEFAULT_RATE_PER_MIN == 30.0,
      "min_request_gap=2.5s（24 req/min）为 scan_daemon 的 coin-list 留额度余量")


# ════════════════════════════════════════════════════════════
# 3. 不变量③：缺失 ≠ 0 + 字段映射不串位
# ════════════════════════════════════════════════════════════
print("\n【测试3】不变量③：缺失 ⇒ None；OI / 费率键名映射不串位")
check(ing._num(None) is None and ing._num("") is None and ing._num("abc") is None,
      "_num：缺键 / 空串 / 非数值 ⇒ None（不写 0 冒充「无持仓/无费率」）")
check(ing._num("54425573401.8174") == 54425573401.8174 and ing._num(0) == 0.0,
      "_num：字符串数值正常转 float；真实的 0 保留为 0")

_oi = ing.oi_rows_for([
    {"exchange": "All", "open_interest_usd": 1.0e10, "open_interest_quantity": 100.0,
     "open_interest_by_coin_margin": 2.0e9, "open_interest_by_stable_coin_margin": 8.0e9,
     "open_interest_change_percent_24h": -0.67},
    {"exchange": "Binance", "open_interest_usd": 1.09e10},
])
check(set(_oi) == {"All", "Binance"}, "OI 行按交易所建键（含 'All' 聚合行）", str(set(_oi)))
check(_oi["All"]["oi_usd"] == 1.0e10 and _oi["All"]["oi_qty"] == 100.0
      and _oi["All"]["oi_coin_margin"] == 2.0e9 and _oi["All"]["oi_stable_margin"] == 8.0e9
      and _oi["All"]["oi_chg24h"] == -0.67,
      "OI 字段逐列映射正确（币本位/稳定币本位/24h 变化各归其位）", str(_oi["All"]))
check(_oi["Binance"]["oi_qty"] is None and _oi["Binance"]["oi_chg24h"] is None,
      "行内缺列 ⇒ None（不跨行串位、不补 0）", str(_oi["Binance"]))

_fu = ing.build_funding_map([
    {"symbol": "BTC", "stablecoin_margin_list": [
        {"exchange": "Binance", "funding_rate": "-0.001419", "funding_rate_interval": 8,
         "next_funding_time": 1759022400000}]},
    {"symbol": "ETH", "stablecoin_margin_list": []},
])
check(set(_fu) == {"BTC", "ETH"}, "费率表按币种基码建键")
check(_fu["BTC"]["Binance"]["funding_rate"] == -0.001419
      and _fu["BTC"]["Binance"]["interval_h"] == 8.0,
      "费率：字符串 → float，间隔单列（不把 interval 当 rate）", str(_fu["BTC"]["Binance"]))
check(_fu["BTC"]["Binance"]["next_ts"] is not None
      and _fu["BTC"]["Binance"]["next_ts"].tzinfo is not None,
      "next_funding_time(ms) → 带时区 UTC datetime")
check(_fu["ETH"] == {}, "无分所列表 ⇒ 空 dict（该币退化为「仅 OI」）")
check(ing._ts_from_ms(None) is None and ing._ts_from_ms("bad") is None,
      "_ts_from_ms 非法输入 ⇒ None（不抛、不编造时刻）")

check(ing.base_code("BTCUSDT") == "BTC" and ing.base_code("1000PEPEUSDT") == "1000PEPE"
      and ing.base_code("BTCUSDC") == "BTC" and ing.base_code("BTC") == "BTC",
      "base_code：去稳定币计价后缀；无后缀原样（对齐 CoinGlass 请求口径）")


# ════════════════════════════════════════════════════════════
# 4. 不变量④：口径隔离（'All' ≠ 分所；PK 结构守卫）
# ════════════════════════════════════════════════════════════
print("\n【测试4】不变量④：'All' 聚合行与分所行结构上不可混算")
check("PRIMARY KEY (symbol, exchange, ts)" in _MIG_SRC,
      "新表 PK 含 exchange（聚合行与分所行是不同主键行，不会互相覆盖）")
check("All" in _MIG_SRC and "严禁" in _MIG_SRC and "相加" in _MIG_SRC,
      "迁移注释显式声明 'All' 行严禁与分所行相加（披露到位）")
check("ON CONFLICT (symbol, exchange, ts)" in _INGEST_SRC,
      "UPSERT 冲突键与 PK 一致（(symbol, exchange, ts)）")
check("COALESCE(EXCLUDED." in _INGEST_SRC,
      "UPSERT 用 COALESCE 保住已有值（本轮未取到的列不被 NULL 冲掉）")
check("币种基码" in _MIG_SRC and "BTCUSDT" in _MIG_SRC,
      "迁移注释披露 symbol = 币种基码，且与 biz.liquidation_history 的合约码口径区分")
check(ing.floor_minute is not None and
      _INGEST_SRC.count("floor_minute") >= 2,
      "快照 ts 按分钟对齐（同轮采集共享同一 ts ⇒ 重跑覆盖而非重复堆积）")


# ════════════════════════════════════════════════════════════
# 5. 不变量⑤：调度注册与错峰（CGV4-002 / 003）
# ════════════════════════════════════════════════════════════
print("\n【测试5】不变量⑤：三条日频作业已注册、错峰、且不带 --resume")
_tree = ast.parse(_read(SCHEDULER))
_sched = None
for _node in _tree.body:
    if isinstance(_node, ast.AnnAssign) and getattr(_node.target, "id", None) == "SCHEDULE":
        _sched = ast.literal_eval(_node.value)
check(isinstance(_sched, list) and len(_sched) > 0, "SCHEDULE 可枚举（守卫非空转）")
_by_key = {e[0]: e for e in (_sched or [])}
_expect = {
    "coinglass_liq_history_binance": ("10 1 * * *", "phase_backfill_liq_history.py"),
    "coinglass_liq_history_all": ("10 2 * * *", "phase_backfill_liq_history.py"),
    "coinglass_derivatives_snapshot": ("10 3 * * *", "ingest_coinglass_derivatives.py"),
}
for key, (cron, script) in _expect.items():
    e = _by_key.get(key)
    check(e is not None, f"{key} 已注册")
    if not e:
        continue
    check(e[1] == cron, f"{key} cron = {cron}（错峰，避免三个作业叠加击穿 30/min）", e[1])
    check(e[2] == script, f"{key} script = {script}", e[2])
    check(os.path.exists(os.path.join(_BIN, e[2])), f"{key} 脚本在 scripts/bin 存在")
    check("--resume" not in e[3], f"{key} 日频作业不带 --resume（游标会永久跳过）", str(e[3]))
check(_by_key["coinglass_liq_history_binance"][3][1] == "binance"
      and _by_key["coinglass_liq_history_all"][3][1] == "all",
      "两个爆仓作业口径分列（binance / all 各自成作业，不混一轮）")
check(_by_key["coinglass_derivatives_snapshot"][3][0] == "--limit",
      "衍生品作业带显式 --limit（单轮有界，防超容器寿命）")


# ════════════════════════════════════════════════════════════
# 6. 客户端：端点路径 / 参数 / 限流响应头捕获
# ════════════════════════════════════════════════════════════
print("\n【测试6】客户端端点路径与 X-RateLimit 捕获（CGV4-000）")
_cli = cg.CoinGlassClient("k", base_url="https://open-api-v4.coinglass.com")
_calls: list[tuple] = []
_cli.get = lambda path, params=None: (_calls.append((path, params)) or [])
_cli.open_interest_exchange_list("BTC")
_cli.funding_rate_exchange_list()
_cli.open_interest_aggregated_history("BTC", interval="4h", limit=3)
_cli.global_long_short_account_ratio_history("Binance", "BTCUSDT", interval="4h", limit=1)
_cli.top_long_short_account_ratio_history("Binance", "BTCUSDT")
_cli.top_long_short_position_ratio_history("Binance", "BTCUSDT")
_cli.funding_rate_accumulated_exchange_list("1d")
_cli.open_interest_aggregated_stablecoin_history(["Binance", "OKX"], "BTC")
check(_calls[0] == ("/api/futures/open-interest/exchange-list", {"symbol": "BTC"}),
      "OI exchange-list 路径/参数正确（symbol=币种基码）", str(_calls[0]))
check(_calls[1] == ("/api/futures/funding-rate/exchange-list", None),
      "funding-rate/exchange-list **无参**一次拉全", str(_calls[1]))
check(_calls[2][0] == "/api/futures/open-interest/aggregated-history"
      and _calls[2][1] == {"symbol": "BTC", "interval": "4h", "limit": 3},
      "OI aggregated-history 路径/参数正确", str(_calls[2]))
check(_calls[3][0] == "/api/futures/global-long-short-account-ratio/history",
      "全站多空比路径正确", str(_calls[3][0]))
check(_calls[4][0] == "/api/futures/top-long-short-account-ratio/history"
      and _calls[5][0] == "/api/futures/top-long-short-position-ratio/history",
      "顶级交易员「账户数 / 持仓量」两条路径各自独立（不合并成一条）")
check(_calls[6] == ("/api/futures/funding-rate/accumulated-exchange-list", {"range": "1d"}),
      "累计费率需 range 参数", str(_calls[6]))
check(_calls[7][1]["exchange_list"] == "Binance,OKX",
      "稳定币本位 OI 的 exchange_list 由 list 逗号拼接（无 'all' 快捷值）", str(_calls[7]))

_cli2 = cg.CoinGlassClient("k", base_url="https://x", min_request_gap=0.0)

class _Resp:
    status_code = 200
    headers = {"X-RateLimit-Remaining": "29", "x-ratelimit-limit": "30", "Content-Type": "application/json"}
    text = ""

    def json(self):
        return {"code": "0", "data": []}

_cli2.session.get = lambda *a, **k: _Resp()
_body = _cli2.get_raw("/api/futures/x")
check(_cli2.last_rate_limit == {"X-RateLimit-Remaining": "29", "x-ratelimit-limit": "30"},
      "只捕获 X-RateLimit-* 头（配额监控用），不带入无关头", str(_cli2.last_rate_limit))
check(_body["_http_status"] == 200, "get_raw 回填 _http_status（HTTP 恒 200，业务看 code）")
check(cg.SUPPORTED_INTERVALS_HOBBYIST[0] == "4h"
      and "1h" not in cg.SUPPORTED_INTERVALS_HOBBYIST,
      "套餐粒度下限 4h 常量未被放宽（1h 会 403）")
check("/api/futures/funding-rate/arbitrage" not in _read(CLIENT),
      "未封装 funding-rate/arbitrage（实测 401 Upgrade plan，HOBBYIST 不可用）")


# ════════════════════════════════════════════════════════════
print(f"\n{passed}/{passed + failed} passed")
sys.exit(1 if failed else 0)