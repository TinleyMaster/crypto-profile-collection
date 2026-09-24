# Coinglass 套餐数据接入方案（存量利用率提升 + 爆仓历史回填）

> 定位：本方案是 [盘面异动扫描系统设计方案](./盘面异动扫描系统设计方案.md) §12.1 缺口清单的**执行方案**。
> 范围仅限 CoinGlass 付费数据；**不改判定阈值、不新增打分维度、不替换任何现有免费源**。
> 状态：**P0-D / P0-A / P0-C / P1 均已完成**（P0-D+P0-B 见 2026-09-23；P0-A / P0-C / P1 见 2026-09-24，已实跑验收）；P2 未排期（按 §4.5 触发条件单独立项）。
> 2026-09-24 评审修订：落地 **5 处必改**（symbol 口径 / 删除 `alias_bases()` 映射 / 覆盖时长 / 中断归因 / 验收期望值）+ **P0-C 目标降级**（「拉齐口径」→「量化偏高幅度**下界**」），逐条见 §5.2、§3.3-8、§1.2-3、§4.4、§8.2、§4.3。

## 0. 一句话结论

Hobbyist 套餐提供 **80+ 接口、30 req/min（≈43,200 次/天）**，线上实际只用了 **1 个接口、288 次/天（0.7%）**。
本方案分四步把闲置额度换成**可标定的样本**：

- **P0-D（首批，已完成）** 加密早报「3衍生品」接入 **24h 爆仓概况**：只读 DB、**零接口调用**、只展示不进分（§4.7）；
- **P0** 让「已采未用」的列进入消费，并把爆仓比率改成**可标定的估计**（**量化** §10.5 混合口径的偏高幅度**下界**，不声称「拉齐口径」）；
- **P1** 用 `liquidation/aggregated-history` 回填 **180 天 × 4h** 爆仓历史，把 §12.1 的「爆仓维历史回测能力接近于零」补成正样本；
- **P2** 按**明确触发条件**决定是否扩基差 / OI 拆分 / 订单簿深度等维度（不预建、不预采）。

---

## 1. 背景与问题

### 1.1 现状盘点（已核实，2026-09-23）

| 层 | 事实 | 证据位置 |
|---|---|---|
| 采集 | 线上唯一在跑的 CoinGlass 调用是 `liquidation/coin-list`，每 300s 一次 → `biz.liquidation_snapshot` | `scripts/bin/scan_daemon.py`（`task_scan_liquidation`） |
| 客户端 | 已封装但**全仓零调用**：`liquidation_history()` / `liquidation_exchange_list()` / `funding_rate_history()` | `scripts/src/crypto_research/clients/coinglass_client.py` |
| 落库 | 每轮写入 5 列**无人消费**：`liq_usd_4h/_12h/_24h`、`long_liq_usd_4h`、`short_liq_usd_4h` | 判定链只读 `long/short_liq_usd_1h`（`analysis/squeeze_fuel.py`、`scan_daemon.task_scan_squeeze`） |
| 丢弃 | 接口已返回的 `long/short_liquidation_usd_12h/_24h` **连列都没有**，写入时静默丢弃 | `task_scan_liquidation` 的 INSERT 列清单 |
| 额度 | 288 / 43,200 次/天，用量 **0.7%** | 官方定价页 `30 Rate limit/min` |

### 1.2 三个问题的性质不同

1. **存而不用的浪费**（P0-A）：采集端已经把 4h/12h/24h 三档滚动窗口取回来了，消费端只认 1h。
2. **口径缺陷**（P0-C）：§10.5 已披露「分子 = CoinGlass **全交易所**爆仓额 / 分母 = **Binance** 24h 成交额」⇒ 比率系统性偏高，两个 `*_LIQ_RATIO` 阈值是在混合口径下标出来的临时值。而套餐里**恰好有单交易所口径**（`liquidation/history` 的 `exchange=Binance`），可据此给出混合口径**偏高幅度的下界**——⚠️ 但**不能「一次性把口径拉齐」**，理由见 §4.3 的目标降级。
3. **能力缺口**（P1）：`liquidation_snapshot` 是**滚动窗口快照**、**自 09-21 02:00 UTC 起才积累**（2026-09-24 实测：全表 **648** 个 5min 批次 ≈ **54h 有效覆盖** / 73h 跨度，527 币；原写「自 09-16 起」与数据不符，已更正），导致 §12.1 的 A3（无样本外）、B11（爆仓阈值只有临时值）、C9（爆仓维历史≈0）**无法用数据解决**——而 `aggregated-history` 提供的是**分段增量**，180 天 × 4h 可直接当回测正样本。

### 1.3 目标与非目标

**目标**

- G1：CoinGlass 已付费字段的**消费率 100%**——要么被消费，要么被明确标注为「仅供展示/标定」，不留「写而不读」。
- G2：给出「现役混合口径相对同源单所口径**偏高幅度的下界**」，为阈值重新标定提供**下界级**依据（⚠️ **目标降级（2026-09-24）**：不声称「同源同窗口拉齐」——分子端可同源，分母端窗口无法对齐，见 §4.3）。
- G3：≥180 天、≥4h 粒度的爆仓历史落库，供 `workbench/calib_squeeze_liq_thr.py` 一类的标定/回测脚本消费。

**非目标（本方案明确不做）**

- N1：**不动任何阈值**（`SQZ_SHORT_LIQ_RATIO_MIN` / `LONG_LIQ_RATIO_THR` 取值一律以标定脚本实跑为准，不写进文档）。
- N2：**不把 4h/1d 粒度数据接进 5m 实时判定链**（粒度不匹配，见 §4.4）。
- N3：不替换 Binance 免费源（K 线 / OI / CVD / 多空比继续走自建与免费端点）。
- N4：不接期权、现货行情、ETF flow、指数类（理由见 §4.6）。

---

## 2. 数据基础（套餐边界，已实测 + 已核对官方文档）

### 2.1 套餐硬边界

| 项 | Hobbyist | 说明 |
|---|---|---|
| 价格 / 接口数 | $29/mo / 80+ | — |
| 限频 | **30 req/min** | 官方定价页；客户端 docstring 记录的「1.3 req/s 连续 30 次无 429」是**突发额度实测值，不可依赖**，一律以 30/min 为预算 |
| 时间粒度下限 | **4h** | 1h 及以下返回 `code=403 + details.upgrade_required=STANDARD`；⚠️ **HTTP 状态恒为 200**，业务错误只在 body 的 `code` 里 |
| 历史范围 @4h | **180 天**（官方文档口径；**30 天窗口已实测全量返回 = 180 行/币**，180 天上限本身仍未直接验证 ⇐ 见 §8.2 实测取证） | @6h/8h/12h = 360 天；@1d = 全历史 |

### 2.2 与本方案相关的接口可用性（Hobbyist）

| 接口 | 用途 | 可用 | 粒度限制 |
|---|---|---|---|
| `/api/futures/liquidation/coin-list` | 滚动窗口快照（**现役**） | ✅ | 无（自带 1h/4h/12h/24h） |
| `/api/futures/liquidation/history` | 交易对级爆仓历史 | ✅ | `>=4h` |
| `/api/futures/liquidation/aggregated-history` | 币种级爆仓历史（多所聚合） | ✅ | `>=4h` |
| `/api/futures/liquidation/exchange-list` | 各交易所爆仓额 | ✅ | `>=4h` |
| `/api/futures/funding-rate/history` | 资金费率 OHLC | ✅ | `>=4h` |
| `/api/futures/basis/history` | 期货基差 | ✅ | `>=4h` |
| `/api/futures/open-interest/aggregated-*` | 聚合 OI / 稳定币本位 / 币本位拆分 | ✅ | `>=4h` |
| `/api/futures/orderbook/ask-bids-history`（含 aggregated） | 挂单深度（±range） | ✅ | `>=4h` |
| `/api/futures/open-interest/exchange-list`、`/taker-buy-sell-volume/exchange-list`、`/funding-rate/accumulated-exchange-list`、`/pairs-markets` | 全市场快照类 | ✅ | **无限制** |
| `/api/futures/liquidation/map`、`/aggregated-map`、`/max-pain`、`/orderbook/large-limit-order*`、`/liquidation/order` | 清算地图 / 大额挂单 / 实时爆仓单 | ❌ | Standard+ |

### 2.3 已确认不可用（勿重复探测）

- **多空比全系列**（`global/top-long-short-*`、`net-position`）：Hobbyist 下 404 ⇒ 大户多空仓仍只能走 Binance 免费端点 + §10.10 的代理口径。
- **爆仓热图 model1~3**：401 Upgrade plan。
- `/api/futures/coins-markets`、`/coins-price-change`、`/api/spot/coins-markets`：文档标注 ❌（**反直觉**，只有 `pairs-markets` 可用）。

---

## 3. 总体设计

### 3.1 三层改动，边界不重叠

```
采集层  P0：不改节奏（仍 300s 一次 coin-list）   P1：新增一个独立回填脚本（一次性 + 可重跑）
存储层  P0：不加列（先定消费点）                 P1：新增 biz.liquidation_history（4h 起）
消费层  P0：判定邮件/展示 + 标定脚本读新列        P1：只进标定/回测脚本，不进实时判定
```

### 3.2 阶段交付边界

| 阶段 | 交付物 | 是否影响实时判定 | 是否需重启容器 |
|---|---|---|---|
| **P0-D** | 早报「3衍生品」24h 爆仓概况（只读 DB）+ 前置补列 `fix_068` | 否 | **是**（`scan_daemon` 需重启才写新列；早报侧无需重启） |
| **P0-A** | 消费侧改造（读已落库的 4h/12h/24h） | 否（只影响展示与标注） | 是（`scan_daemon`） |
| **P0-C** | 混合口径**偏高幅度下界**（标定脚本侧） | 否 | 否 |
| **P1** | 迁移 `fix_069` + `phase_backfill_liq_history.py` + 单测 | 否 | 否 |
| **P2** | 按触发条件单独立项 | — | — |

### 3.3 必须继承的既有纪律（逐条映射，违反即返工）

1. **缺失 ≠ 0**（§10.6）：任一列缺失走 `missing` 分支，**绝不用 0 或 4h/4 之类的换算冒充**。
2. **滚动窗口严禁跨桶差分**（AGENTS.md P1-1）：`coin-list` 的 `*_liq_usd_*` 是滚动窗口，相邻快照相减 = 「新滚入 − 滚出」，平稳时≈0、回落时为负。
3. **口径必须显式披露**（§10.5）：本文新增**第二种**爆仓口径（分段增量），必须在列名、注释、邮件脚注三处同时标注。
4. **阈值不在文档留数字**（§10.5 / §10.10.4）：本方案只交付「可标定样本」，不交付取值。
5. **影子模式**（AGENTS.md）：任何新增判定分支先只记录不发信。
6. **metrics 版本位**：`metrics` 结构变更递增 `fuel_metric_ver` / `gap_metric_ver` 类版本位，跨版本回看历史行必须先看它。
7. **迁移幂等**：`CREATE TABLE IF NOT EXISTS` + 可重复执行，`ON CONFLICT DO UPDATE` 语义明确。
8. **符号直传本库合约码，不做别名映射**（更正见 §5.2）：P1 是**请求侧**取数（把池内合约码原样传给接口），与 `scan_daemon._perp_alias_map()` / `sqz.alias_bases()` 的**响应侧反向匹配**（消费 `coin-list` 返回的基码）不是同一场景 ⇒ 回填**不需要**别名映射；`1000PEPEUSDT` 这类前缀币在池内本就是这个码，原样传参即可。
9. **文档与代码同步**：落地后按 §10 清单回写。

---

## 4. 详细设计

### 4.1 P0-A：让「已采未用」的 4h/12h/24h 列进入消费

**先明确两种列的正确用法**（这是本节的核心，写错就会重演 P1-1 假 0）：

| 允许 | 禁止 |
|---|---|
| ① **跨币横截面**比较（同列、同窗口 ⇒ 可比） | ❌ 与 `*_liq_usd_1h` 混算比值（分子滚动窗口不同，比值无意义） |
| ② **规模背景展示**（邮件里给「最近 1h / 24h」两档绝对额） | ❌ 跨桶差分（见 §3.3-2） |
| ③ **存在性判据**：「该币 24h 内是否发生过成规模爆仓」 | ❌ 用 `4h ÷ 4` 当 1h（滚动窗口是累计额，不是均值） |
| ④ 标定脚本的分组分桶维度 | ❌ 作为 5m 窗口判定的输入 |

**三个消费点**

1. **告警邮件规模背景**（`scan_daemon` 判定邮件的爆仓行）：在现有「最近 1h 多空爆仓」后追加「（24h 累计：多 X / 空 Y）」，脚注沿用并扩展现有 `liq_scope=coinglass_rolling_1h` 披露。
2. **缺口存在性标注**：当 `_latest_liq_snapshot()` 因超龄（> `LIQ_SNAPSHOT_MAX_AGE_MIN`）返回 `None` 时，**判定行为不变**（仍走 `missing` ⇒ `mixed`），但在 `metrics.fuel` 里额外落 `liq_bg_24h_usd`（背景值）+ `liq_scope='coinglass_rolling_24h'`，供排查与邮件脚注使用。
3. **标定脚本**：`workbench/calib_squeeze_liq_thr.py` 增加按 `liq_usd_4h` / `liq_usd_24h` 的分组维度（仅只读统计）。

> **P0-A 不新增列**：先用现有 5 列把消费打通；确有必要补列时，**先有消费点再有列**（避免继续制造「写而不读」）。

### 4.2 P0-B（原可选）：补 `long/short_liq_usd_24h` 列

**触发条件**：仅当邮件背景行确定需要「24h 多空分列」时才加。
> **已定（2026-09-23）：必需**——P0-D 的方向行依赖它，已随 `fix_068_liquidation_snapshot_24h_split.sql` 上线。
> **范围收敛**：实际只补 **24h** 两列；`12h` 分列**未补**（当前无消费点，避免制造新的「写而不读」）。
若只展示合计额，则**不加列**——当前 INSERT 丢这两个字段，属已知行为，不是缺陷。

### 4.3 P0-C：混合口径**偏高幅度下界**（只改标定侧，不动线上判定）

**现状问题**：`short_liq_ratio = CoinGlass 全交易所 1h 爆仓额 / Binance 24h 成交额`，分子分母**不同源不同窗**。

**本方案并列给出两个口径**（不互相替换；A = 现役**混合**口径，B = 新增**单所同窗**口径，仅用于给 A 的偏高幅度定下界）：

| 口径 | 分子 | 分母 | 粒度 | 用途 |
|---|---|---|---|---|
| A（现役，保留） | coin-list 全交易所滚动 1h | Binance 滚动 24h 成交额 | 5m | 线上判定（已披露为混合口径） |
| **B（新增）** | `liquidation/history` `exchange=Binance` 的 **4h 分段增量** | `asset_klines` 1h 的 `quote_vol` 在同一 4h 墙钟区间求和 | 4h | **标定**：给出「混合口径偏高幅度」的**下界**（非阈值基准） |

- 口径 B 的分子分母**取同一 4h 墙钟区间**，自洽性可验证（分母可由 `asset_klines` 直接求和，无需新数据源）。
- ⚠️ 口径 B 是 **4h 分段增量**、口径 A 是 **滚动窗口**——**两者数值不可换算、不可相加**，任何比较必须在同一口径内做。
- 口径 B **不接入判定**（粒度不匹配，N2）；它只回答一个问题：**混合口径的偏高幅度至少是多少**。
- ⚠️ **目标降级（2026-09-24 评审结论）**：原目标「把口径拉齐」**不成立**，改为「**量化偏高幅度的下界**」。唯一能严格成立的方向是**分子端**：口径 A 的分子 = **全交易所**爆仓额 ⊇ 口径 B 的分子 = **Binance** 单所 ⇒ `A分子 / B分子 ≥ 1`；而**分母端**（Binance **24h** 成交额 vs 同一 **4h** 区间成交额）**无法在同一窗口对齐** ⇒ 「跨所放大」与「分母窗口错配」两个来源**不可分离**，`A/B` 之比**只能读作下界**，**不得**据此直推「阈值应平移多少」。

### 4.4 P1：4h 爆仓历史回填

**为什么必须用新表**（不能复用 `biz.liquidation_snapshot`）：

- `liquidation_snapshot` 的 PK 是 `(symbol, ts)`，且 `ts` 是 **5m 桶**；4h 对齐点同时是 5m 对齐点 ⇒ **回填行会与轮询行撞同一 PK**（`ON CONFLICT DO UPDATE` 会互相覆盖）。
- 两表**语义不同**：snapshot = 滚动窗口绝对值；history = **分段增量**。放同一张表必然被误用。
- 保留期不同：snapshot 30 天（`prune_scan_data`），history 需长期保留。

**表设计**：见 §5。**关键列 `interval` 与 `exchange_scope` 都进 PK**，从结构上杜绝「4h 与 1d 混桶」「Binance 与全所混算」。

**回填预算（按 527 个池内币估算）**

| 项 | 估算 |
|---|---|
| 每币每口径请求数 | 180 天 ÷ 4h = **1080 点**；`limit` 取客户端上限 **4500**（超出 ⇒ `code=400`）⇒ **单请求即覆盖全窗口** = **1 次/币**（`--dry-run` 实打值） |
| 总请求数 | 527 × 1 **次/币** = **527 次**（仅 Binance 口径） → 两口径 **1054 次** |
| 耗时 | 1054 ÷ 24 req/min ≈ **44 分钟**（单口径 527 次 ≈ **22.0 分钟**，`--dry-run` 实测值） |
| 落库行数 | 527 × 1080 ≈ **57 万行/口径**（PG 无压力） |

**节流与容错**

- `min_request_gap = 2.5s`（= 24 req/min，为 daemon 的 coin-list 与手动调用留 6 req/min 余量）；**禁止复用 `CG_MIN_GAP = 0.3`**。
- 429 / `code != 0` 走**指数退避**（1→2→4→8s，上限 3 次）后跳过该币并记录，**不整轮失败**。
- **可续跑**（强约束，非可选）：88 分钟 **≫ 容器实例寿命实测 ≈74.6 min**（AGENTS.md 寿命实测）⇒ 作业**必然跨执行窗口**。⚠️ **归因更正（2026-09-24）**：中断源是**实例定期回收 / 部署重启**这类**常态机制**，**不是** AGENTS.md 记录的那几次**异常停摆**（那是已被修复的缺陷，不能当作常态中断归因）。⇒ 必须有游标表（§5 的 `biz.liquidation_backfill_cursor`），`--resume` 从断点继续。
- 回填**幂等**：`PK + ON CONFLICT DO UPDATE`，重复执行只覆盖不新增。

**回填数据的消费边界（必须写进代码注释与表注释）**

- ✅ 允许：`workbench/calib_squeeze_liq_thr.py`、`backtest_scan_scenarios.py` 一类的**标定/回测**脚本。
- ❌ 禁止：`scan_squeeze` / `squeeze_fuel` 的任何实时分支。
- 护栏：单测断言「实时判定的 SQL 不含 `biz.liquidation_history`」，并在表注释首行写明用途限制。

### 4.5 P2：扩维（**有触发条件才立项**，不预先采集）

| 候选 | 触发条件 | 落到哪里 |
|---|---|---|
| `basis/history`（期货基差，4h+） | 判定链需要「即时溢价/拥挤度」补 `funding`（8h 结算，滞后）时 | 新表 `biz.basis_snapshot` + 标定侧 |
| `open-interest/aggregated-stablecoin-history` / `aggregated-coin-margin-history` | 需要区分「稳定币本位 vs 币本位」杠杆结构时 | 标定侧优先 |
| `orderbook/ask-bids-history`（±range 深度） | 真正开始做 §7 的 B8「价差/深度过滤」时 | 过滤层 |
| 全市场快照类（`open-interest/exchange-list` 等，无粒度限制） | 需要降低 Binance `/futures/data/*` 的 weight 消耗时 | 采集层替代/校验 |

### 4.6 明确不做的事（避免过度工程）

| 不做 | 理由 |
|---|---|
| 期权 `option/*` | 项目对期权**零消费点**，接了也只会变成新的「写而不读」 |
| ETF `/api/etf/*/flow-history` | 已有 `biz.etf_flow_daily`（另有来源与清洗链路），接入仅为交叉校验，收益 < 维护成本 |
| 现货行情 / `futures/price/history` | Binance 免费源已覆盖，重复取数会引入第二套口径 |
| 指数类（Fear&Greed / AHR999 / Rainbow 等） | 与现有 `fear_greed` / `market_daily` 重叠，且当前无消费点 |
| `/api/exchange/balance/*`（链上） | **已判定不接**：`macro_market.py` 明确记为「替代死链路 CoinGlass」「CM Community 原生，**比 CoinGlass 可靠**」⇒ 项目已选用 CM Community 承担交易所净流，接回来是主动降级。（早先「疑似 legacy v2 路径」的复核推测**已撤销**，不作为待确认项） |

### 4.7 加密早报接入（**决策记录：首批只接「24h 爆仓概况」**）

**决策（2026-09-23，已确认）**：首批只接**24h 爆仓概况**一项；落位方式为**扩展现有「3衍生品」维度**；**只展示、不进分**。
本项**先于 P0-A/P1 开工**，因为它是唯一「零接口调用」的消费点。

**为什么选早报作为第一个消费端**

| 维度 | 说明 |
|---|---|
| 口径匹配 | 早报是**日级**产品，24h 滚动窗口天然对齐；Hobbyist 的 4h 粒度地板对日报**无影响**，可绕开扫描系统的粒度痛点 |
| 零新增采集 | 24h 滚动值（及本方案补列的 24h 多空分列）在 `biz.liquidation_snapshot` 里**已经存在**，早报直接读表 ⇒ **0 次接口调用** |
| 额度 | 08:30 落快照一次，不产生持续额度占用 |
| 不碰打分 | 「只展示不进分」与扫描系统的影子模式同构，保持现有 `emotion_subscore` / `structure_subscore` 序列可比 |

**链路落位（硬约束）**

```
08:30  build_daily_brief.py → get_market_overview(force_refresh="1")
         ├─ fetchers 注册表新增 ("liquidation", fetch_liquidation_overview, ())   ← 只读 DB，不调接口
         ├─ 组装层：dimensions["3衍生品"]["data"] 增挂 "liquidation_24h"
         └─ save_snapshot() → biz.market_overview_snapshot.payload (JSONB)
09:00  send_daily_brief.py → 只读快照 → render_brief_html()   ← 禁止在渲染期调 CoinGlass
```

- 数据源必须挂在 `macro_market.get_market_overview()` 的 `fetchers` 注册表里（08:30 上游），**不能**放进 `render_brief_html()` 的渲染期。
- 旧快照缺 `liquidation_24h` key 时渲染**隐藏该行**（缺失≠0），不显示 0。

**取数设计（三个必须处理的坑）**

1. **批次对齐去重**：`liquidation_snapshot` 是 5min 桶、每币一行，直接按时间范围求和会**跨桶重复计数** ⇒ 必须 `DISTINCT ON (symbol) ... ORDER BY symbol, ts DESC` **每币只取该批次最新一行**再求和。
2. **批次窗口**（锚定 now，而非固定取「表里最后一行」）：窗口内无行 ⇒ `status="error"`、整行隐藏（陈旧数据不展示，**缺失≠0**）。
   初版取 20min，**真机取证后判定过紧**：实测批间隔 p50=300s、p95=600s，但 7 天内出现过 3 次 >20min 的空洞（最大 14.4h）；且早报 08:30 取数时刻的最近一批曾陈旧 **30min** ⇒ 原值会让整行在真实运行中静默消失。已放宽到 **4h**（取值只留代码常量，不入文档），并以 `ts` 披露「数据截至」补偿时效。
3. **覆盖率护栏**（缺失≠0 的展示层体现）：若该批次命中的 symbol 数低于池内标的数的**约定比例**（阈值只留代码常量，不在文档留数字），**降级为「本期数据不完整，暂不展示」**，绝不展示一个偏小的假合计。
   标定证据：`liquidation_snapshot` 全表 425 个批次 / 近 30h 的 338 个批次，`coverage_ratio` **恒为 1.0000**（每批次都写满池内全部 symbol）⇒ 下限取「无假阳性风险、但对响应被截断更敏感」的较高值。

> **已知局限（不掩盖）**：护栏分母 = **过去 24h 该表出现过的 `count(DISTINCT symbol)`**，与分子同源自洽（§4.7 决策），代价是**持续性的整体截断无法被检出**——若连续 24h 每批都只返回一半 symbol，分母会同步塌陷到该半数，`ratio` 仍≈1.0。该场景的兜底只能来自**跨天对比**（今日 `symbols_covered` 与昨日快照的偏离），属后续增量，不在 P0-D 范围。

**展示口径披露（三处必须一致）**

| 项 | 口径 |
|---|---|
| 数据源 | `CoinGlass 全交易所 · 滚动 24h · 5min 快照` |
| 覆盖范围 | **池内 N 个标的合计**，**不得写成「全网爆仓」** |
| 时效 | 批次窗口放宽到 4h 后，脚注必须带 **`数据截至 MM-DD HH:MM`**（`ts` 按北京时间渲染）；`ts` 缺失/不可解析则省略该段，不阻断整行 |
| 档位 | `1h / 4h / 12h / 24h` 四档属**同族滚动窗口**，可比较占比（「近 1h 占 24h 的 X%」合法）；**禁止**与 4h 分段增量（`liquidation_history`）混算 |

**多空方向行的前置依赖**

- 现有表**没有** `long/short_liq_usd_24h`（只有合计 `liq_usd_24h`），接口返回的 12h/24h 多空分列历史上被写入端丢弃 ⇒ 需 §5.1 的 `ALTER TABLE`（**已随 `fix_068` 上线**，即原 P0-B **由条件触发转为必需**）。
- **补列前**只展示 24h 合计 + 四档；**补列后**新增「多 X / 空 Y」；**旧行无方向 ⇒ 不补 0、不显示方向**（降级为 `status="partial"`，合计仍展示）。

**「不进分」的实现护栏**

合并只允许发生在**组装层**（`dimensions` 组装处）。**不得**在 `fetch_binance_derivatives()` 的返回值上原地注入新字段 —— 因为该对象会作为 `derivatives=` 实参进入 `compute_emotion_subscore()`，原地注入会让新字段进入打分函数的可见范围。

实现落点：`build_derivatives_dimension()` 用 `dict(derivatives or {})` 浅拷贝后挂 `liquidation_24h`，原对象键集合不被打分函数以外的路径看见（单测断言）。

**实现落点清单（已落地，2026-09-23）**

| 层 | 文件 | 改动 |
|---|---|---|
| 存储 | `scripts/migrations/fix_068_liquidation_snapshot_24h_split.sql` | 补 `long/short_liq_usd_24h` 两列（幂等） |
| 写入 | `scripts/bin/scan_daemon.py` `task_scan_liquidation` | payload 取值 + INSERT 列 + `ON CONFLICT DO UPDATE` 各补两列 |
| 取数 | `workbench/macro_market.py` `fetch_liquidation_overview()` / `_read_liquidation_snapshot()` / `summarize_liquidation_snapshot()` / `_latest_row_per_symbol()` | 只读 DB（`DISTINCT ON` + 批次窗口 + 覆盖率护栏），纯函数可单测 |
| 组装 | 同上 `build_derivatives_dimension()` + `fetchers` 注册表新增 `("liquidation", fetch_liquidation_overview, ())` | 浅拷贝挂载，不进分 |
| 邮件 | `scripts/bin/send_daily_brief.py` `_render_liquidation_row()` / `_fmt_liq_as_of()` + `macro_market.generate_morning_brief()` 生成 `M2_liquidation` | 缺失整行隐藏；口径 + 时效脚注 |
| 契约 | `workbench/brief_data_model.py` `_WARNING_FIELDS` 登记 `M2_liquidation: [liq_usd_24h]` | 缺失仅作**一般降级**提示（与 `M2_etf_flow` 同属可选辅助模块） |
| 单测 | `workbench/test_liq_overview_brief.py` | 49 条断言：去重不重复计数 / 覆盖率不足返回 None / 缺失≠0 / 不污染 `compute_emotion_subscore` / 渲染口径 + 时效披露 |

**两个取值（标定依据见上「三个坑」，取值只留代码常量、不入文档）**：`LIQ_OVERVIEW_MIN_COVERAGE_RATIO`（覆盖率下限）、`LIQ_OVERVIEW_BATCH_WINDOW_MIN`（批次窗口分钟数）。

**与 §4.5 候选的关系**：B（全市场杠杆三件套）/ C（OI 拆分）/ D（期权与机构情绪）**保留为候选**，触发条件见 §4.5；ETF 流入 / Fear&Greed / 山寨季 / 稳定币净流 / BTC 周期指标在早报**已有来源**，不重复接。

---

## 5. 表结构

### 5.1 `fix_068_liquidation_snapshot_24h_split.sql`（**已上线**，仅 P0-B 两列）

> 落地位置：`05_代码与脚本/scripts/migrations/fix_068_liquidation_snapshot_24h_split.sql`
> 只补两列，**不含** P1 的 `liquidation_history` / `liquidation_backfill_cursor`（另编 `fix_069`，见 §5.2）。

```sql
ALTER TABLE biz.liquidation_snapshot
    ADD COLUMN IF NOT EXISTS long_liq_usd_24h  NUMERIC(24,2),
    ADD COLUMN IF NOT EXISTS short_liq_usd_24h NUMERIC(24,2);

COMMENT ON COLUMN biz.liquidation_snapshot.long_liq_usd_24h IS
  '滚动 24h 多单爆仓额（CoinGlass 全交易所口径）。与 liq_usd_24h 同族滚动窗口，'
  '可比较占比；严禁跨桶差分，严禁与 biz.liquidation_history 的 4h 分段增量换算。'
  '历史行 NULL，不得补 0。';
COMMENT ON COLUMN biz.liquidation_snapshot.short_liq_usd_24h IS
  '滚动 24h 空单爆仓额（CoinGlass 全交易所口径）。与 liq_usd_24h 同族滚动窗口，'
  '可比较占比；严禁跨桶差分，严禁与 biz.liquidation_history 的 4h 分段增量换算。'
  '历史行 NULL，不得补 0。';
```

- 写入端：`scripts/bin/scan_daemon.py` 的 `task_scan_liquidation`（`INSERT ... ON CONFLICT DO UPDATE` 已带两列）。
- 消费端：`workbench/macro_market.py:fetch_liquidation_overview()`（只读）→ 早报「3衍生品」，不进分。
- 接口历史上已在返回值里给出这两个字段，只是写入端丢弃 ⇒ **属补列而非新增采集，零额外额度**。
- 历史行无法回补（滚动窗口值只存在于当时的响应里）⇒ 旧行保持 NULL，**严禁补 0**。

### 5.2 `fix_069_liquidation_history.sql`（**P1 已落地**，2026-09-24，幂等可重复执行）

```sql
-- 1. 4h+ 爆仓历史（分段增量口径，与 liquidation_snapshot 的滚动窗口严格区分）
CREATE TABLE IF NOT EXISTS biz.liquidation_history (
    symbol          TEXT        NOT NULL,        -- 本库合约码，如 BTCUSDT（与 biz.liquidation_snapshot.symbol 同口径；≠ coin-list 返回的基码 BTC）
    interval        TEXT        NOT NULL,        -- 4h/6h/8h/12h/1d，显式存，禁混桶
    exchange_scope  TEXT        NOT NULL,        -- 'binance' / 'all'，口径列，禁混算
    ts              TIMESTAMPTZ NOT NULL,        -- 区间起点（UTC，interval 对齐）
    long_liq_usd    NUMERIC(24,2),               -- 该区间内多单爆仓额（分段增量）
    short_liq_usd   NUMERIC(24,2),
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, interval, exchange_scope, ts)
);
CREATE INDEX IF NOT EXISTS idx_liq_history_sym_iv_scope_ts
    ON biz.liquidation_history (symbol, interval, exchange_scope, ts DESC);

COMMENT ON TABLE biz.liquidation_history IS
  'CoinGlass 4h+ 爆仓历史（分段增量，非滚动窗口）。'
  '⚠️ 仅供标定/回测消费，禁止接入 scan_squeeze / squeeze_fuel 实时判定；'
  '⚠️ 与 biz.liquidation_snapshot 的滚动窗口口径不可换算、不可相加。';

-- 2. 回填游标（容器重启后 --resume 用）
CREATE TABLE IF NOT EXISTS biz.liquidation_backfill_cursor (
    symbol          TEXT        NOT NULL,
    interval        TEXT        NOT NULL,
    exchange_scope  TEXT        NOT NULL,
    done_through    TIMESTAMPTZ NOT NULL,        -- 已成功覆盖到的区间起点（含）
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, interval, exchange_scope)
);
```

> 编号说明：P0-D 开工时占用了 `fix_068`，故 P1 顺延为 `fix_069`。落地位置：`05_代码与脚本/scripts/migrations/fix_069_liquidation_history.sql`（2026-09-24 已手动应用，**连跑两次均成功**，幂等验证通过）。

> **符号口径更正（2026-09-24）**：`symbol` 存**本库合约码**（`BTCUSDT`），与 `biz.liquidation_snapshot.symbol`、`--symbols` 同源同口径。原注释写「币种码，如 BTC（与 coin-list 的 symbol 同口径）」是**两处都不对**：① `coin-list` 返回的是**币种基码**（客户端 docstring 示例 `"symbol": "BTC"`，`scan_daemon` 靠 `_perp_alias_map()` 才能映射成合约码）；② 本表由**请求侧**写入，`liquidation/history` 按**交易对**请求（客户端示例 `BTCUSDT`）。
> 两个接口的符号取值域**不同**：`liquidation/history` = 交易对级、`aggregated-history` = 币种级（§2.2）⇒ `--scope all` 走聚合接口时，映射方向（币种码 → 合约码）**必须先 `--probe` 实测返回值写法**再定，**不得**照抄 `_perp_alias_map()`（它是消费 `coin-list` 响应时的**反向**匹配，且带 `alias_bases()` 的「抢码」防抖，本场景用不上）。

**保留策略**：`liquidation_history` **不纳入 `prune_scan_data` 清理**（体量可控且为回测资产）；
若日后体量增长，再按 `interval` 分档处理（同 `prune_scan_data` 的既有分档风格）。

---

## 6. 脚本与调度

### 6.1 `scripts/bin/phase_backfill_liq_history.py`（新增）

| CLI | 语义 |
|---|---|
| `--probe` | 逐接口打 1 次，打印 `code` / `msg` / `upgrade_required` / 返回条数；**用于套餐边界复验**，不写库 |
| `--dry-run` | 只算请求数与预计耗时，不写库、不落游标 |
| `--scope binance\|all` | 口径；`all` 需先取 `/api/futures/supported-exchanges` 动态拼 `exchange_list`（**已实测确认**：文档无 `all` 快捷值，缺失 `exchange_list` ⇒ `code=400`；实测 `n=11`）；且 `symbol` 须传**币种基码**，传合约码 ⇒ `code=0` 但 0 行 |
| `--interval 4h` | 粒度（默认 4h） |
| `--days 180` | 回填窗口（Hobbyist @4h 上限 180 天） |
| `--symbols BTCUSDT,ETHUSDT` | 指定币；**默认全池 = `biz.liquidation_snapshot` 近 7 天出现过的 symbol**（= 现役扫描池 527 币，与早报/判定链同口径；**不取 Binance `exchangeInfo` 全量**，否则含大量池外长尾、平白多花额度）。合约码**原样直传**，不做别名映射 —— 映射是响应侧的事，见 §3.3-8 |
| `--resume` | 从 `liquidation_backfill_cursor` 续跑 |
| `--json` | 机器可读输出（供验收断言） |

**并发**：单进程串行 + `min_request_gap=2.5s`（**不并发**，避免与 daemon 争额度）。

### 6.2 调度

- **P0/P1 均不进 `scheduler.py`**：P1 是一次性回填 + 手动重跑，无周期语义。
- 若日后需要滚动保鲜（如每月补一次尾部），再按 `告警胜率赔率日报方案` 的方式注册 `core` 类别任务（**不新增类别**，`task_manager.CATEGORY_MAX` 未收录的类别会落到默认上限 2）。

---

## 7. 阈值与标定

- 本方案**不产出任何阈值数字结论**（§3.3-4）。P0-C 与 P1 的产出是**样本**，取值一律以标定脚本当次实跑为准。
- 标定前置条件（不满足则脚本内 `rc!=0` 拒判，沿用既有 `sample_ok/decisive/reliable` 三态）：
  1. `liquidation_history` 在目标窗口内**覆盖率达标**（按 (symbol, interval, exchange_scope) 统计缺口，缺失不补 0）；
  2. 口径 A 与口径 B **分表分组**统计，禁止合并成一个分布；
  3. `asset_klines` 1h 在同期覆盖达标（口径 B 的分母来源）。
- 现役 `SQZ_SHORT_LIQ_RATIO_MIN` / `LONG_LIQ_RATIO_THR` **保持不动**；⚠️ 口径 B 只给**偏高幅度下界**（§4.3 目标降级），故「可动」的门槛是**下界结论**（至少能说出被放大多少），**不是**「已拉齐口径」。

---

## 8. 落地计划与验收命令

| 阶段 | 交付 | 状态 |
|---|---|---|
| **P0-D** | 早报「3衍生品」接入 24h 爆仓概况（只读 DB + 展示，不进分） | ✅ **已完成**（2026-09-23，验收见下） |
| **P0-B** | 补 24h 多空分列（**已确认必需**：P0-D 方向行依赖） | ✅ **已上线**（`fix_068`） |
| **P0-A** | 扫描侧消费改造（展示 + 标注 + 标定维度） | ✅ **已完成**（2026-09-24，`metrics.fuel` 新增 `liq_bg_*` 四键 + `FUEL_METRIC_VER` 1→2） |
| **P0-C** | 混合口径偏高幅度**下界**（标定脚本侧） | ✅ **已完成**（2026-09-24，口径 A/B 分表 + 四门闸门；实测 `--days 1` ⇒ rc=3 样本不可用，属预期） |
| **P1** | `fix_069` + 回填脚本 + 游标 + 单测 | ✅ **已完成**（2026-09-24，迁移已应用且幂等；回填按需手动执行） |
| **P2** | 按 §4.5 触发条件单独立项 | ⬜ 未排期 |

**部署动作（P0-D 上线后必做）**：`fix_068` 迁移应用 + **重启 `scan_daemon` 容器**（否则写入端仍是旧 INSERT，新列恒为 NULL）。
早报侧（`build_daily_brief` / `send_daily_brief`）无需重启，且**重启前即为合法降级形态**：`status="partial"`、方向行隐藏、24h 合计照常展示。

**部署动作（P0-A / P0-C / P1 上线后）**：`fix_069` 迁移**已应用**；P0-A 改动在 `scan_daemon.py` + `squeeze_fuel.py` ⇒ **需重启 `scan_daemon` 容器**（否则 `metrics.fuel` 仍按 v1 写、无 `liq_bg_*`）；P1 回填脚本与迁移**离线运行，无需重启**（回填按需触发，见 §4.4 预算与节流）。

---

### 8.1 P0-D 验收命令（已完成，可独立复跑）

```bash
# 0) 语法检查（期望 exit=0）
python -m py_compile 05_代码与脚本/workbench/macro_market.py \
                    05_代码与脚本/scripts/bin/send_daily_brief.py \
                    05_代码与脚本/scripts/bin/scan_daemon.py \
                    05_代码与脚本/workbench/brief_data_model.py

# 1) 迁移幂等：连跑两次，第二次应为 0 变更、无异常
#    psql -f 05_代码与脚本/scripts/migrations/fix_068_liquidation_snapshot_24h_split.sql

# 2) 新增护栏单测（期望 49 通过 / 0 失败）
python 05_代码与脚本/workbench/test_liq_overview_brief.py
# 断言：① 同批次每币只取一行（不跨桶重复计数）；② 覆盖率不足返回 None 而非 0；
#       ③ 列缺失返回 None 而非 0；④ 新字段不出现在 compute_emotion_subscore 的可见范围；
#       ⑤ 渲染行无「全网爆仓」字样、缺失整行隐藏；⑥ 时效披露按北京时间渲染

# 3) 早报数据契约回归（期望 20 通过 / 0 失败）
python 05_代码与脚本/workbench/test_brief_data_model.py

# 4) 取数真机复跑（只读 DB，零接口调用）
#    期望：status ∈ {ok, partial}；coverage_ratio ≈ 1.0；liq_usd_24h 与 liq_usd_1h 非 None
python -c "import sys;sys.path[:0]=[r'05_代码与脚本/scripts/bin',r'05_代码与脚本/workbench'];import macro_market as mm;print(mm.fetch_liquidation_overview())"

# 5) 早报链路
python 05_代码与脚本/scripts/bin/build_daily_brief.py --dry-run
# 期望：payload 的 dimensions["3衍生品"]["data"] 含 liquidation_24h（四档 + symbols_covered + scope_note）
python 05_代码与脚本/scripts/bin/send_daily_brief.py --dry-run
# 期望：① 口径披露三处一致（CoinGlass 全交易所 / 滚动 24h / 池内 N 个标的合计）；
#       ② 缺 key 时该行隐藏而非显示 0
```

**验收实测记录（2026-09-23）**

- `py_compile` exit=0（4 个文件）。
- `test_liq_overview_brief.py` → **49 通过 / 0 失败**。
- `test_brief_data_model.py` → **20 通过 / 0 失败**。
- `fix_068` 连跑两次 → 幂等，无异常。
- 标定取证（决定性）：`liquidation_snapshot` 全表 425 批次 / 近 30h 338 批次，`coverage_ratio` **恒为 1.0000**；批间隔 p50=300s / p95=600s，7 天内 3 次 >20min 空洞（最大 14.4h）；**早报 08:30 取数时刻最近一批曾陈旧 30min** ⇒ 原 20min 窗口会导致整行静默消失（已修正为 4h + 时效披露）。
- 真机取数：`coverage_ratio=1.0`（527/527）、`liq_usd_24h=$232.3M`、`liq_usd_1h=$10.0M`；方向列因**容器未重启**为 `None` ⇒ `status="partial"`（预期降级，非缺陷）。
- 真机渲染：`💥 24h 爆仓 $232.3M · 近 1h 占 24h 的 4.3%` + 脚注`口径：CoinGlass 全交易所 · 滚动 24h · 5min 快照 · 池内 527 个标的合计 · 数据截至 09-23 16:00`（无「全网」字样）。

---

### 8.2 P0-A / P0-C / P1 验收命令（已完成，2026-09-24 实跑取证见文末）

**验收命令（可独立复跑；`--` 参数以脚本实际 CLI 为准）**

```bash
# 0) 语法检查
python -m py_compile scripts/src/crypto_research/clients/coinglass_client.py \
                    scripts/bin/phase_backfill_liq_history.py

# 1) 套餐边界复验：期望 aggregated-history / history 返回 code=0；
#    多空比与 heatmap 仍为 404/401（若结果变化，说明套餐或文档已变更，须更新 §2.2）
python scripts/bin/phase_backfill_liq_history.py --probe

# 2) fix_069 迁移幂等：连跑两次，第二次应为 0 变更、无异常
#    （应用方式同既有 fix_* 迁移）

# 3) 回填 dry-run：期望打印请求数 = 1×币数（limit 取客户端上限 4500 @4h 全窗口 ⇒ 单请求/币）、
#    预计耗时（实测 527 次 × 2.5s ≈ 22.0 min）；不写库
python scripts/bin/phase_backfill_liq_history.py --dry-run --scope binance --days 180

# 4) 小样本真跑（2 币 × 30 天，单口径 binance）：期望落库 30×6 = 180 行/币 ⇒ 两币合计 360 行
python scripts/bin/phase_backfill_liq_history.py --scope binance --days 30 --symbols BTCUSDT,ETHUSDT

# 5) 续跑验证：中断后 --resume 应从游标继续，且不产生重复行（PK 幂等）
python scripts/bin/phase_backfill_liq_history.py --resume --scope binance --days 180 --json

# 6) 单测（新增护栏 + 既有回归）
python workbench/test_liq_history_scope.py     # 新增：口径隔离 / 缺失≠0 / 实时判定不读 history 表
python workbench/test_squeeze_fuel.py          # 既有：燃料判定回归
python workbench/test_squeeze_battle.py        # 既有：胜负判定回归

# 7) 标定脚本只读复跑（P0-C 之后）：期望口径 A/B 分表输出（B 侧只输出偏高幅度下界），缺样本时 rc!=0
#    ⚠️ 必须用 --days 1：它同时是**采样窗口**和**分母窗口**，且线上「/24h 成交额」是同量级口径。
#    大的 --days 在 `liquidation_snapshot` 尚不足 30 天时**只是白跑**（见下方 ⚠️ 段）。
python workbench/calib_squeeze_liq_thr.py --days 1 --json
```

**新增单测必须断言的不变量**（防口径漂移）

1. `biz.liquidation_history` 不出现在 `scan_daemon` / `squeeze` / `squeeze_fuel` 的任何 SQL 中；
2. `interval` 或 `exchange_scope` 不同**不得**参与同一次聚合（结构上由 PK 保证，测试再兜一层）；
3. 任一端缺失时，输出为 `None` 而**非 0**；
4. 口径 A 与口径 B 的比值不得被放进同一个分布/同一个分位计算。

**实测取证（2026-09-24，本机复跑）**

| 项 | 结果 |
|---|---|
| `py_compile`（calib / coinglass_client / phase_backfill_liq_history / scan_daemon / squeeze_fuel / 三个测试） | 全绿（EXIT=0） |
| `test_squeeze_span_judge.py` / `test_liq_history_scope.py` / `test_squeeze_fuel.py` / `test_squeeze_battle.py` | **48/48** / **39/39** / **99/99** / **143/143** |
| `phase_backfill_liq_history.py --probe` | 9 用例全符合 §2.2/§2.3（含 2 个反向对照）；`意外可用项 0 个` |
| `--dry-run --scope binance --days 180` | 宇宙 527、待处理 527、527 次 × 2.5s ≈ 22.0 min（按 `limit` 上限 4500 ⇒ 1 次/币） |
| `--resume --scope binance --days 30 --symbols BTCUSDT,ETHUSDT --json` | `resume 跳过 2 / 待处理 0 / rows_written 0` ⇒ **无重复行**（PK + 游标生效） |
| `apply_migration.py migrations/fix_069_liquidation_history.sql` ×2 | 两次均「执行成功」⇒ **幂等通过** |
| DB 实测 `biz.liquidation_history` | `binance`(BTCUSDT+ETHUSDT) 各 180 行、`all`(BTCUSDT+1000PEPEUSDT) 各 180 行；窗口 08-25 08:00 → 09-24 04:00 UTC（= 30×6） |
| `calib_squeeze_liq_thr.py --days 1`（`--json` 与文本） | 均 **EXIT=3**（`SAMPLE_UNUSABLE`，预期）；`scope_split` A 侧 n=54586 / B 侧 n=12；`cross_exchange_lower_bound.ok=false`（n_pairs=6<30）；**前置门路径零比率输出** |

> ⚠️ **命令 7 的窗口必须用 `--days 1`，`--days 30` 是用法错误而非性能缺陷**（2026-09-24 实测，**已证伪早前「`asset_klines` 5m 结果集过大」的归因**）：
>
> - **实测事实**：`biz.asset_klines` 5m × 30 天全表仅 **567,235 行**（1h 152,724 行）；`EXPLAIN (ANALYZE, BUFFERS)` 服务端执行 **7.15 s**，走 `Index Scan using idx_asset_klines_symbol_interval_ot`（每循环 0.049 ms）⇒ **索引与行数都正常**。
> - **真正瓶颈是表自身的数据量**：`biz.liquidation_snapshot` 全表 **368,373 行 / 527 币**，跨度仅 **2026-09-21 02:00 → 09-24 07:55 UTC = 3.25 天**。⇒ `--days 30` / `--days 7` 取到的是**同一批约 145k 行**（实测 days=1 → 55,721 行 / 144.9 s；days=7 → 144,090 行 / 455.9 s；days=30 → 145,148 行 / 604.2 s），并**必然**被 `span_sufficiency()` 判 `rc=3`（跨度 < `MIN_SPAN_DAYS=30`）⇒ 大窗口只是白耗 145 s→604 s 后给出**同一个 rc=3**。
> - **`--days` 的语义陷阱**：它**既是采样窗口也是分母窗口**。当请求窗口 > 表内实际跨度时，采样被表截断、`v.vol_win` 却仍是 30 天成交额合计 ⇒ 比率被系统性**低估约 `days/span ≈ 9.2×`**，且该偏差**不出现在任何输出字段里**（`--days` 只进 SQL 参数、不进输出）。⇒ calib 已追加**前置预检告警**，检测到该情形即在结果前打印提示（只告警、不改行为）。
> - **正式标定闸门**：`liquidation_snapshot` 自 2026-09-21 起积累，要满足 `MIN_SPAN_DAYS=30` 需等到 **≈2026-10-21 之后**（与设计文档 B6/A1 指向的 10 月中下旬一致），或改用 P1 回填的 `biz.liquidation_history`（4h 分段增量、180 天，结果集远小）。
> - **遗留疑点（诚实披露，未锁定）**：服务端 7.15 s vs 客户端 144.9–604.2 s 的 20–85× 差距根因**尚未确定**。已排除：索引缺失、结果集行数膨胀、单纯带宽（200k 窄行传输实测 3.6 s）。⇒ **不建议**在没有 30 天数据做验证的前提下重写为「服务端聚合」，那会改变统计口径。

---

## 9. 风险与注意

| 风险 | 说明 | 对策 |
|---|---|---|
| **采集停摆 ⇒ 早报展示陈旧值** | `liquidation_snapshot` 实测 7 天内出现过 3 次 >20min 空洞（最大 14.4h）；窗口过紧会静默丢行，过宽会展示陈旧值 | 窗口取 4h（只留代码常量）+ 脚注强制披露 **`数据截至 HH:MM`**；超窗则整行隐藏（不展示编造值） |
| **覆盖率护栏的分母与分子同源塌陷** | 分母 = 过去 24h 该表出现过的 `count(DISTINCT symbol)`，与分子同源自洽；代价是**持续性整体截断无法检出** | 已披露为已知局限；兜底需**跨天对比 `symbols_covered`**（后续增量，不在 P0-D） |
| **两套爆仓口径被混用**（最高风险） | 滚动窗口（snapshot）与分段增量（history）数值不可换算；混用即重演 P1-1 假 0 | 独立表 + PK 含 `interval`/`exchange_scope` + 表注释首行声明 + 单测不变量 1/2 |
| **回填被误接进实时判定** | 4h 粒度无法支撑 5m 窗口判定 | N2 写入方案 + 单测不变量 1 |
| **额度争抢** | 回填 24 req/min 与 daemon 共用同一 key | 留 6 req/min 余量；daemon 单次调用影响可忽略；若出现 429 优先降回填速度 |
| **作业跨执行窗口被中断** | 88 min ≫ 实例寿命实测 ≈74.6 min ⇒ 必然跨窗口；中断源为**实例定期回收 / 部署重启**（常态机制），**非**历史那几次异常停摆 | 游标表 + `--resume`（强约束） |
| **官方限频与实测不符** | 官方 30/min，实测曾 1.3 req/s 无 429 | 以官方 30/min 为预算，实测高值视为突发额度，不作为依赖 |
| **密钥单点**（§13 已披露） | `COINGLASS_API_KEY` 缺失时 `CoinGlassClient.__init__` 抛错 | 回填脚本独立读配置、失败不阻塞 daemon；**「coin-list 失败降级为跳过该轮」另立工单**（§11） |
| **符号取值域未知** | 请求侧直传合约码 ⇒ 不存在 `coin-list` 那种「抢码」问题；剩余风险只在 `aggregated-history` 的**返回值币种码写法**（如是否给 `1000PEPE`、是否带 `USDT`） | `--probe` 实测取值域后再定「币种码 → 合约码」映射方向；小样本验收含 `1000PEPEUSDT` 等前缀币 |
| **演示用的「全所」口径需拼交易所名** | 文档无 `all` 快捷值 | `--probe` 首跑实测；失败则先只回填 Binance 口径（不影响 G2/G3） |

---

## 10. 落地后需同步的文档（防文档-代码分叉）

| 文档/位置 | 需改内容 | 状态 |
|---|---|---|
| `README.md` 七维度数据架构「3 衍生品」行 + 大盘早报邮件章节 | 补 24h 爆仓概况及其口径（池内 N 标的 / 滚动窗口 / 只展示不进分） | ✅ 已完成 |
| `workbench/macro_market.py` 模块注释与常量注释 | 记录 `liquidation_24h` 口径与「不进分」约束，防止后续被误接进子分 | ✅ 已完成（代码内注释） |
| `brief_data_model.py` `_WARNING_FIELDS` | `M2_liquidation` 缺失归入**一般降级**（可选辅助模块） | ✅ 已完成 |
| `04_架构与代码方案/盘面异动扫描系统设计方案.md` §3.1 / §10.2 数据源表 | 补 `liquidation_history` 行，标注「分段增量，仅标定/回测」 | ✅ 已完成（2026-09-24：§3.1 覆盖旧「未采用」行；§10.2 与 §10.3 已补行并追加 `fix_069`） |
| 同上 §10.5 口径披露 | 增列口径 B（同源同窗口 4h，**仅给偏高幅度下界**），并明确 A/B 不可换算 | ✅ 已完成（2026-09-24：改为「口径 A / B 并行」表 + 4 条要点） |
| 同上 §12.1 → **B16 关闭** | 「爆仓细粒度历史未接入」改为已完成，注明粒度与保留期 | ✅ 已完成（2026-09-24） |
| 同上 §12.1 A3 / B11 / C9 | 「无法标定/无样本外」改为「样本已具备，待跑标定」 | ✅ 已完成（2026-09-24） |
| 同上 §13 CoinGlass 依赖项 | 更新额度使用情况与回填预算 | ✅ 已完成（2026-09-24：补官方 30/min、288 次/天（0.7%）、回填 1054/2108 次 ≈ 88 min、游标 + resume 依据） |
| `clients/coinglass_client.py` 模块 docstring | 增封装 `liquidation_aggregated_history()` 等方法与实测边界 | ✅ 已完成（2026-09-24） |
| `AGENTS.md` | 追加本轮约束条目（口径分离、回填节流、游标续跑、链上源选 CM 而非 CoinGlass） | ✅ 已完成（2026-09-24，本方案同轮提交） |

---

## 11. 待确认事项

1. ~~P0-B 是否真的需要 12h/24h 多空分列~~ → **已定：只补 24h 两列**，随 `fix_068` 上线（P0-D 的方向行依赖它）；12h 分列当前无消费点，**不补**。
2. **早报的覆盖范围**：现在只能给「池内 N 个标的合计」，是否要扩到**真·全市场**（那就得走 `liquidation/aggregated-history`，即 P1/P2 路线）？
3. ~~**覆盖率护栏的比例阈值**：取值走标定流程，**不在文档留数字**~~ → **已标定（2026-09-23）**：取证见 §4.7「三个坑」第 3 条与 §8.1 实测记录；取值只留代码常量，**仍不在文档留数字**。
4. ~~`--scope all` 的全交易所列表获取方式（`supported-exchanges` 动态拼 vs 官方是否接受 `Binance,OKX,...` 全量），需 `--probe` 实测~~ → **已定（2026-09-24 `--probe` 实测）**：`aggregated-history` 的 `exchange_list` **必填**（缺失 ⇒ `code=400`），**无 `all` 快捷值** ⇒ 由 `supported-exchanges`（实测 `n=11`）动态拼接全量。同批实测：`symbol` 须传**币种基码**（传合约码 `BTCUSDT` ⇒ `code=0` 但**静默 0 行**）。
5. `coin-list` 失败时 `scan_squeeze` 整轮失败（§13 密钥单点）是否要降级为「跳过爆仓维、其余照跑」——**另立工单**。
6. ~~**早报展示位版式**：现有 `research.html` 的衍生品指标网格是否足够承载「四档 + 多空方向」~~ → **已定**：邮件侧在「3衍生品」内新增**一行**（合计 + 多空方向 + `1h/24h` 占比 + 口径脚注），由 `_render_liquidation_row()` 渲染；`research.html` 网格版式**本轮不改**（网页侧随后续维扩再定）。