import psycopg

c = psycopg.connect(
    "postgresql://root:iuU2F8Vx1aj7A6gw3Pd4bH9rG5eL0RyW@43.166.198.83:32405/crypto",
    connect_timeout=10,
)
cur = c.cursor()

cur.execute("SELECT count(*) FROM core.asset")
print("asset count:", cur.fetchone()[0])

cur.execute("""
    EXPLAIN ANALYZE
    SELECT a.asset_id FROM core.asset a
    WHERE a.canonical_symbol ILIKE '%btc%'
       OR a.canonical_name ILIKE '%btc%'
    LIMIT 20
""")
for r in cur.fetchall():
    print(r[0])

c.close()