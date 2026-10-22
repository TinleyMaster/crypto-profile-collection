---
name: token-tracker-sop
description: 代币埋伏追踪 SOP 的持续监控——跑监控日报、看观察池哪些开关接近触发、判读 pending/digest、给池子加币加开关、验证监控覆盖。触发词：追踪观察池、跑监控日报、埋伏SOP监控、观察池有什么值得看、加开关、漂移检测。单币一次性深度投研请用 `crypto-research`，本 skill 只做持续跟踪。
agent_created: true
---

# 代币埋伏追踪 SOP（持续监控）

**本 skill 是薄封装**：抓取/判读/落库全部复用 `/opt/crypto-research` 的既有脚本，不重复造轮子。

## 何时用

- "跑监控日报" / "观察池有什么值得看"
- "哪个开关接近触发" / 判读 `pending` / `digest`
- 给池子**加币 / 加开关**
- 验证监控覆盖（哪些开关没数据源）

**边界**：单币一次性深度投研 → `crypto-research`。本 skill 只做**持续监控**。

---

## 运行环境

```bash
cd /opt/crypto-research/05_代码与脚本/scripts     # 宿主机
# 容器内：/app/scripts
```

```bash
# 主命令（重建版，替代已丢失的 monitor.py / sop.py / send_pool_digest.py）
python bin/sop_monitor.py --digest        # 日报 → data/sop/digest/YYYY-MM-DD.md
python bin/sop_monitor.py --pending       # 只看接近触发 + 已触发
python bin/sop_monitor.py --json          # 结构化（程序化消费）
python bin/sop_monitor.py --token UNI     # 单币
python bin/sop_monitor.py --dry-run       # 只读表不抓网络（体检用，验证表结构/解析）
```

> ⚠️ 原 `monitor.py` / `sop.py` / `build_sop_db.py` / `load_to_prod.py` /
> `send_pool_digest.py` / `cloud_digest.py` 在部署机上**已不存在**（只在旧 Windows 工作区）。
> 现以 **Postgres `sop.sop_switches` 表为唯一真相**，`sop_monitor.py` 直接读库、实时抓数、出报告。
> 不要再去找那些脚本，也不要再维护 CSV / SQLite 副本。

---

## 核心方法论

暴涨 = 市场预期的重定价。左侧埋伏找的是「**当前价格没反映、但基本面开关拨动会触发重估**」的变量。

```
新币报告 → ① 淘汰筛(7 红线) → 不合格剔除 / 合格进池
             ↓
          ② 提炼「暴涨开关」(每币不同，反向推导法) → 写成可监控指标
             ↓
          ③ 每日/每周监控 → 开关拨动 + 价格没反映 → 左侧埋伏
```

**关键认知**：你不需要预测暴涨，只需要在开关拨动时知道"该看一眼"。
`triggered` / `pending` **只是提醒，不是买入信号**。下单前必须重抓实时价——报告有时滞。

---

## 单一真相：`sop.sop_switches`

**改开关 = 改这张表，不要手改任何派生文件。**

```sql
-- 表结构
id, token, switch_id, switch_type, narrative, indicator, source,
auto, freq, threshold, data_kind, data_ref, trigger_json, watch_json
```

| 字段 | 含义 |
|---|---|
| `token` | 币种 |
| `switch_id` | 唯一编号（币+类型+序号，如 `AERO-C1`） |
| `switch_type` | A=催化剂事件 / B=基本面拐点 / C=结构反转 |
| `narrative` | 一句话逻辑 |
| `indicator` | 具体指标（人读） |
| `source` | 数据源（人读） |
| `auto` | 自动 / 半自动 / 人工 |
| `freq` | 日 / 周 / 事件 |
| `threshold` | 自然语言触发条件（**仅供人读，机器不解析**） |
| `data_kind` | 抓取器类型（下表） |
| `data_ref` | 抓取参数（合约码 / slug） |
| `watch_json` | `[{"kind":..., "symbol"/"slug":...}]`，一开关可挂多源 |
| `trigger_json` | **结构化条件（机器判读的唯一依据）** |

### `trigger_json` 格式（判读靠它，不靠 threshold）

```json
{"logic": "and",
 "conds": [{"metric": "RSI", "op": "<", "val": 35},
           {"metric": "PX",  "op": ">", "ref": "MA20"}],
 "note_on": "RSI 超卖且站回 MA20（转强信号）"}
```

- `logic`：`and` / `or`（缺省 and）
- `op`：`<` / `>`（实测全库仅此两种；代码也支持 `<=` `>=` `==`）
- 操作数：`val`（字面量）或 `ref`（引用另一指标）
- **实测词表**：`metric` ∈ `FUND` / `LSR` / `PX` / `RSI` / `TVL`；`ref` ∈ `MA20` / `MA50`

**`trigger_json` 为空 = 人工/事件项**，进报告的日历表，由人盯。

---

## 数据源注册表（`data_kind`）

| kind | 参数 | 返回指标 | 源 | 脚本支持 |
|---|---|---|---|---|
| `binance_kline` | `symbol="XXXUSDT"` | `PX` / `RSI14` / `MA20` / `MA50` | Binance fapi | ✅ |
| `binance_lsr` | `symbol` | `LSR` | Binance fapi | ✅ |
| `binance_funding` | `symbol` | `FUND`（当周期费率） | Binance fapi | ✅ |
| `defillama_tvl` | `slug` | `TVL` | DefiLlama | ✅ |
| `binance_oi` | `symbol` | `OI` | Binance fapi | ⬜ |
| `coinglass_oi` | `symbol` | 跨所聚合 OI | CoinGlass | ⬜ |
| `coinglass_liquidation` | `symbol` | 24h 多/空清算 | CoinGlass | ⬜ |
| `defillama_fees` | `slug` | 24h fees | DefiLlama | ⬜ |
| `alternative_fng` | — | 恐贪指数 | alternative.me | ⬜ |
| `github` | `repo` | last_commit | GitHub | ⬜ |
| `unlock_next` | `symbol` | 最近解锁日+占流通比 | 读 `biz.asset_unlock_event` | ⬜ |
| `tokenomics_burn` | `symbol` | 销毁机制 | 读 `biz.asset_tokenomics` | ⬜ |
| `catalyst_gov` | `symbol` | 近 30d 治理事件 | 读 `biz.asset_catalyst` | ⬜ |

⬜ = 未接入。这些开关会进报告「**待接入数据源**」段并**明确列出**，
**绝不会**被当成"未触发"——这是刻意设计，避免静默漏判。

### DefiLlama slug 陷阱（实测踩过）

`watch_json` 里的 slug 可能是脏的：

| 写的 | 实际 | 情况 |
|---|---|---|
| `aerodrome` | `aerodrome-v1` / `-slipstream` / `-ignition` | 3 个候选，直连 400 |
| `ondo` | `ondo-yield-assets` 等 6 个 | 直连 400 |
| `chip` | **无对应协议** | LuckyChip / Chips 都不相关 |

脚本会在直连失败时反查 `/protocols`，**多候选时不自动选**（合并多个池会算错 TVL），
改为在「数据源备注」里报歧义。看到歧义 → 需人工确认真实 slug 后回写 `watch_json`。

---

## 判读口径（"值得看一眼"，非买入信号）

| 指标 | 信号 | 含义 |
|---|---|---|
| RSI < 30 | 超卖 | 可能接近底部（等站回 MA20 确认） |
| RSI > 70 | 超买 | 别追高 |
| LSR < 1.1 | 空头占优 | 潜在空头回补 |
| LSR > 1.6 | 多头拥挤 | 洗盘/回撤风险 |
| FUND > 0.05%/8h | 多头拥挤 | 警惕费率过热 |
| TVL 显著高于基线 | 业务扩张 | B 型开关向好 |

**框架 4 条回测修正（已生效）**：
1. 叙事/实用币分型筛（别用同一把尺）
2. B 触发必过 C 稀释门槛（解锁墙 / FDV 流通比）
3. C 开关降级为**择时**，不是方向判断
4. 费用开关查收入驱动 vs 国库驱动

---

## 加新币 / 加开关

1. **过 7 条淘汰红线**：R1 叙事崩塌 / R2 归零尾部 / R3 薄盘控盘 / R4 价值真空 / R5 交易所去优 / R6 评分≤4.5且看空 / R7 监管致命。
   纯叙事/催化剂币走「叙事专属屏」，**不进 R3/R4 硬剔**。
2. **定类型**：事件型 / 估值型 / 协议型 / 叙事型 / 微盘型。
3. **反向推导开关**（风险项反面向好 = 开关）：A 事件 / B 基本面拐点 / C 结构反转，每类挑 1 个。
4. **写库**（`sop.sop_switches` 插一行，幂等键 `switch_id`）。
5. **验证**：`python bin/sop_monitor.py --token {SYM}` 确认新开关能取到数、判读不报错。

写库示例：
```sql
INSERT INTO sop.sop_switches
  (token, switch_id, switch_type, narrative, indicator, source, auto, freq,
   threshold, data_kind, data_ref, watch_json, trigger_json)
VALUES
  ('UNI','UNI-C9','C','RSI 超卖且站回 MA20','RSI14/MA20','交易所K线','自动','日',
   'RSI<35 且 PX>MA20','binance_kline','UNIUSDT',
   '[{"kind":"binance_kline","symbol":"UNIUSDT"}]',
   '{"logic":"and","conds":[{"metric":"RSI","op":"<","val":35},{"metric":"PX","op":">","ref":"MA20"}],"note_on":"超卖修复"}')
ON CONFLICT (switch_id) DO UPDATE SET
  narrative=EXCLUDED.narrative, threshold=EXCLUDED.threshold,
  data_kind=EXCLUDED.data_kind, data_ref=EXCLUDED.data_ref,
  watch_json=EXCLUDED.watch_json, trigger_json=EXCLUDED.trigger_json;
```

---

## 铁律

- **不主动碰 biz/core 业务表**。本 skill 只读 `sop.*` + 业务表只读查询，**不写**。
- **凭证不进对话、不进源码**。`DATABASE_URL` / `COINGLASS_API_KEY` 只从 `scripts/.env` 读。
  **绝不打印 `get_settings()` 对象**——它会带出全部密钥（`.env` 已被 gitignore，但对话记录会留）。
- **单一真相是 `sop.sop_switches` 表**。不要手改任何 CSV / SQLite 副本，也不要试图"再建一套"。
- **`pending` / `triggered` 不是买入信号**。下单前重抓实时价。
- **扩池需拍板**。库里现为 **136 开关 / 28 币**；如需加币，先跟用户确认，不要擅自扩。
- 报告里的「待接入数据源」段是**未实现抓取器**的诚实披露，不要为"清空它"而伪造数据。

---

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 大量「待接入数据源」 | 该开关的 `data_kind` 无 fetcher | 看报告列出的 kind，按需实现或改用现有 kind |
| 「数据源备注」报 slug 歧义 | `watch_json` 的 slug 脏 | 人工确认真实 slug 后回写 |
| 大量「人工/事件项」 | `trigger_json` 为空 | 正常。若要机器判读，补 `trigger_json` |
| 判读全 `no_data` | 抓取失败 / 网络 | 看 `--json` 的 `fetch_errors` |
| DefiLlama 400 | slug 不对 | 见上表 |

---

## 日报结构

`data/sop/digest/YYYY-MM-DD.md`：

1. **总览**：已触发 / 接近触发 / 未触发 / 数据缺失 / 人工项 / 待接入 / 合计
2. **🟢 已触发**：开关 + 条件求值（逐条 ✅/✗ 带实际数值）
3. **🟡 接近触发**：同上
4. **⚪ 数据缺失**：指标缺值（**不猜**）
5. **🔍 数据源备注**：slug 歧义 / 取到但无值
6. **📅 人工/事件项**：需自己盯日历
7. **🔧 待接入数据源**：未实现 fetcher 的 kind 列表
8. **⚠️ 抓取失败**：异常明细

报告头部固定声明：**"本报告只是「值得看一眼」，不是买入信号。"**
