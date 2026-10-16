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
