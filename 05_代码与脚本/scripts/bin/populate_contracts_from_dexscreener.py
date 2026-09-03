"""
从 DexScreener 补充无合约地址资产的合约地址。

适用场景：CMC/CG 都未提供合约地址、且「近期新上市」的资产（典型如
Cosmos 原生代币，但在 BSC 等 EVM 链上有交易所上市用的包装代币）。

为什么只处理近期新上市资产：
  - 老牌原生币（LTC/XRP/DASH 等）无合约地址是常态，它们有自己的链，
    DexScreener 只会匹配到第三方桥接的 wrapped 代币，补入反而污染数据。
  - 只有新上市资产（launch_date 近期）才存在「CMC/CG 数据未跟上、
    但 DEX 已有真实流通」的窗口，此时补合约才有价值。

匹配策略（防污染）：
  1. 仅处理 launch_date 在最近 --max-age-days 天内的资产；
  2. baseToken.symbol 与资产 canonical_symbol 规范化后精确一致；
  3. baseToken.name 与资产 canonical_name 规范化后一致（大小写/空白/符号不敏感）；
  4. 仅收录已知 EVM 链；
  5. fdv >= --min-fdv（过滤同名假币，默认 50 万美元）；
  6. 每条链只取 fdv 最高的一条合约；
  7. 最多补 --max-chains 条链（默认 2）。

用法：
  python populate_contracts_from_dexscreener.py [--limit N] [--min-fdv X] [--max-age-days D] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

# 清除代理变量：requests 会读取 HTTP(S)_PROXY，本地 socks5 代理不可用会导致请求失败
for _proxy_var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_proxy_var, None)

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

# DexScreener chainId -> core.asset_contract.chain
CHAIN_MAP = {
    "ethereum": "ethereum",
    "bsc": "bsc",
    "polygon": "polygon",
    "arbitrum": "arbitrum",
    "base": "base",
    "optimism": "optimism",
    "avalanche": "avalanche",
    "fantom": "fantom",
    "cronos": "cronos",
}

SESSION = requests.Session()
SESSION.trust_env = False


def _norm(s: str | None) -> str:
    """规范化字符串：只保留小写字母数字，用于模糊比对。"""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _search_dexscreener(symbol: str) -> list[dict]:
    """按 symbol 搜索 DexScreener，返回 pairs 列表。"""
    url = "https://api.dexscreener.com/latest/dex/search"
    resp = SESSION.get(url, params={"q": symbol}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("pairs") or []


def _pick_contracts(pairs: list[dict], symbol: str, name: str, min_fdv: float, max_chains: int) -> list[dict]:
    """从 pairs 中挑选候选合约：规范化 symbol+name 匹配 + EVM 链 + fdv 阈值 + 每链取 fdv 最高。"""
    norm_symbol = _norm(symbol)
    norm_name = _norm(name)

    # 按 (chain, address) 聚合，每条合约记录最高 fdv（同一合约可能在多个 dex 有多个 pair）
    best_by_contract: dict[tuple[str, str], dict] = {}
    for p in pairs:
        chain_id = (p.get("chainId") or "").lower()
        if chain_id not in CHAIN_MAP:
            continue
        bt = p.get("baseToken") or {}
        if _norm(bt.get("symbol")) != norm_symbol:
            continue
        if norm_name and _norm(bt.get("name")) != norm_name:
            continue
        fdv = p.get("fdv") or 0
        if fdv < min_fdv:
            continue
        addr = (bt.get("address") or "").lower()
        if not addr:
            continue
        key = (chain_id, addr)
        prev = best_by_contract.get(key)
        if prev is None or fdv > (prev.get("fdv") or 0):
            best_by_contract[key] = {
                "chain": CHAIN_MAP[chain_id],
                "contract_address": addr,
                "fdv": fdv,
                "name": bt.get("name"),
                "symbol": bt.get("symbol"),
                "pair_created_at": p.get("pairCreatedAt"),
            }

    # 每条链取 fdv 最高的一条，按 fdv 降序，最多 max_chains 条
    best_by_chain: dict[str, dict] = {}
    for c in best_by_contract.values():
        prev = best_by_chain.get(c["chain"])
        if prev is None or c["fdv"] > prev["fdv"]:
            best_by_chain[c["chain"]] = c

    candidates = sorted(best_by_chain.values(), key=lambda x: x["fdv"], reverse=True)
    return candidates[:max_chains]


def _find_assets_without_contract(conn, limit: int, max_age_days: int) -> list[dict]:
    cur = conn.cursor()
    sql = """
        SELECT a.asset_id, a.canonical_symbol, a.canonical_name
        FROM core.asset a
        LEFT JOIN core.asset_contract c ON c.asset_id = a.asset_id
        WHERE c.contract_id IS NULL
          AND a.canonical_symbol IS NOT NULL
          AND a.canonical_symbol != ''
          AND a.launch_date >= CURRENT_DATE - make_interval(days => %s)
        ORDER BY a.launch_date DESC NULLS LAST, a.asset_id
    """
    if limit:
        sql += " LIMIT %s"
        cur.execute(sql, (max_age_days, limit))
    else:
        cur.execute(sql, (max_age_days,))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _upsert_contract(conn, asset_id: int, c: dict, is_primary: bool) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO core.asset_contract (asset_id, chain, contract_address, is_primary, source_code)
        VALUES (%s, %s, %s, %s, 'dexscreener')
        ON CONFLICT (chain, contract_address) DO UPDATE SET
            asset_id = EXCLUDED.asset_id,
            is_primary = EXCLUDED.is_primary,
            source_code = 'dexscreener',
            updated_at = NOW()
        """,
        (asset_id, c["chain"], c["contract_address"], is_primary),
    )


def _fill_launch_date(conn, asset_id: int, pair_created_at) -> bool:
    """若 core.asset.launch_date 为 NULL 且 pairCreatedAt 可用，则回填。返回是否更新。"""
    if not pair_created_at:
        return False
    try:
        from datetime import datetime, timezone
        # pairCreatedAt 可能是 ISO 字符串或毫秒时间戳
        if isinstance(pair_created_at, (int, float)):
            dt = datetime.fromtimestamp(pair_created_at / 1000, tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(pair_created_at).replace("Z", "+00:00"))
        launch = dt.date()
        cur = conn.cursor()
        cur.execute(
            "UPDATE core.asset SET launch_date = %s, updated_at = NOW() "
            "WHERE asset_id = %s AND launch_date IS NULL",
            (launch, asset_id),
        )
        return cur.rowcount > 0
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="从 DexScreener 补充无合约地址资产的合约地址")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少个资产（0=全部）")
    parser.add_argument("--min-fdv", type=float, default=500_000, help="fdv 阈值（美元），过滤同名假币")
    parser.add_argument("--max-chains", type=int, default=2, help="每个资产最多补几条链")
    parser.add_argument("--max-age-days", type=int, default=90, help="只处理 launch_date 在最近 N 天内的资产")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不写库")
    args = parser.parse_args()

    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    settings = get_settings(require_database=True)

    with get_connection(settings.database_url) as conn:
        assets = _find_assets_without_contract(conn, args.limit, args.max_age_days)

    print(f"无合约地址的资产共 {len(assets)} 个，开始从 DexScreener 补合约...", flush=True)

    filled = 0
    for idx, a in enumerate(assets, 1):
        symbol = a["canonical_symbol"]
        name = a["canonical_name"] or ""
        try:
            pairs = _search_dexscreener(symbol)
        except Exception as e:
            print(f"[{idx}/{len(assets)}] {symbol} (asset {a['asset_id']}) 搜索失败: {e}", flush=True)
            time.sleep(1)
            continue

        candidates = _pick_contracts(pairs, symbol, name, args.min_fdv, args.max_chains)
        if not candidates:
            print(f"[{idx}/{len(assets)}] {symbol} ({name}) 无匹配候选，跳过", flush=True)
        else:
            desc = ", ".join(f"{c['chain']}:{c['contract_address']} (fdv ${c['fdv']:,.0f})" for c in candidates)
            print(f"[{idx}/{len(assets)}] {symbol} ({name}) -> {desc}", flush=True)
            if not args.dry_run:
                with get_connection(settings.database_url) as conn:
                    for i, c in enumerate(candidates):
                        _upsert_contract(conn, a["asset_id"], c, is_primary=(i == 0))
                    # 补 launch_date（取 fdv 最高 pair 的 pairCreatedAt）
                    primary = candidates[0]
                    if _fill_launch_date(conn, a["asset_id"], primary.get("pair_created_at")):
                        print(f"    → launch_date 已回填 (pairCreatedAt={primary.get('pair_created_at')})", flush=True)
                    conn.commit()
                filled += 1

        time.sleep(1)  # DexScreener 免费接口限流

    if args.dry_run:
        print(f"\n[dry-run] 预览完成，候选 {filled} 个资产（未写库）。", flush=True)
    else:
        print(f"\n完成：为 {filled} 个资产补充了合约地址。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
