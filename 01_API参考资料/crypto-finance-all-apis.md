# 加密货币/金融市场 — ALL Skills API 端点综合分析

> 整理日期：2026-07-25
> 覆盖：Binance Skills Hub（18技能）+ westock-data + neodata-financial-search + 通达信 MCP

---

# 第一部分：币安链上/Web3（公开，无需 Key）

> 全部通过 `https://web3.binance.com` 或 `https://www.binance.com/bapi/defi`，User-Agent 需带 `binance-web3/2.0 (Skill)`

## 1.1 🔊 Meme 币热点（meme-rush）

| 端点 | 方法 | 说明 |
|---|---|---|
| `/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse/rank/list/ai` | POST | Launchpad 生命周期（New/Finalizing/Migrated），chainId: CT_501/56/8453 |
| `/bapi/defi/v2/public/wallet-direct/buw/wallet/market/token/social-rush/rank/list/ai` | GET | AI 热点话题 + 关联代币，chainId: CT_501/56 |

## 1.2 💰 聪明钱信号（trading-signal / binance-trading-signal）

| 端点 | 方法 | 说明 |
|---|---|---|
| `/bapi/defi/v1/public/wallet-direct/buw/wallet/web/signal/smart-money/ai` | GET | 聪明钱买卖信号（触发价/maxGain/exitRate/链/合约） |

## 1.3 📊 市场排行榜（crypto-market-rank）

Base: `https://web3.binance.com`

| 端点 | 方法 | 说明 |
|---|---|---|
| `/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse/social/hype/rank/leaderboard/ai` | GET | 社交热度排行榜 |
| `/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse/unified/rank/list/ai` | POST | 统一排行（Trending/TopSearch/Alpha/Stock） |
| `/bapi/defi/v1/public/wallet-direct/tracker/wallet/token/inflow/rank/query/ai` | POST | 聪明钱净流入排行 |
| `/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse/exclusive/rank/list/ai` | GET | Pulse Launchpad TOP Meme（BSC） |
| `/bapi/defi/v1/public/wallet-direct/market/leaderboard/query/ai` | GET | 交易员 PnL 排行榜 |

## 1.4 🔍 代币搜索与分析（query-token-* 系列）

Base: `https://web3.binance.com`

| 端点 | 方法 | 说明 |
|---|---|---|
| `/bapi/defi/v5/public/wallet-direct/buw/wallet/market/token/search/ai` | GET | 关键词/合约搜索代币（全链） |
| `/bapi/defi/v1/public/wallet-direct/buw/wallet/dex/market/token/meta/info/ai` | GET | 静态元数据（名称/logo/社交链接/创建者） |
| `/bapi/defi/v4/public/wallet-direct/buw/wallet/market/token/dynamic/info/ai` | GET | 实时数据（价格/24h变化/量/持有者/流动性） |
| `/bapi/defi/v1/public/wallet-direct/security/token/audit` | POST | 合约安全审计（蜜罐/增发/跑路检测） |

## 1.5 🏦 钱包/持仓/交易（query-address-info + CLI 抽象）

| 端点 | 方法 | 说明 |
|---|---|---|
| `/bapi/defi/v3/public/wallet-direct/buw/wallet/address/pnl/active-position-list/ai` | GET | 钱包持仓列表（价格/24h变化） |

CLI 抽象技能（binance-agentic-wallet / leaderboard / wallet-tracker）通过 `baw` CLI 调用，端点不公开。

## 1.6 🏭 代币化证券/RWA（binance-tokenized-securities-info）

Base: `https://www.binance.com`

| 端点 | 方法 | 说明 |
|---|---|---|
| `/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/rwa/stock/detail/list/ai` | GET | Ondo 代币化股票列表 |
| `/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/rwa/meta/ai` | GET | 公司元数据（CEO/行业/报告） |
| `/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/rwa/market/status/ai` | GET | Ondo 市场开/休市状态 |
| `/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/rwa/asset/market/status/ai` | GET | 单资产交易状态 |
| `/bapi/defi/v2/public/wallet-direct/buw/wallet/market/token/rwa/dynamic/ai` | GET | 实时价格/持有者/基本面 |
| `/bapi/defi/v1/public/wallet-direct/buw/wallet/dex/market/token/kline/ai` | GET | K 线数据 |

## 1.7 ⚽ 世界杯 AI（binance-sports-ai-analyzer）

Base: `https://web3.binance.com`

| 端点 | 方法 | 说明 |
|---|---|---|
| `/bapi/defi/v1/public/wc-assistant/match/recent-unfinished` | GET | 未完成比赛 |
| `/bapi/defi/v1/public/wc-assistant/match/resolve-by-slug` | POST | 解析比赛 slug |
| `/bapi/defi/v1/public/wc-assistant/match/prediction/{cmid}` | GET | 预测概率/信号 |
| `/bapi/defi/v1/public/wc-assistant/match/news-insights/{cmid}` | GET | AI 事件卡片 |
| `/bapi/defi/v1/public/wc-assistant/match/recompute-final/{cmid}` | POST | 重算概率 |
| `/bapi/defi/v1/public/wc-assistant/match/master-analysis/{cmid}` | GET | AI 主分析 |
| `/bapi/defi/v1/public/wallet-direct/prediction/web/market/detail-by-slug` | POST | 预测市场详情 |

---

# 第二部分：币安 CEX/SAPI（大多需 Key）

## 2.1 P2P 交易（p2p）

**公开** — Base: `https://www.binance.com`

| 端点 | 方法 | 说明 |
|---|---|---|
| `/bapi/c2c/v1/public/c2c/agent/quote-price` | GET | P2P 报价 |
| `/bapi/c2c/v1/public/c2c/agent/ad-list` | GET | P2P 广告搜索 |
| `/bapi/c2c/v1/public/c2c/agent/trade-methods` | GET | 支付方式 |

**需 API Key** — Base: `https://api.binance.com`（27个端点，详见原文档）

## 2.2 法币（fiat）

**公开** — Base: `https://www.binance.com`

| 端点 | 方法 | 说明 |
|---|---|---|
| `/bapi/fiat/v1/public/fiatpayment/agent/get-capabilities` | GET | 国家/法币/币种能力 |
| `/bapi/fiat/v1/public/fiatpayment/agent/get-buy-and-sell-payment-methods` | GET | 买卖支付方式 |
| `/bapi/fiat/v1/public/fiatpayment/agent/get-deposit-and-withdraw-payment-methods` | GET | 充提支付方式 |
| `/bapi/fiat/v1/public/fiatpayment/agent/get-price` | GET | 法币/加密货币汇率 |

**需 API Key** — Base: `https://api.binance.com`（5个端点，详见原文档）

## 2.3 现货/合约/理财（binance-cli）

通过 `binance-cli` 访问标准 Binance REST API。覆盖：现货(`/api/v3/*`)、USDS-M合约(`/fapi/v1/*`)、COIN-M合约(`/dapi/v1/*`)、杠杆、闪兑、理财、质押、钱包、子账户、WebSocket 实时流。

## 2.4 支付/链上入金

| Base URL | 端点前缀 | 认证 | 说明 |
|---|---|---|---|
| `https://bpay.binanceapi.com` | `/binancepay/openapi/user/*` | API Key/Secret | Binance Pay（C2C/PIX 支付） |
| `https://api.commonservice.io` | `/papi/v1/ramp/connect/buy/*` | RSA SHA256 | Onchain Pay（法币买币） |

## 2.5 外部 K 线

| 端点 | 方法 | 说明 |
|---|---|---|
| `https://dquery.sintral.io/u-kline/v1/k-line/candles` | GET | OHLCV K 线（用于技术分析） |

---

# 第三部分：金融数据平台 — 中国/全球传统金融

## 3.1 westock-data（腾讯自选股 CLI）

> 无原始 HTTP 端点，通过 CLI `westock-data <命令>` 调用，覆盖 A股/港股/美股/日韩/期货/外汇/可转债/宏观。无用户级鉴权。

### 3.1.1 行情相关

| 命令 | 说明 | 市场 |
|---|---|---|
| `search <关键词>` | 搜索股票/ETF/板块/期货/外汇 | A/港/美/日/韩 |
| `quote <代码>` | 实时行情（含涨跌停/ADR/盘前盘后） | A/港/美/日/韩 |
| `kline <代码> --period day\|week\|month` | K线（最大2000条） | A/港/美/期货/外汇 |
| `minute <代码> --days 1~5` | 分时 | A/港/美/期货 |
| `technical <代码> --group macd\|kdj\|rsi\|boll\|all` | 技术指标 | A/港/美 |
| `chip <代码>` | 筹码成本分布 | 仅A股 |

### 3.1.2 市场/板块

| 命令 | 说明 | 市场 |
|---|---|---|
| `lhb --type institution\|hotmoney` | 龙虎榜 | A股 |
| `index constituent <代码>` | 指数成份股 | A股/港股 |
| `index list` | 指数清单（1400+） | — |
| `market-overview --type summary\|margin\|valuation` | 大盘画像（8维） | A股 |
| `connect --exchange sh\|sz` | 沪深港通标的 | A股 |
| `ipo --market hs\|hk\|us` | 新股日历 | 沪深/港/美 |
| `sector ranking` | 板块行情榜（涨幅/资金/北向） | A股 |
| `sector constituent <代码>` | 板块成份股 | A股 |

### 3.1.3 研究/事件/资金/财务

| 命令 | 说明 | 市场 |
|---|---|---|
| `score <代码>` | 综合评分（资金/基本面/风险/技术） | A/港/美 |
| `rating <代码>` | 机构评级/目标价 | 港股/美股 |
| `consensus <代码>` | 一致预期 | A股/港股 |
| `report <代码>` | 研报 | 全市场 |
| `events tags <代码>` | 42类事件标签 | 全市场 |
| `risk <代码> --types pledge\|unlock` | 8种风险事件 | A股 |
| `calendar --event dividend\|ipo\|lockup_release` | 投资日历 | 沪深/港/美 |
| `fund flow <代码>` | 资金流向（主力/散户/超大单） | A股/港股 |
| `fund short <代码>` | 卖空数据 | 港股/美股 |
| `fund margin <代码>` | 融资融券 | A股 |
| `fund block <代码>` | 大宗交易 | A股 |
| `shareholder <代码>` | 股东结构 | A股/港股 |
| `dividend list <代码>` | 分红派息 | A/港/美 |
| `buyback <代码>` | 公司回购 | A股/港股 |
| `finance <代码> --type lrb\|zcfz\|income` | 三大财务报表 | A/港/美 |

### 3.1.4 ETF / 热搜 / 宏观

| 命令 | 说明 | 市场 |
|---|---|---|
| `etf detail <代码>` | ETF详情（行情/持仓/经理） | — |
| `etf holdings <代码>` | ETF持仓明细 | — |
| `etf nav <代码>` | 净值历史 | — |
| `hot stock\|wechat\|news\|board\|etf` | 热搜排行 | — |
| `macro indicator <指标> --year` | 27个宏观指标（GDP/CPI/PMI/M2/利率等） | 中国/全球 |

### 3.1.5 期货 / 外汇 / 债券

| 命令 | 说明 | 市场 |
|---|---|---|
| `futures detail <代码>` | 期货行情 | CME/NYMEX/LME等 |
| `forex list` | 外汇列表 | 全货币对 |
| `quote fxCNH` | 外汇实时行情 | 人民币/主要货币 |
| `bond detail <代码>` | 可转债详情（含转股价值/溢价率） | 沪深 |

### 3.1.6 代码格式速查

| 市场 | 格式 | 示例 |
|---|---|---|
| 沪市A股 | `sh<代码>` | `sh600519` |
| 深市A股 | `sz<代码>` | `sz000001` |
| 北交所 | `bj<代码>` | `bj430047` |
| 港股 | `hk<代码>` | `hk00700` |
| 美股 | `us<代码>` | `usAAPL` |
| 日股 | `t<代码>` | `t7203` |
| 韩股 | `ks<代码>` | `ks005930` |
| 板块 | `pt<代码>` | `pt01801080` |
| 期货 | `fu<代码>` | `fuGC` |
| 外汇 | `fx<代码>` | `fxCNH` |
| 可转债 | `sh/sz<代码>` | `sh113052` |

---

## 3.2 neodata-financial-search（自然语言金融搜索）

### API 端点

| 端点 | 方法 | 认证 | 说明 |
|---|---|---|---|
| `https://copilot.tencent.com/agenttool/v1/neodata` | POST JSON | Bearer Token（`connect_cloud_service` 获取，12h缓存） | 自然语言 → 结构化金融数据 |

**请求体：**
```json
{"query": "...", "channel": "neodata", "sub_channel": "workbuddy", "data_type": "all"}
```

**调用方式：** `python3 scripts/query.py --query "..."`（鉴权自动管理）

### 覆盖的 8 大类 60+ 子能力

| 类别 | 覆盖范围 | 市场 |
|---|---|---|
| **股票-基本面** | 公司概况、三表、财务指标、股东、主营构成、经营指标 | A/港/美 |
| **股票-行情** | 实时行情、K线、资金流向、龙虎榜、两融、大宗、卖空、港股通 | A/港/美/日/韩 |
| **股票-事件** | 风险事件、业绩披露、股权变动、分红、回购、业绩会议 | A/港/美 |
| **股票-投研** | 机构观点/评级/盈利预测、估值、行业对比、概念归类 | A/港/美 |
| **指数** | 成份股、实时/历史行情、交易统计、估值、债券指数 | A/港/美/全球 |
| **板块** | 成份股、行情、资金统计、异动监测、排行、估值 | A股 |
| **基金** | 基础信息、行情、净值、业绩、回撤、持仓、规模 | 公募 |
| **宏观** | 全球/中国指标、利率、经济事件日历 | 全球 |
| **外汇/大宗** | 人民币中间价、货币对、黄金、期货（能源/金属/农产品/利率） | 全球 |

---

## 3.3 wb-finance-skill（金融路由层）

> 不直接提供数据，按优先级将查询路由给子技能。

**路由优先级：**

| 优先级 | 数据源 | 适用场景 |
|---|---|---|
| 1 | `neodata-financial-search` | 自然语言查询、基本面、事件、宏观 |
| 2 | `westock-data` | 技术指标、筹码、股东明细、ETF持仓、龙虎榜、两融 |
| 3 | 通达信 MCP（可选） | 深度结构化财务、补充验证 |
| 4 | WebSearch | 以上都覆盖不到时 |

---

## 3.4 通达信 MCP（可选安装，共10个工具）

| 工具 | 说明 | 市场 |
|---|---|---|
| `tdx_lookup_stock` | 代码检索 | A/港/美/指数/基金 |
| `tdx_quotes` | 实时行情 | A/港/美 |
| `tdx_kline` | K线（5分钟~年线12周期） | A/港/美 |
| `tdx_indicator_select` | 自然语言指标查询（PE/PB/主营） | A股 |
| `tdx_screener` | 自然语言条件选股 | A股 |
| `tdx_api_data` | 深度财务数据（三表/大宗/两融/龙虎榜/股东/分红/解禁/港股三表） | A/港 |
| `wenda_news_query` | 新闻查询 | A/港 |
| `wenda_notice_query` | 公告查询 | A/港 |
| `wenda_report_query` | 研报查询 | A/港 |
| `wenda_macro_query` | 宏观数据 | 中国 |

---

# 第四部分：综合分析矩阵

## 按数据维度交叉对比

| 数据维度 | 币安 BAPI | westock-data | neodata | 通达信 MCP |
|---|---|---|---|---|
| **加密货币行情** | ✅ DEX价格/24h变化/量/流动性 | ❌ | ❌ | ❌ |
| **Meme币热点** | ✅ 话题聚合 + 生命周期 | ❌ | ❌ | ❌ |
| **聪明钱信号** | ✅ 链上买卖/maxGain | ❌ | ❌ | ❌ |
| **合约安全审计** | ✅ 蜜罐/增发/权限 | ❌ | ❌ | ❌ |
| **代币化证券** | ✅ Ondo RWA | ❌ | ❌ | ❌ |
| **钱包持仓** | ✅ 地址画像 | ❌ | ❌ | ❌ |
| **传统股票行情** | ❌ | ✅ A/港/美/日/韩 | ✅ A/港/美/日/韩 | ✅ A/港/美 |
| **K线/技术指标** | ✅ DEX K线 | ✅ 全市场 | ❌ | ✅ A/港/美 |
| **财务报表** | ❌ | ✅ A/港/美 | ✅ A/港/美 | ✅ A/港（深度） |
| **龙虎榜** | ❌ | ✅ A股 | ✅ A股 | ✅ A股 |
| **资金流向** | ✅ 聪明钱净流入 | ✅ A股/港股 | ✅ A股/港股 | ❌ |
| **宏观指标** | ❌ | ✅ 27个中国/全球 | ✅ 全球 | ✅ 中国 |
| **机构研报** | ❌ | ✅ 全市场 | ✅ A/港/美 | ✅ A/港 |
| **ETF 数据** | ❌ | ✅ 详情/持仓/净值 | ❌ | ❌ |
| **自然语言查询** | ❌ | ❌ | ✅ | ✅（选股/指标） |
| **预测/信号** | ✅ AI信号+置信度 | ❌ | ❌ | ❌ |

## 按数据用途推荐技能

| 用途 | 推荐技能组合 |
|---|---|
| Meme 币短线追踪 | `meme-rush` + `trading-signal` |
| 代币基本面研究 | `query-token-info` + `query-token-audit` + `query-address-info` |
| 市场热度感知 | `crypto-market-rank` + `meme-rush(topic-rush)` |
| 传统金融投研（股票） | `westock-data` + `neodata-financial-search` |
| A股量化选股 | `westock-data` + `westock-tool` + 通达信 MCP |
| 跨市场宏观分析 | `neodata-financial-search` + `westock-data macro` |
| 全栈金融自媒体 | `westock-data` + `meme-rush` + `neodata` + `wb-finance-skill` |
