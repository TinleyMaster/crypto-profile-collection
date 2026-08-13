"""
链上代币持仓分布快照：从区块浏览器爬取持币地址分布、集中度、CEX标签。

BSCScan 已无免费 API，改为网页 HTML 解析。

用法:
    python phase_chain_holder_snapshot.py --asset-id 9052 --chain bsc
    python phase_chain_holder_snapshot.py --contract 0xcf3232B85b43BCa90E51D38cc06Cc8bB8C8A3E36 --chain bsc
    python phase_chain_holder_snapshot.py --asset-id 9052 --chain bsc --save
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(line_buffering=True)

import psycopg
import psycopg.rows
import requests
from bs4 import BeautifulSoup

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection


# ── 配置 ──────────────────────────────────────────────────

EXPLORER_URLS = {
    "bsc": "https://bscscan.com",
    "eth": "https://etherscan.io",
    "polygon": "https://polygonscan.com",
    "arb": "https://arbiscan.io",
    "opt": "https://optimistic.etherscan.io",
    "base": "https://basescan.org",
    "avax": "https://snowtrace.io",
}

# 链名别名：数据库用完整名（ethereum/arbitrum/avalanche），脚本用简称
CHAIN_ALIASES = {
    "ethereum": "eth",
    "eth": "eth",
    "bsc": "bsc",
    "bnb": "bsc",
    "binance": "bsc",
    "polygon": "polygon",
    "matic": "polygon",
    "arbitrum": "arb",
    "arb": "arb",
    "optimism": "opt",
    "op": "opt",
    "base": "base",
    "avalanche": "avax",
    "avax": "avax",
}

# 浏览器简称 → 数据库完整名列表（反查合约地址时兼容多种命名）
CHAIN_DB_NAMES = {
    "eth": ("ethereum", "eth"),
    "bsc": ("bsc", "bnb", "binance"),
    "polygon": ("polygon", "matic"),
    "arb": ("arbitrum", "arb"),
    "opt": ("optimism", "op"),
    "base": ("base",),
    "avax": ("avalanche", "avax"),
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

RE_PCT = re.compile(r'([\d.]+)%')
RE_NUMBER = re.compile(r'([\d,]+\.?\d*)')


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="代币持仓分布快照")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--asset-id", "--asset_id", type=int, dest="asset_id")
    g.add_argument("--contract", type=str, help="合约地址")
    p.add_argument("--chain", type=str, default="bsc",
                   help="链简称: bsc/eth/polygon/arb/opt/base/avax")
    p.add_argument("--save", action="store_true", help="写入数据库")
    p.add_argument("--force", action="store_true", help="忽略缓存强制爬取（兼容上层调用）")
    p.add_argument("--holders-limit", type=int, default=50,
                   help="最多解析多少持币地址")
    return p


# ── 合约地址解析 ──────────────────────────────────────────

def resolve_contract(conn, asset_id: int | None, contract_address: str | None,
                     chain: str) -> dict | None:
    """获取合约地址和资产信息。chain 参数已归一化为浏览器简称。"""
    # 归一化 chain 对应的数据库完整名列表，用于反查
    db_names = CHAIN_DB_NAMES.get(chain, (chain,))
    placeholders = ",".join(["%s"] * len(db_names))

    if contract_address:
        # 用合约地址反查 asset_id
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                f"""SELECT a.asset_id, a.canonical_symbol AS symbol,
                          a.canonical_name AS name
                   FROM core.asset_contract c
                   JOIN core.asset a ON a.asset_id = c.asset_id
                   WHERE c.contract_address = %s AND c.chain IN ({placeholders})""",
                (contract_address.lower(), *db_names),
            )
            row = cur.fetchone()
            if row:
                row["contract_address"] = contract_address.lower()
                row["chain"] = chain
            return row

    if asset_id:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                f"""SELECT a.asset_id, a.canonical_symbol AS symbol,
                          a.canonical_name AS name,
                          c.contract_address, c.chain
                   FROM core.asset a
                   LEFT JOIN core.asset_contract c
                       ON c.asset_id = a.asset_id AND c.chain IN ({placeholders})
                   WHERE a.asset_id = %s
                   ORDER BY c.contract_address NULLS LAST
                   LIMIT 1""",
                (*db_names, asset_id),
            )
            row = cur.fetchone()
        if row and not row["contract_address"]:
            print(f"  资产 {asset_id} 在 {chain} 上无合约地址")
            return None
        return row

    return None


# ── BSCScan 网页解析 ─────────────────────────────────────

def _parse_pct(s: str) -> float | None:
    m = RE_PCT.search(s)
    return float(m.group(1)) if m else None


def _parse_number(s: str) -> str | None:
    m = RE_NUMBER.search(s)
    return m.group(1).replace(",", "") if m else None


def _parse_token_amount(s: str) -> float | None:
    """解析带单位的代币数量，如 '1.04 B', '41.09 M', '355,242.71'。"""
    m = re.search(r'([\d,]+\.?\d*)\s*([BKMT])?', s or "", re.IGNORECASE)
    if not m:
        return None
    val = float(m.group(1).replace(",", ""))
    mult = {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}
    return val * mult.get((m.group(2) or "").upper(), 1.0)


def _fetch_html(url: str, timeout: int = 20, retries: int = 5) -> str | None:
    """带重试的页面抓取，返回 HTML 文本（失败返回 None）。

    区块浏览器对国内网络偶发 ConnectionResetError，需多次重试 + 递增间隔。
    """
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(2 * attempt)
    print(f"  [WARN] requests 抓取失败（重试 {retries} 次）: {last_err}")
    return None


def _fetch_html_browser(url: str, timeout: int = 20, retries: int = 2) -> str | None:
    """用无头浏览器抓取渲染后的 HTML（绕过区块浏览器反爬，带重试）。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [WARN] playwright 未安装，无法使用无头浏览器")
        return None

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                ])
                context = browser.new_context(user_agent=HEADERS["User-Agent"])
                page = context.new_page()

                # 拦截非必要资源加速
                page.route("**/*", lambda route: route.abort()
                    if route.request.resource_type in ("image", "font", "media", "stylesheet")
                    else route.continue_())

                page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                # 等待持币列表 JS 渲染完成
                page.wait_for_timeout(4000)
                html = page.content()
                browser.close()
                return html
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(2)
    print(f"  [WARN] 无头浏览器抓取失败: {last_err}")
    return None


def _parse_holders_html(html: str, max_holders: int) -> dict:
    """从区块浏览器 HTML 解析持仓分布数据。"""
    soup = BeautifulSoup(html, "html.parser")
    body_text = soup.get_text()

    result = {
        "total_holders": 0,
        "total_supply": None,
        "top_holders_json": [],
        "tier_distribution_json": [],
        "top_5_pct": None,
        "top_10_pct": None,
        "top_25_pct": None,
        "top_50_pct": None,
        "top_100_pct": None,
    }

    # 解析总持币数（页面文本中的 "Holders 153,662"）
    m = re.search(r'Holders\s+([\d,]+)', body_text)
    if m:
        result["total_holders"] = int(m.group(1).replace(",", ""))

    # 解析总供应（Total / Max Total Supply），用于计算每个地址占比
    m = re.search(r'(?:Max\s+)?Total\s+Supply[^\d]*([\d,]+(?:\.\d+)?)', body_text)
    if m:
        result["total_supply"] = float(m.group(1).replace(",", ""))

    # 解析 Top 集中度：BSCScan 格式:
    # "Supply of Top 5 holders: 67.99% | Top 10 holders: 84.11%"
    for label, key in [("Top 5", "top_5_pct"), ("Top 10", "top_10_pct"),
                       ("Top 25", "top_25_pct"), ("Top 50", "top_50_pct"),
                       ("Top 100", "top_100_pct")]:
        m = re.search(rf'{label}\s+holders?:\s*([\d.]+)%', body_text)
        if m:
            result[key] = float(m.group(1))

    # 解析 Tier 分布
    tier_names = ["Whale", "Shark", "Dolphin", "Fish", "Crab", "Shrimp"]
    tiers = []
    for tn in tier_names:
        # 格式: "🐋 Whale 129 0.09% 99.67%"
        pattern = rf'{re.escape(tn)}\s+([\d,]+)\s+([\d.]+)%\s+([\d.]+)%'
        m = re.search(pattern, body_text)
        if m:
            tiers.append({
                "tier": tn,
                "count": int(m.group(1).replace(",", "")),
                "pct_holders": float(m.group(2)),
                "pct_supply": float(m.group(3)),
            })
    result["tier_distribution_json"] = tiers

    # 解析持仓列表（从 HTML table）
    holders_table = soup.select_one("table")
    if not holders_table:
        print("  [WARN] 未找到持仓表格")
        result["top_holders_json"] = []
    else:
        rows = holders_table.select("tr")[1:]  # 跳过表头
        parsed = []

        for row in rows:
            if len(parsed) >= max_holders:
                break

            cols = row.select("td")
            if len(cols) < 3:
                continue

            rank_text = cols[0].get_text(strip=True)
            try:
                rank = int(rank_text)
            except ValueError:
                continue

            # 地址
            addr_link = cols[1].select_one("a")
            if not addr_link:
                continue
            addr_text = addr_link.get_text(strip=True)

            # Label（BSCScan 自动标注如 "Gate 5", "KuCoin: Hot Wallet 2"）
            label_spans = cols[1].select("span")
            label = ""
            for sp in label_spans:
                t = sp.get_text(strip=True)
                if t and t != addr_text:
                    label = t
                    break

            # 数量
            qty_text = cols[2].get_text(strip=True)
            qty = qty_text.replace(",", "")

            # 百分比
            pct_text = cols[3].get_text(strip=True) if len(cols) > 3 else ""
            pct = _parse_pct(pct_text)

            parsed.append({
                "rank": rank,
                "address": addr_text,
                "label": label,
                "amount": qty,
                "pct": pct,
            })

        result["top_holders_json"] = parsed

    return result


def _fetch_tokenholders(explorer_url: str, contract_address: str) -> str | None:
    """请求 etherscan 系区块浏览器的持币数据接口（generic-tokenholders2）。

    该接口直接返回集中度 Cohort、Tier 分布、持币列表三张表，
    比主页面更稳定（requests 即可拿到，无需 JS 渲染）。
    """
    url = f"{explorer_url}/token/generic-tokenholders2?m=normal&a={contract_address}"
    headers = dict(HEADERS)
    headers["Referer"] = f"{explorer_url}/token/{contract_address}"
    last_err = None
    for attempt in range(1, 6):
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            last_err = e
            if attempt < 5:
                time.sleep(2 * attempt)
    print(f"  [WARN] 持币接口抓取失败: {last_err}")
    return None


def _parse_tokenholders_html(html: str, max_holders: int,
                             total_supply: float | None = None) -> dict:
    """解析 generic-tokenholders2 接口返回的 HTML（Cohort/Tier/持币列表）。

    total_supply 来自主页面 Total/Max Total Supply 字段；用于计算每个地址占比。
    不要用 Cohort 表累加求总供应——Cohort 的 Holding Amount 与持币列表存在不一致，
    会导致占比偏低。
    """
    soup = BeautifulSoup(html, "html.parser")
    result = {
        "total_holders": 0,
        "top_holders_json": [],
        "tier_distribution_json": [],
        "top_5_pct": None,
        "top_10_pct": None,
        "top_25_pct": None,
        "top_50_pct": None,
        "top_100_pct": None,
    }

    cohort = {}  # 如 {"1-5": 90.39, "6-10": 3.58, ...}
    tiers = []
    holders = []

    for t in soup.select("table"):
        rows = t.select("tr")
        if not rows:
            continue
        header_text = rows[0].get_text(" ", strip=True)

        # 1) Cohort 集中度表
        if "Cohort" in header_text or "Top 1-5" in header_text:
            for row in rows[1:]:
                cells = [c.get_text(strip=True) for c in row.select("td")]
                if not cells:
                    continue
                label = cells[0]
                pct = None
                for c in cells:
                    m = re.search(r'([\d.]+)%', c)
                    if m:
                        pct = float(m.group(1))
                m2 = re.match(r'Top\s+(\d+)-(\d+)', label)
                if m2:
                    cohort[f"{m2.group(1)}-{m2.group(2)}"] = pct

        # 2) Tier 分布表
        elif "Tier" in header_text or "Whale" in header_text:
            for row in rows[1:]:
                cells = [c.get_text(strip=True) for c in row.select("td")]
                if len(cells) < 4:
                    continue
                m = re.search(r'(Whale|Shark|Dolphin|Fish|Crab|Shrimp)', cells[0])
                if not m:
                    continue
                tier = m.group(1)
                count = int(cells[1].replace(",", "")) if cells[1].replace(",", "").isdigit() else 0
                pct_holders = _parse_pct(cells[2])
                pct_supply = _parse_pct(cells[3])
                tiers.append({
                    "tier": tier,
                    "count": count,
                    "pct_holders": pct_holders,
                    "pct_supply": pct_supply,
                })

        # 3) 持币列表
        elif "Rank" in header_text and "Address" in header_text and "Quantity" in header_text:
            for row in rows[1:]:
                if len(holders) >= max_holders:
                    break
                cells = row.select("td")
                if len(cells) < 4:
                    continue
                rank_text = cells[0].get_text(strip=True)
                try:
                    rank = int(rank_text)
                except ValueError:
                    continue
                # 完整地址从 a 标签 href 提取（持币地址在 ?a= 参数里）
                addr = ""
                for a in row.select("a"):
                    href = a.get("href", "")
                    m = re.search(r'a=(0x[a-fA-F0-9]{40})', href)
                    if m:
                        addr = m.group(1)
                        break
                if not addr:
                    # 备选：href 里第二个 0x 地址（第一个是 token 合约地址）
                    for a in row.select("a"):
                        href = a.get("href", "")
                        addrs = re.findall(r'0x[a-fA-F0-9]{40}', href)
                        if len(addrs) >= 2:
                            addr = addrs[-1]
                            break
                if not addr:
                    m = re.search(r'0x[a-fA-F0-9]{40}', cells[1].get_text(strip=True))
                    if m:
                        addr = m.group(0)
                if not addr:
                    continue
                label = cells[2].get_text(strip=True) if len(cells) > 2 else ""
                quantity = cells[3].get_text(strip=True).replace(",", "") if len(cells) > 3 else ""
                pct = _parse_pct(cells[4].get_text(strip=True)) if len(cells) > 4 else None
                holders.append({
                    "rank": rank,
                    "address": addr,
                    "label": label,
                    "amount": quantity,
                    "pct": pct,
                })

    # 计算每个地址的真实占比（etherscan 的 Percentage 列对部分代币显示 0.0000%，需自行计算）
    if total_supply and total_supply > 0:
        for h in holders:
            try:
                qty = float(str(h.get("amount", "")).replace(",", ""))
            except (ValueError, TypeError):
                continue
            h["pct"] = round(qty / total_supply * 100, 4)

    # Cohort 累加 → Top 5/10/25/50/100 集中度
    if cohort:
        result["top_5_pct"] = cohort.get("1-5")
        top10 = None
        if cohort.get("1-5") is not None and cohort.get("6-10") is not None:
            top10 = round(cohort["1-5"] + cohort["6-10"], 2)
        result["top_10_pct"] = top10
        top25 = None
        if top10 is not None and cohort.get("11-25") is not None:
            top25 = round(top10 + cohort["11-25"], 2)
        result["top_25_pct"] = top25
        top50 = None
        if top25 is not None and cohort.get("26-50") is not None:
            top50 = round(top25 + cohort["26-50"], 2)
        result["top_50_pct"] = top50
        top100 = None
        if top50 is not None and cohort.get("51-100") is not None:
            top100 = round(top50 + cohort["51-100"], 2)
        result["top_100_pct"] = top100

    result["tier_distribution_json"] = tiers
    result["top_holders_json"] = holders
    return result


def scrape_holders(explorer_url: str, contract_address: str,
                   max_holders: int = 50) -> dict:
    """从区块浏览器抓取持仓分布数据。

    策略：
    1. 先请求 token 主页面拿 total_holders（及 BSCScan 主页面直接渲染的持币表）
    2. 再请求 generic-tokenholders2 接口拿集中度/Tier/持币列表（etherscan 系）
    3. 接口失败时回退无头浏览器抓主页面渲染后的完整 HTML
    """
    token_url = f"{explorer_url}/token/{contract_address}"

    result = {
        "chain": "",
        "contract_address": contract_address.lower(),
        "total_holders": 0,
        "top_holders_json": [],
        "tier_distribution_json": [],
        "top_5_pct": None,
        "top_10_pct": None,
        "top_25_pct": None,
        "top_50_pct": None,
        "top_100_pct": None,
        "scraped_at": None,
    }

    # Step 1: 主页面 → total_holders（及 BSCScan 直接渲染的持币表）
    html = _fetch_html(token_url)
    main_parsed = _parse_holders_html(html, max_holders) if html else None
    main_total = main_parsed["total_holders"] if main_parsed else 0
    result["total_holders"] = main_total

    # Step 2: 持币接口 → 集中度/Tier/持币列表（etherscan 系，最稳定）
    main_total_supply = main_parsed["total_supply"] if main_parsed else None
    api_html = _fetch_tokenholders(explorer_url, contract_address)
    api_parsed = _parse_tokenholders_html(api_html, max_holders,
                                          total_supply=main_total_supply) if api_html else None

    # 合并：接口数据优先（含集中度/Tier/持币列表），total_holders 保留主页面的
    if api_parsed and api_parsed["top_holders_json"]:
        result["top_holders_json"] = api_parsed["top_holders_json"]
        result["tier_distribution_json"] = api_parsed["tier_distribution_json"]
        result["top_5_pct"] = api_parsed["top_5_pct"]
        result["top_10_pct"] = api_parsed["top_10_pct"]
        result["top_25_pct"] = api_parsed["top_25_pct"]
        result["top_50_pct"] = api_parsed["top_50_pct"]
        result["top_100_pct"] = api_parsed["top_100_pct"]
    elif main_parsed and main_parsed["top_holders_json"]:
        result.update(main_parsed)

    # Step 3: 仍无持币数据 → 回退无头浏览器
    if not result["top_holders_json"]:
        print("  [INFO] 接口未获取到持币数据，改用无头浏览器...")
        browser_html = _fetch_html_browser(token_url)
        browser_parsed = _parse_holders_html(browser_html, max_holders) if browser_html else None
        if browser_parsed:
            result.update(browser_parsed)

    result["scraped_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    print(f"  总持币: {result['total_holders']}, 解析持仓: {len(result['top_holders_json'])} 条")
    tiers = result["tier_distribution_json"]
    if tiers:
        print(f"  Top 10 集中度: {result.get('top_10_pct')}%")
        print(f"  Whale 占比: {tiers[0].get('pct_supply') if tiers else 'N/A'}%")
        print(f"  CEX 标签: {len([h for h in result['top_holders_json'] if h['label']])} 个地址有标签")

    return result


# ── 数据库 ────────────────────────────────────────────────

ENSURE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS biz.asset_token_holders (
    asset_id INTEGER PRIMARY KEY REFERENCES core.asset(asset_id),
    chain TEXT DEFAULT 'bsc',
    contract_address TEXT,
    source_url TEXT,
    total_holders INTEGER,
    top_5_pct NUMERIC,
    top_10_pct NUMERIC,
    top_25_pct NUMERIC,
    top_50_pct NUMERIC,
    top_100_pct NUMERIC,
    top_holders_json JSONB,
    tier_distribution_json JSONB,
    scraped_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
)
"""

UPSERT_SQL = """
INSERT INTO biz.asset_token_holders (
    asset_id, chain, contract_address, source_url,
    total_holders,
    top_5_pct, top_10_pct, top_25_pct, top_50_pct, top_100_pct,
    top_holders_json, tier_distribution_json,
    scraped_at, updated_at
) VALUES (
    %(asset_id)s, %(chain)s, %(contract_address)s, %(source_url)s,
    %(total_holders)s,
    %(top_5_pct)s, %(top_10_pct)s, %(top_25_pct)s, %(top_50_pct)s, %(top_100_pct)s,
    %(top_holders_json)s, %(tier_distribution_json)s,
    NOW(), NOW()
)
ON CONFLICT (asset_id) DO UPDATE SET
    chain = EXCLUDED.chain,
    contract_address = EXCLUDED.contract_address,
    source_url = EXCLUDED.source_url,
    total_holders = EXCLUDED.total_holders,
    top_5_pct = EXCLUDED.top_5_pct,
    top_10_pct = EXCLUDED.top_10_pct,
    top_25_pct = EXCLUDED.top_25_pct,
    top_50_pct = EXCLUDED.top_50_pct,
    top_100_pct = EXCLUDED.top_100_pct,
    top_holders_json = EXCLUDED.top_holders_json,
    tier_distribution_json = EXCLUDED.tier_distribution_json,
    scraped_at = EXCLUDED.scraped_at,
    updated_at = NOW()
"""


def ensure_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(ENSURE_TABLE_SQL)
    conn.commit()


def save_to_db(conn, asset_id: int, chain: str, contract_address: str,
               explorer_url: str, data: dict) -> None:
    token_url = f"{explorer_url}/token/{contract_address}"
    with conn.cursor() as cur:
        cur.execute(UPSERT_SQL, {
            "asset_id": asset_id,
            "chain": chain,
            "contract_address": contract_address.lower(),
            "source_url": token_url,
            "total_holders": data.get("total_holders", 0),
            "top_5_pct": data.get("top_5_pct"),
            "top_10_pct": data.get("top_10_pct"),
            "top_25_pct": data.get("top_25_pct"),
            "top_50_pct": data.get("top_50_pct"),
            "top_100_pct": data.get("top_100_pct"),
            "top_holders_json": json.dumps(data.get("top_holders_json", []), ensure_ascii=False),
            "tier_distribution_json": json.dumps(data.get("tier_distribution_json", []), ensure_ascii=False),
        })
    conn.commit()
    print("  已写入 biz.asset_token_holders")


# ── 主流程 ─────────────────────────────────────────────────

def main() -> int:
    args = build_parser().parse_args()
    settings = get_settings(require_database=True)

    # 归一化链名：ethereum→eth, arbitrum→arb, avalanche→avax 等
    raw_chain = args.chain.lower()
    chain = CHAIN_ALIASES.get(raw_chain, raw_chain)
    explorer_url = EXPLORER_URLS.get(chain)
    if not explorer_url:
        print(f"ERROR: 不支持的链: {raw_chain}")
        return 1

    with get_connection(settings.database_url) as conn:
        info = resolve_contract(conn, args.asset_id, args.contract, chain)
        if not info:
            print(f"ERROR: 无法找到合约地址或资产信息")
            return 1

        asset_id = info["asset_id"]
        contract_address = info["contract_address"]
        symbol = info.get("symbol", "")
        name = info.get("name", "")

        print(f"资产: {symbol} ({name}) [asset_id={asset_id}]")
        print(f"链: {chain}, 合约: {contract_address}")

        # 确保表存在
        ensure_table(conn)

        # 爬取
        print(f"  抓取 {explorer_url}/token/{contract_address}...")
        data = scrape_holders(explorer_url, contract_address, args.holders_limit)

        if args.save:
            save_to_db(conn, asset_id, chain, contract_address, explorer_url, data)

        # JSON 输出
        output = {
            "status": "ok",
            "asset_id": asset_id,
            "symbol": symbol,
            "name": name,
            "chain": chain,
            "contract_address": contract_address,
            "total_holders": data["total_holders"],
            "top_5_pct": data["top_5_pct"],
            "top_10_pct": data["top_10_pct"],
            "top_50_pct": data["top_50_pct"],
            "top_100_pct": data["top_100_pct"],
            "top_holders": data["top_holders_json"][:10],
            "tier_distribution": data["tier_distribution_json"],
        }
        print(json.dumps(output, ensure_ascii=False, default=str))
        return 0


if __name__ == "__main__":
    sys.exit(main())
