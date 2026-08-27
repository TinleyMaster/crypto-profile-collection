"""
Phase B2 third_party: 第三方专项链接补齐（审计 / 评级）。

对已映射 DefiLlama 的资产，调用 /protocol/{slug} 详情接口，提取：
- audit_links      审计公司审计报告链接（content_topics 含 audit）
- DefiLlama 协议页  第三方评级页（content_topics 含 third_party_rating）

写入 biz.doc_source_entry，作为投研缺失清单中「审计 / 第三方评级」主题的来源。

说明：
- TGE/IDO 与链上异常事件缺少可稳定映射到单资产的 URL 来源，暂不在此阶段落地。
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

DEFILLAMA_PROTOCOL_URL = "https://api.llama.fi/protocol/{}"
RATING_PAGE_URL = "https://defillama.com/protocol/{}"

# 速率限制（秒），DefiLlama 免费接口较宽松，但仍保持保守节流
RATE_LIMIT_DELAY = 0.3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="补齐第三方专项链接（审计/评级）。")
    parser.add_argument("--dry-run", action="store_true", help="预览不写入。")
    parser.add_argument("--limit", type=int, default=50, help="批量模式最大处理资产数。")
    parser.add_argument("--asset-id", type=int, default=None, help="仅处理指定资产ID。")
    parser.add_argument("--timeout", type=int, default=10, help="HTTP 读取超时(秒)。")
    return parser


def _fetch_protocol(slug: str, timeout: int) -> dict | None:
    """拉取 DefiLlama 协议详情，失败返回 None。"""
    url = DEFILLAMA_PROTOCOL_URL.format(urllib.parse.quote(slug))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "crypto-research/1.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, OSError, ValueError):
        return None


def _clean_url(raw) -> str | None:
    if not isinstance(raw, str):
        return None
    url = raw.strip()
    if not url.startswith("http") or len(url) > 500:
        return None
    return url


def _entry(asset_id: int, entry_type: str, entry_url: str, discovered_from: str,
           topics: list[str], method: str, confidence: float) -> dict:
    return {
        "entity_type": "asset",
        "asset_id": asset_id,
        "protocol_id": None,
        "source_code": "dl",
        "entry_type": entry_type,
        "entry_url": entry_url,
        "discovered_from": discovered_from,
        "is_primary": False,
        "content_topics": topics,
        "classify_method": method,
        "classify_confidence": confidence,
    }


def extract_entries(asset_id: int, slug: str, protocol: dict) -> list[dict]:
    """从协议详情提取审计 + 评级入口，返回待写入条目。

    audit_links / 协议页均来自 DefiLlama 结构化字段，主题明确，
    直接落结构化主题（method=url_key，confidence=0.9），
    不走关键词分类器，避免「lido 含 ido」这类误判。
    """
    entries: list[dict] = []
    seen: set[str] = set()

    # 1) 审计报告链接
    for raw in (protocol.get("audit_links") or []):
        url = _clean_url(raw)
        if not url or url in seen:
            continue
        seen.add(url)
        entries.append(_entry(asset_id, "docs", url, "dl_protocol.audit_links", ["audit"], "url_key", 0.9))

    # 2) DefiLlama 协议页（第三方评级）
    rating_url = RATING_PAGE_URL.format(urllib.parse.quote(slug))
    entries.append(_entry(asset_id, "other", rating_url, "dl_protocol.rating", ["third_party_rating"], "url_key", 0.9))

    return entries


def main() -> int:
    args = build_parser().parse_args()

    import psycopg

    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection
    from crypto_research.db.upsert import fetch_one, load_sql

    settings = get_settings(require_database=True)
    upsert_entry_sql = load_sql("biz/upsert_doc_source_entry.sql")

    # 1. 查询待处理资产（单资产或批量）
    with get_connection(settings.database_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            if args.asset_id is not None:
                cur.execute(
                    """
                    SELECT asm.asset_id, p.protocol_id, p.slug, p.name
                    FROM core.asset_source_map AS asm
                    INNER JOIN src_dl.protocol_list AS p ON p.protocol_id = asm.source_asset_key
                    WHERE asm.source_code = 'dl'
                      AND asm.asset_id = %s
                      AND p.slug IS NOT NULL AND TRIM(p.slug) != ''
                    ORDER BY p.protocol_id
                    LIMIT 1
                    """,
                    (args.asset_id,),
                )
            else:
                cur.execute(load_sql("src_dl/select_dl_third_party_candidates.sql"), (args.limit,))
            candidates = [dict(row) for row in cur.fetchall()]

    if not candidates:
        print(json.dumps({"status": "no_candidates"}, ensure_ascii=False))
        return 0

    print(f"待处理资产: {len(candidates)}")

    written = 0
    matched = 0
    skipped = 0
    preview_entries: list[dict] = []

    # 拉取 + 逐资产写入提交：增量提交，任意中断都能断点续跑（NOT EXISTS 标记）
    with get_connection(settings.database_url) as conn:
        for i, asset in enumerate(candidates):
            slug = asset["slug"]
            if i > 0:
                time.sleep(RATE_LIMIT_DELAY)

            protocol = _fetch_protocol(slug, args.timeout)
            if protocol is None:
                skipped += 1
                print(f"[{i + 1}/{len(candidates)}] {slug} 拉取失败", flush=True)
                continue

            found = extract_entries(asset["asset_id"], slug, protocol)
            if not found:
                skipped += 1
                continue

            if args.dry_run:
                preview_entries.extend(found)
            else:
                for entry in found:
                    fetch_one(conn, upsert_entry_sql,
                        (entry["entity_type"], entry["asset_id"], entry["protocol_id"],
                         entry["source_code"], entry["entry_type"], entry["entry_url"],
                         entry["discovered_from"], entry["is_primary"],
                         entry["content_topics"], entry["classify_method"], entry["classify_confidence"]))
                    written += 1
                conn.commit()

            matched += 1
            print(f"[{i + 1}/{len(candidates)}] {slug} +{len(found)} 条第三方链接", flush=True)

    if args.dry_run:
        result = {
            "mode": "dry-run",
            "candidates": len(candidates),
            "matched": matched,
            "entries": len(preview_entries),
            "skipped": skipped,
            "first_entry": preview_entries[0] if preview_entries else None,
        }
    else:
        result = {
            "status": "complete",
            "candidates": len(candidates),
            "matched": matched,
            "entries": written,
            "written": written,
            "skipped": skipped,
        }
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
