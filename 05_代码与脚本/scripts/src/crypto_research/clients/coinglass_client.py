"""CoinGlass OpenAPI 客户端（合约衍生品：爆仓 / 资金费率等）。

用途：为「轧空行情扫描」补齐 Binance 自采拿不到的维度——**爆仓**。
Binance 侧 `allForceOrders` 已下线（404），实时 `!forceOrder` 订阅长期无产出
（biz.liquidation_events 至今 0 行），故爆仓只能用 CoinGlass。
K线 / OI / CVD / 多空比仍走自建与 Binance 免费端点（`/futures/data/*`，5m 粒度、
500 根历史）作为**现役**来源。多空比在 CoinGlass 侧已于 2026-09-28 复测确认
**≥4h 可用**（见下），故一并封装为**并行备用源**——但**不替换**现役链路。

鉴权与响应约定：
  - 请求头 `CG-API-KEY: <key>`
  - 统一响应体 `{"code": "0", "msg": "...", "data": ...}`；code != "0" 视为业务失败
    （注意常见 403 是「粒度/套餐不足」而非 HTTP 403，HTTP 状态始终 200）

HOBBYIST 套餐实测边界（2026-09-21 实测，勿重复探测）：
  - 时间粒度只支持 "4h"/"6h"/"8h"/"12h"/"1d"/"1w"，**不支持 1h 及以下**，
    低于该粒度返回 code=403 + details.upgrade_required=STANDARD
  - 可用：liquidation/coin-list、liquidation/history(4h+)、liquidation/exchange-list、
    open-interest/history(4h+)、taker-buy-sell-volume/history(4h+)、funding-rate/history(4h+)
  - 不可用：爆仓热图（401 Upgrade plan）、`funding-rate/arbitrage`（401 Upgrade plan）
  - 限频宽松：约 1.3 req/s 连续 30 次请求无 429（**突发额度，不可依赖**；预算一律按官方 30/min）

2026-09-24 实测补充（P1 回填前先探，勿再重复探测）：
  - `liquidation/history`：`exchange=Binance` + `symbol=<合约码>`（`BTCUSDT`/`1000PEPEUSDT` 均可）
    ⇒ 返回 `[{time, long_liquidation_usd, short_liquidation_usd}]`（**字符串**数值，用前须 float）。
  - `liquidation/aggregated-history`：**`exchange_list` 为必填**（缺失 ⇒ code=400
    `Required String parameter 'exchange_list' is not present`），**无 `all` 快捷值**
    ⇒ 需先取 `supported-exchanges`（实测 28 个）再逗号拼接。
    `symbol` 取**币种基码**（`BTC` / `1000PEPE`）；传合约码（`BTCUSDT`）**不报错但返回 0 行**
    （静默空集 ⇒ 消费侧必须把「0 行」当 not_found，不得当 0 爆仓额）。
    返回 `[{time, aggregated_long_liquidation_usd, aggregated_short_liquidation_usd}]`（数值）。
  - `limit` 上限 **4500**（超出 ⇒ code=400），但 @4h **服务端最多回 180 天**（=1080 点）
    ⇒ 单请求即可覆盖全窗口，**无需分页**。
  - ⚠️ `startTime` / `endTime` 参数**被忽略**（传了仍返回最新 N 点）⇒ 不能靠时间参数向后翻页，
    要更长历史只能放大 `interval`（@6h/8h/12h=360 天，@1d=全历史）。

用法：
    from crypto_research.clients.coinglass_client import CoinGlassClient
    client = CoinGlassClient(api_key=settings.coinglass_api_key)
    rows = client.liquidation_coin_list()          # 全币种滚动爆仓额（1h/4h/12h/24h）
    hist = client.liquidation_history("Binance", "BTCUSDT", interval="4h", limit=100)
    agg  = client.liquidation_aggregated_history(["Binance", "OKX"], "BTC", interval="4h")
"""
from __future__ import annotations

import threading
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DEFAULT_BASE_URL = "https://open-api-v4.coinglass.com"
# HOBBYIST 及以上套餐可用的最小粒度集合（低于此集合返回 403）
SUPPORTED_INTERVALS_HOBBYIST = ("4h", "6h", "8h", "12h", "1d", "1w")
# HOBBYIST 套餐全局速率上限（次/分钟）——多任务共享，须客户端级限流（工单 CGV4 红线②）
DEFAULT_RATE_PER_MIN = 30.0


class CoinGlassError(RuntimeError):
    """CoinGlass 业务错误（code != 0）或响应结构异常。"""

    def __init__(self, message: str, code: str | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class TokenBucket:
    """进程内全局令牌桶限流器（默认 30 req/min 对齐 HOBBYIST 套餐上限）。

    与 `min_request_gap`（固定间隔）的区别：令牌桶允许短时突发（容量 = burst），
    长期平均速率被钉死在 `rate_per_min`，更适合「一次突发几个小请求 + 长时间空转」
    的采集形态。多任务共享同一 key 时应各持一个桶（跨进程不共享，见工单红线②）。
    """

    def __init__(self, rate_per_min: float = DEFAULT_RATE_PER_MIN,
                 burst: int | None = None) -> None:
        self.rate = max(0.0, rate_per_min) / 60.0  # 令牌/秒
        self.capacity = float(burst if burst is not None else max(1, int(rate_per_min)))
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def consume(self, n: float = 1.0) -> float:
        """消费 n 个令牌，不足则睡眠等待。返回累计等待秒数。"""
        if self.rate <= 0:
            return 0.0
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= n:
                    self._tokens -= n
                    return waited
                wait = (n - self._tokens) / self.rate
            time.sleep(wait)
            waited += wait


class CoinGlassClient:
    """CoinGlass OpenAPI 客户端。"""

    def __init__(
        self,
        api_key: str | None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: int = 20,
        min_request_gap: float = 0.0,
        rate_per_min: float | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("COINGLASS_API_KEY 未配置")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.min_request_gap = max(0.0, min_request_gap)
        self._last_request_ts = 0.0
        # 全局令牌桶（可选）；未传 rate_per_min 时退化为纯 min_request_gap 节流
        self._bucket = TokenBucket(rate_per_min) if rate_per_min and rate_per_min > 0 else None
        # 最近一次响应中的 X-RateLimit-* 头（配额监控用），get_raw 每次刷新
        self.last_rate_limit: dict[str, str] = {}

        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({
            "CG-API-KEY": api_key,
            "Accept": "application/json",
            "User-Agent": "crypto-research-coinglass/1.0",
        })
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    # ── 内部方法 ─────────────────────────────────────────────────

    def _throttle(self) -> None:
        if self.min_request_gap > 0:
            gap = self.min_request_gap - (time.time() - self._last_request_ts)
            if gap > 0:
                time.sleep(gap)
        if self._bucket is not None:
            self._bucket.consume(1.0)

    def get_raw(self, path: str, params: dict | None = None) -> dict:
        """返回完整响应体（含 code/msg/data），不抛业务错误（便于排查套餐边界）。"""
        url = f"{self.base_url}/{path.lstrip('/')}"
        self._throttle()
        resp = self.session.get(url, params=params, timeout=self.timeout)
        self._last_request_ts = time.time()
        # 记录 X-RateLimit-* 响应头供配额监控（工单 CGV4-000）
        self.last_rate_limit = {k: v for k, v in resp.headers.items()
                                if k.lower().startswith("x-ratelimit")}
        try:
            body: Any = resp.json()
        except ValueError:
            body = {"raw": resp.text[:500]}
        if not isinstance(body, dict):
            body = {"data": body}
        body["_http_status"] = resp.status_code
        return body

    def get(self, path: str, params: dict | None = None) -> Any:
        """GET 并返回 data 字段；套餐/参数类业务错误抛 CoinGlassError。"""
        body = self.get_raw(path, params)
        status = body.get("_http_status")
        code = body.get("code")
        if status == 429:
            raise CoinGlassError(f"429 限频：{body.get('msg')}", code, status)
        if code is not None and str(code) != "0":
            raise CoinGlassError(
                f"CoinGlass 业务错误 code={code} msg={body.get('msg')}", str(code), status)
        if "data" not in body:
            raise CoinGlassError(f"响应缺少 data 字段：{str(body)[:200]}", None, status)
        return body["data"]

    # ── 公开接口 ─────────────────────────────────────────────────

    def liquidation_coin_list(self) -> list[dict]:
        """全币种滚动窗口爆仓额（当前 1h / 4h / 12h / 24h，多空分列）。

        这是 HOBBYIST 套餐下唯一能拿到 1h 粒度爆仓数据的接口（1597+ 币种）。
        它是**滚动窗口快照**而非历史序列，只能高频轮询落库后取**绝对值**使用
        （语义＝最近 1h/4h/… 的爆仓额）。⚠️ 相邻快照相减**不等于**该间隔内新增
        爆仓额（差值是「新滚入 − 滚出」，平稳时≈0、回落时常为负），严禁跨桶差分
        （见 2026-09-21 审计 P1-1 与 `scan_daemon._latest_liq_snapshot`）。

        每条字段示例：
            {"symbol": "BTC", "liquidation_usd_24h": ..., "long_liquidation_usd_24h": ...,
             "short_liquidation_usd_24h": ..., "liquidation_usd_1h": ...,
             "long_liquidation_usd_1h": ..., "short_liquidation_usd_1h": ...}
        """
        return self._as_list(self.get("/api/futures/liquidation/coin-list"))

    def liquidation_history(self, exchange: str, symbol: str,
                            interval: str = "4h", limit: int = 100) -> list[dict]:
        """单交易所·单币种爆仓历史（多头/空头爆仓额），粒度 ≥4h。

        `symbol` 为**交易对级合约码**（本库口径，如 `BTCUSDT` / `1000PEPEUSDT`）。
        返回 `[{time(ms), long_liquidation_usd, short_liquidation_usd}]`，数值为**字符串**。
        ⚠️ 分段增量（每个 interval 区间内新增的爆仓额），**不是**滚动窗口快照；
        与 `liquidation_coin_list` 的 `*_liq_usd_1h` 口径不可换算、不可相加（见模块 docstring）。
        """
        return self._as_list(self.get("/api/futures/liquidation/history", {
            "exchange": exchange, "symbol": symbol, "interval": interval, "limit": limit}))

    def liquidation_aggregated_history(self, exchange_list: list[str] | str, symbol: str,
                                       interval: str = "4h", limit: int = 1080) -> list[dict]:
        """币种级·多所聚合爆仓历史，粒度 ≥4h。

        `symbol` 取**币种基码**（`BTC` / `1000PEPE`）——传合约码不报错但**返回 0 行**（实测）。
        `exchange_list` 为**必填**（无 `all` 快捷值，缺失 ⇒ code=400），
        传 list 时按逗号拼接；全所口径用 `supported_exchanges()` 的返回值。
        返回 `[{time(ms), aggregated_long_liquidation_usd, aggregated_short_liquidation_usd}]`（数值）。
        ⚠️ 同为**分段增量**口径；`limit` 上限 4500，但 @4h 服务端最多回 180 天（1080 点）。
        """
        ex = exchange_list if isinstance(exchange_list, str) else ",".join(exchange_list)
        return self._as_list(self.get("/api/futures/liquidation/aggregated-history", {
            "exchange_list": ex, "symbol": symbol, "interval": interval, "limit": limit}))

    def supported_exchanges(self) -> list[str]:
        """当前套餐支持的交易所名列表（用于拼 `aggregated-history` 的 `exchange_list`）。"""
        data = self.get("/api/futures/supported-exchanges")
        if isinstance(data, list):
            return [str(x) for x in data]
        return []

    def liquidation_exchange_list(self, range_: str = "24h") -> list[dict]:
        """各交易所爆仓额汇总（range: 1h/4h/12h/24h）。"""
        return self._as_list(self.get("/api/futures/liquidation/exchange-list", {"range": range_}))

    def funding_rate_history(self, exchange: str, symbol: str,
                             interval: str = "4h", limit: int = 100) -> list[dict]:
        """资金费率历史（OHLC），粒度 ≥4h；仅作辅助（8h 结算滞后指标）。"""
        return self._as_list(self.get("/api/futures/funding-rate/history", {
            "exchange": exchange, "symbol": symbol, "interval": interval, "limit": limit}))

    # ── 跨所衍生品聚合（CGV4-003，2026-09-28 HOBBYIST 实测可用）───────────
    # 实测补充（2026-09-28，勿重复探测）：
    #   - `open-interest/exchange-list`：`symbol` 取**币种基码**（BTC），返回含 `exchange="All"`
    #     的跨所聚合行 + 各所分列 + 币本位/稳定币本位拆分（一次请求拿全「聚合/分所/币本位」）。
    #   - `funding-rate/exchange-list`：**无参**，一次返回全币种（对症 1900+）当期费率快照，
    #     每币含 `stablecoin_margin_list` / `coin_margin_list`（各所 funding_rate/interval/next_time）。
    #   - `open-interest/aggregated-history`：OHLC（**字符串**），`symbol` 取币种基码。
    #   - 多空比三接口（global/top-account/top-position）：`exchange`+`symbol`(合约码)，粒度 ≥4h。
    #   - ❌ `funding-rate/arbitrage` 返回 code=401 Upgrade plan（HOBBYIST 不可用，勿接）。

    def open_interest_exchange_list(self, symbol: str) -> list[dict]:
        """跨所未平仓合约（OI）快照：`symbol` 传**币种基码**（BTC / 1000PEPE）。

        返回每所一行（`exchange`，含 `"All"` 聚合行）：
        `open_interest_usd` / `open_interest_quantity` /
        `open_interest_by_coin_margin`(币本位) / `open_interest_by_stable_coin_margin` /
        `open_interest_change_percent_{5m,15m,30m,1h,4h,24h}`。一次请求即得「聚合/分所/币本位」。
        """
        return self._as_list(self.get("/api/futures/open-interest/exchange-list", {"symbol": symbol}))

    def open_interest_aggregated_history(self, symbol: str,
                                         interval: str = "4h", limit: int = 100) -> list[dict]:
        """跨所聚合 OI 历史（OHLC，`open/high/low/close` 为**字符串**）；`symbol` 传币种基码。"""
        return self._as_list(self.get("/api/futures/open-interest/aggregated-history", {
            "symbol": symbol, "interval": interval, "limit": limit}))

    def open_interest_aggregated_stablecoin_history(self, exchange_list: list[str] | str, symbol: str,
                                                    interval: str = "4h", limit: int = 100) -> list[dict]:
        """跨所聚合**稳定币本位** OI 历史（OHLC）；`exchange_list` 必填、无 `all` 快捷值。"""
        ex = exchange_list if isinstance(exchange_list, str) else ",".join(exchange_list)
        return self._as_list(self.get("/api/futures/open-interest/aggregated-stablecoin-history", {
            "exchange_list": ex, "symbol": symbol, "interval": interval, "limit": limit}))

    def funding_rate_exchange_list(self) -> list[dict]:
        """全币种·各所当期资金费率快照（**无参，一次拉全**）。

        每行 `{symbol(基码), stablecoin_margin_list:[{exchange, funding_rate,
        funding_rate_interval, next_funding_time}], coin_margin_list:[...]}`。
        实测单请求返回 1900+ 币种 ⇒ 费率维度近乎零成本。
        """
        return self._as_list(self.get("/api/futures/funding-rate/exchange-list"))

    def funding_rate_accumulated_exchange_list(self, range_: str = "1d") -> list[dict]:
        """各所**累计**资金费率快照（`range` 必填，如 1d/7d/30d）；含币本位/稳定币本位分列。"""
        return self._as_list(self.get("/api/futures/funding-rate/accumulated-exchange-list",
                                      {"range": range_}))

    def global_long_short_account_ratio_history(self, exchange: str, symbol: str,
                                                interval: str = "4h", limit: int = 100) -> list[dict]:
        """全站账户多空比历史（`symbol` 为**合约码**；HOBBYIST 粒度 ≥4h）。"""
        return self._as_list(self.get("/api/futures/global-long-short-account-ratio/history", {
            "exchange": exchange, "symbol": symbol, "interval": interval, "limit": limit}))

    def top_long_short_account_ratio_history(self, exchange: str, symbol: str,
                                             interval: str = "4h", limit: int = 100) -> list[dict]:
        """顶级交易员**账户数**多空比历史（`symbol` 为合约码；粒度 ≥4h）。"""
        return self._as_list(self.get("/api/futures/top-long-short-account-ratio/history", {
            "exchange": exchange, "symbol": symbol, "interval": interval, "limit": limit}))

    def top_long_short_position_ratio_history(self, exchange: str, symbol: str,
                                              interval: str = "4h", limit: int = 100) -> list[dict]:
        """顶级交易员**持仓量**多空比历史（`symbol` 为合约码；粒度 ≥4h）。"""
        return self._as_list(self.get("/api/futures/top-long-short-position-ratio/history", {
            "exchange": exchange, "symbol": symbol, "interval": interval, "limit": limit}))

    @staticmethod
    def _as_list(data: Any) -> list[dict]:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("list", "data", "items", "results"):
                if isinstance(data.get(key), list):
                    return data[key]
        return []