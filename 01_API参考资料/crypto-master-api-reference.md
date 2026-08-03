# 链上数据分析 — 全信息源 API 终极参考手册

> 版本：v1.0 | 日期：2026-07-25
> 覆盖：6 大信息源 | 100+ API 端点 | 4 个调用脚本

---

## 目录

1. [信息源总览](#信息源总览)
2. [Binance Skills Hub API](#一binance-skills-hub-api)
3. [CoinGecko API](#二coingecko-api)
4. [DefiLlama API](#三defillama-api)
5. [DexScreener API](#四dexscreener-api)
6. [CoinCap API](#五coincap-api)
7. [westock-data CLI](#六westock-data-cli)
8. [调用脚本速查](#调用脚本速查)
9. [自媒体数据选型指南](#自媒体数据选型指南)

---

## 信息源总览

| # | 信息源 | 端点数 | 认证 | 免费额度 | 侧重 |
|:---:|---|:---:|:---:|---|---|
| 1 | **Binance Skills Hub** | 28+ | 公开/部分需 Key | 无明确限制 | Meme热点/聪明钱/合约审计/DEX |
| 2 | **CoinGecko** | 20+ | 公开 | 30 req/min | 价格/市值/代币元数据/趋势 |
| 3 | **DefiLlama** | 10+ | 公开 | **无限制** | DeFi TVL/协议数据/费用/跨链 |
| 4 | **DexScreener** | 5+ | 公开 | 300 req/min | DEX新币/交易对/K线 |
| 5 | **CoinCap** | 6+ | 公开 | 200 req/min | 实时行情/WebSocket |
| 6 | **westock-data** | 40+ CLI | 公开 | 无限制 | 传统金融/宏观/A股 |

---

## 一、Binance Skills Hub API

### 1.1 Meme 币热点 (meme-rush)

> 零认证；图标路径需拼接 `https://bin.bnbstatic.com`；百分比字段已格式化

**Base URL:** `https://web3.binance.com`

| # | 端点 | 方法 | 说明 |
|:---:|---|---|---|
| 1 | `/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse/rank/list/ai` | **POST** | Launchpad 生命周期排行 |
| 2 | `/bapi/defi/v2/public/wallet-direct/buw/wallet/market/token/social-rush/rank/list/ai` | GET | AI 热点话题 + 关联代币 |

**meme-rush 参数：**
| 参数 | 必填 | 说明 |
|---|---|---|
| `chainId` | 是 | `CT_501`(Solana) / `56`(BSC) / `8453`(Base) |
| `rankType` | 是 | `10`=New(刚创建) / `20`=Finalizing(即将完成) / `30`=Migrated(已迁移DEX) |
| `limit` | 否 | 最大 200 |
| `keywords` | 否 | 关键词过滤 |
| `holdersMin`/`holdersMax` | 否 | 持有者数量范围 |
| `liquidityMin`/`liquidityMax` | 否 | 流动性范围 |
| `marketCapMin`/`marketCapMax` | 否 | 市值范围 |

**调用示例:**
```bash
# Solana 新币 (POST)
curl -X POST 'https://web3.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse/rank/list/ai' \
  -H 'Content-Type: application/json' \
  -H 'User-Agent: binance-web3/2.0 (Skill)' \
  -d '{"chainId":"CT_501","rankType":10,"limit":20}'

# AI 热点话题 (GET)
curl 'https://web3.binance.com/bapi/defi/v2/public/wallet-direct/buw/wallet/market/token/social-rush/rank/list/ai?chainId=CT_501&rankType=20&sort=20&limit=10' \
  -H 'User-Agent: binance-web3/2.0 (Skill)'
```

### 1.2 聪明钱信号 (trading-signal)

| # | 端点 | 方法 | 说明 |
|:---:|---|---|---|
| 3 | `/bapi/defi/v1/public/wallet-direct/buw/wallet/web/signal/smart-money/ai` | GET | 聪明钱买卖信号 |

**返回字段:** `signalId`, `ticker`, `chainId`, `contractAddress`, `direction`(买/卖), `alertPrice`, `currentPrice`, `highestPrice`, `maxGain`, `exitRate`, `status`, `smartMoneyCount`

```bash
curl 'https://web3.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/web/signal/smart-money/ai' \
  -H 'User-Agent: binance-web3/2.0 (Skill)'
```

### 1.3 市场排行榜 (crypto-market-rank)

| # | 端点 | 方法 | 说明 |
|:---:|---|---|---|
| 4 | `/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse/social/hype/rank/leaderboard/ai` | GET | 社交热度排行 |
| 5 | `/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse/unified/rank/list/ai` | POST | 统一排行（Trending/TopSearch/Alpha） |
| 6 | `/bapi/defi/v1/public/wallet-direct/tracker/wallet/token/inflow/rank/query/ai` | POST | 聪明钱净流入排行 |
| 7 | `/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse/exclusive/rank/list/ai` | GET | Pulse Launchpad TOP Meme |
| 8 | `/bapi/defi/v1/public/wallet-direct/market/leaderboard/query/ai` | GET | 交易员 PnL 排行榜 |

### 1.4 代币搜索与分析 (query-token-*)

| # | 端点 | 方法 | 说明 |
|:---:|---|---|---|
| 9 | `/bapi/defi/v5/public/wallet-direct/buw/wallet/market/token/search/ai` | GET | 关键词/合约搜索（全链） |
| 10 | `/bapi/defi/v1/public/wallet-direct/buw/wallet/dex/market/token/meta/info/ai` | GET | 代币元数据（名称/logo/社交） |
| 11 | `/bapi/defi/v4/public/wallet-direct/buw/wallet/market/token/dynamic/info/ai` | GET | 实时市场数据 |
| 12 | `/bapi/defi/v1/public/wallet-direct/security/token/audit` | POST | 合约安全审计 |

### 1.5 钱包持仓 (query-address-info)

| # | 端点 | 方法 | 说明 |
|:---:|---|---|---|
| 13 | `/bapi/defi/v3/public/wallet-direct/buw/wallet/address/pnl/active-position-list/ai` | GET | 钱包持仓（含价格/24h变化） |

### 1.6 代币化证券/RWA (binance-tokenized-securities-info)

Base: `https://www.binance.com`

| # | 端点 | 方法 | 说明 |
|:---:|---|---|---|
| 14 | `/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/rwa/stock/detail/list/ai` | GET | Ondo 代币化股票列表 |
| 15 | `/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/rwa/meta/ai` | GET | 公司元数据 |
| 16 | `/bapi/defi/v2/public/wallet-direct/buw/wallet/market/token/rwa/dynamic/ai` | GET | 实时价格/持有者/基本面 |
| 17 | `/bapi/defi/v1/public/wallet-direct/buw/wallet/dex/market/token/kline/ai` | GET | K 线数据 |

### 1.7 外部 K 线

| # | 端点 | 方法 | 说明 |
|:---:|---|---|---|
| 18 | `https://dquery.sintral.io/u-kline/v1/k-line/candles` | GET | OHLCV K 线（技术分析用） |

---

## 二、CoinGecko API

> 零认证 | 30 req/min | 20,000+ 代币 | 最广泛的独立行情源

**Base URL:** `https://api.coingecko.com/api/v3`

| # | 端点 | 方法 | 说明 |
|:---:|---|---|---|
| 19 | `/simple/price?ids=bitcoin,ethereum&vs_currencies=usd&include_24hr_change=true&include_market_cap=true` | GET | 批量价格/市值/24h变化 |
| 20 | `/search/trending` | GET | Top 15 热门代币 |
| 21 | `/coins/{id}?localization=false&community_data=true&developer_data=true` | GET | 代币详情（社区/开发者/描述/官网） |
| 22 | `/coins/{id}/market_chart?vs_currency=usd&days=7` | GET | 历史价格/市值/交易量 |
| 23 | `/global` | GET | 全球总市值/24h量/BTC占比/ETH Gas |
| 24 | `/exchanges?per_page=10` | GET | 交易所排行（交易量/信任评分） |
| 25 | `/coins/categories` | GET | 代币分类（Layer1/DeFi/Meme等） |
| 26 | `/coins/{id}/tickers` | GET | 跨交易所行情对比 |
| 27 | `/coins/{id}/ohlc?vs_currency=usd&days=1` | GET | OHLC 蜡烛数据 |

```bash
# 价格 + 24h变化 + 市值
curl -s 'https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,solana&vs_currencies=usd&include_24hr_change=true&include_market_cap=true'

# 全球市场总览
curl -s 'https://api.coingecko.com/api/v3/global'
```

---

## 三、DefiLlama API

> 零认证 | **无频率限制** | 6,000+ 协议 | 350+ 链 | DeFi 数据天花板

**Base URL:** `https://api.llama.fi`

| # | 端点 | 方法 | 说明 |
|:---:|---|---|---|
| 28 | `/v2/chains` | GET | 所有链 TVL 排行 |
| 29 | `/v2/historicalChainTvl/{chain}` | GET | 单链 TVL 历史走势 |
| 30 | `/protocols` | GET | 协议排行（总 TVL） |
| 31 | `/protocols/{chain}` | GET | 按链过滤的协议排行 |
| 32 | `/protocol/{slug}` | GET | 单协议详情（TVL/费用/收入/国库） |
| 33 | `/overview/fees` | GET | 协议费用 & 收入排行 |
| 34 | `/raises` | GET | 最近融资事件 |
| 35 | `/stablecoins` | GET | 稳定币排行（各链发行量） |

**独立端点 (yields.llama.fi / bridges.llama.fi):**

| # | 端点 | 方法 | 说明 |
|:---:|---|---|---|
| 36 | `https://yields.llama.fi/pools` | GET | 收益池排行 |
| 37 | `/bridges` | GET | 跨链桥流量排行 |
| 38 | `/bridges/{bridge_id}` | GET | 单桥 24h/7d/30d 流量 |

```bash
# 链 TVL 排行
curl -s 'https://api.llama.fi/v2/chains'

# 单协议详情 (以 uniswap 为例)
curl -s 'https://api.llama.fi/protocol/uniswap'

# 稳定币发行排行
curl -s 'https://api.llama.fi/stablecoins'
```

---

## 四、DexScreener API

> 零认证 | 300 req/min | 80+ 链 | DEX 新币追踪最强

**Base URL:** `https://api.dexscreener.com`

| # | 端点 | 方法 | 说明 |
|:---:|---|---|---|
| 39 | `/latest/dex/search?q={query}` | GET | 搜索代币（名称/合约/交易对） |
| 40 | `/latest/dex/pairs/{chain_id}/{pair_addresses}` | GET | 交易对详情（价格/量/流动性） |
| 41 | `/latest/dex/tokens/{token_addresses}` | GET | 代币在所有 DEX 的交易对 |
| 42 | `/token-boosts/latest/v1` | GET | 最新 Boosted 代币 |
| 43 | `/token-boosts/top/v1` | GET | 今日涨幅最大 Top Boosted |

**返回字段:** `pairAddress`, `baseToken/quoteToken`(含symbol/name), `priceUsd`, `priceChange`(5m/1h/6h/24h), `volume`(各时段), `liquidity`, `fdv`, `marketCap`, `pairCreatedAt`, `txns`(买卖笔数), `url`, `chainId`, `dexId`

```bash
# 搜索代币
curl -s 'https://api.dexscreener.com/latest/dex/search?q=BONK'

# Top Boosted (涨幅最大)
curl -s 'https://api.dexscreener.com/token-boosts/top/v1'
```

---

## 五、CoinCap API

> 零认证 | 200 req/min | 1,000+ 代币 | WebSocket 支持

**Base URL:** `https://api.coincap.io/v2`

| # | 端点 | 方法 | 说明 |
|:---:|---|---|---|
| 44 | `/assets?limit=20` | GET | 代币排行（市值/价格/24h变化/供应量） |
| 45 | `/assets/{id}` | GET | 单代币详情 |
| 46 | `/assets/{id}/history?interval=d1` | GET | 价格历史（m1/m5/m15/m30/h1/h2/h6/h12/d1） |
| 47 | `/assets/{id}/markets?limit=20` | GET | 跨交易所行情对比 |
| 48 | `/rates` | GET | 法币汇率 |
| 49 | `/exchanges` | GET | 交易所列表 |

**WebSocket:** `wss://ws.coincap.io/prices?assets=bitcoin,ethereum`

```bash
# Top 20 代币
curl -s 'https://api.coincap.io/v2/assets?limit=20'

# 比特币历史 (日线)
curl -s 'https://api.coincap.io/v2/assets/bitcoin/history?interval=d1'
```

---

## 六、westock-data CLI

> 零认证 | CLI 工具 | 传统金融 + 少量加密 | 40+ 命令

### 6.1 行情搜索

| 命令 | 参数 | 说明 |
|---|---|---|
| `search <关键词>` | `--type etf\|bond\|sector\|index\|futures\|forex` | 搜索股票/ETF/板块/期货/外汇 |
| `quote <代码>` | 批量逗号分隔 | 实时行情（含涨跌停/ADR/盘前盘后） |
| `kline <代码>` | `--period day\|week\|month --start/--end --fq qfq\|hfq` | K 线（最大 2000 条） |
| `minute <代码>` | `--days 1~5` | 分时数据 |
| `technical <代码>` | `--group macd\|kdj\|rsi\|boll\|all` | 技术指标 |
| `chip <代码>` | | 筹码成本分布（仅 A 股） |

### 6.2 市场/板块

| 命令 | 说明 |
|---|---|
| `lhb --type institution\|hotmoney` | 龙虎榜 |
| `index constituent <代码>` | 指数成份股 |
| `market-overview --type summary\|margin\|valuation\|all` | 大盘画像 |
| `ipo --market hs\|hk\|us` | 新股日历 |
| `sector ranking` | 板块行情榜 |
| `sector constituent <代码>` | 板块成份股 |

### 6.3 研究/事件/资金/财务

| 命令 | 说明 |
|---|---|
| `score <代码>` | 综合评分 |
| `rating <代码>` | 机构评级 |
| `report <代码>` | 研报 |
| `events tags <代码>` | 42 类事件标签 |
| `risk <代码> --types pledge\|unlock` | 风险事件 |
| `calendar --event dividend\|ipo\|lockup_release` | 投资日历 |
| `fund flow <代码>` | 资金流向 |
| `fund short <代码>` | 卖空数据 |
| `fund margin <代码>` | 融资融券 |
| `shareholder <代码>` | 股东结构 |
| `dividend list <代码>` | 分红派息 |
| `finance <代码> --type lrb\|zcfz\|income` | 财务报表 |

### 6.4 ETF/热搜/宏观

| 命令 | 说明 |
|---|---|
| `etf detail <代码>` | ETF 详情（行情/持仓/经理） |
| `etf holdings <代码>` | ETF 持仓明细 |
| `hot stock\|wechat\|news\|board\|etf` | 热搜排行 |
| `macro indicator <指标> --year` | 27 个宏观指标（GDP/CPI/PMI/M2） |

### 6.5 期货/外汇/债券

| 命令 | 说明 |
|---|---|
| `futures detail <代码>` | 期货行情（CME/NYMEX/LME） |
| `forex list` | 外汇列表 |
| `quote fxCNH` | 外汇实时行情 |
| `bond detail <代码>` | 可转债详情 |

### 代码格式速查

| 市场 | 格式 | 示例 |
|---|---|---|
| A股 | `sh600519` / `sz000001` | 贵州茅台 / 平安银行 |
| 港股 | `hk00700` | 腾讯 |
| 美股 | `usAAPL` | Apple |
| 日股 | `t7203` | 丰田 |
| 韩股 | `ks005930` | 三星 |
| 板块 | `pt01801080` | 白酒 |
| 期货 | `fuGC` | 黄金 |
| 外汇 | `fxCNH` | 离岸人民币 |
| 可转债 | `sh113052` | 可转债 |

---

## 调用脚本速查

### `crypto_data.py` — 四大免费 API 统一入口

```bash
# 价格
python3 crypto_data.py coingecko price bitcoin,ethereum,solana

# 全球总览
python3 crypto_data.py coingecko global

# 热门趋势
python3 crypto_data.py coingecko trending

# DeFi TVL 排行
python3 crypto_data.py defillama chains

# 单链 TVL
python3 crypto_data.py defillama chain ethereum

# DEX 搜索
python3 crypto_data.py dexscreener search bonk

# Top Boosted
python3 crypto_data.py dexscreener top

# 市值排行
python3 crypto_data.py coincap assets 10

# 法币汇率
python3 crypto_data.py coincap rates
```

### Binance Skills Hub CLI（已安装技能）

```bash
# Meme 热点
node ~/.workbuddy/skills/binance-skills/binance-web3/meme-rush/scripts/cli.mjs \
  meme-rush '{"chainId":"CT_501","rankType":10,"limit":20}'

node ~/.workbuddy/skills/binance-skills/binance-web3/meme-rush/scripts/cli.mjs \
  topic-rush '{"chainId":"CT_501","rankType":10,"sort":10}'
```

---

## 自媒体数据选型指南

### 按内容类型推荐信息源

| 内容类型 | 主力信源 | 备选信源 | 交叉验证 |
|---|---|---|---|
| **Meme 币热度榜** | Binance `meme-rush` | DexScreener `top` | 对比两边的排名差异 |
| **链上聪明钱追踪** | Binance `trading-signal` | DexScreener `search` | 核对合约地址 |
| **DeFi 生态报告** | DefiLlama `chains/protocols` | CoinGecko `categories` | TVL vs 价格背离 |
| **新币速递** | Binance `meme-rush`(New) | DexScreener `boosts` | 确认 DEX 上线时间 |
| **代币基本面** | Binance `query-token-info` | CoinGecko `coin_info` | 市值/持有者/社交 |
| **合约安全性** | Binance `query-token-audit` | DexScreener `pairs` | 核查流动性锁仓 |
| **宏观联动** | DefiLlama `stablecoins` | CoinGecko `global` | 稳定币发行量 vs 总市值 |
| **跨链资金流** | DefiLlama `bridges` | Binance `inflow/rank` | 桥流量 vs 链上净流入 |
| **传统金融关联** | `westock-data` | CoinCap `rates` | 美元指数 vs BTC |

### 典型工作流：一份 Meme 币研报的数据流

```
1. 发现热点
   └→ Binance topic-rush → 获取当前 AI 话题
   └→ Binance meme-rush (New/Finalizing) → 新币列表

2. 交叉验证
   └→ DexScreener search → 对比同一代币在不同 DEX 的数据
   └→ CoinGecko trending → 确认是否在全市场有热度

3. 深度分析
   └→ Binance query-token-info → 合约/创建者/社交链接
   └→ Binance query-token-audit → 安全扫描
   └→ CoinGecko coin_info → 社区数据/开发者活跃度

4. 资金面
   └→ Binance trading-signal → 聪明钱是否参与
   └→ DefiLlama protocol → 相关协议 TVL 变化

5. 产出
   └→ 对比数据 → 撰写研报 → 制作视频
```

---

## 附录：端点编号速查

| 编号范围 | 信息源 |
|---|---|
| 1-18 | Binance Skills Hub (Web3 BAPI) |
| 19-27 | CoinGecko API v3 |
| 28-38 | DefiLlama API |
| 39-43 | DexScreener API |
| 44-49 | CoinCap API v2 |
| CLI | westock-data (40+ 命令) |
