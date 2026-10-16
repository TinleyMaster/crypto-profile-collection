# tests/fixtures/attr_gold.jsonl · 归属评测集（P3-B / 工单 P3-ATTR-001）

**状态：v1（半自动定标已完成）**。348 条候选经 `attr_gold_label.py` 定标后：
**可评分 329（正 30 / 负 299）· 跳过 19**。跳过的 19 条见 `attr_gold_review.md`，需人工确认。

> ⚠️ **桶标签不可信**：分桶基于 `core.asset` 的 symbol 匹配，而 symbol 撞名极普遍
> （实测 `SHY`→`Shytoshi Kusama`、`NEX`→`Nexus`、`B3`/`ACN`/`C`/`V` 均为股票 ticker），
> 导致大量**非加密新闻被错分进 `crypto_gap`**。故标签一律**按正文判读**（人工判读覆盖表）。

## 文件清单

| 文件 | 作用 |
|---|---|
| `attr_gold.jsonl` | gold 集（定标后） |
| `attr_gold_overrides.json` | **人工判读覆盖表**（pos / review / neg），标签的事实来源 |
| `attr_gold_review.md` | 待人工复核清单（19 条，当前不计分） |

## 文件格式（每行一个 JSON）

| 字段 | 说明 |
|---|---|
| `cid` | `biz.asset_catalyst.catalyst_id` |
| `bucket` | `crypto_gap` / `tradfi_only` / `no_pairs` / `unknown_mixed`（**仅作分层，不当标签**） |
| `source_code` / `published_at` | 来源 / 发布时间 |
| `title` / `body_snippet` | 标题 / 正文前 300 字 |
| `related_pairs` | 源给的交易对（可能为空） |
| `label` | `[asset_id…]` \| `["__non_crypto__"]` \| `null` |
| `label_symbols` | 标签对应的 symbol（便于人读） |
| `label_source` | `ai_review`（已定标，可计分）\| `review`（存疑，跳过） |
| `needs_review` | 是否需人工确认 |
| `hint` | 判读备注 / `[default_neg:<bucket>]` 等溯源标记 |

## 标签语义

- `label = [asset_id…]`：期望连到这些资产（**召回目标**）
- `label = ["__non_crypto__"]`：期望**连不上**（**精度目标**，任何预测都算 FP）
- `label = null`：未标注 / 存疑，**不参与计分**

## 定标流程（半自动）

```bash
# 1) 采样（只读 DB，产候选集；会覆盖 attr_gold.jsonl）
python scripts/bin/attr_gold_sample.py

# 2) 半自动定标（只读 DB，按覆盖表定标；打印全部“默认归负”行供复核）
python scripts/bin/attr_gold_label.py            # 加 --dry 只打印
#    → 人工只需处理 attr_gold_review.md 的存疑清单

# 3) 评测（--predict 用当前 linker 出 baseline）
python scripts/bin/attr_eval.py --predict
python scripts/bin/attr_eval.py --predict --bucket crypto_gap
```

**定标优先级**（`attr_gold_label.py`）：① `pos` 显式正 → ② `review` 存疑跳过 →
③ `neg` 显式负（纠正撞名假正样本）→ ④ 既有 `auto` 按严格口径重解析 →
⑤ 其余按桶**默认归负**（逐条打印，杜绝静默默认）。

**严格解析**：`pos` 的 ticker → `core.asset` 中 symbol 完全相等、`market_cap_rank IS NOT NULL`、
非合成资产中 **rank 最小者（取 1 个）**。避免把 `Bitcoin AI` / `Wrapped Solana` 等同名异物算进标签。

## ⚠️ 已知污染（实测教训，务必保留）

1. **股票 ticker 撞名**是本案核心病灶。既有 `auto` 正样本中亦有污染，已人工纠正 4 条：
   `29937`(B3=巴西证交所)、`7967`(ACN=埃森哲)、`35116`(SHY=美债ETF)、`18022`(C=花旗)。
2. 初版 `has_crypto_context()` 护栏**不足以拦撞名**——标题含“代币化/加密资产/稳定币”等词即放行，
   故 5 类假正样本漏过。⇒ **护栏须基于“具体可交易资产事件”，而非词面加密语境**。

## Baseline（2026-10-16，当前 linker，329 条计分：正 30 / 负 299）

| 指标 | 值 |
|---|---|
| precision | **0.042** |
| recall | **0.033** |
| F1 | **0.037** |
| TP / FP / FN | 1 / 23 / 29 |
| 负样本保持为空 | **289/299 = 96.7%** |

> ⚠️ 与 v0（70 条时 F1 0.046）不可直接比较：v1 扩容到 329 条后，**更多负样本暴露了 linker 的假阳性**，
> 精度由 0.111 降至 0.042。**v1 为 P3-C 改动的公平门槛**（同集对比）。

**linker 两类硬伤（v1 实测）**

| 类型 | 表现 | 样例 |
|---|---|---|
| 假阳性 A（撞名） | 商品/股票永续对撞名加密资产 | `XAUUSDT→XAU`、`XPDUSDT→XPD`、`SNPSUSDT→SNPS`、`HTZ→ATWO` |
| 假阳性 B（项目名通道吐垃圾） | 宏观标题被组织词正则乱匹配 | `Bank Indonesia… → APEPE,SXT,TPTU`、`Grok Bot… → BTT,SUN,PUNDIX` |
| 假阴性（召回≈0） | 真加密对**全部漏连** | `CT`/`W`/`CASHCAT`/`PUFFER`/`SOL`/`G`/`NEAR`/`AEON`/`DGAI`/`DRV` |

> 任何 P3-C 改动必须报告本表，且以 **同集** 对比：F1 不得劣于 0.037、负样本保持率不得低于 96.7%。

---

## Baseline v2（2026-10-16，P3-C 落地后 · 同集 329 条）

| 指标 | v1（改前） | **v2（改后）** | 变化 |
|---|---|---|---|
| precision | 0.042 | **1.000** | ↑ 23.8× |
| recall | 0.033 | **0.867** | ↑ 26.3× |
| **F1** | 0.037 | **0.929** | **↑ 25.1×** |
| TP / FP / FN | 1 / 23 / 29 | **26 / 0 / 4** | FP 清零 |
| **负样本保持为空** | 96.7% | **299/299 = 100.0%** | ✅ |

分桶（v2，全部桶 FP=0）：`crypto_gap` TP=25 FN=4 · `no_pairs` TP=1 · `tradfi_only` 50/50 · `unknown_mixed` 45/45。

### P3-C 修复清单（`linker.py` + `pipeline.py`）

| 编号 | 修复 | 依据 |
|---|---|---|
| **B1** | 歧义 gate 改用 `has_strict_crypto_context`（先剥离 pairs 字面，再查**严格**词表） | 原 `has_crypto_context` 被 pairs 回响 +「现货/合约/交易所/spot」等 TradFi 高频词**恒放行**；剔除「现货×2、交易所、合约、spot」→ 拦 5 行 |
| **B1b** | 严格词表**剔除交易所名/信源名**（binance/okx/bybit/… + 币安 + coindesk/cointelegraph/…） | 「Bybit 新增新思科技（SNPS USDT）股票永续」靠 `Bybit` 过闸 → 误连 SNPS（cid 33231） |
| **B1c** | 严格词表**剔除 RWA 语义** `代币(?!化)` / `token(?!i[sz])` | 「巴西证交所 B3 推出**代币化**资产平台」（cid 29937）、「使用**代币化**美国国债」（cid 35116）说的是 RWA 上链，非某个币 |
| **B2** | `_EQUITY_TICKER_COLLISIONS` 补 `SNPS` / `RTX` / `B3` | 评测集实测 FP |
| **B3** | 项目名通道：提取正则**去 `re.I`**（只认首字母大写词）+ `_ORG_WORD_STOPWORDS` 停用词 | `and`/`new`/`two` 曾成活候选 → 命中 `Ape and Pepe`/`Sun [New]`/`Arena Two` |
| **B4** | 项目名通道 `canonical_name` 匹配改 **`^word` 首词**，`LIMIT 3 → 1` | `Gravity` 命中 `Gravity`+`Gravity Finance`、`Cycle` 命中 `Cycle`+`Burn Cycle`+`$FYC` → 8 个 FP |
| **C1a** | `extract_base_symbol` 支持**裸 symbol**（`CT`/`CASHCAT`/`NEAR`） | 原一律返回 `None` ⇒ 14 条正样本漏连 |
| **C1a'** | **弱信号护栏** `_filter_bare_pairs`：裸 symbol 或单字符 base 须**标题独立 token** 佐证 | 放开裸 symbol 后，美股/美债 ticker（SHY/B3/GM/MS/ACN/PEP/V/C/M/BLK/…）全线误连（50 行） |
| **C1b** | 单字符 base 合法（`WUSDT→W`），但 quote 拆分区分稳定币/币本位 | `WBTC` 曾被错拆成 `W`+`BTC`（Wormhole）；`W`/`G`/`Q`/`O` 是真实币 |
| **C1c** | 严格 gate **白名单优先** | `SOLUSDT` 因 `_EQUITY_TICKER_COLLISIONS` 交集被 gate 误杀 |
| **C1d** | `XXX/USDT` 形态纳入 cashtag 提取 | cid 19112「上线 CT/USDT 现货交易」漏连 |
| **C1e** | `asset_source_map` **空源记忆**短路 | 实测该表**无任何以 ticker 为 key 的源**（cg=slug / cmc/dl=数字 id / cm=24 行小写）⇒ `source_hint='binance'` 恒空；原工单「改 source_hint 回落」**判为无解** |

**残余 FN 4 条（已知、可接受）**：`WBTC`（真 wrapped 被名称过滤排除）· `DGAI`×2（标题写项目名 `DGrid AI` 而非 ticker）· `牛来`（中文 ticker）。

**回归测试**：`python tests/test_attr_guards.py` → **34/34 全绿**（注入用例覆盖 A 商品同名 / B 美股撞名 / C 强信号召回 / D 门禁不破）。

