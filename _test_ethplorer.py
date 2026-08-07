"""测试数据库中 ETH/BSC token 的 Ethplorer 返回情况"""
import psycopg
import requests
import time

conn = psycopg.connect(
    "postgresql://root:iuU2F8Vx1aj7A6gw3Pd4bH9rG5eL0RyW@43.166.198.83:32405/crypto",
    connect_timeout=10,
)
cur = conn.cursor()

# 统计各链合约数量
cur.execute("""
    SELECT chain, COUNT(*) FROM core.asset_contract
    WHERE contract_address IS NOT NULL
    GROUP BY chain ORDER BY COUNT(*) DESC LIMIT 20
""")
print("=== 合约分布 ===")
for row in cur.fetchall():
    print(f"  {row[0]}: {row[1]}")

# 测试 ETH 和 BSC 各 10 个随机 token
for chain, base in [("ethereum", "https://api.ethplorer.io"), ("bsc", "https://api.binplorer.com")]:
    cur.execute("""
        SELECT a.canonical_symbol, ac.contract_address
        FROM core.asset_contract ac
        JOIN core.asset a ON a.asset_id = ac.asset_id
        WHERE ac.chain = %s AND ac.contract_address IS NOT NULL
        ORDER BY RANDOM() LIMIT 10
    """, (chain,))
    
    rows = cur.fetchall()
    ok = 0
    err = 0
    for symbol, addr in rows:
        url = f"{base}/getTopTokenHolders/{addr}?apiKey=freekey&limit=3"
        try:
            r = requests.get(url, timeout=15)
            data = r.json()
            if "error" in data:
                err += 1
                if data["error"].get("code") != 150:
                    print(f"  [{symbol}] {addr[:10]}... -> {data['error']}")
            else:
                ok += 1
            time.sleep(0.25)
        except Exception as e:
            err += 1
            print(f"  [{symbol}] {addr[:10]}... -> EXCEPTION: {e}")
    print(f"\n{chain}: {ok}/{ok+err} OK ({ok/(ok+err)*100:.0f}%)")

conn.close()