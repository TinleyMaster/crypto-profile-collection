# 工作台：OBM + CM 双源链上指标入库+分位计算

> 工单：FEAT-OBM-CM-ONCHAIN-PCTL
> 状态：代码已落地，待手动执行

---

## 执行顺序

```
1. 建表 → 2. OBM入库 → 3. CM收缩 → 4. 建视图 → 5. 验证
```

---

## 步骤 1：建表

```bash
psql -f 05_代码与脚本/scripts/sql/biz/obm_btc_daily.sql
```

验证：
```sql
\d biz.obm_btc_daily
-- 应看到 23 指标的长表结构，含 source_cutoff 列
```

---

## 步骤 2：OBM 入库

```bash
cd 05_代码与脚本/scripts
python bin/ingest_obm_btc_daily.py --data-dir ../../data_external/obm/
```

验证：
```sql
SELECT COUNT(DISTINCT metric_name) FROM biz.obm_btc_daily;
-- 应返回 23

SELECT MAX(metric_date) FROM biz.obm_btc_daily;
-- 应返回 2026-08-24
```

---

## 步骤 3：CM 范围收缩

```bash
cd 05_代码与脚本/scripts
python bin/cm_range_consolidate.py --execute
```

验证：
```sql
SELECT cm_symbol, COUNT(*) FROM biz.cm_asset_onchain_daily GROUP BY cm_symbol;
-- 应仅有 btc,eth,doge,xrp,ada，无 bnb/sol
```

---

## 步骤 4：建视图

```bash
psql -f 05_代码与脚本/scripts/sql/biz/obm_percentile.sql
```

验证：
```sql
SELECT * FROM biz.obm_percentile_full WHERE metric_name = 'obm_mvrv_btc_daily' 
  AND metric_date = '2021-11-10';
-- pct_full 应 ≥ 90（HIGH）
```

---

## 步骤 5：验证

```bash
cd 05_代码与脚本/scripts
python bin/validate_obm_onchain.py
```

---

## 文件清单

| 文件 | 用途 |
|------|------|
| `sql/biz/obm_btc_daily.sql` | 建表 DDL |
| `bin/ingest_obm_btc_daily.py` | OBM 入库脚本 |
| `bin/cm_range_consolidate.py` | CM 范围收缩 |
| `sql/biz/obm_percentile.sql` | 分位视图 |
| `bin/validate_obm_onchain.py` | 验证脚本 |
