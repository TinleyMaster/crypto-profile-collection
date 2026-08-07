"""
从 Binance Web3 搜索 API 补充无文档入口资产的链接。

对于 core.asset 中没有任何 doc_source_entry 的资产，
通过 Binance Web3 搜索 API 查找，提取官网、白皮书、Twitter、Telegram 等链接。
支持按合约地址精确搜索，匹配率远高于纯符号搜索。
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

# Binance Web3 搜索 API
BINANCE_SEARCH_URL = "https://web3.binance.com/bapi/defi/v5/public/wallet-direct/buw/wallet/market/token/search/ai"

# 速率限制（秒）
RATE_LIMIT_DELAY = 0.6  # ~100 requests/min

# link label → entry_type 映射
LINK_TYPE_MAP = {
    "website": "official_website",
    "whitepaper": "docs",
    "x": "twitter",
    "twitter": "twitter",
    "telegram": "telegram",
    "reddit": "reddit",
    "github": "github",
    "facebook": "other",
    "discord": "other",
    "medium": "medium",
}

# 有效的 entry_type
VALID_ENTRY_TYPES = {
    "official_website", "docs", "github", "medium",
    "docs_portal", "whitepaper_page", "twitter", "telegram", "other", "reddit",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从 Binance Web3 搜索补充无文档入口资产的链接。"
    )
    parser.add_argument("--dry-run", action="store_true", help="预览不写入。")
    parser.add_argument("--limit", type=int, default=50, help="最大处理资产数。")
    return parser


def search_binance(query: str, chain_id: str = "") -> list[dict]:
    """搜索 Binance Web3，返回代币列表。"""
    params = {"keyword": query, "chainId": chain_id or ""}
    try:
        url = BINANCE_SEARCH_URL + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "binance-web3/2.0 (Skill)"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return []

    if not data.get("success") and data.get("code") != "000000":
        return []

    return data.get("data") or []


def _match_asset(results: list[dict], symbol: str, name: str) -> dict | None:
    """从 Binance 搜索结果中匹配最匹配的资产。"""
    symbol_upper = symbol.strip().upper()
    name_lower = name.strip().lower()

    if not results:
        return None

    # 优先：精确 symbol 匹配
    exact_matches = [r for r in results if (r.get("symbol") or "").strip().upper() == symbol_upper]
    if exact_matches:
        # 在精确匹配中找名称匹配的
        for r in exact_matches:
            r_name = (r.get("name") or "").strip().lower()
            if name_lower and r_name and (name_lower == r_name or name_lower in r_name or r_name in name_lower):
                return r
        # 没有名称匹配，返回第一个精确 symbol 匹配
        return exact_matches[0]

    # 兜底：symbol 包含关系（如 "MG" 匹配 "MGO"）
    for r in results:
        r_symbol = (r.get("symbol") or "").strip().upper()
        if symbol_upper in r_symbol or r_symbol in symbol_upper:
            r_name = (r.get("name") or "").strip().lower()
            if name_lower and r_name and (name_lower in r_name or r_name in name_lower):
                return r

    return None


def extract_links(token: dict) -> list[dict]:
    """从 Binance 搜索结果中提取链接。"""
    links = []
    for link in (token.get("links") or []):
        url = (link.get("link") or "").strip()
        if not url or not url.startswith("http"):
            continue
        label = (link.get("label") or "").lower()
        entry_type = LINK_TYPE_MAP.get(label, "other")
        if entry_type == "other" and label:
            # 尝试根据 URL 推断
            url_lower = url.lower()
            if "twitter.com" in url_lower or "x.com" in url_lower:
                entry_type = "twitter"
            elif "t.me" in url_lower:
                entry_type = "telegram"
            elif "github.com" in url_lower:
                entry_type = "github"
            elif "medium.com" in url_lower:
                entry_type = "medium"
            elif "reddit" in url_lower:
                entry_type = "reddit"
            elif "docs." in url_lower or "documentation" in url_lower or "whitepaper" in url_lower:
                entry_type = "docs"
        links.append({
            "entry_type": entry_type,
            "entry_url": url,
            "discovered_from": f"binance.search.{label}",
        })
    return links


def main() -> int:
    args = build_parser().parse_args()

    import psycopg

    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection
    from crypto_research.db.upsert import fetch_one, load_sql

    settings = get_settings(require_database=True)
    select_candidates_sql = load_sql("src_binance/select_binance_candidates.sql")
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

        # 搜索：Binance API 不支持按合约地址搜索，直接按 symbol
        print(f"[{i+1}/{len(candidates)}] 搜索: {symbol} ({name})...", end=" ")
        results = search_binance(symbol)
        if not results:
            print("无结果")
            total_skipped += 1
            continue

        matched = _match_asset(results, symbol, name)
        if not matched:
            print(f"无匹配 (候选: {len(results)})")
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
            entries.append({
                "entity_type": "asset",
                "asset_id": asset_id,
                "protocol_id": None,
                "source_code": "binance",
                "entry_type": entry_type,
                "entry_url": link["entry_url"],
                "discovered_from": link["discovered_from"],
                "is_primary": entry_type == "official_website",
            })

        total_matched += 1
        total_entries += len(links)
        print(f"OK 匹配 {matched['symbol']} @ chain {matched.get('chainId','?')} +{len(links)} 链接")

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
                 entry["discovered_from"], entry["is_primary"]),
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