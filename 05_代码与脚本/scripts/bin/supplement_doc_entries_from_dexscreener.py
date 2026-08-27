"""
从 DexScreener 补充无文档入口资产的链接。

对于 core.asset 中没有任何 doc_source_entry 的资产，
通过 DexScreener API 搜索，提取官网、Twitter、Telegram 等链接。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))


# 行缓冲：确保 print 实时输出
sys.stdout.reconfigure(line_buffering=True)

# DexScreener 搜索 API
DEXSCREENER_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search?q={}"

# 速率限制：每批搜索之间的间隔（秒）
RATE_LIMIT_DELAY = 1.2  # ~50 requests/min，保守

# 有效的 entry_type
VALID_ENTRY_TYPES = {
    "official_website", "docs", "github", "medium",
    "docs_portal", "whitepaper_page", "twitter", "telegram", "other",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从 DexScreener 补充无文档入口资产的链接。"
    )
    parser.add_argument("--dry-run", action="store_true", help="预览不写入。")
    parser.add_argument("--limit", type=int, default=50, help="最大处理资产数。")
    return parser


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
            info = p.get("info") or {}
            websites = []
            socials = []
            for w in info.get("websites") or []:
                if w.get("url"):
                    websites.append({"label": w.get("label", ""), "url": w["url"]})
            for s in info.get("socials") or []:
                if s.get("url"):
                    socials.append({"type": s.get("type", ""), "url": s["url"]})

            seen[addr] = {
                "name": bt.get("name", ""),
                "symbol": bt.get("symbol", ""),
                "address": addr,
                "chain_id": p.get("chainId", ""),
                "liquidity_usd": liq,
                "websites": websites,
                "socials": socials,
            }

    return sorted(seen.values(), key=lambda x: x["liquidity_usd"], reverse=True)


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


def _match_asset(results: list[dict], symbol: str, name: str) -> dict | None:
    """从 DexScreener 搜索结果中匹配最匹配的资产。"""
    symbol_upper = symbol.strip().upper()
    name_lower = name.strip().lower()

    for r in results:
        r_symbol = (r.get("symbol") or "").strip().upper()
        r_name = (r.get("name") or "").strip().lower()

        # 符号必须完全匹配（大小写不敏感）
        if r_symbol != symbol_upper:
            continue

        # 名称相似度检查：避免同名不同币的误匹配
        # 如果名字也是完全匹配或包含关系，返回
        if name_lower == r_name or name_lower in r_name or r_name in name_lower:
            return r

        # 如果名字不匹配但符号精确匹配，也接受（有些 DexScreener 名字带后缀）
        return r

    return None


def extract_links(matched: dict) -> list[dict]:
    """从匹配的 DexScreener 结果中提取链接。"""
    links = []

    # 官网链接
    for w in matched.get("websites") or []:
        url = (w.get("url") or "").strip()
        if not url or not url.startswith("http"):
            continue
        entry_type = _classify_url(url, w.get("label", ""))
        links.append({
            "entry_type": entry_type,
            "entry_url": url,
            "discovered_from": "dexscreener.website",
        })

    # 社交链接
    for s in matched.get("socials") or []:
        url = (s.get("url") or "").strip()
        if not url or not url.startswith("http"):
            continue
        social_type = (s.get("type") or "").lower()
        if social_type == "twitter":
            entry_type = "twitter"
        elif social_type == "telegram":
            entry_type = "telegram"
        elif social_type == "discord":
            entry_type = "other"
        elif social_type == "reddit":
            entry_type = "reddit"
        else:
            entry_type = "other"
        links.append({
            "entry_type": entry_type,
            "entry_url": url,
            "discovered_from": f"dexscreener.social.{social_type}",
        })

    return links


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

    # 1. 查询候选资产
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

    entries: list[dict] = []

    for i, asset in enumerate(candidates):
        symbol = asset["canonical_symbol"]
        name = asset["canonical_name"]
        asset_id = asset["asset_id"]

        # 速率限制
        if i > 0:
            time.sleep(RATE_LIMIT_DELAY)

        print(f"[{i+1}/{len(candidates)}] 搜索: {symbol} ({name})...", end=" ")

        results = search_dexscreener(symbol)
        if not results:
            print("无结果")
            total_skipped += 1
            continue

        matched = _match_asset(results, symbol, name)
        if not matched:
            print(f"符号匹配但名称不匹配 (候选: {len(results)})")
            total_skipped += 1
            continue

        links = extract_links(matched)
        if not links:
            print("匹配成功但无可用链接")
            total_skipped += 1
            continue

        for link in links:
            entry_type = link["entry_type"]
            if entry_type not in VALID_ENTRY_TYPES:
                entry_type = "other"
            topics, method, confidence = classify_entry_fields(link["entry_url"], source_code="dexscreener")
            entries.append({
                "entity_type": "asset",
                "asset_id": asset_id,
                "protocol_id": None,
                "source_code": "dexscreener",
                "entry_type": entry_type,
                "entry_url": link["entry_url"],
                "discovered_from": link["discovered_from"],
                "is_primary": False,  # 统一由裁决脚本设置，避免多来源各标各的
                "content_topics": topics,
                "classify_method": method,
                "classify_confidence": confidence,
            })

        total_matched += 1
        total_entries += len(links)
        print(f"OK 匹配 {matched['symbol']} @ {matched['chain_id']} +{len(links)} 链接")

    # 2. 写入数据库
    if args.dry_run:
        result = {
            "mode": "dry-run",
            "candidates": len(candidates),
            "matched": total_matched,
            "entries": total_entries,
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
        "skipped": total_skipped,
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())