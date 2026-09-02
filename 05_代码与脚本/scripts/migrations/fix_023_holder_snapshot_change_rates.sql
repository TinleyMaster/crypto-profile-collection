-- fix_023: onchain_holder_snapshot 变化率字段补全
-- 对应工单 MEME-01（schema drift 修复 + 变化率字段补写）
-- 分支 A（prod 无列）执行此 ALTER；分支 B（列已存在）跳过

BEGIN;

ALTER TABLE biz.onchain_holder_snapshot
  ADD COLUMN IF NOT EXISTS holder_change_7d          INTEGER,
  ADD COLUMN IF NOT EXISTS holder_change_30d         INTEGER,
  ADD COLUMN IF NOT EXISTS whale_balance_change_7d_pct  NUMERIC(6,2),
  ADD COLUMN IF NOT EXISTS whale_balance_change_30d_pct NUMERIC(6,2);

COMMIT;
