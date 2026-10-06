#!/usr/bin/env python3
"""审计 2026-10-06 修复回归（纯离线）：
  · P0 交易方向参照价锚定系统实时价（prompt 注入 + 后置闸门）
  · P1 赛道市值变化改用期初市值加权（etl_sector_flow_daily）
  · P2 恐贪极值信号统一到 SSOT（消除同一封邮件两个恐贪数字）
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_BIN = os.path.join(os.path.dirname(_HERE), "scripts", "bin")
_SCRIPTS_SRC = os.path.join(os.path.dirname(_HERE), "scripts", "src")
for _p in (_HERE, _SCRIPTS_BIN, _SCRIPTS_SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

_fails = []


def check(cond, msg, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {msg}" + (f" | {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(msg)


# ════════════════════════════════════════════════════════
# P0 单元：参照价解析 / 符号匹配 / 闸门
# ════════════════════════════════════════════════════════
print("[P0] 参照价解析与闸门（_parse_price_num / _asset_names_symbol / _trade_ref_price_ok）")
try:
    import macro_market as mm

    check(mm._parse_price_num(85818) == 85818.0, "int → float")
    check(mm._parse_price_num("85,818") == 85818.0, "千分位逗号")
    check(mm._parse_price_num("$85,818.00") == 85818.0, "美元符号 + 小数")
    check(mm._parse_price_num("85818 USD") == 85818.0, "带单位")
    check(mm._parse_price_num(0) is None, "0 → None（非有效价）")
    check(mm._parse_price_num(-5) is None, "负值 → None")
    check(mm._parse_price_num(None) is None and mm._parse_price_num("") is None, "None/空 → None")
    check(mm._parse_price_num("N/A") is None, "占位符 → None")

    check(mm._asset_names_symbol("BTC", "BTC") is True, "精确匹配")
    check(mm._asset_names_symbol("BTC/ETH", "BTC") is True, "组合标的匹配 BTC")
    check(mm._asset_names_symbol("BTC/ETH", "ETH") is True, "组合标的匹配 ETH")
    check(mm._asset_names_symbol("BTCETF", "BTC") is False, "BTCETF 不误命中 BTC")
    check(mm._asset_names_symbol("AI板块", "BTC") is False, "无符号 → False")

    _pm = {"BTC": 86000.0, "ETH": 2714.0}
    _ok, _r = mm._trade_ref_price_ok({"asset": "BTC", "ref_price": "115000"}, _pm)
    check(_ok is False and "偏离" in _r, "BTC 参照价 115000 偏离 86000 → 拒收", _r)
    _ok2, _ = mm._trade_ref_price_ok({"asset": "BTC", "ref_price": "88000"}, _pm)
    check(_ok2 is True, "BTC 参照价 88000（2.3%）→ 放行")
    _ok3, _ = mm._trade_ref_price_ok({"asset": "SOL", "ref_price": "999999"}, _pm)
    check(_ok3 is True, "未提供实时价的标的（SOL）→ 不误伤")
    _ok4, _r4 = mm._trade_ref_price_ok({"asset": "ETH", "ref_price": None}, _pm)
    check(_ok4 is False and "缺失" in _r4, "命中标的但参照价缺失 → 拒收")

    _kept = mm._filter_trade_ref_price([
        {"asset": "BTC", "ref_price": "115000"},
        {"asset": "ETH", "ref_price": "2714"},
    ], _pm)
    check(len(_kept) == 1 and _kept[0]["asset"] == "ETH", "过滤器只保留合规条目")

    # _build_brief_price_map：m0 优先 + payload 兜底
    _pm2 = mm._build_brief_price_map({"btc_price": 86000, "eth_price": 2714}, {})
    check(_pm2.get("BTC") == 86000.0 and _pm2.get("ETH") == 2714.0, "从 M0_tldr 建价表")
    _pm3 = mm._build_brief_price_map({}, {"dimensions": {"2盘面": {"data": {
        "btc": {"price": 86123.45}, "eth": {"price": 2700}}}}})
    check(_pm3.get("BTC") == 86123.45, "M0 缺失时从 payload 2盘面 兜底")
except Exception as _e:
    check(False, "P0 单元执行", f"{type(_e).__name__}: {_e}")


# ════════════════════════════════════════════════════════
# P0 集成：假 LLM 捕获 prompt + 参照价闸门生效
# ════════════════════════════════════════════════════════
print("[P0] prompt 注入实时价 + 后置闸门（假 LLM）")
_captured = {}


class _FakeLLM:
    def __init__(self, *a, **k):
        pass

    def is_available(self):
        return True

    def chat(self, system_prompt, user_prompt, **kw):
        _captured["system"] = system_prompt
        _captured["user"] = user_prompt
        return json.dumps({
            "headline": "测试", "market_regime": "震荡", "bias": "中性",
            "conviction": "medium", "key_drivers": ["x"], "sector_rotation": "",
            "trade_suggestions": [
                {"direction": "做多", "asset": "BTC", "horizon": "波段",
                 "trigger": "站上 88000", "invalidate": "跌破 84000", "target": "92000",
                 "ref_price": "115000", "ref_as_of": "2026-10-05 08:30",
                 "reason": "越涨越买", "confidence": "high"},
                {"direction": "做多", "asset": "ETH", "horizon": "短线",
                 "trigger": "站上 2800", "invalidate": "跌破 2600", "target": "3000",
                 "ref_price": "2714", "ref_as_of": "2026-10-06 08:30",
                 "reason": "合规", "confidence": "medium"},
            ],
            "no_trade_reason": "",
            "risk_warnings": [], "watchlist": [],
        }, ensure_ascii=False)


try:
    import crypto_research.clients.llm_client as _llmmod
    _llmmod.LLMClient = _FakeLLM
    _gn_orig = mm._fetch_global_cex_netflow_7d
    mm._fetch_global_cex_netflow_7d = lambda: None

    _brief = {
        "M0_tldr": {"btc_price": 86000.0, "eth_price": 2714.0,
                    "btc_change_24h_pct": -0.5, "eth_change_24h_pct": -0.2},
        "M1_cycle": {}, "M2_flow": {},
        "M2_sector_flow": {"status": "empty", "sectors": []},
        "M2_etf_flow": {"status": "empty", "assets": []},
        "M2_whale_moves": {"status": "empty", "transfers": [], "total_count": 0,
                           "total_usd": 0, "net_exchange_usd": 0},
        "M2_exchange_flow": {"status": "empty", "assets": []},
        "M2_holder_concentration": {"status": "empty", "whale_buying": [], "whale_selling": []},
        "M3_highlights": [], "M4_risks": [], "M6_catalyst": {},
        "M6_upcoming_unlocks": {"status": "empty", "unlocks": []},
    }
    _res = mm.generate_morning_brief_ai_summary(_brief)
    _up = _captured.get("user", "")
    _sp = _captured.get("system", "")

    check("BTC 现价：86,000 USD" in _up, "prompt 注入 BTC 实时价（千分位）")
    check("ETH 现价：2,714.00 USD" in _up, "prompt 注入 ETH 实时价")
    check("24h -0.50%" in _up, "prompt 注入 BTC 24h 涨跌（取自 M0 而非空的 m2）")
    check("价格锚定" in _sp, "system prompt 含第 12 条价格锚定约束")
    check("ref_price" in _sp and "系统会校验" in _sp, "system prompt 明示参照价会被校验")

    _trades = _res.get("trade_suggestions") or []
    _assets = [t.get("asset") for t in _trades]
    check(_assets == ["ETH"], f"BTC（参照价 115000）被闸门剔除，仅留 ETH（实际 {_assets}）", str(_trades))
    check(_res.get("no_trade_reason", "") == "", "仍有合规条目时不写 no_trade_reason")

    # 全部偏离 → 走「作废」无操作分支
    class _FakeLLM2(_FakeLLM):
        def chat(self, system_prompt, user_prompt, **kw):
            return json.dumps({
                "headline": "x", "trade_suggestions": [
                    {"direction": "做多", "asset": "BTC", "horizon": "波段",
                     "trigger": "上 118000", "invalidate": "下 112000", "target": "123000",
                     "ref_price": "115000", "ref_as_of": "2025-01-01", "reason": "z", "confidence": "high"}],
                "risk_warnings": [], "watchlist": [],
            }, ensure_ascii=False)

    _llmmod.LLMClient = _FakeLLM2
    _res2 = mm.generate_morning_brief_ai_summary(_brief)
    check((_res2.get("trade_suggestions") or []) == [], "全偏离 → 交易建议清空")
    check("作废" in (_res2.get("no_trade_reason") or ""), "全偏离 → no_trade_reason 说明作废",
          _res2.get("no_trade_reason"))

    mm._fetch_global_cex_netflow_7d = _gn_orig
except Exception as _e:
    check(False, "P0 集成执行", f"{type(_e).__name__}: {_e}")


# ════════════════════════════════════════════════════════
# P1 源码守卫 +（可选）真库功能校验
# ════════════════════════════════════════════════════════
print("[P1] 赛道期初市值加权（etl_sector_flow_daily 源码 + 功能）")
try:
    _etl_path = os.path.join(_HERE, "..", "scripts", "bin", "etl_sector_flow_daily.py")
    _etl_path = os.path.abspath(_etl_path)
    _etl_src = open(_etl_path, encoding="utf-8").read()
    check("market_cap / (1 + dq.percent_change_7d / 100.0)" in _etl_src,
          "7d 权重 = 期初市值（mcap/(1+r)）")
    check("market_cap / (1 + dq.percent_change_24h / 100.0)" in _etl_src, "24h 同为期初市值权重")
    check("market_cap / (1 + dq.percent_change_30d / 100.0)" in _etl_src, "30d 同为期初市值权重")
    check("dq.market_cap * dq.percent_change_7d" not in _etl_src,
          "旧的「当前市值加权」表达式已移除")
except Exception as _e:
    check(False, "P1 源码守卫执行", f"{type(_e).__name__}: {_e}")

# 真库功能校验（有 DATABASE_URL 才跑，否则跳过）
try:
    _root = os.path.abspath(os.path.join(_HERE, "..", "scripts"))
    _env = {}
    _envf = os.path.join(_root, ".env")
    _have_db = False
    if os.path.exists(_envf):
        for _line in open(_envf, encoding="utf-8", errors="ignore"):
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                _env[_k.strip()] = _v.strip().strip('"').strip("'")
        _have_db = bool(_env.get("DATABASE_URL"))
    if _have_db:
        sys.path.insert(0, os.path.join(_root, "bin"))
        sys.path.insert(0, os.path.join(_root, "src"))
        import psycopg
        import etl_sector_flow_daily as etl
        with psycopg.connect(_env["DATABASE_URL"]) as _c2:
            with _c2.cursor() as _cur:
                _cur.execute("SELECT MAX(quote_time)::date FROM src_cmc.cmc_asset_quote_snapshot "
                             "WHERE market_cap IS NOT NULL")
                _d = _cur.fetchone()[0]
            _m = etl.calc_sector_mcap(_c2, _d)
        _infra = (_m.get("infra") or {}).get("mcap_change_7d_pct")
        _ai = (_m.get("ai") or {}).get("mcap_change_7d_pct")
        check(_infra is not None and _infra < 500,
              f"infra 7d 不再被单币拉爆（{_infra}）", str(_infra))
        check(_ai is not None and abs(_ai) < 200, f"ai 7d 量级合理（{_ai}）", str(_ai))
    else:
        print("  [SKIP] 无 DATABASE_URL → 跳过真库功能校验")
except Exception as _e:
    print(f"  [SKIP] 真库功能校验未执行：{type(_e).__name__}: {_e}")


# ════════════════════════════════════════════════════════
# P2 源码守卫：恐贪信号取 SSOT
# ════════════════════════════════════════════════════════
print("[P2] 恐贪极值信号统一到 SSOT")
try:
    _mm_src = open(os.path.join(_HERE, "macro_market.py"), encoding="utf-8").read()
    check("_fg_ssot_val = _resolve_fear_greed_ssot().get(\"value\")" in _mm_src,
          "score_opportunities 恐贪取 SSOT 值")
except Exception as _e:
    check(False, "P2 源码守卫执行", f"{type(_e).__name__}: {_e}")


print()
if _fails:
    print(f"[FAIL] {len(_fails)} 项失败：")
    for _f in _fails:
        print(f"   - {_f}")
    sys.exit(1)
print("[OK] 全部通过")
sys.exit(0)
