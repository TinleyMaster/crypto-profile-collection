#!/usr/bin/env python3
"""DEX 热搜/趋势补全：GeckoTerminal trending_pools + DexScreener token-boosts → asset_social_heat。

用法：
    python phase_dex_trending_heat.py --limit 100
    python phase_dex_trending_heat.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from contextlib import contextmanager

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(line_buffering=True)

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

import requests

# DEX 链 slug → core.asset_contract.chain 全称映射（对齐 MEME-07 同坑）
CHAIN_MAP = {
    "eth": "ethereum",
    "ethereum": "ethereum",
    "bsc": "bsc",
    "binance-smart-chain": "bsc",
    "sol": "solana",
    "solana": "solana",
    "base": "base",
    "polygon": "polygon",
    "matic": "polygon",
    "arbitrum": "arbitrum",
    "arb": "arbitrum",
    "optimism": "optimism",
    "op": "optimism",
    "avalanche": "avalanche",
    "avax": "avalanche",
    "robinhood": "robinhood",
}

UPSERT_SQL = """
INSERT INTO biz.asset_social_heat (
    asset_id, symbol, dex_trending_json, dex_boost_score, dex_source, last_dex_seen, updated_at
) VALUES (
    %(asset_id)s, %(symbol)s, %(dex_trending_json)s, %(dex_boost_score)s,
    %(dex_source)s, %(last_dex_seen)s, NOW()
)
ON CONFLICT (asset_id) DO UPDATE SET
    dex_trending_json = EXCLUDED.dex_trending_json,
    dex_boost_score   = EXCLUDED.dex_boost_score,
    dex_source        = EXCLUDED.dex_source,
    last_dex_seen     = EXCLUDED.last_dex_seen,
    updated_at        = NOW()
"""

UPSERT_KEYS = {"asset_id", "symbol", "dex_trending_json", "dex_boost_score", "dex_source", "last_dex_seen"}

TIMEOUT = 20


# ── GeckoTerminal ──────────────────────────────────────────────────────

def fetch_gecko_trending() -> list[dict]:
    """拉 GeckoTerminal trending pools，返回标准化 [{chain, token_address, pool_name, volume_24h}]."""
    url = "https://api.geckoterminal.com/api/v2/networks/trending_pools"
    try:
        r = requests.get(url, headers={"Accept": "application/json"}, timeout=TIMEOUT)
        if r.status_code != 200:
            print(f"  [GeckoTerminal] HTTP {r.status_code}", file=sys.stderr)
            return []
        data = r.json().get("data", [])
    except Exception as e:
        print(f"  [GeckoTerminal] ERROR: {e}", file=sys.stderr)
        return []

    out = []
    for item in data:
        attrs = item.get("attributes", {}) or {}
        rels = item.get("relationships", {}) or {}
        base_token = (rels.get("base_token") or {}).get("data") or {}
        chain_slug = (rels.get("network") or {}).get("data", {}).get("id", "")
        token_addr = base_token.get("id", "")  # 格式: "eth/0x..."
        parts = token_addr.split("/", 1)
        chain_raw = parts[0] if len(parts) == 2 else chain_slug
        addr = parts[1] if len(parts) == 2 else token_addr

        chain_norm = CHAIN_MAP.get(chain_raw.lower(), chain_raw.lower())
        vol = attrs.get("volume_24h") or 0
        out.append({
            "chain": chain_norm,
            "token_address": addr.lower() if chain_norm not in ("solana",) else addr,
            "pool_name": attrs.get("name", ""),
            "volume_24h": float(vol) if vol else 0,
            "source": "geckoterminal",
        })
    return out


# ── DexScreener ────────────────────────────────────────────────────────

def fetch_dexscreener_boosts() -> list[dict]:
    """拉 DexScreener token-boosts top，返回标准化 [{chain, token_address, boost_amount}]."""
    url = "https://api.dexscreener.com/token-boosts/top/v1"
    try:
        r = requests.get(url, headers={"Accept": "application/json"}, timeout=TIMEOUT)
        if r.status_code != 200:
            print(f"  [DexScreener] HTTP {r.status_code}", file=sys.stderr)
            return []
        data = r.json()
    except Exception as e:
        print(f"  [DexScreener] ERROR: {e}", file=sys.stderr)
        return []

    out = []
    for item in (data if isinstance(data, list) else []):
        chain_raw = (item.get("chainId") or "").lower()
        chain_norm = CHAIN_MAP.get(chain_raw, chain_raw)
        addr = item.get("tokenAddress") or ""
        if chain_norm in ("solana",):
            pass  # Solana 地址大小写敏感
        else:
            addr = addr.lower()
        out.append({
            "chain": chain_norm,
            "token_address": addr,
            "boost_amount": item.get("totalAmount") or 0,
            "volume_24h": 0,  # 后续 enrich 补
            "source": "dexscreener",
        })
    return out


def enrich_dexscreener_volume(entries: list[dict]) -> list[dict]:
    """对 DexScreener 命中的 token 批量拉取 volume_24h（/tokens/v1 多 token 查询）。"""
    dex_entries = [e for e in entries if e["source"] == "dexscreener"]
    if not dex_entries:
        return entries
    # DexScreener /tokens/v1/{chainId}/{tokenAddress} 逐条拉，限 10 条防限流
    for e in dex_entries[:10]:
        chain_raw = ""
        for k, v in CHAIN_MAP.items():
            if v == e["chain"]:
                chain_raw = k
                break
        if not chain_raw:
            chain_raw = e["chain"]
        url = f"https://api.dexscreener.com/tokens/v1/{chain_raw}/{e['token_address']}"
        try:
            r = requests.get(url, headers={"Accept": "application/json"}, timeout=TIMEOUT)
            if r.status_code == 200:
                pairs = r.json()
                if isinstance(pairs, list) and pairs:
                    vol = pairs[0].get("volume") or {}
                    e["volume_24h"] = float(vol.get("h24") or 0)
        except Exception:
            pass
        time.sleep(0.3)
    return entries


# ── 映射 + 聚合 ───────────────────────────────────────────────────────

def map_to_asset_id(conn, entries: list[dict]) -> dict[tuple[str, str], dict]:
    """DEX entries → {(chain, token_address): {asset_id, symbol}} 映射。"""
    if not entries:
        return {}
    # 收集所有 (chain, address) 对
    pairs = list({(e["chain"], e["token_address"]) for e in entries})
    with conn.cursor() as cur:
        results = {}
        # 分批查询（每批 200 对）
        for i in range(0, len(pairs), 200):
            batch = pairs[i:i + 200]
            conditions = []
            params = []
            for chain, addr in batch:
                conditions.append("(ac.chain = %s AND ac.contract_address = %s)")
                params.extend([chain, addr])
            cur.execute(f"""
                SELECT DISTINCT ON (ac.asset_id)
                    ac.chain, ac.contract_address, ac.asset_id, a.canonical_symbol
                FROM core.asset_contract ac
                JOIN core.asset a ON a.asset_id = ac.asset_id
                WHERE {' OR '.join(conditions)}
            """, params)
            for row in cur.fetchall():
                key = (row[0], row[1])
                results[key] = {"asset_id": row[2], "symbol": row[3]}
        return results


def aggregate_dex_signals(entries: list[dict], mapping: dict) -> list[dict]:
    """按 asset_id 聚合 DEX 信号 → UPSERT 行列表。"""
    from datetime import datetime, timezone

    # 按 asset_id 聚合
    by_asset: dict[int, dict] = {}
    for e in entries:
        key = (e["chain"], e["token_address"])
        info = mapping.get(key)
        if not info:
            continue
        aid = info["asset_id"]
        if aid not in by_asset:
            by_asset[aid] = {
                "asset_id": aid,
                "symbol": info["symbol"],
                "sources": [],
                "max_volume_24h": 0,
                "total_boost": 0,
                "trending_hits": 0,
            }
        rec = by_asset[aid]
        rec["sources"].append(e["source"])
        if e.get("volume_24h", 0) > rec["max_volume_24h"]:
            rec["max_volume_24h"] = e["volume_24h"]
        rec["total_boost"] += e.get("boost_amount", 0)
        rec["trending_hits"] += 1

    # 生成 UPSERT 行
    rows = []
    for aid, rec in by_asset.items():
        dex_json = {
            "trending_hits": rec["trending_hits"],
            "max_volume_24h": rec["max_volume_24h"],
            "total_boost": rec["total_boost"],
            "sources": list(set(rec["sources"])),
        }
        # 综合得分：GeckoTerminal 权重 0.7 + DexScreener 权重 0.3
        geo_hits = sum(1 for e in entries if e["asset_id"] == aid and e["source"] == "geckoterminal") if False else 0
        ds_hits = sum(1 for e in entries if e["asset_id"] == aid and e["source"] == "dexscreener") if False else 0
        # 简化：按命中次数加权
        score = rec["trending_hits"] * 10 + (rec["total_boost"] / 1000 if rec["total_boost"] else 0)

        rows.append({
            "asset_id": aid,
            "symbol": rec["symbol"],
            "dex_trending_json": json.dumps(dex_json, ensure_ascii=False),
            "dex_boost_score": round(score, 2),
            "dex_source": ", ".join(sorted(set(rec["sources"]))),
            "last_dex_seen": datetime.now(timezone.utc).isoformat(),
        })
    return rows


# ── Main ───────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="DEX 热搜/趋势补全（GeckoTerminal + DexScreener）")
    parser.add_argument("--limit", type=int, default=200, help="最多写入多少资产")
    parser.add_argument("--dry-run", action="store_true", help="只打印不落库")
    args = parser.parse_args()

    settings = get_settings(require_database=True)

    with get_connection(settings.database_url) as conn:
        print("拉取 GeckoTerminal trending pools...")
        geo = fetch_gecko_trending()
        print(f"  GeckoTerminal: {len(geo)} 条")

        print("拉取 DexScreener token-boosts...")
        ds = fetch_dexscreener_boosts()
        print(f"  DexScreener: {len(ds)} 条")

        print("补充 DexScreener volume_24h...")
        ds = enrich_dexscreener_volume(ds)

        all_entries = geo + ds
        if not all_entries:
            print("无 DEX 数据可处理")
            return 0

        print("映射 token → asset_id...")
        mapping = map_to_asset_id(conn, all_entries)
        print(f"  命中: {len(mapping)} 资产")

        print("聚合 DEX 信号...")
        rows = aggregate_dex_signals(all_entries, mapping)

        if not rows:
            print("聚合后无数据")
            return 0

        # 按 boost_score 降序取 top N
        rows.sort(key=lambda x: x["dex_boost_score"], reverse=True)
        rows = rows[:args.limit]

        print(f"\n待写入: {len(rows)} 资产")
        for r in rows[:10]:
            print(f"  asset_id={r['asset_id']} {r['symbol']} boost={r['dex_boost_score']} src={r['dex_source']}")
        if len(rows) > 10:
            print(f"  ... 共 {len(rows)} 条")

        if args.dry_run:
            print("\n[dry-run] 不写入数据库")
            return 0

        # UPSERT（逐行 fail-soft）
        success = 0
        fail = 0
        for i, row in enumerate(rows, 1):
            try:
                missing = UPSERT_KEYS - set(row.keys())
                if missing:
                    print(f"  ERROR 缺失占位符 {missing}", file=sys.stderr)
                    fail += 1
                    continue
                with conn.cursor() as cur:
                    cur.execute(UPSERT_SQL, row)
                conn.commit()
                success += 1
            except Exception as e:
                conn.rollback()
                print(f"  ERROR UPSERT asset_id={row['asset_id']}: {e}", file=sys.stderr)
                fail += 1
                continue

            if i % 50 == 0:
                print(f"  -- 进度 {i}/{len(rows)} --")

        print(f"\n{'=' * 60}")
        print(f"写入完成: success={success}, fail={fail}")
        print(f"{'=' * 60}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
