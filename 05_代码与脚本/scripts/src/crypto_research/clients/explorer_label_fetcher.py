"""区块浏览器地址标签抓取器。

从各链区块浏览器的地址详情页 HTML 中提取 name tag / 标签文本，
作为 onchain_address_label 的补充数据源。

支持的链：eth, bsc, arbitrum, base, polygon
（optimism / avalanche 有 Cloudflare / Routescan 形态差异，暂不支持）

用法：
    fetcher = ExplorerLabelFetcher(chain="eth", proxy="http://127.0.0.1:7890")
    label = fetcher.fetch("0x...")
    # label = {"label_text": "Binance 5", "label_type": "exchange", "display_name": "Binance"}

批量：
    results = fetcher.fetch_batch(["0x...", "0x..."])
"""
from __future__ import annotations

import re
import time
from typing import Any

try:
    import requests
except ImportError:
    requests = None  # noqa

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None  # noqa


# 链 → explorer 主机
EXPLORER_HOSTS: dict[str, str] = {
    "eth": "https://etherscan.io",
    "ethereum": "https://etherscan.io",
    "bsc": "https://bscscan.com",
    "bnb": "https://bscscan.com",
    "arbitrum": "https://arbiscan.io",
    "arb": "https://arbiscan.io",
    "base": "https://basescan.org",
    "polygon": "https://polygonscan.com",
    "matic": "https://polygonscan.com",
    # "optimism": "https://optimistic.etherscan.io",  # Cloudflare 拦截
    # "avalanche": "https://snowtrace.io",           # Routescan 形态不同
}

# 交易所名归一化（与 collect_exchange_wallets.py 保持同步）
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

# 非交易所标签关键词 → 标签类型映射
# 只保留高价值标签：exchange / smart_money / whale / mev_bot / market_maker / dex
# （smart_money 靠行为推断，HTML 爬取不到）
NON_EXCHANGE_LABEL_TYPES: list[tuple[str, str]] = [
    # mev_bot
    ("mev", "mev_bot"),
    ("bot", "mev_bot"),
    # market_maker
    ("market maker", "market_maker"),
    # dex
    ("liquidity pool", "dex"),
    ("lp ", "dex"),
    ("dex", "dex"),
    ("uniswap", "dex"),
    ("sushiswap", "dex"),
    ("pancakeswap", "dex"),
    # whale
    ("whale", "whale"),
]

RE_EVM_ADDR = re.compile(r"^0x[a-fA-F0-9]{40}$")
RE_TRUNC_ADDR = re.compile(r"^0x[0-9a-fA-F]{2,}\.\.\.[0-9a-fA-F]{2,}$")
DEFAULT_TIMEOUT = 30
DEFAULT_DELAY = 2.0  # 每次请求间隔秒数（礼貌限速）


class ExplorerLabelFetcher:
    """从区块浏览器地址详情页提取 name tag。

    设计原则：
    - 失败不抛出，返回 None（不阻塞主流程）
    - 内置限速（delay 秒/请求）
    - 支持代理
    """

    def __init__(self, chain: str, proxy: str | None = None,
                 delay: float = DEFAULT_DELAY, timeout: int = DEFAULT_TIMEOUT,
                 use_playwright_fallback: bool = True):
        if requests is None:
            raise ImportError("需要 requests：pip install requests")
        if BeautifulSoup is None:
            raise ImportError("需要 beautifulsoup4：pip install beautifulsoup4")

        self.chain = chain.strip().lower()
        self.host = EXPLORER_HOSTS.get(self.chain)
        if not self.host:
            raise ValueError(f"不支持的链 {self.chain}（可用: {', '.join(EXPLORER_HOSTS)}）")

        self.proxy = proxy
        self.delay = delay
        self.timeout = timeout
        self.use_playwright_fallback = use_playwright_fallback
        self._last_request_at = 0.0

        # 会话复用
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml",
        })
        if proxy:
            self._session.proxies = {"http": proxy, "https": proxy}

        # Playwright 懒加载（遇到 403 时才启动）
        self._pw = None          # sync_playwright 实例
        self._pw_browser = None  # Browser 实例
        self._pw_context = None  # BrowserContext 实例
        self._pw_available = None  # None=未检测, True=可用, False=不可用

    # ── 公共 API ────────────────────────────────────────────

    def fetch(self, address: str) -> dict[str, Any] | None:
        """抓取单个地址的标签。

        返回:
            {
                "label_text": "Binance 5",     # 原始标签文本
                "label_type": "exchange",       # 分类后的标签类型
                "display_name": "Binance",      # 归一化展示名
                "is_exchange": True,
            }
            无标签/失败返回 None
        """
        if not RE_EVM_ADDR.match(address or ""):
            return None

        html = self._fetch_page(address)
        if not html:
            return None

        label_text = self._extract_name_tag(html)
        if not label_text:
            return None

        return self._classify_label(label_text)

    def fetch_batch(self, addresses: list[str]) -> dict[str, dict[str, Any]]:
        """批量抓取，返回 {address: label_info}，跳过无标签/失败的。"""
        results: dict[str, dict[str, Any]] = {}
        for addr in addresses:
            info = self.fetch(addr)
            if info:
                results[addr.lower()] = info
        return results

    def fetch_batch_with_stats(
        self, addresses: list[str]
    ) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
        """批量抓取，返回 (results, stats)。

        stats 包含:
          - ok: 成功查到标签的数量
          - no_label: 页面正常但无标签
          - http_403: 被反爬拦截
          - http_429: 被限流
          - http_other: 其他 HTTP 错误
          - network_error: 网络/超时错误
          - total: 总处理数
        """
        results: dict[str, dict[str, Any]] = {}
        stats = {
            "ok": 0, "no_label": 0,
            "http_403": 0, "http_429": 0, "http_other": 0,
            "network_error": 0, "total": len(addresses),
        }
        for addr in addresses:
            info, status = self.fetch_with_status(addr)
            if info:
                results[addr.lower()] = info
                stats["ok"] += 1
            elif status in stats:
                stats[status] += 1
            else:
                stats["no_label"] += 1
        return results, stats

    def fetch_with_status(
        self, address: str
    ) -> tuple[dict[str, Any] | None, str]:
        """带状态码的抓取，返回 (label_info, status)。

        status: 'ok', 'no_label', 'http_403', 'http_429', 'http_other',
                'network_error', 'invalid_address'
        """
        if not RE_EVM_ADDR.match(address or ""):
            return None, "invalid_address"

        html, status = self._fetch_page_with_status(address)
        if not html:
            return None, status

        label_text = self._extract_name_tag(html)
        if not label_text:
            return None, "no_label"

        info = self._classify_label(label_text)
        if not info:
            return None, "no_label"

        return info, "ok"

    # ── 内部：HTTP ──────────────────────────────────────────

    def _rate_limit(self) -> None:
        """简单限速：确保两次请求间隔 >= delay 秒。"""
        elapsed = time.time() - self._last_request_at
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_request_at = time.time()

    def _fetch_page(self, address: str) -> str | None:
        """拉取地址详情页 HTML。失败返回 None（向后兼容接口）。"""
        html, _ = self._fetch_page_with_status(address)
        return html

    def _fetch_page_with_status(self, address: str) -> tuple[str | None, str]:
        """拉取地址详情页 HTML，返回 (html, status)。

        优先走 requests；遇到 403/Cloudflare 时，若启用了 fallback，
        自动切换到 Playwright 无头浏览器重试。

        status: 'ok', 'http_403', 'http_429', 'http_other', 'network_error'
        """
        self._rate_limit()
        url = f"{self.host}/address/{address}"
        try:
            resp = self._session.get(url, timeout=self.timeout, allow_redirects=True)
            if resp.status_code == 403:
                return self._try_playwright_fallback(url, "http_403")
            if resp.status_code == 429:
                return self._try_playwright_fallback(url, "http_429")
            if resp.status_code >= 400:
                return self._try_playwright_fallback(url, "http_other")
            # 如果页面包含 Cloudflare 挑战，也视为 403 类拦截
            if 'Just a moment...' in resp.text and 'Cloudflare' in resp.text:
                return self._try_playwright_fallback(url, "http_403")
            return resp.text, "ok"
        except Exception:
            return self._try_playwright_fallback(url, "network_error")

    def _try_playwright_fallback(
        self, url: str, original_status: str
    ) -> tuple[str | None, str]:
        """尝试用 Playwright 绕过 Cloudflare。失败返回原始状态。"""
        if not self.use_playwright_fallback:
            return None, original_status
        if self._pw_available is False:
            # 已知 Playwright 不可用，直接跳过
            return None, original_status

        html = self._fetch_page_playwright(url)
        if html:
            return html, "ok"
        # Playwright 也失败了，标记为不可用避免后续反复尝试
        return None, original_status

    def _ensure_playwright(self) -> bool:
        """确保 Playwright 浏览器已启动。成功返回 True。"""
        if self._pw_browser is not None:
            return True
        if self._pw_available is False:
            return False

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self._pw_available = False
            return False

        try:
            self._pw = sync_playwright().start()
            launch_kwargs = {
                "headless": True,
                "args": [
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                ],
            }
            if self.proxy:
                launch_kwargs["proxy"] = {"server": self.proxy}
            self._pw_browser = self._pw.chromium.launch(**launch_kwargs)
            self._pw_context = self._pw_browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
            )
            self._pw_available = True
            return True
        except Exception:
            self._pw_available = False
            self._cleanup_playwright()
            return False

    def _fetch_page_playwright(self, url: str) -> str | None:
        """用 Playwright 抓取页面 HTML。失败返回 None。"""
        if not self._ensure_playwright():
            return None

        try:
            page = self._pw_context.new_page()
            try:
                # 等待页面加载 + Cloudflare 挑战通过
                page.goto(url, timeout=self.timeout * 1000,
                          wait_until="domcontentloaded")
                # 额外等待：如果有 Cloudflare 挑战，等它过去
                for _ in range(20):  # 最多等 10 秒
                    content = page.content()
                    if 'Just a moment...' in content and 'Cloudflare' in content:
                        time.sleep(0.5)
                        continue
                    break
                return page.content()
            finally:
                page.close()
        except Exception:
            return None

    def _cleanup_playwright(self) -> None:
        """关闭 Playwright 资源。"""
        if self._pw_context:
            try:
                self._pw_context.close()
            except Exception:
                pass
            self._pw_context = None
        if self._pw_browser:
            try:
                self._pw_browser.close()
            except Exception:
                pass
            self._pw_browser = None
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None

    def close(self) -> None:
        """释放所有资源（requests session + Playwright）。"""
        try:
            self._session.close()
        except Exception:
            pass
        self._cleanup_playwright()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ── 内部：HTML 解析 ─────────────────────────────────────

    def _extract_name_tag(self, html: str) -> str | None:
        """从地址详情页提取 name tag 文本。

        Etherscan 系列浏览器的地址详情页，name tag 通常在：
        - <span class="hash-tag text-truncate"> 内
        - 或者 #ContentPlaceHolder1_divSummary 附近的标签
        - 老版本在 .address-tag / #nameTag 等位置

        策略：找所有可能含标签的元素，选第一个"看起来像标签"的文本。
        """
        soup = BeautifulSoup(html, "html.parser")

        # 候选选择器（按优先级）
        selectors = [
            "span.hash-tag.text-truncate",
            ".hash-tag",
            "#nameTag",
            ".address-tag",
            "a[href*='/accounts/label/']",  # 标签页链接
            "div[data-title='Name Tag']",
        ]

        for sel in selectors:
            el = soup.select_one(sel)
            if not el:
                continue
            text = el.get_text(strip=True)
            if self._looks_like_label(text):
                return text

        # 兜底：找页面上标题附近带 "Name Tag" 的行
        for th in soup.find_all("th"):
            if "name tag" in th.get_text(strip=True).lower():
                td = th.find_next_sibling("td")
                if td:
                    text = td.get_text(strip=True)
                    if self._looks_like_label(text):
                        return text

        return None

    def _looks_like_label(self, text: str) -> bool:
        """判断文本是否像 name tag（而不是地址本身）。"""
        if not text:
            return False
        if len(text) > 100:
            return False
        text = text.strip()
        # 完整地址 → 不是标签
        if RE_EVM_ADDR.match(text):
            return False
        # 截断地址 → 不是标签
        if RE_TRUNC_ADDR.match(text):
            return False
        # 太短 → 不是标签
        if len(text) < 3:
            return False
        return True

    # ── 内部：标签分类 ──────────────────────────────────────

    def _classify_label(self, label_text: str) -> dict[str, Any] | None:
        """将原始标签文本分类为 label_type + display_name。

        策略：
        1. 先匹配交易所关键词 → exchange
        2. 再匹配非交易所高价值关键词 → 对应类型
        3. 都不匹配 → 返回 None（不存，避免噪声）
        """
        low = label_text.lower().strip()

        # 1. 交易所匹配
        for kw, normalized in EXCHANGE_NAME_MAP.items():
            if kw in low:
                return {
                    "label_text": label_text,
                    "label_type": "exchange",
                    "display_name": normalized,
                    "is_exchange": True,
                }

        # 2. 非交易所高价值标签匹配
        for kw, label_type in NON_EXCHANGE_LABEL_TYPES:
            if kw in low:
                return {
                    "label_text": label_text,
                    "label_type": label_type,
                    "display_name": label_text,
                    "is_exchange": False,
                }

        # 3. 兜底：不存（避免 other 类噪声）
        return None
