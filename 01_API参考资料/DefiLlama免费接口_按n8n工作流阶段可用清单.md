# DefiLlama 免费接口｜按 n8n 工作流阶段可用清单

## 先说结论

- 按你现在的搭建路线，DefiLlama 免费接口里**真正值得先接**的只有 4 类：
  - 协议 TVL
  - 协议 Fees / Revenue
  - 协议发现与 slug 校验
  - 少量基础档案补充
- 你规划里的 **代币解锁**，**不在免费接口里**，属于 Pro API。
- 你规划里的 **现货行情主链路**，**不建议用 DefiLlama 免费接口替代 CMC**，因为它更偏 token price，不是完整市值行情终端。

## 一张表看懂

| 你的阶段 | 模块 | 免费接口可用性 | 推荐接口 | 结论 |
|---|---|---:|---|---|
| 阶段 1 | slug 校验 / 手动补映射 | 可用 | `/protocols`、`/overview/fees` | 可直接用 |
| 阶段 2 | TVL 最小闭环 | 可用 | `/protocol/{protocol}` | 最优先接 |
| 阶段 3 | 协议收入 / 现金流 | 可用 | `/summary/fees/{protocol}?dataType=dailyFees`、`dailyRevenue` | 可直接扩 |
| 阶段 3 | 基础档案更新 | 部分可用 | `/protocol/{protocol}`、`/summary/fees/{protocol}` | 可做补充，不够完整 |
| 阶段 3 | 现货行情采集 | 部分可用 | `/prices/current/{coins}`、`/prices/historical/{timestamp}/{coins}` | 只适合补充，不适合替代 CMC |
| 阶段 3 | 代币解锁采集 | 不可用 | 无免费接口 | 需要 Pro API |
| 阶段 4 | 20 币种批量调度 | 可用 | 继续复用上面接口 | 免费接口足够做 MVP |
| 阶段 5 | 告警与波动预警 | 可用 | `/protocol/{protocol}`、`/summary/fees/{protocol}` | 可直接做 TVL / 收入异动 |
| 阶段 6 | 手动扩充币种库 | 可用 | `/protocols`、`/overview/fees`、`/v2/chains` | 很适合做人工白名单扩容 |
| 阶段 7 | NotebookLM 素材层 | 间接可用 | 建议读你自己的库，不要实时直打 API | 素材层依赖前面落库 |

## 阶段 1：环境初始化时，DefiLlama 能用什么

### 1）协议列表

```text
GET https://api.llama.fi/protocols
```

用途：
- 检查某个协议是否被 DefiLlama 收录
- 人工确认 `defillama_slug`
- 给 `coin_basic.defillama_slug` 填值

适合你现在的原因：
- 你已经决定 `defillama_slug` 人工维护，这个接口正好做白名单校验

### 2）支持 Fees/Revenue 的协议列表

```text
GET https://api.llama.fi/overview/fees
```

用途：
- 看某个协议是否有 fees / revenue 数据覆盖
- 提前确认后续是否能做收入链路

建议：
- 新增币种时，先查 `/protocols` 确认 slug
- 再查 `/overview/fees` 确认这个 slug 有没有收入数据覆盖

### 3）链列表

```text
GET https://api.llama.fi/v2/chains
```

用途：
- 校验链名是否和 DefiLlama 口径一致
- 给你后面做链维度拆分时当字典表参考

## 阶段 2：TVL 最小闭环

### 主接口

```text
GET https://api.llama.fi/protocol/{protocol}
```

你的 Lorenzo 样例：

```text
GET https://api.llama.fi/protocol/lorenzo-protocol
```

这个接口最适合写入：
- `coin_tvl_timeseries`

核心可取字段：
- 协议名称、分类、链列表
- `currentChainTvls`
- `chainTvls`
- 历史 TVL 时序

为什么它适合做你的最小闭环：
- 一个接口就能覆盖 `slug -> 拉时序 -> 清洗 -> 落表`
- 最容易先把幂等、日期转换、手动反复执行这三件事跑顺

### 可选简化接口

```text
GET https://api.llama.fi/tvl/{protocol}
```

用途：
- 只拿当前 TVL 数值

建议：
- **不要把它当主链路**
- 你的目标是时序落库，所以优先还是 `/protocol/{protocol}`

## 阶段 3：扩展现金流 / 基础档案 / 行情

### A. 协议 Fees / Revenue

#### 费用

```text
GET https://api.llama.fi/summary/fees/{protocol}?dataType=dailyFees
```

Lorenzo：

```text
GET https://api.llama.fi/summary/fees/lorenzo-protocol?dataType=dailyFees
```

#### 协议收入

```text
GET https://api.llama.fi/summary/fees/{protocol}?dataType=dailyRevenue
```

Lorenzo：

```text
GET https://api.llama.fi/summary/fees/lorenzo-protocol?dataType=dailyRevenue
```

#### 默认口径

```text
GET https://api.llama.fi/summary/fees/{protocol}
```

默认等价于：

```text
dataType=dailyFees
```

建议你在 n8n 里**不要依赖默认值**，直接写死：
- 费用链路：`dailyFees`
- 收入链路：`dailyRevenue`

这样后面不会混淆。

### B. 基础档案更新

DefiLlama 免费接口能补一部分档案，但**不够当完整主数据源**。

可复用：

```text
GET https://api.llama.fi/protocol/{protocol}
GET https://api.llama.fi/summary/fees/{protocol}
```

可补字段示例：
- 协议名
- 分类
- 链列表
- 官网
- logo
- twitter
- github
- 子协议 / 关联协议

不建议只靠 DefiLlama 的原因：
- 某些币的 token 基础资料不完整
- 代币级字段如流通量、FDV、发行信息，还是 CMC 更合适

### C. 现货行情采集

DefiLlama 免费接口有价格接口，但**不适合替代你的 CMC 主行情链路**。

可用接口：

```text
GET https://api.llama.fi/prices/current/{coins}
GET https://api.llama.fi/prices/historical/{timestamp}/{coins}
GET https://api.llama.fi/chart/{coins}
GET https://api.llama.fi/percentage/{coins}
```

限制点：
- 需要你提供 `chain:address` 或 `coingecko:xxx`
- 更偏 token price 查询
- 不天然覆盖你要的完整现货字段组合：`market_cap / fdv / circulating_supply / volume_24h`

结论：
- **可做补充价格校验**
- **不建议替代 CMC**

### D. 代币解锁采集

结论：**免费接口不可用**

你现在工作流里的“代币解锁”如果坚持用 DefiLlama，需要走 Pro API：

```text
GET https://pro-api.llama.fi/{KEY}/api/emissions
GET https://pro-api.llama.fi/{KEY}/api/emission/{protocol}
```

所以你当前阶段最合理的判断是：
- TVL：先做
- Fees / Revenue：可以做
- Unlock：先挂起，等你确认要不要上 Pro

## 阶段 4：20 币种 MVP 批量测试

这一阶段不需要新增接口，继续复用即可：

- TVL：`/protocol/{protocol}`
- Fees：`/summary/fees/{protocol}?dataType=dailyFees`
- Revenue：`/summary/fees/{protocol}?dataType=dailyRevenue`

这阶段的重点不是“找更多接口”，而是验证：
- 白名单 slug 是否完整
- 单币失败是否可隔离
- 重跑是否幂等
- API 是否会在批量下出现异常

## 阶段 5：告警与运维

DefiLlama 免费接口已经足够支持你做两类预警：

### 1）TVL 异动预警

来源：

```text
GET https://api.llama.fi/protocol/{protocol}
```

可做：
- 1d / 7d TVL 变化
- 单链 TVL 异常下滑

### 2）协议收入 / 费用异动预警

来源：

```text
GET https://api.llama.fi/summary/fees/{protocol}?dataType=dailyFees
GET https://api.llama.fi/summary/fees/{protocol}?dataType=dailyRevenue
```

可做：
- `dailyFees` 突变
- `dailyRevenue` 连续下滑
- `fees` 高、`revenue` 低的变现质量预警

## 阶段 6：选择性扩币

这一阶段 DefiLlama 免费接口非常适合你“只扩白名单，不做全量垃圾导入”的思路。

推荐流程：

1. 先用 `/protocols` 查协议是否存在
2. 再用 `/overview/fees` 看是否有收入覆盖
3. 再人工确认后写入 `coin_basic`

这比“全量导入几千个协议再清洗”更符合你现在的研究目标。

## 阶段 7：NotebookLM 素材联动

这阶段**不建议** NotebookLM 素材子工作流直接打 DefiLlama。

正确做法：
- 先让前面的 TVL / Fees / Revenue 全部稳定落库
- 素材层只读你自己的数据库

原因：
- 素材模板需要稳定字段
- 实时直打外部 API，会让 Markdown 结构经常变

## 你现在真正该接的接口清单

### 立刻可接

```text
GET https://api.llama.fi/protocols
GET https://api.llama.fi/overview/fees
GET https://api.llama.fi/v2/chains
GET https://api.llama.fi/protocol/{protocol}
GET https://api.llama.fi/summary/fees/{protocol}?dataType=dailyFees
GET https://api.llama.fi/summary/fees/{protocol}?dataType=dailyRevenue
```

### 可以留着以后补充

```text
GET https://api.llama.fi/prices/current/{coins}
GET https://api.llama.fi/prices/historical/{timestamp}/{coins}
GET https://api.llama.fi/chart/{coins}
GET https://api.llama.fi/percentage/{coins}
```

### 当前别接，因不在免费接口

```text
GET https://pro-api.llama.fi/{KEY}/api/emissions
GET https://pro-api.llama.fi/{KEY}/api/emission/{protocol}
```

## 最后给你的落地建议

按你现在的节奏，DefiLlama 这一块最合理的执行顺序就是：

1. 先用 `/protocols` 补齐并校验 `defillama_slug`
2. 先只接 `/protocol/{protocol}`，把 TVL 最小闭环跑通
3. TVL 稳定后，再加 `/summary/fees/{protocol}?dataType=dailyFees`
4. 再加 `/summary/fees/{protocol}?dataType=dailyRevenue`
5. 解锁链路先不要做，除非你决定上 Pro API

一句话总结：

- **免费接口足够你完成 TVL + Fees + Revenue 的 MVP**
- **免费接口不够你完成 DefiLlama 解锁链路**
- **现货行情主链路仍然应该交给 CMC**
