# Binance Skills Hub — 完整 API 端点参考

> 来源：`@binance/binance-skills-hub` (2026-07-25)
> 整理：18 个技能，79+ 个 API 端点

---

## 一、CEX/SAPI 端点 (api.binance.com / www.binance.com)

### 1.1 P2P 交易 (p2p)

**公开接口** — Base: `https://www.binance.com`

| 端点 | 方法 | 说明 |
|---|---|---|
| `/bapi/c2c/v1/public/c2c/agent/quote-price` | GET | 快速 P2P 报价 |
| `/bapi/c2c/v1/public/c2c/agent/ad-list` | GET | 搜索 P2P 广告 |
| `/bapi/c2c/v1/public/c2c/agent/trade-methods` | GET | 列出法币支持的支付方式 |
| `/bapi/c2c/v1/public/c2c/agent/check-version` | GET | 技能版本检查 |

**需认证** — Base: `https://api.binance.com`

| 端点 | 方法 | 说明 |
|---|---|---|
| `/sapi/v1/c2c/orderMatch/listUserOrderHistory` | GET | P2P 订单历史 |
| `/sapi/v1/c2c/orderMatch/getUserOrderSummary` | GET | P2P 订单统计 |
| `/sapi/v1/c2c/agent/orderMatch/getUserOrderDetail` | POST | 订单详情 |
| `/sapi/v1/c2c/agent/orderMatch/listOrders` | POST | 订单列表（多条件筛选） |
| `/sapi/v1/c2c/agent/complaint/query-complaints` | POST | 申诉记录 |
| `/sapi/v1/c2c/agent/complaint/submit-evidence` | POST | 提交申诉证据 (WRITE) |
| `/sapi/v1/c2c/agent/complaint/get-complaint-flows` | POST | 申诉流程时间线 |
| `/sapi/v1/c2c/agent/complaint/cancel-complaint` | POST | 撤销申诉 (WRITE) |
| `/sapi/v1/c2c/agent/complaint/get-complaint-reasons` | POST | 获取申诉原因列表 |
| `/sapi/v1/c2c/agent/file-upload/get-s3-presigned-url` | GET | 获取 S3 上传凭证 |
| `/sapi/v1/c2c/agent/ads/getDetailByNo` | POST | 按编号查广告 |
| `/sapi/v1/c2c/agent/ads/listWithPagination` | POST | 用户广告列表 |
| `/sapi/v1/c2c/agent/ads/search` | POST | 市场广告搜索 |
| `/sapi/v1/c2c/agent/ads/getReferencePrice` | POST | 参考价格 |
| `/sapi/v1/c2c/agent/ads/getAvailableAdsCategory` | GET | 可发布广告类别 |
| `/sapi/v1/c2c/agent/ads/getPayMethodByUserId` | GET | 用户支付方式 |
| `/sapi/v1/c2c/agent/ads/listAllTradeMethods` | POST | 全部交易方式 |
| `/sapi/v1/c2c/agent/ads/post` | POST | 发布广告 (WRITE) |
| `/sapi/v1/c2c/agent/ads/update` | POST | 修改广告 (WRITE) |
| `/sapi/v1/c2c/agent/ads/updateStatus` | POST | 批量改状态 |
| `/sapi/v1/c2c/agent/merchant/getAdDetails` | GET | 商家主页 |
| `/sapi/v1/c2c/agent/digitalCurrency/list` | POST | 支持的加密货币 |
| `/sapi/v1/c2c/agent/fiatCurrency/list` | POST | 支持的法币 |

### 1.2 法币充提 (fiat)

**公开接口** — Base: `https://www.binance.com`

| 端点 | 方法 | 说明 |
|---|---|---|
| `/bapi/fiat/v1/public/fiatpayment/agent/get-capabilities` | GET | 查询国家支持的法币/币种 |
| `/bapi/fiat/v1/public/fiatpayment/agent/get-buy-and-sell-payment-methods` | GET | 买卖支付方式 |
| `/bapi/fiat/v1/public/fiatpayment/agent/get-deposit-and-withdraw-payment-methods` | GET | 充提支付方式 |
| `/bapi/fiat/v1/public/fiatpayment/agent/get-price` | GET | 法币/加密货币汇率 |

**需认证** — Base: `https://api.binance.com`

| 端点 | 方法 | 说明 |
|---|---|---|
| `/sapi/v1/fiat/deposit` | POST | 法币充值 |
| `/sapi/v2/fiat/withdraw` | POST | 法币提现 |
| `/sapi/v1/fiat/orders` | GET | 充提历史 |
| `/sapi/v1/fiat/payments` | GET | 支付历史 |
| `/sapi/v1/fiat/get-order-detail` | GET | 订单详情 |

### 1.3 现货/合约/理财等 (binance-cli)

通过 `binance-cli` 访问 Binance REST API `https://api.binance.com`，需 API Key + Secret。

| 模块 | 基础路径 | 读写 |
|---|---|---|
| 现货交易 | `/api/v3/*` | GET/POST/DELETE |
| USDS-M 合约 | `/fapi/v1/*` | GET/POST/DELETE |
| COIN-M 合约 | `/dapi/v1/*` | GET/POST/DELETE |
| 组合保证金 | `/papi/v1/*` | GET/POST/DELETE |
| 杠杆交易 | `/sapi/v1/margin/*` | GET/POST |
| 闪兑 | `/sapi/v1/convert/*` | GET/POST |
| 理财 | `/sapi/v1/simple-earn/*` | GET/POST |
| 质押 | `/sapi/v1/staking/*` | GET/POST |
| 法币 | `/sapi/v1/fiat/*` | GET/POST |
| 钱包 | `/sapi/v1/capital/*` | GET/POST |
| 子账户 | `/sapi/v1/sub-account/*` | GET/POST |
| WebSocket | `wss://stream.binance.com/*` | 实时推送 |

### 1.4 币安广场发文 (square-post)

Base: `https://www.binance.com`，需 OpenAPI Key。

| 端点 | 方法 | 说明 |
|---|---|---|
| `/bapi/composite/v1/public/pgc/openApi` | POST | 发文 API v1 |
| `/bapi/composite/v2/public/pgc/openApi` | POST | 发文 API v2（预签名上传） |

---

## 二、Web3/BAPI 端点 (web3.binance.com / defi)

> 全部为**公开接口**，无需认证。

### 2.1 代币化证券/RWA (binance-tokenized-securities-info)

Base: `https://www.binance.com`

| 端点 | 方法 | 说明 |
|---|---|---|
| `/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/rwa/stock/detail/list/ai` | GET | Ondo 代币化股票列表 |
| `/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/rwa/meta/ai` | GET | RWA 元数据（公司/CEO/行业） |
| `/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/rwa/market/status/ai` | GET | Ondo 市场开/休市状态 |
| `/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/rwa/asset/market/status/ai` | GET | 单资产交易状态 |
| `/bapi/defi/v2/public/wallet-direct/buw/wallet/market/token/rwa/dynamic/ai` | GET | 实时价格/持有者/基本面 |
| `/bapi/defi/v1/public/wallet-direct/buw/wallet/dex/market/token/kline/ai` | GET | 代币化股票 K 线 |

### 2.2 代币合约审计 (query-token-audit)

Base: `https://web3.binance.com`

| 端点 | 方法 | 说明 |
|---|---|---|
| `/bapi/defi/v1/public/wallet-direct/security/token/audit` | POST | 代币安全扫描（蜜罐/增发/跑路检测） |

### 2.3 聪明钱信号 (trading-signal)

Base: `https://web3.binance.com`

| 端点 | 方法 | 说明 |
|---|---|---|
| `/bapi/defi/v1/public/wallet-direct/buw/wallet/web/signal/smart-money/ai` | GET | 聪明钱买卖信号（触发价/maxGain/exitRate） |

### 2.4 代币搜索 (query-token-info)

Base: `https://web3.binance.com`

| 端点 | 方法 | 说明 |
|---|---|---|
| `/bapi/defi/v5/public/wallet-direct/buw/wallet/market/token/search/ai` | GET | 关键词/合约搜索代币 |
| `/bapi/defi/v1/public/wallet-direct/buw/wallet/dex/market/token/meta/info/ai` | GET | 代币静态元数据 |
| `/bapi/defi/v4/public/wallet-direct/buw/wallet/market/token/dynamic/info/ai` | GET | 实时市场数据（价格/量/持有者） |

### 2.5 钱包持仓 (query-address-info)

Base: `https://web3.binance.com`

| 端点 | 方法 | 说明 |
|---|---|---|
| `/bapi/defi/v3/public/wallet-direct/buw/wallet/address/pnl/active-position-list/ai` | GET | 钱包持仓列表（含价格/24h变化） |

### 2.6 Meme 热点追踪 (meme-rush)

Base: `https://web3.binance.com`

| 端点 | 方法 | 说明 |
|---|---|---|
| `/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse/rank/list/ai` | POST | Launchpad 生命周期排行（New/Finalizing/Migrated） |
| `/bapi/defi/v2/public/wallet-direct/buw/wallet/market/token/social-rush/rank/list/ai` | GET | AI 热点话题 + 关联代币 |

### 2.7 市场排行榜 (crypto-market-rank)

Base: `https://web3.binance.com`

| 端点 | 方法 | 说明 |
|---|---|---|
| `/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse/social/hype/rank/leaderboard/ai` | GET | 社交热度排行榜 |
| `/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse/unified/rank/list/ai` | POST | 统一代币排行（热搜/Alpha/股票） |
| `/bapi/defi/v1/public/wallet-direct/tracker/wallet/token/inflow/rank/query/ai` | POST | 聪明钱净流入排行 |
| `/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse/exclusive/rank/list/ai` | GET | Pulse Launchpad TOP Meme |
| `/bapi/defi/v1/public/wallet-direct/market/leaderboard/query/ai` | GET | 交易员 PnL 排行榜 |

### 2.8 世界杯 AI 分析 (binance-sports-ai-analyzer)

Base: `https://web3.binance.com`

| 端点 | 方法 | 说明 |
|---|---|---|
| `/bapi/defi/v1/public/wc-assistant/match/recent-unfinished` | GET | 未完成比赛列表 |
| `/bapi/defi/v1/public/wc-assistant/match/resolve-by-slug` | POST | 按 slug 解析比赛 |
| `/bapi/defi/v1/public/wc-assistant/match/prediction/{cmid}` | GET | 基础预测概率/信号 |
| `/bapi/defi/v1/public/wc-assistant/match/news-insights/{cmid}` | GET | AI 事件卡片 |
| `/bapi/defi/v1/public/wc-assistant/match/recompute-final/{cmid}` | POST | 重新计算概率 |
| `/bapi/defi/v1/public/wc-assistant/match/master-analysis/{cmid}` | GET | AI 主分析 |
| `/bapi/defi/v1/public/wallet-direct/prediction/web/market/detail-by-slug` | POST | 预测市场详情 |

### 2.9 CLI 抽象技能（后端端点不公开）

以下技能通过 `baw` CLI 调用，具体 API 端点被抽象封装：

| 技能 | 功能 |
|---|---|
| binance-agentic-wallet | 钱包连接/余额/发送/兑换/限价单/DeFi |
| binance-leaderboard | 排行榜/地址分析/Gem Hunter |
| binance-wallet-tracker | 钱包追踪/群组管理/实时推送 |
| binance-trading-signal | 自定义信号策略管理 |

---

## 三、其他外部端点

### 3.1 链上支付 (onchain-pay)

Base: `https://api.commonservice.io`，需 RSA SHA256 签名认证。

| 端点 | 方法 | 说明 |
|---|---|---|
| `/papi/v1/ramp/connect/buy/payment-method-list` | POST | 支付方式列表 |
| `/papi/v2/ramp/connect/buy/payment-method-list` | POST | 所有支付方式（简化版） |
| `/papi/v1/ramp/connect/buy/trading-pairs` | POST | 支持的法币/币种对 |
| `/papi/v1/ramp/connect/buy/estimated-quote` | POST | 实时报价 |
| `/papi/v1/ramp/connect/buy/pre-order` | POST | 创建买单 (WRITE) |
| `/papi/v1/ramp/connect/order` | POST | 订单查询 |
| `/papi/v1/ramp/connect/crypto-network` | POST | 支持的网络/提币费 |
| `/papi/v1/ramp/connect/buy/p2p/trading-pairs` | POST | P2P 交易对 |

### 3.2 支付助手 (payment)

Base: `https://bpay.binanceapi.com`，需 API Key/Secret。

| 端点 | 方法 | 说明 |
|---|---|---|
| `/binancepay/openapi/user/c2c/parseQr` | POST | 解析 C2C 二维码 |
| `/binancepay/openapi/user/c2c/confirmPayment` | POST | C2C 确认支付 (WRITE) |
| `/binancepay/openapi/user/c2c/queryPaymentStatus` | POST | C2C 订单查询 |
| `/binancepay/openapi/user/pix/parseQr` | POST | 解析 PIX 二维码 |
| `/binancepay/openapi/user/pix/confirmPayment` | POST | PIX 确认支付 (WRITE) |
| `/binancepay/openapi/user/pix/queryPaymentStatus` | POST | PIX 订单查询 |

### 3.3 外部 K 线 (query-token-info 外联)

Base: `https://dquery.sintral.io`

| 端点 | 方法 | 说明 |
|---|---|---|
| `/u-kline/v1/k-line/candles` | GET | OHLCV K 线数据 |

---

## 附录：通用规则

| 规则 | 说明 |
|---|---|
| 图标 URL | 相对路径时前置 `https://bin.bnbstatic.com` |
| 百分比字段 | 已格式化为字符串，直接展示即可 |
| User-Agent | 需携带 `binance-web3/2.0 (Skill)` |
| 限频 | 文档未明确，建议控制请求频率 |
| 支持的链 | Solana=`CT_501`、BSC=`56`、Base=`8453` |
