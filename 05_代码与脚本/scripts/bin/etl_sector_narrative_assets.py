#!/usr/bin/env python3
"""
FEAT-SECTOR-002: 叙事板块→资产映射 ETL

从 src_cmc.cmc_category_member + core.asset_source_map 派生，
打通「叙事板块 → 代币」下钻路径，写入 biz.sector_narrative_asset。

用法:
    python etl_sector_narrative_assets.py          # 跑最新一天
    python etl_sector_narrative_assets.py --date 2026-08-31
    python etl_sector_narrative_assets.py --backfill  # 回填所有有数据的日期

依赖表:
  - src_cmc.cmc_category         (分类元数据)
  - src_cmc.cmc_category_member  (分类成员日快照)
  - core.asset_source_map        (cmc_id -> asset_id 映射)

输出表:
  - biz.sector_narrative_asset   (叙事→资产映射日快照)
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

# ── 叙事标准名 → CMC 分类名映射 ──────────────────────────────────────
# NARRATIVE_WATCHLIST 中的标准名（左） → CMC category_name 实际名（右）
# 精确匹配的不需要写在这，只有名称不一致的才需要
NARRATIVE_CMC_NAME_MAP: dict[str, str] = {
    "AI & Big Data": "Artificial Intelligence",    # 暂定，如 CMC 无则用 asset.categories 兜底
    "Real World Assets": "Real World Assets Protocols",
    "DePIN": "DePIN",
    "Liquid Staking": "Liquid Staking Derivatives",
    "Yield Farming": "Yield Farming",
    "Restaking": "Restaking",
    "NFTs & Collectibles": "NFTs & Collectibles",
    "Metaverse": "Metaverse",
    "File Storage": "Storage",
    "Zero Knowledge": "Zero Knowledge Proofs",
    # 以下为精确匹配，列出供参考（无需映射）:
    # "Layer 1", "Layer 2", "DeFi", "Memes", "Gaming",
    # "Lending", "Derivatives", "Bridges", "Stablecoin",
    # "Privacy", "Oracles", "SocialFi",
}

# 叙事关注列表（与 macro_market.py NARRATIVE_WATCHLIST 对齐）
NARRATIVE_WATCHLIST: list[str] = [
    "Layer 1", "Layer 2", "DeFi", "Memes", "AI & Big Data", "Real World Assets",
    "Gaming", "DePIN", "Liquid Staking", "Lending", "Derivatives", "Yield Farming",
    "Restaking", "Bridges", "Stablecoin", "NFTs & Collectibles", "Metaverse",
    "Privacy", "Oracles", "File Storage", "Zero Knowledge", "SocialFi",
]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="ETL: 叙事板块→资产映射（biz.sector_narrative_asset）")
    p.add_argument("--date", type=str, default=None,
                   help="指定快照日期 YYYY-MM-DD，默认取 src_cmc 最新日期")
    p.add_argument("--backfill", action="store_true",
                   help="回填模式：处理所有 src_cmc 有数据的日期")
    p.add_argument("--dry-run", action="store_true",
                   help="只计算不写入")
    return p


def get_latest_snapshot_date(conn) -> date | None:
    """取 src_cmc.cmc_category_member 中数据量最多的那天（避免部分采集中断导致取到空数据日期）。"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT snapshot_date
            FROM src_cmc.cmc_category_member
            GROUP BY snapshot_date
            ORDER BY count(*) DESC
            LIMIT 1
        """)
        row = cur.fetchone()
        return row[0] if row and row[0] else None


def get_available_dates(conn) -> list[date]:
    """取所有有数据的快照日期。"""
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT snapshot_date FROM src_cmc.cmc_category_member ORDER BY snapshot_date")
        return [r[0] for r in cur.fetchall()]


def resolve_cmc_categories(conn) -> dict[str, dict]:
    """
    解析 NARRATIVE_WATCHLIST → CMC 分类信息。
    返回 {narrative_std_name: {category_id, category_name}}
    """
    result: dict[str, dict] = {}

    with conn.cursor() as cur:
        # 先拿全部分类
        cur.execute("SELECT category_id, category_name FROM src_cmc.cmc_category")
        all_cats = {row[1]: row[0] for row in cur.fetchall()}  # name -> id

    for narrative in NARRATIVE_WATCHLIST:
        # 优先用映射表
        cmc_name = NARRATIVE_CMC_NAME_MAP.get(narrative, narrative)
        cat_id = all_cats.get(cmc_name)

        if cat_id:
            result[narrative] = {
                "category_id": cat_id,
                "category_name": cmc_name,
            }
            continue

        # 模糊匹配兜底
        for cat_name, cid in all_cats.items():
            if narrative.lower() in cat_name.lower():
                result[narrative] = {
                    "category_id": cid,
                    "category_name": cat_name,
                }
                break

    return result


def etl_for_date(conn, snapshot_date: date, narrative_cats: dict[str, dict], dry_run: bool = False) -> dict:
    """
    对指定日期执行 ETL，返回统计信息。
    """
    stats = {"date": str(snapshot_date), "narratives": 0, "rows": 0, "matched": 0, "skipped": []}

    insert_rows: list[tuple] = []

    with conn.cursor() as cur:
        for narrative, cat_info in narrative_cats.items():
            cat_id = cat_info["category_id"]
            cat_name = cat_info["category_name"]

            # 取该分类在指定日期的成员
            cur.execute("""
                SELECT
                    cm.cmc_id,
                    cm.rank_in_category,
                    cm.market_cap,
                    cm.percent_change_24h,
                    cam.symbol,
                    cam.name,
                    asm.asset_id
                FROM src_cmc.cmc_category_member cm
                LEFT JOIN src_cmc.cmc_asset_map cam ON cm.cmc_id = cam.cmc_id
                LEFT JOIN core.asset_source_map asm
                  ON asm.source_code = 'cmc'
                 AND asm.source_asset_key = cm.cmc_id::text
                WHERE cm.category_id = %s
                  AND cm.snapshot_date = %s
                ORDER BY cm.market_cap DESC NULLS LAST
            """, (cat_id, snapshot_date))

            rows = cur.fetchall()
            if not rows:
                stats["skipped"].append(narrative)
                continue

            # 计算板块总市值（用于 weight_pct）
            total_mcap = sum((r[2] or 0) for r in rows if r[2] and r[2] > 0)

            for cmc_id, rank, mcap, pct24h, symbol, name, asset_id in rows:
                weight = (mcap / total_mcap * 100) if total_mcap and mcap else None
                insert_rows.append((
                    narrative,
                    cat_id,
                    cat_name,
                    asset_id,
                    cmc_id,
                    symbol,
                    name,
                    rank,
                    mcap,
                    weight,
                    pct24h,
                    snapshot_date,
                ))

            stats["narratives"] += 1
            stats["rows"] += len(rows)
            stats["matched"] += sum(1 for r in rows if r[6] is not None)

    if dry_run or not insert_rows:
        return stats

    # 写入数据库（UPSERT 模式，支持重跑）
    with conn.cursor() as cur:
        # 先删当天数据（幂等）
        cur.execute("DELETE FROM biz.sector_narrative_asset WHERE as_of_date = %s", (snapshot_date,))

        # 批量插入
        cur.executemany("""
            INSERT INTO biz.sector_narrative_asset (
                narrative, cmc_category_id, cmc_category_name, asset_id,
                cmc_id, symbol, name, rank_in_category, market_cap,
                weight_pct, percent_change_24h, as_of_date
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, insert_rows)

        conn.commit()

    return stats


def main() -> int:
    args = build_parser().parse_args()

    from crypto_research.db.conn import get_connection
    from crypto_research.config import get_settings

    settings = get_settings(require_database=True)
    if not settings.database_url:
        print("ERROR: DATABASE_URL is required", file=sys.stderr)
        return 1

    with get_connection(settings.database_url) as conn:
        # 解析叙事 → CMC 分类映射
        narrative_cats = resolve_cmc_categories(conn)
        print(f"[INFO] 匹配到 {len(narrative_cats)}/{len(NARRATIVE_WATCHLIST)} 个叙事分类")
        for n, info in sorted(narrative_cats.items()):
            print(f"  {n:25s} → {info['category_name']}")
        missing = [n for n in NARRATIVE_WATCHLIST if n not in narrative_cats]
        if missing:
            print(f"[WARN] 未匹配的叙事: {missing}")

        if args.backfill:
            dates = get_available_dates(conn)
            print(f"\n[INFO] 回填模式，共 {len(dates)} 天数据")
        elif args.date:
            dates = [datetime.strptime(args.date, "%Y-%m-%d").date()]
        else:
            latest = get_latest_snapshot_date(conn)
            if not latest:
                print("ERROR: src_cmc.cmc_category_member 无数据", file=sys.stderr)
                return 1
            dates = [latest]
            print(f"[INFO] 使用最新快照日期: {latest}")

        total_rows = 0
        total_matched = 0
        for d in dates:
            stats = etl_for_date(conn, d, narrative_cats, dry_run=args.dry_run)
            match_rate = stats['matched']/stats['rows']*100 if stats['rows'] else 0
            print(f"  {stats['date']}: {stats['narratives']} 叙事, "
                  f"{stats['rows']} 条映射, "
                  f"{stats['matched']} 匹配 asset_id ({match_rate:.1f}%)"
                  f"{' [DRY-RUN]' if args.dry_run else ''}")
            if stats["skipped"]:
                print(f"    跳过(无数据): {stats['skipped']}")
            total_rows += stats["rows"]
            total_matched += stats["matched"]

        print(f"\n[DONE] 总计 {len(dates)} 天, {total_rows} 条, "
              f"{total_matched} 匹配 ({total_matched/total_rows*100:.1f}%)"
              f"{' [DRY-RUN]' if args.dry_run else ''}" if total_rows > 0 else f"\n[DONE] 总计 {len(dates)} 天, 0 条数据 {' [DRY-RUN]' if args.dry_run else ''}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
