"""
代币解锁数据采集：从 tokenomist.ai 用无头浏览器爬取解锁时间表。

用法:
    python phase_chain_token_unlocks.py --asset-id 1234
    python phase_chain_token_unlocks.py --symbol ARB
    python phase_chain_token_unlocks.py --asset-id 1234 --save  # 写入数据库
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

import psycopg
import psycopg.rows
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

NAV_TIMEOUT = 20  # 页面导航超时（秒）
WAIT_MS = 3000      # 页面渲染等待（毫秒）


def _log(msg: str) -> None:
    """日志输出到 stderr，避免污染 stdout 的 JSON。"""
    print(msg, file=sys.stderr, flush=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="代币解锁数据采集")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--asset-id", "--asset_id", type=int, dest="asset_id", help="资产 ID")
    g.add_argument("--symbol", type=str, help="代币符号")
    p.add_argument("--save", action="store_true", help="写入数据库（默认只输出 JSON）")
    p.add_argument("--output-json", action="store_true", default=True, help="JSON 格式输出")
    p.add_argument("--url", type=str,
                   help="用户提供的 tokenomics 项目网址（覆盖 slug 猜测，直接按该 slug 爬取）")
    p.add_argument("--no-browser-search", action="store_true",
                   help="禁用无头浏览器首页搜索兜底（批量模式下可显著提速）")
    return p


# ── 资产解析 ──────────────────────────────────────────────

def resolve_asset(conn, asset_id: int | None, symbol: str | None) -> dict | None:
    """根据 asset_id 或 symbol 查找资产信息。"""
    query = """
        SELECT a.asset_id, a.canonical_symbol AS symbol, a.canonical_name AS name,
               asm_cg.source_asset_key AS coingecko_id
        FROM core.asset a
        LEFT JOIN core.asset_source_map asm_cg
            ON asm_cg.asset_id = a.asset_id AND asm_cg.source_code = 'cg'
        WHERE {}
    """
    if asset_id:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(query.format("a.asset_id = %s"), (asset_id,))
            return cur.fetchone()
    if symbol:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(query.format("UPPER(a.canonical_symbol) = UPPER(%s) LIMIT 1"), (symbol,))
            return cur.fetchone()
    return None


def guess_slugs(asset: dict) -> list[str]:
    """推断 tokenomist 的 token slug 候选列表。

    tokenomist 的 URL 规则: https://tokenomist.ai/{slug}
    slug 基本等于 CoinGecko 的 coin_id，即项目名的小写形式。
    如: Optimism → optimism, Arbitrum → arbitrum, Uniswap → uniswap
    """
    name = (asset.get("name") or "").strip()
    symbol = (asset.get("symbol") or "").strip().lower()

    slugs = []

    # 1. 主候选：项目名小写 + 连字符（空格与点都转连字符）→ 覆盖绝大多数情况
    #    tokenomics.com 统一用连字符，含品牌名中的点（如 Pump.fun → pump-fun）
    if name:
        name_slug = name.lower().replace(" ", "-").replace(".", "-")
        if name_slug not in slugs:
            slugs.append(name_slug)
        # 兜底：保留点的变体（部分项目 slug 可能保留点）
        dot_slug = name.lower().replace(" ", "-")
        if dot_slug != name_slug and dot_slug not in slugs:
            slugs.append(dot_slug)

    # 2. symbol 兜底（如 BTC→bitcoin 等特殊映射）
    slug_map = {
        "btc": "bitcoin", "eth": "ethereum", "bnb": "binancecoin",
        "sol": "solana", "matic": "polygon",
    }
    symbol_slug = slug_map.get(symbol, symbol)
    if symbol_slug and symbol_slug not in slugs:
        slugs.append(symbol_slug)

    # 3. 数据库 CG ID 作为参考（asset_source_map 可能不准，放最后）
    cg_id = asset.get("coingecko_id")
    if cg_id:
        cg_slug = cg_id.strip().lower()
        if cg_slug not in slugs:
            slugs.append(cg_slug)

    return slugs


def _extract_slug_from_url(url: str) -> str | None:
    """从用户提供的 tokenomics 网址中提取项目 slug。

    支持:
      https://app.tokenomics.com/tokenomics/akedo-games
      https://app.tokenomics.com/tokenomics/akedo-games/unlocks
      https://tokenomist.ai/akedo-games
      https://tokenomist.ai/akedo-games/unlock-events
    """
    from urllib.parse import urlparse
    path = (urlparse(url).path or "").strip("/")
    if not path:
        return None
    # 新版 tokenomics.com 的 path 形如 tokenomics/{slug}/...
    if path.startswith("tokenomics/"):
        path = path[len("tokenomics/"):]
    parts = [p for p in path.split("/") if p]
    if not parts:
        return None
    slug = parts[0]
    # 若首段是已知子路径（说明 URL 缺少 slug），尝试下一段
    if slug in ("unlocks", "unlock-events", "revenue", "valuation"):
        slug = parts[1] if len(parts) > 1 else ""
    return slug or None


def _parse_search_results(payload) -> list[dict]:
    """解析搜索 API 返回，兼容新版嵌套结构 {success, data: {results}} 与旧版列表。"""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if payload.get("success"):
            data = payload.get("data") or {}
            if isinstance(data, dict):
                results = data.get("results")
                if isinstance(results, list):
                    return results
        results = payload.get("results")
        if isinstance(results, list):
            return results
    return []


def _names_match(asset_name: str, result_name: str) -> bool:
    """判断资产项目名与搜索结果项目名是否指向同一项目（symbol 歧义消解用）。

    归一化（小写 + 去除非字母数字）后比较是否相等。
    例：Pump.fun → pumpfun 与 PumpBTC → pumpbtc 不等，判定为不同项目。
    """
    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", (s or "").lower())

    a, b = _norm(asset_name), _norm(result_name)
    return bool(a and b and a == b)


def _search_tokenomist_slug(symbol: str, name: str) -> str | None:
    """通过搜索 API 查找正确的 slug。

    使用新版 tokenomics.com 的 /api/search/audits 接口（返回 project_slug），
    该接口对 ticker 匹配准确（旧版 /api/search 已失效）。

    symbol 存在歧义（同一 symbol 对应多个项目，如 PUMP = Pump.fun / PumpBTC）时，
    用项目名 name 消歧：ticker 精确匹配后必须项目名也匹配才采纳，否则判定未收录。
    """
    queries = [symbol]
    if name and name.lower() != symbol.lower():
        queries.append(name)

    api_urls = [
        "https://app.tokenomics.com/api/search/audits",
    ]

    for api_url in api_urls:
        for q in queries:
            try:
                resp = requests.get(
                    api_url,
                    params={"q": q, "limit": 50},
                    headers={"Accept": "application/json"},
                    timeout=10,
                )
                if resp.status_code != 200:
                    continue
                results = _parse_search_results(resp.json())
                if not results:
                    continue
                # 结果格式: [{name, ticker, project_slug, ...}, ...]
                # ticker 可能带尾部空格，需 strip 后再匹配
                ticker_matches = [
                    r for r in results
                    if (r.get("ticker") or r.get("symbol") or "").strip().upper() == symbol.upper()
                ]
                if ticker_matches:
                    # 有项目名时，在 ticker 候选中消歧（symbol 歧义保护）
                    if name:
                        for r in ticker_matches:
                            if _names_match(name, (r.get("name") or "").strip()):
                                slug = (r.get("project_slug") or r.get("slug") or "").strip()
                                if slug:
                                    _log(f"  [搜索] 通过 API 找到 slug: {slug} ({api_url})")
                                    return slug
                        _log(f"  [搜索] symbol {symbol} 匹配到 {len(ticker_matches)} 个结果但项目名不匹配，判定为歧义，跳过")
                        return None
                    # 无项目名时，回退到第一个 ticker 精确匹配
                    first = ticker_matches[0]
                    slug = (first.get("project_slug") or first.get("slug") or "").strip()
                    if slug:
                        _log(f"  [搜索] 通过 API 找到 slug: {slug} ({api_url})")
                        return slug
                # 无 ticker 精确匹配：有项目名时校验首位，否则直接取首位
                first = results[0]
                first_slug = (first.get("project_slug") or first.get("slug") or "").strip()
                if not first_slug:
                    continue
                if name and not _names_match(name, (first.get("name") or "").strip()):
                    _log(f"  [搜索] 首位结果项目名不匹配（{first.get('name')}），跳过")
                    continue
                _log(f"  [搜索] 通过 API 找到 slug（首位）: {first_slug} ({api_url})")
                return first_slug
            except Exception as e:
                _log(f"  [搜索API] {api_url} 查询 '{q}' 失败: {e}")
    return None


def _search_tokenomist_slug_browser(symbol: str, name: str = "",
                                     playwright_p=None) -> str | None:
    """用无头浏览器打开 tokenomics.com 首页，按 symbol 匹配代币 slug。

    /api/search 用 requests 常被 Cloudflare 拦截，改用真实浏览器渲染首页后，
    从页面里的 token 链接（/tokenomics/{slug}）匹配 symbol 得到正确 slug。
    用于解决数据库 name 与 tokenomics slug 不一致的情况（如 AKEDO → akedo-games）。

    复用 scrape_tokenomist 已启动的 Playwright 实例（playwright_p），
    避免同一进程内嵌套 sync_playwright() 触发 "Sync API inside asyncio loop"。
    playwright_p=None 时自行启动并收口（向后兼容独立调用）。
    """
    own_p = playwright_p is None
    p = playwright_p
    browser = None
    try:
        if p is None:
            p = sync_playwright().start()
        browser = p.chromium.launch(headless=True, args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
        ])
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/151.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        _log("  [浏览器搜索] 打开 tokenomics.com 首页...")
        try:
            page.goto("https://app.tokenomics.com/", wait_until="domcontentloaded",
                      timeout=NAV_TIMEOUT * 1000)
        except PlaywrightTimeout:
            _log("  [浏览器搜索] 首页加载超时，尝试用已有内容")
        except Exception as e:
            _log(f"  [浏览器搜索] 首页导航失败: {e}")
            return None

        page.wait_for_timeout(WAIT_MS)

        # 优先尝试首页搜索框，缩小结果范围
        _try_search_box(page, symbol)

        # 遍历页面所有 token 链接，按 symbol 精确匹配
        links = page.evaluate(
            """() => Array.from(document.querySelectorAll('a[href*="/tokenomics/"]'))
                .map(a => ({href: a.href, text: (a.textContent || '').trim()}))
                .filter(x => x.href)"""
        )

        target = (symbol or "").strip().upper()
        if not target:
            return None

        # 精确词边界匹配 symbol（避免 AKE 误匹配 AKEDO/AKEB 等）
        pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(target)}(?![A-Za-z0-9])")
        for it in links:
            m = re.search(r"/tokenomics/([^/?#]+)", it.get("href", ""))
            if not m:
                continue
            slug = m.group(1)
            if pattern.search((it.get("text") or "").upper()):
                _log(f"  [浏览器搜索] 首页匹配到 symbol {target} → slug: {slug}")
                return slug

        _log(f"  [浏览器搜索] 首页未匹配到 symbol {target}")
        return None
    except Exception as e:
        _log(f"  [WARN] 浏览器搜索 slug 失败: {e}")
        return None
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        # 仅当本函数自行启动时，才负责 stop；复用外层实例绝不碰 p
        if own_p and p is not None:
            try:
                p.stop()
            except Exception:
                pass


def _try_search_box(page, symbol: str) -> None:
    """尝试在首页搜索框输入 symbol 并触发过滤（搜索框存在则缩小结果，无则跳过）。"""
    selectors = [
        'input[type="search"]',
        'input[type="text"]',
        'input[placeholder*="search" i]',
        'input[placeholder*="Search" i]',
        'input[placeholder*="token" i]',
        'input[placeholder*="Token" i]',
        'input[placeholder*="find" i]',
        'input[placeholder*="Find" i]',
    ]
    for sel in selectors:
        try:
            inp = page.locator(sel).first
            if not inp.is_visible(timeout=500):
                continue
            inp.fill(symbol)
            inp.press("Enter")
            page.wait_for_timeout(2500)
            _log(f"  [浏览器搜索] 已在搜索框输入: {symbol}")
            return
        except Exception:
            continue


# ── 页面爬取 ──────────────────────────────────────────────

def _norm_name(s: str) -> str:
    """项目名归一化（小写 + 去非字母数字），用于身份比对。"""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _clean_page_project_name(page_title: str) -> str:
    """从 tokenomics 页面标题提取项目名。

    tokenomics.com 标题形如「Tutorial (TUT) Token Unlocks & Tokenomics | Tokenomist」。
    去掉 site 后缀（| / - 之后）与尾部 symbol 括号，得到「Tutorial」。
    """
    if not page_title:
        return ""
    head = re.split(r"\s+Token", page_title)[0].strip()
    head = re.split(r"\s*[\|\-]\s*", head)[0].strip()
    head = re.sub(r"\s*\([^)]*\)\s*$", "", head).strip()
    return head


def _page_identity_ok(page_title: str, asset_name: str, asset_symbol: str) -> bool:
    """判断已抓取的页面是否确实属于目标资产（防同名 symbol 串项目）。

    仅当「能解析出页面项目名且明确不同于目标资产」时才判定为不匹配；
    取不到标题或格式异常时一律放行，宁可不拦截也绝不误杀正确页面。
    """
    page_name = _clean_page_project_name(page_title)
    a, b = _norm_name(page_name), _norm_name(asset_name)
    if not a or not b:
        return True
    # 名称相等，或一方包含另一方（容忍 "Tutellus" vs "Tutellus Protocol"）
    if a == b or a in b or b in a:
        return True
    # 仅当双方都是完整项目名（>=4 字符）且明显不同，才认定不是同一项目
    if len(a) >= 4 and len(b) >= 4:
        return False
    return True


def _scrape_variant(slug: str, variant: dict, is_fallback: bool, context,
                    include_extras: bool = False,
                    asset_name: str = "", asset_symbol: str = "") -> dict | None:
    """用 Playwright 爬取单个数据源（新版 tokenomics.com 或旧版 tokenomist.ai）。

    context 为已创建的 Playwright BrowserContext，本函数只负责新建/关闭 page，
    避免每个 slug 都重启浏览器。
    include_extras=True 时额外爬取 revenue / valuation 子页面（仅新版支持）。
    asset_name / asset_symbol 用于抓取后校验页面身份，避免同名 symbol 串项目。"""
    key = variant["key"]
    base_url = variant["base_tpl"].format(slug=slug)
    unlock_url = base_url + variant["unlock_path"]

    _log(f"  数据源: {key} | slug: {slug}{' (备选)' if is_fallback else ''}")
    _log(f"  目标 URL: {unlock_url}")

    result = {
        "source_url": base_url,
        "source_name": variant["key"],
        "slug": slug,
        "overview": {},
        "unlock_events": [],
        "allocation": [],
        "revenue": {},
        "valuation": {},
    }

    page = None
    try:
        page = context.new_page()

        # 拦截非必要资源以加速
        page.route("**/*", lambda route: route.abort()
            if route.request.resource_type in ("image", "font", "media", "stylesheet")
            else route.continue_()
        )

        # ── Step 1: 爬 Overview 页面 ──
        _log("  加载 Overview 页面...")
        try:
            page.goto(base_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT * 1000)
        except PlaywrightTimeout:
            _log("  [WARN] Overview 页面加载超时，尝试用已有内容")
        except Exception as e:
            _log(f"  [ERROR] Overview 页面导航失败: {e}")
            page.close()
            return None

        page.wait_for_timeout(WAIT_MS)
        _close_popups(page)
        overview = variant["extract_overview"](page, slug)
        result["overview"] = overview

        # Overview 为空说明 slug 不对或该数据源未收录
        if not overview:
            _log(f"  Overview 为空，{key} 未收录该 slug")
            page.close()
            return None

        # 抓取后校验页面身份：避免同名 symbol 串项目（如 Tutellus 与 Tutorial
        # 两个 TUT 项目共用一个 "tut" slug 时，抓到另一个项目的页面）。
        # 页面标题解析出的项目名若明显不同于目标资产，则放弃该页面，
        # 改试下一个 slug / 数据源，宁缺毋滥。
        if asset_name or asset_symbol:
            page_title = page.title()
            if not _page_identity_ok(page_title, asset_name, asset_symbol):
                _log(f"  [身份校验] 页面标题「{page_title}」与目标资产 "
                     f"{asset_name or asset_symbol} 不匹配，疑似串项目，放弃")
                page.close()
                return None

        _log(f"  Overview: {json.dumps(overview, ensure_ascii=False)}")

        # ── Step 2: 爬 Unlock Events 页面 ──
        _log("  加载 Unlock Events 页面...")
        try:
            page.goto(unlock_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT * 1000)
        except PlaywrightTimeout:
            _log("  [WARN] Unlock Events 页面加载超时")
        except Exception as e:
            _log(f"  [WARN] Unlock Events 页面导航失败: {e}")
            page.close()
            return result

        page.wait_for_timeout(WAIT_MS)
        _close_popups(page)

        unlocks_ov_fn = variant.get("extract_unlocks_overview")
        if unlocks_ov_fn:
            unlocks_ov = unlocks_ov_fn(page)
            if unlocks_ov:
                # 合并解锁进度到 overview，避免覆盖 Overview 页已提取的字段
                result["overview"] = {**result["overview"], **unlocks_ov}

        events = variant["extract_unlock_events"](page)
        result["unlock_events"] = events
        _log(f"  Unlock Events: {len(events)} 条")

        # P1-2: overview 的 next_unlock_value/amount 摊到最近的 upcoming 事件
        # （新站事件表常无 value 列，overview 的 Next Unlock 摘要包含 USD 值）
        ov = result["overview"]
        next_val = ov.get("next_unlock_value_str")
        next_amt = ov.get("next_unlock_amount_str")
        next_pct_mcap = ov.get("next_unlock_pct_mcap")
        if next_val or next_amt or next_pct_mcap:
            upcoming = [e for e in events if e.get("is_upcoming")]
            if upcoming:
                # 取日期最早的 upcoming 事件（最近的一次解锁）
                from datetime import datetime as _dt
                def _pd(s):
                    try:
                        return _dt.strptime(s, "%b %d, %Y")
                    except Exception:
                        return _dt(2000, 1, 1)
                target = min(upcoming, key=lambda e: _pd(e["date"]))
                if next_val and not target.get("value_str"):
                    target["value_str"] = next_val
                    target["value_usd"] = _parse_value_str(next_val)
                if next_amt and not target.get("amount_str"):
                    target["amount_str"] = next_amt
                # P2-1: overview 的 % of MCAP 摊到 upcoming 事件
                if next_pct_mcap and not target.get("pct"):
                    target["pct"] = next_pct_mcap
                    target["ratio_mcap"] = True

        # ── Step 3: 可选爬取 revenue / valuation 子页面 ──
        if include_extras:
            for sub_key, sub_path in (
                ("revenue", variant.get("revenue_path")),
                ("valuation", variant.get("valuation_path")),
            ):
                if not sub_path:
                    continue
                sub_url = base_url + sub_path
                _log(f"  加载 {sub_key} 页面: {sub_path}")
                try:
                    page.goto(sub_url, wait_until="networkidle", timeout=NAV_TIMEOUT * 1000)
                except PlaywrightTimeout:
                    _log(f"  [WARN] {sub_key} 页面加载超时，尝试用已有内容")
                except Exception as e:
                    _log(f"  [WARN] {sub_key} 页面导航失败: {e}")
                    continue
                page.wait_for_timeout(4000)
                _close_popups(page)
                result[sub_key] = _extract_subpage(page)
                _log(f"  {sub_key}: {len(result[sub_key].get('faq', []))} FAQ, "
                     f"{len(result[sub_key].get('tables', []))} tables")

        page.close()
        return result

    except Exception as e:
        _log(f"  [ERROR] 页面爬取失败 ({key}): {e}")
        _log(traceback.format_exc())
        if page:
            try:
                page.close()
            except Exception:
                pass
        return None


def scrape_tokenomist(slugs: list[str], symbol: str = "", name: str = "",
                      include_extras: bool = False,
                      no_browser_search: bool = False) -> dict | None:
    """用 Playwright 爬取解锁数据。

    依次尝试 slugs × 数据源（新版 app.tokenomics.com 优先，旧版 tokenomist.ai 兜底），
    overview 为空则换下一个。
    include_extras=True 时额外爬取 revenue / valuation 子页面。
    name 用于搜索兜底时的 symbol 歧义消解。
    no_browser_search=True 时跳过无头浏览器首页搜索兜底，用于批量模式提速。"""
    p = None
    browser = None
    context = None
    try:
        p = sync_playwright().start()
        browser = p.chromium.launch(headless=True, args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
        ])
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/151.0.0.0 Safari/537.36"
            ),
        )

        for idx, slug in enumerate(slugs):
            is_fallback = idx > 0
            for variant in SOURCE_VARIANTS:
                result = _scrape_variant(slug, variant, is_fallback, context,
                                         include_extras=include_extras,
                                         asset_name=name, asset_symbol=symbol)
                if result:
                    return result

        # 所有 slug + 数据源都失败，尝试搜索找到正确 slug
        if symbol:
            # 兜底1：搜索 API（requests）
            searched_slug = _search_tokenomist_slug(symbol, name)
            if searched_slug and searched_slug not in slugs:
                _log(f"  [搜索兜底] 尝试搜索到的 slug: {searched_slug}")
                # 递归调用自己，只试这一个 slug
                return scrape_tokenomist([searched_slug], symbol, name,
                                         include_extras=include_extras,
                                         no_browser_search=no_browser_search)

            # 兜底2：无头浏览器在首页搜索（API 常被 Cloudflare 拦截）
            if not no_browser_search:
                browser_slug = _search_tokenomist_slug_browser(symbol, name, playwright_p=p)
                if browser_slug and browser_slug not in slugs:
                    _log(f"  [浏览器搜索兜底] 首页匹配到 slug: {browser_slug}")
                    return scrape_tokenomist([browser_slug], symbol, name,
                                             include_extras=include_extras,
                                             no_browser_search=True)

        _log(f"  所有 slug 均失败，该代币可能未被收录")
        return None
    finally:
        if context:
            try:
                context.close()
            except Exception:
                pass
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        if p:
            try:
                p.stop()
            except Exception:
                pass


def _close_popups(page) -> None:
    """关闭 tokenomist 的广告弹窗和对话框。"""
    close_selectors = [
        'button:has-text("Dismiss")',
        'button:has-text("No thanks")',
        'button:has-text("Maybe later")',
        'button:has-text("Close")',
        '[aria-label="Close"]',
        '[aria-label="Dismiss"]',
        '.modal-close',
        '[class*="close"]',
    ]
    for sel in close_selectors:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=500):
                el.click()
                page.wait_for_timeout(200)
        except Exception:
            pass

    # 按 Escape 关闭可能的模态框
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
    except Exception:
        pass


def _extract_overview(page, slug: str) -> dict:
    """从 Overview 页面提取关键数据（app.tokenomics.com 新版结构）。"""
    overview: dict = {}

    try:
        full_text = page.locator("body").inner_text(timeout=5000)
    except Exception:
        full_text = ""

    # TGE 日期: "TGE Date November 1, 2025"
    m = re.search(r'TGE\s*Date\s*([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})', full_text)
    if m:
        overview["tge_date"] = m.group(1)

    # Max Total Supply / Total Supply: 支持带单位后缀（M/B/K），如 "Max Total Supply 244.08M"
    m = re.search(r'Max\s+Total\s+Supply\s*([\d,]+(?:\.\d+)?)\s*([BMKbmk]?)', full_text)
    if m:
        num = m.group(1).replace(",", "")
        suffix = m.group(2).upper()
        overview["max_supply_str"] = num + suffix if suffix else num
    m = re.search(r'Total\s+Supply\s*([\d,]+(?:\.\d+)?)\s*([BMKbmk]?)', full_text)
    if m:
        num = m.group(1).replace(",", "")
        suffix = m.group(2).upper()
        overview["total_amount_str"] = num + suffix if suffix else num

    # 分配表（Overview 页面的 Allocation Distribution 部分）
    allocations = _extract_allocation(page)
    if allocations:
        overview["allocation"] = allocations

    # 投资者轮次与条款（Investor Rounds & Terms 表格）
    investor_rounds = _extract_investor_rounds(page)
    if investor_rounds:
        overview["investor_rounds"] = investor_rounds

    # Tokenomics FAQ 板块（Q&A）
    faq = _extract_faq(page)
    if faq:
        overview["faq"] = faq

    return overview


def _extract_unlocks_overview(page) -> dict:
    """从 Unlocks 页面提取解锁进度 + 下一次解锁信息（app.tokenomics.com 新版结构）。"""
    ov: dict = {}

    try:
        full_text = page.locator("body").inner_text(timeout=5000)
    except Exception:
        full_text = ""

    # 释放进度: "Released: 33.1%" / "Unlocked 33.1%" / "Locked 65.8%"
    m = re.search(r'Released[:\s]*([\d.]+)%', full_text)
    if m:
        ov["released_pct"] = float(m.group(1))
    m = re.search(r'Locked\s*([\d.]+)%', full_text)
    if m:
        ov["locked_pct"] = float(m.group(1))

    # 下一次解锁: "Next Unlock Sep 1, 2026 ... USD Value $32.2M Tokens 11.2M % of Supply 1.1% % of MCAP 3.4%"
    m = re.search(r'Next\s+Unlock\s*([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})', full_text)
    if m:
        ov["next_unlock_date"] = m.group(1)
    # P1-2: 放宽正则 — 兼容 "USD Value $32.2M" / "Value $32.2M" / "USDValue$32.2M" 等变体
    m = re.search(r'(?:USD\s*)?Value\s*\$([\d,.]+[BMKbmk]?)', full_text)
    if m:
        ov["next_unlock_value_str"] = "$" + m.group(1)
    m = re.search(r'Tokens?\s*([\d,.]+[BMKbmk]?)', full_text)
    if m:
        ov["next_unlock_amount_str"] = m.group(1)
    m = re.search(r'%\s*of\s*Supply\s*([\d.]+)%', full_text)
    if m:
        ov["next_unlock_pct"] = float(m.group(1))
    # P2-1: % of MCAP
    m = re.search(r'%\s*of\s*MCAP\s*([\d.]+)%', full_text)
    if m:
        ov["next_unlock_pct_mcap"] = float(m.group(1))

    return ov


def _extract_allocation(page) -> list[dict]:
    """提取分配表（app.tokenomics.com 新版：Pool Name | Allocation % | ...）。"""
    allocations = []
    try:
        rows = page.locator("table tr").all()
        for row in rows[:20]:
            try:
                text = row.inner_text().strip()
                if not text:
                    continue
                # 模式: "Community 40.00% 18.8% $1.3B 22 Days"
                m = re.match(r'(.+?)\s+([\d.]+)%', text)
                if not m:
                    continue
                name = m.group(1).strip()
                pct = float(m.group(2))
                # 过滤表头/汇总行
                if not name or name.lower() in ("pool name", "name", "total", "average"):
                    continue
                # 去重：同一 pool 可能同时出现在分配表与投资者轮次表中
                if any(a["name"].lower() == name.lower() for a in allocations):
                    continue
                allocations.append({"name": name, "pct": pct})
            except Exception:
                continue
    except Exception:
        pass
    return allocations[:15]


def _extract_faq(page) -> list[dict]:
    """提取 Overview 页面的 Tokenomics FAQ 板块（<details>/<summary> 手风琴）。

    每个 FAQ 项是一个 <details>，textContent 依次为「问题 + 答案」。
    返回 [{"q": 问题, "a": 答案}, ...]。
    """
    try:
        faqs = page.evaluate("""
            () => {
                const results = [];
                const details = document.querySelectorAll('details');
                details.forEach(d => {
                    const summary = d.querySelector('summary');
                    if (!summary) return;
                    const q = summary.textContent.trim().replace(/\\s+/g, ' ');
                    const full = d.textContent.trim().replace(/\\s+/g, ' ');
                    // 去掉前面的问题文本，剩下即答案
                    let a = full;
                    if (a.startsWith(q)) {
                        a = a.slice(q.length).trim();
                    }
                    if (q && a) {
                        results.push({ q: q, a: a.slice(0, 3000) });
                    }
                });
                return results;
            }
        """)
        _log(f"  FAQ: {len(faqs)} 条")
        return faqs
    except Exception as e:
        _log(f"  [WARN] FAQ 提取失败: {e}")
        return []


def _extract_investor_rounds(page) -> list[dict]:
    """提取 Overview 页面的 Investor Rounds & Terms 表格。

    该表列: Round | Allocation | Entry Price | Entry FDV | Raised | Vesting Terms
    与上方 Allocation Distribution 表（Pool Name | Allocation % ...）不同，
    通过表头含 "entry price"/"vesting"/"round" 来定位。
    返回 [{列名: 值}, ...]。
    """
    try:
        rows = page.evaluate("""
            () => {
                const tables = Array.from(document.querySelectorAll('table'));
                for (const table of tables) {
                    const headerCells = Array.from(table.querySelectorAll('thead th, thead td, tr:first-child th, tr:first-child td'));
                    const headers = headerCells.map(h => h.textContent.trim().replace(/\\s+/g, ' '));
                    const joined = headers.join(' ').toLowerCase();
                    if (!(joined.includes('entry price') || joined.includes('vesting') || joined.includes('entry fdv'))) {
                        continue;
                    }
                    const result = [];
                    table.querySelectorAll('tr').forEach(tr => {
                        const cells = Array.from(tr.querySelectorAll('td, th'))
                            .map(td => td.textContent.trim().replace(/\\s+/g, ' '))
                            .filter(Boolean);
                        if (cells.length < 2) return;
                        // 跳过表头行（含 Round / Entry Price 等列名）
                        if (cells.some(c => /entry price|entry fdv|vesting terms|^round$/i.test(c))) return;
                        const obj = {};
                        cells.forEach((c, i) => {
                            const key = headers[i] || ('col' + (i + 1));
                            obj[key] = c;
                        });
                        result.push(obj);
                    });
                    if (result.length) return result;
                }
                return [];
            }
        """)
        _log(f"  Investor Rounds: {len(rows)} 行")
        return rows
    except Exception as e:
        _log(f"  [WARN] Investor Rounds 提取失败: {e}")
        return []


def _extract_tables(page) -> list[list[list[str]]]:
    """提取页面上所有表格，返回 [表格][行][单元格] 的文本结构。"""
    try:
        return page.evaluate("""
            () => Array.from(document.querySelectorAll('table')).map(t =>
                Array.from(t.querySelectorAll('tr')).map(tr =>
                    Array.from(tr.querySelectorAll('th, td'))
                        .map(c => c.textContent.trim().replace(/\\s+/g, ' '))
                        .filter(Boolean)
                ).filter(r => r.length > 0)
            )
        """)
    except Exception as e:
        _log(f"  [WARN] 表格提取失败: {e}")
        return []


def _clean_subpage_text(text: str) -> str:
    """清洗 tokenomics.com 子页面正文，去除顶部导航/面包屑与尾部推荐/页脚噪音。

    tokenomics.com 的 /revenue、/valuation 页面结构固定：
    - 正文以 "Protocol Revenue" / "Token Valuation" 标题开头；
    - 正文在 "Similar Tokens" 推荐列表处结束。
    """
    if not text:
        return ""
    lines = text.splitlines()
    # 尾部：截断到 "Similar Tokens"（其后为推荐列表 + 页脚）
    for i, ln in enumerate(lines):
        if ln.strip() == "Similar Tokens":
            lines = lines[:i]
            break
    # 头部：定位正文标题，去除导航/面包屑
    start = 0
    for i, ln in enumerate(lines):
        if "Protocol Revenue" in ln or "Token Valuation" in ln:
            start = i
            break
    return "\n".join(lines[start:]).strip()


def _extract_subpage(page) -> dict:
    """提取 revenue / valuation 子页面的通用结构化数据。

    返回 {"text": 正文文本（已清洗导航噪音）, "faq": Q&A, "tables": 所有表格}。
    """
    out: dict = {}
    raw_text = ""
    try:
        raw_text = page.locator("body").inner_text(timeout=5000)[:5000]
    except Exception:
        raw_text = ""
    out["text"] = _clean_subpage_text(raw_text)
    out["faq"] = _extract_faq(page)
    out["tables"] = _extract_tables(page)
    return out


def _parse_value_str(value_str: str) -> float:
    """解析 '$32.2M' / '$1.2B' 等 USD 值字符串为数值。"""
    if not value_str:
        return 0.0
    m = re.match(r'^\$?\s*([\d,.]+)\s*([BMKbmk]?)$', value_str.strip())
    if not m:
        return 0.0
    num = float(m.group(1).replace(",", ""))
    unit = m.group(2).upper()
    if unit == "B":
        num *= 1_000_000_000
    elif unit == "M":
        num *= 1_000_000
    elif unit == "K":
        num *= 1_000
    return round(num, 2)


def _parse_token_amount_str(amount_str: str) -> str:
    """归一化代币数量字符串（'11.2M' / '11.2 Million' / '11.2M ARB'）。"""
    if not amount_str:
        return ""
    s = amount_str.strip()
    m = re.match(r'^([\d,.]+)\s*(BMKbmk]?|Billion|Million|Thousand)', s)
    if m:
        num = m.group(1)
        unit_map = {"B": "B", "M": "M", "K": "K",
                    "Billion": "B", "Million": "M", "Thousand": "K"}
        unit = unit_map.get(m.group(2).capitalize() if len(m.group(2)) > 1 else m.group(2), "")
        return num + unit
    # 仅数字
    m = re.match(r'^([\d,.]+)$', s)
    return m.group(1) if m else s


def _extract_unlock_events(page) -> list[dict]:
    """从 Unlocks 页面提取事件列表（app.tokenomics.com 新版结构）。

    新版表格列: Unlock Date | % of MCAP | Unlock Recipients | Countdown
    P1-2/P2-1: 尝试捕获可能的 USD Value 列；% of MCAP 存入 pct 并标注 ratio_mcap=True。
    P2-3: 尝试从行文本解析代币数量（如 '11.2M ARB'）。
    """
    events = []

    # 方法 1：JS 提取 DOM 表格
    try:
        rows_data = page.evaluate("""
            () => {
                const tables = document.querySelectorAll('table');
                for (const table of tables) {
                    const headers = Array.from(table.querySelectorAll('thead th, thead td, tr:first-child th, tr:first-child td'));
                    const headerTexts = headers.map(h => h.textContent.trim().toLowerCase());
                    if (headerTexts.some(h => h.includes('unlock date')) && headerTexts.some(h => h.includes('mcap'))) {
                        const rows = [];
                        const trs = table.querySelectorAll('tbody tr, tr');
                        for (const tr of trs) {
                            const tds = tr.querySelectorAll('td');
                            if (tds.length < 3) continue;
                            const date = tds[0].textContent.trim();
                            if (!/\\w{3,9}\\s+\\d{1,2},\\s+\\d{4}/.test(date)) continue;
                            rows.push({
                                date: date,
                                pct: tds[1].textContent.trim(),
                                recipients: tds[2].textContent.trim(),
                                status: tds[3] ? tds[3].textContent.trim() : '',
                                rowText: tr.textContent.trim(),
                            });
                        }
                        return rows;
                    }
                }
                return [];
            }
        """)
        _log(f"  JS 提取表格: {len(rows_data)} 行")
    except Exception as e:
        _log(f"  JS 提取失败: {e}, 回退到文本解析")
        rows_data = None

    if rows_data:
        seen = set()
        for row in rows_data:
            date_str = row["date"]
            pct_str = row["pct"]
            status_str = row.get("status", "")
            recipients_str = row.get("recipients", "")
            row_text = row.get("rowText", "")

            pm = re.match(r'^\+?([\d.]+)%$', pct_str)
            pct = float(pm.group(1)) if pm else 0.0

            rm = re.match(r'(\d+)\s*Recipients?', recipients_str)
            recipients = int(rm.group(1)) if rm else 1

            is_upcoming = "left" in status_str.lower()

            # P1-2: 行内若有 USD 值（'$32.2M'），解析
            value_usd = 0.0
            value_str = ""
            vm = re.search(r'\$([\d,.]+[BMKbmk]?)', row_text)
            if vm:
                value_str = "$" + vm.group(1)
                value_usd = _parse_value_str(value_str)

            # P2-3: 行内若有代币数量（'11.2M ARB' / 'Tokens 11.2M'），解析
            amount_str = ""
            am = re.search(r'(?:Tokens?|Tokens\s*)\s*([\d,.]+[BMKbmk]?)', row_text)
            if am:
                amount_str = _parse_token_amount_str(am.group(1))
            else:
                am2 = re.search(r'\b([\d,.]+)\s*[A-Z]{2,6}\b', row_text)
                if am2:
                    amount_str = _parse_token_amount_str(am2.group(1))

            key = (date_str, pct)
            if key in seen:
                continue
            seen.add(key)

            events.append({
                "date": date_str, "value_usd": value_usd, "value_str": value_str,
                "amount_str": amount_str, "pct": pct, "ratio_mcap": True,
                "allocations": recipients, "status": status_str,
                "is_upcoming": is_upcoming,
            })

    # 方法 2：innerText 回退
    if not events:
        events = _extract_unlock_events_text(page)

    from datetime import datetime
    def parse_dt(s):
        try: return datetime.strptime(s, "%b %d, %Y")
        except: return datetime(2000, 1, 1)

    upcoming = sorted([e for e in events if e["is_upcoming"]], key=lambda e: parse_dt(e["date"]))
    past = sorted([e for e in events if not e["is_upcoming"]], key=lambda e: parse_dt(e["date"]), reverse=True)
    return upcoming + past


def _extract_unlock_events_text(page) -> list[dict]:
    """innerText 文本解析（回退方案，适配新版日期格式 Mon D, YYYY）。"""
    events = []
    try:
        all_text = page.locator("body").inner_text(timeout=5000)
    except Exception:
        return events

    date_re = re.compile(r'([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})')
    pct_re = re.compile(r'([\d.]+)%')
    recipients_re = re.compile(r'(\d+)\s*Recipients?')
    countdown_re = re.compile(r'(\d+\s+\w+\s+(?:ago|left))')
    value_re = re.compile(r'\$([\d,.]+[BMKbmk]?)')
    amount_re = re.compile(r'(?:Tokens?|Tokens\s*)\s*([\d,.]+[BMKbmk]?)')

    seen = set()
    for line in all_text.split("\n"):
        line = line.strip()
        if not line:
            continue
        dm = date_re.search(line)
        pm = pct_re.search(line)
        if not dm or not pm:
            continue
        date_str = dm.group(1)
        pct = float(pm.group(1))
        rm = recipients_re.search(line)
        recipients = int(rm.group(1)) if rm else 1
        cm = countdown_re.search(line)
        status = cm.group(1) if cm else ""
        is_upcoming = "left" in status.lower()

        value_usd = 0.0
        value_str = ""
        vm = value_re.search(line)
        if vm:
            value_str = "$" + vm.group(1)
            value_usd = _parse_value_str(value_str)

        amount_str = ""
        am = amount_re.search(line)
        if am:
            amount_str = _parse_token_amount_str(am.group(1))

        key = (date_str, pct)
        if key in seen:
            continue
        seen.add(key)
        events.append({
            "date": date_str, "value_usd": value_usd, "value_str": value_str,
            "amount_str": amount_str, "pct": pct, "ratio_mcap": True,
            "allocations": recipients, "status": status,
            "is_upcoming": is_upcoming,
        })
    return events


# ── 旧版 tokenomist.ai 解析函数（部分代币仍仅存于旧站） ─────

def _extract_overview_legacy(page, slug: str) -> dict:
    """从 Overview 页面提取关键数据（旧版 tokenomist.ai 结构）。"""
    overview: dict = {}

    try:
        full_text = page.locator("body").inner_text(timeout=5000)
    except Exception:
        full_text = ""

    # 释放进度: "57.36% Released 5.74B / 10.00B"
    m = re.search(r'([\d.]+)%\s*Released\s*([\d.]+[BMK]?)\s*/\s*([\d.]+[BMK]?)', full_text)
    if m:
        overview["released_pct"] = float(m.group(1))
        overview["released_amount_str"] = m.group(2)
        overview["total_amount_str"] = m.group(3)

    # 下一次解锁: "Aug 16 2026 92.65M ARB 1.61% ... $7.40M 5 days left"
    m = re.search(
        r'(\w{3}\s+\d{1,2}\s+\d{4})\s+([\d.]+[BMK]?)\s+\w+\s+([\d.]+)%\s+.*?\$([\d.]+[BMK]?)\s+(\d+\s+\w+\s+left)?',
        full_text
    )
    if m:
        overview["next_unlock_date"] = m.group(1)
        overview["next_unlock_amount_str"] = m.group(2)
        overview["next_unlock_pct"] = float(m.group(3))
        overview["next_unlock_value_str"] = "$" + m.group(4)
        if m.group(5):
            overview["next_unlock_when"] = m.group(5)

    # Market Cap / FDV
    m = re.search(r'Reported Market Cap\s*\$([\d.]+[BMK]?)', full_text)
    if m:
        overview["market_cap_str"] = "$" + m.group(1)

    m = re.search(r'Fully Diluted Value[^$]*\$([\d.]+[BMK]?)', full_text)
    if m:
        overview["fdv_str"] = "$" + m.group(1)

    # Float %
    m = re.search(r'Float\s*%\s*([\d.]+)%', full_text)
    if m:
        overview["float_pct"] = float(m.group(1))

    # 分配表（Overview 页面的 Allocation 部分）
    allocations = _extract_allocation_legacy(page)
    if allocations:
        overview["allocation"] = allocations

    return overview


def _extract_allocation_legacy(page) -> list[dict]:
    """提取分配表（旧版 tokenomist.ai 结构）。"""
    allocations = []
    try:
        # 尝试找 allocation 表格行
        rows = page.locator("table tr, [class*='allocation'] [class*='row']").all()
        for row in rows[:15]:
            try:
                text = row.inner_text().strip()
                if not text:
                    continue
                # 模式: "Name 42.8% 4,278,000,000.00"
                m = re.match(r'(.+?)\s+([\d.]+)%\s+([\d,]+(?:\.\d+)?)', text)
                if m:
                    allocations.append({
                        "name": m.group(1).strip(),
                        "pct": float(m.group(2)),
                        "amount_str": m.group(3).replace(",", ""),
                    })
            except Exception:
                continue
    except Exception:
        pass
    return allocations[:10]


def _extract_unlock_events_legacy(page) -> list[dict]:
    """从 Unlock Events 页面提取事件列表（旧版 tokenomist.ai 结构）。

    优先用 JS 直接解析 DOM 表格（可靠），失败回退到 innerText。
    """
    events = []

    # 方法 1：JS 提取 DOM 表格
    try:
        rows_data = page.evaluate("""
            () => {
                const tables = document.querySelectorAll('table');
                for (const table of tables) {
                    const headers = Array.from(table.querySelectorAll('thead th, thead td, tr:first-child th, tr:first-child td'));
                    const headerTexts = headers.map(h => h.textContent.trim().toLowerCase());
                    if (headerTexts.some(h => h === 'date') && headerTexts.some(h => h === 'value')) {
                        const rows = [];
                        const tbody = table.querySelector('tbody') || table;
                        const trs = tbody.querySelectorAll('tr');
                        for (const tr of trs) {
                            const tds = tr.querySelectorAll('td');
                            if (tds.length < 5) continue;
                            const date = tds[0].textContent.trim();
                            if (!/\\d{1,2}\\s+\\w{3}\\s+\\d{4}/.test(date)) continue;
                            rows.push({
                                date: date,
                                value: tds[1].textContent.trim(),
                                pct: tds[2].textContent.trim(),
                                allocation: tds[3].textContent.trim(),
                                status: tds[4].textContent.trim(),
                            });
                        }
                        return rows;
                    }
                }
                return [];
            }
        """)
        _log(f"  JS 提取表格: {len(rows_data)} 行")
    except Exception as e:
        _log(f"  JS 提取失败: {e}, 回退到文本解析")
        rows_data = None

    if rows_data is not None:
        seen_dates = set()
        for row in rows_data:
            date_str = row["date"]
            if date_str in seen_dates:
                continue
            seen_dates.add(date_str)

            value_str = row["value"]
            pct_str = row["pct"]
            status_str = row.get("status", "")
            alloc_str = row.get("allocation", "")

            value_match = re.match(r'^\$([\d.]+)([BMK]?)', value_str)
            if not value_match:
                continue

            value_num = float(value_match.group(1))
            unit = value_match.group(2)
            if unit == "B": value_num *= 1_000_000_000
            elif unit == "M": value_num *= 1_000_000
            elif unit == "K": value_num *= 1_000

            pct_match = re.match(r'^\+?([\d.]+)%$', pct_str)
            pct = float(pct_match.group(1)) if pct_match else 0.0

            alloc_count = 1
            m2 = re.match(r'(\d+)\s+Allocation[s]?', alloc_str)
            if m2: alloc_count = int(m2.group(1))

            is_upcoming = "left" in status_str.lower()

            events.append({
                "date": date_str, "value_usd": round(value_num, 2),
                "value_str": value_str, "pct": pct,
                "allocations": alloc_count, "status": status_str,
                "is_upcoming": is_upcoming,
            })

    # 方法 2：innerText 回退
    if not events:
        events = _extract_unlock_events_text_legacy(page)

    from datetime import datetime
    def parse_dt(s):
        try: return datetime.strptime(s, "%d %b %Y")
        except: return datetime(2000, 1, 1)

    upcoming = sorted([e for e in events if e["is_upcoming"]], key=lambda e: parse_dt(e["date"]))
    past = sorted([e for e in events if not e["is_upcoming"]], key=lambda e: parse_dt(e["date"]), reverse=True)
    return upcoming + past


def _extract_unlock_events_text_legacy(page) -> list[dict]:
    """innerText 文本解析（回退方案，旧版 tokenomist.ai 结构）。"""
    events = []
    try:
        all_text = page.locator("body").inner_text(timeout=5000)
    except Exception:
        return events

    raw_lines = all_text.split("\n")
    content_lines = [l.strip() for l in raw_lines if l.strip() and l.strip() != "\t"]

    header_idx = -1
    for i in range(len(content_lines) - 4):
        if content_lines[i].lower() == "date" and content_lines[i + 1].lower() == "value":
            header_idx = i
            break
    if header_idx < 0:
        return events

    data_start = header_idx + 5
    date_re = re.compile(r'^\d{1,2}\s+\w{3}\s+\d{4}$')
    value_re = re.compile(r'^\$([\d.]+)([BMK]?)$')
    seen_dates = set()

    i = data_start
    while i + 4 < len(content_lines):
        date_str = content_lines[i]
        value_str = content_lines[i + 1]
        if date_re.match(date_str) and value_re.match(value_str):
            if date_str not in seen_dates:
                seen_dates.add(date_str)
                vm = value_re.match(value_str)
                vn = float(vm.group(1))
                u = vm.group(2)
                if u == "B": vn *= 1_000_000_000
                elif u == "M": vn *= 1_000_000
                elif u == "K": vn *= 1_000

                pm = re.match(r'^\+?([\d.]+)%$', content_lines[i + 2])
                pct = float(pm.group(1)) if pm else 0.0

                ac = 1
                am = re.match(r'(\d+)\s+Allocation[s]?', content_lines[i + 3])
                if am: ac = int(am.group(1))

                events.append({
                    "date": date_str, "value_usd": round(vn, 2), "value_str": value_str,
                    "pct": pct, "allocations": ac, "status": content_lines[i + 4],
                    "is_upcoming": "left" in content_lines[i + 4].lower(),
                })
            i += 5
        else:
            i += 1
    return events


# 数据源变体：新版 app.tokenomics.com 优先，旧版 tokenomist.ai 兜底
SOURCE_VARIANTS = [
    {
        "key": "tokenomics.com",
        "base_tpl": "https://app.tokenomics.com/tokenomics/{slug}",
        "unlock_path": "/unlocks",
        "revenue_path": "/revenue",
        "valuation_path": "/valuation",
        "extract_overview": _extract_overview,
        "extract_unlocks_overview": _extract_unlocks_overview,
        "extract_unlock_events": _extract_unlock_events,
    },
    {
        "key": "tokenomist.ai",
        "base_tpl": "https://tokenomist.ai/{slug}",
        "unlock_path": "/unlock-events",
        "extract_overview": _extract_overview_legacy,
        "extract_unlocks_overview": None,
        "extract_unlock_events": _extract_unlock_events_legacy,
    },
]


# ── 存入数据库 ─────────────────────────────────────────────

def ensure_table(conn) -> None:
    """确保 biz.asset_token_unlocks 表存在，并包含 revenue/valuation 等列。"""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS biz.asset_token_unlocks (
                asset_id INTEGER PRIMARY KEY REFERENCES core.asset(asset_id),
                source_url TEXT,
                source_name TEXT DEFAULT 'tokenomist',
                slug TEXT,
                overview_json JSONB,
                unlock_events_json JSONB,
                revenue_json JSONB,
                valuation_json JSONB,
                methodology_json JSONB,
                input_snapshot_json JSONB,
                scraped_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                crawl_status VARCHAR(20) NOT NULL DEFAULT 'ok',
                last_attempt_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # 兼容旧表：补充可能缺失的列（JSONB 列与状态列分开处理，避免类型错建）
        for col in ("revenue_json", "valuation_json", "methodology_json", "input_snapshot_json"):
            cur.execute(f"""
                ALTER TABLE biz.asset_token_unlocks
                ADD COLUMN IF NOT EXISTS {col} JSONB
            """)
        # 状态列必须用正确类型（VARCHAR/TIMESTAMPTZ），不能走上面的 JSONB 分支
        cur.execute(
            "ALTER TABLE biz.asset_token_unlocks "
            "ADD COLUMN IF NOT EXISTS crawl_status VARCHAR(20) NOT NULL DEFAULT 'ok'"
        )
        cur.execute(
            "ALTER TABLE biz.asset_token_unlocks "
            "ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMPTZ DEFAULT NOW()"
        )
        # P2-1: 解锁占市值百分比（主源 tokenomics.com 的 % of MCAP）
        cur.execute(
            "ALTER TABLE biz.asset_token_unlocks "
            "ADD COLUMN IF NOT EXISTS unlock_ratio_mcap NUMERIC"
        )
    conn.commit()


def save_to_db(conn, asset_id: int, data: dict) -> None:
    """写入或更新 biz.asset_token_unlocks。"""
    import json as json_mod

    sql = """
        INSERT INTO biz.asset_token_unlocks (
            asset_id, source_url, source_name, slug,
            overview_json, unlock_events_json, revenue_json, valuation_json,
            scraped_at, updated_at, crawl_status, last_attempt_at
        ) VALUES (
            %(asset_id)s, %(source_url)s, %(source_name)s, %(slug)s,
            %(overview_json)s, %(unlock_events_json)s,
            %(revenue_json)s, %(valuation_json)s,
            NOW(), NOW(), %(crawl_status)s, NOW()
        )
        ON CONFLICT (asset_id) DO UPDATE SET
            source_url = EXCLUDED.source_url,
            source_name = EXCLUDED.source_name,
            slug = EXCLUDED.slug,
            overview_json = EXCLUDED.overview_json,
            unlock_events_json = EXCLUDED.unlock_events_json,
            revenue_json = EXCLUDED.revenue_json,
            valuation_json = EXCLUDED.valuation_json,
            crawl_status = EXCLUDED.crawl_status,
            last_attempt_at = NOW(),
            updated_at = NOW()
    """
    with conn.cursor() as cur:
        cur.execute(sql, {
            "asset_id": asset_id,
            "source_url": data.get("source_url"),
            "source_name": data.get("source_name", "tokenomist"),
            "slug": data.get("slug"),
            "overview_json": json_mod.dumps(data.get("overview", {}), ensure_ascii=False),
            "unlock_events_json": json_mod.dumps(data.get("unlock_events", []), ensure_ascii=False),
            "revenue_json": json_mod.dumps(data.get("revenue", {}), ensure_ascii=False),
            "valuation_json": json_mod.dumps(data.get("valuation", {}), ensure_ascii=False),
            "crawl_status": data.get("crawl_status", "ok"),
        })
    conn.commit()
    _log(f"  已写入数据库 (asset_id={asset_id}, crawl_status={data.get('crawl_status', 'ok')})")


def _mark_not_found(conn, asset_id: int, data: dict | None = None) -> None:
    """写入 not_found 墓碑：站点未收录时占位，避免每日重复爬取。

    写入一行 crawl_status='not_found' 的记录；若已存在 ok 记录则跳过
    （不覆盖已成功的数据）。
    """
    import json as json_mod

    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM biz.asset_token_unlocks WHERE asset_id = %s AND crawl_status = 'ok'",
            (asset_id,),
        )
        if cur.fetchone():
            return
        data = data or {}
        cur.execute(
            """
            INSERT INTO biz.asset_token_unlocks (
                asset_id, source_url, source_name, slug,
                overview_json, unlock_events_json, revenue_json, valuation_json,
                scraped_at, updated_at, crawl_status, last_attempt_at
            ) VALUES (
                %(asset_id)s, NULL, %(source_name)s, %(slug)s,
                '{}'::jsonb, '[]'::jsonb, '{}'::jsonb, '{}'::jsonb,
                NOW(), NOW(), 'not_found', NOW()
            )
            ON CONFLICT (asset_id) DO UPDATE SET
                crawl_status = 'not_found',
                last_attempt_at = NOW(),
                updated_at = NOW()
            """,
            {
                "asset_id": asset_id,
                "source_name": (data.get("source_name") or "tokenomist"),
                "slug": data.get("slug"),
            },
        )
    conn.commit()
    _log(f"  已写入 not_found 墓碑 (asset_id={asset_id})")


# ── 主流程 ─────────────────────────────────────────────────

def main() -> int:
    try:
        return _main()
    except Exception as e:
        _log(f"[FATAL] {e}")
        _log(traceback.format_exc())
        print(json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False))
        return 2


def _check_playwright() -> str | None:
    """检查 Playwright + Chromium 是否可用，返回 None 表示正常。"""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
    except Exception as e:
        return f"Playwright/Chromium 不可用: {e}"
    return None


def _main() -> int:
    args = build_parser().parse_args()

    # 预检 Playwright 可用性
    pw_error = _check_playwright()
    if pw_error:
        _log(f"[FATAL] {pw_error}")
        print(json.dumps({"status": "error", "message": pw_error}, ensure_ascii=False))
        return 2

    settings = get_settings(require_database=True)

    with get_connection(settings.database_url) as conn:
        # 解析资产
        asset = resolve_asset(conn, args.asset_id, args.symbol)
        if not asset:
            print(json.dumps({"status": "error", "message": "资产未找到"}, ensure_ascii=False))
            return 1

        asset_id = asset["asset_id"]

        if args.url:
            slug = _extract_slug_from_url(args.url)
            if not slug:
                print(json.dumps({"status": "error", "message": "无法从网址解析 tokenomics slug"},
                                 ensure_ascii=False))
                return 1
            slugs = [slug]
            _log(f"使用用户提供的网址 slug: {slug} (来源 {args.url})")
        else:
            slugs = guess_slugs(asset)

        _log(f"资产: {asset['symbol']} ({asset['name']}), asset_id={asset_id}")

        # 爬取（自动回退备选 slug）
        data = scrape_tokenomist(
            slugs, symbol=asset["symbol"], name=asset["name"],
            no_browser_search=args.no_browser_search,
        )

        if data is None:
            # tokenomist 未收录 → 返回 not_found 状态
            # P1-1: 写 not_found 墓碑，避免 batch 每日重复爬取该资产
            if args.save:
                ensure_table(conn)
                _mark_not_found(conn, asset_id)
            print(json.dumps({
                "status": "not_found",
                "message": "该代币未被 tokenomist 收录",
                "asset_id": asset_id,
                "symbol": asset["symbol"],
                "name": asset["name"],
            }, ensure_ascii=False))
            return 0

        data["asset_id"] = asset_id
        data["symbol"] = asset["symbol"]
        data["name"] = asset["name"]
        data["status"] = "ok"

        # P1-3: overview 有解锁信号但事件为空 → 疑似解析失败，标记 parse_empty
        # 避免"假成功"污染 pending 判定（不覆盖已有 ok 数据）
        overview = data.get("overview", {}) or {}
        overview_signals = any(
            k in overview for k in ("released_pct", "next_unlock_date", "released_amount_str")
        )
        if not data.get("unlock_events") and overview_signals:
            _log("[WARN] overview 有解锁信号但事件为空，疑似解析失败，标记 parse_empty")
            data["crawl_status"] = "parse_empty"
        else:
            data["crawl_status"] = "ok"

        # 写入数据库
        if args.save:
            ensure_table(conn)
            save_to_db(conn, asset_id, data)

        # JSON 输出到 stdout
        output = {k: v for k, v in data.items() if k not in ("status",)}
        print(json.dumps({"status": "ok", **output}, ensure_ascii=False, default=str))
        return 0


if __name__ == "__main__":
    sys.exit(main())
