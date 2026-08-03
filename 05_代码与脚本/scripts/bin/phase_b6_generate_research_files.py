"""
Phase B6: 生成投研资料文件

为每个币生成两个核心文件：
  1. {symbol}_投研网址链接.txt  - 入选的投研网址链接（一行一个，带注释）
  2. {symbol}_基础数据.md       - CMC + DeFiLlama 聚合基础数据

输出路径: docs_storage/{symbol}_{asset_id}/

使用方式:
  先运行 B5（链接健康检查+AI筛选），再运行 B6 生成文件。
"""
from __future__ import annotations

import argparse
import json
import sys
import io
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

STORAGE_ROOT = Path(__file__).resolve().parents[3] / "docs_storage"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Phase B6: 生成投研资料文件")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=100, help="最大处理币种数")
    p.add_argument("--asset-id", type=int, default=0, help="只处理指定 asset_id")
    p.add_argument("--symbol", type=str, default="", help="只处理指定 symbol")
    p.add_argument("--storage-root", type=str, default=str(STORAGE_ROOT))
    p.add_argument("--max-urls", type=int, default=40, help="每个币最多入选链接数")
    p.add_argument("--output-format", type=str, default="both",
                   choices=["both", "txt_only", "md_only"], help="输出文件类型")
    return p


def sanitize_name(name: str) -> str:
    """清理非法文件名字符"""
    import re
    return re.sub(r'[\\/:*?"<>|]', '_', name)


def get_asset_meta(conn, asset_id: int) -> dict | None:
    """获取 coin_basic 基础元数据"""
    import psycopg

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            SELECT asset_id, cmc_id, defillama_slug,
                   coin_symbol, coin_name, asset_type,
                   main_chain, primary_contract_address,
                   official_website, description_short, logo_url,
                   mapping_status, last_refreshed_at
            FROM biz.coin_basic
            WHERE asset_id = %s
        """, (asset_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def get_cmc_info(conn, cmc_id: int) -> dict | None:
    """获取 CMC 详细信息"""
    import psycopg

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            SELECT cmc_id, description, logo, notice, date_launched,
                   tags, urls, platform_json, category_hint
            FROM src_cmc.cmc_asset_info
            WHERE cmc_id = %s
        """, (cmc_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def get_dl_info(conn, defillama_slug: str, cmc_id: int = 0) -> dict | None:
    """获取 DeFiLlama 协议信息"""
    import psycopg

    queries = []
    params_list = []

    if defillama_slug:
        queries.append("slug = %s")
        params_list.append(defillama_slug)
    if cmc_id:
        queries.append("cmc_id = %s")
        params_list.append(str(cmc_id))

    if not queries:
        return None

    where = " OR ".join(queries)
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(f"""
            SELECT protocol_id, name, symbol, slug, category,
                   chain, chains, tvl,
                   change_1h, change_1d, change_7d,
                   url, description, twitter, address
            FROM src_dl.protocol_list
            WHERE {where}
            ORDER BY tvl DESC NULLS LAST
            LIMIT 1
        """, tuple(params_list))
        row = cur.fetchone()
        return dict(row) if row else None


def get_selected_urls(conn, asset_id: int, max_urls: int = 40) -> list[dict]:
    """获取该 asset 的入选投研链接"""
    import psycopg

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        # 优先使用 B5 的表（如果有数据），fallback 到直接查询 doc_asset + doc_source_entry
        cur.execute("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'biz' AND table_name = 'research_url'
            )
        """)
        has_research_url = cur.fetchone()["exists"]

        if has_research_url:
            cur.execute("""
                SELECT url, category, relevance_score, ai_reason,
                       health_status, doc_type, file_name
                FROM biz.research_url
                WHERE asset_id = %s AND is_selected = TRUE
                ORDER BY relevance_score DESC, category
                LIMIT %s
            """, (asset_id, max_urls))
            rows = cur.fetchall()
            if rows:
                return [dict(r) for r in rows]

        # Fallback: 直接从 doc_asset 和 doc_source_entry 查询
        urls = []
        seen = set()

        # doc_asset 中的文档
        cur.execute("""
            SELECT da.source_url AS url, da.doc_type AS category,
                   da.file_name, da.mime_type,
                   0.8 AS relevance_score, '' AS ai_reason,
                   'unchecked' AS health_status
            FROM biz.doc_asset da
            WHERE da.asset_id = %s AND da.source_url IS NOT NULL
              AND da.parse_status != 'dead'
            ORDER BY da.last_seen_at DESC NULLS LAST
            LIMIT %s
        """, (asset_id, max_urls))
        for row in cur.fetchall():
            r = dict(row)
            url_key = (r["url"] or "").strip()
            if url_key and url_key not in seen:
                seen.add(url_key)
                urls.append(r)

        # doc_source_entry 的官网/文档入口
        remaining = max_urls - len(urls)
        if remaining > 0:
            cur.execute("""
                SELECT dse.entry_url AS url, dse.entry_type AS category,
                       NULL AS file_name, NULL AS mime_type,
                       0.5 AS relevance_score, '' AS ai_reason,
                       'unchecked' AS health_status
                FROM biz.doc_source_entry dse
                WHERE dse.asset_id = %s
                  AND dse.entry_type IN ('docs', 'official_website', 'github')
                  AND dse.entry_url IS NOT NULL
                ORDER BY dse.is_primary DESC, dse.updated_at DESC
                LIMIT %s
            """, (asset_id, remaining))
            for row in cur.fetchall():
                r = dict(row)
                url_key = (r["url"] or "").strip()
                if url_key and url_key not in seen:
                    seen.add(url_key)
                    urls.append(r)

        return urls


def generate_urls_txt(urls: list[dict], symbol: str) -> str:
    """生成投研网址链接.txt 内容"""
    lines = [
        f"# {symbol} 投研网址链接",
        f"# 共 {len(urls)} 个链接，可直接导入 NotebookLM / IMA / 乐享知识库",
        f"# 生成时间: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"",
    ]

    for u in urls:
        url = u.get("url", "")
        category = u.get("category", "other")
        score = u.get("relevance_score", 0)
        reason = u.get("ai_reason", "")
        file_name = u.get("file_name", "")
        health = u.get("health_status", "")

        # 构建注释
        tags = []
        if category:
            tags.append(category)
        if health and health != "healthy":
            tags.append(f"⚠{health}")
        if file_name:
            tags.append(f"📄{file_name}")

        comment = f"  # {' | '.join(tags)}" if tags else ""
        if reason:
            comment += f" [{reason}]"

        lines.append(f"{url}{comment}")

    return "\n".join(lines) + "\n"


def generate_base_md(meta: dict, cmc_info: dict | None, dl_info: dict | None) -> str:
    """生成基础数据.md 内容"""
    symbol = meta.get("coin_symbol", "?")
    name = meta.get("coin_name", "?")

    lines = [
        f"# {name} ({symbol}) 基础投研数据",
        f"",
        f"> 数据来源: CoinMarketCap API + DeFiLlama API",
        f"> 生成时间: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"",
        f"## 基本信息",
        f"",
        f"| 属性 | 值 |",
        f"|------|-----|",
        f"| 名称 | {name} |",
        f"| 代币符号 | {symbol} |",
        f"| 资产类型 | {meta.get('asset_type', '-')} |",
    ]

    if meta.get("main_chain"):
        lines.append(f"| 主链 | {meta['main_chain']} |")
    if meta.get("primary_contract_address"):
        lines.append(f"| 合约地址 | `{meta['primary_contract_address']}` |")
    if meta.get("official_website"):
        lines.append(f"| 官网 | {meta['official_website']} |")

    lines.append("")

    # CMC 数据
    if cmc_info:
        lines.append("## CMC 数据")
        lines.append("")

        if cmc_info.get("date_launched"):
            lines.append(f"- **上线日期**: {cmc_info['date_launched']}")
        if cmc_info.get("category_hint"):
            lines.append(f"- **分类**: {cmc_info['category_hint']}")

        tags = cmc_info.get("tags")
        if tags and isinstance(tags, list) and len(tags) > 0:
            tags_str = ", ".join(tags[:10])
            lines.append(f"- **标签**: {tags_str}")

        if cmc_info.get("description"):
            desc = cmc_info["description"]
            # 截断过长描述
            if len(desc) > 1500:
                desc = desc[:1500] + "..."
            lines.append(f"")
            lines.append(f"### 项目简介")
            lines.append(f"")
            lines.append(desc)

        lines.append("")

    # DeFiLlama 数据
    if dl_info:
        lines.append("## DeFiLlama 数据")
        lines.append("")

        if dl_info.get("tvl") is not None:
            tvl = dl_info["tvl"]
            if tvl >= 1_000_000_000:
                tvl_str = f"${tvl/1_000_000_000:.2f}B"
            elif tvl >= 1_000_000:
                tvl_str = f"${tvl/1_000_000:.2f}M"
            else:
                tvl_str = f"${tvl:,.2f}"
            lines.append(f"- **TVL**: {tvl_str}")

        changes = []
        if dl_info.get("change_1h") is not None:
            changes.append(f"1h: {dl_info['change_1h']:+.2f}%")
        if dl_info.get("change_1d") is not None:
            changes.append(f"24h: {dl_info['change_1d']:+.2f}%")
        if dl_info.get("change_7d") is not None:
            changes.append(f"7d: {dl_info['change_7d']:+.2f}%")
        if changes:
            lines.append(f"- **TVL 变化**: {' | '.join(changes)}")

        if dl_info.get("category"):
            lines.append(f"- **赛道**: {dl_info['category']}")

        chains = dl_info.get("chains")
        if chains and isinstance(chains, list) and len(chains) > 0:
            chains_str = ", ".join(chains[:10])
            lines.append(f"- **所属链**: {chains_str}")
        elif dl_info.get("chain"):
            lines.append(f"- **链**: {dl_info['chain']}")

        if dl_info.get("description"):
            desc = dl_info["description"]
            if len(desc) > 1000:
                desc = desc[:1000] + "..."
            lines.append(f"")
            lines.append(f"### 协议简介")
            lines.append(f"")
            lines.append(desc)

        lines.append("")

    # 官方链接汇总
    lines.append("## 官方链接")
    lines.append("")

    if meta.get("official_website"):
        lines.append(f"- 官网: {meta['official_website']}")

    if cmc_info:
        urls = cmc_info.get("urls") or {}
        if isinstance(urls, dict):
            for key, val in urls.items():
                if isinstance(val, list):
                    for v in val[:2]:
                        if isinstance(v, str) and v.strip():
                            lines.append(f"- {key}: {v}")
                elif isinstance(val, str) and val.strip():
                    lines.append(f"- {key}: {val}")

    if dl_info:
        if dl_info.get("url"):
            lines.append(f"- DeFiLlama: {dl_info['url']}")
        if dl_info.get("twitter"):
            lines.append(f"- Twitter: https://x.com/{dl_info['twitter'].lstrip('@')}")

    return "\n".join(lines) + "\n"


def process_one_asset(conn, asset_id: int, storage_root: Path, output_format: str, max_urls: int) -> dict:
    """处理单个币种"""
    meta = get_asset_meta(conn, asset_id)
    if not meta:
        return {"asset_id": asset_id, "status": "no_meta"}

    symbol = meta["coin_symbol"]
    name = meta.get("coin_name", symbol)
    safe_symbol = sanitize_name(symbol)
    safe_name = sanitize_name(name)
    dir_name = f"{safe_symbol}_{asset_id}"
    asset_dir = storage_root / dir_name
    asset_dir.mkdir(parents=True, exist_ok=True)

    # 获取 CMC 和 DL 信息
    cmc_info = None
    if meta.get("cmc_id"):
        cmc_info = get_cmc_info(conn, meta["cmc_id"])

    dl_info = None
    if meta.get("defillama_slug") or meta.get("cmc_id"):
        dl_info = get_dl_info(conn, meta.get("defillama_slug"), meta.get("cmc_id") or 0)

    result = {
        "asset_id": asset_id,
        "symbol": symbol,
        "dir": str(asset_dir),
        "files": [],
    }

    # 生成投研网址链接.txt
    if output_format in ("both", "txt_only"):
        urls = get_selected_urls(conn, asset_id, max_urls)
        if urls:
            txt_path = asset_dir / f"{safe_symbol}_投研网址链接.txt"
            txt_path.write_text(generate_urls_txt(urls, symbol), encoding="utf-8")
            result["files"].append({"type": "txt", "path": str(txt_path), "url_count": len(urls)})

    # 生成基础数据.md
    if output_format in ("both", "md_only"):
        md_path = asset_dir / f"{safe_symbol}_基础数据.md"
        md_path.write_text(generate_base_md(meta, cmc_info, dl_info), encoding="utf-8")
        has_cmc = cmc_info is not None
        has_dl = dl_info is not None
        result["files"].append({
            "type": "md",
            "path": str(md_path),
            "has_cmc": has_cmc,
            "has_dl": has_dl,
        })

    result["status"] = "ok" if result["files"] else "no_urls"
    return result


def main() -> int:
    args = build_parser().parse_args()

    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    settings = get_settings(require_database=True)
    storage_root = Path(args.storage_root)
    storage_root.mkdir(parents=True, exist_ok=True)

    with get_connection(settings.database_url) as conn:
        # 查询要处理的 asset
        import psycopg

        if args.asset_id:
            asset_ids = [args.asset_id]
        elif args.symbol:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT asset_id FROM biz.coin_basic WHERE LOWER(coin_symbol) = LOWER(%s)",
                    (args.symbol,),
                )
                rows = cur.fetchall()
                asset_ids = [r[0] for r in rows]
        else:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT asset_id
                    FROM biz.coin_basic
                    WHERE mapping_status = 'active'
                    ORDER BY cmc_id ASC NULLS LAST
                    LIMIT %s
                """, (args.limit,))
                rows = cur.fetchall()
                asset_ids = [r[0] for r in rows]

    total = len(asset_ids)
    print(f"处理 {total} 个币种, 输出到 {storage_root}")
    if args.dry_run:
        print("[Dry-run 模式]")
        for aid in asset_ids[:5]:
            with get_connection(settings.database_url) as conn:
                meta = get_asset_meta(conn, aid)
                if meta:
                    print(f"  [{meta['coin_symbol']}] {meta['coin_name']}")
        return 0

    ok_count = 0
    skip_count = 0
    error_count = 0

    for i, asset_id in enumerate(asset_ids, 1):
        try:
            with get_connection(settings.database_url) as conn:
                result = process_one_asset(conn, asset_id, storage_root, args.output_format, args.max_urls)

            if result["status"] == "ok":
                ok_count += 1
                file_info = " + ".join(
                    f"{f['type']}({f.get('url_count', '')})" for f in result.get("files", [])
                )
                if i % 50 == 0 or i == total:
                    print(f"  [{i}/{total}] {result['symbol']}: {file_info}")
            elif result["status"] == "no_urls":
                skip_count += 1
            else:
                skip_count += 1

        except Exception as e:
            error_count += 1
            print(f"  [{i}/{total}] asset_id={asset_id} 错误: {e}")

    print(f"\n=== 完成 ===")
    print(f"成功: {ok_count} | 跳过(无链接/无数据): {skip_count} | 错误: {error_count}")
    print(f"输出目录: {storage_root}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
