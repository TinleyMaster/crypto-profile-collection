"""
污染清理：删除 deep_crawl 中 GitHub 跨仓库 + 聚合域名内链条目，重置爬取状态。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import psycopg
from crypto_research.config import get_settings

# ── 聚合类域名：多项目共用平台，内链导航导致大规模 asset_id 污染 ──
AGGREGATION_DOMAINS = [
    "code4rena.com",
    "www.cyberscope.io",
    "reports.immunefi.com",
    "immunefi.com",
    "hashex.org",
    "www.allcryptowhitepapers.com",
    "thatwhitepaperguy.com",
    "www.quillaudits.com",
    "quillaudits.com",
    "blockchainreporter.net",
    "diligence.security",
    "www.reportlinker.com",
    "reportlinker.com",
    "ai.reportlinker.com",
    "conferences.miccai.org",
    "hacken.io",
    "assets.hacken.io",
    "hacken.ghost.io",
    "blog.openzeppelin.com",
    "www.certora.com",
    "certora.cdn.prismic.io",
]


def _build_ilike_any(column: str, domains: list[str]) -> str:
    """构建 ILIKE ANY 条件，匹配域名"""
    patterns = ", ".join(f"'%%{d}%%'" for d in domains)
    return f"{column} ILIKE ANY(ARRAY[{patterns}])"


def run():
    settings = get_settings()
    dsn = settings.database_url

    for attempt in range(3):
        try:
            conn = psycopg.connect(dsn, connect_timeout=10)
            cur = conn.cursor()
            break
        except Exception as e:
            print(f"连接尝试 {attempt+1}/3 失败: {e}")
            time.sleep(2)
    else:
        print("数据库连接失败")
        return

    print("=" * 65)
    print("  污染清理（GitHub + 聚合域名）")
    print("=" * 65)

    # ═══ 1. 当前状态 ═══
    print("\n── 1. 当前 deep_crawl 状态 ──")
    cur.execute("SELECT COUNT(*) FROM biz.doc_source_entry WHERE discovered_from LIKE 'deep_crawl:%%'")
    total_deep = cur.fetchone()[0]

    cur.execute(
        "SELECT COUNT(*) FROM biz.doc_source_entry "
        "WHERE discovered_from LIKE 'deep_crawl:%%' "
        "AND entry_url ILIKE '%%github.com%%'"
    )
    gh_deep = cur.fetchone()[0]

    agg_where = _build_ilike_any("entry_url", AGGREGATION_DOMAINS)
    cur.execute(
        f"SELECT COUNT(*) FROM biz.doc_source_entry "
        f"WHERE discovered_from LIKE 'deep_crawl:%%' AND {agg_where}"
    )
    agg_deep = cur.fetchone()[0]

    print(f"  deep_crawl 总条目:    {total_deep:>8,}")
    print(f"  github.com:           {gh_deep:>8,}")
    print(f"  聚合域名:             {agg_deep:>8,}")
    print(f"  其他(保留):           {total_deep - gh_deep - agg_deep:>8,}")

    # 聚合域名 TOP 分布
    if agg_deep > 0:
        print("\n  聚合域名 TOP 10:")
        cur.execute(f"""
            SELECT LOWER(SPLIT_PART(SPLIT_PART(entry_url, '/', 3), '?', 1)) AS domain,
                   COUNT(*) AS cnt
            FROM biz.doc_source_entry
            WHERE discovered_from LIKE 'deep_crawl:%%' AND {agg_where}
            GROUP BY domain ORDER BY cnt DESC LIMIT 10
        """)
        for domain, cnt in cur.fetchall():
            cur.execute(
                "SELECT a.canonical_symbol FROM biz.doc_source_entry e "
                "JOIN core.asset a ON a.asset_id = e.asset_id "
                "WHERE e.discovered_from LIKE 'deep_crawl:%%' "
                f"AND e.asset_id IS NOT NULL AND {agg_where} "
                "AND LOWER(SPLIT_PART(SPLIT_PART(e.entry_url, '/', 3), '?', 1)) = %s "
                "GROUP BY a.asset_id, a.canonical_symbol ORDER BY COUNT(*) DESC LIMIT 1",
                (domain,),
            )
            top_asset = cur.fetchone()
            asset_str = f" 最大受害者: {top_asset[0]}" if top_asset else ""
            print(f"  {domain:<40} {cnt:>6,} 条{asset_str}")

    total_to_delete = gh_deep + agg_deep
    if total_to_delete == 0:
        print("\n✅ 无需清理。")
        conn.close()
        return

    # ═══ 2. 执行 ═══
    print("\n" + "=" * 65)
    print("  即将执行:")
    if gh_deep > 0:
        print(f"  ① DELETE {gh_deep:,} 条 github.com deep_crawl 条目")
    if agg_deep > 0:
        print(f"  {'②' if gh_deep > 0 else '①'} DELETE {agg_deep:,} 条聚合域名 deep_crawl 条目")
    print(f"  最后: 重置 docs/official_website 原始入口爬取状态")
    print("=" * 65)

    # 步骤 1: 删除 github.com
    if gh_deep > 0:
        print(f"\n[1] 删除 github.com deep_crawl 条目...")
        cur.execute(
            "DELETE FROM biz.doc_source_entry "
            "WHERE discovered_from LIKE 'deep_crawl:%%' "
            "AND entry_url ILIKE '%%github.com%%'"
        )
        print(f"  已删除: {cur.rowcount:,} 条")

    # 步骤 2: 删除聚合域名
    if agg_deep > 0:
        step = 2 if gh_deep > 0 else 1
        print(f"\n[{step}] 删除聚合域名 deep_crawl 条目...")
        cur.execute(
            f"DELETE FROM biz.doc_source_entry "
            f"WHERE discovered_from LIKE 'deep_crawl:%%' AND {agg_where}"
        )
        print(f"  已删除: {cur.rowcount:,} 条")

    # 步骤 3: 重置 docs/official_website 原始入口
    step = (1 if gh_deep > 0 else 0) + (1 if agg_deep > 0 else 0) + 1
    print(f"\n[{step}] 重置 docs/official_website 原始入口 deep_crawled_at...")
    cur.execute(
        "UPDATE biz.doc_source_entry "
        "SET deep_crawled_at = NULL "
        "WHERE discovered_from NOT LIKE 'deep_crawl:%%' "
        "AND deep_crawled_at IS NOT NULL "
        "AND entry_type IN ('docs', 'official_website')"
    )
    print(f"  已重置: {cur.rowcount:,} 条")

    conn.commit()

    # ═══ 3. 验证 ═══
    print("\n── 3. 清理后验证 ──")
    cur.execute("SELECT COUNT(*) FROM biz.doc_source_entry WHERE discovered_from LIKE 'deep_crawl:%%'")
    new_total = cur.fetchone()[0]

    cur.execute(
        "SELECT COUNT(*) FROM biz.doc_source_entry "
        "WHERE discovered_from LIKE 'deep_crawl:%%' "
        "AND entry_url ILIKE '%%github.com%%'"
    )
    new_gh = cur.fetchone()[0]

    cur.execute(
        f"SELECT COUNT(*) FROM biz.doc_source_entry "
        f"WHERE discovered_from LIKE 'deep_crawl:%%' AND {agg_where}"
    )
    new_agg = cur.fetchone()[0]

    cur.execute(
        "SELECT COUNT(*) FROM biz.doc_source_entry "
        "WHERE discovered_from NOT LIKE 'deep_crawl:%%' "
        "AND deep_crawled_at IS NULL "
        "AND entry_type IN ('docs', 'official_website')"
    )
    pending = cur.fetchone()[0]

    print(f"  deep_crawl 剩余: {new_total:>8,} (原有 {total_deep:,})")
    print(f"  github.com:      {new_gh:>8,} (应为 0)")
    print(f"  聚合域名:        {new_agg:>8,} (应为 0)")
    print(f"  待重新爬取:      {pending:>8,} 条")
    print(f"\n  下一步: 运行 B2 深度文档发现（带同仓库+聚合域名过滤）")

    conn.close()
    print("\n清理完成。")


if __name__ == "__main__":
    run()
