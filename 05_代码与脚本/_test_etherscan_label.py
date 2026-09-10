"""测试 Etherscan 标签页能否爬取。"""
import sys
sys.path.insert(0, 'scripts/src')

import requests
from bs4 import BeautifulSoup

PROXY = "http://127.0.0.1:7890"
URL = "https://etherscan.io/accounts/label/Kraken"

session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml",
})
session.proxies = {"http": PROXY, "https": PROXY}

print(f"请求: {URL}")
resp = session.get(URL, timeout=30)
print(f"状态码: {resp.status_code}")
print(f"内容长度: {len(resp.text):,} bytes")

soup = BeautifulSoup(resp.text, "html.parser")

# 找表格
table = soup.find("table")
if table:
    print(f"\n找到表格，分析内容...")
    rows = table.find_all("tr")
    print(f"表格行数: {len(rows)}")
    # 打印表头
    if rows:
        headers = [th.get_text(strip=True) for th in rows[0].find_all("th")]
        print(f"表头: {headers}")
    # 打印前 5 行数据
    for i, row in enumerate(rows[1:6]):
        cols = [td.get_text(strip=True) for td in row.find_all("td")]
        print(f"  行{i+1}: {cols[:4]}")
else:
    print("\n没找到 table 标签，看看页面有啥...")
    # 打印页面标题
    title = soup.find("title")
    if title:
        print(f"页面标题: {title.get_text()}")
    # 看看有没有 h1/h2
    for tag in soup.find_all(["h1", "h2", "h3"])[:5]:
        print(f"  {tag.name}: {tag.get_text(strip=True)[:80]}")
    # 看看 body 里的文本前 500 字
    body = soup.find("body")
    if body:
        text = body.get_text(strip=True)[:500]
        print(f"\nbody 文本前500字:\n{text}")
