#!/usr/bin/env python3
"""Solana 链上持仓采集修复（2026-09-27）· 离线回归护栏。

运行：python workbench/test_solana_holder_20260927.py（纯离线，不连网、不连库）

背景（工单 OBI-OPT-SNAPSHOT-FRESHNESS §五 新发现①）：solana 链上快照自 2026-08-30
起长期断更，经查证 **HELIUS_API_KEY 在 prod 已正确配置**，真因是
（1）mint 参数/数据：solana 原生 mint（So1111…1111）与超大持币数 mint（USDC/USDT）
    在 Helius 恒报 -32602 / -32600；
（2）`_scrape_holders_helius` 拿到空结果时静默 return None（真因不可见，导致误判）；
（3）其后回退的 Solscan(Playwright) 已被 Cloudflare 全面拦截 → 恒失败却每币耗时 2~3 分钟，
    单轮 300 币即 8~15h，链上任务因此被判「stuck / 12h 超时」而杀。

覆盖：
  A. solana_client.classify_rpc_error —— 失败归类（invalid_mint / too_many_accounts / rpc_error）
  B. solana_client._json_rpc —— last_error 落盘（错误 / 429 / 网络异常 / 成功清空）
  C. phase_chain_holder_scrape._scrape_holders_helius —— 失败类别向调用方透传（不再静默 None）
  D. phase_chain_holder_scrape.scrape_holders —— 已摘除 Solscan 回退，且永久不可采直接判死
  E. phase_chain_holder_batch —— 剔除 solana 原生 mint + solana 单币超时下调
"""
import os
import re
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
_SRC = os.path.join(_SCRIPTS, "src")
_BIN = os.path.join(_SCRIPTS, "bin")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
sys.path.insert(0, _SRC)
sys.path.insert(0, _BIN)

from crypto_research.clients.solana_client import SolanaClient, classify_rpc_error  # noqa: E402

_SCRAPE_SRC = open(os.path.join(_BIN, "phase_chain_holder_scrape.py"), encoding="utf-8").read()
_BATCH_SRC = open(os.path.join(_BIN, "phase_chain_holder_batch.py"), encoding="utf-8").read()

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


# ── A. classify_rpc_error ──
print("[A] classify_rpc_error 失败归类")
check(classify_rpc_error({"code": -32602, "message": "Invalid param: not a Token mint"}) == "invalid_mint",
      "A1 -32602 → invalid_mint（原生 mint / 非 token 地址，永久不可采）")
check(classify_rpc_error({"code": -32602, "message": "Invalid param: Invalid"}) == "invalid_mint",
      "A2 -32602 其它文案同样归 invalid_mint（如 pump 未发币地址）")
check(classify_rpc_error(
    {"code": -32600, "message": "Too many accounts requested (10000000 pubkeys)"}) == "too_many_accounts",
    "A3 -32600 + too many accounts → too_many_accounts（USDC/USDT 类大币，免费档无解）")
check(classify_rpc_error({"code": -32600, "message": "other"}) == "rpc_error",
      "A4 -32600 其它文案归 rpc_error（非永久判死）")
check(classify_rpc_error(None) == "" and classify_rpc_error({}) == "",
      "A5 空 error 返回空串（不误判）")


# ── B. _json_rpc 的 last_error ──
print("[B] SolanaClient._json_rpc 失败可观测")


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _Sess:
    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc

    def post(self, *a, **k):
        if self._exc is not None:
            raise self._exc
        return self._resp


def _client(sess):
    c = SolanaClient(api_key="k", calls_per_second=10000)
    c.session = sess  # 注入假 session
    return c


c = _client(_Sess(_Resp(200, {"error": {"code": -32602, "message": "not a Token mint"}})))
check(c._json_rpc("getTokenLargestAccounts", ["m"], retries=1) is None
      and (c.last_error or {}).get("kind") == "invalid_mint",
      "B1 RPC error → last_error.kind=invalid_mint", str(c.last_error))

c = _client(_Sess(_Resp(429, {})))
check(c._json_rpc("getTokenSupply", ["m"], retries=1) is None
      and (c.last_error or {}).get("kind") == "rate_limited",
      "B2 429 重试耗尽 → last_error.kind=rate_limited（原实现静默无痕）", str(c.last_error))

c = _client(_Sess(exc=RuntimeError("boom")))
check(c._json_rpc("getTokenSupply", ["m"], retries=1) is None
      and (c.last_error or {}).get("kind") == "network",
      "B3 网络异常 → last_error.kind=network", str(c.last_error))

c = _client(_Sess(_Resp(200, {"result": {"value": []}})))
check(c._json_rpc("getTokenLargestAccounts", ["m"], retries=1) == {"value": []}
      and c.last_error is None,
      "B4 成功 → last_error 清空（不残留上一次失败）")


# ── C. _scrape_holders_helius 透传失败类别 ──
print("[C] _scrape_holders_helius 失败类别透传")
import crypto_research.config as _cfg  # noqa: E402
import crypto_research.clients.solana_client as _scmod  # noqa: E402
import phase_chain_holder_scrape as _scrape  # noqa: E402

_orig_settings, _orig_client = _cfg.get_settings, _scmod.SolanaClient
_cfg.get_settings = lambda **kw: types.SimpleNamespace(helius_api_key="k")


class _Stub:
    def __init__(self, holders=None, last_error=None):
        self._holders = holders if holders is not None else []
        self.last_error = last_error

    def get_token_holders(self, mint, limit=20):
        return {"top_holders_json": self._holders}


def _stub_factory(instance):
    def _make(api_key=None, **kw):
        return instance
    return _make


try:
    _scmod.SolanaClient = _stub_factory(_Stub(holders=[{"rank": 1}], last_error=None))
    res, kind = _scrape._scrape_holders_helius("solana", "mintX", 20)
    check(kind == "" and res and len(res["top_holders_json"]) == 1,
          "C1 成功 → 类别为空串并返回结果", f"{kind}")

    _scmod.SolanaClient = _stub_factory(
        _Stub(holders=[], last_error={"kind": "invalid_mint", "detail": "not a Token mint"}))
    res, kind = _scrape._scrape_holders_helius("solana", "So11111111111111111111111111111111111111111", 20)
    check(res is None and kind == "invalid_mint",
          "C2 原生 mint → (None, invalid_mint)，不再静默 return None", f"{kind}")

    _scmod.SolanaClient = _stub_factory(
        _Stub(holders=[], last_error={"kind": "too_many_accounts", "detail": "too many"}))
    res, kind = _scrape._scrape_holders_helius("solana", "EPjFWdd5", 20)
    check(res is None and kind == "too_many_accounts",
          "C3 超大持币 mint → (None, too_many_accounts)", f"{kind}")

    _scmod.SolanaClient = _stub_factory(_Stub(holders=[], last_error=None))
    res, kind = _scrape._scrape_holders_helius("solana", "mintY", 20)
    check(res is None and kind == "empty",
          "C4 无 RPC 错误但空结果 → empty（与故障区分）", f"{kind}")
finally:
    _cfg.get_settings, _scmod.SolanaClient = _orig_settings, _orig_client

check(_scrape.PERMANENT_MINT_ERRORS == frozenset({"invalid_mint", "too_many_accounts"}),
      "C5 PERMANENT_MINT_ERRORS 恰含两类永久不可采", str(_scrape.PERMANENT_MINT_ERRORS))


# ── D. Solscan 回退已摘除 ──
print("[D] scrape_holders 已摘除 Solscan 回退")
check("sol_result = _scrape_holders_solscan(" not in _SCRAPE_SRC,
      "D1 scrape_holders 不再调用 _scrape_holders_solscan（死回退已摘除）")
check('回退 Solscan (Playwright)' not in _SCRAPE_SRC,
      "D2 旧的「回退 Solscan (Playwright)」提示字符串已清除")
check(re.search(r'if chain == "solana":[\s\S]{0,1200}?return None', _SCRAPE_SRC) is not None
      and "sol_result" not in _SCRAPE_SRC,
      "D3 solana 分支失败即 return None（快速失败，不悬挂）")
check("in PERMANENT_MINT_ERRORS" in _SCRAPE_SRC,
      "D4 永久不可采类别直接判死（不再浪费重试）")
check("Helius 未取到持仓（" in _SCRAPE_SRC,
      "D5 失败真因显式打印（根因可观测，防再次误判）")
check("当前未被调用" in _SCRAPE_SRC,
      "D6 保留的 _scrape_holders_solscan 已注明未被调用（非隐形死码）")


# ── E. batch 侧 ──
print("[E] phase_chain_holder_batch")
_m = re.search(r"EXCLUDE_CONTRACTS\s*=\s*\((.*?)\n\)", _BATCH_SRC, re.S)
_excl = _m.group(1) if _m else ""
check("So11111111111111111111111111111111111111111" in _excl,
      "E1 剔除 solana 原生 mint（不再白占待采集名额）")
check("0x43fd9de06bb69ad771556e171f960a91c42d2955" in _excl,
      "E2 原有 BTC 错误合约仍在剔除清单（未回退）")
check("max(args.timeout, 120)" in _BATCH_SRC and "max(args.timeout, 300)" not in _BATCH_SRC,
      "E3 solana 单币超时由 300s 下调至 120s（Playwright 回退已摘除）")

# ── F. solana 地址结构护栏 ──
print("[F] phase_chain_holder_batch solana 地址结构护栏")
import phase_chain_holder_batch as _batch  # noqa: E402

_re = _batch.SOLANA_ADDR_RE
_ok = [
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
    "boopkpWqe68MSxLqBGogs8ZbUDN4GXaLhFwNP7mpP1i",      # BOOP（正确大小写，43 位）
    "So11111111111111111111111111111111111111111",      # 原生 mint（由 EXCLUDE 剔除，非护栏）
]
_bad = [
    "token/EdAhkbj5nF9sRM7XN7ewuW8C9XEUMs8P7cnoQ57SYE96",  # Solscan URL 片段（L=50）
    "0xadb2437e6f65682b85f814fbc12fec0508a7b1d0",          # EVM 地址贴到 solana 链
    "7yf97k6jrbkb7bxjyxzmwqlqyxvirltcssgb75qlqan8",        # 降格全小写且含 base58 禁用字符 l
    "c6q5fmpupjbpox84wbce3rn8hyjg11o4yhembqsuys5l",        # 同上（含 l）
]
check(all(re.match(_re, a) for a in _ok), "F1 合法 solana 公钥全部通过护栏")
check(all(not re.match(_re, a) for a in _bad),
      "F2 URL 片段 / EVM 地址 / 含 l 的降格地址被拦下（零误伤的结构判据）")
check(_batch._sol_guard("solana")[0] != "" and _batch._sol_guard("eth") == ("", ()),
      "F3 仅 solana 链追加护栏，其它链不受影响")
check(_BATCH_SRC.count("{sol_guard}") == 2,
      "F4 get_pending_assets / get_total_pending 两处均已接入（口径一致，避免计数漂移）")

print("=" * 60)
print(f"结果：{passed} 通过 / {failed} 失败")
print("=" * 60)
sys.exit(1 if failed else 0)