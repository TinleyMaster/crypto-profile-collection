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

    # ═══ 2.5. 无入口资产分析 ═══
    cur.execute("SELECT COUNT(*) FROM core.asset")
    total_assets = cur.fetchone()[0]
    no_entry = total_assets - unique_assets
    print(f"\n── 2.5. 无任何文档入口的资产 ──")
    print(f"  资产总数: {total_assets:>8,}")
    print(f"  有 doc_source_entry: {unique_assets:>8,} ({unique_assets/total_assets*100:.1f}%)")
    print(f"  无 doc_source_entry: {no_entry:>8,} ({no_entry/total_assets*100:.1f}%)")
    print(f"  原因: 资产的 links 里没有官网/文档/GitHub 等 URL")

    # 各 source 的资产覆盖率
    print(f"\n  各数据源资产覆盖率:")
    for sc, schema, table in [
        ("cmc", "src_cmc", "cmc_asset_map"),
        ("cg", "src_cg", "coin_list"),
        ("dl", "src_dl", "protocol_list"),
    ]:
        cur.execute(f"SELECT COUNT(*) FROM {schema}.{table}")
        src_total = cur.fetchone()[0]
        cur.execute(
            f"SELECT COUNT(DISTINCT asm.asset_id) "
            f"FROM core.asset_source_map asm "
            f"JOIN biz.doc_source_entry e ON e.asset_id = asm.asset_id AND e.source_code = %s "
            f"WHERE asm.source_code = %s",
            (sc, sc),
        )
        with_entry = cur.fetchone()[0]
        pct = with_entry / src_total * 100 if src_total else 0
        print(f"    {sc}: {with_entry:>5,} / {src_total:>5,} ({pct:.1f}%)")

    # 搜索常见代币（RWA 等）看是否有入口
    sample_symbols = ["RWA", "BTC", "ETH", "SOL", "DOGE", "PEPE"]
    print(f"\n  样本代币入口数:")
    for sym in sample_symbols:
        cur.execute("SELECT asset_id, canonical_symbol, canonical_name FROM core.asset WHERE canonical_symbol ILIKE %s LIMIT 1", (sym,))
        row = cur.fetchone()
        if not row:
            print(f"    {sym:<8}  (资产库中不存在)")
            continue
        aid, s, n = row
        cur.execute("SELECT COUNT(*) FROM biz.doc_source_entry WHERE asset_id = %s AND entity_type = 'asset'", (aid,))
        cnt = cur.fetchone()[0]
        if cnt == 0:
            cur.execute(
                "SELECT COUNT(*) FROM core.asset_source_map WHERE asset_id = %s", (aid,)
            )
            scnt = cur.fetchone()[0]
            print(f"    {s:<8}  {n[:24]:<24}  入口: 0 条 (在 {scnt} 个数据源里)")
        else:
            cur.execute(
                "SELECT entry_type, COUNT(*) FROM biz.doc_source_entry "
                "WHERE asset_id = %s AND entity_type = 'asset' "
                "GROUP BY entry_type ORDER BY COUNT(*) DESC",
                (aid,),
            )
            types = cur.fetchall()
            type_str = ", ".join(f"{t}:{c}" for t, c in types[:4])
            print(f"    {s:<8}  {n[:24]:<24}  入口: {cnt:>4} 条 ({type_str})")

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
    all_types = [
        "docs",
        "official_website",
        "docs_portal",
        "medium",
        "announcement",
        "github",
        "other",
        "twitter",
        "telegram",
        "reddit",
        "facebook",
    ]
    uncrawled_by_type = {}
    for et in all_types:
        cur.execute(
            "SELECT COUNT(*) FROM biz.doc_source_entry "
            "WHERE discovered_from NOT LIKE 'deep_crawl:%%' "
            "AND entry_type = %s AND deep_crawled_at IS NULL", (et,)
        )
        cnt = cur.fetchone()[0]
        uncrawled_by_type[et] = cnt
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

    # ═══ 4.5. deep_crawl 条目资产关联情况 ═══
    print("\n── 4.5. deep_crawl 条目资产关联 ──")
    cur.execute("SELECT COUNT(*) FROM biz.doc_source_entry WHERE discovered_from LIKE 'deep_crawl:%%' AND asset_id IS NULL")
    deep_null_asset = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM biz.doc_source_entry WHERE discovered_from LIKE 'deep_crawl:%%' AND asset_id IS NOT NULL")
    deep_has_asset = cur.fetchone()[0]
    deep_total = deep_null_asset + deep_has_asset
    if deep_total > 0:
        print(f"  有 asset_id: {deep_has_asset:>8,} ({deep_has_asset/deep_total*100:.1f}%)")
        print(f"  无 asset_id: {deep_null_asset:>8,} ({deep_null_asset/deep_total*100:.1f}%)")
        if deep_null_asset > 0:
            print(f"  ⚠️ {deep_null_asset:,} 条 deep_crawl 链接无 asset_id，无法按代币查询！")
        else:
            print(f"  ✅ 全部 deep_crawl 链接已关联资产")
    # 按域名看资产关联：审计类域名是否关联了多个不同资产（交叉污染风险）
    print("\n  审计/第三方域名 → 关联资产数（交叉污染风险）:")
    cur.execute("""
        SELECT domain, asset_count,
               CASE WHEN asset_count > 50 THEN '⚠️ 高风险(>50个资产)' ELSE '✅ 正常' END AS risk
        FROM (
            SELECT LOWER(SPLIT_PART(SPLIT_PART(entry_url, '/', 3), '?', 1)) AS domain,
                   COUNT(DISTINCT asset_id) AS asset_count
            FROM biz.doc_source_entry
            WHERE discovered_from LIKE 'deep_crawl:%%'
              AND asset_id IS NOT NULL
            GROUP BY LOWER(SPLIT_PART(SPLIT_PART(entry_url, '/', 3), '?', 1))
            ORDER BY asset_count DESC
            LIMIT 15
        ) sub
    """)
    for domain, ac, risk in cur.fetchall():
        print(f"    {domain:<40} {ac:>5} 个资产  {risk}")

    # deep_crawl 条目按域名 + 无资产的情况
    print("\n  deep_crawl 条目 TOP 域名（含无 asset_id）:")
    cur.execute("""
        SELECT COALESCE(LOWER(SPLIT_PART(SPLIT_PART(entry_url, '/', 3), '?', 1)), '(空)') AS domain,
               COUNT(*) AS total,
               COUNT(asset_id) AS with_asset,
               COUNT(*) - COUNT(asset_id) AS no_asset
        FROM biz.doc_source_entry
        WHERE discovered_from LIKE 'deep_crawl:%%'
        GROUP BY domain
        ORDER BY total DESC
        LIMIT 15
    """)
    for domain, total, with_a, no_a in cur.fetchall():
        flag = " ⚠️" if no_a > 0 else ""
        print(f"    {domain:<40} {total:>6,} 条, 有资产:{with_a:>6,}, 无资产:{no_a:>5,}{flag}")

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

    # ═══ 7.5. 高条目资产污染溯源 ═══
    print("\n── 7.5. 高条目资产污染溯源（TOP 5）──")
    cur.execute("""
        WITH top_assets AS (
            SELECT asset_id FROM biz.doc_source_entry
            WHERE asset_id IS NOT NULL
            GROUP BY asset_id ORDER BY COUNT(*) DESC LIMIT 5
        )
        SELECT a.canonical_symbol,
               e.entry_url,
               e.entry_type,
               COUNT(d.entry_id) AS spawned
        FROM biz.doc_source_entry e
        JOIN core.asset a ON a.asset_id = e.asset_id
        JOIN top_assets ta ON ta.asset_id = e.asset_id
        LEFT JOIN biz.doc_source_entry d
            ON d.discovered_from = 'deep_crawl:' || LEFT(e.entry_url, 50)
        WHERE e.discovered_from NOT LIKE 'deep_crawl:%%'
        GROUP BY a.canonical_symbol, e.entry_url, e.entry_type
        ORDER BY spawned DESC
        LIMIT 30
    """)
    print("  种子入口 → 产出的 deep_crawl 子链接数:")
    for sym, url, etype, spawned in cur.fetchall():
        bar = "⚠️" if (spawned or 0) > 500 else ""
        print(f"  {sym:<8} [{etype:<16}] → {spawned:>7,} 条子链接 {bar}"
              f"\n         {url[:90]}")

    # 每个高条目资产的 deep_crawl 域名分布
    print("\n  各资产 deep_crawl 条目域名 TOP 3:")
    cur.execute("""
        SELECT a.asset_id, a.canonical_symbol FROM core.asset a
        WHERE a.asset_id IN (
            SELECT asset_id FROM biz.doc_source_entry WHERE asset_id IS NOT NULL
            GROUP BY asset_id ORDER BY COUNT(*) DESC LIMIT 5
        )
    """)
    top_asset_ids = cur.fetchall()
    for asset_id, sym in top_asset_ids:
        cur.execute("""
            SELECT LOWER(SPLIT_PART(SPLIT_PART(entry_url, '/', 3), '?', 1)) AS domain,
                   COUNT(*) AS cnt
            FROM biz.doc_source_entry
            WHERE asset_id = %s AND discovered_from LIKE 'deep_crawl:%%'
            GROUP BY domain ORDER BY cnt DESC LIMIT 3
        """, (asset_id,))
        domains = cur.fetchall()
        domain_str = " | ".join(f"{d}({c:,})" for d, c in domains)
        print(f"  {sym:<8} {domain_str}")

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

    # 按类型分类待爬取原始入口
    crawlable = 0  # 值得 B2 深度爬取的
    skip_github = 0
    skip_other = 0
    skip_social = 0
    for et in ["docs", "official_website", "docs_portal", "medium", "announcement"]:
        cnt = uncrawled_by_type.get(et, 0)
        crawlable += cnt
    skip_github = uncrawled_by_type.get("github", 0)
    skip_other = uncrawled_by_type.get("other", 0)
    for et in ["twitter", "telegram", "reddit", "facebook"]:
        skip_social += uncrawled_by_type.get(et, 0)

    if crawlable > 0:
        print(f"  🔴 {crawlable:,} 个文档类入口待爬 (docs/official/docs_portal/medium/announcement) → 运行 B2")
    else:
        print(f"  ✅ 文档类入口已全部爬取")

    if skip_github > 0:
        print(f"  ℹ️ {skip_github:,} 个 github 入口 → 不爬（代码仓库，污染风险高）")
    if skip_other > 0:
        print(f"  ℹ️ {skip_other:,} 个 other 入口 → 不爬（论坛/浏览器等，污染风险高）")
    if skip_social > 0:
        print(f"  ℹ️ {skip_social:,} 个社交入口 (twitter/telegram/reddit/facebook) → 不爬（非文档源）")

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
    if deep_null_asset > 0:
        pct = deep_null_asset / deep_total * 100 if deep_total else 0
        print(f"  🔴 {deep_null_asset:,} 条 deep_crawl 链接无 asset_id ({pct:.1f}%)")
        print(f"     → 这些链接无法按代币查询，需排查 deep_crawl 的 asset_id 继承逻辑")
    print(f"\n  📊 整体评估: {total_entries:,} 条链接覆盖 {unique_assets:,} 个资产")
    print(f"     原始 {orig_cnt:,} + deep_crawl {deep_cnt:,}")

    conn.close()
    print("\n诊断完成。")


if __name__ == "__main__":
    run()
