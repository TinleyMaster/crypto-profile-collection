"""代币赛道标签归一化：从 CMC/CG/DL 多来源计算赛道并入库。

用法:
    python refresh_asset_sectors.py          # dry-run 打印赛道分布
    python refresh_asset_sectors.py --save   # 写入 biz.asset_sector + core.asset.primary_sector
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(line_buffering=True)

import psycopg
import psycopg.rows

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection
from crypto_research.mapping.sector import (
    classify_cmc_sectors,
    classify_cg_sectors,
    classify_dl_sectors,
    merge_sectors,
    primary_sector,
    SECTOR_LABELS,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="代币赛道标签归一化（多来源）")
    p.add_argument("--save", action="store_true", help="写入数据库（默认 dry-run）")
    return p


def load_cmc_signals(conn) -> dict[int, dict]:
    """读取 CMC 来源的赛道信号，返回 {asset_id: {tags, category_hint}}。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            SELECT DISTINCT ON (m.asset_id)
                   m.asset_id,
                   i.tags,
                   i.category_hint
            FROM core.asset_source_map m
            JOIN src_cmc.cmc_asset_info i ON i.cmc_id = m.source_asset_key::bigint
            WHERE m.source_code = 'cmc'
            ORDER BY m.asset_id, i.date_launched DESC NULLS LAST
        """)
        rows = cur.fetchall()
        return {r["asset_id"]: dict(r) for r in rows}


def load_cg_signals(conn) -> dict[int, dict]:
    """读取 CG 来源的赛道信号，返回 {asset_id: {categories}}。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            SELECT DISTINCT ON (m.asset_id)
                   m.asset_id,
                   ci.categories
            FROM core.asset_source_map m
            JOIN src_cg.coin_info ci ON ci.coin_id = m.source_asset_key
            WHERE m.source_code = 'cg'
              AND ci.categories IS NOT NULL
              AND jsonb_array_length(ci.categories) > 0
            ORDER BY m.asset_id
        """)
        rows = cur.fetchall()
        return {r["asset_id"]: dict(r) for r in rows}


def load_dl_signals(conn) -> dict[int, dict]:
    """读取 DL 来源的赛道信号，返回 {asset_id: {category}}。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            SELECT DISTINCT ON (m.asset_id)
                   m.asset_id,
                   pl.category
            FROM core.asset_source_map m
            JOIN src_dl.protocol_list pl ON pl.protocol_id = m.source_asset_key
            WHERE m.source_code = 'dl'
              AND pl.category IS NOT NULL
            ORDER BY m.asset_id
        """)
        rows = cur.fetchall()
        return {r["asset_id"]: dict(r) for r in rows}


def main() -> None:
    args = build_parser().parse_args()
    settings = get_settings()

    with get_connection(settings.database_url) as conn:
        # 读取各来源信号
        print("读取 CMC 赛道信号...")
        cmc_signals = load_cmc_signals(conn)
        print(f"  共 {len(cmc_signals)} 个资产有 CMC 映射")

        print("读取 CG 赛道信号...")
        cg_signals = load_cg_signals(conn)
        print(f"  共 {len(cg_signals)} 个资产有 CG 分类")

        print("读取 DL 赛道信号...")
        dl_signals = load_dl_signals(conn)
        print(f"  共 {len(dl_signals)} 个资产有 DL 分类")

        # 收集所有有信号的资产
        all_asset_ids = set(cmc_signals.keys()) | set(cg_signals.keys()) | set(dl_signals.keys())
        print(f"\n有任一来源信号的资产总数: {len(all_asset_ids)}")

        # 分类
        classified: list[dict] = []
        source_hit = {"cmc": 0, "cg": 0, "dl": 0, "multi_source": 0}

        for asset_id in all_asset_ids:
            cmc = cmc_signals.get(asset_id)
            cg = cg_signals.get(asset_id)
            dl = dl_signals.get(asset_id)

            cmc_sectors = classify_cmc_sectors(cmc["tags"], cmc["category_hint"]) if cmc else []
            cg_sectors = classify_cg_sectors(cg["categories"]) if cg else []
            dl_sectors = classify_dl_sectors(dl["category"]) if dl else []

            # 统计来源命中
            if cmc_sectors:
                source_hit["cmc"] += 1
            if cg_sectors:
                source_hit["cg"] += 1
            if dl_sectors:
                source_hit["dl"] += 1
            sources_with_hit = sum(1 for s in [cmc_sectors, cg_sectors, dl_sectors] if s)
            if sources_with_hit >= 2:
                source_hit["multi_source"] += 1

            sectors = merge_sectors(cmc_sectors, cg_sectors, dl_sectors)
            if sectors:
                classified.append({
                    "asset_id": asset_id,
                    "sectors": sectors,
                    "primary": primary_sector(sectors),
                })

        print(f"\n命中赛道: {len(classified)} 个资产")
        print(f"  CMC 命中: {source_hit['cmc']}")
        print(f"  CG 命中: {source_hit['cg']}")
        print(f"  DL 命中: {source_hit['dl']}")
        print(f"  多来源交叉命中: {source_hit['multi_source']}")

        # 分布统计
        counter = Counter(c["primary"] for c in classified)
        print("\n=== 主赛道分布（命中赛道的资产） ===")
        for sector, cnt in counter.most_common():
            label = SECTOR_LABELS.get(sector, sector)
            pct = cnt / len(classified) * 100
            bar = '█' * int(pct / 2)
            print(f"  {sector:<12} {label:<14} {cnt:6d} ({pct:5.1f}%) {bar}")

        # 多标签占比
        multi = sum(1 for c in classified if len(c["sectors"]) > 1)
        print(f"\n多赛道标签资产: {multi} 个（{multi / max(1, len(classified)):.1%}）")

        if not args.save:
            print("\n[dry-run] 未写入。加 --save 实际入库。")
            return

        # 写入
        cur = conn.cursor()
        # 清空所有来源的旧记录，全量重建
        cur.execute("DELETE FROM biz.asset_sector WHERE source IN ('cmc', 'cg', 'dl')")

        # 重置所有 primary_sector 为 other（后续只更新命中者）
        cur.execute("UPDATE core.asset SET primary_sector = 'other'")

        # 按来源分别写入标签（保留来源信息）
        for source, signals, classify_fn in [
            ("cmc", cmc_signals, lambda s: classify_cmc_sectors(s["tags"], s["category_hint"])),
            ("cg", cg_signals, lambda s: classify_cg_sectors(s["categories"])),
            ("dl", dl_signals, lambda s: classify_dl_sectors(s["category"])),
        ]:
            tag_rows = []
            for asset_id, sig in signals.items():
                sectors = classify_fn(sig)
                for sector, conf in sectors:
                    tag_rows.append((asset_id, sector, conf))
            if tag_rows:
                cur.executemany(f"""
                    INSERT INTO biz.asset_sector (asset_id, sector, source, confidence, is_primary)
                    VALUES (%s, %s, '{source}', %s, false)
                    ON CONFLICT (asset_id, sector, source) DO UPDATE
                    SET confidence = EXCLUDED.confidence,
                        is_primary = false,
                        updated_at = NOW()
                """, tag_rows)
                print(f"  写入 {source} 来源标签: {len(tag_rows)} 条")

        # 重新计算 is_primary（每个资产取置信度最高的赛道）
        cur.execute("""
            WITH ranked AS (
                SELECT asset_id, sector, source,
                    ROW_NUMBER() OVER (
                        PARTITION BY asset_id
                        ORDER BY confidence DESC,
                            CASE source
                                WHEN 'cmc' THEN 3
                                WHEN 'cg' THEN 2
                                WHEN 'dl' THEN 1
                                ELSE 0
                            END DESC
                    ) as rn
                FROM biz.asset_sector
            )
            UPDATE biz.asset_sector s
            SET is_primary = (r.rn = 1),
                updated_at = NOW()
            FROM ranked r
            WHERE s.asset_id = r.asset_id
              AND s.sector = r.sector
              AND s.source = r.source
        """)

        # 批量 UPDATE 主赛道
        prim_rows = [(c["primary"], c["asset_id"]) for c in classified]
        cur.executemany("""
            UPDATE core.asset SET primary_sector = %s, updated_at = NOW()
            WHERE asset_id = %s
        """, prim_rows)

        conn.commit()
        print(f"\n已写入赛道标签，更新 {len(prim_rows)} 个资产主赛道。")


if __name__ == "__main__":
    main()
