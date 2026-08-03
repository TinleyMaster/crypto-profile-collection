# 子工作流1｜CMC 全量同步 -> coin_basic（节点级配置清单）

## 一、这条工作流的目标

这条子工作流的职责不是跑时序数据，而是：

**把 CMC 可识别到的币种全量同步进 `coin_basic`，作为全量资产主表。**

然后由后续所有业务工作流只读取：

```sql
track_status = '跟踪中'
```

## 二、这条工作流的最终定位

### 做什么

- 全量拉取 CMC 币种目录
- 更新 `coin_basic` 基础身份信息
- 优先补齐 `category`
- 默认把新币标记为 `未跟踪`
- 对已有记录做增量更新

### 不做什么

- 不跑 TVL
- 不跑行情时序
- 不自动把所有币加入跟踪
- 不自动生成 `defillama_slug`
- 不做弱匹配写入 `coingecko_id`

## 三、推荐的字段分层

### A. 自动同步字段

- `cmc_id`
- `coin_symbol`
- `coin_name`
- `main_chain`
- `contract_addresses`
- `official_website`
- `docs_url`
- `github_url`
- `launch_date`
- `last_updated`

说明：

- 这组字段是“CMC 体系内可自动补齐字段”
- 其中 `category` **不来自** `GET /v1/cryptocurrency/map`
- `category` 应在 `子工作流1B：CMC_INFO 详情补充同步` 中补齐

### B. 半自动强匹配字段

- `defillama_slug`
- `coingecko_id`

说明：

- `coingecko_id` 只有在“官网域名一致”或“合约地址完全一致”时才写入
- `defillama_slug` 只有在 `DefiLlama /protocols` 白名单中命中，且官网域名一致时才写入
- 不满足强匹配时，宁可留空，也不要脏写

### C. 人工维护字段

- `remark`
- `track_status`

说明：

- 新资产默认 `track_status = '未跟踪'`
- 只有人工确认过映射后，才改成 `跟踪中`

## 四、推荐使用的 CMC 接口

这条全量同步工作流，建议分两层接口：

### 1）全量目录层

```text
GET /v1/cryptocurrency/map
```

用途：

- 拿全市场币种基础目录
- 返回 `id / symbol / name / slug / is_active / first_historical_data / last_historical_data / platform`

这是底库入口。

补充说明：

- `map` 适合建全量底库
- `map` **不会返回 `category`**
- 所以 `category` 不应在 1A 阶段指望补齐

### 2）详情补充层

```text
GET /v2/cryptocurrency/info?id=...
```

用途：

- 补 `website / docs / github / description / logo / date_launched / platform`

注意：

- 这个接口不适合“一次把全市场所有 id 全拉完”
- 推荐按批次补充

## 五、工作流建议拆成两条

为了稳定，我建议你把“全量同步 coin_basic”拆成两条子工作流：

### 子工作流1A：CMC_MAP 全量目录同步

目标：

- 建立和刷新 `coin_basic` 主底库

### 子工作流1B：CMC_INFO 详情补充同步

目标：

- 对指定范围币种补充详情字段

这样做的原因：

- `map` 轻、稳、适合全量
- `info` 重、慢、适合分批
- `id 映射` 歧义高，适合单独拆出去做强匹配补齐

## 六、先搭哪条

我建议你先搭：

**子工作流1A：CMC_MAP 全量目录同步**

因为它最关键，先把底库建起来。

## 七、子工作流1A：CMC_MAP 全量目录同步

## 节点顺序

```text
When executed by another workflow
-> HTTP_CMC_MAP
-> Code_NORMALIZE_MAP
-> Split Out
-> PG_UPSERT_COIN_BASIC
-> PG_LOG_RUN（可选）
```

## 节点1：When executed by another workflow

直接使用标准子工作流触发器。

## 节点2：HTTP_CMC_MAP

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

### Query Parameters（推荐）

```text
listing_status = active
sort = cmc_rank
```

如果你想连退市资产也保留到底库，后面再改。

### 预期返回

返回数组，每个元素通常有：

- `id`
- `name`
- `symbol`
- `slug`
- `is_active`
- `platform`

## 节点3：Code_NORMALIZE_MAP

作用：

- 把 CMC 原始返回转成 `coin_basic` 可写入结构
- 对新资产默认标记为 `未跟踪`
- 不自动写 `defillama_slug`

补充说明：

- 这里的 `category` 先明确写 `null` 是正常行为
- 因为 `HTTP_CMC_MAP` 本身没有返回 `category`
- 后续由 `子工作流1B：CMC_INFO 详情补充同步` 再更新

可直接粘贴代码：

```javascript
const rows = $json.data ?? [];

return rows.map((row) => {
  const platform = row.platform ?? null;

  return {
    json: {
      cmc_id: row.id ?? null,
      coin_symbol: row.symbol ?? null,
      coin_name: row.name ?? null,
      cmc_slug: row.slug ?? null,
      category: null,
      main_chain: platform?.name ?? null,
      contract_addresses: platform ? {
        chain: platform.name ?? null,
        symbol: platform.symbol ?? null,
        address: platform.token_address ?? null
      } : null,
      track_status: '未跟踪',
      last_updated: new Date().toISOString()
    }
  };
});
```

说明：

- 我这里保留了 `cmc_slug`
- 如果你当前 `coin_basic` 还没有这个字段，建议加上
- 因为它对后面做 CMC 定位、人工校验很有帮助

## 节点4：Split Out

把数组拆成逐条 upsert。

## 节点5：PG_UPSERT_COIN_BASIC

### 推荐 SQL

```sql
INSERT INTO coin_basic
(
    cmc_id,
    coin_symbol,
    coin_name,
    category,
    main_chain,
    contract_addresses,
    track_status,
    last_updated,
    remark
)
VALUES
(
    $1,
    $2,
    $3,
    $4,
    $5,
    $6::jsonb,
    $7,
    $8,
    $9
)
ON CONFLICT (cmc_id) DO UPDATE SET
    coin_symbol = EXCLUDED.coin_symbol,
    coin_name = EXCLUDED.coin_name,
    category = COALESCE(EXCLUDED.category, coin_basic.category),
    main_chain = COALESCE(EXCLUDED.main_chain, coin_basic.main_chain),
    contract_addresses = COALESCE(EXCLUDED.contract_addresses, coin_basic.contract_addresses),
    last_updated = EXCLUDED.last_updated;
```

### 参数区

```text
[
{{ $json.cmc_id }},
{{ $json.coin_symbol ?? null }},
{{ $json.coin_name ?? null }},
{{ $json.category ?? null }},
{{ $json.main_chain ?? null }},
{{ JSON.stringify($json.contract_addresses ?? null) }},
{{ $json.track_status ?? '未跟踪' }},
{{ $json.last_updated }},
{{ $json.cmc_slug ?? null }}
]
```

### 关于 `remark`

上面我临时把 `cmc_slug` 放进了 `remark`，是为了你现在先跑通。

但更推荐的长期方案是：

```sql
ALTER TABLE coin_basic ADD COLUMN cmc_slug VARCHAR(128);
```

然后把 `remark` 换回真正备注用途。

## 八、子工作流1A 的验收标准

跑通后，你至少检查这几件事：

1. `coin_basic` 能插入大量新币种
2. 已有币种重复执行时不会重复写脏数据
3. 新币默认 `track_status = '未跟踪'`
4. `cmc_id` 能稳定作为主键
5. `contract_addresses` JSON 能正常写入

## 九、子工作流1B：CMC_INFO 详情补充同步

这条建议在 1A 跑稳后再搭。

## 节点顺序

```text
When executed by another workflow
-> PG_GET_COIN_BASIC_NEED_DETAIL
-> Code_BUILD_ID_BATCH
-> HTTP_CMC_INFO
-> Code_NORMALIZE_INFO
-> Split Out
-> PG_UPDATE_COIN_BASIC_DETAIL
```

## 节点1：PG_GET_COIN_BASIC_NEED_DETAIL

建议 SQL：

```sql
SELECT
    cmc_id,
    coin_symbol,
    coin_name,
    track_status
FROM coin_basic
WHERE cmc_id IS NOT NULL
  AND (
      official_website IS NULL
      OR docs_url IS NULL
      OR github_url IS NULL
      OR launch_date IS NULL
  )
ORDER BY cmc_id
LIMIT 200;
```

说明：

- 详情同步不要一上来全量打爆
- 先按 100-200 条一批跑

## 节点2：Code_BUILD_ID_BATCH

作用：

- 把多行拼成一个 CMC `id=1,2,3...`

代码：

```javascript
const items = $input.all();
const ids = [];

for (const item of items) {
  if (item.json.cmc_id) ids.push(String(item.json.cmc_id));
}

return [{
  json: {
    cmc_ids: ids.join(',')
  }
}];
```

## 节点3：HTTP_CMC_INFO

### URL

```text
https://pro-api.coinmarketcap.com/v2/cryptocurrency/info?id={{$json.cmc_ids}}
```

### Headers

```text
X-CMC_PRO_API_KEY: {{ $env.CMC_API_KEY }}
Accept: application/json
```

## 节点4：Code_NORMALIZE_INFO

作用：

- 从 `info` 结果中提取详情字段
- 优先补 `category`

补充说明：

- `category` 的主要来源应放在这里，而不是 `map`
- 如果 `info` 某些币也没有明确 `category`，再按 `tag_groups[0]` 或 `tags[0]` 兜底

可直接粘贴代码：

```javascript
const data = $json.data ?? {};
const result = [];

for (const [cmcId, arr] of Object.entries(data)) {
  const info = Array.isArray(arr) ? arr[0] : arr;
  const platform = info?.platform ?? null;
  const category =
    info?.category ??
    info?.tag_groups?.[0] ??
    info?.tags?.[0] ??
    null;

  result.push({
    json: {
      cmc_id: Number(cmcId),
      coin_name: info?.name ?? null,
      category,
      official_website: info?.urls?.website?.[0] ?? null,
      docs_url: info?.urls?.technical_doc?.[0] ?? null,
      github_url: info?.urls?.source_code?.[0] ?? null,
      main_chain: platform?.name ?? null,
      contract_addresses: platform ? {
        chain: platform.name ?? null,
        symbol: platform.symbol ?? null,
        address: platform.token_address ?? null
      } : null,
      launch_date: info?.date_launched ? info.date_launched.split('T')[0] : null,
      last_updated: new Date().toISOString()
    }
  });
}

return result;
```

## 节点5：PG_UPDATE_COIN_BASIC_DETAIL

### SQL

```sql
UPDATE coin_basic
SET
    coin_name = COALESCE($2, coin_name),
    category = COALESCE($3, category),
    official_website = COALESCE($4, official_website),
    docs_url = COALESCE($5, docs_url),
    github_url = COALESCE($6, github_url),
    main_chain = COALESCE($7, main_chain),
    contract_addresses = COALESCE($8::jsonb, contract_addresses),
    launch_date = COALESCE($9, launch_date),
    last_updated = $10
WHERE cmc_id = $1;
```

### 参数区

```text
[
{{ $json.cmc_id }},
{{ $json.coin_name ?? null }},
{{ $json.category ?? null }},
{{ $json.official_website ?? null }},
{{ $json.docs_url ?? null }},
{{ $json.github_url ?? null }},
{{ $json.main_chain ?? null }},
{{ JSON.stringify($json.contract_addresses ?? null) }},
{{ $json.launch_date ?? null }},
{{ $json.last_updated }}
]
```

## 十、子工作流1C：ID 映射补齐（`coingecko_id` / `defillama_slug`）

这条不要并进 1A / 1B 主链，建议单独做成“半自动强匹配补齐”。

### 目标

- 补齐 `coingecko_id`
- 补齐 `defillama_slug`
- 只在强匹配成立时写回 `coin_basic`

### 节点顺序

```text
When executed by another workflow
-> PG_GET_COIN_BASIC_NEED_IDS
-> HTTP_COINGECKO_COINS_LIST
-> Code_MATCH_COINGECKO_STRONG
-> HTTP_DEFILLAMA_PROTOCOLS
-> Code_MATCH_DEFILLAMA_STRONG
-> Loop Over Items
-> PG_UPDATE_COIN_BASIC_IDS
```

### 强匹配规则

#### 1）`coingecko_id`

满足下面任一条件才允许写入：

- `contract_addresses.address` 与 CoinGecko 结果完全一致
- `official_website` 主域名与 CoinGecko 项目官网主域名完全一致

否则：

- 不写入

#### 2）`defillama_slug`

满足下面全部条件才允许写入：

- `DefiLlama /protocols` 中存在候选协议
- 官网主域名完全一致
- 项目名称或代币名称基本一致

否则：

- 不写入

### 推荐 SQL：读取待补齐资产

```sql
SELECT
    cmc_id,
    coin_symbol,
    coin_name,
    official_website,
    contract_addresses,
    coingecko_id,
    defillama_slug
FROM coin_basic
WHERE cmc_id IS NOT NULL
  AND (
      coingecko_id IS NULL
      OR defillama_slug IS NULL
  )
ORDER BY cmc_id
LIMIT 200;
```

### 推荐 SQL：回写强匹配结果

```sql
UPDATE coin_basic
SET
    coingecko_id = COALESCE($2, coingecko_id),
    defillama_slug = COALESCE($3, defillama_slug),
    last_updated = $4
WHERE cmc_id = $1;
```

### 参数区

```text
[
{{ $json.cmc_id }},
{{ $json.coingecko_id ?? null }},
{{ $json.defillama_slug ?? null }},
{{ $json.last_updated }}
]
```

### 这条工作流的边界

- 不做弱匹配
- 不因为名称相似就写入 ID
- 不覆盖已经人工确认过的 `defillama_slug`
- 如果需要人工复核，建议先只输出候选结果，不直接回写

## 十一、全量主表和白名单采集怎么衔接

### 全量层

`coin_basic`：

- 尽量多地收录币种
- 作为“资产目录”和“映射总台账”

### 白名单层

其他时序工作流统一这样读取：

```sql
SELECT *
FROM coin_basic
WHERE track_status = '跟踪中'
  AND defillama_slug IS NOT NULL;
```

或：

```sql
SELECT *
FROM coin_basic
WHERE track_status = '跟踪中'
  AND cmc_id IS NOT NULL;
```

## 十二、你现在最建议的推进顺序

1. 先搭 `子工作流1A：CMC_MAP 全量目录同步`
2. 确认 `coin_basic` 能稳定 upsert
3. 再搭 `子工作流1B：CMC_INFO 详情补充同步`，先把 `category / 官网 / docs / github / launch_date` 补起来
4. 再搭 `子工作流1C：ID 映射补齐`
5. 最后再去做 `coin_market`

原因：

- 先有完整底库
- 先补确定性字段
- 再补高价值 ID 映射
- 后有白名单采集
- 再补行情层

## 十三、一句话总结

这条工作流的本质是：

**先用 CMC 建立全量资产底库并补齐确定性字段，再用强匹配补 `coingecko_id / defillama_slug`，最后用 `track_status` 控制谁进入正式投研采集。**
