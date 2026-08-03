# 子工作流1A｜`WF_CMC_MAP_INGEST` 节点级配置清单（2026-07-29）

## 1. 这条工作流的定位

这是新系统里最底层、最关键的一条资产入口工作流。

它的职责只有一件事：

**把 CoinMarketCap `/v1/cryptocurrency/map` 的全量目录，规范地写入新架构的 `sys -> raw -> src_cmc`。**

这条工作流**不做**下面这些事：

- 不写 `biz.coin_basic`
- 不做 `coingecko_id` 匹配
- 不做 `defillama_slug` 匹配
- 不做文档抓取
- 不做行情汇总

一句话理解：

```text
CMC_MAP -> sys.ingest_run -> raw.api_response -> src_cmc.cmc_asset_map
```

---

## 2. 输入输出

### 输入

- 无需外部业务输入
- 只依赖：
  - `CMC_API_KEY`
  - PostgreSQL 连接

### 输出

写入下面 3 张表：

1. `sys.ingest_run`
2. `raw.api_response`
3. `src_cmc.cmc_asset_map`

---

## 3. 节点总顺序

推荐节点顺序：

```text
When executed by another workflow
-> SET_WORKFLOW_META
-> PG_INSERT_INGEST_RUN
-> HTTP_CMC_MAP
-> CODE_BUILD_RAW_RESPONSE
-> PG_INSERT_RAW_API_RESPONSE
-> CODE_PARSE_CMC_MAP
-> LOOP_OVER_ITEMS
-> PG_UPSERT_CMC_ASSET_MAP
-> PG_FINISH_INGEST_RUN_SUCCESS
```

建议再加一个错误支路：

```text
HTTP_CMC_MAP / PG_INSERT_RAW_API_RESPONSE / PG_UPSERT_CMC_ASSET_MAP
   任一步失败
-> PG_FINISH_INGEST_RUN_FAILED
```

---

## 4. 节点逐个配置

## 节点1：`When executed by another workflow`

使用标准子工作流触发器。

说明：

- 这条工作流应该被主调度工作流调用
- 不建议先挂 Cron

---

## 节点2：`SET_WORKFLOW_META`

建议用 `Set` 节点，固定写入本次任务元数据。

输出字段建议：

```json
{
  "platform_code": "cmc",
  "endpoint_code": "cmc_map",
  "workflow_name": "WF_CMC_MAP_INGEST",
  "request_url": "https://pro-api.coinmarketcap.com/v1/cryptocurrency/map",
  "request_params": {
    "listing_status": "active",
    "sort": "cmc_rank"
  }
}
```

作用：

- 方便后面统一写入 `sys.ingest_run`

---

## 节点3：`PG_INSERT_INGEST_RUN`

### 目标

先在 `sys.ingest_run` 里登记一条“任务开始”记录。

### SQL

```sql
INSERT INTO sys.ingest_run (
    platform_code,
    endpoint_code,
    workflow_name,
    request_params,
    request_url,
    status,
    started_at
) VALUES (
    $1,
    $2,
    $3,
    $4::jsonb,
    $5,
    'running',
    NOW()
)
RETURNING run_id, started_at;
```

### 参数

```text
{{ $json.platform_code }}
{{ $json.endpoint_code }}
{{ $json.workflow_name }}
{{ JSON.stringify($json.request_params) }}
{{ $json.request_url }}
```

### 输出要求

需要把返回的 `run_id` 带到后续节点。

---

## 节点4：`HTTP_CMC_MAP`

### Method

```text
GET
```

### URL

```text
https://pro-api.coinmarketcap.com/v1/cryptocurrency/map
```

### Headers

```text
X-CMC_PRO_API_KEY: {{ $env.CMC_API_KEY }}
Accept: application/json
```

### Query Parameters

```text
listing_status = active
sort = cmc_rank
```

### 预期返回

核心字段通常包括：

- `id`
- `name`
- `symbol`
- `slug`
- `is_active`
- `rank`
- `platform`
- `first_historical_data`
- `last_historical_data`

### 注意

- 这一步拿到的是原始事实，不要在这里做复杂映射
- 不要在 HTTP 节点直接变形为 `coin_basic`

---

## 节点5：`CODE_BUILD_RAW_RESPONSE`

### 目标

把 HTTP 返回包装成可以落到 `raw.api_response` 的结构。

### 推荐代码

```javascript
const input = $input.all();
const httpNode = $('HTTP_CMC_MAP').first().json;
const runNode = $('PG_INSERT_INGEST_RUN').first().json;
const metaNode = $('SET_WORKFLOW_META').first().json;

const payload = httpNode;
const rows = payload.data ?? [];

function stableStringify(obj) {
  if (obj === null || typeof obj !== 'object') return JSON.stringify(obj);
  if (Array.isArray(obj)) return `[${obj.map(stableStringify).join(',')}]`;
  return `{${Object.keys(obj).sort().map(k => `${JSON.stringify(k)}:${stableStringify(obj[k])}`).join(',')}}`;
}

const payloadText = stableStringify(payload);

return [
  {
    json: {
      run_id: runNode.run_id,
      platform_code: metaNode.platform_code,
      endpoint_code: metaNode.endpoint_code,
      request_key: 'listing_status=active&sort=cmc_rank',
      entity_key: null,
      page_key: 'page:all',
      payload,
      payload_hash: payloadText,
      fetched_at: new Date().toISOString(),
      row_count: rows.length
    }
  }
];
```

### 说明

- 这里的 `payload_hash` 先直接放稳定序列化后的文本
- 更理想是后面接一个数据库 `md5(...)` 或 n8n 自带 hash 节点
- 如果你愿意，下一步我可以再帮你把这里改成真正的 `md5`

---

## 节点6：`PG_INSERT_RAW_API_RESPONSE`

### 目标

把本次 API 原始响应先落库。

### SQL

```sql
INSERT INTO raw.api_response (
    run_id,
    platform_code,
    endpoint_code,
    request_key,
    entity_key,
    page_key,
    payload,
    payload_hash,
    fetched_at
) VALUES (
    $1,
    $2,
    $3,
    $4,
    $5,
    $6,
    $7::jsonb,
    md5($8),
    $9::timestamptz
)
ON CONFLICT (
    platform_code,
    endpoint_code,
    COALESCE(request_key, ''),
    COALESCE(page_key, ''),
    payload_hash
) DO NOTHING
RETURNING response_id;
```

### 参数

```text
{{ $json.run_id }}
{{ $json.platform_code }}
{{ $json.endpoint_code }}
{{ $json.request_key }}
{{ $json.entity_key ?? null }}
{{ $json.page_key }}
{{ JSON.stringify($json.payload) }}
{{ $json.payload_hash }}
{{ $json.fetched_at }}
```

### 重要提醒

你现在初始化 SQL 里 `raw.api_response` 的唯一约束是索引，不是表级 `ON CONFLICT` 约束名。  
所以如果当前 PostgreSQL 节点不接受这种表达式冲突目标，可以改成两种做法之一：

1. 先 `SELECT` 查重，再 `INSERT`
2. 把 `uq_raw_api_response_dedup` 改成显式唯一约束字段组合

为了工作流稳定，我更推荐：

- **第一版先不做 `ON CONFLICT`**
- 直接 `INSERT ... RETURNING response_id`

因为 `cmc_map` 本来是低频全量同步，重复写一份原始响应问题不大。

### 更稳的第一版 SQL

```sql
INSERT INTO raw.api_response (
    run_id,
    platform_code,
    endpoint_code,
    request_key,
    entity_key,
    page_key,
    payload,
    payload_hash,
    fetched_at
) VALUES (
    $1,
    $2,
    $3,
    $4,
    $5,
    $6,
    $7::jsonb,
    md5($8),
    $9::timestamptz
)
RETURNING response_id;
```

---

## 节点7：`CODE_PARSE_CMC_MAP`

### 目标

把 CMC 原始 `map` 数据转成 `src_cmc.cmc_asset_map` 的行结构。

### 推荐代码

```javascript
const httpPayload = $('HTTP_CMC_MAP').first().json;
const rawInsert = $('PG_INSERT_RAW_API_RESPONSE').first().json;

const rows = httpPayload.data ?? [];

return rows.map((row) => {
  const platform = row.platform ?? null;

  return {
    json: {
      cmc_id: row.id ?? null,
      symbol: row.symbol ?? null,
      name: row.name ?? null,
      slug: row.slug ?? null,
      listing_status: 'active',
      is_active: row.is_active ?? null,
      rank_num: row.rank ?? null,
      platform_name: platform?.name ?? null,
      platform_slug: platform?.slug ?? null,
      platform_symbol: platform?.symbol ?? null,
      token_address: platform?.token_address ?? null,
      first_historical_data: row.first_historical_data ?? null,
      last_historical_data: row.last_historical_data ?? null,
      raw_response_id: rawInsert.response_id ?? null,
      fetched_at: new Date().toISOString()
    }
  };
}).filter(item => item.json.cmc_id);
```

### 说明

- `listing_status` 这里固定写 `active`，因为请求参数就是 `active`
- 不要在这里额外拼 `official_website` 等字段，因为 `map` 根本不提供

---

## 节点8：`LOOP_OVER_ITEMS`

建议使用：

- `Loop Over Items`
- `Batch Size = 1`

原因：

- 你之前已经验证过串行更稳
- PostgreSQL 节点逐条 upsert 更方便定位失败项

---

## 节点9：`PG_UPSERT_CMC_ASSET_MAP`

### 目标

把每一条目录记录写入 `src_cmc.cmc_asset_map`。

### SQL

```sql
INSERT INTO src_cmc.cmc_asset_map (
    cmc_id,
    symbol,
    name,
    slug,
    listing_status,
    is_active,
    rank_num,
    platform_name,
    platform_slug,
    platform_symbol,
    token_address,
    first_historical_data,
    last_historical_data,
    raw_response_id,
    fetched_at,
    updated_at
) VALUES (
    $1,
    $2,
    $3,
    $4,
    $5,
    $6,
    $7,
    $8,
    $9,
    $10,
    $11,
    $12::timestamptz,
    $13::timestamptz,
    $14,
    $15::timestamptz,
    NOW()
)
ON CONFLICT (cmc_id) DO UPDATE SET
    symbol = EXCLUDED.symbol,
    name = EXCLUDED.name,
    slug = EXCLUDED.slug,
    listing_status = EXCLUDED.listing_status,
    is_active = EXCLUDED.is_active,
    rank_num = EXCLUDED.rank_num,
    platform_name = EXCLUDED.platform_name,
    platform_slug = EXCLUDED.platform_slug,
    platform_symbol = EXCLUDED.platform_symbol,
    token_address = EXCLUDED.token_address,
    first_historical_data = COALESCE(EXCLUDED.first_historical_data, src_cmc.cmc_asset_map.first_historical_data),
    last_historical_data = COALESCE(EXCLUDED.last_historical_data, src_cmc.cmc_asset_map.last_historical_data),
    raw_response_id = EXCLUDED.raw_response_id,
    fetched_at = EXCLUDED.fetched_at,
    updated_at = NOW()
RETURNING cmc_id;
```

### 参数

```text
{{ $json.cmc_id }}
{{ $json.symbol }}
{{ $json.name }}
{{ $json.slug }}
{{ $json.listing_status }}
{{ $json.is_active }}
{{ $json.rank_num }}
{{ $json.platform_name }}
{{ $json.platform_slug }}
{{ $json.platform_symbol }}
{{ $json.token_address }}
{{ $json.first_historical_data ?? null }}
{{ $json.last_historical_data ?? null }}
{{ $json.raw_response_id ?? null }}
{{ $json.fetched_at }}
```

---

## 节点10：`PG_FINISH_INGEST_RUN_SUCCESS`

### 目标

把 `sys.ingest_run` 更新为成功。

### SQL

```sql
UPDATE sys.ingest_run
SET
    status = 'success',
    http_status = 200,
    total_items = $2,
    success_items = $2,
    fail_items = 0,
    finished_at = NOW(),
    duration_ms = EXTRACT(EPOCH FROM (NOW() - started_at)) * 1000
WHERE run_id = $1
RETURNING run_id, status, total_items;
```

### 参数

```text
{{ $('PG_INSERT_INGEST_RUN').first().json.run_id }}
{{ $('CODE_BUILD_RAW_RESPONSE').first().json.row_count }}
```

---

## 5. 失败收口节点

## 节点11：`PG_FINISH_INGEST_RUN_FAILED`

建议单独做一条失败分支。

### SQL

```sql
UPDATE sys.ingest_run
SET
    status = 'failed',
    http_status = $2,
    finished_at = NOW(),
    duration_ms = EXTRACT(EPOCH FROM (NOW() - started_at)) * 1000,
    error_message = $3
WHERE run_id = $1
RETURNING run_id, status;
```

### 参数建议

```text
{{ $('PG_INSERT_INGEST_RUN').first().json.run_id }}
{{ $json.http_status ?? null }}
{{ $json.error_message ?? 'WF_CMC_MAP_INGEST failed' }}
```

---

## 6. 这条工作流跑完后应该验什么

第一次跑完，至少检查下面 4 件事。

### 检查1：是否写入任务日志

```sql
SELECT *
FROM sys.ingest_run
WHERE workflow_name = 'WF_CMC_MAP_INGEST'
ORDER BY run_id DESC
LIMIT 5;
```

### 检查2：是否写入原始响应

```sql
SELECT response_id, platform_code, endpoint_code, fetched_at
FROM raw.api_response
WHERE endpoint_code = 'cmc_map'
ORDER BY response_id DESC
LIMIT 5;
```

### 检查3：是否写入目录表

```sql
SELECT cmc_id, symbol, name, slug, platform_name, token_address
FROM src_cmc.cmc_asset_map
ORDER BY cmc_id
LIMIT 20;
```

### 检查4：条数是否大致合理

```sql
SELECT COUNT(*) FROM src_cmc.cmc_asset_map;
```

---

## 7. 第一版先不要做的事

为了先跑通，我建议这条工作流第一版不要加这些东西：

1. 不分页并发
2. 不自动补 `coin_basic`
3. 不自动做 source mapping
4. 不自动更新 `core.asset`
5. 不在这里处理 `category`

原因很简单：

这条工作流的目标是让最底层目录事实稳定落库，不要混入后续层的逻辑。

---

## 8. 这条工作流和旧版最大的区别

### 旧版

```text
CMC_MAP -> Code_NORMALIZE_MAP -> coin_basic
```

### 新版

```text
CMC_MAP
-> sys.ingest_run
-> raw.api_response
-> src_cmc.cmc_asset_map
```

差别在于：

- 旧版是“直接写业务表”
- 新版是“先写事实层”

这一步改完，后面系统才真的进入新架构。

---

## 9. 下一条应该接什么

这条跑通后，下一条最自然的是：

## `WF_CMC_INFO_BATCH`

因为 `map` 只有目录，没有：

- `official_website`
- `docs_url`
- `github_url`
- `description`
- `logo`

这些都要靠 `CMC /v2/cryptocurrency/info` 来补。

