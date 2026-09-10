"""Etherscan 标签页批量爬虫（Playwright 版）。

用 Playwright 驱动真实 Chrome 浏览器，绕过 Cloudflare，
爬取 Etherscan / BscScan / PolygonScan 等区块浏览器的标签列表页，
提取地址 + 标签名，写入 onchain_address_label 表。

特性：
- 持久化 user_data_dir，过一次 Cloudflare 后复用
- 支持多链（Etherscan 系列）
- 支持分页
- 自动识别交易所标签并分类
- 批量入库，UPSERT 语义

用法：
  python scripts/bin/crawl_explorer_labels.py --chain eth --labels Binance,Kraken,Coinbase
  python scripts/bin/crawl_explorer_labels.py --chain eth --label-list data/etherscan_labels.txt
  python scripts/bin/crawl_explorer_labels.py --chain eth --list   # 列出所有可用标签
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

# 确保能 import 项目模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from playwright.sync_api import sync_playwright, Page, BrowserContext

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection


# ── 链配置 ──────────────────────────────────────────────
CHAIN_CONFIG = {
    "eth": {
        "base_url": "https://etherscan.io",
        "label_list_url": "https://etherscan.io/labelcloud",
        "label_url_template": "https://etherscan.io/accounts/label/{label}",
        "chrome_profile_name": "etherscan-crawler",
    },
    "bsc": {
        "base_url": "https://bscscan.com",
        "label_list_url": "https://bscscan.com/labelcloud",
        "label_url_template": "https://bscscan.com/accounts/label/{label}",
        "chrome_profile_name": "bscscan-crawler",
    },
    "polygon": {
        "base_url": "https://polygonscan.com",
        "label_list_url": "https://polygonscan.com/labelcloud",
        "label_url_template": "https://polygonscan.com/accounts/label/{label}",
        "chrome_profile_name": "polygonscan-crawler",
    },
    "base": {
        "base_url": "https://basescan.org",
        "label_list_url": "https://basescan.org/labelcloud",
        "label_url_template": "https://basescan.org/accounts/label/{label}",
        "chrome_profile_name": "basescan-crawler",
    },
}

# Chrome 可执行文件路径
CHROME_PATH = r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
# Playwright 用户数据根目录
USER_DATA_ROOT = Path.home() / ".trae-cn" / "playwright_profiles"

# 交易所名归一化（和 explorer_label_fetcher.py 保持一致）
EXCHANGE_NAME_MAP = {
    "binance": "Binance", "binance us": "Binance US", "binanceus": "Binance US",
    "coinbase": "Coinbase", "coinbase prime": "Coinbase Prime",
    "okx": "OKX", "okex": "OKX", "kraken": "Kraken", "bybit": "Bybit",
    "kucoin": "KuCoin", "gate": "Gate.io", "gate.io": "Gate.io", "gateio": "Gate.io",
    "huobi": "Huobi", "htx": "HTX", "bitfinex": "Bitfinex", "bitget": "Bitget",
    "mexc": "MEXC", "crypto.com": "Crypto.com", "cryptocom": "Crypto.com",
    "upbit": "Upbit", "bithumb": "Bithumb", "gemini": "Gemini", "bitstamp": "Bitstamp",
    "poloniex": "Poloniex", "deribit": "Deribit", "bitmart": "BitMart", "lbank": "LBank",
    "xt.com": "XT.COM", "xtcom": "XT.COM", "bittrex": "Bittrex", "bitmex": "BitMEX",
    "korbit": "Korbit", "coinone": "Coinone", "ftx": "FTX", "hotbit": "Hotbit",
}

RE_ADDR = re.compile(r"^0x[a-fA-F0-9]{40}$")


def classify_label(label_name: str) -> tuple[str, str]:
    """根据标签名分类，返回 (label_type, display_name)。

    只保留高价值类型：exchange / dex / mev_bot / market_maker / whale / smart_money
    匹配不到返回 ('', '') 表示跳过。
    """
    low = label_name.lower().strip()

    # 1. 交易所匹配
    for kw, normalized in EXCHANGE_NAME_MAP.items():
        if kw in low:
            return "exchange", normalized

    # 2. 其他高价值类型（简单关键词匹配）
    mev_keywords = ["mev", "bot", "flashbot", "jito", "bloxroute"]
    dex_keywords = ["dex", "liquidity pool", "lp ", "uniswap", "sushiswap", "pancakeswap", "curve"]
    mm_keywords = ["market maker", "market-maker", "mm ", "wintermute", "gfr", "cumberland"]
    whale_keywords = ["whale", "巨鲸"]

    for kw in mev_keywords:
        if kw in low:
            return "mev_bot", label_name
    for kw in dex_keywords:
        if kw in low:
            return "dex", label_name
    for kw in mm_keywords:
        if kw in low:
            return "market_maker", label_name
    for kw in whale_keywords:
        if kw in low:
            return "whale", label_name

    return "", ""


def create_context(pw, chain: str, headless: bool = False,
                   cookie_file: str | None = None) -> tuple:
    """创建浏览器上下文。

    - 如果有 cookie_file，用普通 browser + 注入 cookie（更快）
    - 否则用持久化 profile（需要手动过一次 Cloudflare）
    """
    config = CHAIN_CONFIG[chain]

    if cookie_file:
        # 普通模式：启动 browser + context，手动注入 cookie
        browser = pw.chromium.launch(
            executable_path=CHROME_PATH,
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        )

        # 加载 cookie
        import json
        cookie_path = Path(cookie_file)
        if cookie_path.exists():
            with open(cookie_path, "r", encoding="utf-8") as f:
                cookies_raw = json.load(f)
            # 转换为 Playwright cookie 格式
            cookies_pw = []
            for c in cookies_raw:
                pw_cookie = {
                    "name": c["name"],
                    "value": c["value"],
                    "domain": c["domain"],
                    "path": c.get("path", "/"),
                    "secure": c.get("secure", False),
                    "httpOnly": c.get("httpOnly", False),
                }
                if c.get("sameSite") and c["sameSite"] != "unspecified":
                    # Playwright 要求首字母大写: Strict / Lax / None
                    samesite = c["sameSite"]
                    if samesite.lower() == "strict":
                        pw_cookie["sameSite"] = "Strict"
                    elif samesite.lower() == "lax":
                        pw_cookie["sameSite"] = "Lax"
                    elif samesite.lower() == "no_restriction":
                        pw_cookie["sameSite"] = "None"
                    # 其他值就不设了
                if not c.get("session", False) and c.get("expirationDate"):
                    pw_cookie["expires"] = int(c["expirationDate"])
                cookies_pw.append(pw_cookie)
            context.add_cookies(cookies_pw)
            print(f"  已加载 {len(cookies_pw)} 个 cookie")

        return browser, context
    else:
        # 持久化 profile 模式
        user_data_dir = USER_DATA_ROOT / config["chrome_profile_name"]
        user_data_dir.mkdir(parents=True, exist_ok=True)
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            executable_path=CHROME_PATH,
            headless=headless,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        return None, context  # persistent context 没有单独的 browser 对象


def wait_for_table(page: Page, timeout: int = 30000) -> bool:
    """等待页面表格加载完成，遇到 Cloudflare 自动等待。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        title = page.title()
        if "Just a moment" in title or "Security" in title:
            # Cloudflare 验证中，等一下
            time.sleep(2)
            continue
        # 检查表格是否出现
        table = page.query_selector("table tbody")
        if table:
            return True
        time.sleep(1)
    return False


def extract_table_page(page: Page) -> list[dict]:
    """从当前标签页提取一页的地址数据。

    返回: [{address, name_tag}, ...]
    """
    results = []
    rows = page.query_selector_all("table tbody tr")
    for row in rows:
        cols = row.query_selector_all("td")
        if len(cols) < 2:
            continue

        # 地址可能在第一列或者第二列，找 0x 开头的
        address = ""
        name_tag = ""
        for col in cols:
            text = col.inner_text().strip()
            if RE_ADDR.match(text):
                address = text
            # 找 name tag 列（一般包含 "Dep:" "Wallet:" "Hot:" 等或者直接是标签名）
            if not address and "0x" in text and len(text) > 20:
                # 可能是带链接的地址
                addr_match = re.search(r"0x[a-fA-F0-9]{40}", text)
                if addr_match:
                    address = addr_match.group(0)

        # 从第一列的 a 链接里拿地址更靠谱
        first_link = row.query_selector("td a[href*='/address/']")
        if first_link:
            href = first_link.get_attribute("href") or ""
            addr_match = re.search(r"/address/(0x[a-fA-F0-9]{40})", href)
            if addr_match:
                address = addr_match.group(1)
            # 链接文本可能是标签名
            link_text = first_link.inner_text().strip()
            if link_text and not RE_ADDR.match(link_text):
                name_tag = link_text

        # 如果还没找到 name tag，遍历所有列
        if not name_tag:
            for col in cols:
                text = col.inner_text().strip()
                # 排除地址、数量、余额列
                if RE_ADDR.match(text) or re.match(r"^[\d,]+\.?\d*\s*(ETH|BNB|MATIC|USDT|USDC|BTC)?$", text, re.I):
                    continue
                if len(text) > 2 and len(text) < 100:
                    name_tag = text
                    break

        if address:
            results.append({
                "address": address.lower(),
                "name_tag": name_tag,
            })
    return results


def get_total_pages(page: Page) -> int:
    """获取总页数。"""
    # 找分页控件里的最大页码
    pagination = page.query_selector(".pagination")
    if not pagination:
        return 1
    page_nums = pagination.query_selector_all("a.page-link, li.page-item a")
    max_page = 1
    for pn in page_nums:
        text = pn.inner_text().strip()
        if text.isdigit():
            max_page = max(max_page, int(text))
    # 也可能是 "Page 1 of 100" 这种格式
    body_text = page.inner_text("body")
    match = re.search(r"Page\s+\d+\s+of\s+(\d+)", body_text, re.I)
    if match:
        max_page = max(max_page, int(match.group(1)))
    return max_page


def go_to_page(page: Page, page_num: int) -> bool:
    """跳转到指定页码。"""
    # 方法1：直接改 URL 参数
    current_url = page.url
    sep = "&" if "?" in current_url else "?"
    # 移除旧的 p= 参数
    current_url = re.sub(r"[?&]p=\d+", "", current_url)
    sep = "&" if "?" in current_url else "?"
    new_url = f"{current_url}{sep}p={page_num}"
    page.goto(new_url, wait_until="domcontentloaded", timeout=30000)
    return wait_for_table(page, timeout=30000)


def crawl_label(page: Page, chain: str, label_slug: str, max_pages: int = 0) -> list[dict]:
    """爬取单个标签的所有地址。

    返回: [{address, name_tag, label_type, display_name}, ...]
    """
    config = CHAIN_CONFIG[chain]
    url = config["label_url_template"].format(label=label_slug)
    print(f"  打开标签页: {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)

    if not wait_for_table(page, timeout=60000):
        title = page.title()
        print(f"  ⚠️  表格加载失败，页面标题: {title}")
        return []

    # 获取总页数
    total_pages = get_total_pages(page)
    if max_pages and max_pages < total_pages:
        total_pages = max_pages
    print(f"  共 {total_pages} 页")

    all_results = []
    for p in range(1, total_pages + 1):
        if p > 1:
            if not go_to_page(page, p):
                print(f"  ⚠️  第 {p} 页跳转失败，跳过")
                continue

        page_results = extract_table_page(page)
        all_results.extend(page_results)
        print(f"  第 {p}/{total_pages} 页: 提取 {len(page_results)} 条")

        # 礼貌限速
        time.sleep(1)

    # 分类
    label_type, display_name = classify_label(label_slug)
    if not label_type:
        print(f"  ⚠️  标签 '{label_slug}' 不属于高价值类型，跳过")
        return []

    # 给每条结果加上分类信息
    for r in all_results:
        r["label_type"] = label_type
        r["display_name"] = display_name
        r["label_slug"] = label_slug

    return all_results


def list_labels(page: Page, chain: str) -> list[str]:
    """列出所有可用的标签（从 labelcloud 页面）。"""
    config = CHAIN_CONFIG[chain]
    url = config["label_list_url"]
    print(f"打开标签云: {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    time.sleep(3)  # 等页面渲染

    # 提取所有标签链接
    links = page.query_selector_all("a[href*='/accounts/label/']")
    labels = set()
    for link in links:
        href = link.get_attribute("href") or ""
        match = re.search(r"/accounts/label/([^/?#]+)", href)
        if match:
            labels.add(match.group(1))

    return sorted(labels)


def save_to_db(conn, chain: str, results: list[dict], source: str = "explorer_label_page") -> int:
    """批量写入数据库，返回新增数量。"""
    if not results:
        return 0

    inserted = 0
    with conn.cursor() as cur:
        for r in results:
            cur.execute("""
                INSERT INTO biz.onchain_address_label
                    (address, chain, label_type, label_name, display_name,
                     confidence, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (address, chain, label_type, label_name) DO NOTHING
            """, (
                r["address"], chain, r["label_type"],
                r["name_tag"] or r["label_slug"],
                r["display_name"],
                "high", source,  # 标签页官方数据 = high 置信度
            ))
            if cur.rowcount:
                inserted += 1

        # 如果是交易所，也写老表
        for r in results:
            if r["label_type"] != "exchange":
                continue
            cur.execute("""
                INSERT INTO biz.onchain_exchange_wallet
                    (address, exchange_name, chain, label, confidence, source)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (address, chain) DO NOTHING
            """, (
                r["address"], r["display_name"], chain,
                r["name_tag"] or r["label_slug"],
                "high", source,
            ))

    conn.commit()
    return inserted


def main():
    parser = argparse.ArgumentParser(description="Etherscan 系列标签页爬虫")
    parser.add_argument("--chain", default="eth", choices=list(CHAIN_CONFIG.keys()),
                        help="要爬的链")
    parser.add_argument("--labels", default="",
                        help="逗号分隔的标签 slug 列表，如 Binance,Kraken,Coinbase")
    parser.add_argument("--max-pages", type=int, default=0,
                        help="每个标签最多爬几页（0=全部）")
    parser.add_argument("--list", action="store_true",
                        help="列出所有可用标签后退出")
    parser.add_argument("--headless", action="store_true",
                        help="无头模式（默认有头，方便过 Cloudflare）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只爬不写库")
    parser.add_argument("--cookies", default="",
                        help="cookie JSON 文件路径（从浏览器导出，用于绕过 Cloudflare）")
    args = parser.parse_args()

    chain = args.chain
    settings = get_settings(require_database=True)

    with sync_playwright() as pw:
        cookie_info = f"，cookie: {args.cookies}" if args.cookies else ""
        print(f"启动 Chrome{cookie_info}...")
        browser, context = create_context(pw, chain, headless=args.headless,
                                          cookie_file=args.cookies or None)
        page = context.pages[0] if context.pages else context.new_page()

        if args.list:
            labels = list_labels(page, chain)
            print(f"\n共 {len(labels)} 个标签:")
            for lbl in labels:
                print(f"  {lbl}")
            context.close()
            if browser:
                browser.close()
            return

        # 解析标签列表
        if args.labels:
            label_list = [l.strip() for l in args.labels.split(",") if l.strip()]
        else:
            print("错误: 请指定 --labels 或 --list")
            context.close()
            if browser:
                browser.close()
            sys.exit(1)

        print(f"\n链: {chain}")
        print(f"标签: {label_list}")
        print(f"模式: {'dry-run（只爬不写）' if args.dry_run else '正式写入'}\n")

        all_results = []
        for i, label in enumerate(label_list):
            print(f"[{i+1}/{len(label_list)}] 爬取标签: {label}")
            try:
                results = crawl_label(page, chain, label, max_pages=args.max_pages)
                print(f"  ✅ 共提取 {len(results)} 条地址")
                all_results.extend(results)
            except Exception as e:
                print(f"  ❌ 失败: {e}")

            time.sleep(2)  # 标签间间隔

        print(f"\n{'─'*50}")
        print(f"总计提取: {len(all_results)} 条地址")

        if not args.dry_run and all_results:
            with get_connection(settings.database_url) as conn:
                inserted = save_to_db(conn, chain, all_results)
                print(f"写入数据库: 新增 {inserted} 条")

        context.close()
        if browser:
            browser.close()


if __name__ == "__main__":
    main()
