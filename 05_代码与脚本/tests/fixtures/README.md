# tests/fixtures/attr_gold.jsonl · 归属评测集（P3-B / 工单 P3-ATTR-001）

**状态：候选集 v0（未定稿）** —— `label_source='review'` 与 `'auto_suspect'` 的条目
必须人工确认后才算 gold。**当前只可对 `label_source='auto'` 的条目计分。**

## 文件格式（每行一个 JSON）

| 字段 | 说明 |
|---|---|
| `cid` | `biz.asset_catalyst.catalyst_id` |
| `bucket` | `crypto_gap` / `tradfi_only` / `no_pairs` / `unknown_mixed` |
| `source_code` | 来源 |
| `published_at` | 发布时间 |
| `title` / `body_snippet` | 标题 / 正文前 300 字 |
| `related_pairs` | 源给的交易对（可能为空） |
| `label` | `[asset_id…]` \| `["__non_crypto__"]` \| `null` |
| `label_symbols` | 标签对应的 symbol（便于人读） |
| `label_source` | `auto`（可信）\| `auto_suspect`（疑污染，需人看）\| `review`（未标注） |
| `confidence` | `high` / `low` |
| `needs_review` | 是否需人工确认 |
| `hint` | 组织词候选 / 降级原因 |

## 标签语义

- `label = [asset_id…]`：期望连到这些资产（召回目标）
- `label = ["__non_crypto__"]`：期望**连不上**（精度目标，任何预测都算 FP）
- `label = null`：未标注，不参与计分

## 分桶与策略

| bucket | 含义 | 标注策略 |
|---|---|---|
| `crypto_gap` | 有真加密交易对却 NULL | auto 正样本；**无加密语境者降级 auto_suspect** |
| `tradfi_only` | 全 TradFi 交易对 | auto 负样本；**正文含加密语境者降级 auto_suspect** |
| `no_pairs` | 源未给交易对（主病灶） | review（附组织词 hint） |
| `unknown_mixed` | 全部基元未知 | review |

## ⚠️ 已知污染（实测教训）

自动标签会把**股票 ticker 撞名加密资产**的行误标为正样本 —— 实测
`cid 9341`（NexGen Energy 美股，pairs=`["NEX"]`）因 `NEX` 撞名被标成 crypto_gap。
故引入 `has_crypto_context()` 护栏：无加密语境即降级 `auto_suspect`。
**148 条 auto 中 78 条（53%）被降级** —— 说明这个污染面很大，也说明**人工抽检不可省**。

## 复算命令

```bash
# 重新采样（会覆盖本文件）
python scripts/bin/attr_gold_sample.py

# 用当前 linker 出 baseline 指标
python scripts/bin/attr_eval.py --predict

# 只评某一桶
python scripts/bin/attr_eval.py --predict --bucket crypto_gap
```

## Baseline（2026-10-16，当前 linker，70 条可信标签）

| 指标 | 值 |
|---|---|
| precision | **0.111** |
| recall | **0.029** |
| F1 | **0.046** |
| TP / FP / FN | 1 / 8 / 33 |
| 负样本保持为空 | **45/45 = 100%** |

> 任何 P3-C 召回改动必须报告本表，且 **F1 不得劣于 0.046、负样本保持率不得低于 100%**。
