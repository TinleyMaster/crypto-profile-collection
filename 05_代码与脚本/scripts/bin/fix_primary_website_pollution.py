"""
修复 primary 官网污染问题。

策略：来源可信度 + 域名一致性裁决
- 来源可信度：CMC(100) > Binance(90) > DL(60) > CG(40) > DexScreener(30)
- 同域名只保留一个 primary（最短路径优先）
- 有 CMC 时，CG/DL 的不同域名官网降级为非 primary
"""
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

# 来源可信度权重
SOURCE_TRUST_WEIGHT = {
    "cmc": 100,
    "binance": 90,
    "dl": 60,
    "cg": 40,
    "dexscreener": 30,
}


def extract_domain(url: str) -> str:
    """提取 URL 的主域名（不含 www.，取最后两段）。"""
    try:
        parsed = urlparse(url)
        host = parsed.netloc or parsed.path
        host = host.replace("www.", "").lower()
        parts = host.split(".")
        if len(parts) >= 2:
            return ".".join(parts[-2:])
        return host
    except Exception:
        return url.lower()


def extract_path_depth(url: str) -> int:
    """计算 URL 路径深度（用于同域名下选最短路径）。"""
    try:
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        if not path:
            return 0
        return len([p for p in path.split("/") if p])
    except Exception:
        return 999


def fix_primary_websites(conn, asset_ids: list[int] | None = None) -> dict:
    """
    修复指定资产的 primary 官网污染。
    如果 asset_ids 为 None，则修复所有有多 primary 官网的资产。

    返回统计信息。
    """
    cur = conn.cursor()

    # 找出所有有多 primary 官网的资产
    if asset_ids:
        placeholders = ",".join(["%s"] * len(asset_ids))
        cur.execute(
            f"""
            SELECT asset_id
            FROM biz.doc_source_entry
            WHERE entry_type = 'official_website' AND is_primary = true
              AND entity_type = 'asset'
              AND asset_id IN ({placeholders})
            GROUP BY asset_id
            HAVING COUNT(*) > 1
            ORDER BY asset_id;
            """,
            asset_ids,
        )
    else:
        cur.execute("""
            SELECT asset_id
            FROM biz.doc_source_entry
            WHERE entry_type = 'official_website' AND is_primary = true
              AND entity_type = 'asset'
            GROUP BY asset_id
            HAVING COUNT(*) > 1
            ORDER BY asset_id;
        """)

    multi_primary_assets = [row[0] for row in cur.fetchall()]
    total = len(multi_primary_assets)
    print(f"需要修复的多 primary 官网资产数: {total}")

    fixed_count = 0
    demoted_count = 0

    for idx, asset_id in enumerate(multi_primary_assets):
        if (idx + 1) % 500 == 0:
            print(f"  进度: {idx + 1}/{total}")

        # 获取该资产所有 primary 官网
        cur.execute("""
            SELECT entry_id, source_code, entry_url, is_primary
            FROM biz.doc_source_entry
            WHERE asset_id = %s AND entry_type = 'official_website'
              AND entity_type = 'asset' AND is_primary = true
            ORDER BY entry_id;
        """, (asset_id,))
        entries = cur.fetchall()

        if len(entries) <= 1:
            continue

        # 按来源可信度排序
        entries_sorted = sorted(
            entries,
            key=lambda e: (
                -SOURCE_TRUST_WEIGHT.get(e[1], 0),
                extract_path_depth(e[2]),
                len(e[2]),
            ),
        )

        # 裁决：最高可信度来源的第一个为 primary
        # 同域名的其他条目降级为非 primary
        winner = entries_sorted[0]
        winner_domain = extract_domain(winner[2])

        demoted_ids = []
        for entry in entries_sorted[1:]:
            entry_domain = extract_domain(entry[2])
            # 不同域名且来源可信度低于 winner → 降级
            # 同域名但路径更深 → 降级
            if entry_domain != winner_domain or extract_path_depth(entry[2]) > extract_path_depth(winner[2]):
                demoted_ids.append(entry[0])

        if demoted_ids:
            placeholders = ",".join(["%s"] * len(demoted_ids))
            cur.execute(
                f"""
                UPDATE biz.doc_source_entry
                SET is_primary = false, updated_at = NOW()
                WHERE entry_id IN ({placeholders});
                """,
                demoted_ids,
            )
            demoted_count += len(demoted_ids)
            fixed_count += 1

    cur.close()
    return {
        "total_multi_primary": total,
        "fixed_assets": fixed_count,
        "demoted_entries": demoted_count,
    }


def main():
    settings = get_settings()

    with get_connection(settings.database_url) as conn:
        # 先跑 Top 100 验证效果
        print("=" * 60)
        print("Phase 1: 修复 CMC Top 100 核心资产")
        print("=" * 60)

        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT a.asset_id
            FROM src_cmc.cmc_asset_map m
            JOIN core.asset_source_map sm
              ON sm.source_code = 'cmc' AND sm.source_asset_key = m.cmc_id::text
            JOIN core.asset a ON a.asset_id = sm.asset_id
            WHERE m.rank_num IS NOT NULL AND m.rank_num <= 100
            ORDER BY a.asset_id;
        """)
        top100_ids = [row[0] for row in cur.fetchall()]
        cur.close()

        stats = fix_primary_websites(conn, asset_ids=top100_ids)
        print(f"\nTop 100 修复结果:")
        print(f"  多 primary 资产数: {stats['total_multi_primary']}")
        print(f"  已修复资产数: {stats['fixed_assets']}")
        print(f"  降级条目数: {stats['demoted_entries']}")

        # 验证 Bitcoin
        cur = conn.cursor()
        cur.execute("""
            SELECT source_code, entry_url, is_primary
            FROM biz.doc_source_entry
            WHERE asset_id = 2 AND entry_type = 'official_website'
              AND entity_type = 'asset'
            ORDER BY is_primary DESC, source_code;
        """)
        print("\n修复后 Bitcoin (asset_id=2) 的官网:")
        for row in cur.fetchall():
            print(f"  [{row[0]}] primary={row[2]} {row[1]}")
        cur.close()

        # 询问是否全量修复
        print("\n" + "=" * 60)
        print("Phase 2: 全量修复所有多 primary 官网资产")
        print("=" * 60)

        stats_all = fix_primary_websites(conn, asset_ids=None)
        print(f"\n全量修复结果:")
        print(f"  多 primary 资产数: {stats_all['total_multi_primary']}")
        print(f"  已修复资产数: {stats_all['fixed_assets']}")
        print(f"  降级条目数: {stats_all['demoted_entries']}")

        # 最终验证：还有多少多 primary
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*)
            FROM (
                SELECT asset_id
                FROM biz.doc_source_entry
                WHERE entry_type = 'official_website' AND is_primary = true
                  AND entity_type = 'asset'
                GROUP BY asset_id
                HAVING COUNT(*) > 1
            ) t;
        """)
        remaining = cur.fetchone()[0]
        print(f"\n修复后剩余多 primary 官网资产数: {remaining}")

        # 看看剩余的是什么情况
        if remaining > 0:
            cur.execute("""
                SELECT a.asset_id, a.canonical_symbol, a.canonical_name, COUNT(*) as cnt,
                       ARRAY_AGG(DISTINCT e.source_code) as sources
                FROM biz.doc_source_entry e
                JOIN core.asset a ON e.asset_id = a.asset_id
                WHERE e.entry_type = 'official_website' AND e.is_primary = true
                  AND e.entity_type = 'asset'
                GROUP BY a.asset_id, a.canonical_symbol, a.canonical_name
                HAVING COUNT(*) > 1
                ORDER BY cnt DESC
                LIMIT 10;
            """)
            print("\n剩余多 primary Top 10:")
            for row in cur.fetchall():
                print(f"  asset_id={row[0]} {row[1]} ({row[2]}): {row[3]} 个, sources={row[4]}")

        cur.close()


if __name__ == "__main__":
    main()
