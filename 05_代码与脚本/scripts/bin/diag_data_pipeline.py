"""
数据链路诊断：检查从数据源到文档链接的完整链路健康状况。
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
    print("  数据链路诊断报告")
    print("=" * 65)

    # ═══ 1. 数据源基数 ═══
    print("\n── 1. 数据源基数 ──")
    cur.execute("SELECT COUNT(*) FROM core.asset")
    print(f"  core.asset (资产总数): {cur.fetchone()[0]:>8,}")

    cur.execute("SELECT COUNT(*) FROM biz.coin_basic")
    print(f"  biz.coin_basic (币种消费表): {cur.fetchone()[0]:>8,}")

    for schema, label, table in [
        ("src_cmc", "CMC", "cmc_asset_map"),
        ("src_cg", "CG", "coin_list"),
        ("src_dl", "DL", "protocol_list"),
    ]:
        cur.execute(f"SELECT COUNT(*) FROM {schema}.{table}")
        print(f"  {label} {table}: {cur.fetchone()[0]:>8,}")

    cur.execute("SELECT source_code, COUNT(*) FROM core.asset_source_map "
                 "GROUP BY source_code ORDER BY source_code")
    for sc, cnt in cur.fetchall():
        print(f"  {sc} → asset 映射: {cnt:>8,}")

    # ═══ 2. doc_source_entry 整体 ═══
    print("\n── 2. doc_source_entry 总览 ──")
    cur.execute("SELECT COUNT(*) FROM biz.doc_source_entry")
    total_entries = cur.fetchone()[0]
    print(f"  总条目数: {total_entries:>8,}")

    cur.execute("SELECT COUNT(DISTINCT asset_id) FROM biz.doc_source_entry WHERE asset_id IS NOT NULL")
    unique_assets = cur.fetchone()[0]
    print(f"  覆盖资产数: {unique_assets:>8,}")

    # 按来源分组
    print("\n  按 source_code:")
    for sc in ["cmc", "cg", "dl"]:
        cur.execute("SELECT COUNT(*) FROM biz.doc_source_entry WHERE source_code = %s", (sc,))
        cnt = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT asset_id) FROM biz.doc_source_entry WHERE source_code = %s AND asset_id IS NOT NULL", (sc,))
        assets = cur.fetchone()[0]
        print(f"    {sc}: {cnt:>8,} 条, {assets:>6,} 个资产")

    # deep_crawl vs 原始
    cur.execute("SELECT COUNT(*) FROM biz.doc_source_entry WHERE discovered_from LIKE 'deep_crawl:%%'")
    deep_cnt = cur.fetchone()[0]
    orig_cnt = total_entries - deep_cnt
    print(f"\n  原始入口(CMC/CG/DL): {orig_cnt:>8,}")
    cur.execute("SELECT COUNT(DISTINCT asset_id) FROM biz.doc_source_entry WHERE discovered_from NOT LIKE 'deep_crawl:%%' AND asset_id IS NOT NULL")
    print(f"    覆盖资产: {cur.fetchone()[0]:>8,}")
    print(f"  deep_crawl 发现: {deep_cnt:>8,}")
    cur.execute("SELECT COUNT(DISTINCT asset_id) FROM biz.doc_source_entry WHERE discovered_from LIKE 'deep_crawl:%%' AND asset_id IS NOT NULL")
    print(f"    覆盖资产: {cur.fetchone()[0]:>8,}")

    # ═══ 3. 每条链路的"投入产出" ═══
    print("\n── 3. 每条链路的投入产出比 ──")
    # 原始入口中，有多少已经被 deep crawl 过
    cur.execute("SELECT COUNT(*) FROM biz.doc_source_entry WHERE discovered_from NOT LIKE 'deep_crawl:%%' AND deep_crawled_at IS NOT NULL")
    crawled = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM biz.doc_source_entry WHERE discovered_from NOT LIKE 'deep_crawl:%%' AND deep_crawled_at IS NULL")
    uncrawled = cur.fetchone()[0]
    print(f"  原始入口已爬取: {crawled:>8,} ({crawled/orig_cnt*100:.1f}%)" if orig_cnt else "  N/A")
    print(f"  原始入口待爬取: {uncrawled:>8,} ({uncrawled/orig_cnt*100:.1f}%)" if orig_cnt else "  N/A")

    # deep_crawl 发现率（从每个入口发现了多少新链接）
    if crawled > 0:
        ratio = deep_cnt / crawled
        print(f"  平均每个入口发现: {ratio:.1f} 条新链接")
    else:
        print(f"  平均每个入口发现: N/A (无已爬取入口)")

    # 按 entry_type 的待爬取情况
    print("\n  原始入口 按 entry_type (待爬取):")
    for et in ["docs", "official_website", "github", "medium", "other"]:
        cur.execute(
            "SELECT COUNT(*) FROM biz.doc_source_entry "
            "WHERE discovered_from NOT LIKE 'deep_crawl:%%' "
            "AND entry_type = %s AND deep_crawled_at IS NULL", (et,)
        )
        cnt = cur.fetchone()[0]
        print(f"    {et}: {cnt:>8,}")

    # ═══ 4. AI 噪声清理进度 ═══
    print("\n── 4. AI 噪声清理进度 ──")
    cur.execute("SELECT COUNT(*) FROM biz.doc_source_entry WHERE ai_noise_checked_at IS NOT NULL")
    ai_checked = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM biz.doc_source_entry WHERE discovered_from LIKE 'deep_crawl:%%' AND ai_noise_checked_at IS NULL")
    ai_unchecked = cur.fetchone()[0]
    print(f"  AI 已标记(相关): {ai_checked:>8,}")
    print(f"  deep_crawl 未检查: {ai_unchecked:>8,}")
    if deep_cnt > 0:
        print(f"  deep_crawl 已处理率: {ai_checked/deep_cnt*100:.1f}%")

    # ═══ 5. 每资产条目分布 ═══
    print("\n── 5. 每资产文档链接数分布 ──")
    cur.execute("""
        SELECT bucket, COUNT(*) FROM (
            SELECT asset_id, COUNT(*) AS cnt,
                CASE
                    WHEN COUNT(*) <= 3 THEN '1-3'
                    WHEN COUNT(*) <= 10 THEN '4-10'
                    WHEN COUNT(*) <= 20 THEN '11-20'
                    WHEN COUNT(*) <= 50 THEN '21-50'
                    WHEN COUNT(*) <= 100 THEN '51-100'
                    ELSE '100+'
                END AS bucket
            FROM biz.doc_source_entry
            WHERE asset_id IS NOT NULL
            GROUP BY asset_id
        ) sub GROUP BY bucket
        ORDER BY MIN(CASE bucket
            WHEN '1-3' THEN 1 WHEN '4-10' THEN 2 WHEN '11-20' THEN 3
            WHEN '21-50' THEN 4 WHEN '51-100' THEN 5 ELSE 6 END)
    """)
    for bucket, cnt in cur.fetchall():
        bar = "█" * (cnt // max(1, unique_assets // 40))
        print(f"  {bucket:>6} 条: {cnt:>6,} 个资产  {bar}")

    # 中位数、平均数
    cur.execute("""
        SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY cnt)::int,
               PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY cnt)::int,
               ROUND(AVG(cnt), 1)
        FROM (SELECT COUNT(*) AS cnt FROM biz.doc_source_entry
              WHERE asset_id IS NOT NULL GROUP BY asset_id) sub
    """)
    p50, p90, avg = cur.fetchone()
    print(f"  中位数: {p50} 条/资产, P90: {p90} 条/资产, 平均数: {avg} 条/资产")

    # ═══ 6. 抽样：低条目数的资产 ═══
    print("\n── 6. 条目最少的资产抽样 (前10) ──")
    cur.execute("""
        SELECT a.canonical_symbol, a.canonical_name, COUNT(*) AS cnt
        FROM biz.doc_source_entry e
        JOIN core.asset a ON a.asset_id = e.asset_id
        WHERE e.asset_id IS NOT NULL
        GROUP BY a.asset_id, a.canonical_symbol, a.canonical_name
        ORDER BY cnt ASC, a.canonical_symbol
        LIMIT 10
    """)
    for sym, name, cnt in cur.fetchall():
        print(f"  {sym or '(无)' :>8}  {name[:30]:<30}  {cnt:>4} 条")

    # ═══ 7. 条目最多的资产 ═══
    print("\n── 7. 条目最多的资产 (前10) ──")
    cur.execute("""
        SELECT a.canonical_symbol, a.canonical_name, COUNT(*) AS cnt
        FROM biz.doc_source_entry e
        JOIN core.asset a ON a.asset_id = e.asset_id
        WHERE e.asset_id IS NOT NULL
        GROUP BY a.asset_id, a.canonical_symbol, a.canonical_name
        ORDER BY cnt DESC
        LIMIT 10
    """)
    for sym, name, cnt in cur.fetchall():
        print(f"  {sym or '(无)' :>8}  {name[:30]:<30}  {cnt:>5} 条")

    # ═══ 8. 哪些资产有原始入口但没 deep_crawl 结果 ═══
    print("\n── 8. 有原始入口但 deep_crawl 产出为0的资产数 ──")
    cur.execute("""
        SELECT COUNT(DISTINCT asset_id) FROM biz.doc_source_entry
        WHERE asset_id IS NOT NULL
          AND discovered_from NOT LIKE 'deep_crawl:%%'
          AND asset_id NOT IN (
              SELECT DISTINCT asset_id FROM biz.doc_source_entry
              WHERE discovered_from LIKE 'deep_crawl:%%'
          )
    """)
    no_deep = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT asset_id) FROM biz.doc_source_entry WHERE asset_id IS NOT NULL")
    total_with_entries = cur.fetchone()[0]
    print(f"  无 deep_crawl 产出: {no_deep} / {total_with_entries} ({no_deep/total_with_entries*100:.1f}%)" if total_with_entries else "  N/A")
    print(f"  原因: 原始入口未爬取 或 爬了但页面里没文档链接")

    # ═══ 9. 建议 ═══
    print("\n" + "=" * 65)
    print("  诊断结论与建议")
    print("=" * 65)
    if uncrawled > 0:
        print(f"  🔴 还有 {uncrawled:,} 个原始入口未爬取 → 运行 B2 深度文档发现")
    else:
        print(f"  ✅ 原始入口已全部爬取")
    if ai_unchecked > 0:
        print(f"  🟡 还有 {ai_unchecked:,} 条 deep_crawl 链接未做 AI 噪声检查")
    else:
        print(f"  ✅ AI 噪声检查已完成")
    if deep_cnt > 0 and crawled > 0 and deep_cnt / crawled < 1.0:
        print(f"  ⚠️ 每个入口平均只发现 {deep_cnt/crawled:.1f} 条新链接，产出较低")
        print(f"     → 可能是大部分项目网站简单、或很多页面不是 HTML")
    if avg < 5:
        print(f"  ⚠️ 平均每资产仅 {avg} 条链接，数据量偏少")
        print(f"     → 建议检查 CMC/CG/DL 数据源是否有足够的 URL 输入")
    print(f"\n  📊 整体评估: {total_entries:,} 条链接覆盖 {unique_assets:,} 个资产")
    print(f"     原始 {orig_cnt:,} + deep_crawl {deep_cnt:,}")

    conn.close()
    print("\n诊断完成。")


if __name__ == "__main__":
    run()
