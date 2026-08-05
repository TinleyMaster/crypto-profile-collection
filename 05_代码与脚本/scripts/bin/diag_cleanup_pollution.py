"""
GitHub 跨仓库污染清理：删除 github.com deep_crawl 条目，重置原始入口爬取状态。
原理：deep_crawl 爬 GitHub 页面时，跨仓库链接被错误继承了同一 asset_id，
     导致大规模污染（如 DINU 的 12 万条 github.com 链接实际来自数百个无关仓库）。
修复后 B2 已有同仓库过滤，删除旧数据后重新爬取即可根治。
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
    print("  GitHub 跨仓库污染清理")
    print("=" * 65)

    # ═══ 1. 当前污染规模 ═══
    print("\n── 1. 污染规模评估 ──")
    cur.execute("SELECT COUNT(*) FROM biz.doc_source_entry WHERE discovered_from LIKE 'deep_crawl:%%'")
    total_deep = cur.fetchone()[0]
    cur.execute(
        "SELECT COUNT(*) FROM biz.doc_source_entry "
        "WHERE discovered_from LIKE 'deep_crawl:%%' "
        "AND entry_url ILIKE '%%github.com%%'"
    )
    gh_deep = cur.fetchone()[0]
    print(f"  deep_crawl 总条目: {total_deep:>8,}")
    print(f"  其中 github.com: {gh_deep:>8,} ({gh_deep/total_deep*100:.1f}%)" if total_deep else "")

    cur.execute(
        "SELECT COUNT(DISTINCT asset_id) FROM biz.doc_source_entry "
        "WHERE discovered_from LIKE 'deep_crawl:%%' "
        "AND entry_url ILIKE '%%github.com%%'"
    )
    affected_assets = cur.fetchone()[0]
    print(f"  受影响资产数: {affected_assets:>8,}")

    # 受影响最严重的资产
    print("\n  污染 TOP 10 资产:")
    cur.execute("""
        SELECT a.canonical_symbol, a.canonical_name, COUNT(*) AS cnt
        FROM biz.doc_source_entry e
        JOIN core.asset a ON a.asset_id = e.asset_id
        WHERE e.discovered_from LIKE 'deep_crawl:%%'
          AND e.entry_url ILIKE '%%github.com%%'
          AND e.asset_id IS NOT NULL
        GROUP BY a.asset_id, a.canonical_symbol, a.canonical_name
        ORDER BY cnt DESC LIMIT 10
    """)
    for sym, name, cnt in cur.fetchall():
        print(f"  {sym or '(无)':>8}  {name[:30]:<30}  {cnt:>7,} 条")

    # ═══ 2. 待重置的原始 GitHub 入口 ═══
    cur.execute(
        "SELECT COUNT(*) FROM biz.doc_source_entry "
        "WHERE discovered_from NOT LIKE 'deep_crawl:%%' "
        "AND deep_crawled_at IS NOT NULL"
    )
    crawled_orig = cur.fetchone()[0]

    cur.execute(
        "SELECT COUNT(*) FROM biz.doc_source_entry "
        "WHERE discovered_from NOT LIKE 'deep_crawl:%%' "
        "AND deep_crawled_at IS NOT NULL "
        "AND entry_url ILIKE '%%github.com%%'"
    )
    gh_crawled = cur.fetchone()[0]

    print(f"\n── 2. 待重置的原始入口 ──")
    print(f"  已爬取的原始入口: {crawled_orig:>8,}")
    print(f"  其中 github.com: {gh_crawled:>8,}")

    # ═══ 3. 执行 ═══
    print("\n" + "=" * 65)
    print("  即将执行:")
    print(f"  ① DELETE {gh_deep:,} 条 github.com deep_crawl 条目")
    print(f"  ② 重置 {gh_crawled:,} 个 GitHub 原始入口的 deep_crawled_at")
    print(f"  ③ 重置部分非 GitHub 原始入口以便重新发现有效链接")
    print(f"\n  下一轮 B2 深爬将使用同仓库过滤，不会再次污染。")
    print("=" * 65)

    # 步骤 1: 删除 github.com deep_crawl 条目
    print("\n[1/3] 删除 github.com deep_crawl 条目...")
    cur.execute(
        "DELETE FROM biz.doc_source_entry "
        "WHERE discovered_from LIKE 'deep_crawl:%%' "
        "AND entry_url ILIKE '%%github.com%%'"
    )
    deleted_gh = cur.rowcount
    print(f"  已删除: {deleted_gh:,} 条")

    # 步骤 2: 重置原始 GitHub 入口的爬取状态
    print("[2/3] 重置原始 GitHub 入口 deep_crawled_at...")
    cur.execute(
        "UPDATE biz.doc_source_entry "
        "SET deep_crawled_at = NULL "
        "WHERE discovered_from NOT LIKE 'deep_crawl:%%' "
        "AND deep_crawled_at IS NOT NULL "
        "AND entry_url ILIKE '%%github.com%%'"
    )
    reset_gh = cur.rowcount
    print(f"  已重置: {reset_gh:,} 条")

    # 步骤 3: 重置 docs/official_website 原始入口（它们发现的非 GitHub 链接是有效的，
    #         但它们的 GitHub 子链接已删，需要重新发现）
    print("[3/3] 重置 docs/official_website 原始入口（让 GitHub 链接重新被发现）...")
    cur.execute(
        "UPDATE biz.doc_source_entry "
        "SET deep_crawled_at = NULL "
        "WHERE discovered_from NOT LIKE 'deep_crawl:%%' "
        "AND deep_crawled_at IS NOT NULL "
        "AND entry_type IN ('docs', 'official_website')"
    )
    reset_docs = cur.rowcount
    print(f"  已重置: {reset_docs:,} 条")

    conn.commit()

    # ═══ 4. 验证 ═══
    print("\n── 4. 清理后验证 ──")
    cur.execute("SELECT COUNT(*) FROM biz.doc_source_entry WHERE discovered_from LIKE 'deep_crawl:%%'")
    new_total = cur.fetchone()[0]
    cur.execute(
        "SELECT COUNT(*) FROM biz.doc_source_entry "
        "WHERE discovered_from LIKE 'deep_crawl:%%' "
        "AND entry_url ILIKE '%%github.com%%'"
    )
    new_gh = cur.fetchone()[0]

    cur.execute(
        "SELECT COUNT(*) FROM biz.doc_source_entry "
        "WHERE discovered_from NOT LIKE 'deep_crawl:%%' "
        "AND deep_crawled_at IS NULL "
        "AND entry_type IN ('docs', 'official_website', 'github')"
    )
    pending = cur.fetchone()[0]

    print(f"  deep_crawl 剩余: {new_total:>8,} (原有 {total_deep:,})")
    print(f"  其中 github.com: {new_gh:>8,} (应为 0)")
    print(f"  待重新爬取: {pending:>8,} 条")
    print(f"\n  下一步: 运行 B2 深度文档发现（带同仓库过滤）")

    conn.close()
    print("\n清理完成。")


if __name__ == "__main__":
    run()
