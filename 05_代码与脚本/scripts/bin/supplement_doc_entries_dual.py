"""
从 DexScreener + Binance 双源补充无文档入口资产的链接。

对于 core.asset 中没有任何 doc_source_entry 的资产，
同时搜索 DexScreener 和 Binance Web3 API，合并去重后写入数据库。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(line_buffering=True)

# === API 端点 ===
DEXSCREENER_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search?q={}"
BINANCE_SEARCH_URL = "https://web3.binance.com/bapi/defi/v5/public/wallet-direct/buw/wallet/market/token/search/ai"

# === 速率限制 ===
RATE_LIMIT_DELAY = 0.7  # 两个 API 轮流调用，~85 req/min 每个

# === 有效的 entry_type ===
VALID_ENTRY_TYPES = {
    "official_website", "docs", "github", "medium",
    "docs_portal", "whitepaper_page", "twitter", "telegram", "other", "reddit",
}


# ============================================================
# 工具函数
# ============================================================

def _classify_url(url: str, label: str = "") -> str:
    """根据 URL 和标签推断 entry_type。"""
    url_lower = url.lower()
    label_lower = label.lower()

    if "twitter.com" in url_lower or "x.com" in url_lower:
        return "twitter"
    if "t.me" in url_lower or "telegram" in url_lower:
        return "telegram"
    if "github.com" in url_lower:
        return "github"
    if "medium.com" in url_lower:
        return "medium"
    if "discord" in url_lower:
        return "other"
    if "reddit" in url_lower:
        return "reddit"
    if any(kw in label_lower for kw in ("docs", "documentation", "whitepaper", "wiki", "gitbook")):
        return "docs"
    if any(kw in label_lower for kw in ("website", "homepage", "official")):
        return "official_website"
    if any(kw in url_lower for kw in ("docs.", "documentation", "whitepaper", "wiki.", "gitbook")):
        return "docs"
    return "official_website"


def _match_symbol(results: list[dict], symbol: str, name: str) -> dict | None:
    """从搜索结果中匹配资产（通用匹配逻辑）。"""
    symbol_upper = symbol.strip().upper()
    name_lower = name.strip().lower()

    if not results:
        return None

    # 优先：精确 symbol 匹配
    exact_matches = [r for r in results if (r.get("symbol") or "").strip().upper() == symbol_upper]
    if exact_matches:
        for r in exact_matches:
            r_name = (r.get("name") or "").strip().lower()
            if name_lower and r_name and (name_lower == r_name or name_lower in r_name or r_name in name_lower):
                return r
        return exact_matches[0]

    # 兜底：symbol 包含关系
    for r in results:
        r_symbol = (r.get("symbol") or "").strip().upper()
        if symbol_upper in r_symbol or r_symbol in symbol_upper:
            r_name = (r.get("name") or "").strip().lower()
            if name_lower and r_name and (name_lower in r_name or r_name in name_lower):
                return r

    return None


# ============================================================
# DexScreener 数据源
# ============================================================

def search_dexscreener(query: str) -> list[dict]:
    """搜索 DexScreener，返回去重后的代币列表。"""
    url = DEXSCREENER_SEARCH_URL.format(urllib.parse.quote(query))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return []

    pairs = data.get("pairs") or []
    if not pairs:
        return []

    seen = {}
    for p in pairs:
        bt = p.get("baseToken") or {}
        addr = (bt.get("address") or "").lower()
        if not addr:
            continue
        liq = float(p.get("liquidity", {}).get("usd", 0) or 0)
        if addr not in seen or liq > seen[addr]["liquidity_usd"]:
            seen[addr] = {
                "name": bt.get("name", ""),
                "symbol": bt.get("symbol", ""),
                "address": addr,
                "chain_id": p.get("chainId", ""),
                "liquidity_usd": liq,
                "websites": p.get("info", {}).get("websites") or [],
                "socials": p.get("info", {}).get("socials") or [],
            }

    return sorted(seen.values(), key=lambda x: x["liquidity_usd"], reverse=True)


def extract_links_from_dexscreener(matched: dict) -> list[dict]:
    """从 DexScreener 匹配结果中提取链接。"""
    links = []
    for w in matched.get("websites") or []:
        url = (w.get("url") or "").strip()
        if not url or not url.startswith("http"):
            continue
        links.append({
            "entry_type": _classify_url(url, w.get("label", "")),
            "entry_url": url,
            "discovered_from": "dexscreener.website",
        })
    for s in matched.get("socials") or []:
        url = (s.get("url") or "").strip()
        if not url or not url.startswith("http"):
            continue
        t = (s.get("type") or "").lower()
        entry_type = {"twitter": "twitter", "telegram": "telegram", "reddit": "reddit"}.get(t, "other")
        links.append({
            "entry_type": entry_type,
            "entry_url": url,
            "discovered_from": f"dexscreener.social.{t}",
        })
    return links


# ============================================================
# Binance 数据源
# ============================================================

def search_binance(query: str) -> list[dict]:
    """搜索 Binance Web3，返回代币列表。"""
    params = {"keyword": query}
    try:
        url = BINANCE_SEARCH_URL + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "binance-web3/2.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return []

    if not data.get("success") and data.get("code") != "000000":
        return []
    return data.get("data") or []


def extract_links_from_binance(token: dict) -> list[dict]:
    """从 Binance 搜索结果中提取链接。"""
    links = []
    for link in (token.get("links") or []):
        url = (link.get("link") or "").strip()
        if not url or not url.startswith("http"):
            continue
        label = (link.get("label") or "").lower()
        entry_type = _classify_url(url, label)
        links.append({
            "entry_type": entry_type,
            "entry_url": url,
            "discovered_from": f"binance.{label}",
        })
    return links


# ============================================================
# 主逻辑
# ============================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从 DexScreener + Binance 双源补充无文档入口资产的链接。"
    )
    parser.add_argument("--dry-run", action="store_true", help="预览不写入。")
    parser.add_argument("--limit", type=int, default=50, help="最大处理资产数。")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    import psycopg

    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection
    from crypto_research.db.upsert import fetch_one, load_sql
    from crypto_research.mapping.classify_link import classify_entry_fields

    settings = get_settings(require_database=True)
    select_candidates_sql = load_sql("src_dexscreener/select_dexscreener_candidates.sql")
    upsert_entry_sql = load_sql("biz/upsert_doc_source_entry.sql")

    with get_connection(settings.database_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(select_candidates_sql, (args.limit,))
            candidates = [dict(row) for row in cur.fetchall()]

    if not candidates:
        print("无候选资产，全部完成。")
        return 0

    print(f"候选资产: {len(candidates)}")

    total_matched = 0
    total_entries = 0
    total_skipped = 0
    ds_matched = 0
    bn_matched = 0

    entries: list[dict] = []

    for i, asset in enumerate(candidates):
        symbol = asset["canonical_symbol"]
        name = asset["canonical_name"]
        asset_id = asset["asset_id"]

        if i > 0:
            time.sleep(RATE_LIMIT_DELAY)

        print(f"[{i+1}/{len(candidates)}] {symbol} ({name})...", end=" ")

        # 两个数据源同时搜索
        ds_results = search_dexscreener(symbol)
        bn_results = search_binance(symbol)

        if not ds_results and not bn_results:
            print("双源均无结果")
            total_skipped += 1
            continue

        # 匹配
        ds_matched_token = _match_symbol(ds_results, symbol, name)
        bn_matched_token = _match_symbol(bn_results, symbol, name)

        if not ds_matched_token and not bn_matched_token:
            print(f"无匹配 (DS={len(ds_results)} BN={len(bn_results)})")
            total_skipped += 1
            continue

        # 提取并合并链接（按 URL 去重）
        all_links = []
        seen_urls = set()

        if ds_matched_token:
            for link in extract_links_from_dexscreener(ds_matched_token):
                if link["entry_url"] not in seen_urls:
                    seen_urls.add(link["entry_url"])
                    all_links.append(link)
            ds_matched += 1

        if bn_matched_token:
            for link in extract_links_from_binance(bn_matched_token):
                if link["entry_url"] not in seen_urls:
                    seen_urls.add(link["entry_url"])
                    all_links.append(link)
            bn_matched += 1

        if not all_links:
            print("匹配成功但无可用链接")
            total_skipped += 1
            continue

        for link in all_links:
            entry_type = link["entry_type"]
            if entry_type not in VALID_ENTRY_TYPES:
                entry_type = "other"
            source_code = "dexscreener" if link["discovered_from"].startswith("dexscreener") else "binance"
            topics, method, confidence = classify_entry_fields(link["entry_url"], source_code=source_code)
            entries.append({
                "entity_type": "asset",
                "asset_id": asset_id,
                "protocol_id": None,
                "source_code": source_code,
                "entry_type": entry_type,
                "entry_url": link["entry_url"],
                "discovered_from": link["discovered_from"],
                "is_primary": entry_type == "official_website",
                "content_topics": topics,
                "classify_method": method,
                "classify_confidence": confidence,
            })

        total_matched += 1
        total_entries += len(all_links)
        flags = []
        if ds_matched_token:
            flags.append("DS")
        if bn_matched_token:
            flags.append("BN")
        print(f"OK {'+'.join(flags)} +{len(all_links)} 链接")

    # 写入数据库
    if args.dry_run:
        result = {
            "mode": "dry-run",
            "candidates": len(candidates),
            "matched": total_matched,
            "entries": total_entries,
            "ds_matched": ds_matched,
            "bn_matched": bn_matched,
            "skipped": total_skipped,
            "first_entry": entries[0] if entries else None,
        }
        print(json.dumps(result, ensure_ascii=False))
        return 0

    with get_connection(settings.database_url) as conn:
        written = 0
        for entry in entries:
            fetch_one(conn, upsert_entry_sql,
                (entry["entity_type"], entry["asset_id"], entry["protocol_id"],
                 entry["source_code"], entry["entry_type"], entry["entry_url"],
                 entry["discovered_from"], entry["is_primary"],
                 entry["content_topics"], entry["classify_method"], entry["classify_confidence"]),
            )
            written += 1
        conn.commit()

    result = {
        "status": "complete",
        "candidates": len(candidates),
        "matched": total_matched,
        "entries": total_entries,
        "written": written,
        "ds_matched": ds_matched,
        "bn_matched": bn_matched,
        "skipped": total_skipped,
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())