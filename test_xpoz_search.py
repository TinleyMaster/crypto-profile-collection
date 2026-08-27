from xpoz import XpozClient

API_KEY = 'K3F454y3n2G4ICF8MEWYL8LAwILAkfBwA4sHsIZP3kyaiBXlkvHfmvMr7qG8JmaUvjakHpk'
client = XpozClient(API_KEY, timeout=30, check_update=False)

# 测试搜索功能
print('=== 测试3: 搜索 $BTC 相关推文 ===')
try:
    result = client.twitter.search_posts(
        query='$BTC lang:en',
        fields=['id', 'text', 'author_username', 'created_at', 'like_count', 'retweet_count'],
        limit=5
    )
    print(f'Success! {len(result.data)} 条')
    for item in result.data[:3]:
        print(f'  @{item.author_username}: {item.text[:80]}...')
except Exception as e:
    print(f'FAIL: {e}')

print()
print('=== 测试4: 搜索 $SOL 相关 ===')
try:
    result = client.twitter.search_posts(
        query='$SOL crypto',
        fields=['id', 'text', 'author_username', 'created_at', 'like_count', 'retweet_count'],
        limit=5
    )
    print(f'Success! {len(result.data)} 条')
    for item in result.data[:3]:
        print(f'  @{item.author_username}: {item.text[:80]}...')
except Exception as e:
    print(f'FAIL: {e}')

client.close()