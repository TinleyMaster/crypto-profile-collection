#!/usr/bin/env python3
"""高亮信号上游缺陷回归护栏（P1-1 asset_id 时序 / P1-4 占位资产错配）。

运行：python workbench/test_macro_market_p1_upstream.py
      （纯离线，不连网、不连库；DB 层用假连接捕获 SQL）

背景
----
P1-1：select_highlight_signals / select_risk_signals 用 dict(o) 浅拷贝生成合并卡，
      若 asset_id 解析发生在这两个函数**之后**，合并卡进 AI 门禁时无 asset_id，
      被 ai_enrich_signals_v2 整批跳过（实测高亮 AI 覆盖率 8/10 → 2/10）。
P1-4：biz.asset_raises 中曾有 408 行挂在无 canonical_symbol 的占位资产上
      （11125 Aztec Connect 一个资产 407 行 / 287 个协议），消费侧若不过滤，
      会产出「甲协议标题 + 乙资产身份」的嵌合融资卡并带着错误 asset_id 进 AI。

判据
----
A. _symbols_from_opportunity —— 单字符 symbol（H）必须能解析，聚合类 target 不得误判
B. select_highlight_signals —— 合并卡必须继承源机会的 asset_id（P1-1 核心）
C. 时序结构 —— asset_id 解析必须早于高亮/风险精选，且不得回退到「事后补 id」
D. _recent_raises —— SQL 必须排除占位资产（P1-4 消费侧护栏）
E. phase_b2_third_party_raises —— 候选 SQL 必须含写入侧守卫
F. _recent_hacks —— 占位资产上的 hack 行必须「保留事件、剥离身份」（P1-4 第三条路径）
G. phase_b2_third_party_hacks —— 映射只认有真实 symbol 的资产
"""
import os
import re
import sys
from contextlib import contextmanager
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "scripts", "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import macro_market as mm  # noqa: E402

_MACRO_SRC = open(os.path.join(_HERE, "macro_market.py"), encoding="utf-8").read()
_B2_SRC = open(os.path.join(os.path.dirname(_HERE), "scripts", "bin",
                            "phase_b2_third_party_raises.py"), encoding="utf-8").read()
_B2_HACKS_SRC = open(os.path.join(os.path.dirname(_HERE), "scripts", "bin",
                                  "phase_b2_third_party_hacks.py"), encoding="utf-8").read()

PLACEHOLDER_FRAGMENT = "TRIM(a.canonical_symbol) NOT IN ('', '-', '?')"

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


def _opp(target, st, score, aid=None, direction="long"):
    o = {"target": target, "direction": direction, "signal_type": st,
         "conviction_score": score, "conviction_tier": "MED",
         "related_dims": ["mvrv_universe"]}
    if aid is not None:
        o["asset_id"] = aid
    return o


# ── A. 单字符 symbol 解析 ──
print("[A] _symbols_from_opportunity")
check(mm._symbols_from_opportunity({"target": "H"}) == {"H"},
      "A1 单字符 symbol H 可解析（旧 {2,10} 会漏掉，导致误判为聚合/宏观）")
check(mm._symbols_from_opportunity({"target": "BTC"}) == {"BTC"}, "A2 常规 symbol")
check(mm._symbols_from_opportunity({"target": "AI & Big Data"}) == set(),
      "A3 聚合类 target 不误判")
check(mm._symbols_from_opportunity({"target": "牛来"}) == set(),
      "A4 中文描述性 target 不误判")
check(mm._symbols_from_opportunity({"target": "BTC", "involved_symbols": ["ETH"]}) == {"ETH"},
      "A5 involved_symbols 优先于 target")

# ── B. 合并卡继承 asset_id ──
print("[B] select_highlight_signals 合并卡 asset_id")
hl = mm.select_highlight_signals(
    [_opp("BTC", "mvrv_deep_under", 80, aid=1), _opp("BTC", "whale_flow", 70, aid=1)],
    max_total=10, min_resonance=1)
check(len(hl) == 1, "B1 同 target 合并为一张卡", str(len(hl)))
check(bool(hl) and hl[0].get("asset_id") == 1,
      "B2 合并卡从源机会继承 asset_id（P1-1 核心：dict(o) 拷贝需发生在补 id 之后）",
      str(hl[0].get("asset_id") if hl else None))

hl2 = mm.select_highlight_signals([_opp("Base 链", "chain_inflow", 60)], max_total=10,
                                  min_resonance=1)
check(len(hl2) == 1 and hl2[0].get("asset_id") is None,
      "B3 聚合类卡片保持无 asset_id（不得凭空挂身份）")

# ── C. 时序结构护栏 ──
print("[C] asset_id 解析时序")
_resolve_pos = _MACRO_SRC.find("symbol_to_asset = _resolve_symbols_to_asset_ids(all_symbols)")
_hl_pos = _MACRO_SRC.find("highlights = select_highlight_signals(")
check(_resolve_pos != -1, "C1 存在 asset_id 解析块")
check(_hl_pos != -1, "C2 存在 select_highlight_signals 调用")
check(-1 < _resolve_pos < _hl_pos,
      "C3 asset_id 解析早于高亮精选（否则合并卡必然缺 id）",
      f"resolve@{_resolve_pos} highlights@{_hl_pos}")
check("为 highlights 和 risk_signals 也补上 asset_id" not in _MACRO_SRC,
      "C4 未回退到「AI 增强后补 asset_id」的事后补丁")


# ── D. _recent_raises 占位资产护栏 ──
print("[D] _recent_raises 消费侧护栏")
_sql_sink: list[str] = []


class _FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        _sql_sink.append(sql)

    def fetchall(self):
        return []


class _FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self, *a, **k):
        return _FakeCursor()


@contextmanager
def _fake_get_connection(url):
    yield _FakeConn()


import crypto_research.config as _cfg  # noqa: E402
import crypto_research.db.conn as _dbconn  # noqa: E402

_orig_conn = _dbconn.get_connection
_orig_settings = _cfg.get_settings
_dbconn.get_connection = _fake_get_connection
_cfg.get_settings = lambda **k: SimpleNamespace(database_url="dummy")
try:
    mm._recent_raises(window_days=90, limit=5)
finally:
    _dbconn.get_connection = _orig_conn
    _cfg.get_settings = _orig_settings

_raise_sql = _sql_sink[-1] if _sql_sink else ""
check(bool(_raise_sql), "D1 _recent_raises 实际执行了查询")
check(PLACEHOLDER_FRAGMENT in _raise_sql,
      "D2 查询排除占位资产（canonical_symbol 为空/'-'/'?'）", _raise_sql.replace("\n", " ")[:160])

# ── E. 写入侧守卫 ──
print("[E] phase_b2_third_party_raises 写入侧守卫")
check(PLACEHOLDER_FRAGMENT in _B2_SRC, "E1 候选 SQL 含占位资产守卫")
_branches = re.findall(r'candidate_sql = f"""([\s\S]*?)"""', _B2_SRC)
check(len(_branches) == 2 and all("INNER JOIN core.asset AS a" in b for b in _branches),
      "E2 两个候选分支（全量/--asset-id）均已加入 core.asset 连接",
      f"branches={len(_branches)}")
check("def count_placeholder_mapped_protocols" in _B2_SRC,
      "E3 保留被守卫排除协议数的可观测性")

# ── F. _recent_hacks 占位资产护栏（保留事件、剥离身份）──
print("[F] _recent_hacks 消费侧护栏")
_sql_sink.clear()
_dbconn.get_connection = _fake_get_connection
_cfg.get_settings = lambda **k: SimpleNamespace(database_url="dummy")
try:
    mm._recent_hacks(window_days=14, limit=5)
finally:
    _dbconn.get_connection = _orig_conn
    _cfg.get_settings = _orig_settings

_hack_sql = _sql_sink[-1] if _sql_sink else ""
_norm_hack_sql = " ".join(_hack_sql.split())
check(bool(_hack_sql), "F1 _recent_hacks 实际执行了查询")
check(PLACEHOLDER_FRAGMENT in _hack_sql,
      "F2 查询按占位资产剥离身份（CASE WHEN … THEN 置空，而非 WHERE 排除）")
check(_hack_sql.count(PLACEHOLDER_FRAGMENT) == 2,
      "F3 asset_id 与 symbol 两列都受同一守卫约束",
      f"count={_hack_sql.count(PLACEHOLDER_FRAGMENT)}")
check("h.name" in _hack_sql and -1 < _hack_sql.find(PLACEHOLDER_FRAGMENT)
      < _hack_sql.find("FROM biz.asset_hacks"),
      "F4 守卫位于 SELECT 列表内（保留事件行，只剥离身份）",
      _norm_hack_sql[:160])

# ── G. 写入侧守卫（hacks）──
print("[G] phase_b2_third_party_hacks 写入侧守卫")
check("placeholder_predicate" in _B2_HACKS_SRC and PLACEHOLDER_FRAGMENT in _B2_HACKS_SRC,
      "G1 映射查询含占位资产守卫")
check("INNER JOIN core.asset AS a ON a.asset_id = asm.asset_id" in _B2_HACKS_SRC,
      "G2 映射查询已加入 core.asset 连接")
check("skipped_placeholder_mapped" in _B2_HACKS_SRC,
      "G3 保留被守卫排除协议数的可观测性")

# ── 汇总 ──
print(f"\n{passed}/{passed + failed} 通过")
sys.exit(1 if failed else 0)