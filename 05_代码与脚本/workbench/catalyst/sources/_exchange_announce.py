"""
交易所公告源基类（REST 轮询）—— OKX / Bitget / Bybit 共用骨架。

为什么需要它：
- 现有催化剂源 99.8% 来自币安（`binance_*` / `kol_*`），**零外部交易所源**；
- 三家交易所的「上新公告」是**一手、免费、分钟级**且**标题自带 symbol** 的最强催化剂，
  可直接落 `asset_id`（补齐现有 67.6% 归属缺口）。

收口能力（三个子类共享，避免三份重复实现）：
1. HTTP：`trust_env=False` 直连优先（服务器直连交易所 API 正常）；
   若直连失败且本机 127.0.0.1:7890 有代理存活，则回退代理（仅本地联调场景）。
2. 重试：网络异常 / 429 指数退避，最多 _MAX_RETRY 次。
3. 增量：只保留 `published_at > since_ts` 的条目（runner 已留 1h 重叠）。
4. **源侧过滤**：剔除 TradFi 永续 / 代币化股票 / Pre-IPO 等**非加密**公告
   （实测三家当前大量推 MCD/JPM/GS/xSHLD/SNPS 类标的，若不过滤会灌入噪音，
   且部分美股 ticker 不在 linker 的撞名清单里，会被误连到同名加密币）。
5. symbol 提取：标题 → base symbol → `XXXUSDT` 形式的 `related_pairs`。

子类必须实现：
- `source_code`（类属性）
- `_endpoint` / `_build_params(page)`  → 请求
- `_parse(payload)`                    → list[CatalystItem]
"""
from __future__ import annotations

import logging
import os
import re
import socket
import time
from typing import ClassVar

import requests

from ..base import BaseCatalystSource
from ..models import CatalystItem

logger = logging.getLogger(__name__)

# ── 网络 ──────────────────────────────────────────────────────────────
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
_LOCAL_PROXY = "http://127.0.0.1:7890"   # 仅本地联调回退用
_MAX_RETRY = 3
_BACKOFF_BASE = 1.5

# ── 非加密公告过滤（源侧第一道闸）────────────────────────────────────
# 实测定位：OKX「to list TradFi perpetual futures…」/「Unified Tokenized Stocks xSHLD…」、
# Bitget「FASTRETAILINGUSDT hot stock perps」/「JIO (Jio Platforms) Pre-IPO…」、
# Bybit「SNPSUSDT on Bybit TradFi Perpetual」—— 全是代币化美股/商品，非加密币。
# 追加 `equity`：OKX 大量「XXX Equity X-Perps」（TQQQ/NFLX/US100/EWZ/UNITREE…）为
# **代币化股票永续**，实测近 14 天该栏目 7 条里 6 条属此类，不加会把股票灌进加密库。
_NON_CRYPTO_RE = re.compile(
    r"tradfi|tokeni[sz]ed\s+stocks?|pre[-\s]?ipo|stock\s+perps?"
    r"|xstocks?|bstocks?|robinhood\s+token|backpack\s+securities"
    r"|commodit|forex|cfds?\b|metals?\b|crude\s+oil|brent\b|natural\s+gas"
    r"|nasdaq|nyse|\bnyse\b|\betf\s+share"
    r"|equity|equities",
    re.IGNORECASE,
)

# 「一包多个 2–6 位代码」= 交易所把代币化股票/ETF 打包上新
# （实测 OKX「to list perpetual futures for H100, ACN, NKE, BWET and SECZ」——
# 全为美股；而加密币上新是单币句「for DNA crypto」或 `XXX/USDT`，绝不会这样打包）。
# 单独成式、**不**带 IGNORECASE，避免命中普通英文句子（"for the new, and…"）。
_EQ_BUNDLE_RE = re.compile(r"\bfor\s+[A-Z0-9]{2,6}(?:\s*,\s*[A-Z0-9]{2,6}){2,}")

# ── symbol 提取 ───────────────────────────────────────────────────────
# ① 直接交易对：SNPSUSDT / FASTRETAILINGUSDT
_PAIR_RE = re.compile(r"(?<![A-Z0-9])([A-Z0-9]{2,20})USDT(?![A-Z0-9])")
# ①' USD 本位对（OKX 写永续常用）：AXSUSD / BLURUSD（base 至少 2 位）
_USD_PAIR_RE = re.compile(r"(?<![A-Z0-9])([A-Z0-9]{2,20})USD(?![A-Z0-9])")
# ② 括号币名：Bitget "NeoSoul (DNA) in the AI zone"
_PAREN_RE = re.compile(r"\(([A-Za-z0-9]{2,15})\)")
# ③ "for DNA crypto" / "support AERGO crypto migration" / "for DNA spot"
_FOR_RE = re.compile(
    r"\b(?:for|support|supports|supporting)\s+([A-Za-z][A-Za-z0-9]{1,14})\s+"
    r"(?:crypto|token|spot|perpetual|futures?|trading|migration)\b",
    re.IGNORECASE,
)
# ④ "list(s) DNA "（list 后直接跟 symbol，排除泛词）
_LIST_VERB_RE = re.compile(
    r"\blists?\s+(?:of\s+)?([A-Z][A-Z0-9]{1,14})"
    r"(?=\s+(?:on|in|to|for|and|,|spot|perpetual|futures|USDT)|$)",
)
# ⑤ 多代码列表："delist DORA, ICX, STORJ, ZEUS and ELF" / "list WUSD, BLURUSD"
#    交易所把一批币写在一句里（尤其下架公告），逐个提出比只取首个关键。
_LIST_OF_CODES_RE = re.compile(
    r"\b(?:list|delist|add)(?:s|ed|ing)?\s+(?:of\s+)?"
    r"([A-Z0-9]{2,15}(?:\s*(?:,|and)\s*[A-Z0-9]{2,15})+)"
)

# 非币种噪声词（大写 token 误匹配拦截）
# 含 **公告自身的高频泛词** —— 实测 `CTUSDT now launched for futures trading`
# 会被 `_FOR_RE` 捕成 `futures` → 产出垃圾对 `FUTURESUSDT`，故必须显式拦截。
_SYMBOL_STOPWORDS = frozenset({
    "USDT", "USDC", "BUSD", "TUSD", "USDP", "FDUSD", "DAI", "USD", "EUR",
    "THE", "AND", "FOR", "NEW", "ALL", "SPOT", "PERP", "PERPS", "LIST", "LISTS",
    "LISTING", "OKX", "BYBIT", "BITGET", "BINANCE", "API", "CEO", "NFT", "ETF",
    "AI", "WEB3", "DEFI", "US", "UK", "EU", "IPO", "UTC", "AM", "PM", "VS",
    "MORE", "ZONE", "TEST", "PRO", "MAX", "MIN", "LTD", "INC", "LLC", "AMM",
    # 公告泛词（`for … trading` / `… futures` 类句式的高频误捕）
    "FUTURES", "FUTURE", "PERPETUAL", "PERPETUALS", "TOKEN", "TOKENS", "TRADING",
    "TRADE", "BOTS", "BOT", "LAUNCH", "LAUNCHED", "LAUNCHES", "MARGIN", "CONTRACT",
    "CONTRACTS", "OPTION", "OPTIONS", "SWAP", "EARN", "STAKING", "STAKED", "ADDED",
    "SUPPORT", "SUPPORTS", "PRODUCT", "PRODUCTS", "DEBUT", "INTRODUCING", "NOW",
    "WILL", "WITH", "ITS", "AVAILABLE", "PAIRS", "PAIR", "MARKET", "MARKETS",
})


def is_crypto_announcement(title: str) -> bool:
    """标题是否为「加密」公告（False = TradFi/代币化股票等，应剔除）。"""
    t = title or ""
    return not (_NON_CRYPTO_RE.search(t) or _EQ_BUNDLE_RE.search(t))


# 营销活动过滤：交易所把「上新配套活动」也塞进 listing 栏目
# （实测 Bybit `type=new_crypto` 返回「KII Alpha Token Splash— Grab a share of…」
# 「USDC Token Splash— …prize pool」）。非上新公告、标题亦提不出 symbol，剔除。
_PROMO_RE = re.compile(
    r"token\s+splash|prize\s+pool|grab\s+a\s+share|giveaway"
    r"|trade\s+to\s+(?:share|win)|trading\s+competition|campaign",
    re.IGNORECASE,
)


def is_promo_announcement(title: str) -> bool:
    """是否营销活动（非上新/下架公告）。"""
    return bool(_PROMO_RE.search(title or ""))


# 批量下架（**不列币名**）：Bybit 常用「to Delist N Token(s)」句式，具体名单在详情页。
# 我们不抓正文 ⇒ 提不出 symbol ⇒ 对单币投研零价值，剔除（带名单的下架公告不受影响）。
_BULK_DELIST_RE = re.compile(
    r"delist\w*\s+\d+\s+(?:token|trading\s+pair|spot\s+pair)", re.IGNORECASE
)


def is_bulk_delist_announcement(title: str) -> bool:
    return bool(_BULK_DELIST_RE.search(title or ""))


def extract_symbols(title: str) -> list[str]:
    """从公告标题提取 **base symbol**（去重、大写、过噪声词）。

    多模式并取（宁可多提，由 linker 的白名单/撞名门禁兜底消歧）：
      ① XXXUSDT / XXXUSD 直接对 ② (XXX) 括号 ③ "for XXX crypto" ④ "list XXX on/in…"

    归一：统一剥掉 quote 后缀只留 base ——
      `ZAMAUSDT`→ZAMA、`SANDUSD`→SAND（OKX 习惯用 `XXXUSD` 写永续，
      若不剥会产出 `SANDUSDUSDT` 垃圾对，且丢掉 SAND/PARTI 这类**真加密币**）。
    """
    if not title:
        return []
    found: list[str] = []

    def _push(raw: str) -> None:
        s = (raw or "").strip().upper()
        # 剥 quote 后缀，只留 base
        if s.endswith("USDT"):
            s = s[:-4]
        elif s.endswith("USD") and len(s) > 3:
            s = s[:-3]
        if not s or s in _SYMBOL_STOPWORDS:
            return
        if not any(c.isalpha() for c in s):      # 纯数字
            return
        if len(s) < 2 or len(s) > 20:
            return
        if s not in found:
            found.append(s)

    for m in _PAIR_RE.finditer(title):
        _push(m.group(1))
    for m in _USD_PAIR_RE.finditer(title):
        _push(m.group(1))
    for m in _LIST_VERB_RE.finditer(title):
        _push(m.group(1))
    for m in _LIST_OF_CODES_RE.finditer(title):
        for code in re.split(r"\s*(?:,|and)\s*", m.group(1)):
            _push(code)
    for m in _PAREN_RE.finditer(title):
        _push(m.group(1))
    for m in _FOR_RE.finditer(title):
        _push(m.group(1))
    return found


def symbols_to_pairs(symbols: list[str]) -> list[str]:
    """base symbol → 标准交易对（linker 只认 XXX USDT 形式）。

    归一：已带 USDT 后缀的（来自 `_PAIR_RE` 的同题重复提取）不再二次追加，
    否则会产生 `ZAMAUSDTUSDT` 这类垃圾对。
    """
    out: list[str] = []
    for s in symbols:
        pair = s if s.endswith("USDT") else f"{s}USDT"
        if pair not in out:
            out.append(pair)
    return out


def _local_proxy_alive(host: str = "127.0.0.1", port: int = 7890) -> bool:
    """探测本地代理端口是否存活（服务器上不存在 → 走直连）。"""
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


class ExchangeAnnouncementSource(BaseCatalystSource):
    """交易所公告源基类（REST 轮询 + 增量 + symbol 关联）。"""

    source_code: ClassVar[str] = ""
    _endpoint: ClassVar[str] = ""
    event_category: ClassVar[str] = "Exchange Announcement"
    timeout: ClassVar[float] = 15.0

    def __init__(self, max_pages: int = 1, request_interval: float = 0.8):
        """
        Args:
            max_pages: 最多翻几页（增量默认 1 页足够；首次也不宜拉太多）
            request_interval: 相邻请求间隔秒
        """
        self.max_pages = max(1, int(max_pages))
        self.request_interval = max(0.0, float(request_interval))
        self._session: requests.Session | None = None
        self._proxy: str | None = None          # 首个请求时惰性决定
        self._proxy_probed = False

    # ── HTTP ─────────────────────────────────────────────────────────
    @property
    def session(self) -> requests.Session:
        if self._session is None:
            s = requests.Session()
            s.trust_env = False                 # 显式直连，跳过环境代理
            s.headers.update({"User-Agent": _USER_AGENT, "Accept": "application/json"})
            self._session = s
        return self._session

    def _resolve_proxy(self) -> str | None:
        """决定本次运行是否走本地代理（仅当显式配置或本机 7890 存活）。"""
        if not self._proxy_probed:
            env = os.environ.get("CATALYST_HTTP_PROXY", "").strip()
            if env:
                self._proxy = env
            elif _local_proxy_alive():
                self._proxy = _LOCAL_PROXY
                logger.info("%s: 检测到本地代理 %s，回退使用", self.source_code, _LOCAL_PROXY)
            else:
                self._proxy = None
            self._proxy_probed = True
        return self._proxy

    def _http_json(self, url: str, params: dict | None = None) -> dict | None:
        """GET → JSON，带指数退避重试 + 代理回退。失败返回 None。"""
        proxies = None
        px = self._resolve_proxy()
        if px:
            proxies = {"http": px, "https": px}

        attempts = [(proxies, 0)]
        if proxies is None and not self._proxy_probed:
            pass
        # 直连失败后，允许再用本地代理兜一次（本地联调）
        do_proxy_fallback = proxies is None and _local_proxy_alive()
        if do_proxy_fallback:
            attempts.append(({"http": _LOCAL_PROXY, "https": _LOCAL_PROXY}, 0))

        for px_map, _ in attempts:
            for i in range(_MAX_RETRY):
                try:
                    r = self.session.get(
                        url, params=params, timeout=self.timeout,
                        proxies=px_map, verify=False,
                    )
                    if r.status_code == 200:
                        return r.json()
                    if r.status_code == 429:
                        wait = _BACKOFF_BASE * (2 ** i)
                        logger.warning("%s: 429 限流，退避 %.1fs（第 %d 次）",
                                       self.source_code, wait, i + 1)
                        time.sleep(wait)
                        continue
                    logger.warning("%s: HTTP %s %s", self.source_code, r.status_code, url)
                    break
                except Exception as e:                      # noqa: BLE001
                    logger.warning("%s: 请求异常(%s) 第%d次: %s",
                                   self.source_code, type(e).__name__, i + 1, str(e)[:120])
                    time.sleep(_BACKOFF_BASE * (2 ** i))
        logger.error("%s: 抓取失败（全部重试耗尽）url=%s", self.source_code, url)
        return None

    # ── 子类接口 ─────────────────────────────────────────────────────
    def _build_params(self, page: int) -> dict:
        raise NotImplementedError

    def _parse(self, payload: dict) -> list[CatalystItem]:
        raise NotImplementedError

    # ── 主流程 ───────────────────────────────────────────────────────
    def fetch(self, since_ts: float | None = None) -> list[CatalystItem]:
        items: list[CatalystItem] = []
        seen_ids: set[str] = set()

        for page in range(1, self.max_pages + 1):
            payload = self._http_json(self._endpoint, self._build_params(page))
            if payload is None:
                break
            page_items = self._parse(payload) or []
            if not page_items:
                break
            for it in page_items:
                if it.source_item_id in seen_ids:
                    continue
                seen_ids.add(it.source_item_id)
                if since_ts and it.published_at and it.published_at <= since_ts:
                    continue                                # 增量：跳过旧条目
                items.append(it)
            if self.request_interval:
                time.sleep(self.request_interval)

        logger.info("%s: fetched %d items (since=%s)", self.source_code, len(items), since_ts)
        return items

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._session:
            self._session.close()
            self._session = None


def build_item(
    *,
    source_code: str,
    item_id: str,
    title: str,
    published_ms,
    source_url: str = "",
    event_category: str = "Exchange Announcement",
    event_subcategory: str = "",
    raw: dict | None = None,
) -> CatalystItem | None:
    """由公告原始字段构造 CatalystItem（统一 symbol→pairs + 非加密/营销过滤）。

    返回 None 表示该条被过滤（非加密公告 / 营销活动 / 无发布时间）。
    """
    if not title or not is_crypto_announcement(title):
        return None
    if is_promo_announcement(title) or is_bulk_delist_announcement(title):
        return None
    try:
        ts = float(published_ms) / 1000.0
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None

    pairs = symbols_to_pairs(extract_symbols(title))
    return CatalystItem(
        source_code=source_code,
        source_item_id=str(item_id or "")[:200],
        source_item_code=str(item_id or "")[:200],
        title=title.strip(),
        body_text="",                       # 交易所公告接口不返回正文（诚实留空）
        body_html="",
        published_at=ts,
        event_category=event_category,
        event_subcategory=event_subcategory,
        related_pairs=pairs,
        source_url=source_url or "",
        seo_keywords=[],
        share_count=0,
        raw_json=raw,
    )
