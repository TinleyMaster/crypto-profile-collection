# n8n 链上投研系统｜下一步实施清单
> 基于当前进度：
> 1. 子工作流1：币种基础档案批量同步（周更｜CMC API → `coin_basic`）
> 2. 子工作流2：TVL&协议现金流时序采集（日更｜DefiLlama API → `coin_tvl_timeseries`）

## 一、当前阶段判断
你现在已经具备了两块核心基础能力：

1. **币种维表已建立**
   后续所有行情、合约、解锁采集都可以从 `coin_basic` 出发，不需要再单独维护币种列表。

2. **DefiLlama 主链路已验证**
   说明 PostgreSQL 连接、n8n 子工作流调用、分批循环、幂等写入这套模式已经跑通。

下一步不建议马上把 `3/4/5/7/8` 全部一起上，而是按下面顺序推进：

1. **先搭子工作流3：现货二级市场行情采集**
2. **再搭主调度A：每日主调度**
3. **然后补子工作流7：每日简报推送**
4. **最后再扩子工作流5、4**

先把“基础档案 + TVL + 市场价格 + 每日汇总”闭环打通，系统就已经具备投研可用性了。

## 二、下一优先级：子工作流3｜现货二级市场行情采集
目标：

- 数据源：CMC API
- 写入表：`coin_market`
- 频率：日更
- 作用：补齐价格、市值、FDV、成交量、7d 涨跌幅，形成最基本的投研看盘层

### 推荐工作流结构
`When executed by another workflow`
→ `Postgres: 读取跟踪币种`
→ `Split in Batches`
→ `HTTP Request: CMC quotes/latest`
→ `Set / Code: 字段清洗`
→ `Postgres: UPSERT 写入 coin_market`
→ `Merge/汇总成功失败数`
→ `Postgres: 写入 sys_run_log`

### 第一步：读取待跟踪币种
建议 SQL：

```sql
SELECT
  coin_symbol,
  cmc_id
FROM coin_basic
WHERE track_status = '跟踪中'
  AND cmc_id IS NOT NULL;
```

### 第二步：请求 CMC 行情
优先使用 `quotes/latest?id=...`，按 `cmc_id` 查，不要按 symbol 模糊查。

如果当前是逐币跑：

```text
GET https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest?id={{$json.cmc_id}}
Header: X-CMC_PRO_API_KEY: {{$env.CMC_API_KEY}}
```

后面如果想优化额度消耗，再改成“先聚合一批 id 再批量请求”。

### 第三步：字段映射建议
从 CMC 返回里至少提取这些字段写入 `coin_market`：

- `coin_symbol`
- `record_date` = 当天日期
- `price_usd`
- `market_cap`
- `fdv`
- `circulating_supply`
- `volume_24h`
- `change_24h`
- `change_7d`
- `data_source` = `coinmarketcap`

### 第四步：写入 SQL
建议 UPSERT：

```sql
INSERT INTO coin_market (
    coin_symbol,
    record_date,
    price_usd,
    market_cap,
    fdv,
    circulating_supply,
    volume_24h,
    change_24h,
    change_7d,
    holder_count,
    data_source
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11
)
ON CONFLICT (coin_symbol, record_date)
DO UPDATE SET
    price_usd = EXCLUDED.price_usd,
    market_cap = EXCLUDED.market_cap,
    fdv = EXCLUDED.fdv,
    circulating_supply = EXCLUDED.circulating_supply,
    volume_24h = EXCLUDED.volume_24h,
    change_24h = EXCLUDED.change_24h,
    change_7d = EXCLUDED.change_7d,
    holder_count = EXCLUDED.holder_count,
    data_source = EXCLUDED.data_source,
    created_at = NOW();
```

### 子工作流3验收标准
满足下面 4 条就算过关：

1. 重复执行不会产生重复记录
2. `coin_market` 能看到当天所有跟踪币的价格数据
3. 个别币种失败不会让整个工作流中断
4. `sys_run_log` 能记录本次成功数、失败数、报错摘要

## 三、子工作流3搭完后，立刻搭主调度A
你现在已经有：

- 子工作流1：基础档案
- 子工作流2：TVL
- 子工作流3：市场行情（待补）

这时最合适的顶层工作流是：

### 主调度A｜日级主调度
Cron：`0 6 * * *`

执行顺序：

1. 调用【子工作流2：TVL&现金流采集】
2. `Wait 30s`
3. 调用【子工作流3：现货行情采集】
4. `Wait 10s`
5. 调用【子工作流7：每日简报汇总&飞书推送】

## 四、子工作流7：先做轻量版日报
第一版日报只做结构化摘要，不要一开始做复杂 AI 报告。

### 日报建议内容
1. 今日成功采集币种数
2. TVL 变化 Top 5
3. 24h 涨跌幅 Top 5
4. FDV / 市值偏高的异常项目提示
5. 失败币种列表

### 日报数据来源
- `coin_tvl_timeseries`
- `coin_market`
- `sys_run_log`

### 输出方式
- 飞书机器人 webhook
- Markdown 文本，不做图片

## 五、子工作流5 和 4 的建议顺序
在“基础档案 + TVL + 行情 + 日报”跑稳之后，再继续：

### 先做子工作流5：代币解锁事件同步
原因：

- 周更，频率低
- 对投研很有价值
- 运维负担比衍生品低

### 再做子工作流4：衍生品合约数据采集
原因：

- 8 小时一跑，频率更高
- 容错、限流、异常告警要求更高
- 更适合放在系统稳定后再加

## 六、当前阶段最应该补的运维能力
先把这 3 个补上：

1. **`sys_run_log` 标准化**
   - `workflow_name`
   - `status`
   - `total_count`
   - `success_count`
   - `fail_count`
   - `error_detail`
   - `duration_second`

2. **子工作流统一返回结构**

```json
{
  "workflow_name": "子工作流3-现货行情采集",
  "status": "success",
  "total_count": 20,
  "success_count": 19,
  "fail_count": 1,
  "error_detail": "BANK: CMC 429",
  "duration_second": 86
}
```

3. **失败不中断**
   - 单币失败记录下来
   - 批次继续执行
   - 最后统一汇总失败列表

## 七、从今天开始的推荐推进顺序
- [ ] 补齐子工作流3：CMC → `coin_market`
- [ ] 给子工作流1、2、3 统一加 `sys_run_log`
- [ ] 搭建主调度A（日级）
- [ ] 搭建子工作流7（轻量日报）
- [ ] 用 20 个重点币种连续跑 3 天
- [ ] 确认无重复写入、无大面积缺数
- [ ] 再开始子工作流5（解锁）
- [ ] 最后再上子工作流4（衍生品）

## 八、MVP 成型标准
当下面 4 条同时满足时，你的链上投研系统就已经进入可用状态：

1. `coin_basic` 持续维护跟踪池
2. `coin_tvl_timeseries` 每日更新
3. `coin_market` 每日更新
4. 飞书每日收到结构化日报

到这一步，系统就已经能稳定产出投研数据了。后面的 Google Drive、NotebookLM、解锁预警，都是加能力，不是从零开始。
