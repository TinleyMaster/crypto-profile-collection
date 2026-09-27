"""离线探针：core.asset_contract 的 solana 地址写入护栏（2026-09-27）。

背景：core.asset_contract 的 solana 链上有 16 行历史污染——15 行被 .lower() 降格
成非 base58 形态、1 行混入 EVM hex 与浏览器 URL 片段、另有 1 行原生 mint
（So1111…1111）被当成 SPL 代币合约。它们自 2026-07-31 建表后一直残留。

本探针锁定三处写入护栏（两处 SQL 侧 + 一处 Python 侧），确保不会再产生这类污染：
  1) phase_chain_contract_backfill.is_valid_solana_addr  —— Python 侧结构校验
  2) phase_a_build_core.POPULATE_FROM_CMC 的 solana 条件 —— 每日 03:00 流水线 ④
  3) phase_a_build_core.POPULATE_FROM_DL  的 solana 条件 —— DefiLlama 回填

纯离线：不连库、不发网络请求。
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
_BIN = os.path.join(_SCRIPTS, "bin")
for _p in (os.path.join(_SCRIPTS, "src"), _BIN):
    if _p not in sys.path:
        sys.path.insert(0, _p)

PASS = 0
FAIL = 0


def check(cond: bool, name: str, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} :: {detail}")


# ── 样本 ────────────────────────────────────────────────────
VALID_B58 = "boopkpWqe68MSxLqBGogs8ZbUDN4GXaLhFwNP7mpP1i"        # 合法 base58（含大写）
VALID_LOWER = "epjfwdd5aufqssqem2qn1xzybapc8g4weggkzwytdt1v"    # 合法全小写（不含 0OIl）
WRAPPED_SOL = "So11111111111111111111111111111111111111111"     # 原生 mint
SYS_PROGRAM = "11111111111111111111111111111111"                # 原生 mint
LOWER_POLLUTED = "2hrz5r18h48b8ptl3n8n5zx65zdb4o2cmchcp3qfsfye"  # 含禁用字符 l 的 lower 产物
EVM_ADDR = "0xadb2437e6f65682b85f814fbc12fec0508a7b1d0"          # EVM hex
URL_FRAG = "token/EdAhkbj5nF9sRM7XN7ewuW8C9XEUMs8P7cnoQ57SYE96"  # 浏览器 URL 片段

import phase_a_build_core as core  # noqa: E402
import phase_chain_contract_backfill as bf  # noqa: E402

print("\n== A. is_valid_solana_addr（Python 侧结构校验）==")
_A = [
    (VALID_B58, True, "合法 base58（含大写）"),
    (VALID_LOWER, True, "合法全小写（不含 0OIl，不可误伤）"),
    (WRAPPED_SOL, False, "Wrapped SOL 原生 mint"),
    (SYS_PROGRAM, False, "System Program 原生 mint"),
    (LOWER_POLLUTED, False, "含禁用字符 l 的 lower 污染"),
    (EVM_ADDR, False, "EVM hex 地址"),
    (URL_FRAG, False, "URL 片段"),
    ("a" * 31, False, "长度 31（过短）"),
    ("a" * 45, False, "长度 45（过长）"),
    ("", False, "空串"),
]
for addr, expect, desc in _A:
    got = bf.is_valid_solana_addr(addr)
    check(got == expect, f"A: {desc} -> {expect}", f"实际 {got}")

print("\n== B. parse_contracts 端到端（假 payload）==")
payload = {"data": {"1": {"contract_address": [
    {"platform": {"name": "Solana"}, "contract_address": VALID_B58},
    {"platform": {"name": "solana (spl)"}, "contract_address": WRAPPED_SOL},
    {"platform": {"name": "Solana"}, "contract_address": EVM_ADDR},
    {"platform": {"name": "Solana"}, "contract_address": URL_FRAG},
    {"platform": {"name": "Ethereum"}, "contract_address": EVM_ADDR},
]}}}
rows = bf.parse_contracts(payload, {1: 999})
addrs = [r["contract_address"] for r in rows]
check(len(rows) == 2, "B: 5 条输入只保留 2 条（solana 合法 1 + ethereum 1）", f"得到 {rows}")
check(VALID_B58 in addrs, "B: 合法 solana 地址被保留")
check(any(r["chain"] == "ethereum" for r in rows), "B: 非 solana 链不受护栏影响")
check(all(r["contract_address"] != EVM_ADDR for r in rows if r["chain"] == "solana"),
      "B: solana 行未写入 EVM 地址")
check(WRAPPED_SOL not in addrs, "B: 原生 mint 未被写入")
check(all(r["chain"] != "solana" or bf.is_valid_solana_addr(r["contract_address"]) for r in rows),
      "B: 输出中 solana 行全部通过结构校验")

print("\n== C. POPULATE_FROM_CMC 护栏（每日 03:00 流水线 ④，15 行的源头）==")
cmc = core.POPULATE_FROM_CMC
check("[1-9A-HJ-NP-Za-km-z]{32,44}" in cmc, "C: 含 base58 形态正则")
check(WRAPPED_SOL in cmc and SYS_PROGRAM in cmc, "C: 含原生 mint 白名单排除")
check("LOWER(m.platform_name) NOT IN ('solana', 'solana (spl)')" in cmc,
      "C: solana 条件与链归一化口径一致")
check("WHEN LOWER(m.platform_name) IN ('solana', 'solana (spl)') THEN m.token_address" in cmc,
      "C: 仍保留「solana 地址原样」的链感知赋值（未被护栏误改）")
check("ELSE LOWER(m.token_address)" in cmc, "C: 非 solana 仍统一小写")

print("\n== D. POPULATE_FROM_DL 护栏（DefiLlama 回填）==")
dl = core.POPULATE_FROM_DL
check("[1-9A-HJ-NP-Za-km-z]{32,44}" in dl, "D: 含 base58 形态正则")
check(WRAPPED_SOL in dl and SYS_PROGRAM in dl, "D: 含原生 mint 白名单排除")
check("LOWER(COALESCE(addr_chain, raw_chain)) <> 'solana'" in dl,
      "D: solana 条件与链归一化口径一致")
check("WHEN LOWER(COALESCE(addr_chain, raw_chain)) = 'solana' THEN contract_address" in dl,
      "D: 仍保留「solana 地址原样」的链感知赋值")

print("\n== E. 相邻回归：其它写入方未被波及 ==")
check("CASE_SENSITIVE_CHAINS" in core.__dict__ and "solana" in core.CASE_SENSITIVE_CHAINS,
      "E: CASE_SENSITIVE_CHAINS 仍含 solana（CG 侧）")
import phase_chain_holder_scrape as scrape  # noqa: E402
check(scrape._norm_addr("solana", VALID_B58) == VALID_B58,
      "E: holder_scrape._norm_addr 对 solana 仍保留原样")
check(scrape._norm_addr("bsc", EVM_ADDR.upper()) == EVM_ADDR,
      "E: holder_scrape._norm_addr 对 EVM 仍转小写")

print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)