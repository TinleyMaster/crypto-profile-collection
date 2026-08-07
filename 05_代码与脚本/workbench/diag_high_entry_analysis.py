"""高条目资产污染溯源分析"""
import psycopg
import psycopg.rows

DB = 'postgresql://root:iuU2F8Vx1aj7A6gw3Pd4bH9rG5eL0RyW@43.166.198.83:32405/crypto'

conn = psycopg.connect(DB)
cur = conn.cursor(row_factory=psycopg.rows.dict_row)

# 1. 高条目资产概览
print('=' * 80)
print('高条目资产(>500) 按来源分析')
print('=' * 80)
cur.execute("""
SELECT a.asset_id, a.canonical_symbol, a.canonical_name,
       COUNT(*) AS total,
       COUNT(*) FILTER (WHERE discovered_from LIKE 'deep_crawl:%%') AS deep_crawl,
       COUNT(*) FILTER (WHERE discovered_from NOT LIKE 'deep_crawl:%%') AS original,
       COUNT(DISTINCT SUBSTRING(entry_url FROM 'https?://([^/]+)')) AS unique_domains
FROM biz.doc_source_entry dse
INNER JOIN core.asset a ON a.asset_id = dse.asset_id
WHERE dse.entity_type = 'asset'
GROUP BY a.asset_id, a.canonical_symbol, a.canonical_name
HAVING COUNT(*) > 500
ORDER BY total DESC
""")
rows = cur.fetchall()
header = "{:>8s}  {:20s}  {:>6s}  {:>6s}  {:>5s}  {:>7s}".format(
    'symbol', 'name', 'total', 'dc', 'orig', 'domains')
print(header)
print('-' * len(header))
for r in rows:
    print("{:>8s}  {:20s}  {:6d}  {:6d}  {:5d}  {:7d}".format(
        r['canonical_symbol'], r['canonical_name'][:20],
        r['total'], r['deep_crawl'], r['original'], r['unique_domains']))

# 2. TOP 5 域名分布
print()
print('=' * 80)
print('TOP 5 高条目资产 - 域名分布')
print('=' * 80)

top5_symbols = ['AUDT', 'VKA', 'DMD', 'TIA.N', 'PROS']
for sym in top5_symbols:
    cur.execute("SELECT asset_id FROM core.asset WHERE canonical_symbol = %s", (sym,))
    row = cur.fetchone()
    if not row:
        continue
    aid = row['asset_id']
    
    cur.execute("""
        SELECT SUBSTRING(entry_url FROM 'https?://([^/]+)') AS domain,
               COUNT(*) AS cnt,
               COUNT(*) FILTER (WHERE discovered_from LIKE 'deep_crawl:%%') AS dc_cnt
        FROM biz.doc_source_entry
        WHERE asset_id = %s AND entity_type = 'asset'
        GROUP BY domain
        ORDER BY cnt DESC
        LIMIT 15
    """, (aid,))
    print('\n--- {} (asset_id={}) ---'.format(sym, aid))
    for r_domain in cur.fetchall():
        domain = r_domain['domain'] or '(unknown)'
        cnt = r_domain['cnt']
        dc = r_domain['dc_cnt']
        bar = '#' * min(cnt // 50, 50)
        print("  {:45s}  cnt={:5d}  dc={:5d}  {}".format(domain, cnt, dc, bar))

# 3. AUDT 深度分析
print()
print('=' * 80)
print('AUDT - 污染来源链分析')
print('=' * 80)
cur.execute("SELECT asset_id FROM core.asset WHERE canonical_symbol = 'AUDT'")
aid = cur.fetchone()['asset_id']

cur.execute("""
    SELECT discovered_from, COUNT(*) AS cnt
    FROM biz.doc_source_entry
    WHERE asset_id = %s AND entity_type = 'asset'
      AND discovered_from LIKE 'deep_crawl:%%'
    GROUP BY discovered_from
    ORDER BY cnt DESC
    LIMIT 20
""", (aid,))
print('\nAUDT deep_crawl 来源分布:')
for r in cur.fetchall():
    src = r['discovered_from'][:80]
    print("  {:80s}  cnt={:5d}".format(src, r['cnt']))

print('\nAUDT entry_type 分布:')
cur.execute("""
    SELECT entry_type, COUNT(*) AS cnt
    FROM biz.doc_source_entry
    WHERE asset_id = %s AND entity_type = 'asset'
    GROUP BY entry_type
    ORDER BY cnt DESC
""", (aid,))
for r in cur.fetchall():
    print("  {:25s}  cnt={:5d}".format(r['entry_type'], r['cnt']))

# 4. 跨资产污染域名
print()
print('=' * 80)
print('跨资产污染域名 (>10 资产关联)')
print('=' * 80)
cur.execute("""
    SELECT SUBSTRING(entry_url FROM 'https?://([^/]+)') AS domain,
           COUNT(DISTINCT asset_id) AS asset_count,
           SUM(cnt) AS total_entries
    FROM (
        SELECT asset_id, entry_url, COUNT(*) AS cnt
        FROM biz.doc_source_entry
        WHERE entity_type = 'asset' AND discovered_from LIKE 'deep_crawl:%%'
        GROUP BY asset_id, entry_url
    ) t
    GROUP BY domain
    HAVING COUNT(DISTINCT asset_id) > 10
    ORDER BY asset_count DESC
    LIMIT 30
""")
print('domain'.ljust(45), 'assets'.rjust(7), 'entries'.rjust(8))
print('-' * 65)
for r in cur.fetchall():
    domain = str(r['domain'] or '(unknown)')
    ac = r['asset_count'] or 0
    te = r['total_entries'] or 0
    print(domain.ljust(45), str(ac).rjust(7), str(te).rjust(8))

# 5. PROS 特殊分析 - 为什么域名分布只有9条
print()
print('=' * 80)
print('PROS - 域名提取异常分析')
print('=' * 80)
cur.execute("SELECT asset_id FROM core.asset WHERE canonical_symbol = 'PROS'")
aid = cur.fetchone()['asset_id']

cur.execute("""
    SELECT entry_url, COUNT(*) AS cnt
    FROM biz.doc_source_entry
    WHERE asset_id = %s AND entity_type = 'asset'
    GROUP BY entry_url
    ORDER BY cnt DESC
    LIMIT 10
""", (aid,))
print('\nPROS top URLs:')
for r in cur.fetchall():
    url = r['entry_url'][:100]
    print("  cnt={:4d}  {}".format(r['cnt'], url))

# 6. AUDT 原始种子链接
print()
print('=' * 80)
print('AUDT - 原始种子链接（非 deep_crawl）')
print('=' * 80)
cur.execute("""
    SELECT entry_url, source_code, entry_type, discovered_from
    FROM biz.doc_source_entry
    WHERE asset_id = %s AND entity_type = 'asset'
      AND discovered_from NOT LIKE 'deep_crawl:%%'
    ORDER BY entry_type
""", (aid,))
rows = cur.fetchall()
print('共 {} 条原始种子链接'.format(len(rows)))
for r in rows[:30]:
    print("  [{:8s}] [{:20s}] {}".format(
        r['source_code'], r['entry_type'], r['entry_url'][:80]))

conn.close()