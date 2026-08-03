# API 测试结果

测试时间：2026-07-25

## 环境说明

- 当前终端默认带了失效代理：`127.0.0.1:7898`
- 已修正 [`crypto_data.py`](file:///Users/tinley/Documents/web3数据/crypto-data/crypto_data.py)，现在会默认绕过系统代理
- 同时增加了 `curl` 兼容回退，用来处理部分 `urllib` 的 TLS 握手问题

## 1. `crypto_data.py` 内置命令实测

### CoinGecko

| 命令 | 结果 |
| --- | --- |
| `coingecko price bitcoin,ethereum usd` | 通过 |
| `coingecko trending` | 通过 |
| `coingecko coin bitcoin` | 通过 |
| `coingecko chart bitcoin 7` | 通过 |
| `coingecko global` | 通过 |
| `coingecko exchanges 5` | 通过 |

### DefiLlama

| 命令 | 结果 |
| --- | --- |
| `defillama chains` | 通过 |
| `defillama chain Ethereum` | 通过 |
| `defillama protocols` | 通过 |
| `defillama protocol uniswap` | 通过 |
| `defillama fees` | 通过 |
| `defillama raises` | 失败，当前为付费接口，返回 `402 Payment Required` |
| `defillama stablecoins` | 通过 |
| `defillama yields` | 通过 |
| `defillama bridges` | 失败，当前为付费接口，返回 `402 Payment Required` |

说明：

- `stablecoins` 已从旧地址修正到 `https://stablecoins.llama.fi/stablecoins`
- `bridges` 已更新到当前地址 `https://bridges.llama.fi/bridges`，但当前返回付费限制

### DexScreener

| 命令 | 结果 |
| --- | --- |
| `dexscreener search bonk` | 通过 |
| `dexscreener pairs solana <pairAddress>` | 通过 |
| `dexscreener tokens <tokenAddress>` | 通过 |
| `dexscreener boosts` | 通过 |
| `dexscreener top` | 通过 |

### CoinCap

| 命令 | 结果 |
| --- | --- |
| `coincap assets 5` | 失败 |
| `coincap asset bitcoin` | 失败 |
| `coincap history bitcoin d1` | 失败 |
| `coincap markets bitcoin 5` | 失败 |
| `coincap rates` | 失败 |
| `coincap exchanges` | 失败 |

说明：

- 当前环境下 `urllib` 和 `curl` 访问 `https://api.coincap.io` 都出现 TLS 握手失败
- 这更像目标站点与当前运行环境的链路兼容问题，不是参数错误

## 2. 文档中的公开接口抽样实测

### 已确认可跑通

| 来源 | 端点 |
| --- | --- |
| Binance Web3 | `/bapi/defi/v2/public/wallet-direct/buw/wallet/market/token/social-rush/rank/list/ai` |
| Binance Web3 | `/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse/rank/list/ai` |
| Binance Web3 | `/bapi/defi/v5/public/wallet-direct/buw/wallet/market/token/search/ai` |
| Binance Web3 | `/bapi/defi/v1/public/wallet-direct/buw/wallet/dex/market/token/meta/info/ai` |
| Binance Web3 | `/bapi/defi/v4/public/wallet-direct/buw/wallet/market/token/dynamic/info/ai` |
| Binance Web3 | `/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse/unified/rank/list/ai` |
| Binance Web3 | `/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse/exclusive/rank/list/ai` |
| Binance RWA | `/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/rwa/stock/detail/list/ai` |
| Binance RWA | `/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/rwa/market/status/ai` |
| Binance RWA | `/bapi/defi/v2/public/wallet-direct/buw/wallet/market/token/rwa/dynamic/ai` |
| Binance RWA | `/bapi/defi/v1/public/wallet-direct/buw/wallet/dex/market/token/kline/ai` |
| Binance P2P | `/bapi/c2c/v1/public/c2c/agent/quote-price` |
| Binance P2P | `/bapi/c2c/v1/public/c2c/agent/ad-list` |
| Binance P2P | `/bapi/c2c/v1/public/c2c/agent/trade-methods` |

### 端点存在，但本仓库文档缺少足够参数示例

这些端点请求后返回了 `illegal parameter`，说明服务在线，但目前文档里给出的信息不足以直接构造完整请求：

- `/bapi/defi/v1/public/wallet-direct/buw/wallet/web/signal/smart-money/ai`
- `/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse/social/hype/rank/leaderboard/ai`
- `/bapi/defi/v1/public/wallet-direct/tracker/wallet/token/inflow/rank/query/ai`
- `/bapi/defi/v1/public/wallet-direct/market/leaderboard/query/ai`
- `/bapi/defi/v1/public/wallet-direct/security/token/audit`
- `/bapi/defi/v3/public/wallet-direct/buw/wallet/address/pnl/active-position-list/ai`
- `/bapi/fiat/v1/public/fiatpayment/agent/get-price`
- `/bapi/fiat/v1/public/fiatpayment/agent/get-capabilities`
- `/bapi/fiat/v1/public/fiatpayment/agent/get-buy-and-sell-payment-methods`
- `/bapi/fiat/v1/public/fiatpayment/agent/get-deposit-and-withdraw-payment-methods`

## 3. 需要 Key 的接口

以下能力需要你在 [`.env`](file:///Users/tinley/Documents/web3数据/crypto-data/.env) 中补充凭证后再测：

- Binance `api.binance.com` 下的 `sapi` 接口
- Binance Pay
- Binance Square / OpenAPI
- commonservice / Onchain Pay
- NeoData financial search

## 4. 建议

优先使用当前已经跑通的 3 组数据源：

1. CoinGecko
2. DefiLlama（除 `raises` / `bridges`）
3. DexScreener

如果后续要继续扩 Binace 文档端点，建议先把每个端点的“最小可用参数”补成具体示例，再做自动化回归。
