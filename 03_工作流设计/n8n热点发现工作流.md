# n8n 热点币种发现工作流

> 目标：每天自动发现热门新币 / 土狗 / 话题币，写入候选池，不直接污染正式跟踪池 `coin_basic`

## 一、先说结论

这条工作流不要写进 `coin_basic`，而是写进独立候选池：

- `token_discovery_candidates`：去重后的候选项目主表
- `token_discovery_snapshots`：每次发现时的行情快照表

这样你就能做到：

1. 自动发现热点币
2. 支持未被 CMC 收录的币
3. 支持高频更新和淘汰
4. 人工确认后再转入 `coin_basic`

## 二、推荐数据源

基于当前已经验证可跑通的接口，发现层优先使用：

1. `CoinGecko trending`
2. `DexScreener boosts`
3. `DexScreener top`
4. `Binance Web3 social-rush`
5. `Binance Web3 pulse rank`
6. `Binance Web3 unified rank`

不建议第一版接入：

- `CoinCap`
- `DefiLlama bridges`
- `Binance trading-signal`
- `Binance token audit`

因为这些要么当前环境不稳定，要么缺最小参数，要么受限。

## 三、建表 SQL

### 1）候选池主表

```sql
CREATE TABLE token_discovery_candidates (
    token_key VARCHAR(256) PRIMARY KEY,
    token_symbol VARCHAR(64),
    token_name VARCHAR(256),
    chain_id VARCHAR(64),
    contract_address TEXT,
    source_platform VARCHAR(64),
    source_type VARCHAR(64),
    source_count INT DEFAULT 1,
    source_tags TEXT,
    hot_score NUMERIC,
    risk_score NUMERIC,
    price_usd NUMERIC,
    volume_24h NUMERIC,
    liquidity_usd NUMERIC,
    market_cap NUMERIC,
    fdv NUMERIC,
    pair_url TEXT,
    first_seen_at TIMESTAMP DEFAULT NOW(),
    last_seen_at TIMESTAMP DEFAULT NOW(),
    seen_count INT DEFAULT 1,
    candidate_status VARCHAR(32) DEFAULT '待筛选',
    remark TEXT
);
```

### 2）发现快照表

```sql
CREATE TABLE token_discovery_snapshots (
    token_key VARCHAR(256) NOT NULL,
    record_time TIMESTAMP NOT NULL,
    token_symbol VARCHAR(64),
    token_name VARCHAR(256),
    chain_id VARCHAR(64),
    contract_address TEXT,
    source_platform VARCHAR(64),
    source_type VARCHAR(64),
    hot_score NUMERIC,
    price_usd NUMERIC,
    volume_24h NUMERIC,
    liquidity_usd NUMERIC,
    market_cap NUMERIC,
    fdv NUMERIC,
    pair_url TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (token_key, record_time)
);
```

### 3）`token_key` 规则

优先级：

1. `chain_id + ':' + contract_address`
2. 如果没有合约地址：`source_platform + ':' + token_symbol`

例如：

- `solana:DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263`
- `coingecko:ZAMA`

## 四、n8n 工作流结构

推荐工作流名：

`Subworkflow 0 - Hot Token Discovery`

触发器：

- 第一版先用 `Manual Trigger`
- 跑通后改成 `Cron`，建议每 2 小时或每 4 小时

### 节点顺序

1. `Manual Trigger`
2. `Set Chain Config`
3. `Get CoinGecko Trending`
4. `Normalize CoinGecko Trending`
5. `Get DexScreener Boosts`
6. `Normalize DexScreener Boosts`
7. `Get DexScreener Top`
8. `Normalize DexScreener Top`
9. `Get Binance Social Rush`
10. `Normalize Binance Social Rush`
11. `Get Binance Pulse Rank`
12. `Normalize Binance Pulse Rank`
13. `Get Binance Unified Rank`
14. `Normalize Binance Unified Rank`
15. `Merge All Sources`
16. `Deduplicate And Score`
17. `Loop Candidates`
18. `Upsert Candidate`
19. `Insert Candidate Snapshot`
20. `Build Hot Token Summary`
21. `Send Feishu Summary`

## 五、英文命名规范

建议统一使用：

`Verb + Source/Target`

比如：

- `Get CoinGecko Trending`
- `Normalize DexScreener Top`
- `Upsert Candidate`
- `Send Feishu Summary`

这样做的原因很简单：

1. 执行日志更直观
2. 每个节点在干什么一眼可见
3. 后面增加新数据源时也不会把命名体系搞乱

推荐命名如下：

| 节点类型 | 英文名称 | 说明 |
| --- | --- | --- |
| Trigger | `Manual Trigger` | 手动测试 |
| Set | `Set Chain Config` | 集中配置 Binance 链参数 |
| HTTP Request | `Get CoinGecko Trending` | 拉取 CoinGecko 热门榜 |
| Code | `Normalize CoinGecko Trending` | 标准化 CoinGecko 返回 |
| HTTP Request | `Get DexScreener Boosts` | 拉取 DexScreener Boosts |
| Code | `Normalize DexScreener Boosts` | 标准化 Boosts 返回 |
| HTTP Request | `Get DexScreener Top` | 拉取 DexScreener Top |
| Code | `Normalize DexScreener Top` | 标准化 Top 返回 |
| HTTP Request | `Get Binance Social Rush` | 拉取 Binance Social Rush |
| Code | `Normalize Binance Social Rush` | 标准化 Social Rush 返回 |
| HTTP Request | `Get Binance Pulse Rank` | 拉取 Binance Pulse Rank |
| Code | `Normalize Binance Pulse Rank` | 标准化 Pulse Rank 返回 |
| HTTP Request | `Get Binance Unified Rank` | 拉取 Binance Unified Rank |
| Code | `Normalize Binance Unified Rank` | 标准化 Unified Rank 返回 |
| Merge | `Merge All Sources` | 一次汇总 6 路标准化结果 |
| Code | `Deduplicate And Score` | 去重并热度打分 |
| Loop Over Items | `Loop Candidates` | 逐条写入候选池 |
| Postgres | `Upsert Candidate` | 写主表 |
| Postgres | `Insert Candidate Snapshot` | 写快照表 |
| Code | `Build Hot Token Summary` | 构造摘要文本 |
| HTTP Request | `Send Feishu Summary` | 推送飞书 |

## 六、连接方式

这条工作流不要串成一条直线，而是先经过一个配置节点，再从配置节点分 6 条支路，然后统一汇总到一个多输入 `Merge`：

```text
Manual Trigger
  -> Set Chain Config
      ├─> Get CoinGecko Trending -> Normalize CoinGecko Trending ─┐
      ├─> Get DexScreener Boosts -> Normalize DexScreener Boosts ─┤
      ├─> Get DexScreener Top -> Normalize DexScreener Top ───────┤
      ├─> Get Binance Social Rush -> Normalize Binance Social Rush ┤
      ├─> Get Binance Pulse Rank -> Normalize Binance Pulse Rank ──┤
      └─> Get Binance Unified Rank -> Normalize Binance Unified Rank┘

All Normalize nodes
  -> Merge All Sources
  -> Deduplicate And Score
      ├─> Loop Candidates -> Upsert Candidate -> Insert Candidate Snapshot
      └─> Build Hot Token Summary -> Send Feishu Summary
```

关键点：

- `Merge All Sources` 选择 `Append`
- `Merge All Sources` 的 `Number of Inputs` 设为 `6`
- 6 个输入都接各自的 `Normalize` 节点，不要接 `HTTP Request` 节点
- Binance 相关 `HTTP Request` 节点都从 `Set Chain Config` 读取链参数，不要手写 `CT_501`
- `Loop Candidates` 使用 `loop` 输出接写库节点
- `Insert Candidate Snapshot` 再回接到 `Loop Candidates`
- 摘要节点从 `Deduplicate And Score` 直接分一条线出来

## 七、逐节点搭建步骤

下面按你在 n8n 中的实际操作顺序来写。

### 1）`Manual Trigger`

- 节点类型：`Manual Trigger`
- 节点名称：`Manual Trigger`
- 搭建步骤：
  1. 新建工作流
  2. 添加 `Manual Trigger`
  3. 把它放在画布最左侧

如果后面要给主调度调用，再额外新增 `When Executed by Another Workflow`，命名为 `Execute Workflow Trigger`。

### 2）`Set Chain Config`

- 节点类型：`Edit Fields (Set)`
- 节点名称：`Set Chain Config`
- 搭建步骤：
  1. 从 `Manual Trigger` 拉线
  2. 添加 `Edit Fields` 或 `Set` 节点
  3. `Mode` 选 `JSON`
  4. `Include Other Input Fields` 保持关闭
  5. JSON 输入框直接粘贴下面这段：

```json
{
  "chain_name": "solana",
  "binance_chain_id": "CT_501",
  "social_rank_type": 20,
  "pulse_rank_type": 10,
  "unified_rank_type": "TRENDING",
  "rank_limit": 20,
  "social_sort": 20
}
```

  6. 如果后面要切到 BSC 或 Base，只需要改这一处

推荐映射：

- `solana` -> `CT_501`
- `bsc` -> `56`
- `base` -> `8453`

### 3）`Get CoinGecko Trending`

- 节点类型：`HTTP Request`
- 节点名称：`Get CoinGecko Trending`
- 搭建步骤：
  1. 从 `Set Chain Config` 拉一条线出来
  2. 添加 `HTTP Request`
  3. `Method` 选择 `GET`
  4. `URL` 填：

```text
https://api.coingecko.com/api/v3/search/trending
```

  5. `Response Format` 选 `JSON`
  6. 其他参数保持默认

### 4）`Normalize CoinGecko Trending`

- 节点类型：`Code`
- 节点名称：`Normalize CoinGecko Trending`
- 搭建步骤：
  1. 从 `Get CoinGecko Trending` 拉线
  2. 添加 `Code`
  3. `Mode` 选 `Run Once for All Items`
  4. 粘贴下面代码：

```javascript
const rows = $input.first().json.coins || [];

function mapChainName(chainId) {
  const mapping = {
    CT_501: 'Solana',
    '56': 'BSC',
    '8453': 'Base',
    solana: 'Solana',
    bsc: 'BSC',
    base: 'Base',
    ethereum: 'Ethereum'
  };
  return mapping[chainId] || chainId || '-';
}

return rows.map((row) => {
  const item = row.item || {};
  const chainId = item.asset_platform_id || 'unknown';
  const tokenKey = item.id ? `coingecko:${item.id}` : `coingecko:${item.symbol || item.name}`;

  return {
    json: {
      token_key: tokenKey,
      token_symbol: item.symbol || null,
      token_name: item.name || null,
      chain_id: chainId,
      chain_name: mapChainName(chainId),
      contract_address: null,
      source_platform: 'coingecko',
      source_type: 'trending',
      hot_score_base: 20,
      price_usd: item.data?.price ?? null,
      volume_24h: item.data?.total_volume ?? null,
      liquidity_usd: null,
      market_cap: item.data?.market_cap ?? null,
      fdv: item.data?.fully_diluted_valuation ?? null,
      pair_url: item.data?.sparkline ?? null
    }
  };
});
```

### 5）`Get DexScreener Boosts`

- 节点类型：`HTTP Request`
- 节点名称：`Get DexScreener Boosts`
- 搭建步骤：
  1. 从 `Set Chain Config` 再拉一条新线
  2. 添加 `HTTP Request`
  3. `Method` 选 `GET`
  4. `URL` 填：

```text
https://api.dexscreener.com/token-boosts/latest/v1
```

  5. `Response Format` 选 `JSON`

### 6）`Normalize DexScreener Boosts`

- 节点类型：`Code`
- 节点名称：`Normalize DexScreener Boosts`
- 搭建步骤：
  1. 从 `Get DexScreener Boosts` 拉线
  2. 添加 `Code`
  3. `Mode` 选 `Run Once for All Items`
  4. 粘贴代码：

```javascript
const rows = $input.all().map(item => item.json);

function pickShortText(...values) {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) {
      return value.trim().slice(0, 120);
    }
  }
  return null;
}

function mapChainName(chainId) {
  const mapping = {
    CT_501: 'Solana',
    '56': 'BSC',
    '8453': 'Base',
    solana: 'Solana',
    bsc: 'BSC',
    base: 'Base',
    ethereum: 'Ethereum'
  };
  return mapping[chainId] || chainId || '-';
}

return rows.map((row) => {
  const chainId = row.chainId || 'unknown';
  const address = row.tokenAddress || '';
  const tokenKey = address ? `${chainId}:${address}` : `dexscreener:${row.url || row.description || row.chainId}`;
  const tokenSymbol = pickShortText(row.symbol, row.tokenAddress);
  const tokenName = pickShortText(
    row.symbol,
    row.name,
    row.baseToken?.name,
    row.baseToken?.symbol,
    row.description
  );

  return {
    json: {
      token_key: tokenKey,
      token_symbol: tokenSymbol,
      token_name: tokenName,
      chain_id: chainId,
      chain_name: mapChainName(chainId),
      contract_address: address || null,
      source_platform: 'dexscreener',
      source_type: 'boosts',
      hot_score_base: 30,
      price_usd: null,
      volume_24h: null,
      liquidity_usd: null,
      market_cap: null,
      fdv: null,
      pair_url: row.url || null
    }
  };
});
```

### 7）`Get DexScreener Top`

- 节点类型：`HTTP Request`
- 节点名称：`Get DexScreener Top`
- 搭建步骤：
  1. 从 `Set Chain Config` 拉第三条线
  2. 添加 `HTTP Request`
  3. `Method` 选 `GET`
  4. `URL` 填：

```text
https://api.dexscreener.com/token-boosts/top/v1
```

  5. `Response Format` 选 `JSON`

### 8）`Normalize DexScreener Top`

- 节点类型：`Code`
- 节点名称：`Normalize DexScreener Top`
- 搭建步骤：
  1. 从 `Get DexScreener Top` 拉线
  2. 添加 `Code`
  3. `Mode` 选 `Run Once for All Items`
  4. 粘贴代码：

```javascript
const rows = $input.all().map(item => item.json);

function pickShortText(...values) {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) {
      return value.trim().slice(0, 120);
    }
  }
  return null;
}

function mapChainName(chainId) {
  const mapping = {
    CT_501: 'Solana',
    '56': 'BSC',
    '8453': 'Base',
    solana: 'Solana',
    bsc: 'BSC',
    base: 'Base',
    ethereum: 'Ethereum'
  };
  return mapping[chainId] || chainId || '-';
}

return rows.map((row) => {
  const chainId = row.chainId || 'unknown';
  const address = row.tokenAddress || '';
  const tokenKey = address ? `${chainId}:${address}` : `dexscreener:${row.url || row.description || row.chainId}`;
  const tokenSymbol = pickShortText(row.symbol, row.tokenAddress);
  const tokenName = pickShortText(
    row.symbol,
    row.name,
    row.baseToken?.name,
    row.baseToken?.symbol,
    row.description
  );

  return {
    json: {
      token_key: tokenKey,
      token_symbol: tokenSymbol,
      token_name: tokenName,
      chain_id: chainId,
      chain_name: mapChainName(chainId),
      contract_address: address || null,
      source_platform: 'dexscreener',
      source_type: 'top',
      hot_score_base: 35,
      price_usd: null,
      volume_24h: null,
      liquidity_usd: null,
      market_cap: null,
      fdv: null,
      pair_url: row.url || null
    }
  };
});
```

### 9）`Get Binance Social Rush`

- 节点类型：`HTTP Request`
- 节点名称：`Get Binance Social Rush`
- 搭建步骤：
  1. 从 `Set Chain Config` 拉第四条线
  2. 添加 `HTTP Request`
  3. `Method` 选 `GET`
  4. `URL` 填表达式：

```text
=https://web3.binance.com/bapi/defi/v2/public/wallet-direct/buw/wallet/market/token/social-rush/rank/list/ai?chainId={{$json.binance_chain_id}}&rankType={{$json.social_rank_type}}&sort={{$json.social_sort}}&limit={{$json.rank_limit}}
```

  5. 打开 `Send Headers`
  6. 新增 Header：
     - `User-Agent` = `binance-web3/2.0 (Skill)`
  7. `Response Format` 选 `JSON`

### 10）`Normalize Binance Social Rush`

- 节点类型：`Code`
- 节点名称：`Normalize Binance Social Rush`
- 搭建步骤：
  1. 从 `Get Binance Social Rush` 拉线
  2. 添加 `Code`
  3. `Mode` 选 `Run Once for All Items`
  4. 粘贴代码：

```javascript
const rows = $input.first().json?.data || [];

function pickFirst(...values) {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return null;
}

function mapChainName(chainId) {
  const mapping = {
    CT_501: 'Solana',
    '56': 'BSC',
    '8453': 'Base',
    solana: 'Solana',
    bsc: 'BSC',
    base: 'Base',
    ethereum: 'Ethereum'
  };
  return mapping[chainId] || chainId || '-';
}

return rows.map((row) => {
  const firstToken = row.tokens?.[0] || row.tokenList?.[0] || row.relatedTokens?.[0] || null;
  const chainId = pickFirst(
    row.chainId,
    firstToken?.chainId
  ) || 'unknown';
  const address = pickFirst(
    row.contractAddress,
    row.tokenAddress,
    firstToken?.contractAddress,
    firstToken?.tokenAddress
  ) || '';
  const tokenSymbol = pickFirst(
    row.symbol,
    row.tokenSymbol,
    firstToken?.symbol,
    firstToken?.tokenSymbol
  );
  const tokenName = pickFirst(
    row.tokenName,
    row.name,
    row.topicNameCn,
    row.topicNameEn,
    row.topicName,
    firstToken?.tokenName,
    firstToken?.name
  );
  const tokenKey = address ? `${chainId}:${address}` : `binance-social:${tokenSymbol || tokenName || 'unknown'}`;

  return {
    json: {
      token_key: tokenKey,
      token_symbol: tokenSymbol,
      token_name: tokenName,
      chain_id: chainId,
      chain_name: mapChainName(chainId),
      contract_address: address || null,
      source_platform: 'binance',
      source_type: 'social_rush',
      hot_score_base: 40,
      price_usd: row.price ?? firstToken?.price ?? null,
      volume_24h: row.volume24h ?? firstToken?.volume24h ?? null,
      liquidity_usd: row.liquidity ?? firstToken?.liquidity ?? null,
      market_cap: row.marketCap ?? firstToken?.marketCap ?? null,
      fdv: row.fdv ?? firstToken?.fdv ?? null,
      pair_url: row.webUrl || firstToken?.webUrl || firstToken?.previewLink || null
    }
  };
});
```

### 11）`Get Binance Pulse Rank`

- 节点类型：`HTTP Request`
- 节点名称：`Get Binance Pulse Rank`
- 搭建步骤：
  1. 从 `Set Chain Config` 拉第五条线
  2. 添加 `HTTP Request`
  3. `Method` 选 `POST`
  4. `URL` 填：

```text
https://web3.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse/rank/list/ai
```

  5. 打开 `Send Headers`
  6. 新增两条 Header：
     - `Content-Type` = `application/json`
     - `User-Agent` = `binance-web3/2.0 (Skill)`
  7. 打开 `Send Body`
  8. `Body Content Type` 选 `JSON`
  9. `Specify Body` 选 `Using JSON`
  10. 点击 JSON 输入框左侧 `fx`，把整个请求体切成“完整表达式”
  11. 请求体直接填下面这段，不要给表达式再套双引号：

```javascript
={{
{
  chainId: $json.binance_chain_id,
  rankType: Number($json.pulse_rank_type),
  limit: Number($json.rank_limit)
}
}}
```

  12. `Response Format` 选 `JSON`
  13. 如果右侧预览里出现的是：

```json
{
  "chainId": "CT_501",
  "rankType": 10,
  "limit": 20
}
```

就说明 `Set Chain Config` 已经生效，而且类型也对了。

### 12）`Normalize Binance Pulse Rank`

- 节点类型：`Code`
- 节点名称：`Normalize Binance Pulse Rank`
- 搭建步骤：
  1. 从 `Get Binance Pulse Rank` 拉线
  2. 添加 `Code`
  3. `Mode` 选 `Run Once for All Items`
  4. 粘贴代码：

```javascript
const rows = $input.first().json?.data || [];

function pickFirst(...values) {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return null;
}

function mapChainName(chainId) {
  const mapping = {
    CT_501: 'Solana',
    '56': 'BSC',
    '8453': 'Base',
    solana: 'Solana',
    bsc: 'BSC',
    base: 'Base',
    ethereum: 'Ethereum'
  };
  return mapping[chainId] || chainId || '-';
}

return rows.map((row) => {
  const firstToken = row.tokens?.[0] || row.tokenList?.[0] || null;
  const chainId = pickFirst(
    row.chainId,
    firstToken?.chainId
  ) || 'unknown';
  const address = pickFirst(
    row.contractAddress,
    row.tokenAddress,
    firstToken?.contractAddress,
    firstToken?.tokenAddress
  ) || '';
  const tokenSymbol = pickFirst(
    row.symbol,
    row.tokenSymbol,
    firstToken?.symbol,
    firstToken?.tokenSymbol
  );
  const tokenName = pickFirst(
    row.tokenName,
    row.name,
    firstToken?.tokenName,
    firstToken?.name
  );
  const tokenKey = address ? `${chainId}:${address}` : `binance-pulse:${tokenSymbol || tokenName || 'unknown'}`;

  return {
    json: {
      token_key: tokenKey,
      token_symbol: tokenSymbol,
      token_name: tokenName,
      chain_id: chainId,
      chain_name: mapChainName(chainId),
      contract_address: address || null,
      source_platform: 'binance',
      source_type: 'pulse_rank',
      hot_score_base: 45,
      price_usd: row.price ?? firstToken?.price ?? null,
      volume_24h: row.volume24h ?? firstToken?.volume24h ?? null,
      liquidity_usd: row.liquidity ?? firstToken?.liquidity ?? null,
      market_cap: row.marketCap ?? firstToken?.marketCap ?? null,
      fdv: row.fdv ?? firstToken?.fdv ?? null,
      pair_url: row.webUrl || firstToken?.webUrl || firstToken?.previewLink || null
    }
  };
});
```

### 13）`Get Binance Unified Rank`

- 节点类型：`HTTP Request`
- 节点名称：`Get Binance Unified Rank`
- 搭建步骤：
  1. 从 `Set Chain Config` 拉第六条线
  2. 添加 `HTTP Request`
  3. `Method` 选 `POST`
  4. `URL` 填：

```text
https://web3.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse/unified/rank/list/ai
```

  5. 打开 `Send Headers`
  6. 新增两条 Header：
     - `Content-Type` = `application/json`
     - `User-Agent` = `binance-web3/2.0 (Skill)`
  7. 打开 `Send Body`
  8. `Body Content Type` 选 `JSON`
  9. `Specify Body` 选 `Using JSON`
  10. 点击 JSON 输入框左侧 `fx`，把整个请求体切成“完整表达式”
  11. 请求体直接填下面这段，不要给表达式再套双引号：

```javascript
={{
{
  chainId: $json.binance_chain_id,
  rankType: $json.unified_rank_type,
  limit: Number($json.rank_limit)
}
}}
```

  12. `Response Format` 选 `JSON`
  13. 如果右侧预览里出现的是：

```json
{
  "chainId": "CT_501",
  "rankType": "TRENDING",
  "limit": 20
}
```

就说明 `Set Chain Config` 已经生效，而且类型也对了。

### 14）`Normalize Binance Unified Rank`

- 节点类型：`Code`
- 节点名称：`Normalize Binance Unified Rank`
- 搭建步骤：
  1. 从 `Get Binance Unified Rank` 拉线
  2. 添加 `Code`
  3. `Mode` 选 `Run Once for All Items`
  4. 注意：这个接口当前实测返回的是 `1 item`，并且数据列表在 `data.tokens` 里，不是直接在 `data` 里
  5. 粘贴代码：

```javascript
const rows = $input.first().json?.data?.tokens || [];

function pickFirst(...values) {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return null;
}

function mapChainName(chainId) {
  const mapping = {
    CT_501: 'Solana',
    '56': 'BSC',
    '8453': 'Base',
    solana: 'Solana',
    bsc: 'BSC',
    base: 'Base',
    ethereum: 'Ethereum'
  };
  return mapping[chainId] || chainId || '-';
}

return rows.map((row) => {
  const chainId = pickFirst(row.chainId) || 'unknown';
  const address = pickFirst(
    row.contractAddress,
    row.tokenAddress
  ) || '';
  const tokenSymbol = pickFirst(
    row.symbol,
    row.tokenSymbol
  );
  const tokenName = pickFirst(
    row.tokenName,
    row.name
  );
  const tokenKey = address ? `${chainId}:${address}` : `binance-unified:${tokenSymbol || tokenName || 'unknown'}`;

  return {
    json: {
      token_key: tokenKey,
      token_symbol: tokenSymbol,
      token_name: tokenName,
      chain_id: chainId,
      chain_name: mapChainName(chainId),
      contract_address: address || null,
      source_platform: 'binance',
      source_type: 'unified_rank',
      hot_score_base: 50,
      price_usd: row.price || null,
      volume_24h: row.volume24h || null,
      liquidity_usd: row.liquidity || null,
      market_cap: row.marketCap || null,
      fdv: row.fdv || null,
      pair_url: row.webUrl || row.previewLink || null
    }
  };
});
```

### 15）`Merge All Sources`

- 节点类型：`Merge`
- 节点名称：`Merge All Sources`
- 搭建步骤：
  1. 添加 `Merge`
  2. `Mode` 选择 `Append`
  3. `Number of Inputs` 设为 `6`
  4. `Input 1` 接 `Normalize CoinGecko Trending`
  5. `Input 2` 接 `Normalize DexScreener Boosts`
  6. `Input 3` 接 `Normalize DexScreener Top`
  7. `Input 4` 接 `Normalize Binance Social Rush`
  8. `Input 5` 接 `Normalize Binance Pulse Rank`
  9. `Input 6` 接 `Normalize Binance Unified Rank`

### 16）`Deduplicate And Score`

- 节点类型：`Code`
- 节点名称：`Deduplicate And Score`
- 搭建步骤：
  1. 从 `Merge All Sources` 拉线
  2. 添加 `Code`
  3. `Mode` 选 `Run Once for All Items`
  4. 粘贴代码：

```javascript
const items = $input.all().map(i => i.json);
const grouped = new Map();

for (const item of items) {
  const key = item.token_key;
  if (!grouped.has(key)) {
    grouped.set(key, {
      ...item,
      source_count: 1,
      source_tags: [`${item.source_platform}:${item.source_type}`],
      hot_score: item.hot_score_base || 0,
    });
    continue;
  }

  const existing = grouped.get(key);
  existing.source_count += 1;
  existing.source_tags.push(`${item.source_platform}:${item.source_type}`);
  existing.hot_score += item.hot_score_base || 0;
  existing.price_usd = existing.price_usd ?? item.price_usd ?? null;
  existing.volume_24h = existing.volume_24h ?? item.volume_24h ?? null;
  existing.liquidity_usd = existing.liquidity_usd ?? item.liquidity_usd ?? null;
  existing.market_cap = existing.market_cap ?? item.market_cap ?? null;
  existing.fdv = existing.fdv ?? item.fdv ?? null;
  existing.pair_url = existing.pair_url ?? item.pair_url ?? null;
}

const now = new Date().toISOString();

const scored = Array.from(grouped.values()).map((item) => {
  if (item.source_count >= 2) item.hot_score += 15 * (item.source_count - 1);
  if ((item.volume_24h || 0) > 1000000) item.hot_score += 10;
  if ((item.liquidity_usd || 0) > 300000) item.hot_score += 10;

  return {
    json: {
      ...item,
      source_tags: item.source_tags.join(','),
      chain_name: item.chain_name || item.chain_id || '-',
      risk_score: null,
      candidate_status: '待筛选',
      first_seen_at: now,
      last_seen_at: now,
      record_time: now
    }
  };
});

scored.sort((a, b) => (b.json.hot_score || 0) - (a.json.hot_score || 0));
return scored.slice(0, 50);
```

### 17）`Loop Candidates`

- 节点类型：`Loop Over Items`
- 节点名称：`Loop Candidates`
- 搭建步骤：
  1. 从 `Deduplicate And Score` 拉一条线
  2. 添加 `Loop Over Items`
  3. `Batch Size` 填 `1`
  4. 使用 `loop` 输出接后面的 `Upsert Candidate`

### 18）`Upsert Candidate`

- 节点类型：`Postgres`
- 节点名称：`Upsert Candidate`
- 搭建步骤：
  1. 从 `Loop Candidates` 的 `loop` 输出拉线
  2. 添加 `Postgres`
  3. 选择 PostgreSQL 凭证
  4. `Operation` 选择 `Execute Query`
  5. `Query` 直接粘贴下面这段 SQL：

```sql
INSERT INTO token_discovery_candidates (
    token_key,
    token_symbol,
    token_name,
    chain_id,
    contract_address,
    source_platform,
    source_type,
    source_count,
    source_tags,
    hot_score,
    risk_score,
    price_usd,
    volume_24h,
    liquidity_usd,
    market_cap,
    fdv,
    pair_url,
    candidate_status,
    first_seen_at,
    last_seen_at
) VALUES (
    LEFT($1, 256),
    LEFT($2, 64),
    LEFT($3, 256),
    LEFT($4, 64),
    $5,
    LEFT($6, 64),
    LEFT($7, 64),
    $8,
    $9,
    $10,
    $11,
    $12,
    $13,
    $14,
    $15,
    $16,
    $17,
    LEFT($18, 32),
    $19,
    $20
)
ON CONFLICT (token_key)
DO UPDATE SET
    token_symbol = EXCLUDED.token_symbol,
    token_name = EXCLUDED.token_name,
    chain_id = EXCLUDED.chain_id,
    contract_address = EXCLUDED.contract_address,
    source_platform = EXCLUDED.source_platform,
    source_type = EXCLUDED.source_type,
    source_count = EXCLUDED.source_count,
    source_tags = EXCLUDED.source_tags,
    hot_score = EXCLUDED.hot_score,
    risk_score = EXCLUDED.risk_score,
    price_usd = EXCLUDED.price_usd,
    volume_24h = EXCLUDED.volume_24h,
    liquidity_usd = EXCLUDED.liquidity_usd,
    market_cap = EXCLUDED.market_cap,
    fdv = EXCLUDED.fdv,
    pair_url = EXCLUDED.pair_url,
    last_seen_at = NOW(),
    seen_count = token_discovery_candidates.seen_count + 1;
```
  6. `Query Parameters` 不要写成逗号拼接文本，直接切到表达式模式，返回一个数组：

```javascript
={{
[
  $json.token_key ?? null,
  $json.token_symbol ?? null,
  $json.token_name ?? null,
  $json.chain_id ?? null,
  $json.contract_address ?? null,
  $json.source_platform ?? null,
  $json.source_type ?? null,
  $json.source_count ?? null,
  $json.source_tags ?? null,
  $json.hot_score ?? null,
  $json.risk_score ?? null,
  $json.price_usd ?? null,
  $json.volume_24h ?? null,
  $json.liquidity_usd ?? null,
  $json.market_cap ?? null,
  $json.fdv ?? null,
  $json.pair_url ?? null,
  $json.candidate_status ?? null,
  $json.first_seen_at ?? null,
  $json.last_seen_at ?? null
]
}}
```

  7. 如果这里报 `there is no parameter $1`，通常就是因为 `Query Parameters` 不是数组表达式，而是普通字符串

### 19）`Insert Candidate Snapshot`

- 节点类型：`Postgres`
- 节点名称：`Insert Candidate Snapshot`
- 搭建步骤：
  1. 从 `Upsert Candidate` 拉线
  2. 添加 `Postgres`
  3. `Operation` 选择 `Execute Query`
  4. `Query` 直接粘贴下面这段 SQL：

```sql
INSERT INTO token_discovery_snapshots (
    token_key,
    record_time,
    token_symbol,
    token_name,
    chain_id,
    contract_address,
    source_platform,
    source_type,
    hot_score,
    price_usd,
    volume_24h,
    liquidity_usd,
    market_cap,
    fdv,
    pair_url
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
    $11, $12, $13, $14, $15
)
ON CONFLICT (token_key, record_time)
DO NOTHING;
```
  5. 注意：这个节点的当前输入通常已经是 `Upsert Candidate` 的执行结果，比如 `{ "success": true }`，所以这里不要再用 `$json.token_key`
  6. `Query Parameters` 要切到表达式模式，并且显式从 `Loop Candidates` 节点取值，返回一个数组：

```javascript
={{
[
  $('Loop Candidates').item.json.token_key ?? null,
  $('Loop Candidates').item.json.record_time ?? null,
  $('Loop Candidates').item.json.token_symbol ?? null,
  $('Loop Candidates').item.json.token_name ?? null,
  $('Loop Candidates').item.json.chain_id ?? null,
  $('Loop Candidates').item.json.contract_address ?? null,
  $('Loop Candidates').item.json.source_platform ?? null,
  $('Loop Candidates').item.json.source_type ?? null,
  $('Loop Candidates').item.json.hot_score ?? null,
  $('Loop Candidates').item.json.price_usd ?? null,
  $('Loop Candidates').item.json.volume_24h ?? null,
  $('Loop Candidates').item.json.liquidity_usd ?? null,
  $('Loop Candidates').item.json.market_cap ?? null,
  $('Loop Candidates').item.json.fdv ?? null,
  $('Loop Candidates').item.json.pair_url ?? null
]
}}
```
  7. 如果这里继续报 `there is no parameter $1`，优先检查是不是还在用 `$json.xxx`
  8. 如果这里报 `duplicate key value violates unique constraint "token_discovery_snapshots_pkey"`，说明同一个 `token_key + record_time` 被重复写入；保留上面的 `ON CONFLICT ... DO NOTHING` 即可
  9. 把 `Insert Candidate Snapshot` 回接到 `Loop Candidates`

### 20）`Build Hot Token Summary`

- 节点类型：`Code`
- 节点名称：`Build Hot Token Summary`
- 搭建步骤：
  1. 从 `Deduplicate And Score` 再单独拉一条线
  2. 添加 `Code`
  3. `Mode` 选 `Run Once for All Items`
  4. 粘贴代码：

```javascript
const items = $input.all().map(i => i.json);
const lines = ['# 热点候选币 Top 10', ''];

const filtered = items
  .filter(item => {
    const symbol =
      typeof item.token_symbol === 'string' && item.token_symbol.trim()
        ? item.token_symbol.trim()
        : (typeof item.token_name === 'string' && item.token_name.trim() ? item.token_name.trim() : '');
    const address =
      typeof item.contract_address === 'string' && item.contract_address.trim()
        ? item.contract_address.trim()
        : '';
    return Boolean(symbol) && Boolean(address);
  })
  .slice(0, 10);

filtered.forEach((item, index) => {
  const tokenSymbol =
    typeof item.token_symbol === 'string' && item.token_symbol.trim()
      ? item.token_symbol.trim()
      : '未知';

  const tokenName =
    typeof item.token_name === 'string' && item.token_name.trim()
      ? item.token_name.trim()
      : '-';

  const chainName = item.chain_name || item.chain_id || '-';
  const address =
    typeof item.contract_address === 'string' && item.contract_address.trim()
      ? item.contract_address.trim()
      : '-';
  const pairUrl =
    typeof item.pair_url === 'string' && item.pair_url.trim()
      ? item.pair_url.trim()
      : '-';

  lines.push(
    `${index + 1}. ${tokenSymbol} | 名称=${tokenName} | 链=${chainName} | 合约=${address} | 热度=${item.hot_score || 0} | 来源数=${item.source_count || 1}`
  );
  lines.push(
    `   链接=${pairUrl}`
  );
});

if (filtered.length === 0) {
  lines.push('当前没有提取到同时包含 symbol 和合约地址的候选币，优先检查 Binance Social Rush / Pulse Rank 的原始返回结构。');
}

lines.push('');
lines.push('字段说明：热度 / 来源数 / 合约地址为完整展示 / 链接可直接点击跳转');

return [
  {
    json: {
      message: lines.join('\n')
    }
  }
];
```

  5. 说明：
     - `token_symbol` 优先取标准 symbol
     - 如果某些 Binance 返回里没有直接给 symbol，就尝试从对象字段里兜底
     - 还取不到时显示 `未知`

### 24）`Send Feishu Summary`

- 节点类型：`HTTP Request`
- 节点名称：`Send Feishu Summary`
- 搭建步骤：
  1. 从 `Build Hot Token Summary` 拉线
  2. 添加 `HTTP Request`
  3. `Method` 选 `POST`
  4. `URL` 填你的飞书机器人 webhook
  5. 打开 `Send Headers`
  6. 新增 Header：
     - `Content-Type` = `application/json`
  7. 打开 `Send Body`
  8. `Body Content Type` 选 `JSON`
  9. 点击 JSON 输入框左侧 `fx`，让整个输入框进入表达式模式
  10. Body 整段替换成下面这段，不要在外面再包引号：

```javascript
={{
{
  msg_type: "text",
  content: {
    text: String($json.message ?? "")
  }
}
}}
```
  11. 不要写成 `"={{ ... }}"`，前后不能有双引号
  12. 也不要再用 `JSON.stringify(...)`，因为 `Using JSON` 这里要的是对象，不是字符串
  13. 如果看到报错 `Unexpected token '='`，通常就是输入框里还残留了外层引号

## 八、标准化输出结构

每个 Normalize 的 `Code` 节点，都统一返回这套字段：

```json
{
  "token_key": "",
  "token_symbol": "",
  "token_name": "",
  "chain_id": "",
  "contract_address": "",
  "source_platform": "",
  "source_type": "",
  "hot_score_base": 0,
  "price_usd": null,
  "volume_24h": null,
  "liquidity_usd": null,
  "market_cap": null,
  "fdv": null,
  "pair_url": ""
}
```

## 九、入库 SQL

### 1）候选池 UPSERT

```sql
INSERT INTO token_discovery_candidates (
    token_key,
    token_symbol,
    token_name,
    chain_id,
    contract_address,
    source_platform,
    source_type,
    source_count,
    source_tags,
    hot_score,
    risk_score,
    price_usd,
    volume_24h,
    liquidity_usd,
    market_cap,
    fdv,
    pair_url,
    candidate_status,
    first_seen_at,
    last_seen_at
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
    $11, $12, $13, $14, $15, $16, $17, $18, $19, $20
)
ON CONFLICT (token_key)
DO UPDATE SET
    token_symbol = EXCLUDED.token_symbol,
    token_name = EXCLUDED.token_name,
    chain_id = EXCLUDED.chain_id,
    contract_address = EXCLUDED.contract_address,
    source_platform = EXCLUDED.source_platform,
    source_type = EXCLUDED.source_type,
    source_count = EXCLUDED.source_count,
    source_tags = EXCLUDED.source_tags,
    hot_score = EXCLUDED.hot_score,
    risk_score = EXCLUDED.risk_score,
    price_usd = EXCLUDED.price_usd,
    volume_24h = EXCLUDED.volume_24h,
    liquidity_usd = EXCLUDED.liquidity_usd,
    market_cap = EXCLUDED.market_cap,
    fdv = EXCLUDED.fdv,
    pair_url = EXCLUDED.pair_url,
    last_seen_at = NOW(),
    seen_count = token_discovery_candidates.seen_count + 1;
```

### 2）快照表 INSERT

```sql
INSERT INTO token_discovery_snapshots (
    token_key,
    record_time,
    token_symbol,
    token_name,
    chain_id,
    contract_address,
    source_platform,
    source_type,
    hot_score,
    price_usd,
    volume_24h,
    liquidity_usd,
    market_cap,
    fdv,
    pair_url
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
    $11, $12, $13, $14, $15
)
ON CONFLICT (token_key, record_time)
DO NOTHING;
```

## 十、飞书推送内容

第一版就推 Top 10：

```markdown
# 热点候选币 Top 10

1. BONK | solana | 85
2. XXX  | base   | 72
3. YYY  | bsc    | 68

字段说明：
- hot_score：综合热度分
- source_count：被多少个平台同时命中
- pair_url：DexScreener 链接
```

## 十一、这条工作流跑通的验收标准

满足下面 5 条就算通过：

1. 能抓到 CoinGecko / DexScreener / Binance Web3 的热点候选
2. 相同币不会重复写入候选池主表
3. 每次运行都会写入快照表
4. 未被 CMC 收录的币也能进入候选池
5. 飞书能收到每日 / 每轮的热点候选摘要

## 十二、下一步怎么接到现有系统

这条发现工作流跑通后，再做下面两件事：

1. 在飞书里人工筛选值得长期跟踪的币
2. 手动补 `cmc_id` / `defillama_slug`
3. 再写入 `coin_basic`
4. 之后由子工作流2 / 子工作流3 接管日常跟踪

也就是说：

- `热点发现工作流` 负责找机会
- `coin_basic` 负责正式跟踪
- 两者不要混为一谈
