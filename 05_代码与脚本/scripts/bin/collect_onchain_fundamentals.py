#!/usr/bin/env python3
"""链上基本面因子采集器：持有者分布 + 合约安全核查 → 落库（按日快照，供因子回测）。

职责边界：只收集链上事实并落库，不做任何投研判断。

用法：
    python bin/collect_onchain_fundamentals.py --symbol PONS
    python bin/collect_onchain_fundamentals.py --symbol PONS --top 500
    python bin/collect_onchain_fundamentals.py --symbol UNI --chain eth --rpc https://... --explorer https://... --contract 0x...

产出（均幂等，当日重复跑自动覆盖）：
    - biz.onchain_holder_snapshot     集中度聚合快照（top1/5/10/20/50/100 + burn/流通）
    - biz.onchain_holder_rank         Top 持有者明细快照（地址/持仓/类型）
    - biz.asset_contract_security     合约安全（代理/实现/部署者/信誉，upsert）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── 路径约定 ───────────────────────────────────────────────────────
_here = os.path.dirname(os.path.abspath(__file__))
_code_root = os.path.dirname(os.path.dirname(_here))
_scripts_src = os.path.join(_code_root, "scripts", "src")
for cand in (_scripts_src,):
    if cand and os.path.isdir(cand) and cand not in sys.path:
        sys.path.insert(0, cand)

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import requests  # noqa: E402

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402
from crypto_research.persist import persist_contract_security, persist_holder_snapshot  # noqa: E402
from data_health import resolve_asset  # noqa: E402

# EIP1967 / OpenZeppelin storage slots
SLOT_IMPL = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
SLOT_OZ_ADMIN = "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103"
SLOT_OWNER = "0x0000000000000000000000000000000000000000000000000000000000000000"
# 方法选择器
SEL_IMPL = "0x5c60da1b"  # implementation()
SEL_ADMIN = "0xf851a440"  # admin()
SEL_OWNER = "0x8da5cb5b"  # owner()
SEL_NAME = "0x06fdde03"  # name()


# ════════════════════════════════════════════════════════════════
# 链上读取（公共 RPC，无 API Key）
# ════════════════════════════════════════════════════════════════
def _rpc(url: str, method: str, params: list) -> dict:
    r = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                      timeout=30, headers={"Content-Type": "application/json"})
    return r.json()


def _last_addr(hexv: str | None) -> str | None:
    """0x 十六进制 → 地址；空/零地址/过短 → None。"""
    if not hexv or hexv == "0x" or len(hexv) < 66:
        return None
    try:
        if int(hexv, 16) == 0:
            return None
    except ValueError:
        return None
    return "0x" + hexv[-40:].lower()


def _eth_call(url: str, to: str, data: str) -> str | None:
    """eth_call；revert/无返回 → None。"""
    res = _rpc(url, "eth_call", [{"to": to, "data": data}, "latest"])
    if "result" in res and res["result"] not in (None, "0x"):
        return res["result"]
    return None


def inspect_contract(url: str, addr: str) -> dict:
    """代理/属性核查：EIP1967 slots + 标准接口 + 合约代码。返回纯事实。"""
    out = {"address": addr, "is_contract": False, "eip1967_impl": None,
           "oz_admin": None, "impl_call": None, "admin_call": None,
           "owner_call": None, "name_hex": None, "is_proxy": False,
           "proxy_type": "unknown"}
    code = _rpc(url, "eth_getCode", [addr, "latest"]).get("result")
    if not code or code in ("0x", "0x0"):
        return out
    out["is_contract"] = True
    out["eip1967_impl"] = _last_addr(_rpc(url, "eth_getStorageAt", [addr, SLOT_IMPL, "latest"]).get("result"))
    out["oz_admin"] = _last_addr(_rpc(url, "eth_getStorageAt", [addr, SLOT_OZ_ADMIN, "latest"]).get("result"))
    out["impl_call"] = _last_addr(_eth_call(url, addr, SEL_IMPL))
    out["admin_call"] = _last_addr(_eth_call(url, addr, SEL_ADMIN))
    out["owner_call"] = _last_addr(_eth_call(url, addr, SEL_OWNER))
    name_hex = _eth_call(url, addr, SEL_NAME)
    out["name_hex"] = name_hex
    if name_hex and name_hex != "0x":
        try:
            ln = int(name_hex[2:66], 16) * 2
            out["name_text"] = bytes.fromhex(name_hex[66:66 + ln]).decode(errors="ignore")
        except Exception:
            pass
    # 代理判定：EIP1967 slot 或标准实现接口命中
    if out["eip1967_impl"]:
        out["is_proxy"], out["proxy_type"] = True, "eip1967"
    elif out["oz_admin"]:
        out["is_proxy"], out["proxy_type"] = True, "oz"
    elif out["impl_call"] or out["admin_call"]:
        out["is_proxy"], out["proxy_type"] = True, "transparent"
    return out


# ════════════════════════════════════════════════════════════════
# Blockscout API（浏览器级 headers + 限流重试）
# ════════════════════════════════════════════════════════════════
def _bs_get(explorer: str, path: str, params: dict | None = None, retries: int = 4) -> dict | None:
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{explorer}/",
    })
    for i in range(retries):
        try:
            r = sess.get(f"{explorer}/api/v2/{path}", params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
        time.sleep(8 * (i + 1))
    return None


def fetch_holders(explorer: str, contract: str, top: int) -> list[dict]:
    """Blockscout v1 getTokenHolders 分页拉 Top 持有者（冷却 + 退避重试 + 限流控制）。"""
    holders: list[dict] = []
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{explorer}/token/{contract}",
    })
    page = 1
    stall = 0  # 连续失败计数
    while len(holders) < top and page <= (top // 100) + 2:
        if page == 1 and stall == 0:
            time.sleep(10)  # 开场冷却，降低被 Cloudflare 立即拦截概率
        d = None
        for attempt in range(4):
            try:
                r = sess.get(f"{explorer}/api",
                             params={"module": "token", "action": "getTokenHolders",
                                     "contractaddress": contract, "page": page, "offset": 100},
                             timeout=30)
                if r.status_code == 200:
                    try:
                        d = r.json()
                    except Exception:
                        d = None
                    break
            except requests.RequestException:
                pass
            time.sleep(8 * (attempt + 1))
        if not d or not isinstance(d.get("result"), list) or not d["result"]:
            stall += 1
            if stall >= 3:
                break
            time.sleep(10)
            continue
        stall = 0
        holders.extend(d["result"])
        if len(d["result"]) < 100:
            break
        page += 1
        time.sleep(1.5)
    return holders[:top]


def fetch_contract_meta(explorer: str, contract: str) -> dict:
    """Blockscout v2 合约元数据：implementations/proxy_type/creator/verified/reputation。"""
    d = _bs_get(explorer, f"addresses/{contract}")
    if not d:
        return {}
    return {
        "contract_name": d.get("name"),
        "implementations": d.get("implementations") or [],
        "proxy_type": d.get("proxy_type"),
        "creator_addr": d.get("creator_address_hash"),
        "contract_verified": d.get("is_verified"),
        "reputation": d.get("reputation"),
        "is_scam": d.get("is_scam"),
    }


# ════════════════════════════════════════════════════════════════
# 落库统一走 crypto_research.persist（persist_holder_snapshot /
# persist_contract_security），本脚本只负责采集与调用。
# ════════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════════
def main() -> int:
    parser = argparse.ArgumentParser(description="链上基本面因子采集器（持有者分布 + 合约安全核查 → 落库）")
    parser.add_argument("--symbol", required=True, help="币种符号/名称/asset_id")
    parser.add_argument("--chain", default="robinhood", help="链标识（默认 robinhood）")
    parser.add_argument("--contract", default="", help="代币合约地址（不传则从 DB 解析）")
    parser.add_argument("--rpc", default="https://rpc.mainnet.chain.robinhood.com", help="公共 RPC")
    parser.add_argument("--explorer", default="https://robinhoodchain.blockscout.com", help="Blockscout 实例")
    parser.add_argument("--top", type=int, default=300, help="Top 持有者拉取数（默认 300）")
    parser.add_argument("--total-supply", type=float, default=0,
                        help="总供应（代币本位，0=从链上/DB 推断；PONS 已知 1B）")
    parser.add_argument("--total-holders", type=int, default=0,
                        help="独立持有者总数（浏览器页面值；0=留空，仅记录拉取数）")
    parser.add_argument("--dry-run", action="store_true", help="只采集不落库")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    burn_addr = "0x000000000000000000000000000000000000dEaD"

    with get_connection(settings.database_url) as conn:
        candidates = resolve_asset(conn, int(args.symbol) if args.symbol.isdigit() else args.symbol)
        if not candidates:
            print(f"[collect] 未找到资产: {args.symbol}")
            return 1
        asset = candidates[0]
        aid = asset["asset_id"]
        print(f"[collect] 标的: {aid} {asset['canonical_symbol']} {asset['canonical_name']}")

        # 合约地址解析：--contract 优先，否则 core.asset_contract / doc_source_entry(explorer)
        contract = (args.contract or "").strip()
        if not contract:
            with conn.cursor() as cur:
                cur.execute("SELECT c.contract_address FROM core.asset_contract c "
                            "WHERE c.asset_id=%s AND LOWER(c.chain)=LOWER(%s) "
                            "ORDER BY c.is_primary DESC LIMIT 1",
                            (aid, args.chain))
                row = cur.fetchone()
                if row:
                    contract = row[0]
        if not contract:
            print("[collect] 未解析到合约地址，请用 --contract 指定")
            return 1
        print(f"[collect] 合约: {contract} @ {args.chain}")

        # ── 持有者分布 ──
        print(f"[collect] 拉取 Top{args.top} 持有者（Blockscout）...")
        holders = fetch_holders(args.explorer, contract, args.top)
        print(f"[collect] 拉到 {len(holders)} 个地址")
        if not holders:
            print("[collect] 持有者拉取失败（可能限流/需登录），跳过持有者快照")

        # 地址类型判定（对前 50 大地址 getCode，够识别合约）
        is_contract_map: dict[str, bool] = {}
        for h in holders[:50]:
            low = h["address"].lower()
            code = _rpc(args.rpc, "eth_getCode", [low, "latest"]).get("result")
            is_contract_map[low] = bool(code and code not in ("0x", "0x0"))

        # ── 合约安全核查 ──
        print(f"[collect] 合约安全核查（RPC + Blockscout）...")
        insp = inspect_contract(args.rpc, contract)
        meta = fetch_contract_meta(args.explorer, contract)
        print(f"[collect] 合约名={insp.get('name_text') or meta.get('contract_name')} "
              f"is_proxy={insp['is_proxy']}({insp['proxy_type']}) "
              f"verified={meta.get('contract_verified')} reputation={meta.get('reputation')}")

        total_supply = args.total_supply
        if not total_supply:
            total_supply = 1_000_000_000 if args.chain == "robinhood" else 0

        if args.dry_run:
            print("[collect] dry-run：跳过落库")
            return 0

        # ── 落库 ──
        today = datetime.now(timezone.utc).date()
        sid = 0
        if holders:
            sid = persist_holder_snapshot(conn, aid, args.chain, contract, today, holders,
                                          burn_addr, is_contract_map, total_supply,
                                          total_holders=args.total_holders)
            print(f"[collect] 持有者快照: snapshot_id={sid} @ {today} "
                  f"（当日重复跑已覆盖）")
        persist_contract_security(conn, aid, args.chain, contract, insp, meta)
        print(f"[collect] 合约安全: upsert asset_contract_security 完成")
        print("[collect] 完成：数据已落库，可查询 biz.onchain_holder_snapshot / onchain_holder_rank / asset_contract_security")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
