# tvl-workflow 验收结果与下一步建议

## 一、当前结果

`tvl-workflow` 已顺利跑通，`coin_tvl_timeseries` 已成功写入 `lorenzo-protocol / BANK / 2026-07-24` 的日度记录。

本次核对后的关键结果如下：

```text
defillama_slug      = lorenzo-protocol
coin_symbol         = BANK
record_date         = 2026-07-24
total_tvl           = 553796603
tvl_btc_chain       = 463745012
tvl_bsc             = 90051456
tvl_eth             = 135
tvl_other           = 0
daily_fees          = 0
daily_revenue       = 0
apr_7d              = 0.013821968447111086
tvl_change_7d       = -8.84885907171609
revenue_change_7d   = -19.160104986876643
data_source         = DefiLlama
```

结论：

- TVL 主链路已跑通
- Fees / Revenue 已成功接入
- `Insert_Rows` 幂等写入已可正常工作
- 当前这张表已经可以作为协议日度基本面快照表使用

## 二、这张表现在能反映什么

`coin_tvl_timeseries` 现在不是“纯 TVL 表”，而是：

**协议日度基本面核心快照表**

它主要回答 4 个问题：

1. 协议当前有多大
2. 最近 7 天资金是在流入还是流出
3. 协议最近有没有赚钱、赚钱能力是否变弱
4. 单位 TVL 对应的收入效率如何

### 字段逻辑

#### 1）规模层

- `total_tvl`：协议当前总锁仓规模
- `tvl_btc_chain / tvl_bsc / tvl_eth / tvl_other`：TVL 链分布

投研意义：

- 看协议体量
- 看协议重心在哪条链
- 看是否过度依赖单链

#### 2）趋势层

- `tvl_change_7d`：当前总 TVL 相比 7 天前的变化百分比

投研意义：

- 看协议最近在吸金还是失血
- 适合做 TVL 异动预警

#### 3）经营层

- `daily_fees`：用户当天支付的总费用
- `daily_revenue`：协议当天实际留存收入
- `revenue_change_7d`：最近 7 天收入相对前一个 7 天的变化

投研意义：

- 看业务活跃度
- 看商业化能力是否在变强或变弱

#### 4）效率层

- `apr_7d`

当前口径：

```text
近7天 revenue 年化 / 近7天平均 TVL * 100
```

注意：

- 它不是用户真实 APY
- 它更接近协议层面的“收入收益率”
- 适合做协议横向比较和内部跟踪

## 三、当前这张表的投研价值

这张表已经能支持你做最基础的协议经营判断：

### 能看出来的

- 协议体量大不大
- 最近 TVL 是在流入还是流出
- 收入有没有恶化
- 收入效率高不高
- TVL 和 Revenue 是否同向变化

### 现在还看不出来的

- 估值是否便宜：缺 `price / market_cap / fdv`
- 供给压力是否要来：缺 `unlock`
- 资金博弈是否极端：缺 `oi / funding / long-short`
- 用户行为是否转弱：缺活跃地址、交易笔数等

所以最准确的定位是：

**它已经是有投研意义的协议基本面表，但还不是完整投研总表。**

## 四、按 `n8n工作流.md`，下一步该搭什么

根据 [n8n工作流.md](file:///e:/瞎搞乱搞/web3/加密货币研究报告/n8n工作流.md)，你已经完成：

- 阶段 2：TVL&协议现金流时序采集最小闭环

按文档里的阶段顺序，下一步进入：

- **阶段 3：逐个搭建剩余采集子工作流**

### 我建议你下一步先搭

**子工作流1：币种基础档案批量同步（周更｜CMC API → coin_basic）**

而不是先上解锁或衍生品。

### 原因

1. `coin_basic` 是所有后续工作流的主表和映射源
2. 后面 `coin_market`、`coin_unlock_events`、`coin_derivatives` 都要依赖更完整的币种基础信息
3. 先把 `cmc_id / coin_name / category / official_website / launch_date` 这些维度补齐，后面扩币种会轻松很多
4. 相比 Unlock 和 CoinGlass，CMC 基础档案链路更稳、更适合作为阶段 3 的第一条

## 五、推荐的实际推进顺序

严格按可维护性排序，我建议你这样走：

### 第一步

**子工作流1：CMC 基础档案同步**

目标：

- 从 `coin_basic` 已有 `cmc_id` 出发
- 调 CMC 获取基础信息
- 更新 `coin_name / category / official_website / total_supply / launch_date / last_updated`

建议写入方式：

- 只更新维度字段
- 保持 `defillama_slug` 仍由人工维护，不自动覆盖

### 第二步

**子工作流3：现货二级市场行情采集（日更｜CMC API → coin_market）**

目标：

- 采集 `price_usd / market_cap / fdv / circulating_supply / volume_24h / change_24h / change_7d`

这一步补完后，你的投研视角会从：

- “协议经营”

升级到：

- “协议经营 + 代币估值”

这是当前最值得补的一层。

### 第三步

**子工作流5：代币解锁事件同步**

注意：

- 你文档里写的是 DefiLlama Unlocks
- 但 DefiLlama 的 Unlock 接口不是免费接口
- 如果你当前没有 Pro API，先不要在这一条上卡太久

可选处理：

- 要么晚一点再做
- 要么改成其他可用数据源

### 第四步

**子工作流4：衍生品合约数据采集（CoinGlass）**

这一步适合在：

- 现货行情链路稳定后
- 你准备开始做交易层预警时

再接入。

## 六、下一步最小落地目标

如果只定一个最小目标，我建议你下一步就做：

**CMC 基础档案同步子工作流**

验收标准建议：

1. 能从 `coin_basic` 读出带 `cmc_id` 的币种
2. 能成功调用 CMC 基础信息接口
3. 能回写 `coin_basic`
4. 重复执行不会写脏数据
5. 单币失败不影响其他币

## 七、一句话路线图

你现在已经完成了：

```text
协议基本面底座（TVL + Fees + Revenue）
```

下一步最合理的是补：

```text
币种基础档案（coin_basic） -> 现货行情（coin_market）
```

等这两步完成后，你的系统就会从“协议数据采集器”升级成真正可用于日常投研跟踪的 MVP。
