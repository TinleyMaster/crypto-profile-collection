"""用 Playwright 测试 Etherscan 标签页。"""
import sys
sys.path.insert(0, 'scripts/src')

from playwright.sync_api import sync_playwright

URL = "https://etherscan.io/accounts/label/Kraken"

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=False,  # 有头模式，更容易过 Cloudflare
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        locale="en-US",
        viewport={"width": 1280, "height": 800},
    )
    page = context.new_page()
    print(f"打开: {URL}")
    page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    print(f"页面标题: {page.title()}")

    # 等待表格出现
    try:
        page.wait_for_selector("table", timeout=15000)
        print("✅ 找到表格")

        # 提取前几行数据
        rows = page.query_selector_all("table tbody tr")
        print(f"表格行数: {len(rows)}")

        # 打印表头
        headers = [th.inner_text().strip() for th in page.query_selector_all("table thead th")]
        print(f"表头: {headers}")

        # 打印前 5 行
        for i, row in enumerate(rows[:5]):
            cols = [td.inner_text().strip() for td in row.query_selector_all("td")]
            print(f"  行{i+1}: {cols[:4]}")

        # 看看有没有分页
        pagination = page.query_selector(".pagination")
        if pagination:
            page_text = pagination.inner_text().strip()
            print(f"分页: {page_text[:100]}")

    except Exception as e:
        print(f"❌ 找不到表格: {e}")
        print("页面内容前500字:")
        print(page.inner_text("body")[:500])

    browser.close()
