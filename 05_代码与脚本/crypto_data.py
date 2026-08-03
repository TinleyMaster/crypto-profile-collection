#!/usr/bin/env python3
"""
Crypto Data Aggregator — 统一加密数据采集工具
==============================================
包装四大免费公开 API，零依赖（仅用 urllib），一行命令拉数据。

支持的 API：
  coingecko — 价格/市值/趋势 (30 req/min)
  defillama  — DeFi TVL/链上指标 (无限制)
  dexscreener — DEX 新币/交易对 (300 req/min)
  coincap   — 实时行情 + WebSocket (200 req/min)

使用方法：
  python3 crypto_data.py <api> <command> [options]

示例：
  python3 crypto_data.py coingecko price bitcoin ethereum
  python3 crypto_data.py defillama chains
  python3 crypto_data.py dexscreener search bonk
  python3 crypto_data.py coincap assets 10
"""

import ssl
import subprocess
import shutil
import urllib.error
import urllib.request
import urllib.parse
import json
import sys
from typing import Optional

TIMEOUT = 15
HTTP_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    urllib.request.HTTPHandler(),
)

# ============================================================
# HTTP helper
# ============================================================
def fetch(url: str, method: str = "GET", body: Optional[dict] = None) -> dict:
    """Simple HTTP fetch with error handling."""
    headers = {"User-Agent": "crypto-data-aggregator/1.0"}
    data = None
    if method == "POST" and body:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with HTTP_OPENER.open(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"body": raw[:500]}
        if isinstance(payload, dict):
            payload.setdefault("error", f"HTTP Error {e.code}: {e.reason}")
            payload.setdefault("status", e.code)
            payload.setdefault("url", url)
            return payload
        return {
            "error": f"HTTP Error {e.code}: {e.reason}",
            "status": e.code,
            "url": url,
            "body": payload,
        }
    except Exception as e:
        # Some endpoints negotiate TLS oddly with urllib on macOS/OpenSSL.
        # Curl is available by default, so we use it as a compatibility fallback.
        if shutil.which("curl"):
            cmd = ["curl", "-sS", "--max-time", str(TIMEOUT), "--noproxy", "*", "-X", method]
            for k, v in headers.items():
                cmd += ["-H", f"{k}: {v}"]
            if method == "POST" and body:
                cmd += ["-d", json.dumps(body, ensure_ascii=False)]
            cmd.append(url)
            try:
                resp = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT + 5)
                if resp.returncode == 0:
                    return json.loads(resp.stdout)
                return {
                    "error": str(e),
                    "url": url,
                    "curl_fallback_error": (resp.stderr or resp.stdout).strip(),
                }
            except Exception as curl_error:
                return {
                    "error": str(e),
                    "url": url,
                    "curl_fallback_error": str(curl_error),
                }
        return {"error": str(e), "url": url}

def print_json(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))

# ============================================================
# CoinGecko API v3
# ============================================================
class CoinGecko:
    BASE = "https://api.coingecko.com/api/v3"

    @classmethod
    def price(cls, coins: list, vs: str = "usd"):
        """当前价格 + 24h变化 + 市值"""
        ids = ",".join(coins)
        url = f"{cls.BASE}/simple/price?ids={ids}&vs_currencies={vs}&include_24hr_change=true&include_market_cap=true&include_24hr_vol=true"
        return fetch(url)

    @classmethod
    def trending(cls):
        """热门代币 (Top 15)"""
        url = f"{cls.BASE}/search/trending"
        return fetch(url)

    @classmethod
    def coin_info(cls, coin_id: str):
        """代币详细信息（社区数据/开发者数据/市场数据）"""
        url = f"{cls.BASE}/coins/{coin_id}?localization=false&tickers=false&community_data=true&developer_data=true"
        return fetch(url)

    @classmethod
    def market_chart(cls, coin_id: str, vs: str = "usd", days: int = 7):
        """历史价格/市值/交易量"""
        url = f"{cls.BASE}/coins/{coin_id}/market_chart?vs_currency={vs}&days={days}"
        return fetch(url)

    @classmethod
    def global_data(cls):
        """全球市场总览（总市值/24h交易量/BTC占比/ETH Gas）"""
        url = f"{cls.BASE}/global"
        return fetch(url)

    @classmethod
    def exchanges(cls, per_page: int = 10):
        """交易所排行（交易量/信任评分）"""
        url = f"{cls.BASE}/exchanges?per_page={per_page}"
        return fetch(url)


# ============================================================
# DefiLlama API
# ============================================================
class DefiLlama:
    BASE = "https://api.llama.fi"

    @classmethod
    def chains(cls):
        """所有链的 TVL 排行"""
        url = f"{cls.BASE}/v2/chains"
        return fetch(url)

    @classmethod
    def chain(cls, chain: str):
        """单链 TVL 历史"""
        url = f"{cls.BASE}/v2/historicalChainTvl/{chain}"
        return fetch(url)

    @classmethod
    def protocols(cls, chain: str = ""):
        """协议排行（可按链过滤）"""
        url = f"{cls.BASE}/protocols"
        if chain:
            url = f"{cls.BASE}/protocols/{chain}"
        return fetch(url)

    @classmethod
    def protocol(cls, slug: str):
        """单个协议详情（TVL/费用/收入）"""
        url = f"{cls.BASE}/protocol/{slug}"
        return fetch(url)

    @classmethod
    def fees_revenue(cls, protocol_slug: str = ""):
        """协议费用与收入排行"""
        url = f"{cls.BASE}/overview/fees"
        if protocol_slug:
            url = f"{cls.BASE}/overview/fees/{protocol_slug}"
        return fetch(url)

    @classmethod
    def raises(cls):
        """最近融资事件"""
        url = f"{cls.BASE}/raises"
        return fetch(url)

    @classmethod
    def stablecoins(cls):
        """稳定币排行（各链发行量）"""
        url = "https://stablecoins.llama.fi/stablecoins"
        return fetch(url)

    @classmethod
    def yields(cls):
        """收益池排行"""
        url = "https://yields.llama.fi/pools"
        return fetch(url)

    @classmethod
    def bridges(cls):
        """跨链桥流量"""
        url = "https://bridges.llama.fi/bridges"
        return fetch(url)


# ============================================================
# DexScreener API
# ============================================================
class DexScreener:
    BASE = "https://api.dexscreener.com"

    @classmethod
    def search(cls, query: str):
        """搜索代币（名称/合约/交易对）"""
        url = f"{cls.BASE}/latest/dex/search?q={urllib.parse.quote(query)}"
        return fetch(url)

    @classmethod
    def pairs(cls, chain_id: str, pair_addresses: list):
        """查特定交易对"""
        ids = ",".join(pair_addresses)
        url = f"{cls.BASE}/latest/dex/pairs/{chain_id}/{ids}"
        return fetch(url)

    @classmethod
    def tokens(cls, token_addresses: list):
        """查代币在所有 DEX 上的交易对"""
        ids = ",".join(token_addresses)
        url = f"{cls.BASE}/latest/dex/tokens/{ids}"
        return fetch(url)

    @classmethod
    def token_boosts(cls):
        """DEX 上最活跃的代币（Top Boosted）"""
        url = f"{cls.BASE}/token-boosts/latest/v1"
        return fetch(url)

    @classmethod
    def top_boosted(cls):
        """今天涨幅最大的 Meme/新币"""
        url = f"{cls.BASE}/token-boosts/top/v1"
        return fetch(url)


# ============================================================
# CoinCap API v2
# ============================================================
class CoinCap:
    BASE = "https://api.coincap.io/v2"

    @classmethod
    def assets(cls, limit: int = 20):
        """代币排行（市值/价格/24h变化）"""
        url = f"{cls.BASE}/assets?limit={limit}"
        return fetch(url)

    @classmethod
    def asset(cls, asset_id: str):
        """单个代币详情 + 历史"""
        url = f"{cls.BASE}/assets/{asset_id}"
        return fetch(url)

    @classmethod
    def asset_history(cls, asset_id: str, interval: str = "d1"):
        """代币价格历史 (m1/m5/m15/m30/h1/h2/h6/h12/d1)"""
        url = f"{cls.BASE}/assets/{asset_id}/history?interval={interval}"
        return fetch(url)

    @classmethod
    def markets(cls, asset_id: str = "", limit: int = 20):
        """交易所行情（买卖价/交易量）"""
        if asset_id:
            url = f"{cls.BASE}/assets/{asset_id}/markets?limit={limit}"
        else:
            url = f"{cls.BASE}/markets?limit={limit}"
        return fetch(url)

    @classmethod
    def rates(cls):
        """法币汇率"""
        url = f"{cls.BASE}/rates"
        return fetch(url)

    @classmethod
    def exchanges(cls):
        """交易所列表"""
        url = f"{cls.BASE}/exchanges"
        return fetch(url)


# ============================================================
# CLI dispatch
# ============================================================
API_MAP = {
    "coingecko": {
        "price":       ("coins [vs_currency]",         lambda a: CoinGecko.price(a[0].split(","), a[1] if len(a) > 1 else "usd")),
        "trending":    ("",                             lambda a: CoinGecko.trending()),
        "coin":        ("coin_id",                      lambda a: CoinGecko.coin_info(a[0])),
        "chart":       ("coin_id [days]",               lambda a: CoinGecko.market_chart(a[0], "usd", int(a[1]) if len(a) > 1 else 7)),
        "global":      ("",                             lambda a: CoinGecko.global_data()),
        "exchanges":   ("[per_page]",                   lambda a: CoinGecko.exchanges(int(a[0]) if a else 10)),
    },
    "defillama": {
        "chains":      ("",                             lambda a: DefiLlama.chains()),
        "chain":       ("chain",                        lambda a: DefiLlama.chain(a[0])),
        "protocols":   ("[chain]",                      lambda a: DefiLlama.protocols(a[0] if a else "")),
        "protocol":    ("slug",                         lambda a: DefiLlama.protocol(a[0])),
        "fees":        ("[slug]",                       lambda a: DefiLlama.fees_revenue(a[0] if a else "")),
        "raises":      ("",                             lambda a: DefiLlama.raises()),
        "stablecoins": ("",                             lambda a: DefiLlama.stablecoins()),
        "yields":      ("",                             lambda a: DefiLlama.yields()),
        "bridges":     ("",                             lambda a: DefiLlama.bridges()),
    },
    "dexscreener": {
        "search":      ("query",                        lambda a: DexScreener.search(a[0])),
        "pairs":       ("chain_id pair_addr ...",       lambda a: DexScreener.pairs(a[0], a[1:])),
        "tokens":      ("token_addr ...",               lambda a: DexScreener.tokens(a[1:]) if a[0] == "tokens" else DexScreener.tokens(a)),
        "boosts":      ("",                             lambda a: DexScreener.token_boosts()),
        "top":         ("",                             lambda a: DexScreener.top_boosted()),
    },
    "coincap": {
        "assets":      ("[limit]",                      lambda a: CoinCap.assets(int(a[0]) if a else 20)),
        "asset":       ("asset_id",                     lambda a: CoinCap.asset(a[0])),
        "history":     ("asset_id [interval]",          lambda a: CoinCap.asset_history(a[0], a[1] if len(a) > 1 else "d1")),
        "markets":     ("[asset_id] [limit]",           lambda a: CoinCap.markets(a[0], int(a[1]) if len(a) > 1 else 20) if a else CoinCap.markets()),
        "rates":       ("",                             lambda a: CoinCap.rates()),
        "exchanges":   ("",                             lambda a: CoinCap.exchanges()),
    },
}

def usage():
    print("Crypto Data Aggregator v1.0")
    print("=" * 45)
    print("Usage: python3 crypto_data.py <api> <command> [args]")
    print()
    for api_name, commands in API_MAP.items():
        print(f"[{api_name}]")
        for cmd, (args_desc, _) in commands.items():
            print(f"  {cmd} {args_desc}")
        print()

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        usage()
        sys.exit(0)

    api = sys.argv[1].lower()
    if api not in API_MAP:
        print(f"Unknown API: {api}")
        print(f"Available: {', '.join(API_MAP.keys())}")
        sys.exit(1)

    if len(sys.argv) < 3:
        print(f"[{api}] commands:")
        for cmd, (desc, _) in API_MAP[api].items():
            print(f"  {cmd} {desc}")
        sys.exit(0)

    cmd = sys.argv[2].lower()
    if cmd not in API_MAP[api]:
        print(f"Unknown command '{cmd}' for {api}")
        sys.exit(1)

    args = sys.argv[3:]
    _, fn = API_MAP[api][cmd]
    result = fn(args)
    print_json(result)
