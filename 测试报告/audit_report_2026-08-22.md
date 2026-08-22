# 加密货币投研资料采集系统 — 投研审计报告

> 审计日期：2026-08-22  
> 审计目标：以"产出可用投研结论"为核心，评估系统数据质量、功能可用性与投研可信度  
> 审计范围：网站 https://crypto-profile-collection.zeabur.app/ + 源代码 + PostgreSQL 数据库（只读）  
> 审计原则：不改代码，只审计与提出优化建议

---

## 一、执行摘要（TL;DR）

系统架构完整、功能模块齐全，但**投研结论的可信度存在严重数据层缺陷**。核心问题集中在：

1. **资产主数据污染**：23 个资产名称与 CMC 官方不一致，最严重的是 Bitcoin（rank 1）被错误命名为 "Bullish Trump Coin"（meme 币）
2. **代币经济学数据单位混乱**：LLM 提取的 total_supply 与 core.asset 实际供应差异达 5 个数量级（ETH 差 49 万倍），且已传导至最终投研结论
3. **投研数据覆盖极低**：18,061 资产中，链上快照仅 49（0.27%）、代币经济学 100（0.55%）、衍生品 6（0.03%）、研究结论仅 6 条
4. **多个分析模块因数据缺失无法运行**：背离检测（price:false）、链上持仓（空）、解锁（无缓存）、GitHub 活跃度（0 条）
5. **搜索排序与分页参数失效**：limit 参数被硬编码为 20，且高市值资产未优先排序

**结论**：当前系统处于"基础设施已搭建、数据管线未跑通"的阶段。对于 TOP 资产（ETH 等）部分功能可用，但数据准确性不足以支撑真实投研决策；对于长尾资产，几乎所有投研分析功能均不可用。

---

## 二、P0 级问题（阻断投研可信度）

### P0-1：资产主数据身份混淆 — Bitcoin 被命名为 "Bullish Trump Coin"

| 项目 | 详情 |
|------|------|
| 影响 | 投研结论、搜索、竞品对比、前端展示全部受影响 |
| 根因 | `core.asset_source_map` 中 asset_id=2 同时映射了 cmc_id=1（Bitcoin）和 cmc_id=32295（Bullish Trump Coin），同 symbol 合并逻辑未做 name/rank 校验 |
| 证据 | `core.asset` 中 asset_id=2：canonical_name='Bullish Trump Coin'，asset_type='meme'，但 market_cap_rank=1、market_cap=$1.29T |
| 范围 | 23 个资产存在名称与 CMC 不一致（含 ETH/XRP/TRON/DOGE 等 TOP 资产） |
| 代码位置 | 跨源匹配脚本（bootstrap_cmc / bootstrap_cg_list）的 symbol-only 合并策略 |

**建议**：
- 紧急：对 `core.asset_source_map` 中一个 asset_id 映射多个 cmc_id 的 84 条记录进行人工/规则复核
- 引入 cmc_rank 作为合并校验字段：若新来源的 rank 与已确认主来源差异过大，标记为冲突而非自动覆盖
- 在 `core.asset` 更新时增加"名称突变检测"：canonical_name 变更幅度超过阈值时进入待审核队列

### P0-2：代币经济学 total_supply 单位错误传导至投研结论

| 项目 | 详情 |
|------|------|
| 影响 | 通胀率/未流通占比计算爆炸、估值维度结论失真 |
| 根因 | LLM 从官网/文档提取的 total_supply 未做单位归一化（ETH 提取为 244.086，实际为 1.2 亿） |
| 证据 | `biz.asset_tokenomics` 中 ETH total_supply=244.0862694（LLM 提取），core.asset total_supply=120,682,172（CMC 数据），差异 49 万倍 |
| 传导 | 研究结论生成时引用 tokenomics.total_supply，输出"ETH 总供应约 2.441 亿枚"——错误数据进入最终结论 |
| 范围 | 7 个资产存在 supply 严重偏离（ratio<0.1 或 >10） |

**建议**：
- 在 tokenomics 入库前增加 supply 校验层：与 core.asset 的 supply 做 ratio 检查，偏离 >10 倍时标记为"单位疑似错误"
- LLM prompt 中明确要求输出 supply 的单位（如"枚"、"百万枚"、"十亿枚"），并在解析时做单位换算
- 研究结论生成时，优先使用 core.asset 的 supply 数据（经过多源交叉验证），tokenomics 仅作补充

---

## 三、P1 级问题（严重影响可用性）

### P1-1：搜索接口 limit 参数失效 + 排序不合理

| 项目 | 详情 |
|------|------|
| 证据 | `app.py: _get_db_stats().search_assets(q, limit=20, tier=tier)` 硬编码 limit=20，忽略前端传入的 limit 参数 |
| 影响 | 搜索 "bitcoin" 返回 20 条且 Bitcoin 未排首位（首位是 HarryPotterObamaSonic10Inu）；搜索 "BTC" 首位是 Bullish Trump Coin |
| 建议 | 读取 request.args.get("limit", 20)；搜索排序加入 market_cap_rank 权重（rank 越小越靠前） |

### P1-2：背离检测模块数据路径设计缺陷

| 项目 | 详情 |
|------|------|
| 证据 | divergence API 返回 price:false，但 asset_market_daily 中有价格数据 |
| 根因 | `get_divergence_signals()` 仅从 `biz.asset_social_heat.market_json` 取价格，而 60%（180/298）的 social_heat 记录 market_json 为空 |
| 建议 | 背离检测应直接从 `biz.asset_market_daily` 取价格数据（这是权威行情源），social_heat 仅作情绪数据补充 |

### P1-3：链上持仓快照断更 3 天

| 项目 | 详情 |
|------|------|
| 证据 | `biz.onchain_holder_snapshot` 最新日期 2026-08-19，今天是 08-22；调度表规定每日 05:30 运行 |
| 根因 | `sys.ingest_run` 中无链上相关运行记录（workflow_name 无 onchain/holder/snapshot 关键字） |
| 影响 | 链上持仓、鲸鱼行为、CEX 净流入等模块全部无数据 |
| 建议 | 检查 scheduler.py 中链上快照任务的配置与运行日志；确认 Ethplorer API key 是否有效/额度是否耗尽 |

### P1-4：竞品对比 inflation_pct 计算语义错误 + 数值爆炸

| 项目 | 详情 |
|------|------|
| 证据 | competitors API 返回 ETH inflation_pct=-49,442,127.27% |
| 根因 | db_stats.py 中 `(1 - circ/total) * 100` 计算的是"未流通占比"，但字段名 inflation_pct 暗示通胀率；且 total_supply 来自 tokenomics（244），circ 来自 core.asset（1.2 亿），导致 circ >> total |
| 建议 | ① 重命名字段为 `unlocked_pct` 或 `circulating_ratio`；② 计算前校验 circ <= total，否则报错或取反；③ 统一 supply 数据源 |

### P1-5：衍生品数据仅覆盖 6 个资产

| 项目 | 详情 |
|------|------|
| 证据 | `biz.asset_derivatives` 仅 6 条记录 |
| 影响 | 衍生品资金面分析对 99.97% 的资产不可用 |
| 建议 | 检查 derivatives_client.py 的采集逻辑：是否仅对特定列表运行？是否 API 限流导致只采集了前几个？ |

---

## 四、P2 级问题（体验/工程优化）

### P2-a：研究结论生成引用编号语义不清

- 现象：sentiment 维度引用 index 27/28/30，但只列了 3 个 URL，编号与资料全集索引的对应关系不透明
- 建议：引用编号采用"维度.序号"格式（如 supply.1, market.3），并在前端展示时支持 hover 查看来源

### P2-b：KOL 监控仅 1 个博主、27 条帖子

- 现象：KOL 页面显示"监控博主 1"，信号 27 条，今日信号 0
- 建议：确认 kol_daemon 是否正常运行；检查币安广场接口是否变更（Playwright 拦截 bapi 可能因前端改版失效）

### P2-c：GitHub 活跃度、大额转账、白皮书摘要均为 0

- 现象：功能模块存在但数据为 0
- 建议：检查对应调度任务是否启用；确认 GitHub API token 是否配置；大额转账需先有链上快照数据

### P2-d：行情历史仅 3 天数据

- 现象：ETH market-history 返回 3 个数据点（8/19-8/21）
- 建议：CMC 行情快照 pipeline（WF_CMC_QUOTE_SNAPSHOT）已成功运行 7 次，但 asset_market_daily 只有 1016 个资产、2975 条记录——说明快照入库逻辑可能只处理了部分资产，或去重/更新逻辑导致旧数据被覆盖

### P2-e：解锁数据仅 28 个资产

- 现象：解锁事件覆盖 28 资产，解锁压力 8 资产
- 建议：tokenomics.com 爬取覆盖率有限，可考虑对无解锁数据的资产自动标记"无已知解锁计划"而非返回错误

### P2-f：SPA 爬取进度 75.9% 但官网正文质量未知

- 现象：docs 深度爬取 55,203/69,601（79.3%），official_website 28,100/40,138（70.0%）
- 风险：大量爬取内容可能为空页/错误页/反爬页，content_topics 分类覆盖率需进一步验证

---

## 五、数据覆盖全景（18,061 资产基准）

| 数据域 | 覆盖资产数 | 覆盖率 | 最新日期 | 状态 |
|--------|-----------|--------|---------|------|
| 资产主数据 | 18,061 | 100% | — | 存在 23 条名称污染 |
| 文档入口 | 260,659 条 | — | — | 正常 |
| 日行情 | 1,016 | 5.6% | 8-21 | 数据积累中 |
| 社交热度 | 295 | 1.6% | 8-22 | 正常 |
| 代币经济学 | 100 | 0.55% | 8-19 | 数据积累中 |
| 解锁事件 | 28 | 0.15% | — | 数据积累中 |
| 链上快照 | 49 | 0.27% | 8-19 | **断更 3 天** |
| 衍生品 | 6 | 0.03% | 8-22 | 覆盖率极低 |
| 融资数据 | 474 | 2.6% | — | 正常 |
| 研究结论 | 6 条 | — | 8-21 | 极少 |
| 研究笔记本 | 28 | — | — | 正常 |
| 每日推荐 | 150 | — | 8-22 | 正常 |
| 白皮书摘要 | 1 | ~0% | — | 未启动 |
| GitHub 活跃 | 0 | 0% | — | 未启动 |
| 大额转账 | 0 | 0% | — | 未启动 |
| KOL 信号 | 27 条 | — | 8-21 | 仅 1 博主 |

---

## 六、投研结论可信度评估

| 维度 | 可信度 | 说明 |
|------|--------|------|
| 资产身份识别 | ⚠️ 低 | 23 个资产名称错误，搜索排序不合理 |
| 估值分析 | ⚠️ 低 | supply 单位错误传导至结论，inflation 计算语义错误 |
| 筹码分析 | ❌ 不可用 | 链上快照断更，集中度/鲸鱼行为无数据 |
| 情绪分析 | ✅ 中 | 社交热度数据正常，但 market_json 缺失率高 |
| 催化分析 | ⚠️ 低 | 解锁数据极少，融资数据覆盖 2.6% |
| 衍生品资金面 | ⚠️ 低 | 仅 6 个资产有数据 |
| 背离检测 | ❌ 不可用 | price 数据路径错误，链上数据缺失 |
| 竞品对比 | ⚠️ 低 | inflation 计算错误，supply 单位混乱 |
| KOL 信号 | ⚠️ 低 | 仅 1 博主，样本量不足 |
| 每日推荐 | ✅ 中 | 数据正常，但无法验证回测质量 |

**综合评估**：当前系统**不适合直接用于真实资金决策**。建议优先修复 P0/P1 数据质量问题，待数据覆盖率达到 TOP 1000 资产 >80% 后再投入实际投研使用。

---

## 七、优化建议优先级清单

### 立即执行（本周）
1. **修复 Bitcoin 等 23 个资产的名称污染**：核对 `core.asset_source_map` 多 cmc_id 映射记录，按 cmc_rank 和 market_cap 确定正确名称
2. **修复 tokenomics supply 单位校验**：对 LLM 提取的 supply 做 ratio 校验，偏离 >10 倍时拒收或标记
3. **修复搜索 limit 参数与排序**：读取 request limit，按 market_cap_rank 加权排序
4. **修复 divergence 价格数据源**：从 asset_market_daily 取价格，不从 social_heat.market_json

### 短期执行（2 周内）
5. **恢复链上快照采集**：排查 scheduler 中 holder snapshot 任务失败原因
6. **扩大衍生品覆盖**：检查 derivatives_client 的采集逻辑与限流处理
7. **增加 supply 计算防御**：circ > total 时返回 None 并告警，不输出负值
8. **KOL 监控扩容**：确认 kol_daemon 运行状态，增加监控博主数量

### 中期执行（1 个月内）
9. **数据质量监控面板**：新增"数据新鲜度/覆盖率/异常值"监控 API，前端展示各模块健康度
10. **研究结论引用可追溯**：引用编号与资料库 entry_id 绑定，支持点击跳转原文
11. **行情历史数据补全**：排查 CMC 快照入库逻辑，确保每日全量资产行情落库
12. **跨源合并规则加固**：symbol-only 匹配增加 rank/market_cap/name_similarity 多重校验

---

## 八、附录：关键测试记录

- 健康检查：✅ 200
- 资产搜索：✅ 200（limit 参数失效）
- 投研页面 /research/1209：✅ 200（2.2s）
- KOL 页面 /kol：✅ 200（1.7s）
- 代币经济学 API：✅ 正常（但数据单位错误）
- 衍生品 API：✅ 正常
- 行情历史 API：✅ 仅 3 天数据
- 背离检测 API：⚠️ price:false
- 解锁 API：❌ 无缓存数据
- 链上持仓 API：⚠️ 空响应
- 研究结论生成：✅ 异步任务 25s 完成（但结论含错误数据）
- 每日推荐 API：✅ 正常
