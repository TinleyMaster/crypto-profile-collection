# 工作台：OBM + CM 双源链上指标入库+分位计算

> 工单：FEAT-OBM-CM-ONCHAIN-PCTL
> 状态：✅ 已执行完成

---

## 执行结果

| 步骤 | 状态 | 说明 |
|------|------|------|
| 1. 建表 | ✅ | `biz.obm_btc_daily` 创建成功 |
| 2. OBM入库 | ✅ | 23个指标，148,217行 |
| 3. CM入库 | ✅ | 5个币种（btc/eth/doge/xrp/ada），22,912行 |
| 4. 分位视图 | ✅ | `obm_percentile_full`（135,042行）+ `cm_onchain_percentile_full`（21,631行） |
| 5. 验证 | ✅ | 通过 |

---

## 验证结果

### OBM 验证
- ✅ 指标数量：23/23
- ✅ 最大日期：2026-08-24
- ✅ Supply 单调性：无违规

### CM 验证
- ✅ 币种分布：ada(3,166) / btc(6,351) / doge(4,551) / eth(3,952) / xrp(4,892)
- ✅ bnb/sol 已排除
- ✅ BTC MVRV 锚点：
  - 2018-12-15: 0.690
  - 2021-11-10: 2.721
  - 2022-11-21: 0.778

### 分位视图验证
- ✅ `obm_percentile_full`: 135,042 行
- ✅ `cm_onchain_percentile_full`: 21,631 行

---

## 已创建文件

| 文件 | 用途 |
|------|------|
| `sql/biz/obm_btc_daily.sql` | 建表 DDL |
| `bin/ingest_obm_btc_daily.py` | OBM 入库脚本 |
| `bin/cm_range_consolidate.py` | CM 范围收缩 |
| `sql/biz/obm_percentile.sql` | 分位视图 |
| `bin/validate_obm_onchain.py` | 验证脚本 |
