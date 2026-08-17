"""代币赛道标签归一化：从 CMC tags/category_hint 计算赛道并入库。

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
    primary_sector,
    SECTOR_LABELS,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="代币赛道标签归一化")
    p.add_argument("--save", action="store_true", help="写入数据库（默认 dry-run）")
    return p


def load_cmc_signals() -> list[dict]:
    """读取有 CMC 映射的资产的 tags + category_hint（每个资产取最新一条 cmc 记录）。"""
    with get_connection(_settings.database_url) as conn:
        cur = conn.cursor(row_factory=psycopg.rows.dict_row)
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
        return cur.fetchall()


def main() -> None:
    global _settings
    args = build_parser().parse_args()
    _settings = get_settings()

    print("读取 CMC 赛道信号...")
    rows = load_cmc_signals()
    print(f"  共 {len(rows)} 个资产有 CMC 映射")

    # 分类
    classified: list[dict] = []
    for r in rows:
        sectors = classify_cmc_sectors(r["tags"], r["category_hint"])
        if sectors:
            classified.append({
                "asset_id": r["asset_id"],
                "sectors": sectors,
                "primary": primary_sector(sectors),
            })

    print(f"  命中赛道: {len(classified)} 个资产, "
          f"未命中（保持 other）: {len(rows) - len(classified)} 个")

    # 分布统计
    counter = Counter(c["primary"] for c in classified)
    print("\n=== 主赛道分布 ===")
    for sector, cnt in counter.most_common():
        label = SECTOR_LABELS.get(sector, sector)
        print(f"  {sector:<12} {label:<14} {cnt}")

    # 多标签占比
    multi = sum(1 for c in classified if len(c["sectors"]) > 1)
    print(f"\n多赛道标签资产: {multi} 个（{multi / max(1, len(classified)):.1%}）")

    if not args.save:
        print("\n[dry-run] 未写入。加 --save 实际入库。")
        return

    # 写入
    with get_connection(_settings.database_url) as conn:
        cur = conn.cursor()
        # 清空 cmc 来源的旧记录，全量重建
        cur.execute("DELETE FROM biz.asset_sector WHERE source = 'cmc'")

        # 重置所有 primary_sector 为 other（后续只更新命中者）
        cur.execute("UPDATE core.asset SET primary_sector = 'other'")

        # 批量 INSERT 标签（executemany 减少往返）
        tag_rows = [
            (c["asset_id"], sector, conf, sector == c["primary"])
            for c in classified for sector, conf in c["sectors"]
        ]
        cur.executemany("""
            INSERT INTO biz.asset_sector (asset_id, sector, source, confidence, is_primary)
            VALUES (%s, %s, 'cmc', %s, %s)
            ON CONFLICT (asset_id, sector, source) DO UPDATE
            SET confidence = EXCLUDED.confidence,
                is_primary = EXCLUDED.is_primary,
                updated_at = NOW()
        """, tag_rows)

        # 批量 UPDATE 主赛道
        prim_rows = [(c["primary"], c["asset_id"]) for c in classified]
        cur.executemany("""
            UPDATE core.asset SET primary_sector = %s, updated_at = NOW()
            WHERE asset_id = %s
        """, prim_rows)

        conn.commit()
        print(f"\n已写入 {len(tag_rows)} 条赛道标签, 更新 {len(prim_rows)} 个资产主赛道。")


if __name__ == "__main__":
    main()
